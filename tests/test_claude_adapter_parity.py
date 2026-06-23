"""`.claude/` ↔ `templates/claude_code/.claude/` 어댑터 전파 parity 가드 (T-0089·[[T-0088]]).

루트 도그푸딩 `.claude/` 어댑터는 채택 프로젝트용 `templates/claude_code/.claude/` 로
동기화된다(pm_update 전파). 드리프트 시 채택 프로젝트가 옛/다른 동작을 받는다
([[verify-engine-template-propagation]]). v2 머지 전 codex 제안 — 핵심 어댑터 산출물의
양 트리 parity 를 자동 검증한다:
  - `settings.json`: 양쪽에 PreCompact 키 존재 + 동일 훅 명령(루트에 ctx 훅 없는 차이는 허용).
  - `precompact_capture_hook.sh`: byte-identical.
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
    "precompact_capture_hook.sh",
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


def test_settings_precompact_propagated_both_trees():
    """PreCompact 훅이 양 트리 settings 에 존재(채택 프로젝트 폴백 보장·codex must-fix).

    루트에 ctx 훅이 없는 차이는 허용하되 PreCompact 전파는 강제 — precompact 훅이
    템플릿에만 있고 루트엔 없으면(또는 반대) 전파 드리프트.
    """
    root_block = _precompact_block(ROOT_CLAUDE / "settings.json")
    tmpl_block = _precompact_block(TEMPLATE_CLAUDE / "settings.json")
    assert root_block, "root settings.json 에 PreCompact 훅 누락 (전파 드리프트)"
    assert tmpl_block, "templates settings.json 에 PreCompact 훅 누락 (전파 드리프트)"


def test_settings_precompact_hook_command_identical():
    """양 트리 PreCompact 블록이 동일한 precompact 훅을 같은 명령으로 가리킨다.

    의도된 settings 차이(ctx 훅)는 허용하지만 PreCompact 블록 자체는 동일해야 한다 —
    같은 훅·타임아웃을 채택 프로젝트가 받게.
    """
    root_block = _precompact_block(ROOT_CLAUDE / "settings.json")
    tmpl_block = _precompact_block(TEMPLATE_CLAUDE / "settings.json")
    assert root_block == tmpl_block, (
        f"PreCompact 블록이 root↔templates 불일치:\nroot={root_block}\ntmpl={tmpl_block}"
    )
    # 블록이 실제로 precompact 훅을 가리키는지(빈 가드 무력화 방지).
    flat = json.dumps(root_block)
    assert "precompact_capture_hook.sh" in flat, (
        f"PreCompact 블록이 precompact 훅을 안 가리킴: {root_block}"
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
