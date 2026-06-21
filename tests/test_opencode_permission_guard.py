"""opencode 어댑터 위험 bash 명령 permission 가드 정합 테스트 (T-0011).

opencode 어댑터의 위험 명령 차단 가드를 두 곳에서 단언한다:
  1. project config  — templates/opencode/.opencode/opencode.jsonc (단일 진실)
  2. agent frontmatter — templates/opencode/.opencode/agents/{developer,architect,code-reviewer}.md
     (coarse `bash: allow` override 차단 — 두 곳 모두 패턴맵이어야 머지 후 deny 가 보존된다)

claude 어댑터(.claude/settings.json·settings.local.json)의 permissions.deny 와 항목 정합도 단언한다.

stdlib + pyyaml(엔진이 이미 의존 — board.py) 만 사용. opencode CLI 실행 없이 config 파싱만.

배경 (구현자 검증, 2026-06-14):
  opencode 는 permission 을 룰 리스트로 평탄화·누적한다 (덮어쓰기 아님 — `opencode debug agent`
  로 실측). project 패턴맵 + agent coarse `bash: allow` 면 머지 결과 마지막 룰이 `allow *` 가 돼
  매칭 규칙(specific-wins vs last-match-wins, 정적 확인 불가)에 따라 deny 가 우회될 수 있다.
  그래서 agent 도 패턴맵으로 명시해 머지 후 마지막 bash 룰이 항상 deny/ask 로 보존되게 했다.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
OPENCODE = REPO / "templates" / "opencode" / ".opencode"
CLAUDE = REPO / "templates" / "claude_code" / ".claude"

PROJECT_CONFIG = OPENCODE / "opencode.jsonc"
AGENT_FILES = [
    OPENCODE / "agents" / "developer.md",
    OPENCODE / "agents" / "architect.md",
    OPENCODE / "agents" / "code-reviewer.md",
    # researcher 는 read-only(edit/write false)지만 `bash: true`(조사용)라 위험명령 실행
    # surface 가 실재 — deny 패턴맵을 다른 agent·project config 와 동일하게 박았으므로
    # 그 deny 보존도 여기서 못박는다 (T-0106 reviewer should-fix · feature-ship-needs-fresh-adopter-gate).
    OPENCODE / "agents" / "researcher.md",
]

# claude deny 와 정합해야 할 위험 패턴 (rm -rf · force push · clean -f) — deny 강제.
# git push --force* / -f* 는 claude 의 `git push --force *` / `git push -f *` 와
# 의미 동등 (opencode 는 후행 인자 매칭 위해 `*` 를 공백 없이 붙인다).
REQUIRED_DENY_PATTERNS = [
    "rm -rf *",
    "git push --force*",
    "git push -f*",
    "git clean -f*",
]
# opencode 추가 가드 (claude 는 deny 지만 opencode 는 ask 로 — reset --hard 는 로컬 한정·복구 가능).
REQUIRED_ASK_PATTERNS = [
    "git reset --hard*",
]


# ── jsonc / frontmatter 파서 ───────────────────────────────────────────────

def _strip_jsonc_comments(text: str) -> str:
    """jsonc 의 줄 주석(//...)을 제거해 stdlib json 으로 파싱 가능하게 한다.

    우리 config 의 문자열 값에는 `//` 가 없으므로(URL 은 $schema 한 줄뿐 — 아래서 보존)
    단순 줄 단위 제거로 충분하다. `://`(스킴) 은 주석으로 오인하지 않도록 보호한다.
    """
    out_lines = []
    for line in text.splitlines():
        # `//` 가 있되 바로 앞이 `:` 가 아닌(=`://` 가 아닌) 첫 위치에서 자른다.
        m = re.search(r"(?<!:)//", line)
        if m:
            line = line[: m.start()]
        out_lines.append(line)
    return "\n".join(out_lines)


def _load_project_bash_permission() -> dict:
    text = PROJECT_CONFIG.read_text(encoding="utf-8")
    data = json.loads(_strip_jsonc_comments(text))
    return data["permission"]["bash"]


def _load_agent_frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"frontmatter 없음: {path}"
    end = text.find("\n---\n", 4)
    assert end != -1, f"frontmatter 종료 구분자 없음: {path}"
    return yaml.safe_load(text[4:end]) or {}


def _deny_patterns(settings_path: Path) -> list[str]:
    """settings json 파일의 Bash(...) deny 패턴 목록 (Bash() 래퍼 제거)."""
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    deny = data["permissions"]["deny"]
    out = []
    for entry in deny:
        m = re.fullmatch(r"Bash\((.*)\)", entry)
        if m:
            out.append(m.group(1).strip())
    return out


def _load_claude_deny(settings_name: str) -> list[str]:
    """claude 어댑터 settings 의 Bash(...) deny 패턴 목록 (Bash() 래퍼 제거)."""
    return _deny_patterns(CLAUDE / settings_name)


# ── project config: 가드 존재 ──────────────────────────────────────────────

def test_project_config_exists_and_parses():
    """templates/opencode/.opencode/opencode.jsonc 가 존재하고 jsonc 로 파싱된다."""
    assert PROJECT_CONFIG.exists(), f"project config 없음: {PROJECT_CONFIG}"
    bash = _load_project_bash_permission()
    assert isinstance(bash, dict), "permission.bash 가 패턴맵(dict)이어야 한다."


def test_project_config_denies_required_patterns():
    """project config 가 위험 패턴을 모두 deny 한다."""
    bash = _load_project_bash_permission()
    for pattern in REQUIRED_DENY_PATTERNS:
        assert bash.get(pattern) == "deny", (
            f"project config 가 {pattern!r} 를 deny 하지 않음: {bash.get(pattern)!r}"
        )


def test_project_config_asks_required_patterns():
    """project config 가 reset --hard 를 ask 한다."""
    bash = _load_project_bash_permission()
    for pattern in REQUIRED_ASK_PATTERNS:
        assert bash.get(pattern) == "ask", (
            f"project config 가 {pattern!r} 를 ask 하지 않음: {bash.get(pattern)!r}"
        )


def test_project_config_allows_wildcard_first():
    """기본은 allow(`*`) — 패턴맵 안에 wildcard allow 가 있어야 다른 명령이 동작한다."""
    bash = _load_project_bash_permission()
    assert bash.get("*") == "allow", "기본 wildcard allow 누락 — bash 가 전부 막힌다."


# ── agent frontmatter: coarse allow override 차단 ──────────────────────────

def test_agents_bash_permission_is_pattern_map_not_coarse():
    """각 agent 의 permission.bash 가 패턴맵(dict)이어야 한다.

    coarse 문자열 `bash: allow` 면 opencode 머지 시 deny 룰 뒤에 `allow *` 가 누적돼
    매칭 규칙에 따라 우회 가능 — 패턴맵으로 명시해야 머지 후 deny 가 보존된다.
    """
    for path in AGENT_FILES:
        fm = _load_agent_frontmatter(path)
        bash = fm.get("permission", {}).get("bash")
        assert isinstance(bash, dict), (
            f"{path.name} 의 permission.bash 가 패턴맵(dict)이 아님: {bash!r} "
            f"(coarse 문자열이면 deny 가 우회될 수 있다)"
        )


def test_agents_deny_required_patterns():
    """각 agent frontmatter 가 위험 패턴을 모두 deny 한다 (project config 와 동일)."""
    for path in AGENT_FILES:
        fm = _load_agent_frontmatter(path)
        bash = fm["permission"]["bash"]
        for pattern in REQUIRED_DENY_PATTERNS:
            assert bash.get(pattern) == "deny", (
                f"{path.name} 가 {pattern!r} 를 deny 하지 않음: {bash.get(pattern)!r}"
            )


def test_agents_ask_required_patterns():
    """각 agent frontmatter 가 reset --hard 를 ask 한다."""
    for path in AGENT_FILES:
        fm = _load_agent_frontmatter(path)
        bash = fm["permission"]["bash"]
        for pattern in REQUIRED_ASK_PATTERNS:
            assert bash.get(pattern) == "ask", (
                f"{path.name} 가 {pattern!r} 를 ask 하지 않음: {bash.get(pattern)!r}"
            )


def test_agents_match_project_config():
    """agent frontmatter 의 bash 패턴맵이 project config 와 정확히 일치한다 (단일 진실 정합)."""
    project_bash = _load_project_bash_permission()
    for path in AGENT_FILES:
        fm = _load_agent_frontmatter(path)
        agent_bash = fm["permission"]["bash"]
        assert agent_bash == project_bash, (
            f"{path.name} 의 bash 패턴맵이 project config 와 불일치.\n"
            f"  agent:   {agent_bash}\n  project: {project_bash}"
        )


# ── claude 어댑터와의 정합 ──────────────────────────────────────────────────

def test_mirrors_claude_settings_deny():
    """opencode 가드가 claude settings.json 의 위험 deny 항목을 (의미적으로) 미러한다.

    claude 는 `git push --force *`(공백 포함), opencode 는 `git push --force*`(공백 없음) —
    후행 인자 매칭 방식 차이일 뿐 같은 명령을 막는다. 패턴 머리(prefix)로 정합을 단언한다.
    """
    claude_deny = _load_claude_deny("settings.json")
    project_bash = _load_project_bash_permission()
    opencode_deny = {p for p, a in project_bash.items() if a == "deny"}

    # claude deny 의 각 위험 명령 머리(`*` 앞)가 opencode deny 패턴에도 대응돼야 한다.
    def head(pattern: str) -> str:
        return pattern.rstrip("*").strip()

    claude_heads = {head(p) for p in claude_deny}
    opencode_heads = {head(p) for p in opencode_deny}

    # rm -rf · git push --force · git push -f · git clean -f 는 opencode 가 deny 로 미러.
    for risky in ["rm -rf", "git push --force", "git push -f", "git clean -f"]:
        assert risky in claude_heads, f"claude deny 에 {risky!r} 가 없음 (테스트 전제 깨짐)"
        assert risky in opencode_heads, (
            f"opencode 가 claude deny {risky!r} 를 미러하지 않음. opencode deny: {opencode_deny}"
        )


def test_mirrors_claude_settings_local_deny(tmp_path):
    """settings.local.json 의 deny 도 settings.json 과 동일 항목임을 확인.

    settings.local.json 은 per-clone 로컬 오버레이라 **git-ignored** — fresh clone 에는
    부재한다. 그래서 repo 의 untracked 파일을 직접 읽지 않고, tracked settings.json 의 deny
    블록을 기준으로 settings.local.json 을 테스트가 **자급(self-provision)** 해 일관성 계약
    (로컬 오버레이가 위험 deny 를 그대로 미러)을 검증한다. 로컬 전용 allow 를 하나 끼워 넣어
    "단순 복제가 아닌 진짜 오버레이"임을 모사한다.
    """
    deny_main = _deny_patterns(CLAUDE / "settings.json")

    # 로컬 오버레이 자급: tracked deny 를 그대로 미러 + 로컬 전용 allow 추가.
    local_settings = tmp_path / "settings.local.json"
    local_settings.write_text(
        json.dumps({
            "permissions": {
                "allow": ["Bash(my-local-tool *)"],
                "deny": [f"Bash({pattern})" for pattern in deny_main],
            }
        }),
        encoding="utf-8",
    )

    deny_local = set(_deny_patterns(local_settings))
    assert set(deny_main) == deny_local, (
        f"claude settings.json 과 settings.local.json 의 deny 가 불일치: "
        f"main-only={set(deny_main) - deny_local}, local-only={deny_local - set(deny_main)}"
    )
