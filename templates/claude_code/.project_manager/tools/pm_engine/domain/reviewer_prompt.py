"""추가 리뷰 입력 선택 (층: domain)."""
from __future__ import annotations

from typing import NamedTuple
from typing import Sequence
from pm_engine.domain import round_rules


class TicketBodySelection(NamedTuple):
    """리뷰 입력 티켓 본문 — 명세 + 선별한 라운드, 그리고 접어 둔 산출 라운드 수."""

    text: str
    omitted_rounds: int


# 생략 표기 한 줄 — 리뷰어가 "이 티켓에 라운드가 더 있다"는 사실만 알면 된다(순번·크기는 board
# 에서 본다). 상한이 없으므로 정보성이다.
_TICKET_BODY_OMISSION_LINE = "(생략한 라운드 {n}개 — 역할별 마지막 산출만 싣습니다)"


def _select_ticket_body_for_review(body: str, rounds: Sequence) -> TicketBodySelection:
    """명세 전문 + **역할별 마지막 산출 라운드**를 순번 순으로 이어 붙인다.

    명세(`body`)는 권위 절과 PM 판정 블록을 그대로 담은 파일이라 전량 싣는다. 라운드는 파일
    하나가 산출 하나라 선별이 파일 선택으로 끝난다 — 역할마다 마지막 것만 싣고, 시드 그대로인
    라운드(산출 없음)는 세지도 싣지도 않는다. 이어 붙이는 표기는 `round_rules.render_rounds_for_show`
    가 소유한다(사람이 `board.py show` 에서 읽는 그 구분선과 같은 규칙 하나).

    라운드가 없으면 명세 원문을 **바이트 그대로** 돌려준다 — 라운드 없는 티켓의 입력이 이 선별
    도입 전후로 동일해야 한다.
    """
    landed = [item for item in rounds if not item.pending]
    last_by_role: dict[str, int] = {}
    for item in landed:
        last_by_role[item.role] = max(item.ordinal, last_by_role.get(item.role, 0))
    selected = sorted(
        (item for item in landed if last_by_role[item.role] == item.ordinal),
        key=lambda item: item.ordinal,
    )
    # 생략 = **접힌 산출**의 수다. 산출 없는 라운드는 애초에 실을 것이 없으므로 세지 않는다 —
    # 세면 접힌 산출이 없는 티켓에서도 "생략" 과 선별 요약 헤더가 붙는다.
    omitted = len(landed) - len(selected)
    if not selected and not omitted:
        return TicketBodySelection(body, 0)

    parts = [body if body.endswith("\n") else body + "\n"]
    if omitted:
        parts.append("\n" + _TICKET_BODY_OMISSION_LINE.format(n=omitted) + "\n")
    if selected:
        parts.append("\n" + round_rules.render_rounds_for_show(selected))
    return TicketBodySelection("".join(parts), omitted)


# 스폰면 경고 문구 — 준비면(`round_seed._warn_unharvested_developer_round`)과 같은 축을
# 쓰되, 문구는 다르다. 형상 B(앞선 산출 라운드가 있고 최신만 시드)에서 준비면 문구를 그대로
# 복제하면 "리뷰어 입력에 실리지 않습니다"가 거짓이 된다 — 이 표면은 그렇게 단정하지 않고, 이번
# 프롬프트에 실제로 실리는 developer 산출 라운드 이름(없으면 "없음")을 값으로 말한다.
_SPAWN_SEED_DEVELOPER_ROUND_WARNING = (
    "경고: 추가 리뷰어가 {ticket} 의 산출 없는 developer 라운드 위에서 스폰됩니다 — "
    "{name} 이 시드 골격 그대로입니다. 이번 프롬프트에 실리는 developer 산출 라운드: {latest}."
)
