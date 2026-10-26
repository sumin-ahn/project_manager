"""기록된 instance-owned template 좌표·git blob·도달성 판독 (층: app)."""
from __future__ import annotations

from pathlib import Path
from pathlib import PurePosixPath
from pm_engine.domain import conf_schema
from pm_engine.domain import install_rules
from pm_engine.infra import install_files
from pm_engine.infra import install_git
from pm_engine.infra import install_receipt
from pm_engine.infra import install_source
from typing import NamedTuple
import difflib


class InstanceOwnedTemplateTarget(NamedTuple):
    """설치 인스턴스가 소유하는 한 파일과 설치 때 선택된 template 좌표."""
    relpath: str
    harness: str
    mode: str
    weight: str | None
    template: Path | None
    template_git_relpath: str | None


def _recorded_instance_owned_template_coordinates(dest_root: Path) -> dict:
    """설치 영수증의 파일별 template 좌표 — 구/손상 기록은 빈 좌표다."""
    loaded = install_receipt._load_install_receipt_document(Path(dest_root), quiet=True)
    if loaded.status != "ok" or loaded.document is None:
        return {}
    if install_receipt._install_receipt_is_newer_schema(loaded.document):
        return {}
    coordinates = loaded.document.get(install_receipt.INSTALL_RECEIPT_TEMPLATE_COORDINATES_KEY)
    return coordinates if isinstance(coordinates, dict) else {}


def _resolve_recorded_template_coordinate(
        source_root: Path, dest_relpath: str, coordinate) -> tuple[str, Path, str] | None:
    """영수증 좌표를 안전한 checkout 내부 source로 해소한다.

    install.json은 채택자가 편집할 수 있으므로 source를 그대로 현재 파일 read 경로로 쓰지 않는다.
    `templates/<등록 tree>/...` 아래이며 lite rename을 적용한 dst가 관측 대상과 정확히 같을 때만
    받아들인다. lite weight의 full source는 해당 tree에 lite 변종이 없을 때의 선언된 폴백이라
    허용하지만, full weight가 lite source를 가리키는 역조합은 거부한다.
    """
    if not isinstance(coordinate, dict):
        return None
    weight = coordinate.get("weight")
    source = coordinate.get("source")
    if weight not in install_rules.WEIGHT_CHOICES or not isinstance(source, str) or "\\" in source:
        return None
    parts = source.split("/")
    template_dirs = {
        dirname
        for dirnames in install_rules.HARNESS_TEMPLATE_DIRS.values()
        for dirname in dirnames
    }
    if (len(parts) < 3 or parts[0] != "templates" or parts[1] not in template_dirs
            or any(part in {"", ".", ".."} for part in parts)):
        return None
    source_template_rel = Path(*parts[2:])
    is_lite = source_template_rel.name.endswith(install_rules.LITE_SUFFIX)
    mapped_dest = (
        install_source._full_relpath_for_lite(source_template_rel) if is_lite else source_template_rel)
    if mapped_dest.as_posix() != dest_relpath or (weight == "full" and is_lite):
        return None
    git_relpath = "/".join(parts)
    return weight, Path(source_root).joinpath(*parts), git_relpath


def instance_owned_template_targets(
        dest_root: Path, source_root: Path) -> list[InstanceOwnedTemplateTarget]:
    """설치 하네스의 instance-owned 파일 전수(진입문서 ``none`` 포함).

    ``adapter_config_targets``는 실제 동기 채널이라 ``none``을 의도적으로 제외한다. 이 함수는
    반대로 *관측* 인벤토리이므로 ``INSTANCE_OWNED_ADAPTER_FILES`` 전량을 소비한다. 공존
    opencode+codex의 공유 ``AGENTS.md``는 최초 import와 같은 중립 소유 template(codex) 하나로
    접어 파일당 한 줄 계약을 지킨다.
    """
    dest_root = Path(dest_root)
    source_root = Path(source_root)
    coordinates = _recorded_instance_owned_template_coordinates(dest_root)
    names = install_receipt.installed_harnesses(dest_root, source_root)
    installed = set(names)
    neutral_owner_by_rel = {
        rel.as_posix(): owner
        for rel, (members, owner) in install_rules.NEUTRAL_SHARED_ENTRY_DOCS.items()
        if members <= installed
    }
    out: list[InstanceOwnedTemplateTarget] = []
    seen: set[str] = set()
    for harness in names:
        for relpath in sorted(install_rules.INSTANCE_OWNED_ADAPTER_FILES.get(harness, ())):
            owner = neutral_owner_by_rel.get(relpath, harness)
            if owner != harness:
                continue
            if relpath in seen:
                continue
            resolved = _resolve_recorded_template_coordinate(
                source_root, relpath, coordinates.get(relpath))
            weight, template, template_git_relpath = (
                resolved if resolved is not None else (None, None, None))
            out.append(InstanceOwnedTemplateTarget(
                relpath=relpath,
                harness=owner,
                mode=install_rules.ADAPTER_CONFIG_CHANNEL[owner][relpath],
                weight=weight,
                template=template,
                template_git_relpath=template_git_relpath,
            ))
            seen.add(relpath)
    return out


def _instance_owned_fallback_rev(dest_root: Path) -> str | None:
    """진입문서 등 파일별 원장 rev가 없는 경로의 직전 동기 세대."""
    try:
        text = install_files.read_dest_text(
            Path(dest_root), Path(".project_manager") / "local.conf")
    except (OSError, install_files.UnsafeDestPathError, UnicodeDecodeError):
        return None
    value = conf_schema.parse(text).get("upstream.rev", "").strip()
    return value or None


def _template_generation_size(old_lines: list[str], current_lines: list[str]) -> tuple[int, int, int]:
    """template 두 세대의 (+줄, -줄, 첫 차이 current 줄 번호)."""
    matcher = difflib.SequenceMatcher(a=old_lines, b=current_lines, autojunk=False)
    added = 0
    removed = 0
    first_diff: int | None = None
    for tag, old_start, old_end, current_start, current_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        if first_diff is None:
            first_diff = current_start + 1
        removed += old_end - old_start
        added += current_end - current_start
    return added, removed, first_diff or 1


# 소스 checkout 의 git object DB 를 읽는 두 관용구 — 세대 요약(`instance_owned_template_delta_lines`)
# 과 기준 diff(`adapter_config_reports`)가 **같은 읽기**를 공유한다(두 번째 git 경로 신설 0).
SOURCE_PREFIX_OK = "ok"


SOURCE_PREFIX_NOT_GIT = "not-git"          # 소스가 git checkout 이 아니다(세대 판정 비대상).


SOURCE_PREFIX_UNRESOLVED = "unresolved"    # 접두를 못 읽거나 비정상 형상(기준 해소 불가).


def _source_git_path_prefix(
        source_root: Path, runner: install_git.GitRunner) -> tuple[str, PurePosixPath]:
    """소스 checkout 의 **저장소 최상위 상대 접두** — (판정, 접두).

    영수증의 template 좌표는 `source_root` 상대지만 `show`/`ls-tree` 의 path 는 저장소 최상위
    상대다. source_root 가 상위 저장소의 하위 디렉터리면 접두 없는 조회가 모든 과거 파일을
    "없음" 으로 오판한다. 접두 자체를 해소하지 못하거나 비정상 형상(개행 포함·절대·`..`)이면
    전량 추가로 접지 않고 `SOURCE_PREFIX_UNRESOLVED` 로 내린다(판정 불가 ≠ 변경 없음).
    """
    git_rc, git_out = runner([
        "-C", str(source_root), "rev-parse", "--is-inside-work-tree",
    ])
    if git_rc != 0 and "not a git repository" in git_out.lower():
        return SOURCE_PREFIX_NOT_GIT, PurePosixPath("")
    if git_rc == 0 and git_out.strip().lower() != "true":
        return SOURCE_PREFIX_NOT_GIT, PurePosixPath("")
    prefix_rc, prefix_out = runner([
        "-C", str(source_root), "rev-parse", "--show-prefix",
    ])
    source_prefix = prefix_out.rstrip("\r\n") if prefix_rc == 0 else ""
    prefix_path = PurePosixPath(source_prefix)
    if (prefix_rc != 0 or "\n" in source_prefix or "\r" in source_prefix
            or prefix_path.is_absolute()
            or any(part in {".", ".."} for part in prefix_path.parts)):
        return SOURCE_PREFIX_UNRESOLVED, PurePosixPath("")
    return SOURCE_PREFIX_OK, prefix_path


def _baseline_rev_reachable(source_root: Path, rev: str, runner: install_git.GitRunner) -> bool:
    """그 rev 가 소스 object DB 에 실재하는 commit 인가 (`cat-file -e <rev>^{commit}`)."""
    rc, _out = runner([
        "-C", str(source_root), "cat-file", "-e", rev + "^{commit}",
    ])
    return rc == 0


TEMPLATE_BLOB_OK = "ok"


TEMPLATE_BLOB_ABSENT = "absent"            # 그 시점 tree 에 경로가 없었다(신규 파일·기존 0줄).


TEMPLATE_BLOB_UNRESOLVED = "unresolved"    # 판정 불가(git I/O·손상) — 전량 추가로 접지 않는다.


def _template_blob_at_rev(source_root: Path, rev: str, git_relpath: str,
                          runner: install_git.GitRunner) -> tuple[str, str]:
    """그 rev 시점 template 파일 내용 — (판정, 텍스트). 원장은 내용을 담지 않아 여기서 읽는다.

    `git show rev:path` 비0 하나만으로는 과거 경로 부재와 git I/O·timeout·저장소 손상을 구분할 수
    없다. 같은 commit tree 를 별도 조회해 **부재가 증명될 때만** 신규 파일로 본다(그 경우 텍스트는
    빈 문자열). tree 조회 실패 또는 경로가 있는데 blob show 만 실패한 경우는 해소 불가다.
    """
    rc, text = runner([
        "-C", str(source_root), "show", f"{rev}:{git_relpath}",
    ])
    if rc == 0:
        return TEMPLATE_BLOB_OK, text
    tree_rc, tree_out = runner([
        "-C", str(source_root), "ls-tree", "--name-only", "-z", rev, "--", git_relpath,
    ])
    tree_paths = {
        line
        for nul_chunk in tree_out.split("\0")
        for line in nul_chunk.splitlines()
        if line
    }
    if tree_rc != 0 or git_relpath in tree_paths:
        return TEMPLATE_BLOB_UNRESOLVED, ""
    return TEMPLATE_BLOB_ABSENT, ""
