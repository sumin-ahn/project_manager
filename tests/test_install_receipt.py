"""영속 설치 기록(install receipt) — 설치 하네스 판정의 원천을 추론에서 기록으로 옮긴다.

증거 추론(`_pm_install_evidence`)은 구조상 두 방향으로 틀린다: 전용 판별자가 둘뿐인 하네스는 그
파일이 함께 사라지면 **미검출**(표기 유실)이고, 채택자 자작 진입문서는 **거짓 양성**(표기 소음)이다.
설치·추가 시점에 사실을 박제하고(`record_install_receipt`) 판정이 그걸 1순위로 읽으면
(`installed_harnesses`) 앞의 한계가 기록 보유 인스턴스에서 사라진다.

이 파일이 못박는 축:
  - 기록 시점 — `--new`·`--into`·add-harness 의 **적용 경로**만(계획·질의는 안 쓴다).
  - 기록 소비 — 판정이 기록을 진실로 쓰고(전용 증거 전부 삭제해도 검출 유지) 자기 갱신
    (`pm_update` 표기 독자 seam)도 같은 판정을 탄다.
  - 기록 부재 — 구 인스턴스는 추론 폴백 + 다음 설치 행위에서 backfill.
  - 기록 위치의 근거 — git 추적(다중 clone 공유)·manifest 미등재(pm_update clobber 면역).
  - 깨진 기록 — 조용한 강등 0(경고 후 추론 폴백).

실 하네스·네트워크 0 — 설치는 tmp_path 안 `--new`/`--into` 이고 opencode 모델 조회는 고정한다.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import shutil
from pathlib import Path

import pytest

from _win_skip import _can_symlink

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

requires_symlink = pytest.mark.skipif(
    not _can_symlink(), reason="Windows: symlink requires Developer Mode/admin")


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def pm_import():
    return _load("pm_import")


@pytest.fixture(scope="module")
def pm_update():
    return _load("pm_update")


@pytest.fixture(autouse=True)
def _hermetic_opencode_models(pm_import, monkeypatch):
    """실 `opencode models` CLI 미호출 고정(미설치 동치·hermetic)."""
    monkeypatch.setattr(pm_import, "_real_models_runner", lambda: (False, []))


def _new_instance(pm_import, dest: Path, harness: str = "claude") -> Path:
    rc = pm_import.main(
        ["--new", str(dest), "--harness", harness, "--fill", "manual",
         "--from", str(REPO), "--name", "Receipt Inst"])
    assert rc == 0, f"인스턴스 셋업 실패(rc={rc})"
    return dest


def _receipt_path(pm_import, dest: Path) -> Path:
    return dest / pm_import.INSTALL_RECEIPT_RELPATH


def _receipt(pm_import, dest: Path) -> dict:
    return json.loads(_receipt_path(pm_import, dest).read_text(encoding="utf-8"))


def _drop_receipt(pm_import, dest: Path) -> None:
    """기록 도입 **이전** 인스턴스 형상 — 어댑터는 그대로 두고 기록만 없앤다."""
    _receipt_path(pm_import, dest).unlink()


# ── 기록 시점: 설치·추가가 실제로 일어난 run 만 쓴다 ─────────────────────────

def test_new_import_records_the_installed_harness(pm_import, tmp_path):
    dest = _new_instance(pm_import, tmp_path / "new_claude", "claude")

    assert _receipt(pm_import, dest)["harnesses"] == ["claude"]
    assert pm_import.installed_harnesses(dest, REPO) == ["claude"]


def test_add_harness_extends_the_receipt(pm_import, tmp_path):
    dest = _new_instance(pm_import, tmp_path / "add_codex", "claude")

    pm_import.add_harness(dest, "codex", dry_run=False, source_root=REPO)

    assert _receipt(pm_import, dest)["harnesses"] == ["claude", "codex"]
    assert pm_import.installed_harnesses(dest, REPO) == ["claude", "codex"]


def test_into_records_the_union_of_existing_and_selected(pm_import, tmp_path):
    """`--into` 는 이번 선택만이 아니라 **기존 독자까지** 기록한다(표기 독자 집합과 같은 값)."""
    dest = _new_instance(pm_import, tmp_path / "into_union", "codex")

    assert pm_import.main(
        ["--into", str(dest), "--harness", "claude", "--fill", "manual",
         "--from", str(REPO), "--name", "Receipt Inst"]) == 0

    assert _receipt(pm_import, dest)["harnesses"] == ["claude", "codex"]


def test_planning_paths_do_not_write_a_receipt(pm_import, tmp_path, capsys):
    """판정·계획은 기록을 만들지 않는다 — dry-run 무변경 계약과 질의 순수성.

    기록을 판정 함수에서 쓰면 `--dry-run`·read-only 갱신 확인 같은 무변경 명령이 인스턴스를 고치고,
    비-PM 트리에도 기록이 생긴다. 그래서 쓰기는 적용 경로에만 있다."""
    dest = _new_instance(pm_import, tmp_path / "no_write_on_query", "claude")
    _drop_receipt(pm_import, dest)

    assert pm_import.installed_harnesses(dest, REPO) == ["claude"]  # 추론 폴백.
    assert not _receipt_path(pm_import, dest).exists(), "질의가 기록을 만들었다"

    capsys.readouterr()
    pm_import.add_harness(dest, "codex", dry_run=True, source_root=REPO)
    assert not _receipt_path(pm_import, dest).exists(), "dry-run 이 기록을 만들었다"

    assert pm_import.main(
        ["--into", str(dest), "--harness", "codex", "--fill", "manual", "--dry-run",
         "--from", str(REPO), "--name", "Receipt Inst"]) == 0
    assert not _receipt_path(pm_import, dest).exists(), "import dry-run 이 기록을 만들었다"


def test_recording_is_idempotent_for_the_same_set(pm_import, tmp_path):
    """같은 집합 재기록은 무변경 — 재-import 마다 byte 가 흔들리지 않는다(git diff churn 0)."""
    dest = _new_instance(pm_import, tmp_path / "idempotent", "claude")
    before = _receipt_path(pm_import, dest).read_bytes()

    assert pm_import.record_install_receipt(dest, ["claude"]) is False
    assert _receipt_path(pm_import, dest).read_bytes() == before

    assert pm_import.record_install_receipt(dest, ["claude", "codex"]) is True
    assert _receipt(pm_import, dest)["harnesses"] == ["claude", "codex"]


# ── 기록 소비: 추론 한계 케이스 폐쇄 ─────────────────────────────────────────

def _erase_exclusive_evidence(dest: Path, harness: str) -> list[str]:
    """그 하네스 **전용** PM 판별자를 전부 지운다 — 추론이 미검출로 떨어지는 형상."""
    erased = []
    for rel in {
        # `.opencode/command` 는 T-0674 로 복원된 전용 증거다(팔레트 사본 채널).
        "opencode": (".opencode/pm-instructions.md", ".opencode/pm_orch_opencode.py",
                     ".opencode/command"),
        "claude": (".claude/pm_orch_claude.py",),
    }[harness]:
        target = dest / rel
        if target.is_dir():
            shutil.rmtree(target)
            erased.append(rel)
        elif target.exists():
            target.unlink()
            erased.append(rel)
    return erased


def test_receipt_keeps_detection_after_every_exclusive_marker_is_gone(
        pm_import, tmp_path):
    """전용 판별자를 **전부** 지워도 기록이 있으면 검출은 유지된다(추론 한계 폐쇄).

    sensitivity: 같은 트리에서 기록만 없애면 옛 추론 판정이 그 하네스를 통째로 놓친다 — 이 기록이
    load-bearing 이라는 증거이자, 그 미검출이 곧 공유 문서 재렌더에서의 표기 유실이다."""
    dest = _new_instance(pm_import, tmp_path / "evidence_gone", "opencode")
    assert _erase_exclusive_evidence(dest, "opencode"), "픽스처 전제(전용 판별자 실재)"

    assert pm_import.installed_harnesses(dest, REPO) == ["opencode"]

    _drop_receipt(pm_import, dest)
    assert pm_import.installed_harnesses(dest, REPO) == [], \
        "추론 폴백이 이 형상을 검출하면 이 테스트는 한계 케이스가 아니다"


def test_self_update_notation_reader_set_follows_the_receipt(
        pm_import, pm_update, tmp_path):
    """`pm_update` 표기 독자 seam 도 같은 판정을 탄다 — 판정 사본 0.

    설치(pm_import)만 기록을 보고 갱신(pm_update)이 추론을 들고 있으면, 다음 자기 갱신이 공유
    문서를 그 하네스 없는 표기로 되돌린다(판정 사본이 만드는 재발 경로)."""
    dest = _new_instance(pm_import, tmp_path / "update_seam", "opencode")
    _erase_exclusive_evidence(dest, "opencode")

    selected = pm_update._installed_entry_notation_manifests(dest, REPO, [])

    assert [p.parent.parent.name for p in selected] == ["opencode"], selected


def test_non_pm_tree_stays_undetected_without_a_receipt(pm_import, tmp_path):
    """비-PM 트리 오검출 0 불변 — 기록이 없으면 추론이 그대로 판정한다(거짓 양성 축 무변)."""
    dest = tmp_path / "own_codex"
    (dest / ".codex" / "agents").mkdir(parents=True)
    (dest / ".codex" / "agents" / "my-agent.toml").write_text("model = 'x'\n", encoding="utf-8")
    (dest / ".agents" / "skills" / "my-skill").mkdir(parents=True)
    (dest / ".agents" / "skills" / "my-skill" / "SKILL.md").write_text("# mine\n", encoding="utf-8")
    (dest / "AGENTS.md").write_text("# 우리 프로젝트 규약\n", encoding="utf-8")

    assert pm_import.installed_harnesses(dest, REPO) == []
    assert not _receipt_path(pm_import, dest).exists()


# ── 기록 부재: 폴백 + 다음 설치 행위에서 backfill ────────────────────────────

def test_legacy_instance_falls_back_then_backfills_on_add_harness(pm_import, tmp_path):
    """기록 없는 구 인스턴스 → 추론 폴백. add-harness 가 그 산출을 기록으로 옮긴다(원천 정합)."""
    dest = _new_instance(pm_import, tmp_path / "backfill_add", "claude")
    _drop_receipt(pm_import, dest)

    pm_import.add_harness(dest, "codex", dry_run=False, source_root=REPO)

    assert _receipt(pm_import, dest)["harnesses"] == ["claude", "codex"], \
        "추론으로 찾은 기존 하네스가 backfill 에서 빠졌다(독자 유실)"


def test_legacy_instance_backfills_on_reimport(pm_import, tmp_path):
    """`--into` 재-import 도 같은 backfill 지점이다 — 채택자의 표준 업그레이드 경로."""
    dest = _new_instance(pm_import, tmp_path / "backfill_into", "claude")
    _drop_receipt(pm_import, dest)

    assert pm_import.main(
        ["--into", str(dest), "--harness", "claude", "--fill", "manual",
         "--from", str(REPO), "--name", "Receipt Inst"]) == 0

    assert _receipt(pm_import, dest)["harnesses"] == ["claude"]


# ── 기록 위치의 근거: git 추적(clone 공유) · manifest 미등재(clobber 면역) ───

def test_receipt_is_git_tracked_scope_not_per_clone(pm_import, tmp_path):
    """기록은 git 무시 대상이 아니다 — 기록이 서술하는 어댑터 파일과 같은 채널로 clone 을 탄다.

    per-clone(local.conf) 자리에 두면 같은 저장소를 clone 한 두 번째 사용자에겐 기록이 없어, 어댑터
    파일이 다 있는데도 판정이 추론으로 내려간다(이 기록이 닫으려는 한계가 그 clone 에서 부활)."""
    dest = _new_instance(pm_import, tmp_path / "tracked", "claude")
    relpath = pm_import.INSTALL_RECEIPT_RELPATH.as_posix()

    ignored = subprocess.run(
        ["git", "-C", str(dest), "check-ignore", "-q", relpath],
        capture_output=True, text=True)

    assert ignored.returncode == 1, f"{relpath} 가 git-ignored — clone 공유가 끊긴다"
    # 대조군: per-clone 파일(local.conf)은 실제로 무시된다(가드 비공허성).
    assert subprocess.run(
        ["git", "-C", str(dest), "check-ignore", "-q", ".project_manager/local.conf"],
        capture_output=True, text=True).returncode == 0


def test_receipt_is_outside_the_engine_manifest(pm_import, pm_update):
    """기록은 manifest 미등재 — `pm_update` 동기 대상이 아니라 자기 갱신이 덮지 않는다."""
    declared = {
        str(entry).replace("\\", "/")
        for entry in pm_update.read_manifest(REPO / ".project_manager" / "engine.manifest")
    }
    relpath = pm_import.INSTALL_RECEIPT_RELPATH.as_posix()

    assert not any(relpath == d or relpath.startswith(d.rstrip("/") + "/") for d in declared), \
        f"{relpath} 가 manifest 소유 — pm_update 가 덮어 기록이 흔들린다"


def test_update_does_not_clobber_the_receipt(pm_import, pm_update, tmp_path, monkeypatch):
    """자기 갱신을 한 번 돌려도 기록 byte 는 그대로다(면역 실측·선언만이 아님)."""
    dest = _new_instance(pm_import, tmp_path / "update_keeps", "claude")
    before = _receipt_path(pm_import, dest).read_bytes()
    monkeypatch.setattr(pm_update, "REPO", dest)

    assert pm_update.main(["--from", str(REPO)]) == 0

    assert _receipt_path(pm_import, dest).read_bytes() == before


# ── 깨진 기록: 조용한 강등 0 ────────────────────────────────────────────────

def test_corrupt_receipt_falls_back_loudly(pm_import, tmp_path, capsys):
    dest = _new_instance(pm_import, tmp_path / "corrupt", "claude")
    _receipt_path(pm_import, dest).write_text("{ not json", encoding="utf-8")
    capsys.readouterr()

    assert pm_import.installed_harnesses(dest, REPO) == ["claude"]  # 추론 폴백.

    err = capsys.readouterr().err
    assert "설치 기록" in err and "증거 추론" in err, err


def test_receipt_without_harness_list_falls_back_loudly(pm_import, tmp_path, capsys):
    dest = _new_instance(pm_import, tmp_path / "no_key", "claude")
    _receipt_path(pm_import, dest).write_text('{"schema": 1}\n', encoding="utf-8")
    capsys.readouterr()

    assert pm_import.installed_harnesses(dest, REPO) == ["claude"]
    assert "설치 기록" in capsys.readouterr().err


def test_unregistered_harness_in_receipt_is_excluded_loudly(pm_import, tmp_path, capsys):
    """미등록 이름은 판정에서 빼되 알린다 — registry 조회에서 터지게 두지도, 삼키지도 않는다."""
    dest = _new_instance(pm_import, tmp_path / "unknown", "claude")
    _receipt_path(pm_import, dest).write_text(
        '{"schema": 1, "harnesses": ["claude", "fourth"]}\n', encoding="utf-8")
    capsys.readouterr()

    assert pm_import.installed_harnesses(dest, REPO) == ["claude"]
    assert "fourth" in capsys.readouterr().err


def test_receipt_with_only_unknown_names_is_treated_as_absent(pm_import, tmp_path, capsys):
    """유효 항목 0 은 기록 없음과 같게 다룬다 — 독자 0 으로 굳으면 표기가 통째로 유실된다."""
    dest = _new_instance(pm_import, tmp_path / "all_unknown", "claude")
    _receipt_path(pm_import, dest).write_text(
        '{"schema": 1, "harnesses": ["fourth"]}\n', encoding="utf-8")
    capsys.readouterr()

    assert pm_import.installed_harnesses(dest, REPO) == ["claude"]  # 추론 폴백.


@requires_symlink
def test_receipt_channel_does_not_follow_a_symlinked_path(pm_import, tmp_path, capsys):
    """기록 경로가 저장소 밖 지향 symlink 로 바뀌면 읽지도 쓰지도 않는다(loud 폴백).

    기록도 dest 파일이라 다른 내용 IO 채널과 같은 fd 규율을 탄다 — 경로 재열기로 밖을 고치지 않는다."""
    dest = _new_instance(pm_import, tmp_path / "linked_receipt", "claude")
    outside = tmp_path / "OUTSIDE_RECEIPT.json"
    outside.write_text('{"schema": 1, "harnesses": ["codex"]}\n', encoding="utf-8")
    receipt = _receipt_path(pm_import, dest)
    receipt.unlink()
    receipt.symlink_to(outside)
    capsys.readouterr()

    assert pm_import.installed_harnesses(dest, REPO) == ["claude"], \
        "링크를 따라가 밖의 기록을 진실로 삼음"
    assert "설치 기록" in capsys.readouterr().err

    assert pm_import.record_install_receipt(dest, ["claude", "codex"]) is False
    assert json.loads(outside.read_text(encoding="utf-8"))["harnesses"] == ["codex"], \
        "링크를 따라 저장소 밖 파일을 덮음"
    # 안전하게 **읽을 수 없는** 경로는 쓰기 시도 자체를 하지 않는다(더 이른 거부·같은 결과).
    assert "읽을 수 없어 갱신하지 않습니다" in capsys.readouterr().err


# ── 깨진 기록의 남은 두 형상: 빈 목록 · 상위 schema ──────────────────────────

def test_empty_harness_list_is_loud_not_silent(pm_import, tmp_path, capsys):
    """`{"harnesses": []}` 는 정상 상태가 아니라 잘린·손상된 기록이다 — 알리고 추론으로 내려간다."""
    dest = _new_instance(pm_import, tmp_path / "empty_list", "claude")
    _receipt_path(pm_import, dest).write_text(
        '{"schema": 1, "harnesses": []}\n', encoding="utf-8")
    capsys.readouterr()

    assert pm_import.installed_harnesses(dest, REPO) == ["claude"]  # 추론 폴백.

    err = capsys.readouterr().err
    assert "유효한 하네스가 없어" in err, err
    assert str(_receipt_path(pm_import, dest)) in err, "수정 경로 실값이 없다"


def test_newer_schema_is_refused_loudly(pm_import, tmp_path, capsys):
    """상위 schema 기록은 이 엔진이 모르는 의미를 담을 수 있다 — 목록으로만 읽지 않고 알린다."""
    dest = _new_instance(pm_import, tmp_path / "newer_schema", "claude")
    _receipt_path(pm_import, dest).write_text(
        '{"schema": 99, "harnesses": ["codex"]}\n', encoding="utf-8")
    capsys.readouterr()

    assert pm_import.installed_harnesses(dest, REPO) == ["claude"]  # 추론 폴백(codex 아님).

    err = capsys.readouterr().err
    assert "schema 99" in err and "엔진을 갱신" in err, err


def test_unregistered_name_survives_a_rewrite(pm_import, tmp_path):
    """미등록 이름은 재기록에서 **보존**된다 — 구 엔진이 신 엔진의 설치 사실을 지우지 않는다.

    판정에서 빼는 것(독자 집합)과 기록에서 지우는 것(사실)은 다른 일이다."""
    dest = _new_instance(pm_import, tmp_path / "preserve_future", "claude")
    _receipt_path(pm_import, dest).write_text(
        '{"schema": 1, "harnesses": ["claude", "fourth"]}\n', encoding="utf-8")

    assert pm_import.record_install_receipt(dest, ["claude", "codex"]) is True

    assert _receipt(pm_import, dest)["harnesses"] == ["claude", "codex", "fourth"], \
        "미등록 이름이 재기록에서 사라짐(신 엔진 기록 영구 소실)"
    # 판정은 여전히 등록분만 본다(보존 ≠ 사용).
    assert pm_import.installed_harnesses(dest, REPO) == ["claude", "codex"]


def test_ghost_receipt_entry_is_reported_with_a_fix_path(pm_import, tmp_path, capsys):
    """기록엔 있는데 어댑터가 없는 유령 형상은 알린다 — 판정은 기록 그대로(진실 우선) 유지.

    기록이 관측을 덮으므로, 잘못 박힌 기록은 사람이 고치지 않으면 영원히 그 하네스를 독자로 붙든다.
    제거 채널이 파일 편집뿐이라 그 위치를 실값으로 준다."""
    dest = _new_instance(pm_import, tmp_path / "ghost", "claude")
    pm_import.record_install_receipt(dest, ["claude", "codex"])  # codex 어댑터는 없다.
    capsys.readouterr()

    assert pm_import.installed_harnesses(dest, REPO) == ["claude", "codex"]

    err = capsys.readouterr().err
    assert "codex" in err and "어댑터가 트리에 없습니다" in err, err
    assert str(_receipt_path(pm_import, dest)) in err, "수정 경로 실값이 없다"
    # 정상 인스턴스는 조용하다(경고가 무차별이 아님).
    capsys.readouterr()
    pm_import.installed_harnesses(_new_instance(pm_import, tmp_path / "quiet", "claude"), REPO)
    assert "어댑터가 트리에 없습니다" not in capsys.readouterr().err


def test_receipt_survives_deleted_marker_files_without_ghost_noise(pm_import, tmp_path, capsys):
    """판별자 파일만 사라진 형상은 유령이 아니다 — 어댑터 구조가 남아 있으면 경고하지 않는다."""
    dest = _new_instance(pm_import, tmp_path / "markers_gone", "opencode")
    _erase_exclusive_evidence(dest, "opencode")
    capsys.readouterr()

    assert pm_import.installed_harnesses(dest, REPO) == ["opencode"]
    assert "어댑터가 트리에 없습니다" not in capsys.readouterr().err


def test_newer_schema_receipt_is_not_overwritten(pm_import, tmp_path, capsys):
    """상위 schema 기록은 **쓰기도 거부**한다 — 읽기 거부와 짝이 아니면 신 엔진 기록이 파괴된다.

    구 엔진이 "해석 못 함 → 기록 없음" 으로 보고 자기 형식으로 덮으면, 신 엔진이 남긴 설치 사실이
    사라진다(읽기만 막는 가드는 절반짜리)."""
    dest = _new_instance(pm_import, tmp_path / "future_write", "claude")
    future = '{"schema": 99, "harnesses": ["codex"], "future_key": 1}\n'
    _receipt_path(pm_import, dest).write_text(future, encoding="utf-8")
    capsys.readouterr()

    assert pm_import.record_install_receipt(dest, ["claude", "codex"]) is False

    assert _receipt_path(pm_import, dest).read_text(encoding="utf-8") == future, \
        "상위 schema 기록을 구 엔진 형식으로 덮음"
    err = capsys.readouterr().err
    assert "갱신하지 않습니다" in err and "schema 99" in err, err


def test_add_harness_does_not_destroy_a_newer_schema_receipt(pm_import, tmp_path, capsys):
    """설치 경로(add-harness)에서도 같다 — 기록 갱신 실패가 설치 자체를 막지도 않는다."""
    dest = _new_instance(pm_import, tmp_path / "future_add", "claude")
    future = '{"schema": 99, "harnesses": ["claude"]}\n'
    _receipt_path(pm_import, dest).write_text(future, encoding="utf-8")
    capsys.readouterr()

    pm_import.add_harness(dest, "codex", dry_run=False, source_root=REPO)

    assert _receipt_path(pm_import, dest).read_text(encoding="utf-8") == future
    assert (dest / ".codex").is_dir(), "기록 갱신 거부가 설치를 막았다"
    assert "갱신하지 않습니다" in capsys.readouterr().err


def test_corrupt_receipt_is_backed_up_before_rewrite(pm_import, tmp_path, capsys):
    """손상된 기록은 **백업 후** 다시 쓴다 — 부재와 같게 접어 덮으면 원본이 영구 소실된다.

    기록 거부만 하면 그 인스턴스는 영영 추론 판정에 머문다 → 백업(포렌식 보존) + 재기록(원천 정합)."""
    dest = _new_instance(pm_import, tmp_path / "corrupt_backup", "claude")
    broken = '{ 깨진 JSON "harnesses": ["claude"]\n'
    _receipt_path(pm_import, dest).write_text(broken, encoding="utf-8")
    capsys.readouterr()

    assert pm_import.record_install_receipt(dest, ["claude", "codex"]) is True

    kept = _receipt_path(pm_import, dest).with_name(
        _receipt_path(pm_import, dest).name + pm_import.INSTALL_RECEIPT_CORRUPT_SUFFIX)
    assert kept.read_text(encoding="utf-8") == broken, "손상 원본이 보존되지 않음"
    assert _receipt(pm_import, dest)["harnesses"] == ["claude", "codex"]
    assert "손상된 설치 기록을 백업하고 다시 씁니다" in capsys.readouterr().err


def test_corrupt_receipt_backups_take_a_free_slot(pm_import, tmp_path):
    """손상 백업 자리가 이미 차 있으면 순번으로 비켜 간다 — 앞선 포렌식 사본을 덮지 않는다."""
    dest = _new_instance(pm_import, tmp_path / "corrupt_twice", "claude")
    receipt = _receipt_path(pm_import, dest)
    first, second = "{ 첫 손상\n", "{ 둘째 손상\n"
    receipt.write_text(first, encoding="utf-8")
    pm_import.record_install_receipt(dest, ["claude"])
    receipt.write_text(second, encoding="utf-8")
    pm_import.record_install_receipt(dest, ["claude", "codex"])

    base = receipt.with_name(receipt.name + pm_import.INSTALL_RECEIPT_CORRUPT_SUFFIX)
    assert base.read_text(encoding="utf-8") == first
    assert base.with_name(base.name + ".1").read_text(encoding="utf-8") == second


def test_record_does_not_emit_judgment_warnings(pm_import, tmp_path, capsys):
    """기록 갱신 실행은 **판정 문구를 내지 않는다** — 이미 읽은 문서에서 보존분을 뽑는다.

    갱신 경로가 원본을 다시 읽으면 "증거 추론으로 판정합니다"(판정용 경고)가 기록 실행에서 나가
    사람이 판정이 강등된 줄로 읽는다(오도)."""
    dest = _new_instance(pm_import, tmp_path / "no_judgment_noise", "claude")
    _receipt_path(pm_import, dest).write_text("{ 깨진 JSON\n", encoding="utf-8")
    capsys.readouterr()

    assert pm_import.record_install_receipt(dest, ["claude", "codex"]) is True

    err = capsys.readouterr().err
    assert "증거 추론으로 판정합니다" not in err, err
    assert "손상된 설치 기록을 백업하고 다시 씁니다" in err, err
