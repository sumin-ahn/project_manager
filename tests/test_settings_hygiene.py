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

import importlib.util
import json
import re
from pathlib import Path

import pytest

from _repo_owned_inventory import (
    OWNED,
    RepoFilesFallbackWarning,
    repo_owned_paths,
)

_REPO = Path(__file__).resolve().parent.parent
_SHIP_TEMPLATE = _REPO / "templates" / "claude_code" / ".claude" / "settings.json"
_OPENCODE_INSTRUCTIONS = (
    _REPO / "templates" / "opencode" / ".opencode" / "pm-instructions.md"
)
_PM_ENV_CARDS = (
    _REPO / ".claude" / "skills" / "pm-env" / "SKILL.md",
    _REPO / "templates" / "claude_code" / ".claude" / "skills" / "pm-env" / "SKILL.md",
    _REPO / "templates" / "codex" / ".agents" / "skills" / "pm-env" / "SKILL.md",
    _REPO / "templates" / "opencode" / ".claude" / "skills" / "pm-env" / "SKILL.md",
)
_CLAUDE_DELEGATE_CARDS = (
    _REPO / ".claude" / "skills" / "pm-dev-delegate" / "SKILL.md",
    _REPO / "templates" / "claude_code" / ".claude" / "skills"
    / "pm-dev-delegate" / "SKILL.md",
    _REPO / "templates" / "opencode" / ".claude" / "skills"
    / "pm-dev-delegate" / "SKILL.md",
)


def _card_with_operational_details(path: Path) -> str:
    """상시 카드와 T-0678 sibling 상황별 참조를 하나의 문서 표면으로 읽는다."""
    text = path.read_text(encoding="utf-8")
    details = path.parent / "references" / "operational-details.md"
    if details.is_file():
        text += "\n" + details.read_text(encoding="utf-8")
    return text
_TIMEOUT_CONTRACT_PATH_EXEMPTIONS = {
    # 이 카드 안 cross pm_delegate 블록은 존재하지만 Codex 실행 표면에는 Claude Bash DEFAULT가
    # 적용되지 않는다. 면제 근거는 mirror 여부가 아니라 호출 하네스의 실제 timeout 계약이다.
    "templates/codex/.agents/skills/pm-dev-delegate/SKILL.md":
        "Codex native spawn_agent override에는 Claude Bash DEFAULT 1800초가 적용되지 않음",
    "templates/codex/.agents/skills/pm-review/SKILL.md":
        "Codex exec_command override에는 Claude Bash DEFAULT 1800초가 적용되지 않음",
    "templates/codex/.agents/skills/pm-review/references/operational-details.md":
        "Codex operational detail에도 Claude Bash DEFAULT 1800초가 적용되지 않음",
}
_TOOLS = _REPO / ".project_manager" / "tools"

# 채택자 산출물을 바꾸는 critical env — 하나라도 ship 템플릿에서 소실되면 fail-loud.
_CRITICAL_ENV_KEYS = ("BASH_DEFAULT_TIMEOUT_MS", "BASH_MAX_TIMEOUT_MS")


def _load(path: Path) -> dict:
    """settings.json 을 파싱한다 (깨진 JSON = fail-loud·재직렬화 파손 감지)."""
    assert path.is_file(), f"settings.json 부재: {path}"
    return json.loads(path.read_text(encoding="utf-8"))


def _load_tool(name: str):
    spec = importlib.util.spec_from_file_location(name, _TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _harness_bash_cap_sec() -> float:
    """출하 template 이 선언한 하네스 Bash 상한(초) — 엔진 벽시계가 넘으면 안 되는 천장."""
    env = _load(_SHIP_TEMPLATE).get("env", {})
    return int(env["BASH_MAX_TIMEOUT_MS"]) / 1000.0


def _claude_bash_timeouts_ms() -> tuple[int, int]:
    """Claude의 (timeout 미지정 기본값, 호출자가 명시할 수 있는 상한)."""
    env = _load(_SHIP_TEMPLATE).get("env", {})
    return int(env["BASH_DEFAULT_TIMEOUT_MS"]), int(env["BASH_MAX_TIMEOUT_MS"])


def _opencode_bash_cap_sec() -> float:
    text = _OPENCODE_INSTRUCTIONS.read_text(encoding="utf-8")
    match = re.search(r"OPENCODE_EXPERIMENTAL_\s*\n?\s*BASH_DEFAULT_TIMEOUT_MS=(\d+)", text)
    assert match, "opencode PM 지침에 Bash 상한 export 부재"
    return int(match.group(1)) / 1000.0


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


# ── max(primary+fallback 실행 경로) + 여유 ≤ 명시 timeout 상한(MAX) ────────────────
# 둘 다 **우리 소유**인데 값이 정확히 같으면(구 1800 vs 1800) 여유가 0이다 — 엔진이 자기 타임아웃을
# 분류(인프라 실패 → 폴백)하고 부분 산출물을 raw 에 박제하기 전에 하네스가 먼저 프로세스를 죽일 수
# 있고, 그러면 아무 진단도 안 남는다(위임 kill 누적 8회가 전부 원인 불명이었던 축 중 하나 ·
# 백그라운드 커맨드가 **무출력으로** kill 된 실측 2건). 엔진 백스톱이 하네스별로 갈린 뒤로 좌변은
# **선언된 값 중 최댓값**이다 — 로컬 GPU 축(장시간 위임)이 이 부등식을 지배한다.

def test_declared_wall_backstops_stay_under_harness_bash_cap():
    """유효 primary+fallback 최악 예산 + 여유가 claude MAX/opencode 상한 모두 이하."""
    delegate = _load_tool("pm_delegate")
    margin = delegate._HARNESS_CAP_MARGIN_SEC
    worst = delegate.max_declared_execution_path_budget()
    assert worst == 29220  # opencode 2시도: 2×(14400 + 2×90 + 3×10 정리)
    # codex→opencode 직접 반려 시나리오: 각 timeout/retry 뒤 wait+drain 10초를 시도마다 산입.
    assert delegate._harness_timeout_budget("codex", 3600) + \
        delegate._harness_timeout_budget("opencode", 14400) == 18220
    for surface, cap in (
            ("claude settings", _harness_bash_cap_sec()),
            ("opencode instructions", _opencode_bash_cap_sec())):
        assert worst + margin <= cap, (
            f"{surface}: 최악 primary+fallback {worst}s + 여유 {margin}s > 상한 {cap:.0f}s — "
            "엔진 분류/부분 산출물 박제 전에 하네스가 선행 kill")


def test_claude_default_timeout_is_tight_and_not_a_delegation_budget():
    """DEFAULT는 일반 명령용으로 MAX보다 작고, 의도적으로 위임 최악 예산을 담지 않는다.

    이 단언은 최악 경로 부등식을 DEFAULT에도 잘못 적용해 다시 8시간으로 올리는 회귀를 막는다.
    """
    default_ms, max_ms = _claude_bash_timeouts_ms()
    worst_ms = _load_tool("pm_delegate").max_declared_execution_path_budget() * 1000
    assert default_ms < max_ms, (
        f"Claude Bash DEFAULT {default_ms}ms >= MAX {max_ms}ms — 일반 무-파라미터 명령이 "
        "명시 장시간 호출과 같은 수명으로 되돌아감")
    assert default_ms < worst_ms, (
        f"Claude Bash DEFAULT {default_ms}ms가 위임 최악 예산 {worst_ms}ms를 담음 — "
        "DEFAULT는 의도적으로 위임 예산보다 작아야 하고 위임 호출층이 MAX를 명시해야 함")


def test_cross_delegate_cards_explicitly_request_claude_bash_max_timeout():
    """Claude가 소비하는 cross 위임 카드가 호출층 Bash timeout을 잃으면 fail-loud."""
    default_ms, max_ms = _claude_bash_timeouts_ms()
    contract = f"`timeout: {max_ms}`(ms)을 반드시"
    for path in _CLAUDE_DELEGATE_CARDS:
        text = path.read_text(encoding="utf-8")
        assert contract in text, (
            f"{path}: cross 위임 실 실행의 Claude Bash 명시 timeout 부재 — "
            f"DEFAULT {default_ms}ms에서 위임 false-kill 가능")
        assert "CLI `--timeout`" in text, (
            f"{path}: Bash 호출층 timeout과 pm_delegate turn timeout 구분 소실")


def _execution_command_budget_seconds(card_text: str) -> list[tuple[str, int]]:
    """카드가 담은 장시간 엔진 커맨드를 찾아 그 엔진의 최악 예산을 계산한다.

    스킬 이름을 열거하지 않는다. 새 카드가 같은 실행 커맨드를 담으면 자동 대상이 된다. pytest처럼
    엔진 장시간 예산 소유자가 아닌 커맨드는 결과에 없어서 일반 Bash DEFAULT를 그대로 쓴다.
    """
    budgets: list[tuple[str, int]] = []
    delegate = _load_tool("pm_delegate")
    external = _load_tool("external_review")
    command_budgets = {
        "pm_delegate.py": delegate.max_declared_execution_path_budget(),
        "external_review.py": int(
            external.reviewer_profile(external.DEFAULT_REVIEWER_CMD).wall_timeout
        ),
    }
    for script, budget in command_budgets.items():
        if re.search(
                rf"(?:python3|python|py(?:\s+-\d+(?:\.\d+)?)?)\s+"
                rf"\.project_manager/tools/{re.escape(script)}(?:\s|`)",
                card_text):
            budgets.append((script, budget))
    return budgets


def _long_engine_command_markdown(root: Path = _REPO) -> dict[Path, list[tuple[str, int]]]:
    """root 아래 PM 엔진 커맨드 보유 Markdown을 내용으로 자동 판정한다.

    카드/위키 이름이나 디렉터리를 열거하지 않는다. 테스트 fixture와 VCS 내부만 제외하고, 새 출하
    Markdown이 두 엔진 커맨드를 담는 순간 자동 대상이 된다.
    """
    targets: dict[Path, list[tuple[str, int]]] = {}
    for path in repo_owned_paths(root, ".", mode=OWNED):
        if path.suffix != ".md":
            continue
        relative = path.relative_to(root)
        if relative.parts and relative.parts[0] in {".git", "tests"}:
            continue
        if relative.parts[:2] == (".project_manager", ".local"):
            continue
        budgets = _execution_command_budget_seconds(path.read_text(encoding="utf-8"))
        if budgets:
            targets[path] = budgets
    return targets


def test_all_long_engine_command_markdown_declares_explicit_bash_timeout():
    """엔진 예산 > Bash DEFAULT인 모든 PM-facing Markdown이 명시 timeout을 가진다."""
    default_ms, max_ms = _claude_bash_timeouts_ms()
    default_sec = default_ms // 1000
    contract = f"`timeout: {max_ms}`(ms)을 반드시"
    targets = _long_engine_command_markdown()
    assert targets, "장시간 엔진 커맨드 카드 분류가 비었음(vacuous pass)"
    for path, budgets in targets.items():
        over_default = [(script, budget) for script, budget in budgets if budget > default_sec]
        if not over_default:
            continue
        relative = path.relative_to(_REPO).as_posix()
        exemption = _TIMEOUT_CONTRACT_PATH_EXEMPTIONS.get(relative)
        if exemption is not None:
            assert exemption.strip(), f"{relative}: timeout 계약 면제 사유가 비었음"
            continue
        text = path.read_text(encoding="utf-8")
        assert contract in text, (
            f"{path}: 엔진 최악 예산 {over_default}가 Bash DEFAULT {default_sec}s를 넘는데 "
            f"호출층 명시 timeout {max_ms}ms 계약 부재")
        assert "CLI `--timeout`" in text, (
            f"{path}: 호출층 Bash timeout과 엔진 CLI timeout 구분 소실")

    # 짧은 pytest 카드는 장시간 엔진 실행 커맨드가 아니므로 계약 강제 대상이 아님을 실제 분류로 잠근다.
    regression = _card_with_operational_details(
        _REPO / ".claude/skills/pm-regression/SKILL.md")
    assert "pytest" in regression and _execution_command_budget_seconds(regression) == []


def test_new_markdown_with_engine_command_is_automatically_classified(tmp_path):
    """새 파일명을 가드에 등록하지 않아도 커맨드 보유 여부만으로 대상이 된다."""
    new_doc = tmp_path / "future" / "new_surface.md"
    new_doc.parent.mkdir()
    new_doc.write_text(
        "```bash\npython3 .project_manager/tools/external_review.py --ticket T-9999\n```\n",
        encoding="utf-8",
    )
    with pytest.warns(RepoFilesFallbackWarning, match="filesystem 전수 순회"):
        targets = _long_engine_command_markdown(tmp_path)
    assert list(targets) == [new_doc]
    assert targets[new_doc][0][0] == "external_review.py"


def test_all_pm_env_cards_match_shipped_harness_caps():
    """pm-env 4사본이 분리된 Claude DEFAULT/MAX와 opencode 상한을 정확히 광고한다."""
    default_ms, max_ms = _claude_bash_timeouts_ms()
    assert int(_opencode_bash_cap_sec() * 1000) == max_ms
    for path in _PM_ENV_CARDS:
        text = _card_with_operational_details(path)
        assert f"`BASH_DEFAULT_TIMEOUT_MS`(ms·출하 기본 {default_ms}=30분)" in text, path
        assert f"`BASH_MAX_TIMEOUT_MS`(ms·출하 기본 {max_ms}=8시간 8분 20초)" in text, path
        assert f"OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS={max_ms}" in text, path


def test_harness_bash_cap_covers_local_gpu_delegation():
    """하네스 상한이 **실측된 3시간 로컬 GPU 위임**을 담는다.

    상한을 안 올리면 엔진 백스톱을 아무리 올려도 30분에 하네스가 먼저 죽인다(무출력 kill·실물 관찰).
    이 단언이 red 면 출하 template 의 Bash 상한이 되돌려진 것이다."""
    assert _harness_bash_cap_sec() > 3 * 3600.0


def test_harness_bash_cap_margin_is_positive():
    """정리는 실행식에 직접 들고, 공용 보조 여유가 출하 상한의 실제 공간 안에 들어간다."""
    delegate = _load_tool("pm_delegate")
    relay = _load_tool("pm_relay")
    # 부모 wait 5초 + pipe drain 5초는 margin이 아니라 매 시도 실행 예산에 직접 든다.
    assert relay.process_cleanup_budget_per_attempt() == 10
    assert delegate._HARNESS_CAP_KILL_GRACE_BUDGET_SEC == 10
    measured = delegate._HARNESS_CAP_MEASURED_AUX_BUDGET_SEC
    assert delegate._HARNESS_CAP_MARGIN_SEC == relay.HARNESS_CAP_MARGIN_SEC
    assert delegate._HARNESS_CAP_MARGIN_SEC == ((measured + 9) // 10) * 10 == 10
    actual_slack = _harness_bash_cap_sec() - delegate.max_declared_execution_path_budget()
    assert actual_slack >= 2 * delegate._HARNESS_CAP_MARGIN_SEC, (
        f"출하 상한 실여유 {actual_slack:g}s가 측정 기반 margin "
        f"{delegate._HARNESS_CAP_MARGIN_SEC}s의 2배 미만")
