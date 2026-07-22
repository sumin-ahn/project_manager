"""codex 어댑터 위임 규약 회귀 가드 (T-0402·ADR-0070).

codex 어댑터의 위임 4축은 **`.codex/agents/*.toml` custom agent** 로 표현된다 — claude 의
`.claude/agents/*.md`·opencode 의 `.opencode/agents/*.md` 에 대응하는 codex 등가물(md→TOML
재표현·G6). 위임 = codex multi_agent **in-session spawn**(부모 sandbox 상속·`codex exec --agent`
플래그 부재라 외부 프로세스 위임 없음·D1). PM 은 메인세션이라 `pm.toml` 은 **없다**(load-bearing
absence·opencode 의 `pm.md` primary 에 해당하는 파일 부재).

이 테스트는 그 어댑터 계약을 회귀 가드한다:
  (a) `.codex/agents/{architect,code-reviewer,developer,researcher}.toml` 4축 실재 + `pm.toml` 부재.
  (b) 각 TOML 이 `tomllib` 로 파싱된다(포맷 유효·triple-quote developer_instructions 안전).
  (c) 필수 필드 3종 `name`/`description`/`developer_instructions` 존재 + `name` == 파일 stem.
  (d) `model` 키 **부재**(D5 — 사용자 config 기본 상속·harness-특수 모델 분기 0).
  (e) `sandbox_mode` 존재 + 역할별 값(쓰기 축 developer/architect=`workspace-write`·읽기 축
      code-reviewer/researcher=`read-only`).
  (f) `{{PROJECT_NAME}}` 토큰이 있어 pm_import/pm_update(@render)의 결정적 치환 타깃이 된다.
  (g) developer_instructions 가 공통 코어 `AGENTS.md` 를 부트스트랩 진입으로 참조(D3 C-v2 — codex
      전용 정적 진입 doc 없음·방법론은 공통 코어 + TOML + 스킬로 전달).

정적 계약은 stdlib(tomllib·Python 3.11+)로 검사하고, command policy는 설치된 codex CLI의
`execpolicy check`로 실제 판정한다(CLI 부재 환경만 명시 skip).
미러: `tests/test_opencode_adapter_delegation.py`.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CODEX = REPO / "templates" / "codex"
AGENTS_DIR = CODEX / ".codex" / "agents"
AGENTS_MD = CODEX / "AGENTS.md"
CONFIG = CODEX / ".codex" / "config.toml"
COMMAND_RULES = CODEX / ".codex" / "rules" / "default.rules"

AGENT_NAMES = ("architect", "code-reviewer", "developer", "researcher")
REQUIRED_FIELDS = ("name", "description", "developer_instructions")
PROJECT_NAME_TOKEN = "{{PROJECT_NAME}}"

# 역할별 sandbox_mode — 쓰기 필요(코드 편집·설계 초안) vs 읽기 전용(검토·조사). (ADR-0070 인터페이스)
SANDBOX_BY_AGENT = {
    "developer": "workspace-write",     # 코드 + 테스트 쓰기
    "architect": "workspace-write",     # 설계 초안 문서 쓰기
    "code-reviewer": "read-only",       # 검토 = 읽기·회귀 실행 (generate ≠ evaluate)
    "researcher": "read-only",          # gather = 읽기 전용
}


def _toml_path(name: str) -> Path:
    return AGENTS_DIR / f"{name}.toml"


def _load(name: str) -> dict:
    with open(_toml_path(name), "rb") as fh:
        return tomllib.load(fh)


# ── (a) 4축 실재 + pm.toml 부재 ─────────────────────────────────────────────

def test_agent_toml_files_present():
    """`.codex/agents/{architect,code-reviewer,developer,researcher}.toml` 4축이 실재한다."""
    assert AGENTS_DIR.is_dir(), f"codex agents 디렉토리 없음: {AGENTS_DIR}"
    for name in AGENT_NAMES:
        assert _toml_path(name).is_file(), f"codex agent 정의 없음: {_toml_path(name)}"


def test_no_pm_toml_load_bearing_absence():
    """`.codex/agents/pm.toml` 은 **없다** — PM=메인세션(ADR-0070 D1·load-bearing absence).

    codex 는 headless 명명-agent 타깃(`codex exec --agent`)이 없어 opencode 의 `pm.md`(mode:
    primary·relay spawn 타깃)에 해당하는 파일을 두지 않는다. relay 는 `codex exec` 메인세션에
    부트스트랩 프롬프트를 주입해 PM 을 구동한다(T-0404). pm.toml 이 생기면 이 결정이 깨진 것."""
    assert not _toml_path("pm").exists(), (
        "codex 에 pm.toml 이 존재 — PM=메인세션 결정(ADR-0070 D1) 위반. "
        "PM 은 codex exec 메인세션이 담당하며 명명 agent 로 두지 않는다."
    )


def test_agent_dir_has_exactly_the_four_axes():
    """`.codex/agents/` 에 정확히 4개 TOML(4축)만 있고 그 외 TOML 은 없다(pm.toml 등 유입 차단)."""
    tomls = sorted(p.stem for p in AGENTS_DIR.glob("*.toml"))
    assert tomls == sorted(AGENT_NAMES), (
        f".codex/agents TOML 집합이 4축과 불일치 — 예상 {sorted(AGENT_NAMES)}, 실제 {tomls}"
    )


# ── (b) tomllib 파싱 ────────────────────────────────────────────────────────

def test_all_toml_parse():
    """각 agent TOML 이 tomllib 로 파싱된다(포맷 유효·triple-quote developer_instructions 안전·G6)."""
    for name in AGENT_NAMES:
        try:
            _load(name)
        except tomllib.TOMLDecodeError as exc:  # pragma: no cover - 실패 시 진단
            raise AssertionError(f"{name}.toml 파싱 실패: {exc}") from exc


# ── (c) 필수 필드 3종 + name == stem ────────────────────────────────────────

def test_required_fields_present():
    """필수 필드 `name`/`description`/`developer_instructions` 가 모두 있고 비어있지 않다."""
    for name in AGENT_NAMES:
        data = _load(name)
        for field in REQUIRED_FIELDS:
            assert field in data, f"{name}.toml 에 필수 필드 {field!r} 없음"
            assert str(data[field]).strip(), f"{name}.toml 의 {field!r} 가 비어있음"


def test_name_matches_filename():
    """각 TOML 의 `name` 이 파일 stem 과 일치한다(codex 스폰 타깃 라벨 정합)."""
    for name in AGENT_NAMES:
        assert _load(name)["name"] == name, (
            f"{name}.toml 의 name 이 파일명과 불일치: {_load(name)['name']!r}"
        )


# ── (d) model 키 부재 (D5) ──────────────────────────────────────────────────

def test_no_model_key():
    """어느 agent TOML 에도 `model` 키가 없다 — 사용자 config 기본(gpt-5.5) 상속 (ADR-0070 D5).

    opencode 의 `{{OPENCODE_PRO_MODEL}}` pin·`resolve_opencode_model` 분기가 codex 엔 불필요하다
    (harness-특수 모델 해소 분기 0). `model` 이 박히면 그 단순성 결정이 깨진 것."""
    offenders = [name for name in AGENT_NAMES if "model" in _load(name)]
    assert not offenders, (
        f"codex agent TOML 에 model 키 잔존(D5 위반·사용자 config 기본 상속 깨짐): {offenders}"
    )


# ── (e) sandbox_mode 존재 + 역할별 값 ───────────────────────────────────────

def test_sandbox_mode_present_and_role_correct():
    """각 agent 에 `sandbox_mode` 존재 + 역할별 값 정합(쓰기 축 vs 읽기 축).

    developer/architect = workspace-write(코드·설계 초안 쓰기)·code-reviewer/researcher = read-only
    (검토·조사 = 읽기). 잘못 매핑되면 폴백 시 쓰기가 막히거나(모순) 읽기 축이 과권한을 얻는다."""
    for name in AGENT_NAMES:
        data = _load(name)
        assert "sandbox_mode" in data, f"{name}.toml 에 sandbox_mode 없음"
        assert data["sandbox_mode"] == SANDBOX_BY_AGENT[name], (
            f"{name}.toml sandbox_mode 가 {SANDBOX_BY_AGENT[name]!r} 가 아님: "
            f"{data['sandbox_mode']!r} (역할별 쓰기/읽기 권한 불일치)"
        )


def test_default_permissions_allow_routine_work_and_auto_review_escalations():
    """일상 작업은 무질의, sandbox 밖 저위험 요청은 auto-review — Claude settings 대응."""
    with open(CONFIG, "rb") as fh:
        config = tomllib.load(fh)
    assert config["sandbox_mode"] == "workspace-write"
    assert config["approval_policy"] == "on-request"
    assert config["approvals_reviewer"] == "auto_review"
    assert config["sandbox_workspace_write"]["network_access"] is False


def test_command_rules_allow_local_checkpoints_and_guard_dangerous_commands():
    """명령 정책 파일에 핵심 allow/prompt/forbidden 경계가 선언돼 있다."""
    rules = COMMAND_RULES.read_text(encoding="utf-8")
    for safe_prefix in (
        '["git", "add"]',
        '["git", "commit"]',
        '[["python", "python3", "py"], "-m", "pytest"]',
    ):
        assert safe_prefix in rules
    assert 'pattern = ["git", "push"]' in rules
    assert 'pattern = ["git", "reset"]' in rules
    assert 'decision = "prompt"' in rules
    for dangerous_prefix in ('["rm"]', '["git", "clean"]'):
        assert dangerous_prefix in rules


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (("git", "add", "--", "README.md"), "allow"),
        (("git", "commit", "-m", "checkpoint"), "allow"),
        (("git", "fetch", "origin"), "allow"),
        (("python3", "-m", "pytest", "tests/", "-q"), "allow"),
        (("python3", ".project_manager/tools/board.py", "list"), "allow"),
        (("git", "push", "origin", "task/main"), "prompt"),
        (("git", "push", "origin", "main", "--force"), "prompt"),
        (("git", "reset", "-q", "--hard"), "prompt"),
        (("git", "reset", "--soft", "HEAD~1"), "prompt"),
        (("rm", "-rf", "build"), "forbidden"),
        (("git", "clean", "-df"), "forbidden"),
        (("git", "clean", "--force"), "forbidden"),
    ],
)
def test_command_policy_decisions(argv: tuple[str, ...], expected: str):
    """실제 Codex execpolicy 해석기로 옵션 순서·축약형까지 판정한다."""
    executable = shutil.which("codex")
    if executable is None:
        pytest.skip("codex CLI unavailable")
    proc = subprocess.run(
        [executable, "execpolicy", "check", "--rules", str(COMMAND_RULES), "--", *argv],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["decision"] == expected


# ── (f) {{PROJECT_NAME}} 토큰 (pm_import/pm_update @render 치환 타깃) ─────────

def test_project_name_token_present_for_render():
    """각 agent 에 `{{PROJECT_NAME}}` 토큰이 있어 @render 결정적 치환 타깃이 된다 (manifest @render).

    `.codex/agents @render @source` 등록이라 pm_import/pm_update 가 채택자 값으로 치환한다 —
    토큰이 사라지면 채택자가 프레임워크 문구 그대로를 받는다(치환 no-op)."""
    for name in AGENT_NAMES:
        data = _load(name)
        blob = data["description"] + "\n" + data["developer_instructions"]
        assert PROJECT_NAME_TOKEN in blob, (
            f"{name}.toml 에 {PROJECT_NAME_TOKEN} 토큰 없음 — @render 치환 타깃 상실"
        )


# ── (g) 공통 코어 AGENTS.md 참조 (D3 C-v2) ──────────────────────────────────

def test_common_core_agents_md_exists():
    """codex 공통 코어 `AGENTS.md` 가 실재한다(방법론 진입 — codex 전용 정적 doc 없음·D3 C-v2)."""
    assert AGENTS_MD.is_file(), f"codex 공통 코어 AGENTS.md 없음: {AGENTS_MD}"


def test_developer_instructions_reference_common_core():
    """각 agent 의 developer_instructions 가 공통 코어 `AGENTS.md` 를 부트스트랩 진입으로 참조한다.

    D3 C-v2 — codex 전용 정적 진입 doc 이 없으므로 방법론 전달은 공통 코어 AGENTS.md(자동 로드) +
    이 TOML + `.agents/skills` 로 한다. agent 프롬프트가 공통 코어를 진입으로 가리켜야 부트스트랩이 성립."""
    for name in AGENT_NAMES:
        di = _load(name)["developer_instructions"]
        assert "AGENTS.md" in di, (
            f"{name}.toml developer_instructions 가 공통 코어 AGENTS.md 를 참조하지 않음 (D3 C-v2)"
        )
