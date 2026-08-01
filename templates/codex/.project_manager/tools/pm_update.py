#!/usr/bin/env python3
"""engine.manifest 기반 배포 sync — upstream 엔진 경로만 덮어쓴다.

엔진/상태 분리의 managed-manifest 배포. 인스턴스 상태(tickets·status·log·
decisions/*.md·areas.md…)와 per-clone 로컬(board.md·pm_state·local.conf·.local)은
manifest 밖이라 절대 건드리지 않으므로, upstream 갱신이 인스턴스와 *구조적으로*
충돌하지 않는다 (수동 MERGE 백포트의 기계화).

사용:
    # 인스턴스/타깃 내부에서 실행 (self-location):
    python3 .project_manager/tools/pm_update.py --from <upstream-checkout> [--dry-run]
    # --from 생략 시 dest local.conf 의 upstream= 을 기본으로 쓴다(pm_import 가 자동 기록):
    python3 .project_manager/tools/pm_update.py [--dry-run]

    # 루트(upstream)에서 특정 templates 타깃으로 동기화:
    python3 .project_manager/tools/pm_update.py --from <upstream-checkout> --target <name> [--dry-run]
    # 예: --target opencode  →  templates/opencode/ 에 동기화

    # 루트(upstream)에서 존재하는 모든 templates 타깃으로 동기화:
    python3 .project_manager/tools/pm_update.py --from <upstream-checkout> --all-targets [--dry-run]

    # 받은 baseline ↔ upstream HEAD 변경점만 read-only 확인 (실 sync 안 함):
    python3 .project_manager/tools/pm_update.py --changes [--from <checkout>] [--count-only] [--log]

동작:
  engine.manifest 의 각 경로를 <upstream>/<path> → <dest-root>/<path> 로 복사(overwrite).
  디렉토리는 재귀. manifest 에 없는 경로는 무시. --dry-run = 변경 예정만 출력(미적용).
  --target 지정 시 dest-root = REPO/templates/<target>/ (타깃 자신의 manifest 우선).
  sync 적용 후에는 등록 repo 전수 **보호 훅 재설치**— 훅은 엔진 코드에서 생성되는
  런타임 산출물이라 파일 복사만으론 새 훅이 배포되지 않는다(--target 은 비발화).

결정:
  - merge 아니라 overwrite (엔진은 upstream 단일 진실). 커스터마이즈 가능 문서는 manifest 에서
    제외 — 채택자 customization 은 local.conf(operational)·canonical home(free-form FILL)이 보존.
  - 어떤 경로를 엔진으로 볼지는 *dest-root 의* engine.manifest 가 정한다(없으면 source 의 것).
  - stdlib 만. plan/apply 분리로 테스트 결정론.
"""

from __future__ import annotations

import argparse
import base64
import datetime
import filecmp
import importlib.util
import os
import re
import shutil
import stat
import sys
import warnings
import zlib
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from repo_owned_files import RepoOwnedEntry

REPO = Path(__file__).resolve().parents[2]
MANIFEST = REPO / ".project_manager" / "engine.manifest"
DEFAULT_REVIEWER_CMD = "codex exec --sandbox read-only --skip-git-repo-check"


def _is_engine_rev_skew(exc) -> bool:
    """stamped sibling 로더가 표시한 사본 불일치인가."""
    return getattr(exc, "_engine_rev_skew", False)

# manifest 의 render 태그 () — path 행 끝 `  @render` 면 byte-copy 대신 render_adapter.
RENDER_TAG = "@render"
# manifest 의 target-owned 태그 — path 행 끝 `  @target-owned` 면 그 경로는 타깃 자신만
# 보유하는 어댑터다(엔진 upstream/루트에 source 부재가 정상). source-부재 skip 의 *명시* 판별자.
# `@render` 와 독립 — `.claude/agents @render`(루트 upstream 에 존재해야 하는 엔진 리소스)는
# render=True 이지만 target_owned=False 라, 잘못된 --from 에서 빠지면 skip 이 아니라 rc2 가 된다.
TARGET_OWNED_TAG = "@target-owned"
# manifest 의 source-remap 태그 — path 행 끝 `  @source=<relpath>` 면 그 경로는
# source_root 아래 canonical 소스(`<source_root>/<relpath>`)에서 읽되 dest 에는 manifest 경로로
# 기록한다(_remap_to_dest). opencode 어댑터(`.opencode/*`)가 프레임워크 루트의
# `templates/opencode/.opencode/*` 에 살지만 채택자 dest 엔 `.opencode/*` 로 전파돼야 하는 비대칭을
# 잇는다(framework-owned·claude `.claude/*` 대칭). `@target-owned`(source-부재 정상·skip)와 대비:
# @source 는 source 가 *실재*(templates/ 아래)하므로 부재면 rc2(엔진/템플릿 누락 은폐 금지).
SOURCE_TAG_PREFIX = "@source="
# read_manifest 가 path 행 끝에서 떼어낼 수 있는 boolean 마커들(복수·순서 무관). `@source=<path>` 는
# 값 운반 마커라 prefix 검사로 별도 처리(이 튜플 밖).
_MANIFEST_MARKERS = (RENDER_TAG, TARGET_OWNED_TAG)


class ManifestEntry(str):
    """manifest 한 경로 — `str` 서브클래스라 기존 `in`/`.startswith`/`==` 가 그대로 동작한다.

    추가 속성:
    - `render`(bool): path 행 끝에 `@render` 태그가 있으면 True(byte-copy 대신 render_adapter
      로 채운다). 미주석=False → 오늘과 정확히 동일(순수 copy2·후방호환).
    - `target_owned`(bool): path 행 끝에 `@target-owned` 태그가 있으면 True — 타깃 자신만 보유
      하는 어댑터라 엔진 upstream 에 source-부재가 정상(전파 대상 아님). source-부재 skip 의
      명시 판별자. `@render` 와 독립이며, 두 마커는 한 행에 같이 올 수 있다(순서 무관).
    - `source_rel`(str|None): path 행 끝에 `@source=<relpath>` 태그가 있으면 그 canonical 소스
      상대경로— source_root 아래 그 경로에서 읽되 dest 엔 manifest 경로(=`str(self)`)
      로 기록한다(_source_root_rel·_remap_to_dest). 미주석=None → source 읽기 경로 = manifest 경로
      (오늘 동작·후방호환). @render 와 공존 가능(토큰-form 소스 읽어 렌더).

    str 을 상속함으로써 read_manifest 의 반환이 path+플래그 의미를 가지면서도 `entry in entries`·
    `e.startswith(...)` 같은 기존 호출부/테스트를 한 줄도 깨지 않는다.
    """

    render: bool
    target_owned: bool
    source_rel: str | None

    def __new__(
        cls,
        path: str,
        render: bool = False,
        target_owned: bool = False,
        source_rel: str | None = None,
    ) -> "ManifestEntry":
        obj = super().__new__(cls, path)
        obj.render = render
        obj.target_owned = target_owned
        obj.source_rel = source_rel
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


class _ManifestTextSource:
    """선택된 flavor manifest 합집합 텍스트를 change tuple에 싣는 인메모리 source.

    합집합은 upstream checkout 안의 단일 파일로 존재하지 않는다. 임시 파일을 만들지 않고도
    plan/apply 4-tuple 계약을 유지하도록 ``read_text``만 제공한다. engine.manifest 전용이며
    apply의 self-prop 분기가 이 객체를 직접 소비한다.
    """

    __slots__ = ("text",)

    def __init__(self, text: str) -> None:
        self.text = text

    def read_text(self, encoding: str = "utf-8") -> str:
        return self.text


def _templates_dir() -> Path:
    """REPO/templates/ 경로. 없어도 안전하게 반환 (존재 여부는 호출부가 판단)."""
    return REPO / "templates"


def _is_noninteractive() -> bool:
    """`PM_NONINTERACTIVE` env 가 truthy 면 True — 비대화 결정 신호.

    Windows DEVNULL stdin 의 `isatty()` 가 신뢰불가한 cross-OS 함정을 회피. truthy 판정은
    `"1"`/`"true"`/`"yes"`/`"on"`(대소문자 무관) — board._is_noninteractive 와 동일 동작
    (stdlib-only·board 미import 결합 회피). 빈/`"0"`/`"false"` 등은 미설정 취급(isatty 폴백).
    """
    return os.environ.get("PM_NONINTERACTIVE", "").strip().lower() in (
        "1", "true", "yes", "on"
    )


def maybe_prompt_external_review(dest_root: Path) -> None:
    """업데이트 후 외부 코드리뷰 opt-in — 아직 미설정이면 1회 묻는다.

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
    # 명시적 비대화 신호 우선: Windows DEVNULL isatty() 신뢰불가 함정 회피.
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
            f.write("# 외부 코드리뷰\n"
                    "external_review_enabled=true\n"
                    f"reviewer_cmd={DEFAULT_REVIEWER_CMD}\n")
            print("  ✓ 외부 리뷰 ON (reviewer_cmd 기본 codex)")
        else:
            f.write("# 외부 코드리뷰 — 기본 OFF.\nexternal_review_enabled=false\n")
            print("  → 외부 리뷰 OFF (나중에 local.conf 로 켤 수 있음).")


def maybe_prompt_delegate_optin(dest_root: Path) -> None:
    """동기 후 cross-harness 위임(pm_delegate) opt-in — 아직 실키 미결정이면 (
    maybe_prompt_external_review 동형).

    delegate_enabled **실키**(주석 예시가 아니라 `_read_local_conf` 가 파싱하는 활성 키)가 이미
    있으면 결정됨 → no-op. **TTY** 면 1회 질문 — y=true·그 외/무입력=false 실키를 대상 local.conf 에
    기록한다(질문 응답 기록이 pm_update 의 **유일한 conf write 예외**·그 외 설정 write 는 board.py
    init 단일 채널). **비-TTY(CI/스크립트)** 면 질문·write 없이 도입 advisory 1줄만 표면화(기본 OFF
    유지). conf 부재(init 전)면 무발화. effective_dest 기준(--target 루트 오염 방지)."""
    local_conf = dest_root / ".project_manager" / "local.conf"
    if not local_conf.exists():
        return  # init 전 — board.py init 이 시드/질문한다
    if "delegate_enabled" in _read_local_conf(local_conf):
        return  # 실키로 이미 결정됨(주석 예시는 _read_local_conf 파싱 제외 — 미결정 취급)
    if _is_noninteractive() or not sys.stdin.isatty():
        # 비-TTY — 질문·write 없이 도입 안내만(기본 OFF 유지·write 는 질문 응답 경로 한정).
        print("[pm_update] pm_delegate cross-harness 위임 채널이 도입됐습니다(기본 OFF) — "
              "`board.py init` 재실행으로 local.conf 에 `delegate_*` 주석 시드/opt-in 질문을 받거나 "
              "수동 참조하세요(켜면 프롬프트/코드가 외부 하네스로 전송·과금).")
        return
    print("\n[pm_update] cross-harness 위임(pm_delegate)을 켤까요? 켜면 위임 프롬프트/코드가 외부 "
          "하네스로 *전송*되고 그 하네스에 *과금*됩니다.")
    try:
        answer = input("  켜기 [y/N]: ").strip().lower()
    except EOFError:
        # stdin EOF(Ctrl-D) = 기본 거절 → false 실키를 **기록**(매번 재질문 방지·opt-in 결정 박제).
        answer = ""
    # 개행 없는 conf(`…upstream_rev=abc`)에 바로 append 시 기존 값 손상 방지 — 필요 시 개행 선행.
    existing = local_conf.read_text(encoding="utf-8")
    with local_conf.open("a", encoding="utf-8") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        if answer in ("y", "yes"):
            f.write("# cross-harness 위임 — ON.\ndelegate_enabled=true\n")
            print("  ✓ cross-harness 위임 ON (delegate_enabled=true·외부 송신·과금 수용).")
        else:
            f.write("# cross-harness 위임 — 기본 OFF. 켜려면 true 로.\n"
                    "delegate_enabled=false\n")
            print("  → cross-harness 위임 OFF (나중에 local.conf delegate_enabled=true 로 켤 수 있음).")


def read_manifest(path: Path) -> list[ManifestEntry]:
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
    out: list[ManifestEntry] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # 행 끝의 마커들(복수·순서 무관)을 떼어낸다 — path 와 마커, 마커끼리는 공백 구분.
        parts = line.split()
        render = False
        target_owned = False
        source_rel: str | None = None
        while parts and (
            parts[-1] in _MANIFEST_MARKERS or parts[-1].startswith(SOURCE_TAG_PREFIX)
        ):
            marker = parts.pop()
            if marker == RENDER_TAG:
                render = True
            elif marker == TARGET_OWNED_TAG:
                target_owned = True
            elif marker.startswith(SOURCE_TAG_PREFIX):
                # `@source=<path>` — 값 운반 마커. 빈 값(`@source=`)은 무의미하므로 None 취급
                #   (source 읽기 경로 = manifest 경로·후방호환).
                source_rel = marker[len(SOURCE_TAG_PREFIX):] or None
        line = " ".join(parts)
        out.append(ManifestEntry(line, render, target_owned, source_rel))
    return out


def _manifest_entry_line(entry) -> str:
    """ManifestEntry를 손실 없이 한 manifest 행으로 직렬화한다(마커 순서 결정적)."""
    markers: list[str] = []
    if _entry_render_flag(entry):
        markers.append(RENDER_TAG)
    if _entry_target_owned_flag(entry):
        markers.append(TARGET_OWNED_TAG)
    source_rel = getattr(entry, "source_rel", None)
    if source_rel:
        markers.append(f"{SOURCE_TAG_PREFIX}{source_rel}")
    return "    ".join((str(entry), *markers))


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
    first_text = paths[0].read_text(encoding="utf-8")
    merged: list[ManifestEntry] = []
    seen: dict[str, ManifestEntry] = {}
    additions: list[tuple[str, list[ManifestEntry]]] = []
    conflicts: list[str] = []
    for index, path in enumerate(paths):
        current = read_manifest(path)
        added_here: list[ManifestEntry] = []
        for entry in current:
            key = str(entry).replace("\\", "/")
            if key in seen:
                first_markers = _manifest_marker_key(seen[key])
                next_markers = _manifest_marker_key(entry)
                if (
                    first_markers != next_markers
                    and not (
                        key == _MANIFEST_SELF_REL
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
                *(_manifest_entry_line(entry) for entry in entries),
            ])
        text = "\n".join(chunks) + "\n"
    return {
        "entries": merged,
        "text": text,
        "conflicts": sorted(set(conflicts)),
        "paths": paths,
    }


def _entry_render_flag(entry) -> bool:
    """manifest 항목의 render 플래그 — ManifestEntry 면 `.render`, 평문 str(레거시 호출)면 False.

    plan() 이 `list[str]`(기존 테스트·외부 호출)과 `list[ManifestEntry]`(read_manifest) 둘 다
    받게 정규화한다 — 후방호환(평문 str 항목은 render 비대상).
    """
    return bool(getattr(entry, "render", False))


def _entry_target_owned_flag(entry) -> bool:
    """manifest 항목의 target_owned 플래그 — ManifestEntry 면 `.target_owned`, 평문 str 면 False.

    source-부재 skip 판별자. 평문 str 항목(레거시 호출)은 target-owned 가 아니므로
    source-부재 시 엔진 누락으로 보고 rc2(후방호환·is_owned skip 은 명시 마커 한정).
    """
    return bool(getattr(entry, "target_owned", False))


def _read_local_conf(path: Path) -> dict[str, str]:
    """local.conf → key=value dict. board.local_config 파싱 규칙 미러.

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


def _load_repo_owned_files():
    """공용 seam을 복구채널 면제로 로드한다(구형/부재 helper면 인라인 폴백).

    이 도구는 부분/중단 배포를 고치는 복구 채널이므로 새 ``engine_rev`` seam 자체에 의존해
    자기잠김되면 안 된다. 폴백도 공용 seam과 같은 path-key 캐시를 써서, 이후 stamped 소비자가
    그 캐시를 다시 검증하는 성질은 그대로 보존한다.
    """
    path = Path(__file__).resolve().with_name("repo_owned_files.py").resolve()

    def load_direct():
        module_name = f"_project_manager_repo_owned_files:{path}"
        cached = sys.modules.get(module_name)
        if cached is not None:
            return cached
        direct_spec = importlib.util.spec_from_file_location(module_name, path)
        if direct_spec is None or direct_spec.loader is None:
            raise RuntimeError(
                "repo_owned_files.py를 로드할 수 없음 — 엔진 사본을 pm-update로 재동기화하라"
            )
        module = importlib.util.module_from_spec(direct_spec)
        sys.modules[module_name] = module
        try:
            direct_spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(module_name, None)
            raise
        return module

    helper_path = Path(__file__).resolve().with_name("engine_rev.py")
    try:
        spec = importlib.util.spec_from_file_location(
            "_pm_update_repo_owned_loader", helper_path
        )
        if spec is None or spec.loader is None:
            return load_direct()
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        loader = getattr(helper, "load_repo_owned_files", None)
        if loader is None:
            return load_direct()
        return loader(path, allow_unverified=True)
    # pm-update는 중단된 배포를 고치는 복구 채널이다. helper의 import·API 협상 어느
    # 단계든 실패하면(손상된 소스의 SyntaxError·구형 시그니처 TypeError 포함) 직접
    # 로드로 계속해야 자기잠김이 없다. BaseException은 종료/인터럽트 의미를 보존한다.
    except Exception:
        return load_direct()


class SkippedRepoShippingEntryWarning(RuntimeWarning):
    """pm-update가 byte-copy할 수 없는 tracked 엔트리를 명시적으로 제외했다는 신호."""


class EmptyShippingInventoryError(RuntimeError):
    """존재하는 manifest 엔트리의 tracked 출하 인벤토리가 0건인 결함."""

    def __init__(
            self, checkout: Path, subtree: str,
            *, filesystem_fallback: bool = False) -> None:
        self.checkout = Path(checkout)
        self.subtree = subtree
        self.filesystem_fallback = filesystem_fallback
        diagnosis = (
            "filesystem 강등 상태이므로 소스 디렉토리가 비었는지와 checkout 루트가 "
            "올바른지 확인하라"
            if filesystem_fallback
            else "checkout 루트가 올바른지와 git index에 이 경로가 등재됐는지 확인하라"
        )
        super().__init__(
            "pm-update 출하 인벤토리가 0건임 "
            f"(checkout={self.checkout}, subtree={subtree!r}); "
            + diagnosis
        )


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
        raise EmptyShippingInventoryError(
            root, rel, filesystem_fallback=filesystem_fallback)
    return tracked


def _shippable_tracked_entries(
    root: Path,
    entries: list["RepoOwnedEntry"],
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
                SkippedRepoShippingEntryWarning,
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
            SkippedRepoShippingEntryWarning,
            stacklevel=2,
        )
    elif src.is_dir():
        repo_files = _load_repo_owned_files()
        tracked = _shipping_inventory(repo_files, root, rel)
        for relative, source in _shippable_tracked_entries(root, tracked):
            yield relative.as_posix(), source
    elif src.is_file():
        repo_files = _load_repo_owned_files()
        tracked = _shipping_inventory(repo_files, root, rel)
        for accepted_relative, source in _shippable_tracked_entries(root, tracked):
            yield accepted_relative.as_posix(), source
    # missing → 아무것도 yield 안 함 (호출부가 missing 으로 보고)


# ── board-분리 인지 dest 리매핑 ───────────────────────────
# manifest 는 ticket 본문 템플릿을 `wiki/tickets/_template.md` 로 들고 있다(canonical·
# legacy adopter 의 실 위치). 그러나 board(tickets+areas)가 `.project_manager/board/`
# (submodule)로 분리된 adopter(board.py board_root)에선 `_template.md` 가
# `board/tickets/_template.md` 에 산다(board_root() 추종·B 마이그레이션이 거기로 옮김).
# manifest 항목은 legacy-correct 로 두고(자체 drift 회피), *동기 시 dest 경로만*
# board_root 로 해소한다 — board-분리 dest 면 board/tickets/_template.md 로, legacy dest 면
# 종전 wiki/tickets/_template.md 로(무변경). 이로써 board-분리 adopter 의 매 sync 가
# wiki/tickets/_template.md 를 부활시키지 않는다(drift-0·실 발생 버그 reconcile).
#
# board.py board_root() 의 *실측* 판별(`<dest>/.project_manager/board/tickets` 가 dir 인가)을
# 동형 복제한다 — pm_update 는 stdlib-only(board 미import 결합 회피·_resolve_dest_source 와
# 동형)이고, 판별은 단일 is_dir() probe 라 board.py line 71/95 와 정확히 같다. 어떤 manifest
# 항목이 board-분리 시 board/ 로 옮겨가는지는 아래 `_TEMPLATE_REL`→`_BOARD_TEMPLATE_REL`
# 매핑(`_dest_relpath_for`)이 단일 진실.
_TEMPLATE_REL = ".project_manager/wiki/tickets/_template.md"
_BOARD_TEMPLATE_REL = ".project_manager/board/tickets/_template.md"


def _is_board_separated(dest_root: Path) -> bool:
    """dest 가 board-분리 형상인가 — `<dest>/.project_manager/board/tickets` 가 실 dir.

    board.py board_root() 의 판별과 동형(line 71/95) — pm_update 가 board 를 import 하지 않고
    같은 *실측* probe 로 dest 레이아웃을 가른다. board/tickets 가 없으면 legacy(False·무변경).
    """
    return (Path(dest_root) / ".project_manager" / "board" / "tickets").is_dir()


def _dest_relpath_for(rel: str, dest_root: Path) -> str:
    """manifest source relpath → dest 기록 relpath (board-분리 인지 리매핑).

    `wiki/tickets/_template.md` 항목은 board-분리 dest 에서 `board/tickets/_template.md` 로
    리매핑한다(board_root() 추종) — source 는 upstream 의 wiki/ 에서 그대로 읽되 dest 만 옮긴다.
    legacy dest(board/ 미분리)거나 다른 모든 항목은 입력 그대로(무변경·후방호환). 경로 비교는
    OS-무관하게 posix-normalize 한다(_iter_files 가 str(Path) 로 yield 해 Windows 에선 `\\` 가
    섞일 수 있다)."""
    rel_norm = rel.replace("\\", "/")
    if rel_norm == _TEMPLATE_REL and _is_board_separated(dest_root):
        return _BOARD_TEMPLATE_REL
    return rel


# ── @source source-remap (_dest_relpath_for dest-remap 의 대칭 source 쌍) ──
# manifest 항목이 `@source=<relpath>` 를 달면 source_root 아래 그 canonical 경로에서 읽되(source_rel),
# dest 엔 manifest 경로(str(entry))로 기록한다. opencode 어댑터(`.opencode/agents`·`command`)는
# 채택자 dest 엔 `.opencode/*` 로 살지만 프레임워크 루트의 canonical 소스는 `templates/opencode/
# .opencode/*` 에 있다(루트=claude·`.opencode/` 부재). 이 비대칭을 잇는 read-side remap.
def _source_root_rel(entry) -> str:
    """manifest 항목의 source-root 상대 *읽기* 경로 — @source= 있으면 source_rel, 없으면 str(entry).

    기본(마커 부재·source_rel None)은 dest relpath 를 그대로 source-root 상대 읽기 경로로 쓴다
    (오늘 동작·후방호환). `@source=<path>`가 있으면 source_root 아래 그 canonical 경로에서
    읽는다 — dest 기록 경로는 manifest 경로 유지(_remap_to_dest 가 치환). 평문 str 항목(레거시 호출)은
    source_rel 속성 부재 → str(entry)(getattr 폴백).
    """
    return getattr(entry, "source_rel", None) or str(entry)


def _remap_to_dest(rel: str, source_rel: str, manifest_path: str) -> str:
    """source-root relpath → manifest(dest) 기록 relpath (@source source-remap).

    _iter_files 가 source_rel(canonical 소스) 아래에서 yield 한 relpath 의 source_rel prefix 를
    manifest_path(dest)로 치환한다 — `_dest_relpath_for`(dest-remap)의 대칭 source 쌍. source_rel ==
    manifest_path(마커 부재·기본)면 무변경(후방호환). 파일 항목(단일)은 yield 가 source_rel 자체라
    manifest_path 로 통째 치환, 디렉토리 항목은 `source_rel/…` 하위를 `manifest_path/…` 로 옮긴다.
    경로 비교는 OS-무관 posix-normalize(_iter_files 가 Windows 에서 `\\` 섞을 수 있음·_dest_relpath_for
    동형)."""
    if source_rel == manifest_path:
        return rel
    rel_norm = rel.replace("\\", "/")
    src_norm = source_rel.replace("\\", "/")
    if rel_norm == src_norm:
        return manifest_path
    prefix = src_norm + "/"
    if rel_norm.startswith(prefix):
        return manifest_path + "/" + rel_norm[len(prefix):]
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
    dest_root: Path | None = None,
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
    effective_dest = Path(dest_root) if dest_root is not None else REPO
    entry = manifest[entry_index]
    rel = str(entry)
    source_rel = _source_root_rel(entry)
    target_owned = _entry_target_owned_flag(entry)
    if not (source_root / source_rel).exists():
        return [], True, target_owned

    files: list[tuple[str, Path]] = []
    for shipped_rel, source in _iter_files(source_root, source_rel):
        shipped_rel = _remap_to_dest(shipped_rel, source_rel, rel)
        shipped_rel = _dest_relpath_for(shipped_rel, effective_dest)
        if _manifest_owner_index(manifest, shipped_rel, effective_dest) != entry_index:
            continue
        files.append((shipped_rel, source))
    return files, False, target_owned


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
    """pm_import 모듈을 같은 tools/ 에서 직접 로드 (_load_pm_render 패턴 동형).

    upstream_rev baseline 기록(매 sync)에 pm_import 의 URL 안전 git 호출
    (read_upstream_rev — argv-list·timeout·GIT_TERMINAL_PROMPT=0)과 local.conf set-or-replace
    (`_set_conf_keys` — record_upstream_rev 와 동일 백엔드)를 *재사용*한다 — pm_update 가 자체
    git/conf-write 를 중복 구현하지 않게(엔진 stdlib-only 철학 안에서 검증된 안전 계약을 상속).
    로드 실패는 호출부가 fail-soft
    (baseline 기록은 best-effort·sync 자체를 깨지 않는다).
    """
    import_py = Path(__file__).resolve().parent / "pm_import.py"
    spec = importlib.util.spec_from_file_location("pm_import", import_py)
    if spec is None or spec.loader is None:
        raise ImportError(f"pm_import 로드 불가: {import_py}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── upstream baseline↔HEAD 변경점 요약 (read-only) ─────────
# git `name-status` 코드(첫 글자) → 표시용. R(rename)·C(copy)는 첫 글자만 본다(접두).
_NAME_STATUS_LABELS = {"M": "M", "A": "A", "D": "D", "R": "R", "C": "C", "T": "T"}


def _path_under_manifest(rel_path: str, manifest: list) -> bool:
    """changed relpath 가 manifest 항목(파일=동일·디렉토리=prefix)에 속하는지 — 엔진 영향 판정.

    manifest 한 줄은 파일 또는 디렉토리(repo 루트 기준·재귀). changed 파일이 manifest 의
    파일 항목과 정확히 같거나, manifest 디렉토리 항목 *아래*(posix prefix + `/`)면 이번 동기가
    덮어쓰는 엔진 경로다. _iter_files 의 디렉토리 재귀 의미와 동형(파일은 `==`·디렉토리는
    `startswith(d + "/")`). manifest 의 @render/@target-owned 마커는 ManifestEntry 가 이미
    떼어내 path-only 값이라 `str(entry)` 로 순수 경로만 비교한다.
    """
    rel_norm = rel_path.replace("\\", "/").strip("/")
    for entry in manifest:
        item = str(entry).replace("\\", "/").strip("/")
        if not item:
            continue
        if rel_norm == item or rel_norm.startswith(item + "/"):
            return True
    return False


def summarize_upstream_changes(
    checkout: Path,
    baseline: str,
    manifest: list,
    *,
    git_runner=None,
) -> dict:
    """upstream 로컬 checkout 의 baseline..HEAD 변경점을 read-only 로 요약한다 ().

    채택자가 받은 baseline(`upstream_rev`) ↔ 그 이후 upstream HEAD 에 쌓인 변경을 *이미 로컬에
    있는* checkout 에서 `git log`/`diff --name-status` 로 집계한다 — **fetch/clone 안 함**
    (네트워크 0). git 안전 계약(argv-list·timeout·GIT_TERMINAL_PROMPT=0·config
    격리)은 pm_import._real_upstream_git_runner 를 재사용한다(git_runner 미주입 시). 테스트는
    git_runner 를 주입해 라이브 git 0 으로 결정론을 얻는다(DI seam).

    `manifest` 는 "무엇이 엔진인가"의 판별 집합 — 호출부가 **sync 와 동일한**
    resolve_manifest_for_dest(effective_dest, source)로 해소해 넘긴다(dest 우선·없으면 source).
    이 함수가 자체 로드하지 않는 이유: 엔진 영향(이번 동기가 받는 것) 분류는 실 sync 가 쓰는
    manifest 와 *반드시* 일치해야 하기 때문(codex MF — source 단독은 dest 커스터마이즈/--target
    에서 어긋난다). 빈 manifest → 전부 'other'(graceful·엔진 영향 0 보수 표시).

    반환 dict:
      - `status`: 'ok' | 'baseline_unreachable' | 'up_to_date' | 'summary_failed'
      - `head`: HEAD commit(rev-parse HEAD) 또는 '' (실패 시)
      - `count`: baseline..HEAD commit 수 (int)
      - `engine`: [(code, path)] — manifest 항목에 속하는 변경(이번 동기가 받는 것)
      - `other`: [(code, path)] — manifest 밖 변경(동기 안 받음)
      - `log`: [(sha, subject)] — `git log --oneline baseline..HEAD` (--log 옵션용)

    호출부(main --changes 분기)가 baseline 부재·URL upstream·HEAD==baseline 등 *상위* 게이트를
    이미 처리한 뒤 진입한다. 여기선 baseline rev 가 checkout 에서 도달 가능한지(rc)만 본다 —
    도달불가(force-push·shallow)면 status='baseline_unreachable'(호출부가 재clone 권고). log/diff
    가 rc≠0(도달가능한데도 git 호출 실패·예외)면 status='summary_failed' — 빈 결과를 "변경 0"으로
    오판하지 않게 surface 한다(codex suggestion 1·advisory 오판 금지).
    """
    runner = git_runner if git_runner is not None else _load_pm_import()._real_upstream_git_runner()
    result: dict = {
        "status": "ok",
        "head": "",
        "count": 0,
        "engine": [],
        "other": [],
        "log": [],
    }

    # HEAD 해소 (rev-parse) — checkout 의 현재 HEAD commit.
    rc, out = runner(["-C", str(checkout), "rev-parse", "HEAD"])
    if rc == 0 and out.strip():
        result["head"] = out.strip().splitlines()[0].strip()

    # baseline 도달성 검사 — baseline commit object 가 이 checkout 에 있는지(force-push·shallow
    # 시 없을 수 있다). `cat-file -e <rev>^{commit}` rc 로 판정(네트워크 0·로컬 object DB 만).
    rc, _out = runner(["-C", str(checkout), "cat-file", "-e", baseline + "^{commit}"])
    if rc != 0:
        result["status"] = "baseline_unreachable"
        return result

    # baseline == HEAD 면 변경 0 — log/diff 모두 빈 출력이라 자연히 up_to_date 로 떨어지지만,
    # 호출부가 보통 상위에서 거른다(별도 키 비교). 여기선 log 집계로 count 를 낸다.
    # rc≠0(도달가능한데도 git 실패)은 summary_failed 로 surface(빈 결과 오판 금지·suggestion 1).
    rc, out = runner(["-C", str(checkout), "log", "--oneline", f"{baseline}..HEAD"])
    if rc != 0:
        result["status"] = "summary_failed"
        return result
    log_entries: list[tuple[str, str]] = []
    for line in out.splitlines():
        line = line.rstrip("\n")
        if not line.strip():
            continue
        sha, _, subject = line.partition(" ")
        log_entries.append((sha.strip(), subject.strip()))
    result["log"] = log_entries
    result["count"] = len(log_entries)
    if result["count"] == 0:
        result["status"] = "up_to_date"

    # diff --name-status baseline..HEAD — 변경 파일 목록(M/A/D/R…). 첫 토큰=코드, 둘째=경로
    # (R/C 는 `R100\told\tnew` 3필드라 마지막 필드를 새 경로로 본다). rc≠0 면 summary_failed —
    # commit 수는 났지만 파일 분류가 불가능하므로 "엔진 영향 0" 오판을 피해 surface 한다.
    rc, out = runner(["-C", str(checkout), "diff", "--name-status", f"{baseline}..HEAD"])
    if rc != 0:
        result["status"] = "summary_failed"
        return result
    for line in out.splitlines():
        line = line.rstrip("\n")
        if not line.strip():
            continue
        fields = line.split("\t")
        raw_code = fields[0].strip()
        code = _NAME_STATUS_LABELS.get(raw_code[:1], raw_code[:1] or "?")
        path = fields[-1].strip() if len(fields) > 1 else ""
        if not path:
            continue
        bucket = "engine" if _path_under_manifest(path, manifest) else "other"
        result[bucket].append((code, path))

    return result


def _resolve_dest_source(args) -> tuple:
    """args(--target·--from) → (rc, dest_root, source_root). rc≠0 이면 메시지는 이미 출력됨.

    dest/source 해소는 sync(main)와 read-only --changes가 공유한다 — 둘 다
    같은 우선순위(명시 --from local.conf upstream= 에러)·URL 게이트·stale
    가드를 거쳐야 일관적이다. 추출로 두 진입이 같은 코드 경로를 탄다(중복 0). 성공 시 rc=0 +
    (dest_root[None=self-loc], source_root[디렉토리 검증 통과]). 실패 시 rc≠0(메시지 stderr 출력)
    + (None, None).
    """
    # dest_root: --target 지정 시 REPO/templates/<target>/, 아니면 None(self-location=REPO).
    if args.target:
        try:
            dest_root = resolve_target_root(args.target)
        except (ValueError, FileNotFoundError) as exc:
            print(str(exc), file=sys.stderr)
            return 1, None, None
    else:
        dest_root = None  # 호출부가 REPO fallback 사용

    effective_dest = dest_root if dest_root is not None else REPO

    # ── upstream(source) 해소 — 순서: 명시 --from local.conf upstream= 에러.
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
            return 1, None, None
        # upstream= 이 URL(릴리스 추적 기본값)이면 엔진은
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
                "(git clone/fetch 안 함). `pm-update` 스킬(URL→cache clone 후 sync)을 "
                "쓰거나, `--from <로컬 checkout>` 으로 로컬 경로를 명시하라.",
                file=sys.stderr,
            )
            return 1, None, None
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
        return 1, None, None

    return 0, dest_root, source_root


def _run_changes(args) -> int:
    """`--changes` read-only 분기 — baseline..HEAD 변경점 요약 출력(실 sync 안 함).

    dest/source 해소는 sync 와 공유(_resolve_dest_source) — URL upstream 은 거기서 명확 에러로
    멈춘다(엔진은 git clone/fetch 안 함). baseline(`upstream_rev`)은 *dest* local.conf
    에서 읽는다(매 sync 시 pm_update 가 기록한 마지막 동기 기준점). 전부 fail-soft·exit 0(graceful
    안내) — baseline 미기록·HEAD==baseline·baseline 도달불가 각각 메시지로 surface 한다.
    """
    rc, dest_root, source_root = _resolve_dest_source(args)
    if rc != 0:
        return rc  # URL upstream·미등록·stale 는 sync 와 동일한 명확 에러(rc≠0).

    effective_dest = dest_root if dest_root is not None else REPO
    baseline = _read_local_conf(
        effective_dest / ".project_manager" / "local.conf").get("upstream_rev", "").strip()

    # baseline 미기록(아직 sync 한 적 없음·구 import) — graceful 안내(exit 0). 다음 sync 후 추적된다.
    if not baseline:
        print(
            "upstream 변경: baseline 미기록 — 아직 동기 baseline(upstream_rev)이 local.conf 에 "
            "없다. 다음 `pm-update`(실 sync) 후 baseline 이 기록되면 변경점이 추적된다."
        )
        return 0

    # 엔진 영향 판별 manifest 는 **sync 와 동일한** resolve_manifest_for_dest 로 해소한다
    # (dest 우선·없으면 source·codex MF). 실 sync 가 dest manifest 로 "무엇이 엔진인가"를 정하므로
    # --changes 의 "엔진 영향(이번 동기가 받는 것)"도 같은 manifest 를 써야 일치한다 — source 단독은
    # dest 커스터마이즈/--target 에서 어긋난다. 둘 다 부재(fresh-adopter)면 빈 manifest → 전부
    # 'other'(graceful·엔진 영향 0 보수 표시·summarize_upstream_changes 가 빈 리스트 허용).
    try:
        manifest_path = resolve_manifest_for_dest(effective_dest, source_root)
        manifest = read_manifest(manifest_path)
    except FileNotFoundError:
        manifest = []

    summary = summarize_upstream_changes(source_root, baseline, manifest)

    # baseline rev 가 checkout 에서 도달 불가(force-push·shallow clone) — 재clone 권고(exit 0).
    if summary["status"] == "baseline_unreachable":
        print(
            f"upstream 변경: baseline {baseline[:12]} 가 upstream checkout 에서 도달 불가 "
            "(force-push 됐거나 shallow clone). upstream 을 재clone 하거나 `--from <온전한 "
            "checkout>` 으로 다시 확인하라."
        )
        return 0

    # 변경점 집계 실패(log/diff git 호출 rc≠0) — 빈 결과를 "변경 0"으로 오판하지 않게 surface
    # (codex suggestion 1·advisory 오판 금지). exit 0 유지(read-only 안내)하되 명확히 알린다.
    if summary["status"] == "summary_failed":
        print(
            f"upstream 변경: baseline {baseline[:12]} 이후 변경점 집계 실패(요약 불가) — "
            "upstream checkout 의 git log/diff 호출이 실패했다. checkout 이 온전한 git work "
            "tree 인지 확인하거나 `--from <온전한 checkout>` 으로 다시 시도하라.",
            file=sys.stderr,
        )
        return 0

    head = summary["head"]
    count = summary["count"]

    # --count-only: commit 개수 1줄만(advisory/스크립트).
    if args.count_only:
        print(str(count))
        return 0

    # HEAD == baseline(변경 0·최신) — count 0.
    if count == 0:
        print(f"upstream 변경: baseline {baseline[:12]} → HEAD {head[:12]} (변경 0·최신)")
        return 0

    # ── 3블록 요약 (채택자-facing·기본 간결) ──────────────────────────────────
    print(f"upstream 변경: baseline {baseline[:12]} → HEAD {head[:12]} ({count} commits)")

    engine = summary["engine"]
    other = summary["other"]
    print(f"엔진 영향 (manifest 경로·이번 동기가 받는 것): {len(engine)} files")
    for code, path in engine:
        print(f"  {code} {path}")
    print(f"그 외 변경 (manifest 밖·동기 안 받음): {len(other)} files")

    # --log: git log --oneline baseline..HEAD 꼬리.
    if args.log:
        print("커밋 (baseline..HEAD):")
        for sha, subject in summary["log"]:
            print(f"  {sha} {subject}")

    return 0


# 경로 upstream 에서 baseline 과 *함께* 기록하는 현재-관찰 키 (board._DRIFT_SEEN_KEY 동명).
_SEEN_REV_KEY = "upstream_seen_rev"


def _upstream_shape(pm_import, dest_root: Path) -> str:
    """dest local.conf 의 `upstream=` 값 모양 — 'url' | 'path' (네트워크 0).

    seen-rev 동시 기록의 분기 입력이다. 미등록(`--from` 직접 지정·구 import)·분류 실패는
    `_resolve_dest_source` 와 동일하게 **보수적으로 'path'** 취급한다(기존 동작·fail-soft).
    """
    stored = _read_local_conf(
        dest_root / ".project_manager" / "local.conf").get("upstream", "").strip()
    if not stored:
        return "path"
    try:
        return pm_import.classify_upstream(stored)
    except Exception:  # noqa: BLE001 — 분류 실패는 보수적으로 경로 취급(fail-soft).
        return "path"


def record_upstream_revs(dest_root: Path, source_root: Path) -> tuple[bool, dict[str, str]]:
    """매 sync 후 upstream rev 키들을 dest local.conf 에 **단일 write** 로 기록.

    반환 `(변경 여부, 이번에 엔진이 기록한 {키: rev})` — 호출부가 *실제로 무엇을 썼는지* 를
    보고 안내 문구를 정한다(결과 상태로 역추론 금지: URL 형상은 스킬층이 쓴 seen 이 이미
    baseline 과 같아서 "엔진이 썼다"와 구분되지 않는다). 기록 생략 시 `(False, {})`.

    기록 키:
      - `upstream_rev`      (baseline·항상) — drift-lint의 "마지막 동기 이후" 기준점
        pm_import(import 시)와 여기(매 sync) 둘 다 갱신해야 그 의미가 성립한다.
      - `upstream_seen_rev` (현재 관찰값·**경로 upstream 한정**) — 경로 형상은 fetch 채널이
        따로 없어 *동기 시점의 로컬 checkout rev 가 곧 관찰값*이다('로컬 경로'
        분기와 동일 규정). baseline 만 갱신하면 두 키가 영구히 어긋나 정상 흡수 직후에도 drift
        거짓 경보가 상시 뜬다(실측). URL 형상은 **건드리지 않는다** — 스킬층이 fetch 후
        관찰값을 기록한다(한 키 2역 금지·race/자기비교 회피).

    두 키를 한 번의 공용 atomic writer로 묶는다 — 중간 중단에도 baseline 만 앞선 반쪽
    상태가 생기지 않는다(어긋난 두 키 = 거짓 drift 의 원인이었다). rev 읽기는 pm_import 의
    read_upstream_rev(URL 안전 git 호출), 파일 갱신은 pm_import 의 `_write_conf_keys`(키 중복
    정규화·atomic replace·실효값 검증·record_upstream_rev·pm_config upstream set 과 동일 백엔드)를
    재사용한다. git repo 아님·HEAD 해소 실패·pm_import 로드 실패·local.conf 부재는 **graceful
    생략**(best-effort — sync 자체는 안 깬다).
    """
    try:
        pm_import = _load_pm_import()
    except Exception:  # noqa: BLE001 — 로드 실패는 baseline best-effort: sync 를 안 깬다.
        return False, {}
    rev = pm_import.read_upstream_rev(source_root)
    if not rev:
        return False, {}  # git repo 아님·HEAD 해소 실패 — graceful 생략(URL upstream 포함).

    updates = {"upstream_rev": rev}
    if _upstream_shape(pm_import, dest_root) == "path":
        updates[_SEEN_REV_KEY] = rev

    local_conf = dest_root / ".project_manager" / "local.conf"
    if not local_conf.is_file():
        print(f"경고: local.conf 없음 ({local_conf}) — upstream_rev 기록 건너뜀.", file=sys.stderr)
        return False, {}
    changed = pm_import._write_conf_keys(local_conf, updates)
    return changed, updates


def record_upstream_rev_baseline(dest_root: Path, source_root: Path) -> bool:
    """`record_upstream_revs` 의 변경-여부 전용 wrapper (시그니처 보존·기존 호출부/테스트)."""
    return record_upstream_revs(dest_root, source_root)[0]


def converge_upstream_revs(
    dest_root: Path, source_root: Path, skew_status: str, skew_new: list[str]
) -> None:
    """skew 안전장치를 보존하며 sync 뒤 revision 키를 수렴·안내한다."""
    if skew_status == "skew":
        print(
            f"→ manifest skew({len(skew_new)}건)로 upstream_rev baseline(+경로 upstream 의 "
            "upstream_seen_rev 관찰값) 갱신을 **억제**한다 — drift-lint 가 계속 이 skew 를 울리게 "
            "둔다. 로컬 engine.manifest 를 reconcile 한 뒤 다시 pm-update 하라(신규 등재분 "
            ")."
        )
        return

    # 안내 문구는 **엔진이 실제로 기록한 키**(recorded)로 정한다 — 파일의 결과 상태로
    # 역추론하면 URL 형상(스킬층이 쓴 seen 이 이미 baseline 과 같음)에서 "동시 기록" 이
    # 거짓으로 뜬다.
    changed, recorded = record_upstream_revs(dest_root, source_root)
    if changed:
        seen_note = " (+upstream_seen_rev 동시 기록)" if _SEEN_REV_KEY in recorded else ""
        print("✓ local.conf upstream_rev baseline 갱신 (drift-lint 기준점): "
              f"{recorded['upstream_rev']}{seen_note}")


def detect_manifest_skew(
    local_manifest: list,
    source_root: Path,
    *,
    upstream_manifest: Path | None = None,
    upstream_manifests: list[Path] | None = None,
) -> tuple[str, list[str]]:
    """upstream engine.manifest ↔ 로컬(sync 에 쓰인) manifest 대조 — 신규 등재분 탐지.

    로컬 manifest 가 구형이면 `pm_update` 는 로컬 등재분만 복사해 upstream 이 새로 등재한 엔진
    경로(신규 등재분)가 도달하지 않는데, upstream_rev baseline 은 무조건 최신으로 갱신돼
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
            upstream_entries = merge_manifest_sources(
                [Path(path) for path in upstream_manifests]
            )["entries"]
        else:
            if upstream_manifest is None:
                upstream_manifest = Path(source_root) / ".project_manager" / "engine.manifest"
            upstream_entries = read_manifest(upstream_manifest)
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


# manifest self-prop 엔트리(채택자 engine.manifest 가 자기 자신을 전파 대상으로 등재한 행)의
# path — flavor-correct upstream 해소(resolve_manifest_selfheal)와 root 폴백의 단일 기준.
_MANIFEST_SELF_REL = ".project_manager/engine.manifest"


def _manifest_marker_key(entry) -> tuple:
    """ManifestEntry 의 마커 3종(@render/@target-owned/@source)을 비교키 튜플로 — 경로 집합만으론
    못 잡는 flavor 차이(예: `@source=templates/claude_code/...` vs bare)를 selfheal 이 감지하게 한다.

    평문 str 항목(레거시)은 getattr 폴백으로 (False, False, None)(마커 없음·후방호환).
    """
    return (
        bool(getattr(entry, "render", False)),
        bool(getattr(entry, "target_owned", False)),
        getattr(entry, "source_rel", None),
    )


def _selfprop_upstream_rel(local_entries: list) -> str:
    """채택자 로컬 manifest 의 self-prop 엔트리(`.project_manager/engine.manifest`)를 따라 flavor-correct
    upstream manifest 의 source-root 상대 *읽기* 경로를 낸다(codex MF).

    claude_code/opencode 채택자의 self-prop 는 `@source=templates/<harness>/.project_manager/
    engine.manifest` 라, 그 @source(=_source_root_rel)가 같은 flavor upstream manifest 를 가리킨다.
    self-prop 엔트리가 없거나 bare(@source 부재)면 root manifest(`_MANIFEST_SELF_REL`·현행 폴백).
    이로써 flavor↔flavor 비교가 성립해 root(bare) 승격이 flavor manifest 를 클로버하지 않는다.
    """
    for entry in local_entries:
        if str(entry) == _MANIFEST_SELF_REL:
            return _source_root_rel(entry)  # @source 있으면 그 경로·없으면 str(entry)=_MANIFEST_SELF_REL
    return _MANIFEST_SELF_REL


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
            "⚠️ frozen 다중-harness 의심 — @source 선택 선언이 있는 manifest와 "
            f"선언되지 않은 타 flavor({flavor}) 관리 경로 일부가 함께 관측됐다 "
        )
    else:
        lead = (
            "⚠️ frozen 다중-harness 의심 — @source 선언이 없는 legacy manifest와 "
            f"타 flavor({flavor}) 관리 경로 일부가 함께 관측됐다 "
        )
    print(
        lead
        + f"({len(observed)}/{len(evidence_paths)}: {', '.join(observed)}). "
        "이 상태는 legacy 다중-harness 설치의 일부 누락 또는 사용자 stray 파일일 수 있어 "
        "해당 flavor는 자동 자기치유하지 않는다.\n"
        f"    `add-harness {cli_flavor}`는 guest @render만 등록하므로 완전 마이그레이션이 아니다.\n"
        "    완전 마이그레이션(등록 flavor 전체; 관측 누락에도 안전하나 원치 않는 flavor도 "
        "추가될 수 있으므로 dry-run 검토):\n"
        "      <manager>/pm-import.sh --into <project> --harness all --dry-run\n"
        "      <manager>/pm-import.sh --into <project> --harness all\n"
        "      cd <project> && ./pm-update.sh\n"
        "    재-import가 커스터마이즈된 CLAUDE.md/AGENTS.md를 템플릿 판으로 덮을 수 있으니, "
        "진입 문서 커스텀은 .pm_import_backups/<날짜>/ 백업에서 재병합하라.\n"
        "    해당 파일이 stray라면 이 경고를 무시해도 된다.",
        file=sys.stderr,
    )


def _frozen_flavor_evidence(
    entries: list,
    other_candidate_paths: set[str],
    local_core_paths: set[str],
    guest_paths: set[str],
) -> list[str] | None:
    """다른 모든 후보에는 없는 배타적 flavor 경로의 frozen evidence를 계산한다.

    guest 절이 flavor 고유 경로 하나라도 소유하면 그 flavor 전체가 add-harness refresh 채널이다.
    선언/legacy 선택 분기가 같은 ``None`` skip 신호를 소비해 guest 경로 밖의 같은-flavor 파일이
    남아 있어도 frozen 경고나 자동 승격 근거로 다시 쓰지 않게 한다.
    """
    unique_paths = [
        str(entry).replace("\\", "/")
        for entry in entries
        if str(entry).replace("\\", "/") not in other_candidate_paths
    ]
    if any(_path_owned_by(rel, guest_paths) for rel in unique_paths):
        return None
    return [rel for rel in unique_paths if rel not in local_core_paths]


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
        "⚠️ frozen 다중-harness 의심 — @source 선언이 없는 legacy engine.manifest의 "
        "core 경로 집합이 현행 flavor 후보 중 정확히 하나와 완전 일치하지 않는다. "
        f"관측 형상: 로컬 core {len(local_core_paths)}행; {shapes}.\n"
        "    로컬 manifest는 그대로 사용한다(자동 flavor 승격·행 제거·치유 0).\n"
        "    `add-harness`는 guest @render만 등록하므로 완전 마이그레이션이 아니다.\n"
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
) -> tuple[list[Path], bool]:
    """채택자에 실제 설치된 flavor들의 upstream manifest를 선택 순서대로 해소한다.

    설치 manifest의 ``@source=templates/<flavor>/...`` 선언이 있으면 그 flavor 집합만 신뢰한다.
    파일 존재는 사용자 파일/PM 홈의 어댑터 사본과 구별할 수 없으므로 선언이 하나라도 있으면
    존재-휴리스틱을 전혀 타지 않는다. 선언 순서는 manifest 행의 최초 출현 순서이고, 첫 flavor
    우선 + 후속 선언 순서라는 합집합 우선순위를 그대로 보존한다.

    ``@source`` flavor 선언이 전혀 없는 구 manifest는 로컬 core 경로 집합이 **정확히 한 후보와
    완전 일치**할 때만 그 후보를 primary로 고른다. 부분집합, 존재 경로, 은퇴 행 추정, 최소
    초과집합/tiebreak는 사용하지 않는다. 완전 일치가 아니면 로컬 manifest를 그대로 계획에
    사용하고, frozen/stray를 구분할 수 없다는 진단과 검증된 완전 재-import 절차만 낸다.

    선언이 하나라도 있으면 존재-휴리스틱에 의한 자동 선택은 비발화한다. 선언되지 않은 flavor의
    관리-고유 경로 일부가 보이면 같은 frozen/stray 마이그레이션 경고만 내고 자동 승격하지 않는다.
    선언된 후순위 manifest가 부재해도 선택 목록에서 버리지 않아 호출부가 로컬 union을 유지하며
    경고하고, 해소 불가 선언은 primary만 유지한 채 경고한다.

    add-harness guest 절은 별도 refresh 채널이므로 core 합집합으로 승격하지 않는다. guest가 소유한
    경로만 존재해 후보가 된 flavor는 제외해 기존 add-harness 불가침 계약을 유지한다.
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
    guest_block = _extract_guest_manifest_block(local_text)
    guest_paths = {
        ln.split()[0].replace("\\", "/")
        for ln in guest_block.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    } if guest_block else set()
    local_core_paths = {
        str(entry).replace("\\", "/")
        for entry in local_entries
        if not _path_owned_by(str(entry).replace("\\", "/"), guest_paths)
    }

    primary_flavor = next(
        (flavor for flavor, candidate in candidate_by_flavor.items() if candidate == primary),
        None,
    )
    source_declarations = [
        getattr(entry, "source_rel", None)
        for entry in local_entries
        if not _path_owned_by(str(entry).replace("\\", "/"), guest_paths)
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
            candidate_entries[candidate] = read_manifest(candidate)
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

    exact_matches = [
        candidate for candidate, paths in candidate_paths.items()
        if paths == local_core_paths
    ]
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


def resolve_manifest_selfheal(effective_dest: Path, source_root: Path) -> dict:
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
      - manifest: plan 이 쓸 ManifestEntry 리스트 — 'heal' 이면 flavor upstream_entries, 그 외 None
                  (None 이면 호출부가 resolve_manifest_for_dest 산출 로컬 manifest 를 그대로 쓴다).
      - upstream_manifest: 대조에 쓴 flavor-correct upstream engine.manifest **Path** — 호출부(main)가
                  이 경로를 detect_manifest_skew 에 그대로 넘겨 두 기전의 대조 기준을 flavor 로 정합시킨다
                  (). 로컬 manifest 부재('no_local')는 self-prop 이 없어 root 폴백 경로.
    """
    dest_manifest = Path(effective_dest) / ".project_manager" / "engine.manifest"
    root_manifest = Path(source_root) / ".project_manager" / "engine.manifest"
    if not dest_manifest.exists():
        # 로컬 manifest 부재(fresh/구 import) — resolve_manifest_for_dest 가 이미 source manifest 를
        #   집으므로 plan 이 upstream 기준(신규 등재 포함)으로 돈다. 승격 불요(무변경·현행). self-prop
        #   이 없어 skew 대조는 root(=resolve 산출과 동일) 로 정합.
        return {"status": "no_local", "added": [], "removed": [],
                "manifest": None, "upstream_manifest": root_manifest,
                "upstream_manifests": [root_manifest], "manifest_text": None,
                "merge_conflicts": []}
    local_entries = read_manifest(dest_manifest)
    local_text = dest_manifest.read_text(encoding="utf-8")
    # 설치된 flavor들의 upstream manifest 합집합. 첫 항목은 self-prop가 지정한 기존 flavor이고,
    # 추가 항목은 설치 manifest의 flavor provenance로 일반화해 발견한다(고정 조합 손-끼워넣기 없음).
    upstream_manifest = Path(source_root) / _selfprop_upstream_rel(local_entries)
    upstream_manifests: list[Path] = [upstream_manifest]
    try:
        upstream_manifests, legacy_preserved = _selected_upstream_manifests(
            effective_dest, source_root, local_entries, local_text)
        upstream_manifest = upstream_manifests[0]
        if legacy_preserved:
            return {
                "status": "legacy_preserved",
                "added": [],
                "removed": [],
                "manifest": None,
                "upstream_manifest": upstream_manifest,
                "upstream_manifests": upstream_manifests,
                "manifest_text": None,
                "merge_conflicts": [],
            }
        merged_upstream = merge_manifest_sources(upstream_manifests)
        upstream_text = merged_upstream["text"]
        upstream_entries = merged_upstream["entries"]
    except (FileNotFoundError, OSError, UnicodeError, ValueError):
        # flavor upstream 읽기 실패 — skew 대조도 같은 경로를 넘겨 upstream_missing 으로 정합(fail-soft).
        return {"status": "upstream_missing", "added": [], "removed": [],
                "manifest": None, "upstream_manifest": upstream_manifest,
                "upstream_manifests": upstream_manifests, "manifest_text": None,
                "merge_conflicts": []}
    # add-harness guest 절(로컬-전용 `@target-owned` guest)은 **core 비교에서 제외**한다:
    #   섞으면 항상 removed 비어있지 않아 영구 diverged → upstream 신규 항목 자기치유(승격) 불능. 절은
    #   apply 가 재부착하므로(대칭·`_copy_manifest_preserving_guest`) 승격돼도 잔존한다. 판정 사본 없이
    #   추출 헬퍼를 재사용해 in_sync 판정도 core 로(strip==upstream), 경로 집합도 core 로 좁힌다.
    guest_block = _extract_guest_manifest_block(local_text)
    guest_paths = {
        ln.split()[0] for ln in guest_block.splitlines()
        if ln.strip() and not ln.strip().startswith("#")} if guest_block else set()
    if _strip_guest_manifest_block(local_text) == upstream_text:
        return {"status": "in_sync", "added": [], "removed": [],
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
    local_markers = {str(e): _manifest_marker_key(e) for e in core_local_entries}
    upstream_markers = {str(e): _manifest_marker_key(e) for e in upstream_entries}
    added = sorted(set(upstream_markers) - set(local_markers))
    removed = sorted(set(local_markers) - set(upstream_markers))
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
    if removed or marker_divergent:
        # 로컬-전용 경로 또는 공통 경로 마커 divergence = 로컬이 flavor upstream 의 단순 부분집합이
        #   아니다(채택자 커스텀 편집·마커 손질). 전체 교체하면 그 커스텀/구조를 클로버하므로 승격하지
        #   않고 현행 로컬 manifest 를 유지한다. upstream 신규 등재분은 skew 대조가
        #   surface 한다(안전망). "항목 제외" 커스텀(로컬⊂upstream·마커 정합)은 아래 heal 로 전체 교체.
        return {"status": "diverged", "added": added, "removed": removed,
                "manifest": None, "upstream_manifest": upstream_manifest,
                "upstream_manifests": upstream_manifests,
                "manifest_text": upstream_text,
                "merge_conflicts": merged_upstream["conflicts"]}
    if not added and not (
        legacy_without_source_provenance and provenance_divergent
    ):
        # 경로/마커 동일(주석만 차이) — 도달할 신규 등재 경로 0. manifest 자신도 self-prop 엔트리라
        #   plan 이 파일은 갱신한다(승격 불요). in_sync 로 취급(baseline 갱신 진행).
        return {"status": "in_sync", "added": [], "removed": [],
                "manifest": None, "upstream_manifest": upstream_manifest,
                "upstream_manifests": upstream_manifests,
                "manifest_text": upstream_text,
                "merge_conflicts": merged_upstream["conflicts"]}
    # 로컬 ⊂ upstream(removed 0·마커 정합·added>0), 또는 경로는 같지만 @source가 전무한 legacy
    # manifest — flavor upstream 을 계획 기준으로 승격해 신규 경로와 source provenance를 같은
    # sync에서 도달시킨다. provenance-only 승격은 bare opencode 경로를 source root에서 찾는 rc=2도
    # 막는다.
    return {"status": "heal", "added": added, "removed": [],
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
    )
    for path in added:
        print(f"    + {path}  (upstream 신규/재-등재 — 이번 sync 로 도달)")


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


def _selected_upstream_core_paths(selfheal: dict) -> set[str]:
    """이번 실행에서 실제 승격된 selfheal manifest의 core 경로 집합.

    ``selfheal["manifest"]``는 heal 판정에서만 채워진 선택 flavor 합집합이다. 후보 upstream
    manifest를 직접 다시 읽으면 diverged/in_sync처럼 승격하지 않은 실행에서도 guest 보호를
    해제한다. 실제 plan 기준으로 승격된 엔트리만 반환해 후순위 flavor의 1-run 승격은 유지하면서
    미승격 상태의 guest는 계속 보호한다.
    """
    entries = selfheal.get("manifest") or []
    return {str(entry).replace("\\", "/") for entry in entries}


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
    # opencode 어댑터 전용 — pm_import 가 import 시 local.conf 에 기록(모델 해소 시만).
    # self-update 의 @source 재렌더가 `.opencode/agents` 를 렌더할 때 이 매핑으로
    # local.conf 재유도. **미해소**(opencode 없이 import 한 채택자·local.conf 에 opencode_pro_model
    # 부재)면 render_adapter 가 leak 으로 rc-fail 하지 않고 intentional-TODO 로 graceful 중화한다
    # (pm_render.neutralize_model_todo·import 대칭) — 한 토큰 미해소가 엔진/타 어댑터 update
    # 전체를 막지 않는다(부분-graceful). claude tree 엔 토큰 부재 → no-op.
    "opencode_pro_model": "OPENCODE_PRO_MODEL",
}


def _operational_from_local_conf(dest_root: Path) -> tuple[dict[str, str], list[str]]:
    """local.conf 의 operational 해소값을 pm_render 의 token-key dict 로 변환.

    local.conf 키(lowercase) → operational token key(uppercase). board.py init 이 안 쓴 키는
    포함하지 않는다(빈값 강제 안 함). 출하 어댑터의 operational 토큰은 import sed 로 이미
    리터럴이라 render 시점엔 보통 부재 — 이 매핑은 재렌더가 그 토큰을 만났을 때 local.conf
    단일 진실로 재유도하기 위한 것().

    **값이 빈 문자열인 키도 dict 에서 제외**한다(부재와 동일 취급) — 빈값을 그대로
    넘기면 렌더가 토큰을 빈 문자열로 silent 치환해(예: `project_name=` 빈값 → description 이
    " 프로젝트") 탐지 신호가 사라진다. 제외하면 토큰이 잔존해
    render 의 _assert_no_leak 가 leak 으로 잡는다(silent-empty = leak 클래스). 제외된 빈값
    token-key 목록을 함께 반환해 render_adapter 가 leak 힌트("값을 채우라")에 싣게 한다.

    반환: (operational dict, 빈값이라 제외된 token-key 목록).
    """
    conf = _read_local_conf(dest_root / ".project_manager" / "local.conf")
    operational: dict[str, str] = {}
    empty_keys: list[str] = []
    for conf_key, token_key in _LOCAL_CONF_TO_OPERATIONAL.items():
        if conf_key not in conf:
            continue
        if conf[conf_key] == "":
            empty_keys.append(token_key)
            continue
        operational[token_key] = conf[conf_key]
    return operational, empty_keys


def _render_text(source_path: Path, dest_root: Path) -> str:
    """source 템플릿을 채택자 local.conf(operational)로 렌더한 텍스트.

    local.conf 의 operational 값을 plain replace 로 채운다(free-form 은 pm_import FILL 채널이
    canonical home 에서 전담). 결과는 자족(잔여 `{{...}}` 0·assertion).
    호출부(apply/plan)가 dst 와 비교/기록한다.
    """
    render_mod = _load_pm_render()
    operational, empty_keys = _operational_from_local_conf(dest_root)
    text = Path(source_path).read_text(encoding="utf-8")
    return render_mod.render_adapter(text, operational=operational, empty_keys=empty_keys)


def _is_text_source(source_path: Path) -> bool:
    """source 가 UTF-8 텍스트로 읽히는가 — render 대상 판정의 유일한 형식 조건.

    옛 `.md` 확장자 열거를 대체한다: 확장자는 열린 집합(하니스가 새 형식을 들여온다)이라 열거하면
    새 형식이 조용히 미커버로 남는다(codex `.toml`). render 가 실제로 요구하는 건 "텍스트로 읽어
    plain replace 할 수 있는가" 뿐이므로 그것만 본다 — 바이너리 리소스는 False → byte-copy.
    IO 실패도 보수적으로 False(byte-copy·기존 동작).
    """
    try:
        Path(source_path).read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return False
    return True


def _render_eq_dst(sp: Path, dst: Path, dest_root: Path) -> bool:
    """render path 의 '변경 없음' 정직 판정 — 렌더 산출물 == dst 현재 내용 ().

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
    manifest_source_text: str | None = None,
) -> tuple[list[tuple], list[str]]:
    """(changes, missing) 반환. changes = [(rel, src, dst, kind)] (kind: new|update).

    dest_root: 동기화 대상 루트. None 이면 REPO(self-location) 사용.

    manifest 항목이 `ManifestEntry`(render 플래그 운반·read_manifest 산출)면 그 path 의 render
    여부를 dst(`_RenderDst` 래퍼)에 실어 apply 가 byte-copy vs render 를 분기하게 한다. 평문
    str 항목(레거시 호출)은 render=False(후방호환·순수 copy2). render path 의 변경검출은
    filecmp 대신 rendered-output 비교(`_render_eq_dst`) — 템플릿≠산출물 오보 회피().

    render_enabled=False 면 manifest @render 태그를 *무시*하고 전부 copy2(토큰-form 보존).
    `--target`(루트→templates/<name> 동기) 경로 전용 — 템플릿은 토큰-form 소스라 절대 렌더
    대상이 아니다(local.conf 부재 → operational 토큰 leak·_assert_no_leak crash). render 는
    채택자 self-update(--target 없음·local.conf 보유)와 pm_import 경로에서만 일어난다.
    """
    effective_dest = dest_root if dest_root is not None else REPO
    changes: list[tuple] = []
    missing: list[str] = []
    for entry_index, entry in enumerate(manifest):
        rel = str(entry)
        if rel.replace("\\", "/") == _MANIFEST_SELF_REL and manifest_source_text is not None:
            # 선택 flavor 합집합은 upstream에 단일 실파일로 존재하지 않는다. 인메모리 source를
            # change tuple에 실어 self-prop가 합집합 전체를 설치/갱신하게 한다.
            dst = _RenderDst(effective_dest / rel, False)
            source = _ManifestTextSource(manifest_source_text)
            if not dst.exists():
                changes.append((rel, source, dst, "new"))
            else:
                try:
                    dst_core = _strip_guest_manifest_block(
                        Path(dst).read_text(encoding="utf-8"))
                    if dst_core.rstrip("\n") != manifest_source_text.rstrip("\n"):
                        changes.append((rel, source, dst, "update"))
                except (OSError, UnicodeDecodeError):
                    changes.append((rel, source, dst, "update"))
            continue
        # render_enabled=False(--target) 면 @render 태그를 강제로 끈다 — 템플릿은 토큰-form
        # 소스라 copy2 로 토큰을 보존해야 한다(렌더 시 operational leak·crash 회피).
        render = _entry_render_flag(entry) if render_enabled else False
        inventory, source_missing, _target_owned = manifest_entry_shipping_inventory(
            source_root,
            manifest,
            entry_index,
            effective_dest,
        )
        if source_missing:
            # 부재 보고는 manifest(dest) 경로(rel) — missing-핸들러가 @target-owned 플래그를
            # str(entry) key 로 조회한다. @source 항목은 non-@target-owned → source 부재면 rc2
            # (템플릿 누락 은폐 금지·안전판).
            missing.append(rel)
            continue
        for r, sp in inventory:
            # render 대상 판정 = @render manifest 선언 + **텍스트로 읽히는가**.
            # 옛 `.md` 확장자 하드 필터는 제거했다: 확장자 열거는 manifest 선언을 덮는 중복
            # 판정이라, codex 가 들여온 `.codex/agents/*.toml`(@render 선언 O)이 byte-copy 로
            # 새어 채택자 트리에 `{{PROJECT_NAME}}` 리터럴을 재전파했다(pm_import 와 동형 결함·
            # 두 채널을 함께 닫는다). 텍스트 아님(바이너리 리소스)은 여전히 byte-copy 로 남는다.
            file_render = render and _is_text_source(sp)
            dst = _RenderDst(effective_dest / r, file_render)
            if not dst.exists():
                changes.append((r, sp, dst, "new"))
            elif file_render:
                # render path: 템플릿이 산출물과 byte-equal 일 수 없으므로 filecmp 는 항상 오보.
                # 렌더한 결과가 dst 와 다를 때만 update(정직 판정).
                if not _render_eq_dst(sp, dst, effective_dest):
                    changes.append((r, sp, dst, "update"))
            elif str(r).replace("\\", "/") == _MANIFEST_SELF_REL:
                # engine.manifest self-prop: dest 는 apply 가 재부착한 add-harness guest 절을 갖고
                # upstream(sp)은 안 갖는다 — raw filecmp 면 매 sync '영원한 update'(churn).
                # guest 절을 차감한 **core 비교**로 판정한다(동일 추출 헬퍼·판정 사본 없음). 절 부재
                # (비-add-harness)면 strip 이 no-op → 기존 byte 비교와 동일. **trailing blank 정규화**
                # (`rstrip("\n")`): strip 이 절 앞 빈 줄을 회수하며 upstream 의 트레일링
                # 블랭크까지 지워, 트레일링 블랭크 보유 upstream 에서 반복 update(churn)가 나던 것을 닫는다.
                try:
                    dst_core = _strip_guest_manifest_block(Path(dst).read_text(encoding="utf-8"))
                    if dst_core.rstrip("\n") != Path(sp).read_text(encoding="utf-8").rstrip("\n"):
                        changes.append((r, sp, dst, "update"))
                except (OSError, UnicodeDecodeError):
                    if not filecmp.cmp(sp, dst, shallow=False):
                        changes.append((r, sp, dst, "update"))
            elif not filecmp.cmp(sp, dst, shallow=False):
                changes.append((r, sp, dst, "update"))
    return changes, missing


# ── add-harness guest @render 절 (engine.manifest self-prop 보존) ──────────────
# engine.manifest 는 self-prop `@source` 라 apply 가 upstream 사본으로 통째 덮어쓴다(guest 는 로컬-전용
# → selfheal 'diverged' 도 *파일* overwrite 는 못 막는다·plan 에 self-prop change 가 실린다·실측). 그래서
# add_harness 가 등재한 guest `@render` 가 1회 update 만에 사라져 렌더/overlay 스캔 커버리지가 끊기던 것을
# () 이 마커 구획으로 닫는다: apply 가 engine.manifest 를 덮기 **전** dest 의 guest 절을
# 추출 → 덮은 **뒤** 재부착한다. 마커는 read_manifest 가 '#' 주석으로 무시하고, 절 안의 라인은
# `@render @target-owned` 유효 항목이라 파서/스캔/렌더가 그대로 소비한다(판정원 단일 = engine.manifest
# 최종 뷰 하나). 절의 *값* 은 pm_import.add_harness 가 쓴다(같은 리터럴 공유·아래 두 상수).
_GUEST_MANIFEST_BEGIN = "# >>> pm add-harness guest @render (local·pm_update-preserved) >>>"
_GUEST_MANIFEST_END = "# <<< pm add-harness guest @render (local) <<<"


def _extract_guest_manifest_block(text: str) -> str | None:
    """engine.manifest 텍스트의 add-harness guest 절(마커 경계 포함)을 반환 — 없으면 None."""
    lines = text.splitlines()
    begin = end = None
    for i, line in enumerate(lines):
        s = line.strip()
        if s == _GUEST_MANIFEST_BEGIN:
            begin = i
        elif s == _GUEST_MANIFEST_END and begin is not None:
            end = i
            break
    if begin is None or end is None or end < begin:
        return None
    return "\n".join(lines[begin:end + 1])


def _strip_guest_manifest_block(text: str) -> str:
    """engine.manifest 텍스트에서 guest 절(마커 포함 + 선행 빈 줄)을 제거 — 마커 부재면 원문 그대로."""
    lines = text.splitlines()
    begin = end = None
    for i, line in enumerate(lines):
        s = line.strip()
        if s == _GUEST_MANIFEST_BEGIN:
            begin = i
        elif s == _GUEST_MANIFEST_END and begin is not None:
            end = i
            break
    if begin is None or end is None or end < begin:
        return text
    lo = begin
    while lo > 0 and lines[lo - 1].strip() == "":  # 절 앞 빈 줄 구분자까지 회수(누적 방지)
        lo -= 1
    kept = "\n".join(lines[:lo] + lines[end + 1:])
    return kept + "\n" if kept and not kept.endswith("\n") else kept


def _reattach_guest_block(new_text: str, guest_block: str | None) -> str:
    """upstream 사본(new_text) 뒤에 guest 절(guest_block)을 재부착 — block 없으면 new_text 그대로."""
    if not guest_block:
        return new_text
    sep = "" if new_text.endswith("\n") or not new_text else "\n"
    return new_text + sep + "\n" + guest_block + "\n"


def _core_manifest_paths(text: str) -> set[str]:
    """manifest 텍스트의 core 경로 집합 (guest 절·마커·주석·빈 줄 제외·마커 떼고 path 만)."""
    out: set[str] = set()
    for ln in _strip_guest_manifest_block(text).splitlines():
        s = ln.strip()
        if s and not s.startswith("#"):
            out.add(s.split()[0].replace("\\", "/"))
    return out


def _path_owned_by(path: str, owner_paths) -> bool:
    """path 가 owner_paths 중 하나에 소유되는가 — **동일**(`path==c`) OR **상위**(`path` 가 `c/` 하위).

    add-harness 등재 차감(pm_import `_guest_render_to_add`)과 update 재부착 차감
    (`_prune_guest_block_owned_by_core`)이 **공유**하는 소유권 판정(경로-포함·판정
    사본 금지) — core 가 `.opencode`(상위)를 가지면 `.opencode/agents` 도 소유로 본다."""
    p = path.replace("\\", "/")
    return any(
        p == c or p.startswith(c.rstrip("/") + "/")
        for c in (str(o).replace("\\", "/") for o in owner_paths))


def _prune_guest_block_owned_by_core(guest_block: str | None, core_text: str) -> str | None:
    """upstream core 가 소유하게 된 경로(**동일 OR 상위**)를 guest 절에서 차감.

    guest 경로가 추후 upstream core manifest 로 승격되면 apply 재부착이 기존 `@target-owned` guest 를
    그대로 붙여 **같은 경로가 core+guest 이중 등재** → 뒤쪽 guest 가 owner 로 이겨 upstream 소스가 영구
    skip 된다. 재부착 전 core 소유 경로를 `_path_owned_by`(경로-포함·add-측과 공유)로 차감해 닫는다.
    남는 guest 라인 0 이면 None(절 제거)."""
    if not guest_block:
        return None
    core_paths = _core_manifest_paths(core_text)
    kept: list[str] = []
    guest_count = 0
    for ln in guest_block.splitlines():
        s = ln.strip()
        if not s or s.startswith("#"):
            kept.append(ln)  # 마커/빈 줄 보존.
            continue
        if _path_owned_by(s.split()[0], core_paths):
            continue  # core 가 소유(동일/상위) — 차감.
        kept.append(ln)
        guest_count += 1
    if guest_count == 0:
        return None  # 전량 승격 — 절 제거.
    return "\n".join(kept)


def _copy_manifest_preserving_guest(sp, dst: Path) -> None:
    """engine.manifest 를 upstream(sp)으로 덮되 dest 의 add-harness guest 절을 재부착.

    재부착 전 **upstream core 가 소유하게 된 경로를 guest 절에서 차감**한다(소유권 전환·이중 등재
    방지). guest 절이 없거나(비-add-harness) 전량 승격되면 순수 copy2 와 동일(무영향)."""
    guest_block = None
    try:
        if dst.is_file():
            guest_block = _extract_guest_manifest_block(dst.read_text(encoding="utf-8"))
    except OSError:
        guest_block = None
    new_text = sp.read_text(encoding="utf-8")
    guest_block = _prune_guest_block_owned_by_core(guest_block, new_text)
    if not guest_block and isinstance(sp, Path):
        shutil.copy2(sp, dst)  # 비-add-harness 또는 전량 승격 — copy2(바이트/메타 무변경).
        return
    dst.write_text(_reattach_guest_block(new_text, guest_block), encoding="utf-8")


def apply(changes: list[tuple]) -> None:
    """change 적용 — render=False(기본)는 순수 copy2, render=True 는 render_adapter 후 기록.

    dst 가 `_RenderDst`(render 플래그 운반·plan 산출)면 그 플래그로 분기한다. 평문 Path dst
    (레거시 직접 호출)는 render 비대상 → copy2(후방호환·현 pm_update 동작 불변).

    engine.manifest self-prop overwrite 는 add-harness guest 절을 재부착한다(위 헬퍼).
    """
    render_mod = None  # render path 가 있을 때만 lazy-load.
    for _r, sp, dst, _kind in changes:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if getattr(dst, "render", False):
            dest_root = _dest_root_for(dst, _r)
            if render_mod is None:
                render_mod = _load_pm_render()
            operational, empty_keys = _operational_from_local_conf(dest_root)
            text = Path(sp).read_text(encoding="utf-8")
            rendered = render_mod.render_adapter(
                text, operational=operational, empty_keys=empty_keys)
            Path(dst).write_text(rendered, encoding="utf-8")
        elif str(_r).replace("\\", "/") == _MANIFEST_SELF_REL:
            # engine.manifest self-prop — upstream 사본으로 덮되 guest 절 보존.
            _copy_manifest_preserving_guest(sp, Path(dst))
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

    타깃 유효성은 REPO/templates/<name>/ 디렉토리 존재로 판단한다.
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


def discover_target_names() -> list[str]:
    """`templates/` 직계 디렉토리의 타깃 이름을 정렬해 반환한다.

    타깃 추가 시 CLI의 고정 목록을 갱신하지 않도록 `resolve_target_root`와 같은
    디렉토리-발견 규칙을 사용한다. `--all-targets`의 대상은 이 함수의 반환값이다.
    """
    templates_dir = _templates_dir()
    if not templates_dir.is_dir():
        return []
    # 숨김 디렉토리(.git 류)는 타깃이 아니다 — 비-숨김 직계 디렉토리는 전부 타깃으로 간주한다
    # (templates/ 는 관례상 타깃 전용·문서 열거 ↔ 디렉토리 집합 일치는 enumeration 가드가 강제).
    return sorted(path.name for path in templates_dir.iterdir()
                  if path.is_dir() and not path.name.startswith("."))


def resolve_manifest_for_dest(dest_root: Path, source_root: Path) -> Path:
    """dest_root 의 engine.manifest 우선, 없으면 source_root 의 것."""
    dest_manifest = dest_root / ".project_manager" / "engine.manifest"
    if dest_manifest.exists():
        return dest_manifest
    source_manifest = source_root / ".project_manager" / "engine.manifest"
    if source_manifest.exists():
        return source_manifest
    raise FileNotFoundError("engine.manifest 없음 (dest·source 둘 다).")


# ── 진입 doc 세대 마이그레이션 ────────────────────────────────
# 기존 채택자의 구형 진입 doc(자족 매뉴얼형 opencode `AGENTS.md`·~22KiB)을 신형(harness-neutral
# 공통 코어 + `.opencode/pm-instructions.md` + `opencode.jsonc` `instructions` 배열)으로 수렴시킨다
# — 2세대 영구 공존 차단(사용자 발의 "신형 전환 선택제=관리 분기"). self-update 흡수 경로 한정이며
# `--target` 엔진 export 는 비발화(skew/selfheal 와 동일 경계).
#
# 판정 = **미수정 여부 단 하나**(mechanize·추측 0). @render 치환(operational 토큰)·manual-fill TODO
# 마커가 채택자본을 세대 원본에서 벌려 놓으므로 순수 해시 대조는 불가능하다(ticket 열린 질문). →
# **치환-불변 정규화**로 판정한다: (1) manual-fill 마커(pm_import._mark_todos)를 벗겨 정규화하고,
# (2) 세대 원본에서 operational 토큰을 줄-경계 wildcard 로, free-form 토큰(`{{PROJECT_CONSTRAINTS}}`)
# 을 *리터럴*(미채움=pristine 요구)로 둔 패턴에 re.fullmatch 한다. operational=출하 렌더(전 채택자
# 결정적)라 wildcard(=미수정), free-form=채택자 FILL 영역이라 리터럴 요구(채웠으면 커스텀 흔적→무손
# loud). 이로써 local.conf 가 tagline/date 를 보존하지 않아도(board.py init 은 py·test_cmd·
# project_name 만 기록) 세대 판정이 성립한다 — 재렌더 대조(local.conf 미보유 토큰서 실패)보다 강건.
# 매칭 시 operational 값을 *포획*해 신형 재렌더에 재사용(채택자 tagline 보존).

# pm_import._mark_todos 가 manual-fill 시 free-form placeholder 줄 끝에 덧붙이는 마커 — 정규화로
# 벗겨낸다(세대 원본엔 없음). pm_import 리터럴과 동일해야 한다(단일 진실·거기서 바뀌면 여기도).
_ENTRY_DOC_MANUAL_TODO_MARKER = " <!-- TODO: 손으로 채우세요 -->"

# 구형 opencode AGENTS.md 의 H1 판별자 — 신형 title 은 "PM 어댑터 공통 코어"(이 문자열 부재). 세대
# clean-match 실패 시 "구형이나 수정됨(→loud) vs 신형/무관(→no-op)" 을 가르는 기계 신호.
_ENTRY_DOC_OLD_GEN_MARKER = "# AGENTS.md — opencode PM 어댑터"

# opencode.jsonc `instructions` 배열에 idempotent 추가할 신형 지침 경로(@source 전파).
_ENTRY_DOC_PM_INSTRUCTIONS_REL = ".opencode/pm-instructions.md"

# 중앙 백업 디렉토리 — pm_import 백업 채널 재사용(BACKUP_DIR_NAME 미러·relpath 미러링). 자동 전환
# 시 원본 AGENTS.md·opencode.jsonc 를 `<dest>/.pm_import_backups/<DATE>/<relpath>` 로 보존한다.
_ENTRY_DOC_BACKUP_DIR = ".pm_import_backups"

# 어댑터 `{{TOKEN}}` placeholder 스캔 — 세대 패턴 빌드용(operational=wildcard·그 외=리터럴 분류).
_ENTRY_DOC_TOKEN_RE = re.compile(r"\{\{([A-Z_]+)\}\}")

# 역대 출하 opencode AGENTS.md 세대(구형·자족 매뉴얼형)의 *원본* 텍스트 = fingerprint 자산. 채택자
# 사이트(프레임워크 git history 없음)에서도 자족적으로 세대 판정을 하려면 원본 텍스트가 필요하므로
# (해시만으론 치환-불변 대조 불가·위 근거) 엔진에 임베드한다. zlib+base64 인코딩 — 이스케이프 함정
# (triple-quote·backslash) 회피 + 콤팩트. 원본 = git `0ccc025`(v1.3.5 출하 세대). provenance 는
# tests/test_entry_doc_migration.py 가 git blob 과 기계 대조(무결성 lock). 세대 추가 시 아래 튜플에
# append("역대 출하본" 확장 지점·해시 목록의 텍스트 판).
_OLD_OPENCODE_AGENTS_V1_3_5_B64 = (
    "eNqtXHtzU0eW/9+fohdmZyUFSTavJF7MFgF2ll1eFZjdmWIpXyFdGw2ypJVkCDXMlgGZNdjZ2BMbZLA98sbEJuNUZCOw2JDa"
    "Kn8U/tS9+g77O+d034dsT0hqqATs++g+ffo8fufRd7868avT5y9fSoxk1LuxWVUo2vl0IWOri+eU+7jpTM10qo2enuP+Dbfa"
    "csdr7mJTuZML7tKMM11XztyUcpemnS/mlbO84Hy54EzXnEezyl2tukvjbn06oWIxeqUzNeUuvqWfLp6LXCwVfmenK+pcKp8a"
    "tktR5T5turW77mIN82E4d3nDrd9VzuqK87DpPqbXnMmVRCymTuZSoyDlJNHjvKniDXe5Gab/7NlzEdDifr+uhu2RkZRKqs7T"
    "KffeusK0USYf/zvrLbe64KxO0YyLP7Rf1pX7aAX/4VVmwWJVOa/HOo9auIa/naUX21tCZmdugajpOY5Xnakxd3FGtZtj7ZfV"
    "fuWvVA2EaO3cH2u/uQ9ylXXy7IlfnzoNtlsJ5UyugT/trRbGoYmdzQn3ybdM41zVmZxwFt/GYqE9mFzpPH6onG/WnM9bkWuj"
    "2VxGFUvZkVTptvpAOdUVvNuZbDitKWVVUuUblqoUCjnlLmA7qlH3yTSYutTepE1bx/rdiXrn8QStRkVOnPo03tvbe1RtbxED"
    "PnYba9Genv37VWe2SvysL4INCotX7kq1p+f3v7/46YV/Pn3y8uDlE786e+b86T/8oefY38Tj6vKFUxf6lX/7/IlzuKfajTEw"
    "vek++S9ibWdOxASCBPaOL7iLLXd1TPX950HemaXnCRWPH+fpexN7LF9FQKbzota5P6GvgN44+AkJK5TS1+1ypZSqFCBeA8oq"
    "jlgeoyBz+YqK9NEKabVhNkY6XzSdxkYU4tajFI1HD3aNAVGMWAlDV5JHLCeLI7StNKQ1gsv95mkrmlAlO5e6bZj8MWSJtYlF"
    "s17FREqVR4t26Wa2XChtb8lzB3vBtgXnm/EocY+kUqtgFVRl7IpdGsnms+VKNk0MpW0tF1O38iBO89eIHakPa4nlsbI0mgeL"
    "hRW0MHfhLUmgW1t3GrPO8xaTpOWMFGd7q/PHsfbrKQgAiJ+bANW0p1B53tnGXGdpKkgiVGxhnSWLuaKcly3a2cUxVbmezTMx"
    "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"
)
_OLD_OPENCODE_AGENTS_GENERATIONS = (_OLD_OPENCODE_AGENTS_V1_3_5_B64,)


def _decode_entry_doc_generation(b64: str) -> str:
    """임베드된 세대 원본(zlib+base64) → 텍스트."""
    return zlib.decompress(base64.b64decode(b64)).decode("utf-8")


def _entry_doc_operational_keys() -> frozenset:
    """세대 대조에서 wildcard(=출하 렌더)로 볼 operational 토큰 집합 = pm_render.OPERATIONAL_KEYS.
    그 밖의 `{{...}}`(free-form·`{{PROJECT_CONSTRAINTS}}`)은 리터럴(pristine 요구). pm_render 로드
    실패는 보수 폴백(하드코딩 동일 집합·엔진 co-located 라 정상 설치엔 항상 로드)."""
    try:
        return frozenset(_load_pm_render().OPERATIONAL_KEYS)
    except Exception:  # noqa: BLE001 — 로드 실패 폴백(pm_render 와 동일 집합).
        return frozenset((
            "PROJECT_NAME", "PROJECT_TAGLINE", "PROJECT_ROOT", "PY", "TEST_CMD", "DATE",
        ))


def _build_entry_doc_pattern(generation_text: str, operational_keys) -> tuple[str, dict]:
    """세대 원본 → (re.fullmatch 패턴, group→token 맵). operational 토큰은 줄-경계 wildcard 캡처
    그룹(`[^\\n]*`), 그 외 `{{...}}`(free-form)는 리터럴, 나머지 텍스트는 re.escape.

    operational 값은 출하 렌더라 채택자마다 다르나 미수정 신호(줄 내 값)이므로 `[^\\n]*`. free-form
    토큰은 채택자 FILL 영역이라 리터럴로 둬 미채움(pristine)일 때만 매칭한다(채웠으면 불일치→loud)."""
    parts: list[str] = []
    group_token: dict[str, str] = {}
    last = 0
    gi = 0
    for m in _ENTRY_DOC_TOKEN_RE.finditer(generation_text):
        parts.append(re.escape(generation_text[last:m.start()]))
        tok = m.group(1)
        if tok in operational_keys:
            name = f"op{gi}"
            gi += 1
            parts.append(f"(?P<{name}>[^\\n]*)")
            group_token[name] = tok
        else:
            parts.append(re.escape(m.group(0)))  # free-form/미상 토큰 = 리터럴(pristine 요구)
        last = m.end()
    parts.append(re.escape(generation_text[last:]))
    return "".join(parts), group_token


def _match_entry_doc_generation(
    generation_text: str, adopter_text: str, operational_keys
) -> dict | None:
    """정규화한 채택자 AGENTS.md 가 세대 원본 구조와 byte-match 하면 포획 operational 값 dict, 아니면 None.

    정규화 = manual-fill 마커 제거(pm_import._mark_todos). operational 토큰의 복수 occurrence 는
    같은 값이어야 한다(출하 시 uniform 치환) — 불일치면 손편집 신호로 None(안전·loud 로 낙하)."""
    normalized = adopter_text.replace(_ENTRY_DOC_MANUAL_TODO_MARKER, "")
    pattern, group_token = _build_entry_doc_pattern(generation_text, operational_keys)
    m = re.fullmatch(pattern, normalized)
    if m is None:
        return None
    values: dict[str, str] = {}
    for name, tok in group_token.items():
        v = m.group(name)
        if tok in values:
            if values[tok] != v:
                return None  # 같은 토큰 occurrence 값 불일치 → 비-uniform(손편집)·안전 낙하
        else:
            values[tok] = v
    return values


def _render_new_entry_doc(
    new_template_text: str, operational: dict, operational_keys
) -> str | None:
    """신형 AGENTS.md 템플릿 → operational 치환 산출물(free-form 리터럴 유지). operational leak
    잔존(값 미보유) 시 None — 미완 렌더 파일을 쓰지 않는다(안전·loud 로 낙하).

    render_adapter(assert_no_leak)는 free-form `{{PROJECT_CONSTRAINTS}}` 에서 raise 하므로 쓰지
    않는다 — operational 만 채우고 free-form 은 pristine 유지(신선 import --fill manual 과 동형)."""
    text = new_template_text
    for key, val in operational.items():
        if val:
            text = text.replace("{{" + key + "}}", str(val))
    for key in operational_keys:
        if "{{" + key + "}}" in text:
            return None  # operational 미해소 잔존 — 자족 위반 방지(free-form 은 허용)
    return text


# quoted-string 원소 추출 (escape-aware) — 등록-확인을 substring 이 아니라 *정확 원소* 대조로
# (): `.opencode/pm-instructions.md.bak` 같은 suffix 나 문자열-내
# 부분일치를 "이미 등록"으로 오인하지 않게 한다.
_JSONC_STRING_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')
# 최상위(depth==1) `"instructions"` 키 + 배열 여는 `[` — brace-depth 스캐너가 이 위치에서 match.
_INSTR_KEY_RE = re.compile(r'"instructions"\s*:\s*\[')


def _mask_jsonc_comments(text: str) -> str:
    """jsonc 주석(`//…`·`/* */`)을 같은 길이 공백(개행 보존)으로 마스킹 — **오프셋 보존**(원본과 1:1).

    문자열 리터럴은 존중한다 — 문자열 안의 `//`(예: `$schema` URL `https://…`)는 주석이 아니므로
    마스킹하지 않는다. 탐지/삽입 위치를 이 마스킹본에서 구하고 실제 write 는 원본에 같은 오프셋으로
    적용해, 주석-아웃된 `"instructions"`/경로를 오탐 없이 걸러내면서 원본 주석·서식을 보존한다."""
    out: list[str] = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:  # escape — 다음 문자 그대로(짝으로 소비)
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":  # 라인 주석 → EOL 까지 blank
            j = i
            while j < n and text[j] != "\n":
                out.append(" ")
                j += 1
            i = j
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":  # 블록 주석 → `*/` 까지 blank(개행 보존)
            j = i
            while j < n and not (text[j] == "*" and j + 1 < n and text[j + 1] == "/"):
                out.append("\n" if text[j] == "\n" else " ")
                j += 1
            if j < n:  # 닫는 `*/`
                out.append("  ")
                j += 2
            i = j
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _scan_array_end(masked: str, body_start: int) -> int:
    """배열 `[` 직후 body_start 부터 매칭되는 `]` 위치를 문자열/중첩 존중으로 찾는다(배열 body 끝).

    문자열 리터럴 내 `]`·중첩 `[...]` 은 건너뛴다. 닫는 `]` 부재(비정상)면 끝(len) 반환."""
    i, n = body_start, len(masked)
    depth = 0
    in_str = False
    while i < n:
        c = masked[i]
        if in_str:
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
        elif c == "[":
            depth += 1
        elif c == "]":
            if depth == 0:
                return i
            depth -= 1
        i += 1
    return n


def _find_toplevel_instructions(masked: str) -> tuple[int | None, int | None, int | None]:
    """주석-마스킹된 jsonc 에서 **최상위(depth==1)** `"instructions"` 배열을 brace-depth 추적으로 찾는다.

    중첩 객체(agent/provider 블록 등)의 `"instructions"` 는 무시한다() — opencode 가 읽는
    진입 지침 배열은 최상위 키 하나다. 문자열 리터럴 내 brace/bracket 은 세지 않는다(문자열 상태 추적).

    반환 (body_start, body_end, root_end):
      - 최상위 instructions 배열 존재 → (배열 `[` 직후, 닫는 `]` 위치, None): 검사/append 용.
      - 부재 → (None, None, 최상위 여는 `{` 직후 오프셋): 신설 블록 삽입 위치.
      - 최상위 `{` 부재(비정상) → (None, None, None)."""
    i, n = 0, len(masked)
    depth = 0
    root_end: int | None = None
    in_str = False
    while i < n:
        c = masked[i]
        if in_str:
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            # 최상위(depth==1)의 "instructions" 키만 후보 — 중첩(depth>1)은 무시.
            if depth == 1:
                m = _INSTR_KEY_RE.match(masked, i)
                if m:
                    body_start = m.end()  # 여는 `[` 직후
                    return body_start, _scan_array_end(masked, body_start), None
            in_str = True
            i += 1
            continue
        if c == "{":
            depth += 1
            if depth == 1 and root_end is None:
                root_end = i + 1  # 최상위 여는 `{` 직후
            i += 1
            continue
        if c == "}":
            depth -= 1
            i += 1
            continue
        i += 1
    return None, None, root_end


def _ensure_jsonc_instructions(jsonc_text: str) -> tuple[str, bool]:
    """opencode.jsonc **최상위** `instructions` 배열에 신형 지침 경로를 idempotent 추가(comment-preserving).

    반환 (new_text, changed). 최상위 배열에 이미 (비-주석) 원소로 있으면 무변경. 최상위 배열이
    있으나 경로가 없으면 배열 앞에 삽입. 최상위 배열이 없으면 최상위 `{` 직후 신설 블록 삽입.
    JSONC(주석)라 json.load 불가 — 주석을 **오프셋 보존 마스킹**한 사본 위에서 **brace-depth 추적**
    으로 위치를 구하고 원본에 같은 오프셋으로 write 한다(비파괴·주석·타 키·provider 보존).

    **최상위(depth==1) 한정** (): 중첩 객체(agent/provider)의 `"instructions"` 가 파일에서
    먼저 나와도 그 중첩 배열에 삽입하지 않는다 — opencode 가 로드하는 진입 지침은 최상위 키다.
    등록-확인은 **quoted-string 원소 정확 대조**(substring 오인 방지·주석-아웃/`.bak` suffix)."""
    rel = _ENTRY_DOC_PM_INSTRUCTIONS_REL
    masked = _mask_jsonc_comments(jsonc_text)  # 주석 blank(오프셋 == 원본)
    body_start, body_end, root_end = _find_toplevel_instructions(masked)
    if body_start is not None:
        elements = _JSONC_STRING_RE.findall(masked[body_start:body_end])  # 주석-아웃 원소 제외
        if rel in elements:
            return jsonc_text, False  # idempotent — 최상위 배열에 이미 등록
        return jsonc_text[:body_start] + f'\n    "{rel}",' + jsonc_text[body_start:], True
    if root_end is None:
        return jsonc_text, False  # 최상위 `{` 없음 — 비정상 config·무변경(안전)
    block = f'\n  "instructions": [\n    "{rel}"\n  ],'
    return jsonc_text[:root_end] + block + jsonc_text[root_end:], True


def _entry_doc_backup_root(dest_root: Path) -> Path:
    """중앙 백업 루트 `<dest>/.pm_import_backups/<DATE>/` (pm_import 채널 재사용·relpath 미러)."""
    return Path(dest_root) / _ENTRY_DOC_BACKUP_DIR / datetime.date.today().isoformat()


def _entry_doc_backup(dest_root: Path, rel: str, backup_root: Path) -> None:
    """`dest_root/rel` 을 `backup_root/rel` 로 복사(중앙 백업·relpath 미러링). 부재면 무동작."""
    src = Path(dest_root) / rel
    if not src.is_file():
        return
    dst = Path(backup_root) / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def migrate_entry_doc(effective_dest: Path, source_root: Path, *, write: bool) -> dict:
    """진입 doc 세대 마이그레이션 — self-update 흡수 경로 한정(호출부가 --target 게이트).

    구형 미수정 opencode `AGENTS.md`(세대 fingerprint clean-match) → 신형 공통 코어로 자동 교체
    (+백업·`opencode.jsonc` instructions 배열 idempotent 추가). 커스텀 흔적(FILL·손편집) → 무손·
    loud 안내. 이미 신형/재실행 → no-op 멱등(부분 전환 시 jsonc instructions 만 복구). `write=False`
    (dry-run)면 판정만 하고 파일을 쓰지 않는다. 반환 dict 는 finding 출력·테스트 단언 공용.

    status ∈ {'not_opencode','no_agents','no_new_template','migrated','loud_manual','noop','recovered'}.
    """
    dest = Path(effective_dest)
    jsonc_path = dest / ".opencode" / "opencode.jsonc"
    agents_path = dest / "AGENTS.md"
    result: dict = {
        "status": "not_opencode", "agents_replaced": False, "jsonc_updated": False,
        "backup_rel": None, "matched_generation": None,
    }
    # opencode 채택자 게이트 — opencode.jsonc 부재면 비-opencode(claude 등)·비발화.
    if not jsonc_path.is_file():
        return result
    if not agents_path.is_file():
        result["status"] = "no_agents"
        return result

    adopter_agents = agents_path.read_text(encoding="utf-8")
    operational_keys = _entry_doc_operational_keys()

    # 세대 clean-match 탐색 (구형 미수정?).
    captured = None
    matched_gen = None
    for idx, b64 in enumerate(_OLD_OPENCODE_AGENTS_GENERATIONS):
        try:
            gen_text = _decode_entry_doc_generation(b64)
        except Exception:  # noqa: BLE001 — 세대 디코드 실패는 그 세대 skip(다음 세대 시도).
            continue
        captured = _match_entry_doc_generation(gen_text, adopter_agents, operational_keys)
        if captured is not None:
            matched_gen = idx
            break

    if captured is not None:
        # ── 구형 미수정 → 자동 전환 ─────────────────────────────────────────
        new_tmpl_path = source_root / "templates" / "opencode" / "AGENTS.md"
        if not new_tmpl_path.is_file():
            # 신형 목적지(source) 부재 — fail-soft(무손·loud 아님·비정상 source 신호는 타 게이트).
            result["status"] = "no_new_template"
            return result
        # operational: 포획값(tagline 등 local.conf 미보유분) + local.conf(현재 진실·py/name/test_cmd 우선).
        local_op, _empty = _operational_from_local_conf(dest)
        operational = {**captured, **local_op}
        new_text = _render_new_entry_doc(
            new_tmpl_path.read_text(encoding="utf-8"), operational, operational_keys)
        if new_text is None:
            # operational 재렌더 미완 — 무손·loud 로 낙하(미완 파일을 쓰지 않는다).
            result["status"] = "loud_manual"
            result["matched_generation"] = matched_gen
            return result
        adopter_jsonc = jsonc_path.read_text(encoding="utf-8")
        new_jsonc, jsonc_changed = _ensure_jsonc_instructions(adopter_jsonc)
        result.update(status="migrated", matched_generation=matched_gen,
                      agents_replaced=True, jsonc_updated=jsonc_changed)
        if write:
            backup_root = _entry_doc_backup_root(dest)
            _entry_doc_backup(dest, "AGENTS.md", backup_root)
            if jsonc_changed:
                _entry_doc_backup(dest, ".opencode/opencode.jsonc", backup_root)
            agents_path.write_text(new_text, encoding="utf-8")
            if jsonc_changed:
                jsonc_path.write_text(new_jsonc, encoding="utf-8")
            result["backup_rel"] = (
                f"{_ENTRY_DOC_BACKUP_DIR}/{datetime.date.today().isoformat()}")
        return result

    # ── clean-match 실패 ────────────────────────────────────────────────────
    if _ENTRY_DOC_OLD_GEN_MARKER in adopter_agents:
        # 구형 세대이나 수정됨(FILL·손편집) → 무손·loud 안내(수동 병합·커스텀 보존).
        result["status"] = "loud_manual"
        return result
    # 신형/무관 — AGENTS.md 미터치. opencode.jsonc instructions 만 idempotent 보장(부분 전환 복구·멱등).
    adopter_jsonc = jsonc_path.read_text(encoding="utf-8")
    new_jsonc, jsonc_changed = _ensure_jsonc_instructions(adopter_jsonc)
    result["jsonc_updated"] = jsonc_changed
    result["status"] = "recovered" if jsonc_changed else "noop"
    if jsonc_changed and write:
        backup_root = _entry_doc_backup_root(dest)
        _entry_doc_backup(dest, ".opencode/opencode.jsonc", backup_root)
        jsonc_path.write_text(new_jsonc, encoding="utf-8")
        result["backup_rel"] = f"{_ENTRY_DOC_BACKUP_DIR}/{datetime.date.today().isoformat()}"
    return result


def _print_entry_doc_migration_finding(result: dict, *, dry_run: bool = False) -> None:
    """migrate_entry_doc 결과를 사람이 읽을 형태로 출력(loud 안내).

    'migrated'/'loud_manual'/'recovered' 만 출력 — 'noop'·'not_opencode'·'no_agents'·
    'no_new_template' 는 조용(정상/무관·노이즈 회피). 전환/복구 자체는 migrate_entry_doc 이 수행."""
    status = result.get("status")
    if status == "migrated":
        verb = "전환 예정" if dry_run else "전환"
        gen = result.get("matched_generation")
        tail = " + opencode.jsonc instructions 배열 추가" if result.get("jsonc_updated") else ""
        print(f"→ 진입 doc 세대 마이그레이션 {verb} — 구형 미수정 opencode AGENTS.md "
              f"(세대 #{gen})를 신형 공통 코어로 교체{tail}.")
        if dry_run:
            print("    (원본은 .pm_import_backups/<DATE>/ 에 백업 예정·적용 안 함)")
        elif result.get("backup_rel"):
            src = "AGENTS.md·opencode.jsonc" if result.get("jsonc_updated") else "AGENTS.md"
            print(f"    백업: {result['backup_rel']}/ (원본 {src})")
    elif status == "loud_manual":
        print("⚠️  진입 doc 세대 마이그레이션 — 구형 opencode AGENTS.md 를 감지했으나 커스텀 흔적"
              "(FILL·손편집)이 있어 자동 전환하지 않는다(무손).")
        print("    신형(공통 코어 + .opencode/pm-instructions.md + opencode.jsonc instructions)으로 "
              "수동 병합하려면:")
        print("      1) templates/opencode/AGENTS.md(신형 공통 코어)로 AGENTS.md 를 교체하고 "
              "커스텀(프로젝트 고유 제약 등)을 프로젝트 고유 제약으로 옮긴다.")
        print("      2) opencode.jsonc 최상위에 "
              '`"instructions": [".opencode/pm-instructions.md"]` 를 추가한다(기존 배열이면 경로 append).')
    elif status == "recovered":
        verb = "추가 예정" if dry_run else "추가"
        print("→ 진입 doc — opencode.jsonc `instructions` 배열에 .opencode/pm-instructions.md "
              f"{verb}(신형 정합·idempotent 복구).")


# ── 보호 훅 전수 재설치 트리거 ────────────────────────────────
# 보호 훅(`.local/repo-hooks/<repo>/pre-push`·`pre-commit`)은 엔진 코드(worktree_pool 의 훅
# 본문 상수)에서 *생성*되는 런타임 산출물이라, 엔진 파일이 갱신돼도 **재설치가 돌아야** 새 훅이
# 디스크에 놓인다. 그런데 기존 설치 트리거는 `repo add`·`worktree add` 둘뿐이었다 — 즉 엔진
# 업그레이드만 한 채택자는 새 훅(예: pre-commit 가드)을 **영영 못 받는다**(값-연결이
# 끊긴 채 green·[[robustness-value-connections-before-ship]]). 그래서 매 sync **실행마다** 등록
# repo 전수 정합 확인 + drift 재설치를 신설한다.
#
# ⚠ **`changes` 유무로 게이트하지 않는다**(내부/외부 게이트 must-fix·격리 실측): 업그레이드
# 경계에서 sync 를 *실행하는 주체는 dest 의 구 엔진*이다 — 이 기능을 배달하는 그 sync 자체는
# 재설치 코드를 갖고 있지도 않다(RUN 1 미발화). 바로 다음 실행은 dest 가 신 엔진이지만
# `changes == 0` 이라, "changes>0 에서만" 으로 좁히면 **다음에 우연히 엔진이 또 바뀔 때까지**
# 훅이 안 깔린다(RUN 2 미발화). 그래서 옆의 `migrate_entry_doc` 와 **동형으로** changes 0 경로
# 에서도 돈다. 노이즈는 트리거를 끄는 대신 **정합이면 조용**(아래 `_protected_hook_in_sync`
# drift 판정)으로 낮춘다 — sidecar reconcile 의 "비교 우선·정합이면 subprocess 0" 과
# 같은 패턴이라 새 개념이 늘지 않는다. 이 판정은 훅 디렉토리가 통째로 지워진 clone 도 덮는다
# (bootstrap reconcile 은 sidecar 파일이 없으면 즉시 return 이라 그 상태를 영구 침묵한다).
#
# 배선은 **기존 계약을 그대로 탄다**(신규 seam 0): dest 의 `pm_config._install_protected_hook_
# reporting` → `_resolve_repo_protected`(areas 권위) →
# `worktree_pool.install_protected_hook`(훅·sidecar·hooksPath). pm_update 는 목록 해소도 훅
# 본문도 재구현하지 않는다.
#
# **dest 의 엔진**을 로드한다(source 아님) — sync 로 방금 갱신된 사본이 새 훅 본문을 들고 있고,
# 등록 repo 레지스트리(areas.md)·훅 디렉토리도 dest(PM 홈) 소유다. `--target`(루트→templates
# 엔진 export)은 비발화 — templates/<name> 은 PM 홈이 아니라 출하 스캐폴드라 등록 repo 가 없다
# (selfheal/skew/진입 doc 마이그레이션과 같은 경계).
def _load_dest_pm_config(dest_root: Path):
    """dest(방금 동기된) `.project_manager/tools/pm_config.py` 를 로드 (_load_pm_import 동형).

    실행 중인 pm_update 프로세스는 **sync 이전** 코드를 메모리에 들고 있으므로, 재설치는 반드시
    디스크의 *새* 사본을 로드해서 돌려야 신 훅 본문이 배포된다. 부재(구형 dest·엔진 미배치)면
    None — 호출부가 fail-soft 로 보고한다. 로드 예외는 전파(호출부가 잡아 loud 보고)."""
    pm_config_py = Path(dest_root) / ".project_manager" / "tools" / "pm_config.py"
    if not pm_config_py.exists():
        return None
    spec = importlib.util.spec_from_file_location("pm_config", pm_config_py)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _read_hook_artifact(path: Path) -> str | None:
    """설치된 훅 산출물(훅 본문·sidecar) 1개를 읽는다 — 부재/읽기 실패는 **None**.

    `_protected_hook_in_sync` 의 유일한 읽기 창구다. 부재와 **읽기 실패**(non-UTF-8 로 깨진
    본문·권한·IO 오류)를 *같은* None 으로 수렴시키는 게 요점 — 둘 다 "이 파일은 현 엔진 산출물이
    아니다" 라는 같은 결론이고, 따라서 같은 해소(재설치)로 가야 한다. 예외를 밖으로 내면 호출부의
    fail-soft 가 그걸 `unavailable`(=재설치 안 함)로 처리해 **깨진 훅이 영영 복구되지 않는다**."""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None            # 부재·권한·IO — 재설치 대상.
    except UnicodeDecodeError:
        return None            # 깨진 본문(non-UTF-8) — 우리 산출물이 아니다 → 재설치 대상.


# 실행 비트 축을 볼 수 있는 플랫폼인가 — POSIX 만. Windows 는 실행 비트 개념이 다르다(NTFS 에
# mode 가 없고 `Path.chmod` 는 read-only 플래그만 만진다·`st_mode` 는 항상 0o666/0o444 계열).
# 거기서 이 축을 보면 **매 sync 거짓 drift → 무한 재설치**가 된다. git-for-windows 는 훅을 sh 로
# 돌리며 실행 비트를 요구하지도 않으므로 축 자체가 무의미하다. 테스트는 이 상수를 뒤집어
# Windows 거동을 hermetic 하게 친다(플랫폼 분기 실행 불요).
_EXEC_BIT_MEANINGFUL = os.name != "nt"


def _hook_artifact_executable(path: Path) -> bool:
    """산출물이 실행 가능한가 — 실행권한 축 (Windows 는 축 비활성이라 항상 True).

    **없으면 git 이 훅을 조용히 건너뛴다** — 본문만 비교하면 `chmod 0644` 된 훅이 `in_sync` 로
    오판돼 보호가 침묵 비활성화된다. stat 실패(부재·권한)는 `False`(drift·재설치)."""
    if not _EXEC_BIT_MEANINGFUL:
        return True
    try:
        return bool(path.stat().st_mode & 0o111)
    except OSError:
        return False


def _protected_hook_in_sync(repo: str, *, pm_config, worktree_pool, board) -> bool:
    """이 repo 의 설치된 보호 훅이 **현재 엔진과 정합**인가 — drift 판정.

    정합이면 재설치를 건너뛴다(매 sync 반복 출력 회피). "비교 우선·정합이면 조용" 은
    sidecar reconcile 과 같은 패턴이다.

    **축을 열거하지 않고 유도한다** — 봐야 할 것은 정의상 "`install_protected_hook` 이 쓰는 것"
    이므로, 그 함수와 **같은 명세**(`worktree_pool.protected_hook_artifacts`)를 읽어 산출물마다
    내용 실행권한(필요한 것만)을 대조하고, 파일이 아닌 bare `core.hooksPath` **배선**은
    `pm_config.protected_hook_wired()`로 본다. 판정이 자체 목록을 들면 설치가
    자랄 때 조용히 갈라진다 — 실제로 그 클래스가 연달아 났다(읽기 실패 축·실행 비트 축).

    **모르면 재설치 쪽으로 기운다**(fail-safe): 파일 부재·**읽기 실패**(깨진 본문·권한·IO)·
    실행권한 상실·명세 부재(구 엔진 사본)·배선 판정 불가(`None`)는 전부 drift 로 수렴한다 —
    재설치는 멱등이라 비용이 낮고, 반대 방향(조용히 stale 유지)은 보호가 꺼진 채 침묵하는
    실패모드다.

    **일반 판정 예외는 밖으로 내지 않는 게 계약**이다(fail-safe False). 단 엔진 사본
    불일치 marker는 계속 실행할 수 없는 상태라 재전파한다. 호출부의
    `unavailable` 은 "dest 엔진/모듈을 못 불렀다"(=판정 자체가 불가능)를 위한 상태지 "파일이
    깨졌다"가 아니다 — 후자를 unavailable 로 흘리면 재설치가 안 돌아 복구 경로가 사라진다."""
    try:
        artifacts_of = getattr(worktree_pool, "protected_hook_artifacts", None)
        if artifacts_of is None:
            return False       # 구 엔진 사본(명세 부재) — 판정 불가 → 재설치.
        # 설치가 받는 것과 **같은 입력**(areas 실효 보호목록)으로 기대 산출물을 유도한다.
        expected_list = list(pm_config._resolve_repo_protected(repo, board=board))
        gate_config = getattr(pm_config, "_protected_push_gate_config", None)
        if gate_config is None:
            return False       # 구 엔진 사본(형상 resolver 부재) — 재설치로 새 계약 배포.
        # read-only drift 판정은 steady-state 무출력 계약을 지킨다. 강등 경고는 실제 설치
        # 깔때기에서 1회 loud하게 나가며, 여기서는 같은 resolver 결과만 소비한다.
        gate_mode, test_cmd = gate_config(repo, board=board, report_downgrade=False)
        for artifact in artifacts_of(
                repo, expected_list, gate_mode=gate_mode, test_cmd=test_cmd):
            if _read_hook_artifact(artifact.path) != artifact.content:
                return False
            if artifact.executable and not _hook_artifact_executable(artifact.path):
                return False
        # 배선 축 — `False`(hooksPath 가 우리 디렉토리를 안 가리킴)면 훅이 아예 발화하지 않는다.
        # `None`(bare 부재·git 실패)은 판정 불가 → 재설치 시도(install 이 결과를 loud 보고).
        return pm_config.protected_hook_wired(repo, worktree_pool=worktree_pool) is True
    except Exception as exc:  # noqa: BLE001 — 일반 판정 실패만 drift로 수렴(단 skew 재전파).
        if _is_engine_rev_skew(exc):
            raise
        return False


def reinstall_protected_hooks(dest_root: Path, *, write: bool) -> dict:
    """등록 repo 전수 보호 훅 정합 확인 + drift 재설치 — 엔진 업그레이드 배포 트리거.

    **매 sync 실행마다** 돈다(changes 유무 무관 — 위 모듈 주석의 RUN1/RUN2 실측 참조).
    `write=False`(dry-run)면 판정만 하고 아무것도 쓰지 않는다(migrate_entry_doc 의 write 플래그
    동형). 반환 dict:
      - `status` — "done" / "no_repos"(등록 repo 0) / "unavailable"(엔진/레지스트리 미해소)
      - `targets` — 판정 대상 repo(= bare 미러 보유) · `in_sync` — 정합이라 건너뛴 repo(조용)
      - `drifted` — 재설치가 필요한 repo(⊆ targets) · `failed` — 그중 설치 실패
      - `no_bare` — 등록됐지만 `.repos/<repo>.git` 이 없어 게이트할 대상이 없는 repo
      - `reason` — unavailable 사유(사람이 읽는 1줄)

    **bare 부재는 실패가 아니다** — 게이트할 미러가 없으면 훅도 무의미하다(install 이 no-op
    False). 매 sync 마다 경고를 울리는 대신 `no_bare` 로 분리해 요약 1줄로만 surface 한다
    (침묵 아님·`_print_protected_hook_reinstall_finding`).

    **fail-soft** — sync 는 이미 성공했다. 일반 엔진 로드 실패(구형 dest)나 레지스트리
    파싱 실패가 update rc 를 바꾸면 안 된다 → 예외를 "unavailable"+사유로 강등하고 호출부가
    경고로 낸다(훅은 추가 가드·`_install_protected_hook` 의 fail-soft 계약과 동형). 단 stamped
    sibling의 marked rev skew는 불일치 엔진으로 계속 실행하지 않도록 재전파한다.

    ⚠ **"unavailable" 은 판정 자체가 불가능한 경우만**이다 — dest 엔진/레지스트리를 못 불러
    *어느 repo 도* 손댈 수 없는 상태. **개별 repo 의 훅 파일이 깨진 것은 unavailable 이 아니라
    drift** 다(`_protected_hook_in_sync` 가 읽기 실패를 False 로 수렴). 그 구분이 무너지면
    "깨진 훅을 발견했는데 재설치는 안 하는" 경로가 생겨 복구 채널이 사라진다."""
    result: dict = {"status": "unavailable", "targets": [], "in_sync": [],
                    "drifted": [], "failed": [], "no_bare": [], "reason": None}
    try:
        pm_config = _load_dest_pm_config(dest_root)
        if pm_config is None:
            result["reason"] = (
                f"{dest_root}/.project_manager/tools/pm_config.py 부재 — 로드 불가")
            return result
        board = pm_config._load_module("board", "board.py")
        worktree_pool = pm_config._load_module("worktree_pool", "worktree_pool.py")
        if board is None or worktree_pool is None:
            missing = "board.py" if board is None else "worktree_pool.py"
            result["reason"] = f"dest 엔진 {missing} 부재/로드 실패"
            return result
        repos = sorted(board.registered_repos())
        if not repos:
            result["status"] = "no_repos"
            return result
        for repo in repos:
            # bare 미러가 있어야 `core.hooksPath` 를 걸 대상이 있다(install 과 같은 가드).
            if not worktree_pool.bare_repo_path(repo).exists():
                result["no_bare"].append(repo)
                continue
            result["targets"].append(repo)
            if _protected_hook_in_sync(repo, pm_config=pm_config,
                                       worktree_pool=worktree_pool, board=board):
                result["in_sync"].append(repo)
                continue
            result["drifted"].append(repo)
            if not write:
                continue
            ok = pm_config._install_protected_hook_reporting(
                repo, board=board, worktree_pool=worktree_pool, action="(재)설치")
            if not ok:
                result["failed"].append(repo)
        result["status"] = "done"
        return result
    except Exception as exc:  # noqa: BLE001 — 재설치 실패가 성공한 sync 를 무효화하면 안 됨.
        if _is_engine_rev_skew(exc):
            raise
        result["status"] = "unavailable"
        result["reason"] = f"{type(exc).__name__}: {exc}"
        return result


def _print_protected_hook_reinstall_finding(result: dict, *, dry_run: bool = False) -> None:
    """reinstall_protected_hooks 결과 요약 (per-repo 성공/실패 줄은 pm_config 깔때기 소관).

    **정합이면 완전히 조용**하다(`in_sync` 만 있는 매 sync 의 정상 경로) — 트리거를 끄지 않고
    출력만 낮춘 게 이 함수다. 등록 repo 0(=`no_repos`)도 조용(걸 대상 없음). `unavailable` 은
    훅이 갱신되지 않았다는 뜻이므로 stderr 경고 + 재설치 커맨드를 낸다(침묵 무력화 금지)."""
    status = result.get("status")
    if status == "no_repos":
        return
    if status == "unavailable":
        print(
            "[경고] 보호 브랜치 훅 정합 확인/재설치를 건너뛰었다 — "
            f"{result.get('reason')}. 이 clone 의 훅은 **옛 엔진 본문**으로 남을 수 있다.\n"
            "  → 재설치(멱등): pm-config repo add <repo>",
            file=sys.stderr,
        )
        return
    drifted = result.get("drifted") or []
    if drifted and dry_run:
        print(f"→ 보호 브랜치 훅 (재)설치 예정: {', '.join(drifted)} "
              "(설치된 훅이 현 엔진과 불일치·적용 안 함)")
    no_bare = result.get("no_bare") or []
    if no_bare:
        print(f"→ 보호 훅 대상 아님(bare `.repos/<repo>.git` 부재): {', '.join(no_bare)} "
              "— 미러를 만들면(`pm-config repo add <repo>`) 훅이 걸린다.")


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="pm_update.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "--from 생략 시 <dest>/.project_manager/local.conf 의 `upstream=` 값을 기본으로 쓴다 "
            "(pm_import 가 한 번 import 하면 자동 기록·--from 명시로 override 가능). "
            "단 upstream= 이 **URL**(릴리스 추적 기본)이면 엔진은 로컬 파일만 복사하므로 "
            "(git clone/fetch 안 함) 자동 진행하지 않고 명확한 에러로 멈춘다 — "
            "`pm-update` 스킬(URL→cache clone)을 쓰거나 `--from <로컬 checkout>` 을 명시하라. "
            "upstream 미등록이거나 그 경로가 부재/디렉토리 아님이어도 명확한 에러로 멈춘다(침묵 폴백 없음)."
        ),
    )
    ap.add_argument("--from", dest="source", required=False, default=None,
                    help="upstream 프레임워크 checkout 경로 "
                         "(생략 시 local.conf 의 upstream= 사용)")
    ap.add_argument("--dry-run", action="store_true")
    target_group = ap.add_mutually_exclusive_group()
    target_group.add_argument(
        "--target",
        metavar="NAME",
        help=(
            "루트에서 templates/<NAME>/ 타깃으로 동기화. "
            "REPO/templates/<NAME>/ 디렉토리가 존재하면 유효. "
            "생략 시 self-location(스크립트 위치 기준 dest) 사용."
        ),
    )
    target_group.add_argument(
        "--all-targets",
        action="store_true",
        help=(
            "루트에서 templates/ 직계 하위의 존재하는 모든 타깃으로 동기화. "
            "새 타깃도 디렉토리만 있으면 자동 포함한다. --target 및 --changes 와 함께 쓸 수 없다."
        ),
    )
    # ── read-only 변경점 확인 (실 sync 안 함) ──────────────
    ap.add_argument(
        "--changes",
        action="store_true",
        help=(
            "받은 upstream baseline(local.conf upstream_rev) ↔ 그 이후 upstream HEAD 변경점을 "
            "read-only 로 요약(실 sync 안 함). 엔진 영향(manifest 경로)/그 외 분리. "
            "upstream 이 로컬 checkout 일 때만(URL 은 명확 에러·git clone/fetch 안 함)."
        ),
    )
    ap.add_argument(
        "--count-only",
        action="store_true",
        help="--changes 와 함께: baseline..HEAD commit 개수 1줄만 출력(advisory/스크립트).",
    )
    ap.add_argument(
        "--log",
        action="store_true",
        help="--changes 와 함께: `git log --oneline baseline..HEAD` 커밋 목록을 꼬리에 출력.",
    )
    args = ap.parse_args(argv)

    # ── --count-only/--log 는 --changes 전용 (codex suggestion 2·CLI 오사용 차단) ──
    #    --changes 없이 주면 일반 sync 가 돌면서 두 옵션이 조용히 무시된다 → 명확 에러로 멈춘다.
    #    --all-targets 분기보다 **앞**이어야 한다 — 뒤면 자식 argv 에 안 실리는 두 옵션이 조용히
    #    무시된 채 실 동기화가 돈다(오사용 검증이 모든 모드에 선행).
    if (args.count_only or args.log) and not args.changes:
        misused = []
        if args.count_only:
            misused.append("--count-only")
        if args.log:
            misused.append("--log")
        print(
            f"오류: {', '.join(misused)} 는 --changes 전용 옵션이다 — --changes 와 함께 쓰라 "
            "(read-only 변경점 확인 모드). 일반 sync 에는 무효.",
            file=sys.stderr,
        )
        return 1

    # 전체 export 는 타깃 집합을 디렉토리에서 매번 발견한다. 단일 타깃 실행을 재사용해
    # manifest/안전 가드/출력의 의미를 갈라놓지 않는다. 한 타깃의 실패는 즉시 반환한다.
    if args.all_targets:
        if args.changes:
            print("오류: --all-targets 는 실 동기화 옵션이며 --changes 와 함께 쓸 수 없다.", file=sys.stderr)
            return 1
        target_names = discover_target_names()
        if not target_names:
            print("오류: templates/ 아래에 동기화할 타깃 디렉토리가 없다.", file=sys.stderr)
            return 1
        for target_name in target_names:
            child_argv = ["--target", target_name]
            if args.source:
                child_argv = ["--from", args.source, *child_argv]
            if args.dry_run:
                child_argv.append("--dry-run")
            rc = main(child_argv)
            if rc:
                return rc
        return 0

    # ── read-only 변경점 확인 — main 초입 early-return(실 sync 안 함).
    #    dest/source 해소는 _run_changes 안에서 sync 와 동일 경로(_resolve_dest_source)로 탄다.
    if args.changes:
        return _run_changes(args)

    # dest/source 해소(--target·--from·URL 게이트·stale 가드)는 --changes 와 공유한다.
    rc, dest_root, source_root = _resolve_dest_source(args)
    if rc != 0:
        return rc
    effective_dest = dest_root if dest_root is not None else REPO

    # manifest: dest_root 의 것 우선, 없으면 source 의 것
    try:
        manifest_path = resolve_manifest_for_dest(effective_dest, source_root)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    manifest = read_manifest(manifest_path)

    # ── manifest 자기치유 (self-update 2-pass) — upstream engine.manifest 를 이번 sync 의
    #    계획 기준으로 승격해, 로컬 manifest 가 구형이어도 신규 등재분이 한 번의 실행으로 plan→apply
    #    에 실린다(회사 실측: bare CLI 흡수가 신규 등재분 미도달). manifest 자신도 self-prop 엔트리
    #라 같은 plan 안에서 로컬 파일이 upstream 판으로 갱신된다(별도 write 불요). upstream
    #    manifest 부재/읽기 실패는 fail-soft(로컬 유지) — baseline 억제가 그 잔여 경로
    #    안전망. --target(엔진 export)은 타깃 manifest 가 루트와 의도적으로 달라 승격하지 않는다
    #    (현행·아래 skew 검출과 동일 경계). 승격 후 skew 는 정의상 0(manifest==upstream).
    selfheal: dict = {
        "status": "skipped", "added": [], "removed": [],
        "manifest": None, "upstream_manifest": None,
        "upstream_manifests": [], "manifest_text": None,
        "merge_conflicts": [],
    }
    skew_manifest = None
    if not args.target:
        selfheal = resolve_manifest_selfheal(effective_dest, source_root)
        _print_manifest_merge_conflicts(selfheal)
        if selfheal["manifest"] is not None:
            manifest = selfheal["manifest"]
        elif selfheal["status"] == "legacy_preserved":
            # exact-match가 아닌 legacy는 로컬 manifest 자체도 불가침이다. bare self-prop를 plan에
            # 남기면 source root manifest가 파일을 통째로 덮어 커스텀 행을 제거하므로, 이번 plan에서
            # self-prop만 제외한다. 나머지 로컬 엔트리는 그대로 갱신하고 skew 대조에는 원문 전체를 쓴다.
            skew_manifest = manifest
            manifest = [
                entry for entry in manifest
                if str(entry).replace("\\", "/") != _MANIFEST_SELF_REL
            ]

    # add-harness guest 절 항목은 **update plan 에서 제외** — guest = add-harness refresh 전용 채널·
    # update 불가침. `@target-owned` skip 은 *source-부재* 때만 발동해, 프레임워크
    # root 에 source 가 실재하는 claude-guest(`.claude/agents`·`.claude/skills`)는 self-update plan 이
    # 그냥 갱신해 채택자의 guest 로컬 수정을 덮었다. guest 절은 apply 가 재부착()하므로 파일엔
    # 남고, plan 에서만 뺀다. 마커/절 추출은 pm_update 재사용(사본 0·guest 정의 = 마커 구획).
    dest_manifest_file = Path(effective_dest) / ".project_manager" / "engine.manifest"
    if dest_manifest_file.is_file():
        gblock = _extract_guest_manifest_block(
            dest_manifest_file.read_text(encoding="utf-8"))
        if gblock:
            gpaths = {
                ln.split()[0] for ln in gblock.splitlines()
                if ln.strip() and not ln.strip().startswith("#")}
            # **승격분 제외**: guest 경로가 upstream core 로
            # 승격되면(selfheal 이 그 경로를 담은 upstream 을 계획 기준으로 올림) 이제 core 라 **1차
            # sync 에서 갱신돼야** 한다 — dest guest 절에 있어도 **upstream core 에 실재하면 필터 밖**
            # (안 그러면 첫 실행이 그 파일을 안 갱신·2회 필요). upstream core = selfheal 이 해소한 flavor
            # manifest 경로(사본 0·같은 대조 기준). --target 은 selfheal 미실행이나 guest 절도 없어 무해.
            upstream_core_paths = _selected_upstream_core_paths(selfheal)
            if upstream_core_paths:
                gpaths -= upstream_core_paths
            manifest = [e for e in manifest if str(e).replace("\\", "/") not in gpaths]

    # --target(루트→templates/<name>) 은 render 를 끈다 — 템플릿은 토큰-form 소스라 copy2 로
    # 토큰을 보존해야 한다(렌더 시 local.conf 부재 → operational leak·_assert_no_leak crash).
    # render 는 채택자 self-update(--target 없음·local.conf 보유)와 pm_import 경로에서만.
    render_enabled = not args.target
    changes, missing = plan(
        source_root,
        manifest,
        dest_root=dest_root,
        render_enabled=render_enabled,
        manifest_source_text=(
            selfheal.get("manifest_text")
            if len(selfheal.get("upstream_manifests", [])) > 1
            else None
        ),
    )

    for r, _sp, _dst, kind in changes:
        # render path 는 byte-copy 가 아니라 재렌더 산출물 — PM 이 구분하게 [render] 로 표기
        # ([update] = byte-copy· dry-run 표기). new 든 update 든 render 면 [render].
        label = "render" if getattr(_dst, "render", False) else kind
        print(f"  [{label}] {r}")

    # ── source 부재 항목 처리 (@target-owned skip · 양 모드 공통) ──
    # manifest 의 일부는 *target-owned 어댑터* 일 수 있다 — 엔진 upstream(루트)엔 source 가
    # 없고 타깃 자신만 보유하는 경로(예: opencode `.opencode/*`). 그런 항목은 upstream→dest
    # 전파 대상이 *아니므로* rc2 로 전체를 막는 대신 graceful skip + 안내 로그로 surface 한다
    # (침묵 skip 금지).
    #
    # skip 은 **`@target-owned` 항목 한정**이다(명시 마커). 옛 구현은 `@render` 를
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

    # ── manifest skew 탐지 — upstream engine.manifest 와 로컬(sync 에 쓰인) manifest
    #    를 대조해 "로컬에 없는 upstream 신규 등재 경로"(신규 엔진 파일)를 찾는다. 로컬 manifest
    #    가 구형이면 신규 경로가 이번 sync 로 도달하지 않으므로, 아래 baseline 갱신을 억제해
    #    drift-lint 가 계속 skew 를 울리게 한다(false-최신 차단). --dry-run 도 동일 대조 결과 표시.
    #
    #    **self-update(채택자 흡수) 경로 한정** — `--target`(루트→templates/<name> 엔진 export)은
    #    타깃별 manifest(templates/*/engine.manifest)가 루트와 *의도적으로* 다르므로(어댑터
    #    비대칭·@target-owned 등) 대조하면 대량 오탐 + baseline 억제가 된다. --target 은 검출/억제
    #    를 비발화하고 현행 거동(무조건 baseline 갱신)을 유지한다(codex must-fix).
    #
    #    **flavor-correct 대조 기준 통일** (): skew 대조 upstream manifest 는 selfheal 이
    #    해소한 *동일* flavor-correct 경로(`selfheal["upstream_manifest"]`)를 넘긴다 — 안 그러면
    #    flavor 채택자(@source self-prop)가 치유 후에도 root-only 경로(`.claude/agents` 등)를 skew
    #    오탐해 baseline 이 억제된다(승격 기준 == 탐지 기준). 승격되면 manifest==flavor
    #    upstream 이라 skew 는 정의상 0.
    skew_status, skew_new = (
        ("skipped", [])
        if args.target
        else detect_manifest_skew(
            skew_manifest if skew_manifest is not None else manifest,
            source_root,
            upstream_manifest=selfheal["upstream_manifest"],
            upstream_manifests=selfheal.get("upstream_manifests"),
        )
    )

    # ── 진입 doc 세대 마이그레이션 — self-update 흡수 경로 한정 ──
    #    --target(엔진 export)은 비발화(skew/selfheal 동일 경계). 구형 미수정 opencode AGENTS.md
    #    를 신형 공통 코어로 자동 전환(+백업·jsonc idempotent), 수정 흔적 있으면 무손·loud 안내.
    #    AGENTS.md·opencode.jsonc 는 instance-owned(manifest 밖)이라 changes 유무와 독립.
    #
    #    ⚠ 시퀀싱 (비파괴 보장): 실제 전환 write 는 **apply(changes) 성공 이후**에만 한다.
    #    apply 가 render/IO 로 중단되면 신규 등재분(예 `.opencode/pm-instructions.md`)이 lay down
    #    되지 않는데, 그 전에 AGENTS.md 를 신형(위임 공백 공통 코어)으로 갈고 jsonc 가 미-laydown
    #    파일을 참조하면 채택자가 반쪽 상태(위임 방법론 공백)에 갇힌다 — 구형은 인라인 자족이라
    #    전환 전이 더 안전한 역설. 따라서 apply 실패 시 채택자가 *완전한 구형*에 남도록, has-changes
    #    경로는 apply 뒤에서만 전환한다. changes 없음(=엔진 최신·신규 등재분도 이미 laydown)·dry-run
    #    (무write)은 apply 가 없으므로 각 경로에서 직접 처리한다. 각 경로 migrate 1회(write flag 만 상이).
    do_migrate = not args.target

    # ── 보호 훅 정합 확인 + drift 재설치 — migrate 와 **같은 경계·같은
    #    시퀀싱**(--target 비발화 · changes 0 경로에서도 write · dry-run 은 판정만). changes 로
    #    게이트하면 이 기능을 배달하는 sync(구 엔진이 실행)도, 그 다음 실행(changes 0)도 발화
    #    하지 않아 채택자가 가드를 못 받는다(격리 실측 RUN1/RUN2·모듈 주석). 반복 출력은
    #    `_protected_hook_in_sync` 정합 판정이 흡수한다(정합이면 무출력).
    do_reinstall = not args.target

    if not changes:
        print("최신 — 변경 없음.")
        _print_manifest_selfheal_finding(selfheal, dry_run=args.dry_run)
        _print_manifest_skew_finding(skew_status, skew_new, dry_run=args.dry_run)
        if do_migrate:
            # 엔진 변경 0 = 이미 최신(신규 등재분도 laydown 완료) → 전환 write 안전(apply 무관).
            result = migrate_entry_doc(
                effective_dest, source_root, write=not args.dry_run)
            _print_entry_doc_migration_finding(result, dry_run=args.dry_run)
        if do_reinstall:
            # **업그레이드 배달 다음 실행이 여기로 온다**(dest 는 신 엔진·changes 0) — 훅이
            # 실제로 깔리는 지점이므로 migrate 와 동형으로 write 한다(정합이면 무출력).
            hooks = reinstall_protected_hooks(
                effective_dest, write=not args.dry_run)
            _print_protected_hook_reinstall_finding(hooks, dry_run=args.dry_run)
        # RUN2 수렴 지점: 엔진을 배달한 RUN1은 구 pm_update로 실행될 수 있으므로, 새 엔진의
        # 변경 0 재실행에서도 경로 upstream의 baseline/seen 쌍을 확인한다. dry-run은 기존
        # 계약대로 local.conf를 절대 쓰지 않는다. manifest skew면 has-changes 경로와 동형으로
        # 두 키를 함께 억제해 반쪽 상태/거짓 drift를 만들지 않는다.
        if not args.dry_run:
            converge_upstream_revs(effective_dest, source_root, skew_status, skew_new)
            maybe_prompt_delegate_optin(effective_dest)  # 변경 0 경로에서도 opt-in/안내
        return 0
    if args.dry_run:
        print(f"[dry-run] {len(changes)} 파일 변경 예정 (적용 안 함).")
        _print_manifest_selfheal_finding(selfheal, dry_run=True)
        _print_manifest_skew_finding(skew_status, skew_new, dry_run=True)
        if do_migrate:  # 판정만(write=False·무부작용).
            result = migrate_entry_doc(effective_dest, source_root, write=False)
            _print_entry_doc_migration_finding(result, dry_run=True)
        if do_reinstall:  # 대상 해소만(write=False·무부작용).
            hooks = reinstall_protected_hooks(effective_dest, write=False)
            _print_protected_hook_reinstall_finding(hooks, dry_run=True)
        return 0

    apply(changes)  # ← 실패 시 예외 전파 → 아래 전환 미도달(채택자 완전한 구형 유지).
    msg = f"✓ {len(changes)} 파일 동기화"
    print(msg)

    _print_manifest_selfheal_finding(selfheal, dry_run=False)
    _print_manifest_skew_finding(skew_status, skew_new, dry_run=False)
    if do_migrate:
        # 전환 write 는 apply(changes) 성공 이후 — 반쪽 상태 방지().
        result = migrate_entry_doc(effective_dest, source_root, write=True)
        _print_entry_doc_migration_finding(result, dry_run=False)

    if do_reinstall:
        # apply 이후 — 방금 착지한 *새* 엔진 사본에서 훅 본문을 읽어 배포한다. 단
        # 이 경로만으로는 부족하다(배달 sync 는 구 엔진이 실행) — 위 changes 0 경로가 짝이다.
        hooks = reinstall_protected_hooks(effective_dest, write=True)
        _print_protected_hook_reinstall_finding(hooks, dry_run=False)

    # upstream_rev baseline 갱신 — 매 sync 마다 source(upstream) HEAD 를
    # local.conf 에 박아 drift-lint의 "마지막 동기 이후" 기준점을 최신화한다. 경로
    # upstream 이면 `upstream_seen_rev`(현재 관찰값)도 같은 rev 로 함께 기록한다(
    # 경로는 동기 시점 checkout rev 가 곧 관찰값·두 키가 어긋난 채 남으면 상시 거짓 drift). 단
    # **manifest skew**(로컬 manifest 구형·신규 등재분 미도달)면 갱신을 억제한다 —
    # baseline 을 최신으로 박으면 drift-lint 가 "최신"으로 침묵해 신규 엔진 파일 누락을 은폐한다
    # (회사 채택자 실측). skew 아님(정합·또는 upstream manifest 부재 fail-soft)이면 현행대로 갱신.
    # source 가 로컬 git checkout 일 때만(URL upstream 은 로컬 checkout 없어 graceful 생략).
    # best-effort — 기록 실패가 동기화 자체를 무효화하지 않는다(파일은 이미 적용됨). --target
    # 모드는 effective_dest(templates/<name>)의 conf 에 기록(루트 오염 방지·maybe_prompt 와 동형).
    converge_upstream_revs(effective_dest, source_root, skew_status, skew_new)

    maybe_prompt_external_review(effective_dest)
    maybe_prompt_delegate_optin(effective_dest)  # 동기 후 delegate opt-in(TTY 질문·비TTY 안내)
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI 경계: repo 출하 seam의 분류 오류만 짧은 실행 오류로 바꾼다.

    apply/render/IO 등 다른 예외는 프로그래밍·시퀀싱 오류이므로 기존처럼 호출자에게 전파한다.
    """
    _console_spec = importlib.util.spec_from_file_location(
        "_console_encoding", Path(__file__).resolve().with_name("console_encoding.py")
    )
    _console_encoding = importlib.util.module_from_spec(_console_spec)
    _console_spec.loader.exec_module(_console_encoding)
    _console_encoding.configure_console_utf8()
    repo_files = _load_repo_owned_files()
    try:
        return _main(argv)
    except EmptyShippingInventoryError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1
    except repo_files.RepoFilesGitError as exc:
        print(
            "오류: source 출하 파일의 git 추적정보를 열거하지 못함 — "
            f"{exc}; 해당 checkout 경로와 git index 상태를 확인·복구한 뒤 다시 실행하라.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
