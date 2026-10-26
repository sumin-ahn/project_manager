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

from pm_engine.domain import install_rules

import hashlib
import importlib.util
import json
import os
import py_compile
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from _repo_owned_inventory import TRACKED_ONLY, repo_owned_paths
from pm_engine.app import adapter_accept
from pm_engine.app import adapter_report
from pm_engine.app import hook_sets
from pm_engine.domain import adapter_specs
from pm_engine.infra import adapter_baseline
from pm_engine.infra import adapter_documents
from pm_engine.infra import install_conf
from pm_engine.infra import pm_paths
from pm_engine.infra import raw_output
from pm_engine.infra import upstream_declarations
from pm_engine.app import update_adapters
from pm_engine.app import update_render
from pm_engine.infra import engine_transfer as engine_copy_io
from pm_engine.cli import config_adapter
from pm_engine.cli import update_cmd

pytestmark = pytest.mark.usefixtures("known_ctx_io")

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
def _pm_update_cli():
    return _load("pm_update", "pm_update.py")


@pytest.fixture
def pm_update(_pm_update_cli, monkeypatch):
    from _manager_home import complete_in_process
    with complete_in_process(monkeypatch):
        yield _pm_update_cli


@pytest.fixture(scope="module")
def pm_config():
    return _load("pm_config", "pm_config.py")


# 출하 template 의 압축 지점 관리 키 값 — 수용 뒤 반영(`harness_ctx.apply_harness_ctx`)은 기본
#   local.conf 에서 이 값을 그대로 두므로, 합성 settings 도 같은 값을 실어 "수용 결과 == template"
#   대조가 훅 세트 축만 재게 한다.
_TEMPLATE_SETTINGS = json.loads(
    (REPO / "templates" / "claude_code" / _SETTINGS_REL).read_text(encoding="utf-8"))
_MANAGED_SETTINGS = {key: _TEMPLATE_SETTINGS[key] for key in ("autoCompactWindow", "idleCompaction")}


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
    return json.dumps({"hooks": {"PreToolUse": groups}, **_MANAGED_SETTINGS},
                      ensure_ascii=False, indent=2) + "\n"


_UPSTREAM_PACKAGE_REL = ".project_manager/tools/pm_engine"
_UPSTREAM_DECLARATION_REL = _UPSTREAM_PACKAGE_REL + "/domain/adapter_specs.py"


def _plant_upstream_package(source: Path, *, extra_tail: str = "") -> Path:
    """상류 트리에 실제 엔진 package 사본을 놓는다 — 게이트가 **상류 세대 선언**을 읽는 전제.

    실 package 를 그대로 복사해야 "상류 세대 == 현행 선언" 이라는 기본 형상이 되고(게이트 통과),
    `extra_tail` 로 `domain/adapter_specs.py` 에 선언을 덧붙이면 **상류만 아는 세대**가 된다(설치본은
    모르는 묶음/경로). 반환값은 선언 파일 경로다. 다시 부르면 package 를 새 세대로 갈아 끼운다."""
    target = source / _UPSTREAM_PACKAGE_REL
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(TOOLS / "pm_engine", target,
                    ignore=shutil.ignore_patterns("__pycache__"))
    declaration = source / _UPSTREAM_DECLARATION_REL
    declaration.write_text(
        (TOOLS / "pm_engine" / "domain" / "adapter_specs.py").read_text(encoding="utf-8")
        + extra_tail, encoding="utf-8")
    # 선언 snapshot 은 tracked-only 배송 열거를 쓴다 — 상류 사본을 자기 git 추적 트리로 만든다.
    subprocess.run(["git", "-C", str(source), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(source), "add", "-f", "-A", "--", _UPSTREAM_PACKAGE_REL],
                   check=True)
    return declaration


def _run_home(tmp_path: Path):
    """선언 조회를 실행하는 Home — 상류 source 와 다른 임시 clone 이다(스테이징이 여기 생긴다)."""
    return pm_paths.home_at(tmp_path / "run_home")


def _make_case(tmp_path: Path, *, dest_settings: str, dest_wrapper: str | None,
               dest_driver: str | None, template_settings: str | None = None,
               template_wrapper: str = _WRAPPER_NEW,
               template_driver: str = _DRIVER_NEW,
               ledger: dict | None = None,
               upstream_tail: str | None = "") -> tuple[Path, Path]:
    """(dest, source) — claude 채택자 + 합성 프레임워크.

    `dest_wrapper`/`dest_driver` 를 None 으로 주면 그 파일이 **설치되지 않은** 형상이다.
    `ledger` 는 어댑터 원장(설치 시점 해시) — `edited` 판정을 만들려면 dest 와 다른 해시를 준다.
    `upstream_tail` 은 상류 선언 파일에 덧붙일 선언(None 이면 package 자체를 놓지 않는다 —
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
        _plant_upstream_package(source, extra_tail=upstream_tail)

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


def test_new_config_with_old_driver_is_detected_as_lockout(tmp_path):
    """신 settings + 구 드라이버 = 락아웃 조합 → 미지원 플래그로 검출된다.

    이게 v1.7.0 흡수에서 실제로 터진 조합이다. 검출 못 하면 채택자는 Bash 전면 차단 상태로
    pm-update 를 green 으로 받는다(성공 보고 + 잠긴 하네스)."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=True),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_OLD)

    findings = hook_sets.judge_adapter_hook_sets(dest, source)

    assert len(findings) == 1, findings
    finding = findings[0]
    assert finding.kind == hook_sets.HOOK_SET_UNSUPPORTED_FLAG
    assert finding.subject == _GIT_ANCHOR_FLAG
    assert finding.unmet_paths == (_DRIVER_REL,), \
        "래퍼는 신세대인데 드라이버까지 미충족으로 뭉뚱그렸다"
    assert finding.remedy == hook_sets.HOOK_SET_REMEDY_ENGINE_STALE


def test_old_config_with_new_driver_is_not_flagged(tmp_path):
    """구 settings + 신 드라이버는 훅 미발화라 무해 — 검사가 조용해야 한다(거짓 red 금지)."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=False),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_NEW)

    assert hook_sets.judge_adapter_hook_sets(dest, source) == []


def test_same_generation_set_is_silent(tmp_path):
    """양쪽 모두 신세대면 판정이 비어 있다."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=True),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_NEW)

    assert hook_sets.judge_adapter_hook_sets(dest, source) == []


def test_old_wrapper_alone_is_detected(tmp_path):
    """dispatch 하는 래퍼가 구세대면 그 파일이 미충족으로 잡힌다(체인 전체가 판정 대상)."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=True),
        dest_wrapper=_WRAPPER_OLD, dest_driver=_DRIVER_NEW)

    findings = hook_sets.judge_adapter_hook_sets(dest, source)

    assert [f.unmet_paths for f in findings] == [(_WRAPPER_REL,)], findings


def test_missing_hook_script_is_detected(tmp_path):
    """config 가 부르는 래퍼가 아예 없으면 그것도 같은 락아웃 클래스다(훅이 매번 실패)."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=True),
        dest_wrapper=None, dest_driver=_DRIVER_NEW)

    findings = hook_sets.judge_adapter_hook_sets(dest, source)

    assert [f.kind for f in findings] == [hook_sets.HOOK_SET_MISSING_SCRIPT], findings
    assert findings[0].unmet_paths == (_WRAPPER_REL,)


def test_adopter_own_hook_script_is_out_of_judgment_scope(tmp_path):
    """채택자 자작 훅은 엔진 소관 밖 — 부재해도 처방하지 않는다(거짓 red 금지)."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=False,
                                          wrapper_rel=".claude/my_own_hook.sh"),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_NEW)

    assert hook_sets.judge_adapter_hook_sets(dest, source) == []


def test_config_ahead_of_engine_prescribes_accept_not_pm_update(tmp_path):
    """상류 template 조차 그 플래그를 못 주면 config 가 앞선 것 → 처방이 수용 채널로 갈린다.

    처방이 갈리지 않으면 채택자는 절대 끝나지 않는 pm-update 를 반복한다(엔진엔 줄 게 없다)."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=True),
        dest_wrapper=_WRAPPER_OLD, dest_driver=_DRIVER_OLD,
        template_settings=_settings(git_anchor=False),
        template_wrapper=_WRAPPER_OLD, template_driver=_DRIVER_OLD)

    findings = hook_sets.judge_adapter_hook_sets(dest, source)

    assert [f.remedy for f in findings] == [hook_sets.HOOK_SET_REMEDY_CONFIG_AHEAD], findings
    lines = " ".join(hook_sets.hook_set_remedy_lines(findings[0]))
    assert "sync-adapter-config --accept" in lines and _SETTINGS_REL in lines


def test_engine_stale_prescribes_pm_update_with_the_unmet_files(tmp_path):
    """dest 엔진 파일이 뒤처진 경우의 처방은 pm-update + 미충족 파일 실값이다."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=True),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_OLD)

    lines = " ".join(hook_sets.hook_set_remedy_lines(
        hook_sets.judge_adapter_hook_sets(dest, source)[0]))

    assert "pm-update" in lines and _DRIVER_REL in lines


def test_missing_source_root_falls_back_to_both_prescriptions(tmp_path):
    """비교 기준이 없으면 처방을 단정하지 않는다(엔진 선행을 먼저 권하되 수용 채널도 남긴다)."""
    dest, _source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=True),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_OLD)

    findings = hook_sets.judge_adapter_hook_sets(dest, None)

    assert [f.remedy for f in findings] == [hook_sets.HOOK_SET_REMEDY_UNKNOWN]
    lines = hook_sets.hook_set_remedy_lines(findings[0])
    assert "pm-update" in lines[0] and "sync-adapter-config --accept" in lines[1]


def test_broken_config_json_is_not_a_finding(tmp_path):
    """파손된 채택자 config 는 판정 불가일 뿐 훅 세대 red 가 아니다(엉뚱한 처방 금지)."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=True),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_OLD)
    (dest / _SETTINGS_REL).write_text("{ 깨진 json", encoding="utf-8")

    assert hook_sets.judge_adapter_hook_sets(dest, source) == []


# ── 선언 정합 (하드코딩 claude 전용 방지) ─────────────────────────────────────


def test_every_registered_harness_declares_a_hook_set():
    """등록 하네스 전수가 훅 세트 선언을 갖는다 — 네 번째 하네스가 조용히 미검사로 못 떨어진다."""
    assert set(adapter_specs.ADAPTER_HOOK_SET) == set(install_rules.REGISTERED_HARNESSES)


def test_flag_support_targets_are_declared_live_files():
    """플래그가 요구하는 파일은 전부 그 하네스의 훅 세트 파일이어야 한다(선언 두 벌 방지).

    밖의 경로를 요구하면 "검사는 하는데 동기가 원자 write 하지 않는" 파일이 생긴다."""
    for harness, spec in adapter_specs.ADAPTER_HOOK_SET.items():
        for flag, required in spec.flag_support.items():
            for relpath in required:
                assert hook_sets._is_hook_set_file(relpath, spec), \
                    f"{harness}:{flag} 가 훅 세트 밖 파일을 요구한다 ({relpath})"
                assert hook_sets.is_live_hook_set_path(relpath), \
                    f"{harness}:{flag} 요구 파일이 원자 write 대상이 아니다 ({relpath})"
        for group in spec.coupled_groups:
            assert len(group) > 1, f"{harness}: 원소 1개짜리 결합 묶음은 의미가 없다 ({group})"
            for relpath in group:
                assert relpath in spec.live_files, \
                    f"{harness}: 결합 묶음이 선언 밖 경로를 담았다 ({relpath})"


def test_flag_support_chain_moves_as_one_coupled_group():
    """플래그 체인은 반드시 한 결합 묶음 안에 있다 — 부분 전파로 갈릴 수 있는 축이 곧 그 체인이다."""
    for harness, spec in adapter_specs.ADAPTER_HOOK_SET.items():
        for flag, required in spec.flag_support.items():
            assert any(set(required) <= set(group) for group in spec.coupled_groups), \
                f"{harness}:{flag} 체인이 어느 결합 묶음에도 통째로 들어 있지 않다 ({required})"


def test_shipped_claude_template_hook_set_is_self_consistent():
    """출하 template 자체가 세대 정합이다 — settings.json 에 플래그를 더하고 드라이버를 안 고치면 red.

    합성 픽스처만으로는 실제 출하물의 재발을 못 막는다(이 클래스의 도입 커밋 T-0587 이 정확히
    settings.json + 래퍼 + 드라이버 동시 변경이었다)."""
    template_root = REPO / "templates" / "claude_code"
    spec = adapter_specs.ADAPTER_HOOK_SET["claude"]
    document = json.loads(
        (template_root / spec.config_relpath).read_text(encoding="utf-8"))

    unmet = hook_sets._hook_set_demands(document, spec, template_root)

    assert unmet == {}, f"출하 template 의 훅 세트가 자기 config 를 감당하지 못한다: {unmet}"


def test_shipped_claude_template_flag_demand_is_not_vacuous(tmp_path):
    """출하 settings.json 이 **실제로** 세대 플래그를 요구한다 — 요구 0 이면 자기정합 단언이 공허하다.

    `unmet == {}` 는 요구가 하나도 없을 때도 green 이다. 배선이 진입점 뒤로 접히면서 config 가
    플래그를 안 넘기게 되면 그 순간 세대 결합 가드(v1.7.0 락아웃 클래스)가 조용히 무력해진다 —
    구세대 드라이버를 심어 요구가 표면에 뜨는지 본다."""
    template_root = REPO / "templates" / "claude_code"
    spec = adapter_specs.ADAPTER_HOOK_SET["claude"]
    document = json.loads(
        (template_root / spec.config_relpath).read_text(encoding="utf-8"))
    stale = tmp_path / "stale"
    (stale / ".claude").mkdir(parents=True)
    for relpath in spec.live_files:
        (stale / relpath).write_text("# 구세대 사본\n", encoding="utf-8")

    unmet = hook_sets._hook_set_demands(document, spec, stale)

    assert (hook_sets.HOOK_SET_UNSUPPORTED_FLAG, _GIT_ANCHOR_FLAG) in unmet, \
        f"출하 settings.json 이 {_GIT_ANCHOR_FLAG} 를 요구하지 않는다(세대 결합 가드 공허): {unmet}"


def test_codex_entrypoint_migration_left_claude_wiring_untouched():
    """codex 진입점 마이그레이션(T-0777)이 claude 배선을 건드리지 않았다.

    claude 는 `PreToolUse matcher=*` 진입점을 이미 갖고 있어 흡수 이득이 없고, 기존 `Bash`
    블록을 지우면 세대 결합 가드가 공허해지며 발화 스코프가 전 도구로 넓어진다. 그래서 이
    티켓의 비목표였고, 그 사실을 값으로 고정한다."""
    document = json.loads(
        (REPO / "templates" / "claude_code" / _SETTINGS_REL).read_text(encoding="utf-8"))
    groups = document["hooks"]["PreToolUse"]

    assert [group["matcher"] for group in groups] == [
        "*", "Bash|Write|Edit|MultiEdit|NotebookEdit|Agent|Task", "Agent"]
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
    adapter_specs.ADAPTER_HOOK_SET["codex"].entrypoints)
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
                        ("^(Bash|apply_patch|collaborationspawn_agent)$"
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


def test_entrypoint_declaration_targets_are_declared_live_files():
    """진입점이 부르는 디스패처는 전부 그 하네스의 훅 세트 파일이어야 한다(선언 두 벌 방지).

    밖의 경로를 선언하면 "판정은 하는데 동기가 원자 write 하지 않는" 파일이 생긴다 —
    `flag_support` 축과 같은 근거다."""
    seen_any = False
    for harness, spec in adapter_specs.ADAPTER_HOOK_SET.items():
        events = [entrypoint.event for entrypoint in spec.entrypoints]
        assert len(events) == len(set(events)), \
            f"{harness}: 한 이벤트에 진입점을 두 번 선언했다 ({events})"
        for entrypoint in spec.entrypoints:
            seen_any = True
            assert spec.config_relpath, \
                f"{harness}: config 가 없는 하네스에 진입점을 선언했다({entrypoint.event})"
            assert hook_sets._is_hook_set_file(entrypoint.dispatcher, spec), \
                f"{harness}:{entrypoint.event} 가 훅 세트 밖 디스패처를 부른다"
            assert hook_sets.is_live_hook_set_path(entrypoint.dispatcher), \
                f"{harness}:{entrypoint.event} 디스패처가 원자 write 대상이 아니다"
    assert seen_any, "진입점 선언이 하나도 없다(공허 가드)"


@pytest.mark.parametrize("harness,template_dir",
                         (("claude", "claude_code"), ("codex", "codex"),
                          ("opencode", "opencode")))
def test_shipped_template_satisfies_its_entrypoint_declaration(
        harness, template_dir):
    """출하 template 이 자기 진입점 선언을 만족한다 — 배선을 지우면 그 자리에서 red.

    합성 픽스처만으로는 실 출하물의 재발을 못 막는다(claude 축의 자기정합 단언과 같은 근거)."""
    findings = hook_sets.judge_adapter_hook_entrypoints(
        REPO / "templates" / template_dir, REPO, [harness])

    assert findings == [], [finding.detail for finding in findings]


def test_missing_entrypoint_is_reported_with_the_event_named(tmp_path):
    """진입점을 하나 빼면 그 이벤트를 **지목**한다(민감도) — 나머지는 조용하다."""
    dropped = "UserPromptSubmit"
    dest = _make_codex_case(tmp_path, hooks=_codex_hooks(
        tuple(event for event in _CODEX_ENTRYPOINT_EVENTS if event != dropped)))

    findings = hook_sets.judge_adapter_hook_entrypoints(dest, None, ["codex"])

    assert [(finding.kind, finding.event) for finding in findings] == [
        (hook_sets.HOOK_ENTRYPOINT_MISSING, dropped)]
    assert _CODEX_HOOKS_REL in findings[0].detail
    assert any("sync-adapter-config --accept" in line
               for line in hook_sets.hook_entrypoint_advisory_lines(findings[0]))


def test_full_entrypoint_set_is_silent(tmp_path):
    """세 진입점이 다 있고 디스패처가 그 세대면 소견 0(거짓 red 금지)."""
    dest = _make_codex_case(tmp_path, hooks=_codex_hooks())

    assert hook_sets.judge_adapter_hook_entrypoints(dest, None, ["codex"]) == []


def test_narrowed_matcher_is_not_a_universal_entrypoint(tmp_path):
    """matcher 가 값 공간을 다 안 덮으면 진입점이 아니다 — 옛 형상이 green 으로 지나가면 안 된다."""
    dest = _make_codex_case(
        tmp_path, hooks=_codex_hooks(matcher="^collaborationspawn_agent$"))

    findings = hook_sets.judge_adapter_hook_entrypoints(dest, None, ["codex"])

    assert {finding.event for finding in findings} == set(_CODEX_ENTRYPOINT_EVENTS)
    assert {finding.kind for finding in findings} == {
        hook_sets.HOOK_ENTRYPOINT_MISSING}


def test_entrypoint_pointing_at_another_command_is_not_credited(tmp_path):
    """matcher 만 맞고 디스패처를 안 부르는 블록은 진입점으로 세지 않는다."""
    document = json.loads(_codex_hooks())
    handler = document["hooks"]["PostToolUse"][0]["hooks"][0]
    handler["command"] = '"$py" .project_manager/tools/pm_log.py snapshot'
    dest = _make_codex_case(
        tmp_path, hooks=json.dumps(document, ensure_ascii=False, indent=2) + "\n")

    findings = hook_sets.judge_adapter_hook_entrypoints(dest, None, ["codex"])

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
        tmp_path, label, command):
    """실제로 발화하지 않는 커맨드는 진입점으로 인정하지 않는다(값 대조·민감도)."""
    dest = _make_codex_case(
        tmp_path, hooks=_codex_hooks_with_command("PreToolUse", command))

    findings = hook_sets.judge_adapter_hook_entrypoints(dest, None, ["codex"])

    assert [(finding.kind, finding.event) for finding in findings] == [
        (hook_sets.HOOK_ENTRYPOINT_MISSING, "PreToolUse")], label


def test_dispatcher_path_and_event_must_live_in_the_same_command(
        tmp_path):
    """경로는 이 커맨드, 호출 값은 저 커맨드로 흩어져 있으면 진입점이 아니다."""
    document = json.loads(_codex_hooks())
    handlers = document["hooks"]["PreToolUse"][0]["hooks"]
    handlers[0]["command"] = f"printf '%s\\n' {_CODEX_DISPATCHER_REL}"
    handlers[0]["commandWindows"] = (
        f'"$py" other_tool.py {_CODEX_DISPATCH_FLAG} PreToolUse')
    dest = _make_codex_case(
        tmp_path, hooks=json.dumps(document, ensure_ascii=False, indent=2) + "\n")

    findings = hook_sets.judge_adapter_hook_entrypoints(dest, None, ["codex"])

    assert [finding.event for finding in findings] == ["PreToolUse"]


@pytest.mark.parametrize("command", (
    f'"$py" {_CODEX_DISPATCHER_REL} {_CODEX_DISPATCH_FLAG} PreToolUse',
    f'"$py" {_CODEX_DISPATCHER_REL} {_CODEX_DISPATCH_FLAG}=PreToolUse',
    f"& $py '{_CODEX_DISPATCHER_REL}' {_CODEX_DISPATCH_FLAG} 'PreToolUse'",
    f'"$py" .\\{_CODEX_DISPATCHER_REL.replace("/", chr(92))} '
    f'{_CODEX_DISPATCH_FLAG} PreToolUse',
))
def test_real_invocations_are_credited_across_shell_notations(
        tmp_path, command):
    """실제로 부르는 표기는 전부 진입점으로 센다 — 조인 fix 가 정상 config 를 오탐하면 안 된다."""
    dest = _make_codex_case(
        tmp_path, hooks=_codex_hooks_with_command("PreToolUse", command))

    assert hook_sets.judge_adapter_hook_entrypoints(dest, None, ["codex"]) == []


@pytest.mark.parametrize("harness,template_dir",
                         (("claude", "claude_code"), ("codex", "codex")))
def test_shipped_config_is_not_falsely_reported_by_the_value_check(
        harness, template_dir):
    """현행 출하 선언 전수가 값 대조에서도 진입점으로 인정된다(오탐 0·역방향 확인)."""
    template_root = REPO / "templates" / template_dir
    spec = adapter_specs.ADAPTER_HOOK_SET[harness]
    document = json.loads(
        (template_root / spec.config_relpath).read_text(encoding="utf-8"))

    assert spec.entrypoints, harness
    for entrypoint in spec.entrypoints:
        assert adapter_documents._entrypoint_is_present(document, entrypoint), \
            f"{harness}:{entrypoint.event} 출하 선언이 값 대조에서 오탐됐다"


def test_dispatcher_that_knows_the_flag_but_not_the_event_is_stale(
        tmp_path):
    """플래그는 알고 그 이벤트는 모르는 디스패처 세대도 stale 이다(값 기준 stale 검사).

    진입점 집합이 늘어난 릴리즈의 흡수 창에서 실제로 나오는 형상이고, 그 창에서 훅은 매
    발화마다 폴백으로 빠진다 — 리터럴 `--hook-dispatch` 존재만 보면 green 으로 지나간다."""
    dest = _make_codex_case(tmp_path, hooks=_codex_hooks(),
                            dispatcher=_DISPATCHER_PREVIOUS_EVENT_SET)

    findings = hook_sets.judge_adapter_hook_entrypoints(dest, None, ["codex"])

    trailing = _CODEX_ENTRYPOINT_EVENTS[-1]
    assert [(finding.kind, finding.event) for finding in findings] == [
        (hook_sets.HOOK_ENTRYPOINT_STALE_DISPATCHER, trailing)]
    assert trailing in findings[0].detail
    assert hook_sets.entrypoint_invocation(
        adapter_specs.ADAPTER_HOOK_SET["codex"].entrypoints[-1]) in findings[0].detail


def test_shipped_codex_dispatcher_supports_every_declared_invocation():
    """출하 디스패처가 출하 config 의 호출 값을 전부 감당한다(자기정합·오탐 0)."""
    template_root = REPO / "templates" / "codex"

    for entrypoint in adapter_specs.ADAPTER_HOOK_SET["codex"].entrypoints:
        assert hook_sets._entrypoint_dispatcher_gap(template_root, entrypoint) is None, \
            entrypoint.event


def test_stale_dispatcher_behind_a_present_entrypoint_is_reported(tmp_path):
    """진입점은 있는데 설치된 디스패처가 그 플래그를 모르면 훅이 매번 폴백으로 빠진다.

    config 는 managed 라 자동 갱신되고 디스패처는 manifest 라 pm-update 가 덮는다 — 두 축의
    도착 순서가 갈리는 창이 실재하고, 그 창에서 가드는 무음 통과한다."""
    dest = _make_codex_case(tmp_path, hooks=_codex_hooks(),
                            dispatcher=_DISPATCHER_OLD)

    findings = hook_sets.judge_adapter_hook_entrypoints(dest, None, ["codex"])

    assert {finding.kind for finding in findings} == {
        hook_sets.HOOK_ENTRYPOINT_STALE_DISPATCHER}
    assert len(findings) == len(_CODEX_ENTRYPOINT_EVENTS)
    assert hook_sets.hook_entrypoint_advisory_lines(findings[0]) == [
        f"pm-update 로 {_CODEX_DISPATCHER_REL} 를 먼저 받아라"]


def test_absent_dispatcher_is_reported_not_silently_credited(tmp_path):
    """디스패처 파일 자체가 없으면 미지원과 같은 판정이다(부재 = 무발화)."""
    dest = _make_codex_case(tmp_path, hooks=_codex_hooks(), dispatcher=None)

    findings = hook_sets.judge_adapter_hook_entrypoints(dest, None, ["codex"])

    assert len(findings) == len(_CODEX_ENTRYPOINT_EVENTS)
    assert {finding.kind for finding in findings} == {
        hook_sets.HOOK_ENTRYPOINT_STALE_DISPATCHER}


def test_broken_or_absent_config_yields_no_entrypoint_finding(tmp_path):
    """config 부재·파손은 어댑터 config 채널이 말하는 상태다 — 여기서 겹쳐 처방하지 않는다."""
    dest = _make_codex_case(tmp_path, hooks="{ 깨진 json")
    assert hook_sets.judge_adapter_hook_entrypoints(dest, None, ["codex"]) == []

    (dest / _CODEX_HOOKS_REL).unlink()
    assert hook_sets.judge_adapter_hook_entrypoints(dest, None, ["codex"]) == []


def test_previous_generation_declaration_without_entrypoints_is_silent(
        tmp_path):
    """진입점 개념이 없던 세대 선언으로 판정하면 조용하다(구세대 형제 강등 경로)."""
    dest = _make_codex_case(tmp_path, hooks=_codex_hooks(("PreToolUse",)))
    legacy_spec = SimpleNamespace(
        config_relpath=_CODEX_HOOKS_REL, live_files=(_CODEX_DISPATCHER_REL,),
        flag_support={}, coupled_groups=())

    assert hook_sets.judge_adapter_hook_entrypoints(
        dest, None, ["codex"], declarations={"codex": legacy_spec}) == []


def test_sync_path_reports_missing_entrypoint_without_blocking(
        pm_update, tmp_path, capsys):
    """pm_update 가 진입점 누락을 **지목**하되 완료 게이트는 막지 않는다(advisory)."""
    dest = _make_codex_case(tmp_path, hooks=_codex_hooks(("PreToolUse",)))
    source = tmp_path / "framework"
    (source / "templates" / "codex" / ".codex").mkdir(parents=True)
    _plant_upstream_package(source)

    result = update_adapters.check_adapter_hook_sets(pm_paths.home_at(pm_update.REPO), dest, source)
    update_adapters._print_adapter_hook_set_finding(result, dry_run=False)
    err = capsys.readouterr().err

    assert result["status"] == "ok" and result["findings"] == []
    assert {item["event"] for item in result["entrypoints"]} == set(
        _CODEX_ENTRYPOINT_EVENTS) - {"PreToolUse"}
    assert "어댑터 훅 진입점 누락(codex)" in err
    for event in set(_CODEX_ENTRYPOINT_EVENTS) - {"PreToolUse"}:
        assert event in err, event
    assert not update_adapters._adapter_hook_set_gate_failed(result), \
        "advisory 축이 완료 게이트를 막았다 — 훅을 끈 채택자의 흡수가 영구히 잠긴다"


def test_sync_path_is_silent_when_every_entrypoint_is_present(
        pm_update, tmp_path, capsys):
    """진입점이 다 있으면 이 축은 무출력이다(소음 0)."""
    dest = _make_codex_case(tmp_path, hooks=_codex_hooks())
    source = tmp_path / "framework"
    (source / "templates" / "codex" / ".codex").mkdir(parents=True)
    _plant_upstream_package(source)

    result = update_adapters.check_adapter_hook_sets(pm_paths.home_at(pm_update.REPO), dest, source)
    update_adapters._print_adapter_hook_set_finding(result, dry_run=False)

    assert result["entrypoints"] == []
    assert "진입점" not in capsys.readouterr().err


# ── pm_update 동기 경로 배선 ────────────────────────────────────────────────


def test_sync_path_reports_mixed_generation_loudly_and_blocks(pm_update, tmp_path, capsys):
    """혼합 세대는 동기 경로에서 loud 처방 + 완료 게이트 red 다(조용한 통과 금지)."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=True),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_OLD)

    result = update_adapters.check_adapter_hook_sets(pm_paths.home_at(pm_update.REPO), dest, source)
    update_adapters._print_adapter_hook_set_finding(result, dry_run=False)

    assert result["status"] == "ok" and len(result["findings"]) == 1
    assert update_adapters._adapter_hook_set_gate_failed(result)
    err = capsys.readouterr().err
    assert "훅 세트 세대 불일치" in err, err
    assert _GIT_ANCHOR_FLAG in err and _DRIVER_REL in err, err
    assert "pm-update" in err, "처방 커맨드 부재"


def test_sync_path_is_silent_when_generations_match(pm_update, tmp_path, capsys):
    """정합이면 무출력 — 매 sync 마다 우는 경고는 곧 무시된다."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=True),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_NEW)

    result = update_adapters.check_adapter_hook_sets(pm_paths.home_at(pm_update.REPO), dest, source)
    update_adapters._print_adapter_hook_set_finding(result, dry_run=False)

    assert not update_adapters._adapter_hook_set_gate_failed(result)
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


def test_main_sync_run_returns_rc1_on_mixed_generation(pm_update, tmp_path,
                                                       monkeypatch, capsys):
    """실 동기 실행이 혼합 세대를 rc1 로 낸다 — 배선이 실제로 돌아야 게이트다(엔진 변경 0 경로)."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=True),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_OLD)
    _write_source_manifest(source, ["# 합성 manifest — 엔진 변경 0 경로"])
    monkeypatch.setattr(pm_update, "REPO", dest)

    rc = update_cmd.main(pm_paths.home_at(pm_update.REPO), ["--from", str(source)])

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
    # tools/ 밖 행 — tools/ 아래는 엔진 코드라 전달 프로세스가 옮기고 완료 단계의 변경이 0 이 된다(T-0951).
    engine_rel = "sentinel/__hook_set_sentinel__.py"
    engine_src = source / engine_rel
    engine_src.parent.mkdir(parents=True, exist_ok=True)
    engine_src.write_text("# 상류 엔진 파일\n", encoding="utf-8")
    _write_source_manifest(source, [engine_rel])
    monkeypatch.setattr(pm_update, "REPO", dest)

    rc = update_cmd.main(pm_paths.home_at(pm_update.REPO), ["--from", str(source)])

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
    original = engine_copy_io._atomic_copy2
    monkeypatch.setattr(engine_copy_io, "_atomic_copy2",
                        lambda src, dst: (atomic_calls.append(str(dst)), original(src, dst))[1])

    update_render.apply(pm_paths.home_at(pm_update.REPO), [
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
# 다음 세대 package 가 들고 오는 선언 — 선언 자신이 엔진 package 안에 살기 때문에, dest 사본의
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

    선언을 dest 사본에서 읽으면 원자 write 가 영영 한 세대 늦게 도착한다 — 엔진 package 자체를
    갱신하는 바로 그 실행에서, 새 세대가 등재한 훅 파일이 copy2 로 떨어진다."""
    source = tmp_path / "framework"
    source.mkdir()
    _plant_upstream_package(source, extra_tail=_UPSTREAM_ADDS_HOOK_PATH)
    dest = tmp_path / "adopter"
    (dest / ".claude").mkdir(parents=True)
    hook_dst = dest / _NEXT_GENERATION_HOOK_REL
    hook_dst.write_text("# 구 훅\n", encoding="utf-8")
    hook_src = source / "new_hook.sh"
    hook_src.write_text("# 신 훅\n", encoding="utf-8")
    hook_inode = hook_dst.stat().st_ino

    installed = hook_sets.resolve_hook_set_predicate(pm_paths.home_at(pm_update.REPO))
    incoming = hook_sets.resolve_hook_set_predicate(pm_paths.home_at(pm_update.REPO), source)
    assert not installed(_NEXT_GENERATION_HOOK_REL), \
        "픽스처 전제 붕괴 — 설치된 세대가 이미 그 경로를 안다"
    assert incoming(_NEXT_GENERATION_HOOK_REL), "상류 세대 선언이 해소되지 않았다"

    atomic_calls: list[str] = []
    original = engine_copy_io._atomic_copy2
    monkeypatch.setattr(engine_copy_io, "_atomic_copy2",
                        lambda src, dst: (atomic_calls.append(str(dst)),
                                          original(src, dst))[1])
    update_render.apply(pm_paths.home_at(pm_update.REPO), [(_NEXT_GENERATION_HOOK_REL, hook_src, hook_dst, "M")],
                    is_hook_set_path=incoming)

    assert atomic_calls == [str(hook_dst)], atomic_calls
    assert hook_dst.read_text(encoding="utf-8") == "# 신 훅\n"
    if os.name != "nt":
        assert hook_dst.stat().st_ino != hook_inode, "구세대 판정자로 copy2 폴백됐다"


def test_source_generation_predicate_falls_back_to_local_when_absent(
        pm_update, tmp_path):
    """상류에 선언 package 가 없으면 로컬 세대로 폴백한다(판정 자체를 잃지 않는다)."""
    source = tmp_path / "framework"
    source.mkdir()

    predicate = hook_sets.resolve_hook_set_predicate(pm_paths.home_at(pm_update.REPO), source)

    assert predicate(_WRAPPER_REL), "폴백이 로컬 세대 선언을 못 읽었다"
    assert not predicate(".project_manager/tools/board.py")


# ── 경로 스코프 부분 전파 (훅 세트 반쪽 갱신 거부) ────────────────────────────


def test_partial_scope_over_hook_set_is_refused_before_writing(
        pm_update, tmp_path, monkeypatch, capsys):
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

    rc = update_cmd.main(pm_paths.home_at(pm_update.REPO), ["--from", str(source), "--paths", _WRAPPER_REL])

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

    rc = update_cmd.main(pm_paths.home_at(pm_update.REPO),
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

    rc = update_cmd.main(pm_paths.home_at(pm_update.REPO), [
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

    rc = update_cmd.main(pm_paths.home_at(pm_update.REPO), ["--from", str(source), "--paths", _WRAPPER_REL, _DRIVER_REL])

    assert rc == 0, capsys.readouterr()
    assert (dest / _DRIVER_REL).read_text(encoding="utf-8") == _DRIVER_NEW
    assert (dest / _WRAPPER_REL).read_text(encoding="utf-8") == _WRAPPER_NEW


def test_scope_outside_hook_set_is_not_affected():
    """훅 세트를 안 건드리는 전파는 관심사가 아니다(엔진 도구 부분 전파는 종전대로)."""
    assert hook_sets.hook_set_partial_update(
        [".project_manager/tools/board.py"],
        [".project_manager/tools/board.py", _WRAPPER_REL]) == []


def test_uncoupled_hook_file_may_be_propagated_alone():
    """결합이 없는 훅 파일(독립 relay 드라이버)은 단건 전파가 정당하다 — 가드가 과잉이면 안 된다.

    하네스 전체를 한 단위로 보면 `pm_orch_opencode.py` 하나 옮기는 정당한 요청까지 막힌다."""
    assert hook_sets.hook_set_partial_update(
        [".opencode/pm_orch_opencode.py"],
        [".opencode/pm_orch_opencode.py", ".opencode/lib/ctx-guard-core.cjs"]) == []


def test_partial_update_inside_a_declared_directory_is_refused():
    """디렉토리 선언 안의 파일 하나만 옮기는 것도 반쪽 갱신이다(형제 코어가 구세대로 남음)."""
    partial = hook_sets.hook_set_partial_update(
        [".opencode/plugins/ctx-guard.js"],
        [".opencode/plugins/ctx-guard.js", ".opencode/lib/ctx-guard-core.cjs"])

    assert [row[0] for row in partial] == ["opencode"], partial
    assert partial[0][1] == (".opencode/lib/",), partial


def test_already_current_sibling_is_not_demanded():
    """묶음의 형제가 **이미 최신**이면(계획에 없으면) 요구하지 않는다 — 거짓 거부 금지."""
    assert hook_sets.hook_set_partial_update([_WRAPPER_REL], [_WRAPPER_REL]) == []


def test_whole_group_update_is_allowed():
    """묶음이 함께 옮겨지면 통과한다."""
    assert hook_sets.hook_set_partial_update(
        [_WRAPPER_REL, _DRIVER_REL], [_WRAPPER_REL, _DRIVER_REL]) == []


# ── 실참조 경로 해소 (선언 좌표와 다른 토폴로지) ──────────────────────────────


def test_reference_outside_declared_coordinate_is_judged_at_its_real_path(
        tmp_path):
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
    spec = adapter_specs.ADAPTER_HOOK_SET["claude"]

    unmet = hook_sets._hook_set_demands(document, spec, root)

    assert list(unmet) == [(hook_sets.HOOK_SET_UNSUPPORTED_FLAG, _GIT_ANCHOR_FLAG)], unmet
    assert unmet[(hook_sets.HOOK_SET_UNSUPPORTED_FLAG, _GIT_ANCHOR_FLAG)] == (
        f"{prefix}/{_DRIVER_REL}",), "미충족 경로가 실제 좌표가 아니다(처방이 엉뚱한 파일을 가리킴)"


def test_reference_outside_declared_coordinate_is_green_when_generation_matches(
        tmp_path):
    """같은 토폴로지에서 세대가 맞으면 조용하다(거짓 red 금지)."""
    root = tmp_path / "product"
    prefix = "templates/claude_code"
    (root / prefix / ".claude").mkdir(parents=True)
    (root / prefix / _WRAPPER_REL).write_text(_WRAPPER_NEW, encoding="utf-8")
    (root / prefix / _DRIVER_REL).write_text(_DRIVER_NEW, encoding="utf-8")
    document = json.loads(_settings(git_anchor=True,
                                    wrapper_rel=f"{prefix}/{_WRAPPER_REL}"))

    assert hook_sets._hook_set_demands(
        document, adapter_specs.ADAPTER_HOOK_SET["claude"], root) == {}


def test_product_root_settings_reference_is_actually_judged():
    """이 checkout(제품 루트) 자기 settings.json 의 훅 참조가 **검사 대상 안**이고 정합이다.

    루트는 `templates/claude_code/.claude/…` 를 부르는 실 토폴로지다 — 선언 좌표만 보던 판정은 이
    커맨드를 통째로 건너뛰어 stale template 드라이버가 green 이었다. 이 단언이 그 자리를 지킨다."""
    settings = REPO / ".claude" / "settings.json"
    document = json.loads(settings.read_text(encoding="utf-8"))
    spec = adapter_specs.ADAPTER_HOOK_SET["claude"]
    prefixes = [
        hook_sets._hook_set_reference_prefix(
            adapter_documents._hook_script_relpath(
                adapter_documents._split_hook_command(command)[0]) or "", spec)
        for command in adapter_documents._hook_commands(document)
    ]

    assert any(prefix for prefix in prefixes if prefix), \
        f"루트 settings 의 훅 참조가 하나도 훅 세트로 해소되지 않았다: {prefixes}"
    assert hook_sets._hook_set_demands(document, spec, REPO) == {}, \
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


def test_live_hook_set_declaration_covers_shipped_executable_entries():
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
    missing = sorted(rel for rel in seen if not hook_sets.is_live_hook_set_path(rel))
    assert missing == [], f"manifest 등재 실행 파일이 원자 write 선언 밖이다: {missing}"


def test_opencode_shared_core_is_atomic_like_the_claude_axis():
    """opencode 플러그인이 즉시 import 하는 공유 코어도 원자 write 대상이다(하네스 비대칭 금지).

    플러그인 3종이 로드 시점에 `../lib/*-core.cjs` 를 import 하므로 코어가 부분 파일이면 플러그인
    로드 자체가 깨진다 — claude 축이 공유 코어(`ctx_guard.py`)를 넣은 것과 같은 근거다."""
    lib_dir = REPO / "templates" / "opencode" / ".opencode" / "lib"
    cores = sorted(path.name for path in lib_dir.glob("*.cjs"))

    assert cores, "픽스처 전제(opencode 공유 코어)가 사라졌다"
    for name in cores:
        assert hook_sets.is_live_hook_set_path(f".opencode/lib/{name}"), name
    for plugin in sorted((REPO / "templates" / "opencode" / ".opencode"
                          / "plugins").glob("*.js")):
        assert hook_sets.is_live_hook_set_path(f".opencode/plugins/{plugin.name}")


# ── 수용 순서 게이트 (엔진 파일 선행 · config 후행) ──────────────────────────


def _accept_args(source: Path, *, accept=None, accept_all=False,
                 check=False) -> SimpleNamespace:
    return SimpleNamespace(accept=accept, accept_all=accept_all, check=check,
                           list=False, source=str(source))


def test_unreadable_hook_file_is_lenient_for_report_but_blocks_mutation(
        pm_config, tmp_path, capsys):
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

    assert hook_sets.judge_adapter_hook_sets(dest, source) == [], \
        "조회 판정이 판정 불가 파일을 red 로 올렸다(거짓 처방)"
    rc = config_adapter.cmd_sync_adapter_config(pm_paths.home_at(pm_config.REPO),
        _accept_args(source, accept=_SETTINGS_REL), dest_root=dest)

    assert rc == 1
    assert (dest / _SETTINGS_REL).read_text(encoding="utf-8") == before
    assert "수용 거부" in capsys.readouterr().err


def test_check_reports_hook_set_generation_mismatch(pm_config, tmp_path, capsys):
    """`--check` 가 훅 세트 세대 불일치를 red 로 낸다 — pm-update 와 같은 게이트를 본다.

    스킬 카드가 완료 게이트로 지목하는 표면이라, pm-update 가 rc1 로 막는 클래스를 여기서 green
    으로 통과시키면 릴리즈가 false-complete 된다."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=True),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_OLD)

    rc = config_adapter.cmd_sync_adapter_config(pm_paths.home_at(pm_config.REPO),
        _accept_args(source, check=True), dest_root=dest)

    assert rc == 1
    err = capsys.readouterr().err
    assert "훅 세트 세대 불일치" in err and _GIT_ANCHOR_FLAG in err, err


def test_check_sees_hook_sets_even_when_config_channel_is_empty(
        pm_config, tmp_path, capsys):
    """config 채널 대상이 0 이어도 훅 세트 red 는 red 다 — 게이트 표면 전체 우회 금지."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=True),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_OLD)
    (source / "templates" / "claude_code" / _SETTINGS_REL).unlink()  # 채널 대상 0.

    rc = config_adapter.cmd_sync_adapter_config(pm_paths.home_at(pm_config.REPO),
        _accept_args(source, check=True), dest_root=dest)

    out, err = capsys.readouterr()
    assert "채널 대상 없음" in out, out
    assert rc == 1, (out, err)
    assert "훅 세트 세대 불일치" in err, err


def test_check_stays_green_when_hook_set_matches(pm_config, tmp_path, capsys):
    """정합이면 `--check` 는 종전대로 green 이다(가드가 정상 경로를 막지 않는다)."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=True),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_NEW)

    rc = config_adapter.cmd_sync_adapter_config(pm_paths.home_at(pm_config.REPO),
        _accept_args(source, check=True), dest_root=dest)

    assert rc == 0, capsys.readouterr()
    assert "수렴 확인" in capsys.readouterr().out


def test_accept_refuses_when_engine_files_are_behind(pm_config, tmp_path, capsys):
    """수용 선행조건 미충족이면 **파일을 건드리지 않고** 거부한다 — 위험 방향을 기계가 막는다."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=False),
        dest_wrapper=_WRAPPER_OLD, dest_driver=_DRIVER_OLD,
        template_settings=_settings(git_anchor=True))
    before = (dest / _SETTINGS_REL).read_text(encoding="utf-8")

    rc = config_adapter.cmd_sync_adapter_config(pm_paths.home_at(pm_config.REPO),
        _accept_args(source, accept=_SETTINGS_REL), dest_root=dest)

    assert rc == 1
    assert (dest / _SETTINGS_REL).read_text(encoding="utf-8") == before, \
        "선행조건 미충족인데 config 를 앞세웠다(락아웃 재현)"
    err = capsys.readouterr().err
    assert "수용 거부" in err and "pm-update" in err, err


def test_accept_proceeds_when_engine_files_are_current(pm_config, tmp_path):
    """엔진 파일이 이미 그 세대면 수용이 정상 진행된다(게이트가 정상 경로를 막지 않는다)."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=False),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_NEW,
        template_settings=_settings(git_anchor=True))

    rc = config_adapter.cmd_sync_adapter_config(pm_paths.home_at(pm_config.REPO),
        _accept_args(source, accept=_SETTINGS_REL), dest_root=dest)

    assert rc == 0
    assert (dest / _SETTINGS_REL).read_text(encoding="utf-8") == _settings(git_anchor=True)


def test_accept_of_absent_config_reports_channel_error_not_order_refusal(
        pm_config, tmp_path, capsys):
    """dest 에 없는 config 를 수용하려 하면 채널 오류가 그대로 나온다(순서 게이트가 가로채지 않음)."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=True),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_NEW)
    (dest / _SETTINGS_REL).unlink()

    rc = config_adapter.cmd_sync_adapter_config(pm_paths.home_at(pm_config.REPO),
        _accept_args(source, accept=_SETTINGS_REL), dest_root=dest)

    err = capsys.readouterr().err
    assert rc == 1 and "수용 거부" not in err, err
    assert "그 파일이 없다" in err or "채널 대상이 아니다" in err, err


def test_accept_all_accepts_the_set_through_the_same_gate(pm_config, tmp_path, capsys):
    """세트 수용도 파일당 같은 게이트·같은 백업·같은 원자 교체를 탄다."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=False),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_NEW,
        template_settings=_settings(git_anchor=True),
        ledger={_SETTINGS_REL: _ledger_entry(_settings(git_anchor=False))})

    rc = config_adapter.cmd_sync_adapter_config(pm_paths.home_at(pm_config.REPO),
        _accept_args(source, accept_all=True), dest_root=dest)

    assert rc == 0, capsys.readouterr()
    assert (dest / _SETTINGS_REL).read_text(encoding="utf-8") == _settings(git_anchor=True)
    out = capsys.readouterr().out
    assert "세트 수용 결과: 1/1 수용" in out, out


def test_accept_all_refuses_blocked_member_without_touching_it(pm_config, tmp_path, capsys):
    """세트 안에서도 선행조건을 어긴 파일만 거부되고 그 파일은 그대로다(rc1)."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=False),
        dest_wrapper=_WRAPPER_OLD, dest_driver=_DRIVER_OLD,
        template_settings=_settings(git_anchor=True),
        ledger={_SETTINGS_REL: _ledger_entry(_settings(git_anchor=False))})
    before = (dest / _SETTINGS_REL).read_text(encoding="utf-8")

    rc = config_adapter.cmd_sync_adapter_config(pm_paths.home_at(pm_config.REPO),
        _accept_args(source, accept_all=True), dest_root=dest)

    assert rc == 1
    assert (dest / _SETTINGS_REL).read_text(encoding="utf-8") == before
    assert "0/1 수용" in capsys.readouterr().out


def test_accept_all_excludes_adopter_edits_and_says_so(pm_config, tmp_path, capsys):
    """채택자 편집분은 세트가 건드리지 않는다 — 무지목 일괄 교체가 하한선을 깨뜨린다.

    report 모드 대상(settings.json·opencode.jsonc·config.toml)에는 권한 allowlist·모델·threshold
    같은 실 노브가 들어 있다. 편집분은 단건 `--accept` 로만 받고, 제외 사실과 그 커맨드를 낸다."""
    edited = _settings(git_anchor=False).replace("15", "30")  # 채택자 노브 손편집.
    dest, source = _make_case(
        tmp_path, dest_settings=edited,
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_NEW,
        template_settings=_settings(git_anchor=True),
        ledger={_SETTINGS_REL: _ledger_entry(_settings(git_anchor=False))})

    rc = config_adapter.cmd_sync_adapter_config(pm_paths.home_at(pm_config.REPO),
        _accept_args(source, accept_all=True), dest_root=dest)

    out = capsys.readouterr().out
    assert (dest / _SETTINGS_REL).read_text(encoding="utf-8") == edited, \
        "세트 수용이 채택자 편집분을 덮었다"
    assert rc == 1, out
    assert "세트 수용 제외(채택자 편집분)" in out and _SETTINGS_REL in out, out
    assert f"--accept {_SETTINGS_REL}" in out, "단건 수용 경로 안내 부재"


def test_accept_all_prints_targets_before_touching_them(pm_config, tmp_path, capsys):
    """무엇을 건드릴지 **먼저 전부 보이고** 나서 수용한다(수용 줄보다 앞선 대상 목록)."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=False),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_NEW,
        template_settings=_settings(git_anchor=True),
        ledger={_SETTINGS_REL: _ledger_entry(_settings(git_anchor=False))})

    rc = config_adapter.cmd_sync_adapter_config(pm_paths.home_at(pm_config.REPO),
        _accept_args(source, accept_all=True), dest_root=dest)
    out = capsys.readouterr().out

    assert rc == 0, out
    assert out.index("# 세트 수용 대상 1건") < out.index("✓ 어댑터 config 수용"), out
    assert f"  - {_SETTINGS_REL} · claude · " in out, out


# ── 세대 선언 소비자 상류-통일 (T-0610) ──────────────────────────────────────
# 선언 자체가 엔진 package 안에 살기 때문에 설치본 선언은 업그레이드 시점에 정의상 한 세대 뒤다.
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
        pm_config, tmp_path, capsys):
    """상류만 아는 플래그를 요구하는 config 는 수용이 거부된다 — 게이트는 상류 세대로 판정한다.

    설치본 선언은 그 플래그를 모르므로 "선언 밖 플래그" 로 접어 게이트가 통과시킨다(있으나 마나).
    미지원 드라이버 위에 새 config 를 앉히는 게 정확히 이 게이트가 막는 락아웃이다."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=False),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_NEW,  # 신 세대지만 새 플래그는 모름.
        template_settings=_settings(git_anchor=True, flag=_UPSTREAM_ONLY_FLAG),
        upstream_tail=_UPSTREAM_ADDS_FLAG)
    before = (dest / _SETTINGS_REL).read_text(encoding="utf-8")

    installed_only = hook_sets.hook_set_accept_decision(
        dest, source, _SETTINGS_REL,
        declarations=hook_sets.hook_set_declarations(_run_home(tmp_path), None))
    rc = config_adapter.cmd_sync_adapter_config(pm_paths.home_at(pm_config.REPO),
        _accept_args(source, accept=_SETTINGS_REL), dest_root=dest)

    assert installed_only.blockers == [], \
        "픽스처 전제 붕괴 — 설치본 선언이 이미 그 플래그를 안다(상류 축이 무의미)"
    assert rc == 1
    assert (dest / _SETTINGS_REL).read_text(encoding="utf-8") == before, \
        "상류만 아는 세대 요구를 못 보고 config 를 앞세웠다"
    err = capsys.readouterr().err
    assert "수용 거부" in err and _UPSTREAM_ONLY_FLAG in err, err


def test_accept_gate_binds_checked_template_bytes_to_the_copy(tmp_path):
    """게이트가 검사한 template bytes 와 실제 복사 bytes 가 다르면 수용이 중단된다.

    판정↔쓰기 사이에 상류가 바뀌면 "검사한 세대" 와 다른 내용이 설치된다 — dest 동시 편집을 막는
    `expected_sha256` 의 반대편 축이다."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=False),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_NEW,
        template_settings=_settings(git_anchor=True))
    decision = hook_sets.hook_set_accept_decision(
        dest, source, _SETTINGS_REL,
        declarations=hook_sets.hook_set_declarations(_run_home(tmp_path), source, required=True))
    assert decision.blockers == [] and decision.template_sha256, decision
    before = (dest / _SETTINGS_REL).read_text(encoding="utf-8")
    # 판정 뒤 상류가 바뀐다(다른 세대의 config 가 그 자리에 놓임).
    (source / "templates" / "claude_code" / _SETTINGS_REL).write_text(
        _settings(git_anchor=True, flag=_UPSTREAM_ONLY_FLAG), encoding="utf-8")

    outcome = adapter_accept.accept_adapter_config(
        dest, source, _SETTINGS_REL,
        expected_template_sha256=decision.template_sha256)

    assert outcome.status == "raced", outcome
    assert "상류 template" in outcome.detail, outcome.detail
    assert (dest / _SETTINGS_REL).read_text(encoding="utf-8") == before, \
        "검사하지 않은 bytes 가 설치됐다"


def test_accept_gate_is_fail_closed_when_upstream_generation_is_unreadable(
        pm_config, tmp_path, capsys):
    """상류 세대를 못 읽으면 수용을 거부한다 — mutation 게이트는 모르면 멈춘다(fail-closed)."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=False),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_NEW,
        template_settings=_settings(git_anchor=True),
        upstream_tail=None)  # 상류 트리에 선언 package 없음.
    before = (dest / _SETTINGS_REL).read_text(encoding="utf-8")

    rc = config_adapter.cmd_sync_adapter_config(pm_paths.home_at(pm_config.REPO),
        _accept_args(source, accept=_SETTINGS_REL), dest_root=dest)

    err = capsys.readouterr().err
    assert rc == 1, err
    assert (dest / _SETTINGS_REL).read_text(encoding="utf-8") == before
    assert "수용 거부" in err and "상류 세대 선언" in err, err


def test_partial_guard_sees_a_coupled_group_only_upstream_knows(
        pm_update, tmp_path, monkeypatch, capsys):
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

    assert hook_sets.hook_set_partial_update([alpha], [alpha, beta]) == [], \
        "픽스처 전제 붕괴 — 설치본 선언이 이미 그 묶음을 안다"
    rc = update_cmd.main(pm_paths.home_at(pm_update.REPO), ["--from", str(source), "--paths", alpha])

    err = capsys.readouterr().err
    assert rc == 1, err
    assert (dest / alpha).read_text(encoding="utf-8") == "# 구 세대\n", \
        "거부인데 alpha 가 이미 갱신됐다"
    assert "훅 세트를 반쪽만 갱신한다" in err and beta in err, err


def test_accept_gate_binds_the_declaration_snapshot_too(tmp_path):
    """선언 소스(상류 package)가 판정 뒤 바뀌면 중단한다 — template 해시만으로는 못 막는다.

    config template 이 그대로여도 상류가 통째로 갱신되면 **구 선언으로 낸 판정** 위에 새 세대가
    설치된다. 두 해시는 한 벌의 스냅샷이라 어느 쪽이 변해도 raced 다."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=False),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_NEW,
        template_settings=_settings(git_anchor=True))
    decision = hook_sets.hook_set_accept_decision(
        dest, source, _SETTINGS_REL,
        declarations=hook_sets.hook_set_declarations(_run_home(tmp_path), source, required=True))
    assert decision.blockers == [] and decision.generation_sha256, decision
    before = (dest / _SETTINGS_REL).read_text(encoding="utf-8")
    # config template 은 그대로 두고 **선언 소스만** 새 세대로 바꾼다.
    _plant_upstream_package(source, extra_tail=_UPSTREAM_ADDS_FLAG)

    outcome = adapter_accept.accept_adapter_config(
        dest, source, _SETTINGS_REL,
        expected_template_sha256=decision.template_sha256,
        expected_generation_sha256=decision.generation_sha256)

    assert outcome.status == "raced", outcome
    assert "세대 선언" in outcome.detail, outcome.detail
    assert (dest / _SETTINGS_REL).read_text(encoding="utf-8") == before


def test_accept_gate_blocks_when_snapshot_hash_cannot_be_made(
        tmp_path, monkeypatch):
    """스냅샷 해시를 못 만들면 수용을 막는다 — 결속이 조용히 생략된 채 진행되면 안 된다.

    호출부는 `is not None` 으로 결속 인자를 붙이므로, 해시 계산 실패는 "검증 없이 교체" 로
    번역된다. mutation 축은 모르면 멈춘다(구세대 강등의 **의도적** None 과 구분되는 실패성 None)."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=False),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_NEW,
        template_settings=_settings(git_anchor=True))
    generation = hook_sets.hook_set_declarations(_run_home(tmp_path), source, required=True)
    assert hook_sets.hook_set_accept_decision(
        dest, source, _SETTINGS_REL, declarations=generation).blockers == [], \
        "픽스처 전제(정상 경로는 통과)"
    monkeypatch.setattr(adapter_baseline, "file_sha256", lambda _path: None)  # 해시 계산 실패 주입.

    decision = hook_sets.hook_set_accept_decision(
        dest, source, _SETTINGS_REL, declarations=generation)

    assert decision.blockers, "해시 부재인데 게이트가 통과시켰다"
    assert "재확인할 수" in decision.blockers[0].detail, decision.blockers[0].detail


def test_partial_guard_refuses_upstream_only_group_when_upstream_is_unreadable(
        pm_update, tmp_path, monkeypatch, capsys):
    """상류만 아는 묶음 + 상류 미해소 — 로컬 묶음에 없어도 거부한다(fail-closed 계약).

    로컬 membership 으로 좁히면 정확히 이 조합이 빠져나간다: 로컬 선언은 그 묶음을 모르고, 상류
    선언은 읽을 수 없다. 그래서 판정 단위를 훅이 사는 **네임스페이스**로 올린다."""
    alpha, beta = _UPSTREAM_ONLY_GROUP
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=True),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_NEW,
        upstream_tail=None)  # 상류 선언 package 없음 = 세대 미해소.
    for rel in (alpha, beta):
        (source / rel).parent.mkdir(parents=True, exist_ok=True)
        (source / rel).write_text("# 신 세대\n", encoding="utf-8")
        (dest / rel).write_text("# 구 세대\n", encoding="utf-8")
    _write_source_manifest(source, [alpha, beta])
    monkeypatch.setattr(pm_update, "REPO", dest)

    assert hook_sets.hook_set_partial_update([alpha], [alpha, beta]) == [], \
        "픽스처 전제 붕괴 — 로컬 선언이 그 묶음을 안다(구멍 재현 불가)"
    rc = update_cmd.main(pm_paths.home_at(pm_update.REPO), ["--from", str(source), "--paths", alpha])

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

    rc = update_cmd.main(pm_paths.home_at(pm_update.REPO),
        ["--from", str(source), "--paths", _WRAPPER_REL, _DRIVER_REL])

    err = capsys.readouterr().err
    assert rc == 1, err
    assert (dest / _WRAPPER_REL).read_text(encoding="utf-8") == _WRAPPER_OLD
    assert "상류 훅 세트 세대 선언을 확인할 수 없어" in err, err


def test_partial_guard_leaves_non_hook_scope_alone_without_upstream(
        pm_update, tmp_path, monkeypatch, capsys):
    """훅과 무관한 부분 전파는 상류 세대를 못 읽어도 통과한다 — fail-closed 가 과잉이면 안 된다."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=True),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_NEW, upstream_tail=None)
    # tools/ 밖 행 — tools/ 아래 엔진 코드가 상류와 다르면 `--paths` 는 거부된다(T-0951).
    engine_rel = "sentinel/__t0610_sentinel__.py"
    (source / engine_rel).parent.mkdir(parents=True, exist_ok=True)
    (source / engine_rel).write_text("# 상류 엔진 파일\n", encoding="utf-8")
    _write_source_manifest(source, [engine_rel])
    monkeypatch.setattr(pm_update, "REPO", dest)

    rc = update_cmd.main(pm_paths.home_at(pm_update.REPO), ["--from", str(source), "--paths", engine_rel])

    assert rc == 0, capsys.readouterr()
    assert (dest / engine_rel).read_text(encoding="utf-8") == "# 상류 엔진 파일\n"


def test_accept_all_excludes_unrecorded_because_edits_cannot_be_judged(
        pm_config, tmp_path, capsys):
    """원장이 없어 편집 여부를 **판정할 수 없는** config 도 세트가 건드리지 않는다.

    원장 도입 전 구세대 채택자는 커스텀을 해 두고도 `unrecorded` 로 보인다 — 세트가 그것을 받으면
    편집분과 구분 못 한 채 커스텀이 사라진다(모르면 덮지 않는다)."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=False),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_NEW,
        template_settings=_settings(git_anchor=True))  # 원장 없음 → unrecorded.
    before = (dest / _SETTINGS_REL).read_text(encoding="utf-8")
    assert [item.status for item in adapter_report.judge_adapter_configs(dest, source)] \
        == ["unrecorded"], "픽스처 전제(원장 부재 판정)가 아니다"

    rc = config_adapter.cmd_sync_adapter_config(pm_paths.home_at(pm_config.REPO),
        _accept_args(source, accept_all=True), dest_root=dest)

    out = capsys.readouterr().out
    assert (dest / _SETTINGS_REL).read_text(encoding="utf-8") == before, \
        "판정 불가 config 를 세트가 덮었다"
    assert rc == 1, out
    assert "세트 수용 제외(원장 부재로 편집 여부 판정 불가)" in out, out
    assert f"--accept {_SETTINGS_REL}" in out, "단건 수용 처방 부재"


def test_accept_all_reports_no_candidates_when_already_in_sync(pm_config, tmp_path, capsys):
    """이미 상류와 같으면 수용 후보가 없다 — byte churn 0(무의미한 백업 생성 금지)."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=True),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_NEW)

    rc = config_adapter.cmd_sync_adapter_config(pm_paths.home_at(pm_config.REPO),
        _accept_args(source, accept_all=True), dest_root=dest)

    assert rc == 0
    assert "대상 없음" in capsys.readouterr().out
    assert not (dest / ".pm_import_backups").exists(), "후보 0인데 백업을 만들었다"


def test_build_parser_routes_accept_all_and_excludes_other_modes(pm_config):
    """`--accept-all` 이 같은 커맨드로 라우팅되고 조회/단건 수용과 상호 배타다."""
    parser = pm_config.build_parser()
    parsed = parser.parse_args(["sync-adapter-config", "--accept-all"])

    assert parsed.func.func is config_adapter.cmd_sync_adapter_config and parsed.accept_all is True
    with pytest.raises(SystemExit):
        parser.parse_args(["sync-adapter-config", "--accept-all", "--accept", _SETTINGS_REL])
    with pytest.raises(SystemExit):
        parser.parse_args(["sync-adapter-config", "--accept-all", "--check"])


# ── 선언 해시 ↔ 실행 선언 결속 (stale `.pyc` 창 폐쇄) ─────────────────────────
_STALE_PYC_FLAG_A = "--t0611-generation-a"
_STALE_PYC_FLAG_B = "--t0611-generation-b"


def _package_digest(source: Path) -> str:
    """상류 package 의 추적 Python 파일(`__pycache__` 제외) 전체를 시험 쪽 열거로 직접 읽어 만든 digest."""
    package = source.resolve() / _UPSTREAM_PACKAGE_REL
    payloads = {path.relative_to(package).as_posix(): path.read_bytes()
                for path in repo_owned_paths(source, _UPSTREAM_PACKAGE_REL, mode=TRACKED_ONLY)
                if path.suffix == ".py" and "__pycache__" not in path.parts}
    return upstream_declarations._snapshot_digest(payloads)


def _flag_declaration_tail(flag: str) -> str:
    """상류 선언에 플래그 하나를 더하는 꼬리 — 두 세대의 **bytes 길이가 같다**(플래그 길이 동일)."""
    return (
        "\n"
        'ADAPTER_HOOK_SET["claude"] = ADAPTER_HOOK_SET["claude"]._replace(\n'
        "    flag_support=dict(ADAPTER_HOOK_SET['claude'].flag_support, **{\n"
        f"        {flag!r}: ({_WRAPPER_REL!r}, {_DRIVER_REL!r})}}),\n"
        ")\n"
    )


def test_declaration_load_is_bound_to_the_hashed_bytes(tmp_path):
    """해시한 bytes 와 **실행된 선언**이 같은 세대다 — timestamp 유효한 stale `.pyc` 폐쇄.

    엔진 전파는 `copy2`(mtime 보존)로 파일을 내려놓으므로, 크기까지 같은 사본이 앞 세대의 유효한
    `.pyc` 와 짝지어지는 창이 실재한다. 그 창에서 제자리 import 를 하면 "최신 package digest + 구 선언
    코드" 가 성립해, 게이트가 결속했다고 믿는 스냅샷이 거짓이 된다(수용은 그 digest 를 쓰기 직전에
    다시 대조하므로 거짓 통과가 그대로 설치된다)."""
    source = tmp_path / "framework"
    upstream = _plant_upstream_package(
        source, extra_tail=_flag_declaration_tail(_STALE_PYC_FLAG_A))
    stat_before = upstream.stat()
    # 1) A 세대 bytes 의 timestamp `.pyc` 를 실제로 남긴다.
    cached = Path(py_compile.compile(
        str(upstream), doraise=True,
        invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP))
    # 2) 같은 크기·같은 mtime 으로 B 세대를 덮는다(전파가 실제로 만드는 형상).
    upstream.write_text(
        (TOOLS / "pm_engine" / "domain" / "adapter_specs.py").read_text(encoding="utf-8")
        + _flag_declaration_tail(_STALE_PYC_FLAG_B), encoding="utf-8")
    os.utime(upstream, ns=(stat_before.st_atime_ns, stat_before.st_mtime_ns))
    assert upstream.stat().st_size == stat_before.st_size, "픽스처 전제(같은 크기)가 아니다"
    # 민감도 — 제자리 import 는 그 `.pyc` 의 구 선언을 재사용한다(창이 실재해야 아래 단언이 의미 있다).
    stale = subprocess.run(
        [sys.executable, "-I", "-c",
         "import sys; sys.path.insert(0, sys.argv[1]); "
         "from pm_engine.domain import adapter_specs; "
         "print(sorted(adapter_specs.ADAPTER_HOOK_SET['claude'].flag_support))",
         str(source / ".project_manager" / "tools")],
        capture_output=True, text=True, encoding="utf-8", check=True)
    assert cached.is_file() and _STALE_PYC_FLAG_A in stale.stdout, \
        "제자리 import 가 구 선언을 재사용하지 않는다 — 창 재현 실패(단언이 공허하다)"

    generation = hook_sets.hook_set_declarations(_run_home(tmp_path), source, required=True)

    flags = generation.declarations["claude"].flag_support
    assert _STALE_PYC_FLAG_B in flags and _STALE_PYC_FLAG_A not in flags, \
        "해시한 bytes 가 아니라 캐시된 구 선언 코드가 실행됐다(결속 붕괴)"
    assert generation.source_sha256 == _package_digest(source), \
        "digest 가 지금 package bytes 의 것이 아니다"


# ── 조회 축 강등 사유 표면화 (차단 아님·침묵만 제거) ─────────────────────────


def test_check_surfaces_the_query_axis_downgrade_reason(pm_config, tmp_path, capsys):
    """`--check` 는 상류 선언을 못 읽으면 그 사유를 낸다 — 무경고 green 이 곧 관측 불가다.

    조회 축은 관대 계약이라 rc 는 그대로다(차단은 mutation 축의 몫). 다만 침묵하면 채택자는 상류
    전용 플래그를 한 번도 판정받지 못한 green 을 정상 통과로 읽는다."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=True),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_NEW,
        upstream_tail=None)  # 상류 트리에 선언 package 없음 → 로컬 선언으로 강등.

    rc = config_adapter.cmd_sync_adapter_config(pm_paths.home_at(pm_config.REPO),
        _accept_args(source, check=True), dest_root=dest)

    err = capsys.readouterr().err
    assert rc == 0, err                      # 조회 축은 차단하지 않는다.
    assert "설치본 선언으로 판정한다" in err, f"강등 사유가 버려졌다: {err!r}"
    assert "상류 엔진 package 필수 파일 부재" in err, "사유가 없어 무엇을 고쳐야 할지 알 수 없다"


def test_check_stays_quiet_when_the_upstream_generation_resolves(pm_config, tmp_path, capsys):
    """상류 선언이 읽히면 조용하다 — 강등 경고가 상시 울리면 신호가 죽는다."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=True),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_NEW)

    rc = config_adapter.cmd_sync_adapter_config(pm_paths.home_at(pm_config.REPO),
        _accept_args(source, check=True), dest_root=dest)

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

    result = update_adapters.check_adapter_hook_sets(pm_paths.home_at(pm_update.REPO), dest, source)

    err = capsys.readouterr().err
    assert result["status"] == "ok" and result["findings"] == [], result
    assert "설치본 선언으로 판정한다" in err, f"강등 사유가 버려졌다: {err!r}"
    assert "상류 엔진 package 필수 파일 부재" in err, err


def test_upstream_declarations_do_not_write_into_the_upstream_tree(tmp_path, monkeypatch):
    """상류 선언을 읽는 조회는 상류 트리에 아무것도 만들지 않는다 — 스테이징 소유자는 실행 clone.

    상류는 읽기 대상이다(읽기 전용 마운트·권한·볼륨 고갈이 실 형상). 자기 임시물을 거기 만들면
    읽기만 하는 조회가 상류의 쓰기 권한에 매이고, 사유로 강등되게 돼 있는 `required=False` 경로가
    대신 크래시한다 — 다른 상류 실패 모드(파일 부재·읽기 실패·모듈 손상)는 전부 사유로 내려간다."""
    source = tmp_path / "framework"
    _plant_upstream_package(source)
    anchors: list[Path] = []
    real_temp_root = raw_output.temp_root

    def _recorded_temp_root(repo):
        anchors.append(Path(repo))
        return real_temp_root(repo)

    monkeypatch.setattr(raw_output, "temp_root", _recorded_temp_root)
    generation = hook_sets.hook_set_declarations(_run_home(tmp_path), source, required=True)

    assert generation.origin == hook_sets.HOOK_SET_ORIGIN_UPSTREAM, generation.reasons
    assert generation.declarations, generation.reasons
    assert anchors == [tmp_path / "run_home"], anchors
    assert not (source / ".project_manager" / ".local").exists(), \
        "읽기만 해야 할 상류 트리에 스테이징을 만들었다(앵커가 상류다)"


_SELF_UPDATE_PROGRAM = """
import json, sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from pm_engine.app import hook_sets
from pm_engine.domain import adapter_specs
from pm_engine.infra import install_conf
from pm_engine.infra import pm_paths
from pm_engine.infra import raw_output
from pm_engine.infra import upstream_declarations
declaration = Path(adapter_specs.__file__)
loaded = sorted(adapter_specs.ADAPTER_HOOK_SET["claude"].flag_support)
declaration.write_text(sys.argv[4], encoding="utf-8")
generation = hook_sets.hook_set_declarations(
    pm_paths.home_at(Path(sys.argv[3])), Path(sys.argv[2]), required=True)
print(json.dumps({"declaration": str(declaration.resolve()), "loaded": loaded,
                  "flags": sorted(generation.declarations["claude"].flag_support),
                  "origin": generation.origin, "digest": generation.source_sha256}))
"""


def test_same_path_generation_is_loaded_from_the_hashed_bytes(tmp_path):
    """상류가 **실행 중인 그 package**여도 선언은 해시한 bytes 에서 읽는다(빠른 경로 특례 없음).

    "자기 자신이면 메모리 적재 선언을 쓴다" 는 단축은 정확히 같은 결속 붕괴를 남긴다: 적재된 선언은
    import 시점 코드인데 digest 는 지금 디스크 bytes 의 것이다. 자기 갱신 실행은 실행 도중 그 파일을
    덮으므로(pm-update 가 하는 일이 그것이다) 두 시점이 갈리는 창이 상시 열려 있다. 실행 엔진이 곧
    상류 사본인 형상은 별도 프로세스로 재현한다(이 프로세스의 package 는 하나뿐이다)."""
    source = tmp_path / "framework"
    upstream = _plant_upstream_package(
        source, extra_tail=_flag_declaration_tail(_STALE_PYC_FLAG_A))
    next_generation = (
        (TOOLS / "pm_engine" / "domain" / "adapter_specs.py").read_text(encoding="utf-8")
        + _flag_declaration_tail(_STALE_PYC_FLAG_B))

    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-c", _SELF_UPDATE_PROGRAM,
         str(source / ".project_manager" / "tools"), str(source),
         str(tmp_path / "run_home"), next_generation],
        capture_output=True, text=True, encoding="utf-8", check=True)
    observed = json.loads(completed.stdout)

    assert observed["declaration"] == str(upstream.resolve()), \
        "픽스처 전제 붕괴 — 상류가 실행 중인 그 파일이 아니다(빠른 경로 미도달)"
    assert _STALE_PYC_FLAG_A in observed["loaded"], "픽스처 전제(적재 세대 = A)"
    assert _STALE_PYC_FLAG_B in observed["flags"] and _STALE_PYC_FLAG_A not in observed["flags"], \
        "자기 자신 빠른 경로가 메모리 적재(구) 선언을 신 digest 와 짝지었다(결속 붕괴)"
    assert observed["origin"] == hook_sets.HOOK_SET_ORIGIN_UPSTREAM, observed
    assert observed["digest"] == _package_digest(source), "digest 가 그 bytes 의 것이 아니다"


def test_staging_cleanup_failure_does_not_fail_the_declaration_load(
        tmp_path, monkeypatch, capsys):
    """스테이징 뒷정리 실패가 상류 로드 실패로 번역되지 않는다 — 선언은 이미 읽혔다.

    삭제 실패는 Windows 실 클래스다(핸들 잠금·AV 스캔). 그것을 로드 경계 안에 묶으면 사유 한 줄이
    `declarations=None` 으로 번역되고, mutation 게이트가 **근거 없이** fail-closed 로 떨어진다."""
    source = tmp_path / "framework"
    _plant_upstream_package(
        source, extra_tail=_flag_declaration_tail(_STALE_PYC_FLAG_A))
    leaked: list[str] = []

    def _locked_rmtree(path, *args, **kwargs):
        leaked.append(str(path))
        raise OSError(f"임시 디렉토리 정리 실패(주입): {path}")

    monkeypatch.setattr(install_conf, "_force_rmtree", _locked_rmtree)
    try:
        generation = hook_sets.hook_set_declarations(_run_home(tmp_path), source, required=True)
    finally:
        monkeypatch.undo()
        for path in leaked:
            shutil.rmtree(path, ignore_errors=True)

    assert leaked, "스테이징 정리 경로에 도달하지 않았다(단언이 공허하다)"
    assert generation.declarations is not None, generation.reasons
    assert _STALE_PYC_FLAG_A in generation.declarations["claude"].flag_support
    assert generation.origin == hook_sets.HOOK_SET_ORIGIN_UPSTREAM, generation.origin
    assert leaked[0] in capsys.readouterr().err, "경고에 남은 임시 경로가 없다"


def test_query_fallback_wording_has_one_owner(pm_update, pm_config,
                                              tmp_path, capsys):
    """강등 안내 문구는 hook_sets 단일 진실이고 두 소비자가 **같은 문장**을 낸다.

    사본을 두면 한쪽만 고쳐지고, 채택자는 같은 상태를 서로 다른 말로 듣는다(게이트가 둘로 보인다)."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=True),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_NEW,
        upstream_tail=None)  # 상류 선언 package 부재 → 조회 축 강등.
    owned = hook_sets.hook_set_query_fallback_lines(
        hook_sets.hook_set_declarations(_run_home(tmp_path), source))
    assert owned and "설치본 선언으로 판정한다" in owned[0], owned

    update_adapters.check_adapter_hook_sets(pm_paths.home_at(pm_update.REPO), dest, source)
    sync_err = capsys.readouterr().err
    config_adapter.cmd_sync_adapter_config(pm_paths.home_at(pm_config.REPO),
        _accept_args(source, check=True), dest_root=dest)
    check_err = capsys.readouterr().err

    assert owned[0] in sync_err, sync_err
    assert owned[0] in check_err, check_err
    assert hook_sets.hook_set_query_fallback_lines(
        hook_sets.hook_set_declarations(_run_home(tmp_path), REPO)) == [], "해소되면 안내가 없어야 한다"


# ── 커맨드 표기 커버리지 · 문구·API 정리 (T-0612) ─────────────────────────────
# 판정 단위는 **실행되는 스크립트와 그 플래그**다. 표기(인터프리터 선행·플랫폼별 키)가 달라도 같은
# 훅이면 같은 판정을 타야 한다 — 표기 하나로 판정에서 빠지면 그 형상의 채택자만 락아웃을 그대로 받는다.


def test_interpreter_prefixed_command_is_judged_like_a_direct_one(tmp_path):
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

    findings = hook_sets.judge_adapter_hook_sets(dest, source)

    assert [(f.kind, f.subject, f.unmet_paths) for f in findings] == [
        (hook_sets.HOOK_SET_UNSUPPORTED_FLAG, _GIT_ANCHOR_FLAG, (_DRIVER_REL,))], findings


def test_interpreter_options_before_the_script_are_skipped():
    """인터프리터 옵션(`py -3.12 <path>`)을 건너뛰고 스크립트를 집는다 — 인자도 그 뒤부터."""
    script, arguments = adapter_documents._hook_script_and_arguments(
        ["py", "-3.12", "${CLAUDE_PROJECT_DIR}/" + _WRAPPER_REL, _GIT_ANCHOR_FLAG])

    assert script == _WRAPPER_REL and arguments == [_GIT_ANCHOR_FLAG]


def test_non_interpreter_first_token_keeps_the_direct_reading():
    """인터프리터 목록 밖 첫 토큰은 종전대로 스크립트로 읽는다(과잉 해석 금지)."""
    assert adapter_documents._hook_script_and_arguments(
        ["./" + _WRAPPER_REL, _GIT_ANCHOR_FLAG]) == (_WRAPPER_REL, [_GIT_ANCHOR_FLAG])
    assert adapter_documents._hook_script_and_arguments(["bash"]) == (None, [])


def test_windows_command_key_is_scanned_like_the_posix_one(tmp_path):
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

    commands = adapter_documents._hook_commands(document)
    unmet = hook_sets._hook_set_demands(
        document, adapter_specs.ADAPTER_HOOK_SET["claude"], dest)

    assert len(commands) == 2, commands
    assert unmet == {(hook_sets.HOOK_SET_UNSUPPORTED_FLAG, _GIT_ANCHOR_FLAG):
                     (_DRIVER_REL,)}, \
        "Windows 커맨드만 요구하는 플래그가 판정에서 빠졌다"


def test_remedy_line_omits_empty_parentheses(tmp_path):
    """지목할 파일이 없는 소견은 빈 괄호 `()` 없이 처방한다 — 깨진 문장을 내지 않는다.

    상류 세대 미해소 blocker 가 그 형상이다(`unmet_paths=()`) — 무엇이 미충족인지 열거할 수 없다."""
    dest, source = _make_case(
        tmp_path, dest_settings=_settings(git_anchor=True),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_NEW,
        upstream_tail=None)  # 상류 선언 package 부재 → 해소 불가 blocker.
    decision = hook_sets.hook_set_accept_decision(
        dest, source, _SETTINGS_REL,
        declarations=hook_sets.hook_set_declarations(_run_home(tmp_path), source, required=True))
    blocker = decision.blockers[0]
    assert blocker.unmet_paths == (), blocker

    lines = hook_sets.hook_set_remedy_lines(blocker)

    assert lines and all("()" not in line for line in lines), lines
    assert any("먼저 받아라" in line for line in lines), lines
    # 경계 반대편 — 지목할 파일이 있으면 종전대로 괄호로 열거한다.
    stale = hook_sets.judge_adapter_hook_sets(*_make_case(
        tmp_path / "stale", dest_settings=_settings(git_anchor=True),
        dest_wrapper=_WRAPPER_NEW, dest_driver=_DRIVER_OLD))[0]
    assert f"({_DRIVER_REL})" in hook_sets.hook_set_remedy_lines(stale)[0]


def test_coupled_hook_set_paths_takes_declarations_keyword_only():
    """세대 선언은 형제 API 전부와 같이 **kw-only** 다 — 위치인자 표기가 하나만 다르면 안 된다."""
    alpha = adapter_specs.ADAPTER_HOOK_SET["claude"].coupled_groups[0][0]

    assert hook_sets.coupled_hook_set_paths([alpha]) == (alpha,)
    assert hook_sets.coupled_hook_set_paths(
        [alpha], declarations=adapter_specs.ADAPTER_HOOK_SET) == (alpha,)
    with pytest.raises(TypeError):
        hook_sets.coupled_hook_set_paths([alpha], adapter_specs.ADAPTER_HOOK_SET)


# ── 기능 표(관리자 읽기 계약)가 훅 세트 판정을 바꾸지 않는다 (T-0907·AT-005) ─────
# `status --json --with-adapters` 의 기능 표는 같은 선언(`ADAPTER_HOOK_SET`)과 같은 실재 판정
# (`_entrypoint_is_present`)을 소비한다. 그 배선이 들어오면서 훅 커맨드 수집이 이벤트별로
# 갈렸는데(`_hook_commands` → `_hook_event_commands` 평탄화), **판정 입력이 한 커맨드라도
# 달라지면** 세대 게이트가 조용히 다른 것을 보게 된다. 아래는 실 출하 template 으로 그 축을 고정한다.


def _shipped_hook_configs() -> list[tuple[str, Path, dict]]:
    """출하 template 의 훅 선언 config 전수 — (하네스, template 루트, 파싱 문서)."""
    out = []
    for harness, spec in adapter_specs.ADAPTER_HOOK_SET.items():
        if not spec.config_relpath:
            continue
        template_root = REPO / "templates" / install_rules.HARNESS_TEMPLATE_DIRS[harness][0]
        out.append((harness, template_root, json.loads(
            (template_root / spec.config_relpath).read_text(encoding="utf-8"))))
    assert out, "훅 선언 config 를 가진 하네스가 없다(공허 가드)"
    return out


def test_hook_set_spec_fields_are_unchanged_by_the_settings_key_declaration():
    """설정 키 선언이 `AdapterHookSetSpec` 에 필드로 얹히지 않았다 — 키는 **config 파일** 축이다.

    훅 세트 선언에 필드를 더하면 그 선언을 읽는 판정 전부(세대 게이트·원자 write 대상·결합 묶음)가
    같은 튜플 모양에 묶인다. 설정 키는 opencode(`config_relpath` 빈 문자열)와 `.codex/config.toml`
    (훅 세트 밖)에 달 자리가 없어 파일 단위 표로 분리했다."""
    assert adapter_specs.AdapterHookSetSpec._fields == (
        "config_relpath", "live_files", "flag_support", "coupled_groups", "entrypoints")
    channel_paths = {relpath
                     for channel in install_rules.ADAPTER_CONFIG_CHANNEL.values()
                     for relpath in channel}
    assert set(install_rules.ADAPTER_SETTINGS_KEYS) <= channel_paths, \
        "설정 키 선언의 키는 config 파일 relpath 여야 한다(하네스 이름 축 금지)"
    assert all(keys for keys in install_rules.ADAPTER_SETTINGS_KEYS.values()), \
        "빈 키 목록은 그 파일의 기능 축을 통째로 끈다"


def test_declared_settings_keys_resolve_in_the_shipped_templates():
    """선언한 설정 키 전수가 그 하네스 **출하 template** 문서에서 present 로 해소된다.

    선언과 실체가 갈리면 조용히 false-green 이다 — 양쪽 문서에 없는 키는 `_feature_verdict` 가
    None 이라 `_adapter_feature_rows` 가 행 자체를 건너뛰고, 기능 표의 관측 범위와 `distance`
    분모가 소리 없이 줄어 관리자는 그 축을 "차이 없음" 으로 읽는다. 자기 대조 가드
    (`test_shipped_config_has_no_feature_distance_against_itself`)는 양쪽 부재를 same 으로 접어
    이 축을 못 잡는다."""
    template_dir_by_relpath = {
        relpath: install_rules.HARNESS_TEMPLATE_DIRS[harness][0]
        for harness, channel in install_rules.ADAPTER_CONFIG_CHANNEL.items()
        for relpath in channel
    }
    declared = sum(len(keys) for keys in install_rules.ADAPTER_SETTINGS_KEYS.values())
    assert declared > 0, "선언이 비면 이 가드는 공허하다"

    resolved = 0
    for relpath, keys in install_rules.ADAPTER_SETTINGS_KEYS.items():
        template_path = (REPO / "templates" / template_dir_by_relpath[relpath] / relpath)
        document = adapter_documents._load_adapter_config_document(template_path)
        assert document, f"{relpath}: 출하 template 을 문서로 못 읽었다"
        for key in keys:
            present, _value = adapter_report._document_key_value(document, key)
            assert present, (
                f"{relpath}: 선언 키 {key!r} 가 출하 template 에 없다 — 상류에서 지워졌거나 "
                "이름이 바뀌었다(그 키의 기능 행이 표에서 사라져 distance 가 조용히 줄어든다)")
            resolved += 1
    assert resolved == declared


def test_event_split_preserves_the_hook_command_inventory():
    """이벤트별 수집의 평탄화 == 세대 판정이 보던 커맨드 전수(순서 포함) — 실 출하 config 로.

    커맨드 하나가 빠지면 `_hook_set_demands` 가 그 커맨드의 요구를 못 보고 조용히 통과한다."""
    for harness, template_root, document in _shipped_hook_configs():
        expected = [
            entry[key]
            for groups in document["hooks"].values()
            for group in groups
            for entry in group["hooks"]
            for key in ("command", "commandWindows")
            if isinstance(entry.get(key), str) and entry[key].strip()
        ]
        assert adapter_documents._hook_commands(document) == expected, harness
        assert [command
                for commands in adapter_documents._hook_event_commands(document).values()
                for _matcher, command in commands] == expected, harness
        # 판정 자체도 그대로 조용하다(출하 template 자기정합·claude 축과 같은 근거).
        assert hook_sets._hook_set_demands(document, adapter_specs.ADAPTER_HOOK_SET[harness],
                                           template_root) == {}, harness


def test_feature_axis_reuses_the_entrypoint_presence_judgment():
    """기능 표의 이벤트 축은 진입점 **실재 판정**을 그대로 쓴다(두 번째 판정 신설 0).

    출하 codex 커맨드는 인터프리터 해소를 담은 셸 한 줄이라 argv 분해로는 스크립트가 안 나온다 —
    실재 판정을 병용하지 않으면 손대지 않은 config 도 이벤트마다 다름으로 실린다."""
    seen_any = False
    for harness, _template_root, document in _shipped_hook_configs():
        spec = adapter_specs.ADAPTER_HOOK_SET[harness]
        events = adapter_report._hook_event_units(document, spec.entrypoints)
        for entrypoint in spec.entrypoints:
            seen_any = True
            present = adapter_documents._entrypoint_is_present(document, entrypoint)
            assert present, f"{harness}:{entrypoint.event} 출하 config 가 진입점을 잃었다"
            unit = f"{entrypoint.matcher or ''}:{entrypoint.dispatcher}"
            assert unit in events.get(entrypoint.event, ()), \
                f"{harness}:{entrypoint.event} 실재 판정과 기능 축이 갈렸다"
    assert seen_any, "진입점 선언이 하나도 없다(공허 가드)"


# ── 이벤트 축 단위 = (matcher, 스크립트, 인자) (T-0918·X-1) ────────────────────
# 값이 스크립트 **집합**이던 동안은 출하 claude `PreToolUse` 의 `Bash` 그룹(같은 래퍼를
# `--git-anchor-hook` 로 부른다)을 통째로 지워도 집합이 안 변해 표가 `same` 으로 접혔다 —
# 채택자가 git anchor 가드를 잃은 상태가 관리자 화면에서 "차이 없음" 이었다.

_GIT_ANCHOR_MATCHER = "Bash|Write|Edit|MultiEdit|NotebookEdit|Agent|Task"
_SHIPPED_CLAUDE_UNITS = {
    "PreToolUse": ("*:.claude/ctx_stop_hook.sh",
                   "Agent:.claude/delegate_channel_guard_hook.sh",
                   f"{_GIT_ANCHOR_MATCHER}:.claude/ctx_stop_hook.sh --git-anchor-hook"),
    "UserPromptSubmit": (":.claude/ctx_stop_hook.sh",
                         ":.claude/ctx_stop_hook.sh --git-anchor-hook"),
}


def test_hook_event_units_keep_matcher_and_arguments():
    """출하 claude settings.json 의 두 이벤트 단위가 실값 그대로다(matcher · 인자 포함)."""
    spec = adapter_specs.ADAPTER_HOOK_SET["claude"]
    document = json.loads(
        (REPO / "templates" / "claude_code" / spec.config_relpath).read_text(
            encoding="utf-8"))

    units = adapter_report._hook_event_units(document, spec.entrypoints)

    for event, expected in _SHIPPED_CLAUDE_UNITS.items():
        assert units[event] == tuple(sorted(expected)), event


def test_removing_one_matcher_group_is_a_feature_difference():
    """출하 settings.json 에서 `Bash` matcher 그룹만 지우면 그 이벤트가 `differs` 다.

    같은 스크립트가 `*` 그룹에도 있으므로 스크립트 집합으로는 안 잡히는 형상이다."""
    spec = adapter_specs.ADAPTER_HOOK_SET["claude"]
    latest = json.loads(
        (REPO / "templates" / "claude_code" / spec.config_relpath).read_text(
            encoding="utf-8"))
    project = json.loads(json.dumps(latest))
    project["hooks"]["PreToolUse"] = [
        group for group in project["hooks"]["PreToolUse"]
        if group.get("matcher") != _GIT_ANCHOR_MATCHER
    ]
    assert project != latest, "픽스처가 아무것도 지우지 않았다(공허 가드)"

    rows = adapter_report._adapter_feature_rows(spec.config_relpath, project, latest)

    row = next(row for row in rows if row.name == "hooks.PreToolUse")
    assert row.verdict == adapter_report.ADAPTER_FEATURE_DIFFERS
    unit = f"{_GIT_ANCHOR_MATCHER}:.claude/ctx_stop_hook.sh --git-anchor-hook"
    assert unit not in row.project
    assert unit in row.latest
    distance = sum(1 for row in rows
                   if row.verdict != adapter_report.ADAPTER_FEATURE_SAME)
    assert distance >= 1


def test_shipped_config_has_no_feature_distance_against_itself():
    """template 과 바이트가 같은 config 의 기능 거리는 0 이다 — 표기 때문에 다름이 생기지 않는다.

    (`pm-config status --json --with-adapters` 의 거리 축·실측 회귀: codex 훅 커맨드 형상에서
    손대지 않은 `.codex/hooks.json` 이 거리 6 으로 실렸다.)"""
    for harness, channel in install_rules.ADAPTER_CONFIG_CHANNEL.items():
        template_root = REPO / "templates" / install_rules.HARNESS_TEMPLATE_DIRS[harness][0]
        for relpath, mode in channel.items():
            if mode == install_rules.ADAPTER_CONFIG_NO_CHANNEL:
                continue
            document = adapter_documents._load_adapter_config_document(template_root / relpath)
            assert document, f"{relpath}: 출하 template 을 문서로 못 읽었다"
            rows = adapter_report._adapter_feature_rows(relpath, document, document)
            assert [row for row in rows
                    if row.verdict != adapter_report.ADAPTER_FEATURE_SAME] == [], relpath
