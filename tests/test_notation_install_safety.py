"""T-0541 ③④⑤ — 표기 렌더 경로의 설치-전 게이트·경로 안전·비파괴 계약.

닫는 결함 셋(전부 "렌더 단계가 복사 뒤라 늦다"의 파생):

③ 미등록 표기 하네스 — 렌더러는 미등록 하네스 context 를 fail-loud 하는데 그 판정이 복사
   *뒤*라, `--fill manual` 설치는 파일을 다 깔고 나서 `RenderLeakError` 로 죽어 **부분 설치**를
   남겼다. `--fill auto` 미매핑 하네스 게이트와 같은 성질(설치 전 rc1·traceback 0)을 준다.
④ add-harness 의 기존 공유 문서 재렌더가 `is_file()` 만 보고 대상에 넣어 symlink 를 따라가고
   `..` 도 안 걸렀다 — 저장소 밖 파일이 치환·렌더 대상이 될 수 있었다. `_is_safe_dest_path`
   검증 + **복사 시작 전 거부**.
⑤ 그 재렌더 계획을 복사·적용 *뒤*에 계산해 dry-run 계획에도, 백업 범위에도 없었다 —
   비파괴 보장(계획 제시 + 백업 후 변경)이 깨진 지점. 계획을 복사 전으로 올린다.

실 하네스·네트워크 0 — 설치는 tmp_path 안 `--new` 이고 opencode 모델 조회는 고정한다.
"""

from __future__ import annotations

import contextlib
import datetime
import importlib.util
import os
import shutil
import signal
import stat
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


@pytest.fixture(autouse=True)
def _hermetic_opencode_models(pm_import, monkeypatch):
    """실 `opencode models` CLI 미호출 고정(미설치 동치·hermetic)."""
    monkeypatch.setattr(pm_import, "_real_models_runner", lambda: (False, []))


def _live_instance(pm_import, dest: Path, harness: str = "claude") -> Path:
    rc = pm_import.main(
        ["--new", str(dest), "--harness", harness, "--name", "Notation Inst"])
    assert rc == 0, f"라이브 인스턴스 셋업 실패(rc={rc})"
    return dest


# ── ③ 미등록 표기 하네스는 설치 전에 멈춘다 ──────────────────────────────────

def test_unregistered_template_dirs_are_derived_from_render_registry(pm_import):
    assert pm_import.unregistered_skill_notation_template_dirs(
        ("claude_code", "codex", "opencode")) == []
    assert pm_import.unregistered_skill_notation_template_dirs(
        ("claude_code", "fourth")) == ["fourth"]


def _unregister_notation(pm_import, monkeypatch, template_dir: str, attr: str) -> None:
    """실 4번째 하네스 = "template dir 은 있는데 표기 값이 없다" 조건을 재현한다.

    렌더 모듈은 호출마다 새로 로드되므로(중앙 로더 cache=False) 반환 인스턴스를 고쳐도 게이트에
    안 닿는다 — 로더 자체를 감싸 매 로드에서 값을 뺀다."""
    original = pm_import._load_pm_render_module

    def loader():
        module = original()
        getattr(module, attr).pop(template_dir, None)
        return module

    monkeypatch.setattr(pm_import, "_load_pm_render_module", loader)


def _register_notation(pm_import, monkeypatch, template_dir: str) -> None:
    """가짜 template dir 을 표기 registry 에 등록한다(미등록-표기 게이트 우회용).

    렌더 모듈은 호출마다 새로 로드되므로 로더를 감싸 매 로드에서 값을 넣는다."""
    original = pm_import._load_pm_render_module

    def loader():
        module = original()
        module.SKILL_ENTRY_PREFIX_BY_TEMPLATE_DIR[template_dir] = "/"
        module._HARNESS_LABEL_BY_TEMPLATE_DIR[template_dir] = "claude"
        return module

    monkeypatch.setattr(pm_import, "_load_pm_render_module", loader)


def test_import_stops_before_installing_files_for_unregistered_notation(
        pm_import, tmp_path, monkeypatch, capsys):
    """미등록 표기 하네스로 `--fill manual` import → 파일 설치 전 rc1·traceback 0·설치 0."""
    _unregister_notation(
        pm_import, monkeypatch, "codex", "SKILL_ENTRY_PREFIX_BY_TEMPLATE_DIR")
    dest = tmp_path / "unregistered"

    rc = pm_import.main(
        ["--new", str(dest), "--harness", "codex", "--fill", "manual",
         "--from", str(REPO), "--name", "Unregistered"])

    assert rc == 1
    assert not dest.exists(), "복사가 시작됨(부분 설치 잔존 — 설치 전 중단 위반)"
    err = capsys.readouterr().err
    assert "codex" in err and "파일 설치 전 중단" in err, err


def test_into_stops_before_copy_when_a_reader_manifest_is_missing(
        pm_import, tmp_path, monkeypatch, capsys):
    """설치 하네스의 표기 manifest 를 소스에서 못 얻으면 **복사 전 중단**(조용한 제외 0).

    조용히 빼면 그 독자가 없는 것처럼 공유 문서를 단독 표기로 재렌더한다(기존 codex 인스턴스에
    claude 단독 표기) — 이 티켓이 닫은 클래스가 소스 불완전 경로로 되살아난다."""
    dest = tmp_path / "missing_manifest"
    assert pm_import.main(
        ["--new", str(dest), "--harness", "codex", "--fill", "manual",
         "--from", str(REPO), "--name", "MissingManifest"]) == 0
    claude_marker = dest / ".claude" / "settings.json"
    assert not claude_marker.exists()
    # 설치된 codex 의 flavor 트리를 소스에서 못 찾는 상황(부분 checkout 동치).
    monkeypatch.setitem(pm_import.HARNESS_TEMPLATE_DIRS, "codex", ("codex_absent",))
    capsys.readouterr()

    rc = pm_import.main(
        ["--into", str(dest), "--harness", "claude", "--fill", "manual",
         "--from", str(REPO), "--name", "MissingManifest"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "복사 전에 중단" in err and "codex" in err, err
    assert "Traceback" not in err
    assert not claude_marker.exists(), "복사가 시작됨(부분 설치 잔존)"


def test_add_harness_cli_translates_missing_reader_manifest_to_rc1(
        pm_import, tmp_path, monkeypatch, capsys):
    """add-harness 도 표기 manifest 부재를 **복사 전** 친화 메시지 + rc1 로 끝낸다(main 대칭).

    같은 실패를 main 은 rc1 로, add-harness 는 traceback 으로 내던 비대칭을 없앤다."""
    dest = _live_instance(pm_import, tmp_path / "addmissing", "claude")
    # 설치된 claude 의 flavor 트리를 소스에서 못 찾는 상황(부분 checkout 동치). 표기 registry 에는
    #   등록해 둬야 미등록-표기 게이트가 아니라 **manifest 부재** 경로를 실제로 태운다.
    monkeypatch.setitem(pm_import.HARNESS_TEMPLATE_DIRS, "claude", ("claude_absent",))
    _register_notation(pm_import, monkeypatch, "claude_absent")
    capsys.readouterr()

    rc = pm_import.add_harness_cli(dest, "codex", dry_run=False, source_root=REPO)

    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("오류: ") and "claude" in err and "복사 전에 중단" in err, err
    assert "Traceback" not in err
    assert not (dest / ".codex").exists(), "복사가 시작됨(부분 설치 잔존)"


def test_add_harness_stops_before_copy_for_unregistered_notation(
        pm_import, tmp_path, monkeypatch):
    """add-harness 도 같은 게이트를 복사 전에 탄다 — CLI 는 rc1 로 번역(traceback 0)."""
    dest = _live_instance(pm_import, tmp_path / "addgate")
    _unregister_notation(
        pm_import, monkeypatch, "codex", "_HARNESS_LABEL_BY_TEMPLATE_DIR")

    with pytest.raises(ValueError, match="미등록"):
        pm_import.add_harness(dest, "codex", dry_run=False, source_root=REPO)
    assert not (dest / ".codex").exists(), "복사가 시작됨(부분 설치)"

    rc = pm_import.add_harness_cli(dest, "codex", dry_run=False, source_root=REPO)
    assert rc == 1


# ── ④ 공유 문서 재렌더 대상의 경로 안전 ──────────────────────────────────────

def test_shared_rerender_plan_rejects_parent_escape(pm_import, tmp_path):
    """`..` 탈출 경로는 계획 단계에서 거부한다(조작 manifest 방어·unit)."""
    dest = tmp_path / "inst"
    (dest / ".project_manager" / "wiki").mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_text("EXTERNAL\n", encoding="utf-8")
    # inst/.project_manager/wiki 에서 세 단계 올라가면 tmp_path — 저장소 밖 실파일을 가리킨다.
    contexts = {
        ".project_manager/wiki/../../../outside.md": ("codex", "opencode"),
    }
    with pytest.raises(RuntimeError, match="안전하지 않"):
        pm_import._shared_notation_rerender_plan(dest, contexts, None, None)
    assert outside.read_text(encoding="utf-8") == "EXTERNAL\n"


@requires_symlink
def test_add_harness_rejects_symlinked_shared_doc_before_copy(pm_import, tmp_path):
    """공유 문서가 저장소 밖 지향 symlink 면 **복사 시작 전** 거부 — 외부 파일 불변·부분 적용 0."""
    dest = _live_instance(pm_import, tmp_path / "symshared")
    external = tmp_path / "OUTSIDE_ROLE.md"
    external.write_text("EXTERNAL ROLE `/pm-bootstrap`\n", encoding="utf-8")
    role = dest / ".project_manager" / "wiki" / "pm_role.md"
    role.unlink()
    role.symlink_to(external)

    with pytest.raises(pm_import.UnsafeDestPathError, match="안전하지 않"):
        pm_import.add_harness(dest, "codex", dry_run=False, source_root=REPO)

    assert external.read_text(encoding="utf-8") == "EXTERNAL ROLE `/pm-bootstrap`\n", \
        "저장소 밖 파일이 변경됨(④ 미해소)"
    assert not (dest / ".codex").exists(), "복사가 시작됨(복사 전 거부 위반)"


# ── 경로 거부는 CLI 경계에서 rc 1 로 번역된다 (traceback 0) ───────────────────

@requires_symlink
def test_add_harness_cli_translates_unsafe_shared_doc_to_rc1(pm_import, tmp_path, capsys):
    """공유 문서 symlink 거부가 `add_harness_cli` 에서 rc1 + 친화 메시지 — traceback 0."""
    dest = _live_instance(pm_import, tmp_path / "symcli")
    external = tmp_path / "OUTSIDE_ROLE.md"
    external.write_text("EXTERNAL\n", encoding="utf-8")
    role = dest / ".project_manager" / "wiki" / "pm_role.md"
    role.unlink()
    role.symlink_to(external)
    capsys.readouterr()

    rc = pm_import.add_harness_cli(dest, "codex", dry_run=False, source_root=REPO)

    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("오류: ") and "안전하지 않" in err, err
    assert "Traceback" not in err
    assert external.read_text(encoding="utf-8") == "EXTERNAL\n"
    assert not (dest / ".codex").exists()


@requires_symlink
def test_add_harness_cli_translates_unsafe_manifest_to_rc1(pm_import, tmp_path, capsys):
    """engine.manifest symlink 거부도 같은 타입이라 CLI 가 rc1 로 번역한다(옛 uncaught 경로)."""
    dest = _live_instance(pm_import, tmp_path / "symmanicli")
    external = tmp_path / "OUTSIDE_MANIFEST"
    external.write_text("ORIGINAL\n", encoding="utf-8")
    manifest = dest / ".project_manager" / "engine.manifest"
    manifest.unlink()
    manifest.symlink_to(external)
    capsys.readouterr()

    rc = pm_import.add_harness_cli(dest, "codex", dry_run=False, source_root=REPO)

    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("오류: ") and "Traceback" not in err, err
    assert external.read_text(encoding="utf-8") == "ORIGINAL\n"
    assert not (dest / ".codex").exists()


def test_unsafe_dest_path_error_is_a_runtime_error_subclass(pm_import):
    """전용 타입이되 옛 `RuntimeError` 처리부와의 호환은 유지한다."""
    assert issubclass(pm_import.UnsafeDestPathError, RuntimeError)


# ── 백업 경로 점유 판정은 링크를 따라가지 않는다 ──────────────────────────────

@requires_symlink
def test_free_backup_path_treats_broken_symlink_as_occupied(pm_import, tmp_path):
    """깨진 symlink 자리는 `exists()` 가 False 라 옛 코드가 그대로 썼다 — 링크를 따라 저장소 밖에
    파일이 생긴다. `lexists` 판정으로 점유 처리해 다음 순번을 고른다."""
    backup = tmp_path / "backup.md"
    backup.symlink_to(tmp_path / "nonexistent_outside.md")
    assert not backup.exists() and backup.is_symlink()  # 사전조건(깨진 링크).
    chosen = pm_import._free_backup_path(backup)
    assert chosen != backup
    assert chosen.name == "backup.md.1"


@requires_symlink
def test_inplace_backup_does_not_write_through_broken_symlink(pm_import, tmp_path):
    """제자리 편집 백업이 깨진 symlink 를 따라 저장소 밖에 쓰지 않는다(경로 e2e)."""
    dest = tmp_path / "inst"
    role = dest / ".project_manager" / "wiki" / "pm_role.md"
    role.parent.mkdir(parents=True)
    role.write_text("`/pm-bootstrap`\n", encoding="utf-8")
    outside = tmp_path / "OUTSIDE_TARGET.md"  # 아직 없음 = 깨진 링크 대상.
    backup_root = dest / pm_import.BACKUP_DIR_NAME / "2026-01-01"
    planned = backup_root / ".project_manager" / "wiki" / "pm_role.md"
    planned.parent.mkdir(parents=True)
    planned.symlink_to(outside)

    outcome = pm_import._backup_before_inplace_edit(
        dest, [Path(".project_manager/wiki/pm_role.md")], backup_root, set())

    assert outcome.backed_up == [".project_manager/wiki/pm_role.md"]
    assert outcome.refused == [] and outcome.vanished == []
    assert not outside.exists(), "링크를 따라가 저장소 밖에 파일을 만듦(must-fix 2 미해소)"
    assert planned.with_name("pm_role.md.1").read_text(encoding="utf-8") == "`/pm-bootstrap`\n"


@requires_symlink
def test_symlinked_shared_doc_is_rejected_not_silently_skipped(pm_import, tmp_path):
    """조용한 skip 이 아니라 거부다 — 실존하지 않는 경로만 무동작으로 지나간다."""
    dest = tmp_path / "inst"
    wiki = dest / ".project_manager" / "wiki"
    wiki.mkdir(parents=True)
    external = tmp_path / "outside_role.md"
    external.write_text("EXTERNAL\n", encoding="utf-8")
    (wiki / "pm_role.md").symlink_to(external)
    contexts = {".project_manager/wiki/pm_role.md": ("codex", "opencode")}

    with pytest.raises(RuntimeError, match="안전하지 않"):
        pm_import._shared_notation_rerender_plan(dest, contexts, None, None)

    # 부재 경로는 대상이 아니므로 조용히 지나간다(거부 대상은 *실존하는* 위험 경로).
    absent = {".project_manager/wiki/absent.md": ("codex", "opencode")}
    targets, blocked = pm_import._shared_notation_rerender_plan(
        dest, absent, None, None)
    assert targets == [] and blocked == []


# ── ⑤ dry-run 계획 표시 + 백업 범위 ──────────────────────────────────────────

def test_add_harness_dry_run_lists_shared_doc_rerender(pm_import, tmp_path, capsys):
    """dry-run 계획이 기존 공유 문서 재렌더를 표시하고 파일은 그대로다."""
    dest = _live_instance(pm_import, tmp_path / "dryplan")
    role = dest / ".project_manager" / "wiki" / "pm_role.md"
    before = role.read_text(encoding="utf-8")
    capsys.readouterr()

    pm_import.add_harness(dest, "codex", dry_run=True, source_root=REPO)

    out = capsys.readouterr().out
    assert "공유 문서 재렌더" in out, out
    assert "[rerender] .project_manager/wiki/pm_role.md" in out, out
    assert role.read_text(encoding="utf-8") == before, "dry-run 이 파일을 변경함"


def test_add_harness_backs_up_shared_docs_before_rerender(pm_import, tmp_path, capsys):
    """적용 경로는 변경 *직전* 중앙 백업을 남긴다 — 백업 내용 = 변경 전 원본."""
    dest = _live_instance(pm_import, tmp_path / "backupshared")
    role = dest / ".project_manager" / "wiki" / "pm_role.md"
    before = role.read_text(encoding="utf-8")
    capsys.readouterr()

    pm_import.add_harness(dest, "codex", dry_run=False, source_root=REPO)

    backup = (dest / pm_import.BACKUP_DIR_NAME / datetime.date.today().isoformat()
              / ".project_manager" / "wiki" / "pm_role.md")
    assert backup.is_file(), "공유 문서 재렌더가 백업 범위 밖(⑤ 미해소)"
    assert backup.read_text(encoding="utf-8") == before
    after = role.read_text(encoding="utf-8")
    assert after != before, "공유 문서가 병기 표기로 재렌더되지 않음(회귀)"
    assert "(codex)" in after
    assert "공유 문서" in capsys.readouterr().out


def test_shared_docs_are_not_backed_up_when_git_can_restore(pm_import, tmp_path):
    """git 추적&미변경 파일은 백업 생략 — plan_copy 의 git-safe 규칙과 동형."""
    dest = tmp_path / "inst"
    role = dest / ".project_manager" / "wiki" / "pm_role.md"
    role.parent.mkdir(parents=True)
    role.write_text("`/pm-bootstrap`\n", encoding="utf-8")
    backup_root = dest / pm_import.BACKUP_DIR_NAME / "2026-01-01"
    contexts = {".project_manager/wiki/pm_role.md": ("codex", "opencode")}

    targets, blocked = pm_import._shared_notation_rerender_plan(
        dest, contexts, backup_root, {".project_manager/wiki/pm_role.md"})
    assert blocked == [] and targets == [Path(".project_manager/wiki/pm_role.md")]
    outcome = pm_import._backup_before_inplace_edit(
        dest, targets, backup_root, {".project_manager/wiki/pm_role.md"})
    assert outcome.backed_up == [] and outcome.refused == []
    assert outcome.vanished == [] and not backup_root.exists()


def _spike_line(dest: Path) -> str:
    text = (dest / ".project_manager" / "wiki" / "raw" / "README.md").read_text(
        encoding="utf-8")
    return next(line for line in text.splitlines() if "spike-new" in line)


@pytest.mark.parametrize("host", ("claude", "opencode"))
def test_add_harness_leaves_no_stale_notation_in_unowned_wiki_seed(
        pm_import, tmp_path, host):
    """설치 → 다른 하네스 add-harness 후 **manifest 미소유 wiki seed 에 잔존 0**.

    `wiki/raw/README.md` 는 manifest 어느 엔트리도 소유하지 않아 add-harness 재렌더 계획 밖이었다
    — codex 를 얹어도 canonical `/spike-new` 그대로 남았다(조용한 잔존·실 설치본 재현)."""
    dest = _live_instance(pm_import, tmp_path / f"{host}_add", host)
    assert "`/spike-new`" in _spike_line(dest)  # 사전조건(단일 하네스 표기).

    pm_import.add_harness(dest, "codex", dry_run=False, source_root=REPO)

    line = _spike_line(dest)
    assert "`$spike-new`(codex)" in line, f"codex 표기 미반영(조용한 잔존): {line}"
    assert f"`/spike-new`({'claude' if host == 'claude' else 'opencode'})" in line, line
    backup = (dest / pm_import.BACKUP_DIR_NAME / datetime.date.today().isoformat()
              / ".project_manager" / "wiki" / "raw" / "README.md")
    assert backup.is_file(), "미소유 wiki seed 재렌더가 백업 범위 밖"
    assert "`/spike-new`" in backup.read_text(encoding="utf-8")


def test_add_harness_lists_unowned_wiki_seed_in_plan(pm_import, tmp_path, capsys):
    """그 재렌더가 계획(`[rerender]`)에 보이고 dry-run 은 파일을 안 바꾼다."""
    dest = _live_instance(pm_import, tmp_path / "seedplan")
    before = _spike_line(dest)
    capsys.readouterr()

    pm_import.add_harness(dest, "codex", dry_run=True, source_root=REPO)

    out = capsys.readouterr().out
    assert "[rerender] .project_manager/wiki/raw/README.md" in out, out
    assert _spike_line(dest) == before


def test_add_harness_rerender_is_idempotent(pm_import, tmp_path, capsys):
    """재실행하면 재렌더 계획 0건 — 무변경 파일은 계획·백업 어디에도 안 들어간다."""
    dest = _live_instance(pm_import, tmp_path / "idem")
    pm_import.add_harness(dest, "codex", dry_run=False, source_root=REPO)
    capsys.readouterr()

    pm_import.add_harness(dest, "codex", dry_run=False, source_root=REPO)

    out = capsys.readouterr().out
    assert "[rerender]" not in out, out


def test_into_existing_instance_renders_for_both_reader_harnesses(
        pm_import, tmp_path, capsys):
    """codex 인스턴스에 `--into claude` — 공유 문서가 **두 독자** 표기로 나가고 생성 산출물
    (`pm_state.md`)도 계획·백업을 거쳐 따라온다(이번 run 의 하네스 집합만 보면 오표기)."""
    dest = tmp_path / "into_inst"
    assert pm_import.main(
        ["--new", str(dest), "--harness", "codex", "--fill", "manual",
         "--from", str(REPO), "--name", "Into Inst"]) == 0
    state = dest / ".project_manager" / "wiki" / "pm_state.md"
    assert "`$pm-handoff`" in state.read_text(encoding="utf-8")  # 사전조건(codex 단독).
    capsys.readouterr()

    assert pm_import.main(
        ["--into", str(dest), "--harness", "claude", "--fill", "manual",
         "--from", str(REPO), "--name", "Into Inst"]) == 0

    out = capsys.readouterr().out
    assert "[rerender] .project_manager/wiki/pm_state.md" in out, out
    role = (dest / ".project_manager" / "wiki" / "pm_role.md").read_text(encoding="utf-8")
    assert "`$pm-bootstrap" in role and "`/pm-bootstrap" in role, "기존 독자(codex) 표기 유실"
    state_text = state.read_text(encoding="utf-8")
    assert "`/pm-handoff`(claude) / `$pm-handoff`(codex)" in state_text, state_text[:400]
    backup = (dest / pm_import.BACKUP_DIR_NAME / datetime.date.today().isoformat()
              / ".project_manager" / "wiki" / "pm_state.md")
    assert backup.is_file(), "생성 산출물 재렌더가 백업 범위 밖"


def test_generated_template_sibling_is_derived_not_enumerated(pm_import):
    assert pm_import._generated_template_sibling(
        ".project_manager/wiki/pm_state.template.md") == ".project_manager/wiki/pm_state.md"
    assert pm_import._generated_template_sibling(
        ".project_manager/wiki/tickets/_template.md") is None
    assert pm_import._generated_template_sibling(".project_manager/wiki/pm_role.md") is None


def test_unowned_shipped_wiki_relpaths_exclude_manifest_owned(pm_import):
    """미소유 wiki 후보는 출하 인벤토리에서 파생되고 manifest 소유분은 빠진다(손-열거 0)."""
    contexts = {".project_manager/wiki/pm_role.md": ("claude_code",)}
    unowned = pm_import._unowned_shipped_wiki_relpaths(
        REPO, ("claude", "codex"), contexts)
    assert ".project_manager/wiki/raw/README.md" in unowned
    assert ".project_manager/wiki/pm_role.md" not in unowned
    assert all(rel.startswith(".project_manager/wiki/") for rel in unowned)


def test_installed_harnesses_reads_dest_tree(pm_import, tmp_path):
    dest = _live_instance(pm_import, tmp_path / "detect", "claude")
    assert pm_import.installed_harnesses(dest, REPO) == ["claude"]
    pm_import.add_harness(dest, "codex", dry_run=False, source_root=REPO)
    assert pm_import.installed_harnesses(dest, REPO) == ["claude", "codex"]
    assert pm_import.installed_harnesses(tmp_path / "absent", REPO) == []


# ── 표기 렌더는 줄끝을 바꾸지 않는다 (표기 외 바이트 변경 0) ─────────────────

def test_render_preserves_crlf_line_endings(pm_import, tmp_path):
    """CRLF wiki 문서를 재렌더해도 줄끝은 CRLF 그대로다 — 표기 외 바이트 변경 0.

    기본 텍스트 IO 는 universal-newlines 라 read/write 왕복만으로 CRLF 가 LF 로 접힌다. 렌더
    범위가 인스턴스 소유 문서까지 넓어진 뒤에는 그게 Windows 채택자 트리 전체를 뒤집는다."""
    dest = tmp_path / "crlf"
    manifest = dest / ".project_manager" / "engine.manifest"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(".project_manager/wiki/pm_role.md\n", encoding="utf-8")
    target = dest / ".project_manager" / "wiki" / "raw" / "README.md"
    target.parent.mkdir(parents=True)
    target.write_bytes("첫 줄\r\n박제는 `/spike-new` 가 한다\r\n끝 줄\r\n".encode("utf-8"))

    changed = pm_import.render_managed_files(
        dest, {}, {Path(".project_manager/wiki/raw/README.md")},
        installed_notation_context=("codex", "opencode"))

    assert changed == 1
    raw = target.read_bytes()
    assert raw.count(b"\r\n") == 3, f"CRLF 가 접힘(표기 외 바이트 변경): {raw!r}"
    assert b"\n" not in raw.replace(b"\r\n", b""), "LF 가 섞임"
    assert "`$spike-new`(codex) / `/spike-new`(opencode)" in raw.decode("utf-8")


def test_render_preserves_lf_line_endings(pm_import, tmp_path):
    """LF 파일은 LF 그대로 — 줄끝 보존이 반대 방향(LF→CRLF)으로도 새지 않는다."""
    dest = tmp_path / "lf"
    manifest = dest / ".project_manager" / "engine.manifest"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(".project_manager/wiki/pm_role.md\n", encoding="utf-8")
    target = dest / ".project_manager" / "wiki" / "raw" / "README.md"
    target.parent.mkdir(parents=True)
    target.write_bytes("박제는 `/spike-new` 가 한다\n".encode("utf-8"))

    pm_import.render_managed_files(
        dest, {}, {Path(".project_manager/wiki/raw/README.md")},
        installed_notation_context=("codex",))

    assert target.read_bytes() == "박제는 `$spike-new` 가 한다\n".encode("utf-8")


def test_newline_preserving_roundtrip_helpers(pm_import, tmp_path):
    """읽기/쓰기 쌍이 줄끝을 번역하지 않는다(왕복 byte 동일)."""
    path = tmp_path / "mixed.md"
    body = "a\r\nb\nc\r\n"
    path.write_bytes(body.encode("utf-8"))
    text = pm_import.read_text_keeping_newlines(path)
    assert text == body
    pm_import.write_text_keeping_newlines(path, text)
    assert path.read_bytes() == body.encode("utf-8")


def test_add_harness_does_not_double_backup_copied_shared_doc(pm_import, tmp_path):
    """복사 plan 이 싣는 경로는 재렌더 목록에서 빠진다 — 같은 파일 이중 백업 0."""
    dest = _live_instance(pm_import, tmp_path / "nodouble", "opencode")
    pm_import.add_harness(dest, "codex", dry_run=False, source_root=REPO)
    backup_root = (dest / pm_import.BACKUP_DIR_NAME
                   / datetime.date.today().isoformat())
    # 두 번째 백업은 `_free_backup_path` 가 `.1` 순번으로 남긴다 — 그 자리가 비어야 한다.
    assert not (backup_root / "AGENTS.md.1").exists(), "공유 root doc 이 두 번 백업됨"


# ── PM 설치 판정은 PM 고유 자산을 본다 (채택자 자기 용도 트리 오인 방지) ──────

def test_pm_install_evidence_is_derived_and_non_empty(pm_import):
    """등록 하네스 전부가 PM 증거를 갖고, 그 값이 PM 이름 관례를 따른다.

    하나라도 비면 그 하네스는 구조 판정으로 강등돼 오판정이 되살아나므로 여기서 red 로 잡는다.
    production 판정(`installed_harnesses`)이 소비하는 바로 그 함수를 태운다."""
    evidence, shipped_all = pm_import._pm_install_evidence(REPO)
    assert set(evidence) == set(pm_import.REGISTERED_HARNESSES)
    assert set(shipped_all) == set(pm_import.REGISTERED_HARNESSES)
    for harness, marks in evidence.items():
        assert marks, f"{harness}: PM 증거 0건(설치 판정이 구조 검사로 강등됨)"
        assert marks <= shipped_all[harness], f"{harness}: 증거가 출하 집합 밖"
        for rel in marks:
            assert any(part.startswith(("pm-", "pm_")) for part in rel.split("/")), rel
    # 증거는 그 하네스 어댑터 네임스페이스 안에서만 나온다(공유 wiki·엔진은 하네스를 못 가름).
    assert all(
        not rel.startswith(".project_manager/")
        for marks in evidence.values() for rel in marks
    )


# ⚠ 아래 세 추론 축은 **기록이 없는 인스턴스**(기록 도입 전 설치·수기 설치)의 판정을 잰다 —
#   기록이 있으면 그게 진실이라 추론을 아예 안 타므로, 기록을 지워야 이 가드가 공허해지지 않는다.

def _forget_install_receipt(pm_import, dest: Path) -> None:
    """기록 도입 이전 인스턴스 형상 — 어댑터는 그대로 두고 기록만 없앤다(추론 축 재현)."""
    (dest / pm_import.INSTALL_RECEIPT_RELPATH).unlink()


def test_single_missing_sentinel_file_does_not_hide_a_real_install(pm_import, tmp_path):
    """판별자 하나가 빠져도 실 설치는 검출된다 — 거짓 음성(표기 유실)이 거짓 양성보다 나쁘다.

    claude 는 전용 증거가 `.claude/pm_orch_claude.py` 하나뿐이라, 옛 판정은 그 파일만 없으면
    실 PM 인스턴스를 미설치로 봤다(add-harness codex 가 pm_state.md 를 codex 단독 표기로
    재렌더 → 기존 claude 표기 유실)."""
    dest = _live_instance(pm_import, tmp_path / "missing_sentinel", "claude")
    _forget_install_receipt(pm_import, dest)
    (dest / ".claude" / "pm_orch_claude.py").unlink()
    assert pm_import.installed_harnesses(dest, REPO) == ["claude"]


def test_coexisting_install_survives_a_missing_exclusive_sentinel(pm_import, tmp_path):
    """공존 인스턴스에서 전용 판별자 하나가 사라져도 그 하네스는 계속 검출된다.

    claude 전용 증거는 `.claude/pm_orch_claude.py` 하나뿐이라, 공유 증거(`.claude/skills/pm-*`)를
    opencode 몫으로 귀속하면 그 파일 하나 없어진 것만으로 claude 가 통째로 미검출됐다 → 이후
    갱신이 claude 표기를 지운다(거짓 음성 = 표기 유실)."""
    dest = _live_instance(pm_import, tmp_path / "coexist", "claude")
    pm_import.add_harness(dest, "opencode", dry_run=False, source_root=REPO)
    assert pm_import.installed_harnesses(dest, REPO) == ["claude", "opencode"]

    _forget_install_receipt(pm_import, dest)
    (dest / ".claude" / "pm_orch_claude.py").unlink()

    assert pm_import.installed_harnesses(dest, REPO) == ["claude", "opencode"], \
        "전용 판별자 1개 소실로 공존 하네스가 미검출(표기 유실 재발)"


def test_shared_evidence_counts_as_install_conservatively(pm_import, tmp_path):
    """(추론 축) 구조 증거 + 공유 PM 자산이면 설치로 본다 — 유실보다 소음을 택하는 비대칭 판단.

    opencode 설치 + 채택자 자작 `CLAUDE.md` 는 claude 도 독자로 세어 병기 표기가 하나 는다
    (소음). 그 반대(귀속으로 빼기)는 공존 인스턴스에서 표기 유실을 만든다.

    기록이 있는 인스턴스엔 이 소음 자체가 없다(첫 단언) — 추론의 거짓 양성은 기록 부재에서만 남는
    잔여이고, 그 잔여는 여전히 유실보다 안전한 쪽으로 기운다(둘째 단언)."""
    dest = _live_instance(pm_import, tmp_path / "oc_own_claude_md", "opencode")
    (dest / "CLAUDE.md").write_text("# 우리 팀 claude 메모\n", encoding="utf-8")
    assert (dest / ".claude" / "skills" / "pm-bootstrap" / "SKILL.md").is_file()

    assert pm_import.installed_harnesses(dest, REPO) == ["opencode"], \
        "기록이 있는데도 채택자 자작 진입문서가 독자를 늘림(거짓 양성 잔존)"

    _forget_install_receipt(pm_import, dest)
    assert pm_import.installed_harnesses(dest, REPO) == ["claude", "opencode"]

    # 반대 축은 그대로 — PM 자산이 하나도 없으면(채택자 자기 트리) 여전히 미검출이다.
    (dest / "CLAUDE.md").unlink()
    shutil.rmtree(dest / ".claude" / "skills")
    assert pm_import.installed_harnesses(dest, REPO) == ["opencode"]


def test_adopter_owned_codex_tree_is_not_read_as_pm_install(pm_import, tmp_path):
    """채택자가 자기 용도로 만든 `.codex/`·`.agents/`·`AGENTS.md` 는 PM 설치가 아니다."""
    dest = tmp_path / "own_codex"
    (dest / ".codex" / "agents").mkdir(parents=True)
    (dest / ".codex" / "agents" / "my-agent.toml").write_text("model = 'x'\n", encoding="utf-8")
    (dest / ".agents" / "skills" / "my-skill").mkdir(parents=True)
    (dest / ".agents" / "skills" / "my-skill" / "SKILL.md").write_text("# mine\n", encoding="utf-8")
    (dest / "AGENTS.md").write_text("# 우리 프로젝트 규약\n", encoding="utf-8")

    assert pm_import.installed_harnesses(dest, REPO) == []
    # 구조 판정만 하면(판별자 없음) 옛 오판정이 그대로 재현된다 — 가드 비공허성.
    assert pm_import.installed_harnesses(dest) == ["codex"]


def test_self_update_on_adopter_owned_codex_tree_injects_no_codex_notation(
        pm_import, tmp_path, monkeypatch):
    """`pm_update` 자기 갱신도 같은 판정을 쓴다 — 비-PM `.codex` 트리에 codex 표기 주입 0.

    설치(pm_import)만 고치고 갱신(pm_update)이 구조 판정을 들고 있으면, 다음 `pm-update` 가
    공유 wiki 를 codex 병기로 되돌린다(판정 사본이 만드는 재발 경로)."""
    pm_update = _load("pm_update")
    dest = tmp_path / "own_codex_update"
    (dest / ".codex" / "agents").mkdir(parents=True)
    (dest / ".codex" / "agents" / "my-agent.toml").write_text("model = 'x'\n", encoding="utf-8")
    (dest / ".agents" / "skills" / "my-skill").mkdir(parents=True)
    (dest / ".agents" / "skills" / "my-skill" / "SKILL.md").write_text("# mine\n", encoding="utf-8")
    (dest / "AGENTS.md").write_text("# 우리 프로젝트 규약\n", encoding="utf-8")
    assert pm_import.main(
        ["--into", str(dest), "--harness", "claude", "--fill", "manual",
         "--from", str(REPO), "--name", "OwnCodexUpdate"]) == 0

    selected = pm_update._installed_entry_notation_manifests(dest, REPO, [])
    assert [p.parent.parent.name for p in selected] == ["claude_code"], selected

    contexts = pm_update._entry_notation_templates_from_manifests(selected, REPO)
    role = ".project_manager/wiki/pm_role.md"
    assert contexts[role] == ("claude_code",), contexts[role]
    # 그 context 로 렌더한 산출에 codex 표기가 없다(자기 갱신이 `$` 를 주입하지 않는다).
    pm_render = _load("pm_render")
    rendered = pm_render.render_skill_entry_notation(
        "시작은 `/pm-bootstrap` 이다\n", contexts[role])
    assert "$pm-bootstrap" not in rendered and "(claude)" not in rendered, rendered


def test_into_claude_on_adopter_owned_codex_tree_ships_claude_only_notation(
        pm_import, tmp_path):
    """비-PM `.codex` 보유 프로젝트에 `--into claude` → claude 단독 표기(codex 표기 유출 0)."""
    dest = tmp_path / "own_codex_into"
    (dest / ".codex" / "agents").mkdir(parents=True)
    (dest / ".codex" / "agents" / "my-agent.toml").write_text("model = 'x'\n", encoding="utf-8")
    (dest / ".agents" / "skills" / "my-skill").mkdir(parents=True)
    (dest / ".agents" / "skills" / "my-skill" / "SKILL.md").write_text("# mine\n", encoding="utf-8")
    (dest / "AGENTS.md").write_text("# 우리 프로젝트 규약\n", encoding="utf-8")

    rc = pm_import.main(
        ["--into", str(dest), "--harness", "claude", "--fill", "manual",
         "--from", str(REPO), "--name", "OwnCodex"])

    assert rc == 0
    role = (dest / ".project_manager" / "wiki" / "pm_role.md").read_text(encoding="utf-8")
    assert "`/pm-bootstrap" in role
    assert "$pm-bootstrap" not in role, "PM 미설치 codex 트리를 독자로 오인(표기 유출)"
    assert "(claude)" not in role, "단일 독자인데 병기됨"


# ── 기존 문서는 표기 렌더만 받는다 (치환·fill 범위 불가침) ────────────────────

# 채택자가 *직접 쓴* 문서에 리터럴로 남길 수 있는 토큰 전량(operational 3종 + 자유서술 1종).
_ADOPTER_STATUS = (
    "# 상태\n\n"
    "이 프로젝트는 {{PROJECT_NAME}} 이고 테스트는 {{TEST_CMD}}, 인터프리터는 {{PY}} 로 적는다.\n"
    "제약 절은 {{PROJECT_CONSTRAINTS}} 자리표시자를 리터럴로 유지한다.\n"
    "호출은 `/pm-handoff` 로 적는다.\n"
)


def test_add_harness_does_not_substitute_or_fill_existing_docs(pm_import, tmp_path):
    """add-harness 가 기존 문서에 placeholder 치환·자유서술 TODO 를 적용하지 않는다.

    표기 렌더 대상에 넣는 것과 치환·fill 범위에 넣는 것은 다르다 — 후자까지 넓히면 채택자가
    리터럴로 쓴 operational 토큰이 실제 값으로, 자유서술 토큰이 TODO 마커로 바뀐다(콘텐츠
    훼손). `substitute_placeholders` 의 "복사 안 한 사용자 파일 불가침" 불변식이 그 경계다."""
    dest = _live_instance(pm_import, tmp_path / "nosubst")
    status = dest / ".project_manager" / "wiki" / "status.md"
    status.write_text(_ADOPTER_STATUS, encoding="utf-8")

    pm_import.add_harness(dest, "codex", dry_run=False, source_root=REPO)

    after = status.read_text(encoding="utf-8")
    for token in ("{{PROJECT_NAME}}", "{{TEST_CMD}}", "{{PY}}", "{{PROJECT_CONSTRAINTS}}"):
        assert token in after, f"{token} 가 치환/fill 로 사라짐(채택자 콘텐츠 훼손)"
    assert "TODO" not in after, "자유서술 fill 이 기존 문서에 TODO 마커를 주입함"
    # 표기 렌더는 이 파일에도 적용된다 — 치환/fill 만 제외지 렌더 제외가 아니다.
    assert "`/pm-handoff`(claude) / `$pm-handoff`(codex)" in after, after


def test_into_does_not_substitute_or_fill_untouched_existing_docs(pm_import, tmp_path):
    """`--into` 도 같다 — 복사하지 않는 기존 문서는 표기 렌더만 받는다."""
    dest = tmp_path / "into_nosubst"
    assert pm_import.main(
        ["--new", str(dest), "--harness", "codex", "--fill", "manual",
         "--from", str(REPO), "--name", "Into Subst"]) == 0
    state = dest / ".project_manager" / "wiki" / "pm_state.md"
    poisoned = state.read_text(encoding="utf-8") + "\n리터럴 {{PROJECT_NAME}} 보존 확인\n"
    state.write_text(poisoned, encoding="utf-8")

    assert pm_import.main(
        ["--into", str(dest), "--harness", "claude", "--fill", "manual",
         "--from", str(REPO), "--name", "Into Subst"]) == 0

    after = state.read_text(encoding="utf-8")
    assert "리터럴 {{PROJECT_NAME}} 보존 확인" in after, "복사 밖 기존 문서가 치환됨"
    assert "`/pm-handoff`(claude) / `$pm-handoff`(codex)" in after, "표기 렌더는 적용돼야 한다"


# ── T-0566 계획-적용 사이 symlink 교체(TOCTOU) ───────────────────────────────
# 경로 안전은 계획 시점(`_is_safe_dest_path`)에 판정한다. 그 판정과 실제 백업·렌더 쓰기 사이에
# 대상이 symlink 로 교체되면 판정이 무력해지고 경로 재열기가 링크를 따라 저장소 밖을 고친다.
# 실 IO 를 fd 로 옮겨(`O_NOFOLLOW` 컴포넌트 순회) 창 자체를 없앤 것이 T-0566 이다.
# 교체는 monkeypatch 로 **계획 통과 뒤 특정 단계 직전**에 주입해 결정적으로 재현한다.

# 저장소 밖 파일에도 표기 토큰을 둔다 — 링크를 따라갔다면 *실제로 바뀌었을* 내용이어야
# 테스트가 공허하지 않다(재렌더 산출 무변경이면 애초에 쓰기가 없다).
_OUTSIDE_TEXT = "EXTERNAL `/pm-bootstrap` 호출\n"


def _swap_to_outside_symlink(path: Path, outside: Path) -> None:
    """계획 검증을 통과한 대상 경로를 저장소 밖 지향 symlink 로 바꾼다(공격자 역할)."""
    path.unlink()
    path.symlink_to(outside)


@pytest.mark.skipif(os.name != "posix", reason="POSIX 전용 — fd 순회 지원 단언")
def test_fd_walk_is_active_on_posix(pm_import):
    """POSIX 에서는 구조적 폐쇄(fd 순회)가 켜져 있어야 한다 — 조용히 재검사 폴백으로 강등되면
    창이 좁아질 뿐 남는다. 지원 판정이 뒤집히면 이 가드가 먼저 red 로 알린다."""
    assert pm_import._DEST_FD_WALK_SUPPORTED


@requires_symlink
def test_nofollow_open_refuses_symlinked_final_component(pm_import, tmp_path):
    dest = tmp_path / "inst"
    (dest / "wiki").mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_text(_OUTSIDE_TEXT, encoding="utf-8")
    (dest / "wiki" / "doc.md").symlink_to(outside)

    with pytest.raises(pm_import.UnsafeDestPathError):
        pm_import._open_dest_relative_nofollow(dest, Path("wiki/doc.md"), os.O_RDONLY)


def test_nofollow_open_refuses_absolute_rel(pm_import, tmp_path):
    """절대경로·anchor 는 `dir_fd` 를 무시하고 그대로 열리므로 입구에서 거부한다(containment 원시가드).

    실측: `os.open("/etc/hostname", …, dir_fd=fd)` 는 dir_fd 를 무시하고 절대경로를 연다. 현 호출부는
    relpath 만 넘기지만 containment 는 호출부 규율이 아니라 이 함수에서 성립해야 한다. 같은 식의
    `drive` 항이 Windows `C:x`·UNC 도 덮는다(그 플랫폼에서만 관측 가능)."""
    dest = tmp_path / "inst"
    dest.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text(_OUTSIDE_TEXT, encoding="utf-8")

    for bad in (Path(outside), Path(outside.anchor)):
        with pytest.raises(pm_import.UnsafeDestPathError):
            pm_import._open_dest_relative_nofollow(dest, bad, os.O_RDONLY)
    assert outside.read_text(encoding="utf-8") == _OUTSIDE_TEXT


@requires_symlink
def test_dest_root_swap_after_plan_is_refused(pm_import, tmp_path):
    """계획 뒤 **dest 루트 자체**가 저장소 밖 지향 symlink 로 바뀌면 거부한다.

    컴포넌트 순회는 rel 안의 교체만 막는다 — 루트가 통째로 바뀌면 그 '안전한' 순회가 남의 트리에서
    일어난다. 계획 시점에 고정한 루트 신원을 **연 fd** 로 대조해 막는다."""
    dest = tmp_path / "inst"
    rel = Path(".project_manager/wiki/pm_role.md")
    (dest / rel).parent.mkdir(parents=True)
    (dest / rel).write_text("`/pm-bootstrap`\n", encoding="utf-8")
    root_identity = pm_import.dest_root_identity(dest)
    assert root_identity is not None
    # 공격자가 dest 루트를 밖의 동형 트리로 바꿔친다.
    decoy = tmp_path / "decoy"
    (decoy / rel).parent.mkdir(parents=True)
    (decoy / rel).write_text(_OUTSIDE_TEXT, encoding="utf-8")
    shutil.rmtree(dest)
    dest.symlink_to(decoy, target_is_directory=True)

    # 파일 단위 교체(`UnsafeDestPathError`)와 **다른 클래스**여야 한다 — 파일 단위 핸들러가
    #   흡수하면 실행이 계속되고 이후 단계가 교체된 트리에 쓴다.
    assert not issubclass(pm_import.DestRootSwappedError, pm_import.UnsafeDestPathError)
    with pytest.raises(pm_import.DestRootSwappedError, match="dest 루트"):
        pm_import.write_dest_text_keeping_newlines(
            dest, rel, "RENDERED\n", root_identity=root_identity)
    assert (decoy / rel).read_text(encoding="utf-8") == _OUTSIDE_TEXT
    # 고정을 안 주면(옛 동작) 같은 형상에서 밖이 열린다 — 이 가드가 load-bearing 이라는 증거.
    pm_import.write_dest_text_keeping_newlines(dest, rel, "RENDERED\n")
    assert (decoy / rel).read_text(encoding="utf-8") == "RENDERED\n"


def test_root_identity_failure_is_loud_not_a_disabled_check(pm_import, tmp_path, capsys):
    """루트 신원 획득 실패는 None 폴백이 아니라 **적용 전 오류**다.

    None 으로 삼키면 루트 교체 검사가 통째로 꺼진다 — "획득 순간만 해소 불가로 만들고 그 뒤
    교체" 라는 우회가 성립하므로, 못 잡으면 아무것도 열지 않고 멈춘다."""
    missing = tmp_path / "no_such_root"
    with pytest.raises(pm_import.UnsafeDestPathError, match="신원"):
        pm_import.dest_root_identity(missing)

    not_a_dir = tmp_path / "plain.txt"
    not_a_dir.write_text("x\n", encoding="utf-8")
    with pytest.raises(pm_import.UnsafeDestPathError):
        pm_import.dest_root_identity(not_a_dir)

    # `--into` 는 그 실패를 rc1 로 번역하고 복사를 시작하지 않는다.
    capsys.readouterr()
    rc = pm_import.main(
        ["--into", str(missing), "--harness", "codex", "--fill", "manual",
         "--from", str(REPO), "--name", "No Root"])
    assert rc == 1
    assert not missing.exists(), "복사가 시작됨(적용 전 중단 위반)"


@requires_symlink
def test_planned_target_swapped_to_broken_symlink_fails_the_plan(
        pm_import, tmp_path, monkeypatch):
    """계획의 `is_file()` 선검사 잔여 — 안전 판정 직후 깨진 symlink 로 바뀌면 옛 코드는 fd 가드에
    닿지도 못하고 조용히 제외했다. 이제 lstat 선검사가 링크를 후보로 통과시켜 **계획 단계
    fail-loud**(복사 전 rc1)로 끝난다."""
    dest = tmp_path / "inst"
    role = dest / ".project_manager" / "wiki" / "pm_role.md"
    role.parent.mkdir(parents=True)
    role.write_text("`/pm-bootstrap`\n", encoding="utf-8")
    missing = tmp_path / "NOT_THERE.md"  # 대상 부재 = 깨진 링크.
    original = pm_import._is_safe_dest_path

    def swap_after_check(dest_root, rel):
        verdict = original(dest_root, rel)
        if verdict and Path(rel).name == "pm_role.md" and not role.is_symlink():
            _swap_to_outside_symlink(role, missing)
        return verdict

    monkeypatch.setattr(pm_import, "_is_safe_dest_path", swap_after_check)
    contexts = {".project_manager/wiki/pm_role.md": ("codex", "opencode")}

    with pytest.raises(pm_import.UnsafeDestPathError, match="안전하지 않"):
        pm_import._shared_notation_rerender_plan(dest, contexts, None, None)
    assert not missing.exists(), "깨진 링크를 따라 저장소 밖에 파일을 만듦"


@requires_symlink
def test_backup_absorbs_target_deleted_after_precheck(
        pm_import, tmp_path, monkeypatch, capsys):
    """백업 선검사 뒤 삭제 경쟁도 파일 단위 제외 + loud 다 — 예외로 새면 부분 적용이 남는다.

    이 시점엔 복사·manifest 변경이 이미 끝나 있어, `FileNotFoundError` 전파는 곧 부분 적용 잔존이다.
    렌더 쓰기의 같은 경쟁 처리와 동형(제외 + 재렌더 목록에서 제거 + loud)."""
    dest = _live_instance(pm_import, tmp_path / "backup_vanish")
    role = dest / ".project_manager" / "wiki" / "pm_role.md"
    original = pm_import._is_inplace_edit_candidate
    prechecks = {"pm_role.md": 0}

    def delete_after_precheck(dest_root, rel):
        verdict = original(dest_root, rel)
        path = Path(dest_root) / rel
        name = Path(rel).name
        if verdict and name in prechecks:
            prechecks[name] += 1
            # 1회차는 계획 선검사다 — 그때 지우면 계획에서 빠져 백업 경쟁을 못 만든다.
            #   2회차(=백업 선검사) 직후에 지워 선검사↔열기 사이 삭제 경쟁을 만든다.
            if prechecks[name] == 2:
                Path(path).unlink()
        return verdict

    monkeypatch.setattr(pm_import, "_is_inplace_edit_candidate", delete_after_precheck)
    capsys.readouterr()

    pm_import.add_harness(dest, "codex", dry_run=False, source_root=REPO)

    err = capsys.readouterr().err
    assert "대상이 사라져" in err and "pm_role.md" in err, err
    assert not role.exists(), "사라진 대상을 다시 만듦"
    assert (dest / ".codex").is_dir(), "설치 전체가 중단됨(파일 단위 제외 아님)"


@requires_symlink
def test_byte_identical_scope_does_not_follow_a_swapped_link(
        pm_import, tmp_path, monkeypatch):
    """치환·렌더 범위 확장(`_byte_identical_skipped`)이 **안전 판정 뒤 교체된** 링크의 대상을 읽어
    범위에 넣지 않는다 — 경로 기반 소비처(치환)로 저장소 밖 경로가 유입되는 입구를 닫는다.

    교체를 `_is_safe_dest_path` 통과 직후에 주입해야 이 창이 재현된다(교체가 먼저면 그 판정이
    이미 거른다)."""
    dest = _live_instance(pm_import, tmp_path / "byteid")
    template_root = REPO / "templates" / "claude_code"
    rel = Path(".claude") / "settings.json"
    src = template_root / rel
    assert src.is_file(), "픽스처 전제(템플릿 파일 실재)"
    outside = tmp_path / "outside_settings.json"
    outside.write_bytes(src.read_bytes())  # byte-identical 이지만 저장소 밖.
    target = dest / rel
    original = pm_import._is_safe_dest_path

    def swap_after_check(dest_root, checked):
        verdict = original(dest_root, checked)
        if verdict and Path(checked) == rel and not target.is_symlink():
            target.unlink()
            target.symlink_to(outside)
        return verdict

    monkeypatch.setattr(pm_import, "_is_safe_dest_path", swap_after_check)

    out = pm_import._byte_identical_skipped(
        template_root, dest, set(), pm_import.ADD_HARNESS_ADAPTER["claude"][0])

    assert target.is_symlink(), "픽스처 전제(판정 뒤 교체가 실제로 일어남)"
    assert rel not in out, "링크를 따라가 byte-identical 로 보고 치환·렌더 범위에 넣음"


def _swapped_copied_file(tmp_path: Path, content: str) -> tuple[Path, Path, Path]:
    """복사 직후 저장소 밖 지향 symlink 로 교체된 '복사분' 파일 형상 — (dest, rel, outside)."""
    dest = tmp_path / "inst"
    dest.mkdir()
    rel = Path("doc.md")
    (dest / rel).write_text(content, encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text(content, encoding="utf-8")
    (dest / rel).unlink()
    (dest / rel).symlink_to(outside)
    return dest, rel, outside


@requires_symlink
def test_substitute_channel_refuses_swapped_copied_file(pm_import, tmp_path, capsys):
    """치환 채널: 복사분이 교체되면 링크를 따라 저장소 밖을 고치지 않고 loud 제외한다."""
    dest, rel, outside = _swapped_copied_file(tmp_path, "이름은 {{PROJECT_NAME}} 이다\n")
    capsys.readouterr()

    changed = pm_import.substitute_placeholders(dest, {"{{PROJECT_NAME}}": "Swapped"}, {rel})

    assert changed == 0
    assert outside.read_text(encoding="utf-8") == "이름은 {{PROJECT_NAME}} 이다\n"
    assert "placeholder 치환" in capsys.readouterr().err


@requires_symlink
def test_model_token_channel_refuses_swapped_copied_file(pm_import, tmp_path, capsys):
    """opencode 모델 토큰 치환 채널도 같다 — 교체된 복사분은 열지 않는다."""
    dest, rel, outside = _swapped_copied_file(
        tmp_path, f'model: "{pm_import.OPENCODE_MODEL_TOKEN}"\n')
    before = outside.read_text(encoding="utf-8")
    capsys.readouterr()

    changed = pm_import._substitute_model_token(dest, "provider/model", {rel})

    assert changed == 0
    assert outside.read_text(encoding="utf-8") == before
    assert "모델 토큰 치환" in capsys.readouterr().err


@requires_symlink
def test_manual_fill_channel_refuses_swapped_copied_file(pm_import, tmp_path, capsys):
    """자유서술 TODO 표시(fill) 채널도 같다 — 교체된 복사분에 마커를 주입하지 않는다."""
    token = "{{PROJECT_CONSTRAINTS}}"
    dest, rel, outside = _swapped_copied_file(tmp_path, f"제약: {token}\n")
    capsys.readouterr()

    marked = pm_import._mark_todos(dest, [token], {rel})

    assert marked == []
    assert outside.read_text(encoding="utf-8") == f"제약: {token}\n"
    assert "자유서술 TODO 표시" in capsys.readouterr().err


@requires_symlink
def test_root_swap_aborts_the_whole_run_not_just_one_file(
        pm_import, tmp_path, monkeypatch, capsys):
    """루트 교체는 파일 단위 흡수가 아니라 **즉시 전체 중단**이다.

    흡수하면 이후 단계(치환·fill·board init 등 fd 로 감싸지 못하는 외부 단계 포함)가 교체된
    트리에 계속 쓴다. 중단은 부분 적용이 아니라 오염 차단이다 — 대상 트리가 이미 바꿔치기됐다."""
    dest = _live_instance(pm_import, tmp_path / "rootswap")
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    (decoy / "MARKER.md").write_text("DECOY\n", encoding="utf-8")
    moved = tmp_path / "moved_real_instance"
    original = pm_import._backup_before_inplace_edit

    def swap_root_then_backup(*args, **kwargs):
        if not dest.is_symlink():
            dest.rename(moved)           # 공격자가 원본을 치우고
            dest.symlink_to(decoy, target_is_directory=True)  # 그 자리에 밖을 가리키는 링크.
        return original(*args, **kwargs)

    monkeypatch.setattr(pm_import, "_backup_before_inplace_edit", swap_root_then_backup)
    capsys.readouterr()

    rc = pm_import.add_harness_cli(dest, "codex", dry_run=False, source_root=REPO)

    err = capsys.readouterr().err
    assert rc == 1
    assert "dest 루트" in err and "Traceback" not in err, err
    assert sorted(p.name for p in decoy.iterdir()) == ["MARKER.md"], \
        "교체된 트리에 남은 단계가 계속 씀(전체 중단 아님)"
    assert (decoy / "MARKER.md").read_text(encoding="utf-8") == "DECOY\n"


@requires_symlink
def test_copy_channel_refuses_root_swapped_right_after_plan(
        pm_import, tmp_path, monkeypatch, capsys):
    """계획 **직후**(첫 복사 전) 루트가 교체되면 복사 자체가 외부 트리에 아무것도 쓰지 않는다.

    옛 순서는 `CopyAction.run()` *뒤에* 신원을 봤다 — 그 사이 `mkdir(parents=True)`+`copy2` 가
    교체된 트리에 먼저 썼다(디렉토리 생성이 첫 외부 쓰기)."""
    dest = _live_instance(pm_import, tmp_path / "copyswap")
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    moved = tmp_path / "moved_real_instance"
    original = pm_import.plan_copy

    def plan_then_swap_root(*args, **kwargs):
        plan = original(*args, **kwargs)
        if not dest.is_symlink():
            dest.rename(moved)
            dest.symlink_to(decoy, target_is_directory=True)
        return plan

    monkeypatch.setattr(pm_import, "plan_copy", plan_then_swap_root)
    capsys.readouterr()

    rc = pm_import.add_harness_cli(dest, "codex", dry_run=False, source_root=REPO)

    err = capsys.readouterr().err
    assert rc == 1
    assert "dest 루트" in err and "Traceback" not in err, err
    assert list(decoy.iterdir()) == [], "교체된 트리에 복사가 먼저 씀(신원 검사가 복사 뒤)"


@requires_symlink
def test_copy_channel_refuses_ancestor_swapped_between_plan_and_copy(
        pm_import, tmp_path, monkeypatch, capsys):
    """계획↔복사 사이 **조상 디렉토리**가 저장소 밖 지향 symlink 로 바뀌면 유출 0.

    옛 형상에서 `dst.parent.mkdir(parents=True)` 가 링크를 따라가 밖에 디렉토리를 만들고
    `copy2` 가 그 아래로 파일을 쏟았다(reviewer 실측: 25파일 유출). 이제 디렉토리 생성과 파일
    쓰기가 모두 fd 순회를 타므로 첫 걸음에서 거부된다.

    거부는 **파일 단위 제외 + loud** 다(rc 0) — 이미 복사된 파일이 있는 적용 단계에서 전체를
    중단하면 오히려 부분 설치가 남는다(모듈 docstring 의 rc 정책). 루트 교체만 전체 중단이다."""
    dest = tmp_path / "ancestor_swap"
    assert pm_import.main(
        ["--new", str(dest), "--harness", "claude", "--fill", "manual",
         "--from", str(REPO), "--name", "Ancestor Swap"]) == 0
    outside = tmp_path / "outside_tree"
    outside.mkdir()
    wiki = dest / ".project_manager" / "wiki"
    original = pm_import.plan_copy

    def plan_then_swap_ancestor(*args, **kwargs):
        plan = original(*args, **kwargs)
        if not wiki.is_symlink():
            shutil.rmtree(wiki)
            wiki.symlink_to(outside, target_is_directory=True)
        return plan

    monkeypatch.setattr(pm_import, "plan_copy", plan_then_swap_ancestor)
    capsys.readouterr()

    rc = pm_import.main(
        ["--into", str(dest), "--harness", "codex", "--fill", "manual",
         "--from", str(REPO), "--name", "Ancestor Swap"])

    err = capsys.readouterr().err
    assert rc == 0, "적용 단계 파일 단위 제외가 전체 중단으로 바뀜(rc 정책 위반)"
    assert "Traceback" not in err, err
    # 제외 사유는 둘 중 하나다 — 조상이 빈 밖 트리로 바뀌면 계획 때 있던 대상이 "사라진" 것으로
    #   먼저 걸리고(상태 변화), 대상이 밖에 실재하면 경로 가드가 거른다. 어느 쪽이든 요약은 loud.
    assert "복사 대상" in err and ".project_manager/wiki/" in err, "조용한 제외(요약 없음)"
    assert list(outside.iterdir()) == [], "저장소 밖 트리에 복사가 씀(디렉토리 생성 포함)"
    # 스코프 밖 파일은 정상 설치된다(전체 중단이 아니라 파일 단위 제외).
    assert (dest / ".codex").is_dir()


@requires_symlink
def test_dest_file_write_refuses_symlinked_target(pm_import, tmp_path):
    """복사 목적지가 symlink 로 선점돼 있으면 신규·덮어쓰기 어느 형상에서도 링크를 따라가지 않는다.

    옛 `shutil.copy2(src, dst)` 는 링크를 따라가 저장소 밖 파일을 덮었다."""
    dest = tmp_path / "inst"
    (dest / "a").mkdir(parents=True)
    src = tmp_path / "src.md"
    src.write_text("TEMPLATE\n", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("EXTERNAL\n", encoding="utf-8")
    rel = Path("a/b.md")
    (dest / rel).symlink_to(outside)

    # 두 클래스 모두 **파일 단위 제외**다 — 교체(경로 안전)이거나 계획 뒤 상태 변화(EEXIST).
    #   raw OSError 로 새면 적용 루프가 통째로 죽어 rc 정책이 깨지므로 정규화된 클래스여야 한다.
    for overwrite in (False, True):
        with pytest.raises(
                (pm_import.UnsafeDestPathError, pm_import.PlanStateChangedError)):
            pm_import._write_dest_file_from_source_nofollow(
                dest, rel, src, overwrite=overwrite)
    assert outside.read_text(encoding="utf-8") == "EXTERNAL\n"


def test_copy_action_requires_dest_root_binding(pm_import, tmp_path):
    """손-구성 액션은 실행 대상이 아니다 — dest 상대 fd 순회를 할 수 없으므로 fail-loud."""
    src = tmp_path / "src.md"
    src.write_text("x\n", encoding="utf-8")
    action = pm_import.CopyAction(src, tmp_path / "inst" / "dst.md", None)
    with pytest.raises(ValueError, match="dest_root"):
        action.run()


@requires_symlink
def test_absent_target_under_symlinked_ancestor_is_not_silently_skipped(
        pm_import, tmp_path):
    """조상이 깨진 symlink 로 교체되면 `lstat` 은 ENOENT 다 — 진짜 부재와 구분해 후보로 통과시킨다.

    삼키면 요구된 loud 제외가 그 축에서만 침묵이 된다. 진짜 부재(조상 정상)는 그대로 조용한 제외."""
    dest = tmp_path / "inst"
    (dest / ".project_manager").mkdir(parents=True)
    rel = Path(".project_manager/wiki/pm_role.md")
    (dest / ".project_manager" / "wiki").symlink_to(tmp_path / "NOT_THERE")

    assert pm_import._is_inplace_edit_candidate(dest, rel) is True
    # 조상이 정상인 진짜 부재는 조용한 제외(원래 대상이 아니다).
    assert pm_import._is_inplace_edit_candidate(
        dest, Path(".project_manager/absent.md")) is False


@requires_symlink
def test_token_scan_reports_swapped_file_instead_of_judging_absent(
        pm_import, tmp_path, capsys):
    """토큰 판정이 교체된 파일을 '토큰 없음'으로 오판정하지 않고 loud 로 알린다.

    삼키면 모델 해소가 inactive 로 빠지거나 fill 대상이 조용히 줄어든다(판정 채널의 침묵 degrade)."""
    token = pm_import.OPENCODE_MODEL_TOKEN
    dest, rel, _outside = _swapped_copied_file(tmp_path, f'model: "{token}"\n')
    swapped: list[str] = []

    present = pm_import._token_present(dest, token, {rel}, swapped=swapped)

    assert present is False and swapped == ["doc.md"]
    capsys.readouterr()
    result = pm_import.resolve_opencode_model(dest, {rel}, model_arg=None)
    assert result.active is False  # 읽을 수 없으니 판정은 보수적이되,
    assert "모델 토큰 판정" in capsys.readouterr().err  # 조용하지 않다.


def test_mark_todos_does_not_report_excluded_token_as_marked(
        pm_import, tmp_path, monkeypatch, capsys):
    """쓰기가 제외된 파일의 토큰을 '표시됨'으로 보고하지 않는다(성공 선반영 금지).

    읽기는 되고 **쓰기만** 제외되는 경쟁(읽은 뒤 삭제)이 이 성질의 시험대다 — 읽기 단계에서
    걸리는 교체는 애초에 줄 루프에 닿지 않아 구분이 안 된다."""
    token = "{{PROJECT_CONSTRAINTS}}"
    dest = tmp_path / "inst"
    dest.mkdir()
    rel = Path("doc.md")
    (dest / rel).write_text(f"제약: {token}\n", encoding="utf-8")
    original_read = pm_import.read_dest_text

    def read_then_delete(dest_root, read_rel, **kwargs):
        text = original_read(dest_root, read_rel, **kwargs)
        (Path(dest_root) / read_rel).unlink()  # 읽기↔쓰기 사이 삭제 경쟁.
        return text

    monkeypatch.setattr(pm_import, "read_dest_text", read_then_delete)
    capsys.readouterr()

    marked = pm_import._mark_todos(dest, [token], {rel})

    assert marked == [], "쓰기가 제외됐는데 '표시됨'으로 보고함(성공 선반영)"
    assert not (dest / rel).exists(), "사라진 대상을 새로 만듦"
    assert "자유서술 TODO 표시" in capsys.readouterr().err


def test_main_translates_dest_safety_errors_to_rc1(pm_import, capsys):
    """`main` 은 **두 경로 안전 클래스 모두** rc1 + 친화 메시지로 번역한다(traceback 0).

    복사 단계의 fd 가드도 `UnsafeDestPathError` 를 던지므로 백스톱이 없으면 그 경로만 traceback
    으로 끝나 `add_harness_cli` 와 비대칭이 된다. 본문을 별도 함수로 쪼개지 않고 데코레이터로
    감싼다 — 엔진 관용구 가드(진입 `main()` 의 console helper 선행·하네스 이름 분기 면제)가
    함수 **이름** 기준이라 쪼개면 함께 깨진다."""
    for error in (pm_import.DestRootSwappedError("dest 루트가 바뀌었습니다"),
                  pm_import.UnsafeDestPathError("복사 중 경로가 바뀌었습니다")):
        def raise_error(argv=None, exc=error):
            raise exc

        capsys.readouterr()
        rc = pm_import._translate_dest_safety_errors(raise_error)(
            ["--into", "x", "--harness", "codex"])

        err = capsys.readouterr().err
        assert rc == 1
        assert err.startswith("오류: ") and "Traceback" not in err, err
    # 실제 진입도 그 데코레이터를 쓴다(래핑 누락이면 이 단언이 red).
    assert pm_import.main.__wrapped__ is not None


@requires_symlink
def test_broken_symlink_target_is_loudly_excluded_not_silently_skipped(
        pm_import, tmp_path, monkeypatch, capsys):
    """깨진 symlink 로 교체된 대상은 **조용히 생략되지 않고** 파일 단위 loud 제외를 탄다.

    옛 `is_file()` 선검사는 링크를 따라가 깨진 링크를 False 로 보고 그대로 지나쳤다 — 요구된
    loud 제외가 그 경로에서만 침묵으로 바뀌던 지점이다."""
    dest = _live_instance(pm_import, tmp_path / "broken")
    role = dest / ".project_manager" / "wiki" / "pm_role.md"
    missing = tmp_path / "NOT_THERE.md"  # 대상 부재 = 깨진 링크.
    original = pm_import._backup_before_inplace_edit

    def swap_then_backup(*args, **kwargs):
        if not role.is_symlink():
            _swap_to_outside_symlink(role, missing)
        return original(*args, **kwargs)

    monkeypatch.setattr(pm_import, "_backup_before_inplace_edit", swap_then_backup)
    capsys.readouterr()

    pm_import.add_harness(dest, "codex", dry_run=False, source_root=REPO)

    err = capsys.readouterr().err
    assert "계획 검증 뒤" in err and "pm_role.md" in err, err
    assert not missing.exists(), "깨진 링크를 따라 저장소 밖에 파일을 만듦"
    assert role.is_symlink(), "링크 자체가 바뀜(비파괴 위반)"


@requires_symlink
def test_nofollow_open_refuses_symlinked_ancestor(pm_import, tmp_path):
    """마지막 컴포넌트만이 아니라 **조상** 교체도 거부한다(조상 링크로도 밖을 쓴다)."""
    dest = tmp_path / "inst"
    dest.mkdir()
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    (outside_dir / "doc.md").write_text(_OUTSIDE_TEXT, encoding="utf-8")
    (dest / "wiki").symlink_to(outside_dir, target_is_directory=True)

    with pytest.raises(pm_import.UnsafeDestPathError):
        pm_import._open_dest_relative_nofollow(dest, Path("wiki/doc.md"), os.O_RDONLY)


def test_safe_dest_roundtrip_preserves_newlines_and_never_creates(pm_import, tmp_path):
    """안전 읽기/쓰기 짝은 줄끝을 보존하고(T-0541 성질) 없는 파일을 만들지 않는다(제자리 편집)."""
    dest = tmp_path / "inst"
    rel = Path(".project_manager/wiki/doc.md")
    (dest / rel).parent.mkdir(parents=True)
    (dest / rel).write_bytes(b"a\r\nb\r\n")

    text = pm_import.read_dest_text_keeping_newlines(dest, rel)
    assert text == "a\r\nb\r\n"
    pm_import.write_dest_text_keeping_newlines(dest, rel, text.replace("a", "c"))
    assert (dest / rel).read_bytes() == b"c\r\nb\r\n"

    with pytest.raises(FileNotFoundError):
        pm_import.write_dest_text_keeping_newlines(
            dest, Path(".project_manager/wiki/absent.md"), "x")


@requires_symlink
def test_recheck_fallback_also_refuses_the_swapped_path(pm_import, tmp_path, monkeypatch):
    """dir_fd/`O_NOFOLLOW` 미지원 플랫폼 폴백(열기 직전 재검사)도 교체된 경로를 거부한다.

    그 분기는 Linux CI 에서 절대 안 돌아 죽은 코드가 되기 쉬우므로 지원 판정을 꺼서 강제 실행한다."""
    monkeypatch.setattr(pm_import, "_DEST_FD_WALK_SUPPORTED", False)
    dest = tmp_path / "inst"
    rel = Path(".project_manager/wiki/doc.md")
    (dest / rel).parent.mkdir(parents=True)
    (dest / rel).write_bytes(b"a\r\n")
    outside = tmp_path / "outside.md"
    outside.write_text(_OUTSIDE_TEXT, encoding="utf-8")

    assert pm_import.read_dest_text_keeping_newlines(dest, rel) == "a\r\n"
    pm_import.write_dest_text_keeping_newlines(dest, rel, "b\r\n")
    assert (dest / rel).read_bytes() == b"b\r\n"

    _swap_to_outside_symlink(dest / rel, outside)
    with pytest.raises(pm_import.UnsafeDestPathError):
        pm_import.write_dest_text_keeping_newlines(dest, rel, "rendered\r\n")
    assert outside.read_text(encoding="utf-8") == _OUTSIDE_TEXT


@requires_symlink
def test_plan_preview_read_refuses_swap_after_safety_check(pm_import, tmp_path, monkeypatch):
    """`_is_safe_dest_path` 통과와 미리보기 읽기 사이 교체 → 계획 단계 fail-loud(복사 전 중단).

    계획 단계는 아직 아무것도 복사하지 않았으므로 전체 중단이 맞다(T-0541 ④ 와 같은 성질)."""
    dest = tmp_path / "inst"
    role = dest / ".project_manager" / "wiki" / "pm_role.md"
    role.parent.mkdir(parents=True)
    role.write_text("`/pm-bootstrap`\n", encoding="utf-8")
    outside = tmp_path / "OUTSIDE_ROLE.md"
    outside.write_text(_OUTSIDE_TEXT, encoding="utf-8")
    original = pm_import._is_safe_dest_path

    def swap_after_check(dest_root, rel):
        verdict = original(dest_root, rel)
        if verdict and Path(rel).name == "pm_role.md" and not role.is_symlink():
            _swap_to_outside_symlink(role, outside)
        return verdict

    monkeypatch.setattr(pm_import, "_is_safe_dest_path", swap_after_check)
    contexts = {".project_manager/wiki/pm_role.md": ("codex", "opencode")}

    with pytest.raises(pm_import.UnsafeDestPathError, match="안전하지 않"):
        pm_import._shared_notation_rerender_plan(dest, contexts, None, None)
    assert outside.read_text(encoding="utf-8") == _OUTSIDE_TEXT


@requires_symlink
def test_backup_refuses_source_swapped_after_plan(pm_import, tmp_path):
    """계획 뒤 원본이 symlink 로 바뀌면 백업을 거부하고 그 relpath 를 호출부에 돌려준다.

    옛 `copy2(follow_symlinks=False)` 는 **링크 자체**를 백업해 "백업했다"고 보고했다 —
    복원 불가 백업 위에서 재렌더가 진행됐다."""
    dest = tmp_path / "inst"
    role = dest / ".project_manager" / "wiki" / "pm_role.md"
    role.parent.mkdir(parents=True)
    role.write_text("`/pm-bootstrap`\n", encoding="utf-8")
    outside = tmp_path / "OUTSIDE_ROLE.md"
    outside.write_text(_OUTSIDE_TEXT, encoding="utf-8")
    backup_root = dest / pm_import.BACKUP_DIR_NAME / "2026-01-01"
    rel = Path(".project_manager/wiki/pm_role.md")
    assert pm_import._is_safe_dest_path(dest, rel)  # 계획 시점 통과(사전조건).
    _swap_to_outside_symlink(role, outside)

    outcome = pm_import._backup_before_inplace_edit(dest, [rel], backup_root, set())

    assert outcome.backed_up == [] and outcome.vanished == []
    assert outcome.refused == [".project_manager/wiki/pm_role.md"]
    assert not (backup_root / rel).exists(), "교체된 경로를 백업 대상으로 삼음"
    assert outside.read_text(encoding="utf-8") == _OUTSIDE_TEXT


@requires_symlink
def test_backup_refuses_when_backup_dir_is_swapped_to_outside_link(pm_import, tmp_path):
    """백업 target 의 **조상**이 계획 뒤 저장소 밖 지향 symlink 로 바뀌어도 거부한다 —
    옛 경로 기반 `mkdir(parents=True)`+copy2 는 링크를 따라 사용자 파일 내용을 밖에 썼다."""
    dest = tmp_path / "inst"
    role = dest / ".project_manager" / "wiki" / "pm_role.md"
    role.parent.mkdir(parents=True)
    role.write_text("사용자 내용 `/pm-bootstrap`\n", encoding="utf-8")
    outside_dir = tmp_path / "outside_backups"
    outside_dir.mkdir()
    (dest / pm_import.BACKUP_DIR_NAME).symlink_to(outside_dir, target_is_directory=True)
    backup_root = dest / pm_import.BACKUP_DIR_NAME / "2026-01-01"
    rel = Path(".project_manager/wiki/pm_role.md")

    outcome = pm_import._backup_before_inplace_edit(dest, [rel], backup_root, set())

    assert outcome.backed_up == [] and outcome.vanished == []
    assert outcome.refused == [".project_manager/wiki/pm_role.md"]
    assert list(outside_dir.iterdir()) == [], "저장소 밖에 사용자 파일 내용을 씀"


@requires_symlink
def test_sensitivity_path_based_backup_writes_outside_the_repo(pm_import, tmp_path):
    """sensitivity — 같은 형상에서 **옛 경로 기반 백업**은 저장소 밖에 내용을 쓴다(회귀 증거).

    fd 백업을 되돌리면 이 결과가 실제 동작이 되므로, 위 거부 가드는 load-bearing 이다."""
    dest = tmp_path / "inst"
    role = dest / ".project_manager" / "wiki" / "pm_role.md"
    role.parent.mkdir(parents=True)
    role.write_text("사용자 내용\n", encoding="utf-8")
    outside_dir = tmp_path / "outside_backups"
    outside_dir.mkdir()
    (dest / pm_import.BACKUP_DIR_NAME).symlink_to(outside_dir, target_is_directory=True)
    rel = Path(".project_manager/wiki/pm_role.md")

    target = pm_import._free_backup_path(
        dest / pm_import.BACKUP_DIR_NAME / "2026-01-01" / rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(dest / rel, target, follow_symlinks=False)

    leaked = outside_dir / "2026-01-01" / rel
    assert leaked.read_text(encoding="utf-8") == "사용자 내용\n", \
        "옛 경로 기반 백업이 밖으로 안 샜다면 이 형상은 TOCTOU 증거가 아니다"


@requires_symlink
def test_add_harness_refuses_swap_between_plan_and_backup(
        pm_import, tmp_path, monkeypatch, capsys):
    """계획 뒤·백업 직전 교체 → 그 파일만 제외 + loud, 저장소 밖 쓰기 0, 나머지 설치는 완료."""
    dest = _live_instance(pm_import, tmp_path / "swap_backup")
    role = dest / ".project_manager" / "wiki" / "pm_role.md"
    outside = tmp_path / "OUTSIDE_ROLE.md"
    outside.write_text(_OUTSIDE_TEXT, encoding="utf-8")
    original = pm_import._backup_before_inplace_edit

    def swap_then_backup(*args, **kwargs):
        _swap_to_outside_symlink(role, outside)
        return original(*args, **kwargs)

    monkeypatch.setattr(pm_import, "_backup_before_inplace_edit", swap_then_backup)
    capsys.readouterr()

    pm_import.add_harness(dest, "codex", dry_run=False, source_root=REPO)

    err = capsys.readouterr().err
    assert outside.read_text(encoding="utf-8") == _OUTSIDE_TEXT, \
        "저장소 밖 파일이 재렌더로 변경됨(TOCTOU 미해소)"
    assert "계획 검증 뒤" in err and "pm_role.md" in err, err
    assert (dest / ".codex").is_dir(), "한 파일 이상 때문에 설치 전체가 중단됨(파일 단위 제외 아님)"


@requires_symlink
def test_add_harness_refuses_swap_between_backup_and_render(
        pm_import, tmp_path, monkeypatch, capsys):
    """백업까지 끝난 뒤 교체돼도 렌더 쓰기가 링크를 따라가지 않는다(창의 두 번째 구간)."""
    dest = _live_instance(pm_import, tmp_path / "swap_render")
    role = dest / ".project_manager" / "wiki" / "pm_role.md"
    before = role.read_text(encoding="utf-8")
    outside = tmp_path / "OUTSIDE_ROLE.md"
    outside.write_text(_OUTSIDE_TEXT, encoding="utf-8")
    original = pm_import.render_managed_files

    def swap_then_render(*args, **kwargs):
        if not role.is_symlink():
            _swap_to_outside_symlink(role, outside)
        return original(*args, **kwargs)

    monkeypatch.setattr(pm_import, "render_managed_files", swap_then_render)
    capsys.readouterr()

    pm_import.add_harness(dest, "codex", dry_run=False, source_root=REPO)

    err = capsys.readouterr().err
    assert outside.read_text(encoding="utf-8") == _OUTSIDE_TEXT, \
        "렌더 쓰기가 교체된 링크를 따라 저장소 밖을 고침"
    assert "계획 검증 뒤" in err and "pm_role.md" in err, err
    # 백업은 교체 *전* 원본을 담았다 — 비파괴 복원 가능성 불변.
    backup = (dest / pm_import.BACKUP_DIR_NAME / datetime.date.today().isoformat()
              / ".project_manager" / "wiki" / "pm_role.md")
    assert backup.read_text(encoding="utf-8") == before


@requires_symlink
def test_sensitivity_path_based_render_write_follows_the_swapped_symlink(
        pm_import, tmp_path, monkeypatch, capsys):
    """sensitivity — 안전 읽기/쓰기를 옛 경로 기반으로 되돌리면 저장소 밖 파일이 실제로 바뀐다."""
    dest = _live_instance(pm_import, tmp_path / "swap_naive")
    role = dest / ".project_manager" / "wiki" / "pm_role.md"
    outside = tmp_path / "OUTSIDE_ROLE.md"
    outside.write_text(_OUTSIDE_TEXT, encoding="utf-8")
    monkeypatch.setattr(
        pm_import, "read_dest_text_keeping_newlines",
        lambda dest_root, rel, **kwargs: pm_import.read_text_keeping_newlines(
            dest_root / rel))
    monkeypatch.setattr(
        pm_import, "write_dest_text_keeping_newlines",
        lambda dest_root, rel, text, **kwargs: pm_import.write_text_keeping_newlines(
            dest_root / rel, text))
    original = pm_import.render_managed_files

    def swap_then_render(*args, **kwargs):
        if not role.is_symlink():
            _swap_to_outside_symlink(role, outside)
        return original(*args, **kwargs)

    monkeypatch.setattr(pm_import, "render_managed_files", swap_then_render)
    capsys.readouterr()

    pm_import.add_harness(dest, "codex", dry_run=False, source_root=REPO)

    assert outside.read_text(encoding="utf-8") != _OUTSIDE_TEXT, \
        "옛 경로 기반 쓰기가 밖을 안 고쳤다면 이 형상은 TOCTOU 증거가 아니다"
    assert "(codex)" in outside.read_text(encoding="utf-8")


def test_render_absorbs_target_vanishing_between_read_and_write(
        pm_import, tmp_path, monkeypatch, capsys):
    """읽은 뒤 쓰기 전에 대상이 삭제되면 uncaught 로 터지지 않고 파일 단위 loud 제외로 흡수한다.

    적용 단계에서 예외가 새면 "경로 예외는 복사 전에만 던진다 → 부분 적용 0" 불변식이 깨진다.
    `O_CREAT` 를 안 주므로 사라진 파일을 되살리지도 않는다."""
    dest = _live_instance(pm_import, tmp_path / "vanish")
    role = dest / ".project_manager" / "wiki" / "pm_role.md"
    original_read = pm_import.read_dest_text_keeping_newlines
    reads = {"pm_role.md": 0}

    def read_then_delete(dest_root, rel, **kwargs):
        text = original_read(dest_root, rel, **kwargs)
        name = Path(rel).name
        if name in reads:
            reads[name] += 1
            # 1회차는 계획 미리보기 읽기다 — 그때 지우면 계획 자체에서 빠져 창을 못 만든다.
            #   2회차(=렌더 읽기) 직후에 지워 읽기↔쓰기 사이 삭제 경쟁을 만든다.
            if reads[name] == 2:
                role.unlink()
        return text

    monkeypatch.setattr(pm_import, "read_dest_text_keeping_newlines", read_then_delete)
    capsys.readouterr()

    pm_import.add_harness(dest, "codex", dry_run=False, source_root=REPO)

    err = capsys.readouterr().err
    assert "대상이 사라져" in err and "pm_role.md" in err, err
    assert not role.exists(), "사라진 대상을 렌더가 새로 만듦(제자리 편집 위반)"
    assert (dest / ".codex").is_dir(), "설치 전체가 중단됨(파일 단위 제외 아님)"


@requires_symlink
def test_into_refuses_swap_between_plan_and_backup(
        pm_import, tmp_path, monkeypatch, capsys):
    """`--into` 축(두 번째 호출부)도 같은 제외·loud 배선을 탄다 — rc 0·저장소 밖 쓰기 0."""
    dest = tmp_path / "into_swap"
    assert pm_import.main(
        ["--new", str(dest), "--harness", "claude", "--fill", "manual",
         "--from", str(REPO), "--name", "Into Swap"]) == 0
    seed = dest / ".project_manager" / "wiki" / "raw" / "README.md"
    state = dest / ".project_manager" / "wiki" / "pm_state.md"
    outside = tmp_path / "OUTSIDE_SEED.md"
    outside.write_text(_OUTSIDE_TEXT, encoding="utf-8")
    original = pm_import._backup_before_inplace_edit

    def swap_then_backup(*args, **kwargs):
        if not seed.is_symlink():
            _swap_to_outside_symlink(seed, outside)
        return original(*args, **kwargs)

    monkeypatch.setattr(pm_import, "_backup_before_inplace_edit", swap_then_backup)
    capsys.readouterr()

    rc = pm_import.main(
        ["--into", str(dest), "--harness", "codex", "--fill", "manual",
         "--from", str(REPO), "--name", "Into Swap"])

    err = capsys.readouterr().err
    assert rc == 0
    assert outside.read_text(encoding="utf-8") == _OUTSIDE_TEXT
    assert "계획 검증 뒤" in err and "raw/README.md" in err, err
    # 같은 계획의 다른 대상은 정상 재렌더된다(전체 중단 아님·파일 단위 제외).
    assert "(codex)" in state.read_text(encoding="utf-8")


def test_unbackupable_shared_doc_is_dropped_from_rerender(pm_import, tmp_path):
    """백업 자리가 막혀 있으면 그 파일은 재렌더 대상에서 빠진다 — 못 백업하면 안 고친다."""
    dest = tmp_path / "inst"
    role = dest / ".project_manager" / "wiki" / "pm_role.md"
    role.parent.mkdir(parents=True)
    role.write_text("`/pm-bootstrap`\n", encoding="utf-8")
    (dest / pm_import.BACKUP_DIR_NAME).write_text("not a directory", encoding="utf-8")
    backup_root = dest / pm_import.BACKUP_DIR_NAME / "2026-01-01"
    contexts = {".project_manager/wiki/pm_role.md": ("codex", "opencode")}

    targets, blocked = pm_import._shared_notation_rerender_plan(
        dest, contexts, backup_root, set())
    assert targets == []
    assert blocked == [".project_manager/wiki/pm_role.md"]


# ── 교체 축의 남은 파생: 비-일반 파일(FIFO)로의 교체 ─────────────────────────
# `O_NOFOLLOW` 는 symlink 만 거른다. 계획을 통과한 대상이 FIFO 로 바뀌면 유출이 아니라 **정지**가
# 된다 — `O_RDONLY` 열기가 writer 를 기다리며 무기한 블록해 설치가 그 자리에 선다. 내용 IO 채널은
# `O_NONBLOCK` 으로 열고 연 fd 를 `fstat` 해 일반 파일이 아니면 거부한다.
# 정적 형상(처음부터 FIFO)은 계획 선검사(`_is_inplace_edit_candidate`)가 이미 대상에서 빼므로,
# 아래 재현은 전부 **선검사 통과 뒤** 교체다(그 창이 이 가드의 대상).

requires_fifo = pytest.mark.skipif(
    not hasattr(os, "mkfifo"), reason="POSIX 전용 — mkfifo 미지원 플랫폼")


@contextlib.contextmanager
def _fails_instead_of_hanging(seconds: int = 10):
    """가드가 회귀하면 이 블록은 **영원히 멈춘다** — 알람으로 실패 전환해 suite 정지를 막는다."""
    def _timeout(_signum, _frame):
        raise AssertionError(
            f"{seconds}초 안에 반환하지 않음 — 비-일반 파일 열기가 블록됨(가드 회귀)")

    previous = signal.signal(signal.SIGALRM, _timeout)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def _fifo_target(pm_import, tmp_path: Path) -> tuple[Path, Path]:
    """`dest/rel` 자리에 FIFO 가 놓인 인스턴스 형상 — (dest, rel)."""
    dest = tmp_path / "inst"
    rel = Path(".project_manager/wiki/pm_role.md")
    (dest / rel).parent.mkdir(parents=True)
    os.mkfifo(dest / rel)
    return dest, rel


@requires_fifo
@pytest.mark.parametrize("fd_walk", [True, False])
def test_content_read_refuses_a_fifo_instead_of_blocking(
        pm_import, tmp_path, monkeypatch, fd_walk):
    """두 읽기 채널(줄끝 보존·universal)이 FIFO 를 거부한다. 폴백 분기(dir_fd 미지원)도 같다."""
    monkeypatch.setattr(pm_import, "_DEST_FD_WALK_SUPPORTED", fd_walk)
    dest, rel = _fifo_target(pm_import, tmp_path)

    with _fails_instead_of_hanging():
        with pytest.raises(pm_import.UnsafeDestPathError, match="일반 파일이 아닙니다"):
            pm_import.read_dest_text_keeping_newlines(dest, rel)
        with pytest.raises(pm_import.UnsafeDestPathError, match="일반 파일이 아닙니다"):
            pm_import.read_dest_text(dest, rel)


@requires_fifo
def test_sensitivity_open_without_regular_guard_accepts_a_fifo(pm_import, tmp_path):
    """sensitivity — 가드를 끄면 같은 형상에서 FIFO fd 가 그대로 열린다(가드가 load-bearing).

    여기서는 `O_NONBLOCK` 을 손으로 얹어 확인한다. 그 플래그마저 없으면 이 열기는 반환하지 않으므로
    (블록) 관측 자체가 불가능하다 — 두 축(비-블로킹 열기 + 일반 파일 판정)이 함께 필요한 이유다."""
    dest, rel = _fifo_target(pm_import, tmp_path)

    with _fails_instead_of_hanging():
        fd = pm_import._open_dest_relative_nofollow(
            dest, rel, os.O_RDONLY | os.O_NONBLOCK)
    try:
        assert stat.S_ISFIFO(os.fstat(fd).st_mode), "픽스처 전제(대상이 FIFO)"
    finally:
        os.close(fd)


@requires_fifo
def test_content_write_refuses_a_fifo(pm_import, tmp_path):
    """쓰기 채널도 같다 — 비-블로킹 열기가 ENXIO 로 실패하고 거부 클래스로 승격된다."""
    dest, rel = _fifo_target(pm_import, tmp_path)

    with _fails_instead_of_hanging():
        with pytest.raises(pm_import.UnsafeDestPathError):
            pm_import.write_dest_text_keeping_newlines(dest, rel, "RENDERED\n")


@requires_fifo
def test_plan_preview_refuses_a_fifo_swapped_after_precheck(
        pm_import, tmp_path, monkeypatch):
    """계획 선검사 통과 뒤 FIFO 로 교체 → 계획 단계 fail-loud(복사 전 중단·멈추지 않음)."""
    dest = tmp_path / "inst"
    role = dest / ".project_manager" / "wiki" / "pm_role.md"
    role.parent.mkdir(parents=True)
    role.write_text("`/pm-bootstrap`\n", encoding="utf-8")
    original = pm_import._is_inplace_edit_candidate

    def swap_after_precheck(dest_root, rel):
        verdict = original(dest_root, rel)
        if verdict and Path(rel).name == "pm_role.md" and role.is_file():
            role.unlink()
            os.mkfifo(role)
        return verdict

    monkeypatch.setattr(pm_import, "_is_inplace_edit_candidate", swap_after_precheck)
    contexts = {".project_manager/wiki/pm_role.md": ("codex", "opencode")}

    with _fails_instead_of_hanging():
        with pytest.raises(pm_import.UnsafeDestPathError, match="안전하지 않"):
            pm_import._shared_notation_rerender_plan(dest, contexts, None, None)


@requires_fifo
def test_backup_refuses_a_fifo_swapped_after_precheck(pm_import, tmp_path, monkeypatch):
    """적용 단계(백업 채널)는 그 파일만 loud 제외 — 계획 단계와 달리 전체를 멈추지 않는다."""
    dest = tmp_path / "inst"
    role = dest / ".project_manager" / "wiki" / "pm_role.md"
    role.parent.mkdir(parents=True)
    role.write_text("사용자 내용\n", encoding="utf-8")
    backup_root = dest / pm_import.BACKUP_DIR_NAME / "2026-01-01"
    rel = Path(".project_manager/wiki/pm_role.md")
    original = pm_import._is_inplace_edit_candidate

    def swap_after_precheck(dest_root, checked):
        verdict = original(dest_root, checked)
        if verdict and role.is_file():
            role.unlink()
            os.mkfifo(role)
        return verdict

    monkeypatch.setattr(pm_import, "_is_inplace_edit_candidate", swap_after_precheck)

    with _fails_instead_of_hanging():
        outcome = pm_import._backup_before_inplace_edit(dest, [rel], backup_root, set())

    assert outcome.backed_up == [] and outcome.vanished == []
    assert outcome.refused == [".project_manager/wiki/pm_role.md"]
    assert not (backup_root / rel).exists(), "FIFO 를 백업 대상으로 삼음"


# ── dir_fd 미지원 폴백(주로 Windows)의 강제 실행 ────────────────────────────
# 그 분기는 Linux CI 에서 절대 안 돌아 죽은 코드가 되기 쉽다 — 지원 판정을 꺼서 **디렉토리 생성**
# 경로(`create_parents`·`create_leaf_dir`)와 루트 신원 대조까지 실제로 태운다.

@pytest.fixture
def _recheck_fallback(pm_import, monkeypatch):
    monkeypatch.setattr(pm_import, "_DEST_FD_WALK_SUPPORTED", False)


def test_recheck_fallback_creates_missing_backup_parents(
        pm_import, tmp_path, _recheck_fallback):
    """폴백 백업도 부재 중간 디렉토리를 만들고 원본 내용을 그대로 옮긴다(create_parents 분기)."""
    dest = tmp_path / "inst"
    rel = Path(".project_manager/wiki/pm_role.md")
    (dest / rel).parent.mkdir(parents=True)
    (dest / rel).write_text("사용자 내용\n", encoding="utf-8")
    backup_root = dest / pm_import.BACKUP_DIR_NAME / "2026-01-01"

    target = pm_import._backup_to_central_dir_nofollow(dest, rel, backup_root)

    assert target == backup_root / rel
    assert target.read_text(encoding="utf-8") == "사용자 내용\n"


@requires_symlink
def test_recheck_fallback_refuses_symlinked_backup_ancestor(
        pm_import, tmp_path, _recheck_fallback):
    """폴백에서도 백업 target 조상이 저장소 밖 지향 symlink 면 거부한다(밖에 내용 쓰기 0)."""
    dest = tmp_path / "inst"
    rel = Path(".project_manager/wiki/pm_role.md")
    (dest / rel).parent.mkdir(parents=True)
    (dest / rel).write_text("사용자 내용\n", encoding="utf-8")
    outside_dir = tmp_path / "outside_backups"
    outside_dir.mkdir()
    (dest / pm_import.BACKUP_DIR_NAME).symlink_to(outside_dir, target_is_directory=True)
    backup_root = dest / pm_import.BACKUP_DIR_NAME / "2026-01-01"

    with pytest.raises(pm_import.UnsafeDestPathError):
        pm_import._backup_to_central_dir_nofollow(dest, rel, backup_root)
    assert list(outside_dir.iterdir()) == [], "저장소 밖에 사용자 파일 내용을 씀"


def test_recheck_fallback_creates_directory_chain(pm_import, tmp_path, _recheck_fallback):
    """폴백 디렉토리 생성(`_ensure_dest_dir_nofollow`)도 체인을 만든다 — 복사 목적지 준비 경로."""
    dest = tmp_path / "inst"
    dest.mkdir()

    pm_import._ensure_dest_dir_nofollow(dest, Path(".project_manager/wiki/raw"))

    assert (dest / ".project_manager" / "wiki" / "raw").is_dir()
    pm_import._ensure_dest_dir_nofollow(dest, Path(".project_manager/wiki/raw"))  # 멱등.


@requires_symlink
def test_recheck_fallback_refuses_symlinked_directory_ancestor(
        pm_import, tmp_path, _recheck_fallback):
    dest = tmp_path / "inst"
    dest.mkdir()
    outside_dir = tmp_path / "outside_tree"
    outside_dir.mkdir()
    (dest / ".project_manager").symlink_to(outside_dir, target_is_directory=True)

    with pytest.raises(pm_import.UnsafeDestPathError):
        pm_import._ensure_dest_dir_nofollow(dest, Path(".project_manager/wiki/raw"))
    assert list(outside_dir.iterdir()) == [], "링크를 따라 저장소 밖에 디렉토리를 만듦"


@requires_symlink
def test_recheck_fallback_refuses_a_swapped_dest_root(pm_import, tmp_path, _recheck_fallback):
    """폴백에서도 루트 교체는 파일 단위 거부가 아니라 전체 중단 클래스다."""
    dest = tmp_path / "inst"
    rel = Path(".project_manager/wiki/pm_role.md")
    (dest / rel).parent.mkdir(parents=True)
    (dest / rel).write_text("`/pm-bootstrap`\n", encoding="utf-8")
    root_identity = pm_import.dest_root_identity(dest)
    decoy = tmp_path / "decoy"
    (decoy / rel).parent.mkdir(parents=True)
    (decoy / rel).write_text(_OUTSIDE_TEXT, encoding="utf-8")
    shutil.rmtree(dest)
    dest.symlink_to(decoy, target_is_directory=True)

    with pytest.raises(pm_import.DestRootSwappedError):
        pm_import.write_dest_text_keeping_newlines(
            dest, rel, "RENDERED\n", root_identity=root_identity)
    assert (decoy / rel).read_text(encoding="utf-8") == _OUTSIDE_TEXT


def test_recheck_fallback_ensure_dir_checks_root_identity(
        pm_import, tmp_path, _recheck_fallback):
    """폴백 디렉토리 생성도 고정한 루트 신원을 확인한다 — 교체된 트리에 체인을 만들지 않는다."""
    dest = tmp_path / "inst"
    dest.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    stale_identity = pm_import.dest_root_identity(other)

    with pytest.raises(pm_import.DestRootSwappedError):
        pm_import._ensure_dest_dir_nofollow(
            dest, Path(".project_manager/wiki"), root_identity=stale_identity)
    assert not (dest / ".project_manager").exists()


# ── 링크 백업·삭제도 부모 디렉토리 fd 안에서 (경로 재해소 0) ─────────────────
# 복사 대상이 **symlink** 면 링크 자체를 백업하고 걷어낸 뒤 일반 파일을 놓는다. 그 두 걸음이 경로로
# 재해소되면(옛 `copy2(follow_symlinks=False)` + `os.unlink(경로)`) 계획 뒤 부모가 저장소 밖 지향
# 링크로 바뀌었을 때 밖의 링크를 백업하고 **밖의 파일을 지운다**.

@requires_symlink
def test_symlink_backup_and_unlink_refuse_a_swapped_parent(pm_import, tmp_path, capsys):
    """부모가 저장소 밖 지향 링크로 교체된 뒤의 복사는 거부되고, 밖의 파일은 그대로 남는다."""
    dest = tmp_path / "inst"
    (dest / "adapter").mkdir(parents=True)
    outside_dir = tmp_path / "outside_tree"
    outside_dir.mkdir()
    outside_secret = tmp_path / "OUTSIDE_SECRET.md"
    outside_secret.write_text(_OUTSIDE_TEXT, encoding="utf-8")
    (outside_dir / "card.md").symlink_to(outside_secret)  # 밖 트리의 링크(백업/삭제 표적).
    src = tmp_path / "template_card.md"
    src.write_text("TEMPLATE\n", encoding="utf-8")
    rel = Path("adapter/card.md")
    (dest / rel).symlink_to(outside_secret)
    backup_root = dest / pm_import.BACKUP_DIR_NAME / "2026-01-01"
    action = pm_import.CopyAction(
        src, dest / rel, backup_root / rel, dest_root=dest)
    root_identity = pm_import.dest_root_identity(dest)
    # 계획 뒤 부모 교체 — 이제 `dest/adapter/card.md` 는 경로로 열면 **밖 트리**의 링크다.
    shutil.rmtree(dest / "adapter")
    (dest / "adapter").symlink_to(outside_dir, target_is_directory=True)
    assert (dest / rel).is_symlink(), "픽스처 전제(교체된 경로가 밖의 링크를 가리킨다)"

    with pytest.raises(pm_import.UnsafeDestPathError):
        action.run(root_identity=root_identity)

    assert (outside_dir / "card.md").is_symlink(), "밖 트리의 링크를 지웠다(경로 unlink 잔존)"
    assert outside_secret.read_text(encoding="utf-8") == _OUTSIDE_TEXT
    assert not (backup_root / rel).exists(), "교체된 경로를 백업 대상으로 삼음"


@requires_symlink
def test_symlink_backup_preserves_the_link_itself(pm_import, tmp_path):
    """정상 형상 회귀 — 링크 백업은 *링크 자체*를 보존하고(대상 파일 불변) 원본 자리는 일반 파일이 된다."""
    dest = tmp_path / "inst"
    (dest / "adapter").mkdir(parents=True)
    target = tmp_path / "user_target.md"
    target.write_text("사용자 대상\n", encoding="utf-8")
    src = tmp_path / "template_card.md"
    src.write_text("TEMPLATE\n", encoding="utf-8")
    rel = Path("adapter/card.md")
    (dest / rel).symlink_to(target)
    backup_root = dest / pm_import.BACKUP_DIR_NAME / "2026-01-01"

    pm_import.CopyAction(src, dest / rel, backup_root / rel, dest_root=dest).run(
        root_identity=pm_import.dest_root_identity(dest))

    backed_up = backup_root / rel
    assert backed_up.is_symlink(), "링크가 아니라 내용이 백업됐다(복원 불가)"
    assert os.readlink(backed_up) == str(target)
    assert target.read_text(encoding="utf-8") == "사용자 대상\n", "링크 대상이 변경됨"
    assert not (dest / rel).is_symlink()
    assert (dest / rel).read_text(encoding="utf-8") == "TEMPLATE\n"


# ── 루트 교체 계열은 형상과 무관하게 전체 중단 클래스 ────────────────────────
# 루트가 *교체*(다른 디렉토리)됐을 때만이 아니라 **삭제·깨진 링크·일반 파일**로 바뀌었을 때도
# 같은 클래스여야 한다 — UnsafeDestPathError·raw OSError 로 새면 파일 단위 핸들러가 흡수해 실행이
# 계속되고, 남은 단계가 교체된 트리에 쓴다.

@requires_symlink
@pytest.mark.parametrize("shape", ["deleted", "broken_link", "regular_file"])
def test_root_replacement_shapes_are_all_root_swap_class(pm_import, tmp_path, shape):
    dest = tmp_path / "inst"
    rel = Path(".project_manager/wiki/pm_role.md")
    (dest / rel).parent.mkdir(parents=True)
    (dest / rel).write_text("`/pm-bootstrap`\n", encoding="utf-8")
    root_identity = pm_import.dest_root_identity(dest)
    shutil.rmtree(dest)
    if shape == "broken_link":
        dest.symlink_to(tmp_path / "NOT_THERE", target_is_directory=True)
    elif shape == "regular_file":
        dest.write_text("루트 자리에 일반 파일\n", encoding="utf-8")

    with pytest.raises(pm_import.DestRootSwappedError):
        pm_import.read_dest_text_keeping_newlines(dest, rel, root_identity=root_identity)
    # 파일 단위 흡수 클래스와 섞이면 안 된다(핸들러가 삼켜 실행이 계속된다).
    assert not issubclass(pm_import.DestRootSwappedError, pm_import.UnsafeDestPathError)


def test_root_open_failure_without_identity_keeps_path_error_class(pm_import, tmp_path):
    """고정 신원이 없는 질의성 호출은 옛 경로 오류를 유지한다(회귀 보존·오버리치 방지)."""
    with pytest.raises(pm_import.UnsafeDestPathError):
        pm_import.read_dest_text_keeping_newlines(tmp_path / "absent", Path("a.md"))


# ── 적용 단계 삭제 경쟁도 loud 제외(무요약 축 폐쇄) ──────────────────────────

def test_substitute_channel_reports_read_time_deletion(
        pm_import, tmp_path, monkeypatch, capsys):
    """선검사↔읽기 사이 삭제도 요약에 실린다 — 일반 OSError 로 삼키면 그 축만 침묵이 된다."""
    dest = tmp_path / "inst"
    dest.mkdir()
    rel = Path("doc.md")
    (dest / rel).write_text("이름은 {{PROJECT_NAME}} 이다\n", encoding="utf-8")
    original = pm_import._is_inplace_edit_candidate

    def delete_after_precheck(dest_root, checked):
        verdict = original(dest_root, checked)
        path = Path(dest_root) / checked
        if verdict and path.is_file():
            path.unlink()  # 선검사 통과 직후 삭제(읽기가 ENOENT 로 실패한다).
        return verdict

    monkeypatch.setattr(pm_import, "_is_inplace_edit_candidate", delete_after_precheck)
    capsys.readouterr()

    changed = pm_import.substitute_placeholders(dest, {"{{PROJECT_NAME}}": "X"}, {rel})

    err = capsys.readouterr().err
    assert changed == 0
    assert "placeholder 치환" in err and "대상이 사라져" in err, err


@requires_symlink
def test_recheck_fallback_handles_symlink_backup_and_unlink(
        pm_import, tmp_path, _recheck_fallback):
    """dir_fd 미지원 폴백에서도 링크 백업·삭제가 성립한다(정상 형상 + 교체 거부).

    그 분기는 Linux CI 에서 안 돌아 죽은 코드가 되기 쉬우므로 지원 판정을 꺼서 강제 실행한다."""
    dest = tmp_path / "inst"
    (dest / "adapter").mkdir(parents=True)
    target = tmp_path / "user_target.md"
    target.write_text("사용자 대상\n", encoding="utf-8")
    src = tmp_path / "template_card.md"
    src.write_text("TEMPLATE\n", encoding="utf-8")
    rel = Path("adapter/card.md")
    (dest / rel).symlink_to(target)
    backup_root = dest / pm_import.BACKUP_DIR_NAME / "2026-01-01"

    pm_import.CopyAction(src, dest / rel, backup_root / rel, dest_root=dest).run(
        root_identity=pm_import.dest_root_identity(dest))

    assert (backup_root / rel).is_symlink()
    assert (dest / rel).read_text(encoding="utf-8") == "TEMPLATE\n"
    assert target.read_text(encoding="utf-8") == "사용자 대상\n"

    # 교체 축 — 조상이 저장소 밖 지향 링크면 폴백 재검사가 삭제를 거부한다.
    outside_dir = tmp_path / "outside_tree"
    outside_dir.mkdir()
    (outside_dir / "card.md").write_text("밖 파일\n", encoding="utf-8")
    shutil.rmtree(dest / "adapter")
    (dest / "adapter").symlink_to(outside_dir, target_is_directory=True)

    with pytest.raises(pm_import.UnsafeDestPathError):
        pm_import._unlink_dest_relative_nofollow(dest, rel)
    assert (outside_dir / "card.md").is_file(), "폴백이 저장소 밖 파일을 지웠다"


# ── 계획 상태 보존: 적용 시점 재관측 금지 ───────────────────────────────────
# 계획은 "신규(백업 없음)" 또는 "기존(백업 있음)" 을 관측으로 확정한다. 적용 시점에 다시 보면
# 그 사이의 생성·삭제가 조용히 다른 동작이 된다 — 새로 생긴 사용자 파일을 무백업으로 덮거나,
# 사용자가 지운 파일을 되살린다. 상태가 달라지면 **그 파일만** 건드리지 않고 loud 로 알린다.

def _plan_one(pm_import, dest: Path, src: Path, rel: Path, backup: Path | None):
    """단일 파일 plan(계획 시점 상태가 액션에 박힌다) — 적용 전 상태를 흔들어 창을 만든다."""
    return pm_import.plan_copy(
        [src.parent], dest, backup, "full",
        include_relpath=lambda candidate: candidate == rel)


def test_file_created_after_plan_is_not_overwritten_without_backup(
        pm_import, tmp_path, capsys):
    """계획 때 없던 파일이 그 사이 생기면 덮지 않는다 — 계획엔 그 파일의 백업이 아예 없다."""
    dest = tmp_path / "inst"
    dest.mkdir()
    template = tmp_path / "tree"
    template.mkdir()
    rel = Path("card.md")
    (template / rel).write_text("TEMPLATE\n", encoding="utf-8")
    backup_root = dest / pm_import.BACKUP_DIR_NAME / "2026-01-01"
    action = pm_import.CopyAction(
        template / rel, dest / rel, None, dest_root=dest)  # 계획: 신규(백업 없음).
    (dest / rel).write_text("사용자가 방금 만든 파일\n", encoding="utf-8")
    capsys.readouterr()

    outcome = pm_import.apply_copy_plan([action], dest)
    pm_import.report_copy_apply_anomalies(outcome)

    assert outcome.copied == [] and outcome.changed == [rel.as_posix()]
    assert (dest / rel).read_text(encoding="utf-8") == "사용자가 방금 만든 파일\n", \
        "계획에 없던 파일을 무백업으로 덮음"
    assert not backup_root.exists()
    assert "계획 뒤 대상 상태가 달라져" in capsys.readouterr().err


def test_file_deleted_after_plan_is_not_recreated(pm_import, tmp_path, capsys):
    """계획 뒤 사용자가 지운 파일을 되살리지 않는다(제자리 편집 원칙과 같은 결)."""
    dest = tmp_path / "inst"
    dest.mkdir()
    template = tmp_path / "tree"
    template.mkdir()
    rel = Path("card.md")
    (template / rel).write_text("TEMPLATE\n", encoding="utf-8")
    (dest / rel).write_text("기존 파일\n", encoding="utf-8")
    backup_root = dest / pm_import.BACKUP_DIR_NAME / "2026-01-01"
    action = pm_import.CopyAction(
        template / rel, dest / rel, backup_root / rel, dest_root=dest)  # 계획: 기존(백업).
    (dest / rel).unlink()
    capsys.readouterr()

    outcome = pm_import.apply_copy_plan([action], dest)
    pm_import.report_copy_apply_anomalies(outcome)

    assert outcome.copied == [] and outcome.changed == [rel.as_posix()]
    assert not (dest / rel).exists(), "사라진 대상을 복사가 되살림"
    assert "계획 뒤 대상 상태가 달라져" in capsys.readouterr().err


def test_copy_exclusion_keeps_following_channels_on_the_successful_set(
        pm_import, tmp_path, capsys):
    """제외된 파일은 후속 채널 범위 밖이다 — `copied` 는 성공분만 담는다(치환·fill·기록 정합)."""
    dest = tmp_path / "inst"
    dest.mkdir()
    template = tmp_path / "tree"
    template.mkdir()
    good, blocked = Path("good.md"), Path("blocked.md")
    (template / good).write_text("이름은 {{PROJECT_NAME}} 이다\n", encoding="utf-8")
    (template / blocked).write_text("이름은 {{PROJECT_NAME}} 이다\n", encoding="utf-8")
    actions = [
        pm_import.CopyAction(template / good, dest / good, None, dest_root=dest),
        pm_import.CopyAction(template / blocked, dest / blocked, None, dest_root=dest),
    ]
    (dest / blocked).write_text("계획 뒤 생긴 파일\n", encoding="utf-8")
    capsys.readouterr()

    outcome = pm_import.apply_copy_plan(actions, dest)
    changed = pm_import.substitute_placeholders(
        dest, {"{{PROJECT_NAME}}": "Scoped"}, set(outcome.copied))

    assert outcome.copied == [good]
    assert changed == 1, "제외분이 후속 채널 범위에 섞였다"
    assert (dest / blocked).read_text(encoding="utf-8") == "계획 뒤 생긴 파일\n"


def test_plan_state_mismatch_is_a_file_scoped_class(pm_import):
    """상태 변화는 파일 단위 제외 클래스다 — 루트 교체(전체 중단)와 섞이면 안 된다."""
    assert not issubclass(pm_import.PlanStateChangedError, pm_import.DestRootSwappedError)
    assert not issubclass(pm_import.PlanStateChangedError, pm_import.UnsafeDestPathError)


@requires_symlink
def test_playbook_stub_does_not_follow_a_swapped_ancestor(pm_import, tmp_path, capsys):
    """스텁 생성도 fd 규율을 탄다 — 복사 제외 뒤에도 실행이 계속되므로 이 지점이 창이 됐다."""
    dest = tmp_path / "inst"
    (dest / ".project_manager").mkdir(parents=True)
    outside = tmp_path / "outside_tree"
    outside.mkdir()
    (dest / ".project_manager" / "wiki").symlink_to(outside, target_is_directory=True)
    capsys.readouterr()

    status = pm_import.ensure_pm_playbook_local_stub(dest, None)

    assert status == "unsafe-skip"
    assert list(outside.iterdir()) == [], "저장소 밖에 스텁을 씀"
    assert "저장소 밖 쓰기를 피합니다" in capsys.readouterr().err


def test_model_todo_channel_reports_read_time_deletion(
        pm_import, tmp_path, monkeypatch, capsys):
    """모델 TODO 채널의 읽기-전 삭제도 요약에 실린다(6번 계약의 잔여 채널)."""
    dest = tmp_path / "inst"
    dest.mkdir()
    rel = Path("agent.md")
    (dest / rel).write_text(f'model: "{pm_import.OPENCODE_MODEL_TOKEN}"\n', encoding="utf-8")
    original_read = pm_import.read_dest_text

    def delete_then_read(dest_root, read_rel, **kwargs):
        (Path(dest_root) / read_rel).unlink()  # 열거 뒤·읽기 전 삭제 경쟁.
        return original_read(dest_root, read_rel, **kwargs)

    monkeypatch.setattr(pm_import, "read_dest_text", delete_then_read)
    capsys.readouterr()

    marked = pm_import._mark_model_todos(dest, {rel}, [])

    assert marked == []
    assert "모델 TODO 표시" in capsys.readouterr().err


# ── 적용 시점 생성/삭제 경쟁의 errno 정규화 ─────────────────────────────────
# 상태 확인과 실제 open 사이에도 창이 남는다: 신규 자리에 파일이 생기면 `O_EXCL` 이 EEXIST 를,
# 기존 자리에서 파일이 사라지면 `O_TRUNC` 가 ENOENT 를 던진다. raw 로 새면 적용 루프가 통째로
# 죽어 rc 정책(파일 단위 제외 + rc0 요약)이 깨진다.

def test_create_race_after_state_check_is_normalized(pm_import, tmp_path, monkeypatch, capsys):
    """상태 확인 통과 뒤 그 자리에 파일이 생겨도(EEXIST) 그 파일만 제외된다 — 루프 생존."""
    dest = tmp_path / "inst"
    dest.mkdir()
    template = tmp_path / "tree"
    template.mkdir()
    rel = Path("card.md")
    (template / rel).write_text("TEMPLATE\n", encoding="utf-8")
    survivor = Path("other.md")
    (template / survivor).write_text("OTHER\n", encoding="utf-8")
    actions = [
        pm_import.CopyAction(template / rel, dest / rel, None, dest_root=dest),
        pm_import.CopyAction(template / survivor, dest / survivor, None, dest_root=dest),
    ]
    original = pm_import._ensure_dest_dir_nofollow

    def create_race(dest_root, rel_dir, **kwargs):
        result = original(dest_root, rel_dir, **kwargs)
        target = Path(dest_root) / rel
        if not target.exists():
            target.write_text("경쟁 생성\n", encoding="utf-8")  # 상태 확인 뒤·open 전.
        return result

    monkeypatch.setattr(pm_import, "_ensure_dest_dir_nofollow", create_race)
    capsys.readouterr()

    outcome = pm_import.apply_copy_plan(actions, dest)
    pm_import.report_copy_apply_anomalies(outcome)

    assert outcome.changed == [rel.as_posix()]
    assert survivor in outcome.copied, "한 파일의 경쟁이 나머지 복사를 죽였다"
    assert (dest / rel).read_text(encoding="utf-8") == "경쟁 생성\n", "경쟁 생성분을 덮음"
    assert "계획 뒤 대상 상태가 달라져" in capsys.readouterr().err


def test_delete_race_after_state_check_is_normalized(pm_import, tmp_path, monkeypatch, capsys):
    """기존 자리에서 파일이 사라져도(ENOENT) 파일 단위 제외 — 되살리지 않는다."""
    dest = tmp_path / "inst"
    dest.mkdir()
    template = tmp_path / "tree"
    template.mkdir()
    rel = Path("card.md")
    (template / rel).write_text("TEMPLATE\n", encoding="utf-8")
    (dest / rel).write_text("기존\n", encoding="utf-8")
    action = pm_import.CopyAction(template / rel, dest / rel, None, dest_root=dest)
    original_open = pm_import._open_dest_relative_nofollow

    def delete_race(dest_root, open_rel, flags, *args, **kwargs):
        # 상태 확인 통과 뒤·leaf 열기 **직전** 삭제(가장 좁은 실 경쟁 지점).
        target = Path(dest_root) / open_rel
        if Path(open_rel) == rel and target.is_file():
            target.unlink()
        return original_open(dest_root, open_rel, flags, *args, **kwargs)

    monkeypatch.setattr(pm_import, "_open_dest_relative_nofollow", delete_race)
    capsys.readouterr()

    outcome = pm_import.apply_copy_plan([action], dest)
    pm_import.report_copy_apply_anomalies(outcome)

    assert outcome.copied == [] and outcome.changed == [rel.as_posix()]
    assert not (dest / rel).exists(), "사라진 대상을 복사가 되살림"
    assert "계획 뒤 대상 상태가 달라져" in capsys.readouterr().err


def test_missing_template_source_is_not_normalized_away(pm_import, tmp_path):
    """**소스** 부재는 경쟁이 아니라 진짜 누락이다 — 상태 변화 클래스로 삼키지 않는다."""
    dest = tmp_path / "inst"
    dest.mkdir()
    absent_src = tmp_path / "tree" / "gone.md"

    with pytest.raises(FileNotFoundError):
        pm_import._write_dest_file_from_source_nofollow(
            dest, Path("gone.md"), absent_src, overwrite=False)
    assert not isinstance(
        pm_import.PlanStateChangedError("x"), FileNotFoundError), "클래스 혼선"


def test_backup_and_write_are_bound_to_one_leaf_identity(
        pm_import, tmp_path, monkeypatch, capsys):
    """백업 뒤 같은 자리가 다른 파일로 바뀌어도 그 새 파일을 자르지 않는다(백업↔쓰기 창 폐쇄).

    옛 흐름은 백업과 쓰기가 leaf 를 각각 다시 열어, 그 사이 교체된 **백업 없는** 파일을 truncate
    했다. 이제 fd 하나가 계획이 본 inode 에 묶여 있어 그 창 자체가 없다 — 교체는 손상 0 이고,
    우리가 쓴 내용이 그 자리에 안 보이므로 제외로 **보고**된다(없는 성공을 말하지 않는다)."""
    dest = tmp_path / "inst"
    dest.mkdir()
    template = tmp_path / "tree"
    template.mkdir()
    rel = Path("card.md")
    (template / rel).write_text("TEMPLATE\n", encoding="utf-8")
    (dest / rel).write_text("사용자 원본\n", encoding="utf-8")
    backup_root = dest / pm_import.BACKUP_DIR_NAME / "2026-01-01"
    action = pm_import.CopyAction(
        template / rel, dest / rel, backup_root / rel, dest_root=dest)
    original_backup = pm_import._backup_open_fd_nofollow

    def swap_after_backup(dest_root, src_fd, target_base_rel, **kwargs):
        target = original_backup(dest_root, src_fd, target_base_rel, **kwargs)
        (Path(dest_root) / rel).unlink()  # 백업 직후 같은 자리를 **다른 파일**로 교체.
        (Path(dest_root) / rel).write_text("백업 안 된 새 파일\n", encoding="utf-8")
        return target

    monkeypatch.setattr(pm_import, "_backup_open_fd_nofollow", swap_after_backup)
    capsys.readouterr()

    outcome = pm_import.apply_copy_plan([action], dest)
    pm_import.report_copy_apply_anomalies(outcome)

    assert (dest / rel).read_text(encoding="utf-8") == "백업 안 된 새 파일\n", \
        "백업 없는 교체 파일을 덮어씀(백업↔쓰기 창)"
    assert (backup_root / rel).read_text(encoding="utf-8") == "사용자 원본\n"
    assert outcome.copied == [] and outcome.changed == [rel.as_posix()]
    assert "계획 뒤 대상 상태가 달라져" in capsys.readouterr().err


@requires_symlink
def test_symlink_backup_and_unlink_are_identity_bound(
        pm_import, tmp_path, monkeypatch, capsys):
    """링크 백업 뒤 그 자리가 다른 파일로 바뀌면 지우지 않는다(백업↔unlink 창).

    링크는 fd 로 붙들 수 없어 백업 시점 신원(lstat)을 재대조한다 — 백업한 그 링크가 아니면
    삭제도 쓰기도 하지 않는다(백업 없는 남의 파일 삭제 0)."""
    dest = tmp_path / "inst"
    dest.mkdir()
    template = tmp_path / "tree"
    template.mkdir()
    rel = Path("card.md")
    (template / rel).write_text("TEMPLATE\n", encoding="utf-8")
    link_target = tmp_path / "user_target.md"
    link_target.write_text("링크 대상\n", encoding="utf-8")
    (dest / rel).symlink_to(link_target)
    backup_root = dest / pm_import.BACKUP_DIR_NAME / "2026-01-01"
    action = pm_import.CopyAction(
        template / rel, dest / rel, backup_root / rel, dest_root=dest)
    original_backup = pm_import._backup_symlink_nofollow

    def swap_after_backup(dest_root, src_rel, target_base_rel, **kwargs):
        target = original_backup(dest_root, src_rel, target_base_rel, **kwargs)
        (Path(dest_root) / rel).unlink()  # 백업 직후 링크를 **일반 파일**로 교체.
        (Path(dest_root) / rel).write_text("백업 안 된 새 파일\n", encoding="utf-8")
        return target

    monkeypatch.setattr(pm_import, "_backup_symlink_nofollow", swap_after_backup)
    capsys.readouterr()

    outcome = pm_import.apply_copy_plan([action], dest)
    pm_import.report_copy_apply_anomalies(outcome)

    assert (dest / rel).read_text(encoding="utf-8") == "백업 안 된 새 파일\n", \
        "백업 없는 교체 파일을 지우거나 덮어씀(백업↔unlink 창)"
    assert outcome.copied == [] and outcome.changed == [rel.as_posix()]
    assert link_target.read_text(encoding="utf-8") == "링크 대상\n"
    assert "계획 뒤 대상 상태가 달라져" in capsys.readouterr().err


# ── 설치 기록은 실제로 선 어댑터만 ──────────────────────────────────────────

def test_receipt_skips_a_harness_whose_copies_were_all_excluded(
        pm_import, tmp_path, monkeypatch, capsys):
    """복사가 전부 제외돼 어댑터가 서지 않으면 기록에 올리지 않는다 — 기록이 유령을 만들지 않는다."""
    dest = tmp_path / "excluded_harness"
    assert pm_import.main(
        ["--new", str(dest), "--harness", "claude", "--fill", "manual",
         "--from", str(REPO), "--name", "Excluded"]) == 0
    before = (dest / pm_import.INSTALL_RECEIPT_RELPATH).read_text(encoding="utf-8")
    # add-harness 의 복사를 전부 제외시킨다(계획 뒤 상태 변화와 같은 결과).
    monkeypatch.setattr(
        pm_import, "apply_copy_plan",
        lambda plan, dest_root, root_identity=None: pm_import.CopyApplyOutcome(
            [], [], [action.dst.relative_to(dest_root).as_posix() for action in plan]))
    capsys.readouterr()

    pm_import.add_harness(dest, "codex", dry_run=False, source_root=REPO)

    err = capsys.readouterr().err
    assert "codex" in err and "설치 기록에 올리지 않습니다" in err, err
    assert (dest / pm_import.INSTALL_RECEIPT_RELPATH).read_text(encoding="utf-8") == before, \
        "선 적 없는 어댑터가 기록에 올랐다(유령 생성)"
    assert pm_import.installed_harnesses(dest, REPO) == ["claude"]


def test_receipt_keeps_preexisting_harness_even_if_files_are_gone(
        pm_import, tmp_path, capsys):
    """이미 기록된 하네스는 파일이 없어도 이번 실행이 철회하지 않는다(철회는 명시 편집 채널)."""
    dest = tmp_path / "keep_preexisting"
    assert pm_import.main(
        ["--new", str(dest), "--harness", "claude", "--fill", "manual",
         "--from", str(REPO), "--name", "Keep"]) == 0
    shutil.rmtree(dest / ".claude")
    (dest / "CLAUDE.md").unlink()
    capsys.readouterr()

    recordable, dropped = pm_import.established_harnesses(
        dest, ["claude", "codex"], pm_import.installed_harnesses(dest, REPO))

    assert recordable == ["claude"], "기존 기록분을 이번 실행이 철회했다"
    assert dropped == ["codex"]


def test_copied_scope_absence_is_loud_not_silent(pm_import, tmp_path, capsys):
    """복사분 채널의 **선검사 부재**도 요약에 실린다 — 방금 복사한 파일이 없다는 건 사고다.

    옛 형상은 "대상 아님" 과 "복사 뒤 삭제" 를 같은 조용한 skip 으로 접어, 삭제된 파일이 처리 대상
    에서 빠진 사실이 어디에도 남지 않았다(적용 단계의 유일한 신호가 stderr 요약인데)."""
    dest = tmp_path / "inst"
    dest.mkdir()
    present, gone = Path("present.md"), Path("gone.md")
    (dest / present).write_text("이름은 {{PROJECT_NAME}} 이다\n", encoding="utf-8")
    capsys.readouterr()

    changed = pm_import.substitute_placeholders(
        dest, {"{{PROJECT_NAME}}": "Loud"}, {present, gone})

    err = capsys.readouterr().err
    assert changed == 1
    assert "대상이 사라져" in err and "gone.md" in err, err
    assert "present.md" not in err


def test_copied_scope_shape_change_is_reported_as_swapped(pm_import, tmp_path, capsys):
    """실재하지만 편집 대상이 아닌 형상(디렉토리 등)은 교체 축으로 보고한다(부재와 구분)."""
    dest = tmp_path / "inst"
    dest.mkdir()
    rel = Path("became_dir.md")
    (dest / rel).mkdir()
    capsys.readouterr()

    marked = pm_import._mark_todos(dest, ["{{PROJECT_CONSTRAINTS}}"], {rel})

    err = capsys.readouterr().err
    assert marked == []
    assert "안전하게 열 수 없어" in err and "became_dir.md" in err, err


def test_rerender_backup_reports_absent_target(pm_import, tmp_path):
    """계획이 실재를 확인한 재렌더 대상이 사라지면 백업 단계가 loud 로 알린다(조용한 skip 0)."""
    dest = tmp_path / "inst"
    (dest / ".project_manager" / "wiki").mkdir(parents=True)
    backup_root = dest / pm_import.BACKUP_DIR_NAME / "2026-01-01"
    rel = Path(".project_manager/wiki/pm_role.md")  # 계획엔 있었으나 지금은 부재.

    outcome = pm_import._backup_before_inplace_edit(dest, [rel], backup_root, set())

    assert outcome.backed_up == [] and outcome.refused == []
    assert outcome.vanished == [rel.as_posix()], "부재가 조용히 접혔다"
