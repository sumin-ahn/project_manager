"""per-slot pm_state 경로 해소 + graceful 마이그레이션 + 미해소 fail-loud (T-0166·ADR-0033 §3.1).

pm_state 를 *슬롯별*로 분리한다 — multi-PM 연속성(여러 PM 슬롯이 한 clone 공유 보드 위에서
각자 핸드오프 상태 유지·spike §1.3·§3.1). 경로 = `.project_manager/.local/slots/<slot>/pm_state.md`
(gitignored·per-slot). slot 키 = lease 장부 슬롯과 동형(`<repo>_<N>`·`_auto_slot`·T-0123 재사용).

검증 세 축 (pm_handoff·pm_bootstrap):
  - **per-slot read/write**: 슬롯 해소(`<repo>_<N>`) → `.local/slots/<slot>/pm_state.md`.
  - **미해소 fail-loud**: 슬롯 미해소(`_auto_slot` None) → 경로를 지어내지 않고 중단.
  - **graceful 마이그레이션**: legacy 존재 + slot 경로 부재 → 첫 접근 시 slot 경로로 이동.

**hermetic 필수**: 각 도구의 모듈-레벨 `REPO`(import 시점 굳음)를 tmp 로 monkeypatch 한 fresh
모듈 인스턴스를 매 테스트마다 로드한다(test_board_root_external_tools 동류). 경로 해소가 *함수*
(호출 시점 REPO 추종)라 monkeypatch 된 tmp REPO 를 추종한다.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from pm_engine.app import slot_resolution
from pm_engine.infra import pm_paths

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











def _write_leases_multi(path, repo: str, ns: list[int]) -> None:
    """worktree-leases.json — 한 repo 의 여러 슬롯(`work/<repo>_<N>`)."""
    import json
    path.parent.mkdir(parents=True, exist_ok=True)
    leases = [{"repo": repo, "slot": f"work/{repo}_{n}"} for n in ns]
    path.write_text(json.dumps({"leases": leases}), encoding="utf-8")








def _write_leases_states(path, repo: str, slots: list[tuple[int, str]]) -> None:
    """worktree-leases.json — 한 repo 의 (슬롯N, state) 목록 (idle 회귀용)."""
    import json
    path.parent.mkdir(parents=True, exist_ok=True)
    leases = [{"repo": repo, "slot": f"work/{repo}_{n}", "state": st} for n, st in slots]
    path.write_text(json.dumps({"leases": leases}), encoding="utf-8")






# ── _pm_state_path: per-slot / 솔로 폴백 / graceful 마이그레이션 ──────────────











# ── divergent bare 슬롯 dir backfill 마이그레이션 (T-0201) ────────────────────

def _bare_slot_dir(hf, n: int = 1) -> Path:
    return hf._tmp / ".project_manager" / ".local" / "slots" / str(n)




















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
    assert slot_resolution._regression_cwd(pm_paths.home_at(hf.REPO), None, areas, leases) == str(hf._tmp)


# ══════════════════════════════════════════════════════════════════════════
# pm_handoff — PmHandoff.__init__ per-slot 배선 (명시 주입 vs 프로덕션)
# ══════════════════════════════════════════════════════════════════════════

def test_handoff_init_explicit_pm_state_honored(hf, tmp_path):
    """명시 pm_state_file 주입(테스트/override) → 그 경로 고정·explicit 플래그 True."""
    explicit = tmp_path / "explicit" / "pm_state.md"
    inst = hf.PmHandoff(pm_state_file=explicit)
    assert inst._pm_state_file == explicit
    assert inst._pm_state_file_explicit is True




# ══════════════════════════════════════════════════════════════════════════
# pm_bootstrap — _pm_state_display_path (첫-turn 안내 경로)
# ══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def bs(tmp_path, monkeypatch):
    mod = _load("pm_bootstrap")
    monkeypatch.setattr(mod, "REPO", tmp_path)
    mod._tmp = tmp_path
    return mod


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
















# ══════════════════════════════════════════════════════════════════════════
# 트랜잭션 계약 — 중단 게이트(기계회귀) red → legacy 미이동 (codex must-fix)
# ══════════════════════════════════════════════════════════════════════════
#
# 마이그레이션(legacy→slot replace())이 *중단 게이트 통과 후*·pm_state 첫 접촉 직전에만
# 일어나야 한다 — 기계회귀 red 로 중단되면 pm_state 무접촉(legacy 그대로·slot 미생성).
# (출하 step 은 ADR-0039 D4 로 비차단 surface 화 — 더는 중단 게이트가 아니다.)

_PYTEST_RED = "1 passed, 1 failed in 0.1s"  # is_pytest_green → False(중단).
_PYTEST_GREEN = "2 passed in 0.1s"          # is_pytest_green → True(통과).






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

def test_session_owns_untagged_slot1_only(bs):
    """무태그 entry 는 slot-1(`<repo>_1`) 만 자기 것으로 본다 — 미해소(None)는 소유 없음."""
    assert bs._session_owns_untagged(None) is False
    assert bs._session_owns_untagged("project_manager_1") is True


def test_session_owns_untagged_slot2plus_ignores(bs):
    """slot-2+ 는 무태그를 자기 것으로 보지 않는다(핵심 회귀 가드·codex 제언)."""
    assert bs._session_owns_untagged("project_manager_2") is False
    assert bs._session_owns_untagged("project_manager_11") is False


def test_session_owns_untagged_task_always_false(bs):
    """task 세션(`task:<name>`·F7·T-0356)은 무태그 legacy 를 자기 것으로 보지 않는다(항상 False).

    무태그 entry = 태그 도입 이전 솔로/slot-1 계보라 task 것이 아니다. sentinel 판별을 trailing
    `_N` 검사보다 앞에 둬, task 명이 우연히 `_1` 로 끝나도(`task:foo_1`) slot-1 오판(True)을 막는다."""
    assert bs._session_owns_untagged("task:mytask") is False
    assert bs._session_owns_untagged("task:foo_1") is False   # `_1` trailing 오판 회귀 가드.
    assert bs._session_owns_untagged("task:한글작업") is False


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

# ── MF-2: fresh slot-2+ 는 전역 폴백 금지 (타 슬롯 handoff 본문·branch·reattach 유입 0) ──

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




# ── should-fix: 정규식 태그 캡처를 canonical `<repo>_<N>` 로 제약 (서술형 괄호 오캡처 방지) ──

def test_handoff_regex_ignores_descriptive_parens_when_untagged(bs):
    """should-fix: 서술형 괄호(`PM 4차 (아침 대화)`)는 세션 태그로 오인 안 됨(drop 0)."""
    log = "## [2026-07-10] handoff | PM 4차 (아침 대화) → 다음 PM 세션\n- 무태그 본문.\n"
    # bound 미해소 — 서술형 괄호를 태그로 오캡처하면 drop 되어 None 이 됐을 것.
    assert bs.parse_last_handoff_session_num(log) == 4
    entry = bs.extract_slot_handoff_entry(log)
    assert entry is not None and "무태그 본문" in entry["body"]


def test_handoff_regex_canonical_tag_still_captured(bs):
    """canonical `(<repo>_<N>)` 는 여전히 세션 태그로 캡처(제약이 정상 태그를 막지 않음)."""
    log = "## [2026-07-10] handoff | PM 4차 (project_manager_2) → 다음 PM 세션\n- t.\n"
    # 양성 슬롯(slot-2)은 자기 태그 소유 → 4. 태그 캡처가 정상 동작함을 이 단언이 입증한다.
    assert bs.parse_last_handoff_session_num(log, bound_session="project_manager_2") == 4
    # 미해소(None)는 전역 tag-agnostic 파싱 → 태그된 4 도 차수로 복원(codex R4·차수 유실 금지).
    assert bs.parse_last_handoff_session_num(log) == 4


def test_handoff_regex_descriptive_parens_with_number_not_canonical(bs):
    """서술형에 숫자가 있어도(`(회의 3)`) 후행 `_N` 없으면 태그 아님 — 무태그로 읽힌다."""
    log = "## [2026-07-10] handoff | PM 5차 (회의 3) → 다음 PM 세션\n- 본문.\n"
    assert bs.parse_last_handoff_session_num(log) == 5


# ── task 태그 파서 (F7·T-0356) — sentinel `task:` 캡처·소유·차수/본문 추론·슬롯 회귀 불변 ──


def test_handoff_regex_captures_task_tag(bs):
    """RE 가 sentinel 태그 `(task:<name>)` 를 세션 그룹으로 캡처한다 (자유 포맷·한글·하이픈·언더스코어).

    task 명은 validator 가 공백·괄호를 거부(T-0356 협소화)하므로 태그엔 그 문자가 안 온다 — RE 소비측은
    `task:[^)]+` 로 넓게 두되(관용) 캡처 케이스는 유효 명(한글 포함)으로 커버한다."""
    for name in ("mytask", "my-task", "한글작업", "job_v2"):
        line = f"## [2026-07-10] handoff | PM 7차 (task:{name}) → 다음 PM 세션"
        m = bs._LOG_HANDOFF_HEADER_RE.search(line)
        assert m is not None and m.group("session") == f"task:{name}", name


def test_handoff_task_tag_owned_and_num_inferred(bs):
    """task bound_session(`task:<name>`)이 자기 태그 entry 만 소유 — 차수 추론·본문 추출 성공.

    entry 추출/차수 추론(DoD)이 task 태그로 동작함을 못박는다: 태그된 최고차를 차수로 잡고
    (`parse_last_handoff_session_num`), 자기-task handoff 본문을 dump 한다(`extract_slot_handoff_entry`)."""
    log = (
        "## [2026-07-10] handoff | PM 3차 (task:mytask) → 다음 PM 세션\n"
        "- mytask 3차 본문.\n\n"
        "## [2026-07-11] handoff | PM 4차 (task:mytask) → 다음 PM 세션\n"
        "- mytask 4차 본문.\n"
    )
    assert bs.parse_last_handoff_session_num(log, bound_session="task:mytask") == 4
    entry = bs.extract_slot_handoff_entry(log, bound_session="task:mytask")
    assert entry is not None and "mytask 4차 본문" in entry["body"]


def test_handoff_task_tag_excludes_other_task_and_slot(bs):
    """task bound 는 타 task·슬롯 태그 entry 를 자기 것으로 안 본다(cross-anchor 유입 차단)."""
    log = (
        "## [2026-07-10] handoff | PM 9차 (project_manager_2) → 다음 PM 세션\n- 슬롯 본문.\n\n"
        "## [2026-07-11] handoff | PM 3차 (task:other) → 다음 PM 세션\n- other 본문.\n\n"
        "## [2026-07-12] handoff | PM 5차 (task:mytask) → 다음 PM 세션\n- mytask 본문.\n"
    )
    # mytask 는 자기 태그(5차)만 — 슬롯 9차·other 3차 유입 0.
    assert bs.parse_last_handoff_session_num(log, bound_session="task:mytask") == 5
    entry = bs.extract_slot_handoff_entry(log, bound_session="task:mytask")
    assert entry is not None and "mytask 본문" in entry["body"]
    assert "슬롯 본문" not in entry["body"] and "other 본문" not in entry["body"]


def test_handoff_slot_bound_ignores_task_tags(bs):
    """슬롯 회귀 불변 — 슬롯 bound(`<repo>_<N>`)는 task 태그 entry 를 자기 것으로 안 본다.

    task 태그 도입이 슬롯 필터를 오염시키지 않음을 못박는다(양방향 격리)."""
    log = (
        "## [2026-07-10] handoff | PM 8차 (task:mytask) → 다음 PM 세션\n- task 본문.\n\n"
        "## [2026-07-11] handoff | PM 2차 (project_manager_2) → 다음 PM 세션\n- 슬롯2 본문.\n"
    )
    assert bs.parse_last_handoff_session_num(log, bound_session="project_manager_2") == 2


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
    """legacy pm_state(`wiki/pm_state.md`)에 텍스트를 쓴다 (normalize CLI 대상).

    normalize 는 *해소된 슬롯*의 pm_state 를 대상으로 하므로 홈 슬롯 행도 함께 깐다 —
    slot 경로가 아직 없고 legacy 가 있으면 읽기 위치는 legacy 그대로다(이동 없음).
    """
    _make_single_self_host(hf._tmp)
    p = _legacy(hf)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p



















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






















