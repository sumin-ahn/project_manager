"""`.claude/` ↔ `templates/claude_code/.claude/` 어댑터 전파 parity 가드 (T-0089·[[T-0088]]).

루트 도그푸딩 `.claude/` 어댑터는 채택 프로젝트용 `templates/claude_code/.claude/` 로
동기화된다(pm_update 전파). 드리프트 시 채택 프로젝트가 옛/다른 동작을 받는다
([[verify-engine-template-propagation]]). v2 머지 전 codex 제안 — 핵심 어댑터 산출물의
양 트리 parity 를 자동 검증한다:
  - `settings.json`(ADR-0038 D3 비대칭): root=PreCompact breadcrumb(auto-compact ON) /
    template=PreCompact 없음 + autoCompactEnabled:false(auto-compact OFF·hard-stop 단일 게이트).
  - `precompact_capture_hook.sh`: root 전용(template 엔 없음) — byte-identical 대상 아님.
  - `skills/pm-handoff/SKILL.md`·`skills/pm-dev-delegate/SKILL.md`: byte-identical.
  - `agents/*.md`: byte-identical(4파일).

**의도된 차이 허용**: 루트 settings 는 ctx 훅(PreToolUse/UserPromptSubmit/statusLine·
ctx_stop_hook·ctx_statusline)을 *싣지 않는다* — 루트 도그푸딩은 그 훅을 안 쓴다. 그래서
settings 는 byte-identical 을 강제하지 않고 **PreCompact 전파만** 양쪽 강제한다(채택
프로젝트 폴백 보장·codex must-fix 맥락). stdlib + json. 파일 iterate(hermetic).
"""
from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ROOT_CLAUDE = REPO / ".claude"
TEMPLATE_CLAUDE = REPO / "templates" / "claude_code" / ".claude"

# 양 트리에서 byte-identical 이어야 하는 어댑터 산출물 (의도된 차이 없음).
IDENTICAL_RELPATHS = [
    "skills/pm-handoff/SKILL.md",
    "skills/pm-dev-delegate/SKILL.md",
    "skills/pm-wave-claim/SKILL.md",
    "agents/architect.md",
    "agents/code-reviewer.md",
    "agents/developer.md",
    "agents/researcher.md",
]


def _precompact_block(settings_path: Path):
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    return data.get("hooks", {}).get("PreCompact")


# ── settings.json: PreCompact 전파 (양쪽 존재 + 동일 명령) ────────────────────

def test_settings_present_both_trees():
    """settings.json 이 양 트리에 존재 + 유효 JSON."""
    for tree in (ROOT_CLAUDE, TEMPLATE_CLAUDE):
        path = tree / "settings.json"
        assert path.exists(), f"settings.json 없음: {path}"
        json.loads(path.read_text(encoding="utf-8"))  # 파싱 실패 시 raise


def test_precompact_asymmetric_root_breadcrumb_template_autocompact_off():
    """ADR-0038 D3 — precompact 는 이제 **원리적 비대칭**(압축 가능한 곳에만 breadcrumb).

    - root(도그푸딩·ctx hard-stop 훅 부재·auto-compact **ON**): PreCompact breadcrumb 존재
      → `precompact_capture_hook.sh` 를 가리킴 (압축이 수동 핸드오프를 선점할 수 있는 유일한
      net-less tree 라 1줄 신호 보존).
    - template(auto-compact **OFF**·hard-stop 단일 게이트): PreCompact **없음** +
      `autoCompactEnabled:false` + env `DISABLE_AUTO_COMPACT` (압축이 hard-stop 을 선점 못 하니
      백스톱 불요). ctx-훅-템플릿-전용의 거울상 = precompact-root-전용.
    """
    root = json.loads((ROOT_CLAUDE / "settings.json").read_text(encoding="utf-8"))
    tmpl = json.loads((TEMPLATE_CLAUDE / "settings.json").read_text(encoding="utf-8"))
    # root: PreCompact breadcrumb 존재 + auto-compact 유지(비활성 아님).
    root_block = root.get("hooks", {}).get("PreCompact")
    assert root_block, "root settings.json 에 PreCompact breadcrumb 누락 (auto-compact ON tree)"
    assert "precompact_capture_hook.sh" in json.dumps(root_block), (
        f"root PreCompact 가 precompact 훅을 안 가리킴: {root_block}"
    )
    assert root.get("autoCompactEnabled") is not False, "root 는 auto-compact 유지여야 함"
    # template: PreCompact 제거 + auto-compact 이중 비활성.
    assert tmpl.get("hooks", {}).get("PreCompact") is None, (
        "template settings.json 에 PreCompact 잔존 — auto-compact off 라 제거됐어야 함 (ADR-0038 D3)"
    )
    assert tmpl.get("autoCompactEnabled") is False, "template autoCompactEnabled:false 누락"
    assert tmpl.get("env", {}).get("DISABLE_AUTO_COMPACT") == "1", (
        "template env DISABLE_AUTO_COMPACT 누락 (이중 kill-switch)"
    )


# ── byte-identical 어댑터 산출물 (hook·skills·agents) ─────────────────────────

def test_adapter_artifacts_byte_identical():
    """precompact 훅·pm-handoff/pm-dev-delegate skill·agents 4파일이 양 트리 byte-identical.

    각 파일에 대해 양 트리 존재 + 바이트 동일 검증 (pm_update 전파 무드리프트).
    """
    for relpath in IDENTICAL_RELPATHS:
        root_path = ROOT_CLAUDE / relpath
        tmpl_path = TEMPLATE_CLAUDE / relpath
        assert root_path.exists(), f"root 어댑터 산출물 없음: {root_path}"
        assert tmpl_path.exists(), f"templates 어댑터 산출물 없음: {tmpl_path}"
        assert root_path.read_bytes() == tmpl_path.read_bytes(), (
            f"{relpath} 가 root↔templates byte-identical 아님 (전파 드리프트) — "
            "엔진/어댑터 변경 후 pm_update 로 전파 필요"
        )
