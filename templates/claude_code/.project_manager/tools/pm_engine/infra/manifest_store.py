"""engine.manifest 판독·병합과 항목별 배포 파일 목록 (층: infra).

manifest 파일을 읽고, 항목이 실제로 배포하는 파일을 파일 시스템과 git 추적 목록에서 센다.
"""
from __future__ import annotations

import stat
import sys
import warnings
from pathlib import Path

from pm_engine.domain import manifest_rules
from pm_engine.infra import fs, repo_files


def read_manifest(path: Path) -> list[manifest_rules.ManifestEntry]:
    """manifest 파일 → ManifestEntry 리스트 ('#' 주석·빈 줄 제외·마커 파싱).

    각 항목은 `str` 서브클래스 ManifestEntry — 값은 path 문자열이고 `.render`·`.target_owned`·
    `.source_rel` 속성이 그 path 의 마커 여부/값을 운반한다. path 행 끝의 마커(`@render`·
    `@target-owned`·`@source=<path>`)는 복수·순서 무관으로 인식해 전부 떼어내고 순수 경로만
    ManifestEntry 값으로 남긴다.
      - `@render`→ render=True (byte-copy 대신 render_adapter)
      - `@target-owned`→ target_owned=True (엔진 upstream source-부재가 정상·skip 판별)
      - `@source=<path>`→ source_rel=<path> (source_root 아래 canonical 소스에서 읽고
                                     dest 엔 manifest 경로로 기록·source-remap)
    예: `.opencode/agents  @render @source=templates/opencode/.opencode/agents`
        → path=`.opencode/agents`, render=True, source_rel=`templates/opencode/.opencode/agents`.
    미주석=render/target_owned False·source_rel None → 오늘과 동일(순수 copy2·전파 대상·후방호환).
    """
    out: list[manifest_rules.ManifestEntry] = []
    for line in fs.read_text_shared(path, encoding="utf-8").splitlines():
        entry = manifest_rules._parse_manifest_line(line)
        if entry is not None:
            out.append(entry)
    # 구형·flavor manifest의 물리 행 순서가 남아 있어도 새 updater는 엔진 패키지를 먼저
    # 배포한다. 평면 파일 복사 중 끊겨도 패키지는 이미 완전하다.
    return sorted(out, key=lambda entry: str(entry) != manifest_rules._ENGINE_PACKAGE_REL)


def merge_manifest_sources(manifest_paths: list[Path]) -> dict:
    """선택된 template flavor manifest들을 선언 순서대로 합집합한다.

    경로가 처음 등장한 선언을 채택한다. 같은 경로가 뒤 flavor에도 있고 마커가 다르면 첫 선언의
    마커를 유지한다. 이는 ``plan_copy``의 MF3(선택 트리 순서가 결정적 우선순위)와 같은 정책이며,
    첫 flavor가 다른 flavor의 상위집합이라는 전제는 두지 않는다. 후순위 flavor의 주석/레이아웃은
    의도적으로 합치지 않고 새 관리 경로만 결정적 생성 절에 직렬화한다.

    첫 manifest의 주석/레이아웃은 그대로 보존하고, 뒤 manifest에서 새로 추가되는 경로만 생성 절로
    붙인다. 단일 manifest면 원문을 byte-identical 반환한다.
    """
    if not manifest_paths:
        raise ValueError("합칠 engine.manifest가 없습니다.")
    paths = [Path(p) for p in manifest_paths]
    first_text = fs.read_text_shared(paths[0], encoding="utf-8")
    merged: list[manifest_rules.ManifestEntry] = []
    seen: dict[str, manifest_rules.ManifestEntry] = {}
    additions: list[tuple[str, list[manifest_rules.ManifestEntry]]] = []
    conflicts: list[str] = []
    for index, path in enumerate(paths):
        current = read_manifest(path)
        added_here: list[manifest_rules.ManifestEntry] = []
        for entry in current:
            key = str(entry).replace("\\", "/")
            if key in seen:
                first_markers = manifest_rules._manifest_marker_key(seen[key])
                next_markers = manifest_rules._manifest_marker_key(entry)
                if (
                    first_markers != next_markers
                    and not (
                        key == manifest_rules._MANIFEST_SELF_REL
                        and first_markers[:2] == next_markers[:2]
                    )
                ):
                    conflicts.append(key)
                continue
            seen[key] = entry
            merged.append(entry)
            if index:
                added_here.append(entry)
        if index and added_here:
            try:
                flavor = path.parents[1].name
            except IndexError:
                flavor = path.parent.name
            additions.append((flavor, added_here))
    if len(paths) == 1:
        text = first_text
    else:
        chunks = [first_text.rstrip("\n")]
        for flavor, entries in additions:
            chunks.extend([
                "",
                f"# ── 선택 flavor 합집합: {flavor} (pm_import/pm_update 생성) ──",
                *(manifest_rules._manifest_entry_line(entry) for entry in entries),
            ])
        text = "\n".join(chunks) + "\n"
    return {
        "entries": merged,
        "text": text,
        "conflicts": sorted(set(conflicts)),
        "paths": paths,
    }


def _shipping_inventory(repo_files, root: Path, rel: str) -> list:
    """tracked 출하 목록과 미추적 제외 신호를 만든다.

    ``pm-update`` 출하는 manifest 디렉토리의 byte-copy 채널이다. 따라서 공용 seam의 넓은
    domain(gap 검출에 필요한 symlink/gitlink 포함)은 유지하되, 이 소비처에서만 실제 일반
    파일로 좁힌다. git checkout일 때 OWNED와의 차집합 중 같은 일반 파일 판정을 통과한
    미추적 파일 수만 한 줄로 알려 ``git add`` 뒤에만 출하된다는 계약을 숨기지 않는다.
    """
    runner = repo_files._real_git_runner(root)
    tracked = repo_files.list_repo_owned_entries(
        root,
        rel,
        mode=repo_files.TRACKED_ONLY,
        git_runner=runner,
    )
    rc, inside = runner(["rev-parse", "--is-inside-work-tree"])
    filesystem_fallback = not (rc == 0 and inside.strip() == "true")
    if rc == 0 and inside.strip() == "true":
        owned = repo_files.list_repo_owned_files(
            root, rel, mode=repo_files.OWNED, git_runner=runner)
        tracked_paths = {entry.path for entry in tracked}
        untracked_count = sum(
            _is_shippable_regular_file(root, relative, index_mode=None)
            for relative in set(owned) - tracked_paths
        )
        if untracked_count:
            print(
                f"pm-update: untracked {untracked_count}건 제외 — git add 후 전파됨 "
                f"(subtree={rel})",
                file=sys.stderr,
            )
        if not tracked and (root / rel).is_file():
            ignored_rc, _ignored_detail = runner([
                "check-ignore",
                "--quiet",
                "--",
                rel,
            ])
            if ignored_rc == 0:
                print(
                    "pm-update: manifest 선언 단일 파일이 source에서 gitignore되어 "
                    f"출하되지 않음: {rel} — ignore 규칙을 제거하고 git add 하라",
                    file=sys.stderr,
                )
    if not tracked:
        # seam은 coverage·부분 subtree 질의도 쓰므로 빈 결과 자체를 예외로 만들지 않는다.
        # manifest 경로가 존재해 이 소비점에 도달한 경우만 출하 결함으로 직접 승격한다.
        # 위의 OWNED/check-ignore 진단을 먼저 내 원인 판별 정보도 잃지 않는다.
        raise manifest_rules.EmptyShippingInventoryError(
            root, rel, filesystem_fallback=filesystem_fallback)
    return tracked


def _shippable_tracked_entries(
    root: Path,
    entries: list[repo_files.RepoOwnedEntry],
) -> list[tuple[Path, Path]]:
    """tracked 엔트리를 안전한 byte-copy source로 좁히고 제외 이유를 loud하게 합친다."""
    accepted: list[tuple[Path, Path]] = []
    skipped: dict[str, list[str]] = {
        "working tree에서 삭제됨": [],
        "symlink(링크 의미를 byte-copy 출하하지 않음)": [],
        "디렉토리/gitlink(파일 byte-copy 대상 아님)": [],
        "일반 파일이 아닌 엔트리": [],
    }
    for entry in entries:
        relative = entry.path
        index_mode = entry.index_mode
        source = root / relative
        if index_mode == "120000":
            skipped["symlink(링크 의미를 byte-copy 출하하지 않음)"].append(
                relative.as_posix())
            continue
        if index_mode == "160000":
            skipped["디렉토리/gitlink(파일 byte-copy 대상 아님)"].append(
                relative.as_posix())
            continue
        if index_mode is not None and index_mode not in {"100644", "100755"}:
            skipped["일반 파일이 아닌 엔트리"].append(
                f"{relative.as_posix()} (git index mode {index_mode})")
            continue
        try:
            mode = source.lstat().st_mode
        except FileNotFoundError:
            skipped["working tree에서 삭제됨"].append(relative.as_posix())
            continue
        except OSError as exc:
            skipped["일반 파일이 아닌 엔트리"].append(
                f"{relative.as_posix()} ({exc})")
            continue
        if stat.S_ISREG(mode):
            accepted.append((relative, source))
        elif stat.S_ISLNK(mode):
            skipped["symlink(링크 의미를 byte-copy 출하하지 않음)"].append(
                relative.as_posix())
        elif stat.S_ISDIR(mode):
            skipped["디렉토리/gitlink(파일 byte-copy 대상 아님)"].append(
                relative.as_posix())
        else:
            skipped["일반 파일이 아닌 엔트리"].append(relative.as_posix())

    for reason, paths in skipped.items():
        if paths:
            warnings.warn(
                f"pm-update 출하 tracked 엔트리 {len(paths)}건 제외 — {reason}: "
                + ", ".join(paths),
                manifest_rules.SkippedRepoShippingEntryWarning,
                stacklevel=2,
            )
    return accepted


def _is_shippable_regular_file(
    root: Path,
    relative: Path,
    *,
    index_mode: str | None,
) -> bool:
    """출하 가능한 일반 파일인가 — index mode와 working-tree lstat의 공통 판정."""
    if index_mode is not None and index_mode not in {"100644", "100755"}:
        return False
    try:
        return stat.S_ISREG((root / relative).lstat().st_mode)
    except OSError:
        return False


def _iter_files(root: Path, rel: str):
    """manifest 엔트리(파일/디렉토리) → (repo 기준 relpath, src 절대경로) 들.

    디렉토리는 repo-owned seam으로 협착하고 symlink/gitlink 등 제외를 loud하게 표면화한다.
    relpath 는 **항상 posix(슬래시) 정규화**한다(`as_posix()`) — 모듈 전체의 슬래시 관례
    (`_path_under_manifest`·`_dest_relpath_for` 는 `.replace("\\","/")` 로 슬래시 전제)과 통일.
    `str(Path.relative_to)` 는 OS-네이티브 구분자라 Windows 에선 역슬래시(`.claude\\agents\\x.md`)
    를 산출해 plan change 튜플 key 가 소비자/테스트(슬래시)와 어긋났다(pm_render 4건 red).
    POSIX 에선 `str(p.relative_to(root)) == p.relative_to(root).as_posix()` 라 동작 무변경.
    """
    src = root / rel
    if src.is_symlink():
        warnings.warn(
            f"pm-update 출하 manifest 엔트리 제외 — symlink 의미를 byte-copy 출하하지 않음: {rel}",
            manifest_rules.SkippedRepoShippingEntryWarning,
            stacklevel=2,
        )
    elif src.is_dir():
        tracked = _shipping_inventory(repo_files, root, rel)
        for relative, source in _shippable_tracked_entries(root, tracked):
            yield relative.as_posix(), source
    elif src.is_file():
        tracked = _shipping_inventory(repo_files, root, rel)
        for accepted_relative, source in _shippable_tracked_entries(root, tracked):
            yield accepted_relative.as_posix(), source


def _is_board_separated(dest_root: Path) -> bool:
    """dest 가 board-분리 형상인가 — `<dest>/.project_manager/board/tickets` 가 실 dir.

    `pm_paths._board_root_at` 의 판별과 동형 — pm_update 가 board 를 import 하지 않고
    같은 *실측* probe 로 dest 레이아웃을 가른다. board/tickets 가 없으면 legacy(False·무변경).
    """
    return (Path(dest_root) / ".project_manager" / "board" / "tickets").is_dir()


def _dest_relpath_for(rel: str, dest_root: Path) -> str:
    """manifest source relpath → dest 기록 relpath (board-분리 인지 리매핑).

    `wiki/tickets/_template.md` 항목은 board-분리 dest 에서 `board/tickets/_template.md` 로
    리매핑한다(board 루트 추종) — source 는 upstream 의 wiki/ 에서 그대로 읽되 dest 만 옮긴다.
    legacy dest(board/ 미분리)거나 다른 모든 항목은 입력 그대로(무변경·후방호환). 경로 비교는
    OS-무관하게 posix-normalize 한다(_iter_files 가 str(Path) 로 yield 해 Windows 에선 `\\` 가
    섞일 수 있다)."""
    rel_norm = rel.replace("\\", "/")
    if rel_norm == manifest_rules._TEMPLATE_REL and _is_board_separated(dest_root):
        return manifest_rules._BOARD_TEMPLATE_REL
    return rel


def _manifest_owner_index(manifest: list, rel: str, dest_root: Path) -> int | None:
    """``rel``을 공급할 가장 구체적인 manifest 항목의 index.

    디렉터리 remap 위에 단일 파일 remap을 선언하면 더 긴 destination 경로가 override다. 이
    우선순위가 없으면 상위 디렉터리와 파일 항목이 같은 destination을 각각 plan하고, plan 시점의
    기존 파일만 비교한 뒤 apply 순서에 따라 override가 사라질 수 있다. 동일 경로 중복은 뒤 항목을
    택해 manifest의 인접 override가 결정적이게 한다.
    """
    rel_norm = rel.replace("\\", "/").strip("/")
    owners: list[tuple[int, int, int]] = []
    for index, candidate in enumerate(manifest):
        dest_rel = _dest_relpath_for(str(candidate), dest_root)
        dest_norm = dest_rel.replace("\\", "/").strip("/")
        if rel_norm == dest_norm or rel_norm.startswith(dest_norm + "/"):
            owners.append((len(Path(dest_norm).parts), index, index))
    return max(owners)[2] if owners else None


def manifest_entry_shipping_inventory(
    source_root: Path,
    manifest: list,
    entry_index: int,
    dest_root: Path,
) -> tuple[list[tuple[str, Path]], bool, bool]:
    """manifest 한 항목의 실제 byte-copy 출하 inventory와 누락 성격을 반환한다.

    반환은 ``(files, missing, target_owned)``다. ``files``의 각 항목은
    ``(dest repo 상대경로, source 절대경로)``이며, 디렉토리의 tracked-only 열거·일반 파일
    협착·``@source`` 리매핑·가장 구체적인 manifest 소유권·dest 레이아웃 리매핑을 모두
    ``plan``과 같은 경로로 적용한다. source가 없으면 files는 비고 ``missing=True``이며,
    호출자가 ``target_owned``로 정상적인 전파 제외와 엔진 누락 오류를 구분한다.

    출하 목록을 관찰하는 다른 엔진 기능은 이 seam을 소비해야 한다. manifest 경로를 직접
    ``iterdir``/``rglob``로 전개하면 git ignore, index mode, source remap과 override 의미가
    실제 update 계획에서 다시 갈라진다.
    """
    source_root = Path(source_root)
    effective_dest = Path(dest_root)
    entry = manifest[entry_index]
    rel = str(entry)
    source_rel = manifest_rules._source_root_rel(entry)
    target_owned = manifest_rules._entry_target_owned_flag(entry)
    if not (source_root / source_rel).exists():
        return [], True, target_owned

    files: list[tuple[str, Path]] = []
    for shipped_rel, source in _iter_files(source_root, source_rel):
        shipped_rel = manifest_rules._remap_to_dest(shipped_rel, source_rel, rel)
        shipped_rel = _dest_relpath_for(shipped_rel, effective_dest)
        if _manifest_owner_index(manifest, shipped_rel, effective_dest) != entry_index:
            continue
        files.append((shipped_rel, source))
    return files, False, target_owned


def _template_dir_from_manifest(
    manifest_path: Path | None,
    source_root: Path,
) -> str | None:
    """flavor manifest 경로에서 `templates/<dir>` context를 파생한다. root manifest면 None."""
    if manifest_path is None:
        return None
    try:
        rel = Path(manifest_path).resolve().relative_to(
            (Path(source_root) / "templates").resolve()
        )
    except (OSError, ValueError):
        return None
    return rel.parts[0] if len(rel.parts) >= 3 else None


def _entry_notation_templates_from_manifests(
    manifest_paths: list[Path],
    source_root: Path,
) -> dict[str, tuple[str, ...]]:
    """선택 manifest의 항목별 flavor 집합을 선언 순서대로 보존한다.

    서로 다른 경로는 각 flavor 하나를 받고, 같은 물리 경로는 그 파일을 실제로 읽는 flavor를
    전부 받는다. 선택된 flavor manifest를 읽지 못하면 context만 조용히 버리지 않고 fail-loud한다.
    로컬 manifest 동기화는 계속하면서 notation context만 유실하면 canonical slash가 codex의 기존
    dollar 산출물을 덮기 때문이다.
    """
    contexts: dict[str, list[str]] = {}
    for manifest_path in manifest_paths:
        template_dir = _template_dir_from_manifest(manifest_path, source_root)
        if template_dir is None:
            continue
        try:
            entries = read_manifest(manifest_path)
        except (OSError, UnicodeError, ValueError) as exc:
            raise RuntimeError(
                "선택된 flavor manifest에서 스킬 표기 context를 해소할 수 없다 — "
                f"기존 렌더 산출물을 보존하기 위해 동기화를 중단한다: {manifest_path} ({exc})"
            ) from exc
        for entry in entries:
            rel = str(entry).replace("\\", "/")
            flavors = contexts.setdefault(rel, [])
            if template_dir not in flavors:
                flavors.append(template_dir)
    return {rel: tuple(flavors) for rel, flavors in contexts.items()}


def resolve_manifest_for_dest(dest_root: Path, source_root: Path) -> Path:
    """dest_root 의 engine.manifest 우선, 없으면 source_root 의 것."""
    dest_manifest = dest_root / ".project_manager" / "engine.manifest"
    if dest_manifest.exists():
        return dest_manifest
    source_manifest = source_root / ".project_manager" / "engine.manifest"
    if source_manifest.exists():
        return source_manifest
    raise FileNotFoundError("engine.manifest 없음 (dest·source 둘 다).")
