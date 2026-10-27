"""설치 공통 import_render (app)."""
from __future__ import annotations

from pathlib import Path
from pm_engine.app import adapter_render
from pm_engine.domain import home
from pm_engine.domain import install_rules
from pm_engine.domain import render_rules
from pm_engine.infra import install_conf
from pm_engine.infra import install_files
from pm_engine.infra import install_source
from pm_engine.infra import manifest_store
from pm_engine.infra import python_env
from pm_engine.infra import skill_inventory
import sys

def _install_selected_manifest_union(merged: dict | None, dest_root: Path) -> int:
    """사전 검증된 선택 manifest 합집합을 설치한다(여기서는 source를 다시 읽지 않는다)."""
    if merged is None:
        return 0
    target = Path(dest_root) / ".project_manager" / "engine.manifest"
    target.write_text(merged["text"], encoding="utf-8", newline="\n")
    return len(merged["entries"])


# ── placeholder 치환 ───────────────────────────────────────────────────────

def _substitution_map(project_name: str, project_root: Path, today: str) -> dict[str, str]:
    return {
        "{{PROJECT_NAME}}": project_name,
        "{{PROJECT_TAGLINE}}": install_rules.DEFAULT_TAGLINE,
        "{{PROJECT_ROOT}}": str(project_root),
        "{{PY}}": python_env._detect_py(),
        "{{TEST_CMD}}": install_conf._default_test_cmd(),
        "{{DATE}}": today,
    }


def substitute_placeholders(
    dest_root: Path,
    subs: dict[str, str],
    copied_relpaths: set[Path],
    root_identity: tuple | None = None,
) -> int:
    """**이번 run 이 복사한 파일만** 대상으로 operational placeholder 치환. 변경 파일 수 반환.

    apply 단계 전용 — 복사가 끝난 dest 트리를 in-place 수정한다.

    MF1: dest 트리 전체를 rglob 하면 이번 import 가 복사하지 *않은* 기존 사용자 파일까지
    무백업 치환되어 --into 비파괴 보장을 위반한다. 따라서 범위를 copied_relpaths(plan_copy
    가 만든 actions 의 dst relpath)로 엄격히 한정한다. 복사된 파일은 충돌 시 이미 백업됐으므로
    치환해도 안전하고, 복사 안 한 사용자 파일은 절대 건드리지 않는다.

    값이 빈 문자열(`""`/`None`)인 subs 는 치환하지 않는다 — `replace(token, "")` 로 토큰을
    silent 로 비우면(예: 빈 `project.name` → " 프로젝트") 미해소 탐지 신호가 사라진다(잔여 토큰보다
    나쁨). 토큰을 남기면 @render path 는 이후 render_managed_files 의 _assert_no_leak 가 leak 으로
    잡고(같은 subs 를 render 채널에도 넘겨 빈값 힌트까지 표면화), 비-@render path 는 리터럴 토큰이
    가시적으로 남아(침묵 비움 아님) 사람이 즉시 알아챈다. 이 함수는 render *이전* 단계라
    빈값이 render 가드 도달 전에 이미 지워졌다(codex must-fix — 최초 import 경로 사각).
    """
    changed = 0
    swapped: list[str] = []
    vanished: list[str] = []
    sed_exclude = install_source._dest_sed_exclude(dest_root)  # 치환 시점·dest manifest 기준(codex must-fix)
    for rel in sorted(copied_relpaths):
        if any(part in install_source.COPY_EXCLUDE_DIR_NAMES for part in rel.parts):
            continue
        if not install_source._should_substitute(rel, sed_exclude):
            continue
        # 복사 직후라도 교체 창은 실재한다 — 선검사는 lstat, 실 IO 는 nofollow fd 로 한다.
        #   **선검사 탈락도 loud** 다: 이 범위는 방금 복사한 파일이라 부재·형상 변화가 곧 사고다.
        anomaly = install_files._copied_scope_anomaly(dest_root, rel)
        if anomaly == "vanished":
            vanished.append(rel.as_posix())
            continue
        if anomaly == "swapped":
            swapped.append(rel.as_posix())
            continue
        try:
            text = install_files.read_dest_text(dest_root, rel, root_identity=root_identity)
        except install_files.UnsafeDestPathError:
            swapped.append(rel.as_posix())
            continue
        except FileNotFoundError:
            # 선검사↔읽기 사이 삭제 경쟁 — 쓰기 쪽과 **같은 loud 제외**로 흡수한다. 일반 OSError
            #   로 삼키면 요약에 안 실려 "적용 단계 제외는 stderr 요약이 신호" 라는 규칙이 이
            #   경로에서만 깨진다(제외는 하되 반드시 알린다).
            vanished.append(rel.as_posix())
            continue
        except (UnicodeDecodeError, OSError):
            continue
        new_text = text
        # 이 파일에서 소비 시점이 소유한 토큰은 설치가 건드리지 않는다 — 굳히면
        #   manifest bare 등재 byte-copy 가 다음 sync 에 토큰-form 을 되돌려 진동한다.
        consumption_owned = install_source._consumption_time_tokens(rel)
        for token, value in subs.items():
            if token in consumption_owned:
                continue
            if value is None or value == "":
                # 빈값 subs 는 치환하지 않는다 — 토큰을 그대로 남겨 이후 render 단계
                # (render_managed_files)의 _assert_no_leak 가 leak 으로 잡게 한다(silent-empty =
                # leak 클래스). `replace(token, "")` 는 미해소를 *침묵 비움*(예:
                # `{{PROJECT_NAME}}`→"" → description 이 " 프로젝트")으로 박제해 탐지 신호 자체를
                # 없앤다 — 잔여 토큰보다 더 나쁘다. subs 는 이후
                # render_managed_files 에도 그대로 전달돼 render 채널이 같은 빈값을 힌트로 표면화한다.
                continue
            if token in new_text:
                new_text = new_text.replace(token, value)
        if new_text != text:
            try:
                install_files.write_dest_text(dest_root, rel, new_text, root_identity=root_identity)
            except install_files.UnsafeDestPathError:
                swapped.append(rel.as_posix())
                continue
            except FileNotFoundError:
                vanished.append(rel.as_posix())
                continue
            changed += 1
    install_files._report_copied_scope_anomalies("placeholder 치환", swapped, vanished)
    return changed


def unregistered_skill_notation_template_dirs(
    home: home.Home,
    template_dirs: tuple[str, ...] | list[str],
) -> list[str]:
    """render_rules 스킬 표기 registry 에 값이 없는 template dir 목록(설치 전 게이트용).

    렌더러는 호출 토큰이 있는 문서를 미등록 하네스 context 로 만나면 `RenderLeakError` 로
    중단한다(알 수 없는 하네스를 조용히 `/` 로 복사하지 않는 fail-loud). 그런데 그 판정이
    **복사 뒤 렌더 단계**라, 새 하네스를 registry 에 등록하고 표기 값을 안 넣은 채 설치하면
    파일이 다 깔린 뒤 uncaught 로 터져 **부분 설치가 남았다**. 이 함수로 *복사 전에* 같은
    조건을 판정해 `--fill auto` 미매핑 하네스 게이트와 같은 성질(파일 설치 전 중단·traceback 0·
    설치 파일 0)을 `--fill manual` 경로에도 준다.

    registry는 실행 패키지에서 직접 읽으며, 패키지 적재 오류는 실행 실패로 전파한다.
    """
    prefixes = render_rules.SKILL_ENTRY_PREFIX_BY_TEMPLATE_DIR
    labels = render_rules._HARNESS_LABEL_BY_TEMPLATE_DIR
    return [
        dirname
        for dirname in dict.fromkeys(template_dirs)
        if dirname not in prefixes or dirname not in labels
    ]


def _unregistered_skill_notation_message(unregistered: list[str]) -> str:
    """미등록 표기 하네스 안내 — main/add-harness 가 같은 문구를 쓴다(단일 진실)."""
    return (
        f"스킬 호출 표기 값이 미등록인 template 디렉터리: {', '.join(unregistered)} — "
        "파일 설치 전 중단합니다. pm_engine.domain.render_rules.SKILL_ENTRY_PREFIX_BY_TEMPLATE_DIR 와 "
        "_HARNESS_LABEL_BY_TEMPLATE_DIR 에 그 하네스의 실제 호출 표기를 등록한 뒤 다시 "
        "시도하세요(부분 설치를 남기지 않습니다)."
    )


def _render_managed_relpaths(dest_root: Path) -> set[str]:
    """복사된 트리의 engine.manifest 에서 `@render` path(repo 기준 relpath·POSIX) 집합.

    manifest_store.read_manifest 를 재사용해 `.render` True 항목만 모은다. manifest 부재
    → 빈 set(render 대상 0·무동작), 판독 예외는 전파한다. 디렉토리 path 는 하위 어댑터
    산출물의 prefix 매칭에 쓴다."""
    manifest = dest_root / ".project_manager" / "engine.manifest"
    if not manifest.is_file():
        return set()
    return {
        str(e).replace("\\", "/")
        for e in manifest_store.read_manifest(manifest)
        if getattr(e, "render", False)
    }


def render_managed_files(
    home: home.Home,
    dest_root: Path,
    subs: dict[str, str],
    copied_relpaths: set[Path],
    entry_notation_templates: dict[str, tuple[str, ...]] | None = None,
    installed_notation_context: tuple[str, ...] | None = None,
    root_identity: tuple | None = None,
) -> int:
    """이번 run 이 복사한 @render path 파일을 render_adapter 산출물로 다시 쓴다. 변경 수 반환.

    범위 = copied_relpaths(비파괴·substitute_placeholders 와 동일 보장). 대상 판정은 **@render
    manifest 선언(`_is_render_managed`) 단독**이다 — 옛 `.md` 확장자 하드 필터는 제거했다.
    확장자 조건은 manifest 선언을 덮는 중복·모순 판정이었고, `.codex/agents @render`(TOML)처럼
    선언은 맞는데 코드가 안 따라가는 형상을 만들었다. 텍스트로 못 읽는 파일은 아래 read_text 의
    UnicodeDecodeError 가 걸러낸다(byte-copy 그대로 남음).

    operational 은 이번 import 의 subs(이미 substitute 가 리터럴로 박았으므로 보통 no-op).
    free-form 은 pm_import 의 FILL 채널이 canonical home 에서 전담하므로 render-overlay 가
    관여하지 않는다.

    installed_notation_context: **이 인스턴스를 읽는 하네스 전체**의 template dir(registry
    순서·dest 실설치 ∪ 이번 선택). manifest 어느 엔트리도 소유하지 않는 인스턴스 wiki 문서
    (`_is_notation_fallback_scope`)의 표기를 이 집합으로 폴백 렌더한다 — 옛 동작은 소유자 부재를
    `continue` 로 **조용히 건너뛰어** 다중 하네스 설치에서 canonical slash 가 그대로 출하됐다
    (예 `wiki/raw/README.md` 의 `/spike-new`). 이번 run 의 선택만 넘기면 `--into` 로 하네스를
    덧붙일 때 기존 독자가 빠져 반대 방향 오표기가 되므로 호출부가 `installed_harnesses` 로 합집합을
    만든다. 폴백이 실제로 문서를 바꾸면 stderr 로 표기한다(조용한 degrade 금지). None 이면 폴백
    없음(호출자 미배선).

    subs(중괄호 포함 token→value)를 adapter_render의 bare-key operational dict 로 변환해 넘긴다."""
    managed = _render_managed_relpaths(dest_root)
    notation_contexts = entry_notation_templates or {}
    fallback_context = tuple(installed_notation_context or ())
    if not managed and not notation_contexts and not fallback_context:
        return 0
    # subs 는 `{{KEY}}`→value — adapter_render는 bare KEY 를 기대하므로 변환.
    operational = {
        token.strip("{}"): value for token, value in subs.items()
    }
    changed = 0

    def notation_context(rel_posix: str) -> tuple[str, ...] | None:
        matches = [
            (len(owner.rstrip("/")), context)
            for owner, context in notation_contexts.items()
            if rel_posix == owner.rstrip("/")
            or rel_posix.startswith(owner.rstrip("/") + "/")
        ]
        return max(matches, default=(0, None), key=lambda item: item[0])[1]

    fallback_rendered: list[str] = []
    # 계획 검증 뒤 경로가 교체된 대상 — 그 파일만 빼고 크게 알린다(아래 loud).
    swapped: list[str] = []
    # 읽은 뒤 쓰기 전에 대상이 사라진 경우(경쟁 삭제) — 같은 파일 단위 제외이되 사유가 다르다.
    vanished: list[str] = []
    for rel in sorted(copied_relpaths):
        rel_posix = rel.as_posix()
        file_render = install_rules._is_render_managed(rel_posix, managed)
        context = notation_context(rel_posix)
        notation_managed = (
            file_render
            or rel_posix.startswith(install_rules.INSTANCE_SHARED_WIKI_PREFIX)
            or rel_posix == "AGENTS.md"
        )
        # manifest 소유자 부재 = 인스턴스 소유 wiki seed. 조용히 건너뛰지 않고 설치 하네스
        #   전체 집합으로 폴백한다(단일 하네스면 그 하네스 표기·다중이면 병기).
        used_fallback = False
        if (
            context is None
            and not file_render
            and fallback_context
            and install_files._is_notation_fallback_scope(rel_posix)
        ):
            context = fallback_context
            used_fallback = True
        if not file_render and not (context and notation_managed):
            continue
        # 선검사는 `lstat` 이다 — `is_file()` 은 링크를 따라가 **깨진 symlink** 로 교체된 대상을
        #   조용히 건너뛰었다(loud 제외가 그 경로에서만 침묵으로 바뀜). 이 범위(복사분 + 계획이
        #   실재를 확인한 재렌더 대상)에서 부재·형상 변화는 사고이므로 요약에 싣는다.
        anomaly = install_files._copied_scope_anomaly(dest_root, rel)
        if anomaly == "vanished":
            vanished.append(rel_posix)
            continue
        if anomaly == "swapped":
            swapped.append(rel_posix)
            continue
        # 읽기·쓰기 모두 **경로가 아니라 fd** 로 한다 — 계획(`_is_safe_dest_path`)과 이 쓰기
        #   사이에 대상이 symlink 로 교체되면 경로 재열기는 링크를 따라 저장소 밖을 고친다.
        try:
            text = install_files.read_dest_text_keeping_newlines(
                dest_root, rel, root_identity=root_identity)
        except install_files.UnsafeDestPathError:
            swapped.append(rel_posix)
            continue
        except FileNotFoundError:
            vanished.append(rel_posix)  # 읽기 전 삭제도 loud 제외(아래 쓰기 쪽과 동형).
            continue
        except (UnicodeDecodeError, OSError):
            continue
        rendered = (
            adapter_render.render_adapter(
                home,
                text,
                operational=operational,
                template_dir=context,
            )
            if file_render
            else render_rules.render_skill_entry_notation(text, context, skill_names=skill_inventory._installed_skill_entry_names(home))
        )
        if rendered != text:
            try:
                install_files.write_dest_text_keeping_newlines(
                    dest_root, rel, rendered, root_identity=root_identity)
            except install_files.UnsafeDestPathError:
                swapped.append(rel_posix)
                continue
            except FileNotFoundError:
                # 읽기와 쓰기 사이 삭제 경쟁. `O_CREAT` 를 안 주므로 되살리지 않고, 적용 단계라
                #   전체 중단도 하지 않는다 — 교체와 같은 파일 단위 loud 제외로 흡수한다
                #   (uncaught 로 터지면 "복사 전에만 던진다 → 부분 적용 0" 불변식이 깨진다).
                vanished.append(rel_posix)
                continue
            changed += 1
            if used_fallback:
                fallback_rendered.append(rel_posix)
    if fallback_rendered:
        print(
            f"  ⚠️ manifest 미소유 인스턴스 wiki {len(fallback_rendered)}건 — 설치 하네스 전체"
            f"({', '.join(fallback_context)}) 표기로 폴백 렌더: {', '.join(fallback_rendered)}",
            file=sys.stderr,
        )
    if swapped:
        print(
            f"  ⚠️ 렌더 대상 {len(swapped)}건: 계획 검증 뒤 경로를 안전하게 열 수 없어(symlink "
            f"교체·비-디렉토리 컴포넌트) 렌더를 건너뜁니다(저장소 밖 파일 불변·비파괴): "
            f"{', '.join(swapped)}. 경로를 정리한 뒤 다시 실행하세요.",
            file=sys.stderr,
        )
    if vanished:
        print(
            f"  ⚠️ 렌더 대상 {len(vanished)}건: 계획 검증 뒤 대상이 사라져(경쟁 삭제) 렌더를 "
            f"건너뜁니다(새로 만들지 않습니다): {', '.join(vanished)}.",
            file=sys.stderr,
        )
    return changed


def render_file(
    home: home.Home,
    template_path: Path,
    operational: dict | None = None,
) -> str:
    """템플릿 파일 → 렌더 텍스트 (편의 래퍼·source 를 leak 에러에 명시).

    **공유 읽기 seam 의 등재 예외다**(템플릿과 목적지의 공통 읽기 경계). 인자는 상류 체크아웃의 템플릿
    *원본*(원자 교체 대상 아님)일 수도, 같은 상대경로의 목적지 사본(`pm_import` 가 원자 교체)일
    수도 있어 **정적으로 구분되지 않는다**. 렌더는 순수 변환이라 이 판독이 상태를 만들지 않고,
    호출부(`pm_update`·`pm_import`)의 목적지 판독은 이미 각자 seam 경로를 지난다.
    """
    text = Path(template_path).read_text(encoding="utf-8")
    return adapter_render.render_adapter(home, text, operational, source=str(template_path))
