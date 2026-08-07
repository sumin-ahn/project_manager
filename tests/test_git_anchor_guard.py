"""T-0587 — raw git cwd-misanchor 판정 seam + 3하네스 배선."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
BOARD_PY = REPO / ".project_manager" / "tools" / "board.py"
CLAUDE_DRIVER = REPO / "templates" / "claude_code" / ".claude" / "pm_orch_claude.py"
CLAUDE_WRAPPER = REPO / "templates" / "claude_code" / ".claude" / "ctx_stop_hook.sh"
OPEN_CORE = REPO / "templates" / "opencode" / ".opencode" / "lib" / "git-anchor-core.cjs"
OPEN_PLUGIN = REPO / "templates" / "opencode" / ".opencode" / "plugins" / "git-anchor.js"
CODEX_RULES = REPO / "templates" / "codex" / ".codex" / "rules" / "default.rules"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(cwd: Path, *args: str) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", *args], cwd=cwd, env=env, check=True,
                   capture_output=True, text=True, encoding="utf-8", errors="replace")


def _seed_repo(root: Path) -> None:
    root.mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    for rel in ("tests/shared.txt", "templates/shared.txt"):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("seed\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "seed")


@pytest.fixture
def topology(tmp_path):
    """서로 다른 git인 PM 홈 + 그 아래 등록 linked slot(양쪽 tests/templates 실재)."""
    home = tmp_path / "pm-home"
    _seed_repo(home)
    ticket = home / ".project_manager" / "wiki" / "tickets" / "done" / "T-0001-x.md"
    ticket.parent.mkdir(parents=True)
    ticket.write_text("---\nid: T-0001\n---\n", encoding="utf-8")
    status = home / ".project_manager" / "wiki" / "status.md"
    status.parent.mkdir(parents=True, exist_ok=True)
    status.write_text("status\n", encoding="utf-8")

    source = tmp_path / "product-source"
    _seed_repo(source)
    slot = home / "work" / "product_1"
    slot.parent.mkdir(parents=True)
    _git(source, "worktree", "add", "-q", str(slot))
    ledger = home / ".project_manager" / ".local" / "worktree-leases.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(json.dumps({"leases": [{"slot": "work/product_1", "repo": "product",
                                               "state": "leased"}]}) + "\n", encoding="utf-8")
    return home, slot


def test_cross_existing_pathspec_is_the_only_deny(topology):
    board = _load("git_anchor_board_deny", BOARD_PY)
    home, _slot = topology
    got = board.judge_git_anchor(str(home), ["git", "add", "tests/shared.txt"])
    assert got["verdict"] == "deny"
    assert got["cwd_identity"] == "pm-home"
    assert "tests/shared.txt" in got["reason"]


def test_false_deny_zero_for_normal_home_slot_and_missing_path(topology, tmp_path):
    board = _load("git_anchor_board_normal", BOARD_PY)
    home, slot = topology
    normal_home = board.judge_git_anchor(
        str(home), ["git", "commit", "-m", "done", "--", ".project_manager/wiki/status.md"]
    )
    normal_slot = board.judge_git_anchor(
        str(slot), ["git", "commit", "-m", "code", "--", "tests/shared.txt"]
    )
    missing = board.judge_git_anchor(str(home), ["git", "add", "tests/not-there.txt"])
    reset_cwd = board.judge_git_anchor(str(tmp_path / "not-a-repo"), ["git", "commit", "-m", "x"])
    assert normal_home["verdict"] == "warn" and normal_home["cwd_identity"] == "pm-home"
    assert normal_slot["verdict"] == "ok" and normal_slot["cwd_identity"] == "slot"
    assert missing["verdict"] == "warn"
    assert reset_cwd["verdict"] == "warn" and reset_cwd["cwd_identity"] == "non-repo"


def test_read_git_is_ok_and_git_dash_c_changes_anchor(topology):
    board = _load("git_anchor_board_read", BOARD_PY)
    home, slot = topology
    assert board.judge_git_anchor(str(home), ["git", "status"])["verdict"] == "ok"
    got = board.judge_git_anchor(str(home), ["git", "-C", str(slot), "add", "tests/shared.txt"])
    assert got["verdict"] == "ok" and got["cwd_identity"] == "slot"


def test_shell_parser_uses_worst_git_judgment(topology):
    board = _load("git_anchor_board_shell", BOARD_PY)
    home, slot = topology
    got = board.judge_git_anchor_command(
        str(home), "echo pre && git status && git add -- tests/shared.txt"
    )
    assert got["verdict"] == "deny"
    assert board.judge_git_anchor_command(str(home), "python3 build.py")["verdict"] == "ok"
    # 명시 cd를 안 따르면 PM 홈 tests로 오인해 정상 slot 작업을 false-deny한다.
    changed = board.judge_git_anchor_command(
        str(home), f"cd {slot} && git add -- tests/shared.txt"
    )
    assert changed["verdict"] == "ok" and changed["cwd_identity"] == "slot"


@pytest.mark.parametrize(
    ("command", "identity"),
    [
        ("cd /nonexistent || git add tests/shared.txt", "pm-home"),
        ("cd {slot} | git add tests/shared.txt", "pm-home"),
        ("false && cd {slot}; git add tests/shared.txt", "pm-home"),
        ("/usr/bin/git add tests/shared.txt", "pm-home"),
    ],
)
def test_shell_control_flow_preserves_actual_git_cwd(topology, command, identity):
    board = _load(f"git_anchor_shell_{identity}_{abs(hash(command))}", BOARD_PY)
    home, slot = topology
    got = board.judge_git_anchor_command(
        str(home), command.format(home=home, slot=slot)
    )
    assert got["verdict"] == "deny"
    assert got["cwd_identity"] == identity


def test_shell_if_and_multiple_git_calls_keep_context(topology):
    board = _load("git_anchor_shell_if_multi", BOARD_PY)
    home, slot = topology
    conditional = board.judge_git_anchor_command(
        str(home), f"if cd {slot}; then git add tests/shared.txt; fi"
    )
    assert conditional["verdict"] == "ok"
    assert conditional["cwd_identity"] == "slot"

    multiple = board.judge_git_anchor_command(
        str(home), f"git -C {slot} commit -m slot && git commit -m home"
    )
    assert multiple["verdict"] == "warn"
    assert "호출 1 [slot/ok]" in multiple["reason"]
    assert "호출 2 [pm-home/warn]" in multiple["reason"]


def test_dynamic_cd_is_ambiguous_and_never_blocks(topology):
    board = _load("git_anchor_shell_dynamic_cd", BOARD_PY)
    home, _slot = topology
    got = board.judge_git_anchor_command(
        str(home), "cd $TARGET; git add tests/shared.txt"
    )
    assert got["verdict"] == "warn"
    assert "정적으로 단일 증명할 수 없음" in got["reason"]


@pytest.mark.parametrize(
    "command",
    [
        "cd {slot} >/dev/null && git add tests/shared.txt",
        "cd {slot} 2>/dev/null && git add tests/shared.txt",
        "cd -P {slot} && git add tests/shared.txt",
        "pushd {slot} && git add tests/shared.txt",
        "pushd {slot} >/dev/null; git add tests/shared.txt",
        "pushd {slot}; popd; git add tests/shared.txt",
    ],
)
def test_unmodeled_cwd_changers_are_ambiguity_warn(topology, command):
    board = _load(f"git_anchor_shell_unknown_cwd_{abs(hash(command))}", BOARD_PY)
    home, slot = topology
    got = board.judge_git_anchor_command(str(home), command.format(slot=slot))
    assert got["verdict"] == "warn"
    assert "정적으로 단일 증명할 수 없음" in got["reason"]


def test_unexecuted_cwd_changer_preserves_proven_home_anchor(topology):
    board = _load("git_anchor_shell_unexecuted_pushd", BOARD_PY)
    home, slot = topology
    got = board.judge_git_anchor_command(
        str(home), f"false && pushd {slot}; git add tests/shared.txt"
    )
    assert got["verdict"] == "deny"
    assert got["cwd_identity"] == "pm-home"


@pytest.mark.parametrize(
    "command",
    [
        "cat <<'EOF' > NOTES.md\n# how to stage\ngit add tests/shared.txt\nEOF",
        "cat <<EOF > NOTES.md\ngit add tests/shared.txt\nEOF",
        "cat <<-EOF >> NOTES.md\n\tgit add tests/shared.txt\n\tEOF",
        "git commit -F - <<EOF\nmessage: git add tests/shared.txt\nEOF",
    ],
)
def test_heredoc_body_git_is_data_and_never_denies(topology, command):
    board = _load(f"git_anchor_heredoc_{abs(hash(command))}", BOARD_PY)
    home, _slot = topology
    got = board.judge_git_anchor_command(str(home), command)
    assert got["verdict"] in {"ok", "warn"}


def test_real_commands_around_heredoc_are_still_judged(topology):
    board = _load("git_anchor_heredoc_surrounding", BOARD_PY)
    home, _slot = topology
    before = board.judge_git_anchor_command(
        str(home), "git add tests/shared.txt; cat <<EOF\ngit status\nEOF"
    )
    after = board.judge_git_anchor_command(
        str(home), "cat <<EOF\ngit status\nEOF\ngit add tests/shared.txt"
    )
    assert before["verdict"] == "deny"
    assert after["verdict"] == "deny"


@pytest.mark.parametrize("command", [
    "echo '<<EOF'\ngit add tests/shared.txt\nEOF",
    "echo ok # <<EOF\ngit add tests/shared.txt\nEOF",
])
def test_fake_heredoc_does_not_hide_real_git(topology, command):
    board = _load(f"git_anchor_fake_heredoc_{abs(hash(command))}", BOARD_PY)
    home, _slot = topology
    assert board.judge_git_anchor_command(str(home), command)["verdict"] == "deny"


@pytest.mark.parametrize(
    "command",
    [
        "git add tests/shared.txt &",
        "{ git add tests/shared.txt; }",
    ],
)
def test_unmodeled_shell_shapes_surface_ambiguity_warn(topology, command):
    board = _load(f"git_anchor_unknown_shape_{abs(hash(command))}", BOARD_PY)
    home, _slot = topology
    got = board.judge_git_anchor_command(str(home), command)
    assert got["verdict"] == "warn"
    assert "정적으로" in got["reason"]


@pytest.mark.parametrize(
    "command",
    [
        "echo git add tests/shared.txt",
        "printf %s git add tests/shared.txt",
        "# git add tests/shared.txt",
    ],
)
def test_git_data_arguments_and_comments_are_not_commands(topology, command):
    board = _load(f"git_anchor_shell_data_{abs(hash(command))}", BOARD_PY)
    home, _slot = topology
    got = board.judge_git_anchor_command(str(home), command)
    assert got["verdict"] == "ok"


@pytest.mark.parametrize(
    "command",
    [
        "while false; do git add tests/shared.txt; done",
        "for x in one; do git add tests/shared.txt; done",
    ],
)
def test_unsupported_loop_control_is_ambiguity_warn(topology, command):
    board = _load(f"git_anchor_shell_loop_{abs(hash(command))}", BOARD_PY)
    home, _slot = topology
    got = board.judge_git_anchor_command(str(home), command)
    assert got["verdict"] == "warn"
    assert "정적으로 단일 증명할 수 없음" in got["reason"]


def test_newline_is_a_sequential_shell_boundary(topology):
    board = _load("git_anchor_shell_newline", BOARD_PY)
    home, slot = topology
    got = board.judge_git_anchor_command(
        str(home), f"git -C {slot} status\ngit add tests/shared.txt"
    )
    assert got["verdict"] == "deny"
    assert "호출 1 [slot/ok]" in got["reason"]
    assert "호출 2 [pm-home/deny]" in got["reason"]


def test_if_else_tracks_failed_cd_branch_cwd(topology):
    board = _load("git_anchor_shell_else", BOARD_PY)
    home, _slot = topology
    got = board.judge_git_anchor_command(
        str(home),
        "if cd /nonexistent; then true; else git add tests/shared.txt; fi",
    )
    assert got["verdict"] == "deny"
    assert got["cwd_identity"] == "pm-home"


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "add", "tests/../templates/shared.txt"],
        ["git", "add", ":(top)tests/shared.txt"],
        ["git", "restore", "tests/shared.txt"],
        ["git", "commit", "-m", "path", "tests/shared.txt"],
    ],
)
def test_git_pathspec_normalization_and_no_separator_forms_deny(topology, argv):
    board = _load(f"git_anchor_pathspec_{abs(hash(tuple(argv)))}", BOARD_PY)
    home, _slot = topology
    got = board.judge_git_anchor(str(home), argv)
    assert got["verdict"] == "deny"


def test_shell_redirection_targets_are_not_git_pathspecs(topology):
    board = _load("git_anchor_shell_redirection", BOARD_PY)
    home, _slot = topology
    commit = board.judge_git_anchor_command(
        str(home), "git commit -m msg > tests/shared.txt"
    )
    add = board.judge_git_anchor_command(
        str(home), "git add src/app.py 2> tests/shared.txt"
    )
    still_dangerous = board.judge_git_anchor_command(
        str(home), "git add tests/shared.txt > /dev/null"
    )
    assert commit["verdict"] == "warn"
    assert add["verdict"] == "warn"
    assert still_dangerous["verdict"] == "deny"


@pytest.mark.parametrize("command", [
    "git>/tmp/out add tests/shared.txt",
    "git</dev/null add tests/shared.txt",
    "/usr/bin/git>out add tests/shared.txt",
])
def test_attached_redirection_still_recognizes_git(topology, command):
    board = _load(f"git_anchor_attached_redirect_{abs(hash(command))}", BOARD_PY)
    home, _slot = topology
    assert board.judge_git_anchor_command(str(home), command)["verdict"] == "deny"


def test_pathspecs_resolve_from_invocation_cwd(topology):
    board = _load("git_anchor_pathspec_cwd", BOARD_PY)
    home, _slot = topology
    assert board.judge_git_anchor(str(home / "tests"), ["git", "add", "shared.txt"])["verdict"] == "deny"
    assert board.judge_git_anchor(str(home), ["git", "-C", "tests", "add", "shared.txt"])["verdict"] == "deny"
    assert board.judge_git_anchor(str(home / "templates"), ["git", "add", "../tests/shared.txt"])["verdict"] == "deny"


def test_work_tree_and_git_dir_overrides_are_never_approved_or_denied(topology):
    board = _load("git_anchor_work_tree_override", BOARD_PY)
    home, slot = topology
    cases = [
        (str(slot), ["git", "--work-tree", str(home), "add", "tests/shared.txt"]),
        (str(home), ["git", "--work-tree", str(slot), "add", "tests/shared.txt"]),
        (str(slot), [f"GIT_WORK_TREE={home}", "git", "add", "tests/shared.txt"]),
        (str(slot), [f"GIT_DIR={home / '.git'}", "git", "add", "tests/shared.txt"]),
    ]
    assert all(board.judge_git_anchor(cwd, argv)["verdict"] == "warn" for cwd, argv in cases)


def test_git_clean_exclude_values_are_not_pathspecs(topology):
    board = _load("git_anchor_clean_exclude", BOARD_PY)
    home, _slot = topology
    assert board.judge_git_anchor(str(home), ["git", "clean", "-e", "tests/shared.txt"])["verdict"] == "warn"
    assert board.judge_git_anchor(str(home), ["git", "clean", "--exclude", "tests/shared.txt"])["verdict"] == "warn"
    assert board.judge_git_anchor(str(home), ["git", "clean", "-ne", "tests/shared.txt"])["verdict"] == "warn"


def test_r8_parser_boundaries(topology):
    board = _load("git_anchor_r8", BOARD_PY)
    home, slot = topology
    assert board.judge_git_anchor(str(home / "tests"), ["git", "add", ":(top)tests/shared.txt"])["verdict"] == "deny"
    assert board.judge_git_anchor(str(home / "tests"), ["git", "add", ":/tests/shared.txt"])["verdict"] == "deny"
    assert board.judge_git_anchor_command(str(home), "echo x# <<EOF\ngit add tests/shared.txt\nEOF")["verdict"] != "deny"
    assert board.judge_git_anchor_command(str(home), "cat <<''\ngit add tests/shared.txt\n\n")["verdict"] != "deny"
    assert board.judge_git_anchor_command(str(home), "'git' add tests/shared.txt")["verdict"] == "deny"
    assert board.judge_git_anchor_command(str(home), "git\\\n add tests/shared.txt")["verdict"] == "deny"
    assert board.judge_git_anchor_command(str(slot), f"env GIT_WORK_TREE={home} GIT_DIR={home / '.git'} git add tests/shared.txt")["verdict"] == "warn"
    assert board.judge_git_anchor_command(str(home), "command git add tests/shared.txt")["verdict"] == "deny"


def test_r9_heredoc_data_preserves_backslash_newline(topology):
    board = _load("git_anchor_r9_heredoc", BOARD_PY)
    home, _slot = topology
    command = "cat <<'EOF'\nEO\\\nF\ngit add tests/shared.txt\nEOF"
    assert board.judge_git_anchor_command(str(home), command)["verdict"] == "ok"
    assert board._normalize_shell_line_continuations("git\\\n add") == "git add"
    quoted = "'git\\\n add'"
    assert board._normalize_shell_line_continuations(quoted) == quoted


def test_r9_wrapper_and_fragmented_command_words(topology):
    board = _load("git_anchor_r9_wrappers", BOARD_PY)
    home, slot = topology
    overrides = [
        f"env -i PATH=/usr/bin:/bin GIT_WORK_TREE={home} GIT_DIR={home / '.git'} git add tests/shared.txt",
        f"env -- GIT_WORK_TREE={home} GIT_DIR={home / '.git'} git add tests/shared.txt",
        f"/usr/bin/env GIT_WORK_TREE={home} GIT_DIR={home / '.git'} git add tests/shared.txt",
    ]
    assert all(
        board.judge_git_anchor_command(str(slot), command)["verdict"] == "warn"
        for command in overrides
    )
    home_mutations = [
        "command -- git add tests/shared.txt",
        "command -p git add tests/shared.txt",
        "g''it add tests/shared.txt",
        "/usr/bin/g''it add tests/shared.txt",
    ]
    assert all(
        board.judge_git_anchor_command(str(home), command)["verdict"] == "deny"
        for command in home_mutations
    )
    assert board.judge_git_anchor_command(
        str(slot), "command -p git add tests/shared.txt"
    )["verdict"] == "ok"
    assert board.judge_git_anchor_command(
        str(home), "env --unknown git add tests/shared.txt"
    )["verdict"] == "warn"


def test_r10_execution_wrappers_and_escaped_quote_continuation(topology):
    board = _load("git_anchor_r10_wrappers", BOARD_PY)
    home, slot = topology
    wrappers = [
        "exec git add tests/shared.txt",
        "nice git add tests/shared.txt",
        "timeout 5 git add tests/shared.txt",
        "nohup git add tests/shared.txt",
        "sudo git add tests/shared.txt",
        "exec env -i command -p nice -n 5 timeout --preserve-status 5 nohup -- git add tests/shared.txt",
    ]
    assert all(
        board.judge_git_anchor_command(str(home), command)["verdict"] == "deny"
        for command in wrappers
    )
    assert all(
        board.judge_git_anchor_command(str(slot), command)["verdict"] == "ok"
        for command in wrappers
    )
    assert board.judge_git_anchor_command(
        str(home), "sudo -u root git add tests/shared.txt"
    )["verdict"] == "warn"
    escaped_quote = 'echo \\"; git\\\n add tests/shared.txt'
    assert board.judge_git_anchor_command(str(home), escaped_quote)["verdict"] == "deny"


def test_r11_dynamic_command_word_is_ambiguity_not_ok(topology):
    board = _load("git_anchor_r11_dynamic_wrapper", BOARD_PY)
    home, slot = topology
    dynamic = [
        "$WRAPPER git add tests/shared.txt",
        '"$WRAPPER" git add tests/shared.txt',
    ]
    for cwd in (home, slot):
        assert all(
            board.judge_git_anchor_command(str(cwd), command)["verdict"] == "warn"
            for command in dynamic
        )
    assert board.judge_git_anchor_command(
        str(slot), "git add tests/shared.txt"
    )["verdict"] == "ok"
    data_only = [
        "echo $WRAPPER git add tests/shared.txt",
        "printf %s '$WRAPPER git add tests/shared.txt'",
        "# $WRAPPER git add tests/shared.txt",
    ]
    assert all(
        board.judge_git_anchor_command(str(home), command)["verdict"] == "ok"
        for command in data_only
    )


def test_pathspec_file_and_ambiguous_ref_forms_fail_open(topology):
    board = _load("git_anchor_pathspec_fail_open", BOARD_PY)
    home, _slot = topology
    from_file = board.judge_git_anchor(
        str(home), ["git", "add", "--pathspec-from-file", "templates/shared.txt"]
    )
    checkout = board.judge_git_anchor(str(home), ["git", "checkout", "tests/shared.txt"])
    reset = board.judge_git_anchor(str(home), ["git", "reset", "tests/shared.txt"])
    combined_message = board.judge_git_anchor(
        str(home), ["git", "commit", "-am", "tests/shared.txt"]
    )
    top_magic_shell = board.judge_git_anchor_command(
        str(home), "git add ':(top)tests/shared.txt'"
    )
    assert from_file["verdict"] == "warn"
    assert checkout["verdict"] == "warn"
    assert reset["verdict"] == "warn"
    assert combined_message["verdict"] == "warn"
    assert top_magic_shell["verdict"] == "deny"


def test_malicious_ledger_parent_and_symlink_slots_are_not_registered(topology, tmp_path):
    board = _load("git_anchor_lease_containment", BOARD_PY)
    home, _slot = topology
    ledger = home / ".project_manager" / ".local" / "worktree-leases.json"

    ledger.write_text(json.dumps({"leases": [
        {"slot": "../product-source", "repo": "product", "state": "leased"},
    ]}) + "\n", encoding="utf-8")
    assert board.judge_git_anchor(
        str(home), ["git", "add", "tests/shared.txt"]
    )["verdict"] == "warn"

    outside = tmp_path / "product-source"
    symlink_slot = home / "work" / "evil_1"
    symlink_slot.symlink_to(outside, target_is_directory=True)
    ledger.write_text(json.dumps({"leases": [
        {"slot": "work/evil_1", "repo": "evil", "state": "leased"},
    ]}) + "\n", encoding="utf-8")
    assert board.judge_git_anchor(
        str(home), ["git", "add", "tests/shared.txt"]
    )["verdict"] == "warn"


@pytest.mark.parametrize(
    "lease",
    [
        {"slot": "work/product_1", "repo": "product", "state": "idle"},
        {"slot": "work/x/../product_1", "repo": "product", "state": "leased"},
    ],
)
def test_only_canonical_active_lease_is_slot_identity(topology, lease):
    board = _load(f"git_anchor_lease_identity_{lease['state']}_{len(lease['slot'])}", BOARD_PY)
    home, slot = topology
    ledger = home / ".project_manager" / ".local" / "worktree-leases.json"
    ledger.write_text(json.dumps({"leases": [lease]}) + "\n", encoding="utf-8")
    got = board.judge_git_anchor(str(slot), ["git", "commit", "-m", "normal"])
    assert got["verdict"] == "warn"
    assert got["cwd_identity"] == "worktree"


def test_internal_symlink_slot_alias_and_target_are_not_registered(topology):
    board = _load("git_anchor_lease_internal_symlink", BOARD_PY)
    home, slot = topology
    alias = home / "work" / "evil_1"
    alias.symlink_to(slot, target_is_directory=True)
    ledger = home / ".project_manager" / ".local" / "worktree-leases.json"
    ledger.write_text(json.dumps({"leases": [
        {"slot": "work/evil_1", "repo": "evil", "state": "leased"},
    ]}) + "\n", encoding="utf-8")

    alias_got = board.judge_git_anchor(str(alias), ["git", "commit", "-m", "alias"])
    target_got = board.judge_git_anchor(str(slot), ["git", "commit", "-m", "target"])
    assert alias_got["verdict"] == "warn" and alias_got["cwd_identity"] == "worktree"
    assert target_got["verdict"] == "warn" and target_got["cwd_identity"] == "worktree"


def test_registered_slot_mutation_is_explicitly_allowed(topology):
    board = _load("git_anchor_registered_slot_allow", BOARD_PY)
    _home, slot = topology
    got = board.judge_git_anchor(str(slot), ["git", "add", "tests/shared.txt"])
    assert got["verdict"] == "ok"
    assert got["cwd_identity"] == "slot"


@pytest.mark.parametrize("subcommand", ["rm", "mv", "clean", "apply", "stash"])
def test_additional_mutations_warn_at_home_and_allow_registered_slot(topology, subcommand):
    board = _load(f"git_anchor_mutation_{subcommand}", BOARD_PY)
    home, slot = topology
    argv = ["git", subcommand]
    if subcommand == "mv":
        argv += ["tests/shared.txt", "templates/shared.txt"]
    elif subcommand == "apply":
        argv += ["change.patch"]
    elif subcommand == "stash":
        argv += ["push"]
    else:
        argv += ["tests/shared.txt"]
    home_got = board.judge_git_anchor(str(home), argv)
    slot_got = board.judge_git_anchor(str(slot), argv)
    expected_home = {
        "rm": "deny", "mv": "deny", "clean": "deny", "apply": "warn", "stash": "warn",
    }
    assert home_got["verdict"] == expected_home[subcommand]
    assert slot_got["verdict"] == "ok" and slot_got["cwd_identity"] == "slot"


def test_board_cli_emits_hook_json(topology, capsys):
    board = _load("git_anchor_board_cli", BOARD_PY)
    home, _slot = topology
    rc = board.main(["git-anchor", "--cwd", str(home), "--command", "git add tests/shared.txt"])
    got = json.loads(capsys.readouterr().out)
    assert rc == 0 and got["verdict"] == "deny"


def test_claude_hook_prefilter_warn_and_deny(monkeypatch, topology):
    driver = _load("git_anchor_claude", CLAUDE_DRIVER)
    home, _slot = topology
    calls = []

    class FakeBoard:
        @staticmethod
        def judge_git_anchor_command(cwd, command):
            calls.append((cwd, command))
            verdict = "warn" if "$WRAPPER" in command else ("deny" if "tests/" in command else "warn")
            return {"verdict": verdict, "cwd_identity": "pm-home", "reason": "fixture"}

    monkeypatch.setattr(driver, "_load_board", lambda _root: FakeBoard)
    base = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": str(home)}
    assert driver.git_anchor_hook_evaluate(
        {**base, "tool_input": {"command": "python3 build.py"}}, home
    ) is None
    assert calls == []  # 성능 DoD: 비-git은 seam import/호출 0.
    warn = driver.git_anchor_hook_evaluate(
        {**base, "tool_input": {"command": "git commit -m x"}}, home
    )["hookSpecificOutput"]
    deny = driver.git_anchor_hook_evaluate(
        {**base, "tool_input": {"command": "git add tests/x"}}, home
    )["hookSpecificOutput"]
    attached = driver.git_anchor_hook_evaluate(
        {**base, "tool_input": {"command": "git>/tmp/out add tests/x"}}, home
    )["hookSpecificOutput"]
    fragmented = driver.git_anchor_hook_evaluate(
        {**base, "tool_input": {"command": "g''it add tests/x"}}, home
    )["hookSpecificOutput"]
    escaped = driver.git_anchor_hook_evaluate(
        {**base, "tool_input": {"command": 'echo \\"; git\\\n add tests/x'}}, home
    )["hookSpecificOutput"]
    dynamic = driver.git_anchor_hook_evaluate(
        {**base, "tool_input": {"command": "$WRAPPER git add tests/x"}}, home
    )["hookSpecificOutput"]
    assert "additionalContext" in warn and "permissionDecision" not in warn
    assert deny["permissionDecision"] == "deny" and "permissionDecisionReason" in deny
    assert attached["permissionDecision"] == "deny"
    assert fragmented["permissionDecision"] == "deny"
    assert escaped["permissionDecision"] == "deny"
    assert "additionalContext" in dynamic and "permissionDecision" not in dynamic


@pytest.mark.parametrize("failure_site", ["import", "judge"])
def test_claude_hook_system_exit_is_warn_and_rc0(
    monkeypatch, topology, capsys, failure_site,
):
    driver = _load(f"git_anchor_claude_system_exit_{failure_site}", CLAUDE_DRIVER)
    home, _slot = topology

    class ExitBoard:
        @staticmethod
        def judge_git_anchor_command(_cwd, _command):
            raise SystemExit(23)

    if failure_site == "import":
        def fail_load(_root):
            raise SystemExit(17)
        monkeypatch.setattr(driver, "_load_board", fail_load)
    else:
        monkeypatch.setattr(driver, "_load_board", lambda _root: ExitBoard)
    payload = {
        "hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": str(home),
        "tool_input": {"command": "git add tests/shared.txt"},
    }
    monkeypatch.setattr(driver.sys, "stdin", io.StringIO(json.dumps(payload)))
    rc = driver.run_git_anchor_hook(home)
    got = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert "additionalContext" in got["hookSpecificOutput"]
    assert "판정 불가" in got["hookSpecificOutput"]["additionalContext"]


@pytest.mark.parametrize("failure_site", ["usage", "root"])
def test_claude_hook_mode_outer_boundary_is_warn_rc0(monkeypatch, capsys, failure_site):
    driver = _load(f"git_anchor_claude_outer_{failure_site}", CLAUDE_DRIVER)
    if failure_site == "usage":
        argv = ["--git-anchor-hook", "--bogus"]
    else:
        monkeypatch.setattr(
            driver.ctx_guard, "repo_root", lambda _path: (_ for _ in ()).throw(SystemExit(31)),
        )
        argv = ["--git-anchor-hook"]
    rc = driver.main(argv)
    got = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert "additionalContext" in got["hookSpecificOutput"]
    assert "판정 불가" in got["hookSpecificOutput"]["additionalContext"]


def test_claude_settings_and_self_resolving_wrapper_are_wired():
    wrapper = CLAUDE_WRAPPER.read_text(encoding="utf-8")
    assert "--git-anchor-hook" in wrapper and "pm_orch_claude.py" in wrapper
    for settings in (REPO / ".claude" / "settings.json",
                     REPO / "templates" / "claude_code" / ".claude" / "settings.json"):
        data = json.loads(settings.read_text(encoding="utf-8"))
        hooks = data["hooks"]["PreToolUse"]
        assert any(row.get("matcher") == "Bash" and "--git-anchor-hook" in json.dumps(row)
                   for row in hooks)


def test_codex_execpolicy_records_capability_gap_without_overbroad_rule():
    text = CODEX_RULES.read_text(encoding="utf-8")
    assert "raw git cwd-anchor 파리티 예외(T-0587)" in text
    assert "cwd predicate" in text and "false-deny 0" in text


def test_opencode_thin_plugin_and_core_node_selfcheck():
    assert OPEN_PLUGIN.is_file() and OPEN_CORE.is_file()
    plugin = OPEN_PLUGIN.read_text(encoding="utf-8")
    assert plugin.count("export const") == 1 and "GitAnchorPlugin" in plugin
    node = shutil.which("node")
    if node is None:
        pytest.skip("node 없음")
    script = r'''
const m = require("./git-anchor-core.cjs");
const assert = require("node:assert");
assert.strictEqual(m.containsGitCommand("python3 build.py"), false);
assert.strictEqual(m.containsGitCommand("echo x && git status"), true);
assert.strictEqual(m.containsGitCommand("/usr/bin/git>out status"), true);
assert.strictEqual(m.containsGitCommand("g''it add tests/shared.txt"), true);
assert.strictEqual(m.containsGitCommand("git\\\n add tests/shared.txt"), true);
assert.strictEqual(m.containsGitCommand('echo \\"; git\\\n add tests/shared.txt'), true);
assert.strictEqual(m.containsGitCommand('$WRAPPER git add tests/shared.txt'), true);
let calls = 0;
const fake = (py, argv, opts) => {
  calls += 1;
  return {status:0, stdout:JSON.stringify({verdict:"warn",cwd_identity:"pm-home",reason:"fixture"})+"\n", stderr:""};
};
assert.strictEqual(m.judgeCommand("/r", "/r", "python3 build.py", fake).verdict, "ok");
assert.strictEqual(calls, 0);
assert.strictEqual(m.judgeCommand("/r", "/r", "git commit -m x", fake).verdict, "warn");
assert.strictEqual(calls, 1);
assert.strictEqual(typeof m.GitAnchorPlugin, "function");
assert.strictEqual(typeof m.makeGitAnchorPlugin, "function");

(async () => {
  const toasts = [];
  const judge = (_root, cwd, command) => ({
    verdict: command.includes("git add") ? "deny" : "warn",
    cwd_identity: cwd.endsWith("slot") ? "slot" : "pm-home",
    reason: cwd.endsWith("slot") ? "slot fixture" : "home fixture",
  });
  const factory = m.makeGitAnchorPlugin(judge);
  const hooks = await factory({
    directory: "/repo",
    worktree: "/repo",
    client: {tui: {showToast: async (value) => toasts.push(value)}},
  });
  await hooks["tool.execute.before"](
    {tool:"bash", sessionID:"A"}, {args:{command:"git commit -m a", cwd:"/repo"}}
  );
  await hooks["tool.execute.before"](
    {tool:"bash", sessionID:"B"}, {args:{command:"git commit -m b", cwd:"/repo/slot"}}
  );
  const a1 = {system: []};
  await hooks["experimental.chat.system.transform"]({sessionID:"A"}, a1);
  assert.strictEqual(a1.system.length, 1);
  assert.match(a1.system[0], /home fixture/);
  const a2 = {system: []};
  await hooks["experimental.chat.system.transform"]({sessionID:"A"}, a2);
  assert.strictEqual(a2.system.length, 0);
  const b1 = {system: []};
  await hooks["experimental.chat.system.transform"]({sessionID:"B"}, b1);
  assert.strictEqual(b1.system.length, 1);
  assert.match(b1.system[0], /slot fixture/);
  assert.strictEqual(toasts.length, 2);
  await assert.rejects(
    hooks["tool.execute.before"](
      {tool:"bash", sessionID:"A"}, {args:{command:"git add tests/shared.txt", cwd:"/repo"}}
    ),
    /git-anchor\/deny/
  );

  const errorToasts = [];
  const failingFactory = m.makeGitAnchorPlugin(() => { throw new Error("fixture boom"); });
  const failingHooks = await failingFactory({
    directory: "/repo",
    worktree: "/repo",
    client: {tui: {showToast: async (value) => errorToasts.push(value)}},
  });
  await failingHooks["tool.execute.before"](
    {tool:"bash", sessionID:"ERR"}, {args:{command:"git add tests/shared.txt", cwd:"/repo"}}
  );
  const errorContext = {system: []};
  await failingHooks["experimental.chat.system.transform"]({sessionID:"ERR"}, errorContext);
  assert.strictEqual(errorContext.system.length, 1);
  assert.match(errorContext.system[0], /git-anchor\/warn/);
  assert.match(errorContext.system[0], /판정 인프라 예외\(fixture boom\)/);
  assert.strictEqual(errorToasts.length, 1);
  console.log("GIT_ANCHOR_CORE_OK");
})().catch((error) => { console.error(error); process.exit(1); });
'''
    result = subprocess.run([node, "-e", script], cwd=OPEN_CORE.parent, check=True,
                            capture_output=True, text=True, encoding="utf-8")
    assert "GIT_ANCHOR_CORE_OK" in result.stdout
