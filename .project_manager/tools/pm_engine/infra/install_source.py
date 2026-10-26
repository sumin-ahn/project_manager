"""설치 공통 install_source (infra)."""
from __future__ import annotations

from pathlib import Path
from pm_engine.domain import install_rules
from pm_engine.domain import manifest_rules
from pm_engine.infra import fs
from pm_engine.infra import manifest_store
from pm_engine.infra import repo_files
import stat
import warnings

# 치환 대상 판정은 **제외 사유 기반**이다 — 옛 확장자 allowlist(`(".md", ".json", ".sh", ".py")`)는
# allowlist 는 "규칙이 적용될 지점을 사람이 열거" 하는 형상이라, 새 하니스가 새 파일
# 형식을 들여오면 조용히 미커버로 남는다 — codex(세 번째 하니스)의 `.codex/agents/*.toml` 이 정확히
# 그렇게 `{{PROJECT_NAME}}` 을 리터럴로 출하했다. 이제 판정은 뒤집혀 "제외 사유가 있는가" 만 본다
# (`_should_substitute`) → `.yaml`·`.jsonc` 같은 네 번째 하니스의 새 형식도 자동 편입된다.
#
# 제외 사유는 **닫힌 집합**(프레임워크가 소유한 엔진 자산)이라 열거해도 안전하다 — 열거가 위험했던 건
# *열린* 쪽(채택자/하니스가 계속 늘리는 파일 형식)을 열거했기 때문이다:
#   엔진 소스 `.project_manager/tools/**` (`_is_engine_source` — 주석의 토큰은 *설명*·verbatim)
#   엔진 메타데이터 `.project_manager/engine.manifest` (`_is_engine_metadata` — 아래)
#   manifest 파생 방법론 문서 제외집합 (`_dest_sed_exclude` — pm_role·pm_playbook)
#   텍스트로 못 읽는 파일 — 각 치환 루프의 `read_text` UnicodeDecodeError 가 걸러낸다.

# 엔진 메타데이터. engine.manifest 는 채택자에게 *개인화되어* 출하되는
# 산출물이 아니라 엔진이 읽는 기계 설정이고, 그 주석은 placeholder 메커니즘을 *설명*하며 토큰을 담는다
# (예 codex manifest 의 "`.codex/agents` 는 developer_instructions 에 {{PROJECT_NAME}} 토큰 보유 →
# @render"). 치환하면 설명이 concrete 값으로 변질된다 — `.project_manager/tools/**` 주석과 같은 클래스
# (_is_engine_source docstring 참조)이나, tools/ prefix 밖이라 별도 판정이 필요하다.
ENGINE_METADATA_RELPATHS = frozenset({
    ".project_manager/engine.manifest",
})


# ── 토큰 소유권: 파일 × 토큰 단위로 치환 주체를 하나로 ───────────────────────
# operational 토큰의 치환 주체는 셋 중 하나다:
#   설치-시 치환 — pm_import(기본값·아래 `_should_substitute` 가 제외 사유만 본다)
#   소비-시 치환 — 그 템플릿이 *산출물을 만드는 시점*(pm_state 생성·domain 페이지 생성)
#   상시 토큰    — 엔진 소유 문서가 토큰을 *설명*으로 담는다(엔진 소스·manifest·방법론 문서)
# 같은 파일을 설치-시와 소비-시가 반대 방향으로 소유하면 **매 sync 마다 진동한다**: 설치가 값으로
# 굳히고, manifest bare 등재의 byte-copy(`pm_update`)가 다음 흡수에서 토큰-form 으로 되돌린다
# (채택자 실측). 아래 선언은 소비-시 소유를 명시해 **그 파일의 그 토큰에서만** 설치-시 치환을
# 끈다 — 같은 파일의 다른 토큰(`{{PROJECT_NAME}}` 등)은 종전대로 설치 시 치환된다
# (치환 예외 *파일* 목록이 아니다).
# 신규 bare 등재 파일이 선언 없이 토큰을 담으면 `tests/test_manifest_render_token_guard.py` 가 fail.
CONSUMPTION_TIME_TOKENS: dict[str, frozenset] = {
    # 소비처: `task_store.ensure_task_pm_state`(task pm_state 렌더)·`board.cmd_init`
    #   (per-clone `wiki/pm_state.md` seed) — 둘 다 생성 시각으로 채운다.
    ".project_manager/wiki/pm_state.template.md": frozenset({"{{DATE}}"}),
    # 소비처: 사람이 스캐폴드를 복사해 domain 페이지를 만드는 시점. 엔진 생성 경로
    #   (`doc_page_store.write_draft_page`)는 스캐폴드 frontmatter 를 복사하지 않고 자체 today 로 쓴다.
    ".project_manager/wiki/domain/_template.md": frozenset({"{{DATE}}"}),
}


# operational placeholder 치환에서 *제외*하는 방법론 문서 (repo 기준 relpath) — engine.manifest 파생.
# 하드코딩 목록(과거 pm_role.md·pm_playbook.md 리터럴 frozenset) 대신 manifest 의 `.project_manager/wiki/`
# 직속 `pm_*.md` 비-템플릿(= 방법론 문서 절)에서 결정적으로 유도한다. 이 문서들은 `{{PROJECT_NAME}}`·
# `{{DATE}}` 토큰을 *메커니즘 설명*으로 담아(placeholder 아님·local.conf 가 런타임 해소·`docs/placeholders.md`)
# 치환하면 문서가 concrete 값으로 변질되므로 제외한다. 파생이라 신규 방법론 .md 가 manifest 에 추가되면
# 자동 편입 — "목록 수동 추가 잊음 → 조용한 placeholder 오치환" 클래스 종결.
#
# ⚠️ 파생 기준 manifest 는 **모듈-import 시점(실행 checkout)이 아니라 치환 시점의 dest 인스턴스**
# (`dest_root/.project_manager/engine.manifest`)다. pm_import 는
# `--from <다른 framework checkout>` 에서 복사할 수 있어(구버전 로컬 도구가 신버전 upstream 을 흡수하는
# 실 운영 경로), *실제 복사되는 쪽* manifest 가 기준이어야 upstream 진화(신규 직속 방법론 문서)가 자동
# 편입된다 — 모듈-시점 상수는 실행 checkout 의 manifest 에 묶여 이 보장을 잃는다. 치환은 복사 *이후*
# 단계라 dest manifest 는 그 시점에 실재한다(manifest = self-prop 복사 대상). 각 치환 call site 는 아래
# `_dest_sed_exclude(dest_root)` 로 dest 기준 제외 집합을 산출해 `_should_substitute` 에 넘긴다.

# manifest 부재·로드 실패(broken-manifest)에서도 기존 제외를 조용히 잃지 않게 하는 리터럴 floor
# (should-fix). 정상 경로(manifest 파싱 성공)는 파생 결과를, 실패 경로만 이 floor 를 반환한다.
SED_EXCLUDE_FLOOR = frozenset({
    ".project_manager/wiki/pm_role.md",
    ".project_manager/wiki/pm_playbook.md",
    ".project_manager/wiki/pm_principles.md",
})


def _is_template_scaffold(filename: str) -> bool:
    """파일명이 템플릿 스캐폴드 관례(`*.template.md`·`_template.md`·`*_template.md`)인가.

    템플릿(예: pm_state.template.md)은 `{{DATE}}` 등 토큰을 *렌더 대상*(도구·skill 이 새 산출물
    생성 시 채움)으로 담아, 토큰을 *설명*으로 담는 방법론 문서(pm_role·pm_playbook)와 성격이 다르다.
    치환-제외 집합엔 넣지 않는다(현행 동작 보존 — 템플릿의 operational 토큰은 import 치환 대상)."""
    stem = filename[:-len(".md")] if filename.endswith(".md") else filename
    return stem.endswith(".template") or stem == "_template" or stem.endswith("_template")


def _derive_sed_exclude_relpaths(manifest_path: Path) -> frozenset:
    """치환-제외 방법론 문서(repo 기준 relpath·POSIX) 집합을 engine.manifest 에서 파생한다.

    규칙 = manifest 엔트리 중 `.project_manager/wiki/` **직속** `pm_*.md` 파일 중
    *템플릿이 아닌* 것. README 같은 출하 색인은 render/update 채널에 들어와도 operational
    placeholder는 실제 프로젝트 값으로 치환돼야 하므로 방법론 리터럴 제외 집합에는 들지 않는다.
    서브디렉토리(`tickets/_template.md`·`raw/spikes/_template.md`·`domain/_template.md`)는 "직속"
    조건으로, 직속 템플릿(`pm_state.template.md`)은 `_is_template_scaffold` 로 제외된다 — 현재
    산출은 정확히 {pm_role.md, pm_playbook.md, pm_principles.md}.

    manifest 파싱은 manifest_store.read_manifest 재사용(새 파서 신설 없음·_render_managed_relpaths 동형).
    manifest 부재 → 리터럴 floor SED_EXCLUDE_FLOOR, 파싱 실패는 전파한다(깨진 manifest 로 방법론
    문서를 오치환하지 않는다). 정상
    파싱은 파생 결과를 그대로 반환한다(manifest 가 방법론 문서를 명시적으로 뺐다면 그 판단을 존중)."""
    wiki_prefix = ".project_manager/wiki/"
    if not manifest_path.is_file():
        return SED_EXCLUDE_FLOOR
    entries = manifest_store.read_manifest(manifest_path)
    out: set = set()
    for entry in entries:
        rel = str(entry).replace("\\", "/")
        if not rel.startswith(wiki_prefix):
            continue
        remainder = rel[len(wiki_prefix):]
        if "/" in remainder:  # 서브디렉토리 — 방법론 문서 절이 아님(직속만)
            continue
        if not remainder.endswith(".md"):
            continue
        if not remainder.startswith("pm_"):
            continue
        if _is_template_scaffold(remainder):  # 템플릿 스캐폴드 — 치환 대상(제외 아님)
            continue
        out.add(rel)
    return frozenset(out)


def _dest_sed_exclude(dest_root: Path) -> frozenset:
    """dest 인스턴스의 engine.manifest 기준 치환-제외 집합 (치환 시점 산출·codex must-fix).

    모듈-import 시점(실행 checkout)이 아니라 *복사가 끝난 dest* 의 manifest 에서 파생한다 — pm_import
    는 `--from <다른 checkout>` 에서 복사할 수 있어, 실제 dest 에 실린 manifest 가 기준이어야 upstream
    진화(신규 직속 방법론 문서)가 자동 편입된다. manifest 는 self-prop 복사 대상이라 치환(복사 이후)
    단계엔 dest 에 실재한다. 부재는 SED_EXCLUDE_FLOOR, 파싱 실패는 전파한다."""
    return _derive_sed_exclude_relpaths(dest_root / ".project_manager" / "engine.manifest")


# tracked-only 출하 목록 위에서도 복사 정책과 fill 스캔의 공통 방어를 보존하려고 유지한다.
# 복사/스캔 제외 디렉토리명 (무겁고 재설치 대상 / stale 산출물 / VCS 메타).
#   node_modules — opencode 의존성(재설치 대상). __pycache__ — stale 바이트코드(.pyc).
#   .git — VCS 메타(템플릿엔 없어 복사목록엔 안 끼지만, fill 폴백 전체 스캔이 대형 repo
#          .git 을 텍스트 read 하지 않도록 명시 제외 — 낭비 방지·결정론).
COPY_EXCLUDE_DIR_NAMES = frozenset({"node_modules", "__pycache__", ".git"})


# 복사 제외 파일 (정확 dst relpath) — adopter 에게 출하하지 않을 프레임워크-repo 내부 문서.
#   README.md(최상위) — 템플릿 트리의 "어댑터 타깃" 설명서다(프레임워크 상대링크 `../../README.md`·
#   `../opencode/README.md` 를 담아 adopter 트리에선 dangling·오해 소지). 채택자는 자기 프로젝트
#   README 를 쓴다 → 프레임워크-내부 doc 를 adopter README 로 박제하지 않는다(다중 선택 충돌도 소거).
#   하위 `.project_manager/wiki/*/README.md`(wiki 구조 안내)는 유지 — 정확 relpath `README.md` 만 제외.
COPY_EXCLUDE_RELPATHS = frozenset({"README.md"})


class SkippedTemplateShippingEntryWarning(RuntimeWarning):
    """pm-import가 byte-copy할 수 없는 템플릿 엔트리를 명시적으로 제외했다는 신호."""


class EmptyTemplateShippingInventoryError(RuntimeError):
    """존재하는 출하 템플릿의 tracked 인벤토리가 0건인 결함."""

    def __init__(
            self, checkout: Path, subtree: str = ".",
            *, filesystem_fallback: bool = False) -> None:
        self.checkout = Path(checkout)
        self.subtree = subtree
        self.filesystem_fallback = filesystem_fallback
        diagnosis = (
            "filesystem 강등 상태이므로 소스 디렉토리가 비었는지와 checkout 루트가 "
            "올바른지 확인하라"
            if filesystem_fallback
            else "checkout 루트가 올바른지와 git index에 출하 파일이 등재됐는지 확인하라"
        )
        super().__init__(
            "pm-import 출하 인벤토리가 0건임 "
            f"(checkout={self.checkout}, subtree={subtree!r}); "
            + diagnosis
        )


def _full_relpath_for_lite(rel: Path) -> Path:
    """lite 변종 relpath `X.lite.md` → full 진입 relpath `X.md` (이름 치환).

    `X.lite.md` 만 매핑한다(이름이 정확히 `.lite.md` 로 끝나는 경우). 반환 relpath 는
    같은 디렉토리의 `X.md` 다 — lite 배치 시 dst 가 full 진입 이름으로 들어가게 한다.
    """
    base = rel.name[: -len(install_rules.LITE_SUFFIX)]  # 'X.lite.md' → 'X'
    return rel.with_name(base + ".md")


def _shippable_template_files(template_root: Path) -> list[Path]:
    """repo 출하 엔트리를 링크를 추종하지 않는 일반 파일 목록으로 좁힌다.

    git 인벤토리는 index mode가 진실이므로 일반 파일 mode만 허용한다. 비-git 폴백은 mode를
    얻을 수 없어 그 경로에서만 lstat()으로 등가 판정한다. 두 판정을 한 엔트리에 함께 적용하면
    index와 working tree 중 어느 쪽이 출하 계약인지 갈리므로 의도적으로 섞지 않는다.

    0건 판정은 복사 계획 수가 아니라 이 원시 열거 수에 둔다. ``plan_copy``는 목적지 byte를
    비교하지 않아 동일 트리 재-import도 모든 출하 파일을 다시 계획하므로 정당한 zero-action
    no-op이 없고, 빈 원시 인벤토리만 출하 결함이다.
    """
    accepted: list[Path] = []
    skipped: dict[str, list[str]] = {
        "symlink(index mode 120000, 링크 대상 내용을 복사하지 않음)": [],
        "gitlink(index mode 160000, 파일 byte-copy 대상 아님)": [],
        "지원하지 않는 git index mode": [],
        "filesystem 폴백 symlink(lstat, 링크 대상 내용을 복사하지 않음)": [],
        "filesystem 폴백 일반 파일이 아닌 엔트리(lstat)": [],
        "filesystem 폴백 lstat 실패": [],
    }
    entries = repo_files.list_repo_owned_entries(
        template_root, ".", mode=repo_files.TRACKED_ONLY
    )
    if not entries:
        runner = repo_files._real_git_runner(template_root)
        probe_rc, inside = runner(["rev-parse", "--is-inside-work-tree"])
        filesystem_fallback = not (
            probe_rc == 0 and inside.strip() == "true")
        # seam의 빈 결과는 부분 subtree 질의에는 유효할 수 있다. 그러나 선택된 템플릿은
        # 프레임워크 출하 단위이므로 소비 지점에서 직접 막아 warning의 조건부 발화에 기대지 않는다.
        raise EmptyTemplateShippingInventoryError(
            template_root, ".", filesystem_fallback=filesystem_fallback)
    for entry in entries:
        rel = entry.path
        source = template_root / rel
        index_mode = entry.index_mode
        if index_mode in {"100644", "100755"}:
            accepted.append(source)
            continue
        if index_mode == "120000":
            skipped["symlink(index mode 120000, 링크 대상 내용을 복사하지 않음)"].append(
                rel.as_posix()
            )
            continue
        if index_mode == "160000":
            skipped["gitlink(index mode 160000, 파일 byte-copy 대상 아님)"].append(
                rel.as_posix()
            )
            continue
        if index_mode is not None:
            skipped["지원하지 않는 git index mode"].append(
                f"{rel.as_posix()} ({index_mode})"
            )
            continue

        # filesystem 폴백에는 index mode가 없으므로 여기서만 링크 비추종 lstat 판정을 쓴다.
        try:
            fallback_mode = source.lstat().st_mode
        except OSError as exc:
            skipped["filesystem 폴백 lstat 실패"].append(
                f"{rel.as_posix()} ({exc})"
            )
            continue
        if stat.S_ISREG(fallback_mode):
            accepted.append(source)
        elif stat.S_ISLNK(fallback_mode):
            skipped[
                "filesystem 폴백 symlink(lstat, 링크 대상 내용을 복사하지 않음)"
            ].append(rel.as_posix())
        else:
            skipped["filesystem 폴백 일반 파일이 아닌 엔트리(lstat)"].append(
                rel.as_posix()
            )

    for reason, paths in skipped.items():
        if paths:
            warnings.warn(
                f"pm-import 템플릿 출하 엔트리 {len(paths)}건 제외 — {reason}: "
                + ", ".join(paths),
                SkippedTemplateShippingEntryWarning,
                stacklevel=2,
            )
    return accepted


def _iter_source_files(template_root: Path, weight: str = "full"):
    """template_root의 repo 추적 파일을 (dst relpath, 절대경로)로. 정책 경로 추가 제외.

    weight 관례 (`*.lite.md` = `*.md` 의 lite 변종):
      - full(기본): 모든 `*.lite.md` 를 복사 대상에서 *제외*한다(lite 변종이 full 배포에
        끼면 안 됨). full `X.md` 는 그대로 복사.
      - lite: 각 `X.lite.md` 를 dst relpath `X.md` 로 복사(이름 치환). 동시에 (a) 같은
        트리의 full `X.md` 는 복사 제외(lite 가 그 자리를 차지), (b) 원본 이름 `X.lite.md`
        도 그대로는 복사 제외(dst 에 `*.lite.md` 잔존 금지).
    yield 하는 relpath 는 *dst* 기준이므로 lite 모드에선 `X.md` 로 치환돼 나간다 —
    placeholder 치환(copied_relpaths 기준)·다중-tree 충돌 판정이 이 dst relpath 위에서 돈다.

    출하 인벤토리는 공용 repo_files seam의 TRACKED_ONLY 의미를 쓴다. 따라서 유지보수자
    checkout의 미추적·machine-local 파일은 복사 후보가 아니며, 비-git 소스에서 filesystem
    폴백할 때는 seam이 RepoFilesFallbackWarning으로 추적 보장 소실을 loud하게 알린다.
    COPY_EXCLUDE_*는 추적 여부와 별개인 출하 정책(node_modules·stale bytecode·내부 README)과
    fill 스캔 공통 방어를 위해 그 위에 계속 적용한다. 엔진 패키지 교체가 `.project_manager/tools/`
    바로 아래에 남기는 락·stage·backup 도 추적 여부와 무관하게 제외한다(그 자리를 지우지는 않는다).
    """
    files = [
        path
        for path in _shippable_template_files(template_root)
        if not any(
            part in COPY_EXCLUDE_DIR_NAMES
            for part in path.relative_to(template_root).parts
        )
        # 프레임워크-내부 doc(최상위 README.md 등)은 adopter 로 출하하지 않는다.
        and path.relative_to(template_root).as_posix() not in COPY_EXCLUDE_RELPATHS
        # 엔진 패키지 교체의 락·복구 트리는 이 템플릿의 운영 산출물이다(git 없는 사본에도 남는다).
        and not manifest_rules._is_engine_transfer_artifact(
            path.relative_to(template_root).as_posix())
    ]
    # lite 모드: 이 트리에서 lite 가 대체할 full 진입 relpath 집합을 먼저 모은다.
    lite_overridden: set[Path] = set()
    if weight == "lite":
        for path in files:
            rel = path.relative_to(template_root)
            if rel.name.endswith(install_rules.LITE_SUFFIX):
                lite_overridden.add(_full_relpath_for_lite(rel))

    for path in files:
        rel = path.relative_to(template_root)
        is_lite = rel.name.endswith(install_rules.LITE_SUFFIX)
        if weight == "lite":
            if is_lite:
                # X.lite.md → dst X.md (이름 치환). 원본 lite 이름은 dst 에 안 남는다.
                yield _full_relpath_for_lite(rel), path
                continue
            if rel in lite_overridden:
                # full X.md 는 같은 트리의 lite 변종이 그 자리를 차지하므로 제외.
                continue
            yield rel, path
        else:  # full
            if is_lite:
                # lite 변종은 full 배포에 끼면 안 됨 — 제외.
                continue
            yield rel, path


def _is_engine_source(rel: Path) -> bool:
    """엔진 소스 코드(`.project_manager/tools/`)인가 — placeholder 처리에서 전면 제외 대상.

    엔진 도구(.py)는 verbatim canonical 사본이다: 코드는 런타임에 local.conf 에서 `project.name`·
    `runtime.py`·`test.cmd` 를 읽지, baked placeholder 를 쓰지 않는다. 그런데 그 *주석·docstring·예시 문자열*
    엔 `{{PROJECT_NAME}}`·`{{OPENCODE_PRO_MODEL}}`·`{{PROJECT_CONSTRAINTS}}` 같은 토큰이 문서로
    등장한다(엔진이 placeholder 메커니즘을 설명하므로). 이 문자열들은 *placeholder 가 아니라
    문서*다 — substitute/fill/token-scan 이 건드리면 (a) 주석이 concrete 값으로 변질 (b) free-form
    토큰에 `<!-- TODO -->` 주입 (c) `{{OPENCODE_PRO_MODEL}}` 이 claude 트리의 모델-해소 게이트를
    오발(_token_present True → resolve active). 따라서 엔진 소스는 placeholder 처리 전 범위에서
    제외해 verbatim 으로 둔다 (adapter_render/pm_update 가 토큰을 문서화하며 표면화).

    rel 은 dest-rel(`​.project_manager/tools/board.py`) 또는 절대/소스 경로(dry-run plan 의
    `action.dst`/`action.src`) 둘 다 올 수 있어 substring 매칭으로 통일한다(`tools/` 트레일링
    슬래시로 `tools_backup` 등 오탐 방지)."""
    p = rel.as_posix()
    return p.startswith(".project_manager/tools/") or "/.project_manager/tools/" in p


def _is_engine_metadata(rel: Path) -> bool:
    """엔진 메타데이터(`.project_manager/engine.manifest`)인가 — 치환 제외 대상.

    _is_engine_source 와 같은 클래스(엔진 소유·토큰이 *설명*)이나 `.project_manager/tools/` prefix
    밖이라 별도 판정이다. rel 은 dest-rel 또는 절대/소스 경로 둘 다 올 수 있어 `_is_engine_source`
    와 같은 suffix 매칭으로 통일한다(경로 구분자 정규화)."""
    p = rel.as_posix()
    return any(p == m or p.endswith("/" + m) for m in ENGINE_METADATA_RELPATHS)


def _consumption_time_tokens(rel: Path) -> frozenset:
    """이 파일에서 **설치 시 치환을 하지 않을** 토큰 집합 (소비 시점 소유·`CONSUMPTION_TIME_TOKENS`).

    파일 전체를 제외하는 `_should_substitute` 와 축이 다르다 — 여기서 걸러도 같은 파일의 다른
    operational 토큰은 종전대로 설치 시 치환된다. rel 은 dest-rel 또는 절대/소스 경로 둘 다 올 수
    있어 `_is_engine_metadata` 와 같은 suffix 매칭으로 통일한다."""
    p = rel.as_posix()
    for owned_rel, tokens in CONSUMPTION_TIME_TOKENS.items():
        if p == owned_rel or p.endswith("/" + owned_rel):
            return tokens
    return frozenset()


def _should_substitute(rel: Path, exclude_relpaths: frozenset) -> bool:
    """이 파일이 operational placeholder 치환 대상인가 — **제외 사유가 없으면 대상**.

    옛 판정은 확장자 allowlist(`SUBSTITUTE_SUFFIXES`) gate 였다: 열린 집합(하니스가 계속 늘리는
    파일 형식)을 사람이 열거한 형상이라, codex 가 들여온 `.codex/agents/*.toml` 이 어느 채널도
    못 타고 `{{PROJECT_NAME}}` 을 리터럴로 출하했다. 판정을 뒤집어 **닫힌 집합(엔진 소유 자산)의
    제외 사유**만 본다 → 네 번째 하니스의 새 형식(`.yaml`·`.jsonc`)도 자동 편입된다.

    제외 사유(모듈 상단 주석과 동기):
      방법론 문서 — exclude_relpaths = dest 인스턴스 manifest 파생 치환-제외 집합
         (`_dest_sed_exclude`). 호출부가 치환 시점에 dest 기준으로 산출해 넘긴다(모듈-시점 상수
         아님).
      엔진 소스(`.project_manager/tools/**`) — verbatim.
      엔진 메타데이터(`engine.manifest`) — 주석이 토큰 메커니즘을 *설명*.
      텍스트로 못 읽는 파일 — 이 판정이 아니라 호출부 `read_text` 의 UnicodeDecodeError 가
         걸러낸다(파일 내용 없이는 판정 불가·현 구조 유지).

    범위(어떤 파일이 이 판정에 오는가)는 넓히지 않는다 — 호출부는 전부 `copied_relpaths`
    (이번 run 이 실제 복사한 파일)로 한정된다(`substitute_placeholders` docstring MF1 비파괴)."""
    if rel.as_posix() in exclude_relpaths:
        return False
    if _is_engine_source(rel):  # 엔진 소스(.py)는 verbatim — 주석의 토큰-문서는 placeholder 아님
        return False
    if _is_engine_metadata(rel):  # engine.manifest 주석의 토큰-문서도 placeholder 아님
        return False
    return True


# ── 모드 준비 (--new / --into) ─────────────────────────────────────────────

def resolve_template_roots(
    source_root: Path,
    harness: str | tuple[str, ...] | list[str],
) -> list[Path]:
    """선택 집합에 대응하는 ``templates/<harness>/`` 경로들. 없으면 fail-loud."""
    selected = (
        install_rules.parse_harness_selection(harness)
        if isinstance(harness, str)
        else tuple(harness)
    )
    roots: list[Path] = []
    for harness_name in selected:
        for name in install_rules.HARNESS_TEMPLATE_DIRS[harness_name]:
            root = source_root / "templates" / name
            if not root.is_dir():
                raise FileNotFoundError(
                    f"소스 어댑터 트리 없음: {root}. "
                    f"--from 이 올바른 프레임워크 checkout 인지 확인하라 "
                    f"(templates/{name}/ 필요). 또는 --harness claude 처럼 설치할 하네스를 "
                    f"좁혀라."
                )
            roots.append(root)
    return roots


def _has_harness_templates(root: Path, harness: str) -> bool:
    """root 가 harness 의 어댑터 소스 트리(templates/<dir>/…)를 전부 보유하는가."""
    return all(
        (root / "templates" / name).is_dir()
        for name in install_rules.HARNESS_TEMPLATE_DIRS[harness]
    )


def _pm_update_read_manifest(manifest_path: Path) -> list:
    """manifest_store.read_manifest 재사용 (부재는 빈 리스트·파싱 실패는 전파). 새 파서 신설 없음."""
    if not manifest_path.is_file():
        return []
    return list(manifest_store.read_manifest(manifest_path))


def _dest_manifest_core_paths(dest_root: Path) -> set[str]:
    """dest engine.manifest 의 **core**(guest 절 제외) 경로 집합 — add-harness 복사/등재 차감 기준
    host 가 pm_update 로 이미 관리(소유)하는 경로다.

    옛 flavor-native 판정(`_engine_render_relpaths`)은 *guest flavor* 관점이라 **claude-as-guest** 를
    놓쳤다 — dest 실소유로 대체한다. guest 절(add-harness 자기 산출·refresh 재복사 대상)은 stripped 라
    제외 → refresh 가 guest 를 재복사한다. manifest 부재는 빈 set(무차감)."""
    manifest = dest_root / ".project_manager" / "engine.manifest"
    if not manifest.is_file():
        return set()
    return manifest_rules._core_manifest_paths(fs.read_text_shared(manifest, encoding="utf-8"))


def _flavor_render_relpaths(template_root: Path) -> set[str]:
    """guest flavor manifest 의 `@render` 선언 경로 **전부** (namespace 무관·host 실소유 미차감·손-열거 0).

    add-harness 복사/등재 후보의 **단일 출처**: flavor 가 `@render` 로 선언한 경로가 곧
    그 하네스가 관리하는 footprint 다. 여기엔 **cross-ns 의존물**도 포함된다 — opencode flavor 의
    `.claude/skills @render`(PM 스킬 채널·네이티브 소비)는 `.opencode` namespace 밖이지만
    opencode 어댑터가 반드시 소비한다. host 실소유 차감은 이 위에 downstream 으로 얹힌다(복사=
    `_in_adapter_namespace`·등재=`_guest_render_sync_plan`·둘 다 dest 실소유 `_path_owned_by` 기준). manifest
    부재·파싱 실패는 빈 set(무동작)."""
    manifest = template_root / ".project_manager" / "engine.manifest"
    if not manifest.is_file():
        return set()
    return {str(e).replace("\\", "/") for e in _pm_update_read_manifest(manifest)
            if getattr(e, "render", False)}


def _guest_manifest_lines(
        template_root: Path, adapter_dirs: tuple, root_doc: str,
        dest_owned: set[str]) -> list[str]:
    """add-harness 가 레이다운하는 guest 어댑터의 manifest **후보** 라인 (dest 등재용·손-열거 0).

    한 절에 두 종류가 모이고 **한 줄의 `@render` 유무가 소유 채널을 가른다**(새 어휘 0):

      - **어댑터 렌더물**(`@render @target-owned [@source=…]`) — 재렌더 전파(update 채널이 conf 로
        다시 렌더한다·손편집은 되돌아간다).
        후보 = guest flavor manifest 의 `@render` **선언 전부**(`_flavor_render_relpaths` 와 같은
        판정). flavor `@render` 선언 자체가 이미 경계다 — flavor 는 자기가 관리하는 경로만 `@render`
        로 선언하므로("flavor 미선언 경로 유입 0" 불변식의 구조적 보장) namespace cap 없이 **cross-ns
        의존물**(opencode 의 `.claude/skills @render` — 네이티브 소비이나 `.opencode` 밖)도 후보다.
      - **엔진 파일**(`@source=… @target-owned`) — pm_update 소유(byte-copy 전파).
        후보 = flavor manifest 의 **비-`@render`** 엔트리 중 **복사 술어**(`_in_adapter_namespace`)를
        통과하는 것. 복사와 같은 술어를 쓰므로 "등재 ⊆ 복사" 가 구조적으로 보장된다 — 별도 판정을
        두면 claude-as-guest 처럼 복사하지 않은 경로를 등재해 pm_update 가 없던 파일을 만든다.
        이 행이 없으면 `.codex/pm_orch_codex.py`·`.opencode/lib`·claude ctx 가드처럼 엔진 패키지(`pm_engine`)
        와 짝인 engine-mirror 가 설치 시점 사본으로 영구 동결된다(코어↔드라이버 skew).

    host 가 이미 소유한 것은 downstream 차감(`_guest_render_sync_plan` 의 `_path_owned_by`·기준
    `_core_manifest_paths`)이 dest 기준으로 정확히 뺀다(엔진 행은 `_in_adapter_namespace` 가 같은
    `dest_owned` 로 여기서 이미 뺀다). 두 종류 모두 host 인스턴스 소유라 `@target-owned` 를 붙인다 —
    렌더물은 재렌더 clobber 계약(MF-2), 엔진 행은 upstream flavor 부재 시 loud `[skip]` + rc0 이다.
    직렬화는 `manifest_rules._manifest_entry_line` 재사용(마커 순서 결정적·손 f-string 금지)."""
    entries = _pm_update_read_manifest(
        template_root / ".project_manager" / "engine.manifest")
    render_paths = {str(e).replace("\\", "/")
                    for e in entries if getattr(e, "render", False)}
    lines: list[str] = []
    for entry in entries:
        rel = str(entry).replace("\\", "/")
        render = bool(getattr(entry, "render", False))
        if not render and not install_rules._in_adapter_namespace(
                Path(rel), adapter_dirs, root_doc, dest_owned, render_paths):
            continue
        lines.append(manifest_rules._manifest_entry_line(manifest_rules.ManifestEntry(
            rel, render, True, getattr(entry, "source_rel", None))))
    return sorted(lines, key=lambda line: line.split()[0])


# PM 어댑터 고유 판별자로 인정하는 이름 관례 — 파일명/디렉토리에 이 접두사가 있으면 PM 이
# 깔아 준 자산이다(`pm_orch_claude.py`·`pm-instructions.md`·`skills/pm-bootstrap/`). 어댑터
# 네임스페이스만 보면 일반 프로젝트가 자기 용도로 만든 `.codex/`·`AGENTS.md` 를 PM 설치로
# 오인한다 — 그 오판은 표기 독자 집합을 부풀려 없는 하네스의 호출법을 출하한다.
_PM_ASSET_NAME_PREFIXES = ("pm-", "pm_")
