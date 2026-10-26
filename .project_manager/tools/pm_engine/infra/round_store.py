"""라운드 파일 저장 (층: infra).

라운드 디렉터리 위치, 순번 예약(배타 생성), 재개방, 원자 교체, 디렉터리 스캔과 엔진 임시 파일 규약을 소유한다.
"""
from __future__ import annotations

from pm_engine.infra import pm_paths
import re
from pm_engine.domain import ticket_rules
from pm_engine.infra import ticket_store

from pm_engine.domain import reviewer_budget

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


# ── 프롬프트 조립 ─────────────────────────────────────────────────────────


def _load_review_context(home) -> str:
    """인스턴스 overlay 또는 기본 헤더에 티켓 결정 권위 규약을 빠짐없이 결합한다."""
    context_path = home.local_dir.parent / "review_context.local.md"
    if context_path.exists():
        try:
            context = fs.read_text_shared(context_path, encoding="utf-8").strip() + "\n"
            if reviewer_budget._AUTHORITATIVE_TICKET_CONTEXT not in context:
                context += "\n" + reviewer_budget._AUTHORITATIVE_TICKET_CONTEXT + "\n"
            return context
        except OSError:
            pass
    return reviewer_budget._DEFAULT_CONTEXT_HEADER


def _find_ticket_file(home, ticket_id: str, ) -> Path:
    """board 에서 ticket 파일 하나를 찾는다 (못 찾으면 fail-loud).

    ticket 디렉토리는 `pm_paths.tickets_dir(home)`으로 해소한다.
    board/ 분리 후 wiki/ legacy 위치(stale·ticket 미발견)를 안 보게.

    조회 판정은 공용 정확-일치 seam(`ticket_store.find_ticket_exact`)이 소유한다 — `{id}-*.md`
    prefix glob 의 **첫 매칭**을 믿으면 `T-NNNN` 과 `T-NNNN-001` 공존 시 **다른 티켓의
    touches** 로 검토 범위가 정해진다(조용한 오판). 사본 판정을 두지 않는 이유는
    같은 클래스가 표면마다 half-fix 로 재발해서다. 슬러그 없는 파일명(`T-NNNN.md`)은 prefix
    충돌이 불가능한 정확 이름이라 seam 이 비면 그 폴백을 그대로 본다(종전 동작).
    """
    if not ticket_id or re.search(r"[\\/*?\[\]]", ticket_id):
        raise pm_paths.AnchorResolutionError(f"ticket id 형식이 안전하지 않습니다: {ticket_id!r}")
    tickets_dir = pm_paths.tickets_dir(home)
    status_dirs = tuple(ticket_rules.STATUS_DIRS)
    found = ticket_store.find_ticket_exact(
        home, ticket_id,
        search_dirs=[(status, tickets_dir / status) for status in status_dirs],
    )
    if found is not None:
        return found[1]
    for status_dir in status_dirs:
        exact = tickets_dir / status_dir / f"{ticket_id}.md"
        if exact.exists():
            return exact
    raise pm_paths.AnchorResolutionError(
        f"ticket {ticket_id} 을 해소된 board에서 찾지 못했습니다: {tickets_dir}"
    )


class _TicketTouches(list[str]):
    """실 ticket 파일에서 읽은 touches와 그 파일 provenance.

    list 하위형이라 기존 소비자/테스트 seam은 그대로 동작한다. main은 이 provenance가 있을 때
    빈-diff 게이트를 지난 뒤 같은 파일의 본문을 읽는다. plain list가 주입된 테스트
    seam은 실제 ticket 파일을 약속하지 않은 fixture라 본문 조회 실패를 hard-fail로 승격하지 않는다.
    """

    def __init__(self, values: list[str], source: Path):
        super().__init__(values)
        self.source = source


def parse_ticket_touches(home, ticket_id: str, ) -> list[str]:
    """board ticket frontmatter 의 touches 필드를 파싱해 경로 목록을 반환한다.

    touches 값 파싱은 이 파일의 YAML 리더가 하고, **어느 파일이 그 티켓인가**는 board 의 정확-일치
    seam 이 정한다(`_find_ticket_file`). 못 찾으면 fail-loud.
    """
    source = _find_ticket_file(home, ticket_id, )
    return _TicketTouches(_parse_touches_from_file(source), source)


def _split_ticket_frontmatter(text: str, *, source: Path) -> tuple[str | None, str]:
    """frontmatter 원문과 본문을 한 경계에서 분리한다(None이면 frontmatter 없음).

    ticket_store.load_ticket을 재사용하지 않는 이유는 본문을 YAML 재직렬화나 strip 없이 byte-for-byte
    보존해야 하고, touches의 경량 파서도 YAML 의존 없이 같은 원문을 소비하기 때문이다. opener가
    있으면 closer도 반드시 있어야 하며 두 소비자 모두 같은 AnchorResolutionError로 fail-loud 한다.
    """
    if not text.startswith("---\n"):
        return None, text
    after_open = text[4:]
    end = after_open.find("\n---\n")
    if end == -1:
        raise pm_paths.AnchorResolutionError(f"ticket frontmatter가 닫히지 않았습니다: {source}")
    return after_open[:end], after_open[end + 5:]


def _load_ticket_text_and_body(path: Path) -> tuple[str, str]:
    """ticket 파일의 전문과 frontmatter만 벗긴 본문 (판독 1회).

    전문이 따로 필요한 이유는 라운드 판독이다 — 라운드의 "산출 없음(시드 그대로)" 판정은 준비가
    시드를 렌더할 때 본 것과 **같은 명세 텍스트**를 입력으로 써야 하고, 준비(`pm_delegate`)는
    파일 전문을 쓴다.
    """
    raw = fs.read_text_shared(path, encoding="utf-8")
    _frontmatter, body = _split_ticket_frontmatter(raw, source=path)
    return raw, body


def _parse_title_from_file(path: Path) -> str | None:
    """ticket 파일에서 frontmatter title 스칼라를 추출한다(선별 헤더 요약 전용).

    board 의 YAML frontmatter 로더(`_parse_estimate_from_file` 과 같은 seam)를 재사용한다 —
    자체 정규식은 estimate 축에서 이미 확인된 결함(YAML 주석 꼬리가 값에 붙는 오독)을 title 에서
    반복할 뿐이라 두지 않는다."""
    fm, _body = ticket_store.load_ticket(path)
    title = fm.get("title") if isinstance(fm, dict) else None
    return title.strip() or None if isinstance(title, str) else None


def _ticket_body_selection_header(path: Path, ticket_id: str) -> str:
    """선별한 티켓 본문 앞에 붙는 frontmatter 요약.

    estimate 는 board YAML 파서(`_parse_estimate_from_file`)를 그대로 호출한다 — 표시용이라고
    값이 갈리는 경량 스캐너를 따로 두지 않는다(F-001)."""
    title = _parse_title_from_file(path)
    touches = _parse_touches_from_file(path)
    estimate = _parse_estimate_from_file(path)
    return (
        f"id={ticket_id} · title={title or '(미상)'} · "
        f"touches=[{', '.join(touches)}] · estimate={estimate or '(미상)'}"
    )


def _parse_frontmatter_scalar(path: Path, key: str) -> str | None:
    """ticket frontmatter 의 문자열 스칼라 (없거나 비-문자열이면 None).

    frontmatter 로더(`ticket_store.load_ticket` = YAML)를 재사용한다. 자체 정규식
    파싱은 YAML 이 주석으로 읽는 꼬리(`estimate: small # reviewed`)를 값에 붙여 `small` 을
    모르는 값으로 만들었다 — board 가 읽는 값과 다른 값을 보였다. 같은 값을 두 번 해석하지
    않는 것이 유일한 해소다.

    비-문자열(리스트·숫자)은 값을 지어내지 않고 None 이다."""
    fm, _body = ticket_store.load_ticket(path)
    value = fm.get(key) if isinstance(fm, dict) else None
    return value.strip() or None if isinstance(value, str) else None


def _parse_estimate_from_file(path: Path) -> str | None:
    """ticket 파일의 `estimate` 스칼라 — **board 와 같은 파서**(리뷰 입력 머리 표시)."""
    return _parse_frontmatter_scalar(path, "estimate")


def _parse_touches_from_file(path: Path) -> list[str]:
    """ticket 파일에서 frontmatter touches 를 추출한다."""
    fm_text, _body = _split_ticket_frontmatter(
        fs.read_text_shared(path, encoding="utf-8"), source=path,
    )
    if fm_text is None:
        return []
    touches: list[str] = []
    in_touches = False
    for line in fm_text.splitlines():
        if re.match(r"^touches\s*:", line):
            empty_inline = re.match(r"^touches\s*:\s*\[\s*\]\s*$", line)
            inline_match = re.match(r"^touches\s*:\s*\[(.+)\]", line)
            if empty_inline:
                touches = []
                in_touches = False
            elif inline_match:
                items = [s.strip().strip("\"'") for s in inline_match.group(1).split(",")]
                touches = [i for i in items if i]
                in_touches = False
            elif re.match(r"^touches\s*:\s*$", line):
                in_touches = True
            else:
                val = re.sub(r"^touches\s*:\s*", "", line).strip().strip("\"'")
                if val:
                    touches = [val]
                in_touches = False
        elif in_touches:
            item_match = re.match(r"^\s*-\s+(.+)$", line)
            if item_match:
                touches.append(item_match.group(1).strip().strip("\"'"))
            elif line and not line[0].isspace():
                in_touches = False
    return touches
