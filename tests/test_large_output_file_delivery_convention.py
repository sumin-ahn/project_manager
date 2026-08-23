"""T-0337 — 대형 산출물 파일-전달 규약 회귀 가드 (양 하네스 쓰기 역할).

모델 응답(서브에이전트 최종 보고 포함)은 출력 상한(opencode 32k 토큰)에서 **조용히** 잘린다
(PM 70 라이브 확증 — 수신자는 절단을 감지하지 못한다). 그 잔여 축을 우회하는 파일-전달 규약 —
*대형 산출은 파일로 쓰고 응답엔 절대경로 + 요약 몇 줄만* — developer/architect 카드에 실려
있는지 기계로 못박는다. researcher는 파일 생성 없이 조사 분할, code-reviewer는 티켓 절에 기록한다.

생성 자체는 tool 훅으로 인터셉트 불가(구조)하므로 가드는 "출하 문서에 규약이 실려 있음"만
durable 하게 검증한다 (결정: 규약이지 강제가 아니다).

검사 축 (T-0266 4어휘 패턴 동형 — 얕은 단일 문자열이 아니라 규약 절 안의 어휘 클래스 검증):
  (1) 쓰기 산출 역할 4 카드가 규약 절 마커 + 4 핵심 어휘 + 임계(200줄/8KB)를 가진다.
  (2) opencode 2 카드는 safe_write(8KB 청크)·write 16KB deny 를 추가 명시하고,
      claude 2 카드는 opencode 전용 safe_write 를 섞지 않는다.
  (3) 규약 절에 미충전 placeholder({{…}})·framework wikilink([[…]]) 0.

⚠️ '요약'·'파일' 은 카드 다른 곳(researcher 산출 형식 등)에도 등장하므로 whole-file 검사는
vacuous — 반드시 규약 절(마커 ~ 다음 '## ' 헤딩)만 슬라이스해 그 안에서 어휘를 확인한다
(T-0105 section-slice 동형).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CLAUDE_AGENTS = REPO / "templates" / "claude_code" / ".claude" / "agents"
OPENCODE_AGENTS = REPO / "templates" / "opencode" / ".opencode" / "agents"

# 독립 파일 산출이 허용된 역할만 이 규약 대상이다. researcher와 reviewer는 아래 별도 계약.
_ROLES = ("developer", "developer-hard", "architect")
CLAUDE_CARDS = [(f"claude {r}", CLAUDE_AGENTS / f"{r}.md") for r in _ROLES]
OPENCODE_CARDS = [(f"opencode {r}", OPENCODE_AGENTS / f"{r}.md") for r in _ROLES]
ALL_CARDS = CLAUDE_CARDS + OPENCODE_CARDS
REPORT_ONLY_CARDS = [
    ("claude researcher", CLAUDE_AGENTS / "researcher.md", "researcher"),
    ("opencode researcher", OPENCODE_AGENTS / "researcher.md", "researcher"),
    ("claude code-reviewer", CLAUDE_AGENTS / "code-reviewer.md", "code-reviewer"),
    ("opencode code-reviewer", OPENCODE_AGENTS / "code-reviewer.md", "code-reviewer"),
]

# 규약 절 슬라이스 앵커 — 4 카드 공통(각 카드 본문은 역할 결에 맞게 다르지만 마커는 공유).
CONVENTION_MARKER = "**대형 산출물은 파일로 — 응답(보고) 절단 우회.**"

# 4 핵심 어휘 클래스 (ticket 인터페이스: 파일·경로·요약·절단). 각 클래스는 허용 토큰 집합 —
# 규약 절 안에서 4 어휘가 *모두* 등장해야 통과 (얕은 단일 문자열 아님 · T-0266 동형).
_CORE_VOCAB = [
    ("파일 산출 (file)", ("파일",)),
    ("절대경로 반환 (path)", ("절대경로",)),
    ("핵심 요약 (summary)", ("요약",)),
    ("응답 절단 이유 (truncation)", ("절단", "잘린", "잘려", "잘리")),
]

# 임계 가이드(결정: 200줄/8KB — safe_write chunk 와 결 정합). 숫자를 카드에 명시.
_THRESHOLD = [
    ("줄 임계", ("200줄", "200 줄")),
    ("바이트 임계", ("8KB", "8 KB")),
]

# opencode 하네스 관용 — 대형 파일 쓰기 = safe_write(8KB chunk). 4 카드 공통 요구.
_OPENCODE_SAFE_WRITE = [
    ("safe_write 지시", ("safe_write",)),
]
# write 16KB deny 임계는 OPENCODE_CARDS(developer·architect·전부 write-capable)에 무조건
# 요구한다 — read-only 역할(researcher·code-reviewer)은 OPENCODE_CARDS 밖(REPORT_ONLY_CARDS)이고
# edit 가 자기 티켓 사본 절로 한정돼(ADR-0089 전원 참여·T-0696·T-0745 — edit permission 자체는
# 두 역할 모두 allow) 대형 산출 **파일**을 새로 쓰지 않으므로 16KB 임계 자체가 무의미(그 문구를
# 이 카드에 넣으면 write 가 16KB 까지는 되는 듯 오인 · T-0342: read-only 카드 16KB 문구 정합).
_OPENCODE_WRITE_16KB_DENY = [
    ("write deny 임계(16KB)", ("16KB", "16 KB")),
]

# 출하 doc 이 wikilink 하면 안 되는 framework-내부 ID (채택자 트리엔 부재 → dangling · T-0090).
_FRAMEWORK_WIKILINK = re.compile(r"\[\[(ADR-\d+|T-\d+|idea-\d+)\]\]")


def _convention_region(path: Path) -> str:
    """카드에서 규약 절(마커 ~ 다음 '## ' 헤딩)만 슬라이스. 마커 부재면 빈 문자열.

    '요약'·'파일' 은 카드 다른 곳에도 있어 whole-file 검사는 vacuous — 규약 절 안에서만
    어휘를 확인한다(T-0105 section-slice 동형).
    """
    text = path.read_text(encoding="utf-8")
    idx = text.find(CONVENTION_MARKER)
    if idx == -1:
        return ""
    rest = text[idx:]
    end = rest.find("\n## ")
    return rest if end == -1 else rest[:end]


# ── (1) 4 카드 규약 존재 + 4 핵심 어휘 + 임계 ─────────────────────────────────

@pytest.mark.parametrize("label,path", ALL_CARDS)
def test_card_has_convention_marker(label, path):
    """4 카드 모두 파일-전달 규약 절 마커를 담는다 (2 역할 × 2 하네스)."""
    text = path.read_text(encoding="utf-8")
    assert CONVENTION_MARKER in text, (
        f"{label} ({path.relative_to(REPO)}) 에 파일-전달 규약 마커가 없음 — "
        f"대형 산출물 파일-전달 규약 절 누락 (T-0337)"
    )


@pytest.mark.parametrize("label,path", ALL_CARDS)
def test_card_convention_has_core_vocab(label, path):
    """규약 절이 4 핵심 어휘(파일·경로·요약·절단)를 모두 담는다.

    얕은 단일 문자열이 아니라 4 어휘 클래스가 규약 절 안에서 전부 등장해야 한다 (T-0266 동형).
    """
    region = _convention_region(path)
    assert region, f"{label} ({path.relative_to(REPO)}): 규약 절 마커 부재 (T-0337)"
    missing = [name for name, tokens in _CORE_VOCAB if not any(t in region for t in tokens)]
    assert not missing, (
        f"{label} ({path.relative_to(REPO)}) 규약 절에 핵심 어휘 누락: {missing} "
        f"— 파일·경로·요약·절단 4어휘를 모두 담아야 함 (T-0337)"
    )


@pytest.mark.parametrize("label,path", ALL_CARDS)
def test_card_convention_states_threshold(label, path):
    """규약 절이 임계 가이드(200줄/8KB)를 숫자로 명시한다 (결정: 임계 가이드 · T-0337)."""
    region = _convention_region(path)
    assert region, f"{label} ({path.relative_to(REPO)}): 규약 절 마커 부재 (T-0337)"
    missing = [name for name, tokens in _THRESHOLD if not any(t in region for t in tokens)]
    assert not missing, (
        f"{label} ({path.relative_to(REPO)}) 규약 절에 임계 명시 누락: {missing} "
        f"— 200줄/8KB 임계를 카드에 명시해야 함 (T-0337)"
    )


# ── (2) 하네스 관용 — opencode safe_write / claude 미혼입 ─────────────────────

@pytest.mark.parametrize("label,path", OPENCODE_CARDS)
def test_opencode_card_convention_uses_safe_write(label, path):
    """opencode 2 카드 규약 절이 safe_write(8KB 청크) + write 16KB deny 임계를 명시한다 (T-0334 연계).

    opencode write 는 16KB 초과를 거부하므로 대형 파일 쓰기는 safe_write chunk 로 해야 한다 —
    inbound(tool_output) 이 아니라 outbound(생성) 축의 파일-쓰기 채널을 카드가 안내해야 함.
    OPENCODE_CARDS(developer·architect)는 전부 write-capable 역할이라 16KB 임계 명시를 무조건
    요구한다 — read-only 역할(researcher·code-reviewer)은 REPORT_ONLY_CARDS 로 별도 취급되며
    edit 가 자기 티켓 사본 절로 한정돼(ADR-0089 전원 참여·T-0696·T-0745 — edit permission 자체는
    두 역할 모두 allow) 대형 산출 파일을 새로 쓰지 않으므로 16KB 임계가 무의미하다(T-0342).
    """
    region = _convention_region(path)
    assert region, f"{label} ({path.relative_to(REPO)}): 규약 절 마커 부재 (T-0337)"
    required = list(_OPENCODE_SAFE_WRITE) + list(_OPENCODE_WRITE_16KB_DENY)
    missing = [name for name, tokens in required if not any(t in region for t in tokens)]
    assert not missing, (
        f"{label} ({path.relative_to(REPO)}) 규약 절에 opencode 대형-쓰기 지시 누락: {missing} "
        f"— safe_write(8KB 청크)·write 16KB deny 를 명시해야 함 (T-0334 · T-0337 · T-0342)"
    )


@pytest.mark.parametrize("label,path", CLAUDE_CARDS)
def test_claude_card_convention_no_safe_write(label, path):
    """claude 2 카드 규약 절은 opencode 전용 safe_write 를 섞지 않는다 (하네스 관용).

    safe_write 는 opencode plugin custom tool — claude 카드에 넣으면 부정확하다. 하네스별
    관용대로 유지(byte-identical 강제 아님)한다는 결정을 못박는다.
    """
    region = _convention_region(path)
    assert region, f"{label} ({path.relative_to(REPO)}): 규약 절 마커 부재 (T-0337)"
    assert "safe_write" not in region, (
        f"{label} ({path.relative_to(REPO)}) 규약 절에 opencode 전용 safe_write 가 섞임 "
        f"— claude 카드는 하네스 관용대로 (T-0337)"
    )


# ── (3) 규약 절 = placeholder·framework wikilink 0 ────────────────────────────

@pytest.mark.parametrize("label,path", ALL_CARDS)
def test_card_convention_no_placeholder_or_wikilink(label, path):
    """규약 절에 미충전 placeholder({{…}})·framework wikilink([[…]]) 가 없다 (DoD · T-0090).

    출하 doc 은 채택자 트리에서 그대로 읽히므로 미충전 토큰·dangling wikilink 가 있으면 안 된다.
    """
    region = _convention_region(path)
    assert region, f"{label} ({path.relative_to(REPO)}): 규약 절 마커 부재 (T-0337)"
    assert "{{" not in region, (
        f"{label} ({path.relative_to(REPO)}) 규약 절에 미충전 placeholder 잔존 (T-0337)"
    )
    hits = _FRAMEWORK_WIKILINK.findall(region)
    assert not hits, (
        f"{label} ({path.relative_to(REPO)}) 규약 절에 framework wikilink {hits} 잔존 — "
        f"plain text 로 (T-0090 · T-0337)"
    )


@pytest.mark.parametrize("label,path,role", REPORT_ONLY_CARDS)
def test_report_only_roles_do_not_create_standalone_delivery_files(label, path, role):
    """read-only 조사와 ticket-backed 리뷰는 별도 artifact로 권한을 우회하지 않는다."""
    text = path.read_text(encoding="utf-8")
    assert CONVENTION_MARKER not in text
    if role == "researcher":
        assert "파일 산출로 우회하지 않는다" in text
        assert "200줄/8KB" in text
    else:
        assert "라운드 파일" in text
        assert "별도 산출 파일" in text
