"""T-0677 tier 기계 보조 신호 회귀."""

from __future__ import annotations

import importlib.util
import sys
import types
import uuid
from pathlib import Path

from _git_fixture import init_git_repo


REPO = Path(__file__).resolve().parents[1]
BOARD_PY = REPO / ".project_manager" / "tools" / "board.py"


def _load_board():
    name = f"board_tier_signals_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, BOARD_PY)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _tool_tree(tmp_path: Path) -> Path:
    tools = tmp_path / ".project_manager" / "tools"
    tools.mkdir(parents=True)
    (tools / "shared.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tools / "alpha.py").write_text("import shared\n", encoding="utf-8")
    (tools / "beta.py").write_text("from shared import VALUE\n", encoding="utf-8")
    (tools / "solo.py").write_text("VALUE = 2\n", encoding="utf-8")
    # 테스트의 import는 공용 코드 사용 도구 수에 포함되지 않는다.
    (tools / "test_solo.py").write_text("import solo\n", encoding="utf-8")
    # 공용 코드 판정은 `git ls-files` 가 낸 repo-owned 목록을 입력으로 쓴다 — 픽스처가 자기
    # checkout 이라고 선언해야 답이 픽스처 자신의 함수가 된다.
    init_git_repo(tmp_path)
    return tools


def test_tier_signals_tool_module_boundary_one_and_two(tmp_path):
    board = _load_board()
    tools = _tool_tree(tmp_path)

    one = board.tier_signals([".project_manager/tools/alpha.py"], tools)
    two = board.tier_signals([
        ".project_manager/tools/alpha.py",
        ".project_manager/tools/beta.py",
        ".project_manager/tools/test_helper.py",
    ], tools)

    assert len(one.tool_modules) == 1
    assert len(two.tool_modules) == 2
    assert ".project_manager/tools/test_helper.py" not in two.tool_modules


def test_shared_code_list_is_derived_from_current_import_statements(tmp_path):
    board = _load_board()
    tools = _tool_tree(tmp_path)

    before = board._shared_tool_code(tools)
    assert before == (".project_manager/tools/shared.py",)

    # 하드코딩 목록이 아니라 import 구문의 현재 상태를 따라감을 고정한다.
    (tools / "beta.py").write_text("from solo import VALUE\n", encoding="utf-8")
    after = board._shared_tool_code(tools)
    assert after == ()


def test_shared_code_derives_literal_dynamic_loader_dependencies(tmp_path):
    board = _load_board()
    tools = tmp_path / ".project_manager" / "tools"
    tools.mkdir(parents=True)
    (tools / "shared.py").write_text("VALUE = 1\n", encoding="utf-8")
    loader = (
        "path = ROOT / 'shared.py'\n"
        "module = _load_module_from_path(path, 'shared.py', verifier=verify)\n"
    )
    (tools / "alpha.py").write_text(loader, encoding="utf-8")
    (tools / "beta.py").write_text(loader, encoding="utf-8")
    init_git_repo(tmp_path)

    assert board._shared_tool_code(tools) == (".project_manager/tools/shared.py",)


def test_shared_scan_inventory_is_owned_seam_not_disk_walk(tmp_path):
    board = _load_board()
    tools = tmp_path / ".project_manager" / "tools"
    tools.mkdir(parents=True)
    (tools / "owned.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tools / "alpha.py").write_text("import owned\nimport ignored\n", encoding="utf-8")
    (tools / "beta.py").write_text("import owned\nimport ignored\n", encoding="utf-8")
    # 디스크에 있어도 중앙 OWNED 열거가 내지 않은 파일은 스캔 대상이 아니다.
    (tools / "ignored.py").write_text("VALUE = 2\n", encoding="utf-8")
    owned = [
        Path(".project_manager/tools/owned.py"),
        Path(".project_manager/tools/alpha.py"),
        Path(".project_manager/tools/beta.py"),
    ]

    assert board._shared_tool_code(
        tools, list_owned=lambda _repo, _subtree: owned,
    ) == (".project_manager/tools/owned.py",)
    source = BOARD_PY.read_text(encoding="utf-8")
    function_source = source.split("def _shared_tool_code", 1)[1].split("\ndef ", 1)[0]
    assert ".rglob(" not in function_source


def test_real_file_lock_is_shared_via_literal_dynamic_loads():
    board = _load_board()
    tools = REPO / ".project_manager" / "tools"
    assert ".project_manager/tools/file_lock.py" in board._shared_tool_code(tools)


def test_tier_signals_marks_only_touched_shared_code(tmp_path):
    board = _load_board()
    tools = _tool_tree(tmp_path)
    signals = board.tier_signals([
        ".project_manager/tools/shared.py",
        ".project_manager/tools/solo.py",
    ], tools)
    assert signals.shared_code == (".project_manager/tools/shared.py",)


def test_tool_directory_touch_expands_to_owned_python_modules(tmp_path):
    board = _load_board()
    repo = tmp_path
    tools = repo / ".project_manager" / "tools"
    tools.mkdir(parents=True)
    for name in ("alpha.py", "beta.py", "test_helper.py"):
        (tools / name).write_text("VALUE = 1\n", encoding="utf-8")

    signals = board.tier_signals(
        [".project_manager/tools/"], tools,
        list_owned=lambda _repo, _subtree: [
            Path(".project_manager/tools/alpha.py"),
            Path(".project_manager/tools/beta.py"),
            Path(".project_manager/tools/test_helper.py"),
        ],
    )
    assert signals.tool_modules == (
        ".project_manager/tools/alpha.py",
        ".project_manager/tools/beta.py",
    )
    assert signals.h1 is True


def test_unresolved_tool_directory_uses_conservative_h1_default(tmp_path):
    board = _load_board()
    tools = tmp_path / ".project_manager" / "tools"
    signals = board.tier_signals(
        [".project_manager/tools/"], tools,
        list_owned=lambda _repo, _subtree: (_ for _ in ()).throw(OSError("boom")),
    )
    assert signals.tool_modules == ()
    assert signals.unresolved_directories == (".project_manager/tools",)
    assert signals.h1 is True


def test_docs_only_accepts_cards_wiki_readme_and_changelog(tmp_path):
    board = _load_board()
    tools = _tool_tree(tmp_path)
    docs = [
        ".claude/skills/pm-foo/SKILL.md",
        ".claude/agents/developer.md",
        ".codex/agents/developer.toml",
        ".project_manager/wiki/guide.md",
        "AGENTS.md",
        "README.md",
        "docs/CHANGELOG.md",
    ]
    assert board.tier_signals(docs, tools).docs_only is True
    assert board.tier_signals(docs + ["pyproject.toml"], tools).docs_only is False
    assert board.tier_signals([], tools).docs_only is False


def test_docs_only_expands_skill_directory_touch_like_t0678(tmp_path):
    board = _load_board()
    tools = tmp_path / ".project_manager" / "tools"
    tools.mkdir(parents=True)
    skill = tmp_path / ".claude" / "skills" / "pm-example" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# skill\n", encoding="utf-8")
    owned = [Path(".claude/skills/pm-example/SKILL.md")]
    signals = board.tier_signals(
        [".claude/skills/"], tools,
        list_owned=lambda _repo, _subtree: owned,
    )
    assert signals.docs_only is True


def test_docs_only_unresolved_directory_cannot_false_safe_yes(tmp_path):
    board = _load_board()
    tools = tmp_path / ".project_manager" / "tools"
    signals = board.tier_signals(
        [".claude/skills/"], tools,
        list_owned=lambda _repo, _subtree: (_ for _ in ()).throw(OSError("boom")),
    )
    assert signals.docs_only is False


def test_tier_signals_cli_is_advisory_rc_zero(tmp_path, monkeypatch, capsys):
    board = _load_board()
    tools = _tool_tree(tmp_path)
    ticket = tmp_path / "ticket.md"
    monkeypatch.setattr(board, "REPO", tmp_path)
    monkeypatch.setattr(board, "find_ticket_exact", lambda _tid: ("claimed", ticket))
    monkeypatch.setattr(board, "load_ticket", lambda _path: ({
        "touches": [".project_manager/tools/shared.py"],
    }, ""))

    assert board.cmd_tier_signals(types.SimpleNamespace(id="T-0677")) == 0
    out = capsys.readouterr().out
    assert "advisory" in out and "h1 tool modules: 1" in out
    assert "h2 shared code: yes" in out and "docs-only: no" in out
