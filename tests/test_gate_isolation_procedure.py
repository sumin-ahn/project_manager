"""리뷰 격리 스냅샷의 소유자가 엔진임을 못박는 문서-가드.

배경 — **실패 모드가 운영적이다**: 리뷰 게이트가 dev 의 라이브 편집과 같은 working tree 를
공유하면 경합한다(실측 2회: 리뷰 false-red · 리뷰의 sensitivity `git checkout` ↔ dev 편집 실경합).
한동안은 PM 이 손으로 스냅샷을 만드는 표준 절차로 막았고, 그 손절차 자체가 stale-index·기점 stale
같은 새 false-green 클래스를 낳았다. 지금은 **묶음 리뷰 위임이 엔진에서 스냅샷을 만든다** —
장부가 선언한 묶음 브랜치 tip 에 결속하고, 커밋되지 않은 변경을 리뷰 입력으로 받지 않으며,
리뷰 뒤 정리까지 같은 실행이 한다.

이 가드가 지키는 것은 둘이다.
  (1) 카드·상세 문서가 **엔진 소유**를 서술한다(누가 만드는가·무엇에 결속하는가·언제 거부하는가).
  (2) 폐기된 **손절차가 되살아나지 않는다** — 손 worktree 생성·손 index 복사·손 제거 커맨드는
      문서에서 사라진 상태여야 한다(되살아나면 두 경로가 갈리고 옛 false-green 이 함께 돌아온다).

canonical 소스 = 루트 `.claude/skills/pm-dev-delegate/SKILL.md`(+ `references/operational-details.md`).
templates/* 사본은 pm_update 전파 후 어댑터 파리티 가드가 byte-동일로 보장하므로 여기서는
canonical 내용만 검사한다.

sensitivity — whole-file 검사는 vacuous('reviewer'·'git' 등이 카드 곳곳에 등장). 반드시 절
마커로 슬라이스해 그 안에서만 확인한다. 절이 통째로 제거되면 region 이 빈 문자열 → 모든 assert 가
fail-loud.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SKILL = REPO / ".claude" / "skills" / "pm-dev-delegate" / "SKILL.md"
DETAILS = SKILL.parent / "references" / "operational-details.md"

# 절 슬라이스 앵커 — 실 문서 헤딩과 byte-정확히 일치해야 함(드리프트 시 region 빔 → fail-loud).
ISOLATION_MARKER = "#### 리뷰 격리 스냅샷 (엔진 소유)"
REVIEWER_MARKER = "### code-reviewer 위임 (묶음 1회)"

# 폐기된 손절차 커맨드 — 문서 어디에도 되살아나면 안 된다.
RETIRED_HAND_COMMANDS = (
    "gate_snapshot.py",
    "git worktree add",
    "git checkout-index",
    "git worktree remove",
)

# 출하 doc 이 wikilink 하면 안 되는 framework-내부 ID (채택자 트리엔 부재 → dangling).
_FRAMEWORK_WIKILINK = re.compile(r"\[\[(ADR-\d+|T-\d+|idea-\d+)\]\]")


def _region(marker: str, source: Path, *, stops: tuple[str, ...] = ("\n## ",)) -> str:
    """`marker` 헤딩 ~ 다음 형제/상위 헤딩 직전까지 슬라이스.

    마커 부재면 빈 문자열(→ 호출측 assert fail-loud). `stops` 가 종료 헤딩 표기라, 상세 문서의
    '#### ' 절은 다음 '## ' 에서 멈추고(하위 서브섹션을 삼킨다), 카드의 '### ' 절은 다음 '### '
    형제 헤딩에서도 멈춘다(옆 절을 삼켜 판정이 vacuous 해지는 것을 막는다).
    """
    text = source.read_text(encoding="utf-8")
    idx = text.find(marker)
    if idx == -1:
        return ""
    rest = text[idx + len(marker):]
    ends = [pos for pos in (rest.find(stop) for stop in stops) if pos != -1]
    return marker + (rest if not ends else rest[:min(ends)])


def _flat(region: str) -> str:
    """줄바꿈으로 갈린 문구를 한 줄로 — 산문 가드는 줄 접힘에 걸리면 안 된다."""
    return " ".join(region.split())


def _isolation_region() -> str:
    return _region(ISOLATION_MARKER, DETAILS)


def _reviewer_region() -> str:
    return _region(REVIEWER_MARKER, SKILL, stops=("\n## ", "\n### "))


# ── (1) 절 존재 ──────────────────────────────────────────────────────────────

def test_details_have_engine_owned_isolation_section():
    """상세 문서에 엔진 소유 격리 스냅샷 절이 존재한다."""
    assert SKILL.is_file(), f"누락: {SKILL}"
    assert DETAILS.is_file(), f"누락: {DETAILS}"
    assert _isolation_region(), (
        f"{DETAILS.relative_to(REPO)} 에 격리 스냅샷 절 마커가 없음 — 소유자 서술 누락"
    )


def test_isolation_section_names_the_engine_as_the_owner():
    """누가 만드는가 — 엔진이고, PM 손절차는 없다고 명시한다."""
    region = _isolation_region()
    assert region, "격리 절 마커 부재"
    flat = _flat(region)
    assert "엔진이 만든다" in flat, "격리 절이 스냅샷 생성 주체를 엔진으로 명시하지 않음"
    assert "손절차는 없다" in flat, (
        "격리 절이 PM 손절차 부재를 명시하지 않음 — 손절차 부활의 문이 열린다"
    )


# ── (2) 결속·거부 조건 ───────────────────────────────────────────────────────

def test_isolation_section_states_branch_binding_and_refusal():
    """무엇에 결속하고 언제 거부하는가 — 묶음 브랜치 tip · 미커밋 변경 거부."""
    region = _isolation_region()
    assert region, "격리 절 마커 부재"
    required = [
        ("브랜치 결속", "묶음 브랜치 tip"),
        ("결속 판정 횟수", "두 번"),
        ("미커밋 거부", "커밋되지 않은 변경"),
        ("범위 기준", "merge-base"),
        ("송신 전 차단", "송신 전"),
    ]
    flat = _flat(region)
    missing = [name for name, token in required if token not in flat]
    assert not missing, f"격리 절에 결속·거부 서술 누락: {missing}"


def test_isolation_section_keeps_additional_reviewer_channel_separate():
    """추가 리뷰어 채널은 staged diff 축이라 이 스냅샷 축과 별개임을 명시한다."""
    region = _isolation_region()
    assert region, "격리 절 마커 부재"
    missing = [
        name for name, token in (
            ("추가 리뷰어 도구명", "additional_reviewer"),
            ("staged diff 근거", "staged diff"),
            ("별개 축 명시", "별개"),
        ) if token not in _flat(region)
    ]
    assert not missing, f"격리 절에 추가 리뷰어 채널 구분 누락: {missing}"


# ── (3) 폐기된 손절차 부활 금지 ──────────────────────────────────────────────

def test_retired_hand_snapshot_commands_do_not_return():
    """손 스냅샷 커맨드가 카드·상세 어디에도 없다 — 두 경로가 갈리면 옛 false-green 이 돌아온다."""
    for source in (SKILL, DETAILS):
        text = source.read_text(encoding="utf-8")
        revived = [token for token in RETIRED_HAND_COMMANDS if token in text]
        assert not revived, (
            f"{source.relative_to(REPO)} 에 폐기된 손 스냅샷 커맨드 잔존: {revived}"
        )


def test_reviewer_section_runs_the_engine_cluster_review():
    """리뷰 위임 절이 엔진 실행 한 줄을 실값으로 싣는다(손 조립 0)."""
    reviewer = _reviewer_region()
    assert reviewer, "code-reviewer 위임 절 마커 부재"
    required = [
        ("묶음 리뷰 실행", "--role code-reviewer"),
        ("묶음 인자", "--cluster"),
        ("검토 중점 주입", "--focus"),
        ("손 git 0 선언", "손 git 은 0이다"),
        ("프롬프트 조립 주체", "프롬프트**는 엔진이 조립한다"),
    ]
    flat = _flat(reviewer)
    missing = [name for name, token in required if token not in flat]
    assert not missing, f"리뷰 위임 절에 엔진 경로 서술 누락: {missing}"
    assert "--prompt-file" in reviewer, (
        "리뷰 위임 절이 `--prompt-file` 을 주지 않는다는 계약을 적지 않음"
    )


def test_reviewer_section_has_no_hand_written_prompt_block():
    """리뷰 위임 절에 손 프롬프트 블록이 없다 — 프롬프트 단일 진실은 엔진 조립기다."""
    reviewer = _reviewer_region()
    assert reviewer, "code-reviewer 위임 절 마커 부재"
    for marker in ("Agent 툴 호출:", "task tool 호출:", "spawn_agent("):
        assert marker not in reviewer, (
            f"리뷰 위임 절에 손 위임 블록 마커 잔존: {marker}"
        )


# ── (4) 출하 doc 위생 — placeholder·framework wikilink 0 ─────────────────────

def test_isolation_section_has_no_placeholder_or_wikilink():
    """격리 절에 미충전 mustache placeholder({{…}})·framework wikilink([[…]]) 가 없다.

    스킬은 채택자 트리로 전파돼 그대로 읽히므로 미충전 토큰·dangling wikilink 가 있으면 안 된다.
    (커맨드 자리표시 `<통합 브랜치>` 같은 angle-bracket 관용은 mustache/wikilink 가 아니라 허용.)
    """
    region = _isolation_region()
    assert region, "격리 절 마커 부재"
    assert "{{" not in region, "격리 절에 미충전 placeholder({{…}}) 잔존"
    hits = _FRAMEWORK_WIKILINK.findall(region)
    assert not hits, f"격리 절에 framework wikilink {hits} 잔존 — plain text 로"
