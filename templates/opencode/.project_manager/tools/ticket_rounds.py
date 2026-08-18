#!/usr/bin/env python3
"""라운드 사이드카 공용 seam — 티켓 명세 파일과 분리된 역할 산출 파일을 소유한다.

티켓은 명세 파일(`tickets/<state>/<id>.md`) 하나와 라운드 디렉터리
`tickets/rounds/<id>/NN-<role>.md` 로 이뤄진다. 라운드(역할 산출 1건) = 파일 1개이고, 라운드
디렉터리는 **고정 위치**라 티켓 상태 이동(open→claimed→done)을 따라가지 않는다 — 상태 이동은
명세 파일 하나의 rename 으로 끝난다.

이 모듈이 소유하는 것:
  - 경로 규약(`rounds_dir`·`rounds_dir_for_ticket`)과 파일명 문법(`round_filename`·
    `parse_round_filename`). **순번과 역할은 파일 이름이 단일 진실**이고 첫 줄 헤더의 라벨·날짜는
    사람용이다.
  - 티켓 전역 순번 예약(`reserve_round`) — 채번과 배타 생성을 호출자가 넘긴 락 컨텍스트 안에서
    한 번에 끝낸다. 두 예약이 같은 번호를 계산할 수 있는 창이 그 구간뿐이라 락은 짧고,
    회수(`replace_round`)에는 락이 없다(교체 대상이 자기 파일 하나).
  - 로드·판정·렌더(`load_rounds`·`verify_rounds`·`render_rounds_for_show`).

소유하지 않는 것: 보드 경로 해소와 락 자체(호출자가 `tickets_dir` 와 락 컨텍스트를 넘긴다 —
이 모듈은 board 를 로드하지 않는다), 역할 골격의 *내용*(`pm_delegate` 가 단일 진실이고 여기서는
호출만 한다), 라운드 *내용* 규칙(리뷰 블록·finding ID·PM 판정 형식).

불변식:
  - 같은 티켓에서 순번은 유일하고 1..N 연속이다 — 경합하면 배타 생성이 한쪽만 통과시킨다.
  - 이름 문법을 벗어난 항목은 조용히 건너뛰지 않는다(`load_rounds` 는 loud, `verify_rounds` 는
    판정으로 표면화). **예외는 점(`.`)으로 시작하는 항목** — 엔진의 임시 산출(원자 교체 중간
    파일)과 도구가 흘린 부산물이 사는 자리이고 라운드가 아니다. 회수는 무락이라 그 창 동안
    다른 호출이 이 자리를 지나므로, 자기 임시 파일로 자기 판정을 깨지 않으려면 규약이 필요하다.
  - 교체 뒤 디스크에는 옛 내용 아니면 새 내용만 있다(공용 원자 교체 seam).
  - 판정은 파일시스템 상태만 본다 — 장부도 stamp 도 두지 않는다.
  - **산출 없음(`pending`) 판정은 그 라운드 파일 하나의 내용만 본다** — 날짜도, 같은 티켓의
    다른 라운드도, 명세도 입력이 아니다. 시점에 기대면 같은 역할 앞 라운드의 회수가 손대지
    않은 뒤 라운드의 판정을 뒤집어, 회수면과 조회·lint 면이 서로 다른 답을 낸다.

파일 IO 는 공용 seam 을 지난다 — 판독은 `file_lock.read_text_shared`, 교체는
`file_lock.atomic_replace`, 예약 생성은 `os.O_EXCL`+`os.O_BINARY` 바이트 쓰기다(텍스트 모드
줄끝 번역이 끼면 호출자가 계산한 bytes 와 디스크 bytes 가 갈린다).
"""

from __future__ import annotations

import os
import re
import sys
import uuid
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import NamedTuple

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


# baked 엔진 rev — engine_rev.py --bump 이 STAMPED_MODULES 전체와 함께 기계 재작성한다.
ENGINE_REV = "v1.7.6"

# 라운드 디렉터리는 status 디렉터리(open/claimed/blocked/done)의 형제다 — 티켓 파일이 그 사이를
# 옮겨다녀도 라운드는 자기 자리에 남는다.
ROUNDS_DIRNAME = "rounds"

# 사람용 라운드 절명(첫 줄 헤더의 라벨). 키 집합 = 라운드를 남기는 역할 전부다.
# **권위 방향은 이 표 → board** 다 — 라운드 헤더를 내는 쪽이 여기이고, board 는 절명·argparse
# 선택지를 이 표에서 파생해 읽는다(복제 0). board 를 로드해 가져오지 않는다 — 소비 방향이
# board → 이 모듈이라 반대 방향 로드는 순환이다.
ROLE_LABELS: dict[str, str] = {
    "architect": "설계",
    "developer": "구현 보충",
    "code-reviewer": "리뷰",
    "researcher": "조사",
    "external-reviewer": "추가 리뷰",
}
ROLES: tuple[str, ...] = tuple(ROLE_LABELS)

ROUND_FILE_SUFFIX = ".md"
# 2자리 zero-pad. 100 이상은 자릿수만 늘고 숫자 정렬(파서가 int 로 뗀다)은 그대로다.
ROUND_ORDINAL_WIDTH = 2
# 보드 git 이 추적하는 일반 파일이다 — 슬롯 run-dir 사본(소유자 전용)과 권한 관례가 다르다.
ROUND_FILE_MODE = 0o644

# 판정 코드. 완료 게이트가 red 로 보는 것은 순번 유일성·연속성이 깨진 둘(`round-gap` 삭제 의심 ·
# `round-dup` 같은 순번 중복)이고, `round-name`(문법 위반)과 `round-pending`(시드 그대로)은
# 표시용이다. 심각도 자체는 소비자(lint·complete 게이트)가 정한다.
PROBLEM_NAME = "round-name"
PROBLEM_GAP = "round-gap"
PROBLEM_DUPLICATE = "round-dup"
PROBLEM_PENDING = "round-pending"

# 원자 교체 중간 파일의 이름 규약 — 점 접두라 라운드 스캔 표면 밖이다(회수는 무락이라 이 파일이
# 디스크에 있는 창 동안 같은 티켓의 load/verify/reserve 가 지나간다).
ROUND_TEMPORARY_PREFIX = "."
ROUND_TEMPORARY_SUFFIX = ".tmp"

# Windows 가 파일/디렉터리 이름으로 열지 못하는 예약 장치명. 두 플랫폼에서 같은 티켓 ID 가 같은
# 자리를 가리키도록 여기서 함께 거부한다(POSIX 에서만 만들어지는 라운드 디렉터리 금지).
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)

# 이름 후보를 느슨히 뗀 뒤 정규 형태와 대조한다(문법 판정은 `round_filename` 한 곳이 소유).
_ROUND_FILENAME_RE = re.compile(r"\A(?P<ordinal>\d+)-(?P<role>[^\r\n]+)\Z")
_HEADER_DATE_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")


class RoundsError(RuntimeError):
    """라운드 규약 위반 — 파일명 문법·역할·순번·예약 계약."""


class Round(NamedTuple):
    """라운드 파일 하나. `pending` 은 내용이 시드 그대로(산출 없음)라는 뜻이다."""

    ordinal: int
    role: str
    path: Path
    text: str
    pending: bool


class RoundProblem(NamedTuple):
    """`verify_rounds` 판정 1건 — 코드와 사람용 상세."""

    code: str
    detail: str


def _verify_engine_rev(sibling_module, sibling_filename):
    """로드한 형제의 baked ENGINE_REV를 이 사본과 대조한다(skew만 fail-loud)."""
    got = getattr(sibling_module, "ENGINE_REV", None)
    if got != ENGINE_REV:
        err = RuntimeError(
            f"엔진 사본 버전 불일치 — 로더 {Path(__file__).name}(rev={ENGINE_REV!r})가 "
            f"형제 {sibling_filename}(rev={got!r})를 로드했다 (사본 skew: 부분/수동 복사 또는 "
            f"구형 사본). `pm-update`(또는 pm_update.py)로 .project_manager/tools/ 전체를 재동기하라."
        )
        err._engine_rev_skew = True
        raise err


def _require_engine_sibling(path: Path, filename: str) -> None:
    """load-bearing 형제 모듈의 **부재**를 stale 사본과 같은 진단으로 번역한다 (fail-loud)."""
    if path.exists():
        return
    err = RuntimeError(
        f"엔진 사본 불완전 — 로더 {Path(__file__).name}(rev={ENGINE_REV!r})가 형제 "
        f"{filename} 을(를) 찾지 못했다: {path} (부분/수동 복사). `pm-update`(또는 "
        "pm_update.py)로 .project_manager/tools/ 전체를 재동기하라."
    )
    err._engine_rev_skew = True
    raise err


def _load_file_lock():
    """공유 읽기·원자 교체 seam(`file_lock.py`)을 같은 tools/ 에서 경로 로드한다 (지연·1회).

    이 모듈의 판독과 교체가 **둘 다** 이 형제를 지난다 — 원자 교체 대상을 일반 `open` 으로 읽으면
    Windows 가 그 교체를 막아, 쓰기만 바꾼 개선이 0 이 된다(둘은 세트다). 부재도 rev 불일치도
    흡수하지 않는다(fail-loud) — 라운드는 board 데이터라 조용한 강등이 곧 데이터 손실이다.
    """
    lock_path = Path(__file__).resolve().with_name("file_lock.py")
    _require_engine_sibling(lock_path, "file_lock.py")
    return _load_module_from_path(
        lock_path, "file_lock.py", verifier=_verify_engine_rev, cache=True,
    )


def _load_pm_delegate():
    """역할 골격 시드의 단일 진실(`pm_delegate.py`)을 경로 로드한다 (지연·1회).

    시드 문구·리뷰 블록 골격·finding ID prefill 규칙은 그 모듈 소유다 — 여기로 복제해 오면
    두 사본이 갈린 채 "시드 그대로인가" 판정이 조용히 틀린다.
    """
    delegate_path = Path(__file__).resolve().with_name("pm_delegate.py")
    _require_engine_sibling(delegate_path, "pm_delegate.py")
    return _load_module_from_path(
        delegate_path, "pm_delegate.py", verifier=_verify_engine_rev, cache=True,
    )


# ── 경로·이름 규약 ────────────────────────────────────────────────────────

def rounds_dir(tickets_dir: Path | str) -> Path:
    """`<tickets_dir>/rounds` — 모든 티켓의 라운드가 모이는 고정 위치."""
    return Path(tickets_dir) / ROUNDS_DIRNAME


def rounds_dir_for_ticket(ticket_id: str, tickets_dir: Path | str) -> Path:
    """`<tickets_dir>/rounds/<ticket_id>` — 티켓 하나의 라운드 디렉터리."""
    return rounds_dir(tickets_dir) / _safe_ticket_id(ticket_id)


def _safe_ticket_id(ticket_id: str) -> str:
    """경로 한 마디로 쓸 수 있는지만 본다.

    ID **문법**(`T-NNNN`·prefix 형)은 board 소유라 여기서 복제하지 않는다 — 복제하면 발행측이
    문법을 넓힐 때 이 사본만 남아 멀쩡한 티켓의 라운드를 막는다. 여기서 막는 것은 라운드
    디렉터리 밖을 가리키는 이름과 빈 이름이다.

    판정은 문자 열거가 아니라 **두 플랫폼의 경로 규칙**으로 한다 — 구분자 목록만 세면 한쪽에서
    샌다(드라이브 상대 `C:x` 는 POSIX 구분자를 하나도 안 쓰고도 상위로 나간다). 후행 공백·점과
    예약 장치명도 함께 거부한다: Windows 가 그것들을 조용히 다른 이름으로 해소해, 같은 ID 가
    플랫폼마다 다른 디렉터리를 가리키게 된다.
    """
    text = str(ticket_id)
    if not text or text in {".", ".."}:
        raise RoundsError(f"라운드 디렉터리 이름으로 쓸 수 없는 티켓 ID: {ticket_id!r}")
    if (
        PurePosixPath(text).name != text
        or PureWindowsPath(text).name != text
        or ":" in text
        or text != text.rstrip(" .")
        or text.split(".")[0].upper() in _WINDOWS_RESERVED_NAMES
    ):
        raise RoundsError(f"라운드 디렉터리 이름으로 쓸 수 없는 티켓 ID: {ticket_id!r}")
    return text


def _require_role(role: str) -> str:
    if role not in ROLES:
        raise RoundsError(
            f"라운드 역할이 아니다: {role!r} (허용: {', '.join(ROLES)})"
        )
    return role


def _round_stem(ordinal: int, role: str) -> str:
    if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 1:
        raise RoundsError(f"라운드 순번은 1 이상의 정수여야 한다: {ordinal!r}")
    _require_role(role)
    return f"{ordinal:0{ROUND_ORDINAL_WIDTH}d}-{role}"


def round_filename(ordinal: int, role: str) -> str:
    """`NN-<role>.md` — 순번은 2자리 zero-pad(100 이상은 자릿수가 늘어난다)."""
    return f"{_round_stem(ordinal, role)}{ROUND_FILE_SUFFIX}"


def parse_round_filename(name: str) -> tuple[int, str] | None:
    """`NN-<role>.md` 를 `(순번, 역할)` 로 뗀다. 문법·역할·zero-pad 가 어긋나면 None.

    정규 형태와의 **왕복 대조**로 판정한다 — `1-developer.md`(pad 없음)·`001-developer.md`
    (과잉 pad)·`01-dev.md`(역할 아님)·`01-developer.txt`(확장자)는 전부 None 이다.
    """
    text = str(name)
    if not text.endswith(ROUND_FILE_SUFFIX):
        return None
    matched = _ROUND_FILENAME_RE.match(text[: -len(ROUND_FILE_SUFFIX)])
    if matched is None:
        return None
    role = matched.group("role")
    if role not in ROLES:
        return None
    ordinal = int(matched.group("ordinal"))
    if ordinal < 1 or f"{_round_stem(ordinal, role)}{ROUND_FILE_SUFFIX}" != text:
        return None
    return ordinal, role


# ── 헤더·시드 렌더 · 무편집(산출 없음) 판정 ────────────────────────────────

def render_round_header(role: str, *, today: str) -> str:
    """라운드 파일 첫 줄 — `## <라벨> (<role> · <YYYY-MM-DD>)`.

    준비(시드)와 추가 리뷰어 회수(실내용)가 같은 첫 줄을 쓴다. 라운드 파일 형식은 이 모듈이
    소유하므로 헤더 문자열도 여기 하나뿐이다 — 쓰는 자리마다 다시 적으면 라벨·표기가 갈린다.
    """
    _require_role(role)
    if not isinstance(today, str) or _HEADER_DATE_RE.match(today) is None:
        raise RoundsError(f"라운드 헤더 날짜는 YYYY-MM-DD 형식이어야 한다: {today!r}")
    return f"## {ROLE_LABELS[role]} ({role} · {today})"


def render_round_seed(
    role: str, ticket_text: str, *, today: str,
    previous_round: tuple[int, str] | None = None,
) -> str:
    """라운드 파일의 시드 — 첫 줄 헤더 + 역할 골격 본문.

    헤더 형식은 `## <라벨> (<role> · <YYYY-MM-DD>)` 로 종전 역할 절 헤더와 같다. 골격 본문은
    `pm_delegate` 를 부른다(로직 이동이 아니라 호출 — 시드 문구의 단일 진실은 그쪽이다).

    `previous_round` 는 **같은 역할의 직전 라운드** `(순번, 본문)` 이다 — 리뷰 역할 골격이
    확인 대상 finding ID 를 프리필하는 유일한 입력이고, 예약 시점에만 쓴다. 없으면
    자리표시자다. 무편집 판정(`_text_is_pending`)은 이 값을 입력으로 쓰지 않는다.
    """
    return (
        render_round_header(role, today=today) + "\n\n"
        + _render_round_seed_body(role, ticket_text, previous_round)
    )


def _render_round_seed_body(
    role: str, ticket_text: str, previous_round: tuple[int, str] | None = None,
) -> str:
    return _load_pm_delegate().render_ticket_growth_section_seed(
        role, ticket_text, previous_round=previous_round,
    )


def _round_header_re(role: str) -> re.Pattern[str]:
    """첫 줄 헤더 문법 — 라벨은 자유(호출자 override 허용), 역할과 날짜 표기는 고정."""
    return re.compile(
        rf"## [^\r\n]+ \({re.escape(role)} · \d{{4}}-\d{{2}}-\d{{2}}\)"
    )


def _normalized_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _text_is_pending(role: str, text: str) -> bool:
    """이 파일 하나만 보고 "시드 그대로(산출 없음)"를 판정한다 — 시점 독립.

    첫 줄은 날짜와 사람이 고른 라벨이 드는 자리라 **문법만** 본다(라벨·날짜 자유). 그 아래
    본문은 골격 소유자(`pm_delegate`)가 내용 구조로 판정한다 — 자리표시자만 든 골격이면 산출이
    없다. 판정 입력에 board 의 다른 라운드도 명세도 넣지 않는다: 넣으면 같은 역할 앞 라운드의
    회수가 손대지 않은 뒤 라운드의 판정을 뒤집는다(회수면과 표시면이 서로 다른 답을 낸다).
    """
    first_line, separator, rest = text.partition("\n")
    if not separator:
        return False
    if _round_header_re(role).fullmatch(first_line.rstrip("\r")) is None:
        return False
    body = _normalized_newlines(rest)
    if not body.startswith("\n"):
        return False        # 헤더와 본문 사이 빈 줄까지가 시드 형식이다.
    return _load_pm_delegate().ticket_round_body_is_pending(role, body[1:])


def latest_round_of_role(rounds, role: str):
    """그 역할의 **마지막 산출 라운드**(없으면 None) — 직전-라운드 규칙의 단일 소유자.

    배제는 하나다: 산출이 없는 라운드(`pending`)는 자리표시자 골격뿐이라 어떤 소비자에게도
    직전 산출이 아니다. 시드 프리필(예약)·확인 대상 finding ID(추가 리뷰어 프롬프트)가 같은
    규칙을 봐야 한쪽이 표면 밖 ID 를 받거나 "prefill 을 해소할 수 없어 강등" 경고를 정상
    경로에서 낸다. 입력은 `Round`(또는 `role`·`ordinal`·`text`·`pending` 을 가진 같은 모양)의
    목록이다.
    """
    candidates = [
        item for item in rounds
        if item.role == role and not getattr(item, "pending", False)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda item: item.ordinal)


def previous_round_of_role(rounds, role: str):
    """같은 역할 직전 산출 라운드의 `(순번, 본문)` — 시드 프리필의 유일한 입력.

    규칙은 `latest_round_of_role` 하나이고 이 함수는 시드 렌더가 받는 모양으로 줄인 것이다.
    """
    latest = latest_round_of_role(rounds, role)
    return None if latest is None else (latest.ordinal, latest.text)


def round_is_pending(round: Round, *, ticket_text: str = "") -> bool:
    """이 라운드가 아직 시드 그대로(산출 없음)인가.

    `ticket_text` 는 판정 입력이 아니다 — 소비자 호출 좌표를 유지하기 위한 자리이고, 판정은
    라운드 파일 내용만 본다.
    """
    return _text_is_pending(round.role, round.text)


# ── 예약 · 로드 · 교체 ──────────────────────────────────────────────────────

def _write_new_file(path: Path, text: str) -> None:
    """`path` 를 배타 생성해 UTF-8 바이트 그대로 쓰고 sync 한다 (있으면 FileExistsError).

    `os.O_BINARY`(Windows)를 함께 연다 — 그 플랫폼의 `os.open` 은 텍스트 모드가 기본이라 CRT 가
    LF 를 CRLF 로 번역해, 호출자가 계산한 bytes 와 디스크 bytes 가 갈린다(시드 대조가 조용히
    어긋나는 클래스). 내구성 sync 는 자기가 연 쓰기 가능 fd 위에서 한다.
    """
    binary = getattr(os, "O_BINARY", 0)
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | binary, ROUND_FILE_MODE,
    )
    try:
        os.write(descriptor, text.encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _is_engine_temporary(name: str) -> bool:
    """점 접두 = 라운드가 아닌 엔진 임시 산출·부산물(스캔 표면 밖)."""
    return name.startswith(ROUND_TEMPORARY_PREFIX)


def _scan_round_files(directory: Path) -> list[tuple[int, str, Path]]:
    """라운드 디렉터리의 항목을 순번 순으로 뗀다 — 문법을 벗어난 항목은 loud.

    점-접두 항목만 예외로 건너뛴다(`ROUND_TEMPORARY_PREFIX` 규약) — 진행 중인 원자 교체의
    중간 파일이 그 자리에 있고, 그것을 문법 위반으로 읽으면 무락 회수 창 동안 같은 티켓의
    로드·예약이 무작위로 죽는다.
    """
    if not directory.is_dir():
        return []
    found: list[tuple[int, str, Path]] = []
    for entry in sorted(directory.iterdir(), key=lambda item: item.name):
        if _is_engine_temporary(entry.name):
            continue
        parsed = parse_round_filename(entry.name) if entry.is_file() else None
        if parsed is None:
            raise RoundsError(
                f"라운드 디렉터리에 이름 문법을 벗어난 항목: {entry} "
                f"(라운드 파일은 `NN-<role>.md` 뿐이다)"
            )
        found.append((parsed[0], parsed[1], entry))
    return sorted(found, key=lambda item: (item[0], item[1]))


def reserve_round(
    tickets_dir: Path | str, ticket_id: str, role: str, *, content: str, lock,
) -> Path:
    """다음 순번을 채번해 `NN-<role>.md` 를 `content` 로 배타 생성하고 그 경로를 돌려준다.

    `lock` 은 호출자가 넘기는 **아직 진입하지 않은** 락 컨텍스트다(보드 락). 채번과 생성 사이가
    두 예약이 같은 번호를 계산할 수 있는 유일한 창이라 이 함수가 그 구간만 잡았다 놓는다 —
    회수·기록 경로에는 락이 없다. 보드 락은 재진입이 없으므로 호출자는 이 락을 잡지 않은 채
    넘긴다. 락 없이 부르면 순번이 조용히 갈리므로 호출 자체를 거부한다.

    `content` 는 준비면 시드, 추가 리뷰어 회수면 실내용이다 — 이 함수는 그 구분을 모른다.
    """
    _require_role(role)
    if lock is None:
        raise RoundsError(
            "라운드 예약에는 락 컨텍스트가 필요하다 — 채번+생성을 직렬화하지 않으면 두 예약이 "
            "같은 순번을 잡는다 (호출자가 board_lock() 을 넘긴다)"
        )
    directory = rounds_dir_for_ticket(ticket_id, tickets_dir)
    with lock:
        directory.mkdir(parents=True, exist_ok=True)
        existing = _scan_round_files(directory)
        ordinal = (max(item[0] for item in existing) + 1) if existing else 1
        path = directory / round_filename(ordinal, role)
        try:
            _write_new_file(path, content)
        except FileExistsError as exc:
            raise RoundsError(
                f"라운드 예약 충돌 — 이미 있는 파일: {path} (같은 순번을 두 예약이 잡았다)"
            ) from exc
    return path


def load_rounds(
    tickets_dir: Path | str, ticket_id: str, *, ticket_text: str = "",
) -> list[Round]:
    """티켓의 라운드를 순번 순으로 읽는다 (없으면 빈 목록).

    판독은 공유 읽기 seam 을 지나고 개행은 원문 그대로 둔다(`newline=""`) — 회수된 bytes 를
    보존해 되쓰는 지점들이 그 구분에 기대 있다. 무편집 판정은 라운드마다 **자기 파일 하나**만
    보므로 라운드 사이 순서 의존이 없다.

    `ticket_text` 는 판정 입력이 아니다 — 소비자 호출 좌표를 유지하기 위한 자리다.
    """
    directory = rounds_dir_for_ticket(ticket_id, tickets_dir)
    rounds: list[Round] = []
    for ordinal, role, path in _scan_round_files(directory):
        text = _load_file_lock().read_text_shared(
            path, encoding="utf-8", newline="",
        )
        rounds.append(
            Round(
                ordinal=ordinal, role=role, path=path, text=text,
                pending=_text_is_pending(role, text),
            )
        )
    return rounds


def _temporary_round_path(target: Path) -> Path:
    """원자 교체 중간 파일 경로 — 같은 디렉터리(rename 요건) + 점 접두(스캔 표면 밖)."""
    return target.with_name(
        f"{ROUND_TEMPORARY_PREFIX}{target.name}.{os.getpid()}.{uuid.uuid4().hex}"
        f"{ROUND_TEMPORARY_SUFFIX}"
    )


def replace_round(path: Path | str, text: str) -> None:
    """라운드 파일을 같은 디렉터리 임시 파일을 거쳐 원자 교체한다 (bytes·개행 보존).

    락은 없다 — 교체 대상이 자기 파일 하나이고, 원자 교체가 "디스크에는 옛 내용 아니면 새 내용"
    을 보장한다(열린 리더는 옛 내용을 끝까지 읽는다). 그래서 중간 파일은 **점-접두 규약**으로
    둔다 — 락이 없는 만큼 그 창 동안 같은 티켓의 로드·판정·예약이 이 디렉터리를 지나간다.
    """
    target = Path(path)
    temporary = _temporary_round_path(target)
    try:
        _write_new_file(temporary, text)
        _load_file_lock().atomic_replace(temporary, target)
    finally:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass


# ── 판정 · 렌더 ────────────────────────────────────────────────────────────

def verify_rounds(
    tickets_dir: Path | str, ticket_id: str, *, ticket_text: str = "",
) -> list[RoundProblem]:
    """라운드 디렉터리의 현재 상태만 보고 판정한다 (장부 없음).

    `round-name` 은 파일명 문법·역할 집합 위반, `round-dup` 은 같은 순번을 둘 이상이 쥔 상태
    (라운드 순서가 모호하다), `round-gap` 은 1..N 의 빈틈(삭제 의심), `round-pending` 은 시드
    그대로인 라운드다. **완료 게이트가 red 로 보는 것은 순번 유일성·연속성이 깨진 둘
    (`round-gap`·`round-dup`)** 이고 나머지 둘은 표시용이다(심각도는 소비자 소유).

    점-접두 항목은 라운드가 아니라 엔진 임시 산출·부산물이라 판정하지 않는다. 판독 실패는
    삼키지 않는다. `ticket_text` 는 판정 입력이 아니다 — 소비자 호출 좌표를 유지하기 위한
    자리다(산출 없음 판정은 라운드 파일 내용만 본다).
    """
    directory = rounds_dir_for_ticket(ticket_id, tickets_dir)
    problems: list[RoundProblem] = []
    valid: list[tuple[int, str, Path]] = []
    if directory.is_dir():
        for entry in sorted(directory.iterdir(), key=lambda item: item.name):
            if _is_engine_temporary(entry.name):
                continue
            parsed = parse_round_filename(entry.name) if entry.is_file() else None
            if parsed is None:
                problems.append(
                    RoundProblem(
                        PROBLEM_NAME,
                        f"라운드 파일 이름 문법 위반: {entry.name}",
                    )
                )
                continue
            valid.append((parsed[0], parsed[1], entry))
    valid.sort(key=lambda item: (item[0], item[1]))

    roles_by_ordinal: dict[int, list[str]] = {}
    for ordinal, role, _path in valid:
        roles_by_ordinal.setdefault(ordinal, []).append(role)
    for ordinal, roles in sorted(roles_by_ordinal.items()):
        if len(roles) > 1:
            problems.append(
                RoundProblem(
                    PROBLEM_DUPLICATE,
                    f"순번 중복 {ordinal:0{ROUND_ORDINAL_WIDTH}d}: {', '.join(sorted(roles))}",
                )
            )
    if roles_by_ordinal:
        missing = [
            f"{number:0{ROUND_ORDINAL_WIDTH}d}"
            for number in range(1, max(roles_by_ordinal) + 1)
            if number not in roles_by_ordinal
        ]
        if missing:
            problems.append(
                RoundProblem(
                    PROBLEM_GAP,
                    f"순번 빈틈(삭제 의심): {', '.join(missing)}",
                )
            )
    for ordinal, role, path in valid:
        text = _load_file_lock().read_text_shared(
            path, encoding="utf-8", newline="",
        )
        if _text_is_pending(role, text):
            problems.append(
                RoundProblem(
                    PROBLEM_PENDING,
                    f"산출 없음(시드 그대로): {round_filename(ordinal, role)}",
                )
            )
    return problems


def render_rounds_for_show(rounds) -> str:
    """라운드를 순번 순으로 이어붙인다 — 각 라운드 앞에 `--- NN-<role> ---` 구분선.

    시드 그대로인 라운드는 구분선에 `(산출 없음)` 을 병기한다. 구분선 모양은 유지하므로 사람이
    읽는 자리에서도, 구분선으로 잘라 읽는 자리에서도 같은 규칙 하나만 보면 된다.
    """
    blocks: list[str] = []
    for item in sorted(rounds, key=lambda entry: (entry.ordinal, entry.role)):
        marker = " (산출 없음)" if item.pending else ""
        body = item.text if item.text.endswith(("\n", "\r")) else item.text + "\n"
        blocks.append(
            f"--- {_round_stem(item.ordinal, item.role)}{marker} ---\n{body}"
        )
    return "\n".join(blocks)
