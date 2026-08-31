"""하네스·위임 파리티 정적 가드 — 이름이 권한·경로·env·동의 축을 가르지 않는다 (T-0887).

claude·codex·opencode 는 사용자가 교차로 쓰는 같은 등급의 하네스다. 셋 다로 PM 을 돌리고 셋 다에
developer·architect·code-reviewer 를 위임한다. 그런데 엔진은 한때 추가 리뷰어 역할에만 저장소
거울·임시 홈·별도 동의 축을 붙였다 — 같은 행위(저장소 내용을 모델 API 로 보내는 것)에 채널마다
다른 규칙을 준 것이고 근거가 없다(PM 이 파일을 읽는 순간 같은 호출이 게이트 없이 일어난다).

**불변식 둘**:

1. 하네스 이름(claude·codex·opencode)이 접근 가능한 경로·env·동의 축·게이트 통과 여부를 바꾸지
   않는다. 정당한 비대칭은 CLI 형식뿐이고, 그 자리는 아래 원장에 사유와 함께 등재한다.
2. 위임자는 피위임자에게 자신과 같은 권한을 준다 — 위임 방향·하네스 조합과 무관하다. 위임 경로
   에서 피위임자 권한을 좁히는 자리는 역할축(generate≠evaluate) 하나뿐이고 그것도 전수 등재한다.

판정 방식은 셋이다.

- **조건 분기** — 조건식(If/While/IfExp/assert/comprehension-if/match-case) 서브트리에 하네스
  이름 그 자체인 문자열 리터럴이 있는 자리. 부분 문자열(`.claude/skills/...` 같은 경로)은 어댑터
  레이아웃 상수라 세지 않는다.
- **선언표** — 하네스 이름을 **키로 갖는 매핑 리터럴**. 조건식만 보면 `{"opencode": ("HOME",)}`
  같은 표 형태의 차등이 통째로 시야 밖에 남는다(그 형상은 엔진이 이미 쓴다).
- **위임 권한 자리** — 역할축 집합(`WRITE_ROLES`·`READ_ROLES`·`RESUME_MUTATING_ROLES`) 참조와
  sandbox 강등 리터럴(`read-only`). 하네스 이름이 없어도 피위임자 권한을 좁히는 자리다.

원장 밖 자리가 하나라도 나오면 실패이고, 원장 항목이 코드에서 사라져도 실패다(죽은 예외 금지).
총량은 `LEDGER_CEILING` 이하여야 한다 — **새 등재는 다른 항목을 지워야만 가능하다**(축소 전용).
그리고 사유 문구가 아니라 **분기 몸통의 효과**로 권한 차등을 판정한다: 하네스 이름 조건이 감싸는
서브트리가 env·HOME·PATH·CWD·PERMISSION·SANDBOX·MODE 를 건드리면 등재 여부와 무관하게 실패다.
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

# 위임 권한 자리 — 역할축 집합 이름과 sandbox 강등 리터럴.
ROLE_AXIS_NAMES = ("WRITE_ROLES", "READ_ROLES", "RESUME_MUTATING_ROLES")
SANDBOX_DOWNGRADE_LITERAL = "read-only"

# 원장 키 = (엔진 파일, 그 자리를 담은 함수 또는 모듈 선언 이름, 하네스/역할축 이름).
LedgerKey = tuple[str, str, str]


@dataclass(frozen=True)
class Site:
    """등재된 자리 — 사유와 기대 개수.

    `reason` 은 사람이 읽는 설명이다. **판정은 사유 문구가 아니라 코드 효과가 한다** — 권한·env·
    경로를 건드리는 분기는 어떤 사유를 붙여도 등재가 불가능하다.
    """

    reason: str
    expected_count: int = 1


# ── ① 하네스 이름이 조건식에 나오는 자리 (전부 CLI 형식) ──────────────────
CONDITION_BRANCHES: dict[LedgerKey, Site] = {
    ("additional_reviewer.py", "_structured_reviewer_argv", "codex"): Site(
        "argv 조립 — codex 는 `exec --json` 형식이라 relay 의 codex 빌더로 간다"),
    ("additional_reviewer.py", "_structured_reviewer_argv", "claude"): Site(
        "argv 조립 — claude 는 `-p --output-format stream-json` 형식이라 별도 빌더로 간다"),
    ("additional_reviewer.py", "_structured_transport", "opencode"): Site(
        "CLI 형식 — opencode 만 프롬프트를 stdin 이 아니라 `--file` 첨부로 받는다"),
    ("delegate_channel_guard.py", "decide", "claude"): Site(
        "CLI 형식 — claude 네이티브 Task tool 이 있는 세션에서만 네이티브 경로를 고른다"),
    ("pm_config.py", "_print_delegate_model_guidance", "opencode"): Site(
        "안내 문구 — opencode 만 로컬 GPU 모델 표기를 별도로 안내한다"),
    ("pm_delegate.py", "_build_target_argv", "codex"): Site(
        "argv 조립 — codex 빌더 분기(형식 계약은 pm_relay 소유)"),
    ("pm_delegate.py", "_build_target_argv", "claude"): Site(
        "argv 조립 — claude 빌더 분기(형식 계약은 pm_relay 소유)"),
    ("pm_delegate.py", "_dry_run_harness_annotations", "opencode"): Site(
        "dry-run 표기 — opencode 만 `--dir`/`--file` 자리표시자를 미리보기에 덧붙인다",
        expected_count=2),
    ("pm_delegate.py", "_prepare_attempt_transport", "opencode"): Site(
        "CLI 형식 — opencode 만 프롬프트 파일 sandbox 를 준비한다"),
    ("pm_import.py", "_build_runner_argv", "claude"): Site(
        "어댑터 설치 — 라이브 스모크 러너의 claude CLI argv"),
    ("pm_import.py", "_build_runner_argv", "codex"): Site(
        "어댑터 설치 — 라이브 스모크 러너의 codex CLI argv"),
    ("pm_import.py", "_build_runner_argv", "opencode"): Site(
        "어댑터 설치 — 라이브 스모크 러너의 opencode CLI argv"),
    ("pm_import.py", "_real_models_runner", "opencode"): Site(
        "어댑터 설치 — opencode 만 실 모델 목록을 CLI 로 조회한다"),
    ("pm_import.py", "add_harness", "claude"): Site(
        "어댑터 설치 — claude 타깃의 어댑터 디렉터리 배치"),
    ("pm_import.py", "add_harness", "codex"): Site(
        "어댑터 설치 — codex 타깃의 어댑터 디렉터리 배치"),
    ("pm_import.py", "main", "claude"): Site(
        "어댑터 설치 — `--harness claude` 스캐폴드 선택"),
    ("pm_import.py", "main", "codex"): Site(
        "어댑터 설치 — `--harness codex` 스캐폴드 선택"),
    ("pm_import.py", "run_fill", "codex"): Site(
        "어댑터 설치 — codex 스캐폴드 채우기의 CLI 형식 분기"),
    ("pm_import.py", "run_fill", "opencode"): Site(
        "어댑터 설치 — opencode 스캐폴드 채우기의 CLI 형식 분기"),
}

# ── ② 하네스 이름을 키로 갖는 선언표 ──────────────────────────────────────
# 정당한 비대칭(CLI 형식·어댑터 레이아웃)은 분기가 아니라 **표**로 적는다는 규약의 실물이다.
# 표 하나가 세 하네스 키를 가지므로 원장 항목도 셋이 된다(값 = (사유, 하네스들)).
HARNESS_TABLES: dict[tuple[str, str], tuple[str, tuple[str, ...]]] = {
    ("additional_reviewer.py", "_REVIEWER_PROGRESS_CONTRACTS"): (
        "CLI 형식 — 하네스마다 실행 파일과 진행 신호 옵션 계약이 다르다", HARNESS_NAMES),
    ("pm_config.py", "_DELEGATE_MODEL_FORMAT_HINTS"): (
        "안내 문구 — 하네스마다 모델 값 형식과 조회 수단이 다르다", HARNESS_NAMES),
    ("pm_delegate.py", "_HARNESS_AUTH_ENV"): (
        "CLI 인증 — 하네스마다 자기 CLI 가 읽는 인증 env 이름이 다르다", HARNESS_NAMES),
    ("pm_delegate.py", "_READ_TMP_ARGV_MODE_BY_HARNESS"): (
        "CLI 형식 — read 역할 임시 쓰기 자리를 여는 공개 플래그가 CLI 마다 다르다", HARNESS_NAMES),
    ("pm_delegate.py", "_READ_TMP_PARENT_COMPONENT_BY_HARNESS"): (
        "CLI 형식 — CLI 가 허용하는 임시 경로 부모 표기 실측값", HARNESS_NAMES),
    ("pm_delegate.py", "_READ_TMP_PYTEST_REL_BY_HARNESS"): (
        "CLI 형식 — 회귀 실행 위치 안내에 쓰는 상대 경로 표기", HARNESS_NAMES),
    ("pm_delegate.py", "_READ_TMP_REANCHOR_EXEC_ROOT_BY_HARNESS"): (
        "CLI 형식 — 실행 root 재앵커 플래그(`-C`) 보유 여부 실측값", HARNESS_NAMES),
    ("pm_delegate.py", "_READ_TMP_TMP_TEMP_USE_WRITABLE_PATH_BY_HARNESS"): (
        "CLI 형식 — TMPDIR/TMP/TEMP 표기를 CLI 가 어떻게 읽는지의 실측값", HARNESS_NAMES),
    ("pm_delegate.py", "_READ_TMP_WRITABLE_COMPONENT_BY_HARNESS"): (
        "CLI 형식 — 임시 쓰기 자리 이름 표기 실측값", HARNESS_NAMES),
    ("pm_import.py", "ADAPTER_CONFIG_CHANNEL"): (
        "어댑터 설치 — 하네스마다 config 파일 채널이 다르다", HARNESS_NAMES),
    ("pm_import.py", "ADAPTER_HOOK_SET"): (
        "어댑터 설치 — 하네스마다 훅 등록 파일 집합이 다르다", HARNESS_NAMES),
    ("pm_import.py", "ADD_HARNESS_ADAPTER"): (
        "어댑터 설치 — 하네스마다 어댑터 디렉터리 이름이 다르다", HARNESS_NAMES),
    ("pm_import.py", "ADD_HARNESS_CREATE_IF_ABSENT"): (
        "어댑터 설치 — 부재 시 새로 만들 config 파일이 하네스마다 다르다", HARNESS_NAMES),
    ("pm_import.py", "ADD_HARNESS_PRESERVE_EXISTING_TOML_FIELDS"): (
        "어댑터 설치 — 손편집 보존 대상 필드가 하네스 config 포맷마다 다르다", HARNESS_NAMES),
    ("pm_import.py", "HARNESS_TEMPLATE_DIRS"): (
        "어댑터 설치 — 하네스마다 출하 template 디렉터리가 다르다", HARNESS_NAMES),
    ("pm_import.py", "INSTANCE_OWNED_ADAPTER_FILES"): (
        "어댑터 설치 — 인스턴스 소유 config 파일 이름이 하네스마다 다르다", HARNESS_NAMES),
    ("pm_relay.py", "HARNESS_CAP_ENV"): (
        "CLI 형식 — 호출층 상한을 관측할 수 있는 env 이름이 하네스마다 다르다", HARNESS_NAMES),
    ("pm_relay.py", "HARNESS_PROFILES"): (
        "CLI 형식 — 하네스별 실행 프로필(실행 파일·형식·시간 계약) 단일 선언표", HARNESS_NAMES),
    ("pm_relay.py", "HARNESS_REPLY_ADAPTERS"): (
        "CLI 형식 — 회신 wire 포맷이 하네스마다 다르다", HARNESS_NAMES),
    ("pm_relay.py", "HARNESS_RESUME_SUPPORT"): (
        "CLI 형식 — 세션 재개 플래그 보유 여부 실측값", HARNESS_NAMES),
    ("pm_relay.py", "HARNESS_SESSION_MARKERS"): (
        "CLI 형식 — 세션 감지 마커 env 이름이 하네스마다 다르다", HARNESS_NAMES),
    ("pm_relay.py", "REASONING_ALLOWED"): (
        "CLI 형식 — reasoning 허용집합이 CLI 마다 다르다(미지원값 fail-loud 용)", HARNESS_NAMES),
    ("pm_relay.py", "_RUNTIME_ROLE_CONFIG_BUILDERS"): (
        "CLI 형식 — 역할 config 를 argv/파일 중 무엇으로 넘기는지가 CLI 마다 다르다",
        HARNESS_NAMES),
    ("pm_render.py", "SKILL_ENTRY_PREFIX_BY_TEMPLATE_DIR"): (
        "어댑터 설치 — 스킬 호출 표기 접두가 하네스마다 다르다", ("codex", "opencode")),
    ("pm_render.py", "_HARNESS_LABEL_BY_TEMPLATE_DIR"): (
        "안내 문구 — 카드에 찍는 하네스 표기 라벨", ("codex", "opencode")),
}

# ── ③ 위임 경로에서 피위임자 권한을 좁히는 자리 ───────────────────────────
# 하네스와 무관하게 generate≠evaluate 를 강제하는 역할축 하나뿐이다. 여기 없는 새 좁힘이 들어오면
# (하네스 이름이 없어도) 미등재로 걸린다.
_ROLE_AXIS_REASON = "역할축 — 하네스와 무관하게 generate≠evaluate 를 강제한다"
DELEGATION_AUTHORITY_SITES: dict[LedgerKey, Site] = {
    ("delegate_scope.py", "allowed_paths", "WRITE_ROLES"): Site(_ROLE_AXIS_REASON),
    ("pm_delegate.py", "RESUME_MUTATING_ROLES", "WRITE_ROLES"): Site(_ROLE_AXIS_REASON),
    ("pm_delegate.py", "_apply_read_tmp_argv", "READ_ROLES"): Site(_ROLE_AXIS_REASON),
    ("pm_delegate.py", "_changed_overlap_paths", "WRITE_ROLES"): Site(_ROLE_AXIS_REASON),
    ("pm_delegate.py", "_execute_and_collect", "RESUME_MUTATING_ROLES"): Site(
        "역할축 — 재실행이 라운드를 하나 더 만드는 역할을 mutating 으로 다룬다"),
    ("pm_delegate.py", "_perm_axis", "WRITE_ROLES"): Site(_ROLE_AXIS_REASON),
    ("pm_delegate.py", "_prepare_attempt_transport", "READ_ROLES"): Site(_ROLE_AXIS_REASON),
    ("pm_delegate.py", "_run_delegate_cli", "READ_ROLES"): Site(_ROLE_AXIS_REASON),
    ("pm_delegate.py", "_run_delegate_cli", "WRITE_ROLES"): Site(
        _ROLE_AXIS_REASON, expected_count=2),
    ("pm_delegate.py", "check_write_target_reanchor", "WRITE_ROLES"): Site(_ROLE_AXIS_REASON),
    ("pm_delegate.py", "report_scope_audit", "WRITE_ROLES"): Site(_ROLE_AXIS_REASON),
    ("pm_relay.py", "build_claude_argv", "WRITE_ROLES"): Site(_ROLE_AXIS_REASON),
    ("pm_relay.py", "claude_tools", "WRITE_ROLES"): Site(_ROLE_AXIS_REASON),
    ("pm_relay.py", "perm_axis", "WRITE_ROLES"): Site(_ROLE_AXIS_REASON),
    ("pm_delegate.py", "_apply_read_tmp_argv", SANDBOX_DOWNGRADE_LITERAL): Site(
        "역할축 — read 역할 argv 가 들고 온 sandbox 계약을 확인하고 임시 자리만 연다"),
    ("pm_relay.py", "CODEX_SANDBOX", SANDBOX_DOWNGRADE_LITERAL): Site(
        "역할축 — read 축 codex sandbox 값 선언(하네스가 아니라 권한축이 키다)"),
}

# 원장 총량 상한 — **축소 전용**. 새 자리를 등재하려면 다른 자리를 코드에서 지워야 한다.
# 현재 총량과 같은 값이라 항목 추가만으로는 통과할 수 없다.
LEDGER_CEILING = 110

# 하네스 분기 몸통에서 금지하는 효과 — 이 축을 건드리면 등재 자체가 불가능하다.
PERMISSION_EFFECT_KEYS = ("HOME", "PATH", "CWD", "PERMISSION", "SANDBOX", "MODE")


def ledger() -> dict[LedgerKey, Site]:
    """세 원장을 하나로 — 측정 Counter 와 같은 키 공간이다."""
    merged: dict[LedgerKey, Site] = dict(CONDITION_BRANCHES)
    for (source, holder), (reason, harnesses) in HARNESS_TABLES.items():
        for harness in harnesses:
            merged[(source, holder, harness)] = Site(reason)
    merged.update(DELEGATION_AUTHORITY_SITES)
    return merged


def _scope_names(tree: ast.Module) -> dict[int, str]:
    """모든 노드 → 그 노드를 감싸는 가장 안쪽 함수 이름(모듈 스코프면 선언 이름)."""
    owner: dict[int, str] = {id(tree): "<module>"}

    def walk(node: ast.AST, current: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = child.name
            elif current == "<module>" and isinstance(child, (ast.Assign, ast.AnnAssign)):
                targets = (child.targets if isinstance(child, ast.Assign)
                           else [child.target])
                declared = [t.id for t in targets if isinstance(t, ast.Name)]
                name = declared[0] if declared else current
            else:
                name = current
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


def _harness_keyed_mappings(tree: ast.Module) -> list[tuple[ast.AST, str]]:
    """(그 표를 담은 노드, 하네스 이름) — 하네스 이름을 **키로** 갖는 매핑 리터럴."""
    found: list[tuple[ast.AST, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            keys = list(node.keys)
        elif isinstance(node, ast.DictComp):
            keys = [node.key]
        else:
            continue
        for key in keys:
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                value = key.value.strip().lower()
                if value in HARNESS_NAMES:
                    found.append((node, value))
    return found


def _delegation_authority_nodes(tree: ast.Module) -> list[tuple[ast.AST, str]]:
    """(노드, 역할축 이름) — 피위임자 권한을 좁히는 자리."""
    found: list[tuple[ast.AST, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in ROLE_AXIS_NAMES:
            if isinstance(node.ctx, ast.Load):
                found.append((node, node.id))
        elif isinstance(node, ast.Attribute) and node.attr in ROLE_AXIS_NAMES:
            found.append((node, node.attr))
        elif (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and node.value == SANDBOX_DOWNGRADE_LITERAL):
            found.append((node, SANDBOX_DOWNGRADE_LITERAL))
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


def _measured_sites(sources: list[Path]) -> Counter[LedgerKey]:
    """소스 전수에서 하네스 분기·선언표·위임 권한 자리를 같은 키 공간으로 센다."""
    measured: Counter[LedgerKey] = Counter()
    for path in sources:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        owner = _scope_names(tree)
        for holder, condition in _condition_subtrees(tree):
            for name in _harness_names_in(condition):
                measured[(path.name, owner.get(id(holder), "<module>"), name)] += 1
        for holder, name in _harness_keyed_mappings(tree):
            measured[(path.name, owner.get(id(holder), "<module>"), name)] += 1
        for node, name in _delegation_authority_nodes(tree):
            measured[(path.name, owner.get(id(node), "<module>"), name)] += 1
    return measured


def _is_env_like(expr: ast.AST) -> bool:
    """이 식이 env 매핑을 가리키는가(이름·속성에 env/environ 이 든다)."""
    for node in ast.walk(expr):
        if isinstance(node, ast.Name) and "env" in node.id.lower():
            return True
        if isinstance(node, ast.Attribute) and "env" in node.attr.lower():
            return True
    return False


def _permission_effects(subtree: ast.AST) -> list[str]:
    """이 서브트리가 env·권한·경로를 건드리는 자리 목록(효과 판정)."""
    effects: list[str] = []
    for node in ast.walk(subtree):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Subscript):
                if _is_env_like(target.value):
                    effects.append(f"env 대입 (line {node.lineno})")
                for key in ast.walk(target.slice):
                    if (isinstance(key, ast.Constant) and isinstance(key.value, str)
                            and key.value.upper() in PERMISSION_EFFECT_KEYS):
                        effects.append(f"{key.value} 키 대입 (line {node.lineno})")
            if (isinstance(target, ast.Attribute)
                    and target.attr.upper() in PERMISSION_EFFECT_KEYS):
                effects.append(f"{target.attr} 속성 대입 (line {node.lineno})")
        if (isinstance(node, ast.Attribute) and node.attr == "environ"
                and isinstance(node.value, ast.Name) and node.value.id == "os"):
            effects.append(f"os.environ 접근 (line {node.lineno})")
    return effects


def _permission_effect_offenders(sources: list[Path]) -> list[str]:
    """하네스 이름 조건이 감싸는 몸통에서 권한·env 효과가 있는 자리 전수."""
    offenders: list[str] = []
    for path in sources:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        owner = _scope_names(tree)
        for node in ast.walk(tree):
            if isinstance(node, (ast.If, ast.While)):
                guarded: list[ast.AST] = list(node.body) + list(node.orelse)
            elif isinstance(node, ast.IfExp):
                guarded = [node.body, node.orelse]
            else:
                continue
            if not _harness_names_in(node.test):
                continue
            for subtree in guarded:
                for effect in _permission_effects(subtree):
                    holder = owner.get(id(node), "<module>")
                    offenders.append(f"{path.name}::{holder} :: {effect}")
    return offenders


def _engine_sources() -> list[Path]:
    return sorted(TOOLS.glob("*.py"))


def test_engine_sources_are_actually_scanned():
    """스캔 대상이 비면 판정 불능이다 — 초록이 사각의 증거가 되지 않게 먼저 센다."""
    sources = _engine_sources()
    assert len(sources) >= 20, [path.name for path in sources]
    assert _measured_sites(sources), "측정 자리 0 — 스캐너가 무력화됐다"


def test_no_site_outside_the_ledger():
    """원장 밖에서 하네스 이름·역할축으로 갈리는 자리가 없다."""
    measured = _measured_sites(_engine_sources())
    registered = ledger()
    unregistered = sorted(key for key in measured if key not in registered)
    assert not unregistered, (
        "원장 미등재 자리 — 권한·경로·env·동의 축을 가르는 자리라면 지우고, CLI 형식·역할축이면 "
        f"사유와 함께 원장에 등재하라(총량 상한 {LEDGER_CEILING}): {unregistered}"
    )


def test_no_dead_ledger_entry():
    """코드에서 사라진 원장 항목을 남기지 않는다 (원장은 축소 방향으로만 움직인다)."""
    measured = _measured_sites(_engine_sources())
    registered = ledger()
    stale = sorted(
        key for key, entry in registered.items()
        if measured.get(key, 0) != entry.expected_count
    )
    assert not stale, (
        "사라졌거나 개수가 바뀐 자리 — 원장에서 제거/재검토하라: "
        f"{[(key, registered[key].expected_count, measured.get(key, 0)) for key in stale]}"
    )


def test_ledger_total_never_grows():
    """원장 총량이 상한을 넘지 않는다 — 새 등재는 다른 자리를 지워야만 가능하다."""
    registered = ledger()
    total = sum(entry.expected_count for entry in registered.values())
    assert total <= LEDGER_CEILING, (
        f"원장 총량 {total} > 상한 {LEDGER_CEILING} — 자리를 추가하려면 다른 자리를 코드에서 "
        "지우고 상한을 그만큼 내려라(축소 전용)."
    )


def test_every_ledger_entry_carries_a_reason():
    """등재 사유는 사람이 읽는 설명으로 남는다 — 판정은 코드 효과가 한다."""
    empty = sorted(key for key, entry in ledger().items() if not entry.reason.strip())
    assert not empty, empty


def test_no_permission_effect_inside_a_harness_branch():
    """하네스 이름 조건이 감싸는 몸통이 env·권한·경로를 건드리지 않는다 (효과 판정)."""
    offenders = _permission_effect_offenders(_engine_sources())
    assert not offenders, (
        "하네스 분기 안에서 권한·env·경로 효과 발생 — 등재로 정당화할 수 없는 차등이다. "
        f"그 분기를 지워라: {offenders}"
    )


def test_permission_effect_inside_a_harness_branch_is_never_ledgerable(tmp_path):
    """감도 — 권한 효과가 있는 분기는 원장에 올려도 통과하지 못한다."""
    injected = tmp_path / "pm_relay.py"
    injected.write_text(
        "def _scoped_home(harness, env):\n"
        "    if harness == 'opencode':\n"
        "        env['HOME'] = '/tmp/limited'\n"
        "    return env\n",
        encoding="utf-8",
    )

    key = ("pm_relay.py", "_scoped_home", "opencode")
    measured = _measured_sites([injected])
    assert measured[key] == 1
    # 원장에 CLI 형식 사유로 올려도(우회 시도) 효과 판정이 따로 잡는다.
    offenders = _permission_effect_offenders([injected])
    assert offenders and all("HOME" in offender or "env" in offender
                             for offender in offenders), offenders


def test_harness_keyed_mapping_is_measured(tmp_path):
    """감도 — 표 형태의 하네스별 env 차등도 조건식과 같은 축으로 잡힌다."""
    injected = tmp_path / "pm_delegate.py"
    injected.write_text(
        "_HARNESS_ENV_STRIP: dict[str, tuple[str, ...]] = {'opencode': ('HOME', 'PATH')}\n"
        "\n"
        "def build_env(harness):\n"
        "    out = {}\n"
        "    for key in _HARNESS_ENV_STRIP.get(harness, ()):\n"
        "        out.pop(key, None)\n"
        "    return out\n",
        encoding="utf-8",
    )

    key = ("pm_delegate.py", "_HARNESS_ENV_STRIP", "opencode")
    measured = _measured_sites([injected])

    assert measured[key] == 1
    assert key not in ledger()


def test_delegation_authority_narrowing_is_measured(tmp_path):
    """감도 — 하네스 이름이 없어도 피위임자 권한을 좁히는 새 자리는 미등재로 잡힌다."""
    injected = tmp_path / "pm_delegate.py"
    injected.write_text(
        "def _narrow(role, argv):\n"
        "    if role not in WRITE_ROLES:\n"
        "        return argv + ['-s', 'read-only']\n"
        "    return argv\n",
        encoding="utf-8",
    )

    measured = _measured_sites([injected])
    registered = ledger()

    assert measured[("pm_delegate.py", "_narrow", "WRITE_ROLES")] == 1
    assert measured[("pm_delegate.py", "_narrow", "read-only")] == 1
    assert ("pm_delegate.py", "_narrow", "WRITE_ROLES") not in registered
    assert ("pm_delegate.py", "_narrow", "read-only") not in registered


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

    measured = _measured_sites([injected])

    assert measured == Counter({("pm_delegate.py", "build_env", "codex"): 1})
    assert ("pm_delegate.py", "build_env", "codex") not in ledger()


def test_a_removed_site_makes_its_ledger_entry_dead(tmp_path):
    """감도 — 등재된 자리를 코드에서 지우고 원장을 그대로 두면 죽은 예외로 잡힌다."""
    emptied = tmp_path / "pm_import.py"
    emptied.write_text("def add_harness(name):\n    return name\n", encoding="utf-8")

    measured = _measured_sites([emptied])

    assert measured.get(("pm_import.py", "add_harness", "codex"), 0) == 0
    assert ("pm_import.py", "add_harness", "codex") in ledger()


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

    assert _measured_sites([paths_only]) == Counter()


@pytest.mark.parametrize("relpath", [
    ".project_manager/wiki/pm_principles.md",
    ".claude/skills/pm-dev-delegate/SKILL.md",
    "templates/codex/.agents/skills/pm-dev-delegate/SKILL.md",
    "templates/opencode/.claude/skills/pm-dev-delegate/SKILL.md",
    "CHANGELOG.md",
])
def test_equal_authority_rule_is_stated_on_every_pm_surface(relpath):
    """코덱스·오픈코드가 PM 일 때 읽는 자리에도 위임 권한 동등 규칙이 실려 있다 (T-0887)."""
    text = (REPO / relpath).read_text(encoding="utf-8")
    assert "위임자는 피위임자에게 자신과 같은 권한을 준다" in text, relpath
    assert "위임 방향·하네스 조합" in text, relpath
