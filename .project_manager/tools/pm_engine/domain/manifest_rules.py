"""engine.manifest 문법·태그·guest 절·경로 소유 판정 (층: domain).

갱신기(`pm_update.py`)와 설치기(`pm_import.py`)·사설 참조 판정이 같은 manifest 문법을 읽도록 한
곳에 둔다. 파일 IO 가 없다.
"""
from __future__ import annotations

from pathlib import Path


# manifest 의 render 태그 — path 행 끝 `  @render` 면 byte-copy 대신 render_adapter.
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
# 엔진 구현 패키지 디렉터리 행 — 평면 엔진 파일보다 먼저 전달한다(read_manifest 정렬). 갱신기는 이
# 디렉터리를 파일 단위로 덮지 않고 통째로 바꾼다(같은 부모의 완성 대기 트리 → 게시).
_ENGINE_PACKAGE_REL = ".project_manager/tools/pm_engine"
# 패키지 교체가 패키지 옆(`.project_manager/tools/` 바로 아래)에 남기는 운영 산출물의 예약 이름.
#   락 파일은 같은 대상의 교체 writer 를 직렬화하려고 지우지 않고 남는다. 완성 대기 트리(stage)와 기존
#   패키지 보존 트리(backup)는 정상 완료 때 정리되고, 실패하면 수동 복구 자료로 남는다. 어느 쪽도
#   새 채택자에게 출하하지 않는다(`_is_engine_transfer_artifact`).
_ENGINE_TRANSFER_LOCK_NAME = ".pm-engine-transfer.lock"
_ENGINE_TRANSFER_STAGE_PREFIX = ".pm-engine-stage-"
_ENGINE_TRANSFER_BACKUP_PREFIX = ".pm-engine-backup-"


def _is_engine_transfer_artifact(relative: str) -> bool:
    """repo 상대경로가 패키지 교체의 운영 산출물(락·stage·backup 과 그 하위)인가.

    부모가 정확히 `.project_manager/tools` 인 자리만 본다. 다른 부모의 같은 이름과, 접미사 없는
    예약 접두사 자체(`.pm-engine-stage-`)는 정상 경로로 둔다."""
    parts = relative.replace("\\", "/").strip("/").split("/")
    parent = _ENGINE_PACKAGE_REL.split("/")[:-1]
    if len(parts) <= len(parent) or parts[:len(parent)] != parent:
        return False
    name = parts[len(parent)]
    if name == _ENGINE_TRANSFER_LOCK_NAME:
        return len(parts) == len(parent) + 1
    return any(
        name.startswith(prefix) and len(name) > len(prefix)
        for prefix in (_ENGINE_TRANSFER_STAGE_PREFIX, _ENGINE_TRANSFER_BACKUP_PREFIX)
    )


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


def _parse_manifest_line(line: str) -> ManifestEntry | None:
    """manifest 한 줄 → ManifestEntry — 주석·빈 줄은 None.

    `read_manifest`(파일 전체)와 guest 절 파싱(`_dest_guest_manifest_entries`·텍스트 조각)이
    **같은 한 함수**를 쓴다. 마커 인식이 갈리면 guest 절의 `@render`/엔진 행 구분이 파일 파싱과
    어긋나 소유 채널 판정이 뒤집힌다(판정 사본 금지)."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
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
    if not parts:
        return None
    return ManifestEntry(" ".join(parts), render, target_owned, source_rel)


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


def _entry_render_flag(entry) -> bool:
    """manifest 항목의 render 플래그 — ManifestEntry 면 `.render`, 평문 str(레거시 호출)면 False.

    plan() 이 `list[str]`(기존 테스트·모듈 밖 호출부)과 `list[ManifestEntry]`(read_manifest) 둘 다
    받게 정규화한다 — 후방호환(평문 str 항목은 render 비대상).
    """
    return bool(getattr(entry, "render", False))


def _entry_target_owned_flag(entry) -> bool:
    """manifest 항목의 target_owned 플래그 — ManifestEntry 면 `.target_owned`, 평문 str 면 False.

    source-부재 skip 판별자. 평문 str 항목(레거시 호출)은 target-owned 가 아니므로
    source-부재 시 엔진 누락으로 보고 rc2(후방호환·is_owned skip 은 명시 마커 한정).
    """
    return bool(getattr(entry, "target_owned", False))


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
    # missing → 아무것도 yield 안 함 (호출부가 missing 으로 보고)


# ── board-분리 인지 dest 리매핑 ───────────────────────────
# manifest 는 ticket 본문 템플릿을 `wiki/tickets/_template.md` 로 들고 있다(canonical·
# legacy adopter 의 실 위치). 그러나 board(tickets+areas)가 `.project_manager/board/`
# (submodule)로 분리된 adopter(`pm_paths` board 루트)에선 `_template.md` 가
# `board/tickets/_template.md` 에 산다(board 루트 추종·B 마이그레이션이 거기로 옮김).
# manifest 항목은 legacy-correct 로 두고(자체 drift 회피), *동기 시 dest 경로만*
# board_root 로 해소한다 — board-분리 dest 면 board/tickets/_template.md 로, legacy dest 면
# 종전 wiki/tickets/_template.md 로(무변경). 이로써 board-분리 adopter 의 매 sync 가
# wiki/tickets/_template.md 를 부활시키지 않는다(drift-0·실 발생 버그 reconcile).
#
# `pm_paths._board_root_at` 의 *실측* 판별(`<dest>/.project_manager/board/tickets` 가 dir 인가)을
# 동형 복제한다 — pm_update 는 stdlib-only(board 미import 결합 회피·_resolve_dest_source 와
# 동형)이고, 판별은 단일 is_dir() probe 라 `pm_paths._board_root_at` 과 정확히 같다. 어떤 manifest
# 항목이 board-분리 시 board/ 로 옮겨가는지는 아래 `_TEMPLATE_REL`→`_BOARD_TEMPLATE_REL`
# 매핑(`_dest_relpath_for`)이 단일 진실.
_TEMPLATE_REL = ".project_manager/wiki/tickets/_template.md"
_BOARD_TEMPLATE_REL = ".project_manager/board/tickets/_template.md"


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


# ── add-harness guest @render 절 (engine.manifest self-prop 보존) ──────────────
# engine.manifest 는 self-prop `@source` 라 apply 가 upstream 사본으로 통째 덮어쓴다(guest 는 로컬-전용
# → selfheal 'diverged' 도 *파일* overwrite 는 못 막는다·plan 에 self-prop change 가 실린다·실측). 그래서
# add_harness 가 등재한 guest `@render` 가 1회 update 만에 사라져 렌더/overlay 스캔 커버리지가 끊기던 것을
# 이 마커 구획으로 닫는다: apply 가 engine.manifest 를 덮기 **전** dest 의 guest 절을
# 추출 → 덮은 **뒤** 재부착한다. 마커는 read_manifest 가 '#' 주석으로 무시하고, 절 안의 라인은
# `@render @target-owned` 유효 항목이라 파서/스캔/렌더가 그대로 소비한다(판정원 단일 = engine.manifest
# 최종 뷰 하나). 절의 *값* 은 app.add_harness 가 쓴다(같은 리터럴 공유·아래 상수 블록).
# pm:data-literal:begin
# 아래 네 상수는 **채택자 디스크에 기록된 데이터**다 — engine.manifest 안에 실제로 적힌 바이트이지
# 이 소스의 산문이 아니다. 따라서 스트립·문구 정리·리팩터 대상이 아니며 한 글자만 달라져도 이미
# 기록된 채택자 manifest 의 guest 절을 못 읽어 절이 조용히 사라진다. 리터럴을 바꿔야 하면 옛 값을
# 지우지 말고 `_LEGACY` 튜플에 남겨라 — **읽기는 세대 집합 전체를 받고, 쓰기는 항상 현행 하나만**
# 낸다(옛 세대는 `_migrate_legacy_guest_markers` 가 다음 sync 에서 1 회 치환한다).
_GUEST_MANIFEST_BEGIN = "# >>> pm add-harness guest @render (local·pm_update-preserved) >>>"
_GUEST_MANIFEST_END = "# <<< pm add-harness guest @render (local) <<<"
_GUEST_MANIFEST_BEGIN_LEGACY: tuple[str, ...] = (
    "# >>> pm add-harness guest @render (local·pm_update-preserved·T-0456) >>>",
)
# 종료 마커는 아직 세대가 하나뿐이라 비어 있다 — 다음 세대를 대비해 시작 마커와 대칭 구조로 둔다.
_GUEST_MANIFEST_END_LEGACY: tuple[str, ...] = ()
# pm:data-literal:end

# 읽기가 인식하는 세대 집합(현행 + 옛). 쓰기 경로는 이 튜플을 쓰지 않는다(현행 상수 단일).
_GUEST_MANIFEST_BEGINS = (_GUEST_MANIFEST_BEGIN,) + _GUEST_MANIFEST_BEGIN_LEGACY
_GUEST_MANIFEST_ENDS = (_GUEST_MANIFEST_END,) + _GUEST_MANIFEST_END_LEGACY


def _extract_guest_manifest_block(text: str) -> str | None:
    """engine.manifest 텍스트의 add-harness guest 절(마커 경계 포함)을 반환 — 없으면 None.

    마커 비교는 **세대 집합 멤버십**이다(현행 + 옛 리터럴) — 옛 세대로 기록된 채택자 manifest 도
    절을 인식해야 apply 재부착이 그것을 보존한다(단일 리터럴 비교였을 때 guest 절이 경고 없이
    사라졌다)."""
    lines = text.splitlines()
    begin = end = None
    for i, line in enumerate(lines):
        s = line.strip()
        if s in _GUEST_MANIFEST_BEGINS:
            begin = i
        elif s in _GUEST_MANIFEST_ENDS and begin is not None:
            end = i
            break
    if begin is None or end is None or end < begin:
        return None
    return "\n".join(lines[begin:end + 1])


def _strip_guest_manifest_block(text: str) -> str:
    """engine.manifest 텍스트에서 guest 절(마커 포함 + 선행 빈 줄)을 제거 — 마커 부재면 원문 그대로.

    마커 비교는 추출과 같은 세대 집합이다 — 읽기 판정이 한쪽만 관용이면 옛 세대 manifest 에서
    core 경로 집합(`_core_manifest_paths`)이 guest 라인을 core 로 세어 소유권 판정이 어긋난다."""
    lines = text.splitlines()
    begin = end = None
    for i, line in enumerate(lines):
        s = line.strip()
        if s in _GUEST_MANIFEST_BEGINS:
            begin = i
        elif s in _GUEST_MANIFEST_ENDS and begin is not None:
            end = i
            break
    if begin is None or end is None or end < begin:
        return text
    lo = begin
    while lo > 0 and lines[lo - 1].strip() == "":  # 절 앞 빈 줄 구분자까지 회수(누적 방지)
        lo -= 1
    kept = "\n".join(lines[:lo] + lines[end + 1:])
    return kept + "\n" if kept and not kept.endswith("\n") else kept


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


# 엔진 코드 접두사 — 이 아래 행이 전달 프로세스가 옮기는 엔진 코드다(`_is_engine_code`). 평면 진입과
# `pm_engine` 패키지가 한 세대로 맞물려 있으므로 세대 단위로만 옮긴다.
_ENGINE_TOOLS_PREFIX = ".project_manager/tools/"


def _paths_overlap(one: str, other: str) -> bool:
    """두 경로가 겹치는가 — 같거나 한쪽이 다른 쪽의 상위 디렉토리(양방향)."""
    return one == other or one.startswith(other + "/") or other.startswith(one + "/")


def _is_engine_code(rel) -> bool:
    """manifest 행(또는 change 의 dest 경로)이 엔진 코드인가 — `.project_manager/tools/` 아래."""
    return str(rel).replace("\\", "/").startswith(_ENGINE_TOOLS_PREFIX)
