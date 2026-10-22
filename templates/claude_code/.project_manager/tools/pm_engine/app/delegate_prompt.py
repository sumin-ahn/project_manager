"""위임 역할과 산출 좌표 안내 (층: app)."""
from __future__ import annotations

from collections.abc import Sequence
from pm_engine.domain import delegate_prompt_rules
from pm_engine.domain import delegate_rounds
from pm_engine.domain import review_contract
from pm_engine.domain import review_form



def _role_preamble(role: str, adapter_directories: Sequence[str]) -> str:
    lines = [delegate_prompt_rules._ROLE_IDENTITIES[role], delegate_prompt_rules._prohibition(adapter_directories)]
    if role in delegate_prompt_rules.REGRESSION_SCOPE_ROLES:
        lines.append(delegate_prompt_rules.REGRESSION_SCOPE_PREAMBLE)
    if role == review_form.INTERNAL_REVIEW_ROLE:
        lines.append(review_contract._internal_review_format_preamble())
    return "\n".join(lines)


def _ticket_copy_preamble(plan: delegate_rounds.TicketCopyPlan) -> str:
    """이 위임의 산출 자리 안내 — 좌표도 다음 finding ID 도 준비가 확정한 실값이다.

    리뷰 채널은 라운드마다 fresh 라 이전 라운드가 쓴 번호를 모른다. 시드에만 값을 실으면 사본을
    열기 전에 프롬프트만 읽는 위임에서 번호가 추측이 된다 — 두 자리가 같은 값을 말해야 한다.
    """
    seat = (
        f"라운드 산출 기록: 이 위임의 산출은 {plan.path} **하나에만** 쓴다"
        f"(첫 줄 헤더는 그대로 두고 그 아래 골격을 채운다). 같은 디렉터리의 "
        f"`{delegate_rounds.TICKET_COPY_SPEC_NAME}`(티켓 명세)와 `{delegate_rounds.TICKET_COPY_ROUNDS_DIRNAME}/`"
        "(이전 라운드)는 **읽기 전용**이다. PM 홈 티켓은 편집하지 마라. "
        "이 파일은 응답과 별개로 종료 시 기계 회수된다."
    )
    if not plan.next_finding_id:
        return seat
    return seat + "\nfinding ID: " + review_form.NEXT_FINDING_ID_RULE.format(
        next_id=plan.next_finding_id,
    )


def _with_ticket_copy_preamble(
    prompt: str, ticket_copy: delegate_rounds.TicketCopyPlan | None,
    cluster_plan: "delegate_rounds.ClusterCopyPlan | None" = None,
) -> str:
    """full/resume delta/fallback/fresh 모든 wire가 이번 run의 같은 사본 지시를 받는 seam.

    자리가 N 이면 N 자리 안내가 붙는다 — 붙이는 쪽과 준비한 쪽이 같은 판정을 쓴다.
    """
    note = _round_copy_preamble(ticket_copy, cluster_plan)
    if note is None or note in prompt:
        return prompt
    return note + "\n\n" + prompt


def _cluster_copy_preamble(plan: delegate_rounds.ClusterCopyPlan) -> str:
    """묶음 산출 자리 안내 — 티켓마다 자기 라운드 파일 하나다(좌표는 엔진이 준다).

    다음 finding ID 도 티켓마다 준비가 확정한 실값을 싣는다(`_ticket_copy_preamble` 과 같은
    규칙) — 리뷰 세션은 fresh 라 프롬프트에 없으면 번호가 추측이 된다.
    """
    seats = "\n".join(
        f"- {round_plan.ticket}: {round_plan.path}"
        + (
            "\n  finding ID: "
            + review_form.NEXT_FINDING_ID_RULE.format(next_id=round_plan.next_finding_id)
            if round_plan.next_finding_id else ""
        )
        for round_plan in plan.rounds
    )
    return (
        "라운드 산출 기록: 이 위임의 산출은 아래 티켓별 라운드 파일에만 쓴다"
        "(첫 줄 헤더는 그대로 두고 그 아래 골격을 채운다). 티켓 하나에 대한 지적은 그 티켓의 "
        "파일에만 적는다.\n"
        f"{seats}\n"
        f"같은 디렉터리의 `{delegate_rounds.TICKET_COPY_SPEC_NAME}`(티켓 명세)와 "
        f"`{delegate_rounds.TICKET_COPY_ROUNDS_DIRNAME}/`(이전 라운드)는 **읽기 전용**이다. PM 홈 티켓은 "
        "편집하지 마라. 이 파일들은 응답과 별개로 종료 시 기계 회수된다."
    )


def _round_copy_preamble(
    ticket_copy: delegate_rounds.TicketCopyPlan | None, cluster_plan: delegate_rounds.ClusterCopyPlan | None,
) -> str | None:
    """이 실행의 산출 자리 안내 — 자리가 N 이면 N 을 적는다(자리 없으면 None).

    쓰는 쪽(준비)과 읽는 쪽(프롬프트)이 이 한 함수를 지나야 "하나만 쓰라"는 안내와 실제로
    깔린 자리 수가 갈리지 않는다.
    """
    if cluster_plan is not None:
        return _cluster_copy_preamble(cluster_plan)
    if ticket_copy is not None:
        return _ticket_copy_preamble(ticket_copy)
    return None
