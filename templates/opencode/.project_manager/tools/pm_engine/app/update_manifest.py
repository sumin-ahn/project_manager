"""manifest 자기치유·flavor·guest·은퇴 소유 조정 (층: app)."""
from __future__ import annotations

from pathlib import Path
from pathlib import PurePosixPath
import sys
import warnings
from pm_engine.app import upstream_changes
from pm_engine.domain import install_rules
from pm_engine.domain import manifest_rules
from pm_engine.infra import fs
from pm_engine.infra import install_receipt
from pm_engine.infra import install_source
from pm_engine.infra import manifest_store
from pm_engine.infra import repo_files
from pm_engine.infra import retired_paths


def _dest_guest_manifest_entries(effective_dest: Path) -> list[manifest_rules.ManifestEntry]:
    """dest engine.manifest 의 add-harness guest 절 행 — 읽기 실패·절 부재는 빈 목록.

    절 안의 한 줄이 **전파 방식**을 스스로 말한다: `@render` 행은 어댑터 렌더물(재렌더),
    비-`@render` 행은 엔진 파일(byte-copy). 두 종류 다 update 채널이 전파하되 방식이 갈리므로,
    경로만이 아니라 마커까지 필요하다 — 파싱해서 돌려준다(파서는 `_parse_manifest_line` 공유)."""
    manifest_file = Path(effective_dest) / ".project_manager" / "engine.manifest"
    try:
        block = manifest_rules._extract_guest_manifest_block(fs.read_text_shared(manifest_file, encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return []
    if not block:
        return []
    entries = [manifest_rules._parse_manifest_line(line) for line in block.splitlines()]
    return [entry for entry in entries if entry is not None]


def _guest_declared_flavors(guest_entries: list) -> list[str]:
    """guest 절 행의 `@source=templates/<flavor>/…` provenance — flavor 이름(최초 등장 순서).

    기록된 출처는 추론보다 항상 정확하므로 **선언된 flavor 는 추론 대상에서 뺀다**. 다만 절 전체를
    "선언됨" 으로 보지는 않는다 — 한 절에 새 세대(provenance 有)와 구 세대(렌더물 행만)가 공존하는
    **혼재 코호트**가 실재하기 때문이다(하네스를 순차로 얹고 하나만 refresh 한 인스턴스)."""
    flavors: list[str] = []
    for entry in guest_entries:
        source_rel = (getattr(entry, "source_rel", None) or "").replace("\\", "/")
        if not source_rel.startswith(upstream_changes._FLAVOR_SOURCE_PREFIX):
            continue
        parts = PurePosixPath(source_rel).parts
        if len(parts) < 3 or parts[1] in {"", ".", ".."}:
            continue
        if parts[1] not in flavors:
            flavors.append(parts[1])
    return flavors


def _flavor_manifest_entries_by_name(source_root: Path) -> dict[str, list]:
    """`templates/*/…/engine.manifest` → {flavor 디렉토리명: entries} (읽기 실패는 제외·fail-soft)."""
    out: dict[str, list] = {}
    for candidate in sorted(
        Path(source_root).glob("templates/*/.project_manager/engine.manifest"),
        key=lambda path: path.as_posix(),
    ):
        try:
            out[candidate.parents[1].name] = manifest_store.read_manifest(candidate)
        except (OSError, UnicodeError, ValueError):
            continue
    return out


def _infer_guest_flavors(source_root: Path, guest_paths: set[str]) -> list[str]:
    """guest 절이 담은 경로에서 flavor 를 **배타 경로 증거**로 추론한다(provenance 미해소분용).

    증거 = 그 flavor 에만 있는 경로(`_flavor_exclusive_paths`·frozen 판정과 같은 헬퍼)를 guest 절이
    실제로 담고 있는가. 단순 namespace 매칭으로 대체하면 여러 flavor 가 함께 선언하는 cross-ns 행
    (codex host + opencode guest 의 `.claude/skills`)이 claude 로 오인돼 **없던 인스턴스에 파일을
    만든다** — 파생 결과가 곧 파일 생성 권한이므로 거짓양성이 비파괴 계약을 깬다. 같은 이유로
    install receipt(존재-추론)도 쓰지 않는다."""
    entries_by_flavor = _flavor_manifest_entries_by_name(source_root)
    if not entries_by_flavor:
        return []
    paths_by_flavor = {
        flavor: {str(entry).replace("\\", "/") for entry in entries}
        for flavor, entries in entries_by_flavor.items()
    }
    inferred: list[str] = []
    for flavor, entries in entries_by_flavor.items():
        other_paths: set[str] = set()
        for other, paths in paths_by_flavor.items():
            if other != flavor:
                other_paths |= paths
        if guest_paths & set(_flavor_exclusive_paths(entries, other_paths)):
            inferred.append(flavor)
    return inferred


def _guest_engine_backfill_entries(
        effective_dest: Path, source_root: Path,
        guest_entries: list) -> tuple[list, list[str]]:
    """dest guest 절이 담아야 할 **엔진 행**을 upstream flavor 에서 재파생 — (엔트리, 해소 flavor).

    파생은 `infra.install_source` 의 단일 생성기(`_guest_manifest_lines` — 복사 술어 `_in_adapter_namespace` 를
    그대로 태운다)를 호출해 얻고 비-`@render` 행만 남긴다. 렌더물 행은 절에 이미 있고(add-harness
    가 등재) 재렌더로 전파되므로 백필 대상이 아니며, 생성기를 공유하므로 "등재 ⊆ 복사" 가 여기서도
    상속된다.
    guest 절이 없거나(비-add-harness) flavor 를 해소할 수 없으면 빈 목록(무동작·현행 거동).

    flavor 해소 = **선언 ∪ 미선언분 추론**이다. 한 절에 두 하네스가 공존하고 그 중 하나만 새 세대로
    refresh 된 **혼재 코호트**에서, 선언이 하나라도 있으면 추론을 통째로 끄는 판정은 구 세대 쪽 엔진
    행을 영구 미등재로 남긴다(정확히 이 티켓이 닫으려는 동결). 추론은 선언되지 않은 flavor 에 대해
    서만 돌고 판정 자체는 배타 경로 증거 그대로라, cross-ns 오탐 가드는 손대지 않는다."""
    if not guest_entries:
        return [], []
    guest_paths = {str(entry).replace("\\", "/") for entry in guest_entries}
    declared = _guest_declared_flavors(guest_entries)
    flavors = [*declared, *(
        flavor for flavor in _infer_guest_flavors(source_root, guest_paths)
        if flavor not in declared
    )]
    if not flavors:
        return [], []
    dest_manifest = Path(effective_dest) / ".project_manager" / "engine.manifest"
    try:
        dest_owned = manifest_rules._core_manifest_paths(fs.read_text_shared(dest_manifest, encoding="utf-8"))
        harness_by_flavor = {
            template_dir: harness
            for harness, template_dirs in install_rules.HARNESS_TEMPLATE_DIRS.items()
            for template_dir in template_dirs
        }
    except Exception as exc:  # noqa: BLE001 — 파생 실패는 현행 거동 유지(sync 자체는 계속).
        # 조용히 접지 않는다 — 파생이 꺼지면 guest 엔진 파일이 그 실행에서 다시 동결되는데,
        #   출력이 없으면 "원래 갱신 대상이 아니다" 와 구분되지 않는다.
        print(f"note: guest 엔진 행 파생을 건너뛴다(fail-soft·엔진 동기는 계속): {exc}",
              file=sys.stderr)
        return [], []
    entries: list = []
    seen: set[str] = set()
    resolved: list[str] = []
    for flavor in flavors:
        harness = harness_by_flavor.get(flavor)
        template_root = Path(source_root) / "templates" / flavor
        if harness not in install_rules.ADD_HARNESS_ADAPTER:
            continue
        if not (template_root / ".project_manager" / "engine.manifest").is_file():
            continue
        adapter_dirs, root_doc = install_rules.ADD_HARNESS_ADAPTER[harness]
        try:
            lines = install_source._guest_manifest_lines(
                template_root, adapter_dirs, root_doc, dest_owned)
        except Exception as exc:  # noqa: BLE001 — 한 flavor 실패가 나머지를 막지 않는다.
            print(f"note: guest flavor {flavor} 의 엔진 행 파생 실패(그 flavor 만 건너뛴다): {exc}",
                  file=sys.stderr)
            continue
        resolved.append(flavor)
        for line in lines:
            entry = manifest_rules._parse_manifest_line(line)
            if entry is None or manifest_rules._entry_render_flag(entry):
                continue
            path = str(entry).replace("\\", "/")
            if path in seen:
                continue
            seen.add(path)
            entries.append(entry)
    return entries, resolved


def detect_manifest_skew(
    local_manifest: list,
    source_root: Path,
    *,
    upstream_manifest: Path | None = None,
    upstream_manifests: list[Path] | None = None,
) -> tuple[str, list[str]]:
    """upstream engine.manifest ↔ 로컬(sync 에 쓰인) manifest 대조 — 신규 등재분 탐지.

    로컬 manifest 가 구형이면 `pm_update` 는 로컬 등재분만 복사해 upstream 이 새로 등재한 엔진
    경로(신규 등재분)가 도달하지 않는데, upstream.rev baseline 은 무조건 최신으로 갱신돼
    drift-lint 가 "최신"으로 침묵한다(구형 identity_args 잔존 →
    pm_handoff AttributeError). 이 함수는 그 skew 를 **탐지만** 한다 — baseline 억제/경고는
    호출부(main)가, 신규 등재분 실제 도달(자기치유)은 이 맡는다(분리: 탐지는 무해).

    `local_manifest` 는 실 sync 가 쓴 manifest(resolve_manifest_for_dest 산출 — dest 우선·없으면
    source). 대조 upstream manifest 는 `upstream_manifests` 전체가 있으면 선언 순서 합집합을,
    아니면 `upstream_manifest` 인자(있으면)를, 둘 다 없으면 source_root 의 root engine.manifest 를
    읽는다. **flavor-correct 통일**selfheal 이 채택자 manifest 선언을 따라 선택 flavor 전체를
    해소하므로, main 은 *그 동일 경로 목록*을 넘겨 두 기전(탐지 / 승격)의 대조 기준을 정합시킨다.
    첫 manifest만 대조하면 diverged 로컬 + 후순위 flavor 신규 경로가 이번 실행에 도달하지 않아도
    in_sync로 오판하고 baseline이 전진한다.

    단일 flavor에서 self-prop `@source` 를 무시하면
    flavor 채택자가 치유 후에도 root-only 경로(`.claude/agents` 등)를 skew 오탐해 baseline 이 억제된다.
    인자 미주입(직접 호출·레거시)은 root 폴백(후방호환). 두 집합의 순수 경로(마커 제외·ManifestEntry
    가 이미 떼어냄·str(e))를 비교해 upstream 에만 있는 경로를 신규 등재분으로 본다 — 로컬에서 제거된
    경로(local−upstream)는 관심 밖(신규 도달 누락만 차단 대상).

    반환 (status, new_entries):
      - ('upstream_missing', []) : upstream engine.manifest 부재/읽기 실패(구 upstream) — fail-soft.
      - ('in_sync', [])          : 신규 등재분 0(정합) — baseline 갱신 진행.
      - ('skew', [<path>…])      : 로컬에 없는 upstream 등재 경로 존재 — baseline 억제 대상(정렬).
    """
    try:
        if upstream_manifests:
            upstream_entries = manifest_store.merge_manifest_sources(
                [Path(path) for path in upstream_manifests]
            )["entries"]
        else:
            if upstream_manifest is None:
                upstream_manifest = Path(source_root) / ".project_manager" / "engine.manifest"
            upstream_entries = manifest_store.read_manifest(upstream_manifest)
    except (FileNotFoundError, OSError, ValueError):
        return "upstream_missing", []
    local_paths = {str(e) for e in local_manifest}
    new_entries = sorted({str(e) for e in upstream_entries} - local_paths)
    return ("skew", new_entries) if new_entries else ("in_sync", [])


def _print_manifest_skew_finding(
    status: str, new_entries: list[str], *, dry_run: bool = False
) -> None:
    """detect_manifest_skew 결과를 사람이 읽을 형태로 출력(loud 경고).

    - 'skew'            : loud 경고 + 신규 등재 경로 목록(reconcile 필요 surface).
    - 'upstream_missing': fail-soft 경고 1줄(구 upstream·부재 — 대조 생략·현행 유지).
    - 'in_sync'         : dry-run 에서만 정합 표시(실 sync 는 조용히 baseline 갱신으로 진행).
    - 'skipped'         : 무출력 — --target(엔진 export) 경로는 skew 대조 비발화(현행 거동).

    baseline 억제/갱신 자체는 호출부(main)가 status 로 결정한다 — 이 함수는 출력만.
    """
    if status == "skew":
        print(
            f"⚠️  manifest skew — upstream engine.manifest 에 등재됐으나 로컬 manifest 에 없는 "
            f"신규 경로 {len(new_entries)}건(이번 sync 로 도달하지 않음·manifest reconcile 필요):"
        )
        for path in new_entries:
            print(f"    + {path}")
        print(
            "    참고: legacy 보존 모드에서는 대조 기준이 표준판이라 .claude/* 같은 무관 flavor "
            "경로가 포함될 수 있다."
        )
    elif status == "upstream_missing":
        print(
            "note: upstream engine.manifest 를 읽을 수 없어(구 upstream·부재) manifest 정합 "
            "대조를 건너뛴다(fail-soft·현행 유지)."
        )
    elif status == "in_sync" and dry_run:
        print("manifest 정합 — upstream 신규 등재분 0(baseline 갱신 진행 예정).")


def _selfprop_upstream_rel(local_entries: list) -> str:
    """채택자 로컬 manifest 의 self-prop 엔트리(`.project_manager/engine.manifest`)를 따라 flavor-correct
    upstream manifest 의 source-root 상대 *읽기* 경로를 낸다(codex MF).

    claude_code/opencode 채택자의 self-prop 는 `@source=templates/<harness>/.project_manager/
    engine.manifest` 라, 그 @source(=_source_root_rel)가 같은 flavor upstream manifest 를 가리킨다.
    self-prop 엔트리가 없거나 bare(@source 부재)면 root manifest(`_MANIFEST_SELF_REL`·현행 폴백).
    이로써 flavor↔flavor 비교가 성립해 root(bare) 승격이 flavor manifest 를 클로버하지 않는다.
    """
    for entry in local_entries:
        if str(entry) == manifest_rules._MANIFEST_SELF_REL:
            return manifest_rules._source_root_rel(entry)  # @source 있으면 그 경로·없으면 str(entry)=_MANIFEST_SELF_REL
    return manifest_rules._MANIFEST_SELF_REL


def _print_frozen_flavor_warning(
    flavor: str,
    observed: list[str],
    evidence_paths: list[str],
    *,
    declared_manifest: bool,
) -> None:
    """자동 승격할 수 없는 타 flavor 일부 관측을 동일 migration 절차로 안내한다."""
    cli_flavor = "claude" if flavor == "claude_code" else flavor
    if declared_manifest:
        lead = (
            "⚠️ 미등재 flavor 파일 관측 — @source 선택 선언이 있는 manifest와 "
            f"선언되지 않은 타 flavor({flavor}) 관리 경로 일부가 함께 관측됐다 "
        )
    else:
        lead = (
            "⚠️ 미등재 flavor 파일 관측 — @source 선언이 없는 legacy manifest와 "
            f"타 flavor({flavor}) 관리 경로 일부가 함께 관측됐다 "
        )
    print(
        lead
        + f"({len(observed)}/{len(evidence_paths)}: {', '.join(observed)}). "
        "관측 경로는 **어느 동기 채널도 선언하지 않은 출하 파일**이라 자동 자기치유하지 않는다 "
        "(`add-harness`로 얹은 어댑터는 guest 절이 렌더물·엔진 파일을 모두 등재하므로 이 경고 "
        "대상이 아니다). 남는 경우는 절 없이 손으로 복사된 flavor 트리, 사용자 stray 파일, "
        "아직 어느 flavor도 선언하지 않은 신규 출하 경로다.\n"
        f"    `add-harness {cli_flavor}`는 그 하네스 어댑터만 등재한다(엔진 전량 마이그레이션 아님).\n"
        "    완전 마이그레이션(등록 flavor 전체; 관측 누락에도 안전하나 원치 않는 flavor도 "
        "추가될 수 있으므로 dry-run 검토):\n"
        "      <manager>/pm-import.sh --into <project> --harness all --dry-run\n"
        "      <manager>/pm-import.sh --into <project> --harness all\n"
        "      cd <project> && ./pm-update.sh\n"
        "    재-import가 커스터마이즈된 CLAUDE.md/AGENTS.md를 템플릿 판으로 덮을 수 있으니, "
        "진입 문서 커스텀은 .pm_import_backups/<날짜>/ 백업에서 재병합하라.\n"
        f"    복구: 그 하네스를 정식으로 얹어 동기 채널을 만든다 — "
        f"`./pm-config.sh add-harness {cli_flavor}` (upstream 이 로컬 경로가 아니면 "
        f"`--from <프레임워크 checkout>` 추가; Windows 는 `.\\pm-config.cmd`).\n"
        "    해당 파일이 stray라면 이 경고를 무시해도 된다.",
        file=sys.stderr,
    )


def _flavor_exclusive_paths(entries: list, other_candidate_paths: set[str]) -> list[str]:
    """다른 어떤 후보 flavor 에도 없는 이 flavor **배타 경로** (선언 순서 보존).

    두 소비자가 공유한다(판정 사본 금지): frozen evidence(`_frozen_flavor_evidence`)와 legacy
    guest 절의 flavor provenance 추론(`_infer_guest_flavors`). 배타성이 판정의 핵심이라 단순
    namespace 매칭으로 대체할 수 없다 — 여러 flavor 가 함께 선언하는 cross-ns 경로(opencode 의
    `.claude/skills`)를 배타 증거로 세면 없던 flavor 를 인스턴스에 만들어 낸다. manifest destination은
    파일과 디렉터리 양쪽이므로 exact 문자열뿐 아니라 상·하위 포함도 중첩이다. 예를 들어 공용
    `.claude/skills`와 flavor override `.claude/skills/pm-dev-delegate/SKILL.md`는 같은 설치 표면이라
    후자를 배타 증거로 세지 않는다."""
    other_paths = {
        str(path).replace("\\", "/") for path in other_candidate_paths
    }
    paths = [str(entry).replace("\\", "/") for entry in entries]
    return [
        rel
        for rel in paths
        if not any(manifest_rules._paths_overlap(rel, other) for other in other_paths)
    ]


def _frozen_flavor_evidence(
    entries: list,
    other_candidate_paths: set[str],
    local_core_paths: set[str],
    guest_paths: set[str],
) -> list[str] | None:
    """다른 모든 후보에는 없는 배타적 flavor 경로의 frozen evidence를 계산한다.

    guest 절이 소유한 경로는 **절 자체가 소유를 선언**했으므로(미등재 flavor 유입이 아니다)
    evidence에서 **개별로** 뺀다.
    flavor 고유 경로가 전부 guest 소유일 때만 ``None``(그 flavor 전체가 절의 선언 관할)이고,
    그 밖의 guest 밖 고유 경로는 남겨 frozen 판정에 쓴다. ``add_harness``는 항상 guest 행을
    등재하므로, guest 소유 하나로 flavor 전체를 버리면 frozen 상태에 도달하는 유일한 경로가 곧
    탐지기를 끈다(add-harness 채택자에게 경고가 구조적으로 발화하지 못함).

    host core 등재도 guest 와 **같은 소유권 판정**(`_path_owned_by`·경로-포함)으로 뺀다. manifest
    등재는 파일과 디렉터리 양쪽이라 exact 문자열 대조는 `.claude/skills` 디렉터리 등재가 이미 덮는
    `.claude/skills/pm-dev-delegate/SKILL.md` 를 "어느 동기 채널도 선언하지 않은 출하 파일" 로
    분류한다(claude 단독 채택자 오탐). 어느 core 등재로도 안 덮이는 진짜 stray 는 그대로 남는다.
    """
    unique_paths = _flavor_exclusive_paths(entries, other_candidate_paths)
    unique_paths = [rel for rel in unique_paths if not manifest_rules._path_owned_by(rel, guest_paths)]
    if not unique_paths:
        return None
    return [rel for rel in unique_paths if not manifest_rules._path_owned_by(rel, local_core_paths)]


def _print_legacy_nonmatch_warning(
    local_core_paths: set[str],
    observed_by_flavor: list[tuple[str, list[str], int]],
) -> None:
    """exact-match가 아닌 legacy 형상을 무변경 유지하며 완전 재-import 절차를 loud하게 낸다."""
    if observed_by_flavor:
        shapes = "; ".join(
            f"{flavor} {len(observed)}/{evidence_count}"
            + (f" ({', '.join(observed)})" if observed else "")
            for flavor, observed, evidence_count in observed_by_flavor
        )
    else:
        shapes = "배타적 flavor 경로 관측 0"
    print(
        "⚠️ legacy manifest 형상 — @source 선언이 없는 legacy engine.manifest의 "
        "core 경로 집합이 현행 flavor 후보 중 정확히 하나와 완전 일치하지 않는다. "
        f"관측 형상: 로컬 core {len(local_core_paths)}행; {shapes}.\n"
        "    로컬 manifest는 그대로 사용한다(자동 flavor 승격·행 제거·치유 0).\n"
        "    `add-harness`는 그 하네스 어댑터만 등재한다(엔진 전량 마이그레이션 아님).\n"
        "    완전 마이그레이션(등록 flavor 전체; 관측 0개·누락에도 안전하나 원치 않는 flavor도 "
        "추가될 수 있으므로 dry-run 검토):\n"
        "      <manager>/pm-import.sh --into <project> --harness all --dry-run\n"
        "      <manager>/pm-import.sh --into <project> --harness all\n"
        "      cd <project> && ./pm-update.sh\n"
        "    재-import가 커스터마이즈된 CLAUDE.md/AGENTS.md를 템플릿 판으로 덮을 수 있으니, "
        "진입 문서 커스텀은 .pm_import_backups/<날짜>/ 백업에서 재병합하라.\n"
        "    관측 파일이나 manifest 행이 사용자 stray/커스텀이면 이 경고를 무시해도 된다.",
        file=sys.stderr,
    )


def _selected_upstream_manifests(
    effective_dest: Path,
    source_root: Path,
    local_entries: list,
    local_text: str,
    guest_backfill_paths: set[str] | None = None,
) -> tuple[list[Path], bool]:
    """채택자에 실제 설치된 flavor들의 upstream manifest를 선택 순서대로 해소한다.

    설치 manifest의 ``@source=templates/<flavor>/...`` 선언이 있으면 그 flavor 집합만 신뢰한다.
    파일 존재는 사용자 파일/PM 홈의 어댑터 사본과 구별할 수 없으므로 선언이 하나라도 있으면
    존재-휴리스틱을 전혀 타지 않는다. 선언 순서는 manifest 행의 최초 출현 순서이고, 첫 flavor
    우선 + 후속 선언 순서라는 합집합 우선순위를 그대로 보존한다.

    ``@source`` flavor 선언이 전혀 없는 구 manifest는 로컬 core 경로 집합이 **정확히 한 후보와
    완전 일치**할 때만 그 후보를 primary로 고른다. 단 strict retired-path 선언의 검증된 역상
    ``(upstream - NEW) ∪ OLD``도 exact-match 후보로 인정한다. 부분집합, 존재 경로, 이름 추측,
    최소 초과집합/tiebreak는 사용하지 않는다. 완전 일치가 아니면 로컬 manifest를 그대로 계획에
    사용하고, frozen/stray를 구분할 수 없다는 진단과 검증된 완전 재-import 절차만 낸다.

    선언이 하나라도 있으면 존재-휴리스틱에 의한 자동 선택은 비발화한다. 선언되지 않은 flavor의
    관리-고유 경로 일부가 보이면 같은 frozen/stray 마이그레이션 경고만 내고 자동 승격하지 않는다.
    선언된 후순위 manifest가 부재해도 선택 목록에서 버리지 않아 호출부가 로컬 union을 유지하며
    경고하고, 해소 불가 선언은 primary만 유지한 채 경고한다.

    add-harness guest 절은 **절이 소유를 선언한 경로**라 core 합집합(flavor 선택) 승격 근거가 되지
    않는다 — 그 경로가 존재한다는 사실은 host core 가 그 flavor 라는 증거가 아니다(전파 자체는
    update 채널이 절의 선언대로 한다). guest가 소유한 경로만 존재해 후보가 된 flavor는 제외한다.

    ``guest_backfill_paths``(이번 실행이 파생한 guest 엔진 행)도 그 guest 소유 집합에 합친다. 절
    텍스트만 보면 legacy 코호트 **첫 실행**에서 같은 run 이 "이 파일들은 어떤 채널도 없다" 고 경고한
    직후 그 파일들을 등재·동기하는 자기모순 출력이 난다 — 파생이 실제로 채널을 만드므로 경고 대상이
    아닌 게 참이다. 파생 경로는 host core 를 차감하고 뽑히므로 core 판정과 겹치지 않는다.
    """
    source_root = Path(source_root)
    primary = source_root / _selfprop_upstream_rel(local_entries)
    candidates = sorted(
        source_root.glob("templates/*/.project_manager/engine.manifest"),
        key=lambda p: p.as_posix(),
    )
    candidate_by_flavor = {
        candidate.parents[1].name: candidate for candidate in candidates
    }
    guest_block = manifest_rules._extract_guest_manifest_block(local_text)
    guest_paths = {
        ln.split()[0].replace("\\", "/")
        for ln in guest_block.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    } if guest_block else set()
    guest_paths |= {
        str(path).replace("\\", "/") for path in (guest_backfill_paths or ())
    }
    local_core_paths = {
        str(entry).replace("\\", "/")
        for entry in local_entries
        if not manifest_rules._path_owned_by(str(entry).replace("\\", "/"), guest_paths)
    }

    primary_flavor = next(
        (flavor for flavor, candidate in candidate_by_flavor.items() if candidate == primary),
        None,
    )
    source_declarations = [
        getattr(entry, "source_rel", None)
        for entry in local_entries
        if not manifest_rules._path_owned_by(str(entry).replace("\\", "/"), guest_paths)
        and getattr(entry, "source_rel", None)
    ]
    declared_flavors: list[str] = []
    unresolved_declarations: list[str] = []
    for source_rel in source_declarations:
        parts = Path(source_rel.replace("\\", "/")).parts
        if (
            len(parts) >= 3
            and parts[0] == "templates"
            and parts[1] not in {"", ".", ".."}
            and (source_root / "templates" / parts[1]).is_dir()
        ):
            flavor = parts[1]
            if flavor not in declared_flavors:
                declared_flavors.append(flavor)
        elif not (
            # flavor 선택과 무관한 bare source remap은 source가 실제 해소될 때만 유효하다.
            not source_rel.replace("\\", "/").startswith("templates/")
            and (source_root / source_rel).exists()
        ):
            unresolved_declarations.append(source_rel)
    if source_declarations and (unresolved_declarations or not declared_flavors):
        unresolved = ", ".join(dict.fromkeys(
            unresolved_declarations or source_declarations
        ))
        print(
            "경고: engine.manifest에 해소할 수 없는 @source 선언이 있어 legacy 존재-휴리스틱을 "
            f"사용하지 않는다: {unresolved}. 선언된 primary manifest만 유지한다.",
            file=sys.stderr,
        )
        return [primary], False
    candidate_entries: dict[Path, list] = {}
    for candidate in candidates:
        try:
            candidate_entries[candidate] = manifest_store.read_manifest(candidate)
        except (OSError, UnicodeError, ValueError) as exc:
            print(
                f"note: legacy flavor 후보 manifest를 읽을 수 없어 제외한다(fail-soft): "
                f"{candidate} ({exc})",
                file=sys.stderr,
            )
    candidate_paths = {
        candidate: {
            str(entry).replace("\\", "/") for entry in entries
        }
        for candidate, entries in candidate_entries.items()
    }
    if declared_flavors:
        # root manifest의 flavor @source는 선택 provenance가 아니라 canonical remap이다. self-prop가
        # template flavor를 가리키는 설치 manifest에서만 선언 집합을 선택 집합으로 해석한다.
        if primary_flavor is None:
            return [primary], False
        ordered_flavors = [primary_flavor, *(
            flavor for flavor in declared_flavors if flavor != primary_flavor
        )]
        selected_declared: list[Path] = []
        for flavor in ordered_flavors:
            candidate = (
                source_root / "templates" / flavor / ".project_manager" / "engine.manifest"
            )
            selected_declared.append(candidate)
            if not candidate.is_file():
                print(
                    "경고: engine.manifest가 선언한 후순위 flavor의 upstream manifest가 없다 — "
                    f"선언을 버리지 않고 로컬 union을 유지한다: {flavor} ({candidate}). "
                    "누락 source가 있으면 apply 전에 중단된다.",
                    file=sys.stderr,
                )
        declared_set = set(ordered_flavors)
        for candidate, entries in candidate_entries.items():
            flavor = candidate.parents[1].name
            if flavor in declared_set:
                continue
            other_paths = set().union(*(
                paths for other, paths in candidate_paths.items()
                if other != candidate
            ))
            evidence_paths = _frozen_flavor_evidence(
                entries, other_paths, local_core_paths, guest_paths)
            if evidence_paths is None:
                continue
            observed = [
                rel for rel in evidence_paths
                if (Path(effective_dest) / rel).exists()
            ]
            if observed:
                _print_frozen_flavor_warning(
                    flavor,
                    observed,
                    evidence_paths,
                    declared_manifest=True,
                )
        return selected_declared, False

    if not candidates:
        return [primary], False
    if not candidate_entries:
        return [primary], False

    # provenance가 없는 legacy 후보를 고를 때만 후보별 선언을 검증한다. 현행 manifest가 flavor를
    # 명시했다면 위에서 선택한 manifest만 후속 self-heal 검증 대상이며, 무관한 template flavor의
    # 손상 선언이 그 정상 경로를 가로막아서는 안 된다.
    candidate_retirements = {
        candidate: retired_paths.validated_retired_path_directives(source_root, [candidate])
        for candidate in candidate_entries
    }
    exact_matches = []
    for candidate, paths in candidate_paths.items():
        retired = candidate_retirements[candidate]
        inverse_paths = (
            paths - {new for _old, new in retired}
        ) | {old for old, _new in retired}
        if paths == local_core_paths or inverse_paths == local_core_paths:
            exact_matches.append(candidate)
    legacy_primary = (
        exact_matches[0]
        if (
            len(exact_matches) == 1
            and Path(effective_dest).resolve() != source_root.resolve()
        )
        # framework checkout의 root manifest가 우연히 template과 같은 경로 집합이어도 root가 primary다.
        # template provenance 복원은 source와 분리된 legacy adopter에서만 필요하다.
        else None
    )
    if legacy_primary is not None:
        for candidate, entries in candidate_entries.items():
            if candidate == legacy_primary:
                continue
            other_paths = set().union(*(
                paths for other, paths in candidate_paths.items()
                if other != candidate
            ))
            evidence_paths = _frozen_flavor_evidence(
                entries, other_paths, local_core_paths, guest_paths)
            if evidence_paths is None:
                continue
            observed = [
                rel for rel in evidence_paths
                if (Path(effective_dest) / rel).exists()
            ]
            if observed:
                _print_frozen_flavor_warning(
                    candidate.parents[1].name,
                    observed,
                    evidence_paths,
                    declared_manifest=False,
                )
        return [legacy_primary], False

    if Path(effective_dest).resolve() == source_root.resolve():
        return [primary], False

    observed_by_flavor: list[tuple[str, list[str], int]] = []
    for candidate, entries in candidate_entries.items():
        other_paths = set().union(*(
            paths for other, paths in candidate_paths.items()
            if other != candidate
        ))
        evidence_paths = _frozen_flavor_evidence(
            entries,
            other_paths,
            local_core_paths,
            guest_paths,
        )
        if evidence_paths is None:
            continue
        observed = [
            rel for rel in evidence_paths
            if (Path(effective_dest) / rel).exists()
        ]
        if observed:
            observed_by_flavor.append((
                candidate.parents[1].name, observed, len(evidence_paths)))
    _print_legacy_nonmatch_warning(local_core_paths, observed_by_flavor)
    return [primary], True


def resolve_manifest_selfheal(
        effective_dest: Path, source_root: Path,
        *, guest_backfill_paths: set[str] | None = None) -> dict:
    """self-update manifest 자기치유 (2-pass 단일 실행) — upstream engine.manifest 를
    이번 sync 의 **계획 기준 manifest 로 승격**해 신규 등재분을 한 번의 실행으로 도달시킨다.

    채택자가 bare `pm-update`/CLI 로 흡수할 때, 로컬 engine.manifest 가 구형이면
    resolve_manifest_for_dest 가 그 구형 로컬 manifest 를 집어 plan 이 신규 등재 경로(upstream 이
    새로 등재한 엔진 파일)를 아예 안 실었다 — 다음 sync 전까진 영영 미도달(
    구 manifest·pm_handoff identity_args 미등재 → AttributeError·손 manifest 교체로만 복구). 이
    함수는 upstream manifest 를 plan 기준으로 승격한다. manifest 자신도 self-prop 엔트리(
    upstream 항상 등재)라 같은 plan 안에서 로컬 manifest 파일이 upstream 판으로 apply 된다 —
    별도 write 없이 정상 순서(missing-check 후·실 apply 시·dry-run 무부작용)에서 갱신된다.

    **flavor-correct upstream 해소** (`@source` self-prop): 비교/승격 대상 upstream
    manifest 는 root(`source_root/.project_manager/engine.manifest`·claude-scoped·bare)가 아니라 채택자
    self-prop 엔트리의 `@source` 를 따라간 *같은 flavor* manifest 다. claude_code/opencode 채택자의
    self-prop 는 `.project_manager/engine.manifest @source=templates/<harness>/.project_manager/
    engine.manifest` 라, 이를 무시하고 root 를 승격하면 flavor manifest(@source 마커 보유)를 root(bare)로
    **클로버**해 하네스-특정 remap 구조를 깬다. self-prop 의 @source(=`_source_root_rel`)로 flavor upstream
    을 읽어 flavor↔flavor 로 비교하면 마커가 정합하고 신규 등재분만 승격된다. self-prop 부재/bare 는
    root 로 폴백(현행).

    ("manifest 진화=스킬 reconcile·self-list 아님")의 통제-상실 우려(채택자 로컬 manifest
    커스텀 제외)는 **전체 교체 + diff loud 표시**로 대체한다(자동 병합 안 함·호출부가 표시).
    flavor upstream manifest 부재/읽기 실패면 fail-soft(로컬 유지·plan 무변경)
    baseline 억제가 그 잔여 경로 안전망이다. --target(엔진 export)은 호출하지 않는다(타깃
    manifest 가 루트와 의도적으로 다름·skew 검출과 동일 경계).

    반환 dict:
      - status  : 'upstream_missing'(flavor upstream 부재·fail-soft) | 'no_local'(로컬 manifest 부재·
                  이미 source manifest 기준) | 'in_sync'(로컬==upstream 또는 경로/선언 동일·무변경) |
                  'diverged'(로컬-전용 경로 또는 공통 경로 마커/@source divergence=커스텀 편집·승격
                  안 함·안전망) | 'legacy_preserved'(후보 exact-match 없음·로컬 manifest 불가침) |
                  'heal'(upstream 신규 등재 또는 exact-match legacy provenance 승격)
      - added   : flavor upstream 에만 있는 순수 경로(신규/재-등재·정렬) — 'heal' 이면 이번 sync 로 도달
      - removed : 로컬 manifest 에만 있던 순수 경로('diverged' 판정 근거·정렬)
      - retired_removed: strict retired-path 선언으로 검증되어 divergence에서 제외한 OLD 경로
      - manifest: plan 이 쓸 ManifestEntry 리스트 — 'heal' 이면 flavor upstream_entries, 그 외 None
                  (None 이면 호출부가 resolve_manifest_for_dest 산출 로컬 manifest 를 그대로 쓴다).
      - upstream_manifest: 대조에 쓴 flavor-correct upstream engine.manifest **Path** — 호출부(main)가
                  이 경로를 detect_manifest_skew 에 그대로 넘겨 두 기전의 대조 기준을 flavor 로 정합시킨다.
                  로컬 manifest 부재('no_local')는 self-prop 이 없어 root 폴백 경로.
    """
    dest_manifest = Path(effective_dest) / ".project_manager" / "engine.manifest"
    root_manifest = Path(source_root) / ".project_manager" / "engine.manifest"
    if not dest_manifest.exists():
        # 로컬 manifest 부재(fresh/구 import) — resolve_manifest_for_dest 가 이미 source manifest 를
        #   집으므로 plan 이 upstream 기준(신규 등재 포함)으로 돈다. 승격 불요(무변경·현행). self-prop
        #   이 없어 skew 대조는 root(=resolve 산출과 동일) 로 정합.
        retired_paths.validated_retired_path_directives(source_root, [root_manifest])
        return {"status": "no_local", "added": [], "removed": [],
                "retired_removed": [],
                "manifest": None, "upstream_manifest": root_manifest,
                "upstream_manifests": [root_manifest], "manifest_text": None,
                "merge_conflicts": []}
    local_entries = manifest_store.read_manifest(dest_manifest)
    local_text = fs.read_text_shared(dest_manifest, encoding="utf-8")
    if guest_backfill_paths is None:
        # 호출부가 이미 해소했으면 넘긴다(중복 IO 회피). 안 넘기면 여기서 해소해
        #   진단 판정이 호출 경로마다 갈리지 않게 한다.
        backfill, _flavors = _guest_engine_backfill_entries(
            effective_dest, source_root,
            _dest_guest_manifest_entries(effective_dest))
        guest_backfill_paths = {str(entry).replace("\\", "/") for entry in backfill}
    # 설치된 flavor들의 upstream manifest 합집합. 첫 항목은 self-prop가 지정한 기존 flavor이고,
    # 추가 항목은 설치 manifest의 flavor provenance로 일반화해 발견한다(고정 조합 손-끼워넣기 없음).
    upstream_manifest = Path(source_root) / _selfprop_upstream_rel(local_entries)
    upstream_manifests: list[Path] = [upstream_manifest]
    try:
        upstream_manifests, legacy_preserved = _selected_upstream_manifests(
            effective_dest, source_root, local_entries, local_text,
            guest_backfill_paths)
        upstream_manifest = upstream_manifests[0]
        if legacy_preserved:
            return {
                "status": "legacy_preserved",
                "added": [],
                "removed": [],
                "retired_removed": [],
                "manifest": None,
                "upstream_manifest": upstream_manifest,
                "upstream_manifests": upstream_manifests,
                "manifest_text": None,
                "merge_conflicts": [],
            }
        merged_upstream = manifest_store.merge_manifest_sources(upstream_manifests)
        upstream_text = merged_upstream["text"]
        upstream_entries = merged_upstream["entries"]
        validated_retirements = retired_paths.validated_retired_path_directives(
            source_root, upstream_manifests
        )
    except (FileNotFoundError, OSError, UnicodeError, ValueError):
        # flavor upstream 읽기 실패 — skew 대조도 같은 경로를 넘겨 upstream_missing 으로 정합(fail-soft).
        return {"status": "upstream_missing", "added": [], "removed": [],
                "retired_removed": [],
                "manifest": None, "upstream_manifest": upstream_manifest,
                "upstream_manifests": upstream_manifests, "manifest_text": None,
                "merge_conflicts": []}
    # add-harness guest 절(로컬-전용 `@target-owned` guest)은 **core 비교에서 제외**한다:
    #   섞으면 항상 removed 비어있지 않아 영구 diverged → upstream 신규 항목 자기치유(승격) 불능. 절은
    #   apply 가 재부착하므로(대칭·`_copy_manifest_preserving_guest`) 승격돼도 잔존한다. 판정 사본 없이
    #   추출 헬퍼를 재사용해 in_sync 판정도 core 로(strip==upstream), 경로 집합도 core 로 좁힌다.
    guest_block = manifest_rules._extract_guest_manifest_block(local_text)
    guest_paths = {
        ln.split()[0] for ln in guest_block.splitlines()
        if ln.strip() and not ln.strip().startswith("#")} if guest_block else set()
    if manifest_rules._strip_guest_manifest_block(local_text) == upstream_text:
        return {"status": "in_sync", "added": [], "removed": [],
                "retired_removed": [],
                "manifest": None, "upstream_manifest": upstream_manifest,
                "upstream_manifests": upstream_manifests,
                "manifest_text": upstream_text,
                "merge_conflicts": merged_upstream["conflicts"]}
    core_local_entries = [
        e for e in local_entries
        if str(e).replace("\\", "/") not in guest_paths
    ]
    # 경로 + 마커(@render/@target-owned/@source) 동시 비교 — 경로 집합만 보면 flavor manifest 의
    #   @source self-prop 을 root bare 로 덮는 클로버를 못 잡는다(codex MF). 다만 @source 자체가
    #   전혀 없던 legacy manifest는 source provenance 추가가 바로 치유 목적이므로 source_rel 차이만
    #   허용한다(render/target-owned 편집은 계속 divergence). 신규 선언 manifest의 공통 경로 마커가
    #   하나라도 갈리면(로컬 커스텀 편집·잘못된 flavor 대조) 승격하지 않는다.
    local_markers = {str(e): manifest_rules._manifest_marker_key(e) for e in core_local_entries}
    upstream_markers = {str(e): manifest_rules._manifest_marker_key(e) for e in upstream_entries}
    added = sorted(set(upstream_markers) - set(local_markers))
    removed = sorted(set(local_markers) - set(upstream_markers))
    validated_old = {old for old, _new in validated_retirements}
    retired_removed = sorted(set(removed) & validated_old)
    effective_removed = sorted(set(removed) - validated_old)
    legacy_without_source_provenance = not any(
        getattr(entry, "source_rel", None) for entry in core_local_entries
    )
    provenance_divergent = sorted(
        p for p in (set(local_markers) & set(upstream_markers))
        if local_markers[p][2] != upstream_markers[p][2]
    )
    marker_divergent = sorted(
        p for p in (set(local_markers) & set(upstream_markers))
        if (
            local_markers[p][:2] != upstream_markers[p][:2]
            if legacy_without_source_provenance
            else local_markers[p] != upstream_markers[p]
        )
    )
    if effective_removed or marker_divergent:
        # 로컬-전용 경로 또는 공통 경로 마커 divergence = 로컬이 flavor upstream 의 단순 부분집합이
        #   아니다(채택자 커스텀 편집·마커 손질). 전체 교체하면 그 커스텀/구조를 클로버하므로 승격하지
        #   않고 현행 로컬 manifest 를 유지한다. upstream 신규 등재분은 skew 대조가
        #   surface 한다(안전망). "항목 제외" 커스텀(로컬⊂upstream·마커 정합)은 아래 heal 로 전체 교체.
        return {"status": "diverged", "added": added, "removed": effective_removed,
                "retired_removed": retired_removed,
                "manifest": None, "upstream_manifest": upstream_manifest,
                "upstream_manifests": upstream_manifests,
                "manifest_text": upstream_text,
                "merge_conflicts": merged_upstream["conflicts"]}
    if not added and not retired_removed and not (
        legacy_without_source_provenance and provenance_divergent
    ):
        # 경로/마커 동일(주석만 차이) — 도달할 신규 등재 경로 0. manifest 자신도 self-prop 엔트리라
        #   plan 이 파일은 갱신한다(승격 불요). in_sync 로 취급(baseline 갱신 진행).
        return {"status": "in_sync", "added": [], "removed": [],
                "retired_removed": [],
                "manifest": None, "upstream_manifest": upstream_manifest,
                "upstream_manifests": upstream_manifests,
                "manifest_text": upstream_text,
                "merge_conflicts": merged_upstream["conflicts"]}
    # 로컬 ⊂ upstream(removed 0·마커 정합·added>0), 또는 경로는 같지만 @source가 전무한 legacy
    # manifest — flavor upstream 을 계획 기준으로 승격해 신규 경로와 source provenance를 같은
    # sync에서 도달시킨다. provenance-only 승격은 bare opencode 경로를 source root에서 찾는 rc=2도
    # 막는다.
    return {"status": "heal", "added": added, "removed": [],
            "retired_removed": retired_removed,
            "manifest": upstream_entries, "upstream_manifest": upstream_manifest,
            "upstream_manifests": upstream_manifests,
            "manifest_text": upstream_text,
            "merge_conflicts": merged_upstream["conflicts"],
            "multi_flavor_recovery": len(upstream_manifests) > 1,
            "provenance_upgrade": bool(
                legacy_without_source_provenance and provenance_divergent
            ),
            "legacy_manifest": legacy_without_source_provenance}


def _print_manifest_selfheal_finding(selfheal: dict, *, dry_run: bool = False) -> None:
    """resolve_manifest_selfheal 결과를 사람이 읽을 형태로 출력(loud diff).

    - 'heal'            : loud — upstream manifest 를 계획 기준으로 승격(전체 교체·자동 병합 없음).
                          upstream 이 새로/재-등재한 경로(+·이번 sync 로 도달)를 표시한다. 로컬 ⊂
                          upstream 이 승격 조건이라 로컬-전용 제거분은 없다(있으면 'diverged').
    - 'upstream_missing': 무출력 — skew 대조가 이어서 fail-soft note 를 낸다(중복 회피).
    - 'diverged'/'in_sync'/'no_local'/'skipped': 무출력 — 'diverged'(로컬-전용 경로=다른 하네스/커스텀)는
                          승격 안 하고 skew 대조에 맡긴다(중복 회피).

    승격 자체는 호출부(main)가 plan manifest 를 교체해 수행 — 이 함수는 출력만.
    """
    if selfheal.get("status") != "heal":
        return
    added = selfheal["added"]
    retired_removed = selfheal.get("retired_removed", [])
    verb = "자기치유 예정" if dry_run else "자기치유"
    if selfheal.get("multi_flavor_recovery"):
        flavors = [
            path.parents[1].name
            for path in selfheal.get("upstream_manifests", [])
            if len(path.parents) >= 2
        ]
        print(
            "⚠️ 설치된 다중 하네스의 manifest 누락(frozen adapter) 감지 — 이 adapter 경로들은 "
            "설치 manifest 밖이라 그동안 pm_update 갱신이 정지돼 있었다. "
            f"선택 flavor 합집합으로 {verb}: {', '.join(flavors)}. "
            "복구를 원치 않으면 해당 어댑터 트리를 제거하라."
            + (
                " engine.manifest의 그 flavor @source 선언도 정리하라."
                if not selfheal.get("legacy_manifest")
                else ""
            )
        )
    if selfheal.get("provenance_upgrade") and not added:
        print(
            f"→ engine.manifest {verb} — 관리 경로는 같지만 @source provenance 선언을 "
            "upstream 형식으로 승격한다."
        )
        return
    print(
        f"→ engine.manifest {verb} — upstream manifest 를 계획 기준으로 승격 "
        f"(선택 flavor 합집합·선언 순서 우선): 신규 등재 +{len(added)}"
        + (f", retired-path OLD -{len(retired_removed)}" if retired_removed else "")
    )
    for path in added:
        print(f"    + {path}  (upstream 신규/재-등재 — 이번 sync 로 도달)")
    for path in retired_removed:
        print(f"    - {path}  (검증된 retired-path OLD — apply 뒤 backup/퇴역)")


def _print_manifest_merge_conflicts(selfheal: dict) -> None:
    """다중 flavor 합집합의 마커 충돌을 pm_import와 대칭으로 stderr에 표면화한다."""
    conflicts = selfheal.get("merge_conflicts", [])
    if not conflicts:
        return
    print(
        "경고: 선택 manifest 중복 경로의 마커/@source 불일치 — 선언 순서상 첫 flavor를 "
        f"우선함 ({len(conflicts)}건): {', '.join(conflicts)}",
        file=sys.stderr,
    )


def _installed_entry_notation_manifests(
    effective_dest: Path,
    source_root: Path,
    upstream_manifests: list[Path],
) -> list[Path]:
    """core 선택에 더해 실제 설치된 add-harness guest를 표기 context에만 포함한다.

    guest flavor는 manifest self-heal의 **선택/승격 대상이 아니다**(core 합집합 밖). 다만 공유
    wiki를 실제로 읽는 하네스 집합에서는 빠지면 안 되므로, 설치 하네스를 찾아 그 flavor manifest를
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
    dest 파일 열거는 공용 seam(`repo_files` OWNED = 추적 + 미추적-비ignore)이다. 직접
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
    여기 도달하지 않지만, `--changes`(read-only)에는 그 채널이 없어 이 보고가 유일한 표면이다 —
    상류가 살아 있는 디렉토리만 스캔하면 "상류 삭제·rename (아래 보고 참조)" 헤더가 빈 포인터가
    되고, 통째 삭제라는 **가장 크게 잔존하는 형상**이 가장 조용해진다.

    ``dest_map`` 은 **dest 좌표만** 담아야 한다 — `@source` 매핑의 상류 좌표가 섞이면 우연히 같은
    문자열인 dest 잔존물이 "상류가 공급함" 으로 접혀 보고에서 사라진다(false negative).

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
        #   잔존물 전부가 보고에서 통째 누락된다(가장 크게 잔존하는 형상인데 가장 조용했다). 아래
        #   per-child source 실재 검사가 자연히 전량을 후보로 만든다. 제외 규칙 우선순위는 그대로다:
        #   `@target-owned` 는 이 루프에 들어오기 전에 이미 빠졌고(source 부재가 정상), 계획이
        #   공급하는 좌표(`planned`)와 dest 미실재 경로도 아래에서 계속 걸러진다.
        try:
            # 열거는 **등재 디렉토리 subtree 로 한정**한다 — dest 전체를 훑으면 비-git dest 의
            # filesystem 폴백이 채택자 프로젝트 전부를 순회한다(등재 밖은 애초에 판정 대상도 아님).
            # 비-git dest 의 폴백 경고는 여기서 삼킨다 — 진단 보고 하나 때문에 채택자에게 엔진
            # 내부 seam 경고 원문을 노출할 이유가 없다(열거 자체는 폴백으로 정상 동작한다).
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


def _manifest_self_prop_needs_update(
        dst_text: str, upstream_text: str,
        backfill_lines: list[str] | None = None) -> bool:
    """engine.manifest self-prop 을 이번 계획에 실어야 하는가 — 두 self-prop 분기의 공유 판정.

    분기는 둘이다: upstream 실파일 사본(단일 flavor)과 인메모리 합집합 텍스트(다중-harness). 판정이
    갈리면 한쪽 형상에서만 마이그레이션/갱신이 도달하므로 **한 함수**로 둔다(판정 사본 금지).

    사유 세 가지:
    - **core 차이**: dest 는 apply 가 재부착한 guest 절을 갖고 upstream 은 안 가지므로 guest 절을
      차감한 core 로 비교한다(raw 비교면 매 sync 영구 update churn). 트레일링 블랭크는
      `rstrip("\\n")` 로 정규화한다(strip 이 절 앞 빈 줄을 회수하며 생기던 반복 update 를 닫는다).
    - **옛 세대 guest 마커**: core 가 같아도 그 자체가 update 사유다. 마커 세대 치환은 apply
      (`_copy_manifest_preserving_guest`) 한 곳에서만 일어나므로, 이 change 가 계획에 안 실리면
      "다음 sync 한 번으로 세대 수렴" 이 그 형상에서 깨진다.
    - **절에 없는 파생 엔진 행**: 같은 이유다. 기록 지점이 apply 한 곳뿐이라, core 가 이미 정합한
      채택자(대다수)에서 self-prop 이 계획에 안 실리면 파생분이 영원히 기록되지 않고 매 실행 추론을
      반복한다. 병합 결과가 현재 절과 같으면(=이미 기록됨) 사유가 아니다(멱등·churn 0).
    """
    _, legacy_markers = manifest_store._migrate_legacy_guest_markers(dst_text)
    if legacy_markers:
        return True
    guest_block = manifest_rules._extract_guest_manifest_block(dst_text)
    if manifest_store._merge_guest_backfill_lines(guest_block, backfill_lines or []) != guest_block:
        return True
    return manifest_rules._strip_guest_manifest_block(dst_text).rstrip("\n") != upstream_text.rstrip("\n")


def _join_guest_channels(
        entries: list, guest_entries: list, guest_backfill: list) -> list:
    """계획 manifest 에 add-harness guest 절의 행을 **합류**시킨다 (core 와 같은 채널).

    guest 절은 두 종류를 담는다 — 어댑터 렌더물(`@render`)과 엔진 파일(비-`@render`) — 그러나
    **update 채널은 하나**다:
      - `@render` 행(어댑터 렌더물) = core `@render` 와 같은 재렌더 경로. 카드의 model 은
        local.conf(`delegate.*`)의 렌더 파생물이라, guest 를 계획에서 빼면 conf 를 바꿔도 그 카드가
        영영 옛 값으로 남는다(채택자 실측: 카드 opus ↔ conf sonnet). 손편집은 다음 update 가
        되돌린다 — 그게 렌더물의 의도된 동작이다(add-harness 재실행을 요구하지 않는다).
      - 비-`@render` 행(엔진 파일) = byte-copy 전파. 이 행이 빠지면 엔진 패키지(`pm_engine`)와 짝인
        드라이버·ctx 가드가 어떤 채널로도 갱신되지 않아 영구 동결된다.

    core 가 이미 소유한 경로(승격분 포함)는 core 선언이 이긴다 — 경로-포함(`_path_owned_by`)으로
    접어 같은 파일을 두 번 계획하지 않는다. 절 자체는 apply 가 재부착하므로(`_copy_manifest_
    preserving_guest`) 계획 합류가 절을 바꾸지 않는다."""
    if not guest_entries and not guest_backfill:
        return entries
    # append 하므로 **항상 새 리스트**로 뜬다 — 입력이 selfheal 승격분 그 자체일 수 있어(같은
    #   객체) 제자리 변경하면 selfheal 산출이 오염된다.
    planned = list(entries)
    planned_paths = {str(entry).replace("\\", "/") for entry in planned}
    # 파생분 우선, 그 뒤 절에 실재하는 행(상류에서 폐기된 경로는 `@target-owned` 라 loud `[skip]`
    #   + rc0). selfheal 이 upstream 을 승격한 run 은 계획 manifest 가 upstream core 전용이라 guest
    #   행이 통째로 빠지므로, 경로 중복만 접고 계획에 얹는다.
    for entry in [*guest_backfill, *guest_entries]:
        path = str(entry).replace("\\", "/")
        if manifest_rules._path_owned_by(path, planned_paths):
            continue
        planned_paths.add(path)
        planned.append(entry)
    return planned


def _resolve_planning_manifest(
        effective_dest: Path, source_root: Path, selfheal: dict | None = None,
        *, guest_entries: list | None = None,
        guest_backfill: list | None = None) -> list:
    """이번 실행의 **계획 기준 manifest** — self-heal 승격분 우선, 없으면 dest/source 로컬 해소.

    실 sync(`_main`)와 미리보기(`--changes`)가 이 한 함수를 공유한다. 갈라져 있던 동안 미리보기는
    낡은 로컬 manifest 로 분류하고 실 sync 는 승격된 upstream manifest 로 계획해, 정확히 self-heal
    이 전달하려는 신규 엔진 파일이 "안 받음" 으로 미리보기됐다. 발생 기전이 "self-heal 을 `_main`
    에만 추가" 였으므로 판정 사본을 두지 않는다 — self-heal 이 또 진화해도 여기 한 곳만 바뀐다.

    `selfheal` 은 **이미 해소한** `resolve_manifest_selfheal` 산출이다 — 호출부가 넘긴다(`_main` 은
    skew 대조·merge conflict 표시에도 그 dict 를 쓰므로 두 번 돌릴 이유가 없고, 해소 여부 자체가
    호출부 게이트다: `--target` 엔진 export 는 타깃 manifest 가 루트와 의도적으로 달라 비발화).
    `None` = self-heal 미해소(승격 없음) → 로컬 해소만 한다.
    폴백 경로의 FileNotFoundError 는 호출부가 처리한다 — 진입별로 rc 가 다르다(sync=rc1 에러,
    미리보기=빈 manifest graceful).

    **`legacy_preserved` 제외도 이 판정의 일부**다: exact-match 가 아닌 legacy 는 로컬 manifest
    자체가 불가침이라 self-prop(`_MANIFEST_SELF_REL`)을 계획에서 뺀다(source root manifest 가 파일을
    통째 덮어 커스텀 행을 지우는 것을 막는다). 이 축이 `_main` 에만 있으면 그 코호트에서 미리보기가
    engine.manifest 를 "받는다" 고 오보한다 — 기준 일치가 이 헬퍼의 존재 이유다.

    **add-harness guest 절의 합류도 여기서** 한다(`_join_guest_channels`). `_main` 에만 있으면
    미리보기와 계획이 guest 축에서 갈려 이 헬퍼의 존재 이유(미리보기 == 계획)가 거짓이 된다.
    `guest_entries`/`guest_backfill` 은 호출부가 이미 해소했으면 넘기고(중복 IO 회피), 없으면
    여기서 해소한다.
    """
    if guest_entries is None:
        guest_entries = _dest_guest_manifest_entries(effective_dest)
    if guest_backfill is None:
        guest_backfill, _flavors = _guest_engine_backfill_entries(
            effective_dest, source_root, guest_entries)
    if selfheal is not None and selfheal["manifest"] is not None:
        entries = selfheal["manifest"]
    else:
        entries = _resolve_local_manifest(effective_dest, source_root)
        if selfheal is not None and selfheal["status"] == "legacy_preserved":
            entries = [
                entry for entry in entries
                if str(entry).replace("\\", "/") != manifest_rules._MANIFEST_SELF_REL
            ]
    return _join_guest_channels(entries, guest_entries, guest_backfill)


def _resolve_local_manifest(effective_dest: Path, source_root: Path) -> list:
    """로컬 해소 manifest **원문 전체**(dest 우선·없으면 source) — 계획 제외 전 상태.

    계획용(`_resolve_planning_manifest`)과 skew 대조용이 갈린다: 전자는 `legacy_preserved` 에서
    self-prop 을 빼고, 후자는 원문 전체를 봐야 upstream 신규 등재분을 정상 검출한다. 두 소비자가
    같은 해소를 쓰도록 이름 붙인 seam."""
    return manifest_store.read_manifest(manifest_store.resolve_manifest_for_dest(effective_dest, source_root))
