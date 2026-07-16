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


# ── divergent bare 슬롯 dir backfill 마이그레이션 (T-0201) ────────────────────

def _bare_slot_dir(hf, n: int = 1) -> Path:
    return hf._tmp / ".project_manager" / ".local" / "slots" / str(n)


def test_pm_state_path_backfills_divergent_bare_slot_dir(hf):
    """`slots/<N>`(divergent bare dir) 존재 + canonical 부재 → canonical 로 이동(backfill)."""
    _make_single_self_host(hf._tmp)  # _auto_slot → ("project_manager", 1).
    bare_dir = _bare_slot_dir(hf, 1)
    bare_dir.mkdir(parents=True)
    (bare_dir / "pm_state.md").write_text("divergent bare 슬롯 내용", encoding="utf-8")

    resolved = hf._pm_state_path()

    sp = _slot_path(hf, "project_manager_1")
    assert resolved == sp
    assert sp.exists() and sp.read_text(encoding="utf-8") == "divergent bare 슬롯 내용"
    assert not bare_dir.exists(), "backfill 은 bare dir 을 이동(정리) — 원본 잔재 없어야 함."


def test_pm_state_path_backfill_skips_when_canonical_already_exists(hf):
    """canonical dir 이 이미 있으면 bare dir 은 건드리지 않는다(안전 — 유실 방지)."""
    _make_single_self_host(hf._tmp)
    sp = _slot_path(hf, "project_manager_1")
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text("canonical 기존 내용", encoding="utf-8")

    bare_dir = _bare_slot_dir(hf, 1)
    bare_dir.mkdir(parents=True)
    (bare_dir / "pm_state.md").write_text("bare 잔재 내용", encoding="utf-8")

    resolved = hf._pm_state_path()

    assert resolved == sp
    assert sp.read_text(encoding="utf-8") == "canonical 기존 내용", "canonical 을 덮어쓰지 않는다."
    assert bare_dir.exists(), "canonical 이 이미 있으면 bare dir 은 그대로 둔다(유실 방지)."


def test_pm_state_path_backfill_noop_when_no_divergent_dir(hf):
    """divergent bare dir 이 없으면 backfill 은 no-op(정상 케이스·회귀 없음)."""
    _make_single_self_host(hf._tmp)
    resolved = hf._pm_state_path()
    sp = _slot_path(hf, "project_manager_1")
    assert resolved == sp
    assert not sp.exists()


def test_pm_state_path_dry_run_does_not_backfill(hf):
    """migrate=False(dry-run/진입부 읽기) → backfill 안 함(부작용 0 계약 보존)."""
    _make_single_self_host(hf._tmp)
    bare_dir = _bare_slot_dir(hf, 1)
    bare_dir.mkdir(parents=True)
    (bare_dir / "pm_state.md").write_text("divergent bare 슬롯 내용", encoding="utf-8")

    resolved = hf._pm_state_path(migrate=False)

    sp = _slot_path(hf, "project_manager_1")
    assert not sp.exists(), "dry-run 은 backfill 이동을 하지 않는다."
    assert bare_dir.exists() and (bare_dir / "pm_state.md").exists(), "bare dir 그대로 보존."
    # legacy·slot 둘 다 부재인 상태의 migrate=False 반환값은 slot_path(정식 위치) — 이동 안 함.
    assert resolved == sp


def test_backfill_divergent_slot_dir_noop_for_bare_slot_key(hf):
    """slot 키 자체가 bare 숫자(트레일링 `_N` 없음)면 대상 아님 — no-op(방어적)."""
    hf._backfill_divergent_slot_dir("4")  # 예외 없이 조용히 통과.
    assert not (hf._tmp / ".project_manager" / ".local" / "slots").exists()


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


def test_resolve_pm_state_bound_slot_no_legacy_fallback(bs, monkeypatch):
    """codex R5: 양성 슬롯 바인딩인데 자기 pm_state 부재 시 legacy 로 폴백하면 안 된다.

    `_pm_state_path(migrate=False)` 는 slot 부재 + legacy 존재면 legacy `wiki/pm_state.md`
    (솔로/slot-1 상태)를 반환한다. bound slot-2 가 그걸 읽으면 타 슬롯 차수·남은작업이 유입돼
    "fresh=1차"·"타 슬롯 최소 유입"(ADR-0047)을 깬다. 해소 경로가 자기 슬롯 디렉토리
    (`.local/slots/<slot>/`) 밖이면 None(fresh) 이어야 한다.

    production 경로(`_resolve_pm_state_file` 실 로직)를 타되 pm_handoff 의존만 스텁으로 격리한다
    — `_pm_state_file` 주입은 이 가드를 우회하므로 쓰지 않는다(codex 지적)."""
    legacy = bs._tmp / ".project_manager" / "wiki" / "pm_state.md"

    class _StubHandoff:
        @staticmethod
        def _pm_state_path(slot, *, migrate=True):
            return legacy  # slot 부재 + legacy 존재 상황 시뮬(legacy 반환)

    monkeypatch.setattr(bs, "_load_tool", lambda name: _StubHandoff if name == "pm_handoff" else None)

    inst = bs.PmBootstrap()
    inst._bound_slot = "work/project_manager_2"   # 양성 슬롯 바인딩(fresh slot-2)
    assert inst._resolve_pm_state_file() is None, "bound slot-2 는 legacy 폴백 유입 금지(None)."


def test_resolve_pm_state_solo_keeps_legacy_fallback(bs, monkeypatch):
    """대칭: 솔로(bound None)는 legacy 가 자기 것(slot-1 계보)이라 현행 폴백 유지."""
    legacy = bs._tmp / ".project_manager" / "wiki" / "pm_state.md"

    class _StubHandoff:
        @staticmethod
        def _pm_state_path(slot, *, migrate=True):
            return legacy

    monkeypatch.setattr(bs, "_load_tool", lambda name: _StubHandoff if name == "pm_handoff" else None)

    inst = bs.PmBootstrap()
    inst._bound_slot = None   # 솔로/미해소
    assert inst._resolve_pm_state_file() == legacy, "솔로는 legacy 폴백 유지(자기 것)."


def test_resolve_pm_state_bound_slot_own_state_used(bs, monkeypatch):
    """양성 슬롯 + 자기 슬롯 pm_state 존재 → 그 per-slot 경로 사용(정상 경로 무회귀)."""
    slot_path = (bs._tmp / ".project_manager" / ".local" / "slots"
                 / "project_manager_2" / "pm_state.md")

    class _StubHandoff:
        @staticmethod
        def _pm_state_path(slot, *, migrate=True):
            return slot_path  # 자기 슬롯 디렉토리 경로

    monkeypatch.setattr(bs, "_load_tool", lambda name: _StubHandoff if name == "pm_handoff" else None)

    inst = bs.PmBootstrap()
    inst._bound_slot = "work/project_manager_2"
    assert inst._resolve_pm_state_file() == slot_path, "자기 슬롯 pm_state 는 정상 사용."


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
    run_pytest_fn=None,
):
    """명시 pm_state_file 주입 *없는*(프로덕션) PmHandoff — run() 이 per-slot 해소.

    단일 self-host 형상(areas+leases)을 monkeypatch 된 tmp REPO 에 깐다. log/playbook 은
    tmp(REPO 추종). subprocess 는 결정론 DI. with_legacy=True 면 legacy pm_state 를 미리
    seed(마이그레이션 케이스), slot_seeded=True 면 slot 경로를 미리 seed(이미 per-slot).
    run_pytest_fn 주입 시 기계회귀 red 케이스(중단 시 무접촉 가드)에 쓴다.
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
# 트랜잭션 계약 — 중단 게이트(기계회귀) red → legacy 미이동 (codex must-fix)
# ══════════════════════════════════════════════════════════════════════════
#
# 마이그레이션(legacy→slot replace())이 *중단 게이트 통과 후*·pm_state 첫 접촉 직전에만
# 일어나야 한다 — 기계회귀 red 로 중단되면 pm_state 무접촉(legacy 그대로·slot 미생성).
# (출하 step 은 ADR-0039 D4 로 비차단 surface 화 — 더는 중단 게이트가 아니다.)

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


def test_run_gates_green_migrates_then_writes_per_slot(hf):
    """기계회귀 green + 출하 surface 무변경 → 통과 후 legacy→slot 이동 + slot 에 기록.

    test_run_migrates_legacy_then_writes_per_slot 의 *회귀 실행* 변형 — skip_pytest=False·
    회귀 green·출하 surface 무변경(git stub (0,"") → 출하 변경 없음·비차단)로 통과시킨다.
    """
    inst = _make_handoff_production(
        hf, with_legacy=True,
        run_pytest_fn=lambda: (0, _PYTEST_GREEN),  # 회귀 green.
        # git stub 이 (0,"") 라 미push diff 출하 변경 없음 → surface "출하 변경 없음"(비차단).
    )
    legacy = _legacy(hf)
    assert legacy.exists()

    rc = inst.run(session_num=4, wave_summary="신규", dry_run=False, skip_pytest=False)
    assert rc == 0
    sp = _slot_path(hf)
    assert sp.exists() and "**4차**" in sp.read_text(encoding="utf-8"), \
        "게이트 통과 후 legacy→slot 이동 + slot 에 기록."
    assert not legacy.exists(), "게이트 green → legacy 는 slot 으로 이동(제거)."


# NB(ADR-0057·T-0323): 이 절이 통째로 검증하던 `--worktree-slot`(raw 문자열) CLI ingress +
# `_is_bare_worktree_slot`/`_canonicalize_worktree_slot`(bare 판정·canonical 접두 정규화)는
# T-0316 이 BREAKING 제거했다 — canonical 은 이제 분해형 `--repo <name> [--slot <N>]`
# (`identity_args.parse_identity`)이라, bare 숫자 문자열이 CLI 로 들어올 경로 자체가 없다
# (`--slot` 단독은 `--repo` 필수 위반으로 구조적 fail-loud). 그 대체 동작(`--repo`/`--slot` 해소·
# M3 리스 조인·`_resolve_explicit_identity_slot`)은 test_pm_handoff.py 의 "ADR-0057 세션
# 정체성 canonical" 스위트(`test_resolve_explicit_identity_slot_*`·`hf.main(["--repo", ...])`)
# 가 대체 커버한다 — 여기선 dead 테스트만 제거.


# ══════════════════════════════════════════════════════════════════════════
# pm_bootstrap — 차수 per-slot 필터 (T-0253·ADR-0044 READ + ADR-0047 ③ 본문 dump)
# 부트스트랩 차수 유도를 전역 max → 자기 슬롯 태그 entry max 로 격리한다. 무태그 entry =
# 솔로/slot-1 귀속, slot-2+ 는 무태그 무시, fresh 슬롯 = 1차. 본문 dump 도 자기 슬롯 handoff.
# ══════════════════════════════════════════════════════════════════════════

# 두 슬롯이 같은 공유 log 를 쓰되 정체성 태그로 시퀀스가 갈린다 — slot-1 은 3차까지, slot-2 는
# 2차까지 핸드오프. 사이에 무태그(태그 도입 전) 40차 entry 하나(=솔로/slot-1 귀속)가 섞여 있다.
_MULTI_SLOT_LOG = (
    "# Project Log\n\n"
    "## [2026-07-01] handoff | PM 40차 → 다음 PM 세션\n"
    "- 무태그(태그 도입 전) 인계.\n\n"
    "## [2026-07-08] handoff | PM 1차 (project_manager_1) → 다음 PM 세션\n"
    "- slot1 1차 인계.\n\n"
    "## [2026-07-08] handoff | PM 1차 (project_manager_2) → 다음 PM 세션\n"
    "- slot2 1차 인계.\n\n"
    "## [2026-07-09] handoff | PM 2차 (project_manager_1) → 다음 PM 세션\n"
    "- slot1 2차 인계.\n\n"
    "## [2026-07-09] handoff | PM 2차 (project_manager_2) → 다음 PM 세션\n"
    "- slot2 2차 인계.\n\n"
    "## [2026-07-10] handoff | PM 3차 (project_manager_1) → 다음 PM 세션\n"
    "- slot1 3차 인계.\n"
)


# ── _session_owns_untagged: 무태그 귀속 판정 (솔로/slot-1 만) ─────────────────

def test_session_owns_untagged_solo_and_slot1(bs):
    """무태그 entry 는 솔로(None)/slot-1(`<repo>_1`) 만 자기 것으로 본다."""
    assert bs._session_owns_untagged(None) is True
    assert bs._session_owns_untagged("project_manager_1") is True


def test_session_owns_untagged_slot2plus_ignores(bs):
    """slot-2+ 는 무태그를 자기 것으로 보지 않는다(핵심 회귀 가드·codex 제언)."""
    assert bs._session_owns_untagged("project_manager_2") is False
    assert bs._session_owns_untagged("project_manager_11") is False


# ── last_handoff_header_line 슬롯 필터: user 연속성 cross-slot 유입 차단 (codex R3) ──

def test_last_handoff_header_line_slot2_uses_own_not_global(bs):
    """codex R3: slot-2 user 연속성은 전역 마지막(slot-1 3차)이 아니라 **자기 슬롯 마지막**
    (slot-2 2차) handoff 헤더를 pickaxe needle 로 써야 한다 — 타 슬롯 작성자 오판정 차단."""
    header = bs.last_handoff_header_line(_MULTI_SLOT_LOG, bound_session="project_manager_2")
    assert header is not None
    assert "(project_manager_2)" in header and "PM 2차" in header
    assert "project_manager_1" not in header, "전역 마지막(slot-1 3차) 유입 0."


def test_last_handoff_header_line_solo_keeps_global(bs):
    """솔로(bound None)는 전역 마지막 handoff 헤더 유지(원 동작·태그 무관 전역 최신)."""
    header = bs.last_handoff_header_line(_MULTI_SLOT_LOG, bound_session=None)
    assert header is not None
    assert "PM 3차 (project_manager_1)" in header, "솔로는 전역 마지막 handoff 유지(태그 무관)."


def test_last_handoff_header_line_fresh_slot3_no_leak(bs):
    """fresh slot-3(자기 handoff 0)은 None — 타 슬롯 handoff 헤더로 폴백 안 함."""
    assert bs.last_handoff_header_line(_MULTI_SLOT_LOG, bound_session="project_manager_3") is None


# ── parse_last_handoff_session_num: 슬롯 필터 max ─────────────────────────────

def test_parse_last_handoff_slot1_max_includes_untagged(bs):
    """slot-1 → 무태그(40) + 자기 태그(1·2·3) 중 max=40 (무태그 귀속·연속성 보존)."""
    assert bs.parse_last_handoff_session_num(
        _MULTI_SLOT_LOG, bound_session="project_manager_1") == 40


def test_parse_last_handoff_slot2_ignores_untagged(bs):
    """slot-2 → 무태그(40) 무시·자기 태그(1·2) 중 max=2 (2슬롯 독립 시퀀스 핵심)."""
    assert bs.parse_last_handoff_session_num(
        _MULTI_SLOT_LOG, bound_session="project_manager_2") == 2


def test_parse_last_handoff_two_slots_independent_sequences(bs):
    """DoD ①: slot-1(3차)·slot-2(2차)가 같은 log 에서 독립 시퀀스로 유도된다(공존)."""
    n1 = bs.parse_last_handoff_session_num(_MULTI_SLOT_LOG, bound_session="project_manager_1")
    n2 = bs.parse_last_handoff_session_num(_MULTI_SLOT_LOG, bound_session="project_manager_2")
    # slot-1 은 무태그 40 을 상속하므로 max=40, slot-2 는 자기 태그만이라 2 — 서로 안 섞인다.
    assert (n1, n2) == (40, 2)
    assert n1 != n2, "두 슬롯이 같은 차수를 주장하지 않는다(사용자 제보 버그 해소)."


def test_parse_last_handoff_fresh_slot_returns_none(bs):
    """DoD: fresh 슬롯(자기 태그 entry 0·무태그 무시) → None(순수 함수·1차 규칙은 context)."""
    assert bs.parse_last_handoff_session_num(
        _MULTI_SLOT_LOG, bound_session="project_manager_3") is None


def test_parse_last_handoff_solo_default_global_max(bs):
    """솔로/미해소(bound None) → 전역 tag-agnostic max — 무태그(40) 포함 최고차. 현행 무회귀."""
    assert bs.parse_last_handoff_session_num(_MULTI_SLOT_LOG) == 40


def test_parse_last_handoff_unresolved_bound_parses_tagged_global(bs):
    """codex R4 회귀: bound 미해소(None·fresh clone=lease 부재)라도 tracked 로그에 태그된
    handoff 만 있으면 **전역 tag-agnostic 파싱**으로 차수를 복원해야 한다(T-0208 log-derived 보존).

    슬롯 필터를 None 에도 태우면(구 버그) tagged entry 를 전부 버려 차수 유실 → placeholder 로
    떨어졌다. bound 는 *양성 해소*일 때만 필터, None 은 원 전역 동작 보존."""
    only_tagged = (
        "## [2026-07-07] handoff | PM 4차 (project_manager_1) → 다음 PM 세션\n- t.\n\n"
        "## [2026-07-08] handoff | PM 5차 (project_manager_1) → 다음 PM 세션\n- t.\n"
    )
    assert bs.parse_last_handoff_session_num(only_tagged) == 5, (
        "미해소 bound=None 은 tagged 로그도 전역 파싱(차수 유실 금지·fresh clone).")


def test_parse_last_handoff_positive_slot_still_filters(bs):
    """대칭 확인: 양성 슬롯 해소(project_manager_2)는 여전히 자기 태그만 필터(전역 아님)."""
    assert bs.parse_last_handoff_session_num(
        _MULTI_SLOT_LOG, bound_session="project_manager_2") == 2


# ── extract_slot_handoff_entry: 자기 슬롯 마지막 handoff 본문 (ADR-0047 ③) ─────

def test_extract_slot_handoff_entry_slot1_last_own_handoff(bs):
    """slot-1 → 자기 마지막 handoff(3차) 본문·제목. 타 슬롯 본문 유입 없음."""
    entry = bs.extract_slot_handoff_entry(_MULTI_SLOT_LOG, bound_session="project_manager_1")
    assert entry is not None
    assert entry["type"] == "handoff"
    assert "PM 3차 (project_manager_1)" in entry["title"]
    assert "slot1 3차 인계" in entry["body"]
    assert "slot2" not in entry["body"], "타 슬롯 본문이 섞이면 안 된다."


def test_extract_slot_handoff_entry_slot2_last_own_handoff(bs):
    """slot-2 → 자기 마지막 handoff(2차) 본문(전역 마지막=slot-1 3차 아님)."""
    entry = bs.extract_slot_handoff_entry(_MULTI_SLOT_LOG, bound_session="project_manager_2")
    assert entry is not None
    assert "PM 2차 (project_manager_2)" in entry["title"]
    assert "slot2 2차 인계" in entry["body"]
    assert "slot1" not in entry["body"], "전역 마지막(slot-1 3차) 본문이 유입되면 안 된다."


def test_extract_slot_handoff_entry_untagged_fallback_for_slot1(bs):
    """무태그 폴백 — slot-1 은 태그 도입 전 무태그 handoff 도 자기 것으로 본다."""
    log = (
        "## [2026-07-01] handoff | PM 40차 → 다음 PM 세션\n"
        "- 무태그 인계 본문.\n"
    )
    entry = bs.extract_slot_handoff_entry(log, bound_session="project_manager_1")
    assert entry is not None
    assert "무태그 인계 본문" in entry["body"]


def test_extract_slot_handoff_entry_slot2_no_own_handoff_is_none(bs):
    """slot-2 가 자기 handoff 0개(무태그만) → None(호출부가 전역 마지막으로 폴백)."""
    log = (
        "## [2026-07-01] handoff | PM 40차 → 다음 PM 세션\n"
        "- 무태그.\n"
    )
    assert bs.extract_slot_handoff_entry(log, bound_session="project_manager_2") is None


def test_extract_slot_handoff_entry_skips_non_handoff_types(bs):
    """전역 마지막이 note 여도 자기 슬롯 마지막 *handoff* 본문을 고른다(type 필터)."""
    log = (
        "## [2026-07-10] handoff | PM 3차 (project_manager_1) → 다음 PM 세션\n"
        "- slot1 3차 인계.\n\n"
        "## [2026-07-11] note | 진행 메모\n"
        "- 이건 handoff 아님.\n"
    )
    entry = bs.extract_slot_handoff_entry(log, bound_session="project_manager_1")
    assert entry is not None
    assert entry["type"] == "handoff"
    assert "slot1 3차 인계" in entry["body"]
    assert "이건 handoff 아님" not in entry["body"]


# ── 타-슬롯 자산 유입 = 0 단언 (DoD·자기 슬롯 필터 후) ─────────────────────────

def test_slot2_filter_zero_other_slot_asset_leak(bs):
    """DoD: slot-2 필터 후 차수·본문 어디에도 타 슬롯(slot-1·무태그) 자산 유입 0."""
    num = bs.parse_last_handoff_session_num(_MULTI_SLOT_LOG, bound_session="project_manager_2")
    entry = bs.extract_slot_handoff_entry(_MULTI_SLOT_LOG, bound_session="project_manager_2")
    # 차수: 무태그 40·slot-1 3 어느 것도 아님 — 오직 자기 슬롯 max(2).
    assert num == 2
    # 본문: slot-1·무태그 entry 어느 것도 유입 안 됨.
    assert entry is not None
    assert "slot1" not in entry["body"]
    assert "무태그" not in entry["body"]
    assert "slot2 2차 인계" in entry["body"]


# ── _collect_handoff_context: 슬롯 스코프 배선 + fresh 슬롯 = 1차 ──────────────

def _bootstrap_for_ctx(bs, tmp_path, *, bound_slot, pm_state_text=None):
    """`_collect_handoff_context` 호출용 최소 PmBootstrap — pm_state 주입·슬롯 바인딩.

    areas 는 **명시 부재**(genuine solo)로 둔다 — 무인자(`bound_slot=None`) 자동해소가 결정적
    으로 None(솔로)이 되게. 단일 self-host 자동바인딩(areas+lease seed)은 별도 헬퍼로 구성한다.
    """
    if pm_state_text is None:
        pm_state_file = tmp_path / "absent_pm_state.md"  # 부재 → 미해소(fresh)
    else:
        pm_state_file = tmp_path / "pm_state.md"
        pm_state_file.write_text(pm_state_text, encoding="utf-8")
    inst = bs.PmBootstrap(
        pm_state_file=pm_state_file,
        areas_file=tmp_path / "no-areas.md",  # 부재 → 자동해소 None(genuine solo)
    )
    inst._bound_slot = bound_slot
    return inst


def _bootstrap_single_self_host(bs, tmp_path, *, pm_state_file=None, log_file=None):
    """단일 self-host(areas+lease slot-1) 무인자 PmBootstrap — 자동바인딩 → project_manager_1.

    `_make_single_self_host` 로 areas+lease 를 tmp REPO 에 깐다(monkeypatch REPO 추종). 무인자
    (`_bound_slot=None`)라 `_auto_bound_session` 이 handoff write 측과 대칭으로 slot-1 해소한다.
    """
    _make_single_self_host(tmp_path)  # areas + lease slot 1
    kwargs = {"areas_file": tmp_path / ".project_manager" / "areas.md"}
    if pm_state_file is not None:
        kwargs["pm_state_file"] = pm_state_file
    if log_file is not None:
        kwargs["log_file"] = log_file
    inst = bs.PmBootstrap(**kwargs)
    inst._bound_slot = None  # 무인자 → 자동해소
    return inst


# ── _bound_session_name / _auto_bound_session: write↔read 대칭 (MF-1) ──────────

def test_bound_session_name_auto_resolves_single_self_host(bs, tmp_path):
    """MF-1: 무인자 부트스트랩이 단일 self-host 에서 slot-1(`project_manager_1`)로 자동바인딩."""
    inst = _bootstrap_single_self_host(bs, tmp_path)
    assert inst._bound_session_name() == "project_manager_1"


def test_bound_session_name_explicit_overrides_auto(bs, tmp_path):
    """명시 `_bound_slot` 은 자동해소를 덮는다(explicit 우선·`work/` 접두 제거)."""
    inst = _bootstrap_single_self_host(bs, tmp_path)
    inst._bound_slot = "work/project_manager_3"
    assert inst._bound_session_name() == "project_manager_3"


def test_bound_session_name_genuine_solo_is_none(bs, tmp_path):
    """등록 repo 0개(genuine solo·areas 부재) → None(무태그=솔로 귀속 신호·현행 무변경)."""
    inst = bs.PmBootstrap(areas_file=tmp_path / "no-areas.md")
    inst._bound_slot = None
    assert inst._bound_session_name() is None


def test_bound_session_name_ambiguous_multipm_is_none_no_crash(bs, tmp_path):
    """모호(repo≥2·SlotResolutionError) → 부트스트랩 read 는 crash 없이 None(솔로 폴백·fail-soft)."""
    areas = tmp_path / ".project_manager" / "areas.md"
    areas.parent.mkdir(parents=True, exist_ok=True)
    areas.write_text(
        "| repo | prefix | git | test_cmd | owner |\n"
        "|---|---|---|---|---|\n"
        "| repo_a | A | g | pytest | me |\n"
        "| repo_b | B | g | pytest | me |\n",
        encoding="utf-8")
    inst = bs.PmBootstrap(areas_file=areas)
    inst._bound_slot = None
    assert inst._bound_session_name() is None  # SlotResolutionError → None (no raise).


def test_collect_handoff_context_slot2_fresh_is_first_session(bs, tmp_path):
    """DoD: fresh slot-2(자기 태그 0·pm_state 부재) → session_num=1(placeholder 아님·슬롯-first)."""
    inst = _bootstrap_for_ctx(bs, tmp_path, bound_slot="work/project_manager_2")
    ctx = inst._collect_handoff_context(
        "## [2026-07-01] handoff | PM 40차 → 다음 PM 세션\n- 무태그.\n"
    )
    assert ctx is not None
    assert ctx["session_num"] == 1, "slot-2 는 무태그 40 을 무시하고 fresh → 1차."
    assert ctx["session_stale"] is False


def test_collect_handoff_context_slot1_inherits_untagged(bs, tmp_path):
    """slot-1(fresh pm_state) → 무태그 40 을 상속 → next 41차(log 폴백·무회귀)."""
    inst = _bootstrap_for_ctx(bs, tmp_path, bound_slot="work/project_manager_1")
    ctx = inst._collect_handoff_context(
        "## [2026-07-01] handoff | PM 40차 → 다음 PM 세션\n- 무태그.\n"
    )
    assert ctx is not None
    assert ctx["session_num"] == 41, "slot-1 은 무태그 40 상속 → next 41."


def test_collect_handoff_context_genuine_solo_empty_stays_placeholder(bs, tmp_path):
    """**genuine solo**(등록 repo 0개·bound 미해소) + handoff 없는 log + pm_state 부재 → None.

    MF-1 재검토: fresh→1차 규칙은 bound 세션이 *해소될 때만* 발동. 등록 repo 가 없는 진짜 솔로는
    bound=None → 규칙 미발동 → 현행 placeholder 경로 보존(회귀 0). (단일 self-host 는 다음 테스트.)
    """
    inst = _bootstrap_for_ctx(bs, tmp_path, bound_slot=None)  # areas 부재 → genuine solo
    ctx = inst._collect_handoff_context("## [2026-07-01] note | 메모\n- x.\n")
    assert ctx is None, "genuine solo 는 fresh→1차 미발동 — placeholder 경로 보존."


def test_collect_handoff_context_single_self_host_empty_is_first(bs, tmp_path):
    """MF-1 정합: **단일 self-host** 무인자 + handoff 없는 log + pm_state 부재 → 1차(placeholder 아님).

    genuine solo 와 대비 — areas+lease 가 있는 단일 self-host 는 무인자라도 slot-1 로 자동바인딩
    되므로(handoff write 대칭) fresh→1차 규칙이 발동한다(첫 세션=1차). 빈-로그 단일 self-host 의
    차수 명확화(코디네이터 지시 — placeholder 가 아니라 1차).
    """
    inst = _bootstrap_single_self_host(bs, tmp_path, pm_state_file=tmp_path / "absent.md")
    ctx = inst._collect_handoff_context("## [2026-07-01] note | 메모\n- x.\n")
    assert ctx is not None
    assert ctx["session_num"] == 1, "단일 self-host 무인자 → slot-1 자동바인딩 → fresh 1차."


def test_collect_handoff_context_single_self_host_owns_tagged_and_untagged(bs, tmp_path):
    """MF-1 코어 버그 재현→수정: 51 무태그 + `PM 52차 (project_manager_1)` → 무인자 read 가 53 announce.

    구버그: 무인자 부트스트랩이 솔로(bound None)로 읽어 자기 태그 52 를 버리고 51 만 봄 →
    announce 52(중복). 대칭화 후: slot-1 로 자동바인딩돼 무태그 51 + 자기 태그 52 를 **둘 다**
    소유 → max 52 → next 53. (T-0208 log 교차검증·stale 감지 침묵 무력화도 함께 해소.)
    """
    log = (
        "## [2026-07-09] handoff | PM 51차 → 다음 PM 세션\n- 무태그 히스토리.\n\n"
        "## [2026-07-10] handoff | PM 52차 (project_manager_1) → 다음 PM 세션\n- slot1 52차.\n"
    )
    inst = _bootstrap_single_self_host(bs, tmp_path, pm_state_file=tmp_path / "absent.md")
    ctx = inst._collect_handoff_context(log)
    assert ctx is not None
    assert ctx["session_num"] == 53, "무태그 51 + 자기 태그 52 소유 → 53(구버그: 52 유실 announce)."


def test_collect_handoff_context_slot_scoped_max_not_global(bs, tmp_path):
    """슬롯 스코프 배선 — slot-2 는 전역 max(40) 가 아니라 자기 슬롯 시퀀스(next 3차)를 announce."""
    inst = _bootstrap_for_ctx(bs, tmp_path, bound_slot="work/project_manager_2")
    ctx = inst._collect_handoff_context(_MULTI_SLOT_LOG)
    assert ctx is not None
    assert ctx["session_num"] == 3, "slot-2 자기 태그 max=2 → next 3(전역 40/41 아님)."


def test_collect_log_entry_dumps_own_slot_handoff_body(bs, tmp_path):
    """`_collect_log_entry` — 자기 슬롯(slot-2) 마지막 handoff 본문 dump(전역 마지막 아님)."""
    log_file = tmp_path / "current.md"
    log_file.write_text(_MULTI_SLOT_LOG, encoding="utf-8")
    inst = bs.PmBootstrap(log_file=log_file)
    inst._bound_slot = "work/project_manager_2"
    entry = inst._collect_log_entry()
    assert entry is not None
    assert "PM 2차 (project_manager_2)" in entry["title"]
    assert "slot2 2차 인계" in entry["body"]
    assert "slot1" not in entry["body"], "전역 마지막(slot-1 3차) 유입 0."


# ── MF-2: fresh slot-2+ 는 전역 폴백 금지 (타 슬롯 handoff 본문·branch·reattach 유입 0) ──

def test_collect_log_entry_fresh_slot2_no_global_fallback(bs, tmp_path):
    """MF-2: bound 해소(slot-2)됐는데 자기 handoff 0개(fresh) → None(전역 마지막 폴백 금지)."""
    log = (
        "## [2026-07-10] handoff | PM 3차 (project_manager_1) → 다음 PM 세션\n"
        "- slot1 3차 인계 본문.\n"
    )
    log_file = tmp_path / "current.md"
    log_file.write_text(log, encoding="utf-8")
    inst = bs.PmBootstrap(log_file=log_file)
    inst._bound_slot = "work/project_manager_2"  # bound 해소·자기 handoff 0개(fresh)
    entry = inst._collect_log_entry()
    assert entry is None, "fresh slot-2 는 전역(slot-1) handoff 본문으로 폴백하면 안 된다(MF-2)."


def test_collect_log_entry_fresh_slot2_ignores_untagged_global(bs, tmp_path):
    """MF-2: fresh slot-2 + 무태그 전역 handoff → None(무태그=slot-1 계보·slot-2 비소유·유입 0)."""
    log = (
        "## [2026-07-09] handoff | PM 50차 → 다음 PM 세션\n- 무태그 전역 본문.\n"
    )
    log_file = tmp_path / "current.md"
    log_file.write_text(log, encoding="utf-8")
    inst = bs.PmBootstrap(log_file=log_file)
    inst._bound_slot = "work/project_manager_2"
    assert inst._collect_log_entry() is None, "무태그 전역도 slot-2 엔 유입 안 됨(MF-2)."


def test_collect_log_entry_solo_unresolved_keeps_global_fallback(bs, tmp_path):
    """MF-2 경계: bound **진짜 미해소**(genuine solo·note 만) → 전역 마지막 폴백 유지(현행 표시)."""
    log = "## [2026-07-09] note | 진행 메모\n- 전역 note 본문.\n"
    log_file = tmp_path / "current.md"
    log_file.write_text(log, encoding="utf-8")
    inst = bs.PmBootstrap(log_file=log_file, areas_file=tmp_path / "no-areas.md")
    inst._bound_slot = None  # genuine solo — 자동해소 None
    entry = inst._collect_log_entry()
    assert entry is not None, "솔로(bound 미해소)는 전역 폴백 유지 — 현행 표시 보존."
    assert "전역 note 본문" in entry["body"]


def test_collect_log_entry_solo_note_after_handoff_returns_note(bs, tmp_path):
    """codex R2 회귀: 솔로에서 handoff **뒤에** note 가 오면 마지막 entry(=note)를 dump 해야 한다.

    T-0179 계약 = 마지막 entry(모든 타입). handoff-우선 필터를 솔로에도 태우면 최신 note/complete
    ("wave 진행 중" 신호)를 과거 handoff 로 가린다. note-만 테스트는 이 회귀를 못 잡으므로(handoff
    부재라 어차피 전역 폴백) handoff→note 순서를 명시적으로 고정한다."""
    log = (
        "## [2026-07-08] handoff | PM 50차 → 다음 PM 세션\n- 과거 handoff 본문.\n\n"
        "## [2026-07-09] complete | T-9999 — 뭔가 완료\n- 최신 complete 본문(wave 진행 중).\n"
    )
    log_file = tmp_path / "current.md"
    log_file.write_text(log, encoding="utf-8")
    inst = bs.PmBootstrap(log_file=log_file, areas_file=tmp_path / "no-areas.md")
    inst._bound_slot = None  # genuine solo
    entry = inst._collect_log_entry()
    assert entry is not None
    assert entry["type"] == "complete", "솔로는 마지막 entry(complete) — 과거 handoff 로 가리면 안 됨."
    assert "최신 complete 본문" in entry["body"]
    assert "과거 handoff 본문" not in entry["body"]


def test_collect_log_entry_fresh_slot_no_reattach_leak(bs, tmp_path):
    """MF-2 파급: fresh slot-2 는 log_entry=None → reattach 가 타 슬롯 branch 로 오경고 안 함."""
    log = (
        "## [2026-07-10] handoff | PM 3차 (project_manager_1) → 다음 PM 세션\n"
        "- worktree: slot=work/project_manager_1 · branch=other-slot-branch\n"
    )
    log_file = tmp_path / "current.md"
    log_file.write_text(log, encoding="utf-8")
    inst = bs.PmBootstrap(log_file=log_file)
    inst._bound_slot = "work/project_manager_2"
    entry = inst._collect_log_entry()
    # log_entry 없음 → reattach body=None → 타 슬롯 branch 로 오경고 없음.
    body = entry.get("body") if entry else None
    assert bs.reattach_warning("my-branch", body) is None, "fresh slot 은 타 슬롯 reattach 오경고 0."


# ── should-fix: 정규식 태그 캡처를 canonical `<repo>_<N>` 로 제약 (서술형 괄호 오캡처 방지) ──

def test_handoff_regex_ignores_descriptive_parens_for_solo(bs):
    """should-fix: 서술형 괄호(`PM 4차 (아침 대화)`)는 세션 태그로 오인 안 됨 — 솔로가 소유(drop 0)."""
    log = "## [2026-07-10] handoff | PM 4차 (아침 대화) → 다음 PM 세션\n- 솔로 본문.\n"
    # 솔로(bound None) — 서술형 괄호를 태그로 오캡처하면 drop 되어 None 이 됐을 것.
    assert bs.parse_last_handoff_session_num(log) == 4
    entry = bs.extract_slot_handoff_entry(log)
    assert entry is not None and "솔로 본문" in entry["body"]


def test_handoff_regex_canonical_tag_still_captured(bs):
    """canonical `(<repo>_<N>)` 는 여전히 세션 태그로 캡처(제약이 정상 태그를 막지 않음)."""
    log = "## [2026-07-10] handoff | PM 4차 (project_manager_2) → 다음 PM 세션\n- t.\n"
    # 양성 슬롯(slot-2)은 자기 태그 소유 → 4. 태그 캡처가 정상 동작함을 이 단언이 입증한다.
    assert bs.parse_last_handoff_session_num(log, bound_session="project_manager_2") == 4
    # 솔로/미해소(None)는 전역 tag-agnostic 파싱 → 태그된 4 도 차수로 복원(codex R4·차수 유실 금지).
    assert bs.parse_last_handoff_session_num(log) == 4


def test_handoff_regex_descriptive_parens_with_number_not_canonical(bs):
    """서술형에 숫자가 있어도(`(회의 3)`) 후행 `_N` 없으면 태그 아님 — 솔로 소유."""
    log = "## [2026-07-10] handoff | PM 5차 (회의 3) → 다음 PM 세션\n- 솔로.\n"
    assert bs.parse_last_handoff_session_num(log) == 5


# ── reconcile_session_num: 순수 함수 단위 (DoD·test_pm_state_per_slot 확장) ────

def test_reconcile_session_num_slot_scoped_log_next(bs):
    """슬롯-스코프 log_next 소비 — pm_state 미해소면 슬롯 log_next 로 폴백(stale 아님)."""
    # slot-2 자기 태그 max=2 → log_next=3. pm_state 미해소(`?`) → 3 폴백.
    assert bs.reconcile_session_num("?", 3) == (3, False)


def test_reconcile_session_num_state_wins_over_slot_log(bs):
    """pm_state 슬롯 차수가 슬롯 log_next 보다 앞서면 pm_state 우선·stale 아님."""
    assert bs.reconcile_session_num(4, 3) == (4, False)


def test_reconcile_session_num_slot_log_wins_when_state_stale(bs):
    """슬롯 log_next 가 pm_state 보다 크면 log 우선(max) + stale(머신 간 미동기)."""
    assert bs.reconcile_session_num(2, 3) == (3, True)


def test_reconcile_session_num_both_unresolved_placeholder(bs):
    """pm_state·슬롯 log 둘 다 미해소 → placeholder 그대로(fresh 규칙은 context 층)."""
    assert bs.reconcile_session_num("?", None) == ("?", False)
    assert bs.reconcile_session_num(None, None) == (None, False)


# ══════════════════════════════════════════════════════════════════════════
# 단일 self-host 라운드트립 — handoff 무인자 write(태그) → 부트스트랩 무인자 read (MF-1 대칭 실증)
# handoff write 측과 부트스트랩 read 측이 같은 슬롯(project_manager_1)으로 대칭 해소돼, handoff 가
# 무인자로 쓴 `(project_manager_1)` 태그 entry 를 부트스트랩이 되읽는다(비대칭 결함 종단 검증).
# ══════════════════════════════════════════════════════════════════════════

def test_single_self_host_roundtrip_handoff_write_bootstrap_read(hf, bs, tmp_path):
    """라운드트립: handoff 무인자 52차 write(태그) → 부트스트랩 무인자 read 가 태그 복원·53 announce.

    hf·bs 는 같은 `tmp_path` REPO 를 monkeypatch(공유 tmp) — handoff 가 tmp 에 쓴 것을 부트스트랩이
    그대로 읽는다. 단일 self-host(areas+lease slot-1)라 양측 다 무인자로 slot-1 자동해소(대칭).
    """
    _make_single_self_host(tmp_path)  # areas + lease slot 1 (hf.REPO=bs.REPO=tmp)
    # 무태그 히스토리(adopter#0 마이그레이션 계보) — 51차.
    log_file = tmp_path / "log.md"
    log_file.write_text(
        "# log\n\n## [2026-07-09] handoff | PM 51차 → 다음 PM 세션\n- 무태그 히스토리.\n",
        encoding="utf-8")
    playbook = tmp_path / "pb.md"
    playbook.write_text("# pb (no anchor)\n", encoding="utf-8")
    # 슬롯 pm_state seed(handoff write 성공 전제) — sliding window 앵커.
    sp = tmp_path / ".project_manager" / ".local" / "slots" / "project_manager_1" / "pm_state.md"
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(_SESSION_SECTION, encoding="utf-8")

    # ── handoff 무인자(production·명시 슬롯/pm_state 없음) → 52차 write ──
    hinst = hf.PmHandoff(
        run_pytest_fn=lambda: (_ for _ in ()).throw(AssertionError("skip")),
        run_git_fn=lambda args: (0, ""),
        log_file=log_file, pm_playbook_file=playbook,
        # pm_state_file 미주입 → per-slot 해소(프로덕션·slot-1 자동).
    )
    assert hinst.run(session_num=52, wave_summary="라운드트립", dry_run=False, skip_pytest=True) == 0

    log_text = log_file.read_text(encoding="utf-8")
    # write 측: canonical 태그가 실제로 박혔다(handoff write 대칭 검증).
    assert "PM 52차 (project_manager_1)" in log_text, "handoff 무인자가 slot-1 태그를 write."

    # ── 부트스트랩 무인자(자동해소) read ──
    binst = _bootstrap_single_self_host(
        bs, tmp_path,
        pm_state_file=tmp_path / "bootstrap_absent.md",  # 부재 → 순수 log-derived.
        log_file=log_file,
    )
    # read 측: 같은 슬롯으로 대칭 해소.
    assert binst._bound_session_name() == "project_manager_1"
    ctx = binst._collect_handoff_context(log_text)
    assert ctx is not None
    # 무태그 51 + 자기 태그 52 를 둘 다 소유 → max 52 → 다음 53차(구버그: 51 유실→52 announce).
    assert ctx["session_num"] == 53
    # 본문 dump 도 자기 슬롯 최신 handoff(52차)로 복원.
    entry = binst._collect_log_entry()
    assert entry is not None
    assert "PM 52차 (project_manager_1)" in entry["title"]
    assert "무태그 히스토리" not in entry["body"], "본문은 최신 자기 handoff(52차)만."


# ══════════════════════════════════════════════════════════════════════════
# pm_handoff — 오형식 차수 정규화 (`**N차차+**` → `**N차**`·T-0254·ADR-0044·§1.6)
# T-0100 이중-차 잔재(finance 솔로 실측)가 앵커 정규식(`\d+차` 1회) 미매치 → pm_state
# derive 실패 → log 폴백 은닉 의존. 파서 관대화(fallback)가 아니라 멱등·비파괴 데이터
# 정규화 도구로 원천 해소(prefer-data-migration-over-fallback).
# ══════════════════════════════════════════════════════════════════════════

# 오형식 `**N차차**`(이중-차) 를 담은 세션 식별 절 — 정상 형식(_SESSION_SECTION)의 오염판.
# 포인터 줄의 `PM 1차~1차` 는 정상 단일 '차'(정규화 대상 아님·비파괴 대조).
_MALFORMED_SESSION_SECTION = (
    "# PM State\n\n"
    "## 세션 식별 (현재까지 사용된 이름)\n\n"
    "최근 N 차 (sliding window, 기본 3 차):\n"
    "  - **50차차** (2026-06-11 · w): w.\n"
    "  - **51차차** (2026-06-12 · w): w.\n"
    "  - **52차차** (2026-06-13 · w): w.\n"
    "  - 이전 차 (PM 1차~1차) = `log/current.md` handoff entry 단일 진실.\n"
    "\n## 진행 중인 의사결정\n\n표.\n"
)


def _write_legacy_state(hf, text: str) -> Path:
    """솔로 legacy pm_state(`wiki/pm_state.md`)에 텍스트를 쓴다 (normalize CLI 대상)."""
    p = _legacy(hf)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


# ── 순수 함수 normalize_session_anchors ──────────────────────────────────────

def test_normalize_fixes_double_cha_tokens(hf):
    """오형식 `**N차차**` 셋을 전부 `**N차**` 로 정규화·이중-차 잔존 0."""
    fixed = hf.normalize_session_anchors(_MALFORMED_SESSION_SECTION)
    assert "**50차**" in fixed and "**51차**" in fixed and "**52차**" in fixed
    assert "차차" not in fixed


def test_normalize_derive_succeeds_after_fix(hf):
    """DoD: 오형식은 앵커 미매치라 유도 실패(placeholder)·정규화 후 infer 성공(max+1)."""
    # 오형식 상태 — `_PM_SESSION_ANCHOR_RE`(`\d+차` 1회) 미매치 → placeholder.
    assert hf.infer_next_session_num(_MALFORMED_SESSION_SECTION) == hf.TRIGGER_SESSION_PLACEHOLDER
    fixed = hf.normalize_session_anchors(_MALFORMED_SESSION_SECTION)
    # 정규화 후 max(50,51,52)+1 = 53 유도 성공(log 폴백 은닉 의존 해소).
    assert hf.infer_next_session_num(fixed) == 53


def test_normalize_idempotent(hf):
    """멱등: 한 번 정규화한 텍스트를 다시 정규화해도 결과가 동일하다."""
    once = hf.normalize_session_anchors(_MALFORMED_SESSION_SECTION)
    twice = hf.normalize_session_anchors(once)
    assert twice == once


def test_normalize_noop_on_clean_returns_same_object(hf):
    """오형식 없는 정상 절 — 무변경 시 입력을 그대로(동일 객체) 반환(값 비교 no-op 감지)."""
    out = hf.normalize_session_anchors(_SESSION_SECTION)
    assert out == _SESSION_SECTION
    assert out is _SESSION_SECTION


def test_normalize_handles_triple_or_more_cha(hf):
    """`차차차`(3회 이상)도 단일 `차` 로 접는다(2회+ 반복 전부 대상)."""
    text = _MALFORMED_SESSION_SECTION.replace("**50차차**", "**50차차차**")
    fixed = hf.normalize_session_anchors(text)
    assert "**50차**" in fixed and "차차" not in fixed


def test_normalize_non_destructive_preserves_dates_and_pointer(hf):
    """비파괴: 오형식 잉여 '차' 만 제거·날짜/요약/포인터 줄은 한 글자도 안 바뀐다."""
    fixed = hf.normalize_session_anchors(_MALFORMED_SESSION_SECTION)
    assert "(2026-06-11 · w): w." in fixed
    assert "이전 차 (PM 1차~1차) = `log/current.md` handoff entry 단일 진실." in fixed
    assert "## 진행 중인 의사결정" in fixed


def test_normalize_no_session_section_unchanged(hf):
    """세션 식별 절이 없으면 원문을 그대로(동일 객체) 반환 — 절 밖은 손대지 않는다."""
    text = "# PM State\n\n## 딴 절\n\n**50차차** 본문.\n"
    out = hf.normalize_session_anchors(text)
    assert out == text
    assert out is text


def test_normalize_scoped_to_session_section_only(hf):
    """비파괴 스코프: 세션 식별 절 *밖*의 `**N차차**` 는 정규화 대상이 아니다(절 경계 존중)."""
    text = (
        "# PM State\n\n"
        "## 세션 식별 (현재까지 사용된 이름)\n\n"
        "  - **50차차** (2026-06-11 · w): w.\n"
        "\n## 다른 절\n\n"
        "여기 **99차차** 는 세션 식별 절 밖이라 정규화 대상 아님.\n"
    )
    fixed = hf.normalize_session_anchors(text)
    assert "**50차**" in fixed, "절 안 오형식은 정규화."
    assert "**99차차**" in fixed, "절 밖 오형식은 보존(비파괴 스코프)."


# finance 실 타깃 형상 — 설명 절(`## 세션 식별 규칙`·anchor 매치하나 entry 0)이 **먼저** 오고,
# 그 뒤 실제 window 절의 entry 가 **전부 오형식**(`**N차차**`·well-formed 0). 절 선택을 정상
# anchor 만으로 하면 window 가 "entry 없음"→설명 절로 폴백→silent no-op(codex must-fix 재현).
_FINANCE_MALFORMED_ONLY_STATE = (
    "# PM State\n\n"
    "## 세션 식별 규칙\n\n"
    "이름은 <repo>_<N> 로 짓는다 — 설명만 있는 절(entry 없음).\n\n"
    "## 세션 식별 (현재까지 사용된 이름)\n\n"
    "최근 N 차 (sliding window, 기본 3 차):\n"
    "  - **87차차** (2026-07-08 · w): w.\n"
    "  - **88차차** (2026-07-09 · w): w.\n"
    "  - **89차차** (2026-07-10 · w): w.\n"
    "  - 이전 차 (PM 1차~1차) = `log/current.md` handoff entry 단일 진실.\n"
    "\n## 진행 중인 의사결정\n\n표.\n"
)


def test_normalize_all_malformed_window_after_rules_section(hf):
    """codex must-fix 재현 — 설명 절이 먼저 오고 window entry 가 전부 오형식이어도 window 절을
    정확히 골라 정규화한다(silent no-op 아님·수정 전엔 이 assert 가 no-op 로 실패).
    """
    fixed = hf.normalize_session_anchors(_FINANCE_MALFORMED_ONLY_STATE)
    assert fixed != _FINANCE_MALFORMED_ONLY_STATE, "window 절을 골라 실제로 정규화(no-op 아님)."
    assert "**87차**" in fixed and "**88차**" in fixed and "**89차**" in fixed
    assert "차차" not in fixed, "이중-차 잔존 0."
    assert "이름은 <repo>_<N> 로 짓는다" in fixed, "설명 절은 무접촉(비파괴)."


def test_normalize_all_malformed_window_enables_derive(hf):
    """DoD — 전부 오형식 window 도 정규화 후 infer 성공(max(87,88,89)+1=90)."""
    assert hf.infer_next_session_num(_FINANCE_MALFORMED_ONLY_STATE) == hf.TRIGGER_SESSION_PLACEHOLDER
    fixed = hf.normalize_session_anchors(_FINANCE_MALFORMED_ONLY_STATE)
    assert hf.infer_next_session_num(fixed) == 90


# ── CLI main(--normalize-session-anchors) ────────────────────────────────────

def test_main_normalize_apply_fixes_legacy(hf, capsys):
    """솔로 apply — legacy pm_state 오형식 교체·rc0·diff 출력·이중-차 잔존 0."""
    p = _write_legacy_state(hf, _MALFORMED_SESSION_SECTION)
    rc = hf.main(["--normalize-session-anchors"])
    assert rc == 0
    content = p.read_text(encoding="utf-8")
    assert "**52차**" in content and "차차" not in content
    out = capsys.readouterr().out
    assert "정규화 적용 완료" in out
    assert "차차" in out, "diff preview 에 제거되는 오형식 토큰 노출."


def test_main_normalize_needs_no_session_seq_or_wave(hf):
    """유지보수 모드는 session-seq/wave-summary 없이 동작(required 체크 우회·SystemExit 0)."""
    _write_legacy_state(hf, _MALFORMED_SESSION_SECTION)
    assert hf.main(["--normalize-session-anchors"]) == 0


def test_main_normalize_dry_run_leaves_file_unchanged(hf, capsys):
    """비파괴 dry-run — 원문 보존(파일 미변경)·diff preview·미적용 안내."""
    p = _write_legacy_state(hf, _MALFORMED_SESSION_SECTION)
    rc = hf.main(["--normalize-session-anchors", "--dry-run"])
    assert rc == 0
    assert p.read_text(encoding="utf-8") == _MALFORMED_SESSION_SECTION
    out = capsys.readouterr().out
    assert "[dry-run]" in out
    assert "차차" in out, "dry-run diff 에 오형식 노출."


def test_main_normalize_idempotent_second_run_noop(hf, capsys):
    """멱등 재현 — apply 후 재실행이 파일을 다시 안 바꾸고 no-op 안내(rc0)."""
    p = _write_legacy_state(hf, _MALFORMED_SESSION_SECTION)
    assert hf.main(["--normalize-session-anchors"]) == 0
    after_first = p.read_text(encoding="utf-8")
    capsys.readouterr()  # 첫 실행 출력 소거.
    assert hf.main(["--normalize-session-anchors"]) == 0
    assert p.read_text(encoding="utf-8") == after_first, "재실행 무변화(멱등)."
    assert "이미 정합" in capsys.readouterr().out


def test_main_normalize_noop_on_clean_file(hf, capsys):
    """이미 정합한 파일 — 변경 없음 no-op(rc0)."""
    _write_legacy_state(hf, _SESSION_SECTION)
    assert hf.main(["--normalize-session-anchors"]) == 0
    assert "변경 없음" in capsys.readouterr().out


def test_main_normalize_missing_file_is_noop(hf, capsys):
    """대상 pm_state 부재(솔로·미생성) — 명시 안내 후 no-op(rc0·크래시 0)."""
    assert hf.main(["--normalize-session-anchors"]) == 0
    assert "대상 파일이 없다" in capsys.readouterr().out


def test_main_normalize_targets_per_slot_pm_state(hf):
    """--session <repo>_<N> → 해당 슬롯 pm_state 를 정규화(솔로 legacy 아님·per-slot 대상)."""
    _make_single_self_host(hf._tmp)
    sp = _slot_path(hf, "project_manager_1")
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(_MALFORMED_SESSION_SECTION, encoding="utf-8")
    # legacy 는 손대면 안 됨(슬롯 대상 격리 대조).
    legacy = _write_legacy_state(hf, _MALFORMED_SESSION_SECTION)

    rc = hf.main(["--normalize-session-anchors", "--session", "project_manager_1"])
    assert rc == 0
    slot_content = sp.read_text(encoding="utf-8")
    assert "**52차**" in slot_content and "차차" not in slot_content, "슬롯 pm_state 정규화."
    assert legacy.read_text(encoding="utf-8") == _MALFORMED_SESSION_SECTION, "legacy 는 무접촉."


def test_main_normalize_finance_all_malformed_not_silent_noop(hf, capsys):
    """CLI end-to-end — finance 실 형상(설명 절 먼저·window 전부 오형식)에서 apply 가 window
    를 정규화하고 파일을 교체한다(silent no-op 아님·codex must-fix 회귀 가드)."""
    p = _write_legacy_state(hf, _FINANCE_MALFORMED_ONLY_STATE)
    rc = hf.main(["--normalize-session-anchors"])
    assert rc == 0
    content = p.read_text(encoding="utf-8")
    assert "**89차**" in content and "차차" not in content, "window 절 실제 교체."
    assert "정규화 적용 완료" in capsys.readouterr().out


