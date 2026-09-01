"""어댑터 훅 세트 세대 정합 (T-0606) — 부분-세대 락아웃 폐쇄.

claude 훅 세트는 갱신 주체가 둘로 갈린다: `settings.json` 은 채택자 소유라 pm_update 가 못 덮고,
래퍼(`ctx_stop_hook.sh`)·드라이버(`pm_orch_claude.py`)는 manifest 등재라 pm_update 가 덮는다.
그래서 "신 settings + 구 드라이버" 세대 혼합 창이 구조적으로 열리고, 그 조합에서 구 드라이버가
미지원 플래그를 argparse rc2 로 거부하면 PreToolUse rc2 = 도구 전면 차단이다(v1.7.0 흡수 실측·
fresh 채택자는 탈출 불능). 이 절이 고정하는 성질:

  - 위험 방향(config 가 앞선 세대)만 검출하고 반대 방향(구 config + 신 드라이버)은 조용하다.
  - 처방이 두 갈래로 갈린다 — dest 엔진 파일이 뒤처졌으면 pm-update, config 가 이 엔진 세대보다
     앞섰으면 `sync-adapter-config --accept`.
  - 수용은 엔진 파일 선행을 기계가 강제한다(순서 위반 = 파일 미변경 거부).
  - 동기는 훅 세트 파일을 원자 교체한다(실행 중 하네스의 torn read 창 제거).

픽스처는 합성 트리다 — 실 template 본문에 의존하면 상류 문구가 바뀔 때마다 무관하게 깨진다.
다만 **출하 template 자기정합**만은 실 트리를 본다(그게 이 클래스의 재발 가드다).
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from _repo_owned_inventory import TRACKED_ONLY, repo_owned_paths

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

_SETTINGS_REL = ".claude/settings.json"
_WRAPPER_REL = ".claude/ctx_stop_hook.sh"
_DRIVER_REL = ".claude/pm_orch_claude.py"
_GIT_ANCHOR_FLAG = "--git-anchor-hook"

# 세대 마커는 **플래그 리터럴 보유 여부** 하나다(엔진 판정과 같은 기준). 본문은 최소 형태로 둔다.
_WRAPPER_NEW = (
    '#!/usr/bin/env bash\n'
    'target="$hook_dir/ctx_stop_hook.py"\n'
    f'if [ "${{1-}}" = "{_GIT_ANCHOR_FLAG}" ]; then target="$hook_dir/pm_orch_claude.py"; fi\n'
    'exec "$py" "$target" "$@"\n'
)
_WRAPPER_OLD = (
    '#!/usr/bin/env bash\n'
    'exec "$py" "$hook_dir/ctx_stop_hook.py" "$@"\n'
)
_DRIVER_NEW = (
    'import argparse\n'
    f'parser.add_argument("{_GIT_ANCHOR_FLAG}", action="store_true")\n'
)
_DRIVER_OLD = (
    'import argparse\n'
    'parser.add_argument("--task", default=None)\n'
)


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pm_import():
    return _load("pm_import", "pm_import.py")


@pytest.fixture(scope="module")
def pm_update():
    return _load("pm_update", "pm_update.py")


@pytest.fixture(scope="module")
def pm_config():
    return _load("pm_config", "pm_config.py")


def _settings(*, git_anchor: bool, wrapper_rel: str = _WRAPPER_REL,
              flag: str = _GIT_ANCHOR_FLAG) -> str:
    """claude settings.json — PreToolUse 훅 2건(공통 넛지 + 선택적 git-anchor Bash 매처)."""
    groups = [{
        "matcher": "*",
        "hooks": [{"type": "command",
                   "command": "${CLAUDE_PROJECT_DIR}/" + wrapper_rel,
                   "timeout": 15}],
    }]
    if git_anchor:
        groups.append({
            "matcher": "Bash",
            "hooks": [{"type": "command",
                       "command": ("${CLAUDE_PROJECT_DIR}/" + wrapper_rel + f" {flag}"),
                       "timeout": 15}],
        })
    return json.dumps({"hooks": {"PreToolUse": groups}},
                      ensure_ascii=False, indent=2) + "\n"


_UPSTREAM_PM_IMPORT_REL = ".project_manager/tools/pm_import.py"


def _plant_upstream_pm_import(source: Path, *, extra_tail: str = "") -> Path:
    """상류 트리에 pm_import 사본을 놓는다 — 게이트가 **상류 세대 선언**을 읽는 전제.

    실 파일을 그대로 복사해야 "상류 세대 == 현행 선언" 이라는 기본 형상이 되고(게이트 통과),
    `extra_tail` 로 선언을 덧붙이면 **상류만 아는 세대**가 된다(설치본은 모르는 묶음/경로)."""
    target = source / _UPSTREAM_PM_IMPORT_REL
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        (TOOLS / "pm_import.py").read_text(encoding="utf-8") + extra_tail,
        encoding="utf-8")
    return target


def _make_case(tmp_path: Path, *, dest_settings: str, dest_wrapper: str | None,
               dest_driver: str | None, template_settings: str | None = None,
               template_wrapper: str = _WRAPPER_NEW,
               template_driver: str = _DRIVER_NEW,
               ledger: dict | None = None,
               upstream_tail: str | None = "") -> tuple[Path, Path]:
    """(dest, source) — claude 채택자 + 합성 프레임워크.

    `dest_wrapper`/`dest_driver` 를 None 으로 주면 그 파일이 **설치되지 않은** 형상이다.
    `ledger` 는 어댑터 원장(설치 시점 해시) — `edited` 판정을 만들려면 dest 와 다른 해시를 준다.
    `upstream_tail` 은 상류 pm_import 사본에 덧붙일 선언(None 이면 사본 자체를 놓지 않는다 —
    상류 세대 미해소 형상)."""
    source = tmp_path / "framework"
    template = source / "templates" / "claude_code"
    (template / ".claude").mkdir(parents=True)
    (template / _SETTINGS_REL).write_text(
        template_settings if template_settings is not None else dest_settings,
        encoding="utf-8")
    (template / _WRAPPER_REL).write_text(template_wrapper, encoding="utf-8")
    (template / _DRIVER_REL).write_text(template_driver, encoding="utf-8")
    if upstream_tail is not None:
        _plant_upstream_pm_import(source, extra_tail=upstream_tail)

    dest = tmp_path / "adopter"
    (dest / ".claude").mkdir(parents=True)
    (dest / ".project_manager").mkdir(parents=True)
    (dest / "CLAUDE.md").write_text("# adopter 진입 문서\n", encoding="utf-8")
    (dest / _SETTINGS_REL).write_text(dest_settings, encoding="utf-8")
    if dest_wrapper is not None:
        (dest / _WRAPPER_REL).write_text(dest_wrapper, encoding="utf-8")
    if dest_driver is not None:
        (dest / _DRIVER_REL).write_text(dest_driver, encoding="utf-8")
    (dest / ".project_manager" / "install.json").write_text(
        '{"schema": 1, "harnesses": ["claude"]}\n', encoding="utf-8")
    if ledger is not None:
        (dest / ".project_manager" / "adapter_baseline.json").write_text(
            json.dumps({"schema": 1, "files": ledger}, ensure_ascii=False) + "\n",
            encoding="utf-8")
    return dest, source


def _ledger_entry(text: str) -> dict:
    """원장 항목 — 설치가 그 내용으로 레이다운했다는 기록(해시만 판정에 쓰인다)."""
    return {"sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "recorded_at": "2026-01-01T00:00:00+09:00", "template_rev": "deadbeef"}


def _write_source_manifest(source: Path, entries: list[str]) -> None:
    """source 트리에 manifest 를 놓고 git 추적으로 만든다(전파 계획이 tracked-only 열거)."""
    manifest = source / ".project_manager" / "engine.manifest"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("\n".join(entries) + "\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(source), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(source), "add", "-f", "-A"], check=True)


# ── 판정: 위험 방향만 검출한다 ──────────────────────────────────────────────


def test_new_config_with_old_driver_is_detected_as_lockout(pm_import, tmp_path):
    """신 settings + 구 드라이버 = 락아웃 조합 → 미지원 플래그로 검출된다.

    이게 v1.7.0 흡수에서 실제로 터진 조합이다. 검출 못 하면 채택자는 Bash 전면 차단 상태로
    pm-update 를 green 으로 받는다(성공 보고 + 잠긴 하네스)."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=True),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_OLD)

    findings = pm_import.judge_adapter_hook_sets(dest, source)

    assert len(findings) == 1, findings
    finding = findings[0]
    assert finding.kind == pm_import.HOOK_SET_UNSUPPORTED_FLAG
    assert finding.subject == _GIT_ANCHOR_FLAG
    assert finding.unmet_paths == (_DRIVER_REL,), \
        "래퍼는 신세대인데 드라이버까지 미충족으로 뭉뚱그렸다"
    assert finding.remedy == pm_import.HOOK_SET_REMEDY_ENGINE_STALE


def test_old_config_with_new_driver_is_not_flagged(pm_import, tmp_path):
    """구 settings + 신 드라이버는 훅 미발화라 무해 — 검사가 조용해야 한다(거짓 red 금지)."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=False),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_NEW)

    assert pm_import.judge_adapter_hook_sets(dest, source) == []


def test_same_generation_set_is_silent(pm_import, tmp_path):
    """양쪽 모두 신세대면 판정이 비어 있다."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=True),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_NEW)

    assert pm_import.judge_adapter_hook_sets(dest, source) == []


def test_old_wrapper_alone_is_detected(pm_import, tmp_path):
    """dispatch 하는 래퍼가 구세대면 그 파일이 미충족으로 잡힌다(체인 전체가 판정 대상)."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=True),
        dest_wrapper=_WRAPPER_OLD, dest_driver=_DRIVER_NEW)

    findings = pm_import.judge_adapter_hook_sets(dest, source)

    assert [f.unmet_paths for f in findings] == [(_WRAPPER_REL,)], findings


def test_missing_hook_script_is_detected(pm_import, tmp_path):
    """config 가 부르는 래퍼가 아예 없으면 그것도 같은 락아웃 클래스다(훅이 매번 실패)."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=True),
        dest_wrapper=None, dest_driver=_DRIVER_NEW)

    findings = pm_import.judge_adapter_hook_sets(dest, source)

    assert [f.kind for f in findings] == [pm_import.HOOK_SET_MISSING_SCRIPT], findings
    assert findings[0].unmet_paths == (_WRAPPER_REL,)


def test_adopter_own_hook_script_is_out_of_judgment_scope(pm_import, tmp_path):
    """채택자 자작 훅은 엔진 소관 밖 — 부재해도 처방하지 않는다(거짓 red 금지)."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=False,
                                          wrapper_rel=".claude/my_own_hook.sh"),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_NEW)

    assert pm_import.judge_adapter_hook_sets(dest, source) == []


def test_config_ahead_of_engine_prescribes_accept_not_pm_update(pm_import, tmp_path):
    """상류 template 조차 그 플래그를 못 주면 config 가 앞선 것 → 처방이 수용 채널로 갈린다.

    처방이 갈리지 않으면 채택자는 절대 끝나지 않는 pm-update 를 반복한다(엔진엔 줄 게 없다)."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=True),
        dest_wrapper=_WRAPPER_OLD, dest_driver=_DRIVER_OLD,
        template_settings=_settings(git_anchor=False),
        template_wrapper=_WRAPPER_OLD, template_driver=_DRIVER_OLD)

    findings = pm_import.judge_adapter_hook_sets(dest, source)

    assert [f.remedy for f in findings] == [pm_import.HOOK_SET_REMEDY_CONFIG_AHEAD], findings
    lines = " ".join(pm_import.hook_set_remedy_lines(findings[0]))
    assert "sync-adapter-config --accept" in lines and _SETTINGS_REL in lines


def test_engine_stale_prescribes_pm_update_with_the_unmet_files(pm_import, tmp_path):
    """dest 엔진 파일이 뒤처진 경우의 처방은 pm-update + 미충족 파일 실값이다."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=True),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_OLD)

    lines = " ".join(pm_import.hook_set_remedy_lines(
        pm_import.judge_adapter_hook_sets(dest, source)[0]))

    assert "pm-update" in lines and _DRIVER_REL in lines


def test_missing_source_root_falls_back_to_both_prescriptions(pm_import, tmp_path):
    """비교 기준이 없으면 처방을 단정하지 않는다(엔진 선행을 먼저 권하되 수용 채널도 남긴다)."""
    dest, _source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=True),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_OLD)

    findings = pm_import.judge_adapter_hook_sets(dest, None)

    assert [f.remedy for f in findings] == [pm_import.HOOK_SET_REMEDY_UNKNOWN]
    lines = pm_import.hook_set_remedy_lines(findings[0])
    assert "pm-update" in lines[0] and "sync-adapter-config --accept" in lines[1]


def test_broken_config_json_is_not_a_finding(pm_import, tmp_path):
    """파손된 채택자 config 는 판정 불가일 뿐 훅 세대 red 가 아니다(엉뚱한 처방 금지)."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=True),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_OLD)
    (dest / _SETTINGS_REL).write_text("{ 깨진 json", encoding="utf-8")

    assert pm_import.judge_adapter_hook_sets(dest, source) == []


# ── 선언 정합 (하드코딩 claude 전용 방지) ─────────────────────────────────────


def test_every_registered_harness_declares_a_hook_set(pm_import):
    """등록 하네스 전수가 훅 세트 선언을 갖는다 — 네 번째 하네스가 조용히 미검사로 못 떨어진다."""
    assert set(pm_import.ADAPTER_HOOK_SET) == set(pm_import.REGISTERED_HARNESSES)


def test_flag_support_targets_are_declared_live_files(pm_import):
    """플래그가 요구하는 파일은 전부 그 하네스의 훅 세트 파일이어야 한다(선언 두 벌 방지).

    밖의 경로를 요구하면 "검사는 하는데 동기가 원자 write 하지 않는" 파일이 생긴다."""
    for harness, spec in pm_import.ADAPTER_HOOK_SET.items():
        for flag, required in spec.flag_support.items():
            for relpath in required:
                assert pm_import._is_hook_set_file(relpath, spec), \
                    f"{harness}:{flag} 가 훅 세트 밖 파일을 요구한다 ({relpath})"
                assert pm_import.is_live_hook_set_path(relpath), \
                    f"{harness}:{flag} 요구 파일이 원자 write 대상이 아니다 ({relpath})"
        for group in spec.coupled_groups:
            assert len(group) > 1, f"{harness}: 원소 1개짜리 결합 묶음은 의미가 없다 ({group})"
            for relpath in group:
                assert relpath in spec.live_files, \
                    f"{harness}: 결합 묶음이 선언 밖 경로를 담았다 ({relpath})"


def test_flag_support_chain_moves_as_one_coupled_group(pm_import):
    """플래그 체인은 반드시 한 결합 묶음 안에 있다 — 부분 전파로 갈릴 수 있는 축이 곧 그 체인이다."""
    for harness, spec in pm_import.ADAPTER_HOOK_SET.items():
        for flag, required in spec.flag_support.items():
            assert any(set(required) <= set(group) for group in spec.coupled_groups), \
                f"{harness}:{flag} 체인이 어느 결합 묶음에도 통째로 들어 있지 않다 ({required})"


def test_shipped_claude_template_hook_set_is_self_consistent(pm_import):
    """출하 template 자체가 세대 정합이다 — settings.json 에 플래그를 더하고 드라이버를 안 고치면 red.

    합성 픽스처만으로는 실제 출하물의 재발을 못 막는다(이 클래스의 도입 커밋 T-0587 이 정확히
    settings.json + 래퍼 + 드라이버 동시 변경이었다)."""
    template_root = REPO / "templates" / "claude_code"
    spec = pm_import.ADAPTER_HOOK_SET["claude"]
    document = json.loads(
        (template_root / spec.config_relpath).read_text(encoding="utf-8"))

    unmet = pm_import._hook_set_demands(document, spec, template_root)

    assert unmet == {}, f"출하 template 의 훅 세트가 자기 config 를 감당하지 못한다: {unmet}"


def test_shipped_claude_template_flag_demand_is_not_vacuous(pm_import, tmp_path):
    """출하 settings.json 이 **실제로** 세대 플래그를 요구한다 — 요구 0 이면 자기정합 단언이 공허하다.

    `unmet == {}` 는 요구가 하나도 없을 때도 green 이다. 배선이 진입점 뒤로 접히면서 config 가
    플래그를 안 넘기게 되면 그 순간 세대 결합 가드(v1.7.0 락아웃 클래스)가 조용히 무력해진다 —
    구세대 드라이버를 심어 요구가 표면에 뜨는지 본다."""
    template_root = REPO / "templates" / "claude_code"
    spec = pm_import.ADAPTER_HOOK_SET["claude"]
    document = json.loads(
        (template_root / spec.config_relpath).read_text(encoding="utf-8"))
    stale = tmp_path / "stale"
    (stale / ".claude").mkdir(parents=True)
    for relpath in spec.live_files:
        (stale / relpath).write_text("# 구세대 사본\n", encoding="utf-8")

    unmet = pm_import._hook_set_demands(document, spec, stale)

    assert (pm_import.HOOK_SET_UNSUPPORTED_FLAG, _GIT_ANCHOR_FLAG) in unmet, \
        f"출하 settings.json 이 {_GIT_ANCHOR_FLAG} 를 요구하지 않는다(세대 결합 가드 공허): {unmet}"


def test_codex_entrypoint_migration_left_claude_wiring_untouched(pm_import):
    """codex 진입점 마이그레이션(T-0777)이 claude 배선을 건드리지 않았다.

    claude 는 `PreToolUse matcher=*` 진입점을 이미 갖고 있어 흡수 이득이 없고, 기존 `Bash`
    블록을 지우면 세대 결합 가드가 공허해지며 발화 스코프가 전 도구로 넓어진다. 그래서 이
    티켓의 비목표였고, 그 사실을 값으로 고정한다."""
    document = json.loads(
        (REPO / "templates" / "claude_code" / _SETTINGS_REL).read_text(encoding="utf-8"))
    groups = document["hooks"]["PreToolUse"]

    assert [group["matcher"] for group in groups] == ["*", "Bash", "Agent"]
    commands = [handler["command"] for group in groups
                for handler in group["hooks"]]
    assert any(command.endswith(_GIT_ANCHOR_FLAG) for command in commands)
    # codex 디스패처 표기가 claude config 로 새면 배선 진실이 두 벌이 된다.
    for command in commands:
        assert "--hook-dispatch" not in command, command
        assert "pm_orch_codex" not in command, command


# ── 역방향 축: 이 엔진 세대가 기대하는 진입점이 config 에 있나 (T-0777) ──────────
# 위 절들은 **config → 엔진** 방향(config 가 요구하는 것을 설치본이 감당하나)만 본다. 진입점이
# 아예 없으면 요구가 0 이라 그 방향으로는 영원히 green 이고, 그 상태에서 등록된 가드는 한 번도
# 발화하지 않는다. 이 절이 반대 방향을 고정한다 — 판정은 advisory 이지 차단이 아니다(config 는
# 채택자 소유).

_CODEX_HOOKS_REL = ".codex/hooks.json"
_CODEX_DISPATCHER_REL = ".codex/pm_orch_codex.py"
_CODEX_DISPATCH_FLAG = "--hook-dispatch"
# 진입점 이벤트 전수는 **엔진 선언에서 파생**한다 — 여기에 목록을 손으로 두 번째로 두면
#   T-0806 처럼 집합이 늘 때 픽스처만 옛 세대로 남아 조용히 다른 형상을 검사한다.
_CODEX_ENTRYPOINT_EVENTS = tuple(
    entrypoint.event for entrypoint in
    _load("pm_import_events", "pm_import.py").ADAPTER_HOOK_SET["codex"].entrypoints)
# 디스패처 세대 마커는 config 가 넘기는 **호출 값**(플래그 + 이벤트 이름) 보유다 — 엔진 판정과
#   같은 기준이다. 플래그만 알고 그 이벤트는 모르는 세대가 실재하고, 그 조합에서 훅은 매 발화마다
#   폴백으로 빠진다.
_DISPATCHER_NEW = (f'CODEX_HOOK_DISPATCH_FLAG = "{_CODEX_DISPATCH_FLAG}"\n'
                   f'CODEX_HOOK_ENTRYPOINT_EVENTS = {_CODEX_ENTRYPOINT_EVENTS!r}\n')
# 진입점 집합이 늘기 전 세대 — 플래그는 아는데 새 이벤트는 모른다.
_DISPATCHER_PREVIOUS_EVENT_SET = (
    f'CODEX_HOOK_DISPATCH_FLAG = "{_CODEX_DISPATCH_FLAG}"\n'
    f'CODEX_HOOK_ENTRYPOINT_EVENTS = {_CODEX_ENTRYPOINT_EVENTS[:-1]!r}\n')
_DISPATCHER_OLD = 'def main(argv=None):\n    return 0\n'


def _codex_hooks(events=_CODEX_ENTRYPOINT_EVENTS, *, matcher: str | None = None) -> str:
    """codex hooks.json — PreToolUse만 안전 도구 exact, 나머지는 범용인 출하 형상."""
    return json.dumps({"hooks": {
        event: [{
            "matcher": (matcher if matcher is not None else
                        ("^(Bash|collaborationspawn_agent)$"
                         if event == "PreToolUse" else ".*")),
            "hooks": [{"type": "command", "timeout": 15,
                       "command": f'"$py" {_CODEX_DISPATCHER_REL} '
                                  f'{_CODEX_DISPATCH_FLAG} {event}'}],
        }] for event in events
    }}, ensure_ascii=False, indent=2) + "\n"


def _make_codex_case(tmp_path: Path, *, hooks: str,
                     dispatcher: str | None = _DISPATCHER_NEW) -> Path:
    """codex 채택자 dest — hooks.json + (선택) 디스패처."""
    dest = tmp_path / "codex-adopter"
    (dest / ".codex").mkdir(parents=True)
    (dest / ".project_manager").mkdir(parents=True)
    (dest / "AGENTS.md").write_text("# adopter 진입 문서\n", encoding="utf-8")
    (dest / _CODEX_HOOKS_REL).write_text(hooks, encoding="utf-8")
    if dispatcher is not None:
        (dest / _CODEX_DISPATCHER_REL).write_text(dispatcher, encoding="utf-8")
    (dest / ".project_manager" / "install.json").write_text(
        '{"schema": 1, "harnesses": ["codex"]}\n', encoding="utf-8")
    return dest


def test_entrypoint_declaration_targets_are_declared_live_files(pm_import):
    """진입점이 부르는 디스패처는 전부 그 하네스의 훅 세트 파일이어야 한다(선언 두 벌 방지).

    밖의 경로를 선언하면 "판정은 하는데 동기가 원자 write 하지 않는" 파일이 생긴다 —
    `flag_support` 축과 같은 근거다."""
    seen_any = False
    for harness, spec in pm_import.ADAPTER_HOOK_SET.items():
        events = [entrypoint.event for entrypoint in spec.entrypoints]
        assert len(events) == len(set(events)), \
            f"{harness}: 한 이벤트에 진입점을 두 번 선언했다 ({events})"
        for entrypoint in spec.entrypoints:
            seen_any = True
            assert spec.config_relpath, \
                f"{harness}: config 가 없는 하네스에 진입점을 선언했다({entrypoint.event})"
            assert pm_import._is_hook_set_file(entrypoint.dispatcher, spec), \
                f"{harness}:{entrypoint.event} 가 훅 세트 밖 디스패처를 부른다"
            assert pm_import.is_live_hook_set_path(entrypoint.dispatcher), \
                f"{harness}:{entrypoint.event} 디스패처가 원자 write 대상이 아니다"
    assert seen_any, "진입점 선언이 하나도 없다(공허 가드)"


@pytest.mark.parametrize("harness,template_dir",
                         (("claude", "claude_code"), ("codex", "codex"),
                          ("opencode", "opencode")))
def test_shipped_template_satisfies_its_entrypoint_declaration(
        pm_import, harness, template_dir):
    """출하 template 이 자기 진입점 선언을 만족한다 — 배선을 지우면 그 자리에서 red.

    합성 픽스처만으로는 실 출하물의 재발을 못 막는다(claude 축의 자기정합 단언과 같은 근거)."""
    findings = pm_import.judge_adapter_hook_entrypoints(
        REPO / "templates" / template_dir, REPO, [harness])

    assert findings == [], [finding.detail for finding in findings]


def test_missing_entrypoint_is_reported_with_the_event_named(pm_import, tmp_path):
    """진입점을 하나 빼면 그 이벤트를 **지목**한다(민감도) — 나머지는 조용하다."""
    dropped = "UserPromptSubmit"
    dest = _make_codex_case(tmp_path, hooks=_codex_hooks(
        tuple(event for event in _CODEX_ENTRYPOINT_EVENTS if event != dropped)))

    findings = pm_import.judge_adapter_hook_entrypoints(dest, None, ["codex"])

    assert [(finding.kind, finding.event) for finding in findings] == [
        (pm_import.HOOK_ENTRYPOINT_MISSING, dropped)]
    assert _CODEX_HOOKS_REL in findings[0].detail
    assert any("sync-adapter-config --accept" in line
               for line in pm_import.hook_entrypoint_advisory_lines(findings[0]))


def test_full_entrypoint_set_is_silent(pm_import, tmp_path):
    """세 진입점이 다 있고 디스패처가 그 세대면 소견 0(거짓 red 금지)."""
    dest = _make_codex_case(tmp_path, hooks=_codex_hooks())

    assert pm_import.judge_adapter_hook_entrypoints(dest, None, ["codex"]) == []


def test_narrowed_matcher_is_not_a_universal_entrypoint(pm_import, tmp_path):
    """matcher 가 값 공간을 다 안 덮으면 진입점이 아니다 — 옛 형상이 green 으로 지나가면 안 된다."""
    dest = _make_codex_case(
        tmp_path, hooks=_codex_hooks(matcher="^collaborationspawn_agent$"))

    findings = pm_import.judge_adapter_hook_entrypoints(dest, None, ["codex"])

    assert {finding.event for finding in findings} == set(_CODEX_ENTRYPOINT_EVENTS)
    assert {finding.kind for finding in findings} == {
        pm_import.HOOK_ENTRYPOINT_MISSING}


def test_entrypoint_pointing_at_another_command_is_not_credited(pm_import, tmp_path):
    """matcher 만 맞고 디스패처를 안 부르는 블록은 진입점으로 세지 않는다."""
    document = json.loads(_codex_hooks())
    handler = document["hooks"]["PostToolUse"][0]["hooks"][0]
    handler["command"] = '"$py" .project_manager/tools/pm_log.py snapshot'
    dest = _make_codex_case(
        tmp_path, hooks=json.dumps(document, ensure_ascii=False, indent=2) + "\n")

    findings = pm_import.judge_adapter_hook_entrypoints(dest, None, ["codex"])

    assert [finding.event for finding in findings] == ["PostToolUse"]


# ── 진입점 판정은 **값 대조**다 (존재가 아니라 호출 값) ──────────────────────
# 디스패처 경로 부분문자열만 보면, 실행하지 않는 문자열이나 다른 이벤트를 부르는 커맨드까지
# 진입점으로 인정된다 — 가드는 발화하지 않는데 소견은 0 건인 false-green 이다. 아래 변형들은
# 그 시야가 실제 표면(어떤 값이 실행되는가)과 어긋나지 않는지를 고정한다.


def _codex_hooks_with_command(event: str, command: str) -> str:
    """한 이벤트의 진입점 커맨드만 바꾼 codex hooks.json."""
    document = json.loads(_codex_hooks())
    document["hooks"][event][0]["hooks"][0]["command"] = command
    document["hooks"][event][0]["hooks"][0]["commandWindows"] = command
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


@pytest.mark.parametrize("label,command", (
    # 플래그 제거 — 디스패처를 부르지만 어느 이벤트로도 dispatch 하지 않는다.
    ("flag 제거", f'"$py" {_CODEX_DISPATCHER_REL}'),
    # 잘못된 이벤트 — 선언은 PreToolUse 인데 호출은 PostToolUse 다.
    ("잘못된 이벤트",
     f'"$py" {_CODEX_DISPATCHER_REL} {_CODEX_DISPATCH_FLAG} PostToolUse'),
    # 비실행 경로 문자열 — 경로가 인자로 출력될 뿐 아무것도 실행되지 않는다.
    ("비실행 경로 문자열", f"printf '%s\\n' {_CODEX_DISPATCHER_REL}"),
    # 접미가 붙은 이벤트 — 포함 판정으로 돌아가면 이것도 같은 값으로 센다.
    ("접미가 붙은 이벤트",
     f'"$py" {_CODEX_DISPATCHER_REL} {_CODEX_DISPATCH_FLAG} PreToolUseLegacy'),
))
def test_entrypoint_that_does_not_actually_fire_is_reported(
        pm_import, tmp_path, label, command):
    """실제로 발화하지 않는 커맨드는 진입점으로 인정하지 않는다(값 대조·민감도)."""
    dest = _make_codex_case(
        tmp_path, hooks=_codex_hooks_with_command("PreToolUse", command))

    findings = pm_import.judge_adapter_hook_entrypoints(dest, None, ["codex"])

    assert [(finding.kind, finding.event) for finding in findings] == [
        (pm_import.HOOK_ENTRYPOINT_MISSING, "PreToolUse")], label


def test_dispatcher_path_and_event_must_live_in_the_same_command(
        pm_import, tmp_path):
    """경로는 이 커맨드, 호출 값은 저 커맨드로 흩어져 있으면 진입점이 아니다."""
    document = json.loads(_codex_hooks())
    handlers = document["hooks"]["PreToolUse"][0]["hooks"]
    handlers[0]["command"] = f"printf '%s\\n' {_CODEX_DISPATCHER_REL}"
    handlers[0]["commandWindows"] = (
        f'"$py" other_tool.py {_CODEX_DISPATCH_FLAG} PreToolUse')
    dest = _make_codex_case(
        tmp_path, hooks=json.dumps(document, ensure_ascii=False, indent=2) + "\n")

    findings = pm_import.judge_adapter_hook_entrypoints(dest, None, ["codex"])

    assert [finding.event for finding in findings] == ["PreToolUse"]


@pytest.mark.parametrize("command", (
    f'"$py" {_CODEX_DISPATCHER_REL} {_CODEX_DISPATCH_FLAG} PreToolUse',
    f'"$py" {_CODEX_DISPATCHER_REL} {_CODEX_DISPATCH_FLAG}=PreToolUse',
    f"& $py '{_CODEX_DISPATCHER_REL}' {_CODEX_DISPATCH_FLAG} 'PreToolUse'",
    f'"$py" .\\{_CODEX_DISPATCHER_REL.replace("/", chr(92))} '
    f'{_CODEX_DISPATCH_FLAG} PreToolUse',
))
def test_real_invocations_are_credited_across_shell_notations(
        pm_import, tmp_path, command):
    """실제로 부르는 표기는 전부 진입점으로 센다 — 조인 fix 가 정상 config 를 오탐하면 안 된다."""
    dest = _make_codex_case(
        tmp_path, hooks=_codex_hooks_with_command("PreToolUse", command))

    assert pm_import.judge_adapter_hook_entrypoints(dest, None, ["codex"]) == []


@pytest.mark.parametrize("harness,template_dir",
                         (("claude", "claude_code"), ("codex", "codex")))
def test_shipped_config_is_not_falsely_reported_by_the_value_check(
        pm_import, harness, template_dir):
    """현행 출하 선언 전수가 값 대조에서도 진입점으로 인정된다(오탐 0·역방향 확인)."""
    template_root = REPO / "templates" / template_dir
    spec = pm_import.ADAPTER_HOOK_SET[harness]
    document = json.loads(
        (template_root / spec.config_relpath).read_text(encoding="utf-8"))

    assert spec.entrypoints, harness
    for entrypoint in spec.entrypoints:
        assert pm_import._entrypoint_is_present(document, entrypoint), \
            f"{harness}:{entrypoint.event} 출하 선언이 값 대조에서 오탐됐다"


def test_dispatcher_that_knows_the_flag_but_not_the_event_is_stale(
        pm_import, tmp_path):
    """플래그는 알고 그 이벤트는 모르는 디스패처 세대도 stale 이다(값 기준 stale 검사).

    진입점 집합이 늘어난 릴리즈의 흡수 창에서 실제로 나오는 형상이고, 그 창에서 훅은 매
    발화마다 폴백으로 빠진다 — 리터럴 `--hook-dispatch` 존재만 보면 green 으로 지나간다."""
    dest = _make_codex_case(tmp_path, hooks=_codex_hooks(),
                            dispatcher=_DISPATCHER_PREVIOUS_EVENT_SET)

    findings = pm_import.judge_adapter_hook_entrypoints(dest, None, ["codex"])

    trailing = _CODEX_ENTRYPOINT_EVENTS[-1]
    assert [(finding.kind, finding.event) for finding in findings] == [
        (pm_import.HOOK_ENTRYPOINT_STALE_DISPATCHER, trailing)]
    assert trailing in findings[0].detail
    assert pm_import.entrypoint_invocation(
        pm_import.ADAPTER_HOOK_SET["codex"].entrypoints[-1]) in findings[0].detail


def test_shipped_codex_dispatcher_supports_every_declared_invocation(pm_import):
    """출하 디스패처가 출하 config 의 호출 값을 전부 감당한다(자기정합·오탐 0)."""
    template_root = REPO / "templates" / "codex"

    for entrypoint in pm_import.ADAPTER_HOOK_SET["codex"].entrypoints:
        assert pm_import._entrypoint_dispatcher_gap(template_root, entrypoint) is None, \
            entrypoint.event


def test_stale_dispatcher_behind_a_present_entrypoint_is_reported(pm_import, tmp_path):
    """진입점은 있는데 설치된 디스패처가 그 플래그를 모르면 훅이 매번 폴백으로 빠진다.

    config 는 managed 라 자동 갱신되고 디스패처는 manifest 라 pm-update 가 덮는다 — 두 축의
    도착 순서가 갈리는 창이 실재하고, 그 창에서 가드는 무음 통과한다."""
    dest = _make_codex_case(tmp_path, hooks=_codex_hooks(),
                            dispatcher=_DISPATCHER_OLD)

    findings = pm_import.judge_adapter_hook_entrypoints(dest, None, ["codex"])

    assert {finding.kind for finding in findings} == {
        pm_import.HOOK_ENTRYPOINT_STALE_DISPATCHER}
    assert len(findings) == len(_CODEX_ENTRYPOINT_EVENTS)
    assert pm_import.hook_entrypoint_advisory_lines(findings[0]) == [
        f"pm-update 로 {_CODEX_DISPATCHER_REL} 를 먼저 받아라"]


def test_absent_dispatcher_is_reported_not_silently_credited(pm_import, tmp_path):
    """디스패처 파일 자체가 없으면 미지원과 같은 판정이다(부재 = 무발화)."""
    dest = _make_codex_case(tmp_path, hooks=_codex_hooks(), dispatcher=None)

    findings = pm_import.judge_adapter_hook_entrypoints(dest, None, ["codex"])

    assert len(findings) == len(_CODEX_ENTRYPOINT_EVENTS)
    assert {finding.kind for finding in findings} == {
        pm_import.HOOK_ENTRYPOINT_STALE_DISPATCHER}


def test_broken_or_absent_config_yields_no_entrypoint_finding(pm_import, tmp_path):
    """config 부재·파손은 어댑터 config 채널이 말하는 상태다 — 여기서 겹쳐 처방하지 않는다."""
    dest = _make_codex_case(tmp_path, hooks="{ 깨진 json")
    assert pm_import.judge_adapter_hook_entrypoints(dest, None, ["codex"]) == []

    (dest / _CODEX_HOOKS_REL).unlink()
    assert pm_import.judge_adapter_hook_entrypoints(dest, None, ["codex"]) == []


def test_previous_generation_declaration_without_entrypoints_is_silent(
        pm_import, tmp_path):
    """진입점 개념이 없던 세대 선언으로 판정하면 조용하다(구세대 형제 강등 경로)."""
    dest = _make_codex_case(tmp_path, hooks=_codex_hooks(("PreToolUse",)))
    legacy_spec = SimpleNamespace(
        config_relpath=_CODEX_HOOKS_REL, live_files=(_CODEX_DISPATCHER_REL,),
        flag_support={}, coupled_groups=())

    assert pm_import.judge_adapter_hook_entrypoints(
        dest, None, ["codex"], declarations={"codex": legacy_spec}) == []


def test_sync_path_reports_missing_entrypoint_without_blocking(
        pm_update, tmp_path, capsys):
    """pm_update 가 진입점 누락을 **지목**하되 완료 게이트는 막지 않는다(advisory)."""
    dest = _make_codex_case(tmp_path, hooks=_codex_hooks(("PreToolUse",)))
    source = tmp_path / "framework"
    (source / "templates" / "codex" / ".codex").mkdir(parents=True)
    _plant_upstream_pm_import(source)

    result = pm_update.check_adapter_hook_sets(dest, source)
    pm_update._print_adapter_hook_set_finding(result, dry_run=False)
    err = capsys.readouterr().err

    assert result["status"] == "ok" and result["findings"] == []
    assert {item["event"] for item in result["entrypoints"]} == set(
        _CODEX_ENTRYPOINT_EVENTS) - {"PreToolUse"}
    assert "어댑터 훅 진입점 누락(codex)" in err
    for event in set(_CODEX_ENTRYPOINT_EVENTS) - {"PreToolUse"}:
        assert event in err, event
    assert not pm_update._adapter_hook_set_gate_failed(result), \
        "advisory 축이 완료 게이트를 막았다 — 훅을 끈 채택자의 흡수가 영구히 잠긴다"


def test_sync_path_is_silent_when_every_entrypoint_is_present(
        pm_update, tmp_path, capsys):
    """진입점이 다 있으면 이 축은 무출력이다(소음 0)."""
    dest = _make_codex_case(tmp_path, hooks=_codex_hooks())
    source = tmp_path / "framework"
    (source / "templates" / "codex" / ".codex").mkdir(parents=True)
    _plant_upstream_pm_import(source)

    result = pm_update.check_adapter_hook_sets(dest, source)
    pm_update._print_adapter_hook_set_finding(result, dry_run=False)

    assert result["entrypoints"] == []
    assert "진입점" not in capsys.readouterr().err


# ── pm_update 동기 경로 배선 ────────────────────────────────────────────────


def test_sync_path_reports_mixed_generation_loudly_and_blocks(pm_update, tmp_path, capsys):
    """혼합 세대는 동기 경로에서 loud 처방 + 완료 게이트 red 다(조용한 통과 금지)."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=True),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_OLD)

    result = pm_update.check_adapter_hook_sets(dest, source)
    pm_update._print_adapter_hook_set_finding(result, dry_run=False)

    assert result["status"] == "ok" and len(result["findings"]) == 1
    assert pm_update._adapter_hook_set_gate_failed(result)
    err = capsys.readouterr().err
    assert "훅 세트 세대 불일치" in err, err
    assert _GIT_ANCHOR_FLAG in err and _DRIVER_REL in err, err
    assert "pm-update" in err, "처방 커맨드 부재"


def test_sync_path_is_silent_when_generations_match(pm_update, tmp_path, capsys):
    """정합이면 무출력 — 매 sync 마다 우는 경고는 곧 무시된다."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=True),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_NEW)

    result = pm_update.check_adapter_hook_sets(dest, source)
    pm_update._print_adapter_hook_set_finding(result, dry_run=False)

    assert not pm_update._adapter_hook_set_gate_failed(result)
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


def test_sync_path_unavailable_is_loud_but_not_blocking(pm_update, tmp_path,
                                                        monkeypatch, capsys):
    """판정 불가(구형/손상 사본)는 경고만 — 가드가 복구 실행 자체를 자기잠금하면 안 된다."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=True),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_OLD)
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: None)

    result = pm_update.check_adapter_hook_sets(dest, source)
    pm_update._print_adapter_hook_set_finding(result, dry_run=False)

    assert result["status"] == "unavailable"
    assert not pm_update._adapter_hook_set_gate_failed(result)
    assert "훅 세트 세대 검사를 건너뛰었다" in capsys.readouterr().err


def test_main_sync_run_returns_rc1_on_mixed_generation(pm_update, tmp_path,
                                                       monkeypatch, capsys):
    """실 동기 실행이 혼합 세대를 rc1 로 낸다 — 배선이 실제로 돌아야 게이트다(엔진 변경 0 경로)."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=True),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_OLD)
    _write_source_manifest(source, ["# 합성 manifest — 엔진 변경 0 경로"])
    monkeypatch.setattr(pm_update, "REPO", dest)

    rc = pm_update.main(["--from", str(source)])

    assert rc == 1, capsys.readouterr()
    err = capsys.readouterr().err
    assert "훅 세트" in err and _GIT_ANCHOR_FLAG in err, err


def test_main_post_apply_gate_returns_rc1_after_engine_files_land(
        pm_update, tmp_path, monkeypatch, capsys):
    """엔진 파일을 **실제로 적용한 뒤** 남은 불일치도 rc1 이다 — apply 후 경로의 durable 박제.

    엔진 변경 0 경로만 덮으면 "배달하는 그 실행"(changes>0)에서 게이트가 도는지가 미검증으로
    남는다 — v1.7.0 흡수에서 락아웃이 터진 자리가 정확히 그 실행이다."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=True),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_OLD)
    engine_rel = ".project_manager/tools/__hook_set_sentinel__.py"
    engine_src = source / engine_rel
    engine_src.parent.mkdir(parents=True, exist_ok=True)
    engine_src.write_text("# 상류 엔진 파일\n", encoding="utf-8")
    _write_source_manifest(source, [engine_rel])
    monkeypatch.setattr(pm_update, "REPO", dest)

    rc = pm_update.main(["--from", str(source)])

    out, err = capsys.readouterr()
    assert (dest / engine_rel).read_text(encoding="utf-8") == "# 상류 엔진 파일\n", \
        "엔진 파일이 적용되지 않아 post-apply 경로를 타지 않았다(픽스처 전제 붕괴)"
    assert "1 파일 동기화" in out, out
    assert rc == 1, (out, err)
    assert "적용됐지만" in err and _GIT_ANCHOR_FLAG in err, err


# ── apply() 훅 세트 원자 write ──────────────────────────────────────────────


def test_apply_writes_hook_set_atomically_and_others_in_place(pm_update, tmp_path,
                                                              monkeypatch):
    """훅 세트 파일만 원자 교체 경로를 탄다 — 실행 중 하네스가 부분 파일을 읽지 않게.

    다른 엔진 파일까지 확대하지 않는 것도 함께 고정한다(결정: 전 파일 확대 안 함)."""
    dest = tmp_path / "dest"
    (dest / ".claude").mkdir(parents=True)
    (dest / ".project_manager" / "tools").mkdir(parents=True)
    source = tmp_path / "src"
    source.mkdir()
    hook_src = source / "hook.sh"
    hook_src.write_text(_WRAPPER_NEW, encoding="utf-8")
    tool_src = source / "tool.py"
    tool_src.write_text("# 신 엔진\n", encoding="utf-8")
    hook_dst = dest / _WRAPPER_REL
    tool_dst = dest / ".project_manager/tools/board.py"
    hook_dst.write_text(_WRAPPER_OLD, encoding="utf-8")
    tool_dst.write_text("# 구 엔진\n", encoding="utf-8")
    hook_inode = hook_dst.stat().st_ino
    tool_inode = tool_dst.stat().st_ino

    atomic_calls: list[str] = []
    original = pm_update._atomic_copy2
    monkeypatch.setattr(pm_update, "_atomic_copy2",
                        lambda src, dst: (atomic_calls.append(str(dst)), original(src, dst))[1])

    pm_update.apply([
        (_WRAPPER_REL, hook_src, hook_dst, "M"),
        (".project_manager/tools/board.py", tool_src, tool_dst, "M"),
    ])

    assert atomic_calls == [str(hook_dst)], atomic_calls
    assert hook_dst.read_text(encoding="utf-8") == _WRAPPER_NEW
    assert tool_dst.read_text(encoding="utf-8") == "# 신 엔진\n"
    if os.name != "nt":
        # POSIX 에선 inode 가 곧 증거다 — 교체는 새 inode, in-place 는 같은 inode.
        assert hook_dst.stat().st_ino != hook_inode, "훅 파일이 in-place 로 덮였다(torn 창)"
        assert tool_dst.stat().st_ino == tool_inode, "훅 세트 밖까지 원자 write 로 확대됐다"


_NEXT_GENERATION_HOOK_REL = ".claude/next_generation_hook.sh"
# 다음 세대 pm_import 가 들고 오는 선언 — 선언 자신이 pm_import 안에 살기 때문에, dest 사본의
# 선언은 업그레이드 시점에 정의상 한 세대 뒤다.
_UPSTREAM_ADDS_HOOK_PATH = (
    "\n"
    'ADAPTER_HOOK_SET["claude"] = ADAPTER_HOOK_SET["claude"]._replace(\n'
    "    live_files=ADAPTER_HOOK_SET['claude'].live_files + "
    f"({_NEXT_GENERATION_HOOK_REL!r},),\n"
    ")\n"
)


def test_apply_uses_source_generation_declaration_not_the_installed_one(
        pm_update, tmp_path, monkeypatch):
    """이번 세대가 **새로 추가한** 훅 경로도 같은 실행에서 원자 교체된다(구세대 선언 폐쇄).

    선언을 dest 사본에서 읽으면 원자 write 가 영영 한 세대 늦게 도착한다 — pm_import 자체를
    갱신하는 바로 그 실행에서, 새 세대가 등재한 훅 파일이 copy2 로 떨어진다."""
    source = tmp_path / "framework"
    source.mkdir()
    _plant_upstream_pm_import(source, extra_tail=_UPSTREAM_ADDS_HOOK_PATH)
    dest = tmp_path / "adopter"
    (dest / ".claude").mkdir(parents=True)
    hook_dst = dest / _NEXT_GENERATION_HOOK_REL
    hook_dst.write_text("# 구 훅\n", encoding="utf-8")
    hook_src = source / "new_hook.sh"
    hook_src.write_text("# 신 훅\n", encoding="utf-8")
    hook_inode = hook_dst.stat().st_ino

    installed = pm_update.resolve_hook_set_predicate()
    incoming = pm_update.resolve_hook_set_predicate(source)
    assert not installed(_NEXT_GENERATION_HOOK_REL), \
        "픽스처 전제 붕괴 — 설치된 세대가 이미 그 경로를 안다"
    assert incoming(_NEXT_GENERATION_HOOK_REL), "상류 세대 선언이 해소되지 않았다"

    atomic_calls: list[str] = []
    original = pm_update._atomic_copy2
    monkeypatch.setattr(pm_update, "_atomic_copy2",
                        lambda src, dst: (atomic_calls.append(str(dst)),
                                          original(src, dst))[1])
    pm_update.apply([(_NEXT_GENERATION_HOOK_REL, hook_src, hook_dst, "M")],
                    is_hook_set_path=incoming)

    assert atomic_calls == [str(hook_dst)], atomic_calls
    assert hook_dst.read_text(encoding="utf-8") == "# 신 훅\n"
    if os.name != "nt":
        assert hook_dst.stat().st_ino != hook_inode, "구세대 판정자로 copy2 폴백됐다"


def test_source_generation_predicate_falls_back_to_local_when_absent(
        pm_update, tmp_path):
    """상류에 pm_import 가 없으면 로컬 세대로 폴백한다(판정 자체를 잃지 않는다)."""
    source = tmp_path / "framework"
    source.mkdir()

    predicate = pm_update.resolve_hook_set_predicate(source)

    assert predicate(_WRAPPER_REL), "폴백이 로컬 세대 선언을 못 읽었다"
    assert not predicate(".project_manager/tools/board.py")


def test_unresolvable_predicate_is_loud_not_silent(pm_update, monkeypatch, capsys):
    """판정자를 아예 못 구하면 훅 파일이 통째로 비원자 write 다 — 그 사실을 stderr 로 남긴다."""
    def _boom():
        raise RuntimeError("사본 손상(주입)")
    monkeypatch.setattr(pm_update, "_load_pm_import", _boom)

    predicate = pm_update.resolve_hook_set_predicate()

    assert not predicate(_WRAPPER_REL)
    err = capsys.readouterr().err
    assert "원자 write 판정자를 해소하지 못했다" in err, f"무진단 침묵: {err!r}"
    assert "사본 손상(주입)" in err, "사유가 없어 원인을 못 짚는다"


# ── 경로 스코프 부분 전파 (훅 세트 반쪽 갱신 거부) ────────────────────────────


def test_partial_scope_over_hook_set_is_refused_before_writing(
        pm_update, pm_import, tmp_path, monkeypatch, capsys):
    """래퍼만 `--paths` 로 옮기려는 요청은 **쓰기 전에** rc1 이다 — 락아웃을 손수 만들지 않게.

    경로 스코프는 어댑터 채널을 끄므로 세대 검사가 전무하다. 반쪽 갱신을 알리기만 하면(엔진 도구
    rev 혼재 경고처럼) 하네스가 잠긴 채로 rc0 성공이 된다."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=True),
        dest_wrapper=_WRAPPER_OLD, dest_driver=_DRIVER_OLD)
    template = source / "templates" / "claude_code"
    for rel in (_WRAPPER_REL, _DRIVER_REL):
        (source / rel).parent.mkdir(parents=True, exist_ok=True)
        (source / rel).write_text((template / rel).read_text(encoding="utf-8"),
                                  encoding="utf-8")
    _write_source_manifest(source, [_WRAPPER_REL, _DRIVER_REL])
    monkeypatch.setattr(pm_update, "REPO", dest)

    rc = pm_update.main(["--from", str(source), "--paths", _WRAPPER_REL])

    err = capsys.readouterr().err
    assert rc == 1, err
    assert (dest / _WRAPPER_REL).read_text(encoding="utf-8") == _WRAPPER_OLD, \
        "거부인데 래퍼가 이미 갱신됐다(쓰기 후 판정)"
    assert "훅 세트를 반쪽만 갱신한다" in err, err
    assert _DRIVER_REL in err, "세트 전량 경로가 안 나와 무엇을 더 지목할지 알 수 없다"


_SOURCE_COORDINATE_MANIFEST = [
    f"{rel}    @source=templates/claude_code/{rel}" for rel in (_WRAPPER_REL, _DRIVER_REL)
]


def test_partial_scope_in_source_coordinates_is_refused(pm_update, tmp_path,
                                                        monkeypatch, capsys):
    """`@source` 상류 좌표로 지목한 부분 요청도 거부된다 — 좌표 표기가 검사를 끄면 안 된다.

    `--paths` 는 dest 좌표와 상류 좌표를 모두 유효 지목으로 받는다. 원문 표기를 선언(dest 좌표)과
    직접 비교하면 상류 좌표 요청은 교집합 0 이라 검사가 통째로 무발화하고, 래퍼만 갱신한 채 rc0 다."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=True),
        dest_wrapper=_WRAPPER_OLD, dest_driver=_DRIVER_OLD)
    _write_source_manifest(source, _SOURCE_COORDINATE_MANIFEST)
    monkeypatch.setattr(pm_update, "REPO", dest)

    rc = pm_update.main(
        ["--from", str(source), "--paths", f"templates/claude_code/{_WRAPPER_REL}"])

    err = capsys.readouterr().err
    assert rc == 1, err
    assert (dest / _WRAPPER_REL).read_text(encoding="utf-8") == _WRAPPER_OLD, \
        "상류 좌표 요청이 검사를 우회해 래퍼만 갱신됐다"
    assert "훅 세트를 반쪽만 갱신한다" in err and _DRIVER_REL in err, err


def test_full_hook_set_scope_in_source_coordinates_is_allowed(
        pm_update, tmp_path, monkeypatch, capsys):
    """상류 좌표로 묶음 전량을 지목하면 통과한다 — 거부 대상은 진부분집합뿐이다."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=True),
        dest_wrapper=_WRAPPER_OLD, dest_driver=_DRIVER_OLD)
    _write_source_manifest(source, _SOURCE_COORDINATE_MANIFEST)
    monkeypatch.setattr(pm_update, "REPO", dest)

    rc = pm_update.main([
        "--from", str(source), "--paths",
        f"templates/claude_code/{_WRAPPER_REL}", f"templates/claude_code/{_DRIVER_REL}",
    ])

    assert rc == 0, capsys.readouterr()
    assert (dest / _WRAPPER_REL).read_text(encoding="utf-8") == _WRAPPER_NEW
    assert (dest / _DRIVER_REL).read_text(encoding="utf-8") == _DRIVER_NEW


def test_full_hook_set_scope_is_allowed(pm_update, tmp_path, monkeypatch, capsys):
    """세트 전량을 지목하면 통과한다 — 가드가 정당한 부분 전파까지 막지 않는다."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=True),
        dest_wrapper=_WRAPPER_OLD, dest_driver=_DRIVER_OLD)
    template = source / "templates" / "claude_code"
    for rel in (_WRAPPER_REL, _DRIVER_REL):
        (source / rel).parent.mkdir(parents=True, exist_ok=True)
        (source / rel).write_text((template / rel).read_text(encoding="utf-8"),
                                  encoding="utf-8")
    _write_source_manifest(source, [_WRAPPER_REL, _DRIVER_REL])
    monkeypatch.setattr(pm_update, "REPO", dest)

    rc = pm_update.main(["--from", str(source), "--paths", _WRAPPER_REL, _DRIVER_REL])

    assert rc == 0, capsys.readouterr()
    assert (dest / _DRIVER_REL).read_text(encoding="utf-8") == _DRIVER_NEW
    assert (dest / _WRAPPER_REL).read_text(encoding="utf-8") == _WRAPPER_NEW


def test_scope_outside_hook_set_is_not_affected(pm_import):
    """훅 세트를 안 건드리는 전파는 관심사가 아니다(엔진 도구 부분 전파는 종전대로)."""
    assert pm_import.hook_set_partial_update(
        [".project_manager/tools/board.py"],
        [".project_manager/tools/board.py", _WRAPPER_REL]) == []


def test_uncoupled_hook_file_may_be_propagated_alone(pm_import):
    """결합이 없는 훅 파일(독립 relay 드라이버)은 단건 전파가 정당하다 — 가드가 과잉이면 안 된다.

    하네스 전체를 한 단위로 보면 `pm_orch_opencode.py` 하나 옮기는 정당한 요청까지 막힌다."""
    assert pm_import.hook_set_partial_update(
        [".opencode/pm_orch_opencode.py"],
        [".opencode/pm_orch_opencode.py", ".opencode/lib/ctx-guard-core.cjs"]) == []


def test_partial_update_inside_a_declared_directory_is_refused(pm_import):
    """디렉토리 선언 안의 파일 하나만 옮기는 것도 반쪽 갱신이다(형제 코어가 구세대로 남음)."""
    partial = pm_import.hook_set_partial_update(
        [".opencode/plugins/ctx-guard.js"],
        [".opencode/plugins/ctx-guard.js", ".opencode/lib/ctx-guard-core.cjs"])

    assert [row[0] for row in partial] == ["opencode"], partial
    assert partial[0][1] == (".opencode/lib/",), partial


def test_already_current_sibling_is_not_demanded(pm_import):
    """묶음의 형제가 **이미 최신**이면(계획에 없으면) 요구하지 않는다 — 거짓 거부 금지."""
    assert pm_import.hook_set_partial_update([_WRAPPER_REL], [_WRAPPER_REL]) == []


def test_whole_group_update_is_allowed(pm_import):
    """묶음이 함께 옮겨지면 통과한다."""
    assert pm_import.hook_set_partial_update(
        [_WRAPPER_REL, _DRIVER_REL], [_WRAPPER_REL, _DRIVER_REL]) == []


# ── 실참조 경로 해소 (선언 좌표와 다른 토폴로지) ──────────────────────────────


def test_reference_outside_declared_coordinate_is_judged_at_its_real_path(
        pm_import, tmp_path):
    """settings 가 다른 좌표로 부르는 훅 세트도 **그 자리에서** 판정한다.

    제품 루트는 자기 `.claude/` 에 훅이 없어 `templates/claude_code/.claude/…` 를 부른다. 좌표가
    선언과 다르다고 검사에서 빼면 그 트리는 세대 검사가 통째로 꺼진다(stale 드라이버가 green)."""
    root = tmp_path / "product"
    prefix = "templates/claude_code"
    (root / prefix / ".claude").mkdir(parents=True)
    (root / prefix / _WRAPPER_REL).write_text(_WRAPPER_NEW, encoding="utf-8")
    (root / prefix / _DRIVER_REL).write_text(_DRIVER_OLD, encoding="utf-8")
    document = json.loads(_settings(git_anchor=True,
                                    wrapper_rel=f"{prefix}/{_WRAPPER_REL}"))
    spec = pm_import.ADAPTER_HOOK_SET["claude"]

    unmet = pm_import._hook_set_demands(document, spec, root)

    assert list(unmet) == [(pm_import.HOOK_SET_UNSUPPORTED_FLAG, _GIT_ANCHOR_FLAG)], unmet
    assert unmet[(pm_import.HOOK_SET_UNSUPPORTED_FLAG, _GIT_ANCHOR_FLAG)] == (
        f"{prefix}/{_DRIVER_REL}",), "미충족 경로가 실제 좌표가 아니다(처방이 엉뚱한 파일을 가리킴)"


def test_reference_outside_declared_coordinate_is_green_when_generation_matches(
        pm_import, tmp_path):
    """같은 토폴로지에서 세대가 맞으면 조용하다(거짓 red 금지)."""
    root = tmp_path / "product"
    prefix = "templates/claude_code"
    (root / prefix / ".claude").mkdir(parents=True)
    (root / prefix / _WRAPPER_REL).write_text(_WRAPPER_NEW, encoding="utf-8")
    (root / prefix / _DRIVER_REL).write_text(_DRIVER_NEW, encoding="utf-8")
    document = json.loads(_settings(git_anchor=True,
                                    wrapper_rel=f"{prefix}/{_WRAPPER_REL}"))

    assert pm_import._hook_set_demands(
        document, pm_import.ADAPTER_HOOK_SET["claude"], root) == {}


def test_product_root_settings_reference_is_actually_judged(pm_import):
    """이 checkout(제품 루트) 자기 settings.json 의 훅 참조가 **검사 대상 안**이고 정합이다.

    루트는 `templates/claude_code/.claude/…` 를 부르는 실 토폴로지다 — 선언 좌표만 보던 판정은 이
    커맨드를 통째로 건너뛰어 stale template 드라이버가 green 이었다. 이 단언이 그 자리를 지킨다."""
    settings = REPO / ".claude" / "settings.json"
    document = json.loads(settings.read_text(encoding="utf-8"))
    spec = pm_import.ADAPTER_HOOK_SET["claude"]
    prefixes = [
        pm_import._hook_set_reference_prefix(
            pm_import._hook_script_relpath(
                pm_import._split_hook_command(command)[0]) or "", spec)
        for command in pm_import._hook_commands(document)
    ]

    assert any(prefix for prefix in prefixes if prefix), \
        f"루트 settings 의 훅 참조가 하나도 훅 세트로 해소되지 않았다: {prefixes}"
    assert pm_import._hook_set_demands(document, spec, REPO) == {}, \
        "루트 토폴로지가 참조하는 훅 세트가 구세대다"


_ADAPTER_NAMESPACES = (".claude/", ".codex/", ".opencode/", ".agents/")
# 하네스가 **실행**하는 파일 확장자. 프롬프트(.md·.toml·.rules)는 실행 중 부분 파일이 문제되지
# 않지만, 코드는 훅/플러그인/드라이버로 즉시 로드돼 torn read 가 곧 고장이다.
_EXECUTABLE_SUFFIXES = (".py", ".sh", ".js", ".cjs")


def _flavor_manifests() -> list[Path]:
    """루트 + 출하 3 flavor 의 engine.manifest (하나만 보면 그 하네스만 가드된다)."""
    roots = [REPO] + [REPO / "templates" / name
                      for name in ("claude_code", "codex", "opencode")]
    return [root / ".project_manager" / "engine.manifest" for root in roots]


def _manifest_executable_entries(manifest_path: Path) -> list[str]:
    """그 manifest 가 등재한 어댑터 네임스페이스의 **실행 파일** dest relpath 전수.

    manifest 한 줄은 파일 또는 디렉토리다(디렉토리는 재귀) — 디렉토리 등재는 `@source` 를 따라
    그 하위의 실행 파일을 펼친다. 그래야 `.opencode/lib`·`.opencode/plugins` 처럼 한 줄이 여러
    코드 파일을 나르는 등재가 가드에 잡힌다. 열거는 공용 repo-owned seam(tracked-only)이다 —
    출하되는 건 추적 파일이고, 직접 tree-walk 하면 미추적 잔재가 거짓 red 를 만든다."""
    out: list[str] = []
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        tokens = line.split()
        rel = tokens[0]
        if not rel.startswith(_ADAPTER_NAMESPACES):
            continue
        source_rel = next(
            (token[len("@source="):] for token in tokens[1:]
             if token.startswith("@source=")), rel)
        source_path = REPO / source_rel
        if source_path.is_dir():
            out.extend(
                f"{rel.rstrip('/')}/{path.relative_to(source_path).as_posix()}"
                for path in sorted(repo_owned_paths(REPO, source_rel, mode=TRACKED_ONLY))
                if path.suffix in _EXECUTABLE_SUFFIXES)
        elif rel.endswith(_EXECUTABLE_SUFFIXES):
            out.append(rel)
    return out


def test_live_hook_set_declaration_covers_shipped_executable_entries(pm_import):
    """**전 하네스** manifest 가 등재한 어댑터 실행 파일이 전부 원자 write 대상이다.

    claude flavor 만 스캔하던 옛 가드는 opencode `.opencode/lib/*.cjs` 누락을 못 봤다(플러그인이
    로드 시점에 즉시 import 하는 공유 코어). 하네스 하나만 보는 가드는 나머지 하네스에서 같은
    클래스를 그대로 열어 둔다 — 새 훅/플러그인/코어를 manifest 에만 더하고 선언을 빼먹으면 그
    파일만 조용히 copy2 로 남는다."""
    seen: dict[str, list[str]] = {}
    for manifest_path in _flavor_manifests():
        for rel in _manifest_executable_entries(manifest_path):
            seen.setdefault(rel, []).append(manifest_path.parent.parent.name)

    assert seen, "픽스처 전제(어댑터 실행 파일 등재)가 사라졌다"
    missing = sorted(rel for rel in seen if not pm_import.is_live_hook_set_path(rel))
    assert missing == [], f"manifest 등재 실행 파일이 원자 write 선언 밖이다: {missing}"


def test_opencode_shared_core_is_atomic_like_the_claude_axis(pm_import):
    """opencode 플러그인이 즉시 import 하는 공유 코어도 원자 write 대상이다(하네스 비대칭 금지).

    플러그인 3종이 로드 시점에 `../lib/*-core.cjs` 를 import 하므로 코어가 부분 파일이면 플러그인
    로드 자체가 깨진다 — claude 축이 공유 코어(`ctx_guard.py`)를 넣은 것과 같은 근거다."""
    lib_dir = REPO / "templates" / "opencode" / ".opencode" / "lib"
    cores = sorted(path.name for path in lib_dir.glob("*.cjs"))

    assert cores, "픽스처 전제(opencode 공유 코어)가 사라졌다"
    for name in cores:
        assert pm_import.is_live_hook_set_path(f".opencode/lib/{name}"), name
    for plugin in sorted((REPO / "templates" / "opencode" / ".opencode"
                          / "plugins").glob("*.js")):
        assert pm_import.is_live_hook_set_path(f".opencode/plugins/{plugin.name}")


# ── 수용 순서 게이트 (엔진 파일 선행 · config 후행) ──────────────────────────


def _accept_args(source: Path, *, accept=None, accept_all=False,
                 check=False) -> SimpleNamespace:
    return SimpleNamespace(accept=accept, accept_all=accept_all, check=check,
                           list=False, source=str(source))


def test_unreadable_hook_file_is_lenient_for_report_but_blocks_mutation(
        pm_config, pm_import, tmp_path, capsys):
    """판독 불가 훅 파일: 조회는 관대(거짓 처방 금지)하고 **수용은 차단**(fail-closed)한다.

    확인되지 않은 파일을 근거로 config 를 새 세대로 앞세우면 그게 곧 락아웃이다 — 조회의 관대함을
    mutation 게이트가 그대로 상속하면 게이트가 있으나 마나다."""
    # dest·template 둘 다 그 플래그를 요구한다 — 조회(dest 요구)와 수용(template 요구)이 **같은
    #   판정 불가 파일**을 보게 해서 두 방향이 실제로 갈리는지 본다(공허한 단언 금지).
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=True),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_NEW)
    (dest / _DRIVER_REL).write_bytes(b"\xff\xfe\x00\x01 binary driver")  # UTF-8 해독 불가.
    before = (dest / _SETTINGS_REL).read_text(encoding="utf-8")

    assert pm_import.judge_adapter_hook_sets(dest, source) == [], \
        "조회 판정이 판정 불가 파일을 red 로 올렸다(거짓 처방)"
    rc = pm_config.cmd_sync_adapter_config(
        _accept_args(source, accept=_SETTINGS_REL), pm_import=pm_import, dest_root=dest)

    assert rc == 1
    assert (dest / _SETTINGS_REL).read_text(encoding="utf-8") == before
    assert "수용 거부" in capsys.readouterr().err


def test_check_reports_hook_set_generation_mismatch(pm_config, pm_import,
                                                    tmp_path, capsys):
    """`--check` 가 훅 세트 세대 불일치를 red 로 낸다 — pm-update 와 같은 게이트를 본다.

    스킬 카드가 완료 게이트로 지목하는 표면이라, pm-update 가 rc1 로 막는 클래스를 여기서 green
    으로 통과시키면 릴리즈가 false-complete 된다."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=True),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_OLD)

    rc = pm_config.cmd_sync_adapter_config(
        _accept_args(source, check=True), pm_import=pm_import, dest_root=dest)

    assert rc == 1
    err = capsys.readouterr().err
    assert "훅 세트 세대 불일치" in err and _GIT_ANCHOR_FLAG in err, err


def test_check_sees_hook_sets_even_when_config_channel_is_empty(
        pm_config, pm_import, tmp_path, capsys):
    """config 채널 대상이 0 이어도 훅 세트 red 는 red 다 — 게이트 표면 전체 우회 금지."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=True),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_OLD)
    (source / "templates" / "claude_code" / _SETTINGS_REL).unlink()  # 채널 대상 0.

    rc = pm_config.cmd_sync_adapter_config(
        _accept_args(source, check=True), pm_import=pm_import, dest_root=dest)

    out, err = capsys.readouterr()
    assert "채널 대상 없음" in out, out
    assert rc == 1, (out, err)
    assert "훅 세트 세대 불일치" in err, err


def test_check_stays_green_when_hook_set_matches(pm_config, pm_import, tmp_path, capsys):
    """정합이면 `--check` 는 종전대로 green 이다(가드가 정상 경로를 막지 않는다)."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=True),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_NEW)

    rc = pm_config.cmd_sync_adapter_config(
        _accept_args(source, check=True), pm_import=pm_import, dest_root=dest)

    assert rc == 0, capsys.readouterr()
    assert "수렴 확인" in capsys.readouterr().out


def test_accept_refuses_when_engine_files_are_behind(pm_config, pm_import, tmp_path, capsys):
    """수용 선행조건 미충족이면 **파일을 건드리지 않고** 거부한다 — 위험 방향을 기계가 막는다."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=False),
        dest_wrapper=_WRAPPER_OLD, dest_driver=_DRIVER_OLD,
        template_settings=_settings(git_anchor=True))
    before = (dest / _SETTINGS_REL).read_text(encoding="utf-8")

    rc = pm_config.cmd_sync_adapter_config(
        _accept_args(source, accept=_SETTINGS_REL), pm_import=pm_import, dest_root=dest)

    assert rc == 1
    assert (dest / _SETTINGS_REL).read_text(encoding="utf-8") == before, \
        "선행조건 미충족인데 config 를 앞세웠다(락아웃 재현)"
    err = capsys.readouterr().err
    assert "수용 거부" in err and "pm-update" in err, err


def test_accept_proceeds_when_engine_files_are_current(pm_config, pm_import, tmp_path):
    """엔진 파일이 이미 그 세대면 수용이 정상 진행된다(게이트가 정상 경로를 막지 않는다)."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=False),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_NEW,
        template_settings=_settings(git_anchor=True))

    rc = pm_config.cmd_sync_adapter_config(
        _accept_args(source, accept=_SETTINGS_REL), pm_import=pm_import, dest_root=dest)

    assert rc == 0
    assert (dest / _SETTINGS_REL).read_text(encoding="utf-8") == _settings(git_anchor=True)


def test_accept_of_absent_config_reports_channel_error_not_order_refusal(
        pm_config, pm_import, tmp_path, capsys):
    """dest 에 없는 config 를 수용하려 하면 채널 오류가 그대로 나온다(순서 게이트가 가로채지 않음)."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=True),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_NEW)
    (dest / _SETTINGS_REL).unlink()

    rc = pm_config.cmd_sync_adapter_config(
        _accept_args(source, accept=_SETTINGS_REL), pm_import=pm_import, dest_root=dest)

    err = capsys.readouterr().err
    assert rc == 1 and "수용 거부" not in err, err
    assert "그 파일이 없다" in err or "채널 대상이 아니다" in err, err


def test_accept_all_accepts_the_set_through_the_same_gate(pm_config, pm_import,
                                                          tmp_path, capsys):
    """세트 수용도 파일당 같은 게이트·같은 백업·같은 원자 교체를 탄다."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=False),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_NEW,
        template_settings=_settings(git_anchor=True),
        ledger={_SETTINGS_REL: _ledger_entry(_settings(git_anchor=False))})

    rc = pm_config.cmd_sync_adapter_config(
        _accept_args(source, accept_all=True), pm_import=pm_import, dest_root=dest)

    assert rc == 0, capsys.readouterr()
    assert (dest / _SETTINGS_REL).read_text(encoding="utf-8") == _settings(git_anchor=True)
    out = capsys.readouterr().out
    assert "세트 수용 결과: 1/1 수용" in out, out


def test_accept_all_refuses_blocked_member_without_touching_it(pm_config, pm_import,
                                                               tmp_path, capsys):
    """세트 안에서도 선행조건을 어긴 파일만 거부되고 그 파일은 그대로다(rc1)."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=False),
        dest_wrapper=_WRAPPER_OLD, dest_driver=_DRIVER_OLD,
        template_settings=_settings(git_anchor=True),
        ledger={_SETTINGS_REL: _ledger_entry(_settings(git_anchor=False))})
    before = (dest / _SETTINGS_REL).read_text(encoding="utf-8")

    rc = pm_config.cmd_sync_adapter_config(
        _accept_args(source, accept_all=True), pm_import=pm_import, dest_root=dest)

    assert rc == 1
    assert (dest / _SETTINGS_REL).read_text(encoding="utf-8") == before
    assert "0/1 수용" in capsys.readouterr().out


def test_accept_all_excludes_adopter_edits_and_says_so(pm_config, pm_import,
                                                       tmp_path, capsys):
    """채택자 편집분은 세트가 건드리지 않는다 — 무지목 일괄 교체가 하한선을 깨뜨린다.

    report 모드 대상(settings.json·opencode.jsonc·config.toml)에는 권한 allowlist·모델·threshold
    같은 실 노브가 들어 있다. 편집분은 단건 `--accept` 로만 받고, 제외 사실과 그 커맨드를 낸다."""
    edited = _settings(git_anchor=False).replace("15", "30")  # 채택자 노브 손편집.
    dest, source = _make_case(
        tmp_path, dest_settings=edited,
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_NEW,
        template_settings=_settings(git_anchor=True),
        ledger={_SETTINGS_REL: _ledger_entry(_settings(git_anchor=False))})

    rc = pm_config.cmd_sync_adapter_config(
        _accept_args(source, accept_all=True), pm_import=pm_import, dest_root=dest)

    out = capsys.readouterr().out
    assert (dest / _SETTINGS_REL).read_text(encoding="utf-8") == edited, \
        "세트 수용이 채택자 편집분을 덮었다"
    assert rc == 1, out
    assert "세트 수용 제외(채택자 편집분)" in out and _SETTINGS_REL in out, out
    assert f"--accept {_SETTINGS_REL}" in out, "단건 수용 경로 안내 부재"


def test_accept_all_prints_targets_before_touching_them(pm_config, pm_import,
                                                        tmp_path, capsys):
    """무엇을 건드릴지 **먼저 전부 보이고** 나서 수용한다(수용 줄보다 앞선 대상 목록)."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=False),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_NEW,
        template_settings=_settings(git_anchor=True),
        ledger={_SETTINGS_REL: _ledger_entry(_settings(git_anchor=False))})

    rc = pm_config.cmd_sync_adapter_config(
        _accept_args(source, accept_all=True), pm_import=pm_import, dest_root=dest)
    out = capsys.readouterr().out

    assert rc == 0, out
    assert out.index("# 세트 수용 대상 1건") < out.index("✓ 어댑터 config 수용"), out
    assert f"  - {_SETTINGS_REL} · claude · " in out, out


# ── 세대 선언 소비자 상류-통일 (T-0610) ──────────────────────────────────────
# 선언 자체가 pm_import 안에 살기 때문에 설치본 선언은 업그레이드 시점에 정의상 한 세대 뒤다.
# 게이트가 그 선언으로 판정하면 **상류가 이번에 들여오는 것**을 모른 채 통과한다.

_UPSTREAM_ONLY_FLAG = "--upstream-only-hook"
_UPSTREAM_ADDS_FLAG = (
    "\n"
    'ADAPTER_HOOK_SET["claude"] = ADAPTER_HOOK_SET["claude"]._replace(\n'
    "    flag_support=dict(ADAPTER_HOOK_SET['claude'].flag_support, **{\n"
    f"        {_UPSTREAM_ONLY_FLAG!r}: ({_WRAPPER_REL!r}, {_DRIVER_REL!r})}}),\n"
    ")\n"
)
_UPSTREAM_ONLY_GROUP = (".claude/alpha_hook.sh", ".claude/alpha_core.py")
_UPSTREAM_ADDS_GROUP = (
    "\n"
    'ADAPTER_HOOK_SET["claude"] = ADAPTER_HOOK_SET["claude"]._replace(\n'
    "    live_files=ADAPTER_HOOK_SET['claude'].live_files + "
    f"{_UPSTREAM_ONLY_GROUP!r},\n"
    "    coupled_groups=ADAPTER_HOOK_SET['claude'].coupled_groups + "
    f"({_UPSTREAM_ONLY_GROUP!r},),\n"
    ")\n"
)


def test_accept_gate_judges_with_upstream_generation_not_installed(
        pm_config, pm_import, tmp_path, capsys):
    """상류만 아는 플래그를 요구하는 config 는 수용이 거부된다 — 게이트는 상류 세대로 판정한다.

    설치본 선언은 그 플래그를 모르므로 "선언 밖 플래그" 로 접어 게이트가 통과시킨다(있으나 마나).
    미지원 드라이버 위에 새 config 를 앉히는 게 정확히 이 게이트가 막는 락아웃이다."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=False),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_NEW,  # 신 세대지만 새 플래그는 모름.
        template_settings=_settings(git_anchor=True, flag=_UPSTREAM_ONLY_FLAG),
        upstream_tail=_UPSTREAM_ADDS_FLAG)
    before = (dest / _SETTINGS_REL).read_text(encoding="utf-8")

    installed_only = pm_import.hook_set_accept_decision(
        dest, source, _SETTINGS_REL,
        declarations=pm_import.hook_set_declarations(None))
    rc = pm_config.cmd_sync_adapter_config(
        _accept_args(source, accept=_SETTINGS_REL), pm_import=pm_import, dest_root=dest)

    assert installed_only.blockers == [], \
        "픽스처 전제 붕괴 — 설치본 선언이 이미 그 플래그를 안다(상류 축이 무의미)"
    assert rc == 1
    assert (dest / _SETTINGS_REL).read_text(encoding="utf-8") == before, \
        "상류만 아는 세대 요구를 못 보고 config 를 앞세웠다"
    err = capsys.readouterr().err
    assert "수용 거부" in err and _UPSTREAM_ONLY_FLAG in err, err


def test_accept_gate_binds_checked_template_bytes_to_the_copy(pm_import, tmp_path):
    """게이트가 검사한 template bytes 와 실제 복사 bytes 가 다르면 수용이 중단된다.

    판정↔쓰기 사이에 상류가 바뀌면 "검사한 세대" 와 다른 내용이 설치된다 — dest 동시 편집을 막는
    `expected_sha256` 의 반대편 축이다."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=False),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_NEW,
        template_settings=_settings(git_anchor=True))
    decision = pm_import.hook_set_accept_decision(
        dest, source, _SETTINGS_REL,
        declarations=pm_import.hook_set_declarations(source, required=True))
    assert decision.blockers == [] and decision.template_sha256, decision
    before = (dest / _SETTINGS_REL).read_text(encoding="utf-8")
    # 판정 뒤 상류가 바뀐다(다른 세대의 config 가 그 자리에 놓임).
    (source / "templates" / "claude_code" / _SETTINGS_REL).write_text(
        _settings(git_anchor=True, flag=_UPSTREAM_ONLY_FLAG), encoding="utf-8")

    outcome = pm_import.accept_adapter_config(
        dest, source, _SETTINGS_REL,
        expected_template_sha256=decision.template_sha256)

    assert outcome.status == "raced", outcome
    assert "상류 template" in outcome.detail, outcome.detail
    assert (dest / _SETTINGS_REL).read_text(encoding="utf-8") == before, \
        "검사하지 않은 bytes 가 설치됐다"


def test_accept_gate_is_fail_closed_when_upstream_generation_is_unreadable(
        pm_config, pm_import, tmp_path, capsys):
    """상류 세대를 못 읽으면 수용을 거부한다 — mutation 게이트는 모르면 멈춘다(fail-closed)."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=False),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_NEW,
        template_settings=_settings(git_anchor=True),
        upstream_tail=None)  # 상류 트리에 pm_import 없음.
    before = (dest / _SETTINGS_REL).read_text(encoding="utf-8")

    rc = pm_config.cmd_sync_adapter_config(
        _accept_args(source, accept=_SETTINGS_REL), pm_import=pm_import, dest_root=dest)

    err = capsys.readouterr().err
    assert rc == 1, err
    assert (dest / _SETTINGS_REL).read_text(encoding="utf-8") == before
    assert "수용 거부" in err and "상류 세대 선언" in err, err


def test_partial_guard_sees_a_coupled_group_only_upstream_knows(
        pm_update, pm_import, tmp_path, monkeypatch, capsys):
    """상류만 아는 결합 묶음의 부분 지목도 거부된다 — 가드도 상류 세대로 판정한다.

    새 묶음을 들여오는 **첫 전파**가 정확히 반쪽 갱신이 나는 자리다(설치본 선언은 그 묶음이 없다)."""
    alpha, beta = _UPSTREAM_ONLY_GROUP
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=True),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_NEW,
        upstream_tail=_UPSTREAM_ADDS_GROUP)
    for rel, text in ((alpha, "# 신 alpha\n"), (beta, "# 신 beta\n")):
        (source / rel).parent.mkdir(parents=True, exist_ok=True)
        (source / rel).write_text(text, encoding="utf-8")
        (dest / rel).write_text("# 구 세대\n", encoding="utf-8")
    _write_source_manifest(source, [alpha, beta])
    monkeypatch.setattr(pm_update, "REPO", dest)

    assert pm_import.hook_set_partial_update([alpha], [alpha, beta]) == [], \
        "픽스처 전제 붕괴 — 설치본 선언이 이미 그 묶음을 안다"
    rc = pm_update.main(["--from", str(source), "--paths", alpha])

    err = capsys.readouterr().err
    assert rc == 1, err
    assert (dest / alpha).read_text(encoding="utf-8") == "# 구 세대\n", \
        "거부인데 alpha 가 이미 갱신됐다"
    assert "훅 세트를 반쪽만 갱신한다" in err and beta in err, err


def test_accept_gate_binds_the_declaration_snapshot_too(pm_import, tmp_path):
    """선언 소스(상류 pm_import)가 판정 뒤 바뀌면 중단한다 — template 해시만으로는 못 막는다.

    config template 이 그대로여도 상류가 통째로 갱신되면 **구 선언으로 낸 판정** 위에 새 세대가
    설치된다. 두 해시는 한 벌의 스냅샷이라 어느 쪽이 변해도 raced 다."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=False),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_NEW,
        template_settings=_settings(git_anchor=True))
    decision = pm_import.hook_set_accept_decision(
        dest, source, _SETTINGS_REL,
        declarations=pm_import.hook_set_declarations(source, required=True))
    assert decision.blockers == [] and decision.generation_sha256, decision
    before = (dest / _SETTINGS_REL).read_text(encoding="utf-8")
    # config template 은 그대로 두고 **선언 소스만** 새 세대로 바꾼다.
    _plant_upstream_pm_import(source, extra_tail=_UPSTREAM_ADDS_FLAG)

    outcome = pm_import.accept_adapter_config(
        dest, source, _SETTINGS_REL,
        expected_template_sha256=decision.template_sha256,
        expected_generation_sha256=decision.generation_sha256)

    assert outcome.status == "raced", outcome
    assert "세대 선언" in outcome.detail, outcome.detail
    assert (dest / _SETTINGS_REL).read_text(encoding="utf-8") == before


def test_accept_gate_blocks_when_snapshot_hash_cannot_be_made(
        pm_import, tmp_path, monkeypatch):
    """스냅샷 해시를 못 만들면 수용을 막는다 — 결속이 조용히 생략된 채 진행되면 안 된다.

    호출부는 `is not None` 으로 결속 인자를 붙이므로, 해시 계산 실패는 "검증 없이 교체" 로
    번역된다. mutation 축은 모르면 멈춘다(구세대 강등의 **의도적** None 과 구분되는 실패성 None)."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=False),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_NEW,
        template_settings=_settings(git_anchor=True))
    generation = pm_import.hook_set_declarations(source, required=True)
    assert pm_import.hook_set_accept_decision(
        dest, source, _SETTINGS_REL, declarations=generation).blockers == [], \
        "픽스처 전제(정상 경로는 통과)"
    monkeypatch.setattr(pm_import, "file_sha256", lambda _path: None)  # 해시 계산 실패 주입.

    decision = pm_import.hook_set_accept_decision(
        dest, source, _SETTINGS_REL, declarations=generation)

    assert decision.blockers, "해시 부재인데 게이트가 통과시켰다"
    assert "재확인할 수" in decision.blockers[0].detail, decision.blockers[0].detail


def test_accept_downgrades_loudly_on_legacy_sibling_engine(pm_config, pm_import,
                                                           tmp_path, capsys):
    """직전 세대 형제 pm_import 에서도 CLI 가 죽지 않는다 — 구 시그니처로 강등하되 loud.

    복구 채널은 사본 세대가 섞인 창을 의도적으로 연다(그게 복구 exemption 의 목적이다). 새 키워드를
    무조건 넘기면 그 창에서 `--accept` 가 TypeError 로 죽어 복구 자체가 막힌다."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=False),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_NEW,
        template_settings=_settings(git_anchor=True),
        ledger={_SETTINGS_REL: _ledger_entry(_settings(git_anchor=False))})

    class LegacyPmImport:
        """T-0606 이전 세대 — 세대 게이트도, 스냅샷 결속 키워드도 없다."""
        ADAPTER_CONFIG_REAPPROVAL_NOTE = pm_import.ADAPTER_CONFIG_REAPPROVAL_NOTE
        resolve_adapter_config_source = staticmethod(
            pm_import.resolve_adapter_config_source)
        judge_adapter_configs = staticmethod(pm_import.judge_adapter_configs)
        unconverged_managed_adapter_configs = staticmethod(
            pm_import.unconverged_managed_adapter_configs)
        ADAPTER_CONFIG_REPORT = pm_import.ADAPTER_CONFIG_REPORT
        ADAPTER_CONFIG_MANAGED = pm_import.ADAPTER_CONFIG_MANAGED

        @staticmethod
        def accept_adapter_config(dest_root, source_root, relpath, *,
                                  expected_sha256=None, root_identity=None):
            return pm_import.accept_adapter_config(
                dest_root, source_root, relpath, expected_sha256=expected_sha256,
                root_identity=root_identity)

    rc = pm_config.cmd_sync_adapter_config(
        _accept_args(source, accept=_SETTINGS_REL),
        pm_import=LegacyPmImport, dest_root=dest)

    out, err = capsys.readouterr()
    assert rc == 0, (out, err)  # TypeError 로 죽지 않는다.
    assert (dest / _SETTINGS_REL).read_text(encoding="utf-8") == _settings(git_anchor=True)
    assert "구세대라 훅 세트 세대 게이트 없이 진행한다" in err, err


def _previous_generation_pm_import(pm_import):
    """직전 세대 형제 — 신 API(`hook_set_accept_decision`)는 없고 구 blocker 판정만 있는 사본."""

    class PreviousGeneration:
        ADAPTER_CONFIG_REAPPROVAL_NOTE = pm_import.ADAPTER_CONFIG_REAPPROVAL_NOTE
        ADAPTER_CONFIG_REPORT = pm_import.ADAPTER_CONFIG_REPORT
        ADAPTER_CONFIG_MANAGED = pm_import.ADAPTER_CONFIG_MANAGED
        resolve_adapter_config_source = staticmethod(
            pm_import.resolve_adapter_config_source)
        judge_adapter_configs = staticmethod(pm_import.judge_adapter_configs)
        unconverged_managed_adapter_configs = staticmethod(
            pm_import.unconverged_managed_adapter_configs)

        @staticmethod
        def hook_set_accept_blockers(dest_root, source_root, relpath):
            """구 시그니처 — 설치본 선언으로 판정한 blockers 목록만 낸다(결속 없음)."""
            return pm_import.hook_set_accept_decision(
                dest_root, source_root, relpath,
                declarations=pm_import.hook_set_declarations(None)).blockers

        @staticmethod
        def accept_adapter_config(dest_root, source_root, relpath, *,
                                  expected_sha256=None, root_identity=None):
            return pm_import.accept_adapter_config(
                dest_root, source_root, relpath, expected_sha256=expected_sha256,
                root_identity=root_identity)

    return PreviousGeneration


def test_previous_generation_sibling_still_enforces_the_order_gate(
        pm_config, pm_import, tmp_path, capsys):
    """직전 세대 형제에서도 순서 게이트는 살아 있다 — 강등이 게이트를 끄면 그 창에 락아웃이 설치된다.

    신 API 부재를 "게이트 없음" 으로 접으면, 직전 세대에 실재하는 blocker 판정까지 버리는 셈이다."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=False),
        dest_wrapper=_WRAPPER_NEW, dest_driver=None,  # 드라이버 미설치 = 선행조건 미충족.
        template_settings=_settings(git_anchor=True))
    before = (dest / _SETTINGS_REL).read_text(encoding="utf-8")

    rc = pm_config.cmd_sync_adapter_config(
        _accept_args(source, accept=_SETTINGS_REL),
        pm_import=_previous_generation_pm_import(pm_import), dest_root=dest)

    err = capsys.readouterr().err
    assert rc == 1, err
    assert (dest / _SETTINGS_REL).read_text(encoding="utf-8") == before, \
        "강등 경로가 게이트를 통째로 꺼 미충족 config 를 앞세웠다"
    assert "수용 거부" in err and _DRIVER_REL in err, err
    assert "구세대라 상류 세대 선언·수용 스냅샷 결속 없이 진행한다" in err, err


def test_previous_generation_sibling_accepts_with_binding_omitted_warning(
        pm_config, pm_import, tmp_path, capsys):
    """선행조건이 충족되면 직전 세대에서도 수용은 진행되고, 결속 생략만 경고로 남는다."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=False),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_NEW,
        template_settings=_settings(git_anchor=True),
        ledger={_SETTINGS_REL: _ledger_entry(_settings(git_anchor=False))})

    rc = pm_config.cmd_sync_adapter_config(
        _accept_args(source, accept=_SETTINGS_REL),
        pm_import=_previous_generation_pm_import(pm_import), dest_root=dest)

    out, err = capsys.readouterr()
    assert rc == 0, (out, err)
    assert (dest / _SETTINGS_REL).read_text(encoding="utf-8") == _settings(git_anchor=True)
    assert "수용 거부" not in err, err
    assert "결속" in err and "구세대" in err, "결속 생략 사실이 안 보인다"


def test_check_downgrades_loudly_on_legacy_sibling_engine(pm_config, pm_import,
                                                          tmp_path, capsys):
    """`--check` 도 구세대 형제에서 죽지 않고 강등 사실을 알린다."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=True),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_NEW)

    class LegacyJudgePmImport:
        ADAPTER_CONFIG_REPORT = pm_import.ADAPTER_CONFIG_REPORT
        ADAPTER_CONFIG_MANAGED = pm_import.ADAPTER_CONFIG_MANAGED
        resolve_adapter_config_source = staticmethod(
            pm_import.resolve_adapter_config_source)
        judge_adapter_configs = staticmethod(pm_import.judge_adapter_configs)
        unconverged_managed_adapter_configs = staticmethod(
            pm_import.unconverged_managed_adapter_configs)
        hook_set_remedy_lines = staticmethod(pm_import.hook_set_remedy_lines)

        @staticmethod
        def judge_adapter_hook_sets(dest_root, source_root=None, harnesses=None):
            return pm_import.judge_adapter_hook_sets(dest_root, source_root, harnesses)

    rc = pm_config.cmd_sync_adapter_config(
        _accept_args(source, check=True), pm_import=LegacyJudgePmImport, dest_root=dest)

    err = capsys.readouterr().err
    assert rc == 0, err
    assert "구세대라 훅 세트 상류 선언 없이 진행한다" in err, err


def test_partial_guard_refuses_upstream_only_group_when_upstream_is_unreadable(
        pm_update, pm_import, tmp_path, monkeypatch, capsys):
    """상류만 아는 묶음 + 상류 미해소 — 로컬 묶음에 없어도 거부한다(fail-closed 계약).

    로컬 membership 으로 좁히면 정확히 이 조합이 빠져나간다: 로컬 선언은 그 묶음을 모르고, 상류
    선언은 읽을 수 없다. 그래서 판정 단위를 훅이 사는 **네임스페이스**로 올린다."""
    alpha, beta = _UPSTREAM_ONLY_GROUP
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=True),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_NEW,
        upstream_tail=None)  # 상류 pm_import 없음 = 세대 미해소.
    for rel in (alpha, beta):
        (source / rel).parent.mkdir(parents=True, exist_ok=True)
        (source / rel).write_text("# 신 세대\n", encoding="utf-8")
        (dest / rel).write_text("# 구 세대\n", encoding="utf-8")
    _write_source_manifest(source, [alpha, beta])
    monkeypatch.setattr(pm_update, "REPO", dest)

    assert pm_import.hook_set_partial_update([alpha], [alpha, beta]) == [], \
        "픽스처 전제 붕괴 — 로컬 선언이 그 묶음을 안다(구멍 재현 불가)"
    rc = pm_update.main(["--from", str(source), "--paths", alpha])

    err = capsys.readouterr().err
    assert rc == 1, err
    assert (dest / alpha).read_text(encoding="utf-8") == "# 구 세대\n"
    assert "어댑터 훅 영역의 부분 전파를 거부한다" in err and alpha in err, err


def test_partial_guard_is_fail_closed_when_upstream_generation_is_unreadable(
        pm_update, tmp_path, monkeypatch, capsys):
    """상류 세대를 못 읽는데 스코프가 훅 파일을 건드리면 거부한다(검증 불가 = 멈춤)."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=True),
        dest_wrapper=_WRAPPER_OLD, dest_driver=_DRIVER_OLD, upstream_tail=None)
    for rel in (_WRAPPER_REL, _DRIVER_REL):
        (source / rel).parent.mkdir(parents=True, exist_ok=True)
        (source / rel).write_text(
            (source / "templates" / "claude_code" / rel).read_text(encoding="utf-8"),
            encoding="utf-8")
    _write_source_manifest(source, [_WRAPPER_REL, _DRIVER_REL])
    monkeypatch.setattr(pm_update, "REPO", dest)

    rc = pm_update.main(
        ["--from", str(source), "--paths", _WRAPPER_REL, _DRIVER_REL])

    err = capsys.readouterr().err
    assert rc == 1, err
    assert (dest / _WRAPPER_REL).read_text(encoding="utf-8") == _WRAPPER_OLD
    assert "상류 훅 세트 세대 선언을 확인할 수 없어" in err, err


def test_partial_guard_is_loud_when_the_judgment_channel_is_absent(
        pm_update, monkeypatch, capsys):
    """판정 채널 자체가 없으면 가드가 꺼진 사실을 알린다 — 무진단 rc0 은 관측 불가다."""
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: None)

    rc = pm_update.refuse_partial_hook_set_scope(
        [(_WRAPPER_REL, "src", "dst", "M")], [(_WRAPPER_REL, "src", "dst", "M")])

    assert rc == 0, "판정 불가가 전파를 자기잠금하면 안 된다"
    assert "부분 전파 가드를 건너뛰었다" in capsys.readouterr().err


def test_partial_guard_leaves_non_hook_scope_alone_without_upstream(
        pm_update, tmp_path, monkeypatch, capsys):
    """훅과 무관한 부분 전파는 상류 세대를 못 읽어도 통과한다 — fail-closed 가 과잉이면 안 된다."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=True),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_NEW, upstream_tail=None)
    engine_rel = ".project_manager/tools/__t0610_sentinel__.py"
    (source / engine_rel).parent.mkdir(parents=True, exist_ok=True)
    (source / engine_rel).write_text("# 상류 엔진 파일\n", encoding="utf-8")
    _write_source_manifest(source, [engine_rel])
    monkeypatch.setattr(pm_update, "REPO", dest)

    rc = pm_update.main(["--from", str(source), "--paths", engine_rel])

    assert rc == 0, capsys.readouterr()
    assert (dest / engine_rel).read_text(encoding="utf-8") == "# 상류 엔진 파일\n"


def test_accept_all_excludes_unrecorded_because_edits_cannot_be_judged(
        pm_config, pm_import, tmp_path, capsys):
    """원장이 없어 편집 여부를 **판정할 수 없는** config 도 세트가 건드리지 않는다.

    원장 도입 전 구세대 채택자는 커스텀을 해 두고도 `unrecorded` 로 보인다 — 세트가 그것을 받으면
    편집분과 구분 못 한 채 커스텀이 사라진다(모르면 덮지 않는다)."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=False),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_NEW,
        template_settings=_settings(git_anchor=True))  # 원장 없음 → unrecorded.
    before = (dest / _SETTINGS_REL).read_text(encoding="utf-8")
    assert [item.status for item in pm_import.judge_adapter_configs(dest, source)] \
        == ["unrecorded"], "픽스처 전제(원장 부재 판정)가 아니다"

    rc = pm_config.cmd_sync_adapter_config(
        _accept_args(source, accept_all=True), pm_import=pm_import, dest_root=dest)

    out = capsys.readouterr().out
    assert (dest / _SETTINGS_REL).read_text(encoding="utf-8") == before, \
        "판정 불가 config 를 세트가 덮었다"
    assert rc == 1, out
    assert "세트 수용 제외(원장 부재로 편집 여부 판정 불가)" in out, out
    assert f"--accept {_SETTINGS_REL}" in out, "단건 수용 처방 부재"


def test_accept_all_reports_no_candidates_when_already_in_sync(pm_config, pm_import,
                                                               tmp_path, capsys):
    """이미 상류와 같으면 수용 후보가 없다 — byte churn 0(무의미한 백업 생성 금지)."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=True),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_NEW)

    rc = pm_config.cmd_sync_adapter_config(
        _accept_args(source, accept_all=True), pm_import=pm_import, dest_root=dest)

    assert rc == 0
    assert "대상 없음" in capsys.readouterr().out
    assert not (dest / ".pm_import_backups").exists(), "후보 0인데 백업을 만들었다"


def test_build_parser_routes_accept_all_and_excludes_other_modes(pm_config):
    """`--accept-all` 이 같은 커맨드로 라우팅되고 조회/단건 수용과 상호 배타다."""
    parser = pm_config.build_parser()
    parsed = parser.parse_args(["sync-adapter-config", "--accept-all"])

    assert parsed.func is pm_config.cmd_sync_adapter_config and parsed.accept_all is True
    with pytest.raises(SystemExit):
        parser.parse_args(["sync-adapter-config", "--accept-all", "--accept", _SETTINGS_REL])
    with pytest.raises(SystemExit):
        parser.parse_args(["sync-adapter-config", "--accept-all", "--check"])


# ── 구세대 형제 강등 사다리 · 선언 결속 · 강등 사유 표면화 (T-0611) ────────────
# T-0610 이 소비자를 상류 선언으로 통일하면서, 그 선언을 **못 받는** 조합의 처분이 한 갈래로
# 뭉쳤다: 형제가 구세대면 해소 지점이 없다는 이유로 그 세대가 이미 제공하던 보호(설치본 선언 기준
# 원자 write 판정·결합 묶음 판정·세대 불일치 판정)까지 함께 버린다. 이 절이 고정하는 성질:
#
#   조회 축은 **관대하되 loud**, mutation 축은 fail-closed — 그리고 어느 축이든 강등이
#   "직전 세대가 하던 일" 까지 끄지 않는다(강등은 사다리이지 스위치가 아니다).


def _t0606_generation_sibling(pm_import):
    """T-0606 세대 형제 사본 — 세대 선언 해소 지점(`hook_set_declarations`)이 아직 없고, 판정
    API 는 **선언 주입 이전 시그니처**만 가진 형상(혼합 세대 복구 중 실제로 열리는 창)."""

    class PreviousGeneration:
        hook_set_remedy_lines = staticmethod(pm_import.hook_set_remedy_lines)

        @staticmethod
        def is_live_hook_set_path(relpath):
            return pm_import.is_live_hook_set_path(relpath)

        @staticmethod
        def hook_set_partial_update(updated_paths, pending_paths):
            return pm_import.hook_set_partial_update(updated_paths, pending_paths)

        @staticmethod
        def judge_adapter_hook_sets(dest_root, source_root=None, harnesses=None):
            return pm_import.judge_adapter_hook_sets(dest_root, source_root, harnesses)

    return PreviousGeneration


def test_predicate_keeps_the_previous_generation_atomic_write_judgment(
        pm_update, pm_import, monkeypatch, capsys):
    """구세대 형제에서도 훅 파일은 원자 교체 대상으로 남는다 — 강등이 판정을 끄면 안 된다.

    선언 해소 지점이 없다는 이유로 무판정(`lambda: False`)으로 내려가면, 혼합 세대 복구 중에 훅
    파일이 통째로 비원자 copy2 로 떨어진다(T-0606 이 닫은 torn read 창이 그대로 다시 열린다)."""
    monkeypatch.setattr(pm_update, "_load_pm_import",
                        lambda: _t0606_generation_sibling(pm_import))

    predicate = pm_update.resolve_hook_set_predicate()

    assert predicate(_WRAPPER_REL), "구세대 형제가 아는 훅 경로까지 비원자 복사로 떨어졌다"
    assert not predicate(".project_manager/tools/board.py"), \
        "훅 세트 밖까지 원자 write 로 확대됐다"
    err = capsys.readouterr().err
    assert "구세대라 훅 세트 상류 세대 선언 없이 진행한다" in err, f"무음 강등: {err!r}"


def test_predicate_is_no_judgment_only_when_the_old_api_is_gone_too(
        pm_update, monkeypatch, capsys):
    """구 API 마저 없는 세대에서만 무판정이다 — 사다리의 마지막 단(도입 이전 경로)."""

    class BeforeHookSets:
        """훅 세트 개념 자체가 없는 사본."""

    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: BeforeHookSets)

    predicate = pm_update.resolve_hook_set_predicate()

    assert not predicate(_WRAPPER_REL)
    assert "원자 write 판정자를 해소하지 못했다" in capsys.readouterr().err


def test_partial_guard_keeps_the_previous_generation_coupled_group_gate(
        pm_update, pm_import, monkeypatch, capsys):
    """구세대 형제에서도 반쪽 갱신은 거부된다 — 새 키워드 TypeError 로 가드가 꺼지면 안 된다.

    그 세대가 이미 막던 락아웃(신 래퍼 + 구 드라이버)이 강등 한 번으로 rc0 통과가 된다."""
    monkeypatch.setattr(pm_update, "_load_pm_import",
                        lambda: _t0606_generation_sibling(pm_import))
    scoped = [(_WRAPPER_REL, None, None, "M")]
    planned = [(_WRAPPER_REL, None, None, "M"), (_DRIVER_REL, None, None, "M")]

    rc = pm_update.refuse_partial_hook_set_scope(scoped, planned, None)

    err = capsys.readouterr().err
    assert rc == 1, err
    assert "훅 세트를 반쪽만 갱신한다" in err and _DRIVER_REL in err, err
    assert "구세대라 훅 세트 상류 세대 선언 주입 없이 진행한다" in err, f"무음 강등: {err!r}"


def test_partial_guard_downgrade_still_lets_unrelated_scope_through(
        pm_update, pm_import, monkeypatch, capsys):
    """훅과 무관한 부분 전파는 강등 경로에서도 통과한다(가드가 과잉이면 복구가 잠긴다)."""
    monkeypatch.setattr(pm_update, "_load_pm_import",
                        lambda: _t0606_generation_sibling(pm_import))
    scoped = [(".project_manager/tools/board.py", None, None, "M")]

    rc = pm_update.refuse_partial_hook_set_scope(scoped, scoped, None)

    assert rc == 0, capsys.readouterr().err


def test_hook_set_check_keeps_the_previous_generation_judgment(
        pm_update, pm_import, tmp_path, monkeypatch, capsys):
    """구세대 형제에서도 세대 불일치 검사는 살아 있다 — `unavailable` 로 접으면 게이트가 사라진다.

    새 키워드를 그대로 넘기면 TypeError 가 나고, 그걸 fail-soft 로 받으면 판정 결과가 빈 목록이
    되어 완료 게이트가 green 이 된다(락아웃을 그대로 둔 채 성공 보고)."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=True),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_OLD)
    monkeypatch.setattr(pm_update, "_load_pm_import",
                        lambda: _t0606_generation_sibling(pm_import))

    result = pm_update.check_adapter_hook_sets(dest, source)

    err = capsys.readouterr().err
    assert result["status"] == "ok", result
    assert [finding["subject"] for finding in result["findings"]] == [_GIT_ANCHOR_FLAG], \
        result["findings"]
    assert pm_update._adapter_hook_set_gate_failed(result) is True
    assert "구세대라 훅 세트 상류 세대 선언 주입 없이 진행한다" in err, f"무음 강등: {err!r}"


# ── 선언 해시 ↔ 실행 선언 결속 (stale `.pyc` 창 폐쇄) ─────────────────────────
_STALE_PYC_FLAG_A = "--t0611-generation-a"
_STALE_PYC_FLAG_B = "--t0611-generation-b"


def _flag_declaration_tail(flag: str) -> str:
    """상류 선언에 플래그 하나를 더하는 꼬리 — 두 세대의 **bytes 길이가 같다**(플래그 길이 동일)."""
    return (
        "\n"
        'ADAPTER_HOOK_SET["claude"] = ADAPTER_HOOK_SET["claude"]._replace(\n'
        "    flag_support=dict(ADAPTER_HOOK_SET['claude'].flag_support, **{\n"
        f"        {flag!r}: ({_WRAPPER_REL!r}, {_DRIVER_REL!r})}}),\n"
        ")\n"
    )


def test_declaration_load_is_bound_to_the_hashed_bytes(pm_import, tmp_path):
    """해시한 bytes 와 **실행된 선언**이 같은 세대다 — timestamp 유효한 stale `.pyc` 폐쇄.

    엔진 전파는 `copy2`(mtime 보존)로 파일을 내려놓으므로, 크기까지 같은 사본이 앞 세대의 유효한
    `.pyc` 와 짝지어지는 창이 실재한다. 그 창에서 제자리 import 를 하면 "최신 파일 해시 + 구 선언
    코드" 가 성립해, 게이트가 결속했다고 믿는 스냅샷이 거짓이 된다(수용은 그 해시를 쓰기 직전에
    다시 대조하므로 거짓 통과가 그대로 설치된다)."""
    source = tmp_path / "framework"
    upstream = _plant_upstream_pm_import(
        source, extra_tail=_flag_declaration_tail(_STALE_PYC_FLAG_A))
    stat_before = upstream.stat()
    # 1) A 세대를 한 번 로드해 그 bytes 의 `.pyc` 를 남긴다.
    assert _STALE_PYC_FLAG_A in pm_import.hook_set_declarations(
        source, required=True).declarations["claude"].flag_support
    cached = Path(importlib.util.cache_from_source(str(upstream)))
    # 2) 같은 크기·같은 mtime 으로 B 세대를 덮는다(전파가 실제로 만드는 형상).
    upstream.write_text(
        (TOOLS / "pm_import.py").read_text(encoding="utf-8")
        + _flag_declaration_tail(_STALE_PYC_FLAG_B), encoding="utf-8")
    os.utime(upstream, ns=(stat_before.st_atime_ns, stat_before.st_mtime_ns))
    assert upstream.stat().st_size == stat_before.st_size, "픽스처 전제(같은 크기)가 아니다"

    generation = pm_import.hook_set_declarations(source, required=True)

    flags = generation.declarations["claude"].flag_support
    assert _STALE_PYC_FLAG_B in flags and _STALE_PYC_FLAG_A not in flags, \
        "해시한 bytes 가 아니라 캐시된 구 선언 코드가 실행됐다(결속 붕괴)"
    assert generation.source_sha256 == hashlib.sha256(
        upstream.read_bytes()).hexdigest(), "해시가 그 bytes 의 것이 아니다"
    if cached.is_file():
        # 민감도 — 이 fixture 가 실제로 stale `.pyc` 창을 재현하는지 확인한다(바이트코드 캐시가
        #   꺼진 환경이면 창 자체가 없어 위 단언이 공허해질 수 있다).
        stale = pm_import._load_module_from_path(
            upstream, "pm_import.py", allow_unverified=True)
        assert _STALE_PYC_FLAG_A in stale.ADAPTER_HOOK_SET["claude"].flag_support, \
            "제자리 import 가 구 선언을 재사용하지 않는다 — 창 재현 실패(단언이 공허하다)"


# ── 조회 축 강등 사유 표면화 (차단 아님·침묵만 제거) ─────────────────────────


def test_check_surfaces_the_query_axis_downgrade_reason(pm_config, pm_import,
                                                        tmp_path, capsys):
    """`--check` 는 상류 선언을 못 읽으면 그 사유를 낸다 — 무경고 green 이 곧 관측 불가다.

    조회 축은 관대 계약이라 rc 는 그대로다(차단은 mutation 축의 몫). 다만 침묵하면 채택자는 상류
    전용 플래그를 한 번도 판정받지 못한 green 을 정상 통과로 읽는다."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=True),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_NEW,
        upstream_tail=None)  # 상류 트리에 pm_import 없음 → 로컬 선언으로 강등.

    rc = pm_config.cmd_sync_adapter_config(
        _accept_args(source, check=True), pm_import=pm_import, dest_root=dest)

    err = capsys.readouterr().err
    assert rc == 0, err                      # 조회 축은 차단하지 않는다.
    assert "설치본 선언으로 판정한다" in err, f"강등 사유가 버려졌다: {err!r}"
    assert "상류 pm_import 부재" in err, "사유가 없어 무엇을 고쳐야 할지 알 수 없다"


def test_check_stays_quiet_when_the_upstream_generation_resolves(pm_config, pm_import,
                                                                 tmp_path, capsys):
    """상류 선언이 읽히면 조용하다 — 강등 경고가 상시 울리면 신호가 죽는다."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=True),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_NEW)

    rc = pm_config.cmd_sync_adapter_config(
        _accept_args(source, check=True), pm_import=pm_import, dest_root=dest)

    err = capsys.readouterr().err
    assert rc == 0, err
    assert "설치본 선언으로 판정한다" not in err, err


def test_sync_check_surfaces_the_query_axis_downgrade_reason(pm_update, tmp_path,
                                                             capsys):
    """pm-update 의 세대 검사도 같은 사유를 낸다 — 변경 0 실행이 무경고 green 이면 안 된다."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=True),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_NEW,
        upstream_tail=None)

    result = pm_update.check_adapter_hook_sets(dest, source)

    err = capsys.readouterr().err
    assert result["status"] == "ok" and result["findings"] == [], result
    assert "설치본 선언으로 판정한다" in err, f"강등 사유가 버려졌다: {err!r}"
    assert "상류 pm_import 부재" in err, err


def test_upstream_declarations_do_not_write_into_the_upstream_tree(pm_import, tmp_path):
    """상류 선언을 읽는 조회는 상류 트리에 아무것도 만들지 않는다 — 스테이징 소유자는 실행 clone.

    상류는 읽기 대상이다(읽기 전용 마운트·권한·볼륨 고갈이 실 형상). 자기 임시물을 거기 만들면
    읽기만 하는 조회가 상류의 쓰기 권한에 매이고, 사유로 강등되게 돼 있는 `required=False` 경로가
    대신 크래시한다 — 다른 상류 실패 모드(파일 부재·읽기 실패·모듈 손상)는 전부 사유로 내려간다."""
    source = tmp_path / "framework"
    upstream = _plant_upstream_pm_import(source)
    # 실 설치본은 형제 엔진 모듈을 함께 갖는다(engine.manifest 가 같은 디렉터리로 출하한다).
    (upstream.parent / "pm_relay.py").write_text(
        (TOOLS / "pm_relay.py").read_text(encoding="utf-8"), encoding="utf-8")

    generation = pm_import.hook_set_declarations(source, required=True)

    assert generation.origin == pm_import.HOOK_SET_ORIGIN_UPSTREAM, generation.reasons
    assert generation.declarations, generation.reasons
    assert not (source / ".project_manager" / ".local").exists(), \
        "읽기만 해야 할 상류 트리에 스테이징을 만들었다(앵커가 상류다)"


def _load_pm_import_at(path: Path, name: str):
    """그 경로의 pm_import 사본을 **실행 중인 엔진**으로 적재한다(자기 자신 경로 재현)."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_same_path_generation_is_loaded_from_the_hashed_bytes(tmp_path):
    """상류가 **실행 중인 그 파일**이어도 선언은 해시한 bytes 에서 읽는다(빠른 경로 특례 없음).

    "자기 자신이면 메모리 적재 선언을 쓴다" 는 단축은 정확히 같은 결속 붕괴를 남긴다: 적재된 선언은
    import 시점 코드인데(그 자체가 stale `.pyc` 였을 수 있다) 해시는 지금 디스크 bytes 의 것이다.
    자기 갱신 실행은 실행 도중 그 파일을 덮으므로(pm-update 가 하는 일이 그것이다) 두 시점이 갈리는
    창이 상시 열려 있고, 그 조합이 곧 게이트가 결속했다고 믿는 거짓 스냅샷이다."""
    source = tmp_path / "framework"
    upstream = _plant_upstream_pm_import(
        source, extra_tail=_flag_declaration_tail(_STALE_PYC_FLAG_A))
    # 여기서는 상류 사본이 곧 **실행 중인 엔진**이다. 실 설치본은 형제 엔진 모듈을 함께 갖고,
    # 스테이징 자리를 형제(`pm_relay.temp_root`)에게 묻는 경로가 그 형제를 실제로 로드한다.
    (upstream.parent / "pm_relay.py").write_text(
        (TOOLS / "pm_relay.py").read_text(encoding="utf-8"), encoding="utf-8")
    running = _load_pm_import_at(upstream, "pm_import_selfpath")
    assert Path(running.__file__).resolve() == upstream.resolve(), \
        "픽스처 전제 붕괴 — 상류가 실행 중인 그 파일이 아니다(빠른 경로 미도달)"
    assert _STALE_PYC_FLAG_A in running.ADAPTER_HOOK_SET["claude"].flag_support, \
        "픽스처 전제(적재 세대 = A)"
    # 실행 중에 그 파일이 새 세대로 덮인다 — 자기 갱신 sync 가 만드는 형상 그대로.
    upstream.write_text(
        (TOOLS / "pm_import.py").read_text(encoding="utf-8")
        + _flag_declaration_tail(_STALE_PYC_FLAG_B), encoding="utf-8")

    generation = running.hook_set_declarations(source, required=True)

    flags = generation.declarations["claude"].flag_support
    assert _STALE_PYC_FLAG_B in flags and _STALE_PYC_FLAG_A not in flags, \
        "자기 자신 빠른 경로가 메모리 적재(구) 선언을 신 해시와 짝지었다(결속 붕괴)"
    assert generation.origin == running.HOOK_SET_ORIGIN_UPSTREAM, generation.origin
    assert generation.source_sha256 == hashlib.sha256(
        upstream.read_bytes()).hexdigest(), "해시가 그 bytes 의 것이 아니다"


def _namespace_aware_legacy_sibling(pm_import):
    """네임스페이스 열거 채널은 있으나 **선언 주입은 없는** 사본.

    강등 분기가 "구세대라서" 라는 이유로 fail-closed 검사까지 끄는지 보는 형상이다 — 강등은 그
    세대가 아는 판정을 유지할 뿐, 검증 불가 상태의 처분을 바꾸지 않는다."""

    class NamespaceAwareLegacy:
        hook_set_namespaces = staticmethod(pm_import.hook_set_namespaces)
        hook_set_remedy_lines = staticmethod(pm_import.hook_set_remedy_lines)

        @staticmethod
        def is_live_hook_set_path(relpath):
            return pm_import.is_live_hook_set_path(relpath)

        @staticmethod
        def hook_set_partial_update(updated_paths, pending_paths):
            return pm_import.hook_set_partial_update(updated_paths, pending_paths)

    return NamespaceAwareLegacy


def test_partial_guard_downgrade_still_fails_closed_over_the_hook_namespace(
        pm_update, pm_import, monkeypatch, capsys):
    """구세대 강등 분기도 상류 미확인 상태의 훅 영역 전파는 거부한다 — 인식 상태가 같으면 처분도 같다.

    구 판정자는 **상류에만 추가된 결합 묶음**을 정의상 모른다. 강등을 이유로 네임스페이스
    fail-closed 를 건너뛰면 그 첫 전파(정확히 반쪽 갱신이 나는 자리)가 이 분기로 빠져나간다."""
    alpha, beta = _UPSTREAM_ONLY_GROUP
    monkeypatch.setattr(pm_update, "_load_pm_import",
                        lambda: _namespace_aware_legacy_sibling(pm_import))
    assert pm_import.hook_set_partial_update([alpha], [alpha, beta]) == [], \
        "픽스처 전제 붕괴 — 설치본 선언이 이미 그 묶음을 안다(구멍 재현 불가)"

    rc = pm_update.refuse_partial_hook_set_scope(
        [(alpha, None, None, "M")],
        [(alpha, None, None, "M"), (beta, None, None, "M")], None)

    err = capsys.readouterr().err
    assert rc == 1, err
    assert "어댑터 훅 영역의 부분 전파를 거부한다" in err and alpha in err, err
    assert "스코프 없이 pm-update" in err, "탈출구 처방 부재"


def test_partial_guard_downgrade_fail_closed_spares_non_hook_paths(
        pm_update, pm_import, monkeypatch, capsys):
    """강등 분기의 fail-closed 도 훅 밖 경로는 건드리지 않는다(복구 전파 자기잠금 금지)."""
    monkeypatch.setattr(pm_update, "_load_pm_import",
                        lambda: _namespace_aware_legacy_sibling(pm_import))
    scoped = [(".project_manager/tools/board.py", None, None, "M")]

    rc = pm_update.refuse_partial_hook_set_scope(scoped, scoped, None)

    assert rc == 0, capsys.readouterr().err


def test_staging_cleanup_failure_does_not_fail_the_declaration_load(
        pm_import, tmp_path, monkeypatch):
    """스테이징 뒷정리 실패가 상류 로드 실패로 번역되지 않는다 — 선언은 이미 읽혔다.

    삭제 실패는 Windows 실 클래스다(핸들 잠금·AV 스캔). 그것을 로드 경계 안에 묶으면 사유 한 줄이
    `declarations=None` 으로 번역되고, mutation 게이트가 **근거 없이** fail-closed 로 떨어진다."""
    source = tmp_path / "framework"
    _plant_upstream_pm_import(
        source, extra_tail=_flag_declaration_tail(_STALE_PYC_FLAG_A))
    leaked: list[str] = []
    real_rmtree = pm_import.shutil.rmtree

    def _locked_rmtree(path, *args, **kwargs):
        leaked.append(str(path))
        raise OSError(f"임시 디렉토리 정리 실패(주입): {path}")

    monkeypatch.setattr(pm_import.shutil, "rmtree", _locked_rmtree)
    try:
        generation = pm_import.hook_set_declarations(source, required=True)
    finally:
        monkeypatch.undo()
        for path in leaked:
            real_rmtree(path, ignore_errors=True)

    assert leaked, "스테이징 정리 경로에 도달하지 않았다(단언이 공허하다)"
    assert generation.declarations is not None, generation.reasons
    assert _STALE_PYC_FLAG_A in generation.declarations["claude"].flag_support
    assert generation.origin == pm_import.HOOK_SET_ORIGIN_UPSTREAM, generation.origin


def test_query_fallback_wording_has_one_owner(pm_import, pm_update, pm_config,
                                              tmp_path, capsys):
    """강등 안내 문구는 pm_import 단일 진실이고 두 소비자가 **같은 문장**을 낸다.

    사본을 두면 한쪽만 고쳐지고, 채택자는 같은 상태를 서로 다른 말로 듣는다(게이트가 둘로 보인다)."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=True),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_NEW,
        upstream_tail=None)  # 상류 pm_import 부재 → 조회 축 강등.
    owned = pm_import.hook_set_query_fallback_lines(
        pm_import.hook_set_declarations(source))
    assert owned and "설치본 선언으로 판정한다" in owned[0], owned

    pm_update.check_adapter_hook_sets(dest, source)
    sync_err = capsys.readouterr().err
    pm_config.cmd_sync_adapter_config(
        _accept_args(source, check=True), pm_import=pm_import, dest_root=dest)
    check_err = capsys.readouterr().err

    assert owned[0] in sync_err, sync_err
    assert owned[0] in check_err, check_err
    assert pm_import.hook_set_query_fallback_lines(
        pm_import.hook_set_declarations(REPO)) == [], "해소되면 안내가 없어야 한다"


# ── 커맨드 표기 커버리지 · 문구·API 정리 (T-0612) ─────────────────────────────
# 판정 단위는 **실행되는 스크립트와 그 플래그**다. 표기(인터프리터 선행·플랫폼별 키)가 달라도 같은
# 훅이면 같은 판정을 타야 한다 — 표기 하나로 판정에서 빠지면 그 형상의 채택자만 락아웃을 그대로 받는다.


def test_interpreter_prefixed_command_is_judged_like_a_direct_one(pm_import, tmp_path):
    """`bash <path> --flag` 표기도 직접 실행 표기와 **같은 판정**을 탄다.

    첫 토큰만 보면 인터프리터를 스크립트로 읽어 훅 세트 밖으로 접고, 그 config 의 세대 요구가
    통째로 판정 밖이 된다(채택자가 표기를 바꿨다는 이유로 게이트가 사라진다)."""
    direct = _settings(git_anchor=True)
    interpreted = direct.replace(
        "${CLAUDE_PROJECT_DIR}/" + _WRAPPER_REL,
        "bash ${CLAUDE_PROJECT_DIR}/" + _WRAPPER_REL)
    dest, source = _make_case(
        tmp_path, dest_settings=interpreted,
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_OLD)   # 락아웃 조합.
    assert "bash ${CLAUDE_PROJECT_DIR}" in interpreted, "픽스처 전제(인터프리터 선행 표기)"

    findings = pm_import.judge_adapter_hook_sets(dest, source)

    assert [(f.kind, f.subject, f.unmet_paths) for f in findings] == [
        (pm_import.HOOK_SET_UNSUPPORTED_FLAG, _GIT_ANCHOR_FLAG, (_DRIVER_REL,))], findings


def test_interpreter_options_before_the_script_are_skipped(pm_import):
    """인터프리터 옵션(`py -3.12 <path>`)을 건너뛰고 스크립트를 집는다 — 인자도 그 뒤부터."""
    script, arguments = pm_import._hook_script_and_arguments(
        ["py", "-3.12", "${CLAUDE_PROJECT_DIR}/" + _WRAPPER_REL, _GIT_ANCHOR_FLAG])

    assert script == _WRAPPER_REL and arguments == [_GIT_ANCHOR_FLAG]


def test_non_interpreter_first_token_keeps_the_direct_reading(pm_import):
    """인터프리터 목록 밖 첫 토큰은 종전대로 스크립트로 읽는다(과잉 해석 금지)."""
    assert pm_import._hook_script_and_arguments(
        ["./" + _WRAPPER_REL, _GIT_ANCHOR_FLAG]) == (_WRAPPER_REL, [_GIT_ANCHOR_FLAG])
    assert pm_import._hook_script_and_arguments(["bash"]) == (None, [])


def test_windows_command_key_is_scanned_like_the_posix_one(pm_import, tmp_path):
    """`commandWindows` 도 같은 스캔에 든다 — 그 플랫폼에서 실제로 실행되는 커맨드다.

    한쪽만 보면 Windows 채택자의 세대 불일치(같은 락아웃)가 통째로 판정 밖이 된다."""
    # Windows 쪽만 그 플래그를 넘긴다(플랫폼별로 다른 세대를 부르는 형상) — 인터프리터 선행 표기도
    #   함께 태운다(두 항목이 같은 스캔·같은 해소를 쓴다).
    document = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{
        "type": "command",
        "command": "${CLAUDE_PROJECT_DIR}/" + _WRAPPER_REL,
        "commandWindows": ("bash ${CLAUDE_PROJECT_DIR}/" + _WRAPPER_REL
                           + " " + _GIT_ANCHOR_FLAG),
    }]}]}}
    dest, _source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=False),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_OLD)

    commands = pm_import._hook_commands(document)
    unmet = pm_import._hook_set_demands(
        document, pm_import.ADAPTER_HOOK_SET["claude"], dest)

    assert len(commands) == 2, commands
    assert unmet == {(pm_import.HOOK_SET_UNSUPPORTED_FLAG, _GIT_ANCHOR_FLAG):
                     (_DRIVER_REL,)}, \
        "Windows 커맨드만 요구하는 플래그가 판정에서 빠졌다"


def test_remedy_line_omits_empty_parentheses(pm_import, tmp_path):
    """지목할 파일이 없는 소견은 빈 괄호 `()` 없이 처방한다 — 깨진 문장을 내지 않는다.

    상류 세대 미해소 blocker 가 그 형상이다(`unmet_paths=()`) — 무엇이 미충족인지 열거할 수 없다."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=True),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_NEW,
        upstream_tail=None)  # 상류 pm_import 부재 → 해소 불가 blocker.
    decision = pm_import.hook_set_accept_decision(
        dest, source, _SETTINGS_REL,
        declarations=pm_import.hook_set_declarations(source, required=True))
    blocker = decision.blockers[0]
    assert blocker.unmet_paths == (), blocker

    lines = pm_import.hook_set_remedy_lines(blocker)

    assert lines and all("()" not in line for line in lines), lines
    assert any("먼저 받아라" in line for line in lines), lines
    # 경계 반대편 — 지목할 파일이 있으면 종전대로 괄호로 열거한다.
    stale = pm_import.judge_adapter_hook_sets(*_make_case(
        tmp_path / "stale", dest_settings=_settings(git_anchor=True),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_OLD))[0]
    assert f"({_DRIVER_REL})" in pm_import.hook_set_remedy_lines(stale)[0]


def test_coupled_hook_set_paths_takes_declarations_keyword_only(pm_import):
    """세대 선언은 형제 API 전부와 같이 **kw-only** 다 — 위치인자 표기가 하나만 다르면 안 된다."""
    alpha = pm_import.ADAPTER_HOOK_SET["claude"].coupled_groups[0][0]

    assert pm_import.coupled_hook_set_paths([alpha]) == (alpha,)
    assert pm_import.coupled_hook_set_paths(
        [alpha], declarations=pm_import.ADAPTER_HOOK_SET) == (alpha,)
    with pytest.raises(TypeError):
        pm_import.coupled_hook_set_paths([alpha], pm_import.ADAPTER_HOOK_SET)


def test_generation_and_sibling_resolve_with_one_load(pm_update, monkeypatch):
    """세대 해소와 판정자 구성이 형제 사본을 **한 번만** 적재한다.

    `_load_pm_import` 는 캐시가 없다 — 두 번 부르면 같은 실행에서 사본을 두 번 exec 하고, 두 번째
    로드는 첫 로드가 등록 경계로 흡수한 skew 를 경계 밖에서 다시 올릴 수 있다."""
    real = pm_update._load_pm_import
    loads: list[int] = []

    def _counted():
        loads.append(1)
        return real()

    monkeypatch.setattr(pm_update, "_load_pm_import", _counted)
    predicate = pm_update.resolve_hook_set_predicate()

    assert predicate(_WRAPPER_REL), "판정자가 훅 경로를 놓쳤다(해소 실패)"
    assert len(loads) == 1, f"형제 사본을 {len(loads)}회 적재했다"
