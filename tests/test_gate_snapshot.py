"""격리 게이트 스냅샷의 내용 신선도와 대상 경계 회귀."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from _win_skip import (
    _can_symlink,
    git_symlink_supported,
    posix_filenames_supported,
    posix_mode_supported,
)


REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
MANIFESTS = {
    flavor: REPO / "templates" / flavor / ".project_manager" / "engine.manifest"
    for flavor in ("claude_code", "codex", "opencode")
}
TEMPLATE_TOOLS = {
    flavor: manifest.parent / "tools" / "gate_snapshot.py"
    for flavor, manifest in MANIFESTS.items()
}


# 해소 가능한 추가 리뷰어 대상 줄 — 대상 해소는 게이트 판정보다 앞이라, 이 절이 재는 축(스냅샷
# 마커 거부·장부 유지)을 태우려면 conf 가 그 세트를 담아야 한다.
_REVIEWER_TARGET_LINES = (
    "additional_reviewer.harness=codex\n"
    "additional_reviewer.model=gpt-5.6-sol\n"
)


def _load(name: str):
    sys.path.insert(0, str(TOOLS))
    try:
        spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(TOOLS))


@pytest.fixture
def snapshot():
    return _load("gate_snapshot")


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test User")
    (repo / "review").mkdir()
    (repo / "review" / "target.txt").write_text("committed\n", encoding="utf-8")
    (repo / "other.txt").write_text("committed-other\n", encoding="utf-8")
    _git(repo, "add", "review/target.txt", "other.txt")
    _git(repo, "commit", "-qm", "initial")
    return repo


def _index_bytes(repo: Path) -> bytes:
    raw_path = _git(repo, "rev-parse", "--git-path", "index").stdout.strip()
    index_path = Path(raw_path)
    if not index_path.is_absolute():
        index_path = repo / index_path
    return index_path.read_bytes()


def _manifest_engine_tools(path: Path) -> set[str]:
    return {
        line.split()[0]
        for raw in path.read_text(encoding="utf-8").splitlines()
        if (line := raw.strip())
        and not line.startswith("#")
        and line.split()[0].startswith(".project_manager/tools/")
        and line.split()[0].endswith(".py")
    }


def test_all_manifests_register_and_all_flavors_ship_gate_snapshot():
    """PM 전파 전에는 red여야 하며, 전파 뒤 실재 파일까지 출하됐음을 단언한다.

    이 도구 개별 단언이다 — "등록은 됐는데 파일이 안 실림" 클래스 전체는
    tests/test_manifest_shipped_paths.py 가 flavor manifest 전 경로로 일반화해 본다."""
    gate_tool = ".project_manager/tools/gate_snapshot.py"
    root_tools = _manifest_engine_tools(REPO / ".project_manager" / "engine.manifest")
    assert gate_tool in root_tools, "canonical manifest에 gate_snapshot.py가 미등록"
    unregistered = [
        flavor
        for flavor, manifest in MANIFESTS.items()
        if gate_tool not in _manifest_engine_tools(manifest)
    ]
    assert not unregistered, (
        f"flavor manifest에 gate_snapshot.py가 미등록: {unregistered}"
    )
    for flavor in MANIFESTS:
        shipped = TEMPLATE_TOOLS[flavor]
        assert shipped.is_file(), f"{flavor} flavor 도구 미출하: {shipped}"
        assert shipped.read_bytes() == (TOOLS / "gate_snapshot.py").read_bytes(), (
            f"{flavor} flavor 도구가 canonical과 다름: {shipped}"
        )


def test_staged_first_round_then_unstaged_second_round_is_rejected(snapshot, tmp_path):
    repo = _repo(tmp_path)
    target = repo / "review" / "target.txt"
    target.write_text("first-round\n", encoding="utf-8")
    _git(repo, "add", "review/target.txt")
    target.write_text("second-round\n", encoding="utf-8")
    output = tmp_path / "gate"
    index_before = _index_bytes(repo)

    with pytest.raises(snapshot.SnapshotError, match="working tree와 다릅니다"):
        snapshot.create_snapshot(repo, output, ["review/target.txt"])

    assert not output.exists()
    assert _index_bytes(repo) == index_before
    assert _git(repo, "show", ":review/target.txt").stdout == "first-round\n"
    assert target.read_text(encoding="utf-8") == "second-round\n"


def test_unrelated_live_edit_does_not_raise_or_enter_review_target(snapshot, tmp_path):
    repo = _repo(tmp_path)
    target = repo / "review" / "target.txt"
    target.write_text("ready-for-review\n", encoding="utf-8")
    _git(repo, "add", "review/target.txt")
    (repo / "other.txt").write_text("parallel-live-edit\n", encoding="utf-8")
    output = tmp_path / "gate"
    index_before = _index_bytes(repo)

    created, files = snapshot.create_snapshot(repo, output, ["review"])

    assert created == output.resolve()
    assert files == ("review/target.txt",)
    assert _index_bytes(repo) == index_before
    assert (output / "review" / "target.txt").read_text(encoding="utf-8") == \
        "ready-for-review\n"
    assert (output / "other.txt").read_text(encoding="utf-8") == "committed-other\n"


def test_successful_snapshot_records_local_fact_marker(snapshot, tmp_path):
    repo = _repo(tmp_path)
    output = tmp_path / "gate"

    created, _files = snapshot.create_snapshot(repo, output, ["review/target.txt"])

    marker = created / ".project_manager" / ".local" / "gate-snapshot.json"
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert set(payload) == {"created_at", "source_repo", "target_path"}
    assert datetime.fromisoformat(payload["created_at"]).utcoffset() is not None
    assert payload["source_repo"] == str(repo.resolve())
    assert payload["target_path"] == str(output.resolve())


def test_snapshot_marker_write_failure_rolls_back_registered_worktree(
    snapshot, tmp_path, monkeypatch,
):
    repo = _repo(tmp_path)
    output = tmp_path / "gate"
    monkeypatch.setattr(
        snapshot,
        "_write_snapshot_marker",
        lambda *args: (_ for _ in ()).throw(OSError("marker write denied")),
    )

    with pytest.raises(OSError, match="marker write denied"):
        snapshot.create_snapshot(repo, output, ["review/target.txt"])

    assert not output.exists()
    assert str(output) not in _git(repo, "worktree", "list", "--porcelain").stdout


@pytest.mark.skipif(
    not git_symlink_supported(), reason="git checkout symlink 왕복을 지원하지 않는 환경"
)
@pytest.mark.parametrize(
    ("symlink_parent", "outside_marker"),
    [
        (Path(".project_manager"), Path(".local/gate-snapshot.json")),
        (Path(".project_manager/.local"), Path("gate-snapshot.json")),
    ],
)
def test_snapshot_marker_rejects_symlink_parent_without_external_write(
    snapshot, tmp_path, symlink_parent, outside_marker,
):
    """추적된 마커 부모 symlink는 외부 파일을 건드리지 않고 생성 전체를 rollback한다."""
    repo = _repo(tmp_path)
    outside = tmp_path / "outside-marker-parent"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("unchanged\n", encoding="utf-8")
    link = repo / symlink_parent
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(outside, link, target_is_directory=True)
    _git(repo, "add", symlink_parent.as_posix())
    _git(repo, "commit", "-qm", "track marker parent symlink")
    output = tmp_path / "gate-with-marker-parent-link"

    with pytest.raises(snapshot.SnapshotError, match="symlink"):
        snapshot.create_snapshot(repo, output, ["review/target.txt"])

    assert sentinel.read_text(encoding="utf-8") == "unchanged\n"
    assert not (outside / outside_marker).exists()
    assert not output.exists()
    assert str(output) not in _git(repo, "worktree", "list", "--porcelain").stdout


def test_pm_home_work_snapshot_marker_blocks_round_in_pm_home_work(
    snapshot, tmp_path, monkeypatch, capsys,
):
    """bare + 관리 슬롯에서 `<PM 홈>/work/gate-*`를 만든 PM 37 체인을 rc1로 닫는다."""
    seed_root = tmp_path / "seed"
    seed_root.mkdir()
    seed = _repo(seed_root)
    pm_home = tmp_path / "pm-home"
    bare = pm_home / ".repos" / "product.git"
    bare.parent.mkdir(parents=True)
    subprocess.run(
        ["git", "clone", "--bare", "-q", str(seed), str(bare)],
        check=True,
        capture_output=True,
        text=True,
    )
    slot = pm_home / "work" / "product_1"
    slot.parent.mkdir()
    _git(bare, "worktree", "add", "-q", "-b", "review-slot", str(slot))

    tickets = pm_home / ".project_manager" / "wiki" / "tickets" / "open"
    tickets.mkdir(parents=True)
    (tickets / "T-0643-anchor.md").write_text(
        "---\nid: T-0643\ntitle: marker fixture\nstatus: open\n---\n",
        encoding="utf-8",
    )
    lease = pm_home / ".project_manager" / ".local" / "worktree-leases.json"
    lease.parent.mkdir(parents=True)
    # 소유자 해소는 정상이어야 이 절이 재는 축(마커 거부)이 관측된다. 장부 손상은 해소 자체가
    # 실패하는 별도 축이다.
    lease.write_text(
        json.dumps({"leases": [{"slot": "work/product_1", "state": "leased"}]}),
        encoding="utf-8",
    )

    target = slot / "review" / "target.txt"
    target.write_text("ready-for-review\n", encoding="utf-8")
    _git(slot, "add", "review/target.txt")
    output = pm_home / "work" / "gate-T0643"
    created, _files = snapshot.create_snapshot(
        slot, output, ["review/target.txt"],
    )

    # conf 소유자는 해소된 PM 홈이다 — 대상 해소가 마커 거부보다 먼저 걸리지 않게 거기 둔다.
    (pm_home / ".project_manager" / "local.conf").write_text(
        _REVIEWER_TARGET_LINES, encoding="utf-8")
    external = _load("additional_reviewer")
    external.REPO = created
    assert external.resolve_pm_home_for_repo(created) == pm_home.resolve()

    side_effects = []

    def _forbidden(name):
        def _fail(*args, **kwargs):
            side_effects.append(name)
            raise AssertionError(f"{name} must not run")
        return _fail

    monkeypatch.setattr(external, "_reserve_round_budget", _forbidden("round"))
    monkeypatch.setattr(external, "_reserve_output", _forbidden("raw"))
    monkeypatch.setattr(external, "run_review", _forbidden("spawn"))

    assert external.main([
        "--gate", "T-0643", "--paths", "review/target.txt",
        "--output-dir", str(tmp_path / "raw"),
    ]) == 1

    err = capsys.readouterr().err
    marker = created / ".project_manager" / ".local" / "gate-snapshot.json"
    assert "게이트 스냅샷 마커가 있는 앵커" in err
    assert str(marker) in err
    assert side_effects == []
    assert not (created / ".project_manager" / ".local" / "review_rounds.json").exists()


def test_parallel_wave_directory_scope_blocks_tracked_wip_with_branch_guidance(
    snapshot, tmp_path
):
    repo = _repo(tmp_path)
    tools = repo / ".project_manager" / "tools"
    tools.mkdir(parents=True)
    target = tools / "dev_a_target.py"
    target.write_text("committed-target\n", encoding="utf-8")
    parallel = tools / "dev_b_parallel.py"
    parallel.write_text("committed-parallel\n", encoding="utf-8")
    _git(repo, "add", ".project_manager/tools")
    _git(repo, "commit", "-qm", "parallel fixture")
    target.write_text("dev-a-ready\n", encoding="utf-8")
    _git(repo, "add", ".project_manager/tools/dev_a_target.py")
    parallel.write_text("dev-b-live-wip\n", encoding="utf-8")
    output = tmp_path / "gate"

    with pytest.raises(snapshot.SnapshotError) as exc:
        snapshot.create_snapshot(repo, output, [".project_manager/tools"])

    message = str(exc.value)
    assert "working tree와 다릅니다" in message
    assert "이 경로가 검토 대상이면 `git add`로 index를 갱신" in message
    assert "다른 dev의 WIP이면 `--paths`를 파일 단위로 좁히십시오" in message
    assert not output.exists()


def test_parallel_wave_file_scope_passes_without_copying_neighbor_wip(
    snapshot, tmp_path
):
    repo = _repo(tmp_path)
    tools = repo / ".project_manager" / "tools"
    tools.mkdir(parents=True)
    target = tools / "dev_a_target.py"
    target.write_text("committed-target\n", encoding="utf-8")
    parallel = tools / "dev_b_parallel.py"
    parallel.write_text("committed-parallel\n", encoding="utf-8")
    _git(repo, "add", ".project_manager/tools")
    _git(repo, "commit", "-qm", "parallel fixture")
    target.write_text("dev-a-ready\n", encoding="utf-8")
    _git(repo, "add", ".project_manager/tools/dev_a_target.py")
    parallel.write_text("dev-b-live-wip\n", encoding="utf-8")
    untracked = tools / "dev_c_new.py"
    untracked.write_text("dev-c-live-wip\n", encoding="utf-8")
    output = tmp_path / "gate"

    created, files = snapshot.create_snapshot(
        repo, output, [".project_manager/tools/dev_a_target.py"]
    )

    assert created == output.resolve()
    assert files == (".project_manager/tools/dev_a_target.py",)
    assert (output / ".project_manager" / "tools" / "dev_a_target.py").read_text(
        encoding="utf-8"
    ) == "dev-a-ready\n"
    assert (
        output / ".project_manager" / "tools" / "dev_b_parallel.py"
    ).read_text(encoding="utf-8") == "committed-parallel\n"
    assert not (
        output / ".project_manager" / "tools" / "dev_c_new.py"
    ).exists()


def test_missing_target_fails_before_snapshot_creation(snapshot, tmp_path):
    repo = _repo(tmp_path)
    output = tmp_path / "gate"

    with pytest.raises(snapshot.SnapshotError, match="비교할 Git 소유 파일이 없습니다"):
        snapshot.create_snapshot(repo, output, ["absent.txt"])

    assert not output.exists()


def test_unstaged_deleted_tracked_target_explains_how_to_stage(snapshot, tmp_path):
    repo = _repo(tmp_path)
    (repo / "review" / "target.txt").unlink()
    output = tmp_path / "gate"

    with pytest.raises(snapshot.SnapshotError) as exc:
        snapshot.create_snapshot(repo, output, ["review"])

    message = str(exc.value)
    assert "`git add -u -- review/target.txt`" in message
    assert "`git add -u`" not in message
    # 상한 이하면 절단 표시를 붙이지 않는다 (T-0544 ⑥ 음성 통제).
    assert "커맨드는 앞" not in message
    assert not output.exists()


def test_staged_deletion_is_absent_from_snapshot(snapshot, tmp_path):
    repo = _repo(tmp_path)
    target = repo / "review" / "target.txt"
    target.unlink()
    _git(repo, "add", "-u", "review/target.txt")
    output = tmp_path / "gate"

    created, files = snapshot.create_snapshot(repo, output, ["review"])

    assert created == output.resolve()
    assert files == ("review/target.txt",)
    assert not (output / "review" / "target.txt").exists()


def test_staged_deletion_with_working_file_left_is_rejected(snapshot, tmp_path):
    repo = _repo(tmp_path)
    _git(repo, "rm", "--cached", "review/target.txt")
    output = tmp_path / "gate"
    index_before = _index_bytes(repo)

    with pytest.raises(
        snapshot.SnapshotError, match="index에서 삭제됐지만 working tree에 남은"
    ):
        snapshot.create_snapshot(repo, output, ["review"])

    assert not output.exists()
    assert _index_bytes(repo) == index_before
    assert (repo / "review" / "target.txt").is_file()


def test_staged_untrack_plus_gitignore_treats_residue_as_intentional_deletion(
    snapshot, tmp_path
):
    repo = _repo(tmp_path)
    target = repo / "review" / "target.txt"
    _git(repo, "rm", "--cached", "review/target.txt")
    (repo / ".gitignore").write_text("/review/target.txt\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    output = tmp_path / "gate"

    created, files = snapshot.create_snapshot(repo, output, ["review/target.txt"])

    assert created == output.resolve()
    assert files == ("review/target.txt",)
    assert target.read_text(encoding="utf-8") == "committed\n"
    assert not (output / "review" / "target.txt").exists()


def test_unstaged_gitignore_does_not_exempt_staged_deletion_residue(
    snapshot, tmp_path
):
    repo = _repo(tmp_path)
    target = repo / "review" / "target.txt"
    _git(repo, "rm", "--cached", "review/target.txt")
    (repo / ".gitignore").write_text("/review/target.txt\n", encoding="utf-8")
    output = tmp_path / "gate"

    with pytest.raises(
        snapshot.SnapshotError, match="index에서 삭제됐지만 working tree에 남은"
    ):
        snapshot.create_snapshot(repo, output, ["review/target.txt"])

    assert not output.exists()
    assert target.read_text(encoding="utf-8") == "committed\n"


def test_staged_file_to_directory_transition_is_delete_and_add(snapshot, tmp_path):
    repo = _repo(tmp_path)
    old_file = repo / "review" / "target.txt"
    old_file.unlink()
    old_file.mkdir()
    child = old_file / "child.txt"
    child.write_text("directory-child\n", encoding="utf-8")
    _git(repo, "add", "-A", "review/target.txt")
    output = tmp_path / "gate"

    _, files = snapshot.create_snapshot(repo, output, ["review/target.txt"])

    assert files == ("review/target.txt", "review/target.txt/child.txt")
    assert not (output / "review" / "target.txt").is_file()
    assert (output / "review" / "target.txt" / "child.txt").read_text(
        encoding="utf-8"
    ) == "directory-child\n"


def test_staged_directory_to_file_transition_is_delete_and_add(snapshot, tmp_path):
    repo = _repo(tmp_path)
    target = repo / "review" / "target.txt"
    target.unlink()
    target.mkdir()
    child = target / "child.txt"
    child.write_text("committed-child\n", encoding="utf-8")
    _git(repo, "add", "-A", "review/target.txt")
    _git(repo, "commit", "-qm", "directory shape")
    child.unlink()
    target.rmdir()
    target.write_text("replacement-file\n", encoding="utf-8")
    _git(repo, "add", "-A", "review/target.txt")
    output = tmp_path / "gate"

    _, files = snapshot.create_snapshot(repo, output, ["review/target.txt"])

    assert files == ("review/target.txt", "review/target.txt/child.txt")
    assert (output / "review" / "target.txt").read_text(
        encoding="utf-8"
    ) == "replacement-file\n"
    assert not (output / "review" / "target.txt" / "child.txt").exists()


def test_untracked_new_file_explains_target_or_parallel_wip_branches(
    snapshot, tmp_path
):
    repo = _repo(tmp_path)
    (repo / "review" / "new.txt").write_text("new\n", encoding="utf-8")
    output = tmp_path / "gate"

    with pytest.raises(snapshot.SnapshotError) as exc:
        snapshot.create_snapshot(repo, output, ["review"])

    message = str(exc.value)
    assert "untracked 신규 파일" in message
    assert "이 경로가 검토 대상이면 `git add`로 index를 갱신" in message
    assert "다른 dev의 WIP이면 `--paths`를 파일 단위로 좁히십시오" in message
    assert not output.exists()


def test_file_created_during_snapshot_changes_selected_set(snapshot, tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    output = tmp_path / "gate"
    original = snapshot._checked_git_input
    injected = False

    def checked_git(root, input_text, *args):
        nonlocal injected
        result = original(root, input_text, *args)
        if not injected and args[:1] == ("checkout-index",):
            injected = True
            for base in (repo, output):
                (base / "review" / "late.txt").write_text("late\n", encoding="utf-8")
        return result

    monkeypatch.setattr(snapshot, "_checked_git_input", checked_git)

    with pytest.raises(snapshot.SnapshotError, match="파일 집합이 변경됐습니다"):
        snapshot.create_snapshot(repo, output, ["review"])

    assert not output.exists()
    assert (repo / "review" / "late.txt").is_file()


def test_file_created_after_first_reenumeration_is_caught_by_final_bookend(
    snapshot, tmp_path, monkeypatch
):
    repo = _repo(tmp_path)
    output = tmp_path / "gate"
    original = snapshot._signatures
    injected = False

    def signatures(root, files):
        nonlocal injected
        result = original(root, files)
        if not injected and Path(root) == output:
            injected = True
            (repo / "review" / "late.txt").write_text("late\n", encoding="utf-8")
        return result

    monkeypatch.setattr(snapshot, "_signatures", signatures)

    with pytest.raises(snapshot.SnapshotError, match="파일 집합이 변경됐습니다"):
        snapshot.create_snapshot(repo, output, ["review"])

    assert not output.exists()
    assert (repo / "review" / "late.txt").is_file()


def test_snapshot_only_file_is_caught_by_snapshot_file_set_bookend(
    snapshot, tmp_path, monkeypatch
):
    repo = _repo(tmp_path)
    output = tmp_path / "gate"
    original = snapshot._signatures
    injected = False

    def signatures(root, files):
        nonlocal injected
        result = original(root, files)
        if not injected and Path(root) == output:
            injected = True
            (output / "review" / "snapshot-only.txt").write_text(
                "snapshot-only\n", encoding="utf-8"
            )
        return result

    monkeypatch.setattr(snapshot, "_signatures", signatures)

    with pytest.raises(snapshot.SnapshotError, match="실 파일 집합"):
        snapshot.create_snapshot(repo, output, ["review"])

    assert not output.exists()


def test_snapshot_only_file_after_index_replication_is_caught_by_first_bookend(
    snapshot, tmp_path, monkeypatch
):
    repo = _repo(tmp_path)
    output = tmp_path / "gate"
    original_replicate = snapshot._replicate_index_and_checkout
    original_signatures = snapshot._signatures
    injected = False
    removed = False

    def replicate(root, destination):
        nonlocal injected
        original_replicate(root, destination)
        injected = True
        (output / "review" / "snapshot-only.txt").write_text(
            "snapshot-only\n", encoding="utf-8"
        )

    def signatures(root, files):
        nonlocal removed
        result = original_signatures(root, files)
        if injected and not removed and Path(root) == output:
            (output / "review" / "snapshot-only.txt").unlink()
            removed = True
        return result

    monkeypatch.setattr(snapshot, "_replicate_index_and_checkout", replicate)
    monkeypatch.setattr(snapshot, "_signatures", signatures)

    with pytest.raises(snapshot.SnapshotError, match="실 파일 집합"):
        snapshot.create_snapshot(repo, output, ["review"])

    assert injected
    assert not removed
    assert not output.exists()


def test_working_tree_stability_check_is_independently_sensitive(
    snapshot, tmp_path, monkeypatch
):
    repo = _repo(tmp_path)
    output = tmp_path / "gate"
    original = snapshot._checked_git_input
    injected = False

    def checked_git(root, input_text, *args):
        nonlocal injected
        result = original(root, input_text, *args)
        if not injected and args[:1] == ("checkout-index",):
            injected = True
            for base in (repo, output):
                (base / "review" / "target.txt").write_text(
                    "changed-during-snapshot\n", encoding="utf-8"
                )
        return result

    monkeypatch.setattr(snapshot, "_checked_git_input", checked_git)

    with pytest.raises(
        snapshot.SnapshotError, match="생성 중 검토 대상 working tree가 변경"
    ):
        snapshot.create_snapshot(repo, output, ["review"])

    assert not output.exists()


@pytest.mark.parametrize("change", [
    "blob",
    pytest.param(
        "mode",
        marks=pytest.mark.skipif(
            not posix_mode_supported(),
            reason="chmod 실행 비트 왕복을 지원하지 않는 filesystem",
        ),
    ),
])
def test_index_stage_entry_change_is_caught_even_when_file_set_is_stable(
    snapshot, tmp_path, monkeypatch, change
):
    repo = _repo(tmp_path)
    output = tmp_path / "gate"
    original = snapshot._checked_git_input
    injected = False

    def checked_git(root, input_text, *args):
        nonlocal injected
        result = original(root, input_text, *args)
        if not injected and args[:1] == ("checkout-index",):
            injected = True
            target = repo / "review" / "target.txt"
            if change == "blob":
                target.write_text("new-index-blob\n", encoding="utf-8")
            else:
                target.chmod(target.stat().st_mode | 0o111)
            _git(repo, "add", "review/target.txt")
        return result

    monkeypatch.setattr(snapshot, "_checked_git_input", checked_git)

    with pytest.raises(
        snapshot.SnapshotError, match="index stage 엔트리\\(mode/OID/stage\\)"
    ):
        snapshot.create_snapshot(repo, output, ["review/target.txt"])

    assert not output.exists()


def test_head_oid_change_is_caught_even_when_file_set_is_stable(
    snapshot, tmp_path, monkeypatch
):
    repo = _repo(tmp_path)
    output = tmp_path / "gate"
    original = snapshot._checked_git_input
    injected = False

    def checked_git(root, input_text, *args):
        nonlocal injected
        result = original(root, input_text, *args)
        if not injected and args[:1] == ("checkout-index",):
            injected = True
            _git(repo, "commit", "--allow-empty", "-qm", "move head only")
        return result

    monkeypatch.setattr(snapshot, "_checked_git_input", checked_git)

    with pytest.raises(snapshot.SnapshotError, match="HEAD OID가 변경됐습니다"):
        snapshot.create_snapshot(repo, output, ["review/target.txt"])

    assert not output.exists()


def test_snapshot_head_must_match_the_captured_source_basis(
    snapshot, tmp_path, monkeypatch
):
    repo = _repo(tmp_path)
    output = tmp_path / "gate"
    original = snapshot._checked_git_input
    injected = False

    def checked_git(root, input_text, *args):
        nonlocal injected
        result = original(root, input_text, *args)
        if not injected and args[:1] == ("checkout-index",):
            injected = True
            _git(output, "commit", "--allow-empty", "-qm", "move snapshot head")
        return result

    monkeypatch.setattr(snapshot, "_checked_git_input", checked_git)

    with pytest.raises(
        snapshot.SnapshotError, match="스냅샷의 HEAD OID가 생성 기준점과 다릅니다"
    ):
        snapshot.create_snapshot(repo, output, ["review/target.txt"])

    assert not output.exists()


def test_snapshot_index_entries_must_match_the_captured_source_basis(
    snapshot, tmp_path, monkeypatch
):
    repo = _repo(tmp_path)
    output = tmp_path / "gate"
    original = snapshot._selected_files
    injected = False

    def selected_files(root, paths):
        nonlocal injected
        if not injected and Path(root) == output:
            injected = True
            (output / "review" / "target.txt").write_text(
                "snapshot-index-diverged\n", encoding="utf-8"
            )
            _git(output, "add", "review/target.txt")
        return original(root, paths)

    monkeypatch.setattr(snapshot, "_selected_files", selected_files)

    with pytest.raises(
        snapshot.SnapshotError,
        match="스냅샷의 Git index stage 엔트리.*생성 기준점과 다릅니다",
    ):
        snapshot.create_snapshot(repo, output, ["review/target.txt"])

    assert not output.exists()


def test_final_working_tree_bookend_catches_same_path_content_change(
    snapshot, tmp_path, monkeypatch
):
    repo = _repo(tmp_path)
    output = tmp_path / "gate"
    original = snapshot._signatures
    root_calls = 0

    def signatures(root, files):
        nonlocal root_calls
        if Path(root) == repo:
            root_calls += 1
            if root_calls == 3:
                (repo / "review" / "target.txt").write_text(
                    "changed-at-final-bookend\n", encoding="utf-8"
                )
        return original(root, files)

    monkeypatch.setattr(snapshot, "_signatures", signatures)

    with pytest.raises(
        snapshot.SnapshotError, match="검증 중 검토 대상 working tree가 변경"
    ):
        snapshot.create_snapshot(repo, output, ["review/target.txt"])

    assert not output.exists()


def test_final_snapshot_bookend_catches_same_path_content_change(
    snapshot, tmp_path, monkeypatch
):
    repo = _repo(tmp_path)
    output = tmp_path / "gate"
    original = snapshot._signatures
    root_calls = 0

    def signatures(root, files):
        nonlocal root_calls
        result = original(root, files)
        if Path(root) == repo:
            root_calls += 1
            if root_calls == 3:
                (output / "review" / "target.txt").write_text(
                    "changed-snapshot-at-final-bookend\n", encoding="utf-8"
                )
        return result

    monkeypatch.setattr(snapshot, "_signatures", signatures)

    with pytest.raises(
        snapshot.SnapshotError, match="검증 중 격리 스냅샷이 변경"
    ):
        snapshot.create_snapshot(repo, output, ["review/target.txt"])

    assert not output.exists()


def test_active_submodule_source_is_unchanged_and_gitlink_stays_in_snapshot_index(
    snapshot, tmp_path
):
    submodule_source = tmp_path / "board-source"
    submodule_source.mkdir()
    _git(submodule_source, "init", "-q")
    _git(submodule_source, "config", "user.email", "test@example.invalid")
    _git(submodule_source, "config", "user.name", "Test User")
    (submodule_source / "ticket.txt").write_text("committed\n", encoding="utf-8")
    _git(submodule_source, "add", "ticket.txt")
    _git(submodule_source, "commit", "-qm", "board initial")

    repo = _repo(tmp_path)
    _git(
        repo,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "-q",
        str(submodule_source),
        "board",
    )
    _git(repo, "commit", "-qam", "add board submodule")
    board = repo / "board"
    (board / "ticket.txt").write_text("parallel-board-wip\n", encoding="utf-8")
    source_head = _git(board, "rev-parse", "HEAD").stdout
    source_status = _git(board, "status", "--porcelain=v1").stdout
    source_diff = _git(board, "diff", "--binary").stdout
    source_gitfile = (board / ".git").read_bytes()
    source_gitlink = _git(repo, "ls-files", "--stage", "--", "board").stdout
    output = tmp_path / "gate"

    snapshot.create_snapshot(repo, output, ["review/target.txt"])

    assert _git(board, "rev-parse", "HEAD").stdout == source_head
    assert _git(board, "status", "--porcelain=v1").stdout == source_status
    assert _git(board, "diff", "--binary").stdout == source_diff
    assert (board / ".git").read_bytes() == source_gitfile
    assert _git(output, "ls-files", "--stage", "--", "board").stdout == \
        source_gitlink
    assert not (output / "board").exists()


def test_unexpected_exception_rolls_back_registered_worktree(
    snapshot, tmp_path, monkeypatch
):
    repo = _repo(tmp_path)
    output = tmp_path / "gate"
    original = snapshot._selected_files
    calls = 0

    def selected_files(root, paths):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValueError("injected unexpected failure")
        return original(root, paths)

    monkeypatch.setattr(snapshot, "_selected_files", selected_files)

    with pytest.raises(ValueError, match="injected unexpected failure"):
        snapshot.create_snapshot(repo, output, ["review"])

    assert not output.exists()
    assert str(output) not in _git(repo, "worktree", "list", "--porcelain").stdout


def test_snapshot_creation_failure_is_loud(snapshot, tmp_path):
    repo = _repo(tmp_path)
    output = tmp_path / "missing-parent" / "gate"

    with pytest.raises(snapshot.SnapshotError, match="부모 디렉터리가 없습니다"):
        snapshot.create_snapshot(repo, output, ["review/target.txt"])


def test_output_inside_shared_repo_is_rejected_before_creating_files(snapshot, tmp_path):
    repo = _repo(tmp_path)
    output = repo / "gate-output"

    with pytest.raises(snapshot.SnapshotError, match="저장소가 추적하는 자리"):
        snapshot.create_snapshot(repo, output, ["review/target.txt"])

    assert not output.exists()
    assert "gate-output" not in _git(repo, "status", "--porcelain").stdout


def test_output_inside_a_gitignored_path_is_accepted(snapshot, tmp_path):
    """무시되는 자리는 저장소 안이어도 통과한다 — 오염 판정의 사실은 `git check-ignore` 다.

    막는 뜻은 "재는 대상 트리를 더럽히지 않는다" 이지 "프로젝트 밖" 이 아니다. 무시되는 자리는
    추적 대상이 아니라 오염시킬 표면이 없고, 그 사실을 `git status` 가 그대로 보여 준다.
    """
    repo = _repo(tmp_path)
    (repo / ".gitignore").write_text("scratch/\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-qm", "ignore scratch")
    scratch = repo / "scratch"
    scratch.mkdir()
    output = scratch / "gate"

    created, files = snapshot.create_snapshot(repo, output, ["review/target.txt"])

    assert created == output.resolve()
    assert files == ("review/target.txt",)
    assert snapshot.is_snapshot(output)
    assert _git(repo, "status", "--porcelain").stdout == ""


def test_existing_output_is_rejected_without_changing_it(snapshot, tmp_path):
    repo = _repo(tmp_path)
    output = tmp_path / "gate"
    output.mkdir()
    marker = output / "keep.txt"
    marker.write_text("keep\n", encoding="utf-8")

    with pytest.raises(snapshot.SnapshotError, match="이미 존재"):
        snapshot.create_snapshot(repo, output, ["review/target.txt"])

    assert marker.read_text(encoding="utf-8") == "keep\n"


def test_absolute_review_path_is_normalized_for_snapshot_enumeration(
    snapshot, tmp_path
):
    repo = _repo(tmp_path)
    output = tmp_path / "gate"

    created, files = snapshot.create_snapshot(repo, output, [str(repo / "review")])

    assert created == output.resolve()
    assert files == ("review/target.txt",)


@pytest.mark.skipif(not _can_symlink(), reason="symlink 을 만들 수 없는 환경")
def test_absolute_path_through_symlinked_repo_prefix_is_accepted(snapshot, tmp_path):
    """저장소 경로 prefix 에 심볼릭 링크가 껴도 절대경로 `--paths` 가 통과한다 (T-0544 ①).

    `_repo_root` 는 resolve() 결과라, 링크 경유 절대경로를 lexical 로만 보면 '저장소 밖'
    false-red 가 났다. prefix 해소로 같은 파일임을 인식해야 한다."""
    repo = _repo(tmp_path)
    link = tmp_path / "link"
    os.symlink(repo, link, target_is_directory=True)
    output = tmp_path / "gate"

    created, files = snapshot.create_snapshot(
        link, output, [str(link / "review" / "target.txt")]
    )

    assert created == output.resolve()
    assert files == ("review/target.txt",)


@pytest.mark.skipif(
    not git_symlink_supported(), reason="git checkout symlink 왕복을 지원하지 않는 환경"
)
def test_symlinked_repo_prefix_does_not_resolve_the_selector_leaf(snapshot, tmp_path):
    """prefix 만 해소하고 leaf 는 그대로 둔다 — 선택자가 링크 대상으로 바뀌면 안 된다 (T-0544 ①).

    leaf 까지 resolve 하면 저장소 안 심볼릭 링크 파일(`review/alias.txt`)을 지정한 선택자가
    링크 대상(`review/target.txt`)으로 바뀌어 검토 범위 자체가 달라진다."""
    repo = _repo(tmp_path)
    alias = repo / "review" / "alias.txt"
    os.symlink("target.txt", alias)
    _git(repo, "add", "review/alias.txt")
    _git(repo, "commit", "-qm", "symlink alias")
    link = tmp_path / "link"
    os.symlink(repo, link, target_is_directory=True)
    output = tmp_path / "gate"

    _, files = snapshot.create_snapshot(
        link, output, [str(link / "review" / "alias.txt")]
    )

    assert files == ("review/alias.txt",)
    assert (output / "review" / "alias.txt").is_symlink()
    assert os.readlink(output / "review" / "alias.txt") == "target.txt"


def test_output_inside_git_common_dir_is_rejected(snapshot, tmp_path):
    """공유 working tree 밖이라도 같은 저장소의 Git 공용 디렉터리면 거부한다 (T-0544 ②)."""
    repo = _repo(tmp_path)
    common_dir = tmp_path / "separate-git-dir"
    _git(repo, "init", "-q", f"--separate-git-dir={common_dir}")
    output = common_dir / "gate"

    with pytest.raises(snapshot.SnapshotError) as exc:
        snapshot.create_snapshot(repo, output, ["review/target.txt"])

    message = str(exc.value)
    assert "저장소가 추적하는 자리" in message
    assert "Git 공용 디렉터리" in message
    assert not output.exists()


def test_output_inside_other_registered_worktree_is_rejected(snapshot, tmp_path):
    """같은 저장소의 다른 등록 worktree 안에는 스냅샷을 만들지 않는다 (T-0544 ②).

    병렬 wave 에서 다른 작업 트리를 오염시키는 경로다 — 생성 전에 거부해야 한다."""
    repo = _repo(tmp_path)
    other = tmp_path / "other-worktree"
    _git(repo, "worktree", "add", "-q", "--detach", str(other))
    output = other / "gate"

    with pytest.raises(snapshot.SnapshotError) as exc:
        snapshot.create_snapshot(repo, output, ["review/target.txt"])

    message = str(exc.value)
    assert "저장소가 추적하는 자리" in message
    assert "다른 worktree" in message
    assert not output.exists()
    assert _git(other, "status", "--porcelain").stdout == ""
    assert str(output) not in _git(repo, "worktree", "list", "--porcelain").stdout


def test_output_inside_a_nested_registered_worktree_is_rejected_even_when_ignored(
    snapshot, tmp_path
):
    """무시되는 부모 아래에 선 등록 worktree 안도 거부한다 — 판정 주체는 오염될 그 트리다.

    무시 판정을 바깥 저장소에 물으면 목적지가 그 저장소의 규칙에 걸리기만 해도 Git 공용
    디렉터리·다른 등록 worktree 거부가 전부 우회된다. 엔진의 임시 루트가 무시되는 자리
    (`.project_manager/.local/tmp`)이고 격리 스냅샷 worktree 가 그 아래 서므로, 이 형상이
    바로 살아 있는 스냅샷 worktree 안에 또 하나를 세우는 경로다."""
    repo = _repo(tmp_path)
    (repo / ".gitignore").write_text("scratch/\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-qm", "ignore scratch")
    nested = repo / "scratch" / "worktree"
    _git(repo, "worktree", "add", "-q", "--detach", str(nested))
    output = nested / "gate"

    with pytest.raises(snapshot.SnapshotError, match="다른 worktree"):
        snapshot.create_snapshot(repo, output, ["review/target.txt"])

    assert not output.exists()
    assert _git(nested, "status", "--porcelain").stdout == ""
    assert str(output) not in _git(repo, "worktree", "list", "--porcelain").stdout


def test_stale_worktree_registration_prescribes_prune_instead_of_outside_repo(
    snapshot, tmp_path
):
    """등록만 남은 경로로 재실행하면 prune 처방을 낸다 (조치 불가능한 오진단 차단).

    `git worktree remove` 없이 디렉터리만 지운 뒤 같은 경로로 다시 도는 흐름에서
    '저장소 밖에 만들어야 한다'는 안내는 손쓸 데가 없다 — git 자체 진단보다도 후퇴한다."""
    repo = _repo(tmp_path)
    output = tmp_path / "gate"
    _git(repo, "worktree", "add", "-q", "--detach", str(output))
    shutil.rmtree(output)

    with pytest.raises(snapshot.SnapshotError) as exc:
        snapshot.create_snapshot(repo, output, ["review/target.txt"])

    message = str(exc.value)
    assert "worktree prune" in message
    assert "저장소 밖" not in message
    assert not output.exists()


def test_stale_worktree_registration_does_not_block_other_output_paths(
    snapshot, tmp_path
):
    """prune 분기가 무관한 출력 경로까지 막지 않음을 고정하는 경계 회귀(부수효과 없음)."""
    repo = _repo(tmp_path)
    stale = tmp_path / "stale-gate"
    _git(repo, "worktree", "add", "-q", "--detach", str(stale))
    shutil.rmtree(stale)
    output = tmp_path / "gate"

    created, files = snapshot.create_snapshot(repo, output, ["review/target.txt"])

    assert created == output.resolve()
    assert files == ("review/target.txt",)


@pytest.mark.skipif(
    not posix_filenames_supported(),
    reason="개행 포함 worktree 경로는 Windows 파일명 규칙에서 표현 불가",
)
def test_registered_worktree_paths_are_parsed_as_nul_records(snapshot, tmp_path):
    """개행·후행 공백이 든 worktree 경로도 거부 목록에 온전히 들어간다 (`-z` 파싱).

    줄 단위 porcelain은 그런 경로를 두 줄로 흘리거나 후행 공백을 잘라, 해당 worktree가
    거부 목록에서 통째로 빠지는 차단 우회 창이 된다."""
    repo = _repo(tmp_path)
    hostile = tmp_path / "other wt \nnewline"
    _git(repo, "worktree", "add", "-q", "--detach", str(hostile))
    output = hostile / "gate"

    with pytest.raises(snapshot.SnapshotError) as exc:
        snapshot.create_snapshot(repo, output, ["review/target.txt"])

    assert "다른 worktree" in str(exc.value)
    assert hostile in snapshot._registered_worktrees(
        snapshot._repo_root(repo)
    )
    assert not output.exists()


def test_out_of_scope_gitignore_staged_before_replication_follows_snapshot_basis(
    snapshot, tmp_path, monkeypatch
):
    """`--paths` 밖 .gitignore 가 캡처 뒤 staged 돼도 면제 판정은 스냅샷 기준을 따른다 (T-0544 ③).

    index 복제 직전에 staged 된 .gitignore 는 스냅샷에 그대로 실린다. 판정 근거(스냅샷)와
    리뷰어가 보는 산출물이 같은 트리라 어긋날 창이 없다 — 안정성 비교 대상 확장이 불필요함을
    보이는 실측 절반."""
    repo = _repo(tmp_path)
    target = repo / "review" / "target.txt"
    _git(repo, "rm", "--cached", "review/target.txt")
    # 캡처 시점에는 미-staged — 이 상태로 판정하면 '삭제 잔여'로 차단된다.
    (repo / ".gitignore").write_text("/review/target.txt\n", encoding="utf-8")
    output = tmp_path / "gate"
    original = snapshot._replicate_index_and_checkout
    injected = False

    def replicate(root, destination):
        nonlocal injected
        if not injected:
            injected = True
            _git(repo, "add", ".gitignore")
        original(root, destination)

    monkeypatch.setattr(snapshot, "_replicate_index_and_checkout", replicate)

    created, files = snapshot.create_snapshot(repo, output, ["review/target.txt"])

    assert injected
    assert created == output.resolve()
    assert files == ("review/target.txt",)
    # 판정 근거 = 스냅샷의 .gitignore. 그 스냅샷이 곧 리뷰 산출물이다.
    assert (output / ".gitignore").read_text(encoding="utf-8") == "/review/target.txt\n"
    assert not (output / "review" / "target.txt").exists()
    assert target.read_text(encoding="utf-8") == "committed\n"


def test_out_of_scope_gitignore_staged_after_replication_does_not_flip_verdict(
    snapshot, tmp_path, monkeypatch
):
    """index 복제 뒤 `--paths` 밖 .gitignore 가 바뀌어도 면제 판정은 흔들리지 않는다 (T-0544 ③).

    스냅샷의 .gitignore 는 복제 시점 단일 `ls-files --stage` 읽기로 확정되고 이후 불변이다.
    범위 밖 병렬 index 변경이 게이트 판정을 뒤집지 못함을 보이는 실측 나머지 절반."""
    repo = _repo(tmp_path)
    target = repo / "review" / "target.txt"
    _git(repo, "rm", "--cached", "review/target.txt")
    gitignore = repo / ".gitignore"
    gitignore.write_text("/review/target.txt\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    output = tmp_path / "gate"
    original = snapshot._replicate_index_and_checkout
    injected = False

    def replicate(root, destination):
        nonlocal injected
        original(root, destination)
        if not injected:
            injected = True
            gitignore.write_text("/unrelated\n", encoding="utf-8")
            _git(repo, "add", ".gitignore")

    monkeypatch.setattr(snapshot, "_replicate_index_and_checkout", replicate)

    created, files = snapshot.create_snapshot(repo, output, ["review/target.txt"])

    assert injected
    assert created == output.resolve()
    assert files == ("review/target.txt",)
    assert (output / ".gitignore").read_text(encoding="utf-8") == "/review/target.txt\n"
    assert gitignore.read_text(encoding="utf-8") == "/unrelated\n"
    assert not (output / "review" / "target.txt").exists()
    assert target.read_text(encoding="utf-8") == "committed\n"


def test_stage_prescription_marks_truncated_command(snapshot, tmp_path):
    """처방 커맨드가 상한에서 잘리면 절단 사실과 전체 개수를 병기한다 (T-0544 ⑥).

    표시가 없으면 커맨드를 그대로 복사해 실행한 사람이 나머지도 stage 됐다고 오해한다."""
    repo = _repo(tmp_path)
    bulk = [f"review/bulk{index:02d}.txt" for index in range(10)]
    for relative in bulk:
        (repo / relative).write_text("committed\n", encoding="utf-8")
    _git(repo, "add", "review")
    _git(repo, "commit", "-qm", "bulk fixture")
    (repo / "review" / "target.txt").unlink()
    for relative in bulk:
        (repo / relative).unlink()
    output = tmp_path / "gate"

    with pytest.raises(snapshot.SnapshotError) as exc:
        snapshot.create_snapshot(repo, output, ["review"])

    message = str(exc.value)
    shown = " ".join(bulk[:8])
    assert f"`git add -u -- {shown}`" in message
    assert "커맨드는 앞 8개만 표시 · 전체 11개" in message
    assert "review/target.txt" not in message.split("`git add -u -- ")[1].split("`")[0]
    assert not output.exists()


def test_eol_attribute_uses_git_normalized_blob_identity(snapshot, tmp_path):
    repo = _repo(tmp_path)
    attributes = repo / ".gitattributes"
    command = repo / "review" / "build.cmd"
    attributes.write_text("*.cmd text eol=crlf\n", encoding="utf-8")
    command.write_bytes(b"echo committed\n")
    _git(repo, "add", ".gitattributes", "review/build.cmd")
    _git(repo, "commit", "-qm", "eol fixture")
    command.write_bytes(b"echo staged\n")
    _git(repo, "add", "review/build.cmd")
    output = tmp_path / "gate"

    created, files = snapshot.create_snapshot(repo, output, ["review/build.cmd"])

    assert created == output.resolve()
    assert files == ("review/build.cmd",)
    assert _git(repo, "diff", "--", "review/build.cmd").stdout == ""
    assert command.read_bytes() == b"echo staged\n"
    assert (output / "review" / "build.cmd").read_bytes() == b"echo staged\r\n"


def test_cli_requires_explicit_review_paths(snapshot, tmp_path):
    repo = _repo(tmp_path)
    with pytest.raises(SystemExit) as exc:
        snapshot.main(
            ["--repo", str(repo), "--output", str(tmp_path / "gate")]
        )
    assert exc.value.code == 2


def test_cli_requires_explicit_repo_even_when_cwd_is_a_repo(
    snapshot, tmp_path, monkeypatch
):
    repo = _repo(tmp_path)
    monkeypatch.chdir(repo)

    with pytest.raises(SystemExit) as exc:
        snapshot.main(
            [
                "--output",
                str(tmp_path / "gate"),
                "--paths",
                "review/target.txt",
            ]
        )
    assert exc.value.code == 2


def test_module_and_cli_help_require_file_scope_for_parallel_waves(snapshot):
    assert "병렬 wave에서는" in snapshot.__doc__
    assert "파일 단위로 지정" in snapshot.__doc__
    paths_action = next(
        action for action in snapshot.build_parser()._actions if action.dest == "paths"
    )
    assert paths_action.help == (
        "검토 대상 파일 또는 디렉터리(병렬 wave에서는 파일 단위로 지정)"
    )


def test_additional_reviewer_head_diff_includes_unstaged_selected_path(tmp_path):
    external = _load("additional_reviewer")
    repo = _repo(tmp_path)
    target = repo / "review" / "target.txt"
    target.write_text("working-only\n", encoding="utf-8")
    external.REPO = repo

    diff = external.extract_diff("HEAD", ["review/target.txt"])

    assert "+working-only" in diff
    assert "review/target.txt" in diff


def test_snapshot_index_exposes_new_modified_deleted_and_renamed_files(
    snapshot, tmp_path
):
    external = _load("additional_reviewer")
    repo = _repo(tmp_path)
    deleted = repo / "review" / "deleted.txt"
    renamed = repo / "review" / "rename-source.txt"
    deleted.write_text("deleted-body\n", encoding="utf-8")
    renamed.write_text("rename-body\n", encoding="utf-8")
    _git(repo, "add", "review")
    _git(repo, "commit", "-qm", "review fixtures")

    (repo / "review" / "target.txt").write_text("modified-body\n", encoding="utf-8")
    deleted.unlink()
    _git(repo, "mv", "review/rename-source.txt", "review/rename-destination.txt")
    (repo / "review" / "new.txt").write_text("new-body\n", encoding="utf-8")
    _git(repo, "add", "-A", "review")
    index_before = _index_bytes(repo)
    output = tmp_path / "gate"

    _, files = snapshot.create_snapshot(repo, output, ["review"])

    assert _index_bytes(repo) == index_before
    tracked = set(_git(output, "ls-files").stdout.splitlines())
    assert "review/new.txt" in tracked
    assert "review/rename-destination.txt" in tracked
    assert "review/deleted.txt" not in tracked
    assert "review/rename-source.txt" not in tracked
    assert {
        "review/new.txt",
        "review/target.txt",
        "review/deleted.txt",
        "review/rename-source.txt",
        "review/rename-destination.txt",
    }.issubset(files)

    external.REPO = output
    diff = external.extract_diff("HEAD", ["review"])

    assert "+new-body" in diff
    assert "+modified-body" in diff
    assert "-deleted-body" in diff
    assert "rename from review/rename-source.txt" in diff
    assert "rename to review/rename-destination.txt" in diff


def test_cli_process_reports_stale_snapshot_without_staging(tmp_path):
    repo = _repo(tmp_path)
    target = repo / "review" / "target.txt"
    target.write_text("staged-copy\n", encoding="utf-8")
    _git(repo, "add", "review/target.txt")
    target.write_text("working-copy\n", encoding="utf-8")
    output = tmp_path / "gate"

    result = subprocess.run(
        [
            sys.executable,
            str(TOOLS / "gate_snapshot.py"),
            "--repo",
            str(repo),
            "--output",
            str(output),
            "--paths",
            "review/target.txt",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert result.returncode == 1
    assert "working tree와 다릅니다" in result.stderr
    assert _git(repo, "show", ":review/target.txt").stdout == "staged-copy\n"


def test_cli_success_reports_resolved_repo_root(tmp_path):
    repo = _repo(tmp_path)
    output = tmp_path / "gate"

    result = subprocess.run(
        [
            sys.executable,
            str(TOOLS / "gate_snapshot.py"),
            "--repo",
            str(repo),
            "--output",
            str(output),
            "--paths",
            "review/target.txt",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert result.returncode == 0, result.stderr
    assert f"repo root: {repo.resolve()}" in result.stdout


def test_cli_success_enumerates_verified_files(tmp_path):
    """성공 출력이 개수와 함께 검증 대상 파일 목록을 병기한다 (T-0544 ⑤).

    개수만으론 무엇을 검증했는지 확인할 수 없어, 수집 누락이 조용한 stale 잔여로 남는다."""
    repo = _repo(tmp_path)
    (repo / "review" / "extra.txt").write_text("extra\n", encoding="utf-8")
    _git(repo, "add", "review/extra.txt")
    _git(repo, "commit", "-qm", "extra fixture")
    output = tmp_path / "gate"

    result = subprocess.run(
        [
            sys.executable,
            str(TOOLS / "gate_snapshot.py"),
            "--repo",
            str(repo),
            "--output",
            str(output),
            "--paths",
            "review",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert result.returncode == 0, result.stderr
    assert "검증 대상 2개" in result.stdout
    assert "  - review/extra.txt" in result.stdout
    assert "  - review/target.txt" in result.stdout
    assert "스냅샷에 없음" not in result.stdout


def test_cli_success_marks_paths_absent_from_the_snapshot(tmp_path):
    """스냅샷에 없는 대상엔 index 기준 삭제라는 사유를 병기한다.

    표기가 없으면 리뷰어가 부재를 검증 누락이나 복사 실패로 읽는다."""
    repo = _repo(tmp_path)
    (repo / "review" / "kept.txt").write_text("kept\n", encoding="utf-8")
    _git(repo, "add", "review/kept.txt")
    _git(repo, "commit", "-qm", "kept fixture")
    (repo / "review" / "target.txt").unlink()
    _git(repo, "add", "-u", "review/target.txt")
    output = tmp_path / "gate"

    result = subprocess.run(
        [
            sys.executable,
            str(TOOLS / "gate_snapshot.py"),
            "--repo",
            str(repo),
            "--output",
            str(output),
            "--paths",
            "review",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert result.returncode == 0, result.stderr
    assert "  - review/target.txt (스냅샷에 없음 — index 기준 삭제)" in result.stdout
    assert "  - review/kept.txt\n" in result.stdout


def test_snapshot_recreation_keeps_pm_home_ledger_while_removed_fix_flag_is_inert(
    tmp_path, monkeypatch, capsys,
):
    """스냅샷 재생성 뒤에도 PM 홈 장부는 존속하고 폐지 플래그는 이를 바꾸지 않는다."""
    repo = _repo(tmp_path)
    tickets = repo / ".project_manager" / "wiki" / "tickets" / "open"
    tickets.mkdir(parents=True)
    (tickets / "T-9999-anchor.md").write_text(
        "---\nid: T-9999\ntitle: PM-home anchor fixture\nstatus: open\n---\n",
        encoding="utf-8",
    )
    (repo / ".project_manager" / "local.conf").write_text(
        _REVIEWER_TARGET_LINES + "additional_reviewer.rounds_max=2\n",
        encoding="utf-8",
    )
    target = repo / "review" / "target.txt"
    target.write_text("changed\n", encoding="utf-8")
    _git(repo, "add", "review/target.txt")

    # 스냅샷의 소유 PM 홈 해소가 정상이어야 마커 거부 축이 관측된다(장부 부재는 해소 실패 축).
    lease = repo / ".project_manager" / ".local" / "worktree-leases.json"
    lease.parent.mkdir(parents=True, exist_ok=True)
    lease.write_text(
        json.dumps({"leases": [{"slot": "work/slot_1", "state": "leased"}]}),
        encoding="utf-8",
    )

    snapshot = _load("gate_snapshot")
    first, _files = snapshot.create_snapshot(
        repo, tmp_path / "gate-round-1", ["review/target.txt"],
    )
    # 스냅샷은 검토 대상 경로만 담으므로 conf 가 없다 — 이 절이 재는 축은 마커 거부라, 대상 해소가
    # 먼저 걸리지 않게 스냅샷 앵커에도 같은 리뷰어 세트를 둔다.
    (first / ".project_manager").mkdir(parents=True, exist_ok=True)
    (first / ".project_manager" / "local.conf").write_text(
        _REVIEWER_TARGET_LINES, encoding="utf-8")
    external = _load("additional_reviewer")

    answers = [
        (
            "판정: 반려\n\n**must-fix** (반드시 수정):\n- 장부 앵커를 고정한다\n",
            True,
        ),
        (
            "판정: 통과\n\n**must-fix** (반드시 수정):\n- 없음\n",
            False,
        ),
    ]
    review_calls = {"n": 0}

    def _review(*args, **kwargs):
        answer, rejected = answers[review_calls["n"]]
        review_calls["n"] += 1
        return {
            "reviewer": "x", "ok": True, "output": answer, "answer": answer,
            "verdict": {"has_must_fix": rejected, "has_pass": not rejected},
            "file": None, "failed": False, "started": True,
            "any_must_fix": rejected, "all_pass": not rejected,
        }

    monkeypatch.setattr(external, "run_review", _review)

    # PM 37의 발단 형상: 격리 스냅샷 cwd(=엔진 자기 앵커)에서 장부 라운드를 열려 하면
    # 호출·예약 전에 막히고 스냅샷 안에는 휘발 장부가 생기지 않는다.
    external.REPO = first
    assert external.main([
        "--gate", "T-0634", "--paths", "review/target.txt",
        "--output-dir", str(tmp_path / "snapshot-raw"),
    ]) == 1
    assert review_calls["n"] == 0
    assert not (first / ".project_manager" / ".local" / "review_rounds.json").exists()
    assert "게이트 스냅샷 마커가 있는 앵커" in capsys.readouterr().err

    # 운영 표준인 PM 홈 앵커에서 반려 라운드를 기록한다.
    external.REPO = repo
    assert external.main([
        "--gate", "T-0634", "--paths", "review/target.txt",
        "--output-dir", str(tmp_path / "home-raw-round"),
    ]) == 1
    assert review_calls["n"] == 1
    ledger_path = repo / ".project_manager" / ".local" / "review_rounds.json"
    assert json.loads(ledger_path.read_text(encoding="utf-8"))["T-0634"]["count"] == 1
    capsys.readouterr()

    # 새 격리 스냅샷을 만들어도 장부는 PM 홈 소유라 사라지지 않는다.
    second, _files = snapshot.create_snapshot(
        repo, tmp_path / "gate-round-2", ["review/target.txt"],
    )
    assert (
        second / ".project_manager" / ".local" / "gate-snapshot.json"
    ).is_file()
    assert second != first and not (
        second / ".project_manager" / ".local" / "review_rounds.json"
    ).exists()
    external.REPO = repo
    before = ledger_path.read_bytes()
    with pytest.raises(SystemExit):
        external.main([
            "--gate", "T-0634", "--paths", "review/target.txt", "--confirm-fix",
            "--output-dir", str(tmp_path / "home-raw-confirm"),
        ])
    assert "unrecognized arguments: --confirm-fix" in capsys.readouterr().err

    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert ledger_path.read_bytes() == before
    assert ledger["T-0634"]["count"] == 1
    assert "confirm_fix" not in ledger["T-0634"]
    assert [row["verdict"] for row in ledger["T-0634"]["rounds"]] == [1]
    assert review_calls["n"] == 1


# ══ T-0701: --paths 가 staged 변경 집합보다 좁을 때 (경고 / --strict-scope 차단) ══
# 리뷰어가 dev 산출을 못 보면 이미 해소된 것을 must-fix 로 내는 false-finding 이 난다
# (PM 41차 실측 2건). 그 입력 누락을 계산으로 표면화한다.

SCOPE_GAP_MESSAGE = "staged 변경인데 --paths 에 없음"


def test_staged_change_outside_paths_warns_without_changing_rc(
    snapshot, tmp_path, capsys,
):
    """(g) staged ⊃ --paths → 경로 나열 + 처방 경고, rc 는 그대로 0."""
    repo = _repo(tmp_path)
    (repo / "review" / "target.txt").write_text("staged-target\n", encoding="utf-8")
    (repo / "other.txt").write_text("staged-other\n", encoding="utf-8")
    _git(repo, "add", "review/target.txt", "other.txt")

    rc = snapshot.main([
        "--repo", str(repo), "--output", str(tmp_path / "gate"),
        "--paths", "review/target.txt",
    ])
    captured = capsys.readouterr()

    assert rc == 0
    assert SCOPE_GAP_MESSAGE in captured.err
    assert "other.txt" in captured.err
    assert "리뷰어가 HEAD 판을 본다" in captured.err
    assert "타 티켓 산출이면 그대로 진행" in captured.err
    assert (tmp_path / "gate").is_dir()               # 경고는 생성을 막지 않는다


def test_strict_scope_blocks_when_staged_change_is_outside_paths(
    snapshot, tmp_path, capsys,
):
    """(h) `--strict-scope` 는 같은 판정을 rc≠0 차단으로 올린다(스냅샷 미생성)."""
    repo = _repo(tmp_path)
    (repo / "review" / "target.txt").write_text("staged-target\n", encoding="utf-8")
    (repo / "other.txt").write_text("staged-other\n", encoding="utf-8")
    _git(repo, "add", "review/target.txt", "other.txt")

    rc = snapshot.main([
        "--repo", str(repo), "--output", str(tmp_path / "gate"),
        "--paths", "review/target.txt", "--strict-scope",
    ])
    captured = capsys.readouterr()

    assert rc != 0
    assert SCOPE_GAP_MESSAGE in captured.err and "other.txt" in captured.err
    assert not (tmp_path / "gate").exists()           # 차단은 worktree 를 남기지 않는다


def test_staged_set_equal_to_paths_has_no_scope_warning(snapshot, tmp_path, capsys):
    """(i) staged == --paths → 무경고 (디렉터리 선택자도 그 아래를 덮는다)."""
    repo = _repo(tmp_path)
    (repo / "review" / "target.txt").write_text("staged-target\n", encoding="utf-8")
    _git(repo, "add", "review/target.txt")

    rc = snapshot.main([
        "--repo", str(repo), "--output", str(tmp_path / "gate"),
        "--paths", "review",
    ])
    captured = capsys.readouterr()

    assert rc == 0
    assert SCOPE_GAP_MESSAGE not in captured.err


def test_scope_check_excludes_untracked_and_other_repositories(
    snapshot, tmp_path, capsys,
):
    """(j) untracked 와 타 repo staged 는 이 저장소 index diff 가 아니라 계산 밖이다."""
    repo = _repo(tmp_path)
    (repo / "review" / "target.txt").write_text("staged-target\n", encoding="utf-8")
    _git(repo, "add", "review/target.txt")
    (repo / "untracked-wip.txt").write_text("wip\n", encoding="utf-8")

    neighbor = tmp_path / "neighbor"
    neighbor.mkdir()
    _git(neighbor, "init", "-q")
    _git(neighbor, "config", "user.email", "test@example.invalid")
    _git(neighbor, "config", "user.name", "Test User")
    (neighbor / "neighbor-staged.txt").write_text("staged\n", encoding="utf-8")
    _git(neighbor, "add", "neighbor-staged.txt")

    rc = snapshot.main([
        "--repo", str(repo), "--output", str(tmp_path / "gate"),
        "--paths", "review/target.txt",
    ])
    captured = capsys.readouterr()

    assert rc == 0
    assert SCOPE_GAP_MESSAGE not in captured.err
    assert "untracked-wip.txt" not in captured.err
    assert "neighbor-staged.txt" not in captured.err


def test_staged_rename_reports_both_sides_of_the_move(snapshot, tmp_path, capsys):
    """rename 은 옛/새 경로 둘 다 알린다 — 검출 설정에 따라 한쪽이 사라지면 안 된다."""
    repo = _repo(tmp_path)
    _git(repo, "mv", "other.txt", "moved.txt")
    (repo / "review" / "target.txt").write_text("staged-target\n", encoding="utf-8")
    _git(repo, "add", "review/target.txt")

    rc = snapshot.main([
        "--repo", str(repo), "--output", str(tmp_path / "gate"),
        "--paths", "review/target.txt",
    ])
    captured = capsys.readouterr()

    assert rc == 0
    assert "other.txt" in captured.err and "moved.txt" in captured.err


def test_strict_scope_is_opt_in_and_documented(snapshot):
    """기본값은 경고(rc 불변)다 — 차단은 명시 opt-in 이어야 게이트가 계속 돈다."""
    action = next(
        action for action in snapshot.build_parser()._actions
        if action.dest == "strict_scope"
    )
    assert action.default is False
    assert "차단" in action.help
