#!/usr/bin/env python3
"""위임 전·후 worktree 상태를 비교해 ``touches`` 밖 변경을 표면화한다.

이 모듈은 판정만 소유한다. 위임 프로세스의 return code를 바꾸거나 변경을 복원하지 않는다.
호출부는 위임 직전에 :func:`capture_worktree_state`를 한 번, 결과 회수 직후에 한 번 호출한 뒤
:func:`out_of_scope_changes`와 :func:`format_warning`을 사용한다.

Ticket ``touches``는 PM-home 좌표일 수 있으므로 비교 전에 반드시 공용
``repo_coordinates.py`` normalizer를 거친다. 읽기 전용 역할과 빈 ``touches``는 허용 경로가
0개다. 항상-허용 예외는 두지 않는다. ``.project_manager/.local/`` 같은 런타임 상태는 gitignore가
이미 status 입력에서 제외하며, 여기서 다시 광역 예외를 만들면 실제 stray 산출물까지 숨길 수 있다.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import NamedTuple


# 쓰기 허용 역할 — **allow-list** 로 둔다(read-only 열거가 아니라). 새 역할이 생기면 자동으로
# 읽기 전용(허용 0) 쪽에 떨어져야 안전 방향이다. 런타임 단일 출처는 호출부(pm_delegate.WRITE_ROLES)
# 주입이고 이 값은 standalone 사용의 기본값이다 — 두 값의 동일성은 테스트가 지킨다.
WRITE_ROLES = frozenset({"developer", "architect"})
ALWAYS_ALLOWED_PATHS: tuple[str, ...] = ()

# git hash-object 한 번에 넘길 경로 수 — argv 길이 한계 회피(dirty 집합이 큰 트리 방어).
HASH_BATCH_SIZE = 100
# 이 크기를 넘는 파일은 해시 보강을 건너뛴다 — 대용량 산출물(빌드 아티팩트·모델 가중치 등)은
# 존재/상태코드로 이미 표면화되고, 그걸 읽는 비용이 판정의 최악 I/O 를 지배한다.
HASH_MAX_FILE_BYTES = 5 * 1024 * 1024

# ── 엔진 사본 rev 스탬프 (형제 사본 skew fail-loud) ──────────────────────
# baked 리터럴 — 이 값은 이 파일 코드 안에 고정된다(engine_rev.py 런타임 읽기 아님). 부분/수동
# 복사로 신 로더 + 구 형제가 섞이면 각자 새/옛 리터럴을 지녀 대조에서 skew 로 검출된다.
# 릴리즈 bump 는 `engine_rev.py --bump vX.Y.Z` 가 전 stamped 모듈 리터럴을 기계 일괄
# 재작성한다(사람 N곳 편집 0).
ENGINE_REV = "v1.5.0"


def _verify_engine_rev(sibling_module, sibling_filename):
    """로드한 형제의 baked ENGINE_REV 를 이 사본과 대조한다(skew만 fail-loud)."""
    got = getattr(sibling_module, "ENGINE_REV", None)
    if got != ENGINE_REV:
        err = RuntimeError(
            f"엔진 사본 버전 불일치 — 로더 {Path(__file__).name}(rev={ENGINE_REV!r})가 "
            f"형제 {sibling_filename}(rev={got!r})를 로드했다 (사본 skew: 부분/수동 복사 또는 "
            f"구형 사본). `pm-update`(또는 pm_update.py)로 .project_manager/tools/ 전체를 재동기하라."
        )
        err._engine_rev_skew = True
        raise err


def _is_engine_rev_skew(exc) -> bool:
    """예외가 rev-스탬프 skew 유래인지(fail-soft 로더의 재-raise 식별)."""
    return getattr(exc, "_engine_rev_skew", False)


def _ticket_touches_error(ticket_id: str, exc: Exception) -> DelegateScopeError:
    """ticket 조회의 로드/호출 실패를 같은 계약 오류 형상으로 감싼다."""
    return DelegateScopeError(f"ticket touches 읽기 실패({ticket_id}): {exc}")


# ticket id 에 허용하지 않는 문자 — glob 메타/경로 구분자(board 조회가 glob 이라 다른 ticket 을
# 오선택할 수 있다·정확 일치만 받는다).
_TICKET_ID_UNSAFE = re.compile(r"[*?\[\]/\\]")


class DelegateScopeError(RuntimeError):
    """git 상태 또는 repo 좌표를 신뢰성 있게 산출할 수 없는 오류."""


class StatusEntry(NamedTuple):
    code: str
    path: str


class WorktreeState(NamedTuple):
    entries: tuple[StatusEntry, ...]
    # dirty 경로의 blob 해시((path, sha) 쌍) — 상태코드가 같은 **재수정**(M→M)을 잡는 유일한 신호.
    # 해시 산출 실패는 이 목록에서 빠질 뿐이라 판정 전체를 잃지 않는다(상태코드 비교로 자연 강등).
    digests: tuple[tuple[str, str], ...] = ()
    # 경로별 (submodule 상태, worktree mode) 지문 — porcelain v2 필드. 내용이 같아도 실행권한
    # (chmod +x)·submodule 포인터가 바뀌면 상태코드/해시 둘 다 그대로라 이 신호만 남는다.
    modes: tuple[tuple[str, str], ...] = ()
    # 캡처 시점 HEAD sha — 위임 중 **커밋**이 나면 전후 worktree 가 둘 다 clean 이라 상태 비교로는
    # 완전 미탐이다(커밋은 역할 계약 위반이지만 가드는 그걸 믿지 않는다).
    head: str = ""


GitRunner = Callable[[Path, list[str]], tuple[int, str]]


def _default_git_runner(cwd: Path, args: list[str]) -> tuple[int, str]:
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(cwd),
    )
    return result.returncode, result.stdout


def parse_porcelain_z(output: str) -> tuple[StatusEntry, ...]:
    """``git status --porcelain=v1 -z``를 경로별 상태로 파싱한다.

    rename/copy는 ``<XY> <new> NUL <old> NUL`` 두 토큰이다. 두 토큰 소비(``index += 1``) 자체는
    **유지해야 한다** — 안 하면 old 경로가 상태코드 없는 유령 엔트리로 다시 파싱된다. 다만 소비한
    old 경로를 버리면 "범위 안으로 rename 해 나간 원본"이 판정에서 사라지므로(범위 밖 파일을
    범위 안으로 옮기면 흔적 0), from-path도 같은 코드로 함께 싣는다. copy(C)의 원본은 실제로는
    안 변하지만 보수 방향(경고)을 택한다 — status의 copy 검출은 기본 off라 실사용 노이즈가 없다.
    """
    tokens = output.split("\0")
    parsed: list[StatusEntry] = []
    index = 0
    while index < len(tokens):
        raw = tokens[index]
        index += 1
        if len(raw) < 4 or raw[2] != " ":
            continue
        code, path = raw[:2], raw[3:]
        from_path = ""
        if code[0] in ("R", "C") or code[1] in ("R", "C"):
            from_path = tokens[index] if index < len(tokens) else ""
            index += 1
        if path:
            parsed.append(StatusEntry(code, path))
        if from_path:
            parsed.append(StatusEntry(code, from_path))
    return tuple(parsed)


def resolve_workspace_root(
    workspace: Path | str,
    *,
    run_git: GitRunner | None = None,
) -> Path:
    """``--cwd``가 repo **하위 디렉토리**여도 판정 기준을 git toplevel로 맞춘다.

    status/해시 캡처와 touches 좌표 정규화가 서로 다른 루트를 쓰면 경로 비교가 어긋나 판정이
    통째로 꺼진다(하위 디렉토리 위임에서 실측된 사각). 해소 실패는 :class:`DelegateScopeError`로
    올려 호출부가 loud degrade 하게 한다.
    """
    cwd = Path(workspace).resolve()
    runner = run_git or _default_git_runner
    try:
        rc, output = runner(cwd, ["rev-parse", "--show-toplevel"])
    except Exception as exc:  # noqa: BLE001 — 호출부가 loud 경고로 바꿀 단일 오류 계약.
        raise DelegateScopeError(f"git toplevel 해소 실패: {exc}") from exc
    root = output.strip()
    if rc != 0 or not root:
        raise DelegateScopeError(f"git toplevel 해소 실패(rc={rc}): {cwd}")
    return Path(root).resolve()


def parse_porcelain_v2_modes(output: str) -> tuple[tuple[str, str], ...]:
    """``git status --porcelain=v2 -z``에서 경로별 ``<sub>:<mW>`` 지문을 뽑는다.

    ``1``/``2``/``u`` 레코드는 submodule 상태(``<sub>``)와 worktree mode(``<mW>``)를 싣는다 —
    chmod(+x)나 submodule 포인터 변경처럼 **내용은 그대로인 변경**은 상태코드도 blob 해시도 안
    바뀔 수 있어(이미 dirty 한 경로) 이 지문이 유일한 신호다. ``?``/``!``(untracked·ignored)은
    mode 정보가 없어 건너뛴다.
    """
    fingerprints: list[tuple[str, str]] = []
    tokens = output.split("\0")
    index = 0
    while index < len(tokens):
        raw = tokens[index]
        index += 1
        if not raw:
            continue
        kind = raw[0]
        if kind not in ("1", "2", "u"):
            continue
        fields = raw.split(" ")
        path_at = {"1": 8, "2": 9, "u": 10}[kind]
        mode_at = 5 if kind in ("1", "2") else 6   # u 레코드는 m1 m2 m3 뒤가 mW
        if len(fields) <= path_at:
            continue
        path = " ".join(fields[path_at:])
        if kind == "2":
            index += 1  # rename/copy 원본 경로 토큰 소비(-z 는 별도 NUL 토큰)
        if path:
            fingerprints.append((path, f"{fields[2]}:{fields[mode_at]}"))
    return tuple(fingerprints)


def _capture_modes(cwd: Path, runner: GitRunner) -> tuple[tuple[str, str], ...]:
    """porcelain v2 지문 캡처 — 실패하면 빈 값(상태코드/해시 비교로 강등)."""
    try:
        rc, output = runner(cwd, ["status", "--porcelain=v2", "-z", "--untracked-files=all"])
    except Exception:  # noqa: BLE001 — 보강 신호라 판정을 죽이지 않는다.
        return ()
    if rc != 0:
        return ()
    return parse_porcelain_v2_modes(output)


def _capture_head(cwd: Path, runner: GitRunner) -> str:
    """HEAD sha — 커밋 없는 repo/해소 실패는 빈 문자열(그 경우 커밋 판정은 비활성)."""
    try:
        rc, output = runner(cwd, ["rev-parse", "HEAD"])
    except Exception:  # noqa: BLE001
        return ""
    return output.strip() if rc == 0 else ""


def _hash_dirty_paths(
    cwd: Path,
    paths: Sequence[str],
    runner: GitRunner,
) -> tuple[tuple[str, str], ...]:
    """dirty 경로의 blob 해시를 산출한다(실재하는 파일만·배치 실패는 파일 단위로 재시도).

    해시는 상태코드 비교를 **보강**하는 신호라 산출 실패가 판정 전체를 죽이면 안 된다 — 못 구한
    경로는 목록에서 빠지고 그 경로는 상태코드만으로 비교된다. ``HASH_MAX_FILE_BYTES`` 초과 파일은
    건너뛴다(대용량 산출물은 상태코드로 이미 보인다).

    ``git hash-object``는 (``core.autocrlf``·clean 필터 등) **정규화를 거친** blob 을 내므로, 줄끝만
    바뀐 재수정은 설정에 따라 같은 해시로 보일 수 있다 — 그 경우 상태코드/mode 신호가 백스톱이다.
    """
    targets = [path for path in paths if _hashable(cwd / path)]
    digests: list[tuple[str, str]] = []
    for start in range(0, len(targets), HASH_BATCH_SIZE):
        batch = targets[start:start + HASH_BATCH_SIZE]
        hashed = _hash_object(cwd, batch, runner)
        if hashed is None:
            for single in batch:  # 배치 실패(권한·삭제 경합 등) → 나머지를 살린다.
                one = _hash_object(cwd, [single], runner)
                if one is not None:
                    digests.extend(one)
            continue
        digests.extend(hashed)
    return tuple(digests)


def _hashable(target: Path) -> bool:
    """해시 대상 판정 — 실재하는 파일이고 상한 이하 크기."""
    try:
        if not target.is_file():
            return False
        return target.stat().st_size <= HASH_MAX_FILE_BYTES
    except OSError:
        return False


def _hash_object(
    cwd: Path,
    batch: Sequence[str],
    runner: GitRunner,
) -> list[tuple[str, str]] | None:
    """``git hash-object -- <paths>`` 1회. 실패/출력 불일치는 None(호출부가 강등)."""
    if not batch:
        return []
    try:
        rc, output = runner(cwd, ["hash-object", "--", *batch])
    except Exception:  # noqa: BLE001 — 보강 신호 산출 실패는 판정을 죽이지 않는다.
        return None
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if rc != 0 or len(lines) != len(batch):
        return None
    return list(zip(batch, lines))


def capture_worktree_state(
    workspace: Path | str,
    *,
    run_git: GitRunner | None = None,
) -> WorktreeState:
    """untracked 파일까지 펼친 worktree 상태 + dirty 경로 내용 해시를 캡처한다."""
    cwd = Path(workspace).resolve()
    runner = run_git or _default_git_runner
    try:
        rc, output = runner(
            cwd,
            ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
        )
    except Exception as exc:  # noqa: BLE001 — 호출부가 loud 경고로 바꿀 단일 오류 계약.
        raise DelegateScopeError(f"git status 실행 실패: {exc}") from exc
    if rc != 0:
        raise DelegateScopeError(f"git status 실패(rc={rc}): {cwd}")
    entries = parse_porcelain_z(output)
    digests = _hash_dirty_paths(cwd, [entry.path for entry in entries], runner)
    return WorktreeState(entries, digests, _capture_modes(cwd, runner), _capture_head(cwd, runner))


def content_signal_missing(state: WorktreeState, workspace: Path | str) -> bool:
    """해시 대상이 있는데 지문을 하나도 못 구했으면 True(재수정 감지 불가 = 조용한 강등 방지 신호)."""
    if state.digests:
        return False
    cwd = Path(workspace)
    return any(_hashable(cwd / entry.path) for entry in state.entries)


def head_moved(before: WorktreeState, after: WorktreeState) -> bool:
    """위임 중 HEAD 가 움직였는지 — 커밋/reset/amend 전부 포함(역할 계약 위반 신호)."""
    return before.head != after.head


def committed_paths(
    before: WorktreeState,
    after: WorktreeState,
    *,
    workspace: Path | str,
    run_git: GitRunner | None = None,
) -> tuple[str, ...]:
    """위임 중 HEAD 가 움직였으면 그 커밋 범위에서 변한 경로를 낸다.

    범위 밖 파일을 고치고 **커밋**하면 전·후 worktree 가 둘 다 clean 이라 상태 비교로는 흔적이 0
    이다. 위임 역할 프롬프트가 커밋을 금지하지만 이 가드의 존재 이유가 바로 "그 금지를 믿지 않는
    것"이므로, HEAD 이동 시 트리 diff 를 판정 입력에 합산한다. diff 산출 실패는 빈 값(상태 비교
    결과는 그대로 살아 있다).
    """
    if not head_moved(before, after) or not after.head:
        return ()
    runner = run_git or _default_git_runner
    args = ["diff-tree", "-r", "--no-commit-id", "--name-only", "-z"]
    args += [before.head, after.head] if before.head else ["--root", after.head]
    try:
        rc, output = runner(Path(workspace), args)
    except Exception:  # noqa: BLE001 — 보강 입력이라 판정을 죽이지 않는다.
        return ()
    if rc != 0:
        return ()
    return tuple(sorted({token for token in output.split("\0") if token}))


def changed_status_paths(before: WorktreeState, after: WorktreeState) -> tuple[str, ...]:
    """전·후 상태가 달라진 경로를 정렬해 반환한다.

    후 상태에 새로 나타난 변경뿐 아니라, 위임 중 사라지거나 다른 XY 상태로 바뀐 기존 dirty
    경로도 잡는다. 범위 밖 파일을 삭제·stage·복원한 경우도 위임이 만진 사실을 숨기지 않는다.

    **상태코드가 같아도** blob 해시나 mode/submodule 지문이 달라졌으면 변경이다 — 이미 dirty(``M``)한
    파일을 위임이 또 고치면 코드는 ``M``→``M`` 그대로라 코드 비교만으론 안 보이고(병렬 공유 트리
    clobber가 정확히 이 형상), chmod(+x)·submodule 포인터 변경은 내용이 같아 해시도 안 움직인다.
    양쪽 신호를 다 가진 경로만 비교하고, 한쪽이라도 없으면 상태코드 판정을 따른다.
    """
    before_map = {entry.path: entry.code for entry in before.entries}
    after_map = {entry.path: entry.code for entry in after.entries}
    before_digests = dict(before.digests)
    after_digests = dict(after.digests)
    before_modes = dict(before.modes)
    after_modes = dict(after.modes)
    changed: list[str] = []
    for path in before_map.keys() | after_map.keys():
        if before_map.get(path) != after_map.get(path):
            changed.append(path)
            continue
        if _signal_differs(before_digests, after_digests, path):
            changed.append(path)
            continue
        if _signal_differs(before_modes, after_modes, path):
            changed.append(path)
    return tuple(sorted(changed))


def _signal_differs(before: dict[str, str], after: dict[str, str], path: str) -> bool:
    """양쪽이 다 아는 경로에서만 보강 신호를 비교한다(한쪽 미상 = 판정 보류)."""
    before_value = before.get(path)
    after_value = after.get(path)
    return (before_value is not None and after_value is not None
            and before_value != after_value)


def _load_repo_coordinates(tools_dir: Path):
    path = tools_dir / "repo_coordinates.py"
    spec = importlib.util.spec_from_file_location("_delegate_repo_coordinates", path)
    if spec is None or spec.loader is None:
        raise DelegateScopeError(f"repo 좌표 normalizer 로드 실패: {path}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 — 좌표 판정 불능은 조용히 허용하면 안 된다.
        if _is_engine_rev_skew(exc):
            raise
        raise DelegateScopeError(f"repo 좌표 normalizer 로드 실패: {path}: {exc}") from exc
    _verify_engine_rev(module, path.name)
    return module


def ticket_touches(
    board_py: Path | str,
    ticket_id: str,
    *,
    pm_root: Path | str,
) -> tuple[str, ...]:
    """board의 ticket frontmatter에서 문자열 ``touches``를 읽는다.

    위임 훅이 프롬프트 문장을 재파싱하지 않도록 ticket ID를 구조화 입력으로 받는다. ticket 부재,
    board 로드 실패, 잘못된 frontmatter는 판정 입력 불능이므로 :class:`DelegateScopeError`다.

    board 조회는 ``<id>-*.md`` **glob** 이라 ID에 메타문자가 들어오면 다른 ticket의 ``touches``를
    집어올 수 있다(엉뚱한 허용 범위 = 조용한 오판). 그래서 메타문자/경로 구분자를 먼저 거부하고,
    조회된 frontmatter의 ``id``가 요청 ID와 **정확히 일치**하는지 재확인한다.
    """
    if not str(ticket_id).strip() or _TICKET_ID_UNSAFE.search(str(ticket_id)):
        raise DelegateScopeError(
            f"ticket id 형식 거부(glob 메타/경로 구분자 포함): {ticket_id!r}"
        )
    ticket_id = str(ticket_id).strip()
    path = Path(board_py)
    spec = importlib.util.spec_from_file_location("_delegate_scope_board", path)
    if spec is None or spec.loader is None:
        raise DelegateScopeError(f"board 모듈 로드 실패: {path}")
    board = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(board)
    except Exception as exc:  # noqa: BLE001 — 중첩 skew는 보존, 그 밖의 로드 실패는 계약 오류로 변환.
        if _is_engine_rev_skew(exc):
            raise
        raise _ticket_touches_error(ticket_id, exc) from exc
    _verify_engine_rev(board, path.name)
    try:
        if hasattr(board, "REPO"):
            board.REPO = Path(pm_root)
        _status, ticket_path = board.find_ticket(ticket_id)
        frontmatter, _body = board.load_ticket(ticket_path)
    except Exception as exc:  # noqa: BLE001 — 잘못된 ticket 입력을 허용 0으로 오인하지 않는다.
        if _is_engine_rev_skew(exc):
            raise
        raise _ticket_touches_error(ticket_id, exc) from exc

    found_id = str(frontmatter.get("id") or "").strip()
    if found_id != ticket_id:
        raise DelegateScopeError(
            f"ticket id 불일치: 요청={ticket_id!r} 조회={found_id!r} ({ticket_path})"
        )

    raw = frontmatter.get("touches")
    if isinstance(raw, str):
        return (raw.strip(),) if raw.strip() else ()
    if isinstance(raw, list):
        return tuple(
            item.strip() for item in raw
            if isinstance(item, str) and item.strip()
        )
    if raw is None:
        return ()
    raise DelegateScopeError(f"ticket touches 형식이 문자열/목록이 아니다: {ticket_id}")


def allowed_paths(
    touches: Sequence[str],
    *,
    role: str,
    pm_root: Path | str,
    workspace: Path | str,
    coordinates_module=None,
    write_roles: Iterable[str] | None = None,
    on_drop: Callable[[str, str], None] | None = None,
) -> tuple[str, ...]:
    """역할과 ticket ``touches``를 repo-relative 허용 경로로 바꾼다.

    ``touches``는 **항목별로** 정규화한다 — multi-PM ticket은 여러 슬롯을 touch 할 수 있고,
    리스트 단위로 정규화하면 이 workspace와 무관한 한 항목 때문에 전체가 예외가 되어 허용 경로가
    통째로 사라진다(경고 폭증 또는 판정 실종). 해소 불가/타 슬롯 항목은 **드롭**하되 ``on_drop``
    으로 호출부가 loud 하게 알린다. 전부 드롭되면 허용 0(보수 방향)이다.
    """
    roles = frozenset(write_roles) if write_roles is not None else WRITE_ROLES
    if role not in roles or not touches:   # 쓰기 역할이 아니면 허용 0(신규 역할 = 안전 방향)
        return ALWAYS_ALLOWED_PATHS

    coords = coordinates_module or _load_repo_coordinates(
        Path(__file__).resolve().parent
    )
    canonical: set[str] = set()
    for item in touches:
        try:
            normalized = coords.normalize_repo_paths(
                [item],
                pm_root=Path(pm_root),
                workspace=Path(workspace),
            )
        except Exception as exc:  # normalizer 구체 예외 타입에 결합하지 않는다(항목 단위 드롭).
            if on_drop is not None:
                on_drop(item, str(exc))
            continue
        for path in normalized:
            value = coords.canonicalize_path_notation(str(path)).rstrip("/")
            if value and value != ".":
                canonical.add(value)
    return tuple(sorted(canonical))


def _covered(path: str, allowed: Iterable[str]) -> bool:
    return any(path == root or path.startswith(root + "/") for root in allowed)


def out_of_scope_changes(
    before: WorktreeState,
    after: WorktreeState,
    *,
    touches: Sequence[str],
    role: str,
    pm_root: Path | str,
    workspace: Path | str,
    coordinates_module=None,
    write_roles: Iterable[str] | None = None,
    on_drop: Callable[[str, str], None] | None = None,
    run_git: GitRunner | None = None,
) -> tuple[str, ...]:
    """위임 중 상태가 바뀐 경로(+커밋된 경로) 중 허용 범위 밖 경로만 반환한다."""
    allowed = allowed_paths(
        touches,
        role=role,
        pm_root=pm_root,
        workspace=workspace,
        coordinates_module=coordinates_module,
        write_roles=write_roles,
        on_drop=on_drop,
    )
    changed = set(changed_status_paths(before, after))
    changed.update(committed_paths(before, after, workspace=workspace, run_git=run_git))
    return tuple(
        path for path in sorted(changed)
        if not _covered(path, allowed)
    )


def format_warning(paths: Sequence[str]) -> str:
    """범위 밖 경로를 loud 경고 블록으로 만든다. 빈 목록이면 출력도 없다."""
    unique = tuple(sorted(set(paths)))
    if not unique:
        return ""
    lines = [
        "=== ⚠ 위임 범위 밖 변경 ===",
        "위임 범위 밖 변경이 감지되었습니다. 차단하지 않으며 PM이 격리/복원/수용을 판정해야 합니다.",
        "  · gitignored 산출물(.project_manager/.local 등)은 git status 입력에 없어 판정 대상이 아닙니다.",
        "  · 위임 시간창 기준이라 다른 터미널/도구가 만든 변경도 섞일 수 있습니다.",
        "  · 판정 범위는 이 git repo(toplevel) 안입니다 — 그 밖/중첩 repo 안의 변경은 보이지 않습니다.",
    ]
    lines.extend(f"  - {path}" for path in unique)
    return "\n".join(lines)
