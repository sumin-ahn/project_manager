"""라운드 파일 이름·헤더 규약 (층: domain).

라운드 역할과 라벨, `NN-<role>.md` 이름 문법, 첫 줄 헤더, 판정 코드, 같은 역할 직전 산출 라운드 규칙,
조회용 이어붙이기를 소유한다.
"""
from __future__ import annotations

import re
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import NamedTuple

from pm_engine.domain import review_form


# 사람용 라운드 절명(첫 줄 헤더의 라벨). 키 집합 = 라운드를 남기는 역할 전부다.
# **권위 방향은 이 표 → board** 다 — 라운드 헤더를 내는 쪽이 여기이고, board 는 절명·argparse
# 선택지를 이 표에서 파생해 읽는다(복제 0). board 를 로드해 가져오지 않는다 — 소비 방향이
# board → 이 모듈이라 반대 방향 로드는 순환이다.
ROLE_LABELS: dict[str, str] = {
    "architect": "설계",
    "developer": "구현 보충",
    "code-reviewer": "리뷰",
    "researcher": "조사",
    "additional-reviewer": "추가 리뷰",
}
ROLES: tuple[str, ...] = tuple(ROLE_LABELS)
ROUND_FILE_SUFFIX = ".md"

# 2자리 zero-pad. 100 이상은 자릿수만 늘고 숫자 정렬(파서가 int 로 뗀다)은 그대로다.
ROUND_ORDINAL_WIDTH = 2

# 판정 코드. 완료 게이트가 red 로 보는 것은 순번 유일성·연속성이 깨진 둘(`round-gap` 삭제 의심 ·
# `round-dup` 같은 순번 중복)이고, `round-name`(문법 위반)과 `round-pending`(시드 그대로)은
# 표시용이다. 심각도 자체는 소비자(lint·complete 게이트)가 정한다.
PROBLEM_NAME = "round-name"
PROBLEM_GAP = "round-gap"
PROBLEM_DUPLICATE = "round-dup"
PROBLEM_PENDING = "round-pending"

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
    if ordinal < 1 or (
        f"{ordinal:0{ROUND_ORDINAL_WIDTH}d}-{role}{ROUND_FILE_SUFFIX}" != text
    ):
        return None
    return ordinal, role


def render_round_header(role: str, *, today: str) -> str:
    """라운드 파일 첫 줄 — `## <라벨> (<role> · <YYYY-MM-DD>)`.

    준비(시드)와 추가 리뷰어 회수(실내용)가 같은 첫 줄을 쓴다. 라운드 파일 형식은 이 모듈이
    소유하므로 헤더 문자열도 여기 하나뿐이다 — 쓰는 자리마다 다시 적으면 라벨·표기가 갈린다.
    """
    _require_role(role)
    if not isinstance(today, str) or _HEADER_DATE_RE.match(today) is None:
        raise RoundsError(f"라운드 헤더 날짜는 YYYY-MM-DD 형식이어야 한다: {today!r}")
    return f"## {ROLE_LABELS[role]} ({role} · {today})"


def _round_header_re(role: str) -> re.Pattern[str]:
    """첫 줄 헤더 문법 — 라벨은 자유(호출자 override 허용), 역할과 날짜 표기는 고정."""
    return re.compile(
        rf"## [^\r\n]+ \({re.escape(role)} · \d{{4}}-\d{{2}}-\d{{2}}\)"
    )


def _text_is_refused(text: str) -> bool:
    """엔진이 판정 표면 밖으로 뺀 라운드인가 — 판독은 표식 소유자(`pm_delegate`)가 낸다."""
    return review_form.pm_review_refused_marker_present(text)


def latest_round_of_role(rounds, role: str):
    """그 역할의 **마지막 산출 라운드**(없으면 None) — 직전-라운드 규칙의 단일 소유자.

    배제는 둘이고 같은 뜻이다: 산출이 없는 라운드(`pending`)는 자리표시자 골격뿐이고, 엔진
    표식이 붙은 라운드는 종결된 예약(대체·회수 거부)이라 판정 표면 밖이다 — 어느 쪽도 어떤
    소비자에게도 직전 산출이 아니다. 표식이 붙는 순간 그 파일은 시드와 bytes 가 달라 `pending`
    이 아니게 되므로, 두 배제가 같은 자리에 있어야 종결된 라운드가 직전 산출로 서지 않는다.
    시드 프리필(예약)·확인 대상 finding ID(추가 리뷰어 프롬프트)가 같은 규칙을 봐야 한쪽이
    표면 밖 ID 를 받거나 "prefill 을 해소할 수 없어 강등" 경고를 정상 경로에서 낸다. 입력은
    `Round`(또는 `role`·`ordinal`·`text`·`pending` 을 가진 같은 모양)의 목록이다.
    """
    candidates = [
        item for item in rounds
        if item.role == role
        and not getattr(item, "pending", False)
        and not _text_is_refused(item.text)
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
