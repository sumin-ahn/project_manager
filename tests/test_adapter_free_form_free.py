"""@render 활성 경로의 출하 어댑터는 free-form-free (ADR-0030 · T-0135/T-0136 가드).

배경: @render 가 활성화되면(T-0133) `pm_render` 가 어댑터를 자족 .md 로 렌더한다. free-form
value-fill 기계(overlay·slot-fill·conditional-omit)는 ADR-0031 로 제거됐으므로 — 어댑터에 free-form
`{{KEY}}` 토큰이 남아 있으면 그 토큰을 아무도 채우지 않아 `_assert_no_leak` 가 **RenderLeakError 로
hard-fail**(자족 산출물 = 토큰 0). 즉 채택자 customization·**안전 라인(보호 영역)**이 조용히 소실되는
대신 emission 순간 큰소리로 표면화된다.

결정(ADR-0030·amends ADR-0028, ADR-0031 cleanup): @render 어댑터 파일은 **operational 토큰만** 보유한다
(free-form 0). 채택자 free-form 은 *기존 canonical home* 에 둔다 — 고유 제약 = root doc(CLAUDE.md/
AGENTS.md §프로젝트 고유 제약) · 보호 영역 = `pm_role.local.md §보호 영역`([[ADR-0025]]). pm_import 의
FILL 채널이 거기서 전담하며, 어댑터는 이를 포인터로 참조.

이 가드는 그 불변식을 lock-in 한다 — @render 될 어댑터 경로의 출하 .md 에 free-form 토큰이
하나라도 있으면 fail. 미래 어댑터가 free-form 을 재유입하면 여기서 잡힌다
(feature-ship-needs-fresh-adopter-gate 클래스).
"""
from pathlib import Path

import pytest

from _harness_matrix import HARNESSES

REPO_ROOT = Path(__file__).resolve().parents[1]

# 활성화 시 omit/leak 대상이 되는 3종 free-form 토큰 (operational·opencode-model 토큰은 제외 —
# 그건 @render 가 결정적으로 치환하며 host-omit 대상이 아니다).
FREEFORM_TOKENS = (
    "{{PROJECT_CONSTRAINTS}}",
    "{{PROTECTED_PATHS}}",
    "{{USER_GATE_ITEMS}}",
)

# @render 활성화 scope (ADR-0030 · T-0133/0135/0136): 하네스별 어댑터 agent/skill 디렉토리.
# root doc(CLAUDE.md/AGENTS.md)·lite·pm_role.local.md 은 @render 밖 = free-form 의 canonical home → 제외.
#   축은 파생(HARNESSES)이되 각 하네스의 agent/skill 트리 경로는 harness-특수(remap 이 달라 손으로
#   못 지운다) — codex 는 `.codex/agents`(.toml @render)·`.agents/skills`(SKILL.md @render·@source remap).
#   신규 하네스가 이 맵을 안 채우면 아래 completeness 가드가 loud 로 죽어 편입을 강제한다(T-0429).
_RENDER_SCOPED_BY_HARNESS = {
    "claude": ("templates/claude_code/.claude/agents",     # T-0135
               "templates/claude_code/.claude/skills"),
    "opencode": ("templates/opencode/.opencode/agents",    # T-0136
                 "templates/opencode/.claude/skills"),      # ADR-0065 단일 소비 미러(command 은퇴·T-0364)
    "codex": ("templates/codex/.codex/agents",             # T-0429 (codex agents = .toml)
              "templates/codex/.agents/skills"),
}
assert set(HARNESSES) <= set(_RENDER_SCOPED_BY_HARNESS), (
    f"신규 하네스가 _RENDER_SCOPED_BY_HARNESS 에 미등록: {set(HARNESSES) - set(_RENDER_SCOPED_BY_HARNESS)}")
RENDER_SCOPED_DIRS = tuple(d for h in sorted(HARNESSES) for d in _RENDER_SCOPED_BY_HARNESS[h])


def _render_scoped_text_files():
    """render-scoped 어댑터 디렉토리의 텍스트 파일 전수 — **확장자 무관**(codex agent 는 `.toml`·
    claude/opencode agent·skill 은 `.md`). 옛 `.md`-only 스캔은 codex `.codex/agents/*.toml` 을
    못 봤다(T-0424 확장자-열거 클래스의 거울상·T-0429). 바이너리는 소비처의 decode 실패로 자연 제외."""
    files = []
    for rel in RENDER_SCOPED_DIRS:
        d = REPO_ROOT / rel
        if d.is_dir():
            files.extend(p for p in sorted(d.rglob("*")) if p.is_file())
    return files


@pytest.mark.parametrize("rel", RENDER_SCOPED_DIRS)
def test_render_scoped_dir_present_and_nonempty(rel):
    """RENDER_SCOPED_DIRS **각 등록 경로**가 실존 + 파일 ≥1 (경로별·MF3). aggregate `assert scoped`
    만 있으면 특정 하네스 경로가 stale/누락돼도 다른 하네스 파일로 green 이 된다 — 경로별로 못박아
    한 하네스의 render-scoped 트리 소실을 즉시 red 로 표면화한다(하네스 축은 파생·경로 실존은 경로별)."""
    d = REPO_ROOT / rel
    assert d.is_dir(), f"render-scoped 경로 부재(stale/누락?): {rel}"
    files = [p for p in d.rglob("*") if p.is_file()]
    assert files, f"render-scoped 경로에 파일 0 (트리 소실?): {rel}"


def test_render_scoped_adapters_are_free_form_free():
    """@render 될 어댑터 경로의 출하 파일(확장자 무관·`.md`+codex `.toml`)에 free-form 토큰 0."""
    scoped = _render_scoped_text_files()
    assert scoped, (
        "render-scoped 어댑터 파일을 하나도 못 찾음 — 경로 상수(RENDER_SCOPED_DIRS)가 stale 인지 확인."
    )
    offenders = []
    for f in scoped:
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # 바이너리·읽기불가 = free-form 치환 대상 아님.
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
