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


def _settings(*, git_anchor: bool, wrapper_rel: str = _WRAPPER_REL) -> str:
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
                       "command": ("${CLAUDE_PROJECT_DIR}/" + wrapper_rel
                                   + f" {_GIT_ANCHOR_FLAG}"),
                       "timeout": 15}],
        })
    return json.dumps({"hooks": {"PreToolUse": groups}},
                      ensure_ascii=False, indent=2) + "\n"


def _make_case(tmp_path: Path, *, dest_settings: str, dest_wrapper: str | None,
               dest_driver: str | None, template_settings: str | None = None,
               template_wrapper: str = _WRAPPER_NEW,
               template_driver: str = _DRIVER_NEW,
               ledger: dict | None = None) -> tuple[Path, Path]:
    """(dest, source) — claude 채택자 + 합성 프레임워크.

    `dest_wrapper`/`dest_driver` 를 None 으로 주면 그 파일이 **설치되지 않은** 형상이다.
    `ledger` 는 어댑터 원장(설치 시점 해시) — `edited` 판정을 만들려면 dest 와 다른 해시를 준다."""
    source = tmp_path / "framework"
    template = source / "templates" / "claude_code"
    (template / ".claude").mkdir(parents=True)
    (template / _SETTINGS_REL).write_text(
        template_settings if template_settings is not None else dest_settings,
        encoding="utf-8")
    (template / _WRAPPER_REL).write_text(template_wrapper, encoding="utf-8")
    (template / _DRIVER_REL).write_text(template_driver, encoding="utf-8")

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
# 다음 세대 pm_import 가 들고 오는 선언 — 판정자 자신이 pm_import 안에 살기 때문에, dest 사본의
# 선언은 업그레이드 시점에 정의상 한 세대 뒤다.
_SOURCE_PM_IMPORT = (
    "def is_live_hook_set_path(relpath):\n"
    f"    return str(relpath) == {_NEXT_GENERATION_HOOK_REL!r}\n"
)


def test_apply_uses_source_generation_declaration_not_the_installed_one(
        pm_update, tmp_path, monkeypatch):
    """이번 세대가 **새로 추가한** 훅 경로도 같은 실행에서 원자 교체된다(구세대 판정자 폐쇄).

    판정자를 dest 사본에서 읽으면 원자 write 가 영영 한 세대 늦게 도착한다 — pm_import 자체를
    갱신하는 바로 그 실행에서, 새 세대가 등재한 훅 파일이 copy2 로 떨어진다."""
    source = tmp_path / "framework"
    (source / ".project_manager" / "tools").mkdir(parents=True)
    (source / ".project_manager" / "tools" / "pm_import.py").write_text(
        _SOURCE_PM_IMPORT, encoding="utf-8")
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
