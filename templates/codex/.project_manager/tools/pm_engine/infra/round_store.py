"""라운드 파일 저장 (층: infra).

라운드 디렉터리 위치, 순번 예약(배타 생성), 재개방, 원자 교체, 디렉터리 스캔과 엔진 임시 파일 규약을 소유한다.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

from pm_engine.domain import round_rules
from pm_engine.infra import fs


# 라운드 디렉터리는 status 디렉터리(open/claimed/blocked/done)의 형제다 — 티켓 파일이 그 사이를
# 옮겨다녀도 라운드는 자기 자리에 남는다.
ROUNDS_DIRNAME = "rounds"

# 보드 git 이 추적하는 일반 파일이다 — 슬롯 run-dir 사본(소유자 전용)과 권한 관례가 다르다.
ROUND_FILE_MODE = 0o644

# 원자 교체 중간 파일의 이름 규약 — 점 접두라 라운드 스캔 표면 밖이다(회수는 무락이라 이 파일이
# 디스크에 있는 창 동안 같은 티켓의 load/verify/reserve 가 지나간다).
ROUND_TEMPORARY_PREFIX = "."
ROUND_TEMPORARY_SUFFIX = ".tmp"


def rounds_dir(tickets_dir: Path | str) -> Path:
    """`<tickets_dir>/rounds` — 모든 티켓의 라운드가 모이는 고정 위치."""
    return Path(tickets_dir) / ROUNDS_DIRNAME


def rounds_dir_for_ticket(ticket_id: str, tickets_dir: Path | str) -> Path:
    """`<tickets_dir>/rounds/<ticket_id>` — 티켓 하나의 라운드 디렉터리."""
    return rounds_dir(tickets_dir) / round_rules._safe_ticket_id(ticket_id)


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
        parsed = round_rules.parse_round_filename(entry.name) if entry.is_file() else None
        if parsed is None:
            raise round_rules.RoundsError(
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
    round_rules._require_role(role)
    if lock is None:
        raise round_rules.RoundsError(
            "라운드 예약에는 락 컨텍스트가 필요하다 — 채번+생성을 직렬화하지 않으면 두 예약이 "
            "같은 순번을 잡는다 (호출자가 board_lock() 을 넘긴다)"
        )
    directory = rounds_dir_for_ticket(ticket_id, tickets_dir)
    with lock:
        directory.mkdir(parents=True, exist_ok=True)
        existing = _scan_round_files(directory)
        ordinal = (max(item[0] for item in existing) + 1) if existing else 1
        path = directory / round_rules.round_filename(ordinal, role)
        try:
            _write_new_file(path, content)
        except FileExistsError as exc:
            raise round_rules.RoundsError(
                f"라운드 예약 충돌 — 이미 있는 파일: {path} (같은 순번을 두 예약이 잡았다)"
            ) from exc
    return path


def reopen_round(
    tickets_dir: Path | str, ticket_id: str, role: str, *, ordinal: int, lock,
) -> Path:
    """이미 예약된 그 순번을 **다시 열어** 경로만 돌려준다 — 채번도 생성도 하지 않는다.

    한 단계를 이어 시키는 자리다. 이어 시킬 때마다 새 순번을 채번하면 같은 역할이 번호만 바꿔
    반복되고 고정 예산의 수열이 그만큼 밀린다. 재개방은 순번을 그대로 두고, 산출은 회수가
    `replace_round` 로 그 파일을 교체한다 — 지나간 판은 board git 이 들고 있다(라운드 파일 안에
    이력을 쌓지 않는다).

    통과 조건은 하나다: **그 순번이 실재하고 역할이 요청과 같다**. 없는 순번이나 다른 역할의
    순번을 열면 순번↔역할 결속이 깨지고, 한 순번을 둘이 쥔 상태(`round-dup`)면 어느 파일을
    여는지가 조용히 갈린다 — 셋 다 거부다.

    락은 예약과 같은 구간을 잡는다 — 판정과 반환 사이에 다른 예약이 이 디렉터리를 바꿀 수 있는
    창이 그 구간이다(보드 락은 재진입이 없어 호출자는 잡지 않은 채 넘긴다).
    """
    round_rules._require_role(role)
    if lock is None:
        raise round_rules.RoundsError(
            "라운드 재개방에는 락 컨텍스트가 필요하다 — 판정+반환을 직렬화하지 않으면 그사이 "
            "예약이 같은 자리를 바꾼다 (호출자가 board_lock() 을 넘긴다)"
        )
    directory = rounds_dir_for_ticket(ticket_id, tickets_dir)
    with lock:
        existing = _scan_round_files(directory)
        matched = [item for item in existing if item[0] == ordinal]
        if not matched:
            reserved = ", ".join(
                round_rules.round_filename(item[0], item[1]) for item in existing
            ) or "없음"
            raise round_rules.RoundsError(
                f"재개방할 라운드가 없다: {ticket_id} ordinal={ordinal} "
                f"(예약된 라운드: {reserved})"
            )
        if len(matched) > 1:
            raise round_rules.RoundsError(
                f"재개방 대상이 모호하다 — 순번 중복: {ticket_id} ordinal={ordinal} "
                f"({', '.join(sorted(item[1] for item in matched))})"
            )
        _ordinal, found_role, path = matched[0]
        if found_role != role:
            raise round_rules.RoundsError(
                f"재개방 대상의 역할이 다르다: {ticket_id} ordinal={ordinal} "
                f"role={found_role} · 요청={role} (한 순번은 한 역할이다)"
            )
    return path


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
        fs.atomic_replace(temporary, target)
    finally:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass
