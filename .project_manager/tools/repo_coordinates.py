#!/usr/bin/env python3
"""PM-home ticket 경로를 소유 repo 상대 좌표로 정규화한다.

Ticket ``touches`` 는 PM 홈 기준이라 ``work/<repo>_<N>/path`` 형태일 수 있다. 코드
소비자는 소유 worktree 안에서 ``path`` 를 써야 하므로, 검증된 슬롯 접두만 제거한다.

검증 소스는 두 가지다.

* ``workspace`` 가 주어지면 그 실재 디렉토리가 정확히 ``pm_root/work/<repo>_<N>`` 인지 본다.
  ``ticket_finish --task`` 처럼 호출부가 이미 lease 소유검사를 마친 경로가 이 seam 을 쓴다.
* ``workspace`` 가 없으면 PM 홈의 lease 장부에서 같은 지속 slot↔repo 매핑을 찾고, 실제
  디렉토리도 확인한다. lease의 활동 상태는 완료 직후 ``idle``일 수 있으므로 보지 않는다.
  ``domain affected --ticket`` 같이 task 인자를 받지 않는 조회 표면이 이 경로다.

``work/<repo>_<N>/`` 접두가 없으면 문자열을 그대로 돌려준다. 접두가 있는데 검증된 슬롯과
다르면 조용히 strip하지 않고 ``RepoCoordinateError`` 를 낸다. 잘못된 stage 귀속이 recall
누락보다 위험하므로 fail-loud가 이 모듈의 핵심 불변식이다.

좌표계 surface grep 감사 메모:
``rg -n 'def (_ticket_touches|_scope_args)|pages_for_touches\\(|get_ticket_touches\\(|repo / touch|\
paths = touches|_freshness_owner_repo|--ticket|--paths' .project_manager/tools/{board.py,domain.py,\
ticket_finish.py,additional_reviewer.py}`` 로 ticket 경로 생산·소비와 scoped regression 지점을 함께
확인했다. 정확 좌표가 필요한 활성 소비는 domain의 affected/capture·ticket_finish의 완료
domain 알림/task stage이며 모두 이 normalizer를 지난다. board scoped regression은
``_ticket_touches``→``_scope_args``에서 ``Path(t).stem``만 써 좌표 무관, freshness는
owner-repo clock을 쓴다. additional_reviewer는 **canonical worktree에서 ``--ticket``은
접두 경로→빈 diff 차단·엔진 티켓 codex 게이트는 ``--paths`` 필수**다.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path


_WORKTREE_PREFIX = re.compile(
    r"^(?P<slot>work/(?P<repo>[^/]+)_(?P<number>\d+))(?:/(?P<relative>.*))?$"
)

# baked stamp. 소비처는 이 값을 자기 rev와 대조해 부분 동기된 구 사본을
# RepoCoordinateError 속성 접근 전에 명시적인 sibling-skew 오류로 막는다.
ENGINE_REV = "v1.7.13"


class RepoCoordinateError(ValueError):
    """worktree 접두를 검증된 workspace/lease에 귀속할 수 없는 오류."""


class NormalizedRepoPath(str):
    """repo-relative 문자열이면서 원래 worktree의 소유 repo/channel을 보존하는 좌표.

    ``str`` 서브클래스라 기존 ``Path / touch``·문자열 비교 소비자는 그대로 동작한다.
    worktree 접두는 PM topology상 canonical code checkout이므로 owner channel은
    ``upstream``이다. ``repo``는 lease/slot의 논리 repo 이름, ``workspace``는 검증한
    checkout의 절대 실경로다. domain 매칭은 페이지 ``repo:`` 채널이 해소한 checkout과
    ``workspace``의 git common-dir 저장소 정체성을 대조해, 같은 저장소의 다른 정상 슬롯은
    허용하고 같은 채널에 같은 상대경로가 있는 다른 repo는 구분한다.
    """

    repo: str
    owner: str
    workspace: Path

    def __new__(
            cls,
            relative: str,
            *,
            repo: str,
            workspace: Path | str,
            owner: str = "upstream",
    ):
        obj = super().__new__(cls, relative)
        obj.repo = repo
        obj.owner = owner
        obj.workspace = Path(workspace).resolve()
        return obj


def canonicalize_path_notation(path: str) -> str:
    """접두 판정 전 표기 변형을 POSIX형 한 좌표로 모은다."""
    norm = path.replace(os.sep, "/").replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    return norm


def has_worktree_prefix(path: str) -> bool:
    """표기 정규화 뒤 ``work/<repo>_<N>`` slot 접두가 있는지 판정한다."""
    return _WORKTREE_PREFIX.match(canonicalize_path_notation(path)) is not None


_WINDOWS_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")


def _normalize_relative(relative: str, original: str) -> str:
    """slot 접두 뒤 상대부를 안전한 POSIX repo-relative 경로로 정규화한다.

    중복 ``/``와 ``.`` 세그먼트는 접고, 그 결과가 repo 루트이거나 상대 좌표가 아닌
    절대/drive/UNC 표기면 slot 전체 선언과 같은 위험으로 fail-loud 한다. ``..``는 접어서
    다른 위치로 재해석하지 않고 원문 세그먼트가 하나라도 있으면 traversal로 거부한다.
    """
    segments = relative.split("/")
    if ".." in segments:
        raise RepoCoordinateError(
            f"worktree touches 경로 traversal은 허용하지 않는다: {original!r}"
        )
    if relative.startswith("//"):
        raise RepoCoordinateError(
            f"worktree slot 상대부에 UNC 경로를 선언할 수 없다: {original!r}"
        )
    if relative.startswith("/"):
        raise RepoCoordinateError(
            f"worktree slot 상대부에 절대경로를 선언할 수 없다: {original!r}"
        )

    normalized = "/".join(segment for segment in segments if segment not in ("", "."))
    if not normalized:
        raise RepoCoordinateError(
            f"worktree slot 전체를 touches로 선언할 수 없다"
            f"(소유 repo 상대 경로가 비어 있음): {original!r}"
        )
    if _WINDOWS_DRIVE_PREFIX.match(normalized):
        raise RepoCoordinateError(
            f"worktree slot 상대부에 drive 경로를 선언할 수 없다: {original!r}"
        )
    return normalized


def _workspace_slot(workspace: Path | str, pm_root: Path) -> tuple[str, Path]:
    """workspace를 PM 홈 상대 slot 문자열로 바꾼다. PM 홈 밖/부재면 fail-loud.

    slot 도출과 실재 검사는 **다른 경로**를 본다:

      - slot 도출 — resolve 경로가 PM 홈 하위면 그것으로, 아니면 **논리 경로**(symlink 를 따라가지
        않은 절대 표기)가 PM 홈 하위일 때만 그것으로. 슬롯 worktree 가 심링크거나 다른 마운트에
        있으면 실체는 PM 홈 밖인데, 그건 좌표 선언이 틀렸다는 뜻이 아니라 슬롯이 다른 곳에
        실재한다는 뜻이다. 논리 경로마저 PM 홈 밖이면 종전대로 fail-loud.
      - 실재 검사와 반환하는 실행 경로 — **resolve 된 경로**. dangling 심링크는 여기서 걸리고,
        호출부(측정·stage)는 파일이 실제로 있는 트리를 받는다.
    """
    root = Path(pm_root).resolve()
    logical_root = Path(os.path.abspath(pm_root))
    candidate = Path(workspace)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    logical = Path(os.path.abspath(candidate))
    try:
        slot = resolved.relative_to(root).as_posix()
    except ValueError as exc:
        try:
            slot = logical.relative_to(logical_root).as_posix()
        except ValueError:
            raise RepoCoordinateError(
                f"workspace가 PM 홈 밖이다: workspace={resolved}, pm_root={root}"
            ) from exc
    if not resolved.is_dir():
        raise RepoCoordinateError(f"workspace 디렉토리가 실재하지 않는다: {resolved}")
    return slot, resolved


# ── 엔진 중앙 로더 부트스트랩 (형제 로드는 이 한 경로만·`repo_owned_files.load_module`) ──
# 공유 읽기 seam 을 지연 로드하기 위해 필요하다([[T-0729]]) — 엔진 전체가 `spec_from_file_location`
# 을 중앙 로더 한 곳에서만 부르는 불변식(deep-import 가드)이라 여기서도 그 경로를 쓴다.
_TOOLS_BOOTSTRAP = os.path.dirname(os.path.abspath(__file__))
_TOOLS_BOOTSTRAP_FILE = os.path.realpath(
    os.path.join(_TOOLS_BOOTSTRAP, "repo_owned_files.py")
)
_TOOLS_BOOTSTRAP_KEY = f"_project_manager_repo_owned_files_bootstrap:{_TOOLS_BOOTSTRAP_FILE}"
_TOOLS_BOOTSTRAP_MODULE = sys.modules.get(_TOOLS_BOOTSTRAP_KEY)
_TOOLS_BOOTSTRAP_SENTINEL = object()
try:
    if (
        _TOOLS_BOOTSTRAP_MODULE is not None
        and os.path.realpath(getattr(_TOOLS_BOOTSTRAP_MODULE, "__file__", ""))
        != _TOOLS_BOOTSTRAP_FILE
    ):
        sys.modules.pop(_TOOLS_BOOTSTRAP_KEY, None)
        _TOOLS_BOOTSTRAP_MODULE = None
    if _TOOLS_BOOTSTRAP_MODULE is None:
        _TOOLS_BOOTSTRAP_PREVIOUS = sys.modules.pop(
            "repo_owned_files", _TOOLS_BOOTSTRAP_SENTINEL
        )
        _TOOLS_BOOTSTRAP_ADDED = not sys.path or sys.path[0] != _TOOLS_BOOTSTRAP
        if _TOOLS_BOOTSTRAP_ADDED:
            sys.path.insert(0, _TOOLS_BOOTSTRAP)
        try:
            import repo_owned_files as _TOOLS_BOOTSTRAP_MODULE
            if (
                os.path.realpath(getattr(_TOOLS_BOOTSTRAP_MODULE, "__file__", ""))
                != _TOOLS_BOOTSTRAP_FILE
            ):
                raise ImportError(
                    "repo_owned_files 형제 경로 불일치: "
                    f"{getattr(_TOOLS_BOOTSTRAP_MODULE, '__file__', None)!r}"
                )
            sys.modules[_TOOLS_BOOTSTRAP_KEY] = _TOOLS_BOOTSTRAP_MODULE
        finally:
            # 엔진 import bootstrap은 메인 스레드 전용이다. 그래도 위치를 가정한 pop(0)은
            # 피하고, 우리가 넣은 값이 남아 있을 때 그 값만 제거한다.
            if _TOOLS_BOOTSTRAP_ADDED:
                try:
                    sys.path.remove(_TOOLS_BOOTSTRAP)
                except ValueError:
                    pass
            if sys.modules.get("repo_owned_files") is _TOOLS_BOOTSTRAP_MODULE:
                sys.modules.pop("repo_owned_files", None)
            if _TOOLS_BOOTSTRAP_PREVIOUS is not _TOOLS_BOOTSTRAP_SENTINEL:
                sys.modules["repo_owned_files"] = _TOOLS_BOOTSTRAP_PREVIOUS
    _load_module_from_path = _TOOLS_BOOTSTRAP_MODULE.load_module
except Exception as _TOOLS_BOOTSTRAP_ERROR:
    if sys.modules.get(_TOOLS_BOOTSTRAP_KEY) is _TOOLS_BOOTSTRAP_MODULE:
        sys.modules.pop(_TOOLS_BOOTSTRAP_KEY, None)

    def _load_module_from_path(
        path,
        expected_filename,
        *,
        verifier=None,
        allow_unverified=False,
        cache=False,
        cache_key=None,
    ):
        """구형/손상 중앙 seam에서 복구 명령까지 띄우는 import-by-name 폴백."""
        target = os.path.realpath(os.fspath(path))
        if os.path.basename(target) != expected_filename:
            raise ValueError(
                f"module filename mismatch: expected {expected_filename!r}, "
                f"got {os.path.basename(target)!r}"
            )
        if verifier is not None and allow_unverified:
            raise ValueError("choose verifier or allow_unverified=True, not both")
        if verifier is None and not allow_unverified:
            raise ValueError(
                "module load requires verifier or explicit allow_unverified=True"
            )
        module_key = cache_key or f"_project_manager_legacy_loaded:{target}"
        module = sys.modules.get(module_key) if cache else None
        inserted = False
        try:
            if module is None:
                if (
                    target == _TOOLS_BOOTSTRAP_FILE
                    and _TOOLS_BOOTSTRAP_MODULE is not None
                ):
                    module = _TOOLS_BOOTSTRAP_MODULE
                else:
                    import_name = os.path.splitext(expected_filename)[0]
                    previous = sys.modules.pop(
                        import_name, _TOOLS_BOOTSTRAP_SENTINEL
                    )
                    parent = os.path.dirname(target)
                    # 런타임에 만든 형제 모듈(중앙 로더 선복구가 방금 복사한 seam 등)을
                    # 이름으로 import 한다 — FileFinder 는 디렉터리 목록을 mtime 으로 캐시하고
                    # 인터프리터 시작 뒤 생긴 파일은 invalidate 없이는 인식이 보장되지 않는다
                    # (Python 문서 `importlib.invalidate_caches` · Windows 실측 간헐
                    # ModuleNotFoundError). 블록은 stdlib-only 라 지역 import 로 두되 sys.path 에
                    # parent 를 넣기 전에 가져와 그 트리의 동명 파일이 stdlib 를 가리지 않게 한다.
                    import importlib as _bootstrap_importlib
                    added = not sys.path or sys.path[0] != parent
                    if added:
                        sys.path.insert(0, parent)
                    try:
                        _bootstrap_importlib.invalidate_caches()
                        module = __import__(import_name)
                        if os.path.realpath(getattr(module, "__file__", "")) != target:
                            raise ImportError(
                                f"{expected_filename} 형제 경로 불일치"
                            )
                    finally:
                        if added:
                            try:
                                sys.path.remove(parent)
                            except ValueError:
                                pass
                        if sys.modules.get(import_name) is module:
                            sys.modules.pop(import_name, None)
                        if previous is not _TOOLS_BOOTSTRAP_SENTINEL:
                            sys.modules[import_name] = previous
                if cache:
                    sys.modules[module_key] = module
                    inserted = True
            if verifier is not None:
                verifier(module, expected_filename)
            return module
        except Exception as exc:
            if cache and (inserted or sys.modules.get(module_key) is module):
                sys.modules.pop(module_key, None)
            if target == _TOOLS_BOOTSTRAP_FILE:
                raise RuntimeError(
                    f"엔진 공용 로더 {target}를 불러올 수 없음; "
                    "pm-update로 .project_manager/tools 전체를 재동기화하라."
                ) from exc
            raise

# REPO = 스크립트 위치 기반(cwd 무관) — board.py·worktree_pool.py·pm_config.py 와 동일
# 앵커 관례(어느 worktree cwd 에서 호출돼도 multi-PM 루트 .project_manager 를 자동 타깃).


# ── 공유 읽기 (원자 교체 대상 장부·[[T-0729]]) ────────────────────────────
# 이 모듈이 읽는 리스 장부(`worktree-leases.json`)는 `worktree_pool` 이 **원자 교체**하는
# 파일이다. 일반 `open` 리더가 하나라도 잡고 있으면 Windows 는 그 교체를 WinError 32 로 막으므로,
# 판독도 공용 seam 의 공유 읽기를 지난다.
#
# seam 로드는 **호출 시점 지연**이다 — board·domain 이 이 모듈을 *import 시점*에 로드하므로
# 모듈 최상단에서 형제를 끌어오면 그 import 경계가 형제 하나만큼 넓어진다. seam 을 못 쓰는
# 형상은 **종전 읽기로 강등**한다(등재 예외 · 조용하지 않게 프로세스당 한 번 사유를 남긴다) —
# 여기서 올리면 실재하는 장부를 "읽을 수 없다" 로 위장해 좌표 해소 전체가 막힌다.

_shared_read_degraded = False


def _warn_shared_read_degraded(cause: str) -> None:
    """강등 사유를 **프로세스당 한 번** 알린다 (판독마다 찍으면 진단이 자기 소음에 묻힌다)."""
    global _shared_read_degraded
    if _shared_read_degraded:
        return
    _shared_read_degraded = True
    print(
        f"경고: 공유 읽기 seam 을 쓸 수 없어 일반 읽기로 진행합니다 ({cause}) — Windows "
        "에서는 이 판독이 열려 있는 동안 리스 장부의 원자 교체가 실패할 수 있습니다. "
        "`pm-update` 로 .project_manager/tools/ 전체를 재동기하십시오.",
        file=sys.stderr,
    )


def _read_text_shared(path: Path, *, encoding: str) -> str:
    """리스 장부를 공유 읽기로 읽는다 — seam 을 못 쓰면 종전 읽기로 강등한다.

    이 모듈은 rev 검증자를 두지 않는다(다른 도구가 import 시점에 로드하는 leaf 좌표 모듈이라
    형제 판정 계층을 갖지 않는 것이 설계다 — 그래서 중앙 로더에 `allow_unverified=True` 로
    묻는다). 구세대 사본은 함수 부재로 드러나므로 `getattr` 로 함께 받는다 — 쓰기 축 등재 예외와
    같은 규칙이다.
    """
    api = None
    try:
        module = _load_module_from_path(
            Path(__file__).resolve().with_name("file_lock.py"), "file_lock.py",
            allow_unverified=True, cache=True,
        )
        api = getattr(module, "read_text_shared", None)
        if api is None:
            _warn_shared_read_degraded("구세대 file_lock 사본에 read_text_shared 가 없음")
    except Exception as exc:  # noqa: BLE001 — 부재/손상 사본은 이 point-read 의 정상 입력이다.
        _warn_shared_read_degraded(f"{type(exc).__name__}: {exc}")
    if api is not None:
        return api(path, encoding=encoding)
    return path.read_text(encoding=encoding)


def _registered_lease_slot(slot: str, repo: str, leases_file: Path) -> bool:
    """장부에서 state와 무관한 지속 slot↔repo 소유 매핑이 정확히 존재하는지 조회한다."""
    try:
        data = json.loads(_read_text_shared(leases_file, encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RepoCoordinateError(f"worktree lease 장부가 없다: {leases_file}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise RepoCoordinateError(f"worktree lease 장부를 읽을 수 없다: {leases_file}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("leases", []), list):
        raise RepoCoordinateError(f"worktree lease 장부 형식이 잘못됐다: {leases_file}")

    matches = [
        row for row in data.get("leases", [])
        if isinstance(row, dict)
        and row.get("slot") == slot
    ]
    if len(matches) != 1:
        return False
    recorded_repo = matches[0].get("repo")
    return recorded_repo == repo


def normalize_repo_path(
    path: str,
    *,
    pm_root: Path,
    workspace: Path | str | None = None,
    leases_file: Path | None = None,
) -> str:
    """한 ticket 경로를 소유 repo 상대 좌표로 정규화한다.

    비-worktree 경로는 byte-for-byte 통과한다. ``work/<repo>_<N>/`` 접두 경로는
    ``workspace`` 또는 지속 lease 매핑 + 실재 디렉토리로 slot을 검증한 뒤에만 접두를
    제거하고, 반환 문자열에 소유 ``repo``/``owner`` 메타데이터를 보존한다.
    """
    canonical = canonicalize_path_notation(path)
    match = _WORKTREE_PREFIX.match(canonical)
    if match is None:
        return path

    slot = match.group("slot")
    repo = match.group("repo")
    number = match.group("number")
    relative = match.group("relative") or ""
    if not number.isdigit() or int(number) < 1:
        raise RepoCoordinateError(f"유효하지 않은 worktree slot 접두: {slot!r}")
    relative = _normalize_relative(relative, path)

    root = Path(pm_root).resolve()
    if workspace is not None:
        expected_slot, candidate = _workspace_slot(workspace, root)
        if expected_slot != slot:
            raise RepoCoordinateError(
                f"touches slot 불일치: 선언={slot!r}, 검증 workspace={expected_slot!r}"
            )
        return NormalizedRepoPath(relative, repo=repo, workspace=candidate)

    ledger = Path(leases_file) if leases_file is not None else (
        root / ".project_manager" / ".local" / "worktree-leases.json"
    )
    if not _registered_lease_slot(slot, repo, ledger):
        raise RepoCoordinateError(
            f"touches slot 불일치: 선언={slot!r}, lease 장부의 지속 slot↔repo 매핑에 "
            f"일치 항목 없음 ({ledger})"
        )
    slot_path = root / slot
    if not slot_path.is_dir():
        raise RepoCoordinateError(
            f"touches slot lease는 있으나 workspace 디렉토리가 없다: {slot_path}"
        )
    return NormalizedRepoPath(relative, repo=repo, workspace=slot_path)


def normalize_repo_paths(
    paths: list[str],
    *,
    pm_root: Path,
    workspace: Path | str | None = None,
    leases_file: Path | None = None,
) -> list[str]:
    """경로 목록에 ``normalize_repo_path``의 단일 불변식을 순서대로 적용한다."""
    return [
        normalize_repo_path(
            path,
            pm_root=pm_root,
            workspace=workspace,
            leases_file=leases_file,
        )
        for path in paths
    ]
