"""보드 freshness와 티켓 원격 상태 표시 (층: cli)."""
from __future__ import annotations

from pm_engine.infra import board_git
import sys
from pm_engine.domain import ticket_rules


def _print_board_freshness(home) -> bool:
    """board freshness 1줄을 **stderr** 로 표면화하고 스냅샷 검증 여부를 돌려준다 (advisory).

    stdout 이 아니라 stderr 인 이유: `board list` stdout 를 파싱하는 소비처(회귀 파서·
    pm_bootstrap counts·아래 anti-degrade warn 과 동형)를 오염시키지 않는다. board 비-git이면 line=None → 무출력(조용히 생략·오탐 0).

    반환값은 **이 줄이 근거로 쓴 원격-추적 스냅샷이 실측 검증됐나**(TTL 이내 재사용 또는 이번
    fetch 성공)다 — 같은 흐름에서 이어지는 티켓 단위 대조가 검증되지 않은 캐시로 단정하지
    않게 한다. `list` 는 이 값을 쓰지 않는다(현행 동작 무변경)."""
    freshness = board_git._board_git_freshness(home)
    if freshness.line is not None:
        print(freshness.line, file=sys.stderr)
    return freshness.verified


def _print_ticket_remote_mismatch(home, ticket_id: str, local_status: str, *,
                                  snapshot_verified: bool) -> None:
    """이 티켓의 로컬 status ↔ 원격-추적 status 불일치를 1줄(stderr) 표면화한다.

    `show` 전용 추가 표면 — 전역 요약(`_print_board_freshness`)만으로는 "이 티켓" 이 stale
    한지 알 수 없다(다중 clone 에서 local open 인데 원격은 이미 done 인 사례가 실사고).
    판정은 claim 이 이미 쓰는 `_board_git_remote_ticket_status`(ls-tree·네트워크 0) 를
    그대로 재사용한다(새 판정 금지) — **fetch 는 새로 하지 않는다**: 직전
    `_print_board_freshness()` 호출이 TTL 가드(`_board_fetch_head_fresh`)를 통과했을 때만
    이미 advisory fetch 를 했으므로, 여기선 그 결과로 갱신된 원격-추적 ref 를 읽기만 한다.

    `snapshot_verified` 가 False 면 **판정 자체를 내지 않는다**(문구만 약하게 하지 않는다):
    원격-추적 ref 는 남아 있어도 이번 조회에서 검증되지 않은 캐시라, 그것으로 "로컬 사본
    stale" 을 말하면 판정 불능을 판정으로 위장하게 된다. 그 형상의 강등 진술은 전역
    freshness 줄("판정불가 — 스냅샷일 수 있음")이 이미 낸다 — 같은 사실을 두 줄로 말하지
    않는다.

    무출력(오탐 0) 조건: 스냅샷 미검증(fetch 실패·TTL 밖) · board 비-git · upstream 미설정 ·
    원격 판정 불가(`None`·원격에 그 티켓 없음 포함) · local_status 가 STATUS_DIRS 밖(draft 는
    board-git 추적 대상이 아님) · 로컬==원격. 이 경우들은 "판정불가/무관"일 뿐 "최신" 을
    주장하지 않으므로 오탐이 아니다.
    """
    if not snapshot_verified:
        return
    if local_status not in ticket_rules.STATUS_DIRS:
        return
    if not board_git._board_git_enabled(home):
        return
    upstream = board_git._board_git_upstream(home)
    if upstream is None:
        return
    remote_status = board_git._board_git_remote_ticket_status(home, upstream.tracking, ticket_id)
    if remote_status is None or remote_status == local_status:
        return
    behind = board_git._board_git_behind(home, upstream.tracking)
    # behind 계산 불가면 수치를 지어내지 않는다(F-003) — 불일치 사실 자체는 검증된
    # 스냅샷에서 읽었으므로 그대로 말한다.
    gap = f"behind {behind}" if behind is not None else "behind 판정불가"
    print(
        f"⚠ {ticket_id} 로컬 사본 stale — 로컬 status={local_status} · "
        f"원격-추적 status={remote_status}({gap}). "
        f"`board.py list` 로 board-git freshness 를 확인하거나 board pull 후 재조회하라.",
        file=sys.stderr,
    )
