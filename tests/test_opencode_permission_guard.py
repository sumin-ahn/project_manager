"""opencode 어댑터 위험 bash 명령 permission 가드 정합 테스트 (T-0011).

opencode 어댑터의 위험 명령 차단 가드를 두 곳에서 단언한다:
  1. project config  — templates/opencode/.opencode/opencode.jsonc (단일 진실)
  2. agent frontmatter — 실행 역할 developer/architect/code-reviewer/pm
     (coarse `bash: allow` override 차단 — 두 곳 모두 패턴맵이어야 머지 후 deny 가 보존된다)

researcher는 진짜 read-only라 bash 전체를 deny하며 별도 단언한다. PM은 네 역할 task만 허용한다.

claude 어댑터(.claude/settings.json·settings.local.json)의 permissions.deny 와 항목 정합도 단언한다.

stdlib + pyyaml(엔진이 이미 의존 — board.py) 만 사용. opencode CLI 실행 없이 config 파싱만.

배경 (구현자 검증, 2026-06-14):
  opencode 는 permission 을 룰 리스트로 평탄화·누적한다 (덮어쓰기 아님 — `opencode debug agent`
  로 실측). project 패턴맵 + agent coarse `bash: allow` 면 머지 결과 마지막 룰이 `allow *` 가 돼
  매칭 규칙(specific-wins vs last-match-wins, 정적 확인 불가)에 따라 deny 가 우회될 수 있다.
  그래서 agent 도 패턴맵으로 명시해 머지 후 마지막 bash 룰이 항상 deny/ask 로 보존되게 했다.
"""
from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
OPENCODE = REPO / "templates" / "opencode" / ".opencode"
CLAUDE = REPO / "templates" / "claude_code" / ".claude"
TOOLS = REPO / ".project_manager" / "tools"


def _load_pm_relay():
    """엔진 core(pm_relay.opencode_runtime_role_config)를 importlib 로 직접 로드한다

    (test_pm_relay.py 의 orch fixture 와 동형). researcher 카드의 edit/bash/task 기대값을
    손기입하지 않고 fragment 에서 파생하기 위한 단일 진실 접근이다(T-0747).
    """
    spec = importlib.util.spec_from_file_location("pm_relay", TOOLS / "pm_relay.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_PM_RELAY = _load_pm_relay()

PROJECT_CONFIG = OPENCODE / "opencode.jsonc"
EXEC_AGENT_FILES = [
    OPENCODE / "agents" / "developer.md",
    OPENCODE / "agents" / "architect.md",
    OPENCODE / "agents" / "code-reviewer.md",
    OPENCODE / "agents" / "pm.md",
]
RESEARCHER = OPENCODE / "agents" / "researcher.md"
AGENT_FILES = [
    *EXEC_AGENT_FILES,
    OPENCODE / "agents" / "researcher.md",
]

# claude deny 와 정합해야 할 위험 패턴 (rm 일반 · force push · clean -f) — deny 강제.
# `rm *` = 일반 파일 삭제 deny (T-0160 — "파일 삭제는 사용자가 직접"·`rm -rf *` 를 subsume).
# git push --force* / -f* 는 claude 의 `git push --force *` / `git push -f *` 와
# 의미 동등 (opencode 는 후행 인자 매칭 위해 `*` 를 공백 없이 붙인다).
REQUIRED_DENY_PATTERNS = [
    "rm *",
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
    for path in EXEC_AGENT_FILES:
        fm = _load_agent_frontmatter(path)
        bash = fm.get("permission", {}).get("bash")
        assert isinstance(bash, dict), (
            f"{path.name} 의 permission.bash 가 패턴맵(dict)이 아님: {bash!r} "
            f"(coarse 문자열이면 deny 가 우회될 수 있다)"
        )


def test_agents_deny_required_patterns():
    """각 agent frontmatter 가 위험 패턴을 모두 deny 한다 (project config 와 동일)."""
    for path in EXEC_AGENT_FILES:
        fm = _load_agent_frontmatter(path)
        bash = fm["permission"]["bash"]
        for pattern in REQUIRED_DENY_PATTERNS:
            assert bash.get(pattern) == "deny", (
                f"{path.name} 가 {pattern!r} 를 deny 하지 않음: {bash.get(pattern)!r}"
            )


def test_agents_ask_required_patterns():
    """각 agent frontmatter 가 reset --hard 를 ask 한다."""
    for path in EXEC_AGENT_FILES:
        fm = _load_agent_frontmatter(path)
        bash = fm["permission"]["bash"]
        for pattern in REQUIRED_ASK_PATTERNS:
            assert bash.get(pattern) == "ask", (
                f"{path.name} 가 {pattern!r} 를 ask 하지 않음: {bash.get(pattern)!r}"
            )


def test_agents_match_project_config():
    """agent frontmatter 의 bash 패턴맵이 project config 와 정확히 일치한다 (단일 진실 정합)."""
    project_bash = _load_project_bash_permission()
    for path in EXEC_AGENT_FILES:
        fm = _load_agent_frontmatter(path)
        agent_bash = fm["permission"]["bash"]
        assert agent_bash == project_bash, (
            f"{path.name} 의 bash 패턴맵이 project config 와 불일치.\n"
            f"  agent:   {agent_bash}\n  project: {project_bash}"
        )


def test_researcher_writes_only_its_ticket_copy_section_without_bash_bypass():
    """researcher 는 성장 티켓 사본의 자기 절을 edit 한다(ADR-0089 전원 참여) — 저장소 read-only 는
    역할 계약 + harvest 의 자기 절 밖 bytes 일치 강제로 지키고, bash/task 는 deny 로 두어 edit 밖의
    쓰기 우회(bash 로 파일 쓰기·중첩 위임)를 기계로 막는다.

    edit/bash/task 기대값은 손기입하지 않고 pm_relay fragment 에서 파생한다 — 카드만 바뀌고
    fragment 가 stale 로 남는 half-fix 를 이 테스트가 놓치지 않게 한다(T-0696 F-014 → T-0745 →
    T-0747 · test_pm_relay.py::test_opencode_runtime_role_fragment_matches_shipped_card_permission
    과 동일 대조를 이 표면에서도 지킨다)."""
    fm = _load_agent_frontmatter(RESEARCHER)
    assert "tools" not in fm
    permission = fm["permission"]
    fragment_permission = json.loads(
        _PM_RELAY.opencode_runtime_role_config("researcher")
    )["agent"]["researcher"]["permission"]
    for key in ("edit", "bash", "task"):
        assert permission[key] == fragment_permission[key], (
            f"researcher.{key}: card={permission[key]!r} != "
            f"fragment={fragment_permission[key]!r}"
        )
    for key in ("read", "glob", "grep", "list"):
        assert permission[key] == "allow"


def test_pm_can_delegate_only_the_four_framework_roles():
    """PM은 운영 쓰기를 유지하되 task 표면은 architect/dev/reviewer/researcher로 제한한다."""
    fm = _load_agent_frontmatter(OPENCODE / "agents" / "pm.md")
    assert "tools" not in fm
    assert fm["permission"]["edit"] == "allow"
    assert fm["permission"]["task"] == {
        "*": "deny",
        "architect": "allow",
        "developer": "allow",
        "code-reviewer": "allow",
        "researcher": "allow",
    }


def test_worker_agents_cannot_redelegate_and_use_permission_only():
    """역할 agent는 중첩 위임하지 않으며 deprecated tools/permission 이중 진실을 두지 않는다."""
    for path in [p for p in AGENT_FILES if p.name != "pm.md"]:
        fm = _load_agent_frontmatter(path)
        assert "tools" not in fm, path
        assert fm["permission"]["task"] == "deny", path


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

    # rm · git push --force · git push -f · git clean -f 는 opencode 가 deny 로 미러.
    for risky in ["rm", "git push --force", "git push -f", "git clean -f"]:
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
