"""repo-owned 열거 seam 우회(`rglob`/`os.walk`) 재발 방지 정적 가드."""

from __future__ import annotations

import ast
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
WalkKey = tuple[str, str, str, str]


@dataclass(frozen=True)
class WalkException:
    reason: str
    expected_count: int = 1


# 아직 seam으로 전환하지 않은 repo 인벤토리 소비처. 담당 티켓이 전환할 때 코드와 이 예외를
# 함께 지운다. 키 = (repo-relative 파일, 함수, 호출 종류, 첫 인자 AST).
MIGRATION_EXCEPTIONS: dict[WalkKey, WalkException] = {
    (
        ".project_manager/tools/pm_import.py",
        "_iter_source_files",
        "rglob",
        "'*'",
    ): WalkException("T-0499가 template 출하 열거를 tracked_only seam으로 전환할 때 제거"),
    (
        "scripts/strip_private_refs.py",
        "_write_inventory",
        "rglob",
        "'*.md'",
    ): WalkException("T-0500이 private-ref 스캔 인벤토리를 공용 seam으로 전환할 때 제거", 2),
    (
        "tests/test_private_context_guard.py",
        "_shipping_paths",
        "rglob",
        "'*.md'",
    ): WalkException("T-0500이 private-context 판정 인벤토리를 공용 seam으로 전환할 때 제거", 3),
}


# repo 소유 인벤토리가 아닌 닫힌 트리 관리/fixture 판정과 canonical seam 자체. 새 호출은 이
# 원장에 사유와 기대 개수를 명시하지 않는 한 red다.
REVIEWED_NON_INVENTORY_EXCEPTIONS: dict[WalkKey, WalkException] = {
    (
        ".project_manager/tools/repo_owned_files.py",
        "list_repo_owned_files",
        "rglob",
        "'*'",
    ): WalkException("canonical seam의 loud filesystem fallback 구현"),
    (
        ".project_manager/tools/domain.py",
        "load_pages",
        "rglob",
        "'*.md'",
    ): WalkException("인스턴스가 관리하는 domain 페이지 디렉토리만 읽는 좁은 페이지 로더"),
    (
        ".project_manager/tools/board.py",
        "collect_rewrite_targets",
        "rglob",
        "'*.md'",
    ): WalkException("board/wiki/log 엔진 관리 디렉토리의 ID rewrite 대상만 열거"),
    (
        ".project_manager/tools/board.py",
        "_collect_wikilink_files",
        "rglob",
        "'*.md'",
    ): WalkException("wiki/tickets/adapter scaffold라는 닫힌 엔진 관리 문서 집합만 열거", 3),
    (
        ".project_manager/tools/board.py",
        "lint_render_leak",
        "rglob",
        "'*'",
    ): WalkException("engine.manifest @render가 선언한 닫힌 경로만 검사"),
    (
        ".project_manager/tools/board.py",
        "_collect_overlay_adapter_files",
        "rglob",
        "'*'",
    ): WalkException("engine.manifest @render가 선언한 adapter overlay 경로만 검사"),
}

_FIXTURE_WALK_KEYS: tuple[WalkKey, ...] = (
    ("tests/test_pm_render.py", "test_adapter_surfaces_no_machine_variant_tokens", "rglob", "'*.md'"),
    ("tests/test_entry_doc_migration.py", "test_scenario_unmodified_auto_migrate", "rglob", "'AGENTS.md'"),
    ("tests/test_fresh_adopter_e2e.py", "test_fresh_adopter_excludes_framework_internal_readme", "rglob", "'*.md'"),
    ("tests/test_fresh_adopter_e2e.py", "_snapshot_tree", "rglob", "'*'"),
    ("tests/test_adapter_session_identity.py", "_scanned_files", "rglob", "'*.md'"),
    ("tests/test_board_lint.py", "_old_collect", "rglob", "pat"),
    ("tests/test_pm_import.py", "_grep_token_files", "rglob", "'*'"),
    ("tests/test_pm_import.py", "_opencode_dest_with_token", "rglob", "'*'"),
    ("tests/test_pm_import.py", "_copied_relpaths_of", "rglob", "'*'"),
    ("tests/test_pm_import.py", "test_add_harness_apply_claude_creates_adapter_and_preserves_devstate", "rglob", "'SKILL.md'"),
    ("tests/test_pm_import.py", "test_non_git_target_all_central_backup_no_siblings", "rglob", "'*.backup.*'"),
    ("tests/test_pm_import.py", "test_add_harness_apply_refresh_backs_up_and_stays_scoped", "rglob", "'agents/pm.md'"),
    ("tests/test_pm_import.py", "test_add_harness_opencode_guest_cross_ns_skills_by_host", "rglob", "'SKILL.md'"),
    ("tests/test_pm_import.py", "test_codex_scaffold_no_unresolved_token_leak", "rglob", "'*'"),
    ("tests/test_pm_import.py", "_lite_md_files", "rglob", "'*.lite.md'"),
    ("tests/test_pm_import.py", "test_import_excludes_pycache", "rglob", "'__pycache__'"),
    ("tests/test_pm_import.py", "test_import_excludes_pycache", "rglob", "'*.pyc'"),
    ("tests/test_pm_import.py", "test_add_harness_codex_refresh_quietly_preserves_identical_instance_config", "rglob", "Path(rel).name"),
    ("tests/test_pm_import.py", "test_add_harness_apply_zero_operational_token_leak", "rglob", "'*'"),
    ("tests/test_adapter_free_form_free.py", "test_render_scoped_dir_present_and_nonempty", "rglob", "'*'"),
    ("tests/test_adapter_free_form_free.py", "_render_scoped_text_files", "rglob", "'*'"),
    ("tests/test_template_ignore_files_tracked.py", "_ondisk_ignore_relpaths", "rglob", "'*'"),
    ("tests/test_manifest_template_parity.py", "_expand_manifest_files", "rglob", "'*'"),
    ("tests/test_flag_unification_parity.py", "_scan_paths_for_old_flags", "rglob", "'*.md'"),
    ("tests/test_settings_hygiene.py", "_long_engine_command_markdown", "rglob", "'*.md'"),
    ("tests/test_pm_handoff_shipping.py", "_expand_manifest_shipping_paths", "os.walk", "abs_p"),
    ("tests/test_opencode_command_skill_pairing.py", "_skill_files", "rglob", "'SKILL.md'"),
    ("tests/test_adapter_token_substitution.py", "_token_leaks", "rglob", "'*'"),
    ("tests/test_opencode_adapter_v2_docs.py", "test_no_shipped_opencode_doc_points_to_removed_agents_anchors", "rglob", "'*.md'"),
    ("tests/test_skill_command_existence.py", "_iter_md_files", "rglob", "'*.md'"),
)
for _key in _FIXTURE_WALK_KEYS:
    REVIEWED_NON_INVENTORY_EXCEPTIONS[_key] = WalkException(
        "격리된 생성 fixture/선언 경로의 결과를 검증하는 테스트 보조 순회"
    )

ALL_EXCEPTIONS = {**MIGRATION_EXCEPTIONS, **REVIEWED_NON_INVENTORY_EXCEPTIONS}


class _WalkVisitor(ast.NodeVisitor):
    def __init__(self, relative: str) -> None:
        self.relative = relative
        self.functions: list[str] = []
        self.calls: list[WalkKey] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.functions.append(node.name)
        self.generic_visit(node)
        self.functions.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node: ast.Call) -> None:
        kind = None
        if isinstance(node.func, ast.Attribute) and node.func.attr == "rglob":
            kind = "rglob"
        elif (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "walk"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "os"
        ):
            kind = "os.walk"
        if kind is not None:
            argument = ast.unparse(node.args[0]) if node.args else ""
            function = self.functions[-1] if self.functions else "<module>"
            self.calls.append((self.relative, function, kind, argument))
        self.generic_visit(node)


def _walk_calls(repo_root: Path) -> list[WalkKey]:
    calls: list[WalkKey] = []
    for relative_dir in (".project_manager/tools", "scripts", "tests"):
        directory = repo_root / relative_dir
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.py")):
            visitor = _WalkVisitor(path.relative_to(repo_root).as_posix())
            visitor.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
            calls.extend(visitor.calls)
    return calls


def _audit(repo_root: Path, exceptions: dict[WalkKey, WalkException] = ALL_EXCEPTIONS):
    actual = Counter(_walk_calls(repo_root))
    expected = Counter({
        key: exception.expected_count
        for key, exception in exceptions.items()
    })
    unauthorized = actual - expected
    stale_or_count_mismatch = expected - actual
    blank_reasons = [
        key for key, exception in exceptions.items() if not exception.reason.strip()
    ]
    return unauthorized, stale_or_count_mismatch, blank_reasons


def test_repo_tree_walks_have_reviewed_nonempty_exceptions_only():
    unauthorized, stale, blank = _audit(REPO)
    assert not blank, f"repo tree walk 예외 사유가 비어 있음: {blank}"
    assert not unauthorized, (
        "repo-owned 열거 seam을 우회한 신규 rglob/os.walk 호출: "
        f"{list(unauthorized.elements())}"
    )
    assert not stale, (
        "사라졌거나 개수가 바뀐 repo tree walk 예외를 원장에서 제거/재검토하라: "
        f"{list(stale.elements())}"
    )


@pytest.mark.parametrize(
    ("filename", "body", "kind"),
    [
        ("future_tool.py", "def collect(root):\n    return root.rglob('*')\n", "rglob"),
        ("future_script.py", "import os\ndef collect(root):\n    return os.walk(root)\n", "os.walk"),
        ("test_future_inventory.py", "def collect(root):\n    return root.rglob('*.md')\n", "rglob"),
    ],
)
def test_guard_sensitivity_rejects_new_walk_in_any_guarded_class(
        tmp_path, filename, body, kind):
    relative_dir = (
        "tests" if filename.startswith("test_")
        else "scripts" if "script" in filename
        else ".project_manager/tools"
    )
    target = tmp_path / relative_dir / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")

    unauthorized, _stale, _blank = _audit(tmp_path, exceptions={})

    assert any(key[2] == kind and key[0].endswith(filename) for key in unauthorized)


def test_guard_rejects_blank_exception_reason(tmp_path):
    target = tmp_path / ".project_manager" / "tools" / "future.py"
    target.parent.mkdir(parents=True)
    target.write_text("def collect(root):\n    return root.rglob('*')\n", encoding="utf-8")
    key = (".project_manager/tools/future.py", "collect", "rglob", "'*'")

    _unauthorized, _stale, blank = _audit(
        tmp_path, exceptions={key: WalkException("   ")})

    assert blank == [key]
