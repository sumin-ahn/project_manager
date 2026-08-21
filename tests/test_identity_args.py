"""identity_args — 공용 정체성 인자 모듈 단위테스트 (T-0322 · ADR-0057).

엔진 canonical(루트 `.project_manager/tools/identity_args.py`)을 importlib 로 직접 로드해
검증한다(도구는 패키지가 아니므로 `spec_from_file_location` 관용구 — 기존 test_pm_bootstrap_*
동류). 두 층을 각각 hermetic 하게 검증한다:

  - **순수 인자 층**: `add_identity_args`·`parse_identity` — 파일 IO 0. 실 argparse 로 파싱한
    `Namespace` 를 넣어 discriminated `Identity` 해소 규칙 전수(slot/repo/none/fail-loud)를 본다.
  - **리스 IO 층**: `leased_sessions`·`repo_slot_numbers`·`resolve_actor_slot` — `tmp_path` 에
    `worktree-leases.json` 을 직접 써서(실 장부 미접촉) 흡수된 두 원 구현(board/pm_config
    `_leased_sessions` · pm_bootstrap `_repo_slot_numbers`)의 동작이 정확히 보존됐는지 + 그 위의
    `resolve_actor_slot`(1개/0개/≥2개) 를 검증한다.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"


def _load(name: str = "identity_args"):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def ia():
    return _load()


def _write_leases(path: Path, entries: list[dict]) -> None:
    """worktree-leases.json — {"leases": [...]} 스키마 (worktree_pool.Lease.to_dict 동형)."""
    path.write_text(json.dumps({"leases": entries}), encoding="utf-8")


def _parse(ia, argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    ia.add_identity_args(parser)
    return parser.parse_args(argv)


# ── add_identity_args (순수) ──────────────────────────────────────────────


def test_add_identity_args_registers_repo_and_slot(ia):
    ns = _parse(ia, ["--repo", "A", "--slot", "2"])
    assert ns.repo == "A"
    assert ns.slot == 2  # type=int


def test_add_identity_args_defaults_none_when_omitted(ia):
    ns = _parse(ia, [])
    assert ns.repo is None
    assert ns.slot is None
    assert ns.task is None  # T-0353 additive 축 — 기본 None(기존 무인자 무영향).


def test_add_identity_args_registers_task(ia):
    # T-0353 — `--task` 축 additive. 슬롯 축과 직교(공존 가능).
    ns = _parse(ia, ["--task", "payments-refactor"])
    assert ns.task == "payments-refactor"
    assert ns.repo is None and ns.slot is None
    ns2 = _parse(ia, ["--task", "job1", "--repo", "A", "--slot", "2"])
    assert ns2.task == "job1" and ns2.repo == "A" and ns2.slot == 2


# ── parse_identity — discriminated 해소 규칙 전수 (ADR-0057 §3.1) ────────────


def test_parse_identity_repo_and_slot_yields_slot_kind(ia):
    ns = _parse(ia, ["--repo", "myproj", "--slot", "3"])
    identity = ia.parse_identity(ns)
    assert identity.kind == "slot"
    assert identity.repo == "myproj"
    assert identity.slot == 3
    assert identity.session == "myproj_3"


def test_parse_identity_repo_alone_yields_repo_kind(ia):
    ns = _parse(ia, ["--repo", "myproj"])
    identity = ia.parse_identity(ns)
    assert identity.kind == "repo"
    assert identity.repo == "myproj"
    assert identity.slot is None
    assert identity.session is None


def test_parse_identity_no_args_yields_none_kind(ia):
    ns = _parse(ia, [])
    identity = ia.parse_identity(ns)
    assert identity.kind == "none"
    assert identity.repo is None
    assert identity.slot is None
    assert identity.session is None


def test_parse_identity_slot_without_repo_fails_loud(ia):
    ns = _parse(ia, ["--slot", "1"])
    with pytest.raises(ValueError, match=r"--slot 은 --repo 필수"):
        ia.parse_identity(ns)


def test_parse_identity_slot_below_one_fails_loud(ia):
    # pm_bootstrap slot≥1 계약 보존 (codex 게이트·test_bootstrap_slot_below_one_rejected 정합) —
    # canonical parse_identity 가 slot 0/음수를 수용하면 T-0315 채택 시 회귀.
    for bad in ("0", "-1"):
        ns = _parse(ia, ["--repo", "A", "--slot", bad])
        with pytest.raises(ValueError, match=r"1 이상"):
            ia.parse_identity(ns)


def test_parse_identity_repo_name_with_underscore_composes_cleanly(ia):
    # repo 이름에 "_" 가 있어도 session 조립은 단순 f-string(파싱 아님) — 무해(ADR-0057 caller 노트).
    ns = _parse(ia, ["--repo", "my_repo", "--slot", "7"])
    identity = ia.parse_identity(ns)
    assert identity.kind == "slot"
    assert identity.session == "my_repo_7"


def test_parse_identity_returns_discriminated_dataclass_not_bare_string(ia):
    # PM 67 리뷰 A 수정 — 모호한 단일 문자열이 아니라 kind 로 분기 가능한 결과여야 한다.
    ns = _parse(ia, ["--repo", "A", "--slot", "1"])
    identity = ia.parse_identity(ns)
    assert not isinstance(identity, str)
    assert hasattr(identity, "kind")


# ── task 축 귀속 + 예약 패턴 거부 (T-0353·⑥) ─────────────────────────────────


def test_parse_identity_task_is_orthogonal_to_slot_axis(ia):
    """`--task` 는 slot 축과 직교 — kind 는 repo/slot 로 그대로 결정되고 task 만 실린다(⑥)."""
    # task 단독 → kind 는 여전히 none(슬롯 축 없음)·task 값만.
    idn = ia.parse_identity(_parse(ia, ["--task", "myjob"]))
    assert idn.kind == "none" and idn.task == "myjob"
    assert idn.repo is None and idn.slot is None
    # task + repo/slot → kind=slot(현행 규칙 불변)·task 공존.
    ids = ia.parse_identity(_parse(ia, ["--task", "myjob", "--repo", "A", "--slot", "2"]))
    assert ids.kind == "slot" and ids.session == "A_2" and ids.task == "myjob"


def test_parse_identity_task_absent_is_none(ia):
    """`--task` 미지정이면 Identity.task 는 None — 기존 caller 무영향(100% 불변)."""
    assert ia.parse_identity(_parse(ia, ["--repo", "A", "--slot", "1"])).task is None
    assert ia.parse_identity(_parse(ia, [])).task is None


def test_is_reserved_task_name_rejects_registered_repo_slot_pattern(ia):
    """`<등록 repo>_<N>` 패턴 task 명은 예약 거부(⑥·슬롯 세션명 충돌 방지)."""
    registered = ["project_manager", "finance"]
    # 등록 repo + _정수 → 예약(거부).
    assert ia.is_reserved_task_name("project_manager_1", registered) is True
    assert ia.is_reserved_task_name("finance_12", registered) is True
    # repo 이름에 언더스코어가 있어도 마지막 _N 을 정확히 매칭(전체 앵커).
    assert ia.is_reserved_task_name("my_repo_3", ["my_repo"]) is True


def test_is_reserved_task_name_allows_free_format(ia):
    """등록 repo 와 무관한 자유 포맷 task 명은 허용(⑥ — 실재 슬롯과만 충돌 방지)."""
    registered = ["project_manager", "finance"]
    for ok in ("payments-refactor", "project_manager", "sikdan_2", "hotfix", "job_v2_thing"):
        # sikdan_2: sikdan 은 미등록 repo 라 _2 여도 슬롯 세션과 충돌 안 함 → 허용.
        assert ia.is_reserved_task_name(ok, registered) is False, ok
    # 등록 repo 지만 _N 형태가 아니면 허용(예: 이름만·trailing 비정수).
    assert ia.is_reserved_task_name("finance_prod", registered) is False
    assert ia.is_reserved_task_name("finance", registered) is False


# ── leased_sessions — board/pm_config `_leased_sessions` 흡수 ────────────────


def test_leased_sessions_returns_only_leased_state(ia, tmp_path):
    leases = tmp_path / "worktree-leases.json"
    _write_leases(leases, [
        {"slot": "work/A_1", "repo": "A", "session": "A_1", "state": "leased"},
        {"slot": "work/A_2", "repo": "A", "session": "A_2", "state": "idle"},
        {"slot": "work/B_1", "repo": "B", "session": "B_1", "state": "leased"},
    ])
    assert ia.leased_sessions(leases) == ["A_1", "B_1"]


def test_leased_sessions_excludes_empty_or_missing_session(ia, tmp_path):
    leases = tmp_path / "worktree-leases.json"
    _write_leases(leases, [
        {"slot": "work/A_1", "repo": "A", "session": "", "state": "leased"},
        {"slot": "work/A_2", "repo": "A", "state": "leased"},  # session 키 부재
    ])
    assert ia.leased_sessions(leases) == []


def test_leased_sessions_missing_state_key_is_excluded(ia, tmp_path):
    # board/pm_config 원 `_leased_sessions` 동형 — state 키 부재는 "leased" 로 back-compat 안
    # 한다(strict `== "leased"`). repo_slot_numbers 의 back-compat 과 의도된 비대칭(docstring 참고).
    leases = tmp_path / "worktree-leases.json"
    _write_leases(leases, [
        {"slot": "work/A_1", "repo": "A", "session": "A_1"},  # state 키 없음
    ])
    assert ia.leased_sessions(leases) == []


def test_leased_sessions_missing_file_returns_empty(ia, tmp_path):
    assert ia.leased_sessions(tmp_path / "absent.json") == []


def test_leased_sessions_corrupt_json_returns_empty(ia, tmp_path):
    leases = tmp_path / "worktree-leases.json"
    leases.write_text("{not valid json", encoding="utf-8")
    assert ia.leased_sessions(leases) == []


def test_leased_sessions_schema_mismatch_returns_empty(ia, tmp_path):
    leases = tmp_path / "worktree-leases.json"
    leases.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    assert ia.leased_sessions(leases) == []


# ── lease_row_count — 상태 무관 행 수 · 부재/손상 구분 (T-0792 · F-001 리뷰 라운드 03 must-fix) ──
# 부재/정상-파싱-0행 = "확인된 0"(유도 허용) · 읽기실패/JSON파손/스키마불일치 = "손상"(None·유도
# 차단) — 두 결론을 접으면 실제로 풀 행을 보유한 손상 장부도 오해소된다(리뷰 재현).


def test_lease_row_count_counts_all_states(ia, tmp_path):
    leases = tmp_path / "worktree-leases.json"
    _write_leases(leases, [
        {"slot": "work/A_1", "repo": "A", "session": "A_1", "state": "leased"},
        {"slot": "work/A_2", "repo": "A", "session": "A_2", "state": "idle"},
    ])
    assert ia.lease_row_count(leases) == 2


def test_lease_row_count_missing_file_returns_zero(ia, tmp_path):
    """장부 파일 자체가 없음(fresh 솔로 홈) → 확인된 0(유도 허용 대상)."""
    assert ia.lease_row_count(tmp_path / "absent.json") == 0


def test_lease_row_count_empty_leases_list_returns_zero(ia, tmp_path):
    """정상 파싱된 `leases` 배열이 빈 리스트 → 확인된 0(유도 허용 대상)."""
    leases = tmp_path / "worktree-leases.json"
    _write_leases(leases, [])
    assert ia.lease_row_count(leases) == 0


def test_lease_row_count_corrupt_json_returns_none(ia, tmp_path):
    """손상 3형 ① JSON 파손 — 파일은 존재하나 파싱 불가 → `None`(행 수 모름·유도 차단).

    F-001 수정 전: 이 경우도 `0` 으로 접혀 단일-등록 유도가 오발화했다(리뷰 재현).
    """
    leases = tmp_path / "worktree-leases.json"
    leases.write_text("{not valid json", encoding="utf-8")
    assert ia.lease_row_count(leases) is None


def test_lease_row_count_schema_mismatch_returns_none(ia, tmp_path):
    """손상 3형 ② 최상위 스키마 불일치(dict 아님) → `None`."""
    leases = tmp_path / "worktree-leases.json"
    leases.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    assert ia.lease_row_count(leases) is None


def test_lease_row_count_read_failure_returns_none(ia, tmp_path):
    """손상 3형 ③ 읽기 실패 — 경로가 파일이 아니라 디렉터리(실제 OSError 유발) → `None`.

    `exists()` 는 True(디렉터리도 존재)이므로 "부재" 분기를 안 타고, 뒤이은 읽기가
    `IsADirectoryError`(OSError 서브클래스)로 실패해야 손상으로 정확히 분류된다.
    """
    leases = tmp_path / "worktree-leases.json"
    leases.mkdir()
    assert ia.lease_row_count(leases) is None


# ── single_registration_session — 세 모듈 공유 술어 (F-002 리뷰 라운드 03 PM 재비준) ──────


def test_single_registration_session_derives_when_registered_one_and_ledger_absent(ia, tmp_path):
    leases = tmp_path / "absent.json"
    assert ia.single_registration_session({"solo"}, leases) == "solo_1"


def test_single_registration_session_derives_when_ledger_empty_list(ia, tmp_path):
    leases = tmp_path / "worktree-leases.json"
    _write_leases(leases, [])
    assert ia.single_registration_session({"solo"}, leases) == "solo_1"


def test_single_registration_session_none_when_ledger_has_row(ia, tmp_path):
    leases = tmp_path / "worktree-leases.json"
    _write_leases(leases, [
        {"slot": "work/other_9", "repo": "solo", "session": "other_9", "state": "idle"},
    ])
    assert ia.single_registration_session({"solo"}, leases) is None


def test_single_registration_session_none_when_two_registered(ia, tmp_path):
    leases = tmp_path / "absent.json"
    assert ia.single_registration_session({"a", "b"}, leases) is None


def test_single_registration_session_none_when_zero_registered(ia, tmp_path):
    leases = tmp_path / "absent.json"
    assert ia.single_registration_session(set(), leases) is None


def test_single_registration_session_none_when_ledger_corrupt(ia, tmp_path):
    """F-001 공유 — 손상 장부(파싱 불가)는 등록 1개라도 유도하지 않는다(행 있는 것과 동일 취급)."""
    leases = tmp_path / "worktree-leases.json"
    leases.write_text("{not valid json", encoding="utf-8")
    assert ia.single_registration_session({"solo"}, leases) is None


# ── repo_slot_numbers — pm_bootstrap `_repo_slot_numbers` 흡수 ───────────────


def test_repo_slot_numbers_dedup_and_sorted(ia, tmp_path):
    leases = tmp_path / "worktree-leases.json"
    _write_leases(leases, [
        {"slot": "work/A_2", "repo": "A", "session": "A_2", "state": "leased"},
        {"slot": "work/A_1", "repo": "A", "session": "A_1", "state": "leased"},
        {"slot": "work/A_1", "repo": "A", "session": "A_1", "state": "leased"},  # 중복
    ])
    assert ia.repo_slot_numbers("A", leases) == [1, 2]


def test_repo_slot_numbers_filters_other_repo(ia, tmp_path):
    leases = tmp_path / "worktree-leases.json"
    _write_leases(leases, [
        {"slot": "work/A_1", "repo": "A", "session": "A_1", "state": "leased"},
        {"slot": "work/B_1", "repo": "B", "session": "B_1", "state": "leased"},
    ])
    assert ia.repo_slot_numbers("A", leases) == [1]


def test_repo_slot_numbers_excludes_idle(ia, tmp_path):
    leases = tmp_path / "worktree-leases.json"
    _write_leases(leases, [
        {"slot": "work/A_1", "repo": "A", "session": "A_1", "state": "leased"},
        {"slot": "work/A_2", "repo": "A", "session": "A_2", "state": "idle"},
    ])
    assert ia.repo_slot_numbers("A", leases) == [1]


def test_repo_slot_numbers_missing_state_key_defaults_leased(ia, tmp_path):
    # pm_bootstrap 원 구현 동형 back-compat — state 키 부재는 leased 취급(leased_sessions 과 비대칭).
    leases = tmp_path / "worktree-leases.json"
    _write_leases(leases, [
        {"slot": "work/A_1", "repo": "A", "session": "A_1"},  # state 키 없음
    ])
    assert ia.repo_slot_numbers("A", leases) == [1]


def test_repo_slot_numbers_no_match_returns_empty_list(ia, tmp_path):
    # repo 는 실재하나(장부 read 성공) 활성 슬롯 0개 — "읽을 수 없음"(None) 과 구분되는 [].
    leases = tmp_path / "worktree-leases.json"
    _write_leases(leases, [
        {"slot": "work/B_1", "repo": "B", "session": "B_1", "state": "leased"},
    ])
    assert ia.repo_slot_numbers("A", leases) == []


def test_repo_slot_numbers_missing_file_returns_none(ia, tmp_path):
    assert ia.repo_slot_numbers("A", tmp_path / "absent.json") is None


def test_repo_slot_numbers_corrupt_json_returns_none(ia, tmp_path):
    leases = tmp_path / "worktree-leases.json"
    leases.write_text("{not valid json", encoding="utf-8")
    assert ia.repo_slot_numbers("A", leases) is None


def test_repo_slot_numbers_schema_mismatch_returns_none(ia, tmp_path):
    leases = tmp_path / "worktree-leases.json"
    leases.write_text(json.dumps({"leases": "not-a-list"}), encoding="utf-8")
    assert ia.repo_slot_numbers("A", leases) is None


# ── resolve_actor_slot — actor `--repo`-단독 해소 (1개/0개/≥2개 · SlotResolutionError) ──


def test_resolve_actor_slot_single_active_slot_returns_session(ia, tmp_path):
    leases = tmp_path / "worktree-leases.json"
    _write_leases(leases, [
        {"slot": "work/A_1", "repo": "A", "session": "A_1", "state": "leased"},
    ])
    assert ia.resolve_actor_slot("A", leases) == "A_1"


def test_resolve_actor_slot_zero_active_slots_returns_none(ia, tmp_path):
    leases = tmp_path / "worktree-leases.json"
    _write_leases(leases, [
        {"slot": "work/A_1", "repo": "A", "session": "A_1", "state": "idle"},
    ])
    assert ia.resolve_actor_slot("A", leases) is None


def test_resolve_actor_slot_missing_leases_file_returns_none(ia, tmp_path):
    assert ia.resolve_actor_slot("A", tmp_path / "absent.json") is None


def test_resolve_actor_slot_multiple_active_slots_fails_loud(ia, tmp_path):
    leases = tmp_path / "worktree-leases.json"
    _write_leases(leases, [
        {"slot": "work/A_1", "repo": "A", "session": "A_1", "state": "leased"},
        {"slot": "work/A_2", "repo": "A", "session": "A_2", "state": "leased"},
    ])
    with pytest.raises(ia.SlotResolutionError, match=r"--slot"):
        ia.resolve_actor_slot("A", leases)


def test_resolve_actor_slot_error_is_dedicated_exception_type(ia, tmp_path):
    leases = tmp_path / "worktree-leases.json"
    _write_leases(leases, [
        {"slot": "work/A_1", "repo": "A", "session": "A_1", "state": "leased"},
        {"slot": "work/A_2", "repo": "A", "session": "A_2", "state": "leased"},
        {"slot": "work/A_3", "repo": "A", "session": "A_3", "state": "leased"},
    ])
    with pytest.raises(Exception) as excinfo:
        ia.resolve_actor_slot("A", leases)
    assert isinstance(excinfo.value, ia.SlotResolutionError)
    assert isinstance(excinfo.value, Exception)
    assert "A" in str(excinfo.value)


# ── F6 작업공간 2단 해소 — resolve_task_workspace (T-0355·spike §3b F6·결정 ⑦) ──────
# 표 4행: (a) --repo X --slot N=그 슬롯(미보유=에러) (b) --repo X=유일해소/모호=에러
# (c) 아무것도 없음=통틀어 유일/모호=에러 (d) --slot 단독=parse 단계 ValueError.
# + readonly 공유 슬롯(role=readonly) carve-out(소유검사 비적용) + cwd 비참여(T-0345 불변).


def _tw(ia, argv: list[str], leases: Path):
    return ia.resolve_task_workspace(ia.parse_identity(_parse(ia, argv)), leases)


def test_f6_none_unique_holding_auto_resolves(ia, tmp_path):
    """행(c): 위치 인자 없음 + task 보유가 통틀어 유일 → 그 슬롯 자동 해소."""
    leases = tmp_path / "worktree-leases.json"
    _write_leases(leases, [
        {"slot": "work/A_1", "repo": "A", "session": "job1", "state": "leased",
         "test_cmd": "pytest -q"},
    ])
    ws = _tw(ia, ["--task", "job1"], leases)
    assert ws.slot == "work/A_1"
    assert ws.repo == "A"
    assert ws.session == "job1"
    assert ws.test_cmd == "pytest -q"
    assert ws.readonly is False


def test_f6_none_ambiguous_holding_raises(ia, tmp_path):
    """행(c) 모호(⑦): 암묵 선택 없이 잉여 슬롯 pm_config release를 안내한다."""
    leases = tmp_path / "worktree-leases.json"
    _write_leases(leases, [
        {"slot": "work/A_1", "repo": "A", "session": "job2", "state": "leased"},
        {"slot": "work/B_1", "repo": "B", "session": "job2", "state": "leased"},
    ])
    with pytest.raises(
        ia.WorkspaceResolutionError,
        match=r"pm_config\.py release <slot> --task job2",
    ):
        _tw(ia, ["--task", "job2"], leases)


def test_f6_repo_only_unique_in_repo_resolves(ia, tmp_path):
    """행(b): --repo X 만 + task 가 X 에서 유일 보유 → 그 슬롯."""
    leases = tmp_path / "worktree-leases.json"
    _write_leases(leases, [
        {"slot": "work/A_1", "repo": "A", "session": "job3", "state": "leased"},
        {"slot": "work/B_1", "repo": "B", "session": "job3", "state": "leased"},
    ])
    ws = _tw(ia, ["--task", "job3", "--repo", "B"], leases)
    assert ws.slot == "work/B_1" and ws.repo == "B"


def test_f6_repo_only_multiple_in_repo_raises(ia, tmp_path):
    """행(b) 모호(⑦): --repo X 만인데 X 에서 2개↑ 보유 → 에러(번호 요구)."""
    leases = tmp_path / "worktree-leases.json"
    _write_leases(leases, [
        {"slot": "work/A_1", "repo": "A", "session": "job4", "state": "leased"},
        {"slot": "work/A_2", "repo": "A", "session": "job4", "state": "leased"},
    ])
    with pytest.raises(ia.WorkspaceResolutionError, match=r"--slot"):
        _tw(ia, ["--task", "job4", "--repo", "A"], leases)


def test_f6_repo_only_none_in_repo_raises(ia, tmp_path):
    """행(b) 경계: --repo X 만인데 task 가 X 에서 0개 보유 → 에러(대여 안내)."""
    leases = tmp_path / "worktree-leases.json"
    _write_leases(leases, [
        {"slot": "work/A_1", "repo": "A", "session": "job5", "state": "leased"},
    ])
    with pytest.raises(ia.WorkspaceResolutionError, match=r"alloc"):
        _tw(ia, ["--task", "job5", "--repo", "Z"], leases)


def test_f6_slot_owned_by_task_resolves(ia, tmp_path):
    """행(a): --repo X --slot N 이 내 task 보유 슬롯 → 그 작업공간."""
    leases = tmp_path / "worktree-leases.json"
    _write_leases(leases, [
        {"slot": "work/A_1", "repo": "A", "session": "job6", "state": "leased"},
        {"slot": "work/A_2", "repo": "A", "session": "job6", "state": "leased"},
    ])
    ws = _tw(ia, ["--task", "job6", "--repo", "A", "--slot", "2"], leases)
    assert ws.slot == "work/A_2"


def test_f6_slot_not_owned_by_task_raises(ia, tmp_path):
    """행(a): --repo X --slot N 이 내 task 보유 아님 → 에러(F6 소유검사)."""
    leases = tmp_path / "worktree-leases.json"
    _write_leases(leases, [
        {"slot": "work/A_1", "repo": "A", "session": "job7", "state": "leased"},
    ])
    with pytest.raises(ia.WorkspaceResolutionError, match=r"보유가 아니"):
        _tw(ia, ["--task", "job7", "--repo", "A", "--slot", "9"], leases)


def test_f6_slot_only_without_repo_rejected_at_parse(ia):
    """행(d): --slot 단독(--repo 없음)은 parse_identity 가 이미 ValueError (repo 없는 번호는 식별자 아님)."""
    ns = _parse(ia, ["--task", "job8", "--slot", "3"])  # argparse 자체는 통과(조합 규칙은 parse_identity)
    with pytest.raises(ValueError, match=r"--repo"):
        ia.parse_identity(ns)


def test_f6_readonly_slot_carve_out_skips_ownership(ia, tmp_path):
    """readonly 공유 슬롯(role=readonly·⑬) carve-out — 내 task 보유 아니어도 조회 지칭 허용."""
    leases = tmp_path / "worktree-leases.json"
    _write_leases(leases, [
        {"slot": "work/R_1", "repo": "R", "role": "readonly", "state": "leased"},
    ])
    ws = _tw(ia, ["--task", "job9", "--repo", "R", "--slot", "1"], leases)
    assert ws.slot == "work/R_1"
    assert ws.readonly is True


def test_f6_non_readonly_unowned_slot_still_raises(ia, tmp_path):
    """carve-out 은 role=readonly 에만 — 남의 task 가 쓰는 work 슬롯은 여전히 소유검사 거부."""
    leases = tmp_path / "worktree-leases.json"
    _write_leases(leases, [
        {"slot": "work/R_1", "repo": "R", "session": "other", "state": "leased"},
    ])
    with pytest.raises(ia.WorkspaceResolutionError):
        _tw(ia, ["--task", "job9", "--repo", "R", "--slot", "1"], leases)


def test_f6_ignores_idle_slots(ia, tmp_path):
    """idle(반납) 슬롯은 보유로 안 센다 — leased 만 F6 대상(slots_for_task 정합)."""
    leases = tmp_path / "worktree-leases.json"
    _write_leases(leases, [
        {"slot": "work/A_1", "repo": "A", "session": "job10", "state": "idle"},
        {"slot": "work/A_2", "repo": "A", "session": "job10", "state": "leased"},
    ])
    ws = _tw(ia, ["--task", "job10"], leases)
    assert ws.slot == "work/A_2"  # idle A_1 제외 → A_2 유일


def test_f6_state_absent_treated_as_leased(ia, tmp_path):
    """state 키 부재 = leased 로 본다 (worktree_pool.from_dict default·back-compat 정합)."""
    leases = tmp_path / "worktree-leases.json"
    _write_leases(leases, [
        {"slot": "work/A_1", "repo": "A", "session": "job11"},  # state 부재
    ])
    ws = _tw(ia, ["--task", "job11"], leases)
    assert ws.slot == "work/A_1"


def test_f6_cwd_does_not_participate(ia, tmp_path, monkeypatch):
    """cwd 비참여(T-0345 불변) — 실행 위치를 어디서 호출하든 장부+명시 인자로만 해소."""
    leases = tmp_path / "worktree-leases.json"
    _write_leases(leases, [
        {"slot": "work/A_1", "repo": "A", "session": "job12", "state": "leased"},
        {"slot": "work/B_1", "repo": "B", "session": "job12", "state": "leased"},
    ])
    # cwd 를 B_1 worktree 로 바꿔도 --repo 명시가 A 면 A 로 해소(cwd 가 B 로 끌어당기지 않음).
    monkeypatch.chdir(tmp_path)
    ws = _tw(ia, ["--task", "job12", "--repo", "A"], leases)
    assert ws.slot == "work/A_1"


# ── task_prefix — F5 task 설정 prefix point-read (T-0355) ─────────────────────


def test_task_prefix_reads_from_tasks_collection(ia, tmp_path):
    leases = tmp_path / "worktree-leases.json"
    leases.write_text(json.dumps({
        "leases": [],
        "tasks": [{"name": "job1", "prefix": "PAY"}, {"name": "job2", "prefix": None}],
    }), encoding="utf-8")
    assert ia.task_prefix("job1", leases) == "PAY"
    assert ia.task_prefix("job2", leases) is None      # 미설정
    assert ia.task_prefix("absent", leases) is None    # 없는 task
    assert ia.task_prefix("job1", tmp_path / "absent.json") is None  # 장부 부재 fail-soft


# ── validate_task_name — CLI 층 공유 validator (T-0355 게이트 must-fix·깔때기 검증) ──


@pytest.mark.parametrize("bad", [
    "", "   ", "a b", "a\tb", "foo)bar", "(x)", "a/b", "a\\b",
    ".hidden", "..", ".", "sub/name",
])
def test_validate_task_name_rejects_unsafe(ia, bad):
    """공백/괄호/path/선행 `.` 등 하류 표면 파손 문자를 fail-loud(InvalidTaskName·ValueError)."""
    with pytest.raises(ia.InvalidTaskName):
        ia.validate_task_name(bad)
    # ValueError 서브클래스라 caller 의 기존 except ValueError 가 잡는다.
    with pytest.raises(ValueError):
        ia.validate_task_name(bad)


@pytest.mark.parametrize("ok", ["job1", "payments-refactor", "결제_리팩터", "a_2_3", "T-0355work"])
def test_validate_task_name_accepts_safe(ia, ok):
    """한글·하이픈·언더스코어·숫자 단일 이름은 통과(부작용 0·예외 미발생)."""
    ia.validate_task_name(ok)                    # registered_repos 없이 통과
    ia.validate_task_name(ok, ["other"])         # 무관 repo 예약패턴 미충돌


def test_validate_task_name_rejects_reserved_slot_pattern(ia):
    """registered_repos 주면 `<repo>_<N>` 슬롯 세션 예약 패턴 거부(⑥·is_reserved_task_name 재사용)."""
    with pytest.raises(ia.InvalidTaskName, match=r"예약"):
        ia.validate_task_name("project_manager_1", ["project_manager"])
    # 미등록 repo 로 시작하는 _N 은 무관(자유 포맷 허용).
    ia.validate_task_name("project_manager_1", ["finance"])
