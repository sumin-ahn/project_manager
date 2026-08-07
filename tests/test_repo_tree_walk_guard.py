"""repo-owned 열거 seam을 우회하는 재귀 tree-walk 등가 API 재발 방지 정적 가드."""

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
        "tests/test_template_ignore_files_tracked.py",
        "_ondisk_ignore_relpaths",
        "rglob",
        "'*'",
    ): WalkException(
        "실 디스크의 ignored 미추적 ignore 규칙 파일 검출이 판정의 본질이라 OWNED "
        "(--exclude-standard)로는 자기-은닉 결함이 누락됨; T-0492 sensitivity가 이를 고정"),
}


# repo 소유 인벤토리가 아닌 닫힌 트리 관리/fixture 판정과 canonical seam 자체. 새 호출은 이
# 원장에 사유와 기대 개수를 명시하지 않는 한 red다.
REVIEWED_NON_INVENTORY_EXCEPTIONS: dict[WalkKey, WalkException] = {
    (
        "tests/test_template_scaffold_parity.py",
        "_unresolved_wiki_links",
        "rglob",
        "'*.md'",
    ): WalkException("출하 wiki 전 파일 링크 해소 검증 — 대상 열거 자체가 판정의 본질"),
    (
        ".project_manager/tools/repo_owned_files.py",
        "list_repo_owned_entries",
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
    (
        "tests/test_repo_tree_walk_guard.py",
        "_walk_calls",
        "rglob",
        "'*.py'",
    ): WalkException("정적 가드가 guarded 하위 디렉토리의 Python 파일 자체를 재귀 발견"),
    (
        "tests/test_entry_doc_migration.py",
        "test_scenario_unmodified_auto_migrate",
        "rglob",
        "'AGENTS.md'",
    ): WalkException("tmp_path adopter에서 migrate가 생성한 중앙 백업 fixture만 검증"),
    (
        "tests/test_fresh_adopter_e2e.py",
        "test_fresh_adopter_excludes_framework_internal_readme",
        "rglob",
        "'*.md'",
    ): WalkException("tmp_path에 fresh import한 adopter fixture의 dangling 문서 링크만 검증"),
    (
        "tests/test_fresh_adopter_e2e.py",
        "_snapshot_tree",
        "rglob",
        "'*'",
    ): WalkException("tmp_path에 생성한 adopter fixture의 add-harness 전후 바이트 스냅샷"),
    (
        "tests/test_board_lint.py",
        "_old_collect",
        "rglob",
        "pat",
    ): WalkException("tmp_path 합성 adapter fixture에서 의도적으로 복원한 옛 수집기의 sensitivity 대조"),
    (
        "tests/test_pm_import.py",
        "_grep_token_files",
        "rglob",
        "'*'",
    ): WalkException("tmp_path import/add-harness 산출 fixture의 토큰 잔존만 검사"),
    (
        "tests/test_pm_import.py",
        "_opencode_dest_with_token",
        "rglob",
        "'*'",
    ): WalkException("tmp_path에 import한 opencode fixture를 토큰 치환 전 상태로 되돌리는 테스트 준비"),
    (
        "tests/test_pm_import.py",
        "_copied_relpaths_of",
        "rglob",
        "'*'",
    ): WalkException("tmp_path import fixture 전체를 함수 단위 copied_relpaths 입력으로 모델링"),
    (
        "tests/test_pm_import.py",
        "test_add_harness_apply_claude_creates_adapter_and_preserves_devstate",
        "rglob",
        "'SKILL.md'",
    ): WalkException("tmp_path live-instance fixture의 add-harness 전 스킬 바이트 스냅샷"),
    (
        "tests/test_pm_import.py",
        "test_non_git_target_all_central_backup_no_siblings",
        "rglob",
        "'*.backup.*'",
    ): WalkException("tmp_path non-git adopter fixture에 분산 형제 백업이 생기지 않았음을 검증"),
    (
        "tests/test_pm_import.py",
        "test_add_harness_apply_refresh_backs_up_and_stays_scoped",
        "rglob",
        "'agents/pm.md'",
    ): WalkException("tmp_path refresh fixture가 만든 중앙 backup 산출물만 검증"),
    (
        "tests/test_pm_import.py",
        "test_add_harness_opencode_guest_cross_ns_skills_by_host",
        "rglob",
        "'SKILL.md'",
    ): WalkException("tmp_path host fixture에 add-harness가 landing한 cross-namespace 스킬을 검증"),
    (
        "tests/test_pm_import.py",
        "test_codex_scaffold_no_unresolved_token_leak",
        "rglob",
        "'*'",
    ): WalkException("tmp_path에 fresh import한 codex scaffold fixture의 토큰 leak만 검증"),
    (
        "tests/test_pm_import.py",
        "_lite_md_files",
        "rglob",
        "'*.lite.md'",
    ): WalkException("tmp_path import fixture에 배치 후 남은 lite 변종이 없는지 검증"),
    (
        "tests/test_pm_import.py",
        "test_import_excludes_pycache",
        "rglob",
        "'__pycache__'",
    ): WalkException("tmp_path import fixture에서 제외돼야 할 cache 디렉토리 산출만 검증"),
    (
        "tests/test_pm_import.py",
        "test_import_excludes_pycache",
        "rglob",
        "'*.pyc'",
    ): WalkException("tmp_path import fixture에서 제외돼야 할 bytecode 산출만 검증"),
    (
        "tests/test_pm_import.py",
        "test_add_harness_codex_refresh_quietly_preserves_identical_instance_config",
        "rglob",
        "Path(rel).name",
    ): WalkException("tmp_path refresh fixture가 불필요한 config backup을 만들지 않았음을 검증"),
    (
        "tests/test_pm_import.py",
        "test_add_harness_apply_zero_operational_token_leak",
        "rglob",
        "'*'",
    ): WalkException("tmp_path add-harness fixture의 각 landing namespace가 nonempty인지 검증"),
    (
        "tests/test_pm_update.py",
        "_landed",
        "rglob",
        "'*'",
    ): WalkException(
        "tmp_path 합성 dest fixture에 **무엇이 착지했는가** 전수 확인 — 경로 스코프의 판정이 "
        "'요청 밖 파일이 하나도 안 왔다'라 열거 자체가 본질(seam 열거로는 미착지 증명 불가)"),
    (
        "tests/test_pm_update.py",
        "_tree_digest",
        "rglob",
        "'*'",
    ): WalkException(
        "tmp_path 합성 dest fixture의 read-only 불변식 스냅샷 — `--changes` 판정이 '어떤 파일도 "
        "추가/수정되지 않았다'라 전수 해시가 본질(좁힌 열거는 예기치 못한 write를 놓친다)"),
    (
        "tests/test_adapter_token_substitution.py",
        "_token_leaks",
        "rglob",
        "'*'",
    ): WalkException("tmp_path에 fresh import한 adapter fixture의 치환 결과만 검사"),
    (
        "tests/test_external_review_reviewer_isolation.py",
        "test_workspace_hides_old_review_raw_from_reviewer_visible_tree",
        "rglob",
        "'*'",
    ): WalkException(
        "tmp 리뷰어 거울 fixture의 **전수** 검사가 판정의 본질 — 격리 성질은 '어떤 파일도 옛 raw를 "
        "담지 않는다'라 seam의 tracked-only 열거로는 검증 자체가 성립하지 않는다"),
    (
        "tests/test_external_review_reviewer_isolation.py",
        "test_no_isolated_absolute_path_survives_in_any_home_artifact",
        "rglob",
        "'*'",
    ): WalkException(
        "tmp 임시 홈 fixture **전수**에 격리 대상 절대경로가 없음을 검증 — '무엇이 남았나'가 판정의 "
        "본질이라 좁힌 열거로는 새 경로 키를 놓친다"),
    (
        "tests/test_external_review_reviewer_isolation.py",
        "test_mirror_excludes_secret_denylist_files",
        "rglob",
        "'*'",
    ): WalkException(
        "tmp 거울 fixture **전수**에 시크릿 본문이 없음을 검증 — '어떤 파일에도 없다'가 판정의 "
        "본질이라 좁힌 열거로는 유출을 놓친다"),
    (
        "tests/test_external_review_reviewer_isolation.py",
        "test_reviewer_home_carries_auth_but_no_sessions",
        "rglob",
        "'*'",
    ): WalkException(
        "tmp 임시 홈 fixture의 **전수** 검사가 판정의 본질 — '선언된 인증/설정 파일 말고는 아무것도 "
        "없다'는 성질이라 좁힌 열거로는 세션/이력 잔존을 놓친다"),
    (
        "tests/test_external_review_reviewer_isolation.py",
        "test_workspace_builds_tree_and_home_side_by_side",
        "rglob",
        "'auth.json'",
    ): WalkException(
        "tmp 거울 fixture 안에 인증 파일이 섞이지 않았음을 이름으로 검증(거울은 git 인벤토리가 "
        "아니라 방금 만든 닫힌 트리)"),
    (
        "tests/test_external_review_reviewer_isolation.py",
        "test_workspace_refuses_files_reached_through_out_of_repo_parent_symlink",
        "rglob",
        "'*'",
    ): WalkException(
        "tmp 리뷰어 거울 fixture 전수에 저장소 밖 내용이 없음을 검증 — 거울은 git 저장소 인벤토리가 "
        "아니라 방금 만든 닫힌 트리다"),
}

ALL_EXCEPTIONS = {**MIGRATION_EXCEPTIONS, **REVIEWED_NON_INVENTORY_EXCEPTIONS}


class _WalkVisitor(ast.NodeVisitor):
    def __init__(self, relative: str) -> None:
        self.relative = relative
        self.functions: list[str] = []
        self.calls: list[WalkKey] = []
        self.os_modules: set[str] = {"os"}
        self.ast_modules: set[str] = {"ast"}
        self.glob_modules: set[str] = {"glob"}
        self.os_walk_functions: set[str] = set()
        self.os_fwalk_functions: set[str] = set()
        self.glob_functions: set[str] = set()
        self.iglob_functions: set[str] = set()

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            local = alias.asname or alias.name
            if alias.name == "os":
                self.os_modules.add(local)
            elif alias.name == "ast":
                self.ast_modules.add(local)
            elif alias.name == "glob":
                self.glob_modules.add(local)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            local = alias.asname or alias.name
            if node.module == "os" and alias.name == "walk":
                self.os_walk_functions.add(local)
            elif node.module == "os" and alias.name == "fwalk":
                self.os_fwalk_functions.add(local)
            elif node.module == "glob" and alias.name == "glob":
                self.glob_functions.add(local)
            elif node.module == "glob" and alias.name == "iglob":
                self.iglob_functions.add(local)
        self.generic_visit(node)

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
            and node.func.value.id in self.os_modules
        ):
            kind = "os.walk"
        elif (
            isinstance(node.func, ast.Name)
            and node.func.id in self.os_walk_functions
        ):
            kind = "os.walk"
        elif (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "fwalk"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in self.os_modules
        ):
            kind = "os.fwalk"
        elif (
            isinstance(node.func, ast.Name)
            and node.func.id in self.os_fwalk_functions
        ):
            kind = "os.fwalk"
        elif (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "walk"
            and not (
                isinstance(node.func.value, ast.Name)
                and node.func.value.id in self.ast_modules
            )
        ):
            kind = "Path.walk"
        elif recursive_glob_kind := self._recursive_glob_kind(node):
            kind = recursive_glob_kind
        elif self._is_recursive_path_glob_call(node):
            kind = "Path.glob(recursive pattern)"
        if kind is not None:
            argument = ast.unparse(node.args[0]) if node.args else ""
            function = self.functions[-1] if self.functions else "<module>"
            self.calls.append((self.relative, function, kind, argument))
        self.generic_visit(node)

    def _recursive_glob_kind(self, node: ast.Call) -> str | None:
        if isinstance(node.func, ast.Attribute):
            if not (
                isinstance(node.func.value, ast.Name)
                and node.func.value.id in self.glob_modules
                and node.func.attr in {"glob", "iglob"}
            ):
                return None
            function_name = node.func.attr
        else:
            if not isinstance(node.func, ast.Name):
                return None
            if node.func.id in self.glob_functions:
                function_name = "glob"
            elif node.func.id in self.iglob_functions:
                function_name = "iglob"
            else:
                return None
        recursive = any(
            keyword.arg == "recursive"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True
            for keyword in node.keywords
        )
        return f"glob.{function_name}(recursive=True)" if recursive else None

    def _is_recursive_path_glob_call(self, node: ast.Call) -> bool:
        if not (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "glob"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            return False
        pattern_parts = node.args[0].value.replace("\\", "/").split("/")
        return "**" in pattern_parts


def _walk_calls(repo_root: Path) -> list[WalkKey]:
    calls: list[WalkKey] = []
    for relative_dir in (".project_manager/tools", "scripts", "tests"):
        directory = repo_root / relative_dir
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.py")):
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
        "repo-owned 열거 seam을 우회한 신규 재귀 tree-walk 호출: "
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
        (
            "future_os_alias.py",
            "import os as operating\ndef collect(root):\n    return operating.walk(root)\n",
            "os.walk",
        ),
        (
            "future_from_os.py",
            "from os import walk as tree_walk\ndef collect(root):\n    return tree_walk(root)\n",
            "os.walk",
        ),
        (
            "future_path_walk.py",
            "from pathlib import Path\ndef collect(root):\n    return Path(root).walk()\n",
            "Path.walk",
        ),
        (
            "future_recursive_glob.py",
            "import glob\ndef collect(root):\n"
            "    return glob.glob(str(root / '**' / '*.md'), recursive=True)\n",
            "glob.glob(recursive=True)",
        ),
        (
            "future_recursive_iglob.py",
            "import glob\ndef collect(root):\n"
            "    return glob.iglob(str(root / '**' / '*.md'), recursive=True)\n",
            "glob.iglob(recursive=True)",
        ),
        (
            "future_path_glob.py",
            "def collect(root):\n    return root.glob('**/*')\n",
            "Path.glob(recursive pattern)",
        ),
        (
            "future_fwalk.py",
            "import os\ndef collect(root):\n    return os.fwalk(root)\n",
            "os.fwalk",
        ),
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


def test_guard_recursively_scans_nested_python_files(tmp_path):
    target = tmp_path / "tests" / "data" / "nested" / "inventory.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "def collect(root):\n    return root.rglob('*.md')\n",
        encoding="utf-8",
    )

    unauthorized, _stale, _blank = _audit(tmp_path, exceptions={})

    assert (
        "tests/data/nested/inventory.py",
        "collect",
        "rglob",
        "'*.md'",
    ) in unauthorized


@pytest.mark.parametrize(
    ("key", "body"),
    [
        (
            (
                "tests/test_pm_render.py",
                "test_adapter_surfaces_no_machine_variant_tokens",
                "rglob",
                "'*.md'",
            ),
            "def test_adapter_surfaces_no_machine_variant_tokens(surface):\n"
            "    return surface.rglob('*.md')\n",
        ),
        (
            (
                "tests/test_adapter_session_identity.py",
                "_scanned_files",
                "rglob",
                "'*.md'",
            ),
            "def _scanned_files(d):\n    return d.rglob('*.md')\n",
        ),
        (
            (
                "tests/test_adapter_free_form_free.py",
                "test_render_scoped_dir_present_and_nonempty",
                "rglob",
                "'*'",
            ),
            "def test_render_scoped_dir_present_and_nonempty(d):\n"
            "    return d.rglob('*')\n",
        ),
        (
            (
                "tests/test_adapter_free_form_free.py",
                "_render_scoped_text_files",
                "rglob",
                "'*'",
            ),
            "def _render_scoped_text_files(d):\n    return d.rglob('*')\n",
        ),
        (
            (
                "tests/test_manifest_template_parity.py",
                "_expand_manifest_files",
                "rglob",
                "'*'",
            ),
            "def _expand_manifest_files(p):\n    return p.rglob('*')\n",
        ),
        (
            (
                "tests/test_flag_unification_parity.py",
                "_scan_paths_for_old_flags",
                "rglob",
                "'*.md'",
            ),
            "def _scan_paths_for_old_flags(base):\n    return base.rglob('*.md')\n",
        ),
        (
            (
                "tests/test_settings_hygiene.py",
                "_long_engine_command_markdown",
                "rglob",
                "'*.md'",
            ),
            "def _long_engine_command_markdown(root):\n    return root.rglob('*.md')\n",
        ),
        (
            (
                "tests/test_pm_handoff_shipping.py",
                "_expand_manifest_shipping_paths",
                "os.walk",
                "abs_p",
            ),
            "import os\n"
            "def _expand_manifest_shipping_paths(abs_p):\n"
            "    return os.walk(abs_p)\n",
        ),
        (
            (
                "tests/test_opencode_command_skill_pairing.py",
                "_skill_files",
                "rglob",
                "'SKILL.md'",
            ),
            "def _skill_files(base):\n    return base.rglob('SKILL.md')\n",
        ),
        (
            (
                "tests/test_skill_command_existence.py",
                "_iter_md_files",
                "rglob",
                "'*.md'",
            ),
            "def _iter_md_files(base):\n    return base.rglob('*.md')\n",
        ),
        (
            (
                "tests/test_terminology.py",
                "_canonical_source_files",
                "glob.glob(recursive=True)",
                "str(REPO / g)",
            ),
            "import glob\n"
            "def _canonical_source_files(REPO, g):\n"
            "    return glob.glob(str(REPO / g), recursive=True)\n",
        ),
        (
            (
                "tests/test_opencode_adapter_v2_docs.py",
                "test_no_shipped_opencode_doc_points_to_removed_agents_anchors",
                "rglob",
                "'*.md'",
            ),
            "def test_no_shipped_opencode_doc_points_to_removed_agents_anchors():\n"
            "    return opencode_root.rglob('*.md')\n",
        ),
    ],
)
def test_removed_migration_walk_restoration_is_rejected(tmp_path, key, body):
    """T-0506 전환을 옛 disk walk로 되돌리면 실 원장 가드가 즉시 red다."""
    target = tmp_path / key[0]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")

    unauthorized, _stale, _blank = _audit(tmp_path, exceptions=ALL_EXCEPTIONS)

    assert key in unauthorized


def test_guard_rejects_blank_exception_reason(tmp_path):
    target = tmp_path / ".project_manager" / "tools" / "future.py"
    target.parent.mkdir(parents=True)
    target.write_text("def collect(root):\n    return root.rglob('*')\n", encoding="utf-8")
    key = (".project_manager/tools/future.py", "collect", "rglob", "'*'")

    _unauthorized, _stale, blank = _audit(
        tmp_path, exceptions={key: WalkException("   ")})

    assert blank == [key]
