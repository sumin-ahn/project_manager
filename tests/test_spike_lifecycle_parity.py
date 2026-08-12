"""T-0266 — spike ADR-0010 draft/sealed 생애주기 파리티 + raw/README 3벌 동기 가드.

ADR-0010 은 spike 를 **`status: draft` 동안 편집 가능 → 사용자 사인오프 시 sealed → 그 뒤
IMMUTABLE** 로 정의한다. 그 생애주기가 claude 스킬(`.claude/skills/spike-new/SKILL.md`)에는
반영됐으나 opencode 출하 스킬 미러·command 기계 사본과 출하 `wiki/raw/README.md`
(2 템플릿)에는 구 all-immutable
모델로 남아 stale/자기모순이던 것을 못박는다.

diff-scoped 리뷰는 *부재*(한쪽 표면이 생애주기 서술을 안 담음)를 못 본다 — 이 가드가 그 갭을
회귀로 막는다.

⚠️ **왜 손 동기인가 (root cause):** 아래 raw/README 3벌은 **`engine.manifest` 밖**이라
`pm_update` 가 자동 전파하지 않는다(실측 확인). 즉 canonical 을 고쳐도 두 템플릿 사본은
자동으로 따라오지 않으므로 **셋 다 손으로** 맞춰야 하고, 그 손 동기 누락이 이번 stale 의
근본 원인이다. 이 가드가 byte-동일을 강제해 "한쪽만 갱신"을 잡는다.

⚠️ 출하 doc 에 framework-내부 wikilink(`[[ADR-0010]]`)를 넣지 않는다 — 채택자 트리엔 그 ADR 이
없어 `board.py lint` 가 dangling 으로 차단한다. 생애주기 인용은 plain text `(ADR-0010)` 로.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# claude 스킬 = canonical 진실 (수정 대상 아님·읽기 전용 reference).
CLAUDE_SKILL = REPO / ".claude" / "skills" / "spike-new" / "SKILL.md"
# opencode 출하 두 표면(T-0674): 모델 skill tool 미러 + 사람 slash command 사본.
OPENCODE_SKILL = REPO / "templates" / "opencode" / ".claude" / "skills" / "spike-new" / "SKILL.md"
OPENCODE_COMMAND = REPO / "templates" / "opencode" / ".opencode" / "command" / "spike-new.md"

# raw/README 3벌 — 같은 문서의 세 트리 사본 (manifest 밖·손 동기·아래 docstring 근거).
RAW_README_CANONICAL = REPO / ".project_manager" / "wiki" / "raw" / "README.md"
RAW_README_CLAUDE = REPO / "templates" / "claude_code" / ".project_manager" / "wiki" / "raw" / "README.md"
RAW_README_OPENCODE = REPO / "templates" / "opencode" / ".project_manager" / "wiki" / "raw" / "README.md"

_RAW_READMES = [
    ("canonical (wiki/raw/README.md)", RAW_README_CANONICAL),
    ("claude_code 템플릿", RAW_README_CLAUDE),
    ("opencode 템플릿", RAW_README_OPENCODE),
]

# ── ADR-0010 생애주기 3상태 어휘 (얕은 "draft" in text 가드 우회 방지) ──────────
# draft(편집상태) → 사인오프 트리거 → sealed(봉인상태) → immutable(취지). 구 all-immutable
# 모델(draft/sealed 부재)은 이 4개를 다 담지 못하므로 fail 한다.
_LIFECYCLE_MARKERS = [
    ("draft 편집상태", "draft"),
    ("sealed 봉인상태", "sealed"),
    ("immutable 취지", "immutable"),
    ("seal 트리거 = 사용자 사인오프", "사인오프"),
]

_LIFECYCLE_SURFACES = [
    ("claude SKILL.md (canonical)", CLAUDE_SKILL),
    ("opencode spike-new 스킬 미러", OPENCODE_SKILL),
    ("opencode spike-new command 사본", OPENCODE_COMMAND),
]

# raw/README 예외절(ADR-0010) 핵심 마커 — 코디네이트된 구모델 회귀(3벌 동시 revert)를 잡는다.
_RAW_EXCEPTION_MARKERS = [
    "## 예외",
    "status: draft",
    "sealed",
    "사인오프",
]

# 출하 doc 이 wikilink 하면 안 되는 framework-내부 ID (채택자 트리엔 부재 → dangling).
import re
_FRAMEWORK_WIKILINK = re.compile(r"\[\[(ADR-\d+|T-\d+|idea-\d+)\]\]")


# ── (1) 스킬/커맨드 생애주기 파리티 (한쪽만 갱신 → fail) ───────────────────────

@pytest.mark.parametrize("label,path", _LIFECYCLE_SURFACES)
def test_surface_describes_adr0010_lifecycle(label, path):
    """claude 스킬과 opencode 커맨드가 **둘 다** ADR-0010 draft/sealed/immutable 생애주기를 서술.

    한쪽만 갱신(예: 스킬만 draft/sealed, 커맨드는 구 all-immutable)이면 그 표면에서 fail 한다.
    얕은 "draft" 단어 세기가 아니라 3상태 어휘(draft·sealed·immutable 취지·사인오프 seal 트리거)
    전부를 요구해 우회를 막는다.
    """
    low = path.read_text(encoding="utf-8").lower()
    missing = [name for name, token in _LIFECYCLE_MARKERS if token.lower() not in low]
    assert not missing, (
        f"{label} ({path.relative_to(REPO)}) 에 ADR-0010 생애주기 어휘 누락: {missing} "
        f"— 구 all-immutable 모델? draft→(사인오프)→sealed→immutable 을 서술해야 함 (T-0266)"
    )


# ── (2) raw/README 3벌 동기 — byte-동일 + 예외절 존재 ─────────────────────────

def test_raw_readmes_byte_identical():
    """raw/README 3벌(canonical + 양 템플릿)이 **byte-동일**하다.

    이 3파일은 같은 문서의 세 트리 사본인데 `engine.manifest` 밖이라 `pm_update` 가 자동
    전파하지 않는다(실측) — canonical 을 고쳐도 두 템플릿이 자동으로 따라오지 않아 stale 이
    쌓인다(이번 T-0266 의 근본 원인). byte-동일 강제가 "한쪽만 갱신"을 결정적으로 잡는다.
    """
    canon_label, canon_path = _RAW_READMES[0]
    canon = canon_path.read_text(encoding="utf-8")
    mismatched = []
    for label, path in _RAW_READMES[1:]:
        if path.read_text(encoding="utf-8") != canon:
            mismatched.append(f"{label} ({path.relative_to(REPO)})")
    assert not mismatched, (
        f"raw/README 사본이 canonical ({canon_path.relative_to(REPO)}) 과 byte-불일치: "
        f"{mismatched} — manifest 밖이라 손 동기 필요(pm_update 전파 없음). 세 벌을 맞춰라 (T-0266)"
    )


@pytest.mark.parametrize("label,path", _RAW_READMES)
def test_raw_readme_contains_lifecycle_exception(label, path):
    """raw/README 3벌 각각이 ADR-0010 spike draft/sealed 예외절을 담는다.

    byte-동일 가드가 부분 갱신을 잡지만, 3벌을 *함께* 구모델로 revert 하면 byte-동일은
    통과한다 — 이 마커 가드가 그 coordinated 회귀까지 잡는다.
    """
    text = path.read_text(encoding="utf-8")
    missing = [m for m in _RAW_EXCEPTION_MARKERS if m not in text]
    assert not missing, (
        f"{label} ({path.relative_to(REPO)}) 에 ADR-0010 spike 예외절 마커 누락: {missing} "
        f"— draft→sealed 생애주기 예외절이 없음 (구 all-immutable 모델? · T-0266)"
    )


# ── (3) 출하 doc = framework wikilink 0 (dangling 차단 · T-0090 규칙) ──────────

def test_shipped_surfaces_no_framework_wikilink():
    """opencode 커맨드·양 템플릿 raw/README 가 framework ADR/ticket 을 wikilink 하지 않는다.

    채택자 트리엔 그 ADR/ticket 파일이 없어 `[[…]]` 는 dangling → `board.py lint` 차단.
    생애주기 인용은 plain text `(ADR-0010)` 로 써야 한다 (T-0090 재발 방지·T-0266).
    canonical wiki/raw/README 는 framework 자기 트리라 제외(그쪽 dangling 규칙은 별도).
    """
    shipped = [OPENCODE_SKILL, OPENCODE_COMMAND, RAW_README_CLAUDE, RAW_README_OPENCODE]
    for path in shipped:
        hits = _FRAMEWORK_WIKILINK.findall(path.read_text(encoding="utf-8"))
        assert not hits, (
            f"{path.relative_to(REPO)} 에 framework wikilink {hits} 잔존 — "
            f"plain text(예 'ADR-0010')로 (T-0090·T-0266)"
        )
