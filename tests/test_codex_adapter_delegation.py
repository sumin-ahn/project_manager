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
  (d) 5장 전부 `model`/`model_reasoning_effort` 가 **local.conf 렌더 토큰**이다 — 리터럴
      모델명이 박히면 채택자의 `delegate.<role>[.hard].{model,reasoning}` 선언이 실행면에
      닿지 못한다(하네스별 예외 없는 하나의 규칙).
  (e) `sandbox_mode` 존재 + 역할별 값(성장 사본 쓰기 축 developer/architect/code-reviewer=
      `workspace-write`·순수 읽기 축 researcher=`read-only`).
  (f) `{{PROJECT_NAME}}` 토큰이 있어 pm_import/pm_update(@render)의 결정적 치환 타깃이 된다.
  (g) developer_instructions 가 공통 코어 `AGENTS.md` 를 부트스트랩 진입으로 참조(D3 C-v2 — codex
      전용 정적 진입 doc 없음·방법론은 공통 코어 + TOML + 스킬로 전달).

정적 계약은 stdlib(tomllib·Python 3.11+)로 검사하고, command policy는 설치된 codex CLI의
`execpolicy check`로 실제 판정한다(CLI 부재 환경만 명시 skip).
미러: `tests/test_opencode_adapter_delegation.py`.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CODEX = REPO / "templates" / "codex"
AGENTS_DIR = CODEX / ".codex" / "agents"
AGENTS_MD = CODEX / "AGENTS.md"
CONFIG = CODEX / ".codex" / "config.toml"
COMMAND_RULES = CODEX / ".codex" / "rules" / "default.rules"
PM_DEV_DELEGATE = CODEX / ".agents" / "skills" / "pm-dev-delegate" / "SKILL.md"

AGENT_NAMES = ("architect", "code-reviewer", "developer", "researcher")
# developer 2티어(난제=hard) 프로필 — 4 역할 축이 아니라 developer 축의 티어 변주다(T-0448).
# 역할 축과 티어 프로필은 **같은 계약**을 쓴다(T-0766) — 5장 전부 model/reasoning 이 local.conf
# 렌더 토큰이다. 티어는 그 값이 normal 과 다른 프로필을 가리킬 뿐 규칙이 다르지 않다.
TIER_PROFILE_NAMES = ("developer-hard",)
# 카드 stem → 위임 토큰 접미. 손열거 사본이 아니라 stem 파생이라 카드가 늘어도 따라온다.
MODEL_TOKEN_RE = re.compile(r"^\{\{DELEGATE_MODEL_[A-Z][A-Z0-9_]*\}\}$")
REASONING_TOKEN_RE = re.compile(r"^\{\{DELEGATE_REASONING_[A-Z][A-Z0-9_]*\}\}$")
REQUIRED_FIELDS = ("name", "description", "developer_instructions")
PROJECT_NAME_TOKEN = "{{PROJECT_NAME}}"

# 역할별 sandbox_mode — code-reviewer도 성장 티켓 사본 자기 절 write가 필요하다(코드 write는 역할 금지).
SANDBOX_BY_AGENT = {
    "developer": "workspace-write",     # 코드 + 테스트 쓰기
    "architect": "workspace-write",     # 설계 초안 문서 쓰기
    "code-reviewer": "workspace-write", # 성장 사본 자기 절 write; 코드·board·git은 역할 계약으로 금지
    "researcher": "workspace-write",    # 성장 사본 자기 절 write(ADR-0089 전원 참여); 저장소는 읽기 전용 계약
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


def test_agent_dir_has_exactly_the_expected_toml_set():
    """`.codex/agents/` 에 4 역할 축 + developer 티어 프로필만 있고 그 외 TOML 은 없다(pm.toml 등 유입 차단).

    4 역할 축(AGENT_NAMES) + developer-hard 티어 프로필(TIER_PROFILE_NAMES·T-0448)이 예상 집합.
    pm.toml(PM=메인세션·load-bearing absence) 등 스트레이 TOML 유입은 여전히 차단한다."""
    expected = sorted(AGENT_NAMES + TIER_PROFILE_NAMES)
    tomls = sorted(p.stem for p in AGENTS_DIR.glob("*.toml"))
    assert tomls == expected, (
        f".codex/agents TOML 집합이 예상과 불일치 — 예상 {expected}, 실제 {tomls}"
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


# ── (d) model/reasoning 이 local.conf 렌더 토큰 (하나의 규칙) ────────────────

def test_model_and_reasoning_are_local_conf_render_tokens():
    """5장 전부 `model`·`model_reasoning_effort` 를 갖고 값이 **리터럴이 아니라 렌더 토큰**이다.

    옛 결정은 codex 역할 카드에서 두 키를 생략해 사용자 config 기본을 상속시켰다. 그 예외는
    폐기됐다 — codex 를 PM 하네스로 쓰는 채택자가 역할별 모델을 conf 로 제어하지 못했고,
    같은 트리의 hard 티어 카드는 이미 반대 계약이라 규칙이 둘로 갈려 있었다. 이제 세 하네스의
    모든 역할·티어 카드가 같은 규칙을 쓴다: 실값은 `local.conf` 의 렌더 파생물이다.
    리터럴 모델명이 박히면 그 단일 진실이 깨진 것이므로 형태까지 단언한다."""
    for name in AGENT_NAMES + TIER_PROFILE_NAMES:
        data = _load(name)
        for field, pattern in (
            ("model", MODEL_TOKEN_RE),
            ("model_reasoning_effort", REASONING_TOKEN_RE),
        ):
            assert field in data, (
                f"{name}.toml 에 {field!r} 없음 — 채택자 local.conf 선언이 실행면에 닿지 못한다"
            )
            assert pattern.fullmatch(str(data[field])), (
                f"{name}.toml 의 {field!r} 가 렌더 토큰이 아니다: {data[field]!r} "
                "— 리터럴이면 conf 단일 진실이 깨진다"
            )
    # 토큰 이름은 카드 stem 에서 파생한다(사본 0) — 표기가 갈리면 렌더가 해소하지 못한다.
    for name in AGENT_NAMES + TIER_PROFILE_NAMES:
        suffix = name.upper().replace("-", "_")
        data = _load(name)
        assert data["model"] == "{{DELEGATE_MODEL_" + suffix + "}}", data["model"]
        assert data["model_reasoning_effort"] == (
            "{{DELEGATE_REASONING_" + suffix + "}}"
        ), data["model_reasoning_effort"]


# ── developer 2티어(난제=hard) 프로필 계약 (T-0448·spike §3.2) ────────────────

def test_developer_hard_tier_profile_valid_and_overrides_model():
    """`developer-hard.toml` 티어 프로필이 실재·정합하고 모델·추론을 명시 보유한다.

    hard 는 normal 프로필을 상속하지 않는 별도 완전 세트다 — 두 키가 없으면 native 난제 경로가
    평시 프로필로 조용히 강등돼 티어 의도가 왜곡된다(fail-loud)."""
    path = _toml_path("developer-hard")
    assert path.is_file(), f"developer-hard 티어 프로필 없음: {path}"
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    # 포맷·필수 필드·name==stem (역할 축과 동일 계약)
    for field in REQUIRED_FIELDS:
        assert field in data and str(data[field]).strip(), f"developer-hard.toml 의 {field!r} 누락/빈값"
    assert data["name"] == "developer-hard", f"name 이 파일 stem 과 불일치: {data['name']!r}"
    # 쓰기 축(코드 편집) — developer 와 동일
    assert data["sandbox_mode"] == "workspace-write", (
        f"developer-hard sandbox_mode 가 workspace-write 아님: {data['sandbox_mode']!r}"
    )
    # 티어 프로필도 역할 축과 같은 계약 — model/reasoning 을 conf 렌더 토큰으로 갖는다.
    assert "model" in data and str(data["model"]).strip(), (
        "developer-hard 에 model 없음 — 티어 프로필도 자기 모델을 명시해야 한다"
    )
    assert "model_reasoning_effort" in data and str(data["model_reasoning_effort"]).strip(), (
        "developer-hard 에 model_reasoning_effort override 없음 — 난제는 상향 추론이 필요하다"
    )
    # @render 치환 타깃 + 공통 코어 진입 (역할 축과 동일)
    blob = data["description"] + "\n" + data["developer_instructions"]
    assert PROJECT_NAME_TOKEN in blob, f"developer-hard 에 {PROJECT_NAME_TOKEN} 토큰 없음 — @render 치환 타깃 상실"
    assert "AGENTS.md" in data["developer_instructions"], (
        "developer-hard developer_instructions 가 공통 코어 AGENTS.md 를 참조하지 않음 (D3 C-v2)"
    )


# ── (e) sandbox_mode 존재 + 역할별 값 ───────────────────────────────────────

def test_sandbox_mode_present_and_role_correct():
    """각 agent 에 `sandbox_mode` 존재 + 역할별 값 정합(쓰기 축 vs 읽기 축).

    developer/architect = workspace-write(코드·설계 초안 쓰기)· code-reviewer/researcher =
    workspace-write(성장 티켓 사본의 자기 절만 쓰는 역할 — 저장소 read-only 는 역할 계약·harvest 의
    자기 절 밖 bytes 일치 강제·git 감사로 제한한다). 순수 read-only sandbox 역할은 남지 않는다."""
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
    config_text = CONFIG.read_text(encoding="utf-8")
    assert "cross-harness 실위임은 전역 true로 완화하지 않고" in config_text
    assert "exec_command require_escalated 건별 승격" in config_text
    assert "pm_delegate.py reusable prefix 승인" in config_text
    # 승격 근거는 도구 승인이다 — 위임 스위치는 "위임을 해도 되는가" 만 정한다(동의 축 아님).
    assert "local.conf delegate.enabled 는 위임 허용 여부만 정하고" in config_text


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
    assert 'decision = "allow"' in rules
    for destructive in ("--force", "--force-with-lease", "--delete", "--mirror"):
        assert destructive in rules
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
        (("git", "push", "origin", "task/main"), "allow"),
        (("git", "push", "origin", "main"), "allow"),
        (("git", "push", "--force", "origin", "main"), "prompt"),
        (("git", "push", "-f", "origin", "task/main"), "prompt"),
        (("git", "push", "--force-with-lease", "origin", "main"), "prompt"),
        (("git", "push", "--delete", "origin", "old-branch"), "prompt"),
        (("git", "push", "--mirror", "origin"), "prompt"),
        (("git", "push", "origin", "main", "--force"), "prompt"),
        (("git", "push", "origin", "task/main", "--force-with-lease"), "prompt"),
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


# ── (h) pm-dev-delegate native spawn 계약 (T-0435) ──────────────────────────

_NATIVE_SPAWN_FIELDS = ("agent_type", "fork_turns", "task_name", "message")
_CUSTOM_ROLES = ("developer", "code-reviewer", "architect")
_FULL_HISTORY_CUSTOM_ROLE_REJECTION = (
    "Full-history forked agents inherit the parent agent type; omit agent_type, or spawn without "
    "a full-history fork."
)


def _native_spawn_is_accepted(call: dict[str, str]) -> bool:
    """Codex custom-role spawn의 최소 schema 계약을 fixture 수준에서 판정한다.

    실제 spawn은 test runner 밖 orchestrator API라 hermetic 테스트에서 호출하지 않는다. 대신 첫 실전의
    거부 원인(full-history fork + custom role)을 정확히 모델링해 문서 예시와 stale fixture를 함께
    검사한다. `fork_turns`는 self-contained 기본 `none` 또는 제한된 최근 맥락의 양의 정수만 허용한다.
    """
    if set(call) != set(_NATIVE_SPAWN_FIELDS):
        return False
    if call["agent_type"] not in _CUSTOM_ROLES:
        return False
    fork = call["fork_turns"]
    return fork == "none" or (fork.isdecimal() and int(fork) > 0)


def _documented_native_spawns(text: str) -> list[dict[str, str]]:
    """SKILL.md의 Python형 spawn 예시에서 native 인자를 추출한다(문서 구조 drift fail-loud)."""
    blocks = re.findall(r"spawn_agent\(\n(.*?)\n\)", text, flags=re.DOTALL)
    calls = []
    for block in blocks:
        fields = dict(re.findall(r'^\s*(agent_type|fork_turns|task_name)="([^"]+)",?$', block, re.MULTILINE))
        message = re.search(r'^\s*message="""', block, flags=re.MULTILINE)
        if message:
            fields["message"] = "present"
        calls.append(fields)
    return calls


def test_pm_dev_delegate_documents_only_native_codex_spawn_fields():
    """Codex 출하 스킬이 3개 성장 역할의 native spawn 4필드를 정확히 안내한다.

    `subagent_type`/`run_in_background`는 다른 harness의 필드라 Codex 실행 예시에 존재하면 첫 위임이
    schema 단계에서 거부된다. spawn 자체가 비동기 thread를 반환한다는 운영 규칙도 문서화해야 한다.
    """
    text = PM_DEV_DELEGATE.read_text(encoding="utf-8")
    calls = _documented_native_spawns(text)
    assert len(calls) == 3, "architect/developer/reviewer 각각 하나의 native spawn_agent 예시가 필요"
    assert {call.get("agent_type") for call in calls} == set(_CUSTOM_ROLES)
    assert all(_native_spawn_is_accepted(call) for call in calls)
    for stale_field in ("subagent_type", "run_in_background"):
        assert stale_field not in text, f"Codex pm-dev-delegate에 타 harness 필드 잔존: {stale_field}"
    assert "비동기로 진행" in text, "spawn 반환 thread의 비동기 운영 규칙 누락"


def test_codex_native_reviewer_can_write_only_its_round_file_by_role_contract():
    """codex reviewer 카드가 쓰기 대상을 라운드 파일 하나로 못박는다 (ADR-0090)."""
    reviewer = _load("code-reviewer")
    assert reviewer["sandbox_mode"] == "workspace-write"
    contract = reviewer["description"] + "\n" + reviewer["developer_instructions"]
    for term in (
        "NN-code-reviewer.md",
        "코드·board·git 수정 권한이 아니다",
        "PM은 위임 전후 git 상태를 감사",
        "지정된 라운드 파일 밖 파일 수정",
        # 명세·이전 라운드는 읽기 전용 입력이다.
        "spec.md",
        "rounds/",
    ):
        assert term in contract
    for stale in ("pm-ticket-section", "성장 티켓 사본", "자기 절"):
        assert stale not in contract, f"codex reviewer 카드에 옛 모델 어휘 잔존: {stale}"


def test_pm_dev_delegate_custom_roles_never_use_full_history_fork():
    """custom agent_type + fork_turns=all 조합은 첫 spawn 거부 재현 fixture로 red가 되어야 한다."""
    text = PM_DEV_DELEGATE.read_text(encoding="utf-8")
    calls = _documented_native_spawns(text)
    assert all(call["fork_turns"] != "all" for call in calls)
    assert 'custom `agent_type`과 full-history\n`fork_turns="all"`은 함께 쓸 수 없다' in text
    assert "양의 정수" in text

    stale_full_history = {
        "call": {
            "agent_type": "developer",
            "fork_turns": "all",
            "task_name": "orch_dev_t0435",
            "message": "implement",
        },
        # PM 7차 첫 custom-role developer spawn의 native 거부 원문 — 요약하면 API contract의
        # 중요한 원인(agent type 상속)이 사라져 stale all 조합 재발을 정확히 진단할 수 없다.
        "error": _FULL_HISTORY_CUSTOM_ROLE_REJECTION,
    }
    stale_claude_fields = {
        "subagent_type": "developer",
        "run_in_background": "true",
        "task_name": "orch_dev_t0435",
        "message": "implement",
    }
    assert stale_full_history["error"] == (
        "Full-history forked agents inherit the parent agent type; omit agent_type, or spawn without "
        "a full-history fork."
    )
    assert not _native_spawn_is_accepted(stale_full_history["call"]), (
        "sensitivity: custom role + full-history fork가 native spawn 가드에서 수락됨"
    )
    assert not _native_spawn_is_accepted(stale_claude_fields), (
        "sensitivity: stale Claude fields가 native spawn 가드에서 수락됨"
    )


def test_pm_dev_delegate_documents_codex_cross_harness_egress_bridge():
    """network-off를 유지한 cross 실위임의 두 계층(도구 승격+argv attestation).

    `--codex-egress-escalated`만 샌드박스 내 명령에 붙이면 권한이 생기지 않는다.
    카드가 정확한 Codex tool metadata와 플래그의 동반, dry-run 선행, 무음 native
    대체 금지를 모두 박아야 사용자 건별 승인 경계가 성립한다.
    """
    text = PM_DEV_DELEGATE.read_text(encoding="utf-8")
    for contract in (
        "Codex egress 건별 승격 (load-bearing)",
        "Codex egress: escalation required",
        'sandbox_permissions="require_escalated"',
        "--codex-egress-escalated",
        "호출층 attestation",
        'prefix_rule=["python3", ".project_manager/tools/pm_delegate.py"]',
        'prefix_rule=["py", ".project_manager/tools/pm_delegate.py"]',
        "delegate.enabled",
        "기본은 허용",
        "native Codex/GPT로 무음 대체하지 마라",
        "sandbox_workspace_write.network_access=true",
    ):
        assert contract in text, f"Codex cross-harness egress 계약 누락: {contract}"

    dry_run_i = text.index("`--dry-run`을 실행")
    permission_i = text.index('sandbox_permissions="require_escalated"')
    attestation_i = text.index("--codex-egress-escalated", permission_i)
    assert dry_run_i < permission_i < attestation_i
    assert 'prefix_rule=["python3", ".project_manager/tools/pm_delegate.py"]' in text
    assert 'prefix_rule=["python3"]' not in text


def test_codex_egress_bridge_does_not_leak_into_shared_harness_cards():
    """Codex tool metadata는 Claude/OpenCode가 byte-공유하는 카드의 계약이 아니다."""
    shared_cards = (
        REPO / ".claude" / "skills" / "pm-dev-delegate" / "SKILL.md",
        REPO / "templates" / "claude_code" / ".claude" / "skills"
        / "pm-dev-delegate" / "SKILL.md",
        REPO / "templates" / "opencode" / ".claude" / "skills"
        / "pm-dev-delegate" / "SKILL.md",
    )
    for path in shared_cards:
        text = path.read_text(encoding="utf-8")
        assert 'sandbox_permissions="require_escalated"' not in text, path
        assert "--codex-egress-escalated" not in text, path


def test_pm_update_dry_run_preserves_codex_native_delegate_override():
    """shared skill render 뒤에도 Codex file override가 update 대상으로 되돌아가지 않는다."""
    result = subprocess.run(
        [
            sys.executable, str(REPO / ".project_manager" / "tools" / "pm_update.py"),
            "--from", str(REPO), "--target", "codex", "--dry-run",
        ],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    delegate_rel = ".agents/skills/pm-dev-delegate/SKILL.md"
    assert not any(
        delegate_rel in line and ("[update]" in line or "[new]" in line)
        for line in result.stdout.splitlines()
    ), result.stdout
