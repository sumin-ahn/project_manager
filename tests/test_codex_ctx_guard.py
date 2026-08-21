"""codex 어댑터 대화형 ctx 가드 정합 테스트 (T-0406·ADR-0070 D4).

codex 어댑터의 2층 ctx 가드 중 **대화형 경로**를 여러 층위에서 단언한다 (relay 경로 기계 가드는
`pm_orch_codex.py` usage 판정·T-0404 소관 — 여기 밖):

  1. config.toml 정합 — `model_auto_compact_token_limit` 숫자 threshold(D4 ②·off 아님)·
       `[features]` multi_agent/hooks·`[sandbox_workspace_write]` network_access=false ·
       **machine-local 무시 키 부재**(trusted-repo 로드 규칙·spike §1.2).
  2. hooks.json 정합 — 실 codex 스키마(최상위 `hooks` 래퍼 → 이벤트 → matcher-group → 중첩
       `hooks[]` → `{type:command, command:<셸 문자열>}`·Claude Code 형 동일)로 `PreCompact` 비차단
       checkpoint 안내(인라인 command string·별도 스크립트 파일 금지) + strict JSON.
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
import sys
import tomllib
from pathlib import Path

import pytest

from _textio import utf8_child_env
from _win_skip import posix_bash_supported
from _hook_commands import inline_script_payloads, powershell_native_arguments

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
    """[features] multi_agent/hooks on — 위임 spawn + PreCompact checkpoint 안내 전제."""
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


# ── 2. hooks.json: PreCompact 비차단 checkpoint 안내 (manual/auto 구분) ────

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


def test_hooks_precompact_allows_compaction_with_manual_auto_matchers_and_json_stdout():
    """PreCompact은 matcher를 보존하되 compaction을 중단하지 않고 checkpoint를 안내한다."""
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
    assert ".project_manager/tools/pm_log.py checkpoint --trigger compaction --phase pre --cwd" in joined
    assert ".project_manager/tools/pm_log.py ctx-guidance --band precompact --json" in joined
    assert ">/dev/null 2>&1" in joined, "checkpoint subprocess 출력 폐기 없음"
    assert "printf" in joined, "JSON stdout을 내는 printf command 없음"
    assert '\"continue\"' not in joined, "비차단 hook에 continue 제어 키가 남음"
    assert '\"stopReason\"' not in joined, "비차단 hook에 stopReason이 남음"
    assert '\"suppressOutput\":true' in joined, "엔진 부재 fail-soft JSON 없음"


def test_hooks_windows_commands_match_posix_nonblocking_checkpoint_contract():
    """Windows/POSIX가 같은 pm_log ctx-guidance 값을 소비하고 비차단 fallback을 둔다."""
    groups = _load_hooks()["hooks"]["PreCompact"]
    assert {group["matcher"] for group in groups} == {"^auto$", "^manual$"}
    for group in groups:
        handler = group["hooks"][0]
        assert "ctx-guidance --band precompact --json" in handler["command"]
        assert "ctx-guidance --band precompact --json" in handler["commandWindows"]
        assert '{\"suppressOutput\":true}' in handler["command"]
        assert '{\"suppressOutput\":true}' in handler["commandWindows"]
        assert "&&" not in handler["commandWindows"]


def test_readme_documents_nonblocking_probe_and_confirmed_headless_non_reachability():
    """README는 0.146.0 비차단 실측과 headless systemMessage 미도달 결론을 고정한다."""
    readme = (REPO / "templates" / "codex" / "README.md").read_text(encoding="utf-8")
    normalized = " ".join(readme.split())
    anchors = (
        "compaction을 차단하지 않는다",
        "`pm_log.py snapshot --json`의 엔진 소유 최종 텍스트",
        "compaction 횟수를 세는 영속 상태는 두지 않는다",
        "Codex CLI 0.147.0 로컬 바이너리의 hook event enum에서 `PostCompact` 지원을 확인",
        "메인테이너 실측(2026-08-06, codex-cli 0.146.0)",
        "`--oss` 프로브(`reach-probe/`)",
        "marker 발화를 확인",
        "`turn_aborted` 0건·`context_compacted` 기록",
        "후속 turn 정상 계속",
        "stdout JSONL·stderr·rollout·`CODEX_HOME` 전수 grep",
        "모델 자기보고도 음성이었다",
        "exec 경로 안내는 모델에 닿지 않는다(관측만 가능)",
        "direct TUI 표시는 미검증",
        "driver 회전 선점이 relay 경로를 실보호",
        "trusted project와 `/hooks` 승인",
    )
    for anchor in anchors:
        assert anchor in normalized, f"README Context safety 앵커 누락: {anchor!r}"
    assert "PM 게이트 실측 후 확정" not in normalized
    assert "codex resume --disable hooks" not in normalized
    assert "features.hooks=false" not in readme


def test_hooks_warning_is_inline_and_only_calls_engine_script():
    """별도 adapter script/정책 복제 없이 pm_log의 checkpoint+ctx-guidance만 호출한다."""
    for e in _precompact_command_entries():
        assert e.get("type") == "command", f"command 타입 hook 아님: {e!r}"
        body = e.get("command", "")
        assert isinstance(body, str), f"command 가 문자열 아님 (실 스키마 위반): {body!r}"
        assert "pm_log.py checkpoint" in body and "pm_log.py ctx-guidance" in body
        assert "printf '%s\\n'" in body
        windows = e.get("commandWindows", "")
        assert isinstance(windows, str) and windows, f"Windows commandWindows 누락: {e!r}"
        assert "pm_log.py' checkpoint" in windows and "pm_log.py' ctx-guidance" in windows
        assert "Write-Output '" in windows
        assert "&&" not in windows and ";" in windows


def _all_windows_commands() -> list[tuple[str, str]]:
    """모든 이벤트의 commandWindows — Windows에서 실제로 실행되는 커맨드 전량."""
    commands = []
    for event, groups in _load_hooks()["hooks"].items():
        for group in groups:
            for handler in group["hooks"]:
                commands.append((event, handler["commandWindows"]))
    assert commands
    return commands


def test_windows_interpreter_resolution_prefers_launcher_and_probes_execution():
    """T-0715 — Windows `python3`/`python`은 실행 시 rc 9009로 죽는 WindowsApps shim일 수 있다.

    존재만 확인하고 쓰면 엔진이 한 번도 실행되지 않은 채 fail-soft 폴백(`suppressOutput`)만
    나가 ctx 안내와 위임 차단 사유가 사라진다. 런처 `py`를 먼저 보고 후보를 실제 실행해
    확인한 뒤에만 채택한다 (CLAUDE.md Windows 노트).
    """
    for event, windows in _all_windows_commands():
        assert "@('py','python3','python')" in windows, event
        assert "$probe = & $cand -c" in windows, event
        assert "sys.version_info >= (3, 11)" in windows, event
        assert "if ($probe -eq 'True') { $py = $cand" in windows, event
        # PowerShell 5.x는 native 인자의 큰따옴표를 삼킨다 — 프로브는 따옴표 없이 쓴다.
        loop = windows[windows.index("foreach ($cand"):windows.index("break } } }")]
        assert '"' not in loop, event
        # 존재 확인만으로 채택하던 옛 형태가 남아 있으면 shim을 그대로 고른다.
        assert "if (Get-Command python3" not in windows, event
        assert "&&" not in windows, event


def _claude_hook_commands() -> list[tuple[str, str]]:
    settings = json.loads(
        (REPO / "templates" / "claude_code" / ".claude" / "settings.json")
        .read_text(encoding="utf-8")
    )
    commands = []
    for event, groups in (settings.get("hooks") or {}).items():
        for group in groups:
            for handler in group.get("hooks", []):
                for key in ("command", "commandWindows"):
                    if isinstance(handler.get(key), str):
                        commands.append((f"claude/{event}/{key}", handler[key]))
    assert commands
    return commands


def test_hook_commands_pass_no_double_quoted_argument_to_a_native_command():
    """T-0720 — PowerShell 5.x 는 native 인자의 큰따옴표를 삼킨다 (Windows 11 실측).

    ``py -3.12 -c 'print("quoted")'`` 가 ``NameError: name 'quoted' is not defined`` 로
    떨어졌다. 큰따옴표를 담은 인라인 스크립트를 native 인자로 넘기면 그 호스트에서 훅은
    **항상** 실패해 폴백으로 빠지고 가드가 무음 통과한다. 셸 안에서만 쓰이는 문자열
    (``Write-Output $fallback``)은 이 축이 아니므로 native 인자 표면만 본다.
    """
    for event, windows in _all_windows_commands():
        for segment in powershell_native_arguments(windows):
            assert '"' not in segment, f"{event}: {segment}"
    for label, command in (
        [(f"codex/{event}", command) for event, command in _all_windows_commands()]
        + [(f"codex/{event}/posix", handler["command"])
           for event, groups in _load_hooks()["hooks"].items()
           for group in groups for handler in group["hooks"]]
        + _claude_hook_commands()
    ):
        for payload in inline_script_payloads(command):
            assert '"' not in payload, f"{label}: {payload}"


def test_powershell_native_argument_scanner_sees_the_regressed_shape():
    """가드 자신의 시야 검증 — 옛 형태(인라인 스크립트 인자)를 실제로 잡아내는가."""
    regressed = (
        "$py = 'py'; $fallback = '{\"systemMessage\":\"x\",\"suppressOutput\":false}'; "
        "$out = & $py -c 'import json; json.loads(\"{}\")' pre; Write-Output $fallback"
    )
    segments = powershell_native_arguments(regressed)

    assert len(segments) == 1
    assert '"' in segments[0]
    assert inline_script_payloads(regressed) == ['import json; json.loads("{}")']
    # 셸 전용 리터럴($fallback)은 native 인자 표면에 들지 않는다.
    assert "systemMessage" not in segments[0]


def test_windows_probe_snippet_runs_and_reports_this_interpreter():
    """출하된 프로브 스니펫을 실 인터프리터로 태운다 (셸 밖 Python 부분의 red 재현 지점)."""
    for event, windows in _all_windows_commands():
        snippet = windows.split("$probe = & $cand -c '", 1)[1].split("'", 1)[0]
        completed = subprocess.run(
            [sys.executable, "-c", snippet],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            check=True, env=utf8_child_env(),
        )
        # 엔진 하한(3.11) 이상인 이 인터프리터는 채택 판정을 받아야 한다.
        assert completed.stdout == "True", f"{event}: {completed.stdout!r}"
        assert completed.stderr == "", event


def test_windows_interpreter_is_never_adopted_without_a_probe():
    """실행 확인 없이 후보를 그대로 채택하는 배선이 남아 있으면 shim이 다시 선택된다."""
    for event, windows in _all_windows_commands():
        for literal in ("$py = 'py'", "$py = 'python3'", "$py = 'python'"):
            assert literal not in windows, f"{event}: {literal}"
        # 엔진 호출(`& $py`)은 반드시 해소 루프 뒤에 온다.
        assert windows.index("$py = $cand") < windows.index("& $py"), event
        assert windows.index("if ($null -eq $py)") < windows.index("& $py"), event


def test_postcompact_wires_snapshot_checkpoint_and_windows_symmetry():
    groups = _load_hooks()["hooks"]["PostCompact"]
    assert {group.get("matcher") for group in groups} == {"^auto$", "^manual$"}
    for group in groups:
        handler = group["hooks"][0]
        for key in ("command", "commandWindows"):
            command = handler[key]
            assert "pm_log.py" in command and "checkpoint" in command and "snapshot" in command
            assert "--json" in command and "--cwd" in command
            assert "--phase post" in command
        assert ">/dev/null 2>&1" in handler["command"]
        assert "*> $null" in handler["commandWindows"]
        assert "&&" not in handler["commandWindows"]


@pytest.mark.skipif(
    not posix_bash_supported(), reason="POSIX shell wrapper 실행 환경이 아님"
)
def test_posix_precompact_discards_checkpoint_noise_and_emits_one_json(tmp_path):
    tools = tmp_path / ".project_manager" / "tools"
    tools.mkdir(parents=True)
    (tools / "pm_log.py").write_text(
        "import json, sys\n"
        "if 'ctx-guidance' in sys.argv:\n"
        "    print(json.dumps({'systemMessage':'ENGINE-GUIDANCE','suppressOutput':False}))\n"
        "else:\n"
        "    print('checkpoint-noise')\n"
        "    print('checkpoint-error', file=sys.stderr)\n",
        encoding="utf-8",
    )
    for group in _load_hooks()["hooks"]["PreCompact"]:
        result = subprocess.run(
            ["sh", "-c", group["hooks"][0]["command"]], cwd=tmp_path,
            capture_output=True, text=True, check=True,
        )
        lines = result.stdout.splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0]) == {
            "systemMessage": "ENGINE-GUIDANCE", "suppressOutput": False,
        }
        assert result.stderr == ""


@pytest.mark.skipif(
    not posix_bash_supported(), reason="POSIX shell wrapper 실행 환경이 아님"
)
def test_posix_postcompact_injects_builder_json_without_checkpoint_noise(tmp_path):
    tools = tmp_path / ".project_manager" / "tools"
    tools.mkdir(parents=True)
    (tools / "pm_log.py").write_text(
        "import json, sys\n"
        "if 'snapshot' in sys.argv:\n"
        "    print(json.dumps({'systemMessage':'SNAPSHOT-VERBATIM','suppressOutput':False}))\n"
        "else:\n"
        "    print('checkpoint-noise')\n"
        "    print('checkpoint-error', file=sys.stderr)\n",
        encoding="utf-8",
    )
    command = _load_hooks()["hooks"]["PostCompact"][0]["hooks"][0]["command"]
    result = subprocess.run(
        ["sh", "-c", command], cwd=tmp_path, capture_output=True, text=True, check=True,
    )
    assert json.loads(result.stdout) == {
        "systemMessage": "SNAPSHOT-VERBATIM", "suppressOutput": False,
    }
    assert result.stderr == ""


def test_hooks_windows_commands_parse_and_execute_for_pre_and_postcompact_when_available(
        tmp_path):
    """PowerShell이 있으면 PreCompact와 PostCompact commandWindows를 모두 파싱·실행한다."""
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if executable is None:
        return  # Linux CI 등 PowerShell 부재 환경은 위 static parser가 contract를 검증한다.

    tools = tmp_path / ".project_manager" / "tools"
    tools.mkdir(parents=True)
    (tools / "pm_log.py").write_text(
        "import json, sys\n"
        "if 'snapshot' in sys.argv:\n"
        "    print(json.dumps({'systemMessage':'SNAPSHOT-WINDOWS','suppressOutput':False}))\n"
        "elif 'ctx-guidance' in sys.argv:\n"
        "    print(json.dumps({'systemMessage':'ENGINE-GUIDANCE','suppressOutput':False}))\n"
        "else:\n"
        "    print('checkpoint-noise')\n"
        "    print('checkpoint-error', file=sys.stderr)\n",
        encoding="utf-8",
    )
    for event in ("PreCompact", "PostCompact"):
        for group in _load_hooks()["hooks"][event]:
            raw_command = group["hooks"][0]["commandWindows"]
            result = subprocess.run(
                [executable, "-NoProfile", "-NonInteractive", "-Command", raw_command],
                cwd=tmp_path, check=True, capture_output=True, text=True,
                encoding="utf-8", errors="replace", env=utf8_child_env(),
            )
            expected = {
                "systemMessage": (
                    "ENGINE-GUIDANCE" if event == "PreCompact" else "SNAPSHOT-WINDOWS"
                ),
                "suppressOutput": False,
            }
            assert json.loads(result.stdout.strip()) == expected
            assert result.stderr == ""


def _rollout_summary(jsonl: str) -> tuple[int, set[str], bool]:
    """저장 Codex JSONL에서 compaction과 예전 echo-only 훅 증거를 요약한다."""
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
    # [ctx-tripwire]는 현행 contract가 아니라 삭제된 echo-only 가드의 역사 fixture marker다.
    legacy_echo_seen = "[ctx-tripwire]" in jsonl
    return compacted, event_types & {"hook_started", "hook_completed"}, legacy_echo_seen


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
    # init 은 areas repo 행을 **항상** 등록하므로(T-0779) REPO 도 tmp 로 묶어야 hermetic 하다 —
    # 안 묶으면 `areas_file()`·`board_lock()` 이 실 저장소 루트를 잡는다.
    _pm = tmp_path / "proj" / ".project_manager"
    (_pm / ".local").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(board, "REPO", tmp_path / "proj")
    monkeypatch.setattr(board, "AREAS_FILE", _pm / "areas.md")
    monkeypatch.setattr(board, "LOCAL_DIR", _pm / ".local")
    monkeypatch.setattr(board, "BOARD_LOCK", _pm / ".local" / "board.lock")
    monkeypatch.setattr(board, "LEASES_FILE", _pm / ".local" / "worktree-leases.json")

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
