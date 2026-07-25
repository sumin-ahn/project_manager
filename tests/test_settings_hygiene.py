"""settings.json hygiene 가드 (T-0300) — 출하 template 의 critical env 존재 + auto-compact 토글 단일화.

Claude Code 의 권한 승인('always allow') 재직렬화가 `.claude/settings.json` 을 다시 쓸 때 커스텀
env 키를 조용히 드롭하던 재발 클래스(PM 61 `DISABLE_AUTO_COMPACT` 드롭·PM 62 중복)를 **ship 템플릿
기준으로 못박는다**. 채택자가 pm_import 로 받는 `templates/claude_code/.claude/settings.json` 에
critical env(ctx-guard 예산이 아닌 bash timeout 노브·T-0293)와 정본 auto-compact 토글이 반드시 살아
있어야 그 채택자 산출물이 안 바뀐다(adopter-facing). 재직렬화 자체는 Claude Code 동작이라 코드로 못
막지만(claude-code-guide: 커스텀 env 드롭은 미문서), **출하본에 존재하는지**는 이 가드가 fail-loud 로 세운다.

정본(claude-code-guide 확인·T-0300): auto-compact 는 top-level `autoCompactEnabled` 가 스키마 정본이고
`env.DISABLE_AUTO_COMPACT` 는 중복 우회수단 — **하나(top-level)만** 남긴다. bash timeout 노브는 정식
문서화 env 라 `env` 블록에 존치한다.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_SHIP_TEMPLATE = _REPO / "templates" / "claude_code" / ".claude" / "settings.json"

# 채택자 산출물을 바꾸는 critical env — 하나라도 ship 템플릿에서 소실되면 fail-loud.
_CRITICAL_ENV_KEYS = ("BASH_DEFAULT_TIMEOUT_MS", "BASH_MAX_TIMEOUT_MS")


def _load(path: Path) -> dict:
    """settings.json 을 파싱한다 (깨진 JSON = fail-loud·재직렬화 파손 감지)."""
    assert path.is_file(), f"settings.json 부재: {path}"
    return json.loads(path.read_text(encoding="utf-8"))


def test_ship_template_has_critical_env():
    """출하 template 이 critical env(bash timeout 노브)를 전부 보유 — 재직렬화 드롭 fail-loud."""
    conf = _load(_SHIP_TEMPLATE)
    env = conf.get("env", {})
    missing = [k for k in _CRITICAL_ENV_KEYS if k not in env]
    assert not missing, (
        f"출하 template settings.json 에서 critical env 소실: {missing} — 권한-승인 재직렬화 드롭 "
        f"의심(T-0300). 채택자가 받는 값이 바뀜(adopter-facing). 복원 필요."
    )


def test_ship_template_autocompact_canonical_toggle_only():
    """auto-compact 는 정본 top-level `autoCompactEnabled` 단일 — env 중복 토글 제거(T-0300 dedup).

    값은 **true**(T-0458 — 서브에이전트 compaction 허용·메인은 훅 hard-stop 이 선행하고
    auto-compact 는 폴백). 이 가드는 (1) 정본 토글의 존재/타입과 (2) env 중복 토글 부재(단일 정본)를
    못박는다 — 재직렬화가 정본 토글을 드롭하거나 env 중복을 되살리면 fail-loud.
    """
    conf = _load(_SHIP_TEMPLATE)
    assert conf.get("autoCompactEnabled") is True, (
        "출하 template 에 정본 토글 `autoCompactEnabled: true` 부재/변경 — 서브에이전트 compaction "
        "봉쇄로 장기 dev 서브에이전트가 API 벽에 죽던 클래스 재발 위험(T-0458·발단 T-0431)."
    )
    env = conf.get("env", {})
    assert "DISABLE_AUTO_COMPACT" not in env, (
        "출하 template 에 중복 auto-compact 토글 `env.DISABLE_AUTO_COMPACT` 재등장 — 정본은 "
        "top-level `autoCompactEnabled` 하나(T-0300·claude-code-guide 확인). 중복 제거 유지."
    )


@pytest.mark.parametrize("path", [_SHIP_TEMPLATE])
def test_settings_json_valid(path: Path):
    """settings.json 이 유효 JSON — 재직렬화 파손/문법오류 조기 감지."""
    _load(path)  # 파싱 실패 시 JSONDecodeError 로 fail-loud.
