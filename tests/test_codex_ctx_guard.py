"""codex 어댑터 대화형 ctx 가드 정합 테스트 (T-0406·ADR-0070 D4).

codex 어댑터의 2층 ctx 가드 중 **대화형 경로**를 여러 층위에서 단언한다 (relay 경로 기계 가드는
`pm_orch_codex.py` usage 판정·T-0404 소관 — 여기 밖):

  1. config.toml 정합 — `model_auto_compact_token_limit` 숫자 threshold(D4 ②·off 아님)·
       `[features]` multi_agent/hooks·`[sandbox_workspace_write]` network_access=false ·
       **machine-local 무시 키 부재**(trusted-repo 로드 규칙·spike §1.2).
  2. hooks.json 정합 — 실 codex 스키마(최상위 `hooks` 래퍼 → 이벤트 → matcher-group → 중첩
       `hooks[]` → `{type:command, command:<셸 문자열>}`·Claude Code 형 동일)로 `PreCompact` loud
       tripwire(인라인 command string·별도 스크립트 파일 금지) + strict JSON.
  3. ctx 예산 키 — `board.py init` 스캐폴드가 `ctx_window_tokens_codex` 주석 예시를 claude/opencode
       와 나란히 박는다(relay driver `_maybe_mark_ctx` 예산 원천·ADR-0041 per-harness 키).

미러: `test_opencode_ctx_guard.py`(config/plugin 정합)·`test_board_portability.py` C8(board init).

instance-owned(두 파일 모두 manifest 미등록·미전파·trust 재승인 churn 회피)의 **권위 단언**은
`test_manifest_template_parity.test_instance_owned_config_not_registered`(T-0402·codex 절 forbidden
등재) — 이 파일은 그 관심사를 중복하지 않고 대화형 가드 산출물 정합에만 집중한다.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CODEX = REPO / "templates" / "codex" / ".codex"
CONFIG_TOML = CODEX / "config.toml"
HOOKS_JSON = CODEX / "hooks.json"
TOOLS = REPO / ".project_manager" / "tools"


def _load_config() -> dict:
    with CONFIG_TOML.open("rb") as fh:
        return tomllib.load(fh)


def _load_hooks() -> dict:
    return json.loads(HOOKS_JSON.read_text(encoding="utf-8"))


# ── 1. config.toml: 문서화된 auto-compact threshold + machine-local 금지 키 부재 ──

def test_config_exists_and_parses():
    """config.toml 이 존재하고 valid TOML 로 파싱된다."""
    assert CONFIG_TOML.exists(), f"codex config.toml 없음: {CONFIG_TOML}"
    assert isinstance(_load_config(), dict)


def test_config_uses_documented_numeric_auto_compact_threshold_not_off_sentinel():
    """config은 문서화된 숫자 threshold만 쓴다; 미검증 off sentinel을 발명하지 않는다."""
    data = _load_config()
    assert "model_auto_compact_token_limit" in data, "auto-compact 상향 키 없음"
    assert data["model_auto_compact_token_limit"] == 900000, (
        f"상향값이 spike §3.6(900000) 아님: {data['model_auto_compact_token_limit']!r}"
    )


def test_config_enables_multi_agent_and_hooks():
    """[features] multi_agent/hooks on — 위임 spawn + PreCompact tripwire 발화 전제."""
    features = _load_config().get("features", {})
    assert features.get("multi_agent") is True, "[features] multi_agent 활성 아님"
    assert features.get("hooks") is True, "[features] hooks 활성 아님(PreCompact 전제)"


def test_config_disables_network_egress():
    """[sandbox_workspace_write] network_access=false — egress 안전 경계(spike §3.6)."""
    sww = _load_config().get("sandbox_workspace_write", {})
    assert sww.get("network_access") is False, "network_access=false 아님(egress 가드)"


def test_config_omits_machine_local_keys():
    """machine-local 키(로드 시 무시·기재 금지)가 실 키로 존재하지 않는다 (spike §1.2 trusted-repo 규칙).

    codex 는 trusted project 라도 model_provider*·profile/profiles·notify·otel 을 무시한다 —
    출하 템플릿에 실으면 채택자 오해. *파싱된 키* 로는 부재여야 한다(설명 주석 언급은 무방).
    """
    data = _load_config()
    forbidden = {"model_provider", "model_providers", "profile", "profiles", "notify"}
    leaked = forbidden & set(data)
    assert not leaked, f"machine-local 금지 키가 실 키로 존재: {sorted(leaked)}"
    assert not any(k == "otel" or k.startswith("otel") for k in data), (
        "otel machine-local 키 존재(기재 금지)"
    )
    # 출하 템플릿은 리뷰된 반입가능 키만 — 화이트리스트(machine-local leak 전수 차단).
    allowed = {
        "model_auto_compact_token_limit",
        "sandbox_mode",
        "approval_policy",
        "approvals_reviewer",
        "features",
        "sandbox_workspace_write",
    }
    assert set(data) <= allowed, f"config.toml 에 예상 밖 top-level 키: {sorted(set(data) - allowed)}"


# ── 2. hooks.json: PreCompact JSON warning (manual/auto 구분) ────────────────

def test_hooks_exists_and_parses_strict_json():
    """hooks.json 이 존재하고 strict JSON 으로 파싱된다(주석 없음 — codex 소비 규약·DoD)."""
    assert HOOKS_JSON.exists(), f"codex hooks.json 없음: {HOOKS_JSON}"
    assert isinstance(_load_hooks(), dict)


def _precompact_command_entries() -> list[dict]:
    """실 codex 스키마를 따라 PreCompact 의 중첩 command hook 엔트리들을 전개한다.

    스키마(reviewer 3중 확정 — 로컬 번들 플러그인 hooks.json 실예시 2건 + 바이너리 ts-rs 타입 +
    Claude Code 형 동일): {"hooks": {"<event>": [ {"hooks": [ {"type":"command","command":<str>} ]} ]}}.
    최상위 "hooks" 래퍼 → 이벤트 → matcher-group 배열 → 각 group 의 중첩 "hooks" 배열 → 엔트리.
    """
    data = _load_hooks()
    assert "hooks" in data, "최상위 'hooks' 래퍼 없음 (실 codex 스키마 위반)"
    events = data["hooks"]
    assert "PreCompact" in events, "PreCompact 이벤트 없음 — compaction 임박 경고 불가"
    groups = events["PreCompact"]
    assert isinstance(groups, list) and groups, "PreCompact matcher-group 목록이 비었음"
    entries = [e for g in groups for e in g.get("hooks", [])]
    assert entries, "PreCompact matcher-group 안 중첩 hooks[] 배열이 비었음"
    return entries


def _precompact_payloads_by_matcher() -> dict[str, tuple[dict, dict]]:
    """각 matcher의 POSIX/Windows inline command가 내는 JSON payload를 파싱한다."""
    groups = _load_hooks()["hooks"]["PreCompact"]
    payloads = {}
    for group in groups:
        matcher = group["matcher"]
        handler = group["hooks"][0]
        posix = handler["command"]
        windows = handler["commandWindows"]
        posix_match = re.fullmatch(r"printf '%s\\n' '(\{.*\})'", posix)
        assert posix_match, f"POSIX handler가 inline JSON printf 형태 아님: {posix!r}"
        windows_match = re.fullmatch(r"Write-Output '(\{.*\})'", windows)
        assert windows_match, f"Windows handler가 PowerShell-safe single-quoted JSON 형태 아님: {windows!r}"
        windows_json = windows_match.group(1)
        assert "'" not in windows_json, (
            f"single-quoted PowerShell literal 안 payload에 apostrophe가 있어 quoting이 깨짐: {windows_json!r}"
        )
        payloads[matcher] = (json.loads(posix_match.group(1)), json.loads(windows_json))
    return payloads


def test_hooks_precompact_warning_uses_manual_auto_matchers_and_json_stdout():
    """PreCompact은 manual/auto를 구분하고 구조화 경고만 낸다.

    기존 stderr echo는 구조화 `systemMessage` 계약이 아니었다. command는 common JSON output
    fields를 사용하며, history 보존 실측 전에는 `continue:false`를 출하하지 않는다.
    """
    groups = _load_hooks()["hooks"]["PreCompact"]
    assert {group.get("matcher") for group in groups} == {"^auto$", "^manual$"}
    entries = _precompact_command_entries()
    cmd_entries = [e for e in entries if e.get("type") == "command"]
    assert cmd_entries, "type=='command' hook 엔트리 없음 (실 스키마의 'type' 태그 누락)"
    # command 는 셸 문자열이어야 한다 (argv 배열 아님 — 원 결함).
    for e in cmd_entries:
        assert isinstance(e.get("command"), str), (
            f"command 가 문자열이 아님 (실 스키마=셸 command string·argv 배열 아님): {e.get('command')!r}"
        )
    joined = " ".join(e["command"] for e in cmd_entries)
    assert "[ctx-tripwire]" in joined, "tripwire 표지([ctx-tripwire]) 없음"
    assert "/pm-handoff" in joined, "핸드오프 안내(/pm-handoff) 없음 — 상태 박제 유도 불가"
    assert "printf" in joined, "JSON stdout을 내는 printf command 없음"
    assert '\"continue\":true' in joined, "명시적인 continue:true 경고 contract 없음"
    assert '\"systemMessage\"' in joined, "UI/event stream 경고 systemMessage 없음"
    assert '\"suppressOutput\":false' in joined, "common output suppressOutput contract 없음"
    assert 'continue\":false' not in joined, "미검증 compaction 취소를 canonical hook에 출하하면 안 됨"


def test_hooks_windows_commands_match_posix_structured_warning_contract():
    """PowerShell-safe native Windows fallback도 각 handler의 JSON 경고 의미를 보존한다."""
    payloads = _precompact_payloads_by_matcher()
    assert set(payloads) == {"^auto$", "^manual$"}
    for matcher, (posix, windows) in payloads.items():
        assert posix == windows, f"{matcher} POSIX/Windows payload 의미 불일치"
        assert posix == {
            "continue": True,
            "systemMessage": posix["systemMessage"],
            "suppressOutput": False,
        }
        assert "[ctx-tripwire]" in posix["systemMessage"]
        assert "/pm-handoff" in posix["systemMessage"]

    assert "auto compaction" in payloads["^auto$"][0]["systemMessage"]
    assert "manual compaction" in payloads["^manual$"][0]["systemMessage"]


def test_hooks_warning_is_inline_no_external_script():
    """경고는 인라인 JSON stdout command다 — 별도 스크립트 전파 surface를 만들지 않는다."""
    for e in _precompact_command_entries():
        assert e.get("type") == "command", f"command 타입 hook 아님: {e!r}"
        body = e.get("command", "")
        assert isinstance(body, str), f"command 가 문자열 아님 (실 스키마 위반): {body!r}"
        # 별도 스크립트(.sh/.py 파일) 실행이 아니라 printf 인라인이어야 한다.
        assert ".sh" not in body and ".py" not in body, (
            f"별도 스크립트 파일 참조 감지 (인라인 규약 위반): {body!r}"
        )
        assert "printf" in body, f"구조화 JSON printf 형태 아님: {body!r}"
        windows = e.get("commandWindows", "")
        assert isinstance(windows, str) and windows, f"Windows commandWindows 누락: {e!r}"
        assert ".sh" not in windows and ".py" not in windows, (
            f"Windows handler가 외부 스크립트를 참조함: {windows!r}"
        )
        assert windows.startswith("Write-Output '"), (
            f"Windows handler가 PowerShell-safe Write-Output 형태 아님: {windows!r}"
        )


def test_hooks_windows_command_emits_parseable_json_when_powershell_available():
    """PowerShell이 있는 host에서는 commandWindows를 실제 실행해 stdout JSON까지 확인한다."""
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if executable is None:
        return  # Linux CI 등 PowerShell 부재 환경은 위 static parser가 contract를 검증한다.

    payloads = _precompact_payloads_by_matcher()
    for group in _load_hooks()["hooks"]["PreCompact"]:
        raw_command = group["hooks"][0]["commandWindows"]
        windows_payload = payloads[group["matcher"]][1]
        result = subprocess.run(
            [executable, "-NoProfile", "-NonInteractive", "-Command", raw_command],
            check=True, capture_output=True, text=True,
        )
        assert json.loads(result.stdout.strip()) == windows_payload


def _rollout_summary(jsonl: str) -> tuple[int, set[str], bool]:
    """저장 Codex JSONL의 event_msg payload만 읽어 compaction/hook 증거를 요약한다."""
    records = []
    for line in jsonl.splitlines():
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            # 이전 echo처럼 JSONL 밖으로 나온 텍스트도 output 부재 증거에서 놓치지 않는다.
            continue
    event_types = {
        record.get("payload", {}).get("type")
        for record in records
        if record.get("type") == "event_msg" and isinstance(record.get("payload"), dict)
    }
    compacted = sum(
        record.get("type") == "event_msg"
        and record.get("payload", {}).get("type") == "context_compacted"
        for record in records
    )
    return compacted, event_types & {"hook_started", "hook_completed"}, "[ctx-tripwire]" in jsonl


def test_recorded_long_tui_rollout_fixture_proves_echo_only_tripwire_was_false_green():
    """T-0442 sanitize fixture: 실제 event_msg JSONL shape에서 4회 compaction·hook 부재를 읽는다."""
    rollout_jsonl = "\n".join([
        '{"timestamp":"2026-07-22T10:50:53Z","type":"event_msg","payload":{"type":"context_compacted"}}',
        '{"timestamp":"2026-07-22T11:00:00Z","type":"event_msg","payload":{"type":"agent_message","message":"continue work"}}',
        '{"timestamp":"2026-07-22T13:49:35Z","type":"event_msg","payload":{"type":"context_compacted"}}',
        '{"timestamp":"2026-07-22T14:44:33Z","type":"event_msg","payload":{"type":"context_compacted"}}',
        '{"timestamp":"2026-07-23T04:49:50Z","type":"event_msg","payload":{"type":"context_compacted"}}',
        '{"timestamp":"2026-07-23T04:50:00Z","type":"turn.completed","usage":{"input_tokens":42}}',
    ])
    compacted, hook_events, tripwire_output = _rollout_summary(rollout_jsonl)
    assert compacted == 4
    assert hook_events == set()
    assert tripwire_output is False

    # 감도: fixture에 hook event/output가 섞이면 같은 parser/filter가 부재 판정을 허용하지 않는다.
    observed = rollout_jsonl + '\n{"type":"event_msg","payload":{"type":"hook_started"}}\n[ctx-tripwire]'
    _, hook_events, tripwire_output = _rollout_summary(observed)
    assert hook_events == {"hook_started"}
    assert tripwire_output is True


# ── 3. ctx_window_tokens_codex 예산 키: board.py init 스캐폴드 ────────────────
# relay driver(_maybe_mark_ctx·T-0404)의 예산 원천 = local.conf ctx_window_tokens_codex
# (ADR-0041 per-harness 키). board.py init 이 claude/opencode 와 나란히 주석 예시를 박는다.

def _load_board():
    spec = importlib.util.spec_from_file_location("board", TOOLS / "board.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_board_init_scaffolds_codex_budget_comment(tmp_path, monkeypatch):
    """board.py init(임시 dir·hermetic)이 local.conf 에 ctx_window_tokens_codex 주석 예시를 박는다.

    미러 test_board_portability.test_init_scaffold_has_harness_override_comment(claude/opencode).
    예시는 반드시 주석(#) — 활성 키로 새면 generic 예산(ctx_window_tokens)을 덮는다.
    """
    board = _load_board()
    conf_path = tmp_path / "local.conf"
    # hermetic: LOCAL_CONF 만 tmp·pm_state/훅/opt-in 부수효과 차단 (C8 _init_isolated 미러).
    monkeypatch.setattr(board, "LOCAL_CONF", conf_path)
    monkeypatch.setattr(board, "PM_STATE_FILE", tmp_path / "pm_state.md")
    monkeypatch.setattr(board, "PM_STATE_TEMPLATE", tmp_path / "missing-template.md")
    monkeypatch.setattr(board, "install_pre_push_hook", lambda: False)
    monkeypatch.setattr(board, "prompt_external_review_optin", lambda: None)

    args = argparse.Namespace(prefix=None, area=None, owner=None, session="pm")
    assert board.cmd_init(args) == 0

    conf_text = conf_path.read_text(encoding="utf-8")
    assert "ctx_window_tokens_codex" in conf_text, "codex 예산 주석 예시 없음"
    # 예시는 반드시 주석(#) — 활성 키로 새어 generic 예산을 덮으면 안 된다(claude/opencode 대칭).
    for line in conf_text.splitlines():
        if "ctx_window_tokens_codex" in line:
            assert line.lstrip().startswith("#"), (
                f"codex 예산 예시는 주석이어야(활성 키 X): {line!r}"
            )
    # claude/opencode 와 한 블록에 나란히.
    assert "ctx_window_tokens_claude" in conf_text
    assert "ctx_window_tokens_opencode" in conf_text
    # 파싱 시 주석 오버라이드는 활성 키로 잡히지 않는다 (local_config 는 # 라인 skip).
    parsed = board.local_config()  # LOCAL_CONF 가 conf_path 로 patch 됨.
    assert "ctx_window_tokens_codex" not in parsed
