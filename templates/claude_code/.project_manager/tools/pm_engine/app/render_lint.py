"""렌더 산출물 범위·출하 템플릿 대조·미해소 토큰 누출 검사 (층: app)."""
from __future__ import annotations

from pathlib import Path
import re
from pm_engine.app import update_render
from pm_engine.infra import fs
from pm_engine.infra import manifest_store
from pm_engine.infra import pm_paths
from pm_engine.infra import shipping_targets


# ── render-leak (리터럴 `{{...}}` 누출 차단) ──────────────────
# @render 어댑터 파일 = render_adapter 산출물(operational 토큰 치환). half-rendered
# 토큰(`{{...}}` 잔존)이 *출하 산출물* 에 새 나가면 harness-load 에이전트 지시가 무음 열화하므로
# 실결함 — blocking(경고 아님).
#
# 스캔 대상 = **@render manifest path 의 산출물**
#    .opencode/agents·command). 토큰은 @render 산출물 path 에서만 leak 으로 간주된다 —
#    adapter_render(post-render assertion) + 이 lint(상시 backstop)가 함께 자족성을 보증한다.

# leak 스캔 토큰 — 대문자/언더스코어 placeholder (`{{PROJECT_NAME}}`·`{{PROTECTED_PATHS}}` 등).
# render_rules._ANY_TOKEN_RE 와 동형(소문자/공백 토큰은 산문이라 제외·오탐 0).
_RENDER_TOKEN_RE = re.compile(r"\{\{[A-Z_]+\}\}")


def _render_managed_relpaths(home) -> set[str]:
    """engine.manifest 에서 `@render` 태그가 붙은 path 들(repo 기준 relpath·POSIX) — 검사 대상.

    manifest_store.read_manifest 를 재사용해 `.render` 플래그가 True 인 항목만 모은다. manifest
    부재·파싱 실패는 빈 set(검사 대상 0·무발화). manifest 의 @render path 가 디렉토리면 그
    하위 출하 어댑터가 전부 산출물이므로 prefix 매칭에 쓴다.

    **트리 성격 게이트 (local.conf 의미론)**: render-leak 은 *렌더
    산출물*(operational 토큰이 concrete 로 치환된 어댑터)의 미해소 토큰을 잡는 가드다. 그런데
    토큰-form *소스 트리*(canonical worktree)는 산출물이 아니라 출하 전 원본이라 토큰이 정상
    이다. local.conf 부재 ⟺ 소스 트리(채택/init 전), 존재 ⟺ 채택 인스턴스(render 산출물 보유)
    이므로, local.conf 가 파일로 없으면 검사 대상 0(무발화)으로 잘라낸다 — `.opencode`(templates
    =소스)가 스캔에서 빠지는 것의 *트리-단위 일반화*. 이로써 루트 manifest 가 `.claude/* @render`
    여도 worktree(local.conf 부재)에선 토큰-form 어댑터를 산출물로 오인하지 않는다.
    ⚠️ 단 도그푸딩 worktree(adopter#0)는 local.conf 를 **보유**해 이 트리 게이트만으론 부족하다 —
    그 트리의 토큰-form 출하 원본 면제는 `_template_mirror_report` 가 파일 단위로 보완한다.
    """
    if not (home.root / ".project_manager" / "local.conf").is_file():
        return set()  # 토큰-form 소스 트리(local.conf 부재) — render 산출물 아님.
    return _manifest_render_relpaths(home)


def _manifest_render_relpaths(home) -> set[str]:
    """engine.manifest 의 `@render` dest 경로 집합 (repo 기준 relpath·POSIX·local.conf 게이트 무관).

    순수 manifest 파생 — render-leak(`_render_managed_relpaths` 가 트리성격 게이트 후 호출)의
    스코프 소스다. 출하 인벤토리(engine.manifest `@render` = 프레임워크가 렌더-관리하는 어댑터
    본문 트리)에서 스코프를 파생하므로, 새 하네스·새 어댑터 항목이 manifest 에 등재되면 자동
    편입된다(손-열거 0). config·hooks·엔진 코드·`node_modules` 등 런타임/설정
    파일은 `@render` 가 아니라 `@source`(byte-copy)/미등재라 이 집합에 애초에 안 든다 — 스코프가
    구조적으로 어댑터 본문으로 좁혀진다. manifest_store.read_manifest 재사용. manifest 부재는 빈
    set(검사 대상 0·무발화), 판독 예외는 전파한다."""
    managed: set[str] = set()
    for manifest_path in _engine_manifest_paths(home):
        for entry in manifest_store.read_manifest(manifest_path):
            if getattr(entry, "render", False):
                managed.add(str(entry).replace("\\", "/"))
    return managed


def _engine_manifest_paths(home) -> list[Path]:
    """render-leak 검사 대상 engine.manifest — **루트 manifest 만** (렌더 산출물 트리).

    render-leak 은 *렌더 산출물*(operational 토큰이 concrete 로 치환된 어댑터 .md)에서 미해소
    토큰을 잡는 가드다. 그 산출물 트리는 **루트 트리**다 — 채택자는 루트 manifest 가 @render 면
    루트 `.claude/`·`.opencode/` 가 렌더된 산출물이다. 도그푸딩 모노레포(이 repo·canonical)는
    루트 manifest 가 `.claude/* @render` 여도 토큰-form 소스라 산출물이 아니다 — 그 트리-성격
    판별은 `_render_managed_relpaths` 의 local.conf 게이트가 한다(부재=소스 트리→검사 0
    render-overlay 의미론). 따라서 이 함수는 manifest *위치*만 정하고, 토큰-form 소스의 무발화는
    local.conf 게이트가 보장한다 — 다만 local.conf 를 보유한 도그푸딩 worktree 에선 그 게이트가
    부족해 파일 단위 보완(`_template_mirror_report` 의 출하-템플릿 mirror 면제)이 붙는다.

    ⚠️ `templates/<harness>/` 는 **스캔하지 않는다**: 출하 템플릿은 *token-form 소스*다(`--target`
    이 copy2 로 토큰을 보존). 그 manifest 가 `.claude/agents @render` 여도 그건 *채택자가 import/
    update 할 때 렌더하라*는 표식이지 템플릿 자신이 렌더 산출물이란 뜻이 아니다 — 템플릿은 늘 토큰을
    가지므로 스캔하면 영구 오탐
    "활성화 시 템플릿이 렌더된다"는 오해에 기반했다(템플릿은 렌더되지 않음).

    `.is_file()` 가드로 존재할 때만."""
    out: list[Path] = []
    root_manifest = home.root / ".project_manager" / "engine.manifest"
    if root_manifest.is_file():
        out.append(root_manifest)
    return out


def _is_render_managed(rel_posix: str, managed: set[str]) -> bool:
    """rel_posix 가 @render manifest path(파일 정확일치 OR 디렉토리 prefix) 하위인지."""
    for m in managed:
        if rel_posix == m or rel_posix.startswith(m.rstrip("/") + "/"):
            return True
    return False


# 출하 템플릿 mirror 판정 상태 (`_template_mirror_report` 반환값) — 면제/힌트 분기의 단일 판정원.
_TEMPLATE_MIRROR_IDENTICAL = "identical"  # 템플릿 사본과 byte-identical → 토큰-form 출하 원본(면제)
_TEMPLATE_MIRROR_DIFFERS = "differs"      # 같은 상대경로가 있으나 내용 다름 → 전파 누락 의심(면제 없음)
_TEMPLATE_MIRROR_ABSENT = "absent"        # 대응 사본 없음 → 일반 render 산출물(면제 없음)


def _shipping_template_render_scopes(home) -> list[tuple[Path, set[str]]]:
    """면제 후보 = `(출하 템플릿 루트, 그 manifest 의 @render 경로 집합)` 목록.

    세 겹 파생 — 손-열거 0 + 성격 게이트 + 범위 게이트:
      이름 축 = `infra.shipping_targets.discover_target_names(home)` 재사용(`--all-targets` 가 쓰는
         그 규칙). 판정 사본을 새로 만들지 않는다(인접 규범 동일). render-leak 판정 축과 같은 실행
         Home 을 넘긴다.
      경로 축 = `infra.shipping_targets.resolve_target_root(home, name)` 재사용 — 단일 path segment 검증 + resolve()
         후 parent 가 `templates/` 실경로임을 이중 확인한다(symlink 탈출 거부·거기 이미 있는 검증).
         `templates/alias -> ..` 같은 링크를 막지 않으면 candidate 가 루트 파일 *자기 자신*으로
         해소돼 byte-identical 이 자명 성립 → 전 파일 면제라는 백스톱 붕괴가 난다.
      성격·범위 축 = `templates/<name>/.project_manager/engine.manifest` 의 **`@render` 선언**.
         manifest 존재만으로는 부족하다 — 빈/무관 manifest 를 둔 트리에 같은 바이트 파일을 놓으면
         실 leak 이 면제된다. 그 템플릿이 *렌더-관리한다고 선언한* 경로(`_is_render_managed` 로
         파일 정확일치/디렉토리 prefix 판정) 하위일 때만 면제 근거가 된다. @render 0 개인 트리는
         후보에서 아예 빠진다(디렉토리 이름·엔진 사본만 흉내낸 트리 봉쇄).

    manifest 부재·`@render` 0 개인 후보는 제외한다 — **면제는 특권이지 기본값이 아니다**(판정원이
    없으면 면제하지 않고 leak 을 보고하는 쪽이 보수적). 타깃 열거·탈출 검증(ValueError)·manifest
    판독 예외는 전파해 lint 가 실패한다."""
    names = shipping_targets.discover_target_names(home)
    scopes: list[tuple[Path, set[str]]] = []
    for name in names:
        # 탈출 검증 포함 — symlink 탈출은 ValueError 로 전파한다.
        root = shipping_targets.resolve_target_root(home, name)
        manifest = root / ".project_manager" / "engine.manifest"
        if not manifest.is_file():
            continue
        render_managed = {
            str(entry).replace("\\", "/")
            for entry in manifest_store.read_manifest(manifest)
            # `@source=<path>` remap 항목은 제외한다 — 아래 후보 조립(`template_root / rel_posix`)
            # 이 "사본은 dest 와 같은 상대경로"를 전제하는데, source-remap 은 그 전제를 깬다
            # (`ManifestEntry.source_rel`). 전제가 안 서는 항목은 면제 판정 범위 밖으로
            # 두는 쪽이 보수적이다(오면제 대신 leak 보고). 완전 해소(remap 경로 추적)는 후속.
            if getattr(entry, "render", False) and not getattr(entry, "source_rel", None)
        }
        if render_managed:
            scopes.append((root, render_managed))
    return scopes


def _template_mirror_report(home, path: Path, rel_posix: str,
                            template_scopes: list[tuple[Path, set[str]]] | None = None
                            ) -> tuple[str, list[str]]:
    """루트 @render 파일과 출하 템플릿의 같은 상대경로 사본의 관계를 판정한다.

    engine.manifest 의 ``@render`` 는 *채택 인스턴스의 dest* 에서 render 하라는 선언이다. 그러나
    프레임워크 공개 루트는 그 dest 를 동시에 canonical source 로 보관한다: 출하 템플릿 트리
    (`_shipping_template_render_scopes`)가 **@render 로 선언한 경로** 아래 같은 상대경로와
    byte-identical 이면 아직 render 산출물이 아니라 출하할 token-form 원본이다
    (`_TEMPLATE_MIRROR_IDENTICAL` → 면제). 도그푸딩 worktree 는 local.conf 를 보유하므로 트리
    게이트만으론 이 성격을 가를 수 없다 — 이 파일 단위 판정이 그 보완이다.

    경로를 손열거하지 않고 템플릿 트리와 상대경로의 대응으로 성격을 파생한다. 한 바이트라도 다르면
    면제하지 않는다(`_TEMPLATE_MIRROR_DIFFERS`) — 템플릿을 가진 트리에서 생긴 실제 half-rendered
    leak 은 계속 lint 하고, 그 상태는 "루트 어댑터만 고치고 전파를 안 했다"는 별도 힌트를 낳는다.

    **다중 템플릿은 first-match 가 아니라 전수 집계**다: 한 상대경로를 여러 타깃이 @render 로 선언할
    수 있고(실재 — `.claude/skills` 는 claude_code·opencode 양쪽 범위), 첫 일치에서 조기 반환하면
    *다른* 타깃의 미전파 drift 가 면제 뒤에 숨는다(`--all-targets` 누락을 놓친다). 적용 후보를 전부
    보고 **하나라도 다르면 DIFFERS 우선**(면제 없음·전파 힌트 finding), 전부 같을 때만 면제한다.
    **선언 범위 안의 사본 부재·확인 실패도 drift** 다 — 신규 파일이 한 타깃에만 전파된 상태에서
    "없으니 비참여"로 넘기면 그 누락이 그대로 숨는다.

    적용 후보가 하나도 없으면(모든 타깃의 @render 범위 *밖*이거나 후보 트리 0) `_TEMPLATE_MIRROR_ABSENT`
    (일반 렌더 산출물·면제 없음). candidate 가 symlink 로 템플릿 트리를 벗어나는 경우는 둘로 가른다 —
    **루트 산출물 자기 자신**으로 해소되면 byte 비교가 자명 성립하므로 비참여(면제도 drift 도 아님),
    그 밖의 트리 밖 링크는 전파 상태를 신뢰할 수 없으므로 drift 로 집계한다. 트리 루트 탈출은
    `_shipping_template_render_scopes` 가 이미 막고, 여기선 파일 단위 containment 를 본다.

    반환은 `(state, drifted_targets)` — drift 후보의 타깃 이름(정렬)을 함께 돌려 finding 문구가 *어느*
    타깃이 어긋났는지 지목하게 한다. `template_scopes` 는 호출부가 루프 밖에서 한 번 구한 결과를
    재사용하기 위한 주입 지점(파일마다 타깃 열거·manifest 재파싱 방지)."""
    try:
        source_bytes = fs.read_bytes_shared(path)
    except OSError:
        return _TEMPLATE_MIRROR_ABSENT, []
    try:
        source_resolved = path.resolve()
    except OSError:
        source_resolved = path
    scopes = (_shipping_template_render_scopes(home) if template_scopes is None else template_scopes)
    # 조건부 표기 렌더가 켜진 @render source는 타깃 사본과 의도적으로 byte-different다. 비교 전에
    # 갱신기의 최소 표기 렌더(`app.update_render`)로 source를 같은 타깃 산출값으로 바꿔야 진짜 drift만 남는다.
    matched = 0                     # 판정 참여 후보 수(@render 선언 + 비-자기참조).
    drifted_targets: list[str] = []  # 그중 drift(내용 불일치·사본 부재·확인 실패·트리 밖 링크) 타깃.
    for template_root, render_managed in scopes:
        # 그 템플릿이 @render 로 선언한 경로 하위가 아니면 면제 근거가 없다(범위 게이트·비참여).
        if not _is_render_managed(rel_posix, render_managed):
            continue
        candidate = template_root / rel_posix
        try:
            exists = candidate.is_file()
            resolved = candidate.resolve() if exists else None
            if resolved is not None and not resolved.is_relative_to(template_root):
                # 링크가 루트 산출물 *자기 자신*을 가리키면 비교가 자명 성립 → 판정 비참여.
                if resolved == source_resolved:
                    continue
                # 그 밖의 트리 밖 링크는 사본이 실제로 전파됐는지 알 수 없다 → drift.
                drifted = True
            else:
                # 범위 안인데 사본이 **아예 없으면** 신규 파일 미전파 = drift 다(면제 근거 아님).
                expected = update_render.render_skill_entry_notation(
                    home,
                    source_bytes.decode("utf-8"),
                    template_root.name,
                    source=str(path),
                ).encode("utf-8")
                drifted = (not exists) or fs.read_bytes_shared(candidate) != expected
        except (OSError, UnicodeError, RuntimeError):
            drifted = True  # 사본 상태를 확인 못 했다 — 확인 못 한 것을 면제하지 않는다(보수).
        matched += 1
        if drifted:
            drifted_targets.append(template_root.name)
    if drifted_targets:
        return _TEMPLATE_MIRROR_DIFFERS, sorted(drifted_targets)
    return (_TEMPLATE_MIRROR_IDENTICAL if matched else _TEMPLATE_MIRROR_ABSENT), []


def lint_render_leak(home) -> list[tuple[str, str, str]]:
    """render 산출물에 리터럴 `{{...}}` 누출 차단 (kind=`render-leak`·blocking).

    `cli.board_lint._ADVISORY_LINT_KINDS` 밖 → `lint --gate` 차단 → pre-push exit 1(dangling-wikilink 미러).
    half-rendered 토큰은 harness-load 에이전트 지시의 무음 열화라 실결함(경고 아님).

    **트리 성격 무발화 경계**: 검사 대상 = engine.manifest 에서 `@render` 태그가 붙은 path 의
    산출물뿐(`_render_managed_relpaths`). 그 헬퍼는 local.conf 부재 트리(토큰-form 소스
    canonical)를 검사 0 으로 잘라낸다(local.conf=트리 성격 판별)
    — 루트 manifest 가 `.claude/* @render` 여도 소스 트리에선 무발화, 채택 인스턴스(local.conf
    보유·render 산출물)에선 미해소 토큰을 잡는다. **단 도그푸딩 worktree 는 local.conf 를 보유해
    트리 게이트가 부족하다** — 출하 템플릿과 byte-identical 인 토큰-form 원본은 파일 단위로
    면제한다(`_template_mirror_report`). adapter_render의 post-render assertion 과 2중
    backstop — pm_update 가 마지막 도구였는지 무관한 상시 가드.

    fail-soft: manifest 부재·파싱 실패·파일 read 오류 → 그 부분 skip(검사 대상 0·신규 무영향).
    """
    managed = _render_managed_relpaths(home)
    if not managed:
        return []  # @render path 0 → 검사 대상 0 (활성화 전 무발화).
    # 텍스트 판정은 `app.update_render._is_text_source` 를 **공유**한다: render-leak 은
    # `.md` 뿐 아니라 @render 산출물 하위 *모든 텍스트 파일*(.toml·.json·.yaml·확장자 없는 텍스트
    # 등)의 미해소 토큰을 잡아야 한다 — `.md` 만 스캔하면 새 하니스 형식을 조용히 놓치는 클래스
    # (codex `.codex/agents/*.toml`)가 이 blocking 백스톱을 통과한다. 확장자 열거·판정 사본을 새로
    # 만들지 않고 render 채널(`app.update_plan.plan`)이 쓰는 그 함수에 위임한다(네 번째 판정 지점 방지).
    # 출하 템플릿 후보는 파일마다 다시 구하지 않는다(타깃 열거·manifest 재파싱 방지).
    template_scopes = _shipping_template_render_scopes(home)
    issues: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for managed_rel in sorted(managed):
        target = home.root / managed_rel
        files: list[Path] = []
        if target.is_dir():
            files = sorted(p for p in target.rglob("*") if p.is_file())
        elif target.is_file():
            files = [target]
        for p in files:
            rel_posix = pm_paths._rel_to_repo(home, p).replace("\\", "/")
            if rel_posix in seen:
                continue
            seen.add(rel_posix)
            # 바이너리 리소스(폰트·이미지 등)는 스캔 대상 아님 — 텍스트 판정 공유(위 주석).
            if not update_render._is_text_source(p):
                continue
            try:
                # rglob("*") 로 넓히면 바이너리가 섞여 UnicodeDecodeError 로 죽을 수 있어
                # OSError 와 함께 잡는다 (_is_text_source 통과 후 TOCTOU 안전판).
                text = fs.read_text_shared(p, encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            leaked = sorted(set(_RENDER_TOKEN_RE.findall(text)))
            if leaked:
                # 공개 루트의 byte-identical template 원본은 token-form 이 정상이다. 반대로 내용이
                # 달라진 렌더 산출물은 아래 finding 으로 떨어진다(_template_mirror_report 참조).
                mirror_state, drifted_targets = _template_mirror_report(
                    home, p, rel_posix, template_scopes)
                if mirror_state == _TEMPLATE_MIRROR_IDENTICAL:
                    continue
                if mirror_state == _TEMPLATE_MIRROR_DIFFERS:
                    # 전파 대상인데 사본이 어긋났다(내용 불일치·부재) = 루트만 고치고 전파를 안 했을
                    # 가능성이 먼저다 — 채널 오진단(overlay/local.conf 만 뒤지기)을 막는 힌트 +
                    # 어느 타깃이 어긋났는지 지목(바로 그 타깃만 재전파하면 된다).
                    cause = (f"@render 관리 path — 출하 템플릿 사본과 불일치"
                             f"({', '.join(drifted_targets)}): 루트 어댑터 수정 후 "
                             "`pm_update --all-targets` 미전파 가능성 (또는 overlay/local.conf 채널 "
                             "누락·미배선 토큰)")
                else:
                    cause = "@render 관리 path — overlay/local.conf 채널 누락 또는 미배선 토큰"
                issues.append((
                    rel_posix, "render-leak",
                    f"render 산출물에 미해소 토큰 잔존: {', '.join(leaked)} ({cause})"))
    return issues
