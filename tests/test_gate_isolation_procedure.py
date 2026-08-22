"""T-0410 — 병렬 게이트 전용 worktree 격리 절차 문서-가드.

배경 — **실패 모드가 운영적이다**: 병렬 wave 에서 내부 reviewer 게이트가 dev 의 라이브 편집과
같은 working tree 를 공유하면 경합한다(2회 실측: PM 2차 T-0389 리뷰 false-red · PM 3차 T-0402
리뷰 sensitivity `git checkout` ↔ T-0409 dev 편집 실경합). 지식/프롬프트 완화("git 조작 금지")
하나로는 재발을 막지 못해, pm-dev-delegate 스킬의 code-reviewer 위임 절에 **격리 스냅샷 표준
절차**(staged 상태를 별도 worktree 로 스냅샷 → reviewer 에 주입 → 제거)를 명문화했다. 이 가드는
그 문서화된 절차·커맨드 문법·완화 문구 정합이 drift 하지 않게 못박는다(지식 아닌 절차의 회귀 보호).

canonical 소스 = 루트 `.claude/skills/pm-dev-delegate/SKILL.md`. templates/* 사본은 pm_update
전파 후 `test_claude_adapter_parity.py::test_adapter_artifacts_byte_identical` 이 byte-동일로
보장(전파는 그 파리티 가드가 소유)하므로, 이 가드는 canonical 내용만 검사한다 — 전파 미완 상태에서
지정 서브셋만 돌려도 green 이고, 전파 후 파리티 가드가 미러 정합을 백스톱한다.

sensitivity — whole-file 검사는 vacuous('reviewer'·'git' 등이 스킬 곳곳에 등장). 반드시 절
마커(#### 게이트 격리 스냅샷 ~ 다음 '## ' 헤딩)로 슬라이스해 그 안에서만 어휘/커맨드를 확인한다
(T-0337 section-slice 동형). 절이 통째로 제거되면 region 이 빈 문자열 → 모든 assert 가 fail-loud.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SKILL = REPO / ".claude" / "skills" / "pm-dev-delegate" / "SKILL.md"
DETAILS = SKILL.parent / "references" / "operational-details.md"

# 절 슬라이스 앵커 — 실 SKILL.md 헤딩과 byte-정확히 일치해야 함(드리프트 시 region 빔 → fail-loud).
ISOLATION_MARKER = "#### 게이트 격리 스냅샷 (병렬 wave · 내부 reviewer 전용)"
REVIEWER_MARKER = "### code-reviewer 위임"

# 출하 doc 이 wikilink 하면 안 되는 framework-내부 ID (채택자 트리엔 부재 → dangling · T-0090).
_FRAMEWORK_WIKILINK = re.compile(r"\[\[(ADR-\d+|T-\d+|idea-\d+)\]\]")


def _region(marker: str, source: Path = SKILL) -> str:
    """SKILL.md 에서 `marker` 헤딩 ~ 다음 '## ' 섹션 헤딩 직전까지 슬라이스.

    마커 부재면 빈 문자열(→ 호출측 assert fail-loud). '## '(2-hash) 헤딩에서 종료하므로 하위
    '#### ' 서브섹션은 삼키고 다음 최상위 섹션에서 멈춘다.
    """
    text = source.read_text(encoding="utf-8")
    idx = text.find(marker)
    if idx == -1:
        return ""
    rest = text[idx:]
    end = rest.find("\n## ")
    return rest if end == -1 else rest[:end]


def _isolation_region() -> str:
    return _region(ISOLATION_MARKER, DETAILS)


def _reviewer_region() -> str:
    # code-reviewer 위임 절 전체(프롬프트 + codex 노트 + 격리 서브섹션) — 완화↔격리 정합 검사용.
    return _region(REVIEWER_MARKER)


# ── (1) 절 존재 + 실행 가능한 형태 ────────────────────────────────────────────

def test_skill_has_gate_isolation_section():
    """SKILL.md code-reviewer 절에 게이트 격리 스냅샷 표준 절차 절이 존재한다."""
    assert SKILL.is_file(), f"누락: {SKILL}"
    assert DETAILS.is_file(), f"누락: {DETAILS}"
    region = _isolation_region()
    assert region, (
        f"{SKILL.relative_to(REPO)} 에 격리 스냅샷 절 마커가 없음 — "
        f"병렬 게이트 격리 표준 절차 누락 (T-0410)"
    )


def test_gate_isolation_states_three_step_flow():
    """격리 절이 생성→주입→제거 3단계 흐름을 명시한다 (인터페이스: 3단계 실 커맨드)."""
    region = _isolation_region()
    assert region, "격리 절 마커 부재 (T-0410)"
    missing = [step for step in ("생성", "주입", "제거") if step not in region]
    assert not missing, (
        f"격리 절에 3단계 흐름 누락: {missing} — 생성→주입→제거 표준 절차를 명시해야 함 (T-0410)"
    )


def test_gate_isolation_has_executable_bash_block():
    """격리 절이 prose 만이 아니라 실행 가능한 ```bash 커맨드 블록을 담는다 (sensitivity).

    빈 마커·서술만으로 통과하는 공허 가드를 막는다 — 절차 커맨드가 실제로 실려 있어야 한다.
    """
    region = _isolation_region()
    assert region, "격리 절 마커 부재 (T-0410)"
    assert "```bash" in region, (
        "격리 절에 실행 가능한 ```bash 커맨드 블록이 없음 — 절차가 prose 로만 남으면 복붙 불가 (T-0410)"
    )


# ── (2) 커맨드 문법 유효 (gate_snapshot 도구 호출 + worktree remove) ──────────

def test_gate_isolation_worktree_commands_present():
    """격리 절이 기계 절차의 커맨드를 담는다 — 생성은 엔진 도구 `gate_snapshot.py`
    (--repo/--output/--paths·신선도 검증 내장) · 제거는 `git worktree remove`.

    수동 2-커맨드(worktree add + checkout-index)는 stale-index false-green 클래스라 도구가
    대체했다 — 그 커맨드가 되살아나면 이 가드가 잡는다(아래 부재 단언).
    """
    region = _isolation_region()
    assert region, "격리 절 마커 부재 (T-0410)"
    required = [
        ("스냅샷 도구 호출", "gate_snapshot.py"),
        ("공유 트리 인자", "--repo"),
        ("출력 경로 인자", "--output"),
        ("검토 경로 인자", "--paths"),
        ("worktree remove", "git worktree remove"),
    ]
    missing = [name for name, token in required if token not in region]
    assert not missing, (
        f"격리 절에 커맨드 문법 누락: {missing} — "
        f"gate_snapshot.py --repo/--output/--paths + git worktree remove 를 명시해야 함 (T-0410)"
    )
    # 수동 절차 부활 금지 — 도구가 닫은 false-green 클래스의 재유입 백스톱.
    revived = [t for t in ("git worktree add", "git checkout-index") if t in region]
    assert not revived, (
        f"격리 절에 수동 스냅샷 커맨드가 되살아남: {revived} — 생성은 gate_snapshot.py 단일 경로여야 함"
    )


def test_gate_isolation_add_precedes_remove():
    """커맨드가 올바른 순서(생성=도구 호출이 제거=remove 앞)로 문서화됐다."""
    region = _isolation_region()
    assert region, "격리 절 마커 부재 (T-0410)"
    create_at = region.find("gate_snapshot.py")
    remove_at = region.find("git worktree remove")
    assert create_at != -1 and remove_at != -1, "생성 도구/remove 커맨드 부재 (T-0410)"
    assert create_at < remove_at, (
        "격리 절 커맨드 순서 역전 — 생성(gate_snapshot.py)이 제거(worktree remove) 앞에 와야 함 (T-0410)"
    )


# ── (3) 대상 = 내부 reviewer, codex external_review 는 비대상 ──────────────────

def test_gate_isolation_targets_internal_reviewer_not_codex():
    """격리 절이 대상을 내부 reviewer 로 명시하고 codex external_review 는 비대상으로 못박는다.

    결정: 격리 대상 = working tree 를 읽는 내부 reviewer. codex external_review 는 staged diff
    기반이라 이미 스냅샷-안정 → 대상 아님(명시).
    """
    region = _isolation_region()
    assert region, "격리 절 마커 부재 (T-0410)"
    assert "내부 reviewer" in region, (
        "격리 절이 대상을 '내부 reviewer' 로 명시하지 않음 (T-0410)"
    )
    codex_exclusion = [
        ("codex 도구명", "external_review"),
        ("staged diff 근거", "staged diff"),
        ("비대상 명시", "대상 아님"),
    ]
    missing = [name for name, token in codex_exclusion if token not in region]
    assert not missing, (
        f"격리 절에 codex external_review 비대상 근거 누락: {missing} — "
        f"staged diff 기반이라 스냅샷-안정=격리 대상 아님을 명시해야 함 (T-0410)"
    )


# ── (4) 비병렬 위임은 격리 선택 ──────────────────────────────────────────────

def test_gate_isolation_is_optional_for_non_parallel_delegation():
    """격리 절이 비병렬 리뷰는 격리 선택(종전대로)임을 명시한다 (결정).

    판정 축은 **병렬 여부**다 — 슬롯이 몇 개인 홈인지가 아니라 같은 트리를 dev 가 라이브로
    편집 중인지가 격리의 이유다.
    """
    region = _isolation_region()
    assert region, "격리 절 마커 부재 (T-0410)"
    missing = [name for name, token in (("비병렬", "비병렬"), ("선택", "선택")) if token not in region]
    assert not missing, (
        f"격리 절에 비병렬=격리 선택 명시 누락: {missing} — 비병렬 리뷰는 종전대로임을 명시해야 함 (T-0410)"
    )


# ── (5) 완화 문구(git 조작 금지) 정합 — 이중 방어 병행 ────────────────────────

def test_mitigation_git_prohibition_present_in_reviewer_delegation():
    """완화책(리뷰 프롬프트 git 조작 금지 명시)이 code-reviewer 위임 절에 실려 있다 (결정: 병행 유지)."""
    reviewer = _reviewer_region()
    assert reviewer, "code-reviewer 위임 절 마커 부재 (T-0410)"
    assert "git 조작 금지" in reviewer, (
        "code-reviewer 위임 절에 'git 조작 금지' 완화 문구가 없음 — 격리와 병행 유지해야 함 (T-0410)"
    )


def test_gate_isolation_declares_dual_defense_with_mitigation():
    """격리 절이 완화(프롬프트 git 조작 금지)와 병행하는 이중 방어임을 명시한다 (완화 문구 정합).

    격리(절차)와 완화(프롬프트)는 병행 유지 — 절차가 경합을 구조적으로 막고, 프롬프트가 사고성
    git 조작을 막는다. 둘 중 하나만 남고 다른 하나가 drift 하는 것을 이 가드가 못박는다.
    """
    region = _isolation_region()
    assert region, "격리 절 마커 부재 (T-0410)"
    assert "git 조작 금지" in region, (
        "격리 절이 프롬프트의 'git 조작 금지' 완화를 참조하지 않음 — 정합 끊김 (T-0410)"
    )
    missing = [name for name, token in (("병행", "병행"), ("이중 방어", "이중 방어")) if token not in region]
    assert not missing, (
        f"격리 절에 이중 방어 병행 명시 누락: {missing} — 격리와 완화가 병행(이중 방어)임을 명시해야 함 (T-0410)"
    )


def test_reviewer_prompt_has_isolation_worklocation_slot():
    """reviewer 위임 프롬프트에 격리 스냅샷 절대경로 주입 슬롯(작업 위치)이 있다 (주입 단계 배선)."""
    reviewer = _reviewer_region()
    assert reviewer, "code-reviewer 위임 절 마커 부재 (T-0410)"
    assert "작업 위치(병렬 wave 시 격리 스냅샷)" in reviewer, (
        "reviewer 프롬프트에 격리 스냅샷 작업 위치 주입 슬롯이 없음 — 3단계 '주입' 대상 부재 (T-0410)"
    )


# ── (6) 출하 doc 위생 — placeholder·framework wikilink 0 ──────────────────────

def test_gate_isolation_no_placeholder_or_wikilink():
    """격리 절에 미충전 mustache placeholder({{…}})·framework wikilink([[…]]) 가 없다 (T-0090).

    스킬은 채택자 트리로 전파돼 그대로 읽히므로 미충전 토큰·dangling wikilink 가 있으면 안 된다.
    (커맨드 자리표시 `<scratch>`·`<T>` 는 angle-bracket 관용 — mustache/wikilink 가 아니라 허용.)
    """
    region = _isolation_region()
    assert region, "격리 절 마커 부재 (T-0410)"
    assert "{{" not in region, (
        f"격리 절에 미충전 placeholder({{{{…}}}}) 잔존 (T-0410)"
    )
    hits = _FRAMEWORK_WIKILINK.findall(region)
    assert not hits, (
        f"격리 절에 framework wikilink {hits} 잔존 — plain text 로 (T-0090 · T-0410)"
    )
