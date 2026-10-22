"""위임 범위 상태와 비교 규칙 (층: domain)."""
from __future__ import annotations

from collections.abc import Iterable
from typing import NamedTuple
from collections.abc import Sequence
import re

ALWAYS_ALLOWED_PATHS: tuple[str, ...] = ()

# git hash-object 한 번에 넘길 경로 수 — argv 길이 한계 회피(dirty 집합이 큰 트리 방어).
HASH_BATCH_SIZE = 100
# 이 크기를 넘는 파일은 해시 보강을 건너뛴다 — 대용량 산출물(빌드 아티팩트·모델 가중치 등)은
# 존재/상태코드로 이미 표면화되고, 그걸 읽는 비용이 판정의 최악 I/O 를 지배한다.
HASH_MAX_FILE_BYTES = 5 * 1024 * 1024


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


def head_moved(before: WorktreeState, after: WorktreeState) -> bool:
    """위임 중 HEAD 가 움직였는지 — 커밋/reset/amend 전부 포함(역할 계약 위반 신호)."""
    return before.head != after.head


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


def _covered(path: str, allowed: Iterable[str]) -> bool:
    return any(path == root or path.startswith(root + "/") for root in allowed)


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
