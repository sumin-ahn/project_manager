"""추가 리뷰어 프롬프트 조립 (층: app)."""
from __future__ import annotations

from pathlib import Path
from typing import Sequence
from pm_engine.infra.harness import delegate_env
import json
from pm_engine.domain import reviewer_budget
from pm_engine.infra import round_store



def build_prompt(
    home, ticket_body: str | None = None,
    adr_refs: list[str] | None = None,
    gate: str | None = None,
    *,
    tree: Path,
    base: str,
    target: str,
    changed_paths: Sequence[str],
    read_commands: Sequence[Sequence[str]],
    ticket_id: str | None = None,
    next_finding_id: str | None = None,
    confirmation_ids: Sequence[str] | None = None,
    ticket_bodies: Sequence[tuple[str, str]] | None = None,
    focus: str | None = None,
    versioned_block: bool = True,
) -> str:
    """검토 맥락과 리뷰 입력 좌표로 표준 프롬프트를 생성한다.

    `next_finding_id` 는 회수 대상 티켓에서 읽은 이 채널의 다음 ID 실값이고,
    `confirmation_ids` 는 그 티켓에서 확인할 수 있는 ID 실값 목록이다(빈 목록 = 확인 대상 없음).

    검토 대상은 티켓 하나(`ticket_body`)이거나 **묶음의 티켓 N**(`ticket_bodies`)이다 — 두
    채널이 이 한 조립기를 쓰고, 본문 절은 같은 표제로 티켓 수만큼 반복된다(크기 1 도 같은 경로).
    `focus` 는 PM 이 준 검토 중점 한 문단이다(내용은 PM 소유 · 엔진은 자리만 만든다).
    `versioned_block` 은 구조화 블록 요구를 이 프롬프트가 소유하는가다 — 산출이 라운드 파일이고
    그 시드가 이미 채널별 골격·다음 ID 를 들고 있는 경로에서는 끈다(요구가 두 벌이면 갈린다)."""
    parts: list[str] = [
        round_store._load_review_context(home).rstrip() + "\n\n",
        reviewer_budget._OUTPUT_FORMAT_BLOCK,
    ]
    if versioned_block:
        parts.append(reviewer_budget._versioned_block_requirement(next_finding_id, confirmation_ids))
    if adr_refs:
        parts.append(f"관련 ADR: {', '.join(adr_refs)}\n\n")
    if gate:
        parts.append(f"게이트 ticket: {gate}\n\n")
    bodies: list[tuple[str | None, str]] = list(ticket_bodies or ())
    if not bodies and ticket_body is not None:
        bodies = [(ticket_id or gate, ticket_body)]
    for label_value, body in bodies:
        ticket_label = f" ({label_value})" if label_value else ""
        parts.append(f"### 게이트 티켓 본문{ticket_label}\n")
        parts.append(body)
        if not body.endswith("\n"):
            parts.append("\n")
        parts.append("\n")
    if focus and focus.strip():
        parts.append("### PM 검토 중점\n")
        parts.append(focus.strip() + "\n\n")
    parts.append("### 리뷰 입력 좌표\n")
    parts.append(f"- 리뷰 대상 트리: {tree.resolve()} ({target})\n")
    parts.append(f"- 기준: {base}\n")
    parts.append(f"- 변경 파일 {len(changed_paths)}건: "
                 + json.dumps(list(changed_paths), ensure_ascii=False) + "\n\n")
    parts.append("### 리뷰 입력 확인\n")
    parts.append(
        "다음 명령으로 대상 트리의 diff를 직접 읽고 변경 파일과 필요한 주변 코드를 검토한다. "
        "출력이 길면 명령의 pathspec 범위 안에서 파일별로 나누어 읽는다. "
        "명령 실패나 트리 불일치는 변경 없음으로 처리하지 않고 판정 불가 사유로 보고한다. "
        "코드와 git 상태를 수정하지 않는다. "
        "`--no-index` 명령의 rc=1은 차이가 있음을 뜻한다.\n\n"
    )
    for argv in read_commands:
        parts.append("```sh\n" + delegate_env.render_shell_command(argv) + "\n```\n")
    return "".join(parts)
