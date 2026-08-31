"""추가 리뷰어 실행 조건 파리티 — 위임과 같은 cwd·env·심볼 부재 (T-0887).

claude·codex·opencode 는 같은 등급의 하네스이고, 세 하네스에 developer·architect·code-reviewer 를
모두 위임한다. 그런데 엔진은 한때 추가 리뷰어 역할에만 저장소 거울·임시 홈·리뷰어 전용 env
allowlist 를 붙였다 — 같은 행위(저장소 내용을 모델 API 로 보내는 것)를 채널마다 다르게 가둔 것이라
근거가 없다(PM 이 파일을 읽는 순간 같은 호출이 게이트 없이 일어난다). 그 컨테이너를 지우고 나서
남은 계약은 셋이다:

1. **cwd** — 리뷰어는 검토 대상 저장소(`diff_root`)에서 돈다. 거울 사본 경로가 아니다.
2. **env** — 위임 채널이 소유한 같은 seam(`pm_delegate.build_env`)이 조립한다. `HOME` 을 임시 홈
   으로 덮지 않는다(그 덮어쓰기가 세션 재사용을 막아 라운드마다 전체 payload 를 다시 과금했다).
3. **심볼 부재** — 거울·임시 홈·리뷰어 전용 env 함수가 모듈에 남아 있지 않다. 남으면 다시 배선된다.

그리고 세 하네스가 같은 값을 받는다 — 하나만 달라도 그게 곧 하네스별 권한 차등이다.

hermetic: 자식 프로세스 스폰 0. `run_review` 를 캡처 스텁으로 갈아 끼워 **실제로 넘어가는**
cwd/env 를 보고, 스텁은 sentinel 로 즉시 빠져나와 장부·raw 경로를 태우지 않는다.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

HARNESSES = ("claude", "codex", "opencode")

# 삭제된 심볼 — "이 하네스에게는 안 보여준다 / 보내기 전에 뺀다 / 보내도 되는지 확인한다" 를 하던
# 자리 전부다. 하나라도 되살아나면 차등이 다시 자란다.
RETIRED_REVIEWER_SYMBOLS = (
    # 가시 범위 격리 컨테이너
    "create_reviewer_workspace",
    "ReviewerWorkspace",
    "ReviewerWorkspaceError",
    "_remove_reviewer_workspace",
    "reviewer_visibility_scope",
    "_project_manager_ancestor",
    "_mirror_tracked_files",
    "_is_denied_mirror_path",
    "_tracked_relative_paths",
    "_build_reviewer_home",
    "reviewer_home_artifacts",
    "reviewer_env",
    "reviewer_env_keep_extra",
    "_run_isolated_review",
    # 채널 동의 축
    "ADDITIONAL_REVIEWER_ENABLED_KEY",
    "_is_enabled",
    "disabled_gate_notice",
    "enabled_decision_key",
    # 내용 필터(시크릿 denylist·기계 mirror payload 제외)
    "_SECRET_DENYLIST_PATTERNS",
    "_matching_denylist_pattern",
    "_is_secret_path",
    "filter_secret_hunks",
    "_filter_diff_hunks",
    "_denylist_patterns",
    "_denylist_extras",
    "_is_review_machine_mirror_path",
    "ReviewContentResolution",
    "resolve_review_content_conf",
    "_conf_with_owner_filters",
    "OwnerFilterConfError",
    # 네트워크 attestation
    "CODEX_EGRESS_FLAG",
)

# 위임 채널에서 같은 이유로 삭제된 심볼.
RETIRED_DELEGATE_SYMBOLS = (
    "PromptSecretHit",
    "scan_prompt_secrets",
    "scan_prompt_secret_hits",
    "secret_scan_prompt_digest",
    "_format_secret_scan_hits",
    "_prompt_file_denylist_pattern",
    "_prompt_file_contained",
    "SECRET_SCAN_ACK_HEX_LENGTH",
    "SECRET_SCAN_HIT_DISPLAY_LIMIT",
    "ACK_FALLBACK_SUPPRESSION_REASON",
    "CODEX_EGRESS_MARKER_ENV",
    "codex_egress_escalation_required",
    "codex_egress_block_message",
    "codex_egress_provenance",
)

# pm_relay 가 두 표면에 공급하던 네트워크 attestation seam.
RETIRED_RELAY_SYMBOLS = (
    "CODEX_EGRESS_MARKER_ENV",
    "CODEX_EGRESS_FLAG",
    "codex_egress_escalation_required",
    "codex_egress_label",
    "codex_egress_provenance",
    "codex_egress_block_message",
    "dry_run_codex_egress_line",
)


def _load(name: str):
    """도구 모듈을 (패키지 아님) importlib 로 경로 로드 — sibling 테스트 동일 규약."""
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def external():
    return _load("additional_reviewer")


class _Spawned(RuntimeError):
    """캡처 스텁이 스폰 경계에서 즉시 빠져나오는 sentinel — 장부·raw 를 태우지 않는다."""


def _capture_run_conditions(external, monkeypatch, *, harness: str,
                            diff_root: Path, output_dir: Path) -> dict:
    """`_run_review_round` 가 리뷰어에게 넘기는 실행 조건(cwd·env)을 잡아 돌려준다."""
    seen: dict = {}

    def _stub(**kwargs):
        seen.update(kwargs)
        raise _Spawned("capture")

    monkeypatch.setattr(external, "run_review", _stub)
    target = external.ReviewerTarget(
        external.REVIEWER_SOURCE_STRUCTURED, f"{harness} exec",
        harness=harness, model="m",
    )
    with pytest.raises(_Spawned):
        external._run_review_round(
            argparse.Namespace(), conf={}, prompt="p",
            reviewer_cmd=target.command, timeout=1, idle_timeout=1,
            output_dir=output_dir, conf_path=None, profile=None,
            target=target, reservation=None,
            pm_home=diff_root, diff_root=diff_root,
        )
    return seen


def test_reviewer_runs_in_the_reviewed_repository(external, monkeypatch, tmp_path):
    """cwd 는 검토 대상 저장소다 — 거울 사본 경로로 되돌리면 여기서 잡힌다."""
    diff_root = tmp_path / "repo"
    diff_root.mkdir()

    seen = _capture_run_conditions(
        external, monkeypatch, harness="codex",
        diff_root=diff_root, output_dir=tmp_path)

    assert seen["cwd"] == diff_root


def test_reviewer_env_comes_from_the_delegate_seam(external, monkeypatch, tmp_path):
    """env 는 위임 채널의 seam 이 조립한다 — 리뷰어 전용 allowlist 를 만들지 않는다."""
    diff_root = tmp_path / "repo"
    diff_root.mkdir()

    seen = _capture_run_conditions(
        external, monkeypatch, harness="codex",
        diff_root=diff_root, output_dir=tmp_path)

    delegate = external._load_pm_delegate()
    assert seen["env"] == delegate.build_env("codex")


def test_reviewer_home_is_the_session_home(external, monkeypatch, tmp_path):
    """`HOME` 이 임시 홈으로 덮이지 않는다 — 덮으면 세션 재사용이 막혀 매 라운드 재과금된다."""
    diff_root = tmp_path / "repo"
    diff_root.mkdir()

    seen = _capture_run_conditions(
        external, monkeypatch, harness="codex",
        diff_root=diff_root, output_dir=tmp_path)

    home = os.environ.get("HOME")
    if home is None:                       # Windows 등 HOME 미설정 환경
        assert "HOME" not in seen["env"]
    else:
        assert seen["env"]["HOME"] == home
        assert not str(seen["env"]["HOME"]).startswith(str(tmp_path))


def test_three_harnesses_get_the_same_cwd_and_home(external, monkeypatch, tmp_path):
    """하네스가 달라도 cwd·HOME 은 같다 — 하나만 달라지면 그게 권한 차등이다."""
    diff_root = tmp_path / "repo"
    diff_root.mkdir()

    conditions = {
        harness: _capture_run_conditions(
            external, monkeypatch, harness=harness,
            diff_root=diff_root, output_dir=tmp_path)
        for harness in HARNESSES
    }

    assert {str(seen["cwd"]) for seen in conditions.values()} == {str(diff_root)}
    assert len({seen["env"].get("HOME") for seen in conditions.values()}) == 1


@pytest.mark.parametrize("symbol", RETIRED_REVIEWER_SYMBOLS)
def test_retired_reviewer_symbols_are_gone(external, symbol):
    """거울·임시 홈·동의 축·내용 필터·attestation 심볼이 모듈에 없다."""
    assert not hasattr(external, symbol), symbol


@pytest.mark.parametrize("symbol", RETIRED_DELEGATE_SYMBOLS)
def test_retired_delegate_symbols_are_gone(symbol):
    """위임 채널에도 프롬프트 시크릿 스캔·경계 검사·attestation 이 남아 있지 않다."""
    assert not hasattr(_load("pm_delegate"), symbol), symbol


@pytest.mark.parametrize("symbol", RETIRED_RELAY_SYMBOLS)
def test_retired_relay_symbols_are_gone(symbol):
    """공용 드라이버에도 네트워크 attestation seam 이 남아 있지 않다."""
    assert not hasattr(_load("pm_relay"), symbol), symbol


def test_no_reviewer_only_conf_keys():
    """리뷰어 전용 env·홈·필터·스위치 노브가 설정 표면에서 사라졌다 (대체 키 없음)."""
    local_conf = _load("local_conf")

    for key in ("additional_reviewer.enabled",
                "additional_reviewer.denylist_extra",
                "additional_reviewer.env_keep_extra",
                "additional_reviewer.home_artifacts_extra"):
        assert key not in local_conf.KNOWN_KEYS, key
    for retired in ("additional_reviewer.enabled", "additional_reviewer_enabled",
                    "external_review_enabled", "review_denylist_extra",
                    "reviewer_env_keep_extra", "reviewer_home_artifacts_extra"):
        assert local_conf.LEGACY_KEY_MAP[retired] is None, retired


@pytest.mark.parametrize("leftover", ["true", "false"])
def test_a_leftover_switch_value_changes_nothing(external, monkeypatch, tmp_path,
                                                 leftover):
    """옛 스위치 값이 conf 에 남아 있어도 실행 조건이 갈리지 않는다 (마이그레이션 계약).

    켜고 끄는 축 자체가 없어졌다 — 남은 행은 지우면 되고, 지우기 전에도 `true`/`false` 어느
    값이든 같은 실행 조건이 나온다.
    """
    diff_root = tmp_path / "repo"
    diff_root.mkdir()

    seen = _capture_run_conditions(
        external, monkeypatch, harness="codex",
        diff_root=diff_root, output_dir=tmp_path)

    delegate = external._load_pm_delegate()
    assert seen["cwd"] == diff_root
    assert seen["env"] == delegate.build_env("codex")
    # 엔진 코드가 그 키를 아예 읽지 않는다 — 값이 무엇이든 읽는 자리가 없다.
    source = (TOOLS / "additional_reviewer.py").read_text(encoding="utf-8")
    assert "additional_reviewer.enabled" not in source
