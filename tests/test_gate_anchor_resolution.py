from __future__ import annotations

import importlib.util
import json
import subprocess
import typing
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / ".project_manager" / "tools"


def _load(name: str):
    source = "pm_delegate.py" if name.startswith("pm_delegate") else "external_review.py"
    spec = importlib.util.spec_from_file_location(name, TOOLS / source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(path: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=path, check=True, capture_output=True, text=True,
    )


def _managed_worktree(tmp_path: Path) -> tuple[Path, Path, str]:
    home = tmp_path / "pm"
    home.mkdir(parents=True)
    _git(home, "init", "-q")
    _git(home, "config", "user.email", "test@example.invalid")
    _git(home, "config", "user.name", "test")
    (home / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(home, "add", "seed.txt")
    _git(home, "commit", "-qm", "seed")
    worktree = home / "work" / "slot"
    worktree.parent.mkdir()
    _git(home, "worktree", "add", "-q", "-b", "test-slot", str(worktree))
    # 실 채택 슬롯처럼 자기 엔진 사본 마커를 둬 repo_root_from_cwd가 슬롯에서 멈추고,
    # lease 기반 PM-home 재앵커가 실제로 load-bearing이 되게 한다.
    (worktree / ".project_manager" / "tools").mkdir(parents=True)

    ticket = "T-" + "9001"
    tickets = home / ".project_manager" / "wiki" / "tickets" / "open"
    tickets.mkdir(parents=True)
    (tickets / f"{ticket}-anchor.md").write_text(
        "---\n"
        f"id: {ticket}\n"
        "title: anchor fixture\n"
        "status: open\n"
        "touches:\n"
        "- work/slot/src\n"
        "---\n",
        encoding="utf-8",
    )
    local = home / ".project_manager" / "local.conf"
    local.write_text(
        "delegate_enabled=true\n"
        "delegate.developer.harness=codex\n"
        "delegate.developer.model=gpt-test\n",
        encoding="utf-8",
    )
    ledger = home / ".project_manager" / ".local" / "worktree-leases.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        json.dumps({"leases": [{"slot": "work/slot", "state": "leased"}]}),
        encoding="utf-8",
    )
    return home, worktree, ticket


def _unregistered_worktree(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "source"
    home.mkdir()
    _git(home, "init", "-q")
    _git(home, "config", "user.email", "test@example.invalid")
    _git(home, "config", "user.name", "test")
    (home / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(home, "add", "seed.txt")
    _git(home, "commit", "-qm", "seed")
    worktree = tmp_path / "snapshot"
    _git(home, "worktree", "add", "-q", "-b", "snapshot", str(worktree))
    return home, worktree


def test_delegate_config_anchor_follows_registered_worktree_from_both_shell_dirs(
    tmp_path, monkeypatch, capsys,
):
    home, worktree, _ticket = _managed_worktree(tmp_path)
    prompt = worktree / "prompt.md"
    prompt.write_text("implement the requested unit", encoding="utf-8")
    delegate = _load("pm_delegate_anchor_dirs")
    monkeypatch.setattr(delegate, "REPO", worktree)
    argv = [
        "--role", "developer", "--prompt-file", str(prompt),
        "--cwd", str(worktree), "--dry-run",
    ]

    observed = []
    for shell_dir in (home, worktree):
        monkeypatch.chdir(shell_dir)
        assert delegate.main(argv) == 0
        observed.append(capsys.readouterr().err.splitlines()[0])

    expected = str((home / ".project_manager" / "local.conf").resolve())
    assert observed[0] == observed[1]
    assert f"local_conf={expected}" in observed[0]


def test_delegate_anchor_oracle_is_sensitive_to_engine_repo_fallback(
    tmp_path, monkeypatch,
):
    _home, worktree, _ticket = _managed_worktree(tmp_path)
    delegate = _load("pm_delegate_anchor_sensitivity")
    monkeypatch.setattr(delegate, "REPO", worktree)
    delegate._CONFIG_REPO_OVERRIDE = worktree
    old_conf = delegate.local_config()
    with pytest.raises(delegate.DelegateError):
        delegate.resolve_delegate(old_conf, "developer", "normal", None, None, None)


def test_unregistered_worktree_allows_boardless_delegate_and_review(
    tmp_path, monkeypatch, capsys,
):
    _home, worktree = _unregistered_worktree(tmp_path)
    local = worktree / ".project_manager" / "local.conf"
    local.parent.mkdir()
    local.write_text(
        "delegate.developer.harness=codex\n"
        "delegate.developer.model=gpt-test\n",
        encoding="utf-8",
    )
    prompt = worktree / "prompt.md"
    prompt.write_text("implement the unit", encoding="utf-8")
    (worktree / "seed.txt").write_text("changed\n", encoding="utf-8")

    delegate = _load("pm_delegate_unregistered")
    monkeypatch.setattr(delegate, "REPO", worktree)
    assert delegate.main([
        "--role", "developer", "--prompt-file", str(prompt),
        "--cwd", str(worktree), "--dry-run",
    ]) == 0
    delegate_err = capsys.readouterr().err
    assert delegate_err.splitlines()[0].startswith("경고: PM 홈 해소 실패")
    assert "board가 필요 없는 실행" in delegate_err
    assert delegate_err.count("board가 필요 없는 실행") == 1
    assert f"pm_home={worktree.resolve()}" in delegate_err

    external = _load("external_review_unregistered")
    monkeypatch.setattr(external, "REPO", worktree)
    assert external.main(["--paths", "seed.txt", "--dry-run"]) == 0
    review_err = capsys.readouterr().err
    assert review_err.splitlines()[0].startswith("경고: PM 홈 해소 실패")
    assert "board가 필요 없는 실행" in review_err
    assert review_err.count("board가 필요 없는 실행") == 1
    assert f"diff_root={worktree.resolve()}" in review_err
    assert f"pm_home={worktree.resolve()}" in review_err


@pytest.mark.parametrize(
    ("extra_args", "expected_rc", "expected_error"),
    [
        (("--tier", "hard", "--dry-run"), 1, "hard 프로필 미설정"),
        ((), 3, "delegate 비활성"),
    ],
)
def test_unregistered_delegate_failure_flushes_anchor_warning_before_return(
    tmp_path, monkeypatch, capsys, extra_args, expected_rc, expected_error,
):
    _home, worktree = _unregistered_worktree(tmp_path)
    prompt = worktree / "prompt.md"
    prompt.write_text("implement the unit", encoding="utf-8")
    delegate = _load(f"pm_delegate_unregistered_failure_{expected_rc}")
    monkeypatch.setattr(delegate, "REPO", worktree)

    rc = delegate.main([
        "--role", "developer", "--prompt-file", str(prompt),
        "--cwd", str(worktree), *extra_args,
    ])

    assert rc == expected_rc
    err = capsys.readouterr().err
    assert err.splitlines()[0].startswith("경고: PM 홈 해소 실패")
    assert err.count("경고: PM 홈 해소 실패") == 1
    assert expected_error in err
    assert str(worktree / ".project_manager" / "local.conf") in err


def test_boardless_review_oracle_is_sensitive_to_unconditional_anchor_error(
    tmp_path, monkeypatch, capsys,
):
    _home, worktree = _unregistered_worktree(tmp_path)
    (worktree / "seed.txt").write_text("changed\n", encoding="utf-8")
    external = _load("external_review_anchor_sensitivity")
    monkeypatch.setattr(external, "REPO", worktree)

    def _old_half_fix(anchor, **kwargs):
        raise external.AnchorResolutionError("unconditional anchor error")

    monkeypatch.setattr(external, "resolve_pm_home_for_repo", _old_half_fix)
    assert external.main(["--paths", "seed.txt", "--dry-run"]) == 1
    assert "unconditional anchor error" in capsys.readouterr().err


def test_explicit_paths_ignore_missing_ticket_in_unregistered_worktree(
    tmp_path, monkeypatch, capsys,
):
    _home, worktree = _unregistered_worktree(tmp_path)
    (worktree / "seed.txt").write_text("changed\n", encoding="utf-8")
    external = _load("external_review_explicit_paths")
    monkeypatch.setattr(external, "REPO", worktree)

    missing = "T-" + "missing"
    assert external.main([
        "--ticket", missing, "--paths", "seed.txt", "--dry-run",
    ]) == 0
    assert "ticket board" not in capsys.readouterr().err

    assert external.main(["--ticket", missing, "--dry-run"]) == 1
    assert "앵커 해소 실패" in capsys.readouterr().err


def test_standalone_repo_uses_itself_for_both_tools(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "standalone"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "test")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "seed.txt")
    _git(repo, "commit", "-qm", "seed")
    local = repo / ".project_manager" / "local.conf"
    local.parent.mkdir()
    local.write_text(
        "delegate.developer.harness=codex\n"
        "delegate.developer.model=gpt-test\n",
        encoding="utf-8",
    )
    prompt = repo / "prompt.md"
    prompt.write_text("implement the unit", encoding="utf-8")
    (repo / "seed.txt").write_text("changed\n", encoding="utf-8")

    delegate = _load("pm_delegate_standalone")
    monkeypatch.setattr(delegate, "REPO", repo)
    assert delegate.main([
        "--role", "developer", "--prompt-file", str(prompt),
        "--cwd", str(repo), "--dry-run",
    ]) == 0
    assert f"pm_home={repo.resolve()}" in capsys.readouterr().err

    external = _load("external_review_standalone")
    monkeypatch.setattr(external, "REPO", repo)
    assert external.main(["--paths", "seed.txt", "--dry-run"]) == 0
    err = capsys.readouterr().err
    assert f"diff_root={repo.resolve()}" in err
    assert f"pm_home={repo.resolve()}" in err


def test_delegate_and_review_failure_sets_match_across_three_repo_shapes(
    tmp_path, monkeypatch, capsys,
):
    managed_home, managed, _ticket = _managed_worktree(tmp_path / "managed")
    (tmp_path / "unregistered").mkdir()
    _source, unregistered = _unregistered_worktree(tmp_path / "unregistered")
    plain = tmp_path / "plain"
    plain.mkdir()
    _git(plain, "init", "-q")
    _git(plain, "config", "user.email", "test@example.invalid")
    _git(plain, "config", "user.name", "test")
    (plain / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(plain, "add", "seed.txt")
    _git(plain, "commit", "-qm", "seed")

    shapes = {
        "registered-slot": (managed, managed_home),
        "unregistered-worktree": (unregistered, unregistered),
        "plain-clone": (plain, plain),
    }
    failure_sets = {}
    for index, (shape, (repo, config_home)) in enumerate(shapes.items()):
        local = config_home / ".project_manager" / "local.conf"
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_text(
            "delegate.developer.harness=codex\n"
            "delegate.developer.model=gpt-test\n",
            encoding="utf-8",
        )
        prompt = repo / "prompt.md"
        prompt.write_text("implement the unit", encoding="utf-8")
        (repo / "seed.txt").write_text(f"changed-{shape}\n", encoding="utf-8")

        delegate = _load(f"pm_delegate_shape_{index}")
        monkeypatch.setattr(delegate, "REPO", repo)
        review = _load(f"external_review_shape_{index}")
        monkeypatch.setattr(review, "REPO", repo)
        results = {
            "delegate-normal": delegate.main([
                "--role", "developer", "--prompt-file", str(prompt),
                "--cwd", str(repo), "--dry-run",
            ]),
            "delegate-hard": delegate.main([
                "--role", "developer", "--tier", "hard",
                "--prompt-file", str(prompt), "--cwd", str(repo), "--dry-run",
            ]),
            "review-changed": review.main([
                "--paths", "seed.txt", "--dry-run",
            ]),
            "review-empty": review.main([
                "--paths", "missing.txt", "--dry-run",
            ]),
        }
        failure_sets[shape] = {name for name, rc in results.items() if rc != 0}
        capsys.readouterr()

    assert failure_sets == {
        shape: {"delegate-hard", "review-empty"} for shape in shapes
    }


def test_delegate_raw_storage_uses_resolved_pm_home_owner(tmp_path, monkeypatch):
    home, worktree, _ticket = _managed_worktree(tmp_path)
    delegate = _load("pm_delegate_raw_owner")
    monkeypatch.setattr(delegate, "REPO", worktree)
    delegate._CONFIG_REPO_OVERRIDE = home

    raw_dir, ledger = delegate._raw_storage()
    assert raw_dir == home / ".project_manager" / ".local" / "delegate"
    assert ledger == home / ".project_manager" / ".local" / "raw_outputs.json"


def test_delegate_anchor_handles_symlink_standalone_and_missing_inputs(
    tmp_path, monkeypatch, capsys,
):
    home, worktree, _ticket = _managed_worktree(tmp_path)
    delegate = _load("pm_delegate_anchor_inputs")
    monkeypatch.setattr(delegate, "REPO", worktree)
    link = tmp_path / "worktree-link"
    link.symlink_to(worktree, target_is_directory=True)
    assert delegate._load_external_review().resolve_pm_home_for_repo(link) == home.resolve()

    standalone = tmp_path / "standalone"
    standalone.mkdir()
    _git(standalone, "init", "-q")
    assert delegate._load_external_review().resolve_pm_home_for_repo(standalone) == standalone

    prompt = worktree / "prompt.md"
    prompt.write_text("safe task", encoding="utf-8")
    missing = tmp_path / "missing"
    rc = delegate.main([
        "--role", "developer", "--prompt-file", str(prompt),
        "--cwd", str(missing), "--dry-run",
    ])
    assert rc == 1 and "git 저장소" in capsys.readouterr().err


def test_duplicate_pm_home_registration_is_rejected(tmp_path):
    outer = tmp_path / "outer"
    inner = outer / "inner"
    inner.mkdir(parents=True)
    _git(inner, "init", "-q")
    _git(inner, "config", "user.email", "test@example.invalid")
    _git(inner, "config", "user.name", "test")
    (inner / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(inner, "add", "seed.txt")
    _git(inner, "commit", "-qm", "seed")
    worktree = inner / "work" / "slot"
    worktree.parent.mkdir()
    _git(inner, "worktree", "add", "-q", "-b", "duplicate-slot", str(worktree))
    ticket = "T-" + "9002"
    for home in (outer, inner):
        tickets = home / ".project_manager" / "wiki" / "tickets" / "open"
        tickets.mkdir(parents=True, exist_ok=True)
        (tickets / f"{ticket}-fixture.md").write_text(
            f"---\nid: {ticket}\ntitle: fixture\nstatus: open\ntouches: []\n---\n",
            encoding="utf-8",
        )
        ledger = home / ".project_manager" / ".local" / "worktree-leases.json"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text(
            json.dumps({"leases": [{"slot": str(worktree), "state": "leased"}]}),
            encoding="utf-8",
        )

    external = _load("external_review_duplicate_owner")
    assert external.resolve_pm_home_for_repo(worktree) == worktree.resolve()
    with pytest.raises(external.AnchorResolutionError, match="모호"):
        external.resolve_pm_home_for_repo(worktree, required=True)


def test_registered_worktree_owner_does_not_require_existing_ticket(tmp_path):
    home, worktree, _ticket = _managed_worktree(tmp_path)
    ticket_root = home / ".project_manager" / "wiki" / "tickets"
    for ticket_file in (ticket_root / "open").glob("*.md"):
        ticket_file.unlink()

    external = _load("external_review_empty_board_owner")
    assert external.resolve_pm_home_for_repo(worktree, required=True) == home.resolve()


def test_tools_only_checkout_cannot_override_unique_board_lease_owner(tmp_path):
    home = tmp_path / "pm"
    source = home / ".project_manager" / "tools" / "source"
    source.mkdir(parents=True)
    _git(source, "init", "-q")
    _git(source, "config", "user.email", "test@example.invalid")
    _git(source, "config", "user.name", "test")
    (source / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(source, "add", "seed.txt")
    _git(source, "commit", "-qm", "seed")
    # tools-only checkout 마커: 실 ticket board는 의도적으로 없다.
    (source / ".project_manager" / "tools").mkdir(parents=True)
    worktree = tmp_path / "slot"
    _git(source, "worktree", "add", "-q", "-b", "nested-slot", str(worktree))

    tickets = home / ".project_manager" / "wiki" / "tickets" / "open"
    tickets.mkdir(parents=True)
    (tickets / "T-fixture.md").write_text(
        "---\nid: T-fixture\ntitle: fixture\nstatus: open\n---\n",
        encoding="utf-8",
    )
    ledger = home / ".project_manager" / ".local" / "worktree-leases.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        json.dumps({"leases": [{"slot": str(worktree), "state": "leased"}]}),
        encoding="utf-8",
    )

    external = _load("external_review_tools_only_ancestor")
    assert external.resolve_pm_home_for_repo(worktree, required=True) == home.resolve()


def test_non_ticket_absolute_paths_bypass_corrupt_lease_ledger(
    tmp_path, monkeypatch, capsys,
):
    home, worktree, _ticket = _managed_worktree(tmp_path)
    source = worktree / "seed.txt"
    source.write_text("changed\n", encoding="utf-8")
    ledger = home / ".project_manager" / ".local" / "worktree-leases.json"
    ledger.write_text("{broken", encoding="utf-8")
    external = _load("external_review_absolute_recovery")
    monkeypatch.setattr(external, "REPO", home)

    assert external.main(["--paths", str(source), "--dry-run"]) == 0
    assert f"diff_root={worktree.resolve()}" in capsys.readouterr().err


def test_external_main_restores_selector_globals_between_calls(
    tmp_path, monkeypatch, capsys,
):
    repos = []
    for name in ("engine", "absolute"):
        repo = tmp_path / name
        repo.mkdir()
        _git(repo, "init", "-q")
        _git(repo, "config", "user.email", "test@example.invalid")
        _git(repo, "config", "user.name", "test")
        (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
        _git(repo, "add", "seed.txt")
        _git(repo, "commit", "-qm", "seed")
        (repo / "seed.txt").write_text(f"changed-{name}\n", encoding="utf-8")
        repos.append(repo)
    engine, absolute = repos
    external = _load("external_review_global_restore")
    original = (
        engine,
        engine / ".project_manager" / "local.conf",
        engine / ".project_manager" / "review_context.local.md",
        engine / ".project_manager" / "wiki" / "tickets",
    )
    monkeypatch.setattr(external, "REPO", original[0])
    monkeypatch.setattr(external, "LOCAL_CONF", original[1])
    monkeypatch.setattr(external, "REVIEW_CONTEXT_FILE", original[2])
    monkeypatch.setattr(external, "TICKETS_DIR", original[3])
    monkeypatch.chdir(engine)

    assert external.main([
        "--paths", str(absolute / "seed.txt"), "--dry-run",
    ]) == 0
    first = capsys.readouterr().err
    assert f"diff_root={absolute.resolve()}" in first
    assert (
        external.REPO, external.LOCAL_CONF,
        external.REVIEW_CONTEXT_FILE, external.TICKETS_DIR,
    ) == original

    assert external.main(["--paths", ".", "--dry-run"]) == 0
    second = capsys.readouterr().err
    assert f"diff_root={engine.resolve()}" in second


def test_external_public_annotations_resolve_at_runtime():
    external = _load("external_review_type_hints")
    assert typing.get_type_hints(external._resolve_diff_root)["paths"] == typing.Sequence[str]


def test_external_ticket_uses_board_home_and_worktree_diff_from_both_shell_dirs(
    tmp_path, monkeypatch, capsys,
):
    home, worktree, ticket = _managed_worktree(tmp_path)
    source = worktree / "src" / "module.py"
    source.parent.mkdir()
    source.write_text("value = 1\n", encoding="utf-8")
    _git(worktree, "add", "src/module.py")
    _git(worktree, "config", "user.email", "test@example.invalid")
    _git(worktree, "config", "user.name", "test")
    _git(worktree, "commit", "-qm", "source")
    source.write_text("value = 2\n", encoding="utf-8")

    outputs = []
    for index, shell_dir in enumerate((home, worktree)):
        external = _load(f"external_review_dual_anchor_{index}")
        # 엔진 사본(PM 홈)과 실제 diff worktree를 갈라 REPO=diff_root 주입을 판별한다.
        monkeypatch.setattr(external, "REPO", home)
        monkeypatch.chdir(shell_dir)
        assert external.main(["--ticket", ticket, "--dry-run"]) == 0
        captured = capsys.readouterr()
        outputs.append(tuple(
            line for line in captured.err.splitlines()
            if line.startswith(("검토 경로:", "base:"))
        ))

    assert outputs[0] == outputs[1] == (("검토 경로: ['src']"), "base: HEAD")


def test_ticket_resolution_failure_never_calls_diff(tmp_path, monkeypatch, capsys):
    _home, worktree, _ticket = _managed_worktree(tmp_path)
    external = _load("external_review_missing_ticket")
    monkeypatch.setattr(external, "REPO", worktree)
    monkeypatch.setattr(
        external, "extract_diff",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("diff must not run")),
    )
    missing_ticket = "T-" + "9998"
    assert external.main(["--ticket", missing_ticket, "--dry-run"]) == 1
    assert "앵커 해소 실패" in capsys.readouterr().err


def test_empty_inline_touches_fails_before_diff(tmp_path, monkeypatch, capsys):
    home, worktree, _ticket = _managed_worktree(tmp_path)
    empty_ticket = "T-" + "empty"
    ticket_file = home / ".project_manager" / "wiki" / "tickets" / "open" / f"{empty_ticket}.md"
    ticket_file.write_text(
        f"---\nid: {empty_ticket}\ntitle: empty\nstatus: open\ntouches: []\n---\n",
        encoding="utf-8",
    )
    external = _load("external_review_empty_touches")
    monkeypatch.setattr(external, "REPO", worktree)
    monkeypatch.setattr(
        external, "extract_diff",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("diff must not run")),
    )

    assert external.main(["--ticket", empty_ticket, "--dry-run"]) == 1
    err = capsys.readouterr().err
    assert "touches가 비어" in err
    assert f"board anchor {home.resolve()}" in err
    assert "['[]']" not in err


def test_repository_root_is_a_valid_review_pathspec(tmp_path):
    home, worktree, _ticket = _managed_worktree(tmp_path)
    external = _load("external_review_root_pathspec")

    assert external._normalize_review_paths(
        ["."], diff_root=worktree, pm_home=worktree, ticket_selected=False,
    ) == (".",)
    assert external._normalize_review_paths(
        [str(worktree)], diff_root=worktree, pm_home=worktree, ticket_selected=False,
    ) == (".",)
    assert external._normalize_review_paths(
        ["work/slot"], diff_root=worktree, pm_home=home, ticket_selected=True,
    ) == (".",)


def test_ticket_touching_other_registered_slot_names_anchor_mismatch(tmp_path):
    home, first, _ticket = _managed_worktree(tmp_path)
    second = home / "work" / "second"
    _git(home, "worktree", "add", "-q", "-b", "second-slot", str(second))
    ledger = home / ".project_manager" / ".local" / "worktree-leases.json"
    ledger.write_text(
        json.dumps({"leases": [
            {"slot": "work/slot", "state": "leased"},
            {"slot": "work/second", "state": "leased"},
        ]}),
        encoding="utf-8",
    )
    external = _load("external_review_other_slot")

    with pytest.raises(
        external.AnchorResolutionError,
        match="ticket touches가 실행 엔진 worktree와 다른 등록 worktree",
    ):
        external._resolve_diff_root(
            second,
            pm_home=home,
            paths=["work/slot/src"],
            base="HEAD",
            ticket_selected=True,
        )
    assert first.resolve() != second.resolve()


def test_configured_review_paths_selects_same_changed_slot_used_for_diff(
    tmp_path, monkeypatch, capsys,
):
    home = tmp_path / "pm"
    home.mkdir()
    _git(home, "init", "-q")
    _git(home, "config", "user.email", "test@example.invalid")
    _git(home, "config", "user.name", "test")
    source = home / "src" / "module.py"
    source.parent.mkdir()
    source.write_text("value = 1\n", encoding="utf-8")
    _git(home, "add", "src/module.py")
    _git(home, "commit", "-qm", "source")
    first = home / "work" / "first"
    second = home / "work" / "second"
    first.parent.mkdir()
    _git(home, "worktree", "add", "-q", "-b", "first-slot", str(first))
    _git(home, "worktree", "add", "-q", "-b", "second-slot", str(second))
    (second / "src" / "module.py").write_text("value = 2\n", encoding="utf-8")

    ticket = "T-" + "board"
    ticket_dir = home / ".project_manager" / "wiki" / "tickets" / "open"
    ticket_dir.mkdir(parents=True)
    (ticket_dir / f"{ticket}.md").write_text(
        f"---\nid: {ticket}\ntitle: board\nstatus: open\ntouches: []\n---\n",
        encoding="utf-8",
    )
    local = home / ".project_manager" / "local.conf"
    local.write_text("review_paths=src/module.py\n", encoding="utf-8")
    ledger = home / ".project_manager" / ".local" / "worktree-leases.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        json.dumps({"leases": [
            {"slot": "work/first", "state": "leased"},
            {"slot": "work/second", "state": "leased"},
        ]}),
        encoding="utf-8",
    )

    external = _load("external_review_config_selector")
    monkeypatch.setattr(external, "REPO", home)
    assert external.main(["--dry-run"]) == 0
    err = capsys.readouterr().err
    assert f"diff_root={second.resolve()}" in err
    assert "검토 경로: ['src/module.py']" in err


def test_delegate_uses_owner_for_containment_and_rejects_other_engine_board(
    tmp_path, monkeypatch, capsys,
):
    home, worktree, ticket = _managed_worktree(tmp_path / "owner")
    prompt = home / ".project_manager" / "task.md"
    prompt.write_text("implement the unit", encoding="utf-8")
    delegate = _load("pm_delegate_owner_containment")
    monkeypatch.setattr(delegate, "REPO", worktree)
    assert delegate.main([
        "--role", "developer", "--prompt-file", str(prompt),
        "--cwd", str(worktree), "--dry-run",
    ]) == 0
    assert f"pm_home={home.resolve()}" in capsys.readouterr().err

    other_home, _other_worktree, _other_ticket = _managed_worktree(tmp_path / "engine")
    delegate = _load("pm_delegate_board_owner_mismatch")
    monkeypatch.setattr(delegate, "REPO", other_home)
    assert delegate.main([
        "--role", "developer", "--prompt-file", str(prompt),
        "--cwd", str(worktree), "--ticket", ticket, "--dry-run",
    ]) == 1
    assert "실행 엔진 board와 --cwd 소유 PM 홈이 다릅니다" in capsys.readouterr().err


def test_delegate_divergence_names_resolved_pm_home_as_profile_source(
    tmp_path, monkeypatch, capsys,
):
    home, worktree, _ticket = _managed_worktree(tmp_path)
    local = worktree / ".project_manager" / "local.conf"
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text(
        "delegate.developer.harness=codex\n"
        "delegate.developer.model=gpt-other\n",
        encoding="utf-8",
    )
    prompt = worktree / "prompt.md"
    prompt.write_text("implement the unit", encoding="utf-8")
    delegate = _load("pm_delegate_divergence_labels")
    monkeypatch.setattr(delegate, "REPO", worktree)

    assert delegate.main([
        "--role", "developer", "--prompt-file", str(prompt),
        "--cwd", str(worktree), "--dry-run",
    ]) == 0
    err = capsys.readouterr().err
    assert "해소된 PM 홈 REPO" in err
    assert "pm-home='gpt-test', cwd-worktree='gpt-other'" in err
    assert "--cwd의 lease 소유자로 해소된 PM 홈 conf가 적용됩니다" in err
    assert "의도한 local.conf를 가진 엔진 사본을 실행하세요" not in err


def test_comma_guidance_requires_comma_and_missing_element(tmp_path):
    external = _load("external_review_comma_guidance")
    (tmp_path / "exists.py").write_text("", encoding="utf-8")
    (tmp_path / "valid,name.py").write_text("", encoding="utf-8")

    (tmp_path / "second.py").write_text("", encoding="utf-8")
    suspicious = external._empty_diff_guidance(["exists.py,second.py"], root=tmp_path)
    ordinary = external._empty_diff_guidance(["missing.py"], root=tmp_path)
    legitimate = external._empty_diff_guidance(["valid,name.py"], root=tmp_path)

    assert "공백 구분" in suspicious
    assert "공백 구분" not in ordinary
    assert "공백 구분" not in legitimate


def test_template_scope_expansion_discovers_every_real_harness(tmp_path):
    delegate = _load("pm_delegate_template_scope")
    expected = {"alpha", "beta", "future"}
    for harness in expected:
        (tmp_path / "templates" / harness / ".project_manager" / "tools").mkdir(
            parents=True,
        )
    (tmp_path / "templates" / "not-a-harness").mkdir(parents=True)

    expanded = delegate._with_template_propagation(
        [".project_manager/tools/pm_delegate.py"], workspace=tmp_path,
    )
    derived = {
        Path(path).parts[1] for path in expanded if path.startswith("templates/")
    }
    assert derived == expected
    assert "templates/future/.project_manager/tools/pm_delegate.py" in expanded

    template_only = delegate._with_template_propagation(
        ["templates/alpha/.project_manager/tools/only.py"], workspace=tmp_path,
    )
    assert template_only == ("templates/alpha/.project_manager/tools/only.py",)


def test_unfinished_round_record_is_counted_separately():
    external = _load("external_review_unfinished_round")
    entry = {
        "count": 3,
        "acked_through": 0,
        "records": [
            {"number": 1, "finished_at": "done", "verdict": True},
            {"number": 2, "finished_at": "done", "verdict": False},
            {"number": 3},
        ],
    }
    assert external._unacked_round_counts(entry) == (3, 1, 2)


def _cross_owned_slot(root):
    """A 장부가 B 소유 슬롯을 등록한 교차 소유 형상.

    두 PM 홈이 서로 다른 review_paths 를 선언하고, A 의 lease 장부가 B 의
    worktree 를 슬롯으로 등록한다. 인자 없는 실행에서 A 의 review_paths 로
    diff_root 를 고르면 그 소유자는 B 라, 표시된 config provenance 와 실제
    전송 범위가 갈린다.
    """
    home_a = root / "home-a"
    home_b = root / "home-b"
    for home, declared in ((home_a, "src/a.py"), (home_b, "src/b.py")):
        home.mkdir(parents=True)
        _git(home, "init", "-q")
        _git(home, "config", "user.email", "test@example.invalid")
        _git(home, "config", "user.name", "test")
        for rel in ("src/a.py", "src/b.py"):
            target = home / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("value = 1\n", encoding="utf-8")
            _git(home, "add", rel)
        _git(home, "commit", "-qm", "seed")
        conf = home / ".project_manager" / "local.conf"
        conf.parent.mkdir(parents=True, exist_ok=True)
        conf.write_text(f"review_paths={declared}\n", encoding="utf-8")

    slot = home_b / "work" / "slot"
    slot.parent.mkdir(parents=True, exist_ok=True)
    _git(home_b, "worktree", "add", "-q", "-b", "slot-branch", str(slot))
    (slot / "src" / "a.py").write_text("value = 2\n", encoding="utf-8")

    for home in (home_a, home_b):
        ledger = home / ".project_manager" / ".local" / "worktree-leases.json"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text(
            json.dumps({"leases": [{"slot": str(slot), "state": "leased"}]}),
            encoding="utf-8",
        )
    return home_a, home_b, slot


def test_config_paths_from_other_pm_home_block_before_external_send(
    tmp_path, monkeypatch, capsys,
):
    home_a, home_b, _slot = _cross_owned_slot(tmp_path / "cross")
    external = _load("external_review_cross_owned_conf")
    monkeypatch.setattr(external, "REPO", home_a)

    assert external.main(["--dry-run"]) == 1
    err = capsys.readouterr().err
    assert str(home_a.resolve()) in err
    assert str(home_b.resolve()) in err


def test_explicit_paths_escape_cross_owned_conf_block(
    tmp_path, monkeypatch, capsys,
):
    home_a, _home_b, slot = _cross_owned_slot(tmp_path / "escape")
    external = _load("external_review_cross_owned_escape")
    monkeypatch.setattr(external, "REPO", home_a)

    assert external.main(["--dry-run", "--paths", str(slot / "src" / "a.py")]) == 0
    capsys.readouterr()
