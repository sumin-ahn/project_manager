"""하네스 분기 정적 가드 — 하네스 이름이 권한·경로·env·동의 축을 가르지 않는다 (T-0887).

claude·codex·opencode 는 사용자가 교차로 쓰는 같은 등급의 하네스다. 셋 다로 PM 을 돌리고 셋 다에
developer·architect·code-reviewer 를 위임한다. 그런데 엔진은 한때 추가 리뷰어 역할에만 저장소
거울·임시 홈·별도 동의 축을 붙였다 — 같은 행위(저장소 내용을 모델 API 로 보내는 것)에 채널마다
다른 규칙을 준 것이고 근거가 없다(PM 이 파일을 읽는 순간 같은 호출이 게이트 없이 일어난다).

**불변식**: 하네스 이름(claude·codex·opencode)이 접근 가능한 경로·env·동의 축·게이트 통과 여부를
바꾸지 않는다. 정당한 비대칭은 CLI 형식(argv 조립·어댑터 설치·dry-run 표기)뿐이고, 그 자리는
아래 원장에 사유와 함께 등재한다.

판정 방식: 엔진 `.project_manager/tools/*.py` 를 AST 로 훑어 조건식(If/While/IfExp/assert/
comprehension-if/match-case)의 서브트리에 **하네스 이름 그 자체**인 문자열 리터럴이 있는 자리를
센다. 부분 문자열(`.claude/skills/...` 같은 경로)은 하네스 정체 분기가 아니므로 세지 않는다 —
그쪽은 어댑터 레이아웃 상수이고 이 축의 판정 대상이 아니다.

원장은 **축소 방향으로만** 바꾼다(pm_principles §가드). 원장 밖 자리가 하나라도 나오면 실패이고,
원장 항목이 코드에서 사라져도 실패다(죽은 예외 금지).
"""

from __future__ import annotations

import ast
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

# 하네스 이름 — 리터럴이 이 값 **자체**일 때만 하네스 정체 분기로 센다.
HARNESS_NAMES = ("claude", "codex", "opencode")

# 원장 키 = (엔진 파일, 그 분기를 담은 함수, 하네스 이름).
BranchKey = tuple[str, str, str]


@dataclass(frozen=True)
class HarnessBranch:
    """등재된 하네스 분기 — 사유와 기대 개수.

    `reason` 은 **CLI 형식**임을 말해야 한다(argv 조립·어댑터 설치·dry-run 표기). 권한·경로·env·
    동의 축을 가르는 사유는 이 원장에 들어올 수 없다 — 그건 지워야 할 차등이지 등재 대상이 아니다.
    """

    reason: str
    expected_count: int = 1


# 하네스 이름이 분기 조건에 나오는 엔진 자리 전수. 전부 CLI 형식이고 권한 분기는 0 이다.
HARNESS_BRANCHES: dict[BranchKey, HarnessBranch] = {
    ("additional_reviewer.py", "_structured_reviewer_argv", "codex"): HarnessBranch(
        "argv 조립 — codex 는 `exec --json` 형식이라 relay 의 codex 빌더로 간다"),
    ("additional_reviewer.py", "_structured_reviewer_argv", "claude"): HarnessBranch(
        "argv 조립 — claude 는 `-p --output-format stream-json` 형식이라 별도 빌더로 간다"),
    ("additional_reviewer.py", "_structured_transport", "opencode"): HarnessBranch(
        "CLI 형식 — opencode 만 프롬프트를 stdin 이 아니라 `--file` 첨부로 받는다"),
    ("delegate_channel_guard.py", "decide", "claude"): HarnessBranch(
        "CLI 형식 — claude 네이티브 Task tool 이 있는 세션에서만 네이티브 경로를 고른다"),
    ("pm_config.py", "_print_delegate_model_guidance", "opencode"): HarnessBranch(
        "안내 문구 — opencode 만 로컬 GPU 모델 표기를 별도로 안내한다"),
    ("pm_delegate.py", "_build_target_argv", "codex"): HarnessBranch(
        "argv 조립 — codex 빌더 분기(형식 계약은 pm_relay 소유)"),
    ("pm_delegate.py", "_build_target_argv", "claude"): HarnessBranch(
        "argv 조립 — claude 빌더 분기(형식 계약은 pm_relay 소유)"),
    ("pm_delegate.py", "_dry_run_harness_annotations", "opencode"): HarnessBranch(
        "dry-run 표기 — opencode 만 `--dir`/`--file` 자리표시자를 미리보기에 덧붙인다",
        expected_count=2),
    ("pm_delegate.py", "_prepare_attempt_transport", "opencode"): HarnessBranch(
        "CLI 형식 — opencode 만 프롬프트 파일 sandbox 를 준비한다"),
    ("pm_import.py", "_build_runner_argv", "claude"): HarnessBranch(
        "어댑터 설치 — 라이브 스모크 러너의 claude CLI argv"),
    ("pm_import.py", "_build_runner_argv", "codex"): HarnessBranch(
        "어댑터 설치 — 라이브 스모크 러너의 codex CLI argv"),
    ("pm_import.py", "_build_runner_argv", "opencode"): HarnessBranch(
        "어댑터 설치 — 라이브 스모크 러너의 opencode CLI argv"),
    ("pm_import.py", "_real_models_runner", "opencode"): HarnessBranch(
        "어댑터 설치 — opencode 만 실 모델 목록을 CLI 로 조회한다"),
    ("pm_import.py", "add_harness", "claude"): HarnessBranch(
        "어댑터 설치 — claude 타깃의 어댑터 디렉터리 배치"),
    ("pm_import.py", "add_harness", "codex"): HarnessBranch(
        "어댑터 설치 — codex 타깃의 어댑터 디렉터리 배치"),
    ("pm_import.py", "main", "claude"): HarnessBranch(
        "어댑터 설치 — `--harness claude` 스캐폴드 선택"),
    ("pm_import.py", "main", "codex"): HarnessBranch(
        "어댑터 설치 — `--harness codex` 스캐폴드 선택"),
    ("pm_import.py", "run_fill", "codex"): HarnessBranch(
        "어댑터 설치 — codex 스캐폴드 채우기의 CLI 형식 분기"),
    ("pm_import.py", "run_fill", "opencode"): HarnessBranch(
        "어댑터 설치 — opencode 스캐폴드 채우기의 CLI 형식 분기"),
}

# 원장이 CLI 형식만 담는다는 계약을 문구로도 못박는다 — 아래 낱말이 사유에 있으면 그건 지워야 할
# 차등이지 등재 대상이 아니다(권한·경로·env·동의 축).
FORBIDDEN_REASON_WORDS = ("권한", "동의", "격리", "홈 ", "allowlist", "가시 범위")


def _enclosing_functions(tree: ast.Module) -> dict[int, str]:
    """모든 노드 → 그 노드를 감싸는 가장 안쪽 함수 이름."""
    owner: dict[int, str] = {id(tree): "<module>"}

    def walk(node: ast.AST, current: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = (child.name
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                    else current)
            owner[id(child)] = name
            walk(child, name)

    walk(tree, "<module>")
    return owner


def _condition_subtrees(tree: ast.Module) -> list[tuple[ast.AST, ast.AST]]:
    """(그 조건을 소유한 노드, 조건식) 목록 — 분기가 되는 자리 전부."""
    found: list[tuple[ast.AST, ast.AST]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.If, ast.While, ast.IfExp)):
            found.append((node, node.test))
        elif isinstance(node, ast.Assert):
            found.append((node, node.test))
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp,
                               ast.GeneratorExp)):
            for generator in node.generators:
                for condition in generator.ifs:
                    found.append((node, condition))
        elif isinstance(node, ast.match_case):
            found.append((node, node.pattern))
    return found


def _harness_names_in(condition: ast.AST) -> set[str]:
    """조건식 서브트리에 등장하는 **하네스 이름 그 자체**인 문자열 리터럴."""
    names: set[str] = set()
    for node in ast.walk(condition):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value.strip().lower()
            if value in HARNESS_NAMES:
                names.add(value)
    return names


def _measured_branches(sources: list[Path]) -> Counter[BranchKey]:
    """소스 전수에서 하네스 정체 분기를 (파일, 함수, 하네스) 키로 센다."""
    measured: Counter[BranchKey] = Counter()
    for path in sources:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        owner = _enclosing_functions(tree)
        for holder, condition in _condition_subtrees(tree):
            for name in _harness_names_in(condition):
                measured[(path.name, owner.get(id(holder), "<module>"), name)] += 1
    return measured


def _engine_sources() -> list[Path]:
    return sorted(TOOLS.glob("*.py"))


def test_engine_sources_are_actually_scanned():
    """스캔 대상이 비면 판정 불능이다 — 초록이 사각의 증거가 되지 않게 먼저 센다."""
    sources = _engine_sources()
    assert len(sources) >= 20, [path.name for path in sources]
    assert _measured_branches(sources), "하네스 분기 0 — 스캐너가 무력화됐다"


def test_no_harness_branch_outside_the_ledger():
    """원장 밖에서 하네스 이름으로 갈리는 자리가 없다."""
    measured = _measured_branches(_engine_sources())
    unregistered = sorted(key for key in measured if key not in HARNESS_BRANCHES)
    assert not unregistered, (
        "원장 미등재 하네스 분기 — 권한·경로·env·동의 축을 가르는 분기라면 지우고, CLI 형식이면 "
        f"사유와 함께 HARNESS_BRANCHES 에 등재하라: {unregistered}"
    )


def test_no_dead_ledger_entry():
    """코드에서 사라진 원장 항목을 남기지 않는다 (원장은 축소 방향으로만 움직인다)."""
    measured = _measured_branches(_engine_sources())
    stale = sorted(
        key for key, entry in HARNESS_BRANCHES.items()
        if measured.get(key, 0) != entry.expected_count
    )
    assert not stale, (
        "사라졌거나 개수가 바뀐 하네스 분기 — 원장에서 제거/재검토하라: "
        f"{[(key, HARNESS_BRANCHES[key].expected_count, measured.get(key, 0)) for key in stale]}"
    )


@pytest.mark.parametrize("key", sorted(HARNESS_BRANCHES), ids=lambda k: "::".join(k))
def test_every_ledger_reason_is_a_cli_form_reason(key):
    """등재 사유는 CLI 형식만 말한다 — 권한·경로·env·동의 축은 등재 대상이 아니라 삭제 대상이다."""
    entry = HARNESS_BRANCHES[key]
    assert entry.reason.strip(), key
    offending = [word for word in FORBIDDEN_REASON_WORDS if word in entry.reason]
    assert not offending, (
        f"{key} 의 등재 사유가 CLI 형식이 아니다({offending}) — 그 분기는 지워야 한다"
    )


def test_no_permission_or_env_branch_on_harness_name(tmp_path):
    """감도 — 하네스 이름으로 env/권한을 가르는 분기를 새로 넣으면 원장 밖 자리로 잡힌다."""
    injected = tmp_path / "pm_delegate.py"
    injected.write_text(
        "def build_env(harness):\n"
        "    out = {}\n"
        "    if harness == 'codex':\n"
        "        out['HOME'] = '/tmp/codex-home'\n"
        "    return out\n",
        encoding="utf-8",
    )

    measured = _measured_branches([injected])

    assert measured == Counter({("pm_delegate.py", "build_env", "codex"): 1})
    assert ("pm_delegate.py", "build_env", "codex") not in HARNESS_BRANCHES


def test_a_removed_site_makes_its_ledger_entry_dead(tmp_path):
    """감도 — 등재된 자리를 코드에서 지우고 원장을 그대로 두면 죽은 예외로 잡힌다."""
    emptied = tmp_path / "pm_import.py"
    emptied.write_text("def add_harness(name):\n    return name\n", encoding="utf-8")

    measured = _measured_branches([emptied])

    assert measured.get(("pm_import.py", "add_harness", "codex"), 0) == 0
    assert ("pm_import.py", "add_harness", "codex") in HARNESS_BRANCHES


def test_adapter_path_literals_are_not_harness_identity_branches(tmp_path):
    """경계 — `.claude/skills/...` 같은 경로 리터럴은 하네스 정체 분기가 아니다."""
    paths_only = tmp_path / "private_refs.py"
    paths_only.write_text(
        "def shipping_paths(rel):\n"
        "    if rel.startswith('.claude/skills/'):\n"
        "        return True\n"
        "    return False\n",
        encoding="utf-8",
    )

    assert _measured_branches([paths_only]) == Counter()
