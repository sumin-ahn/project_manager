#!/usr/bin/env python3
"""engine.manifest 기반 배포 sync — upstream 엔진 경로만 덮어쓴다.

엔진/상태 분리의 managed-manifest 배포. 인스턴스 상태(tickets·status·log·
decisions/*.md·areas.md…)와 per-clone 로컬(board.md·pm_state·local.conf·.local)은
manifest 밖이라 절대 건드리지 않으므로, upstream 갱신이 인스턴스와 *구조적으로*
충돌하지 않는다 (수동 MERGE 백포트의 기계화).

사용:
    # 인스턴스/타깃 내부에서 실행 (self-location):
    python3 .project_manager/tools/pm_update.py --from <upstream-checkout> [--dry-run] [--version V]
    # --from 생략 시 dest local.conf 의 upstream= 을 기본으로 쓴다(pm_import 가 자동 기록·T-0053):
    python3 .project_manager/tools/pm_update.py [--dry-run]

    # 루트(upstream)에서 특정 templates 타깃으로 동기화:
    python3 .project_manager/tools/pm_update.py --from <upstream-checkout> --target <name> [--dry-run]
    # 예: --target opencode  →  templates/opencode/ 에 동기화

동작:
  engine.manifest 의 각 경로를 <upstream>/<path> → <dest-root>/<path> 로 복사(overwrite).
  디렉토리는 재귀. manifest 에 없는 경로는 무시. --dry-run = 변경 예정만 출력(미적용).
  --target 지정 시 dest-root = REPO/templates/<target>/ (타깃 자신의 manifest 우선).

결정:
  - merge 아니라 overwrite (엔진은 upstream 단일 진실). 커스터마이즈 가능 문서는 manifest 에서
    제외 — 채택자 customization 은 local.conf(operational)·canonical home(free-form FILL)이 보존.
  - 어떤 경로를 엔진으로 볼지는 *dest-root 의* engine.manifest 가 정한다(없으면 source 의 것).
  - stdlib 만. plan/apply 분리로 테스트 결정론.
"""

from __future__ import annotations

import argparse
import filecmp
import importlib.util
import os
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MANIFEST = REPO / ".project_manager" / "engine.manifest"
VERSION_FILE = REPO / ".project_manager" / "engine.version"
DEFAULT_REVIEWER_CMD = "codex exec --sandbox read-only --skip-git-repo-check"

# manifest 의 render 태그 (T-0131·§3.3) — path 행 끝 `  @render` 면 byte-copy 대신 render_adapter.
RENDER_TAG = "@render"
# manifest 의 target-owned 태그 (T-0137) — path 행 끝 `  @target-owned` 면 그 경로는 타깃 자신만
# 보유하는 어댑터다(엔진 upstream/루트에 source 부재가 정상). source-부재 skip 의 *명시* 판별자.
# `@render` 와 독립 — `.claude/agents @render`(루트 upstream 에 존재해야 하는 엔진 리소스)는
# render=True 이지만 target_owned=False 라, 잘못된 --from 에서 빠지면 skip 이 아니라 rc2 가 된다.
TARGET_OWNED_TAG = "@target-owned"
# read_manifest 가 path 행 끝에서 떼어낼 수 있는 마커들(복수·순서 무관).
_MANIFEST_MARKERS = (RENDER_TAG, TARGET_OWNED_TAG)


class ManifestEntry(str):
    """manifest 한 경로 — `str` 서브클래스라 기존 `in`/`.startswith`/`==` 가 그대로 동작한다.

    추가 속성:
    - `render`(bool): path 행 끝에 `@render` 태그가 있으면 True(byte-copy 대신 render_adapter
      로 채운다·§3.3). 미주석=False → 오늘과 정확히 동일(순수 copy2·후방호환).
    - `target_owned`(bool): path 행 끝에 `@target-owned` 태그가 있으면 True — 타깃 자신만 보유
      하는 어댑터라 엔진 upstream 에 source-부재가 정상(전파 대상 아님). source-부재 skip 의
      명시 판별자(T-0137). `@render` 와 독립이며, 두 마커는 한 행에 같이 올 수 있다(순서 무관).

    str 을 상속함으로써 read_manifest 의 반환이 path+플래그 의미를 가지면서도 `entry in entries`·
    `e.startswith(...)` 같은 기존 호출부/테스트를 한 줄도 깨지 않는다.
    """

    render: bool
    target_owned: bool

    def __new__(
        cls, path: str, render: bool = False, target_owned: bool = False
    ) -> "ManifestEntry":
        obj = super().__new__(cls, path)
        obj.render = render
        obj.target_owned = target_owned
        return obj


class _RenderDst:
    """change tuple 의 dst — 내부 Path 에 위임하되 `.render` 플래그를 운반하는 thin 래퍼.

    plan 이 dst 에 render 여부를 실어 apply 가 byte-copy vs render 를 분기하게 한다. change
    tuple 을 4-요소로 유지(`(rel, src, dst, kind)`)해 기존 unpack 호출부/테스트를 깨지 않으면서
    render 정보를 운반한다. Path 직접 서브클래싱(버전별 `_flavour` 함정·하위 호환 약화)을 피하고
    `__fspath__`/`__eq__`/`__getattr__` 위임으로 테스트가 쓰는 표면(`dst.exists()`·`dst.parent`·
    `dst == Path(...)`·`str(dst)`·`Path(dst)`)을 모두 지원한다. 평문 Path dst(레거시 apply
    직접 호출)는 이 래퍼가 아니므로 `getattr(dst, "render", False)` 가 False → copy2(후방호환).
    """

    __slots__ = ("_path", "render")

    def __init__(self, path: Path, render: bool = False) -> None:
        self._path = Path(path)
        self.render = render

    def __fspath__(self) -> str:
        return str(self._path)

    def __getattr__(self, name):
        # _path 의 메서드/속성(exists·parent·read_text 등)으로 위임. __slots__ 정의 속성은
        # 이 메서드 진입 전 처리되므로 무한재귀 없음.
        return getattr(self._path, name)

    def __eq__(self, other) -> bool:
        if isinstance(other, _RenderDst):
            return self._path == other._path
        return self._path == other

    def __hash__(self) -> int:
        return hash(self._path)

    def __str__(self) -> str:
        return str(self._path)

    def __repr__(self) -> str:
        return f"_RenderDst({self._path!r}, render={self.render})"


def _templates_dir() -> Path:
    """REPO/templates/ 경로. 없어도 안전하게 반환 (존재 여부는 호출부가 판단)."""
    return REPO / "templates"


def _is_noninteractive() -> bool:
    """`PM_NONINTERACTIVE` env 가 truthy 면 True — 비대화 결정 신호 (T-0071).

    Windows DEVNULL stdin 의 `isatty()` 가 신뢰불가한 cross-OS 함정을 회피. truthy 판정은
    `"1"`/`"true"`/`"yes"`/`"on"`(대소문자 무관) — board._is_noninteractive 와 동일 계약
    (stdlib-only·board 미import 결합 회피). 빈/`"0"`/`"false"` 등은 미설정 취급(isatty 폴백).
    """
    return os.environ.get("PM_NONINTERACTIVE", "").strip().lower() in (
        "1", "true", "yes", "on"
    )


def maybe_prompt_external_review(dest_root: Path) -> None:
    """업데이트 후 외부 코드리뷰 opt-in (ADR-0004) — 아직 미설정이면 1회 묻는다.

    코드 diff 외부 *전송*이라 기본 OFF. 이미 결정됐거나 비대화형이면 안전쪽으로 건너뛴다.

    dest_root: 동기화 대상 루트 (루트 또는 타깃). local.conf 는 이 경로 기준으로 읽고 쓴다.
    --target 모드에서 루트 local.conf 를 오염시키지 않기 위해 반드시 effective_dest 를 전달한다.
    """
    local_conf = dest_root / ".project_manager" / "local.conf"
    if not local_conf.exists():
        return  # init 전 — board.py init 에서 묻는다
    text = local_conf.read_text(encoding="utf-8")
    if "external_review_enabled" in text:
        return  # 이미 결정됨
    # 명시적 비대화 신호 우선 (T-0071): Windows DEVNULL isatty() 신뢰불가 함정 회피.
    # PM_NONINTERACTIVE truthy 면 묻지 않고 안전쪽 skip. isatty 는 보조 폴백(env 없을 때).
    if _is_noninteractive() or not sys.stdin.isatty():
        return
    print("\n[pm_update] 외부 코드리뷰(external_review)를 켤까요? 코드 diff 를 외부 리뷰어"
          "(codex 등)로 *전송*합니다 — 내부 code-reviewer 와 상보적이나 외부 전송 발생.")
    try:
        answer = input("  켜기 [y/N]: ").strip().lower()
    except EOFError:
        answer = ""
    with local_conf.open("a", encoding="utf-8") as f:
        if answer in ("y", "yes"):
            f.write("# 외부 코드리뷰 (ADR-0004)\n"
                    "external_review_enabled=true\n"
                    f"reviewer_cmd={DEFAULT_REVIEWER_CMD}\n")
            print("  ✓ 외부 리뷰 ON (reviewer_cmd 기본 codex)")
        else:
            f.write("# 외부 코드리뷰 (ADR-0004) — 기본 OFF.\nexternal_review_enabled=false\n")
            print("  → 외부 리뷰 OFF (나중에 local.conf 로 켤 수 있음).")


def read_manifest(path: Path) -> list[ManifestEntry]:
    """manifest 파일 → ManifestEntry 리스트 ('#' 주석·빈 줄 제외·마커 파싱).

    각 항목은 `str` 서브클래스 ManifestEntry — 값은 path 문자열이고 `.render`·`.target_owned`
    속성이 그 path 의 마커 여부를 운반한다. path 행 끝의 마커(`@render`·`@target-owned`)는
    복수·순서 무관으로 인식해 전부 떼어내고 순수 경로만 ManifestEntry 값으로 남긴다.
      - `@render`(T-0131)        → render=True (byte-copy 대신 render_adapter·§3.3)
      - `@target-owned`(T-0137)  → target_owned=True (엔진 upstream source-부재가 정상·skip 판별)
    예: `.opencode/agents  @render @target-owned` → path=`.opencode/agents`, 둘 다 True.
    미주석=둘 다 False → 오늘과 동일(순수 copy2·전파 대상·후방호환).
    """
    out: list[ManifestEntry] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # 행 끝의 마커들(복수·순서 무관)을 떼어낸다 — path 와 마커, 마커끼리는 공백 구분.
        parts = line.split()
        render = False
        target_owned = False
        while parts and parts[-1] in _MANIFEST_MARKERS:
            marker = parts.pop()
            if marker == RENDER_TAG:
                render = True
            elif marker == TARGET_OWNED_TAG:
                target_owned = True
        line = " ".join(parts)
        out.append(ManifestEntry(line, render, target_owned))
    return out


def _entry_render_flag(entry) -> bool:
    """manifest 항목의 render 플래그 — ManifestEntry 면 `.render`, 평문 str(레거시 호출)면 False.

    plan() 이 `list[str]`(기존 테스트·외부 호출)과 `list[ManifestEntry]`(read_manifest) 둘 다
    받게 정규화한다 — 후방호환(평문 str 항목은 render 비대상).
    """
    return bool(getattr(entry, "render", False))


def _entry_target_owned_flag(entry) -> bool:
    """manifest 항목의 target_owned 플래그 — ManifestEntry 면 `.target_owned`, 평문 str 면 False.

    source-부재 skip 판별자(T-0137). 평문 str 항목(레거시 호출)은 target-owned 가 아니므로
    source-부재 시 엔진 누락으로 보고 rc2(후방호환·is_owned skip 은 명시 마커 한정).
    """
    return bool(getattr(entry, "target_owned", False))


def _read_local_conf(path: Path) -> dict[str, str]:
    """local.conf → key=value dict (T-0053). board.local_config 파싱 규칙 미러.

    `KEY=value` 줄만 채택. `#` 주석·빈 줄·`=` 없는 줄은 무시. 미존재 → {}. stdlib only —
    board 를 import 하지 않는다(pm_update 는 stdlib-only·결합 회피). 같은 키 중복 시 마지막 값.
    """
    conf: dict[str, str] = {}
    if not path.exists():
        return conf
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        conf[key.strip()] = val.strip()
    return conf


def _iter_files(root: Path, rel: str):
    """manifest 엔트리(파일/디렉토리) → (repo 기준 relpath, src 절대경로) 들."""
    src = root / rel
    if src.is_dir():
        for p in sorted(src.rglob("*")):
            if p.is_file():
                yield str(p.relative_to(root)), p
    elif src.is_file():
        yield str(rel), src
    # missing → 아무것도 yield 안 함 (호출부가 missing 으로 보고)


def _load_pm_render():
    """pm_render 모듈을 같은 tools/ 디렉토리에서 직접 로드 (sys.path 오염 없이·stdlib seam).

    pm_import._detected_py 가 board.py 를 로드하는 패턴과 동형 — pm_update 는 stdlib-only
    철학이나 render 분기는 pm_render(같은 엔진 동기 대상)에 위임한다. import 실패는 호출부가
    안전쪽으로 처리하게 예외를 전파(render path 인데 렌더러 없음 = 명확한 에러가 옳다).
    """
    render_py = Path(__file__).resolve().parent / "pm_render.py"
    spec = importlib.util.spec_from_file_location("pm_render", render_py)
    if spec is None or spec.loader is None:
        raise ImportError(f"pm_render 로드 불가: {render_py}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_pm_import():
    """pm_import 모듈을 같은 tools/ 에서 직접 로드 (T-0145·_load_pm_render 패턴 동형).

    upstream_rev baseline 기록(매 sync·ADR-0032 D2)에 pm_import 의 URL 안전 git 호출
    (read_upstream_rev — argv-list·timeout·GIT_TERMINAL_PROMPT=0)과 local.conf set-or-replace
    (record_upstream_rev)를 *재사용*한다 — pm_update 가 자체 git/conf-write 를 중복 구현하지
    않게(엔진 stdlib-only 철학 안에서 검증된 안전 계약을 상속). 로드 실패는 호출부가 fail-soft
    (baseline 기록은 best-effort·sync 자체를 깨지 않는다).
    """
    import_py = Path(__file__).resolve().parent / "pm_import.py"
    spec = importlib.util.spec_from_file_location("pm_import", import_py)
    if spec is None or spec.loader is None:
        raise ImportError(f"pm_import 로드 불가: {import_py}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def record_upstream_rev_baseline(dest_root: Path, source_root: Path) -> bool:
    """매 sync 후 upstream baseline revision 을 dest local.conf 에 `upstream_rev=<commit>` 기록(T-0145).

    drift-lint(T-0141)의 baseline 입력 — "마지막 동기 이후 upstream 변경분" 의 기준점이다
    (ADR-0032 D2). pm_import(import 시)와 여기(pm_update 매 sync) 둘 다 갱신해야 "마지막 동기
    이후" 가 성립한다. source_root(upstream)가 로컬 git checkout 이면 그 HEAD commit 을 읽어
    기록한다. git repo 아님·HEAD 해소 실패·pm_import 로드 실패는 **graceful 생략**(기록 안 함·
    best-effort·sync 자체는 안 깬다). URL upstream(로컬 checkout 없음)은 baseline 을 못 읽어
    생략 — 스킬층이 fetch 후 `upstream_seen_rev`(별개 키)를 기록한다(한 키 2역 금지).

    pm_import 의 read_upstream_rev(URL 안전 git 호출)·record_upstream_rev(local.conf set-or-
    replace·타 키 보존)를 재사용한다. 변경 시 True·생략/무변경 False.
    """
    try:
        pm_import = _load_pm_import()
    except Exception:  # noqa: BLE001 — 로드 실패는 baseline best-effort: sync 를 안 깬다.
        return False
    rev = pm_import.read_upstream_rev(source_root)
    if not rev:
        return False  # git repo 아님·HEAD 해소 실패 — graceful 생략(URL upstream 포함).
    return bool(pm_import.record_upstream_rev(dest_root, rev))


# local.conf key(lowercase) → operational token key(uppercase·pm_render). board.py init 은
# py·test_cmd·project_name 만 기록 — 나머지(project_root·project_tagline·date)는 local.conf
# 에 없으므로 매핑 부재 시 빈값(render 시 그 토큰이 남아있으면 leak assertion 이 잡는다·그러나
# 출하 어댑터의 operational 토큰은 import sed 로 이미 리터럴이라 render 시점엔 보통 부재 → no-op).
_LOCAL_CONF_TO_OPERATIONAL = {
    "project_name": "PROJECT_NAME",
    "project_tagline": "PROJECT_TAGLINE",
    "project_root": "PROJECT_ROOT",
    "py": "PY",
    "test_cmd": "TEST_CMD",
    "date": "DATE",
    # opencode 어댑터 전용 — pm_import 가 import 시 local.conf 에 기록(T-0033). opencode
    # @render 활성화 시 `{{OPENCODE_PRO_MODEL}}` 을 local.conf 로 재유도(claude tree 엔 부재 → no-op).
    "opencode_pro_model": "OPENCODE_PRO_MODEL",
}


def _operational_from_local_conf(dest_root: Path) -> dict[str, str]:
    """local.conf 의 operational 해소값을 pm_render 의 token-key dict 로 변환.

    local.conf 키(lowercase) → operational token key(uppercase). board.py init 이 안 쓴 키는
    포함하지 않는다(빈값 강제 안 함). 출하 어댑터의 operational 토큰은 import sed 로 이미
    리터럴이라 render 시점엔 보통 부재 — 이 매핑은 재렌더가 그 토큰을 만났을 때 local.conf
    단일 진실로 재유도하기 위한 것(§3.2).
    """
    conf = _read_local_conf(dest_root / ".project_manager" / "local.conf")
    operational: dict[str, str] = {}
    for conf_key, token_key in _LOCAL_CONF_TO_OPERATIONAL.items():
        if conf_key in conf:
            operational[token_key] = conf[conf_key]
    return operational


def _render_text(source_path: Path, dest_root: Path) -> str:
    """source 템플릿을 채택자 local.conf(operational)로 렌더한 텍스트.

    local.conf 의 operational 값을 plain replace 로 채운다(free-form 은 pm_import FILL 채널이
    canonical home 에서 전담·ADR-0030·ADR-0031). 결과는 자족(잔여 `{{...}}` 0·assertion).
    호출부(apply/plan)가 dst 와 비교/기록한다.
    """
    render_mod = _load_pm_render()
    operational = _operational_from_local_conf(dest_root)
    text = Path(source_path).read_text(encoding="utf-8")
    return render_mod.render_adapter(text, operational=operational)


def _render_eq_dst(sp: Path, dst: Path, dest_root: Path) -> bool:
    """render path 의 '변경 없음' 정직 판정 — 렌더 산출물 == dst 현재 내용 (§3.3).

    filecmp.cmp(템플릿, dst) 는 render path 에 *틀림*(템플릿은 렌더 산출물과 byte-equal 일 수
    없어 항상 update 오보). 대신 source 를 dest 의 local.conf(operational)로 렌더해 dst 와 비교한다.
    렌더 실패(렌더러 부재·assertion)는 보수적으로 '다름'(False) 취급 — plan 이 그 path 를
    change 로 띄워 apply 가 실제 렌더에서 명확히 실패하게 한다(침묵 폴백 금지).
    """
    try:
        rendered = _render_text(sp, dest_root)
        return rendered == dst.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001 — 렌더/IO 실패는 '다름'으로 보수 처리.
        return False


def plan(
    source_root: Path,
    manifest: list,
    dest_root: Path | None = None,
    *,
    render_enabled: bool = True,
) -> tuple[list[tuple], list[str]]:
    """(changes, missing) 반환. changes = [(rel, src, dst, kind)] (kind: new|update).

    dest_root: 동기화 대상 루트. None 이면 REPO(self-location) 사용.

    manifest 항목이 `ManifestEntry`(render 플래그 운반·read_manifest 산출)면 그 path 의 render
    여부를 dst(`_RenderDst` 래퍼)에 실어 apply 가 byte-copy vs render 를 분기하게 한다. 평문
    str 항목(레거시 호출)은 render=False(후방호환·순수 copy2). render path 의 변경검출은
    filecmp 대신 rendered-output 비교(`_render_eq_dst`) — 템플릿≠산출물 오보 회피(§3.3).

    render_enabled=False 면 manifest @render 태그를 *무시*하고 전부 copy2(토큰-form 보존).
    `--target`(루트→templates/<name> 동기) 경로 전용 — 템플릿은 토큰-form 소스라 절대 렌더
    대상이 아니다(local.conf 부재 → operational 토큰 leak·_assert_no_leak crash). render 는
    채택자 self-update(--target 없음·local.conf 보유)와 pm_import 경로에서만 일어난다.
    """
    effective_dest = dest_root if dest_root is not None else REPO
    changes: list[tuple] = []
    missing: list[str] = []
    for entry in manifest:
        rel = str(entry)
        # render_enabled=False(--target) 면 @render 태그를 강제로 끈다 — 템플릿은 토큰-form
        # 소스라 copy2 로 토큰을 보존해야 한다(렌더 시 operational leak·crash 회피).
        render = _entry_render_flag(entry) if render_enabled else False
        if not (source_root / rel).exists():
            missing.append(rel)
            continue
        for r, sp in _iter_files(source_root, rel):
            # render 는 `.md` 한정 — @render 디렉토리 하위의 비-.md(이미지·json 등)는 byte-copy
            # (pm_import.render_managed_files 가 이미 `.md` 한정·정렬과 동형). 산출물은 자족 .md.
            file_render = render and Path(r).suffix == ".md"
            dst = _RenderDst(effective_dest / r, file_render)
            if not dst.exists():
                changes.append((r, sp, dst, "new"))
            elif file_render:
                # render path: 템플릿이 산출물과 byte-equal 일 수 없으므로 filecmp 는 항상 오보.
                # 렌더한 결과가 dst 와 다를 때만 update(정직 판정·§3.3).
                if not _render_eq_dst(sp, dst, effective_dest):
                    changes.append((r, sp, dst, "update"))
            elif not filecmp.cmp(sp, dst, shallow=False):
                changes.append((r, sp, dst, "update"))
    return changes, missing


def apply(changes: list[tuple]) -> None:
    """change 적용 — render=False(기본)는 순수 copy2, render=True 는 render_adapter 후 기록.

    dst 가 `_RenderDst`(render 플래그 운반·plan 산출)면 그 플래그로 분기한다. 평문 Path dst
    (레거시 직접 호출)는 render 비대상 → copy2(후방호환·현 pm_update 동작 불변).
    """
    render_mod = None  # render path 가 있을 때만 lazy-load.
    for _r, sp, dst, _kind in changes:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if getattr(dst, "render", False):
            dest_root = _dest_root_for(dst, _r)
            if render_mod is None:
                render_mod = _load_pm_render()
            operational = _operational_from_local_conf(dest_root)
            text = Path(sp).read_text(encoding="utf-8")
            rendered = render_mod.render_adapter(text, operational=operational)
            Path(dst).write_text(rendered, encoding="utf-8")
        else:
            shutil.copy2(sp, dst)


def _dest_root_for(dst: Path, rel: str) -> Path:
    """change 의 dst 절대경로와 그 repo-기준 relpath 로 dest_root 를 역산한다.

    dst = dest_root / rel 이므로 dst 에서 rel 컴포넌트 수만큼 거슬러 올라가면 dest_root.
    plan 이 dst 를 effective_dest/r 로 만들었으므로 정확히 복원된다(render path 의 local.conf
    조회 기준).
    """
    parts = Path(rel).parts
    root = Path(dst)
    for _ in parts:
        root = root.parent
    return root


def resolve_target_root(target_name: str) -> Path:
    """타깃 이름 → 동기화 대상 루트 경로 (항상 REPO/templates/<target_name>/).

    source(--from)와 dest는 독립적이다:
    - source_root(--from): 엔진 파일을 읽어오는 곳
    - dest(이 함수 반환값): 이 스크립트가 속한 REPO의 templates/<target>/

    따라서 --from 이 REPO 외의 upstream 이어도 dest 는 항상 이 REPO 를 가리킨다.

    타깃 유효성은 REPO/templates/<name>/ 디렉토리 존재로 판단한다 (ADR-0006).
    새 타깃 추가가 이 파일 수정을 강제하지 않는다.

    보안: target_name 은 단일 path segment 이어야 한다.
    '/', os.sep, '..', 빈 문자열을 포함하면 path traversal 로 간주해 거부한다.
    이후 resolve() 결과의 parent 가 REPO/templates/ 임을 이중 검증한다.
    """
    # ── 1차: 단일 segment 검증 (빠른 거부) ──────────────────────────────────
    if (
        not target_name
        or "/" in target_name
        or os.sep in target_name
        or target_name == ".."
        or target_name.startswith("../")
        or ".." in target_name.split("/")
    ):
        raise ValueError(
            f"잘못된 타깃 이름: {target_name!r}. "
            "타깃은 단일 path segment 이어야 한다 ('/', '..', 빈 문자열 불허)."
        )

    # ── 2차: resolve() 후 parent 검증 (symlink·우회 방어) ───────────────────
    templates_resolved = (REPO / "templates").resolve()
    candidate = (REPO / "templates" / target_name).resolve()
    if candidate.parent != templates_resolved:
        raise ValueError(
            f"타깃 경로 탈출 시도: {target_name!r} → {candidate}. "
            f"허용 범위: {templates_resolved}/<name>."
        )

    target_root = candidate
    if not target_root.is_dir():
        templates_dir = _templates_dir()
        if templates_dir.is_dir():
            known = sorted(p.name for p in templates_dir.iterdir() if p.is_dir())
        else:
            known = []
        known_hint = ", ".join(known) if known else "(없음)"
        raise FileNotFoundError(
            f"알 수 없는 타깃 또는 디렉토리 없음: {target_name!r}. "
            f"REPO/templates/<name>/ 디렉토리를 먼저 만들어라. "
            f"현재 발견된 타깃: {known_hint}"
        )
    return target_root


def resolve_manifest_for_dest(dest_root: Path, source_root: Path) -> Path:
    """dest_root 의 engine.manifest 우선, 없으면 source_root 의 것."""
    dest_manifest = dest_root / ".project_manager" / "engine.manifest"
    if dest_manifest.exists():
        return dest_manifest
    source_manifest = source_root / ".project_manager" / "engine.manifest"
    if source_manifest.exists():
        return source_manifest
    raise FileNotFoundError("engine.manifest 없음 (dest·source 둘 다).")


def _set_console_codepage_utf8() -> None:
    # Windows 한정 — 콘솔 코드페이지를 UTF-8(65001)로 맞춘다. cp949(한국어) 콘솔에서
    # stdout reconfigure(utf-8)만으로는 콘솔이 UTF-8 바이트를 cp949 로 디코드해 한글이
    # mojibake 되므로, 콘솔 입출력 codepage 자체를 65001 로 설정해 정합시킨다 (T-0068).
    # best-effort: 콘솔 핸들 없음·권한·예외 시 조용히 통과(reconfigure 와 동형 try/except).
    # idempotent — 이미 UTF-8 콘솔엔 65001 재설정이 무해. POSIX 는 진입하지 않는다.
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    # 콘솔/파이프 출력을 UTF-8 로 재설정 — cp949 콘솔이나 리다이렉트된 stdout 에서
    # 이모지·em-dash(—) print 가 UnicodeEncodeError 로 죽는 것을 막는다 (T-0017).
    # 먼저 Windows 콘솔 codepage 를 UTF-8 로 맞춘 뒤(T-0068) 스트림을 reconfigure 한다.
    # reconfigure 미지원 스트림(테스트 캡처 등)은 hasattr 가드로 건너뛴다.
    _set_console_codepage_utf8()
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    ap = argparse.ArgumentParser(
        prog="pm_update.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "--from 생략 시 <dest>/.project_manager/local.conf 의 `upstream=` 값을 기본으로 쓴다 "
            "(pm_import 가 한 번 import 하면 자동 기록·--from 명시로 override 가능). "
            "단 upstream= 이 **URL**(릴리스 추적 기본)이면 엔진은 로컬 파일만 복사하므로 "
            "(git clone/fetch 안 함·ADR-0032 D5) 자동 진행하지 않고 명확한 에러로 멈춘다 — "
            "`pm-update` 스킬(URL→cache clone)을 쓰거나 `--from <로컬 checkout>` 을 명시하라. "
            "upstream 미등록이거나 그 경로가 부재/디렉토리 아님이어도 명확한 에러로 멈춘다(침묵 폴백 없음)."
        ),
    )
    ap.add_argument("--from", dest="source", required=False, default=None,
                    help="upstream 프레임워크 checkout 경로 "
                         "(생략 시 local.conf 의 upstream= 사용)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--version", help="동기화 후 기록할 엔진 버전 (engine.version)")
    ap.add_argument(
        "--target",
        metavar="NAME",
        help=(
            "루트에서 templates/<NAME>/ 타깃으로 동기화. "
            "REPO/templates/<NAME>/ 디렉토리가 존재하면 유효. "
            "생략 시 self-location(스크립트 위치 기준 dest) 사용."
        ),
    )
    args = ap.parse_args(argv)

    # dest_root: --target 지정 시 REPO/templates/<target>/, 아니면 REPO(self-location).
    # source(upstream)는 엔진을 읽어오는 위치일 뿐 — dest 와 무관하다. dest 를 *먼저* 해소해야
    # --from 생략 시 그 dest 의 local.conf 에서 upstream= 을 읽어 기본값으로 쓸 수 있다(T-0053).
    if args.target:
        try:
            dest_root = resolve_target_root(args.target)
        except (ValueError, FileNotFoundError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
    else:
        dest_root = None  # plan() 이 REPO fallback 사용

    effective_dest = dest_root if dest_root is not None else REPO

    # ── upstream(source) 해소 (T-0053) — 순서: ①명시 --from ②local.conf upstream= ③에러.
    #    침묵 폴백 없음. stale(부재/비-디렉토리) 경로는 자동 진행하지 않고 명확한 에러로 멈춘다.
    if args.source:
        source_root = Path(args.source).resolve()
    else:
        local_conf = effective_dest / ".project_manager" / "local.conf"
        stored = _read_local_conf(local_conf).get("upstream", "").strip()
        if not stored:
            print(
                "오류: upstream 미등록 — --from <checkout> 를 주거나 "
                f"{local_conf} 에 `upstream=` 를 등록하라 "
                "(이 프로젝트를 한 번 pm_import 하면 자동 기록된다).",
                file=sys.stderr,
            )
            return 1
        # MF1(codex·D5 경계): upstream= 이 URL(릴리스 추적 기본값·ADR-0032 D4)이면 엔진은
        #   로컬 파일만 복사하므로 `Path(url).resolve()` 했다간 "디렉터리 없음" 류로 침묵 실패한다.
        #   URL 은 디렉토리로 해소하지 말고 *명확·actionable* 에러로 멈춘다 — git freshness 는
        #   스킬층(pm-update: URL→cache clone)이거나 `--from <로컬 checkout>` 명시가 답이다.
        try:
            kind = _load_pm_import().classify_upstream(stored)
        except Exception:  # noqa: BLE001 — 분류 실패는 보수적으로 경로 취급(기존 동작·fail-soft).
            kind = "path"
        if kind == "url":
            print(
                f"오류: upstream 이 URL 이다 ({stored}) — 엔진(pm_update)은 로컬 파일만 복사한다 "
                "(git clone/fetch 안 함·ADR-0032 D5). `pm-update` 스킬(URL→cache clone 후 sync)을 "
                "쓰거나, `--from <로컬 checkout>` 으로 로컬 경로를 명시하라.",
                file=sys.stderr,
            )
            return 1
        source_root = Path(stored).resolve()

    # stale 가드: 해소된 upstream 이 부재/디렉토리 아님 → 자동 진행 금지(명확한 에러). 기존
    # missing-manifest(rc 2)와 구분되는 메시지·rc(=1)로 "upstream 자체가 잘못됐다"를 알린다.
    if not source_root.is_dir():
        origin = "--from" if args.source else f"local.conf upstream= ({effective_dest}/.project_manager/local.conf)"
        print(
            f"오류: upstream 경로가 디렉토리가 아니거나 존재하지 않음: {source_root} "
            f"(출처: {origin}). 체크아웃이 이동/삭제됐다면 --from 으로 올바른 경로를 주거나 "
            "local.conf 의 upstream= 을 갱신하라.",
            file=sys.stderr,
        )
        return 1

    # manifest: dest_root 의 것 우선, 없으면 source 의 것
    try:
        manifest_path = resolve_manifest_for_dest(effective_dest, source_root)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    manifest = read_manifest(manifest_path)
    # --target(루트→templates/<name>) 은 render 를 끈다 — 템플릿은 토큰-form 소스라 copy2 로
    # 토큰을 보존해야 한다(렌더 시 local.conf 부재 → operational leak·_assert_no_leak crash).
    # render 는 채택자 self-update(--target 없음·local.conf 보유)와 pm_import 경로에서만.
    render_enabled = not args.target
    changes, missing = plan(
        source_root, manifest, dest_root=dest_root, render_enabled=render_enabled)

    for r, _sp, _dst, kind in changes:
        # render path 는 byte-copy 가 아니라 재렌더 산출물 — PM 이 구분하게 [render] 로 표기
        # ([update] = byte-copy·§3.3 dry-run 표기). new 든 update 든 render 면 [render].
        label = "render" if getattr(_dst, "render", False) else kind
        print(f"  [{label}] {r}")

    # ── source 부재 항목 처리 (T-0137·D17 · @target-owned skip · 양 모드 공통) ──
    # manifest 의 일부는 *target-owned 어댑터* 일 수 있다 — 엔진 upstream(루트)엔 source 가
    # 없고 타깃 자신만 보유하는 경로(예: opencode `.opencode/*`). 그런 항목은 upstream→dest
    # 전파 대상이 *아니므로* rc2 로 전체를 막는 대신 graceful skip + 안내 로그로 surface 한다
    # (침묵 skip 금지).
    #
    # skip 은 **`@target-owned` 항목 한정**이다(명시 마커·T-0137). 옛 구현은 `@render` 를
    # 판별자로 썼으나 그건 틀렸다(codex 포착): `.claude/agents @render`·`.claude/skills @render`
    # 처럼 *루트 upstream 에 존재해야 하는 엔진 리소스*도 @render 라, 잘못된 --from/upstream 에서
    # 빠지면 rc2 대신 skip 으로 숨겨 엔진 누락을 은폐했다. `@target-owned` 는 @render 와 독립인
    # 명시 마커로, "upstream 이 안 들고 있어도 정상" 을 정확히 표시한다. non-`@target-owned`
    # 항목이 source-부재면 진짜 누락(오타·잘못된 --from·전파돼야 하는데 빠진 도구·@render 엔진
    # 리소스 포함)이므로 rc2 + 에러를 유지한다(silent skip 금지). 혼합이면 non-@target-owned 가
    # 전체를 막는다.
    #
    # 이 판별은 **양 모드(--target·self-update) 공통**이다. opencode 채택자의 self-update 는
    # manifest 에 `.opencode/* @target-owned` 가 있으나 upstream=프레임워크 루트(.opencode/
    # 부재·root=claude)라 source-부재 → 과거 rc2(전체 update 실패)였다. @target-owned 는 어느
    # 모드든 판별자이므로 self-update 에서도 skip 한다.
    if missing:
        # missing 은 path 문자열만 운반하므로 manifest 에서 각 path 의 @target-owned 플래그를
        # 복원한다(plan 의 render_enabled=False 는 copy/render 동작만 끄고 entry 플래그는 보존).
        target_owned_flag = {str(e): _entry_target_owned_flag(e) for e in manifest}
        owned = [r for r in missing if target_owned_flag.get(r, False)]
        engine_missing = [r for r in missing if not target_owned_flag.get(r, False)]
        for r in owned:
            print(
                f"  [skip] {r} — target-owned: upstream source 부재 "
                "(타깃 고유 @target-owned 어댑터·엔진 upstream 에 없음·전파 대상 아님)"
            )
        if engine_missing:
            for r in engine_missing:
                print(f"  [source 에 없음] {r}", file=sys.stderr)
            print(
                f"오류: 엔진 경로 {len(engine_missing)}개가 source 에 없음(non-@target-owned) — "
                "--from 경로가 올바른 엔진 upstream 인지 확인하라 "
                "(@target-owned 어댑터만 target-owned skip 대상).",
                file=sys.stderr,
            )
            return 2

    if not changes:
        print("최신 — 변경 없음.")
        return 0
    if args.dry_run:
        print(f"[dry-run] {len(changes)} 파일 변경 예정 (적용 안 함).")
        return 0

    apply(changes)
    msg = f"✓ {len(changes)} 파일 동기화"
    if args.version:
        version_file = effective_dest / ".project_manager" / "engine.version"
        version_file.write_text(args.version + "\n", encoding="utf-8")
        msg += f" · engine.version={args.version}"
    print(msg)

    # upstream_rev baseline 갱신(T-0145·ADR-0032 D2) — 매 sync 마다 source(upstream) HEAD 를
    # local.conf 에 박아 drift-lint(T-0141)의 "마지막 동기 이후" 기준점을 최신화한다. source 가
    # 로컬 git checkout 일 때만(URL upstream 은 로컬 checkout 없어 graceful 생략). best-effort —
    # 기록 실패가 동기화 자체를 무효화하지 않는다(파일은 이미 적용됨). --target 모드는 effective_
    # dest(templates/<name>)의 conf 에 기록(루트 오염 방지·maybe_prompt_external_review 와 동형).
    if record_upstream_rev_baseline(effective_dest, source_root):
        rev = _read_local_conf(
            effective_dest / ".project_manager" / "local.conf").get("upstream_rev", "")
        print(f"✓ local.conf upstream_rev baseline 갱신 (drift-lint 기준점): {rev}")

    maybe_prompt_external_review(effective_dest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
