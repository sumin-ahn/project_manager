"""기존 설치에 하네스를 추가하고 공유 문서 재렌더를 조정한다 (층: app)."""
from __future__ import annotations

from pathlib import Path
from pm_engine.app import adapter_accept
from pm_engine.app import harness_ctx
from pm_engine.app import import_fill
from pm_engine.app import import_render
from pm_engine.app import install_plan
from pm_engine.domain import adapter_specs
from pm_engine.domain import install_rules
from pm_engine.domain import manifest_rules
from pm_engine.domain import render_rules
from pm_engine.infra import adapter_baseline
from pm_engine.infra import fs
from pm_engine.infra import install_conf
from pm_engine.infra import install_files
from pm_engine.infra import install_git
from pm_engine.infra import install_receipt
from pm_engine.infra import install_source
from pm_engine.infra import manifest_store
from pm_engine.infra import skill_inventory
from pm_engine.infra import upstream_git
import datetime
import sys
import tomllib


# ── add_harness (라이브 인스턴스에 두 번째 harness 어댑터 비파괴 추가) ──────
# raw 다중-harness `--into` 재-import 는 full 재-laydown 으로 라이브 wiki dev-state/엔진을
# 템플릿 starter 로 덮는다. add_harness 는 복사 스코프를 *추가되는 harness 의
# 어댑터 네임스페이스*(ADD_HARNESS_ADAPTER)로 제한해 그 파괴를 구조적으로 차단한다 — 기존 copy/
# render/backup 머신(plan_copy·substitute·resolve_opencode_model·_run_manual_fill)만 재사용한다(신규
# 복사 머신 0). 운영 진입(pm_config add-harness)이 이 core 로 verbatim 위임한다(Decision 3).


# codex 어댑터(`​.codex/agents/*.toml`·`config.toml`·hooks)는 **trusted project + hook trust 승인
# 후에만** 발화한다. import/add-harness 직후 신선 인스턴스는 이 2단계가
# 미승인 상태 — 조용히 두면 위임 subagent 스폰·PreCompact ctx checkpoint 안내가 안 뜬다. `-c projects.<path>.
# trust_level` CLI override 는 **안 먹으므로** 대화형 승인이 유일 경로다.
def _print_codex_trust_guidance() -> None:
    """codex 어댑터 laydown 후 loud 2단계 trust 안내.

    import(`--harness codex`)·add-harness(기존 인스턴스에 codex 추가) 완료 출력 끝에 붙는다 —
    채택자가 첫 부트스트랩 전에 밟아야 할 trust 2단계 + 검증 커맨드를 눈에 띄게(loud) 안내한다.
    """
    print("")
    print("⚠️  codex 어댑터 활성화 전 2단계 trust 승인 필요 (미승인 시 위임/훅 미발화):")
    print("  1) 이 디렉토리에서 대화형 `codex` 를 1회 열어 프로젝트 trust 를 수락한다")
    print("     (`.codex/agents/*.toml`·`config.toml` 은 trusted project 한정 로드).")
    print("  2) codex 안에서 `/hooks` 로 hook trust 를 승인한다 (PreCompact ctx checkpoint 안내 발화 전제).")
    print("  검증: 대화형 codex 에서 위임 4축(architect/developer/code-reviewer/researcher)이 "
          "스폰 목록에 뜨는지 확인한다.")
    print("  ⚠️ `-c projects.<path>.trust_level=trusted` CLI override 는 안 먹는다(실측) — "
          "위 1) 대화형 승인이 필수다.")


def _print_claude_trust_guidance() -> None:
    """claude 어댑터 laydown 후 permissions.allow trust 선행 조건을 loud 안내."""
    print("")
    print("⚠️  Claude Code permissions.allow 적용 조건 안내:")
    print("  이 디렉토리를 아직 trust 승인하지 않았다면 출하 `.claude/settings.json`의")
    print("  `permissions.allow`가 적용되지 않으며 전역 설정에 의존한다.")
    print('  콘솔의 "Ignoring N permissions.allow entries" 경고가 이 상태의 실측 신호다.')
    print("  첫 대화형 `claude` 세션에서 trust 다이얼로그를 수락하면 적용된다.")


def _existing_create_if_absent_relpaths(
    template_root: Path,
    dest_root: Path,
    relpaths: frozenset[str],
    toml_override_fields: dict[str, frozenset[str]],
) -> tuple[set[Path], list[str]]:
    """기존 instance-owned config의 copy 제외 집합과 loud 안내 대상을 계산한다.

    이 helper는 add_harness가 ``plan_copy``를 호출하기 *전* 쓴다. 따라서 보호 파일은 일반
    CopyAction/backup/ancestor validation 경로에 전혀 들어가지 않는다. src가 없는 정책 항목은
    무시하지 않고 ValueError로 fail-loud — 선언과 template drift를 숨기지 않는다.
    """
    skip: set[Path] = set()
    different: list[str] = []
    for rel_str in sorted(relpaths):
        rel = Path(rel_str)
        src = template_root / rel
        if not src.is_file():
            raise ValueError(
                f"add-harness create-if-absent 정책 경로가 template에 없습니다: {rel_str}"
            )
        dst = dest_root / rel
        if not (dst.exists() or dst.is_symlink()):
            continue
        skip.add(rel)
        if not install_plan._same_bytes(src, dst):
            different.append(rel_str)
    for pattern, fields in sorted(toml_override_fields.items()):
        for src in sorted(template_root.glob(pattern)):
            rel = src.relative_to(template_root)
            dst = dest_root / rel
            if not (dst.exists() or dst.is_symlink()) or rel in skip:
                continue
            try:
                text = fs.read_text_shared(dst, encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue  # 읽을 수 없는 agent 파일은 engine-managed refresh의 기존 안전 경로를 탄다.
            # TOML 문법으로 해석한 최상위 키만 본다. nested table·문자열·주석의 동일한
            # 단어는 instance override가 아니며, quoted key도 정상적으로 보호한다.
            try:
                parsed = tomllib.loads(text)
            except tomllib.TOMLDecodeError:
                continue  # 깨진 TOML은 기존 engine-managed refresh 경로를 탄다.
            if not any(field in parsed for field in fields):
                continue
            skip.add(rel)
            if not install_plan._same_bytes(src, dst):
                different.append(rel.as_posix())
    return skip, different


def _unowned_shipped_wiki_relpaths(
        source_root: Path, harnesses, notation_contexts: dict) -> list[str]:
    """선택 하네스 template 이 **출하하는** wiki relpath 중 manifest 소유자가 없는 것.

    표기 폴백이 해소하는 그 부류(인스턴스 상태 seed — `status.md`·`log/current.md`·
    `raw/README.md` …)의 기계 파생이다. 손-열거 0: 출하 인벤토리(`_iter_source_files`·tracked-only)
    에서 wiki 하위를 모으고 manifest 선언(`notation_contexts` 키)이 소유하는 경로를 뺀다.

    add-harness 가 **기존 dest 파일**을 재렌더할 대상을 이 집합으로 한정한다 — 채택자가 직접
    쓴 wiki 문서(티켓·ADR·spike·domain 페이지)는 여기 없으므로 제자리 재작성 대상이 되지
    않는다(출하 seed 만·blast radius 한정). 소스 트리 부재는 빈 목록(무동작), 열거 예외는 전파한다."""
    owners = {rel.rstrip("/") for rel in notation_contexts}
    out: set[str] = set()
    for name in harnesses:
        for dirname in install_rules.HARNESS_TEMPLATE_DIRS.get(name, ()):  # noqa: B905
            root = Path(source_root) / "templates" / dirname
            if not root.is_dir():
                continue
            shipped = list(install_source._iter_source_files(root, "full"))
            for rel, _src in shipped:
                rel_posix = rel.as_posix()
                if not install_files._is_notation_fallback_scope(rel_posix):
                    continue
                if any(rel_posix == owner or rel_posix.startswith(owner + "/")
                       for owner in owners):
                    continue
                out.add(rel_posix)
    return sorted(out)


_WIKI_TEMPLATE_SUFFIX = ".template.md"


def _generated_template_sibling(rel_posix: str) -> str | None:
    """`wiki/pm_state.template.md` → `wiki/pm_state.md` (템플릿이 만들어 내는 인스턴스 파일).

    `board.py init` 이 이 산출물을 만들고 그 시점 표기가 박제된다 — 나중에 하네스를 추가하면
    템플릿은 재렌더되는데 산출물은 아무도 안 건드려 옛 표기로 남는다. 손-열거 대신 `*.template.md`
    관례에서 파생한다(`_template.md` 스캐폴드는 1:1 산출물이 아니라 대상 아님)."""
    if not rel_posix.endswith(_WIKI_TEMPLATE_SUFFIX):
        return None
    return rel_posix[: -len(_WIKI_TEMPLATE_SUFFIX)] + ".md"


def _notation_rerender_contexts(
        entry_notation_templates: dict, unowned_wiki_relpaths, installed_context: tuple,
) -> dict:
    """기존 dest 파일 재렌더 후보 relpath → 표기 context.

    세 부류를 합친다:
      - **manifest 소유 공유 문서** — 둘 이상 하네스가 같은 물리 경로를 읽는 것만(단일이면 그
        하네스 template 이 이미 native 로 깔았다).
      - **manifest 미소유 출하 wiki seed** — 소유자가 없어 `entry_notation_templates` 에 안
        나타난다. 설치 하네스 전체를 독자로 본다(render 단계 폴백과 같은 의미·같은 값).
      - **템플릿이 만들어 낸 인스턴스 파일**(`pm_state.template.md` → `pm_state.md`) — 생성
        시점 표기가 박제돼 하네스 추가 후 옛 표기로 남는 유일한 파생 산출물이다.
    실제로 바뀌는지(무변경 no-op 제외)는 `_shared_notation_rerender_plan` 이 산출 미리보기로
    판정하므로 여기서는 후보만 넓게 모은다.
    """
    contexts = {
        rel: context
        for rel, context in entry_notation_templates.items()
        if len(context) > 1
    }
    for rel, context in entry_notation_templates.items():
        if not install_files._is_notation_fallback_scope(rel):
            continue
        sibling = _generated_template_sibling(rel)
        if sibling is not None:
            contexts.setdefault(sibling, tuple(installed_context) or tuple(context))
    for rel in unowned_wiki_relpaths:
        contexts.setdefault(rel, tuple(installed_context))
    return contexts


def _shared_notation_rerender_plan(
        home, dest_root: Path, entry_notation_templates: dict, backup_root: Path | None,
        git_safe: set | None, root_identity: tuple | None = None,
) -> tuple[list[Path], list[str]]:
    """같은 물리 문서를 둘 이상의 설치 하네스가 읽어 **기존 파일도 병기 렌더**해야 하는 relpath.

    반환 `(재렌더 대상, 백업 불가로 제외한 relpath)`.

    manifest 가 선언한 공유 wiki·공유 루트 doc 중 dest 에 이미 있는 파일만 좁게 연다(사용자
    상태 전체 재복사 아님). **복사 시작 전** 계획으로 산출해 (a) dry-run 이 이 변경을 표시하고
    (b) 적용 시 백업 범위에 들어가게 한다 — 옛 코드는 복사·적용이 끝난 뒤에야 이 집합을 계산해
    dry-run 계획에도 백업에도 없었다(비파괴 보장 구멍).

    경로 안전(`_is_safe_dest_path`)은 **여기서** 판정한다 — 옛 `is_file()` 단독 검사는 symlink 를
    따라가고 `..` 도 안 걸러, 이후 치환·렌더가 저장소 밖 파일을 고칠 수 있었다. 위험 경로는
    조용히 skip 하지 않고 **복사 시작 전 fail-loud**(engine.manifest 가드와 같은 성질 — 부분 적용
    0·외부 파일 불변).

    백업 대상 조상도 여기서 검증한다(`plan_copy` 와 동형 — git 추적&미변경은 git 이 복원하므로
    백업 불요). 백업 자리가 막혀 있으면(예 `.pm_import_backups` 가 일반 파일) **그 파일만 재렌더
    대상에서 빼고 호출부가 loud 하게 알린다** — 백업 못 할 파일은 고치지 않는다(비파괴). 여기서
    전체를 중단하지 않는 이유는 이 재렌더가 *피할 수 있는* 변경이기 때문이다(local.conf 백업은
    board init 이 무조건 덮어 회피 불가라 fail-loud 인 것과 대비).
    """
    unsafe: list[str] = []
    backup_blocked: list[str] = []
    out: list[Path] = []
    checked_ancestors: set[Path] = set()
    for rel_posix, context in sorted(entry_notation_templates.items()):
        if not context:
            continue
        if not (rel_posix.startswith(install_rules.INSTANCE_SHARED_WIKI_PREFIX)
                or rel_posix == "AGENTS.md"):
            continue
        rel = Path(rel_posix)
        if not install_files._is_safe_dest_path(dest_root, rel):
            # 존재하지 않는 경로는 애초에 대상이 아니다 — 실존하는데 불안전할 때만 거부한다.
            if (dest_root / rel).is_symlink() or (dest_root / rel).exists():
                unsafe.append(rel_posix)
            continue
        # 선검사는 `lstat` 이다 — `is_file()` 은 링크를 따라가 **깨진 symlink** 로 교체된 대상을
        #   False 로 보고 조용히 건너뛰었다(fd 가드에 닿지도 않는 침묵 제외). symlink 는 후보로
        #   통과시키고 판정은 아래 nofollow 열기 한 지점에 맡긴다(계획 단계라 그 결과는 fail-loud).
        #   ⚠ 여기서만은 **부재가 정상**이다 — 이 단계의 후보는 "있으면 재렌더할 경로" 라서 없는
        #   것은 사고가 아니다(적용 단계 채널의 loud 부재 규칙과 의도적으로 다른 지점).
        if not install_files._is_inplace_edit_candidate(dest_root, rel):
            continue
        try:
            changes = _notation_rerender_changes_file(
                home, dest_root, rel, context, root_identity=root_identity)
        except install_files.UnsafeDestPathError:
            # `_is_safe_dest_path` 판정과 이 미리보기 읽기 사이에 경로가 교체됐다 — 계획 단계라
            #   전체 fail-loud 로 보낸다(복사 시작 전 중단·부분 적용 0).
            unsafe.append(rel_posix)
            continue
        if not changes:
            continue  # 산출 무변경 — 계획·백업·처리 어디에도 넣지 않는다(멱등 재실행 소음 0).
        if backup_root is not None and not (
                git_safe is not None and rel_posix in git_safe):
            try:
                install_plan._check_ancestor_safe(dest_root, backup_root / rel, checked_ancestors)
            except install_files.AncestorConflict:
                backup_blocked.append(rel_posix)
                continue
        out.append(rel)
    if unsafe:
        raise install_files.UnsafeDestPathError(
            "add-harness 거부: 공유 문서 재렌더 대상 경로가 안전하지 않습니다 "
            f"({', '.join(unsafe)}) — symlink·조상 symlink·repo 밖. 링크를 직접 옮기거나 "
            "제거한 뒤 다시 시도하세요(복사 시작 전 중단 — 외부 파일을 건드리지 않습니다).")
    return out, backup_blocked


def _notation_rerender_changes_file(
        home, dest_root: Path, rel: Path, context: tuple,
        root_identity: tuple | None = None) -> bool:
    """이 파일이 재렌더로 **실제로 바뀌는가** (표기 산출 미리보기 + 미해소 토큰 잔존).

    렌더러는 순수 함수라 계획 단계에서 산출을 미리 만들어 볼 수 있다. 무변경 파일을 계획·백업·
    처리 범위에서 빼면 (a) `[rerender]` 계획이 실제 변경만 말하고 (b) 비-git 인스턴스에서
    add-harness 를 재실행해도 이 경로로는 백업이 쌓이지 않는다(멱등).

    판정 축은 **표기 산출**뿐이다 — 잔존 `{{...}}` 를 변경 후보로 세면 토큰을 *설명*으로 담는
    방법론 문서(pm_role·pm_playbook)와 free-form 홈(pm_role.local.md)이 매 실행 재대상이 돼
    멱등이 깨진다(실측: 재실행마다 3건 재계획·백업 누적). 그 문서들의 operational 치환은
    최초 import 가 이미 끝냈고 sed 제외 규칙이 리터럴 유지를 의도한다.

    렌더는 `domain.render_rules` 의 순수 함수를 직접 부르고, 카드 inventory 는 실행 Home 에서 얻은
    tuple 을 값으로 넘긴다. 렌더 오류는 그대로 올린다(판정 불가를 변경 있음으로 바꾸지 않는다).

    읽기는 symlink 미추종 fd 경로다 — `_is_safe_dest_path` 판정 직후라도 이 미리보기 읽기 전에
    경로가 교체될 수 있어서, 그때는 `UnsafeDestPathError` 를 그대로 올려 호출부가 **복사 전
    fail-loud** 로 처리하게 한다(저장소 밖 파일을 읽지도 않는다)."""
    try:
        # 실제 렌더와 **같은 읽기**(줄끝 미번역)여야 계획과 적용이 어긋나지 않는다.
        text = install_files.read_dest_text_keeping_newlines(dest_root, rel, root_identity=root_identity)
    except (UnicodeDecodeError, OSError):
        return False  # 텍스트가 아니면 렌더 대상이 아니다(byte-copy 그대로).
    return render_rules.render_skill_entry_notation(
        text, context, skill_names=skill_inventory._installed_skill_entry_names(home),
        source=str(dest_root / rel)) != text


def _guest_line_key(line: str) -> tuple:
    """guest manifest 라인의 **마커-무관 비교 키** = (path, frozenset(markers)).

    공백/마커 순서에 불변이라 `.opencode/agents    @render @target-owned` 와
    `.opencode/agents @target-owned @render` 를 같게 본다. 경로 집합만 비교하면 같은 경로의 **마커
    교정**(`@render` → `@render @target-owned`)을 놓쳐 pm_update 가 non-target-owned 누락으로 rc=2
    실패할 수 있다 — sync 의 changed 판정이 이 키 집합을 비교한다."""
    toks = line.split()
    return (toks[0], frozenset(toks[1:])) if toks else ("", frozenset())


def _guest_render_sync_plan(
        dest_root: Path, guest_lines: list[str], adapter_dirs: tuple) -> dict:
    """refresh 시 **이 하네스 namespace 의 guest 절 항목을 현재 flavor 와 동기화**한 계획.

    반환 `{"added": [라인], "removed": [경로], "new_block": str|None, "changed": bool}`.
    - **이 하네스 namespace(`adapter_dirs`) 항목만** 현행 목표로 교체 — 신규 추가 **+ upstream flavor 에서
      폐기/`@render` 해제된 stale 제거**. 목표(`guest_lines`)는 렌더물과 엔진 행 **둘 다**라
      (`_guest_manifest_lines`) 이 동기가 두 채널을 함께 최신화한다.
    - **타 하네스 guest 항목은 불변**(다른 namespace — 순차 add 로 한 절에 공존).
    - 목표 = `guest_lines`(flavor·이미 namespace-limited) **−** host 실소유(경로-포함
      `_path_owned_by`·기준 `_core_manifest_paths`). add·refresh·dry-run preview 가 이 단일 계획을 공유
      (판정 사본 0). manifest 부재는 무동작(changed=False)."""
    empty = {"added": [], "removed": [], "new_block": None, "changed": False}
    manifest = dest_root / ".project_manager" / "engine.manifest"
    if not manifest.is_file():
        return empty
    text = fs.read_text_shared(manifest, encoding="utf-8")
    block = manifest_rules._extract_guest_manifest_block(text)
    existing_lines = [
        ln.rstrip() for ln in (block.splitlines() if block else [])
        if ln.strip() and not ln.strip().startswith("#")]

    # 이 하네스가 관리하는 footprint = adapter namespace ∪ **flavor `@render` 선언**(cross-ns 포함).
    #   guest_lines 는 flavor 후보 전부(`_guest_manifest_lines`·렌더물 + 엔진 행) — 그 경로 집합이 cross-ns
    #   의존물(opencode 의 `.claude/skills`)까지 이 하네스 소유로 판정하게 한다(경로-포함
    #   `_path_owned_by`). namespace-only 판정이면 cross-ns 항목이 target 엔 있는데 existing_this 엔 없어
    #   idempotent refresh 가 매번 changed=True(등재 churn)·타 하네스로 오분류된다(멱등 위반).
    this_flavor_paths = {ln.split()[0] for ln in guest_lines}

    def _this_ns(path: str) -> bool:
        return manifest_rules._path_owned_by(path, this_flavor_paths) or any(
            path == d.rstrip("/") or path.startswith(d.rstrip("/") + "/")
            for d in adapter_dirs)

    # 목표 = flavor 후보(namespace 무관·cross-ns 포함) − host 실소유. guest_lines 는 이미 flavor-declared.
    core_owned = manifest_rules._core_manifest_paths(text)
    target = sorted(
        ln for ln in guest_lines if not manifest_rules._path_owned_by(ln.split()[0], core_owned))
    target_paths = {ln.split()[0] for ln in target}
    existing_this_lines = [ln for ln in existing_lines if _this_ns(ln.split()[0])]
    existing_this = {ln.split()[0] for ln in existing_this_lines}
    other_ns = [ln for ln in existing_lines if not _this_ns(ln.split()[0])]  # 타 하네스 — 불변.
    added = [ln for ln in target if ln.split()[0] not in existing_this]
    removed = sorted(existing_this - target_paths)  # upstream 에서 폐기된 this-ns 경로.
    merged = sorted(set(other_ns + target), key=lambda ln: ln.split()[0])
    new_block = (manifest_rules._GUEST_MANIFEST_BEGIN + "\n" + "\n".join(merged) + "\n"
                 + manifest_rules._GUEST_MANIFEST_END) if merged else None
    # changed = 경로 추가/제거 **OR 마커 교정**(같은 경로·마커 상이) — 경로 집합만 보면 기존
    #   `.opencode/agents @render` → 목표 `@render @target-owned` 교정을 놓쳐 pm_update rc=2
    #   ). this-ns 기존 라인 ↔ 목표를 마커-무관 키 집합으로 비교한다(merged 는 이미 목표 마커 반영).
    changed = ({_guest_line_key(ln) for ln in existing_this_lines}
               != {_guest_line_key(ln) for ln in target})
    return {"added": added, "removed": removed, "new_block": new_block, "changed": changed}


def _append_guest_render_to_manifest(
        dest_root: Path, guest_lines: list[str], adapter_dirs: tuple) -> dict:
    """dest engine.manifest 의 guest 절을 이 하네스 namespace 에 대해 현재 flavor 와 **동기화**한다
    (신규 등재 **+ 폐기 제거**·타 하네스 불변). 반환 `{"added": [라인], "removed": [경로]}`.

    **단일 guest 절**(마커 하나) 아래 모든 하네스 라인이 모이고, pm_update 가 engine.manifest overwrite
    시 재부착한다(MF-1). refresh 가 add-only 였으면 upstream flavor 에서 사라진 경로가 영구 render/lint
    관리로 남았다. write 후 `read_manifest` **왕복 검증**(조용한 미등재 금지·RuntimeError). dest
    manifest 부재·무변경(멱등)은 graceful skip. 계획은 `_guest_render_sync_plan`(preview 공유·판정 사본 0)."""
    manifest = dest_root / ".project_manager" / "engine.manifest"
    # 경로 안전: manifest(또는 조상)가 repo-밖 지향 symlink 면 아래 read/write 가 링크를
    #   따라가 외부 파일을 노출/덮는다 — **fail-loud**(조용한 skip 아님). 부분 적용 방지는 add_harness 의
    #   복사 시작 전 조기 가드가 맡고, 여기선 직접 호출·TOCTOU 백스톱.
    if not install_files._is_safe_dest_path(dest_root, Path(".project_manager") / "engine.manifest"):
        raise install_files.UnsafeDestPathError(
            f"add-harness: engine.manifest 경로가 안전하지 않아 guest 등재를 거부합니다 ({manifest}) "
            "— symlink·조상 symlink·repo 밖. 링크를 옮기거나 제거한 뒤 다시 시도하세요(외부 파일 불변).")
    if not manifest.is_file():
        # 등재를 조용히 생략하지 않는다 — 복사됐지만 render/lint 관리 밖임을 명시.
        print("  ⚠️ engine.manifest 부재 — guest 어댑터가 복사됐으나 render/lint 관리 밖입니다 "
              "(manifest-파생 등재 채널 없음).", file=sys.stderr)
        return {"added": [], "removed": []}
    plan = _guest_render_sync_plan(dest_root, guest_lines, adapter_dirs)
    if not plan["changed"]:
        return {"added": [], "removed": []}  # 멱등 — 이미 동기(재실행 refresh)
    # 판정(절 스트립·블록 조립)은 LF 본문으로, 쓰기는 **이 manifest 의 현재 표기**로 한다 —
    #   CRLF 체크아웃(Windows `core.autocrlf=true`)에서 LF 로 되쓰면 우리가 append 한 guest 절
    #   말고도 파일 전체가 뒤집혀 append-only byte 불변식이 깨진다.
    text, newline = install_files.read_text_preserving_newline(manifest)
    stripped = manifest_rules._strip_guest_manifest_block(text)
    if plan["new_block"]:
        if stripped and not stripped.endswith("\n"):
            stripped += "\n"
        install_files.write_text_preserving_newline(
            manifest, stripped + "\n" + plan["new_block"] + "\n", newline)
    else:
        # this-ns guest 전량 폐기·타 하네스도 0 → 절 제거.
        install_files.write_text_preserving_newline(manifest, stripped, newline)
    # read_manifest 왕복 검증 (fail-loud·추가분 반영). 대조는 **등재 경로 전량**이다 —
    #   `@render` 로 좁히면 guest 절의 엔진 행(비-render·update 채널)이 매번 미반영으로 오판된다.
    after = {str(e).replace("\\", "/") for e in manifest_store.read_manifest(manifest)}
    missing = [ln.split()[0] for ln in plan["added"] if ln.split()[0] not in after]
    if missing:
        raise RuntimeError(
            f"add-harness: guest 절 등재가 read_manifest 왕복에 미반영: {missing}")
    return {"added": plan["added"], "removed": plan["removed"]}


def add_harness(
    home,
    dest_root: Path,
    harness: str,
    *,
    dry_run: bool,
    source_root: Path | None = None,
) -> list[install_files.CopyAction]:
    """라이브 인스턴스에 두 번째 harness 어댑터를 비파괴로 추가한다.

    스코프 = *추가되는 harness 의 어댑터 네임스페이스 ∪ guest flavor `@render` 선언*(ADD_HARNESS_ADAPTER·
    host 실소유: opencode=`.opencode/**`+`AGENTS.md`(+codex host 엔
    cross-ns `.claude/skills` — opencode 네이티브 소비), claude=`.claude/**`(host-소유 제외)+
    `CLAUDE.md`. **제외**(plan 에 애초에 없음→clobber 불가): `.project_manager/**`(엔진+wiki dev-state)·
    `engine.manifest`·`.gitignore`·`.gitattributes`·`.github/**`·루트 파사드·다른 harness·flavor 미선언.

    구현: `plan_copy` 로 전체 어댑터 트리 plan 을 만든 뒤 `_in_adapter_namespace` predicate(네임스페이스
    ∪ flavor `@render` − host 실소유)로 **구조적으로 좁힌다** — 반환·적용 plan 에 flavor 미선언 relpath 가
    0개다(Decision 5 불변식·cross-ns 의존물은 flavor 선언이라 허용).
    첫 add=신규 복사(무손실)·재실행=refresh(엔진 관리 어댑터 파일은 중앙 백업 후 덮음·
    instance-owned config는 create-if-absent 보존·`--into` 백업 철학). fill(LLM) 불요 — operational 토큰 치환(substitute_placeholders)·opencode
    모델 결정적 해소(resolve_opencode_model)·자유서술 TODO 표시(_run_manual_fill·비-LLM)만.

    복사 대상은 아니지만 이번 add 로 **기존 공유 문서**(둘 이상 하네스가 읽는 wiki·`AGENTS.md`)가
    병기 표기로 재렌더된다 — 그 계획은 `_shared_notation_rerender_plan` 이 **복사 시작 전**
    산출·안전 검증하고, 출력/dry-run 계획에 `[rerender]` 로 표시되며, 적용 시 변경 직전에 중앙
    백업을 거친다(비파괴: 계획 제시 + 백업 후 변경).

    dry_run=True 면 plan 만 산출·출력(파일시스템 미변경). 반환값 = 스코프 제한된 CopyAction plan.
    source_root 생략 시 _resolve_add_harness_source 로 소스를 정한다: dest local.conf
    upstream(path·templates 보유) > dest 자신(templates 보유·framework-checkout 자기전환) > 친화
    에러. imported 인스턴스(templates 부재)도 upstream 에서 어댑터 소스를 해소한다.
    harness 는 등록된 단일 하네스 — 집합/미지원 값은 ValueError. dest 미존재는 FileNotFoundError.
    """
    if harness not in install_rules.ADD_HARNESS_ADAPTER:
        raise ValueError(
            f"add_harness: harness 는 {tuple(install_rules.ADD_HARNESS_ADAPTER)} 중 하나여야 한다 "
            f"(단일 harness 추가·집합 선택은 최초 import 소관): {harness!r}"
        )
    dest_root = Path(dest_root).resolve()
    if not dest_root.is_dir():
        raise FileNotFoundError(
            f"add_harness: dest 가 존재하는 라이브 인스턴스 디렉토리가 아니다: {dest_root}"
        )
    # engine.manifest 경로 안전 검증 (**복사 시작 전** fail-loud): manifest(또는 조상)가
    #   repo-밖 지향 symlink 면 이후 read(`_dest_manifest_core_paths`)·write(`_append_guest_render_to_
    #   manifest`)가 링크를 따라가 외부 파일을 노출/덮는다. 불안전이면 어떤 복사·등재도 시작하지 않는다
    #   (부분 적용 0). 읽기·쓰기 지점이 같은 경로라 이 단일 가드가 양쪽을 덮는다.
    if not install_files._is_safe_dest_path(dest_root, Path(".project_manager") / "engine.manifest"):
        raise install_files.UnsafeDestPathError(
            f"add-harness 거부: engine.manifest 경로가 안전하지 않습니다 "
            f"({dest_root / '.project_manager' / 'engine.manifest'}) — symlink·조상 symlink·repo 밖. "
            "링크를 직접 옮기거나 제거한 뒤 다시 시도하세요(비파괴 — 외부 파일을 건드리지 않습니다).")
    src_root = upstream_git._resolve_add_harness_source(dest_root, harness, source_root)
    template_root = install_source.resolve_template_roots(src_root, harness)[0]

    # 표기 독자 = dest 실설치 하네스 ∪ 이번에 추가하는 하네스(공용 판정 helper).
    # 기존 설치분은 따로 붙들어 둔다 — 설치 기록에 올릴 자격 판정(`established_harnesses`)이
    #   "이번에 선 어댑터" 와 "원래 있던 것" 을 갈라야 하기 때문(원래 것은 이번 실행이 철회 안 함).
    preexisting_harnesses = install_receipt.installed_harnesses(dest_root, src_root)
    selected_harnesses = tuple(
        candidate
        for candidate in install_rules.REGISTERED_HARNESSES
        if candidate in {*preexisting_harnesses, harness}
    )
    # 미등록 표기 하네스는 **복사 시작 전** 거부한다 — 렌더 단계에서 터지면 부분 설치가 남는다
    #   (main import 게이트와 같은 성질·같은 문구). ValueError 라 add_harness_cli 가 친화
    #   메시지 + rc1 로 번역한다(traceback 0).
    unregistered_notation = import_render.unregistered_skill_notation_template_dirs(home,
        [install_rules.HARNESS_TEMPLATE_DIRS[name][0] for name in selected_harnesses])
    if unregistered_notation:
        raise ValueError(
            f"add-harness: {import_render._unregistered_skill_notation_message(unregistered_notation)}")
    # 설치 하네스의 표기 manifest 는 **전원 확보**돼야 한다 — 하나라도 없으면 그 독자를 뺀 채
    #   공유 문서를 재렌더하게 되므로 복사 전에 중단한다(main import 와 같은 성질). 타입은
    #   FileNotFoundError — `add_harness_cli` 가 소스 템플릿 부재와 같은 친화 메시지 + rc 1 로
    #   번역해 traceback 을 남기지 않는다(양 진입 대칭).
    notation_manifests = []
    missing_notation_manifests = []
    for name in selected_harnesses:
        manifest = (src_root / "templates" / install_rules.HARNESS_TEMPLATE_DIRS[name][0]
                    / ".project_manager" / "engine.manifest")
        (notation_manifests if manifest.is_file() else missing_notation_manifests).append(
            manifest if manifest.is_file() else f"{name}({manifest})")
    if missing_notation_manifests:
        raise FileNotFoundError(
            "add-harness: 설치 하네스의 표기 manifest 를 소스에서 찾을 수 없습니다: "
            f"{', '.join(missing_notation_manifests)}. 그 하네스를 독자에서 조용히 빼면 공유 "
            "문서가 잘못된 단독 표기로 재렌더되므로 복사 전에 중단합니다 — `--from` 이 그 flavor "
            "template 을 가진 checkout 인지 확인하세요."
        )
    entry_notation_templates = manifest_store._entry_notation_templates_from_manifests(
        notation_manifests, src_root)
    shared_agents_members = install_rules.NEUTRAL_SHARED_ENTRY_DOCS[Path("AGENTS.md")][0]
    selected_shared_agents = tuple(
        install_rules.HARNESS_TEMPLATE_DIRS[name][0]
        for name in selected_harnesses
        if name in shared_agents_members
    )
    # main import 와 같은 규칙 — AGENTS.md 는 manifest 미등재라 소유자 매칭으로 context 가 안
    #   나오고, 독자는 이 진입문서를 읽는 하네스 부분집합이다. 단일 멤버여도 명시 전달한다
    #   (조용한 skip 0·단일이면 그 하네스 표기라 출하 형상에선 no-op).
    if selected_shared_agents:
        entry_notation_templates["AGENTS.md"] = selected_shared_agents

    adapter_dirs, root_doc = install_rules.ADD_HARNESS_ADAPTER[harness]
    # 스코프 표시 문자열 — dirs 튜플을 `d/**` 로 합친다(codex=`.codex/** + .agents/**`·단일은 그대로).
    adapter_scope = " + ".join(f"{d}/**" for d in adapter_dirs)
    # 복사/등재 차감 기준 = **dest(host) 실소유 경로** (guest 절 제외).
    # 판정(`_engine_render_relpaths`)은 *guest flavor* 관점이라 bare `@render` 를 전부 native 로 봐
    # **claude-as-guest**(codex/opencode host 에 claude 추가)를 놓쳤다 — claude flavor 의 `.claude/agents`·
    # `.claude/skills` bare @render 가 native 로 차감돼 복사·등재 0 → pm_update 영구 관리 불능. host 가
    # *실제로* 소유(pm_update 관리)하는 경로만 빼고 나머지는 guest 로 레이다운/등재한다(경로-포함
    # `_path_owned_by`/`_is_render_managed`). opencode host 의 `.claude/skills`(native 소비)처럼
    # host 가 이미 가진 것만 정확히 빠진다.
    dest_owned = install_source._dest_manifest_core_paths(dest_root)
    # guest flavor 가 `@render` 로 선언한 경로 전부 (cross-ns 의존물 포함) — 복사 스코프가
    # namespace 밖이라도 이걸 포함해야 opencode 의 `.claude/skills`(codex host 미소유)가 복사·
    # 렌더된다(그래야 등재된 guest @render 를 render_managed_files 가 실제 파일에 적용). host 실소유
    # (dest_owned)는 아래 `_in_adapter_namespace` 가 그 위에서 차감한다(claude host 는 미복사 유지).
    guest_render_paths = install_source._flavor_render_relpaths(template_root)

    today = datetime.date.today().isoformat()
    # refresh(재실행)는 네임스페이스 안 기존 어댑터를 중앙 디렉토리에 백업 후 덮는다(--into 동형).
    # 첫 add 는 신규라 백업 없음. git 추적&미변경은 백업 생략(git 복원 가능).
    backup_root = dest_root / install_rules.BACKUP_DIR_NAME / today
    git_safe = install_git.git_safe_relpaths(dest_root)

    # 기존 공유 문서 재렌더 계획 — **복사 시작 전** 산출해 경로 안전을 검증하고(위험 경로면 여기서
    #   fail-loud·부분 적용 0) dry-run 표시·백업 범위에 싣는다. 후보는 두 부류다:
    #   manifest 소유 공유 문서(다중 하네스) + **manifest 미소유 출하 wiki seed**. 후자를 빼면
    #   claude 인스턴스에 codex 를 얹었을 때 `wiki/raw/README.md` 가 canonical slash 로 남아
    #   최초 설치 경로에서 닫은 조용한 degrade 가 이 경로로 되살아난다(add-harness 축 실측).
    installed_notation_context = tuple(
        install_rules.HARNESS_TEMPLATE_DIRS[name][0] for name in selected_harnesses)
    rerender_contexts = _notation_rerender_contexts(
        entry_notation_templates,
        _unowned_shipped_wiki_relpaths(
            src_root, selected_harnesses, entry_notation_templates),
        installed_notation_context,
    )
    # dest 루트 신원을 **계획 전에** 고정해 적용까지 넘긴다 — 계획 뒤 루트 자체가 저장소 밖
    #   symlink·다른 디렉토리로 교체되면 컴포넌트 순회가 엉뚱한 트리에서 안전하게 일어난다.
    #   획득 실패는 여기서 fail-loud(`add_harness_cli` 가 rc1 번역·복사 시작 전).
    root_identity = install_files.dest_root_identity(dest_root)
    shared_rerender_relpaths, shared_backup_blocked = _shared_notation_rerender_plan(
        home,
        dest_root, rerender_contexts, backup_root, git_safe, root_identity=root_identity)

    # 전체 어댑터 트리 plan → (네임스페이스 ∪ flavor `@render` − host 실소유)로 구조적 제한(Decision 2·5·
    # ). 그 밖(엔진·wiki·타 harness·설정·파사드·flavor 미선언)은 필터로 제거돼 plan 에 0개다(불변식).
    create_if_absent = install_rules.ADD_HARNESS_CREATE_IF_ABSENT[harness]
    skipped_existing, preserved_different = _existing_create_if_absent_relpaths(
        template_root, dest_root, create_if_absent,
        adapter_specs.ADD_HARNESS_PRESERVE_EXISTING_TOML_FIELDS[harness],
    )
    plan = install_plan.plan_copy(
        [template_root], dest_root, backup_root, "full", git_safe=git_safe,
        skip_existing_relpaths=skipped_existing,
        include_relpath=lambda rel: install_rules._in_adapter_namespace(
            rel, adapter_dirs, root_doc, dest_owned, guest_render_paths), backup_dir_name=install_rules.BACKUP_DIR_NAME,
    )

    # 복사 plan 이 이미 실을 경로는 재렌더 목록에서 뺀다 — 그쪽이 CopyAction 백업 + render 를
    #   모두 하므로 남겨 두면 같은 파일을 두 번 백업하고 계획에 두 번 나온다(예 공유 `AGENTS.md`).
    planned_copy_relpaths = {a.dst.relative_to(dest_root) for a in plan}
    shared_rerender_relpaths = [
        rel for rel in shared_rerender_relpaths if rel not in planned_copy_relpaths]

    n_new = sum(1 for a in plan if a.backup is None and not a._git_safe_skip)
    n_refresh = len(plan) - n_new
    print(f"[pm_import add-harness] {harness} → {dest_root}")
    print(f"  소스: {src_root}/templates/{install_rules.HARNESS_TEMPLATE_DIRS[harness][0]}")
    print(f"  스코프: 어댑터 네임스페이스 + flavor @render ({adapter_scope} + {root_doc} · "
          f"host 실소유 경로 제외)")
    for a in plan:
        print(a.describe())
    for rel in preserved_different:
        print(f"  ⚠️ instance-owned {rel}: 기존 값이 template과 다름 — byte 보존. "
              "template 변경은 수동 반영하세요.")
    print(f"  → {len(plan)} 파일 ({n_new} 신규 · {n_refresh} refresh)")
    # 복사 대상은 아니지만 이번 add 로 *내용이 바뀌는* 기존 공유 문서 — 계획에 명시한다
    #   (dry-run·적용 공통. 적용 경로는 아래에서 백업 후 렌더한다).
    if shared_rerender_relpaths:
        print(f"  공유 문서 재렌더 ({len(shared_rerender_relpaths)}건 · 병기 표기 · 백업 후 변경):")
        for rel in shared_rerender_relpaths:
            print(f"  [rerender] {rel.as_posix()}")
    for rel_posix in shared_backup_blocked:
        print(f"  ⚠️ 공유 문서 {rel_posix}: 백업 자리({install_rules.BACKUP_DIR_NAME}/)가 막혀 있어 재렌더를 "
              "생략합니다 — 백업 못 하는 파일은 고치지 않습니다(비파괴). 해당 경로를 정리한 뒤 "
              "다시 실행하세요.", file=sys.stderr)

    if dry_run:
        # engine.manifest guest `@render` 동기 미리보기 — 추가/제거 예정 둘 다(실제 sync 와 같은 계획·
        #   `_guest_render_sync_plan` 공유·멱등이면 0건).
        gsync = _guest_render_sync_plan(
            dest_root,
            install_source._guest_manifest_lines(template_root, adapter_dirs, root_doc, dest_owned),
            adapter_dirs)
        if gsync["added"]:
            print(f"  engine.manifest guest 절 등재 예정 ({len(gsync['added'])}건):")
            for gl in gsync["added"]:
                print(f"    + {gl}")
        if gsync["removed"]:
            print(f"  engine.manifest guest 절 제거 예정 ({len(gsync['removed'])}건·폐기):")
            for gp in gsync["removed"]:
                print(f"    - {gp}")
        print("[dry-run] 적용 안 함 (파일시스템 미변경).")
        return plan

    # ── 적용 ── 스코프(네임스페이스 ∪ flavor `@render` − host 실소유) 안 파일만 복사·토큰 처리
    #   (스코프 밖은 plan 에 없어 불가침).
    # 복사 **직전** 루트 재확인 — 뒤에 두면 첫 mkdir/쓰기가 이미 교체된 트리에 들어간다.
    #   fd 가드는 *열 파일이 있을 때만* 발화하므로 단계 경계 검사가 false-green(대응 경로 없는
    #   교체 트리에서 아무 일도 안 하고 rc0)까지 막는다.
    install_files.assert_dest_root_unchanged(dest_root, root_identity)
    # 파일 단위 위반(경로 교체·계획 뒤 상태 변화)은 그 파일만 빼고 loud 로 알린다 — 적용 단계
    #   rc 정책과 같은 규칙이고, 후속 채널 범위는 **성공분만** 이다(제외분 미접촉).
    copy_outcome = install_files.apply_copy_plan(plan, dest_root, root_identity=root_identity)
    install_files.report_copy_apply_anomalies(copy_outcome)
    copied_relpaths = set(copy_outcome.copied)
    # guest 어댑터를 dest engine.manifest 에 멱등 등재(렌더물 `@render` + **엔진 행**) —
    # 인스턴스 manifest 가 "이 인스턴스에서 framework-managed 인 것"의 단일 진실이 되어, 아래
    # render_managed_files 와 manifest-파생 overlay 스캔이 guest 를 자연 커버하고, 엔진 행은
    # pm_update 전파 채널을 얻는다(등재 없으면 설치 시점 사본으로 영구 동결).
    # **render 전에** 등재해야 이번 run 의 render_managed_files 가 guest 를 집는다.
    guest_sync = _append_guest_render_to_manifest(
        dest_root,
        install_source._guest_manifest_lines(template_root, adapter_dirs, root_doc, dest_owned),
        adapter_dirs)
    if guest_sync["added"]:
        print(f"  ✓ engine.manifest guest 절 {len(guest_sync['added'])}건 등재: "
              f"{', '.join(ln.split()[0] for ln in guest_sync['added'])}")
    if guest_sync["removed"]:
        print(f"  ✓ engine.manifest guest 절 {len(guest_sync['removed'])}건 제거(폐기 동기): "
              f"{', '.join(guest_sync['removed'])}")
    # 이번 하네스 template 과 **byte-identical 이라 복사만 생략된**
    #   파일만 처리 대상에 추가한다 — token-form 그대로라 미렌더(토큰 잔존) 잔존의 유일 대상. 경로는
    #   template(신뢰)에서 오고 안전 검증(`_is_safe_dest_path`)을 거치며, 타 guest·adopter 자체
    #   생성 파일(내용 상이·copied)은 제외된다(과확장 봉쇄). 기존 렌더 파이프 재사용.
    proc_relpaths = copied_relpaths | install_files._byte_identical_skipped(
        template_root, dest_root, copied_relpaths, adapter_dirs,
        root_identity=root_identity)
    # 같은 물리 문서를 둘 이상의 설치 하네스가 읽으면 기존 파일도 병기 렌더 대상이다(계획은
    #   복사 전 `_shared_notation_rerender_plan` 이 안전 검증까지 마쳤다). 이 파일들은 복사
    #   액션이 아니라 **제자리 변경**이므로 백업이 CopyAction 을 안 탄다 — 변경 직전에 같은
    #   중앙 백업 규칙으로 직접 백업한다(비파괴: 적용 전 계획 제시 + 백업 후 변경).
    backup_outcome = install_files._backup_before_inplace_edit(
        dest_root, shared_rerender_relpaths, backup_root, git_safe,
        root_identity=root_identity)
    shared_backed_up = backup_outcome.backed_up
    if shared_backed_up:
        print(f"  ✓ 공유 문서 {len(shared_backed_up)}건 백업: {', '.join(shared_backed_up)}")
    # 백업 못 한 파일은 고치지 않는다(비파괴) — 교체·삭제 둘 다 재렌더 대상에서 뺀다.
    if backup_outcome.refused:
        print(f"  ⚠️ 공유 문서 {len(backup_outcome.refused)}건: 계획 검증 뒤 경로를 안전하게 열 수 "
              f"없어(symlink 교체·비-디렉토리 컴포넌트) 백업할 수 없습니다 — 재렌더에서 "
              f"제외합니다(원본·저장소 밖 파일 불변): {', '.join(backup_outcome.refused)}",
              file=sys.stderr)
    if backup_outcome.vanished:
        print(f"  ⚠️ 공유 문서 {len(backup_outcome.vanished)}건: 계획 검증 뒤 대상이 사라져(경쟁 "
              f"삭제) 백업할 수 없습니다 — 재렌더에서 제외합니다(새로 만들지 않습니다): "
              f"{', '.join(backup_outcome.vanished)}", file=sys.stderr)
    _backup_dropped = set(backup_outcome.refused) | set(backup_outcome.vanished)
    if _backup_dropped:
        shared_rerender_relpaths = [
            rel for rel in shared_rerender_relpaths
            if rel.as_posix() not in _backup_dropped]
    # ⚠ 기존 문서는 **표기 렌더 채널에만** 싣는다 — placeholder 치환·자유서술 fill 범위
    #   (`proc_relpaths`)에 넣으면 채택자가 쓴 `{{PROJECT_NAME}}` 리터럴이 이름으로 바뀌고
    #   자유서술 토큰에 TODO 마커가 붙는다(콘텐츠 훼손·실측). 이번 run 이 *복사한* 파일만
    #   치환·fill 대상이라는 비파괴 범위(MF1)는 그대로 둔다.
    render_relpaths = proc_relpaths | set(shared_rerender_relpaths)
    # 라이브 인스턴스의 project_name 은 기존 local.conf 를 존중(없으면 디렉토리명 폴백).
    project_name = install_conf._instance_project_name(dest_root)
    subs = import_render._substitution_map(project_name, dest_root, today)
    n_subst = import_render.substitute_placeholders(dest_root, subs, proc_relpaths,
                                      root_identity=root_identity)
    # opencode 모델 토큰 결정적 해소(claude-only 는 inactive) — main 흐름과 동일.
    import_fill.resolve_opencode_model(home, dest_root, proc_relpaths, model_arg=None,
                           root_identity=root_identity)
    # render_managed_files 는 dest 인스턴스 engine.manifest 의 @render path 만 렌더한다 — 위에서 guest
    # `@render` 를 dest manifest 에 등재했으므로 guest 어댑터도 렌더된다.
    import_render.render_managed_files(
        home,
        dest_root,
        subs,
        render_relpaths,
        root_identity=root_identity,
        entry_notation_templates=entry_notation_templates,
        # manifest 미소유 wiki seed 는 소유자 매칭으로 context 가 안 나온다 — 설치 하네스 전체를
        #   폴백 컨텍스트로 넘겨야 위 재렌더 대상이 실제로 병기 표기를 받는다(최초 설치와 같은 채널).
        installed_notation_context=installed_notation_context,
    )
    # 자유서술 placeholder 는 TODO 표시(비-LLM·main manual 흐름과 동일).
    install_files.assert_dest_root_unchanged(dest_root, root_identity)  # 단계 경계 재확인(위와 같은 이유).
    import_fill._run_manual_fill(dest_root, proc_relpaths, root_identity=root_identity)
    # 설치 기록 — 이번에 추가한 하네스 ∪ 이미 설치돼 있던 하네스(`selected_harnesses`) 중 **실제로
    #   어댑터가 선 것만** 박제한다(적용 단계에서 전부 제외됐으면 기록이 유령을 만든다). 이후 판정은
    #   이 기록을 진실로 쓰므로 판별자 파일이 사라져도 독자 집합이 유실되지 않고, 기록이 없던 구
    #   인스턴스는 여기서 backfill 된다(그 시점 추론 산출이 합집합에 이미 들어 있다).
    recordable, unestablished = install_receipt.established_harnesses(
        dest_root, selected_harnesses, preexisting_harnesses)
    if unestablished:
        print(f"  ⚠️ {', '.join(unestablished)} 어댑터가 이번 실행에서 서지 않아(복사 제외) 설치 "
              f"기록에 올리지 않습니다 — 위 제외 사유를 해소한 뒤 다시 실행하세요.", file=sys.stderr)
    template_coordinates = install_receipt._copied_instance_owned_template_coordinates(
        plan, dest_root, src_root, copied_relpaths, "full")
    if install_receipt.record_install_receipt(
            dest_root, recordable, root_identity=root_identity,
            template_coordinates=template_coordinates):
        print(f"  ✓ 설치 기록 갱신 ({install_receipt.INSTALL_RECEIPT_RELPATH.as_posix()}): "
              f"{', '.join(recordable)}")
    # instance-owned config 원장 — **레이다운/보존 판정 직후**가 기록 시점이다. 방금 내려놓은
    #   (또는 기존 값이 template 과 같음을 확인한) 파일만 들어가고, 보존된 편집분은 안 들어간다
    #   (원장 부재 = 다음 동기가 보고 모드 = 안전 기본값).
    baselined = adapter_accept.record_adapter_baseline(
        dest_root, src_root, recordable, root_identity=root_identity)
    if baselined:
        print(f"  ✓ 어댑터 config 원장 기록 ({adapter_baseline.ADAPTER_BASELINE_RELPATH.as_posix()}): "
              f"{', '.join(baselined)}")
    # 하네스 사용 한도 반영 — 원장 기록 **뒤**라야 방금 내려놓은 설정 파일이 무편집으로 증언되고
    #   반영 뒤에도 그 판정이 유지된다(`apply_harness_ctx` 원장 규칙). 검사 거부는 ValueError 라
    #   `add_harness_cli` 가 rc 1 로 번역한다.
    harness_ctx._print_harness_ctx_apply(dest_root)
    # 실복사 수를 보고한다(계획 수 아님) — 제외가 있었으면 위 요약이 사유를 이미 말한다.
    print(f"✓ add-harness 완료: {harness} 어댑터 {len(copied_relpaths)} 파일 복사 · "
          f"{n_subst} 파일 토큰 치환 (스코프: {adapter_scope} + {root_doc})")
    # claude 는 프로젝트 trust 수락 전 settings.json permissions.allow 를 조용히 무시한다.
    if harness == "claude":
        _print_claude_trust_guidance()
    # codex 는 laydown 만으로 활성화되지 않는다 — trusted project + hook trust 2단계 안내.
    if harness == "codex":
        _print_codex_trust_guidance()
    return plan


def add_harness_cli(
    home,
    dest_root: Path,
    harness: str,
    *,
    dry_run: bool,
    source_root: Path | None = None,
) -> int:
    """add_harness 의 main-style CLI 진입 — 인터페이스 예외를 친화 메시지 + rc 로 번역한다.

    운영 진입(`pm_config add-harness`·Decision 3)이 verbatim 위임하는 얇은 래퍼다. add_harness
    자체(확정 시그니처/로직)는 건드리지 않고, 그것이 던지는 인터페이스 예외만 CLI 경계에서
    잡아 `main()` 과 *동일하게* 처리한다(에러 처리의 단일 진실 = CLI contract owner = 이 모듈):
      - ValueError            : 미지원/집합 harness·미등록 표기 하네스(add_harness 입구 검증).
      - FileNotFoundError     : dest 부재/비-디렉토리·소스 템플릿 부재(resolve_template_roots).
      - UpstreamCacheError    : URL upstream 캐시 clone/fetch 실패(소스 해소·복사 전).
      - FileVsDirConflict     : 어댑터 dst 위치에 기존 디렉토리(plan_copy·비파괴 거부).
      - AncestorConflict      : dst 조상에 symlink/비-디렉토리 파일(plan_copy·비파괴 거부).
      - UnsafeDestPathError   : engine.manifest·공유 문서 경로가 repo 밖 지향(복사 전 거부).
      - DestRootSwappedError  : dest 루트 자체가 계획 뒤 교체됨(**적용 중에도** 즉시 전체 중단).
    앞 다섯(ValueError·FileNotFoundError·UpstreamCacheError·FileVsDirConflict·AncestorConflict)과
    `EmptyTemplateShippingInventoryError` 는 add_harness 가 *복사 전*(plan_copy·
    resolve_template_roots·소스 해소·입구 검증)에 던지므로 부분 적용 없이 깨끗한 `오류: …`(stderr) + rc 1 로
    끝난다(traceback 0·main 동형). **`UnsafeDestPathError` 는 복사 전(계획 경로 안전 가드)뿐 아니라
    복사 *중*(`CopyAction.run` 의 fd 가드 — 계획 뒤 조상·목적지 교체)에도 나올 수 있다**: 그때는
    그 시점까지의 복사가 남지만 저장소 밖 쓰기는 0이고, 사람이 정리한 뒤 재실행하면 된다.
    `DestRootSwappedError` 도 적용 중에 나올 수 있으며 그 중단은 부분 적용이 아니라 **오염
    차단**이다(대상 트리가 이미 바꿔치기됐다). 성공은 add_harness 가 자체 plan/summary 를 출력하고
    여기선 rc 0 만 돌려준다(위임 verbatim·중복 출력 0).

    dry_run/source_root 는 add_harness 로 그대로 전달한다(투명 위임). 반환: 0(성공)·1(인터페이스 예외).
    """
    try:
        add_harness(home, dest_root, harness, dry_run=dry_run, source_root=source_root)
    except (
        ValueError,
        FileNotFoundError,
        upstream_git.UpstreamCacheError,
        install_files.FileVsDirConflict,
        install_files.AncestorConflict,
        install_files.UnsafeDestPathError,
        install_files.DestRootSwappedError,
        install_source.EmptyTemplateShippingInventoryError,
    ) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1
    return 0
