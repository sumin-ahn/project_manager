"""핸드오프 기록과 회귀 판정 텍스트."""
from __future__ import annotations

import datetime
import fnmatch
from pm_engine.domain import identity_args
from pm_engine.domain import pytest_report
import re
from pm_engine.domain import session_history


UNFINISHED_RAW_DISPLAY_LIMIT = 5


PM_STATE_LINE_WARNING_THRESHOLD = 700


LOG_ARCHIVE_SUGGEST_THRESHOLD = 40


_LOG_ENTRY_RE = re.compile(r"^## \[\d{4}-\d{2}-\d{2}\]", re.MULTILINE)


SLIDING_WINDOW_SIZE = 3


DIRTY_GATE_LIST_LIMIT = 20


SHIPPING_GLOBS = (
    ".project_manager/tools/*",       # canonical 엔진 (한 단계)
    ".project_manager/tools/**",      # canonical 엔진 (중첩)
    "templates/**",                   # 출하 템플릿 (claude_code·opencode)
    ".claude/**",                     # claude 어댑터 (agents·skills·commands)
    ".opencode/**",                   # opencode 어댑터
    "CLAUDE.md",                      # claude 진입문서
    "AGENTS.md",                      # opencode 진입문서
    "**/CLAUDE.md",                   # 중첩 진입문서 (templates 하위 등)
    "**/AGENTS.md",
    "engine.manifest",                # 엔진 동기 매니페스트
    "**/engine.manifest",
    "pm-*.sh",                        # 루트 파사드 (POSIX)
    "pm-*.cmd",                       # 루트 파사드 (Windows)
    "requirements*.txt",              # 런타임/개발 의존성
    ".project_manager/wiki/pm_role.md",       # 방법론 — templates 로 출하
    ".project_manager/wiki/pm_playbook.md",
    ".project_manager/wiki/pm_principles.md",
    ".project_manager/wiki/README.md",
    ".project_manager/wiki/_template/**",
    ".project_manager/wiki/domain/**",
    # ── engine.manifest 정합 갭 (정확 경로 1:1·과잉발동 회피) ──────────
    # manifest 출하 항목 중 위 글롭에 안 잡히던 5경로를 *정확 경로* 글롭으로 1:1 닫는다
    # 포괄 글롭(`**/_template.md`·`**/*.template.md`·`**/.gitignore` 등)은 repo
    # 전체를 매칭해 비-출하(tests/fixtures/_template.md·wiki decisions/foo.template.md 등)
    # 까지 게이트를 false-fire 시킨다 — ticket 결정("정밀·과잉발동 회피")·tests/ non-shipping
    # 원칙과 모순. 1:1 정확 경로라도 미래 manifest 항목 추가는 정합 가드(test)가 잡아 동기화 강제.
    ".project_manager/wiki/tickets/_template.md",     # ticket 스캐폴드 (manifest 갭)
    ".project_manager/wiki/raw/spikes/_template.md",  # spike 스캐폴드 (manifest 갭)
    ".project_manager/wiki/pm_state.template.md",     # pm_state 템플릿 (manifest 갭)
    ".gitattributes",                                 # log union-merge·forwarder EOL (루트만)
    ".project_manager/.gitignore",                    # .project_manager .gitignore (manifest 갭)
)


PENDING_INTENT_PLACEHOLDER = (
    "<PM 손 — 다음 우선순위 + 사용자 결정 대기. board open ticket 재열거 금지.>"
)


# task 세션 정체성의 log 헤더 태그 sentinel — task 모드는 헤더 태그를 `(task:<name>)` 로
# 박아 서술형 괄호(`PM 4차 (아침 대화)`)·슬롯 태그(`<repo>_<N>`)와 기계적으로 구분한다. task 명은
# 자유 포맷(한글·공백 허용)이라 bare `(<name>)` 로는 서술 괄호와 오탐이 나서 무태그 흡수 회귀
# (`test_handoff_regex_ignores_descriptive_parens_when_untagged`)를 깬다 — sentinel 이 그 클래스를
# 닫는다. domain.bootstrap_rules 소비측(`_LOG_HANDOFF_HEADER_RE`·`_session_owns_untagged`)의 `task:` 판별과
# 미러(모듈 격리라 상수를 각 모듈에 inline). dashboard 자기 섹션은 sentinel 없이 verbatim
# `## <task>`(interface 2) — 사람 가독 표면과 기계 파싱 표면의 요구가 달라 값을 분리한다.
_TASK_TAG_PREFIX = "task:"


_UNRESOLVED_BOUNDARY_ENTRY_LIMIT = 10


_ENTRY_TYPE_RE = r"(?:\S+|<!-- feat/fix/verify/… -->)"


_TICKET_ID_BODY = r"T-(?:[A-Za-z0-9][A-Za-z0-9_-]*-\d+|\d+)"


_TICKET_ENTRY_ID_REF = rf"(?:\[\[{_TICKET_ID_BODY}\]\]|{_TICKET_ID_BODY})"


_ANY_HANDOFF_RE = re.compile(
    r"^## \[\d{4}-\d{2}-\d{2}\] handoff \| PM \d+차"
    r"(?: \(.+\))? → 다음 PM 세션$"
)


_TASK_ENTRY_TAG_RE = re.compile(r"(?: \(|\| \()task:[^)]+\)")


_SLOT_ENTRY_TAG_RE = re.compile(r" \([^()\s]+_\d+\)(?:$| — )")


def _log_entry_headers(log_text: str) -> list[str]:
    """`_LOG_ENTRY_RE`가 가리키는 entry 헤더만 원문 순서로 반환한다."""
    headers: list[str] = []
    for match in _LOG_ENTRY_RE.finditer(log_text):
        line_end = log_text.find("\n", match.start())
        if line_end < 0:
            line_end = len(log_text)
        headers.append(log_text[match.start():line_end].rstrip("\r"))
    return headers


_BOUNDARY_TASK_DEFAULT = object()


def _collect_session_entries(
    log_text: str,
    task: str | None,
    session: str | None = None,
    *,
    boundary_task: str | None | object = _BOUNDARY_TASK_DEFAULT,
) -> tuple[list[str], bool]:
    """직전 자기 handoff 이후 이 세션에 귀속된 완료/checkpoint 헤더를 수집한다.

    task 세션은 같은 ``(task:<name>)`` handoff를 경계로 삼고, ticket_finish/pm_log 가 생산하는
    같은 task 태그의 티켓 완료형/checkpoint만 소비한다. 자기 handoff가 아직 없으면 태그가 귀속
    권위이므로 무관 handoff로 자르지 않고 전체에서 최근 10건만 반환한다. 완료형의 action은
    ``feat``/``fix``/``verify`` 등 임의 단어이며 board 발행 문법의 ticket ID 뒤 ``— …`` 형태로
    판별한다. slot 세션도 같은 canonical session 태그 entry만 소비한다. legacy 무태그 entry는
    동시 slot 혼입 위험 때문에 slot 모드에서 수집하지 않는다. 정체성 미해소 수집만 태그 없는 canonical
    handoff를 경계로 삼고, 자기 경계가 없으면 가장 최근의 다른 handoff로 폴백한다. 본문은
    반환하지 않아 log 원문이 서사의 단일 진실로 남는다. ``boundary_task``를 명시하면 entry
    귀속 task와 handoff 생산 축을 분리한다. 특히 legacy 연속성을 유지하는 수집 전용 모드는
    ``task=<해소 identity>, boundary_task=None``으로 태그 entry를 무태그 handoff 이후만
    수집한다.
    """
    headers = _log_entry_headers(log_text)
    effective_boundary_task = (
        task if boundary_task is _BOUNDARY_TASK_DEFAULT else boundary_task
    )
    if task is not None:
        escaped_task = re.escape(task)
        if effective_boundary_task is not None:
            escaped_boundary_task = re.escape(effective_boundary_task)
            handoff_re = re.compile(
                rf"^## \[\d{{4}}-\d{{2}}-\d{{2}}\] handoff \| PM \d+차 "
                rf"\({_TASK_TAG_PREFIX}{escaped_boundary_task}\) → 다음 PM 세션$"
            )
        else:
            handoff_re = re.compile(
                r"^## \[\d{4}-\d{2}-\d{2}\] handoff \| PM \d+차 → 다음 PM 세션$"
            )
        ticket_entry_re = re.compile(
            rf"^## \[\d{{4}}-\d{{2}}-\d{{2}}\] {_ENTRY_TYPE_RE} \| {_TICKET_ENTRY_ID_REF} — .+ "
            rf"\({_TASK_TAG_PREFIX}{escaped_task}\)$"
        )
        checkpoint_re = re.compile(
            rf"^## \[\d{{4}}-\d{{2}}-\d{{2}}\] checkpoint \| "
            rf"\({_TASK_TAG_PREFIX}{escaped_task}\) — .+$"
        )
        own_boundaries = [
            index for index, header in enumerate(headers)
            if handoff_re.fullmatch(header)
        ]
        # 수집 전용 legacy 축은 자기 생산 문법인 무태그 handoff를 우선하고, 기존 무태그
        # 경계 폴백을 그대로 유지한다. 실행 task 축은 태그가 권위라 무관 경계를 쓰지 않는다.
        fallback_boundaries = (
            [
                index for index, header in enumerate(headers)
                if _ANY_HANDOFF_RE.fullmatch(header)
            ]
            if effective_boundary_task is None else []
        )
        boundary = own_boundaries[-1] if own_boundaries else (
            fallback_boundaries[-1] if fallback_boundaries else -1
        )
        entries = [
            header
            for header in headers[boundary + 1:]
            if ticket_entry_re.fullmatch(header) or checkpoint_re.fullmatch(header)
        ]
        unresolved = boundary < 0
        return (
            entries[-_UNRESOLVED_BOUNDARY_ENTRY_LIMIT:] if unresolved else entries,
            unresolved,
        )

    if session is not None:
        escaped_session = re.escape(session)
        handoff_re = re.compile(
            rf"^## \[\d{{4}}-\d{{2}}-\d{{2}}\] handoff \| PM \d+차 "
            rf"\({escaped_session}\) → 다음 PM 세션$"
        )
        ticket_entry_re = re.compile(
            rf"^## \[\d{{4}}-\d{{2}}-\d{{2}}\] {_ENTRY_TYPE_RE} \| {_TICKET_ENTRY_ID_REF} — .+ "
            rf"\({escaped_session}\)$"
        )
        checkpoint_re = re.compile(
            rf"^## \[\d{{4}}-\d{{2}}-\d{{2}}\] checkpoint \| "
            rf"\({escaped_session}\) — .+$"
        )
    else:
        handoff_re = re.compile(
            r"^## \[\d{4}-\d{2}-\d{2}\] handoff \| PM \d+차 → 다음 PM 세션$"
        )
        ticket_entry_re = re.compile(
            rf"^## \[\d{{4}}-\d{{2}}-\d{{2}}\] {_ENTRY_TYPE_RE} \| {_TICKET_ENTRY_ID_REF} — .+$"
        )
        # domain.ctx_recovery.build_checkpoint_entry() emits untagged headers as
        # ``checkpoint — <trigger>``; tagged task/slot headers use ``| (...)``.
        checkpoint_re = re.compile(
            r"^## \[\d{4}-\d{2}-\d{2}\] checkpoint — .+$"
        )
    own_boundaries = [
        index for index, header in enumerate(headers)
        if handoff_re.fullmatch(header)
    ]
    # 무관 handoff 경계 폴백은 태그 귀속 권위가 없는 무태그 legacy에만 허용한다.
    fallback_boundaries = (
        [
            index for index, header in enumerate(headers)
            if _ANY_HANDOFF_RE.fullmatch(header)
        ]
        if session is None else []
    )
    boundary = own_boundaries[-1] if own_boundaries else (
        fallback_boundaries[-1] if fallback_boundaries else -1
    )
    entries = [
        header
        for header in headers[boundary + 1:]
        if not _TASK_ENTRY_TAG_RE.search(header)
        and (session is not None or not _SLOT_ENTRY_TAG_RE.search(header))
        and (ticket_entry_re.fullmatch(header) or checkpoint_re.fullmatch(header))
    ]
    unresolved = boundary < 0
    return (
        entries[-_UNRESOLVED_BOUNDARY_ENTRY_LIMIT:] if unresolved else entries,
        unresolved,
    )


def collect_session_entries(
    log_text: str,
    task: str | None,
    session: str | None = None,
) -> list[str]:
    """현재 세션 entry만 반환한다. 경계 미해소 시 최근 10건으로 제한한다."""
    return _collect_session_entries(log_text, task, session)[0]


def _session_entries_text(entries: list[str], boundary_unresolved: bool = False) -> str:
    """handoff skeleton의 자동 박제 목록을 compact한 중첩 목록으로 렌더한다."""
    notice = (
        f" (경계 미해소 — 최근 {_UNRESOLVED_BOUNDARY_ENTRY_LIMIT}건)"
        if boundary_unresolved else ""
    )
    if not entries:
        # 0건이면 경계 표기를 붙이지 않는다 — 빈 목록엔 "최근 N건" 서술이 무의미(혼동만).
        return " (이 세션 박제 entry 없음)"
    return notice + "\n" + "\n".join(f"  - {header}" for header in entries)


def _session_tag(session: str | None) -> str:
    """handoff 헤더의 세션 정체성 태그 조각을 빌드한다 — ` ({session})`.

    멀티-PM 모드에서 세션 정체성(canonical `<repo>_<N>`)이 해소되면 헤더 차수 뒤에 `({session})`
    태그를 붙인다 — per-slot 시퀀스의 감사 단서다(이벤트 메타데이터·상태 저장 아님
    무충돌). 미해소(None/빈문자)면 빈 문자열을 반환해 태그를 생략한다 — 현행 헤더와
    byte-호환·하위호환. 선행 공백까지 포함해 반환하므로 템플릿은 `PM {session_num}차{session_tag}`
    로 이어 붙인다(태그 없을 땐 `PM {N}차 →` 로 정확히 현행 스키마 보존).
    """
    if not session:
        return ""
    return f" ({session})"


def _normalize_session_num(session_num: int | str) -> str:
    """세션 차수를 bare 숫자 문자열로 정규화한다 — `19`·`'19'`·`'19차'`·`'19차차'` 모두 `'19'`.

    handoff entry 템플릿은 `PM {session_num}차` 로 '차' 를 *붙인다*. skill 문서가
    `--session-num <N차>` 로 안내해 온 탓에 입력에 이미 '차' 가 있으면 이중 부착('19차차')
    됐고, sliding-window 정규식 `\\d+차` 매칭도 깨졌다.
    후행 '차'/공백을 멱등 제거해 어느 입력이든 안전하게 만든다."""
    return str(session_num).strip().rstrip("차").strip()


HANDOFF_LOG_SKELETON_TEMPLATE = """\
## [{date}] handoff | PM {session_num}차{session_tag} → 다음 PM 세션

- 이 세션 박제 entries:{entries}
- 메타 학습: <PM 손 — ticket 상태에서 도출 불가한 교훈만. 없으면 "없음".>
- pending user intent: {pending_intent}
{dirty_ack_line}- 회귀/incident: <PM 손 — 회귀 "N passed / 상태" 1줄(green 도 — baseline) + 비-자명 incident. (회귀는 1줄 load-bearing 이라 항상 적음 — board/git/log 대량 재열거만 금지.)>
"""


DIRTY_ACK_ENTRY_LABEL = "- incident (dirty-tree ack)"


FORCE_HANDOFF_ENTRY_NOTE = "커밋 안 한 변경 있음, 슬롯 유지: {slots}"


_DIRTY_ACK_LINE_RE = re.compile(
    r"^" + re.escape(DIRTY_ACK_ENTRY_LABEL) + r":[^\r\n]*(?P<cr>\r?)$", re.MULTILINE
)


_INCIDENT_LINE_RE = re.compile(r"^- 회귀/incident:", re.MULTILINE)


class DirtyAckStampError(RuntimeError):
    """기존 handoff entry 에 ack 사유를 박제할 자리를 찾지 못했다 (부작용 0 상태에서 발생)."""


def _dirty_ack_line(note: str | None) -> str:
    """handoff entry 의 dirty-tree ack 박제 줄을 빌드한다 (미ack 이면 줄 자체 생략).

    dirty 를 override 하고 나간 세션(`--ack-dirty` 또는 사용자 명시 핸드오프 호환 신호)만
    이 줄을 갖는다 —
    다음 세션 부트스트랩이 log entry 에서 "왜 미커밋인 채 넘어왔나"를 그대로 읽는다.
    """
    if not note:
        return ""
    return f"{DIRTY_ACK_ENTRY_LABEL}: {note}\n"


def upsert_dirty_ack_line(entry_text: str, note: str, newline: str = "\n") -> str:
    """기존 handoff entry 에 ack 줄을 **멱등 upsert** 한 entry 텍스트를 반환한다.

    같은-세션 재실행은 기존 entry 본문을 보존하는 경로라 skeleton 을 다시 만들지 않는다. 그렇다고
    ack 를 콘솔 경고로 흘리면 "왜 미커밋인 채 넘어왔나"가 log 에 남지 않는다 — 기계 소유 줄
    하나(`DIRTY_ACK_ENTRY_LABEL`)만 골라 갱신한다:
      - 이미 있으면 그 줄을 **교체**(재삽입 없음·같은 입력이면 byte 동일 → 멱등). 기존 줄의
        종결자(CRLF/LF)를 그대로 되살려 CRLF 로그에 bare LF 를 섞지 않는다.
      - 없으면 `- 회귀/incident:` 줄 바로 앞에 삽입(신규 skeleton 과 같은 자리).
      - 그 앵커도 없으면 entry 끝에 붙인다.
    entry 가 비어 앵커도 끝줄도 없으면 `DirtyAckStampError` — 조용히 통과시키지 않는다
    (박제 불가는 차단이 기본).
    """
    line = f"{DIRTY_ACK_ENTRY_LABEL}: {note}"
    existing = _DIRTY_ACK_LINE_RE.search(entry_text)
    if existing is not None:
        replacement = line + existing.group("cr")
        return entry_text[:existing.start()] + replacement + entry_text[existing.end():]
    anchor = _INCIDENT_LINE_RE.search(entry_text)
    if anchor is not None:
        return entry_text[:anchor.start()] + line + newline + entry_text[anchor.start():]
    if not entry_text.strip():
        raise DirtyAckStampError(
            "기존 handoff entry 가 비어 ack 사유를 박제할 자리가 없다"
        )
    suffix = "" if entry_text.endswith(("\n", "\r")) else newline
    return entry_text + suffix + line + newline


def build_handoff_log_skeleton(
    session_num: int | str,
    date: str | None = None,
    session: str | None = None,
    log_text: str = "",
    task: str | None = None,
    dirty_ack_note: str | None = None,
) -> str:
    """log/current.md 에 append 할 handoff entry skeleton 을 반환한다.

    log_text 와 task 로 직전 자기 handoff 이후 complete/checkpoint 헤더를 자동 채운다.
    session 주입 시(정체성 태그 `task:<task>`) 헤더 차수 뒤에 `({session})` 태그를 박는다 —
    task 시퀀스의 감사 단서(이벤트 메타·상태 저장 아님). 미지정이면 태그를 생략한다.
    dirty_ack_note 주입 시(dirty-tree 게이트 override) ack 사유 줄을 추가한다 — 미지정이면
    줄 생략(clean 핸드오프는 현행 스키마 byte-호환).
    """
    if date is None:
        date = datetime.date.today().isoformat()
    entries, boundary_unresolved = _collect_session_entries(
        log_text, task, session, boundary_task=task,
    )
    return HANDOFF_LOG_SKELETON_TEMPLATE.format(
        date=date,
        session_num=_normalize_session_num(session_num),
        session_tag=_session_tag(session),
        entries=_session_entries_text(entries, boundary_unresolved),
        pending_intent=PENDING_INTENT_PLACEHOLDER,
        dirty_ack_line=_dirty_ack_line(dirty_ack_note),
    )


_HANDOFF_HEADER_CANDIDATE_RE = re.compile(
    r"^## \[[^\]\n]+\]\s+handoff\s*\|\s*PM\s+.*→ 다음 PM 세션\s*$",
    re.MULTILINE,
)


_HANDOFF_HEADER_SESSION_RE = re.compile(
    r"^## \[\d{4}-\d{2}-\d{2}\] handoff \| PM (?P<num>\d+)차"
    r"(?: \((?P<session>[^()]+)\))? → 다음 PM 세션$"
)


_SESSION_ENTRIES_BLOCK_RE = re.compile(
    r"^- 이 세션 박제 entries:[^\n]*"
    r"(?:\n  - ## \[[^\n]*)*",
    re.MULTILINE,
)


_NESTED_SESSION_ENTRY_RE = re.compile(r"^  - (## \[[^\n]+)$", re.MULTILINE)


def _handoff_header_belongs_to_session(header: str, session: str | None) -> bool:
    """handoff 후보 헤더가 현재 task/slot 정체성에 속하는지 보수적으로 판정한다.

    차수는 per-slot 시퀀스라 다른 슬롯의 같은 N차는 정상이다. 따라서 "같은 차수" 판정
    전에 현재 정체성 태그를 먼저 좁힌다. 미해소는 태그 없는 헤더만 자기 것으로 본다.
    """
    if session is not None:
        return header.rstrip().endswith(f" ({session}) → 다음 PM 세션")
    return "(" not in header and header.rstrip().endswith(" → 다음 PM 세션")


def _prepare_handoff_log_change(
    log_text: str,
    *,
    session_num: int | str,
    date: str,
    session: str | None,
    task: str | None,
    dirty_ack_note: str | None = None,
) -> tuple[bool, str, str, str | None]:
    """handoff log 변경 계획을 만든다.

    반환은 ``(update_mode, payload, preview, warning)`` 이다. update_mode면 payload는
    기존 log 전체를 비파괴 갱신한 값이고, 아니면 append할 문자열이다. 같은 정체성의 마지막
    handoff 차수가 이번 차수와 같을 때만 갱신한다. 헤더 차수 파싱 실패나 기계 구획 경계
    모호(marker 0/2+)는 현행 append로 보수적 폴백하며 warning 한 줄을 돌려준다.

    dirty_ack_note 는 신규 entry(append) skeleton 에만 박힌다 — 같은-세션 갱신 모드는 기계
    소유 구획('이 세션 박제 entries')만 건드리는 byte-멱등 경로라 본문 줄을 새로 끼우지 않는다.
    """
    normalized_num = _normalize_session_num(session_num)
    candidates = [
        match
        for match in _HANDOFF_HEADER_CANDIDATE_RE.finditer(log_text)
        if _handoff_header_belongs_to_session(match.group(0), session)
    ]

    warning: str | None = None
    target: re.Match | None = None
    if candidates:
        candidate = candidates[-1]
        parsed = _HANDOFF_HEADER_SESSION_RE.fullmatch(candidate.group(0).rstrip())
        if parsed is None:
            warning = (
                "같은-세션 판정 실패 — 마지막 handoff 헤더 차수 파싱 실패; "
                "보수적으로 새 entry를 append합니다 (이중 기록 가능)."
            )
        elif not normalized_num.isdigit():
            warning = (
                f"같은-세션 판정 실패 — 이번 실행 차수 {normalized_num!r} 파싱 실패; "
                "보수적으로 새 entry를 append합니다 (이중 기록 가능)."
            )
        elif int(parsed.group("num")) == int(normalized_num):
            target = candidate

    if target is not None:
        # entry 경계는 다음 canonical log entry 직전. 중첩 박제 목록의 `  - ## [...]`는
        # 두 칸 들여써 `_LOG_ENTRY_RE` 경계가 되지 않는다.
        next_entry = _LOG_ENTRY_RE.search(log_text, target.end())
        entry_end = next_entry.start() if next_entry is not None else len(log_text)
        entry_text = log_text[target.start():entry_end]
        blocks = list(_SESSION_ENTRIES_BLOCK_RE.finditer(entry_text))
        if len(blocks) == 1:
            original_block = blocks[0].group(0)
            block_newline = (
                "\r\n"
                if "\r\n" in original_block or original_block.endswith("\r")
                else "\n"
            )
            old_entries = [
                header.rstrip("\r")
                for header in _NESTED_SESSION_ENTRY_RE.findall(original_block)
            ]
            fresh_entries, _boundary_unresolved = _collect_session_entries(
                log_text, task, session, boundary_task=task,
            )
            # 기존 기계 목록 + 조기 handoff 뒤 새 complete/checkpoint를 원문 순서로 합친다.
            # dict는 insertion order를 보존해 재실행도 byte-멱등이다.
            entries = list(dict.fromkeys([*old_entries, *fresh_entries]))
            refreshed_block = (
                "- 이 세션 박제 entries:" + _session_entries_text(entries)
            )
            if block_newline != "\n":
                refreshed_block = refreshed_block.replace("\n", block_newline)
            # regex는 마지막 줄의 CR은 block에, LF는 suffix에 남긴다. CRLF block이면
            # replacement 끝의 CR도 되살려 경계 개행이 bare LF로 변하지 않게 한다.
            if original_block.endswith("\r") and not refreshed_block.endswith("\r"):
                refreshed_block += "\r"
            new_entry_text = (
                entry_text[:blocks[0].start()]
                + refreshed_block
                + entry_text[blocks[0].end():]
            )
            # ack 사유는 기존 entry 안에서 **기계 소유 줄 하나**로 멱등 upsert 한다 — 재실행이
            # 줄을 쌓지 않고, 박제할 자리가 없으면 DirtyAckStampError 로 fail-loud(무접촉 상태에서
            # 발생 — 이 함수는 계획만 만들고 write 는 호출부가 뒤에 한다).
            if dirty_ack_note:
                new_entry_text = upsert_dirty_ack_line(
                    new_entry_text, dirty_ack_note, block_newline
                )
            updated = (
                log_text[:target.start()] + new_entry_text + log_text[entry_end:]
            )
            return True, updated, refreshed_block, None
        warning = (
            "같은-세션 판정 실패 — 기존 handoff의 '이 세션 박제 entries' 구획 경계가 "
            f"모호함(marker {len(blocks)}개); 보수적으로 새 entry를 append합니다 "
            "(이중 기록 가능)."
        )

    skeleton = build_handoff_log_skeleton(
        session_num=normalized_num,
        date=date,
        session=session,
        log_text=log_text,
        task=task,
        dirty_ack_note=dirty_ack_note,
    )
    return False, "\n" + skeleton, skeleton, warning


def _build_new_session_entry(
    session_num: int | str,
    date_str: str,
    wave_summary: str,
) -> str:
    """새 pm 세션 entry 줄을 빌드한다 (줄바꿈 포함).

    형식: "  - **N차** (YYYY-MM-DD · <wave_summary>): <wave_summary>."
    """
    return (
        f"  - **{_normalize_session_num(session_num)}차** ({date_str} · {wave_summary}): {wave_summary}.\n"
    )


def update_session_window(
    pm_state_text: str,
    session_num: int | str,
    date_str: str,
    wave_summary: str,
) -> str:
    """pm_state.md 의 세션 식별 절에 sliding window 를 적용한 새 텍스트를 반환한다.

    - 신규 세션 entry 추가
    - 기존 entry가 이미 3개일 때만 가장 오래된 세션 entry 제거 (3 차 sliding window)
    - "이전 차 (PM N차~M차)" 포인터 줄 갱신

    앵커 불일치 시 ValueError (추측 편집 금지).
    """
    result = session_history._extract_session_section(pm_state_text)
    if result is None:
        raise ValueError(
            f"앵커 불일치: '{session_history._SESSION_SECTION_ANCHOR}' 가 pm_state.md 에서 발견되지 않았다."
        )
    section_text, start_offset, end_offset = result

    existing_entries = session_history._find_pm_session_entries(section_text)

    if len(existing_entries) == 0:
        # task 생성 시점 state는 완료 세션 0개가 정상이다. worktree_pool이 심은 명시 marker가
        # 있을 때만 첫 핸드오프를 1차 entry로 초기화한다. marker 없는 임의 변형은
        # fail-loud해 추측 편집 금지 경계를 보존한다.
        if identity_args.TASK_PM_STATE_EMPTY_MARKER not in section_text:
            raise ValueError(
                "앵커 불일치: 세션 식별 절에 기존 pm 세션 entry (- **N차** ...) 가 없다."
            )
        new_entry_line = _build_new_session_entry(session_num, date_str, wave_summary)
        new_section = section_text.replace(
            identity_args.TASK_PM_STATE_EMPTY_MARKER,
            new_entry_line.rstrip("\n"),
            1,
        )
        return (
            pm_state_text[:start_offset]
            + new_section
            + pm_state_text[end_offset:]
        )

    # 멱등성 검사 — 이미 해당 session_num entry 가 존재하면 no-op 으로 early-return.
    # 동일 session_num 재실행 시 entry 중복 추가 + 오래된 entry 의 이중 제거를 방지.
    target_num = int(str(session_num).replace("차", ""))
    existing_nums = [int(m.group(1).replace("차", "")) for m in existing_entries]
    if target_num in existing_nums:
        return pm_state_text

    # 창이 아직 차지 않았으면 기존 entry를 제거하지 않고 신규 entry만 붙인다. task 첫 handoff가
    # marker→1차로 바뀐 직후 2차 handoff에서 1차를 제거하면 window가 영원히 1개로 고정되어
    # 연속성이 끊긴다. 1→2→3차까지 공존시키고, **기존 3개 + 신규 1개**가 되는 4차부터만
    # 최고령을 밀어낸다.
    if len(existing_entries) < SLIDING_WINDOW_SIZE:
        new_entry_line = _build_new_session_entry(session_num, date_str, wave_summary)
        last_entry = existing_entries[-1]
        insert_pos = last_entry.end()
        if insert_pos < len(section_text) and section_text[insert_pos] == "\n":
            insert_pos += 1
        new_section = (
            section_text[:insert_pos]
            + new_entry_line
            + section_text[insert_pos:]
        )

        # legacy 템플릿은 첫 window entry와 같은 차수를 "이전 차" 포인터에도
        # 남긴 채 배포된 적이 있다. 아직 eviction 전이라도 새 entry를 추가한 뒤에는
        # 포인터가 현재 window와 겹치면 안 된다. 이 시점에는 창의 최소 차수보다 앞선
        # 세션이 없으므로, stale pointer 줄을 제거한다. 4차에서 첫 eviction이 발생하면
        # 아래의 기존 분기가 1차를 다시 이전 차 포인터로 만든다.
        new_window_min = min(existing_nums + [target_num])
        prev_pointer_match = re.search(
            r"^  - 이전 차 \(PM (.+?)\) = `.+?`[^\n]*\n?",
            new_section,
            re.MULTILINE,
        )
        if prev_pointer_match is not None:
            range_match = re.fullmatch(
                r"(\d+)차~(\d+)차", prev_pointer_match.group(1)
            )
            # 파싱할 수 있는 표준 범위만 손댄다. 비표준 포인터를 추측 편집하지 않는다.
            if range_match and int(range_match.group(2)) >= new_window_min:
                new_section = (
                    new_section[:prev_pointer_match.start()]
                    + new_section[prev_pointer_match.end():]
                )
        return (
            pm_state_text[:start_offset]
            + new_section
            + pm_state_text[end_offset:]
        )

    # 가장 오래된 entry (최소 차수) 를 제거 대상으로 선정
    oldest_entry = existing_entries[0]
    oldest_num = int(oldest_entry.group(1).replace("차", ""))

    # "이전 차 (PM N차~M차)" 포인터 줄 탐색
    prev_pointer_match = re.search(
        r"^  - 이전 차 \(PM (.+?)\) = `.+?`[^\n]*$",
        section_text,
        re.MULTILINE,
    )

    new_section = section_text

    # 1. 가장 오래된 entry 줄 제거
    # 줄 전체를 제거 (줄바꿈 포함)
    oldest_line_start = oldest_entry.start()
    oldest_line_end = oldest_entry.end()
    # 줄바꿈까지 포함
    if oldest_line_end < len(new_section) and new_section[oldest_line_end] == "\n":
        oldest_line_end += 1
    new_section = new_section[:oldest_line_start] + new_section[oldest_line_end:]

    # 2. 이전 차 포인터 줄 갱신 — 제거한 차수를 포함하도록 범위 확장
    if prev_pointer_match is not None:
        old_range_str = prev_pointer_match.group(1)  # 예: "11차~24차"
        # 기존 범위에서 끝 차수 파싱
        range_match = re.match(r"(\d+차)~(\d+차)", old_range_str)
        if range_match:
            old_end_str = range_match.group(2)  # 예: "24차"
            # 새 포인터: 범위는 그대로, 끝은 제거된 오래된 entry 차수로
            new_end_str = f"{oldest_num}차"
            new_range_str = f"{range_match.group(1)}~{new_end_str}"
        else:
            # 단순 케이스: 범위가 하나의 숫자인 경우
            new_range_str = f"{old_range_str}·{oldest_num}차"

        # 포인터 줄 치환 (이전 범위 → 새 범위)
        # 재탐색 필요 (section 이 변경됐으므로)
        new_pointer_match = re.search(
            r"^  - 이전 차 \(PM .+?\) = `.+?`[^\n]*$",
            new_section,
            re.MULTILINE,
        )
        if new_pointer_match is not None:
            old_pointer_line = new_pointer_match.group(0)
            # 괄호 전체 `(PM N차~M차)` 를 매치해 치환 — 괄호 없이 "PM .+? = " 를 쓰면
            # 닫힘 괄호까지 삼켜 `) ` 가 사라지는 버그가 생긴다.
            new_pointer_line = re.sub(
                r"\(PM .+?\)", f"(PM {new_range_str})", old_pointer_line
            )
            new_section = (
                new_section[:new_pointer_match.start()]
                + new_pointer_line
                + new_section[new_pointer_match.end():]
            )
    else:
        # 포인터 줄이 없는 경우 — 제거된 entry 가 있는 자리에 포인터 추가
        # "이전 차 (PM N차~N차) = log/current.md handoff entry 단일 진실." 형식으로 추가
        pointer_line = (
            f"  - 이전 차 (PM {oldest_num}차~{oldest_num}차) = "
            f"`log/current.md` handoff entry 단일 진실.\n"
        )
        # 섹션의 기존 entry 목록 마지막 위치 뒤에 추가
        new_section += pointer_line

    # 3. 신규 세션 entry 추가 — 기존 entry 목록의 마지막 줄 이후에 삽입
    new_entry_line = _build_new_session_entry(session_num, date_str, wave_summary)

    # 현재 entry 목록 마지막 위치 찾기 (재탐색)
    updated_entries = list(session_history._PM_SESSION_ANCHOR_RE.finditer(new_section))
    if updated_entries:
        last_entry = max(updated_entries, key=lambda m: int(m.group(1).replace("차", "")))
        insert_pos = last_entry.end()
        # 줄바꿈 뒤에 삽입
        if insert_pos < len(new_section) and new_section[insert_pos] == "\n":
            insert_pos += 1
        new_section = new_section[:insert_pos] + new_entry_line + new_section[insert_pos:]
    else:
        # 기존 entry 가 모두 제거된 경우 (새 entry 만 있는 경우) — 포인터 줄 앞에 추가
        pointer_search = re.search(
            r"^  - 이전 차 ", new_section, re.MULTILINE
        )
        if pointer_search:
            new_section = (
                new_section[:pointer_search.start()]
                + new_entry_line
                + new_section[pointer_search.start():]
            )
        else:
            new_section += new_entry_line

    # 섹션을 pm_state.md 에 교체
    new_pm_state_text = (
        pm_state_text[:start_offset]
        + new_section
        + pm_state_text[end_offset:]
    )
    return new_pm_state_text


def is_pytest_green(output: str, returncode: int = 0) -> bool:
    """pytest -q 출력이 green (passed 존재, failed 없음) 이면 True.

    passed/failed 는 **요약행 안에서만** 읽는다 — 캡처 로그의 `1 failed` 로 green 회귀를
    red 로 뒤집지 않는다.
    """
    if returncode != 0:
        return False
    line = pytest_report._pytest_summary_line(output)
    if line is None:
        return False
    if pytest_report._pytest_outcome_count(line, "failed") is not None:
        return False
    return pytest_report._pytest_outcome_count(line, "passed") is not None


def parse_pytest_summary(output: str) -> str:
    """pytest -q 출력에서 요약 라인을 추출한다 (`N passed` 부터 줄 끝까지).

    요약행이 없으면 현행대로 출력 꼬리 200자, 출력이 비면 빈 문자열.
    """
    summary = pytest_report._pytest_summary_tail(output)
    if summary is not None:
        return summary
    return output.strip()[-200:] if output.strip() else ""


_PYTEST_GATE_TOKEN = "pytest"


_DEFAULT_GATE_LABEL = "pytest tests/ -q -n auto"


def _gate_is_pytest(gate_cmd: str | None) -> bool:
    """해소된 회귀 명령이 pytest 스위트면 True.

    `None`(해소 실패) = 프레임워크 자기 회귀 기본값 = venv pytest argv 이므로 True.
    """
    return gate_cmd is None or _PYTEST_GATE_TOKEN in gate_cmd


def _gate_label(gate_cmd: str | None) -> str:
    """회귀 게이트를 사람이 읽는 한 줄로 (안내 출력용)."""
    return gate_cmd if gate_cmd else _DEFAULT_GATE_LABEL


def _regression_is_green(output: str, returncode: int, gate_cmd: str | None) -> bool:
    """회귀가 green 이면 True — 판정 기준은 게이트 종류가 정한다(`_gate_is_pytest`).

    - pytest 게이트 — 기존 요약행 판정(`is_pytest_green`) 100% 불변.
    - 비-pytest 게이트 — exit code 0 만 green(요약행 개념이 없다).
    """
    if _gate_is_pytest(gate_cmd):
        return is_pytest_green(output, returncode)
    return returncode == 0


_PENDING_PUSH_BASELINE_REFS = ("@{upstream}", "origin/main")


def _path_is_shipping(path: str) -> bool:
    """경로가 SHIPPING_GLOBS 중 하나라도 매칭하면 True (fnmatch 글롭)."""
    return any(fnmatch.fnmatch(path, glob) for glob in SHIPPING_GLOBS)


def flatten_ack_reason(reason: str) -> str:
    """ack 사유를 **단일행**으로 평탄화한다 — 개행/탭/연속 공백을 단일 공백으로.

    사유는 handoff entry 의 한 줄로 박히므로, CR/LF 가 살아 있으면 사유 문자열 하나로 가짜 entry
    줄(심지어 `## [날짜] handoff …` 헤더)을 위조할 수 있다. 값을 거부하는 대신 무해화해 CLI·엔진
    직접 호출 어느 경로로 들어와도 단일행 보장이 깨지지 않게 한다(단일 choke).
    """
    return " ".join(reason.split())


def _format_dirty_paths(paths: list[str]) -> list[str]:
    """차단/경고 메시지의 경로 열거 줄 목록 — DIRTY_GATE_LIST_LIMIT 초과분은 '… 외 N건' 으로 접는다."""
    ordered = sorted(paths)
    lines = [f"    {path}" for path in ordered[:DIRTY_GATE_LIST_LIMIT]]
    if len(ordered) > DIRTY_GATE_LIST_LIMIT:
        lines.append(f"    … 외 {len(ordered) - DIRTY_GATE_LIST_LIMIT}건")
    return lines


def build_dirty_ack_note(reason: str, dirty_count: int, *, auto: bool) -> str:
    """handoff entry 에 박제할 dirty-tree ack 사유 문장을 만든다 (**단일행 보장**).

    `auto`(사용자 명시 핸드오프 호출부 호환 신호)면 별도 dirty 사유가 없으므로 그 신호로
    차단을 강등했다는 사실 자체를 박는다. 명시 `--ack-dirty` 면 사람이 준 사유를 평탄화해
    그대로 싣고 override 수단을 병기한다. 둘 다 잔존 건수를 함께 남겨 다음 세션이 "몇 개를
    못 보고 넘어왔나"를 log 만 읽고 안다.
    """
    if auto:
        return (
            "사용자 명시 핸드오프 호환 신호 — "
            f"dirty {dirty_count}건 잔존 (`--auto-trigger`·차단하지 않음)."
        )
    return f"{flatten_ack_reason(reason)} (미커밋 {dirty_count}건 — `--ack-dirty` override)."
