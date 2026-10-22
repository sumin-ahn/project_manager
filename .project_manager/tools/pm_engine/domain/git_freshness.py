"""git freshness 순수 판정과 표시 (층: domain)."""
from __future__ import annotations


def parse_git_ahead_behind(rev_list_output: str) -> tuple[int, int] | None:
    """`git rev-list --left-right --count HEAD...@{u}` 출력에서 (ahead, behind) 를 파싱한다.

    출력 형식: `"<ahead>\\t<behind>"` (좌=로컬만·ahead, 우=upstream만·behind). 형식
    불일치(빈 문자열·탭 없음 등) → None(파싱 실패 — 호출부가 upstream 미설정과 동일하게
    graceful 취급).
    """
    stripped = rev_list_output.strip()
    if not stripped:
        return None
    parts = stripped.split()
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def _behind_warning(scope: dict) -> str:
    """behind>0 인데 자동 pull 불가한 scope 의 경고 문자열 + 차단 사유.

    사유: fetch 실패(stale) · status 미확인(clean 미증명) · dirty · diverged(ahead>0).
    """
    behind = scope.get("behind")
    reasons: list[str] = []
    if not scope.get("fetched"):
        reasons.append("fetch 실패")
    if scope.get("dirty") is None:
        reasons.append("status 미확인")
    elif scope.get("dirty"):
        reasons.append("dirty")
    if scope.get("ahead") is None:
        reasons.append("ahead 미확인")
    elif scope["ahead"] > 0:
        reasons.append(f"ahead {scope['ahead']} diverged")
    reason = f" ({', '.join(reasons)})" if reasons else ""
    return f"⚠ behind {behind} — 수동 동기 필요{reason}"


def _format_freshness(scope: dict) -> str:
    """freshness scope 한 줄 표기 — fetch/behind/ahead/상태 + 동기·경고 노트.

    offline(fetch 실패)이면 origin 대비 실측을 못 하므로 remote-tracking 스냅샷을 "최신" 으로
    주장하지 않는다 — behind 0·upstream 미상은 "판정불가 — 스냅샷일 수 있음" fail-soft
    stale board 를 "최신" 으로 오신뢰하는 사고 차단. behind>0 은
    fetch 성공/실패 무관하게 로컬이 이미 아는 뒤처짐이라 그대로 표기(경고 note 는 별도 병기).
    """
    parts: list[str] = []
    fetched = scope.get("fetched")
    if not fetched:
        parts.append("⚠ fetch 실패 (`git fetch origin` · 현행 유지)")
    if scope.get("detached"):
        parts.append("detached HEAD")
    else:
        behind = scope.get("behind")
        ahead = scope.get("ahead") or 0
        if behind is None:
            # upstream 미설정/조회불가 — offline 이면 판정불가로 감싼다(fail-soft).
            parts.append("판정불가 — 스냅샷일 수 있음" if not fetched else "upstream 없음")
        elif behind == 0 and ahead == 0:
            # online 이면 진짜 최신, offline 이면 실측 못 한 stale 스냅샷일 뿐(판정불가).
            parts.append("판정불가 — 스냅샷일 수 있음" if not fetched else "최신")
        else:
            parts.append(f"behind {behind} / ahead {ahead}")
    if scope.get("note"):
        parts.append(scope["note"])
    return " · ".join(parts)


def freshness_decision(
    *, fetched: bool, detached: bool, dirty: bool | None, ahead: int | None, behind: int | None
) -> tuple[str, bool]:
    """fetch 후 scope 상태 문자열 + ff-pull 수행 여부를 판정한다 (순수 함수·자동 pull 안전 게이트).

    - detached HEAD → ("detached", False) — 재부착은 PM 판단·자동 pull 안 함.
    - behind None (upstream 미설정/조회불가) → ("upstream 없음", False).
    - behind 0 · ahead 0 → ("최신", False).
    - behind 0 · ahead>0 → ("ahead-only", False) — 로컬만 앞섬(push 대기·pull 불요).
    - behind>0 · **안전조건 전부 충족** → ("동기", True) — 자동 ff-pull.
    - behind>0 · 안전조건 위반 → ("수동 동기 필요", False) — 표면화만.

    **자동 ff-pull 안전조건**(codex must-fix — 하나라도 어기면 자동 pull 금지):
      `fetched is True` — freshness 를 *실측*(fetch 성공)한 상태여야 한다. fetch 실패면
         behind/ahead 는 stale 원격 데이터라 그 위에서 pull 하면 "실측 후 clean·ff" 전제가 거짓.
      `dirty is False` — working tree 가 *확정 clean*. `git status` 조회 실패(dirty=None·
         미확인)는 clean 을 증명 못 하므로 자동 pull 불가(경고만).
      `ahead == 0` — 로컬 커밋 없음 *확정*(ff 가능·diverged 아님). `ahead=None`(미확인)은
         0 취급하지 않는다 — 증명 없는 pull 금지(codex round-2 must-fix).
    """
    if detached:
        return "detached", False
    if behind is None:
        return "upstream 없음", False
    ahead_n = ahead or 0
    if behind == 0:
        return ("ahead-only", False) if ahead_n > 0 else ("최신", False)
    # behind > 0 — 안전조건 전부 충족(fetch 성공·확정 clean·ff 확정)일 때만 자동 동기.
    # `ahead == 0` 엄격 비교: None(미확인)은 False — ahead_n 폴백을 여기 쓰면 미확인이 0 으로 위장.
    if fetched and dirty is False and ahead == 0:
        return "동기", True
    return "수동 동기 필요", False
