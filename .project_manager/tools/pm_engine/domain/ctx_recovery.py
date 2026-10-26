"""컨텍스트 복구 문안·표시 상한과 ctx 사용률·밴드·회전 임계 순수 규칙."""
from __future__ import annotations

from pathlib import Path
import datetime
import re


_TASK_TAG_PREFIX = "task:"


SNAPSHOT_TIMEOUT_SECONDS = 3.0


SNAPSHOT_MAX_CHARS = 8_000


SNAPSHOT_MAX_BYTES = 24_000


_TODO_MAX_SHOWN = 8


_SNAPSHOT_IDENTITY_HEADING = "## PM 정체성 (compaction 복구)"


CTX_GUARD_REQUIRED_EXPRESSIONS = (
    "압축은 자동이고 세션은 그대로 이어진다",
    "핸드오프는 사용자 명시 지시로만 한다",
    "컨텍스트 잔량은 작업 범위·중단 결정의 입력이 아니며 checkpoint 기록은 진행 중 작업과 "
    "병행하고 진행 중 작업은 계속한다. 세션 종료·작업 축소는 사용자 지시로만 한다",
)


CTX_GUARD_FORBIDDEN_EXPRESSIONS = (
    "새 큰 작업보다 현재 서사 기록을 우선",
)


CTX_GUARD_CONTINUITY_GUIDANCE = (
    f"{CTX_GUARD_REQUIRED_EXPRESSIONS[0]}. checkpoint는 압축 후 서사 복구용이지 "
    f"종료 신호가 아니다. {CTX_GUARD_REQUIRED_EXPRESSIONS[1]}. "
    f"{CTX_GUARD_REQUIRED_EXPRESSIONS[2]}."
)


_PRECOMPACT_BREADCRUMB = (
    f"\n> ⚠ auto-compact 발생 — {CTX_GUARD_CONTINUITY_GUIDANCE}\n"
)


SNAPSHOT_HEARSAY_WARNING = (
    "## 전언 경고 (요약 속 단언은 미검증)\n"
    "- 압축 요약이 남긴 \"불가·없음·누구 몫·이미 됨\"은 근거가 지워진 전언이다. "
    "행동 전제로 삼기 전에 실측 1회.\n"
    "- 접근 경로·환경·선례는 CLAUDE.md 와 `python3 .project_manager/tools/pm_log.py tail`, "
    "log/current.md 검색으로 확인한다.\n"
    "- 이 절은 판단 재료다. 진행 중 작업은 계속한다.\n"
)


_INFLIGHT_UNHARVESTED_SHOWN = 3


_INFLIGHT_RAW_SHOWN = 3


_INFLIGHT_CLAIMED_SHOWN = 12


_INFLIGHT_SLOT_SHOWN = 4


_INFLIGHT_MAX_LINE_CHARS = 200


_INFLIGHT_MAX_LINES = 8


_WIP_PROBE_MAX_CALLS = 3


_WIP_PROBE_TIMEOUT_SECONDS = 1.0


CURRENT_HEADER = """\
# Project Log

> 프로젝트 운영 작업의 시간순 기록. Append-only. 활성 로그는 이 파일(`log/current.md`).
> 여러 세션/clone 이 동시에 append 해도 OK — `.gitattributes` 의 union merge 가 양쪽 entry 를 보존한다.
> 오래된 entry 는 `pm_log.py archive` 로 `log/archive/` 에 봉인된다.
> 형식: `## [YYYY-MM-DD] action | subject`
> Actions: create, update, decide (ADR), ticket, spec, split, handoff, checkpoint, lint
"""


def build_checkpoint_entry(
    task: str | None,
    trigger: str = "manual",
    date: str | None = None,
) -> str:
    """task 축의 compaction/manual 경계 보충 박제 골격을 만든다."""
    if date is None:
        date = datetime.date.today().isoformat()
    identity_tag = f" | ({_TASK_TAG_PREFIX}{task})" if task else ""
    return (
        # Consumer grammar: domain.handoff_text._collect_session_entries() 의 task 태그 판별과
        # 동기화한다.
        f"## [{date}] checkpoint{identity_tag} — {trigger}\n\n"
        "- 구간: <직전 박제 경계 이후>\n"
        "- 서사: <PM 손>\n"
    )


_INFLIGHT_CONTROL_CHAR_RE = re.compile(r"[\r\n\t\x00-\x1f\x7f]+")


def _inflight_safe_text(value: object) -> str:
    """임의 값을 렌더 안전한 단일 줄 문자열로 정규화한다(CR/LF·제어문자 → 공백 1개)."""
    return _INFLIGHT_CONTROL_CHAR_RE.sub(" ", str(value)).strip()


def _fold_join(items: list[str], shown: int, unit: str) -> str:
    """앞 ``shown`` 개만 나열하고 나머지는 "외 N<unit>" 로 접는다(절 길이 자기 상한 · tail-drop 비의존)."""
    head = items[:shown]
    text = " · ".join(head)
    remaining = len(items) - len(head)
    if remaining > 0:
        text += f" 외 {remaining}{unit}"
    return text


def _inflight_capped_line(*, prefix: str, content: str, suffix: str) -> str:
    """``content`` 만 잘라 줄 상한(``_INFLIGHT_MAX_LINE_CHARS``)을 지킨다(접두·명령 포인터는 보존)."""
    line = f"{prefix}{content}{suffix}"
    if len(line) <= _INFLIGHT_MAX_LINE_CHARS:
        return line
    budget = _INFLIGHT_MAX_LINE_CHARS - len(prefix) - len(suffix) - 1
    if budget <= 0:
        return line[:_INFLIGHT_MAX_LINE_CHARS]
    return f"{prefix}{content[:budget]}…{suffix}"


def _unharvested_copies_line(rows: list[dict]) -> str:
    """``delegate_ledger.ticket_copy_records(unharvested=True)`` 실값 → 미회수 라운드 준비 불릿."""
    prefix = "- 미회수 라운드 준비 "
    suffix = " → `python3 .project_manager/tools/pm_delegate.py ticket copies --unharvested`"
    if not rows:
        return _inflight_capped_line(prefix=prefix, content="0건", suffix=suffix)
    labels = [f"{row['ticket']} {row['role']}#{row['ordinal']}" for row in rows]
    content = f"{len(rows)}건: {_fold_join(labels, _INFLIGHT_UNHARVESTED_SHOWN, '건')}"
    return _inflight_capped_line(prefix=prefix, content=content, suffix=suffix)


def _unfinished_raw_line(rows: list[dict], ledger_path: Path) -> str:
    """``raw_output.unfinished_raw_records(lock=False)`` 실값 → 미마감 위임 raw 불릿(kill 증거)."""
    prefix = "- 미마감 위임 raw "
    suffix = " → `python3 .project_manager/tools/pm_delegate.py raw --unfinished`"
    if not rows:
        return _inflight_capped_line(
            prefix=prefix, content=f"0건 (장부: {ledger_path})", suffix=suffix,
        )
    labels = [
        f"{_inflight_safe_text(row.get('started_at'))} "
        f"{_inflight_safe_text(row.get('surface'))}/{_inflight_safe_text(row.get('harness'))}"
        for row in rows
    ]
    content = (
        f"{len(rows)}건: {_fold_join(labels, _INFLIGHT_RAW_SHOWN, '건')} (장부: {ledger_path})"
    )
    return _inflight_capped_line(prefix=prefix, content=content, suffix=suffix)


def _claimed_tickets_line(ticket_ids: list[str]) -> str:
    """claimed 티켓 디렉터리 glob 실값 → ID 목록 불릿(개수만이 아니라 목록으로 편입)."""
    prefix = "- claimed 티켓 "
    if not ticket_ids:
        return f"{prefix}0건"
    safe_ids = sorted(_inflight_safe_text(tid) for tid in ticket_ids)
    folded = _fold_join(safe_ids, _INFLIGHT_CLAIMED_SHOWN, "건")
    return _inflight_capped_line(prefix=prefix, content=f"{len(ticket_ids)}건: {folded}", suffix="")


def build_ctx_guard_guidance(
    band: str,
    *,
    used_pct: int | None = None,
    precompact_at_pct: int | None = None,
) -> str:
    """세 하네스가 공유하는 ctx nudge/compaction 안내문을 렌더한다.

    동적 사용률은 하네스 관측값(사용 한도 대비 쓴 %)을 그대로 표시하되, 세션 연속성 정책은
    :data:`CTX_GUARD_CONTINUITY_GUIDANCE` 한 상수에서만 온다. 밴드는 표현 강도만
    다르고 작업 중단·세션 종료를 유도하지 않는다. final 밴드는 압축 임박 경고 비율
    (`ctx.precompact_at_pct`)을 함께 적는다.
    """
    labels = {
        "nudge": "ctx-nudge",
        "nudge2": "ctx-nudge/강화",
        "final": "ctx-nudge/최종",
        "precompact": "ctx-checkpoint",
    }
    if band not in labels:
        raise ValueError(f"지원하지 않는 ctx guidance band: {band!r}")

    if band == "precompact":
        status = "compaction 경계가 감지됐다. "
        command = (
            "checkpoint 골격이 생성됐다면 현재 구간·서사 불릿을 보충한다. 골격이 없으면 "
            "`python3 .project_manager/tools/pm_log.py checkpoint --task <task> "
            "--trigger compaction`(Windows는 `py -3`)으로 기록한다. "
        )
    else:
        status = ""
        if used_pct is not None:
            status = f"컨텍스트 사용 {used_pct}%"
            if band == "final" and precompact_at_pct is not None:
                status += f" (압축 임박 경고 {precompact_at_pct}% 이상)"
            status += ". "
        checkpoint = "`python3 .project_manager/tools/pm_log.py checkpoint --task <task>"
        if band == "final":
            checkpoint += " --trigger compaction"
        checkpoint += "`"
        command = (
            "checkpoint 기록 권고 구간이다. "
            f"{checkpoint}(Windows는 `py -3`)으로 현재 구간·서사를 기록한다. "
            "`<task>`에는 현재 task 이름을 사용한다. "
        )
    return f"[{labels[band]}] {status}{command}{CTX_GUARD_CONTINUITY_GUIDANCE}"


# 2단 넛지 임계 마진(%p). nudge2 밴드 = max(precompact_at − 이 마진, checkpoint_at) <= 쓴 % <
# precompact_at 이다. 설정 키를 두지 않고 두 비율에서 파생한다.
CTX_NUDGE2_MARGIN_PCT = 3


# 세션 키 파일명 길이 상한 — ctx marker·회전 marker·멤버십 파일이 같은 규칙을 쓴다.
_SESSION_KEY_MAX_CHARS = 64


def context_used_pct(tokens: int, budget: int) -> int:
    """점유 토큰을 적용 한도 E 대비 쓴 % 정수(0~100)로 바꾼다.

    반올림은 Python ``round``(동률은 짝수 쪽)이다. 점유 0 은 미측정 sentinel 이라 0 이고, 한도 0 이하도
    판정할 신호가 없어 0 이다."""
    if tokens <= 0 or budget <= 0:
        return 0
    return max(0, min(100, round(tokens / float(budget) * 100)))


def nudge2_threshold(budget: dict) -> int:
    """2단 넛지 임계(쓴 %) — precompact_at − 마진, checkpoint_at 으로 하한."""
    return max(budget["precompact_at_pct"] - CTX_NUDGE2_MARGIN_PCT, budget["checkpoint_at_pct"])


def classify(used_pct: int, budget: dict) -> str:
    """쓴 % → ``ok`` | ``nudge`` | ``nudge2`` | ``stop``. 세 경계는 모두 그 값을 포함한다."""
    if used_pct >= budget["precompact_at_pct"]:
        return "stop"
    if used_pct >= nudge2_threshold(budget):
        return "nudge2"
    if used_pct >= budget["checkpoint_at_pct"]:
        return "nudge"
    return "ok"


def rotation_due(tokens: int, budget: int, precompact_at_pct: int) -> bool:
    """post-turn 회전 임계 도달 여부 — 원시 정수 곱 ``tokens*100 >= E*precompact_at_pct``.

    반올림 백분율을 쓰지 않는다. 점유 0 이하·한도 0 이하는 신호 없음이라 False 다."""
    if tokens <= 0 or budget <= 0:
        return False
    return tokens * 100 >= budget * precompact_at_pct


def safe_session_id(session_id: str) -> str:
    """세션 ID 를 marker 파일명 키로 — 영숫자·``-``·``_`` 만 남기고 64자로 자른다(빈 값은 unknown)."""
    safe = "".join(c for c in session_id.strip() if c.isalnum() or c in "-_")[:_SESSION_KEY_MAX_CHARS]
    return safe or "unknown"


def _snapshot_within_limits(text: str) -> bool:
    return len(text) <= SNAPSHOT_MAX_CHARS and len(text.encode("utf-8")) <= SNAPSHOT_MAX_BYTES


def _truncate_snapshot_text(text: str) -> str:
    """문자/UTF-8 경계를 깨지 않고 snapshot의 이중 상한 안으로 자른다(머리를 보존)."""
    text = text[:SNAPSHOT_MAX_CHARS]
    if len(text.encode("utf-8")) <= SNAPSHOT_MAX_BYTES:
        return text
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if len(text[:middle].encode("utf-8")) <= SNAPSHOT_MAX_BYTES:
            low = middle
        else:
            high = middle - 1
    return text[:low]


def _truncate_identity_keeping_suffix(identity: str, suffix: str) -> str:
    """identity 를 이중 상한 안으로 절단하되 always-keep 접두(``suffix``)는 뒤에 온전히 붙인다."""
    if not suffix:
        return _truncate_snapshot_text(identity.rstrip() + "\n")
    trimmed = identity.rstrip()
    combined = f"{trimmed}\n{suffix}" if trimmed else suffix
    if _snapshot_within_limits(combined):
        return combined
    low, high = 0, len(identity)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = identity[:middle].rstrip()
        probe = f"{candidate}\n{suffix}" if candidate else suffix
        if _snapshot_within_limits(probe):
            low = middle
        else:
            high = middle - 1
    trimmed_identity = identity[:low].rstrip()
    return f"{trimmed_identity}\n{suffix}" if trimmed_identity else suffix


def cap_snapshot_sections(
    identity: str, sections: list[str], *, required: int = 0,
) -> str:
    """뒤 절부터 통째로 덜고, 초대형 정체성 절은 안전 절단해 이중 상한을 지킨다.

    ``required`` 는 ``sections`` 앞 N 개(예: 전언 경고)를 tail-drop 대상에서 제외한다
    — 총량 cap 경로에서도 always-keep 절이 살아남는다(이전엔 identity 가
    거의 상한을 채운 상태에서 필수 접두까지 일반 절처럼 pop 돼 사라졌다 — 7,942-char identity
    프로브에서 hearsay_present=False 로 실측). 필수 접두를 포함해도 넘치면 identity 의 가변
    부분을 절단해 접두를 지킨다(``_truncate_identity_keeping_suffix``).
    """
    kept = list(sections)
    while len(kept) > required:
        text = "\n".join([identity, *kept]).rstrip() + "\n"
        if _snapshot_within_limits(text):
            return text
        kept.pop()
    required_text = "\n".join(kept).rstrip() + "\n" if kept else ""
    return _truncate_identity_keeping_suffix(identity, required_text)
