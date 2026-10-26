"""설치 공통 install_plan (app)."""
from __future__ import annotations

from pathlib import Path
from pm_engine.domain import install_rules
from pm_engine.infra import fs
from pm_engine.infra import install_files
from pm_engine.infra import install_source
from pm_engine.infra import manifest_store
from typing import Callable
import sys

def _check_ancestor_safe(dest_root: Path, dst: Path, checked: set[Path]) -> None:
    """dest_root 와 dst 사이의 조상 경로 컴포넌트가 안전하게 디렉토리화 가능한지 검증.

    이미 존재하는 조상 컴포넌트가 symlink 이거나 비-디렉토리 파일이면
    AncestorConflict 로 거부한다(plan 단계 — apply 부분 복사·외부 쓰기 전 차단). dest_root
    자신은 상위 가드가 처리하므로 *하위* 조상만 본다. checked 는 이미 검증한 조상 캐시
    (같은 디렉토리를 매 파일마다 재검사하지 않게 — 결정론·성능).
    """
    for ancestor in reversed(dst.parents):
        # dest_root 자신과 그 상위는 --into/--new 가드 소관 — 하위 조상만 검사.
        if ancestor == dest_root or dest_root not in ancestor.parents:
            continue
        if ancestor in checked:
            continue
        if ancestor.is_symlink():
            raise install_files.AncestorConflict(
                f"dst 조상 경로가 symlink 입니다: {ancestor}. 링크를 따라가면 프로젝트 밖에 "
                f"쓸 수 있어 거부합니다(비파괴). 해당 링크를 직접 옮기거나 제거한 뒤 다시 "
                f"시도하세요."
            )
        if ancestor.exists() and not ancestor.is_dir():
            raise install_files.AncestorConflict(
                f"dst 조상 경로에 디렉토리가 아닌 파일이 있습니다: {ancestor}. 그 안에 "
                f"디렉토리를 만들 수 없어 거부합니다(부분 복사 방지). 해당 파일을 직접 "
                f"옮기거나 제거한 뒤 다시 시도하세요(비파괴 — 자동 삭제하지 않습니다)."
            )
        checked.add(ancestor)


def plan_copy(
    template_roots: list[Path],
    dest_root: Path,
    backup_root: Path | None,
    weight: str = "full",
    *,
    backup_dir_name: str,
    git_safe: set | None = None,
    skip_existing_relpaths: set[Path] | None = None,
    include_relpath: Callable[[Path], bool] | None = None,
    source_overrides: dict[tuple[Path, Path], Path] | None = None,
) -> list[install_files.CopyAction]:
    """선택된 어댑터 트리들 → dest 복사 액션(여러 트리는 relpath 기준 병합).

    backup_root: None 이면 백업 안 함(--new — 빈 디렉토리 보장). 비-None 이면 기존 충돌
    파일을 *중앙 디렉토리* `backup_root/<relpath>` 로 백업(--into). 형제 `*.backup.<DATE>`(트리
    전역 분산) 대신 단일 디렉토리로 모은다.

    git_safe: git_safe_relpaths 의 반환 — '추적 중 & 미변경' relpath(posix) 집합 또는
    None. None 이면 git 판정 불가(비-git·오류) → 모든 충돌을 백업(보수적). 집합이면 그 안의
    relpath 는 git 이 복원 가능하므로 백업 없이 덮는다(git-safe skip — 액션 _git_safe_skip 표시).

    weight: 'full'(기본) 이면 `*.lite.md` 를 제외, 'lite' 면 `X.lite.md` 를
    dst `X.md` 로 rename 복사(같은 트리 full `X.md` 제외). _iter_source_files 가 이 관례를
    적용해 dst relpath 를 산출하므로, 아래 다중-tree 중복 판정·치환 범위는 모두 *dst relpath*
    위에서 일관되게 돈다(lite 모드에선 `X.md` 가 dst — 선택 트리들이 각자 lite 변종을 깐다).

    MF3: 여러 선택 트리에서 같은 relpath 가 중복되면(예: 공유 엔진), **내용이 같을
    때만** 조용히 skip 한다. 서로 다른 하네스가 같은 dest relpath 에 다른 bytes 를 공급하면
    첫 트리(registry 정규 순서)를 우선하되 stderr 경고를 남긴다. CLI 입력 순서는 집합 정규화에서
    소거되므로 충돌 결과에 영향을 주지 않는다. 이 우선순위는 결정적
    충돌 해소 정책이지 첫 트리가 나머지의 상위집합이라는 전제를 두지 않는다. 단,
    engine.manifest는 복사 뒤 ``_install_selected_manifest_union``이 선택 트리 선언의 합집합으로
    다시 쓰므로 이 첫-tree 충돌 경고 대상이 아니다. 공유 자동-load 진입문서의 하네스 공존은
    ``NEUTRAL_SHARED_ENTRY_DOCS`` 선언이 중립 source로 통일한다.
    """
    seen: dict[Path, tuple[Path, str]] = {}  # relpath → (채택된 src, 채택 트리명)
    skip_existing_relpaths = skip_existing_relpaths or set()
    source_overrides = source_overrides or {}
    actions: list[install_files.CopyAction] = []
    checked_ancestors: set[Path] = set()  # 검증 완료 조상 캐시(중복 검사 회피).
    for template_root in template_roots:
        for rel, src in install_source._iter_source_files(template_root, weight):
            # 같은 자동-load 진입 doc을 공유하는 하네스 조합은 선언된 중립 source로 병합한다.
            # key에 template_root까지 포함해, 미래의 제4 하네스가 같은 relpath를 쓰더라도 이
            # 그룹 밖 source를 무관하게 덮지 않는다.
            src = source_overrides.get((template_root, rel), src)
            # add_harness처럼 전체 template 중 일부 namespace만 배포할 때는, 범위 밖 파일의
            # conflict/backup/ancestor safety 검증도 만들지 않는다. 최종 action 필터보다 앞에 둬
            # 무관한 engine 파일이 backup root 검증을 발화시키지 않게 한다.
            if include_relpath is not None and not include_relpath(rel):
                continue
            if rel in seen:
                prev_src, prev_tree = seen[rel]
                if _same_bytes(prev_src, src):
                    # 공유 엔진 등 byte-identical — 한 번만 복사(정상).
                    continue
                if rel.as_posix() == ".project_manager/engine.manifest":
                    # 직후 선택 flavor manifest 합집합으로 교체된다. 첫 tree가 최종 결과라는
                    # 일반 충돌 문구를 내면 사용자에게 거짓을 말하므로 여기서는 경고하지 않는다.
                    continue
                # 내용이 다른 중복 — 첫 트리(우선) 채택을 명시적으로 경고.
                print(
                    f"경고: 선택 트리 중복 relpath 내용 불일치 — '{rel.as_posix()}' 는 "
                    f"registry 정규 순서상 첫 트리 '{prev_tree}' 것을 우선함. "
                    f"후순위 트리: '{template_root.name}'.",
                    file=sys.stderr,
                )
                continue
            seen[rel] = (src, template_root.name)
            dst = dest_root / rel
            # caller가 create-if-absent로 선언한 기존 파일은 일반 backup/action/ancestor safety
            # 계산 전에 action 자체를 만들지 않는다. instance-owned config는 backup이 있어도
            # overwrite 권한이 생기지 않으며, 손상된 backup root가 그 보존 분기까지 막아서도 안 된다.
            if rel in skip_existing_relpaths and (dst.exists() or dst.is_symlink()):
                continue
            # dst 조상(dest_root 하위)이 symlink·비-디렉토리 파일이면 거부 —
            #      링크 follow 로 프로젝트 밖 쓰기 / apply 중 mkdir 실패 부분복사 방지.
            _check_ancestor_safe(dest_root, dst, checked_ancestors)
            # dst 가 (symlink 아닌) 디렉토리면 파일 복사·백업 불가 — plan 에서
            #      거부(apply 부분 복사 전 차단). symlink 는 run() 이 링크 자체로 처리하므로 제외.
            if dst.is_dir() and not dst.is_symlink():
                raise install_files.FileVsDirConflict(
                    f"dst 위치에 기존 디렉토리가 있어 파일을 쓸 수 없습니다: {dst}. "
                    f"충돌하는 디렉토리를 직접 옮기거나 제거한 뒤 다시 시도하세요 "
                    f"(비파괴 — 사용자 디렉토리를 자동 삭제하지 않습니다)."
                )
            backup: Path | None = None
            git_safe_skip = False
            # MF1: symlink 도 충돌이다(깨진 링크면 dst.exists() 가 False 라 별도 검사). 충돌
            #      symlink 는 run() 이 링크 자체를 백업하고 일반 파일로 교체한다(대상 불변).
            is_conflict = dst.exists() or dst.is_symlink()
            if backup_root is not None and is_conflict:
                # git 이 추적 중이고 미변경인 파일은 git 이 복원 가능 → 백업 생략(덮기만).
                #   그 외(미추적·dirty·비-git·git 판정불가=git_safe None)는 중앙 디렉토리에 백업.
                #   ⚠️ symlink 충돌은 git_safe 와 무관하게 항상 백업한다 — ls-files 가 symlink 를
                #   추적 중이어도 run() 이 백업하는 것은 *링크 자체*(대상 파일 복제 아님)이고,
                #   git-safe skip 으로 무백업 덮으면 사용자 symlink 구성이 무흔적 손실되기 때문.
                if (git_safe is not None
                        and not dst.is_symlink()
                        and rel.as_posix() in git_safe):
                    git_safe_skip = True  # 백업 None — git 이 복원.
                else:
                    backup = backup_root / rel  # 중앙 디렉토리·relpath 미러링.
                    # 백업 target 의 *전체* 조상 체인
                    #   (`.pm_import_backups/<DATE>/<rel-parents>`) 이 안전한지 plan 단계 검증 —
                    #   일부 조상이 일반 파일/symlink 면 apply 중 mkdir 실패로 부분 복사가 잔존한다.
                    #   dst 조상 가드와 동일 helper·캐시 재사용(중앙 백업 자리 점유까지 한 번에 포착).
                    _check_ancestor_safe(dest_root, backup, checked_ancestors)
            # 계획 시점 관측(`is_conflict`·symlink 여부)을 액션에 넘긴다 — 적용 시점 재관측은
            #   계획 뒤 생성·삭제를 조용히 다른 동작으로 바꾼다(`CopyAction.run` 참조).
            action = install_files.CopyAction(src, dst, backup, dest_root=dest_root,
                                existed=is_conflict, is_symlink=dst.is_symlink(), backup_dir_name=backup_dir_name)
            action._git_safe_skip = git_safe_skip
            actions.append(action)
    return actions


def _same_bytes(a: Path, b: Path) -> bool:
    """두 파일의 바이트 내용이 동일한가 (중복 relpath 충돌 판정용)."""
    try:
        return fs.read_bytes_shared(a) == fs.read_bytes_shared(b)
    except OSError:
        return False


def _prepare_selected_manifest_union(template_roots: list[Path]) -> dict | None:
    """선택 template tree의 manifest 합집합을 읽기 전용으로 사전 검증한다.

    ``plan_copy``와 동일한 ``template_roots`` 순서를 단일 선택 트리로 소비한다. 경로/하네스별
    예외는 없고 각 트리의 manifest 선언을 합치므로, 향후 임의 조합도 같은 집합 의미를 그대로
    재사용할 수 있다. 복사 apply 전에 호출해 후순위 manifest 부재/파싱·병합 실패가 부분 설치를
    남기지 않게 한다. 단일 트리는 합집합 write가 불필요하므로 None을 반환한다.
    """
    manifest_paths = [
        root / ".project_manager" / "engine.manifest"
        for root in template_roots
    ]
    if len(manifest_paths) < 2:
        return None
    return manifest_store.merge_manifest_sources(manifest_paths)


def _warn_selected_manifest_union_conflicts(merged: dict | None) -> None:
    """manifest 합집합의 실제 marker 충돌만 stderr에 표면화한다.

    서로 다른 경로를 선언한 정상 합집합은 ``conflicts``가 비어 완전 무소음이다. 같은 관리 경로의
    @render/@target-owned/@source 의미가 다를 때만 merge 결과가 넣은 경로를 소비한다.
    """
    conflicts = [] if merged is None else merged.get("conflicts", [])
    if not conflicts:
        return
    print(
        "경고: 선택 manifest 중복 경로의 marker/@source 불일치 — registry 정규 순서상 "
        f"첫 flavor를 우선함 ({len(conflicts)}건): {', '.join(conflicts)}",
        file=sys.stderr,
    )


def _selected_entry_doc_source_overrides(
    source_root: Path,
    selected_harnesses: tuple[str, ...] | list[str],
    weight: str = "full",
) -> dict[tuple[Path, Path], Path]:
    """공유 자동-load 진입 doc의 조합별 중립 source override를 만든다.

    정책은 ``NEUTRAL_SHARED_ENTRY_DOCS`` 선언만 소비한다. 그룹 전원이 선택됐을 때만 발화하므로
    단일 하네스의 full/lite 선택은 기존 동작을 보존한다.
    """
    selected = set(selected_harnesses)
    overrides: dict[tuple[Path, Path], Path] = {}
    for rel, (members, neutral_harness) in install_rules.NEUTRAL_SHARED_ENTRY_DOCS.items():
        if not members <= selected:
            continue
        neutral_dirs = install_rules.HARNESS_TEMPLATE_DIRS[neutral_harness]
        if len(neutral_dirs) != 1:
            raise RuntimeError(
                f"중립 진입문서 소유 하네스 {neutral_harness!r}가 단일 template tree가 아닙니다."
            )
        neutral_root = source_root / "templates" / neutral_dirs[0]
        neutral_source = neutral_root / rel
        if weight == "lite":
            lite_source = neutral_source.with_name(
                f"{neutral_source.stem}.lite{neutral_source.suffix}"
            )
            # lite 변종 부재는 선언된 full 호환 폴백이다. 현행 중립 트리에 변종이 없어 정상
            # 조합마다 경고하면 기존 무소음 계약을 깨므로, 실제 선택 source로만 관측 가능하게 둔다.
            if lite_source.is_file() and not lite_source.is_symlink():
                neutral_source = lite_source
        if not neutral_source.is_file() or neutral_source.is_symlink():
            raise FileNotFoundError(
                f"공존용 중립 진입문서 없음/비정상 파일: {neutral_source}"
            )
        for member in members:
            for dirname in install_rules.HARNESS_TEMPLATE_DIRS[member]:
                overrides[(source_root / "templates" / dirname, rel)] = neutral_source
    return overrides
