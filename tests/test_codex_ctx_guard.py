"""codex 어댑터 대화형 ctx 가드 정합 테스트 (T-0406·ADR-0070 D4).

codex 어댑터의 2층 ctx 가드 중 **대화형 경로**를 여러 층위에서 단언한다 (relay 경로 기계 가드는
`pm_orch_codex.py` usage 판정·T-0404 소관 — 여기 밖):

  1. config.toml 정합 — `model_auto_compact_token_limit` 숫자 threshold(D4 ②·off 아님)·
       `[features]` multi_agent/hooks·`[sandbox_workspace_write]` network_access=false ·
       **machine-local 무시 키 부재**(trusted-repo 로드 규칙·spike §1.2).
  2. hooks.json 정합 — 실 codex 스키마(최상위 `hooks` 래퍼 → 이벤트 → matcher-group → 중첩
       `hooks[]` → `{type:command, command:<셸 문자열>}`·Claude Code 형 동일)로 `PreCompact` 비차단
       checkpoint 안내(인라인 command string·별도 스크립트 파일 금지) + strict JSON.
       T-0806 이후 압축 두 이벤트의 커맨드는 범용 진입점(`--hook-dispatch <이벤트>`)이고, 어떤
       기능이 도는지는 디스패처 registry 가 쥔다 — 그래서 checkpoint·안내·snapshot 배선 단언은
       hooks.json 이 아니라 그 registry 값을 본다(판정 자체는 같은 값·표기만 옮겨 왔다).
  3. ctx 예산 키 — `board.py init` 스캐폴드가 `ctx_window_tokens_codex` 주석 예시를 claude/opencode
       와 나란히 박는다(relay driver `_maybe_mark_ctx` 예산 원천·ADR-0041 per-harness 키).

미러: `test_opencode_ctx_guard.py`(config/plugin 정합)·`test_board_portability.py` C8(board init).

instance-owned(두 파일 모두 manifest 미등록·미전파·trust 재승인 churn 회피)의 **권위 단언**은
`test_manifest_template_parity.test_instance_owned_config_not_registered`(T-0402·codex 절 forbidden
등재) — 이 파일은 그 관심사를 중복하지 않고 대화형 가드 산출물 정합에만 집중한다.
"""
from __future__ import annotations

import argparse
import functools
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


DISPATCHER_REL = ".codex/pm_orch_codex.py"
DISPATCHER_PY = REPO / "templates" / "codex" / DISPATCHER_REL


@functools.lru_cache(maxsize=1)
def _dispatcher():
    """출하 디스패처 모듈 — 압축 훅의 기능 배선이 사는 곳(T-0806 이후)."""
    spec = importlib.util.spec_from_file_location("pm_orch_codex", DISPATCHER_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _features(event: str) -> list:
    """그 이벤트에 등록된 기능 — 등재 순서 그대로(옛 커맨드의 단계 순서)."""
    return [feature for feature in _dispatcher().CODEX_HOOK_FEATURES
            if feature.event == event]


def _entrypoint_handler(event: str) -> dict:
    """그 이벤트의 유일한 진입점 handler."""
    groups = _load_hooks()["hooks"][event]
    assert len(groups) == 1, f"{event}: 진입점은 이벤트당 하나다 ({len(groups)}개)"
    assert groups[0]["matcher"] == ".*", f"{event}: {groups[0]['matcher']!r}"
    handlers = groups[0]["hooks"]
    assert len(handlers) == 1, f"{event}: 진입점 handler 는 하나다"
    return handlers[0]


def _feature_tree(tmp_path: Path, pm_log_body: str) -> Path:
    """출하 디스패처 + 가짜 `pm_log.py` 로 만든 최소 채택자 트리.

    진입점 커맨드를 **그대로** 태우되 장부 CLI 만 가짜로 바꿔, checkpoint 단계의 사람글이
    엔벨로프 줄로 새지 않는지를 실행 결과로 본다(옛 `>/dev/null 2>&1` 단언의 승계)."""
    root = tmp_path / "adopter"
    tools = root / ".project_manager" / "tools"
    tools.mkdir(parents=True)
    (tools / "pm_log.py").write_text(pm_log_body, encoding="utf-8", newline="\n")
    # 엔진 루트 해소 마커(`repo_root`) — 폴백에 기대지 않고 트리 루트를 값으로 고정한다.
    (tools / "pm_handoff.py").write_text("", encoding="utf-8", newline="\n")
    (root / ".codex").mkdir(parents=True)
    shutil.copy2(DISPATCHER_PY, root / DISPATCHER_REL)
    return root


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


def test_hooks_precompact_allows_compaction_through_one_entrypoint_and_json_stdout():
    """PreCompact은 진입점 하나로 compaction을 중단하지 않고 checkpoint를 안내한다.

    옛 `^auto$`+`^manual$` 두 matcher는 `trigger` enum 전수(`{manual, auto}`)라 `.*` 하나와 값이
    같다(추출 스키마 대조는 test_codex_hook_dispatch 의 enum 커버 단언). 실행되는 두 단계
    (checkpoint 폐기 + ctx-guidance 엔벨로프)는 진입점 뒤 registry 에 값 그대로 있다."""
    handler = _entrypoint_handler("PreCompact")
    assert handler.get("type") == "command", handler
    # command 는 셸 문자열이어야 한다 (argv 배열 아님 — 원 결함).
    assert isinstance(handler.get("command"), str), (
        f"command 가 문자열이 아님 (실 스키마=셸 command string·argv 배열 아님): {handler.get('command')!r}"
    )
    checkpoint, guidance = _features("PreCompact")
    assert checkpoint.argv[1:] == ("{tools}/pm_log.py", "checkpoint", "--trigger",
                                   "compaction", "--phase", "pre", "--cwd", ".")
    assert checkpoint.side_effect_only is True, "checkpoint 단계 출력 폐기가 사라졌다"
    assert guidance.argv[1:] == ("{tools}/pm_log.py", "ctx-guidance", "--band",
                                 "precompact", "--json")
    assert guidance.side_effect_only is False, "엔벨로프 단계가 답하지 않는다"
    joined = handler["command"]
    assert "printf" in joined, "JSON stdout을 내는 printf command 없음"
    assert '\"continue\"' not in joined, "비차단 hook에 continue 제어 키가 남음"
    assert '\"stopReason\"' not in joined, "비차단 hook에 stopReason이 남음"
    # 엔진/인터프리터 부재에도 유효한 엔벨로프 한 줄이 나가야 한다(옛 fail-soft JSON 승계).
    assert '\"suppressOutput\":false' in joined, "엔진 부재 fail-soft JSON 없음"
    assert "adapter-fallback" in joined, "폴백 사실을 남기는 마커 없음"


def test_hooks_windows_commands_match_posix_nonblocking_checkpoint_contract():
    """Windows/POSIX가 같은 진입점 호출을 하고 비차단 fallback을 둔다."""
    handler = _entrypoint_handler("PreCompact")
    for key in ("command", "commandWindows"):
        assert "--hook-dispatch PreCompact" in handler[key], key
        assert DISPATCHER_REL in handler[key], key
        assert '\"suppressOutput\":false' in handler[key], key
    assert "&&" not in handler["commandWindows"]


def test_readme_documents_nonblocking_probe_and_confirmed_headless_non_reachability():
    """README는 0.146.0 비차단 실측과 headless systemMessage 미도달 결론을 고정한다.

    PostCompact 서술은 **채널 사실**(엔진 snapshot 을 `systemMessage` 엔벨로프로 출력)까지만
    고정한다 — 그 엔벨로프의 모델 도달은 direct TUI 미검증·headless exec 미도달이라 무범위
    '모델에 재주입' 문장은 재등장하면 red 다(T-0770 F-001).
    """
    readme = (REPO / "templates" / "codex" / "README.md").read_text(encoding="utf-8")
    normalized = " ".join(readme.split())
    anchors = (
        "compaction을 차단하지 않는다",
        # T-0770 F-001: 무범위 "모델에 재주입" 을 관측된 출력 채널까지로 좁힌 문안.
        "`pm_log.py snapshot --json`의 엔진 소유 최종 텍스트를 `systemMessage` 엔벨로프로 출력한다",
        "그 엔벨로프의 모델 도달은 direct TUI에서 미검증이고 headless exec에서는 미도달이다",
        "compaction 횟수를 세는 영속 상태는 두지 않는다",
        "Codex CLI 0.147.0 로컬 바이너리의 hook event enum에서 `PostCompact` 지원을 확인",
        "메인테이너 실측(2026-08-06, codex-cli 0.146.0)",
        "`--oss` 프로브(`reach-probe/`)",
        "marker 발화를 확인",
        "`turn_aborted` 0건·`context_compacted` 기록",
        "후속 turn 정상 계속",
        "stdout JSONL·stderr·rollout·`CODEX_HOME` 전수 grep",
        "모델 자기보고도 음성이었다",
        "exec 경로에서 `systemMessage` 안내는 모델에 닿지 않는다(관측만 가능)",
        # T-0770 라이브 실측: 같은 exec 경로라도 진입점 훅의 additionalContext 는 도달한다.
        "`hookSpecificOutput.additionalContext`는 **모델에 닿는다**",
        "direct TUI 표시는 미검증",
        "driver 회전 선점이 relay 경로를 실보호",
        "trusted project와 `/hooks` 승인",
    )
    for anchor in anchors:
        assert anchor in normalized, f"README Context safety 앵커 누락: {anchor!r}"
    assert "PM 게이트 실측 후 확정" not in normalized
    # 옛 무조건 문장(채널 구분 없음)은 T-0770 라이브 실측이 반증했다 — 되살아나면 red.
    assert "exec 경로 안내는 모델에 닿지 않는다" not in normalized
    # T-0770 F-001: PostCompact systemMessage 의 모델 도달은 실측되지 않았다 —
    # 무범위 재주입 단정(변형 포함)이 되살아나면 red.
    assert "모델에 재주입" not in normalized
    assert "최종 텍스트를 모델에" not in normalized
    assert "codex resume --disable hooks" not in normalized
    assert "features.hooks=false" not in readme


def test_hooks_warning_is_inline_and_only_calls_engine_script():
    """별도 adapter script/정책 복제 없이 엔진 파일만 호출한다.

    진입점은 manifest 등재 디스패처를, 그 뒤 registry 는 엔진 `pm_log.py` 만 부른다 — 어느
    단계에도 어댑터 소유 스크립트 파일이나 정책 사본이 없다(원 단언의 관심사)."""
    for e in _precompact_command_entries():
        assert e.get("type") == "command", f"command 타입 hook 아님: {e!r}"
        body = e.get("command", "")
        assert isinstance(body, str), f"command 가 문자열 아님 (실 스키마 위반): {body!r}"
        assert f"{DISPATCHER_REL} --hook-dispatch PreCompact" in body
        assert "printf '%s\\n'" in body
        windows = e.get("commandWindows", "")
        assert isinstance(windows, str) and windows, f"Windows commandWindows 누락: {e!r}"
        assert f"'{DISPATCHER_REL}' --hook-dispatch PreCompact" in windows
        assert "Write-Output " in windows
        assert "&&" not in windows and ";" in windows
    engine_tools = {"{tools}/pm_log.py"}
    for feature in _features("PreCompact") + _features("PostCompact"):
        assert feature.argv[0] == "{py}", feature
        assert feature.argv[1] in engine_tools, f"어댑터 소유 스크립트 호출: {feature}"


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
    """PostCompact 진입점 뒤에 checkpoint(post)+snapshot 두 단계가 그대로 있다."""
    handler = _entrypoint_handler("PostCompact")
    for key in ("command", "commandWindows"):
        assert "--hook-dispatch PostCompact" in handler[key], key
        assert DISPATCHER_REL in handler[key], key
    assert "&&" not in handler["commandWindows"]
    checkpoint, snapshot = _features("PostCompact")
    assert checkpoint.argv[1:] == ("{tools}/pm_log.py", "checkpoint", "--trigger",
                                   "compaction", "--phase", "post", "--cwd", ".")
    assert checkpoint.side_effect_only is True, "checkpoint 단계 출력 폐기가 사라졌다"
    assert snapshot.argv[1:] == ("{tools}/pm_log.py", "snapshot", "--cwd", ".", "--json")
    assert snapshot.side_effect_only is False, "엔벨로프 단계가 답하지 않는다"


# 가짜 장부 CLI — checkpoint 단계는 사람글(stdout)과 에러(stderr)를 내고, 엔벨로프 단계만
#   JSON 을 낸다. 진입점 산출에 그 사람글이 섞이면 호스트가 엔벨로프를 못 읽는다.
_FAKE_PM_LOG = (
    "import json, sys\n"
    "if 'snapshot' in sys.argv:\n"
    "    print(json.dumps({'systemMessage':'SNAPSHOT-VERBATIM','suppressOutput':False}))\n"
    "elif 'ctx-guidance' in sys.argv:\n"
    "    print(json.dumps({'systemMessage':'ENGINE-GUIDANCE','suppressOutput':False}))\n"
    "else:\n"
    "    print('checkpoint-noise')\n"
    "    print('checkpoint-error', file=sys.stderr)\n"
)
_COMPACTION_ENVELOPE = {
    "PreCompact": {"systemMessage": "ENGINE-GUIDANCE", "suppressOutput": False},
    "PostCompact": {"systemMessage": "SNAPSHOT-VERBATIM", "suppressOutput": False},
}


@pytest.mark.skipif(
    not posix_bash_supported(), reason="POSIX shell wrapper 실행 환경이 아님"
)
def test_posix_precompact_discards_checkpoint_noise_and_emits_one_json(tmp_path):
    root = _feature_tree(tmp_path, _FAKE_PM_LOG)

    result = subprocess.run(
        ["sh", "-c", _entrypoint_handler("PreCompact")["command"]], cwd=root,
        capture_output=True, text=True, check=True,
    )

    lines = result.stdout.splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == _COMPACTION_ENVELOPE["PreCompact"]
    assert result.stderr == ""


@pytest.mark.skipif(
    not posix_bash_supported(), reason="POSIX shell wrapper 실행 환경이 아님"
)
def test_posix_postcompact_injects_builder_json_without_checkpoint_noise(tmp_path):
    root = _feature_tree(tmp_path, _FAKE_PM_LOG)

    result = subprocess.run(
        ["sh", "-c", _entrypoint_handler("PostCompact")["command"]], cwd=root,
        capture_output=True, text=True, check=True,
    )

    assert json.loads(result.stdout) == _COMPACTION_ENVELOPE["PostCompact"]
    assert result.stderr == ""


def test_hooks_windows_commands_parse_and_execute_for_pre_and_postcompact_when_available(
        tmp_path):
    """PowerShell이 있으면 PreCompact와 PostCompact commandWindows를 모두 파싱·실행한다."""
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if executable is None:
        return  # Linux CI 등 PowerShell 부재 환경은 위 static parser가 contract를 검증한다.

    for event in ("PreCompact", "PostCompact"):
        root = _feature_tree(tmp_path / event, _FAKE_PM_LOG)
        result = subprocess.run(
            [executable, "-NoProfile", "-NonInteractive", "-Command",
             _entrypoint_handler(event)["commandWindows"]],
            cwd=root, check=True, capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=utf8_child_env(),
        )

        assert json.loads(result.stdout.strip()) == _COMPACTION_ENVELOPE[event]
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


# ── 4. 세션 안 ctx 넛지 (T-0770 · claude 미러) ────────────────────────────────
# codex 어댑터엔 옆에 ctx_guard 모듈이 없어 relay driver 파일(pm_orch_codex.py)이 곧 훅 축의
# 판정 사이트다. 여기서는 그 축만 본다 — 밴드 경계·임계 키·예산 우선순위가 claude 와 **같은
# 값**인지, 점유 파서가 실제 rollout bytes 를 읽는지, 밴드 밖·측정 실패가 침묵인지.

DISPATCHER = CODEX / "pm_orch_codex.py"
CLAUDE_CTX_GUARD = (
    REPO / "templates" / "claude_code" / ".claude" / "ctx_guard.py")
# T-0835 라이브 프로브(architect R1·2026-08-22) 픽스처 — 첫 turn(rollout에 token_count 아직
# 없음) 훅 payload 4건 + 그 시점 rollout 6레코드. 두 파일 모두 `evidence`/새 fixture 헤더에
# 조달 경로(elide 스크립트)를 남긴다.
LIVE_PAYLOADS_FIXTURE = REPO / "tests" / "fixtures" / "codex_0_147_0_live_hook_payloads.json"
FIRST_TURN_ROLLOUT_FIXTURE = (
    REPO / "tests" / "fixtures" / "codex_0_147_0_first_turn_rollout.jsonl")

# codex-cli 0.147.0 격리 CODEX_HOME 라이브 프로브(2026-08-22)에서 채집한 rollout JSONL **원문
# 줄**이다. 절대경로만 <work> 로 치환했고 그 밖의 bytes 는 그대로다 — 조립 문자열 픽스처는
# 파서가 실제 형식을 읽는지 판정하지 못한다(같은 프로브가 훅 stdin transcript_path 실값도 고정).
# 마지막 token_count 의 last_token_usage.input_tokens=15328(그 시점 점유) vs
# total_token_usage.input_tokens=30516(thread 누계) — 두 값이 갈리는 실물이라 오독이 드러난다.
LIVE_ROLLOUT_INPUT_TOKENS = 15328
LIVE_ROLLOUT_THREAD_TOTAL = 30516
LIVE_ROLLOUT_PREVIOUS_TOKENS = 15188
LIVE_ROLLOUT_LINES = (
    '{"timestamp":"2026-08-22T13:46:21.770Z","type":"response_item","payload":{"type":"custom_tool_call","id":"ctc_0dbe7de5479d2aac016a89a82c1dc487d0807438694d4e432a","status":"completed","call_id":"call_4uV2VWCocwPWTSbVHqAtS8RQ","name":"exec","input":"const r = await tools.exec_command({\\"cmd\\":\\"echo probe-ok .\\",\\"workdir\\":\\"<work>\\",\\"yield_time_ms\\":10000,\\"max_output_tokens\\":1000});\\ntext(r.output);\\n","internal_chat_message_metadata_passthrough":{"turn_id":"01a029b8-dd49-7d83-bc4d-16f8f0c4062a"}}}',
    '{"timestamp":"2026-08-22T13:46:21.865Z","type":"response_item","payload":{"type":"custom_tool_call_output","id":"ctco_01a029b8-f329-7af3-a8ca-3b3cd3ad487b","call_id":"call_4uV2VWCocwPWTSbVHqAtS8RQ","output":[{"type":"input_text","text":"Script completed\\nWall time 0.1 seconds\\nOutput:\\n"},{"type":"input_text","text":"probe-ok .\\n"}],"internal_chat_message_metadata_passthrough":{"turn_id":"01a029b8-dd49-7d83-bc4d-16f8f0c4062a"}}}',
    '{"timestamp":"2026-08-22T13:46:21.865Z","type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{"input_tokens":15188,"cached_input_tokens":11008,"cache_write_input_tokens":0,"output_tokens":114,"reasoning_output_tokens":0,"total_tokens":15302},"last_token_usage":{"input_tokens":15188,"cached_input_tokens":11008,"cache_write_input_tokens":0,"output_tokens":114,"reasoning_output_tokens":0,"total_tokens":15302},"model_context_window":258400},"rate_limits":{"limit_id":"codex","limit_name":null,"primary":{"used_percent":12.0,"window_minutes":10080,"resets_at":1787906812},"secondary":null,"credits":{"has_credits":false,"unlimited":false,"balance":"0"},"individual_limit":null,"spend_control_reached":null,"plan_type":"prolite","rate_limit_reached_type":null}}}',
    '{"timestamp":"2026-08-22T13:46:23.898Z","type":"event_msg","payload":{"type":"agent_message","message":"DONE","phase":"final_answer","memory_citation":null}}',
    '{"timestamp":"2026-08-22T13:46:23.898Z","type":"response_item","payload":{"type":"message","id":"msg_0dbe7de5479d2aac016a89a82f7db087d093a5cb3328c42027","role":"assistant","content":[{"type":"output_text","text":"DONE"}],"phase":"final_answer","internal_chat_message_metadata_passthrough":{"turn_id":"01a029b8-dd49-7d83-bc4d-16f8f0c4062a"}}}',
    '{"timestamp":"2026-08-22T13:46:23.932Z","type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{"input_tokens":30516,"cached_input_tokens":25088,"cache_write_input_tokens":0,"output_tokens":119,"reasoning_output_tokens":0,"total_tokens":30635},"last_token_usage":{"input_tokens":15328,"cached_input_tokens":14080,"cache_write_input_tokens":0,"output_tokens":5,"reasoning_output_tokens":0,"total_tokens":15333},"model_context_window":258400},"rate_limits":{"limit_id":"codex","limit_name":null,"primary":{"used_percent":12.0,"window_minutes":10080,"resets_at":1787906812},"secondary":null,"credits":{"has_credits":false,"unlimited":false,"balance":"0"},"individual_limit":null,"spend_control_reached":null,"plan_type":"prolite","rate_limit_reached_type":null}}}',
    '{"timestamp":"2026-08-22T13:46:23.944Z","type":"event_msg","payload":{"type":"task_complete","turn_id":"01a029b8-dd49-7d83-bc4d-16f8f0c4062a","last_agent_message":"DONE","started_at":1787406376,"completed_at":1787406383,"duration_ms":7669,"time_to_first_token_ms":3582}}',
)

# 같은 프로브의 훅 stdin 실값(서브에이전트 발화) — 메인 세션 발화엔 이 두 키가 아예 없다.
LIVE_SUBAGENT_KEYS = {"agent_id": "01a029b9-838e-71c2-9f8a-9993158516ac",
                      "agent_type": "default"}


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def codex_ctx():
    """훅 축 판정이 사는 codex 어댑터 모듈."""
    return _load_module("pm_orch_codex_ctx", DISPATCHER)


@pytest.fixture(scope="module")
def claude_ctx():
    """미러 기준이 되는 claude 공유 코어."""
    return _load_module("claude_ctx_guard", CLAUDE_CTX_GUARD)


def _write_rollout(tmp_path: Path, lines=LIVE_ROLLOUT_LINES, *, filler_bytes: int = 0) -> Path:
    """라이브 원문 줄로 rollout 파일을 만든다(filler 는 꼬리 밖 과거 구간)."""
    path = tmp_path / "rollout-live.jsonl"
    body = ""
    if filler_bytes:
        # 꼬리 밖으로 밀려날 과거 줄 — 여기 있는 token_count 를 읽으면 파서가 최신을 못 본 것이다.
        stale = ('{"type":"event_msg","payload":{"type":"token_count","info":'
                 '{"last_token_usage":{"input_tokens":999999},'
                 '"total_token_usage":{"input_tokens":999999}}}}')
        body += stale + "\n"
        body += ('{"type":"response_item","payload":{"type":"message","text":"'
                 + "x" * filler_bytes + '"}}\n')
    body += "\n".join(lines) + "\n"
    path.write_text(body, encoding="utf-8")
    return path


def _adopter_root(tmp_path: Path, *, conf: str = "", pm_log_body: str | None = None) -> Path:
    """local.conf + (선택) pm_log 스텁만 둔 최소 채택자 트리."""
    root = tmp_path / "adopter"
    tools = root / ".project_manager" / "tools"
    tools.mkdir(parents=True)
    (root / ".project_manager" / "local.conf").write_text(conf, encoding="utf-8")
    if pm_log_body is not None:
        (tools / "pm_log.py").write_text(pm_log_body, encoding="utf-8")
    return root


# 밴드 안에 들어가는 예산 — 라이브 점유 15328 / 20000 = 77% (잔여 23% → nudge2 밴드).
_IN_BAND_BUDGET = "ctx_window_tokens_codex=20000\n"
# 밴드 밖 예산 — 15328 / 60000 = 26% (잔여 74%).
_OUT_OF_BAND_BUDGET = "ctx_window_tokens_codex=60000\n"
_STUB_PM_LOG = (
    "import sys\n"
    "sys.stdout.write('ENGINE-GUIDANCE ' + ' '.join(sys.argv[1:]) + '\\n')\n"
)


def _payload(rollout: Path, **extra) -> dict:
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "echo hi"},
        "session_id": "01a029b8-dd2b-7cc0-aac8-65158e5d120b",
        "transcript_path": str(rollout),
    }
    payload.update(extra)
    return payload


# ── 4.1 점유 파서: 라이브 rollout 원문 bytes ────────────────────────────────

def test_rollout_parser_reads_last_token_usage_from_live_capture(codex_ctx, tmp_path):
    """실물 rollout 마지막 token_count 의 last_token_usage.input_tokens 를 점유로 읽는다."""
    rollout = _write_rollout(tmp_path)

    assert codex_ctx.rollout_context_tokens(str(rollout)) == LIVE_ROLLOUT_INPUT_TOKENS
    # 누계(thread)·직전 요청 값을 잘못 읽으면 같은 파일에서 다른 수가 나온다.
    assert LIVE_ROLLOUT_INPUT_TOKENS not in (LIVE_ROLLOUT_THREAD_TOTAL,
                                             LIVE_ROLLOUT_PREVIOUS_TOKENS)


def test_rollout_parser_prefers_the_newest_record_inside_the_tail(codex_ctx, tmp_path):
    """꼬리만 읽어도 최신 값을 쓴다 — 꼬리 밖 과거 token_count 는 채택되지 않는다."""
    rollout = _write_rollout(tmp_path, filler_bytes=codex_ctx.CTX_ROLLOUT_TAIL_BYTES)

    assert rollout.stat().st_size > codex_ctx.CTX_ROLLOUT_TAIL_BYTES
    assert codex_ctx.rollout_context_tokens(str(rollout)) == LIVE_ROLLOUT_INPUT_TOKENS


@pytest.mark.parametrize("transcript", (None, "", 17, "/no/such/rollout.jsonl"))
def test_rollout_parser_is_fail_open_for_missing_transcripts(codex_ctx, transcript):
    """null·빈 값·비문자열·부재 파일은 전부 0(측정 없음) — 예외를 올리지 않는다."""
    assert codex_ctx.rollout_context_tokens(transcript) == 0


def test_rollout_parser_ignores_records_without_usable_usage(codex_ctx, tmp_path):
    """token_count 0건·손상 줄·비정상 값은 0 — 밴드 판정으로 승격하지 않는다."""
    path = tmp_path / "rollout-noise.jsonl"
    path.write_text(
        '{"type":"event_msg","payload":{"type":"agent_message","message":"hi"}}\n'
        "not json at all\n"
        '{"type":"event_msg","payload":{"type":"token_count","info":null}}\n'
        '{"type":"event_msg","payload":{"type":"token_count","info":'
        '{"last_token_usage":{"input_tokens":-3}}}}\n'
        '{"type":"event_msg","payload":{"type":"token_count","info":'
        '{"last_token_usage":{"input_tokens":true}}}}\n',
        encoding="utf-8")

    assert codex_ctx.rollout_context_tokens(str(path)) == 0


def test_rollout_parser_skips_damaged_lines_to_an_older_usable_record(codex_ctx, tmp_path):
    """마지막 token_count 가 읽히지 않으면 그 앞의 usable 값을 쓴다(부분 손상 robust)."""
    lines = list(LIVE_ROLLOUT_LINES) + [
        '{"type":"event_msg","payload":{"type":"token_count","info":{}}}']
    rollout = _write_rollout(tmp_path, lines)

    assert codex_ctx.rollout_context_tokens(str(rollout)) == LIVE_ROLLOUT_INPUT_TOKENS


# ── 4.1b 첫 turn: token_count 아직 없음 (T-0835) ────────────────────────────
# 라이브 프로브(architect R1·2026-08-22)에서 캡처한 새 thread 첫 UserPromptSubmit 시점 rollout —
# 첫 응답 전이라 token_count 0건이다(§경계 실측). ctx_nudge_envelope 는 이 구간에서 거짓 0%를
# 만들지 않고 침묵해야 하고(I1), 미측정을 "안전(ok 재무장)"으로 승격하지 않는다(I2).

# 압축 직후 codex 가 **명시적으로** 쓰는 last_token_usage.input_tokens=0(측정된 0) — T-0835
# architect R1 라이브 프로브(model_auto_compact_token_limit=7000 강제 auto 압축) idx 39 verbatim.
# 미측정(token_count 0건)과 같은 sentinel(0)로 수렴해야 한다(I2').
LIVE_ZERO_INPUT_TOKEN_COUNT_LINE = (
    '{"timestamp":"2026-08-22T14:37:30.427Z","type":"event_msg","payload":{"type":"token_count",'
    '"info":{"total_token_usage":{"input_tokens":37914,"cached_input_tokens":0,'
    '"cache_write_input_tokens":0,"output_tokens":637,"reasoning_output_tokens":0,'
    '"total_tokens":38551},"last_token_usage":{"input_tokens":0,"cached_input_tokens":0,'
    '"cache_write_input_tokens":0,"output_tokens":0,"reasoning_output_tokens":0,'
    '"total_tokens":6962},"model_context_window":258400},"rate_limits":{"limit_id":"codex",'
    '"limit_name":null,"primary":null,"secondary":null,"credits":null,"individual_limit":null,'
    '"spend_control_reached":null,"plan_type":null,"rate_limit_reached_type":null}}}'
)


def _first_turn_rollout_lines() -> tuple[str, ...]:
    return tuple(FIRST_TURN_ROLLOUT_FIXTURE.read_text(encoding="utf-8").splitlines())


def _first_turn_event(event: str) -> dict:
    """`first_turn_events` fixture 에서 그 이벤트의 라이브 payload(첫 turn) 를 읽는다."""
    document = json.loads(LIVE_PAYLOADS_FIXTURE.read_text(encoding="utf-8"))
    matches = [item for item in document["first_turn_events"]
              if item["hook_event_name"] == event]
    assert len(matches) == 1, document["first_turn_events"]
    return matches[0]


def _first_turn_payload(rollout: Path, event: str) -> dict:
    payload = _first_turn_event(event)
    payload["transcript_path"] = str(rollout)
    payload["cwd"] = str(rollout.parent)
    return payload


@pytest.mark.parametrize("event", ("UserPromptSubmit", "PreToolUse"))
def test_first_turn_without_token_count_is_silent(codex_ctx, tmp_path, event):
    """첫 turn(rollout 에 token_count 0건)은 두 채널 모두 침묵 — 엔진 문구 생성 자체가 없다.

    존재 검사가 아니라 runner 호출 리스트가 **비었음**을 값으로 단언한다(=측정 없음을 실측
    0%로 오인해 안내를 만들지 않았다). I1-a·I1-c."""
    root = _adopter_root(tmp_path, conf=_IN_BAND_BUDGET, pm_log_body=_STUB_PM_LOG)
    rollout = _write_rollout(tmp_path, lines=_first_turn_rollout_lines())
    calls = []

    def runner(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout=b"GUIDANCE", stderr=b"")

    envelope = codex_ctx.ctx_nudge_envelope(
        _first_turn_payload(rollout, event), root, runner=runner)

    assert envelope == {}
    assert calls == [], "측정 없음이 엔진 문구 생성을 호출했다 — 0%를 실측치로 오인했다는 뜻"


@pytest.mark.parametrize("event", ("UserPromptSubmit", "PreToolUse"))
def test_first_turn_never_reports_a_usage_percentage(codex_ctx, tmp_path, event):
    """첫 turn 엔벨로프 직렬화·엔진 호출 어디에도 사용률 수치가 없다(거짓 안내 부재·I1-b).

    감도: 측정 없음을 정상 관측치(used_pct=0)로 바꾸는 회귀가 있으면 `--used-pct` 인자가
    새로 나타나 이 단언이 red 가 된다."""
    root = _adopter_root(tmp_path, conf=_IN_BAND_BUDGET, pm_log_body=_STUB_PM_LOG)
    rollout = _write_rollout(tmp_path, lines=_first_turn_rollout_lines())
    calls = []

    def runner(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout=b"GUIDANCE", stderr=b"")

    envelope = codex_ctx.ctx_nudge_envelope(
        _first_turn_payload(rollout, event), root, runner=runner)

    serialized = json.dumps(envelope, ensure_ascii=False)
    assert "%" not in serialized and "컨텍스트 사용" not in serialized
    assert not any("--used-pct" in call for call in calls)


def test_unmeasured_first_turn_preserves_existing_band_markers(codex_ctx, tmp_path):
    """이미 선 밴드 marker 가 있으면(직전 사이클) 첫 turn 측정 실패가 그걸 지우지 않는다(I2).

    감도: `ctx_nudge_envelope` 의 `if tokens > 0:` 재무장 가드를 지우면 이 marker 가 사라져
    red 가 된다."""
    root = _adopter_root(tmp_path, conf=_IN_BAND_BUDGET, pm_log_body=_STUB_PM_LOG)
    measured_rollout = _write_rollout(tmp_path)
    codex_ctx.ctx_nudge_envelope(_payload(measured_rollout), root)
    marker_dir = root / ".project_manager" / ".local" / "ctx-stop"
    before = sorted(p.name for p in marker_dir.iterdir())
    assert before

    first_turn_rollout = tmp_path / "rollout-first-turn.jsonl"
    first_turn_rollout.write_text(
        "\n".join(_first_turn_rollout_lines()) + "\n", encoding="utf-8")
    first_turn_payload = _first_turn_payload(first_turn_rollout, "UserPromptSubmit")
    first_turn_payload["session_id"] = _payload(measured_rollout)["session_id"]  # 같은 사이클.

    assert codex_ctx.ctx_nudge_envelope(first_turn_payload, root) == {}
    after = sorted(p.name for p in marker_dir.iterdir())
    assert after == before, "측정 불가(첫 turn)가 기존 marker 를 지웠다 — 재무장 가드 위반"


def test_zero_input_token_count_after_compaction_is_treated_as_unmeasured(codex_ctx, tmp_path):
    """압축 직후 codex 가 명시적으로 쓰는 last_token_usage.input_tokens=0(측정된 0)도 미측정과
    같은 sentinel(0)로 수렴한다 — 침묵·marker 부재(라이브 실물 idx 39·I2')."""
    root = _adopter_root(tmp_path, conf=_IN_BAND_BUDGET, pm_log_body=_STUB_PM_LOG)
    rollout = tmp_path / "rollout-post-compaction.jsonl"
    rollout.write_text(LIVE_ZERO_INPUT_TOKEN_COUNT_LINE + "\n", encoding="utf-8")

    assert codex_ctx.rollout_context_tokens(str(rollout)) == 0
    assert codex_ctx.ctx_nudge_envelope(_payload(rollout), root) == {}
    assert not (root / ".project_manager" / ".local" / "ctx-stop").exists()


def test_first_turn_payload_runs_through_the_shipped_entrypoint_silently(codex_ctx, tmp_path):
    """첫 turn payload 를 출하 진입점(`dispatch_hook`)에 그대로 먹여도 합본은 `{}`(I5 확장)."""
    root = _adopter_root(tmp_path, conf=_IN_BAND_BUDGET, pm_log_body=_STUB_PM_LOG)
    rollout = _write_rollout(tmp_path, lines=_first_turn_rollout_lines())

    for event in ("UserPromptSubmit", "PreToolUse"):
        payload = _first_turn_payload(rollout, event)
        assert codex_ctx.dispatch_hook(
            event, json.dumps(payload).encode("utf-8"), root) == {}


def test_prose_does_not_claim_first_turn_coverage(codex_ctx):
    """산문(docstring·출하 README)에 첫 turn 한계 문장 앵커가 있고, "첫 turn 도 보호" 류
    무범위 주장은 없다(I6·claude ctx_guard.py 선례와 동형). 실측 범위(`codex exec`)도
    명시돼야 한다 — 라이브 프로브는 `codex exec`/`exec resume`/auto-compaction 만 쟀고
    direct TUI 는 미실측이라, 그 caveat 없이 결론을 Codex 전체 사실로 쓰면 이 단언이
    red 가 된다(F-002 fix · code-reviewer R4)."""
    readme = (REPO / "templates" / "codex" / "README.md").read_text(encoding="utf-8")
    rollout_doc = codex_ctx.rollout_context_tokens.__doc__ or ""
    docstrings = rollout_doc + (codex_ctx.ctx_nudge_envelope.__doc__ or "")

    assert "첫 turn" in readme and "보호하지 못한다" in readme
    assert "첫 turn" in docstrings and "T-0835" in docstrings
    for prose in (readme, docstrings):
        assert "첫 turn도 보호" not in prose and "첫 turn을 보호한다" not in prose
    # F-002: "무방비는 새 thread 첫 요청 1회뿐" 결론은 codex exec 축만 실측했다 — 그 실측
    # 범위 한정과 direct TUI 미실측 caveat 가 README·rollout_context_tokens docstring
    # 양쪽에 있어야 한다(빠지면 이 티켓 산출 자체가 실측 범위를 넘어선 주장이 된다).
    for prose, label in ((readme, "README"), (rollout_doc, "rollout_context_tokens docstring")):
        assert "codex exec" in prose, f"{label}: codex exec 실측 범위 한정 문구 부재"
        assert "direct TUI" in prose and "미실측" in prose, (
            f"{label}: direct TUI 미실측 caveat 부재")


# ── 4.2 임계·예산·밴드가 claude 와 같은 키·같은 값인가 ───────────────────────

@pytest.mark.parametrize("conf", (
    {},
    {"ctx_nudge_pct": "40", "ctx_stop_pct": "25"},
    {"ctx_nudge_pct": "10", "ctx_stop_pct": "20"},   # 역전 → 둘 다 엔진 기본 폴백.
    {"ctx_stop_pct": "0"},                            # 범위 밖 → 폴백.
    {"ctx_nudge_pct": "abc", "ctx_stop_pct": "  25 "},
))
def test_thresholds_mirror_claude_keys_and_sanity(codex_ctx, claude_ctx, conf):
    """`ctx_nudge_pct`/`ctx_stop_pct` 해소와 sanity 폴백이 claude 와 값으로 같다."""
    assert codex_ctx.ctx_thresholds(conf) == claude_ctx.ctx_thresholds(conf)


@pytest.mark.parametrize("conf", (
    {},
    {"ctx_window_tokens": "500000"},
    {"ctx_window_tokens_codex": "300000", "ctx_window_tokens": "500000"},
    {"ctx_window_tokens_codex": "0", "ctx_window_tokens": "400000"},
    {"ctx_window_tokens_codex": "nope"},
))
def test_budget_precedence_mirrors_claude_per_harness_keys(codex_ctx, claude_ctx, conf):
    """예산 우선순위 `ctx_window_tokens_codex` > `ctx_window_tokens` > 200000 (ADR-0041)."""
    assert codex_ctx.resolve_ctx_budget(conf) == claude_ctx.resolve_budget(conf, "codex")


def test_budget_precedence_values_are_pinned(codex_ctx):
    """우선순위 3층의 실값 — 미러 대조만으로는 두 사이트가 함께 틀릴 수 있다."""
    assert codex_ctx.resolve_ctx_budget(
        {"ctx_window_tokens_codex": "300000", "ctx_window_tokens": "500000"}) == 300000
    assert codex_ctx.resolve_ctx_budget({"ctx_window_tokens": "500000"}) == 500000
    assert codex_ctx.resolve_ctx_budget({}) == codex_ctx.CTX_WINDOW_TOKENS_DEFAULT == 200_000


@pytest.mark.parametrize("conf", (
    {}, {"ctx_nudge_pct": "40", "ctx_stop_pct": "25"}, {"ctx_stop_pct": "5"},
))
def test_band_classification_mirrors_claude_for_every_used_pct(codex_ctx, claude_ctx, conf):
    """0~100% 전 구간에서 밴드 이름이 claude 와 같다 — codex 전용 경계 신설 0."""
    thresholds = codex_ctx.ctx_thresholds(conf)
    assert codex_ctx.nudge2_threshold(thresholds) == claude_ctx.nudge2_threshold(thresholds)
    for used_pct in range(0, 101):
        assert (codex_ctx.classify(used_pct, thresholds)
                == claude_ctx.classify(used_pct, thresholds)), used_pct


@pytest.mark.parametrize("tokens,budget", (
    (0, 200000), (1, 200000), (15328, 20000), (199999, 200000), (400000, 200000),
    (15328, 0), (-5, 200000),
))
def test_used_pct_from_tokens_mirrors_claude(codex_ctx, claude_ctx, tokens, budget):
    assert (codex_ctx.context_used_pct_from_tokens(tokens, budget)
            == claude_ctx.context_used_pct_from_tokens(tokens, budget))


# ── 4.3 배선: registry 한 줄이고 채택자 config 는 무변경 ──────────────────────

def test_ctx_nudge_is_wired_through_the_dispatcher_registry(codex_ctx):
    """두 진입점 이벤트에 도구 무관 기능으로 등록되고, 판정은 in-process 다."""
    features = {feature.feature_id: feature for feature in codex_ctx.CODEX_HOOK_FEATURES
                if feature.feature_id.startswith("ctx-nudge")}

    assert {feature.event for feature in features.values()} == {"PreToolUse",
                                                                "UserPromptSubmit"}
    for feature in features.values():
        assert feature.tool_pattern is None, feature
        assert feature.handler is codex_ctx.ctx_nudge_envelope, feature
        assert feature.event in codex_ctx.CODEX_HOOK_ENTRYPOINT_EVENTS, feature
    exposed = {item["feature_id"] for item in codex_ctx.hook_feature_registry()["features"]}
    assert set(features) <= exposed


def test_ctx_wiring_adds_nothing_to_the_adopter_hooks_config():
    """`.codex/hooks.json` 은 이 축의 배선을 담지 않는다 — 채택자 재승인 0 (T-0770 DoD)."""
    rendered = HOOKS_JSON.read_text(encoding="utf-8")

    for token in ("ctx-nudge", "ctx-guidance --band nudge", "ctx_nudge_pct",
                  "rollout", "transcript_path"):
        assert token not in rendered, token


# ── 4.4 밴드 발화·침묵·멱등 (판정 함수) ──────────────────────────────────────

def test_band_entry_injects_engine_guidance_verbatim(codex_ctx, tmp_path):
    """밴드 안에서는 pm_log ctx-guidance stdout 을 **그대로** additionalContext 로 낸다."""
    root = _adopter_root(tmp_path, conf=_IN_BAND_BUDGET, pm_log_body=_STUB_PM_LOG)
    rollout = _write_rollout(tmp_path)

    envelope = codex_ctx.ctx_nudge_envelope(_payload(rollout), root)

    hook_output = envelope["hookSpecificOutput"]
    assert hook_output["hookEventName"] == "PreToolUse"
    guidance = hook_output["additionalContext"]
    assert guidance.startswith("ENGINE-GUIDANCE ")
    # 엔진에 넘긴 밴드·수치가 실측 판정값이다(15328/20000 = 77% → 잔여 23% → nudge2).
    assert "ctx-guidance --band nudge2" in guidance
    assert "--used-pct 77" in guidance and "--remaining-pct 23" in guidance
    assert "--stop-pct 20" in guidance
    # 비차단 — 어떤 결정 필드도 내지 않는다(ADR-0081 D1·codex 는 allow/ask 자체를 거부한다).
    assert set(envelope) == {"hookSpecificOutput"}
    assert "permissionDecision" not in hook_output and "decision" not in envelope


def test_user_prompt_submit_uses_its_own_event_name(codex_ctx, tmp_path):
    """두 채널 모두 자기 이벤트 이름으로 주입한다(호스트가 이름으로 봉투를 검증한다)."""
    root = _adopter_root(tmp_path, conf=_IN_BAND_BUDGET, pm_log_body=_STUB_PM_LOG)
    rollout = _write_rollout(tmp_path)

    envelope = codex_ctx.ctx_nudge_envelope(
        _payload(rollout, hook_event_name="UserPromptSubmit", tool_name=None), root)

    assert envelope["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"


def test_out_of_band_traffic_is_silent(codex_ctx, tmp_path):
    """밴드 밖은 빈 엔벨로프 — 이 기능이 합본에 기여하는 바이트가 0이다."""
    root = _adopter_root(tmp_path, conf=_OUT_OF_BAND_BUDGET, pm_log_body=_STUB_PM_LOG)
    rollout = _write_rollout(tmp_path)

    assert codex_ctx.ctx_nudge_envelope(_payload(rollout), root) == {}
    assert not (root / ".project_manager" / ".local" / "ctx-stop").exists()


@pytest.mark.parametrize("transcript", (None, "/no/such/rollout.jsonl", "tokenless"))
def test_measurement_failure_is_silent_fail_open(codex_ctx, tmp_path, transcript):
    """transcript null·부재·token_count 0건 3형상 — 빈 엔벨로프(가드가 세션을 막지 않는다)."""
    root = _adopter_root(tmp_path, conf=_IN_BAND_BUDGET, pm_log_body=_STUB_PM_LOG)
    if transcript == "tokenless":
        path = tmp_path / "rollout-tokenless.jsonl"
        path.write_text('{"type":"event_msg","payload":{"type":"agent_message"}}\n',
                        encoding="utf-8")
        transcript = str(path)

    assert codex_ctx.ctx_nudge_envelope(_payload(tmp_path, transcript_path=transcript),
                                        root) == {}


def test_nudge_is_injected_once_per_cycle(codex_ctx, tmp_path):
    """같은 사이클 2회 호출 시 두 번째는 침묵 — marker 선점(멱등·claude 규약 재사용)."""
    root = _adopter_root(tmp_path, conf=_IN_BAND_BUDGET, pm_log_body=_STUB_PM_LOG)
    rollout = _write_rollout(tmp_path)

    first = codex_ctx.ctx_nudge_envelope(_payload(rollout), root)
    second = codex_ctx.ctx_nudge_envelope(_payload(rollout), root)

    assert first != {} and second == {}
    markers = sorted(p.name for p in
                     (root / ".project_manager" / ".local" / "ctx-stop").iterdir())
    assert markers == ["01a029b8-dd2b-7cc0-aac8-65158e5d120b.nudge2"]


def test_measured_ok_rearms_the_cycle_but_unmeasured_ok_does_not(codex_ctx, tmp_path):
    """재무장은 **실측된 ok**(점유>0)에서만 — 측정 실패는 marker 를 보존한다."""
    root = _adopter_root(tmp_path, conf=_IN_BAND_BUDGET, pm_log_body=_STUB_PM_LOG)
    rollout = _write_rollout(tmp_path)
    codex_ctx.ctx_nudge_envelope(_payload(rollout), root)
    marker_dir = root / ".project_manager" / ".local" / "ctx-stop"
    assert list(marker_dir.iterdir())

    # 측정 불가(transcript 부재)는 ok 로 보이지만 재무장하지 않는다.
    codex_ctx.ctx_nudge_envelope(_payload(rollout, transcript_path=None), root)
    assert list(marker_dir.iterdir())

    # 예산이 커져 실측 ok 로 돌아오면(압축 후 형상) 다음 상승 사이클이 열린다.
    (root / ".project_manager" / "local.conf").write_text(_OUT_OF_BAND_BUDGET,
                                                          encoding="utf-8")
    assert codex_ctx.ctx_nudge_envelope(_payload(rollout), root) == {}
    assert not list(marker_dir.iterdir())


def test_subagent_hook_calls_are_exempt(codex_ctx, tmp_path):
    """서브에이전트 발화는 면제 — 부모 사이클 marker 를 소비하지 않는다(라이브 키 실값).

    라이브 실측: 서브에이전트 훅 payload 의 `session_id` 는 **부모와 같고** `agent_id`/
    `agent_type` 만 추가된다. 면제가 없으면 서브에이전트가 부모의 사이클 주입권을 가져간다."""
    root = _adopter_root(tmp_path, conf=_IN_BAND_BUDGET, pm_log_body=_STUB_PM_LOG)
    rollout = _write_rollout(tmp_path)

    assert codex_ctx.ctx_nudge_envelope(_payload(rollout, **LIVE_SUBAGENT_KEYS), root) == {}
    assert not (root / ".project_manager" / ".local" / "ctx-stop").exists()
    # 감도: 같은 payload 에서 서브에이전트 키만 빼면 발화한다.
    assert codex_ctx.ctx_nudge_envelope(_payload(rollout), root) != {}


def test_unrelated_hook_events_are_not_touched(codex_ctx, tmp_path):
    """주입 채널은 두 이벤트뿐 — PostToolUse 등에서는 판정 자체를 하지 않는다."""
    root = _adopter_root(tmp_path, conf=_IN_BAND_BUDGET, pm_log_body=_STUB_PM_LOG)
    rollout = _write_rollout(tmp_path)

    assert codex_ctx.ctx_nudge_envelope(
        _payload(rollout, hook_event_name="PostToolUse"), root) == {}


def test_guidance_engine_absence_is_silent_and_keeps_the_cycle_open(codex_ctx, tmp_path):
    """엔진 문구를 못 읽으면 문구를 복제하지 않고 침묵하며, marker 도 안 쓴다(다음 호출 재시도)."""
    root = _adopter_root(tmp_path, conf=_IN_BAND_BUDGET)  # pm_log.py 없음.
    rollout = _write_rollout(tmp_path)

    assert codex_ctx.ctx_nudge_envelope(_payload(rollout), root) == {}
    assert not (root / ".project_manager" / ".local" / "ctx-stop").exists()


def test_guidance_command_shape_is_the_engine_single_source(codex_ctx, tmp_path):
    """어댑터는 `pm_log.py ctx-guidance` 를 부를 뿐 문구를 만들지 않는다(호출 argv 실값)."""
    root = _adopter_root(tmp_path, conf=_IN_BAND_BUDGET, pm_log_body=_STUB_PM_LOG)
    rollout = _write_rollout(tmp_path)
    calls = []

    def runner(argv, **kwargs):
        calls.append((list(argv), kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout=b"GUIDANCE", stderr=b"")

    envelope = codex_ctx.ctx_nudge_envelope(_payload(rollout), root, runner=runner)

    assert envelope["hookSpecificOutput"]["additionalContext"] == "GUIDANCE"
    argv, kwargs = calls[0]
    assert argv[0] == sys.executable
    assert argv[1] == str(root / ".project_manager" / "tools" / "pm_log.py")
    assert argv[2:] == ["ctx-guidance", "--band", "nudge2", "--used-pct", "77",
                        "--remaining-pct", "23", "--stop-pct", "20"]
    assert kwargs["cwd"] == str(root)
    assert 0 < kwargs["timeout"] <= codex_ctx.CTX_GUIDANCE_TIMEOUT_SEC


def test_guidance_failure_modes_are_silent(codex_ctx, tmp_path):
    """엔진 rc≠0·timeout·OSError 어느 것도 문구 복제나 예외로 새지 않는다."""
    root = _adopter_root(tmp_path, conf=_IN_BAND_BUDGET, pm_log_body=_STUB_PM_LOG)
    rollout = _write_rollout(tmp_path)

    def failing(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 3, stdout=b"partial", stderr=b"")

    def timing_out(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 3.0)

    def exploding(argv, **kwargs):
        raise OSError("no interpreter")

    for runner in (failing, timing_out, exploding):
        assert codex_ctx.ctx_nudge_envelope(_payload(rollout), root, runner=runner) == {}


# ── 4.5 디스패처 합본·출하 CLI (침묵이 실제로 침묵인가) ──────────────────────

def test_dispatcher_merges_the_nudge_without_touching_other_features(codex_ctx, tmp_path):
    """진입점 합본에서 ctx 넛지만 답하면 그 엔벨로프가 그대로 나가고, 자식은 안 뜬다."""
    root = _adopter_root(tmp_path, conf=_IN_BAND_BUDGET, pm_log_body=_STUB_PM_LOG)
    rollout = _write_rollout(tmp_path)
    spawned = []

    def runner(argv, **kwargs):
        spawned.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout=b"{}", stderr=b"")

    envelope = codex_ctx.dispatch_hook(
        "PreToolUse", json.dumps(_payload(rollout)).encode("utf-8"), root, runner=runner)

    assert envelope["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert spawned == [], "도구 무관 판정이 매 호출마다 자식 프로세스를 띄웠다"


def test_dispatcher_stays_byte_identical_to_the_pass_shape_out_of_band(codex_ctx, tmp_path):
    """밴드 밖 합본은 이 가드가 없던 때와 같은 통과 형태(`{}`)다 — 새 잡음 0."""
    root = _adopter_root(tmp_path, conf=_OUT_OF_BAND_BUDGET, pm_log_body=_STUB_PM_LOG)
    rollout = _write_rollout(tmp_path)

    assert codex_ctx.dispatch_hook(
        "PreToolUse", json.dumps(_payload(rollout)).encode("utf-8"), root) == {}
    assert codex_ctx.dispatch_hook(
        "UserPromptSubmit",
        json.dumps(_payload(rollout, hook_event_name="UserPromptSubmit")).encode("utf-8"),
        root) == {}


@pytest.mark.parametrize("conf,expected_prefix", (
    (_IN_BAND_BUDGET, '{"hookSpecificOutput"'),
    (_OUT_OF_BAND_BUDGET, "{}"),
))
def test_shipped_cli_emits_one_line_and_rc0(tmp_path, conf, expected_prefix):
    """출하 CLI(`--hook-dispatch`)를 실제로 태운다 — rc0 + 한 줄, 밴드 밖은 `{}` 뿐."""
    root = _adopter_root(tmp_path, conf=conf, pm_log_body=_STUB_PM_LOG)
    shutil.copy2(DISPATCHER, root / "pm_orch_codex.py")
    (root / ".project_manager" / "tools" / "pm_handoff.py").write_text("", encoding="utf-8")
    rollout = _write_rollout(tmp_path)

    completed = subprocess.run(
        [sys.executable, str(root / "pm_orch_codex.py"), "--hook-dispatch", "PreToolUse"],
        input=json.dumps(_payload(rollout)).encode("utf-8"),
        capture_output=True, timeout=60)

    assert completed.returncode == 0, completed.stderr
    assert len(completed.stdout.splitlines()) == 1
    assert completed.stdout.decode("ascii").startswith(expected_prefix)
    assert "adapter-fallback" not in completed.stdout.decode("ascii")
