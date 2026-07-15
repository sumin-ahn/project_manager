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
