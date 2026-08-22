"""codex 훅 범용 진입점 + 기능 디스패처 (T-0777).

## 이 파일이 있는 이유
`.codex/hooks.json` 은 채택자 소유(manifest 밖)라, 가드 기능을 하나 더할 때마다 **채택자 config
수정 + `/hooks` 재승인**을 다시 요구했다. 그 마찰이 기능마다 반복되는 것이 결함이다. 이제
이벤트당 진입점을 하나만 열고(`matcher .*` → `.codex/pm_orch_codex.py --hook-dispatch <이벤트>`)
"어떤 가드를 돌릴지" 의 판단을 **manifest 등재 코드**가 쥔다 — 이후 기능 추가는 registry 한
줄이고 config 는 다시 안 건드린다.

이 파일이 고정하는 성질:
  - 진입점 집합은 hooks.json·디스패처·엔진 역방향 선언 **세 곳에서 같은 값**이다(선언 두 벌 금지).
  - 진입점 뒤 분기가 옛 matcher 판정을 값으로 보존한다(스폰만 위임 가드로 간다).
  - 걸리는 기능이 없으면 자식 프로세스를 하나도 안 띄운다(진입점이 넓어진 비용 상한).
  - 기능 고장·구세대 디스패처·디스패처 부재 어느 경우에도 **도구 호출이 막히지 않는다**
    (rc0 + 완전한 엔벨로프 · 폴백 사실은 마커로 남는다).

판정 근거는 조립한 문자열이 아니라 **출하 hooks.json 의 커맨드를 실제 셸로 실행한 rc/출력**이다.
"""
from __future__ import annotations

import importlib.util
import json
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from _hook_commands import powershell_native_arguments
from _win_skip import posix_bash_supported

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
CODEX_TEMPLATE = REPO / "templates" / "codex"
HOOKS_JSON = CODEX_TEMPLATE / ".codex" / "hooks.json"
DISPATCHER_REL = ".codex/pm_orch_codex.py"
DISPATCHER_PY = CODEX_TEMPLATE / DISPATCHER_REL


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def dispatcher():
    return _load("pm_orch_codex", DISPATCHER_PY)


@pytest.fixture(scope="module")
def pm_import():
    return _load("pm_import", TOOLS / "pm_import.py")


@pytest.fixture(scope="module")
def guard():
    return _load("delegate_channel_guard", TOOLS / "delegate_channel_guard.py")


def _hooks() -> dict:
    return json.loads(HOOKS_JSON.read_text(encoding="utf-8"))["hooks"]


def _entry_handler(event: str) -> dict:
    groups = _hooks()[event]
    assert len(groups) == 1, f"{event}: 진입점은 이벤트당 하나다 ({len(groups)}개)"
    handlers = groups[0]["hooks"]
    assert len(handlers) == 1, f"{event}: 진입점 handler 는 하나다"
    return handlers[0]


# ── 진입점 선언이 세 곳에서 같은 값인가 ──────────────────────────────────────


def test_shipped_hooks_json_opens_one_universal_entrypoint_per_declared_event(
        dispatcher):
    """선언된 이벤트마다 `matcher .*` 진입점이 정확히 하나씩 있고 디스패처를 부른다."""
    events = _hooks()
    for event in dispatcher.CODEX_HOOK_ENTRYPOINT_EVENTS:
        assert events[event][0]["matcher"] == ".*", event
        handler = _entry_handler(event)
        assert handler["type"] == "command"
        for key in ("command", "commandWindows"):
            assert DISPATCHER_REL in handler[key], (event, key)
            assert f"{dispatcher.CODEX_HOOK_DISPATCH_FLAG} {event}" in handler[key]


def test_engine_reverse_declaration_names_the_same_entrypoints(dispatcher, pm_import):
    """엔진 역방향 선언과 디스패처가 같은 (이벤트, matcher, 디스패처, 플래그)를 말한다.

    두 벌이 갈리면 한쪽만 진입점을 늘려 놓고 다른 쪽은 그 사실을 모른 채 green 이 된다."""
    declared = pm_import.ADAPTER_HOOK_SET["codex"].entrypoints

    assert tuple(item.event for item in declared) == tuple(
        dispatcher.CODEX_HOOK_ENTRYPOINT_EVENTS)
    for item in declared:
        assert item.matcher == ".*"
        assert item.dispatcher == DISPATCHER_REL
        assert item.flag == dispatcher.CODEX_HOOK_DISPATCH_FLAG


def test_every_registered_feature_has_a_shipped_entrypoint(dispatcher):
    """등록 기능의 이벤트는 전부 진입점이 열려 있다 — 안 열린 이벤트의 기능은 죽은 코드다."""
    assert dispatcher.CODEX_HOOK_FEATURES, "등록 기능 0 (공허 가드)"
    for feature in dispatcher.CODEX_HOOK_FEATURES:
        assert feature.event in dispatcher.CODEX_HOOK_ENTRYPOINT_EVENTS, feature
        assert feature.event in _hooks(), feature


def test_events_outside_the_entrypoint_set_keep_their_feature_specific_wiring():
    """진입점 밖 이벤트는 이 티켓이 건드리지 않는다 — 배선 변경 = `/hooks` 재승인이라 최소로 둔다."""
    events = _hooks()
    assert set(events) == {"PreToolUse", "UserPromptSubmit", "PostToolUse",
                           "SubagentStart", "PreCompact", "PostCompact"}
    assert {group["matcher"] for group in events["SubagentStart"]} == {".*"}
    assert {group["matcher"] for group in events["PreCompact"]} == {"^auto$", "^manual$"}
    assert {group["matcher"] for group in events["PostCompact"]} == {"^auto$", "^manual$"}
    for event in ("SubagentStart", "PreCompact", "PostCompact"):
        for group in events[event]:
            for handler in group["hooks"]:
                assert DISPATCHER_REL not in handler["command"], event


def test_timeout_layers_are_ordered_supervisor_dispatcher_host(dispatcher, guard):
    """엔진 감독자 < 디스패처 예산 < 호스트 훅 timeout.

    디스패처 예산이 감독자보다 짧으면 감독자가 완전한 엔벨로프를 내기 전에 죽여 사유가 사라지고,
    호스트 timeout 보다 길면 호스트가 먼저 죽여 같은 일이 벌어진다."""
    for event in dispatcher.CODEX_HOOK_ENTRYPOINT_EVENTS:
        assert (guard.CODEX_SUPERVISOR_TIMEOUT_SECONDS
                < dispatcher.CODEX_HOOK_DISPATCH_BUDGET_SEC
                < _entry_handler(event)["timeout"]), event


# ── 기능 ID 기계 노출 (기능 파리티 가드가 소비) ──────────────────────────────


def test_feature_registry_is_exposed_as_machine_readable_json():
    """`--hook-features` 가 등록 기능 ID 목록을 기계 판독 형태로 낸다.

    배선이 디스패처 뒤로 들어가면 config 파싱으로는 기능을 열거할 수 없다 — 파리티 가드가
    소비할 입력이 이 출력이다."""
    completed = subprocess.run(
        [sys.executable, str(DISPATCHER_PY), "--hook-features"],
        capture_output=True, text=True, encoding="utf-8", check=True, timeout=60)
    payload = json.loads(completed.stdout)

    assert payload["entrypoint_events"] == ["PreToolUse", "UserPromptSubmit",
                                            "PostToolUse"]
    ids = [item["feature_id"] for item in payload["features"]]
    assert ids == sorted(set(ids)) or len(ids) == len(set(ids)), \
        f"기능 ID 중복: {ids}"
    assert "delegate-channel" in ids
    for item in payload["features"]:
        assert item["event"] in payload["entrypoint_events"], item


def test_feature_registry_matches_the_module_declaration(dispatcher):
    """노출 목록이 registry 파생이다 — 손으로 유지되는 두 번째 목록이 아니다."""
    payload = dispatcher.hook_feature_registry()

    assert [item["feature_id"] for item in payload["features"]] == [
        feature.feature_id for feature in dispatcher.CODEX_HOOK_FEATURES]


# ── 출하 문서 (채널 정책이 산문으로도 서 있나) ───────────────────────────────


README = CODEX_TEMPLATE / "README.md"


def test_readme_documents_the_universal_entrypoint_and_feature_registry(dispatcher):
    """출하 README 가 진입점·registry·재승인 불요를 명시한다(채택자가 읽는 유일한 산문)."""
    readme = " ".join(README.read_text(encoding="utf-8").split())

    for anchor in (
        "`PreToolUse`·`UserPromptSubmit`·`PostToolUse`는 이벤트당 진입점을 **하나씩만** 연다",
        "`.codex/pm_orch_codex.py --hook-dispatch <이벤트>`",
        "가드 기능 추가는 엔진 코드 변경뿐",
        "`/hooks` 재승인도 다시 하지 않는다",
        "--hook-features",
        "진입점 집합 자체는 릴리즈 간 불변",
        "adapter-fallback",
    ):
        assert anchor in readme, anchor
    # 옛 산문(“pm_update가 덮지 않는다” 로 끝나던 수동 병합 안내)이 남으면 채널 설명이 두 벌이다.
    assert "`.codex/hooks.json`은 instance-owned라 `pm_update`가 덮지 않는다" not in readme


def test_readme_states_that_report_files_stay_a_manual_accept():
    """`report` 3파일의 이벤트 배선이 수동 `--accept` 로 남는다는 사실이 출하 문서에 있다.

    닫지 않기로 한 것도 결정이다 — 산문에 없으면 채택자는 그 파일이 자동으로 따라온다고 읽는다."""
    readme = " ".join(README.read_text(encoding="utf-8").split())

    for anchor in (
        "`.codex/config.toml` | `report`",
        "`.claude/settings.json` | `report`",
        "`.opencode/opencode.jsonc` | `report`",
        "이 세 파일의 이벤트 배선은 수동 1커맨드로 남는다",
        "sync-adapter-config --accept <경로>",
        "자동화 대상이 아니라는 것이 결정이며",
        "편집분(`edited`)은 어느 분류에서도 자동 갱신되지 않고 보존된다",
    ):
        assert anchor in readme, anchor


# ── 진입점 뒤 분기 (옛 matcher 판정 보존) ────────────────────────────────────


def _spawn_payload() -> dict:
    return {"hook_event_name": "PreToolUse", "tool_name": "collaborationspawn_agent",
            "tool_input": {"task_name": "developer"}, "cwd": "/abs/adopter"}


def _delegate_feature(dispatcher_module):
    """registry 에 등록된 위임 채널 가드 — 옛 exact matcher 판정이 값으로 옮겨 온 항목."""
    features = [feature for feature in dispatcher_module.CODEX_HOOK_FEATURES
                if feature.feature_id == "delegate-channel"]
    assert len(features) == 1, dispatcher_module.CODEX_HOOK_FEATURES
    return features[0]


class _RecordingRunner:
    """자식 실행을 기록하는 stub — 스폰 여부 자체가 판정 대상이다."""

    def __init__(self, stdout: bytes = b"{}", returncode: int = 0):
        self.calls: list[list[str]] = []
        self._stdout = stdout
        self._returncode = returncode

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(argv, self._returncode,
                                           stdout=self._stdout, stderr=b"")


def test_only_matching_features_spawn_a_child(dispatcher, tmp_path):
    """판별식에 안 걸리면 자식을 하나도 안 띄운다 — 진입점이 전 도구 호출로 넓어진 비용 상한."""
    runner = _RecordingRunner()

    idle = dispatcher.dispatch_hook(
        "PreToolUse", json.dumps({"tool_name": "shell"}).encode("utf-8"), tmp_path,
        runner=runner)

    assert idle == {}
    assert runner.calls == []

    matched = dispatcher.dispatch_hook(
        "PreToolUse", json.dumps(_spawn_payload()).encode("utf-8"), tmp_path,
        runner=runner)

    assert matched == {}
    assert len(runner.calls) == 1
    argv = runner.calls[0]
    assert argv[0] == sys.executable
    assert argv[1] == str(tmp_path / ".project_manager" / "tools"
                          / "delegate_channel_guard.py")
    assert argv[2:4] == ["supervise", "PreToolUse"]
    assert argv[-1] == "codex-hook"


def test_events_without_registered_features_answer_the_measured_allow_shape(
        dispatcher, tmp_path):
    """기능이 아직 없는 진입점도 유효한 통과 엔벨로프를 낸다(진입점만 먼저 여는 형상)."""
    runner = _RecordingRunner()

    for event in ("UserPromptSubmit", "PostToolUse"):
        assert dispatcher.dispatch_hook(event, b"{}", tmp_path, runner=runner) == {}
    assert runner.calls == []


def test_unparsable_payload_still_runs_tool_agnostic_features(dispatcher, guard,
                                                              tmp_path):
    """payload 를 못 읽어도 도구 무관 기능은 돌고, 도구 판별 기능은 **경고**로 남는다.

    도구 판별 기능이 안 도는 것은 보수적 처분이라 맞다. 그 사실이 통과와 같은 값(`{}`)으로
    나가면 가드가 꺼진 형상과 구별되지 않는다 — 옛 배선에서 같은 입력에 가드 자식이 내던
    경고를 여기서 낸다."""
    runner = _RecordingRunner()
    agnostic = dispatcher.CodexHookFeature(
        "fixture-agnostic", "PostToolUse", None, ("{py}", "-c", "print('{}')"))

    assert dispatcher.dispatch_hook("PostToolUse", b"not json", tmp_path,
                                    features=(agnostic,), runner=runner) == {}
    assert len(runner.calls) == 1
    envelope = dispatcher.dispatch_hook(
        "PreToolUse", b"not json", tmp_path,
        features=dispatcher.CODEX_HOOK_FEATURES, runner=runner)

    assert set(envelope) == set(guard.CODEX_WARNING_ENVELOPE_KEYS)
    assert guard.CODEX_ADAPTER_FALLBACK_MARKER in envelope["systemMessage"]
    assert "delegate-channel" in envelope["systemMessage"]
    assert len(runner.calls) == 1, "판별식 있는 기능이 미지 payload 에 발화했다"


# ── 판정 불능 vs 정상 미매칭 (조용한 실패 금지 · T-0777 불변식 4) ────────────
# 옛 배선은 hooks.json matcher 가 호스트 쪽에서 판별했고, 판별 근거가 없는 입력은 가드 자식이
# 경고로 냈다(실측: 빈 stdin·`{}`·파손 JSON 모두 rc0 경고 엔벨로프). 판별이 진입점 뒤로 옮겨
# 온 뒤에도 그 의미가 남아야 한다 — 아래는 "못 판정했다" 와 "안 걸렸다" 가 같은 값으로 접히지
# 않는지를 고정한다. 등급은 종전대로 rc0·차단 0 이다(경고만 복원·승격 아님).


@pytest.mark.parametrize("label,payload_bytes,detail_fragment", (
    ("빈 stdin", b"", "빈 stdin"),
    ("파손 JSON", b"{not json", "JSONDecodeError"),
    ("라우팅 필드 부재", b"{}", "tool_name 이 없어"),
    # 빈 이름·비문자열 이름도 라우팅 값이 없는 것과 같다 — 옛 출하 커맨드도 이 입력에
    #   rc0 경고를 냈다(실측: `{"tool_name":""}` → supervisor-fallback 경고).
    ("빈 tool_name", b'{"tool_name": ""}', "tool_name 이 없어"),
    ("비문자열 tool_name", b'{"tool_name": 12}', "tool_name 이 없어"),
    ("JSON 객체가 아님", b"[1, 2]", "JSON 객체가 아님"),
))
def test_undecidable_routing_is_warned_not_silently_skipped(
        dispatcher, guard, tmp_path, label, payload_bytes, detail_fragment):
    """판별 근거가 없으면 조용한 통과가 아니라 마커 붙은 경고다(자식은 안 띄운다)."""
    runner = _RecordingRunner()

    envelope = dispatcher.dispatch_hook("PreToolUse", payload_bytes, tmp_path,
                                        runner=runner)

    assert set(envelope) == set(guard.CODEX_WARNING_ENVELOPE_KEYS), label
    assert guard.CODEX_ADAPTER_FALLBACK_MARKER in envelope["systemMessage"], label
    assert "delegate-channel" in envelope["systemMessage"], label
    assert detail_fragment in envelope["systemMessage"], envelope["systemMessage"]
    assert envelope["suppressOutput"] is guard.CODEX_WARNING_SUPPRESS_OUTPUT
    assert "decision" not in envelope and "hookSpecificOutput" not in envelope, \
        "판정 불능이 차단으로 승격됐다"
    assert runner.calls == [], label


def test_broken_registry_pattern_is_undecidable_not_a_pass(dispatcher, guard,
                                                           tmp_path):
    """판별식 자체가 깨졌으면(정규식 오류) 안 걸린 게 아니라 판정을 못 한 것이다."""
    broken = dispatcher.CodexHookFeature(
        "fixture-broken-pattern", "PreToolUse", "[", ("{py}", "-c", "print('{}')"))
    runner = _RecordingRunner()

    envelope = dispatcher.dispatch_hook(
        "PreToolUse", json.dumps(_spawn_payload()).encode("utf-8"), tmp_path,
        features=(broken,), runner=runner)

    assert set(envelope) == set(guard.CODEX_WARNING_ENVELOPE_KEYS)
    assert guard.CODEX_ADAPTER_FALLBACK_MARKER in envelope["systemMessage"]
    assert "정규식 오류" in envelope["systemMessage"]
    assert runner.calls == []


def test_old_matcher_equivalence_is_preserved_without_new_noise(dispatcher, tmp_path):
    """옛 exact matcher 의 판정 동치 — 일치만 발화하고 불일치·경계는 **조용하다**.

    이 fix 는 경고를 더하는 방향이라, 정상 경로가 같이 시끄러워지면 그게 곧 새 결함이다."""
    feature = _delegate_feature(dispatcher)
    quiet = ("shell", "collaborationwait_agent", "Collaborationspawn_agent",
             "xcollaborationspawn_agent", "collaborationspawn_agentx")

    for tool_name in quiet:
        runner = _RecordingRunner()
        payload = {"hook_event_name": "PreToolUse", "tool_name": tool_name}
        assert dispatcher.dispatch_hook(
            "PreToolUse", json.dumps(payload).encode("utf-8"), tmp_path,
            runner=runner) == {}, tool_name
        assert runner.calls == [], tool_name
        assert not dispatcher._feature_matches(feature, payload), tool_name

    runner = _RecordingRunner()
    assert dispatcher.dispatch_hook(
        "PreToolUse", json.dumps(_spawn_payload()).encode("utf-8"), tmp_path,
        runner=runner) == {}
    assert len(runner.calls) == 1
    assert dispatcher._feature_matches(feature, _spawn_payload())


def test_route_names_the_three_outcomes(dispatcher):
    """판별 결과는 셋이다 — 걸림/안 걸림/판정 불능이 각각 다른 값이어야 구분이 산다."""
    feature = _delegate_feature(dispatcher)

    assert dispatcher._feature_route(feature, _spawn_payload()).decision == \
        dispatcher.CODEX_HOOK_ROUTE_MATCH
    assert dispatcher._feature_route(feature, {"tool_name": "shell"}).decision == \
        dispatcher.CODEX_HOOK_ROUTE_SKIP
    assert dispatcher._feature_route(feature, {}).decision == \
        dispatcher.CODEX_HOOK_ROUTE_UNDECIDABLE
    assert dispatcher._feature_route(
        feature, {}, payload_error="빈 stdin").decision == \
        dispatcher.CODEX_HOOK_ROUTE_UNDECIDABLE
    # 도구 무관 기능은 payload 를 못 읽어도 판정 불능이 아니다(판별할 값이 애초에 없다).
    agnostic = dispatcher.CodexHookFeature(
        "fixture-agnostic", "PostToolUse", None, ("{py}", "-c", "print('{}')"))
    assert dispatcher._feature_route(
        agnostic, {}, payload_error="빈 stdin").decision == \
        dispatcher.CODEX_HOOK_ROUTE_MATCH


def test_unknown_entrypoint_event_is_loud_not_silent(dispatcher, guard, tmp_path):
    """config 가 이 세대에 없는 진입점을 열면 조용히 통과하지 않는다."""
    envelope = dispatcher.dispatch_hook("Stop", b"{}", tmp_path,
                                        runner=_RecordingRunner())

    assert set(envelope) == set(guard.CODEX_WARNING_ENVELOPE_KEYS)
    assert guard.CODEX_ADAPTER_FALLBACK_MARKER in envelope["systemMessage"]


# ── 합본 규칙 ────────────────────────────────────────────────────────────────


_DENY = {
    "decision": "block",
    "reason": "fixture deny",
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": "fixture deny",
    },
    "systemMessage": "fixture deny",
    "suppressOutput": False,
}
_ADVISORY = {"systemMessage": "fixture advisory", "suppressOutput": False}


def test_no_answer_merges_to_the_measured_allow_shape(dispatcher):
    assert dispatcher.merge_hook_envelopes([]) == {}
    assert dispatcher.merge_hook_envelopes([{}, {}]) == {}


def test_single_answer_is_returned_verbatim(dispatcher):
    """응답이 하나면 값이 그대로 나간다 — 진입점 도입이 기존 판정을 바꾸지 않는다."""
    assert dispatcher.merge_hook_envelopes([{}, _DENY, {}]) == _DENY
    assert dispatcher.merge_hook_envelopes([_ADVISORY]) is _ADVISORY


def test_deny_wins_and_advisories_are_appended(dispatcher):
    """둘 이상이면 차단이 기준이고 나머지 안내가 사유에 덧붙는다(사유 유실 0)."""
    merged = dispatcher.merge_hook_envelopes([_ADVISORY, _DENY])

    assert merged["decision"] == "block"
    assert merged["systemMessage"] == "fixture advisory\nfixture deny"
    assert merged["reason"] == merged["systemMessage"]
    assert merged["hookSpecificOutput"]["permissionDecisionReason"] == \
        merged["systemMessage"]
    assert merged["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert merged["suppressOutput"] is False
    assert _DENY["systemMessage"] == "fixture deny", "입력 엔벨로프를 제자리 변형했다"


def test_multiple_advisories_merge_without_inventing_a_decision(dispatcher):
    merged = dispatcher.merge_hook_envelopes(
        [_ADVISORY, {"systemMessage": "second", "suppressOutput": True}])

    assert set(merged) == {"systemMessage", "suppressOutput"}
    assert merged["systemMessage"] == "fixture advisory\nsecond"
    assert merged["suppressOutput"] is False


# ── 고장 경계 — 어떤 실패도 도구 호출을 막지 않는다 ──────────────────────────


def test_feature_failure_degrades_to_a_marked_fallback(dispatcher, guard, tmp_path):
    """기능 자식이 못 답하면 마커 붙은 2필드 경고로 강등된다(스키마=엔진 상수)."""
    runner = _RecordingRunner(stdout=b"", returncode=3)

    envelope = dispatcher.dispatch_hook(
        "PreToolUse", json.dumps(_spawn_payload()).encode("utf-8"), tmp_path,
        runner=runner)

    assert set(envelope) == set(guard.CODEX_WARNING_ENVELOPE_KEYS)
    assert envelope["suppressOutput"] is guard.CODEX_WARNING_SUPPRESS_OUTPUT
    assert guard.CODEX_ADAPTER_FALLBACK_MARKER in envelope["systemMessage"]
    assert "delegate-channel" in envelope["systemMessage"]
    assert "fail-open" in envelope["systemMessage"]


def test_hung_feature_is_bounded_by_the_dispatch_budget(dispatcher, tmp_path):
    """멈춘 자식은 예산 안에서 끊기고 완전한 엔벨로프가 나간다."""
    feature = dispatcher.CodexHookFeature(
        "fixture-hang", "PostToolUse", None,
        ("{py}", "-c", "import time; time.sleep(30)"))

    started = time.monotonic()
    envelope = dispatcher.dispatch_hook("PostToolUse", b"{}", tmp_path,
                                        features=(feature,), budget=1.0)
    elapsed = time.monotonic() - started

    assert elapsed < dispatcher.CODEX_HOOK_DISPATCH_BUDGET_SEC
    assert "TimeoutExpired" in envelope["systemMessage"]


def test_dispatch_cli_always_returns_rc0_with_one_json_line(dispatcher, tmp_path):
    """CLI 진입점은 어떤 경로에서도 rc0 + JSON 한 줄이다(rc≠0 = 도구 차단)."""
    for event in (*dispatcher.CODEX_HOOK_ENTRYPOINT_EVENTS, "Stop"):
        completed = subprocess.run(
            [sys.executable, str(DISPATCHER_PY), "--hook-dispatch", event],
            input=b'{"tool_name":"shell"}', capture_output=True, timeout=60)
        assert completed.returncode == 0, (event, completed.stderr)
        lines = completed.stdout.decode("utf-8").splitlines()
        assert len(lines) == 1, (event, lines)
        assert isinstance(json.loads(lines[0]), dict)


def test_dispatch_output_is_ascii_safe(dispatcher, tmp_path):
    """출력은 ASCII 이스케이프다 — cp949 콘솔을 거쳐도 JSON 이 안 깨진다."""
    completed = subprocess.run(
        [sys.executable, str(DISPATCHER_PY), "--hook-dispatch", "Stop"],
        input=b"{}", capture_output=True, timeout=60)

    completed.stdout.decode("ascii")  # 비-ASCII 가 섞이면 여기서 red.
    assert "fail-open" in json.loads(completed.stdout.decode("ascii"))["systemMessage"]


# ── 출하 커맨드를 실제로 태운다 (조립 문자열 아님) ───────────────────────────


def _adopter_tree(tmp_path: Path, *, conf_lines: tuple[str, ...],
                  dispatcher_body: str | None = None) -> Path:
    """실 엔진 사본 + 디스패처 + local.conf 로 codex 채택자 형상을 만든다."""
    root = tmp_path / "adopter"
    shutil.copytree(TOOLS, root / ".project_manager" / "tools")
    (root / ".codex").mkdir(parents=True)
    if dispatcher_body is None:
        shutil.copy2(DISPATCHER_PY, root / DISPATCHER_REL)
    else:
        (root / DISPATCHER_REL).write_text(dispatcher_body, encoding="utf-8",
                                           newline="\n")
    (root / ".project_manager" / "local.conf").write_text(
        "\n".join(conf_lines) + "\n", encoding="utf-8", newline="\n")
    return root


def _run_entrypoint(event: str, root: Path, payload: dict) -> tuple[dict, float]:
    started = time.monotonic()
    completed = subprocess.run(
        ["bash", "-c", _entry_handler(event)["command"]], cwd=root,
        input=json.dumps(payload), text=True, capture_output=True,
        timeout=_entry_handler(event)["timeout"] + 5, check=True)
    return json.loads(completed.stdout), time.monotonic() - started


_CROSS_CONF = ("delegate_enabled=true", "delegate.developer.harness=opencode",
               "delegate.developer.model=qwen3-coder")


@pytest.mark.skipif(not posix_bash_supported(),
                    reason="POSIX bash wrapper 실행 환경이 아님")
def test_shipped_entrypoint_runs_the_registered_guard_for_a_spawn_payload(
        guard, tmp_path):
    """출하 PreToolUse 진입점을 실제 셸로 태우면 스폰 payload 에서 위임 가드가 발화한다."""
    root = _adopter_tree(tmp_path, conf_lines=_CROSS_CONF)

    result, elapsed = _run_entrypoint("PreToolUse", root, _spawn_payload())

    assert elapsed < _entry_handler("PreToolUse")["timeout"]
    assert set(result) == set(guard.CODEX_DENY_ENVELOPE_KEYS)
    assert "pm_delegate.py" in result["reason"]
    rendered = json.dumps(result, ensure_ascii=False)
    assert guard.CODEX_ADAPTER_FALLBACK_MARKER not in rendered
    assert guard.CODEX_SUPERVISOR_FALLBACK_MARKER not in rendered


@pytest.mark.skipif(not posix_bash_supported(),
                    reason="POSIX bash wrapper 실행 환경이 아님")
@pytest.mark.parametrize("event,payload", (
    ("PreToolUse", {"hook_event_name": "PreToolUse", "tool_name": "shell"}),
    ("PreToolUse", {"hook_event_name": "PreToolUse",
                    "tool_name": "collaborationwait_agent"}),
    ("UserPromptSubmit", {"hook_event_name": "UserPromptSubmit"}),
    ("PostToolUse", {"hook_event_name": "PostToolUse", "tool_name": "shell"}),
))
def test_shipped_entrypoint_passes_unmatched_payloads_through(
        tmp_path, event, payload):
    """진입점이 넓어져도 걸리는 기능이 없으면 통과다 — 정상 사용이 막히지 않는다."""
    root = _adopter_tree(tmp_path, conf_lines=_CROSS_CONF)

    result, _elapsed = _run_entrypoint(event, root, payload)

    assert result == {}


# ── 판정 불능 회귀를 **출하 커맨드**로 (POSIX·Windows 양쪽) ──────────────────
# 옛 exact matcher 경로에 같은 입력을 넣으면 rc0 경고 엔벨로프가 나왔다(실측). 진입점 뒤로
# 판별이 옮겨 온 뒤에도 그 경고가 나오는지를 **실 출하 커맨드 실행**으로 고정한다 — 디스패처
# 단위 판정만으로는 래퍼가 그 출력을 삼키는 형상을 못 본다.

_SHIPPED_TOOL_PATTERN = 'tool_pattern="^collaborationspawn_agent$"'
_BROKEN_TOOL_PATTERN = 'tool_pattern="["'
_POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


def dispatcher_source_with_broken_tool_pattern() -> str:
    """출하 디스패처의 registry 판별식을 **깨진 정규식**으로 바꾼 사본."""
    source = DISPATCHER_PY.read_text(encoding="utf-8")
    assert source.count(_SHIPPED_TOOL_PATTERN) == 1, "registry 판별식 표기가 바뀌었다"
    return source.replace(_SHIPPED_TOOL_PATTERN, _BROKEN_TOOL_PATTERN)


def _windows_dispatcher_argv(event: str) -> list[str]:
    """출하 `commandWindows` 가 실제로 부르는 디스패처 호출을 **값에서** 뽑는다.

    PowerShell 이 없는 호스트에서도 그 커맨드가 넘기는 argv 자체는 그대로 실행할 수 있다.
    인터프리터 해소 래퍼(프로브 루프·폴백 분기)는 `test_codex_ctx_guard` 의 정적 파서가 따로
    고정하므로, 여기서 보는 것은 판정 불능 경고가 그 호출로 실제 나오는가다."""
    windows = _entry_handler(event)["commandWindows"]
    invocations = [segment for segment in powershell_native_arguments(windows)
                   if f"--hook-dispatch {event}" in segment]
    assert len(invocations) == 1, (event, invocations)
    tokens = shlex.split(invocations[0])
    assert tokens[:2] == ["&", "$py"], tokens
    return [sys.executable, *tokens[2:]]


def _run_shipped_command(event: str, root: Path, payload_text: str, *,
                         windows: bool) -> subprocess.CompletedProcess:
    """출하 커맨드를 그대로 실행한다 — 판정 근거는 조립 문자열이 아니라 이 rc/stdout/stderr."""
    if not windows:
        argv = ["bash", "-c", _entry_handler(event)["command"]]
    elif _POWERSHELL is not None:
        argv = [_POWERSHELL, "-NoProfile", "-NonInteractive", "-Command",
                _entry_handler(event)["commandWindows"]]
    else:
        argv = _windows_dispatcher_argv(event)
    return subprocess.run(argv, cwd=root, input=payload_text, text=True,
                          capture_output=True,
                          timeout=_entry_handler(event)["timeout"] + 5)


# (라벨, stdin 원문, 깨진 판별식 사본을 심을지, 경고 사유에 있어야 할 조각)
_UNDECIDABLE_SHIPPED_INPUTS = (
    ("빈 stdin", "", False, "빈 stdin"),
    ("파손 JSON", "{not json", False, "JSONDecodeError"),
    ("라우팅 필드 부재", "{}", False, "tool_name 이 없어"),
    ("잘못된 registry tool_pattern", None, True, "정규식 오류"),
)


@pytest.mark.skipif(not posix_bash_supported(),
                    reason="POSIX bash wrapper 실행 환경이 아님")
@pytest.mark.parametrize("label,payload_text,broken_pattern,detail_fragment",
                         _UNDECIDABLE_SHIPPED_INPUTS)
def test_shipped_posix_entrypoint_warns_when_it_cannot_decide(
        guard, tmp_path, label, payload_text, broken_pattern, detail_fragment):
    """출하 POSIX 커맨드가 판정 불능 입력에 rc0 **경고**를 낸다(옛 경로와 같은 의미)."""
    root = _adopter_tree(
        tmp_path, conf_lines=_CROSS_CONF,
        dispatcher_body=(dispatcher_source_with_broken_tool_pattern()
                         if broken_pattern else None))
    stdin_text = (json.dumps(_spawn_payload()) if payload_text is None
                  else payload_text)

    completed = _run_shipped_command("PreToolUse", root, stdin_text, windows=False)

    assert completed.returncode == 0, (label, completed.stderr)
    envelope = json.loads(completed.stdout)
    assert set(envelope) == set(guard.CODEX_WARNING_ENVELOPE_KEYS), label
    assert guard.CODEX_ADAPTER_FALLBACK_MARKER in envelope["systemMessage"], label
    assert detail_fragment in envelope["systemMessage"], envelope["systemMessage"]
    assert "decision" not in envelope, "판정 불능이 차단으로 승격됐다"


@pytest.mark.parametrize("label,payload_text,broken_pattern,detail_fragment",
                         _UNDECIDABLE_SHIPPED_INPUTS)
def test_shipped_windows_entrypoint_warns_when_it_cannot_decide(
        guard, tmp_path, label, payload_text, broken_pattern, detail_fragment):
    """Windows 쪽 출하 커맨드도 같은 입력에 같은 경고를 낸다(플랫폼 간 판정 비대칭 0)."""
    root = _adopter_tree(
        tmp_path, conf_lines=_CROSS_CONF,
        dispatcher_body=(dispatcher_source_with_broken_tool_pattern()
                         if broken_pattern else None))
    stdin_text = (json.dumps(_spawn_payload()) if payload_text is None
                  else payload_text)

    completed = _run_shipped_command("PreToolUse", root, stdin_text, windows=True)

    assert completed.returncode == 0, (label, completed.stderr)
    envelope = json.loads(completed.stdout)
    assert set(envelope) == set(guard.CODEX_WARNING_ENVELOPE_KEYS), label
    assert guard.CODEX_ADAPTER_FALLBACK_MARKER in envelope["systemMessage"], label
    assert detail_fragment in envelope["systemMessage"], envelope["systemMessage"]
    assert "decision" not in envelope, "판정 불능이 차단으로 승격됐다"


@pytest.mark.skipif(not posix_bash_supported(),
                    reason="POSIX bash wrapper 실행 환경이 아님")
@pytest.mark.parametrize("windows", (False, True))
def test_shipped_entrypoint_stays_quiet_on_normal_traffic(guard, tmp_path, windows):
    """정상 매칭·정상 미매칭은 새로 시끄러워지지 않는다 — 경고를 더한 fix 의 역방향 확인."""
    root = _adopter_tree(tmp_path, conf_lines=_CROSS_CONF)

    for payload in ({"hook_event_name": "PreToolUse", "tool_name": "shell"},
                    {"hook_event_name": "PreToolUse",
                     "tool_name": "collaborationwait_agent"}):
        completed = _run_shipped_command("PreToolUse", root, json.dumps(payload),
                                         windows=windows)
        assert completed.returncode == 0, completed.stderr
        assert json.loads(completed.stdout) == {}, payload

    matched = _run_shipped_command("PreToolUse", root, json.dumps(_spawn_payload()),
                                   windows=windows)
    assert matched.returncode == 0, matched.stderr
    envelope = json.loads(matched.stdout)
    assert set(envelope) == set(guard.CODEX_DENY_ENVELOPE_KEYS)
    assert guard.CODEX_ADAPTER_FALLBACK_MARKER not in json.dumps(envelope,
                                                                ensure_ascii=False)


@pytest.mark.skipif(not posix_bash_supported(),
                    reason="POSIX bash wrapper 실행 환경이 아님")
def test_stale_dispatcher_generation_falls_back_instead_of_locking_out(
        guard, tmp_path):
    """신 config + 구 디스패처(플래그 미지원)는 **락아웃이 아니라** 폴백이다.

    claude v1.7.0 락아웃은 훅이 rc2 를 그대로 흘려 도구 호출이 통째로 막힌 사건이다. codex
    진입점은 rc 를 붙잡아 완전한 엔벨로프로 강등하므로 같은 세대 혼합에서도 통과한다 —
    대신 그 사실이 마커로 남고 엔진 역방향 가드가 지목한다."""
    root = _adopter_tree(tmp_path, conf_lines=_CROSS_CONF,
                         dispatcher_body="import sys\nraise SystemExit(2)\n")

    result, elapsed = _run_entrypoint("PreToolUse", root, _spawn_payload())

    assert elapsed < 5
    assert set(result) == set(guard.CODEX_WARNING_ENVELOPE_KEYS)
    assert guard.CODEX_ADAPTER_FALLBACK_MARKER in result["systemMessage"]


@pytest.mark.skipif(not posix_bash_supported(),
                    reason="POSIX bash wrapper 실행 환경이 아님")
def test_adopter_without_the_dispatcher_is_warned_not_blocked(guard, tmp_path):
    """디스패처가 아예 없는 트리(부분 설치)도 rc0 + 완전한 엔벨로프로 끝난다."""
    root = tmp_path / "bare"
    root.mkdir()

    result, elapsed = _run_entrypoint("PostToolUse", root,
                                      {"hook_event_name": "PostToolUse"})

    assert elapsed < 5
    assert set(result) == set(guard.CODEX_WARNING_ENVELOPE_KEYS)
    assert guard.CODEX_ADAPTER_FALLBACK_MARKER in result["systemMessage"]


# 코드로만 얹는 가드 한 건 — "이후 기능은 registry 한 줄" 이라는 이 배선의 주장 그대로다.
FIXTURE_FEATURE_SNIPPET = (
    "\n# 픽스처: 가드 기능 하나를 코드로만 추가한다(채택자 config 무변경).\n"
    "CODEX_HOOK_FEATURES = CODEX_HOOK_FEATURES + (CodexHookFeature(\n"
    '    "fixture-guard", "PostToolUse", None,\n'
    "    (\"{py}\", \"-c\", "
    "'import json,sys; sys.stdout.write(json.dumps("
    '{"systemMessage": "FIXTURE-GUARD", "suppressOutput": False}))\'),\n'
    "),)\n\n"
)
FIXTURE_FEATURE_ENVELOPE = {"systemMessage": "FIXTURE-GUARD", "suppressOutput": False}
_MAIN_GUARD = 'if __name__ == "__main__":'


def dispatcher_source_with_fixture_feature() -> str:
    """출하 디스패처 + 픽스처 기능 — `__main__` 가드 **앞**에 넣는다.

    뒤에 붙이면 `raise SystemExit(main())` 이 먼저 돌아 등록문이 실행되지 않는다(그 형상은
    기능이 안 붙은 것과 구별되지 않는 조용한 false-green 이다)."""
    source = DISPATCHER_PY.read_text(encoding="utf-8")
    assert source.count(_MAIN_GUARD) == 1, "디스패처 진입 가드 형상이 바뀌었다"
    return source.replace(_MAIN_GUARD, FIXTURE_FEATURE_SNIPPET + _MAIN_GUARD)


@pytest.mark.skipif(not posix_bash_supported(),
                    reason="POSIX bash wrapper 실행 환경이 아님")
def test_a_new_feature_needs_no_config_change(tmp_path):
    """기능을 코드에만 더해도 **같은 출하 config** 로 발화한다 — 이 티켓이 닫는 마찰 자체."""
    root = _adopter_tree(
        tmp_path, conf_lines=("delegate_enabled=false",),
        dispatcher_body=dispatcher_source_with_fixture_feature())

    result, _elapsed = _run_entrypoint("PostToolUse", root,
                                       {"hook_event_name": "PostToolUse",
                                        "tool_name": "shell"})

    assert result == FIXTURE_FEATURE_ENVELOPE
    # 같은 트리에서 진입점을 안 거친 이벤트는 그대로다(발화 스코프가 안 넓어졌다).
    assert _run_entrypoint("UserPromptSubmit", root,
                           {"hook_event_name": "UserPromptSubmit"})[0] == {}
