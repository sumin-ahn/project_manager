"""추가 리뷰어 프롬프트 조립 (층: app)."""
from __future__ import annotations

import pm_engine.domain.reviewer_prompt
from pm_engine.infra import pm_paths
from pm_engine.domain import round_rules
from pm_engine.app import round_seed
import sys

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


def _load_ticket_rounds_for(
    home, ticket_id: str, *, ticket_text: str,
) -> list:
    """티켓의 라운드 목록을 순번 순으로 읽는다 (라운드 없으면 빈 목록).

    라운드 디렉터리 규약 위반은 조용히 건너뛰지 않고 본문 조립 실패와 **같은 깔때기**로 올린다
    — 리뷰 입력이 반쪽이 된 채로 나가면 안 된다(호출부가 loud 하게 멈춘다).
    """
    try:
        return round_seed.load_rounds(
            pm_paths.tickets_dir(home), ticket_id, ticket_text=ticket_text,
        )
    except round_rules.RoundsError as exc:
        raise pm_paths.AnchorResolutionError(
            f"티켓 라운드 사이드카가 손상돼 리뷰 입력을 조립할 수 없습니다: {exc}"
        ) from exc


def _warn_seed_developer_round(rounds: Sequence, *, ticket: str) -> None:
    """추가 리뷰어 스폰면의 loud 경고 — 경고이지 거부가 아니다(rc 는 바꾸지 않는다).

    판정은 준비면(`round_seed._warn_unharvested_developer_round`)이 쓰는 공개 함수
    `round_seed.unharvested_developer_round` 를 그대로 불러 쓴다 — 기준이 둘이면 두 표면이
    어긋난다. `prepare_ticket_body` 한 seam 이 미리보기(`--dry-run`)와 실 스폰을 모두
    지나므로 이 호출 하나가 두 채널을 함께 덮는다.
    """
    stale = round_seed.unharvested_developer_round(rounds)
    if stale is None:
        return
    name = round_rules.round_filename(stale.ordinal, stale.role)
    latest = round_rules.latest_round_of_role(rounds, round_seed.REVIEW_SUBJECT_ROLE)
    latest_name = (
        round_rules.round_filename(latest.ordinal, latest.role)
        if latest is not None else "없음"
    )
    print(
        pm_engine.domain.reviewer_prompt._SPAWN_SEED_DEVELOPER_ROUND_WARNING.format(
            ticket=ticket, name=name, latest=latest_name,
        ),
        file=sys.stderr,
    )
