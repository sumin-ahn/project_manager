"""갱신 manifest 의 하나의 규칙(설치된 하네스의 출하 manifest)·은퇴 보고 (층: app)."""
from __future__ import annotations

from pathlib import Path
import sys
import warnings
from pm_engine.domain import install_rules
from pm_engine.domain import manifest_rules
from pm_engine.infra import fs
from pm_engine.infra import install_receipt
from pm_engine.infra import install_source
from pm_engine.infra import manifest_store
from pm_engine.infra import repo_files
from pm_engine.infra import retired_paths


def shipped_manifest(effective_dest: Path, source_root: Path) -> dict:
    """이번 갱신이 계획하고 사본에 쓰는 manifest — 사본에 설치된 하네스의 출하 manifest 그대로다.

    사본 manifest 의 행은 계획에 쓰지 않는다. 행은 어떤 하네스가 설치됐는지만 알린다.

    - 핵심: self-prop 행(`.project_manager/engine.manifest`)이 가리키는 출하 manifest 에, 그것이 덮지
      않는 사본 행이 가리키는 하네스별 판을 더한다. 행은 `@source=templates/<판>/…` 이면 그 판을
      (상류에 그 판이 없어도 그 판이다), 아니면 그 판에만 있는 경로(`_variant_exclusive_paths`)에 걸칠
      때 그 판을 가리킨다. 아무 판도 정해지지 않으면 상류 루트 manifest 다(사본 manifest 가 없을 때
      포함).
    - 추가 하네스 구획: 구획 행이 가리키는 판의 구획을 그 판의 출하 manifest 에서 매번 다시 만든다
      (add-harness 와 같은 생성기 `install_source._added_harness_manifest_lines`). 핵심이 가진 경로는 뺀다.

    파일 존재는 하네스 판정에 쓰지 않는다. 정해지지 않은 판의 고유 경로가 디스크에 있으면 `frozen` 에
    담아 알리기만 한다. 사본에만 있던 행(`released`)은 관리에서 빠지고 파일은 그대로 남는다. 은퇴
    지시의 OLD 는 퇴역 단계가 다루므로 `released` 에서 뺀다. 출하 manifest 를 읽지 못하면 그 오류가
    그대로 올라간다.

    반환: entries(계획 행) · text(사본에 쓸 manifest 전문) · manifests(핵심에 쓴 출하 manifest 경로) ·
    released · conflicts(합집합 마커 충돌) · frozen([(판, 관측 경로, 고유 경로)]).
    """
    source_root = Path(source_root)
    dest_manifest = Path(effective_dest) / ".project_manager" / "engine.manifest"
    local_text = (fs.read_text_shared(dest_manifest, encoding="utf-8")
                  if dest_manifest.is_file() else "")
    core_rows = [row for row in map(manifest_rules._parse_manifest_line,
                                    manifest_rules._strip_added_harness_manifest_block(local_text).splitlines()) if row]
    added_harness_rows = [row for row in map(manifest_rules._parse_manifest_line,
                                     (manifest_rules._extract_added_harness_manifest_block(local_text) or "").splitlines())
                  if row]
    def shipped_of(name: str) -> Path:
        return source_root / "templates" / name / ".project_manager" / "engine.manifest"

    variant_paths = {path.parents[1].name: [str(entry).replace("\\", "/") for entry in manifest_store.read_manifest(path)]
                    for path in sorted(source_root.glob("templates/*/.project_manager/engine.manifest"),
                                       key=lambda path: path.as_posix())}
    exclusive = {name: _variant_exclusive_paths(paths, set().union(*(
        other for other_name, other in variant_paths.items() if other_name != name)))
        for name, paths in variant_paths.items()}

    def named(rows: list, covered: set[str]) -> list[str]:
        """덮이지 않은 행이 가리키는 판 — 처음 나온 순서."""
        names: list[str] = []
        for row in rows:
            rel = str(row).replace("\\", "/")
            if manifest_rules._path_owned_by(rel, covered):
                continue
            source = (row.source_rel or "").replace("\\", "/")
            names += [source.split("/")[1]] if source.startswith("templates/") else [
                name for name in exclusive
                if any(manifest_rules._paths_overlap(rel, path) for path in exclusive[name])]
        return list(dict.fromkeys(names))

    core = [source_root / manifest_rules._source_root_rel(row) for row in core_rows
            if str(row).replace("\\", "/") == manifest_rules._MANIFEST_SELF_REL]
    covered = {str(entry).replace("\\", "/") for path in core for entry in manifest_store.read_manifest(path)}
    core += [shipped_of(name) for name in named(core_rows, covered) if shipped_of(name) not in core]
    merged = manifest_store.merge_manifest_sources(
        core or [source_root / manifest_rules._MANIFEST_SELF_REL])
    core_paths = {str(entry).replace("\\", "/") for entry in merged["entries"]}
    harness_of = {template_dir: harness for harness, template_dirs in install_rules.HARNESS_TEMPLATE_DIRS.items()
                  for template_dir in template_dirs}
    added_harness_lines: dict[str, str] = {}
    for name in named(added_harness_rows, core_paths):
        adapter_dirs, root_doc = install_rules.ADD_HARNESS_ADAPTER[harness_of[name]]
        for line in install_source._added_harness_manifest_lines(
                shipped_of(name).parents[1], adapter_dirs, root_doc, core_paths):
            added_harness_lines.setdefault(line.split()[0], line)
    block = [added_harness_lines[rel] for rel in sorted(added_harness_lines)
             if not manifest_rules._path_owned_by(rel, core_paths)]
    text = merged["text"]
    if block:
        text = "\n".join([text.rstrip("\n"), "", manifest_rules._ADDED_HARNESS_MANIFEST_BEGIN, *block,
                          manifest_rules._ADDED_HARNESS_MANIFEST_END, ""])
    entries = [*merged["entries"], *map(manifest_rules._parse_manifest_line, block)]
    local = {str(row).replace("\\", "/") for row in [*core_rows, *added_harness_rows]}
    shipped = {str(entry).replace("\\", "/") for entry in entries}
    retired_old = retired_paths.declared_retired_olds(merged["paths"])
    frozen = []
    for name in exclusive:
        observed = [rel for rel in exclusive[name]
                    if not any(manifest_rules._paths_overlap(rel, path) for path in shipped)
                    and (Path(effective_dest) / rel).exists()]
        if observed:
            frozen.append((name, observed, exclusive[name]))
    return {
        "entries": entries,
        "text": text,
        "manifests": merged["paths"],
        "released": sorted(rel for rel in local - retired_old
                           if not any(manifest_rules._paths_overlap(rel, path) for path in shipped)),
        "conflicts": merged["conflicts"],
        "frozen": frozen,
    }


def _print_shipped_manifest_finding(shipped: dict) -> None:
    """출하 manifest 로 맞출 때 관리에서 빠지는 사본 행·미등재 판 관측·합집합 마커 충돌을 알린다.

    새로 받는 파일은 계획 줄(`[new]`)이 이미 보인다."""
    if shipped["released"]:
        print(f"→ engine.manifest 를 상류 출하본으로 맞춘다 — 상류 manifest 에 없는 사본 행 "
              f"{len(shipped['released'])}개는 관리에서 빠지고 파일은 그대로 둔다:")
        for rel in shipped["released"]:
            print(f"    - {rel}")
    for variant, observed, evidence_paths in shipped["frozen"]:
        _print_frozen_variant_warning(variant, observed, evidence_paths)
    if shipped["conflicts"]:
        print(
            "경고: 선택 manifest 중복 경로의 마커/@source 불일치 — 선언 순서상 첫 하네스별 판을 "
            f"우선함 ({len(shipped['conflicts'])}건): {', '.join(shipped['conflicts'])}",
            file=sys.stderr,
        )


def _print_frozen_variant_warning(variant: str, observed: list[str], evidence_paths: list[str]) -> None:
    """engine.manifest 가 가리키지 않는 하네스별 판의 고유 파일이 디스크에 있으면 등재 절차를 안내한다."""
    cli_variant = "claude" if variant == "claude_code" else variant
    print(
        "⚠️ 미등재 하네스별 판 파일 관측 — engine.manifest 가 가리키지 않는 하네스별 판"
        f"({variant}) 관리 경로 일부가 디스크에 있다 "
        f"({len(observed)}/{len(evidence_paths)}: {', '.join(observed)}). "
        "관측 경로는 **어느 동기 채널도 선언하지 않은 출하 파일**이라 갱신이 받지 않는다 "
        "(`add-harness`로 얹은 어댑터는 추가 하네스 절이 렌더물·엔진 파일을 모두 등재하므로 이 경고 "
        "대상이 아니다). 남는 경우는 절 없이 손으로 복사된 하네스별 판 트리, 사용자 stray 파일, "
        "아직 어느 하네스별 판도 선언하지 않은 신규 출하 경로다.\n"
        f"    `add-harness {cli_variant}`는 그 하네스 어댑터만 등재한다(엔진 전량 마이그레이션 아님).\n"
        "    완전 마이그레이션(등록 하네스별 판 전체; 관측 누락에도 안전하나 원치 않는 하네스별 판도 "
        "추가될 수 있으므로 dry-run 검토):\n"
        "      <manager>/pm-import.sh --into <project> --harness all --dry-run\n"
        "      <manager>/pm-import.sh --into <project> --harness all\n"
        "      cd <project> && ./pm-update.sh\n"
        "    재-import가 커스터마이즈된 CLAUDE.md/AGENTS.md를 템플릿 판으로 덮을 수 있으니, "
        "진입 문서 커스텀은 .pm_import_backups/<날짜>/ 백업에서 재병합하라.\n"
        f"    복구: 그 하네스를 정식으로 얹어 동기 채널을 만든다 — "
        f"`./pm-config.sh add-harness {cli_variant}` (upstream 이 로컬 경로가 아니면 "
        f"`--from <프레임워크 checkout>` 추가; Windows 는 `.\\pm-config.cmd`).\n"
        "    해당 파일이 stray라면 이 경고를 무시해도 된다.",
        file=sys.stderr,
    )


def _variant_exclusive_paths(entries: list, other_candidate_paths: set[str]) -> list[str]:
    """다른 어떤 하네스별 판에도 없는 이 판의 **고유 경로** (선언 순서 보존).

    `shipped_manifest` 가 행이 가리키는 판과 미등재 판 관측을 정할 때 쓴다. 고유성이 판정의 핵심이라
    단순 namespace 매칭으로 대체할 수 없다 — 여러 판이 함께 선언하는 cross-ns 경로(opencode 의
    `.claude/skills`)를 고유 경로로 세면 없던 하네스를 인스턴스에 만들어 낸다. manifest destination 은
    파일과 디렉터리 양쪽이므로 exact 문자열뿐 아니라 상·하위 포함도 중첩이다. 예를 들어 공용
    `.claude/skills`와 판 override `.claude/skills/pm-dev-delegate/SKILL.md`는 같은 설치 범위라
    후자를 고유 경로로 세지 않는다."""
    other_paths = {
        str(path).replace("\\", "/") for path in other_candidate_paths
    }
    paths = [str(entry).replace("\\", "/") for entry in entries]
    return [
        rel
        for rel in paths
        if not any(manifest_rules._paths_overlap(rel, other) for other in other_paths)
    ]


def _installed_entry_notation_manifests(
    effective_dest: Path,
    source_root: Path,
    upstream_manifests: list[Path],
) -> list[Path]:
    """core 선택에 더해 실제 설치된 add-harness 추가 하네스를 표기 context에만 포함한다.

    추가 하네스의 판은 계획 manifest 의 **핵심 합집합 밖**이다(구획으로 따로 만든다). 다만 공유
    wiki를 실제로 읽는 하네스 집합에서는 빠지면 안 되므로, 설치 하네스를 찾아 그 하네스별 판 manifest를
    context 입력에만 보탠다.

    판정은 **install_receipt.installed_harnesses 단일 진실**을 쓴다(구조 판정 사본 금지). 어댑터
    디렉터리+root-doc 실재만 보면 일반 프로젝트가 자기 용도로 가진 `.codex/`·`AGENTS.md`를 codex
    PM 설치로 오인해, PM 카드가 없는 인스턴스의 공유 wiki에 codex 표기(`$pm-bootstrap`)를 주입한다
    — pm_import(최초 설치·add-harness)와 pm_update(자기 갱신)가 같은 판정을 써야 표기가 어긋나지
    않는다.
    """
    paths = list(upstream_manifests)
    harnesses = install_receipt.installed_harnesses(effective_dest, source_root)
    template_registry = install_rules.HARNESS_TEMPLATE_DIRS
    seen = {Path(path).resolve() for path in paths}
    for harness in harnesses:
        template_dir = template_registry[harness][0]
        manifest = (
            Path(source_root) / "templates" / template_dir
            / ".project_manager" / "engine.manifest"
        )
        if not manifest.is_file():
            continue
        resolved = manifest.resolve()
        if resolved not in seen:
            paths.append(manifest)
            seen.add(resolved)
    return paths


# ── 상류 은퇴 파일 보고 (삭제 전파 없음·read-only 진단) ────────────────────────
# manifest 디렉토리 엔트리 동기는 **source 열거**라 추가·갱신만 한다(`_iter_files`) — 상류에서
# 은퇴한 파일은 채택자 dest 에 영구 잔존하는데 어떤 출력도 그 사실을 말하지 않았다. 삭제를
# 전파하지 않는 것 자체는 유지한다: dest 에만 있는 파일이 "은퇴 파일" 인지 "채택자 로컬 자산"
# 인지 가를 데이터가 없고(둘 다 source 부재라는 같은 신호), 구분 수단 없는 삭제는 자산 파괴다.
# 그래서 지우지 않고 **보고**만 하며, 그 구분 불가를 출력 문구에 명시한다.

def _retired_manifest_files(
    source_root: Path,
    manifest: list,
    effective_dest: Path,
    dest_map,
) -> list[str]:
    """manifest 디렉토리 엔트리 아래 dest 파일 중 대응 source 가 upstream 에 없는 relpath 들.

    판정 좌표는 dest→source **역방향** 매핑이다 — `_dest_relpath_for`(board-분리 dest remap)와
    `_remap_to_dest`(`@source` source-remap)를 되짚어 upstream 읽기 경로를 만들고 그 실재만 본다.
    dest 파일 열거는 공용 함수(`repo_files` OWNED = 추적 + 미추적-비ignore)이다. 직접
    rglob 하면 ignore 산출물(`__pycache__` 등)이 후보로 새어 보고가 노이즈에 묻힌다.

    ``dest_map`` 은 이번 계획이 훑은 출하 좌표(`plan(inventory_out=…)`) — 상류가 실제로 공급하는
    파일을 재판정 없이 걸러낸다. 그 밖의 제외:
      - `@target-owned` 엔트리 — upstream 에 source 가 없는 게 정상(타깃 고유 어댑터).
      - 더 구체적인 manifest 항목이 소유한 파일 — 그 항목 기준으로 판정된다(override 의미 보존).
      - **working tree 에 실재하지 않는 경로** — OWNED 열거는 git index 기반이라 이미 지웠지만
        commit 하지 않은 파일이 계속 후보로 남는다. "dest 에 잔존한다" 가 이 보고의 전제이므로
        디스크 실재를 다시 확인한다(없으면 보고할 잔존물 자체가 없다).

    **상류에서 통째로 사라진 등재도 이 채널이 본다**: dest 에 파일로 잔존하면 그 파일이, 디렉토리로
    잔존하면 그 아래 소유 파일 전부가 후보다. 실 sync 는 plan 의 missing 채널이 rc2 로 먼저 멈춰
    여기 도달하지 않지만, `--changes`(read-only)에는 그 채널이 없어 이 보고가 유일한 알림 자리다 —
    상류가 살아 있는 디렉토리만 스캔하면 "상류 삭제·rename (아래 보고 참조)" 헤더가 빈 포인터가
    되고, 통째 삭제라는 **가장 크게 잔존하는 경우**가 가장 조용해진다.

    ``dest_map`` 은 **dest 좌표만** 담아야 한다 — `@source` 매핑의 상류 좌표가 섞이면 우연히 같은
    문자열인 dest 잔존물이 "상류가 공급함" 으로 바뀌어 보고에서 사라진다(false negative).

    실패는 fail-soft(빈 목록) — 이 보고는 진단이라 sync 자체를 깨서는 안 된다.
    """
    source_root = Path(source_root)
    effective_dest = Path(effective_dest)
    planned = {
        str(coordinate).replace("\\", "/").strip("/") for coordinate in (dest_map or ())
    }
    dest_runner = repo_files._real_git_runner(effective_dest)

    retired: set[str] = set()
    for index, entry in enumerate(manifest):
        if manifest_rules._entry_target_owned_flag(entry):
            continue
        source_rel = manifest_rules._source_root_rel(entry).replace("\\", "/").strip("/")
        dest_rel = manifest_store._dest_relpath_for(
            str(entry), effective_dest).replace("\\", "/").strip("/")
        source_exists = (source_root / source_rel).exists()
        if not (effective_dest / dest_rel).is_dir():
            # 디렉토리가 아니면 하위 열거가 없다 — 상류에서 사라진 **파일 엔트리**만 후보다.
            #   상류가 공급하는 파일 엔트리는 계획이 갱신하므로 은퇴가 아니다.
            if (not source_exists
                    and (effective_dest / dest_rel).is_file()
                    and dest_rel not in planned):
                retired.add(dest_rel)
            continue
        # 상류 디렉토리가 **통째로 사라져도** 하위 열거를 건너뛰지 않는다 — 건너뛰면 그 아래 dest
        #   잔존물 전부가 보고에서 통째 누락된다(가장 크게 잔존하는 경우인데 가장 조용했다). 아래
        #   per-child source 실재 검사가 자연히 전량을 후보로 만든다. 제외 규칙 우선순위는 그대로다:
        #   `@target-owned` 는 이 루프에 들어오기 전에 이미 빠졌고(source 부재가 정상), 계획이
        #   공급하는 좌표(`planned`)와 dest 미실재 경로도 아래에서 계속 걸러진다.
        try:
            # 열거는 **등재 디렉토리 subtree 로 한정**한다 — dest 전체를 훑으면 비-git dest 의
            # filesystem 대체 동작이 채택자 프로젝트 전부를 순회한다(등재 밖은 애초에 판정 대상도 아님).
            # 비-git dest 의 대체 동작 경고는 여기서 삼킨다 — 진단 보고 하나 때문에 채택자에게 엔진
            # 내부 공용 함수 경고 원문을 노출할 이유가 없다(열거 자체는 대체 동작으로 정상 동작한다).
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", repo_files.RepoFilesFallbackWarning)
                dest_files = [
                    dest_entry.path.as_posix()
                    for dest_entry in repo_files.list_repo_owned_entries(
                        effective_dest,
                        dest_rel,
                        mode=repo_files.OWNED,
                        git_runner=dest_runner,
                    )
                ]
        except (OSError, RuntimeError, ValueError):
            continue
        prefix = dest_rel + "/"
        for rel in dest_files:
            if not rel.startswith(prefix) or rel in planned:
                continue
            if manifest_store._manifest_owner_index(manifest, rel, effective_dest) != index:
                continue
            if not (effective_dest / rel).is_file():
                continue  # index 에만 남은 삭제분 — dest 에 잔존물이 없으니 보고 대상이 아니다.
            source_candidate = source_rel + "/" + rel[len(prefix):]
            if not (source_root / source_candidate).exists():
                retired.add(rel)
    return sorted(retired)


# 은퇴 후보 나열 상한 — 상류가 디렉토리 하나를 통째로 재편하면 후보가 수백 건이 돼 다른 출력이
# 스크롤 밖으로 밀린다. 건수는 항상 정확히 알리고 나열만 자른다(정보 손실 0·"외 M건" 명시).
_RETIRED_REPORT_LIST_LIMIT = 20


def _print_retired_manifest_files(retired: list[str]) -> None:
    """은퇴 후보 보고 — 0 건이면 침묵(apply·dry-run·변경 0·`--changes` 네 경로 공용)."""
    if not retired:
        return
    print(
        f"⚠️ 상류 부재 파일 {len(retired)}건 — 동기는 지우지 않는다"
        "(채택자 로컬 자산과 구분 불가·수동 정리 판단):"
    )
    for rel in retired[:_RETIRED_REPORT_LIST_LIMIT]:
        print(f"    {rel}")
    remaining = len(retired) - _RETIRED_REPORT_LIST_LIMIT
    if remaining > 0:
        print(f"    … 외 {remaining}건 (전량은 engine.manifest 등재 경로를 직접 확인하라)")


