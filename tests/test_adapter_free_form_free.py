"""@render 활성 경로의 출하 어댑터는 free-form-free (ADR-0030 · T-0135/T-0136 가드).

배경: @render 가 활성화되면(T-0133) `pm_render` 가 어댑터의 free-form `{{KEY}}` 토큰을
overlay 로 채우거나 — overlay 미설정이면 **host 행을 통째 omit** 한다(pm_render `_sole_freeform_token`·
CONDITIONAL-OMIT). 어댑터에 free-form 토큰이 남아 있으면 fresh adopter(overlay 미설정)에서 그 줄이
조용히 사라져 채택자 customization·**안전 라인(보호 영역)**이 소실된다.

결정(ADR-0030·amends ADR-0028): @render 어댑터 파일은 **operational 토큰만** 보유한다(free-form 0).
채택자 free-form 은 *기존 canonical home* 에 둔다 — 고유 제약 = root doc(CLAUDE.md/AGENTS.md §프로젝트
고유 제약) · 보호 영역 = `pm_role.local.md §보호 영역`([[ADR-0025]]). 어댑터는 이를 포인터로 참조.

이 가드는 그 불변식을 lock-in 한다 — @render 될 어댑터 경로의 출하 .md 에 free-form 토큰이
하나라도 있으면 fail. 미래 어댑터가 free-form 을 재유입하면 여기서 잡힌다
(feature-ship-needs-fresh-adopter-gate 클래스).
"""
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# 활성화 시 omit/leak 대상이 되는 3종 free-form 토큰 (operational·opencode-model 토큰은 제외 —
# 그건 @render 가 결정적으로 치환하며 host-omit 대상이 아니다).
FREEFORM_TOKENS = (
    "{{PROJECT_CONSTRAINTS}}",
    "{{PROTECTED_PATHS}}",
    "{{USER_GATE_ITEMS}}",
)

# @render 활성화 scope (ADR-0030 · T-0133): 어댑터 디렉토리만.
# root doc(CLAUDE.md/AGENTS.md)·lite·pm_role.local.md 은 @render 밖 = free-form 의 canonical home → 제외.
RENDER_SCOPED_DIRS = (
    "templates/claude_code/.claude/agents",     # T-0135 (claude)
    "templates/claude_code/.claude/skills",
    "templates/opencode/.opencode/agents",      # T-0136 (opencode)
    "templates/opencode/.opencode/command",
)


def _render_scoped_md_files():
    files = []
    for rel in RENDER_SCOPED_DIRS:
        d = REPO_ROOT / rel
        if d.is_dir():
            files.extend(sorted(d.rglob("*.md")))
    return files


def test_render_scoped_adapters_are_free_form_free():
    """@render 될 어댑터 경로의 출하 .md 에 free-form 토큰이 0 이어야 한다."""
    scoped = _render_scoped_md_files()
    assert scoped, (
        "render-scoped 어댑터 .md 를 하나도 못 찾음 — 경로 상수(RENDER_SCOPED_DIRS)가 stale 인지 확인."
    )
    offenders = []
    for f in scoped:
        text = f.read_text(encoding="utf-8")
        for tok in FREEFORM_TOKENS:
            if tok in text:
                offenders.append(f"{f.relative_to(REPO_ROOT).as_posix()}: {tok}")
    assert not offenders, (
        "@render 활성 경로에 free-form 토큰 잔존 — 활성화 시 omit→채택자 customization·안전 라인 소실.\n"
        "고유 제약=root doc(CLAUDE.md §프로젝트 고유 제약)·보호 영역=pm_role.local.md §보호 영역 으로\n"
        "옮기고 포인터로 치환하라 (ADR-0030):\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("token", FREEFORM_TOKENS)
def test_freeform_token_format(token):
    """가드가 검사하는 토큰 형식이 pm_render 의 free-form 토큰과 동일한 `{{KEY}}` 형식인지(자기검증)."""
    assert token.startswith("{{") and token.endswith("}}")
