"""엔진 패키지 디렉터리 교체 — 계획·게시·실패 복구·완료 프로세스 인계.

엔진 패키지(`.project_manager/tools/pm_engine`)는 파일 단위로 덮지 않고, 같은 부모에 출하 목록만 담은
완성 대기 트리(stage)를 만들어 기존 패키지 → backup, stage → 패키지 두 rename 으로 게시한다. 이 파일의
8노드는 추가·변경·삭제(삭제만 있는 경우 포함)와 다음 계획 0, 경로 범위, dry-run 무쓰기, stage·첫
rename·게시·되돌리기·평면 묶음·정리 실패, 실제 두 프로세스의 락, 첫 디스크 변경 뒤 엔진 적재 0 과 실제
새 완료 프로세스를 실측한다. 실패 주입은 os.rename·shutil.copy2·fs 정리·락 seam 에만 한다.
"""
from __future__ import annotations

import errno
import importlib
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from _manager_home import complete_in_process
from pm_engine.app import engine_transfer
from pm_engine.app import update_plan
from pm_engine.app import update_render
from pm_engine.cli import update_cmd
from pm_engine.domain import manifest_rules
from pm_engine.infra import engine_transfer as package_transfer
from pm_engine.infra import fs
from pm_engine.infra import manifest_store
from pm_engine.infra import pm_paths
from test_fresh_adopter_e2e import _snapshot_tree

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
PACKAGE = manifest_rules._ENGINE_PACKAGE_REL
FLAT = ".project_manager/tools/zz_flat_engine.py"
STAGE_PREFIX = manifest_rules._ENGINE_TRANSFER_STAGE_PREFIX
BACKUP_PREFIX = manifest_rules._ENGINE_TRANSFER_BACKUP_PREFIX
LOCK_NAME = manifest_rules._ENGINE_TRANSFER_LOCK_NAME

# 기본 사례 — 추가 1(new.py)·변경 1(changed.py)·삭제 1(obsolete.py)과 평면 엔진 변경 1.
SOURCE_PACKAGE = {"keep.py": "# keep\n", "new.py": "# new\n", "changed.py": "# changed v2\n"}
DEST_PACKAGE = {"keep.py": "# keep\n", "changed.py": "# changed v1\n", "obsolete.py": "# obsolete\n"}


def _write(root: Path, files: dict) -> None:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8", newline="\n")


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                   encoding="utf-8", check=True)


def _source(root: Path, files: dict, manifest: list[str]) -> Path:
    """추적 checkout 인 합성 상류 — 출하 목록은 `git ls-files` 가 낸다."""
    _write(root, {**files, ".project_manager/engine.manifest": "\n".join(manifest) + "\n"})
    _git(root, "init", "-q")
    _git(root, "add", "-f", "-A")
    return root.resolve()


def _case(tmp_path: Path, *, source_package=None, dest_package=None, source_flat="# flat v2\n",
          dest_flat="# flat v1\n", manifest=None, extra_source=None, name="case"):
    """(dest, source) — dest 는 local.conf 의 upstream.path 로 source 를 가리키는 자기 갱신 트리."""
    base = tmp_path / name
    files = {f"{PACKAGE}/{rel}": text for rel, text in (source_package or SOURCE_PACKAGE).items()}
    files.update({FLAT: source_flat, "adapterdir/one.md": "# one\n", **(extra_source or {})})
    source = _source(base / "source", files, manifest or [PACKAGE, FLAT, "adapterdir"])
    dest = base / "dest"
    dest_files = {f"{PACKAGE}/{rel}": text for rel, text in (dest_package or DEST_PACKAGE).items()}
    if dest_flat is not None:
        dest_files[FLAT] = dest_flat
    dest_files[".project_manager/local.conf"] = f"upstream.path={source}\n"
    _write(dest, dest_files)
    return dest, source


def _package_tree(root: Path) -> dict[str, str]:
    return {rel: data.decode("utf-8") for rel, data in package_transfer._package_tree_state(root).items()}


def _transfer_leftovers(tools: Path) -> list[str]:
    """패키지 옆의 교체 산출물 이름 — 락은 지우지 않으므로 stage·backup 만 센다."""
    return sorted(path.name for path in tools.iterdir()
                  if path.name.startswith((STAGE_PREFIX, BACKUP_PREFIX)))


def _record_launch(monkeypatch) -> list:
    launched: list = []
    monkeypatch.setattr(engine_transfer, "_launch_completion",
                        lambda *args: launched.append(args) or 0)
    return launched


def _plan(dest: Path, source: Path):
    return update_plan._engine_code_plan(pm_paths.home_at(dest), dest, source)


def _fail_rename_on(monkeypatch, failing_calls: set[int], error: OSError) -> list:
    """os.rename 의 n번째 호출(1부터)을 실패시키고 호출 인자를 기록한다."""
    calls: list = []
    real_rename = os.rename

    def rename(src, dst):
        calls.append((Path(src), Path(dst)))
        if len(calls) in failing_calls:
            raise error
        real_rename(src, dst)

    monkeypatch.setattr(package_transfer.os, "rename", rename)
    return calls


def test_package_inventory_removes_destination_only_modules(tmp_path, monkeypatch):
    """패키지 계획은 출하 목록과 대상 바이트로 추가·변경·삭제를 내고, 교체 뒤 다음 계획은 0 이다.

    삭제만 있는 형상(source 와 바이트가 같은 keep.py + 대상 전용 obsolete.py)도 교체 행 하나다. 캐시
    디렉터리만 다르면 변화 0 이다. `@source` 자식 override·tracked-only 는 manifest seam 을 따르고,
    패키지 안 tracked 제외(삭제·symlink·gitlink)는 명시 실패지만 일반 경로의 경고는 그대로다. source
    누락은 rc 2, 빈 출하는 rc 1, 대상 자리가 파일이면 검증 실패다. 같은 source·dest 도 완성 stage 뒤
    게시한다."""
    dest, source = _case(tmp_path)
    (dest / PACKAGE / "__pycache__").mkdir()
    (dest / PACKAGE / "__pycache__" / "keep.cpython-311.pyc").write_bytes(b"cache")
    os.chmod(source / PACKAGE / "new.py", 0o755)
    home = pm_paths.home_at(dest)

    engine = _plan(dest, source)
    package = engine["package_plan"]
    assert engine["changes"][0] == (PACKAGE, source / PACKAGE, dest / PACKAGE, "replace-dir")
    assert [change[0] for change in engine["changes"]] == [PACKAGE, FLAT]
    assert (package.added, package.updated, package.deleted) == (
        ("new.py",), ("changed.py",), ("obsolete.py",))
    assert package_transfer.change_file_count(engine["changes"], package) == 4
    assert update_plan.drift_changes(home, dest) == [PACKAGE, FLAT]

    with package_transfer.engine_package_delivery(package):
        pass
    assert _package_tree(dest / PACKAGE) == SOURCE_PACKAGE
    assert not (dest / PACKAGE / "__pycache__").exists(), "이전 캐시가 새 패키지에 남았다"
    if os.name != "nt":
        assert stat.S_IMODE((dest / PACKAGE / "new.py").stat().st_mode) == 0o755
    assert _transfer_leftovers(dest / ".project_manager" / "tools") == []
    assert (dest / ".project_manager" / "tools" / LOCK_NAME).is_file()
    assert [change[0] for change in _plan(dest, source)["changes"]] == [FLAT]
    (dest / FLAT).write_text("# flat v2\n", encoding="utf-8", newline="\n")
    assert _plan(dest, source)["changes"] == []

    # 삭제만 있는 형상 — 옛 파일 단위 계획은 변경 0·누락 0 이었다.
    dest, source = _case(tmp_path, source_package={"keep.py": "# keep\n"},
                         dest_package={"keep.py": "# keep\n", "obsolete.py": "# obsolete\n"},
                         dest_flat="# flat v2\n", name="delete-only")
    engine = _plan(dest, source)
    assert [change[3] for change in engine["changes"]] == ["replace-dir"]
    assert (engine["package_plan"].added, engine["package_plan"].updated,
            engine["package_plan"].deleted) == ((), (), ("obsolete.py",))
    assert package_transfer.change_file_count(engine["changes"], engine["package_plan"]) == 1
    assert update_plan.drift_changes(pm_paths.home_at(dest), dest) == [PACKAGE]

    # 캐시 디렉터리만 다르면 변화가 아니다.
    dest, source = _case(tmp_path, source_package={"keep.py": "# keep\n"},
                         dest_package={"keep.py": "# keep\n", "__pycache__/keep.pyc": b"x",
                                       ".pytest_cache/v": "x"},
                         dest_flat="# flat v2\n", name="cache-only")
    engine = _plan(dest, source)
    assert engine["changes"] == [] and engine["package_plan"].deleted == ()

    # 대상 부재는 backup 없음(None), 빈 디렉터리는 기존 트리이므로 backup 경로를 돌려준다.
    files = (("keep.py", source / PACKAGE / "keep.py"),)
    absent = tmp_path / "absent" / ".project_manager" / "tools" / "pm_engine"
    planned = package_transfer.prepare_engine_package(source / PACKAGE, absent, files)
    assert planned.dest_fingerprint is None and planned.added == ("keep.py",)
    assert package_transfer.replace_engine_package(
        source / PACKAGE, absent, planned.shipping_files) is None
    empty = tmp_path / "empty" / ".project_manager" / "tools" / "pm_engine"
    empty.mkdir(parents=True)
    planned = package_transfer.prepare_engine_package(source / PACKAGE, empty, files)
    assert planned.dest_fingerprint == ()
    backup = package_transfer.replace_engine_package(source / PACKAGE, empty, planned.shipping_files)
    assert backup is not None and backup.is_dir() and backup.name.startswith(BACKUP_PREFIX)
    package_transfer._cleanup_package_tree(backup)
    assert not backup.exists() and _package_tree(empty) == {"keep.py": "# keep\n"}

    # @source 자식 override 는 가장 구체적인 manifest 행이 공급하고, 미추적 파일은 출하하지 않는다.
    dest, source = _case(
        tmp_path, name="override",
        manifest=[PACKAGE, f"{PACKAGE}/keep.py    @source=other/override.py", FLAT],
        extra_source={"other/override.py": "# override\n"})
    (source / PACKAGE / "untracked.py").write_text("# 미추적\n", encoding="utf-8")
    engine = _plan(dest, source)
    shipped = {item.relative: item.source for item in engine["package_plan"].shipping_files}
    assert shipped["keep.py"] == source / "other" / "override.py"
    assert "untracked.py" not in shipped and engine["missing"] == []

    # 패키지 안 tracked 제외는 불완전한 출하라 명시 실패다.
    dest, source = _case(tmp_path, name="tracked-deleted")
    (source / PACKAGE / "keep.py").unlink()
    with pytest.raises(package_transfer.PackageReplaceError) as refused:
        _plan(dest, source)
    assert refused.value.phase == "validation"
    assert isinstance(refused.value.original, manifest_rules.SkippedRepoShippingEntryWarning)
    dest, source = _case(tmp_path, name="tracked-gitlink")
    nested = source / PACKAGE / "nested"
    nested.mkdir()
    _git(nested, "init", "-q")
    _git(nested, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "commit", "-q", "--allow-empty", "-m", "nested")
    _git(source, "add", "-A")
    with pytest.raises(package_transfer.PackageReplaceError) as refused:
        _plan(dest, source)
    assert "gitlink" in str(refused.value)
    dest, source = _case(tmp_path, name="tracked-symlink")
    try:
        os.symlink("keep.py", source / PACKAGE / "link.py")
    except OSError:
        pass                        # 링크를 만들 수 없는 플랫폼은 이 형상이 생기지 않는다.
    else:
        _git(source, "add", "-A")
        with pytest.raises(package_transfer.PackageReplaceError) as refused:
            _plan(dest, source)
        assert "symlink" in str(refused.value)

    # 일반 manifest 경로의 tracked 제외는 종전대로 경고만 하고 나머지를 계획한다.
    dest, source = _case(tmp_path, name="general-warning",
                         extra_source={"adapterdir/gone.md": "# gone\n"})
    (source / "adapterdir" / "gone.md").unlink()
    manifest = manifest_store.read_manifest(source / ".project_manager" / "engine.manifest")
    with pytest.warns(manifest_rules.SkippedRepoShippingEntryWarning):
        changes, missing = update_plan.plan(pm_paths.home_at(dest), source, manifest, dest,
                                            render_enabled=False, conf={})
    assert "adapterdir/one.md" in [str(change[0]) for change in changes] and missing == []

    # source 누락은 rc 2, 추적 파일 없는 패키지는 빈 출하 rc 1 — 어느 쪽도 쓰지 않는다.
    dest, source = _case(tmp_path, name="missing-source")
    shutil.rmtree(source / PACKAGE)
    _git(source, "add", "-A")
    launched = _record_launch(monkeypatch)
    before = _snapshot_tree(dest)
    assert update_cmd.main(pm_paths.home_at(dest), ["--from", str(source)]) == 2
    assert launched == [] and _snapshot_tree(dest) == before
    (source / PACKAGE).mkdir()
    (source / PACKAGE / "scratch.py").write_text("# 미추적\n", encoding="utf-8")
    assert update_cmd.main(pm_paths.home_at(dest), ["--from", str(source)]) == 1
    assert launched == [] and _snapshot_tree(dest) == before

    # 대상 패키지 자리가 파일이면 계획 단계에서 거부한다.
    dest, source = _case(tmp_path, name="dest-file")
    shutil.rmtree(dest / PACKAGE)
    (dest / PACKAGE).write_text("not a package\n", encoding="utf-8")
    with pytest.raises(package_transfer.PackageReplaceError) as refused:
        _plan(dest, source)
    assert refused.value.phase == "validation"

    # 같은 source·dest — 출하 목록만 담은 stage 를 완성한 뒤 게시한다.
    same = tmp_path / "same" / ".project_manager" / "tools" / "pm_engine"
    _write(same, {"keep.py": "# keep\n", "obsolete.py": "# obsolete\n"})
    planned = package_transfer.prepare_engine_package(same, same, (("keep.py", same / "keep.py"),))
    assert planned.deleted == ("obsolete.py",)
    with package_transfer.engine_package_delivery(planned):
        pass
    assert _package_tree(same) == {"keep.py": "# keep\n"}


def test_package_child_scope_is_refused(tmp_path, monkeypatch, capsys):
    """`--paths` 는 자기 갱신에서 패키지 변경이 있으면 늘 거부하고, 내보내기는 패키지 전체 선택만 받는다.

    자식만 고르면 패키지 경로를 고르라고 거부하고 자동으로 넓히지 않는다. 패키지 밖만 고르면 종전 잔존
    거부다. 패키지 경로나 그 조상을 고르면 교체 행을 옮기되, 다른 평면 엔진 변경이 있으면 종전대로
    거부한다. `@source` 패키지는 source 좌표 선택도 같다. 계획 값 누락·행 불일치·경로 탈출·중복·겹침·
    링크·경로 종류는 쓰기 전에 거부한다."""
    dest, source = _case(tmp_path, source_package={"keep.py": "# keep\n"},
                         dest_package={"keep.py": "# keep\n", "obsolete.py": "# obsolete\n"},
                         dest_flat="# flat v2\n")
    launched = _record_launch(monkeypatch)
    before = _snapshot_tree(dest)
    for scope in (PACKAGE, f"{PACKAGE}/keep.py", "adapterdir"):
        assert update_cmd.main(pm_paths.home_at(dest), ["--paths", scope]) == 1, scope
        assert "부분 전파" in capsys.readouterr().err
    assert launched == [] and _snapshot_tree(dest) == before

    repo = tmp_path / "repo"
    target = repo / "templates" / "tgt"
    _write(target, {f"{PACKAGE}/keep.py": "# keep\n", f"{PACKAGE}/obsolete.py": "# obsolete\n",
                    FLAT: "# flat v2\n", ".project_manager/local.conf": f"upstream.path={source}\n"})
    repo_home = pm_paths.home_at(repo)
    target_before = _snapshot_tree(target)
    assert update_cmd.main(repo_home, ["--target", "tgt", "--paths", f"{PACKAGE}/keep.py"]) == 1
    assert "디렉터리 단위로만" in capsys.readouterr().err
    assert update_cmd.main(repo_home, ["--target", "tgt", "--paths", "adapterdir"]) == 1
    assert "부분 전파" in capsys.readouterr().err
    assert _snapshot_tree(target) == target_before, "거부인데 타깃을 바꿨다"

    assert update_cmd.main(repo_home, ["--target", "tgt", "--paths", PACKAGE, PACKAGE]) == 0
    assert _package_tree(target / PACKAGE) == {"keep.py": "# keep\n"}
    assert not (target / "adapterdir").exists(), "스코프 밖 파일이 착지했다"

    (target / PACKAGE / "obsolete.py").write_text("# obsolete\n", encoding="utf-8")
    (target / FLAT).write_text("# flat v1\n", encoding="utf-8")
    assert update_cmd.main(repo_home, ["--target", "tgt", "--paths", ".project_manager/tools"]) == 1
    assert FLAT in capsys.readouterr().err, "별도 평면 엔진 변경의 거부가 약해졌다"
    (target / FLAT).write_text("# flat v2\n", encoding="utf-8", newline="\n")
    assert update_cmd.main(repo_home, ["--target", "tgt", "--paths", ".project_manager/tools"]) == 0
    assert _package_tree(target / PACKAGE) == {"keep.py": "# keep\n"}

    # `@source` 패키지 — 상류 좌표(pkgsrc)로 골라도 전체 선택이다.
    remap_source = _source(tmp_path / "remap-source",
                           {"pkgsrc/keep.py": "# keep\n", FLAT: "# flat v2\n"},
                           [f"{PACKAGE}    @source=pkgsrc", FLAT])
    _write(repo / "templates" / "remap", {
        f"{PACKAGE}/keep.py": "# keep\n", f"{PACKAGE}/obsolete.py": "# obsolete\n",
        FLAT: "# flat v2\n", ".project_manager/local.conf": f"upstream.path={remap_source}\n"})
    assert update_cmd.main(repo_home, ["--target", "remap", "--paths", "pkgsrc/keep.py"]) == 1
    assert update_cmd.main(repo_home, ["--target", "remap", "--paths", "pkgsrc"]) == 0
    assert _package_tree(repo / "templates" / "remap" / PACKAGE) == {"keep.py": "# keep\n"}

    # 계획 값과 교체 행은 함께 다닌다 — 없거나 어긋나면 쓰기 전에 거부한다.
    dest, source = _case(tmp_path, name="metadata")
    engine = _plan(dest, source)
    row, package = engine["changes"][0], engine["package_plan"]
    before = _snapshot_tree(dest)
    for changes, plan_value in (([row], None), ([row, row], package),
                                ([(row[0], row[1], tmp_path / "elsewhere", row[3])], package)):
        with pytest.raises(package_transfer.PackageReplaceError) as refused:
            package_transfer.change_file_count(changes, plan_value)
        assert refused.value.phase == "validation"
    with pytest.raises(package_transfer.PackageReplaceError):
        update_render.apply(pm_paths.home_at(dest), engine["changes"], package_plan=None,
                            is_hook_set_path=lambda _rel: False)
    assert _snapshot_tree(dest) == before

    keep = source / PACKAGE / "keep.py"
    for files in ((("../escape.py", keep),), (("/abs.py", keep),), (("a\\b.py", keep),),
                  (("a.py", keep), ("a.py", keep)), (("a", keep), ("a/b.py", keep)),
                  (("dir.py", source / PACKAGE),), ()):
        with pytest.raises(package_transfer.PackageReplaceError) as refused:
            package_transfer.prepare_engine_package(source / PACKAGE, dest / PACKAGE, files)
        assert refused.value.phase == "validation", files
    with pytest.raises(package_transfer.PackageReplaceError, match="조상"):
        package_transfer.prepare_engine_package(source / PACKAGE, source / PACKAGE / "inner",
                                                (("keep.py", keep),))
    try:
        os.symlink(keep, source / PACKAGE / "linked.py")
    except OSError:
        pass                        # 링크를 만들 수 없는 플랫폼은 이 입력이 생기지 않는다.
    else:
        with pytest.raises(package_transfer.PackageReplaceError, match="일반 파일"):
            package_transfer.prepare_engine_package(
                source / PACKAGE, dest / PACKAGE, (("linked.py", source / PACKAGE / "linked.py"),))
    assert _snapshot_tree(dest) == before


def test_package_dry_run_writes_nothing(tmp_path, monkeypatch, capsys):
    """dry-run 은 `[replace-dir] 경로 (추가·변경·삭제)` 와 합산 파일 수만 보이고 아무것도 쓰지 않는다.

    자기 갱신·단일 타깃·전체 타깃 모두 source·dest·instance 바이트와 락·stage·backup·완료 프로세스가
    0 이다. 변화가 없으면 종전대로 완료 프로세스 미리보기 1회다. 삭제만 있는 실제 전달은 인계 1 이다.
    락 primitive 부재·획득/해제 실패·계획 뒤 source/dest 변화는 강행하지 않고 실패한다."""
    dest, source = _case(tmp_path)
    _write(dest, {".project_manager/wiki/instance.md": "# 인스턴스 상태\n"})
    launched = _record_launch(monkeypatch)
    before, source_before = _snapshot_tree(dest), _snapshot_tree(source)

    assert update_cmd.main(pm_paths.home_at(dest), ["--from", str(source), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert f"  [replace-dir] {PACKAGE} (추가 1·변경 1·삭제 1)" in out, out
    assert f"  [update] {FLAT}" in out and "엔진 코드 4 파일 변경 예정" in out, out
    assert launched == [] and _snapshot_tree(dest) == before and _snapshot_tree(source) == source_before
    assert not (dest / ".project_manager" / "tools" / LOCK_NAME).exists()
    assert _transfer_leftovers(dest / ".project_manager" / "tools") == []

    repo = tmp_path / "repo"
    for name in ("alpha", "beta"):
        _write(repo / "templates" / name, {
            **{f"{PACKAGE}/{rel}": text for rel, text in DEST_PACKAGE.items()},
            FLAT: "# flat v1\n", ".project_manager/local.conf": f"upstream.path={source}\n"})
    targets_before = _snapshot_tree(repo)
    assert update_cmd.main(pm_paths.home_at(repo), ["--target", "alpha", "--dry-run"]) == 0
    assert update_cmd.main(pm_paths.home_at(repo), ["--all-targets", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert out.count(f"  [replace-dir] {PACKAGE} (추가 1·변경 1·삭제 1)") == 3, out
    assert out.count("[dry-run] 5 파일 변경 예정") == 3, out          # 패키지 3 + 평면 1 + adapterdir 1
    assert _snapshot_tree(repo) == targets_before

    # 변화가 없으면 종전대로 완료 프로세스가 나머지 미리보기를 한다(인계 0).
    synced, synced_source = _case(tmp_path, source_package={"keep.py": "# keep\n"},
                                  dest_package={"keep.py": "# keep\n"}, dest_flat="# flat v2\n",
                                  name="synced")
    assert update_cmd.main(pm_paths.home_at(synced), ["--from", str(synced_source), "--dry-run"]) == 0
    assert launched == [(synced, synced_source, 0, ["--from", str(synced_source), "--dry-run"])]

    # 삭제만 있는 실제 전달은 인계 파일 수 1 이다.
    launched.clear()
    delete_only, delete_source = _case(tmp_path, source_package={"keep.py": "# keep\n"},
                                       dest_package={"keep.py": "# keep\n", "obsolete.py": "x\n"},
                                       dest_flat="# flat v2\n", name="delete-only")
    assert update_cmd.main(pm_paths.home_at(delete_only), ["--from", str(delete_source)]) == 0
    assert launched == [(delete_only, delete_source, 1, ["--from", str(delete_source)])]

    # 락 primitive 가 없으면 무락으로 진행하지 않는다.
    launched.clear()
    with monkeypatch.context() as patch:
        patch.setattr(fs, "exclusive_lock_supported", lambda: False)
        assert update_cmd.main(pm_paths.home_at(dest), ["--from", str(source)]) == 1
    assert "OS 배타 파일락이 없어" in capsys.readouterr().err
    assert launched == [] and _snapshot_tree(dest) == before

    def refuse_lock(_fd):
        raise OSError(errno.ENOLCK, "주입한 락 획득 실패")

    with monkeypatch.context() as patch:
        patch.setattr(fs, "acquire_exclusive", refuse_lock)
        assert update_cmd.main(pm_paths.home_at(dest), ["--from", str(source)]) == 1
    err = capsys.readouterr().err
    assert "엔진 패키지 교체 실패(lock)" in err and "주입한 락 획득 실패" in err, err
    assert launched == [] and _package_tree(dest / PACKAGE) == DEST_PACKAGE

    def refuse_release(_fd):
        raise OSError(errno.ENOLCK, "주입한 락 해제 실패")

    with monkeypatch.context() as patch:
        patch.setattr(fs, "release_exclusive", refuse_release)
        assert update_cmd.main(pm_paths.home_at(dest), ["--from", str(source)]) == 1
    err = capsys.readouterr().err
    assert "엔진 패키지 교체 실패(lock)" in err and "주입한 락 해제 실패" in err, err
    assert launched == [], "락 해제 실패인데 완료 프로세스를 띄웠다"
    assert _package_tree(dest / PACKAGE) == SOURCE_PACKAGE, "해제 실패를 기존 패키지 보존으로 오보"
    assert _transfer_leftovers(dest / ".project_manager" / "tools") == []

    # 계획 뒤 dest·source 가 바뀌면 락 안 재확인이 오래된 계획을 거부한다.
    stale_dest, stale_source = _case(tmp_path, name="stale")
    package = _plan(stale_dest, stale_source)["package_plan"]
    (stale_dest / PACKAGE / "keep.py").write_text("# 외부 편집\n", encoding="utf-8")
    with pytest.raises(package_transfer.PackageReplaceError) as refused:
        with package_transfer.engine_package_delivery(package):
            raise AssertionError("오래된 계획으로 게시했다")
    assert refused.value.phase == "stale-plan"
    (stale_dest / PACKAGE / "keep.py").write_text("# keep\n", encoding="utf-8", newline="\n")
    (stale_source / PACKAGE / "new.py").write_text("# 상류 편집\n", encoding="utf-8")
    with pytest.raises(package_transfer.PackageReplaceError) as refused:
        with package_transfer.engine_package_delivery(package):
            raise AssertionError("오래된 계획으로 게시했다")
    assert refused.value.phase == "stale-plan"
    assert _package_tree(stale_dest / PACKAGE) == DEST_PACKAGE


def test_package_stage_failure_preserves_destination(tmp_path, monkeypatch, capsys):
    """stage 생성·복사·읽기·누락·해시 어긋남 실패는 기존 패키지를 그대로 두고 첫 rename 도 하지 않는다.

    자기 stage 는 정리하고, 정리까지 실패하면 두 원인과 stage 경로를 함께 올린다. stage 는 출하 목록만
    담는다(source 디렉터리 통째 복사·미추적 파일 전달 없음). CLI 는 rc 1 이고 완료 프로세스를 띄우지
    않는다."""
    dest, source = _case(tmp_path)
    tools = dest / ".project_manager" / "tools"
    package = _plan(dest, source)["package_plan"]
    renames = _fail_rename_on(monkeypatch, set(), OSError("unused"))

    def failing_mkdtemp(**_kwargs):
        raise OSError(errno.ENOSPC, "주입한 stage 생성 실패")

    with monkeypatch.context() as patch:
        patch.setattr(package_transfer.tempfile, "mkdtemp", failing_mkdtemp)
        with pytest.raises(package_transfer.PackageReplaceError) as failed:
            package_transfer.replace_engine_package(source / PACKAGE, dest / PACKAGE,
                                                    package.shipping_files)
    assert failed.value.phase == "stage" and failed.value.stage is None

    real_copy2 = shutil.copy2
    copies: list = []

    def failing_copy2(src, dst, *args, **kwargs):
        copies.append(Path(dst))
        if len(copies) == 2:
            raise OSError(errno.EIO, "주입한 stage 복사 실패")
        return real_copy2(src, dst, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(package_transfer.shutil, "copy2", failing_copy2)
        with pytest.raises(package_transfer.PackageReplaceError) as failed:
            package_transfer.replace_engine_package(source / PACKAGE, dest / PACKAGE,
                                                    package.shipping_files)
    assert failed.value.phase == "stage" and failed.value.stage is None
    assert isinstance(failed.value.__cause__, OSError) and failed.value.errno == errno.EIO
    assert all(path.parent.name.startswith(STAGE_PREFIX) for path in copies)

    real_read = fs.read_bytes_shared

    def failing_read(path):
        if any(part.startswith(STAGE_PREFIX) for part in Path(path).parts):
            raise PermissionError(errno.EACCES, "주입한 stage 읽기 실패")
        return real_read(path)

    with monkeypatch.context() as patch:
        patch.setattr(fs, "read_bytes_shared", failing_read)
        with pytest.raises(package_transfer.PackageReplaceError) as failed:
            package_transfer.replace_engine_package(source / PACKAGE, dest / PACKAGE,
                                                    package.shipping_files)
    assert failed.value.phase == "stage"

    (source / PACKAGE / "new.py").write_text("# 계획 뒤 바뀜\n", encoding="utf-8")
    with pytest.raises(package_transfer.PackageReplaceError, match="계획한 바이트") as failed:
        package_transfer.replace_engine_package(source / PACKAGE, dest / PACKAGE,
                                                package.shipping_files)
    (source / PACKAGE / "new.py").unlink()
    with pytest.raises(package_transfer.PackageReplaceError) as failed:
        package_transfer.replace_engine_package(source / PACKAGE, dest / PACKAGE,
                                                package.shipping_files)
    assert isinstance(failed.value.original, FileNotFoundError)

    def failing_rmtree(path, **_kwargs):
        raise OSError(errno.EBUSY, f"주입한 stage 정리 실패: {path}")

    with monkeypatch.context() as patch:
        patch.setattr(fs, "force_rmtree", failing_rmtree)
        with pytest.raises(package_transfer.PackageReplaceError) as failed:
            package_transfer.replace_engine_package(source / PACKAGE, dest / PACKAGE,
                                                    package.shipping_files)
    assert isinstance(failed.value.original, FileNotFoundError)
    assert isinstance(failed.value.secondary, OSError) and failed.value.stage.is_dir()
    assert "주입한 stage 정리 실패" in str(failed.value)
    shutil.rmtree(failed.value.stage)

    assert renames == [], "stage 실패인데 rename 을 했다"
    assert _package_tree(dest / PACKAGE) == DEST_PACKAGE
    assert _transfer_leftovers(tools) == []

    # CLI — 복사 실패는 rc 1, 평면 파일·완료 프로세스 0, 미추적 파일은 stage 에 들어가지 않는다.
    dest, source = _case(tmp_path, name="cli")
    (source / PACKAGE / "scratch.py").write_text("# 미추적\n", encoding="utf-8")
    launched = _record_launch(monkeypatch)
    staged: list = []

    def recording_copy2(src, dst, *args, **kwargs):
        staged.append(Path(dst).name)
        if Path(dst).name == "new.py":
            raise OSError(errno.EIO, "주입한 stage 복사 실패")
        return real_copy2(src, dst, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(package_transfer.shutil, "copy2", recording_copy2)
        assert update_cmd.main(pm_paths.home_at(dest), ["--from", str(source)]) == 1
    assert "엔진 패키지 교체 실패(stage)" in capsys.readouterr().err
    assert launched == [] and "scratch.py" not in staged
    assert (dest / FLAT).read_text(encoding="utf-8") == "# flat v1\n"
    assert _package_tree(dest / PACKAGE) == DEST_PACKAGE


def test_package_first_rename_failure_preserves_destination(tmp_path, monkeypatch, capsys):
    """기존 패키지 → backup rename 이 실패하면(권한·사용 중·backup 자리 충돌) 기존 패키지를 그대로 둔다.

    게시·완료 프로세스는 0 이고 완성한 stage 는 정리한다. backup 자리는 덮지 않는다. Windows 에서는
    패키지 안 파일을 연 핸들이 실제 디렉터리 rename 을 막고, 핸들을 닫은 뒤 다시 하면 성공한다."""
    dest, source = _case(tmp_path)
    tools = dest / ".project_manager" / "tools"
    package = _plan(dest, source)["package_plan"]
    for error in (PermissionError(errno.EACCES, "주입한 권한 거부"),
                  OSError(errno.EBUSY, "주입한 사용 중")):
        with monkeypatch.context() as patch:
            calls: list = []
            real_rename = os.rename

            def rename(src, dst, _error=error, _calls=calls):
                _calls.append((Path(src), Path(dst)))
                if len(_calls) == 1:
                    raise _error
                real_rename(src, dst)

            patch.setattr(package_transfer.os, "rename", rename)
            with pytest.raises(package_transfer.PackageReplaceError) as failed:
                package_transfer.replace_engine_package(source / PACKAGE, dest / PACKAGE,
                                                        package.shipping_files)
        assert failed.value.phase == "first-rename" and failed.value.original is error
        assert calls[0][0] == dest / PACKAGE and calls[0][1].name.startswith(BACKUP_PREFIX)
        assert len(calls) == 1, "첫 rename 실패 뒤 게시를 시도했다"
        assert _package_tree(dest / PACKAGE) == DEST_PACKAGE and _transfer_leftovers(tools) == []

    fixed_stage = tools / f"{STAGE_PREFIX}fixed"
    occupied = tools / f"{BACKUP_PREFIX}fixed"
    occupied.mkdir()
    (occupied / "marker").write_text("앞 세대 복구 자료\n", encoding="utf-8")

    def fixed_mkdtemp(**_kwargs):
        fixed_stage.mkdir()
        return str(fixed_stage)

    with monkeypatch.context() as patch:
        patch.setattr(package_transfer.tempfile, "mkdtemp", fixed_mkdtemp)
        with pytest.raises(package_transfer.PackageReplaceError) as failed:
            package_transfer.replace_engine_package(source / PACKAGE, dest / PACKAGE,
                                                    package.shipping_files)
    assert failed.value.phase == "first-rename"
    assert isinstance(failed.value.original, FileExistsError)
    assert (occupied / "marker").read_text(encoding="utf-8") == "앞 세대 복구 자료\n"
    assert not fixed_stage.exists() and _package_tree(dest / PACKAGE) == DEST_PACKAGE
    shutil.rmtree(occupied)

    launched = _record_launch(monkeypatch)
    with monkeypatch.context() as patch:
        _fail_rename_on(patch, {1}, PermissionError(errno.EACCES, "주입한 권한 거부"))
        assert update_cmd.main(pm_paths.home_at(dest), ["--from", str(source)]) == 1
    assert "엔진 패키지 교체 실패(first-rename)" in capsys.readouterr().err
    assert launched == [] and (dest / FLAT).read_text(encoding="utf-8") == "# flat v1\n"

    if os.name == "nt":
        # 공유 삭제 없이 연 파일 핸들은 그 파일을 담은 디렉터리의 rename 을 막는다(native 실측 축).
        handle = open(dest / PACKAGE / "keep.py", "rb")
        try:
            with pytest.raises(package_transfer.PackageReplaceError) as failed:
                package_transfer.replace_engine_package(source / PACKAGE, dest / PACKAGE,
                                                        package.shipping_files)
            assert failed.value.phase == "first-rename"
            assert _package_tree(dest / PACKAGE) == DEST_PACKAGE
        finally:
            handle.close()
        backup = package_transfer.replace_engine_package(source / PACKAGE, dest / PACKAGE,
                                                         package.shipping_files)
        assert backup is not None and _package_tree(dest / PACKAGE) == SOURCE_PACKAGE


def test_package_publish_failure_restores_destination(tmp_path, monkeypatch, capsys):
    """stage → 패키지 rename(게시)이 실패하면 backup 을 되돌려 기존 바이트를 복원한다.

    기존 패키지가 없던 경우는 부재 그대로다(없는 backup 을 복구 대상으로 만들지 않는다). 원래 게시 오류가
    원인으로 남고, 평면 파일·완료 프로세스·성공 인계는 0 이다."""
    dest, source = _case(tmp_path)
    tools = dest / ".project_manager" / "tools"
    package = _plan(dest, source)["package_plan"]
    injected = OSError(errno.EXDEV, "주입한 게시 실패")
    with monkeypatch.context() as patch:
        calls = _fail_rename_on(patch, {2}, injected)
        with pytest.raises(package_transfer.PackageReplaceError) as failed:
            package_transfer.replace_engine_package(source / PACKAGE, dest / PACKAGE,
                                                    package.shipping_files)
    assert failed.value.phase == "publish" and failed.value.original is injected
    assert failed.value.__cause__ is injected
    assert [call[1] for call in calls] == [calls[0][1], dest / PACKAGE, dest / PACKAGE]
    assert calls[2][0] == calls[0][1], "backup 을 되돌리지 않았다"
    assert _package_tree(dest / PACKAGE) == DEST_PACKAGE and _transfer_leftovers(tools) == []

    absent = tmp_path / "absent" / ".project_manager" / "tools" / "pm_engine"
    with monkeypatch.context() as patch:
        calls = _fail_rename_on(patch, {1}, injected)
        with pytest.raises(package_transfer.PackageReplaceError) as failed:
            package_transfer.replace_engine_package(source / PACKAGE, absent, package.shipping_files)
    assert failed.value.phase == "publish" and failed.value.backup is None and len(calls) == 1
    assert not absent.exists() and _transfer_leftovers(absent.parent) == []

    launched = _record_launch(monkeypatch)
    with monkeypatch.context() as patch:
        _fail_rename_on(patch, {2}, injected)
        assert update_cmd.main(pm_paths.home_at(dest), ["--from", str(source)]) == 1
    assert "엔진 패키지 교체 실패(publish)" in capsys.readouterr().err
    assert launched == [] and (dest / FLAT).read_text(encoding="utf-8") == "# flat v1\n"
    assert _package_tree(dest / PACKAGE) == DEST_PACKAGE


_TWO_WRITER_SCRIPT = '''
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from pm_engine.infra import engine_transfer

role, source_dir, dest_dir, signals = sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4]), Path(sys.argv[5])


def wait(name):
    deadline = time.monotonic() + 60
    while not (signals / name).exists():
        if time.monotonic() > deadline:
            sys.exit(9)
        time.sleep(0.02)


files = tuple(sorted((name, source_dir / name) for name in os.listdir(source_dir)))
plan = engine_transfer.prepare_engine_package(source_dir, dest_dir, files)
(signals / f"{role}-planned").touch()
wait(f"go-{role}")
try:
    with engine_transfer.engine_package_delivery(plan):
        (signals / f"{role}-inside").touch()
        if role == "first":
            wait("release")
except engine_transfer.PackageReplaceError as exc:
    print(exc.phase)
    sys.exit(3)
print("published")
'''


def test_package_rollback_failure_keeps_recovery_paths(tmp_path, monkeypatch, capsys):
    """게시와 되돌리기가 모두 실패하면 두 원인과 실제 stage·backup 을 남기고 패키지 부재를 알린다.

    평면 묶음 실패는 게시한 패키지와 backup 을 남기고 완료 프로세스를 띄우지 않는다. backup 정리의
    권한 실패·반환만 성공한 정리·정리 뒤 lstat 접근 실패도 성공으로 바꾸지 않는다. 같은 대상을 향한 실제
    두 프로세스는 락에서 기다리고, 뒤의 프로세스는 오래된 계획으로 거부된다."""
    dest, source = _case(tmp_path)
    tools = dest / ".project_manager" / "tools"
    package = _plan(dest, source)["package_plan"]
    with monkeypatch.context() as patch:
        _fail_rename_on(patch, {2, 3}, OSError(errno.EIO, "주입한 rename 실패"))
        with pytest.raises(package_transfer.PackageReplaceError) as failed:
            package_transfer.replace_engine_package(source / PACKAGE, dest / PACKAGE,
                                                    package.shipping_files)
    error = failed.value
    assert error.phase == "rollback" and not (dest / PACKAGE).exists()
    assert isinstance(error.original, OSError) and isinstance(error.secondary, OSError)
    assert _package_tree(error.backup) == DEST_PACKAGE and _package_tree(error.stage) == SOURCE_PACKAGE
    assert str(error.stage) in str(error) and str(error.backup) in str(error)
    os.rename(error.backup, dest / PACKAGE)
    shutil.rmtree(error.stage)

    with pytest.raises(package_transfer.PackageReplaceError) as failed:
        with package_transfer.engine_package_delivery(package):
            raise OSError(errno.EIO, "주입한 평면 실패")
    assert failed.value.phase == "flat-batch"
    assert _package_tree(dest / PACKAGE) == SOURCE_PACKAGE
    assert _package_tree(failed.value.backup) == DEST_PACKAGE
    shutil.rmtree(dest / PACKAGE)
    os.rename(failed.value.backup, dest / PACKAGE)

    with pytest.raises(RuntimeError) as interrupted:
        with package_transfer.engine_package_delivery(package):
            raise RuntimeError("주입한 프로그래밍 오류")
    notes = "\n".join(getattr(interrupted.value, "__notes__", []))
    assert BACKUP_PREFIX in notes, "보존 경로 메모 없이 원래 예외를 올렸다"
    backup = next(path for path in tools.iterdir() if path.name.startswith(BACKUP_PREFIX))
    shutil.rmtree(dest / PACKAGE)
    os.rename(backup, dest / PACKAGE)

    # CLI — 평면 복사 실패는 rc 1, 게시된 패키지와 backup 을 남기고 완료 프로세스 0.
    launched = _record_launch(monkeypatch)
    real_copy2 = shutil.copy2

    def flat_failure(src, dst, *args, **kwargs):
        if Path(dst) == dest / FLAT:
            raise OSError(errno.EIO, "주입한 평면 실패")
        return real_copy2(src, dst, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(package_transfer.shutil, "copy2", flat_failure)
        assert update_cmd.main(pm_paths.home_at(dest), ["--from", str(source)]) == 1
    err = capsys.readouterr().err
    assert "엔진 패키지 교체 실패(flat-batch)" in err and "복구:" in err, err
    assert launched == [] and _package_tree(dest / PACKAGE) == SOURCE_PACKAGE
    (backup,) = [path for path in tools.iterdir() if path.name.startswith(BACKUP_PREFIX)]
    shutil.rmtree(dest / PACKAGE)
    os.rename(backup, dest / PACKAGE)

    # backup 정리 실패 세 형상 — 모두 cleanup 실패이고 backup 이 남는다.
    real_rmtree = fs.force_rmtree
    removed: list = []

    def permission_rmtree(path, **_kwargs):
        raise PermissionError(errno.EACCES, f"주입한 정리 거부: {path}")

    def returns_only(path, **_kwargs):
        return None

    def removes_then_lstat_fails(path, **kwargs):
        real_rmtree(path, **kwargs)
        removed.append(Path(path))

    real_lstat = Path.lstat

    def guarded_lstat(self):
        if removed and self == removed[-1]:
            raise PermissionError(errno.EACCES, "주입한 lstat 접근 실패")
        return real_lstat(self)

    for rmtree in (permission_rmtree, returns_only, removes_then_lstat_fails):
        cleanup_package = _plan(dest, source)["package_plan"]
        with monkeypatch.context() as patch:
            patch.setattr(fs, "force_rmtree", rmtree)
            patch.setattr(Path, "lstat", guarded_lstat)
            with pytest.raises(package_transfer.PackageReplaceError) as failed:
                with package_transfer.engine_package_delivery(cleanup_package):
                    pass
        assert failed.value.phase == "cleanup", rmtree.__name__
        assert _package_tree(dest / PACKAGE) == SOURCE_PACKAGE
        leftover = failed.value.backup
        if leftover.exists():
            shutil.rmtree(dest / PACKAGE)
            os.rename(leftover, dest / PACKAGE)
        else:
            _write(dest, {f"{PACKAGE}/{rel}": text for rel, text in DEST_PACKAGE.items()})
            for rel in set(SOURCE_PACKAGE) - set(DEST_PACKAGE):
                (dest / PACKAGE / rel).unlink()
        removed.clear()

    # 같은 대상을 향한 실제 두 프로세스 — 뒤의 프로세스는 락에서 기다렸다가 오래된 계획으로 거부된다.
    writers, writer_source = _case(tmp_path, name="writers")
    signals = tmp_path / "signals"
    signals.mkdir()
    script = tmp_path / "two_writers.py"
    script.write_text(_TWO_WRITER_SCRIPT, encoding="utf-8")
    command = [sys.executable, str(script), str(TOOLS)]
    processes = {
        role: subprocess.Popen([*command, role, str(writer_source / PACKAGE),
                                str(writers / PACKAGE), str(signals)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding="utf-8")
        for role in ("first", "second")
    }

    def wait_for(name):
        deadline = time.monotonic() + 60
        while not (signals / name).exists():
            assert time.monotonic() < deadline, name
            time.sleep(0.02)

    try:
        wait_for("first-planned")
        wait_for("second-planned")
        (signals / "go-first").touch()
        wait_for("first-inside")
        (signals / "go-second").touch()
        time.sleep(0.5)
        assert processes["second"].poll() is None, "두 번째 writer 가 락을 기다리지 않았다"
        assert not (signals / "second-inside").exists()
        (signals / "release").touch()
        first_out, first_err = processes["first"].communicate(timeout=60)
        second_out, second_err = processes["second"].communicate(timeout=60)
    finally:
        for process in processes.values():
            if process.poll() is None:
                process.kill()
    assert processes["first"].returncode == 0 and first_out.strip() == "published", first_err
    assert processes["second"].returncode == 3 and second_out.strip() == "stale-plan", second_err
    assert not (signals / "second-inside").exists()
    assert _package_tree(writers / PACKAGE) == SOURCE_PACKAGE
    assert _transfer_leftovers(writers / ".project_manager" / "tools") == []


_CHILD_UPDATER = '''import json
import os
import sys

import pm_engine

print(json.dumps({
    "pid": os.getpid(), "file": pm_engine.__file__, "generation": pm_engine.GENERATION,
    "executable": sys.executable, "argv": sys.argv[1:],
    "handoff": json.loads(os.environ["PM_UPDATE_COMPLETION"]),
}))
print("완료 단계 표준 출력")
print("완료 단계 표준 오류", file=sys.stderr)
sys.exit(int(os.environ.get("T1000_CHILD_RC", "0")))
'''


class _ImportWatch:
    """메타 경로 finder — 새로 적재하려는 pm_engine 모듈 이름을 기록만 한다."""

    def __init__(self) -> None:
        self.names: list[str] = []

    def find_spec(self, name, path=None, target=None):
        if name == "pm_engine" or name.startswith("pm_engine."):
            self.names.append(name)
        return None


def test_package_delivery_uses_preloaded_code_and_fresh_completion(tmp_path, monkeypatch, capsys):
    """전달과 일반 적용은 같은 교체 함수를 쓰고, 패키지 → 평면 → backup 정리 → 완료 프로세스 순이다.

    첫 디스크 변경 뒤 부모는 pm_engine 을 새로 적재·reload 하지 않는다. 완료 프로세스는 실제 새 프로세스로
    받은 갱신기를 같은 인터프리터·같은 인자로 띄우고, 새 세대 패키지를 적재하며 인계 값 세 키와 실제 파일
    합계(패키지 추가·변경·삭제 + 평면)를 받는다. 한글 표준 출력·오류와 0 아닌 rc, 기동 실패가 그대로
    올라온다. 완료 단계에 패키지 변경이 남으면 쓰기 전에 멈춘다."""
    manifest = [PACKAGE, ".project_manager/tools/pm_update.py"]
    source_files = {
        f"{PACKAGE}/__init__.py": 'GENERATION = "new"\n', f"{PACKAGE}/added.py": "# added\n",
        ".project_manager/tools/pm_update.py": _CHILD_UPDATER,
    }
    source = _source(tmp_path / "source", source_files, manifest)
    dest = tmp_path / "dest"
    _write(dest, {f"{PACKAGE}/__init__.py": 'GENERATION = "old"\n', f"{PACKAGE}/obsolete.py": "# old\n",
                  ".project_manager/tools/pm_update.py": "# old updater\n",
                  ".project_manager/local.conf": f"upstream.path={source}\n"})
    monkeypatch.delenv("T1000_CHILD_RC", raising=False)

    events: list[str] = []
    publishes: list[Path] = []
    watch = _ImportWatch()
    reloads: list = []
    real_replace = package_transfer.replace_engine_package
    real_cleanup = package_transfer._cleanup_package_tree
    real_launch = engine_transfer._launch_completion
    real_copy2 = shutil.copy2

    def replace(source_dir, dest_dir, shipping_files):
        events.append("publish")
        publishes.append(Path(dest_dir))
        if watch not in sys.meta_path:
            sys.meta_path.insert(0, watch)
        return real_replace(source_dir, dest_dir, shipping_files)

    def cleanup(path):
        events.append("cleanup")
        return real_cleanup(path)

    def launch(*args):
        events.append("launch")
        return real_launch(*args)

    def copy2(src, dst, *args, **kwargs):
        if Path(dst).name == "pm_update.py":
            events.append("flat")
        return real_copy2(src, dst, *args, **kwargs)

    monkeypatch.setattr(package_transfer, "replace_engine_package", replace)
    monkeypatch.setattr(package_transfer, "_cleanup_package_tree", cleanup)
    monkeypatch.setattr(engine_transfer, "_launch_completion", launch)
    monkeypatch.setattr(engine_transfer.shutil, "copy2", copy2)
    monkeypatch.setattr(importlib, "reload", lambda module: reloads.append(module))
    loaded_before = {name for name in sys.modules if name.startswith("pm_engine")}
    try:
        rc = update_cmd.main(pm_paths.home_at(dest), ["--from", str(source)])
    finally:
        if watch in sys.meta_path:
            sys.meta_path.remove(watch)
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    assert events == ["publish", "flat", "cleanup", "launch"], events
    assert watch.names == [] and reloads == [], (watch.names, reloads)
    assert {name for name in sys.modules if name.startswith("pm_engine")} == loaded_before

    report = json.loads(next(line for line in captured.out.splitlines() if line.startswith("{")))
    assert report["pid"] != os.getpid() and report["generation"] == "new"
    assert Path(report["file"]).resolve() == (dest / PACKAGE / "__init__.py").resolve()
    assert Path(report["file"]).resolve() != Path(sys.modules["pm_engine"].__file__).resolve()
    assert report["executable"] == sys.executable and report["argv"] == ["--from", str(source)]
    assert report["handoff"] == {"dest": str(dest), "source": str(source), "transferred": 4}
    assert "완료 단계 표준 출력" in captured.out and "완료 단계 표준 오류" in captured.err
    assert _package_tree(dest / PACKAGE) == {"__init__.py": 'GENERATION = "new"\n',
                                            "added.py": "# added\n"}

    # 0 아닌 rc 와 기동 실패는 그대로 올라온다(변경 0 이어도 완료 프로세스는 뜬다).
    monkeypatch.setenv("T1000_CHILD_RC", "7")
    assert update_cmd.main(pm_paths.home_at(dest), ["--from", str(source)]) == 7
    monkeypatch.delenv("T1000_CHILD_RC")

    real_popen = subprocess.Popen

    def spawn_failure(argv, *args, **kwargs):
        if str(argv[1]).endswith("pm_update.py"):     # 완료 프로세스 기동만 실패시킨다(git 조회는 그대로).
            raise FileNotFoundError(errno.ENOENT, "주입한 기동 실패")
        return real_popen(argv, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(engine_transfer.subprocess, "Popen", spawn_failure)
        with pytest.raises(FileNotFoundError, match="주입한 기동 실패"):
            update_cmd.main(pm_paths.home_at(dest), ["--from", str(source)])

    # 일반 적용(`--target`)도 같은 교체 함수를 거친다.
    repo = tmp_path / "repo"
    target = repo / "templates" / "tgt"
    _write(target, {f"{PACKAGE}/__init__.py": 'GENERATION = "old"\n',
                    f"{PACKAGE}/obsolete.py": "# old\n",
                    ".project_manager/local.conf": f"upstream.path={source}\n"})
    publishes.clear()
    assert update_cmd.main(pm_paths.home_at(repo), ["--target", "tgt"]) == 0
    assert publishes == [target / PACKAGE]
    assert "obsolete.py" not in _package_tree(target / PACKAGE)

    # 완료 단계에서 패키지 변경이 보이면 쓰기 전에 멈춘다(재귀 교체·자식 재기동 없음).
    (dest / PACKAGE / "stray.py").write_text("# 전달 뒤 생김\n", encoding="utf-8")
    monkeypatch.setenv(engine_transfer.COMPLETION_ENV, json.dumps(
        {"dest": str(dest), "source": str(source), "transferred": 0}))
    before = _snapshot_tree(dest)
    events.clear()
    with complete_in_process(monkeypatch):
        assert update_cmd.main(pm_paths.home_at(dest), ["--from", str(source)]) == 1
    assert "완료 단계에서 엔진 코드 변경 1건" in capsys.readouterr().err
    assert events == [] and _snapshot_tree(dest) == before
