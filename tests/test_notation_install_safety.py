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

import datetime
import importlib.util
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

    done = pm_import._backup_before_inplace_edit(
        dest, [Path(".project_manager/wiki/pm_role.md")], backup_root, set())

    assert done == [".project_manager/wiki/pm_role.md"]
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
    done = pm_import._backup_before_inplace_edit(
        dest, targets, backup_root, {".project_manager/wiki/pm_role.md"})
    assert done == [] and not backup_root.exists()


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


def test_single_missing_sentinel_file_does_not_hide_a_real_install(pm_import, tmp_path):
    """판별자 하나가 빠져도 실 설치는 검출된다 — 거짓 음성(표기 유실)이 거짓 양성보다 나쁘다.

    claude 는 전용 증거가 `.claude/pm_orch_claude.py` 하나뿐이라, 옛 판정은 그 파일만 없으면
    실 PM 인스턴스를 미설치로 봤다(add-harness codex 가 pm_state.md 를 codex 단독 표기로
    재렌더 → 기존 claude 표기 유실)."""
    dest = _live_instance(pm_import, tmp_path / "missing_sentinel", "claude")
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

    (dest / ".claude" / "pm_orch_claude.py").unlink()

    assert pm_import.installed_harnesses(dest, REPO) == ["claude", "opencode"], \
        "전용 판별자 1개 소실로 공존 하네스가 미검출(표기 유실 재발)"


def test_shared_evidence_counts_as_install_conservatively(pm_import, tmp_path):
    """구조 증거 + 공유 PM 자산이면 설치로 본다 — 유실보다 소음을 택하는 비대칭 판단.

    opencode 설치 + 채택자 자작 `CLAUDE.md` 는 claude 도 독자로 세어 병기 표기가 하나 는다
    (소음). 그 반대(귀속으로 빼기)는 공존 인스턴스에서 표기 유실을 만든다."""
    dest = _live_instance(pm_import, tmp_path / "oc_own_claude_md", "opencode")
    (dest / "CLAUDE.md").write_text("# 우리 팀 claude 메모\n", encoding="utf-8")
    assert (dest / ".claude" / "skills" / "pm-bootstrap" / "SKILL.md").is_file()

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
