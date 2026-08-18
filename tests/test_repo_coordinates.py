"""PM-home ticket touches → 소유 repo 좌표 공용 normalizer 테스트 (T-0473)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
MODULE = REPO / ".project_manager" / "tools" / "repo_coordinates.py"


@pytest.fixture(scope="module")
def coords():
    spec = importlib.util.spec_from_file_location("repo_coordinates", MODULE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_lease(
        root: Path, slot: str, *, repo: str = "project_manager", state: str = "leased") -> Path:
    ledger = root / ".project_manager" / ".local" / "worktree-leases.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        json.dumps({
            "leases": [{
                "slot": slot,
                "repo": repo,
                "session": "T-0473",
                "state": state,
            }],
        }),
        encoding="utf-8",
    )
    return ledger


def test_normalize_repo_path_strips_only_registered_live_slot(coords, tmp_path):
    slot = "work/project_manager_1"
    (tmp_path / slot).mkdir(parents=True)
    ledger = _write_lease(tmp_path, slot)

    assert coords.normalize_repo_path(
        f"{slot}/src/change.py",
        pm_root=tmp_path,
        leases_file=ledger,
    ) == "src/change.py"


def test_normalize_repo_path_slot_mismatch_fails_loud(coords, tmp_path):
    registered = "work/project_manager_1"
    (tmp_path / registered).mkdir(parents=True)
    ledger = _write_lease(tmp_path, registered)

    with pytest.raises(coords.RepoCoordinateError, match="slot 불일치"):
        coords.normalize_repo_path(
            "work/project_manager_2/src/change.py",
            pm_root=tmp_path,
            leases_file=ledger,
        )


def test_normalize_repo_path_non_worktree_path_passes_unchanged(coords, tmp_path):
    paths = [
        ".project_manager/wiki/domain/page.md",
        "src/change.py",
        ".project_manager/tools/domain.py",
    ]
    assert coords.normalize_repo_paths(paths, pm_root=tmp_path) == paths


def test_normalize_repo_path_idle_slot_keeps_persistent_repo_ownership(coords, tmp_path):
    """handoff --done 뒤 idle이어도 장부 slot↔repo 소유 관계와 디렉토리가 남으면 유효하다."""
    slot = "work/project_manager_2"
    (tmp_path / slot).mkdir(parents=True)
    ledger = _write_lease(tmp_path, slot, state="idle")

    normalized = coords.normalize_repo_path(
        f"{slot}/src/change.py", pm_root=tmp_path, leases_file=ledger)
    assert normalized == "src/change.py"
    assert normalized.repo == "project_manager"
    assert normalized.owner == "upstream"
    assert normalized.workspace == (tmp_path / slot).resolve()


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        (r"work\project_manager_1\src\a.py", "src/a.py"),
        ("./work/project_manager_1/src/a.py", "src/a.py"),
        ("work/project_manager_1/src//./nested.py", "src/nested.py"),
    ],
)
def test_normalize_repo_path_canonicalizes_separator_and_leading_dot(
        coords, tmp_path, written, expected):
    slot = "work/project_manager_1"
    (tmp_path / slot).mkdir(parents=True)
    ledger = _write_lease(tmp_path, slot)
    assert coords.normalize_repo_path(
        written, pm_root=tmp_path, leases_file=ledger) == expected


@pytest.mark.parametrize(
    "written",
    [
        "work/project_manager_1",
        "work/project_manager_1/",
        "work/project_manager_1/.",
    ],
)
def test_normalize_repo_path_rejects_whole_slot_with_or_without_trailing_slash(
        coords, tmp_path, written):
    """두 표기를 같은 fail-loud 판정으로 모아 광역 slot stage 선언을 막는다."""
    slot = "work/project_manager_1"
    (tmp_path / slot).mkdir(parents=True)
    ledger = _write_lease(tmp_path, slot)
    with pytest.raises(coords.RepoCoordinateError, match="slot 전체"):
        coords.normalize_repo_path(written, pm_root=tmp_path, leases_file=ledger)


def test_normalize_repo_path_rejects_traversal_at_coordinate_seam(coords, tmp_path):
    slot = "work/project_manager_1"
    (tmp_path / slot).mkdir(parents=True)
    ledger = _write_lease(tmp_path, slot)
    with pytest.raises(coords.RepoCoordinateError, match="traversal"):
        coords.normalize_repo_path(
            f"{slot}/../../outside.py", pm_root=tmp_path, leases_file=ledger)


@pytest.mark.parametrize(
    ("relative", "message"),
    [
        ("/abs/path.py", "절대경로"),
        ("C:/outside.py", "drive"),
        ("//server/share.py", "UNC"),
    ],
)
def test_normalize_repo_path_rejects_non_relative_coordinates(
        coords, tmp_path, relative, message):
    """접두 뒤 absolute/drive/UNC 표기는 repo 상대 좌표로 반환하지 않는다."""
    slot = "work/project_manager_1"
    (tmp_path / slot).mkdir(parents=True)
    ledger = _write_lease(tmp_path, slot)
    with pytest.raises(coords.RepoCoordinateError, match=message):
        coords.normalize_repo_path(
            f"{slot}/{relative}", pm_root=tmp_path, leases_file=ledger)



def _symlink_slot(link: Path, target: Path) -> None:
    """슬롯 경로를 target 심링크로 만든다 (권한/플랫폼 미지원이면 skip)."""
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"심링크 슬롯을 만들 수 없다 ({exc})")


def test_workspace_slot_derives_from_logical_path_when_slot_is_symlink(coords, tmp_path):
    """슬롯이 PM 홈 밖 실체를 가리키는 심링크여도 논리 경로로 slot 을 도출한다.

    실체 경로만 보면 `workspace가 PM 홈 밖` 으로 fail-loud 해, 심링크/외부 마운트 슬롯에서만
    접두 형식 touches 정규화가 통째로 막힌다. 반환하는 실행 경로는 resolve 된 실체 그대로다.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    home = tmp_path / "home"
    link = home / "work" / "project_manager_1"
    _symlink_slot(link, outside)

    normalized = coords.normalize_repo_path(
        "work/project_manager_1/src/engine.py", pm_root=home, workspace=link)

    assert normalized == "src/engine.py"
    assert normalized.workspace == outside.resolve()   # 실행 경로 = 실재 트리


def test_workspace_slot_existence_check_uses_resolved_path(coords, tmp_path):
    """실재 검사는 resolve 경로가 기준 — dangling 심링크는 slot 도출과 무관하게 fail-loud."""
    home = tmp_path / "home"
    link = home / "work" / "project_manager_1"
    _symlink_slot(link, tmp_path / "missing")

    with pytest.raises(coords.RepoCoordinateError, match="실재하지 않는다"):
        coords.normalize_repo_path(
            "work/project_manager_1/src/engine.py", pm_root=home, workspace=link)
