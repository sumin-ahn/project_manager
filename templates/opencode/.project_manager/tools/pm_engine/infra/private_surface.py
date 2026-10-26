"""저장소 소유 파일과 manifest에 따른 사설 참조 출하 표면 (층: infra)."""
from __future__ import annotations

from pathlib import Path
from pm_engine.infra import manifest_store, repo_files


def repo_owned_paths(root: Path) -> list[Path]:
    """현재 tree에 존재하는 repo-owned 파일을 공용 ``OWNED`` 규칙으로 반환한다.

    추적 파일과 미추적·비-ignore 파일만 포함한다. git 없는 소스 아카이브에서는 공용
    seam이 loud warning과 함께 filesystem 폴백한다.
    """
    root = root.resolve()
    return [
        path
        for relative in repo_files.list_repo_owned_files(
            root, Path("."), mode=repo_files.OWNED
        )
        if (path := root / relative).is_file()
    ]


_UNREGISTERED_ROOT_DESTINATIONS: tuple[Path, ...] = (Path(".claude/run_tests_hook.sh"),)


def _root_destination_language_paths(root: Path) -> set[Path]:
    """루트 자신을 dest 로 삼는 manifest 항목을 전개해 언어 축 루트 목적지 사본을 찾는다.

    ``shipping_paths`` 의 python 축은 templates 상대경로 일치로 루트 사본(``ctx_guard.py`` 등)을
    찾지만, 그 트릭을 확장자 전체로 일반화하면 우연히 같은 상대경로를 쓰는 인스턴스-소유 파일
    (루트 ``.gitignore``·``.claude/settings.json`` — engine.manifest 가 "미등록·전파 안 함"이라
    문서화한 예외)까지 오분류한다. 대신 ``pm_update`` 가 실제 update 계획에 쓰는
    ``manifest_entry_shipping_inventory`` 를 dest=root=source 로 호출해 **루트 자신의 manifest 가
    실제로 선언한** 항목만 전개한다(판정 사본 0) — 인스턴스-소유 예외는애초에 manifest 미등재라
    자동으로 빠진다.
    """
    resolved_root = root.resolve()
    manifest_path = manifest_store.resolve_manifest_for_dest(resolved_root, resolved_root)
    manifest = manifest_store.read_manifest(manifest_path)
    found: set[Path] = set()
    for entry_index in range(len(manifest)):
        shipped, _missing, _target_owned = manifest_store.manifest_entry_shipping_inventory(
            resolved_root, manifest, entry_index, resolved_root
        )
        for dest_relative, _source in shipped:
            destination = resolved_root / dest_relative
            if destination.suffix in (".py", ".md"):
                continue
            if destination.is_file():
                found.add(destination)
    for relative in _UNREGISTERED_ROOT_DESTINATIONS:
        candidate = resolved_root / relative
        if candidate.is_file():
            found.add(candidate)
    return found


def language_paths(root: Path) -> list[Path]:
    """출하 표면 − python − markdown. ``shipping_paths`` 와 같은 ``repo_owned_paths`` 열거에서
    파생한다(두 번째 열거 함수를 신설하지 않는다).

    templates 트리 아래 python·markdown 이 아닌 모든 파일 + 그 상대경로를 실제로 manifest 가
    루트 목적지로 선언한 사본(``_root_destination_language_paths``). 확장자 등록 여부는 여기서
    보지 않는다 — 미등록 확장자는 ``language_of``/``language_prose_spans`` 호출 시점에 fail-loud
    한다(시야를 조용히 좁히지 않는다).
    """
    resolved_root = root.resolve()
    python_paths, markdown_paths = shipping_paths(root)
    excluded = set(python_paths) | set(markdown_paths)
    found: set[Path] = set()
    for path in repo_owned_paths(root):
        if path in excluded:
            continue
        relative = path.relative_to(resolved_root)
        parts = relative.parts
        if len(parts) < 3 or parts[0] != "templates":
            continue
        found.add(path)
    found |= _root_destination_language_paths(root) - excluded
    return sorted(found)


def shipping_paths(root: Path) -> tuple[list[Path], list[Path]]:
    """private-context 출하 표면을 repo-owned 열거 결과에서 분류한다.

    ``dashboard.md``와 ``pm_state.md`` 같은 per-clone 파생 파일은 경로별 예외가
    아니라 git ignore/소유 판정으로 빠진다. 따라서 앞으로 다른 파생 파일이 생겨도
    같은 규칙이 적용되고, 비-ignore 신규 출하 파일은 계속 검사한다.

    python 축은 루트 canonical 엔진(``.project_manager/tools/*.py``) +
    ``templates/<harness>/`` 트리 전체의 모든 python(엔진 사본 + 어댑터, 깊이 무관) +
    templates canonical 과 같은 상대경로를 공유하는 루트 목적지 사본(예:
    ``.claude/ctx_guard.py`` — ``engine.manifest`` 의 ``@source=`` 목적지가 이 트리 안에
    실재하면 그것도 출하 표면이라는 규칙의 파생)이다. 마지막 항은 경로를 하드코딩하지
    않고 templates 열거 결과에서 상대경로 대조로 도출한다.
    """
    resolved_root = root.resolve()
    owned = repo_owned_paths(root)
    python_paths: set[Path] = set()
    markdown_paths: set[Path] = set()
    template_python_relpaths: set[Path] = set()
    for path in owned:
        relative = path.relative_to(resolved_root)
        parts = relative.parts
        if relative == Path("CLAUDE.md"):
            markdown_paths.add(path)
            continue
        if (
            path.suffix == ".py"
            and relative.parent == Path(".project_manager", "tools")
        ):
            python_paths.add(path)
            continue
        if (
            path.suffix == ".md"
            and len(parts) >= 3
            and parts[0] == ".project_manager"
            and parts[1] == "wiki"
        ):
            markdown_paths.add(path)
            continue
        if path.suffix == ".md" and parts and parts[0] == ".claude":
            markdown_paths.add(path)
            continue
        if len(parts) < 3 or parts[0] != "templates":
            continue
        if path.suffix == ".md":
            markdown_paths.add(path)
        elif path.suffix == ".py":
            python_paths.add(path)
            template_python_relpaths.add(Path(*parts[2:]))
    # 루트 목적지 사본 — templates canonical 과 상대경로가 일치하는 루트 python 만
    # 편입한다(파생·하드코딩 0). 오늘 값: `.claude/ctx_guard.py`·`.claude/ctx_stop_hook.py`.
    for path in owned:
        if path.suffix != ".py" or path in python_paths:
            continue
        relative = path.relative_to(resolved_root)
        if relative.parts and relative.parts[0] == "templates":
            continue
        if relative in template_python_relpaths:
            python_paths.add(path)
    return sorted(python_paths), sorted(markdown_paths)
