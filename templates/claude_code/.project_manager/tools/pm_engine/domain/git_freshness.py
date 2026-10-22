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
