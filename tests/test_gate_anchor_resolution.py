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


def _git_out(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=path, check=True, capture_output=True, text=True,
    ).stdout.strip()


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


def _dual_slot_home(root: Path) -> tuple[Path, Path, Path]:
    """등록 슬롯 2개를 가진 PM 홈 — conf review_paths 로 슬롯을 고르는 형상.

    두 슬롯 모두 깨끗한 상태로 돌려주므로, 각 테스트가 필요한 변경(작업트리/커밋)을 직접 만든다.
    반환: (PM 홈, 첫 슬롯, 둘째 슬롯).
    """
    home = root
    home.mkdir(parents=True)
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
    return home, first, second


def _commit_in_slot(slot: Path, text: str) -> None:
    """슬롯에 **커밋만 된** 변경 1건을 만든다(작업트리는 깨끗)."""
    (slot / "src" / "module.py").write_text(text, encoding="utf-8")
    _git(slot, "add", "src/module.py")
    _git(slot, "commit", "-qm", "committed change")


def test_configured_review_paths_selects_same_changed_slot_used_for_diff(
    tmp_path, monkeypatch, capsys,
):
    home, _first, second = _dual_slot_home(tmp_path / "pm")
    (second / "src" / "module.py").write_text("value = 2\n", encoding="utf-8")

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


def _cross_owned_slot(root, *, declare_review_paths: bool = True):
    """A 장부가 B 소유 슬롯을 등록한 교차 소유 형상.

    두 PM 홈이 서로 다른 review_paths 를 선언하고, A 의 lease 장부가 B 의
    worktree 를 슬롯으로 등록한다. 인자 없는 실행에서 A 의 review_paths 로
    diff_root 를 고르면 그 소유자는 B 라, 표시된 config provenance 와 실제
    전송 범위가 갈린다.

    `declare_review_paths=False` 면 두 conf 모두 review_paths 를 선언하지 않아 범위가 엔진 고정
    기본 경로로 떨어진다 — 범위 출처가 최초 PM 홈이라는 사실은 같은 형상이다.
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
        conf.write_text(
            f"review_paths={declared}\n" if declare_review_paths
            else "# review_paths 미선언 — 엔진 고정 기본 경로로 떨어진다\n",
            encoding="utf-8",
        )

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


# ── 강등 실행의 소유 PM 홈 필터 승계 (송신 방향) ────────────────────────────


def _demoted_worktree_with_owner_filters(tmp_path) -> tuple[Path, Path]:
    """lease 장부 손상으로 config 소유자가 슬롯으로 강등되는 형상 + PM 홈 전용 필터 선언.

    PM 홈만 `review_paths=src` 와 `review_denylist_extra=*.vault` 를 선언하고, 슬롯에는 conf 가
    없다. 승계가 없으면 이 실행은 엔진 기본 경로로 `src/keys.vault` 까지 필터 없이 내보낸다.
    """
    home, worktree, _ticket = _managed_worktree(tmp_path)
    conf = home / ".project_manager" / "local.conf"
    conf.write_text(
        conf.read_text(encoding="utf-8")
        + "review_paths=src\n"
        + "review_denylist_extra=*.vault\n",
        encoding="utf-8",
    )
    _git(worktree, "config", "user.email", "test@example.invalid")
    _git(worktree, "config", "user.name", "test")
    source = worktree / "src" / "module.py"
    source.parent.mkdir()
    source.write_text("value = 1\n", encoding="utf-8")
    secret = worktree / "src" / "keys.vault"
    secret.write_text("token = 1\n", encoding="utf-8")
    _git(worktree, "add", "src/module.py", "src/keys.vault")
    _git(worktree, "commit", "-qm", "source")
    source.write_text("value = 2\n", encoding="utf-8")
    secret.write_text("token = 2\n", encoding="utf-8")
    (home / ".project_manager" / ".local" / "worktree-leases.json").write_text(
        "{broken", encoding="utf-8",
    )
    return home, worktree


def test_demoted_conf_owner_inherits_owner_pm_home_review_filters(
    tmp_path, monkeypatch, capsys,
):
    """강등 실행도 소유 PM 홈의 denylist/review_paths 를 승계해 필터가 좁아지지 않는다."""
    home, worktree = _demoted_worktree_with_owner_filters(tmp_path)
    external = _load("external_review_demoted_inherit")
    monkeypatch.setattr(external, "REPO", worktree)

    assert external.main(["--dry-run"]) == 0
    err = capsys.readouterr().err
    assert "소유 PM 홈 유효 필터를 승계했습니다" in err
    assert str(home.resolve()) in err
    assert "검토 경로: ['src']" in err
    assert "'*.vault' 매칭" in err


def test_demoted_owner_without_unique_candidate_blocks_before_external_send(
    tmp_path, monkeypatch, capsys,
):
    """소유 PM 홈을 되찾을 수 없는 강등은 diff 추출 전에 차단한다 — 무필터 송신 0."""
    _home, worktree = _unregistered_worktree(tmp_path)
    (worktree / "seed.txt").write_text("changed\n", encoding="utf-8")
    external = _load("external_review_demoted_block")
    monkeypatch.setattr(external, "REPO", worktree)
    monkeypatch.setattr(
        external, "extract_diff",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("diff must not run"),
        ),
    )
    monkeypatch.setattr(
        external, "run_review",
        lambda *args, **kwargs: pytest.fail("외부 송신 전에 차단해야 한다"),
    )

    assert external.main(["--dry-run"]) == 1
    err = capsys.readouterr().err
    assert "시크릿 필터가 좁아진" in err
    assert "전송 전에 중단합니다" in err
    assert str(worktree.resolve()) in err


def test_explicit_paths_escape_demoted_owner_filter_block(
    tmp_path, monkeypatch, capsys,
):
    """명시 --paths 는 강등 차단의 탈출구로 남는다(복구 채널 자기잠김 금지)."""
    _home, worktree = _unregistered_worktree(tmp_path)
    (worktree / "seed.txt").write_text("changed\n", encoding="utf-8")
    external = _load("external_review_demoted_escape")
    monkeypatch.setattr(external, "REPO", worktree)

    assert external.main(["--paths", "seed.txt", "--dry-run"]) == 0
    err = capsys.readouterr().err
    assert "소유 PM 홈 필터를 승계하지 못했습니다" in err
    assert "명시 --paths 범위로 계속" in err


def test_ambiguous_owner_candidates_block_without_paths_and_warn_with_paths(
    tmp_path, capsys,
):
    """후보 2+ 행: 어느 PM 홈이 지배하는지 확정 못 하므로 차단, 명시 --paths 만 탈출구."""
    external = _load("external_review_owner_filter_matrix")
    homes = (tmp_path / "home-a", tmp_path / "home-b")
    for home in homes:
        (home / ".project_manager").mkdir(parents=True)
        (home / ".project_manager" / "local.conf").write_text(
            "review_denylist_extra=*.vault\n", encoding="utf-8",
        )
    demotion = external.PmHomeDemotion(tmp_path / "slot", "중복 등록", tuple(homes))

    with pytest.raises(
        external.AnchorResolutionError, match="하나로 좁히지 못했습니다",
    ):
        external._conf_with_owner_filters({}, [demotion], explicit_paths=False)

    kept = external._conf_with_owner_filters(
        {"review_paths": "."}, [demotion], explicit_paths=True,
    )
    assert kept == {"review_paths": "."}
    assert "승계하지 못했습니다" in capsys.readouterr().err


def _demoted_worktree_with_owner_default_scope(tmp_path) -> tuple[Path, Path]:
    """소유 PM 홈은 review_paths 미선언(=엔진 기본 경로)이고 슬롯만 `.` 를 선언한 강등 형상.

    슬롯 선언이 살아남으면 lease 손상만으로 송신 범위가 소유 유효 범위보다 넓어진다 — 기본 경로
    밖 파일(`docs/notes.md`)의 포함 여부가 그 판별자다.
    """
    home, worktree, _ticket = _managed_worktree(tmp_path)
    slot_conf = worktree / ".project_manager" / "local.conf"
    slot_conf.write_text("review_paths=.\n", encoding="utf-8")
    _git(worktree, "config", "user.email", "test@example.invalid")
    _git(worktree, "config", "user.name", "test")
    source = worktree / "src" / "module.py"
    source.parent.mkdir()
    source.write_text("value = 1\n", encoding="utf-8")
    note = worktree / "docs" / "notes.md"
    note.parent.mkdir()
    note.write_text("draft\n", encoding="utf-8")
    _git(worktree, "add", "src/module.py", "docs/notes.md")
    _git(worktree, "commit", "-qm", "source")
    source.write_text("value = 2\n", encoding="utf-8")
    note.write_text("draft 2\n", encoding="utf-8")
    (home / ".project_manager" / ".local" / "worktree-leases.json").write_text(
        "{broken", encoding="utf-8",
    )
    return home, worktree


def test_demoted_run_inherits_owner_default_scope_over_slot_declaration(
    tmp_path, monkeypatch, capsys,
):
    """소유 PM 홈이 미선언이면 그 **유효 범위**(엔진 기본 경로)를 승계한다 — 슬롯 `.` 는 진다."""
    _home, worktree = _demoted_worktree_with_owner_default_scope(tmp_path)
    external = _load("external_review_owner_default_scope")
    monkeypatch.setattr(external, "REPO", worktree)

    assert external.main(["--dry-run"]) == 0
    captured = capsys.readouterr()
    assert "검토 경로: ['src', 'tests', 'scripts', '.project_manager/tools']" in captured.err
    assert "src/module.py" in captured.out
    assert "docs/notes.md" not in captured.out


def test_owner_scope_oracle_is_sensitive_to_surviving_slot_declaration(
    tmp_path, monkeypatch, capsys,
):
    """소유 미선언 시 슬롯 선언을 남기던 옛 병합으로 되돌리면 송신 범위가 `.` 로 넓어진다."""
    _home, worktree = _demoted_worktree_with_owner_default_scope(tmp_path)
    external = _load("external_review_owner_scope_sensitivity")
    monkeypatch.setattr(external, "REPO", worktree)

    def _legacy_merge(conf, owner_filters):
        merged = dict(conf)
        owner_paths = owner_filters.get("review_paths", "").strip()
        if owner_paths:
            merged["review_paths"] = owner_paths
        return merged

    monkeypatch.setattr(external, "_merged_owner_filters", _legacy_merge)
    assert external.main(["--dry-run"]) == 0
    captured = capsys.readouterr()
    assert "검토 경로: ['.']" in captured.err
    assert "docs/notes.md" in captured.out


def test_owner_conf_read_failure_blocks_with_and_without_explicit_paths(
    tmp_path, monkeypatch, capsys,
):
    """소유 conf 를 못 읽으면 어떤 인자에서도 차단한다 — --paths 는 확인 못 한 denylist 를 대체 못 한다."""
    home, worktree = _demoted_worktree_with_owner_filters(tmp_path)
    (home / ".project_manager" / "local.conf").write_bytes(b"\xff\xfe\x00")
    external = _load("external_review_owner_conf_unreadable")
    monkeypatch.setattr(external, "REPO", worktree)
    monkeypatch.setattr(
        external, "extract_diff",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("diff must not run"),
        ),
    )

    for argv in (["--dry-run"], ["--paths", "src/module.py", "--dry-run"]):
        assert external.main(argv) == 1
        err = capsys.readouterr().err
        assert "소유 PM 홈 conf 를 읽지 못했습니다" in err
        assert "명시 --paths 로 대체되지 않으므로" in err


def _cross_repo_absolute_target(
    tmp_path, *, break_target_owner: bool,
) -> tuple[Path, Path, Path, Path]:
    """엔진 A = 자기 conf 와 소유 conf 가 모두 깨진 PM 홈의 슬롯 · 절대 `--paths` 대상 B = 다른 슬롯.

    A 컨텍스트는 (1) 강등 + 소유 PM 홈 conf 판독 불가, (2) **엔진 자기 슬롯 conf 자체도 판독 불가**라
    선택 전 config 를 읽거나 검사하는 순간 중단된다. B 소유 PM 홈은 정상이고 `*.b-vault` 를
    선언한다. `break_target_owner=True` 면 B 쪽 소유 관계까지 깨뜨려 **선택된 소유자** 기준
    fail-closed 를 본다.
    """
    home_a, slot_a, _ticket_a = _managed_worktree(tmp_path / "a")
    (home_a / ".project_manager" / "local.conf").write_bytes(b"\xff\xfe\x00")
    (home_a / ".project_manager" / ".local" / "worktree-leases.json").write_text(
        "{broken", encoding="utf-8",
    )
    (slot_a / ".project_manager" / "local.conf").write_bytes(b"\xff\xfe\x00")

    home_b, slot_b, _ticket_b = _managed_worktree(tmp_path / "b")
    conf_b = home_b / ".project_manager" / "local.conf"
    conf_b.write_text("review_denylist_extra=*.b-vault\n", encoding="utf-8")
    _git(slot_b, "config", "user.email", "test@example.invalid")
    _git(slot_b, "config", "user.name", "test")
    source = slot_b / "src" / "module.py"
    source.parent.mkdir()
    source.write_text("value = 1\n", encoding="utf-8")
    secret = slot_b / "src" / "keys.b-vault"
    secret.write_text("token = 1\n", encoding="utf-8")
    _git(slot_b, "add", "src/module.py", "src/keys.b-vault")
    _git(slot_b, "commit", "-qm", "source")
    source.write_text("value = 2\n", encoding="utf-8")
    secret.write_text("token = 2\n", encoding="utf-8")
    if break_target_owner:
        conf_b.write_bytes(b"\xff\xfe\x00")
        (home_b / ".project_manager" / ".local" / "worktree-leases.json").write_text(
            "{broken", encoding="utf-8",
        )
    return home_a, slot_a, home_b, slot_b


def test_absolute_paths_to_other_repo_use_selected_owner_not_engine_context(
    tmp_path, monkeypatch, capsys,
):
    """초기 엔진 컨텍스트가 강등·손상이어도 다른 repo를 가리키는 절대 --paths 는 자기잠기지 않는다."""
    home_a, slot_a, home_b, slot_b = _cross_repo_absolute_target(
        tmp_path, break_target_owner=False,
    )
    external = _load("external_review_cross_repo_absolute")
    monkeypatch.setattr(external, "REPO", slot_a)

    # A 컨텍스트는 실제로 차단 사유를 갖는다 — 옛 순서(엔진 먼저 검사)라면 여기서 막혔다.
    with pytest.raises(external.OwnerFilterConfError):
        external._owner_filter_conf(
            external.PmHomeDemotion(slot_a, "lease 손상", (home_a,)),
        )

    assert external.main([
        "--dry-run", "--paths", str(slot_b / "src" / "module.py"),
    ]) == 0
    err = capsys.readouterr().err
    assert f"diff_root={slot_b.resolve()}" in err
    assert f"pm_home={home_b.resolve()}" in err
    assert "소유 PM 홈 conf 를 읽지 못했습니다" not in err

    # 적용된 denylist 도 선택된 소유자(B)의 선언이다 — 명시 지정한 `*.b-vault` 경로가 차단된다.
    assert external.main([
        "--dry-run", "--paths", str(slot_b / "src" / "keys.b-vault"),
    ]) == 1
    blocked = capsys.readouterr().err
    assert "*.b-vault" in blocked


def test_explicit_anchor_run_has_zero_pre_selection_conf_dependency(
    tmp_path, monkeypatch, capsys,
):
    """명시 앵커 실행은 선택 전 config 를 **읽지도 검사하지도** 않는다 — 로드 seam 호출로 단언."""
    _home_a, slot_a, home_b, slot_b = _cross_repo_absolute_target(
        tmp_path, break_target_owner=False,
    )
    external = _load("external_review_no_pre_selection_conf")
    monkeypatch.setattr(external, "REPO", slot_a)
    real_loader = external._local_config_for_repo
    loaded: list[Path] = []

    def _tracking_loader(repo):
        loaded.append(Path(repo).resolve())
        return real_loader(repo)

    monkeypatch.setattr(external, "_local_config_for_repo", _tracking_loader)

    assert external.main([
        "--dry-run", "--paths", str(slot_b / "src" / "module.py"),
    ]) == 0
    # 선택된 소유자 conf 정확히 1회 — 엔진 슬롯/그 소유 PM 홈 conf 는 열리지 않는다.
    assert loaded == [home_b.resolve()]
    assert f"pm_home={home_b.resolve()}" in capsys.readouterr().err


def test_absolute_paths_still_fail_closed_when_target_owner_conf_is_unreadable(
    tmp_path, monkeypatch, capsys,
):
    """역케이스: 선택된 소유자(B) 쪽 conf 를 못 읽으면 절대 --paths 여도 차단(MF-3 불변)."""
    _home_a, slot_a, home_b, slot_b = _cross_repo_absolute_target(
        tmp_path, break_target_owner=True,
    )
    external = _load("external_review_cross_repo_absolute_closed")
    monkeypatch.setattr(external, "REPO", slot_a)
    monkeypatch.setattr(
        external, "run_review",
        lambda *args, **kwargs: pytest.fail("외부 송신 전에 차단해야 한다"),
    )

    assert external.main([
        "--dry-run", "--paths", str(slot_b / "src" / "module.py"),
    ]) == 1
    err = capsys.readouterr().err
    assert "소유 PM 홈 conf 를 읽지 못했습니다" in err
    assert str((home_b / ".project_manager" / "local.conf").resolve()) in err


def test_owner_conf_read_failure_oracle_is_sensitive_to_paths_escape(
    tmp_path, monkeypatch, capsys,
):
    """읽기 실패를 후보 모호성과 같은 등급으로 되돌리면 --paths 가 미검증 필터 송신을 통과시킨다."""
    home, worktree = _demoted_worktree_with_owner_filters(tmp_path)
    (home / ".project_manager" / "local.conf").write_bytes(b"\xff\xfe\x00")
    external = _load("external_review_owner_conf_escape_sensitivity")
    monkeypatch.setattr(external, "REPO", worktree)
    real_owner_filter_conf = external._owner_filter_conf

    def _legacy_owner_filter_conf(demotion):
        try:
            return real_owner_filter_conf(demotion)
        except external.OwnerFilterConfError as exc:
            raise external.AnchorResolutionError(str(exc)) from exc

    monkeypatch.setattr(external, "_owner_filter_conf", _legacy_owner_filter_conf)
    assert external.main(["--paths", "src/module.py", "--dry-run"]) == 0
    assert "승계하지 못했습니다" in capsys.readouterr().err


def test_demoted_filter_oracle_is_sensitive_to_unfiltered_send(
    tmp_path, monkeypatch, capsys,
):
    """승계·차단 가드를 강등 이전 동작으로 되돌리면 두 형상 모두 무필터로 통과한다."""
    _home, worktree = _demoted_worktree_with_owner_filters(tmp_path / "inherit")
    external = _load("external_review_demoted_sensitivity")
    monkeypatch.setattr(external, "REPO", worktree)
    monkeypatch.setattr(
        external, "_conf_with_owner_filters",
        lambda conf, demotions, **kwargs: conf,
    )

    assert external.main(["--dry-run"]) == 0
    err = capsys.readouterr().err
    assert "'*.vault' 매칭" not in err        # 시크릿 파일이 필터 없이 diff 에 남는다
    assert "검토 경로: ['src']" not in err     # 소유 PM 홈 범위도 적용되지 않는다

    (tmp_path / "block").mkdir()
    _source, unregistered = _unregistered_worktree(tmp_path / "block")
    module = unregistered / "src" / "module.py"
    module.parent.mkdir()
    module.write_text("value = 1\n", encoding="utf-8")
    _git(unregistered, "add", "src/module.py")
    _git(unregistered, "commit", "-qm", "source")
    module.write_text("value = 2\n", encoding="utf-8")
    monkeypatch.setattr(external, "REPO", unregistered)

    # 승계 불가 형상도 차단 없이 diff 추출·송신 단계까지 내려간다(옛 동작).
    assert external.main(["--dry-run"]) == 0
    unblocked = capsys.readouterr().err
    assert "시크릿 필터가 좁아진" not in unblocked
    assert "검토 경로:" in unblocked


# ── diff 폭 표 · 슬롯 소유 근거 단계 ────────────────────────────────────────


def _multi_commit_base_home(root: Path) -> tuple[Path, Path, Path]:
    """base 에 커밋이 2개인 PM 홈 + **아무 작업도 하지 않은** 등록 슬롯 2개.

    `old` 는 첫 커밋에, `tip` 은 두 번째 커밋에 놓인다. 두 슬롯 모두 자기 변경이 없지만 `tip` 의
    `HEAD~1..HEAD` 는 **공유 base 의 마지막 커밋**이라 비어 있지 않다 — 이 단계를 소유 근거로 쓰면
    놀고 있는 슬롯이 '변경 슬롯'으로 뽑힌다.
    """
    home = root
    home.mkdir(parents=True)
    _git(home, "init", "-q")
    _git(home, "config", "user.email", "test@example.invalid")
    _git(home, "config", "user.name", "test")
    source = home / "src" / "module.py"
    source.parent.mkdir()
    source.write_text("value = 1\n", encoding="utf-8")
    _git(home, "add", "src/module.py")
    _git(home, "commit", "-qm", "first")
    first_commit = _git_out(home, "rev-parse", "HEAD")
    source.write_text("value = 2\n", encoding="utf-8")
    _git(home, "add", "src/module.py")
    _git(home, "commit", "-qm", "second")

    old = home / "work" / "old"
    tip = home / "work" / "tip"
    old.parent.mkdir()
    _git(home, "worktree", "add", "-q", "-b", "old-slot", str(old), first_commit)
    _git(home, "worktree", "add", "-q", "-b", "tip-slot", str(tip))
    conf = home / ".project_manager" / "local.conf"
    conf.parent.mkdir(parents=True)
    conf.write_text("review_paths=src/module.py\n", encoding="utf-8")
    ledger = home / ".project_manager" / ".local" / "worktree-leases.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        json.dumps({"leases": [
            {"slot": "work/old", "state": "leased"},
            {"slot": "work/tip", "state": "leased"},
        ]}),
        encoding="utf-8",
    )
    return home, old, tip


def test_diff_width_table_and_slot_ownership_evidence_are_stated_once():
    """폭 표는 한 곳뿐이고, 슬롯 소유 근거는 그 표에서 **명시 base 만** 남긴 부분집합이다."""
    external = _load("external_review_diff_width")
    assert external._diff_bases("HEAD") == ("HEAD", "HEAD~1..HEAD")
    assert external._diff_bases("main") == ("main",)
    # 암묵 폴백 단계는 소유 근거가 아니다. 사용자가 그 리비전을 직접 지정하면 근거가 된다.
    assert external._slot_selection_bases("HEAD") == ("HEAD",)
    assert external._slot_selection_bases("main") == ("main",)
    assert external._slot_selection_bases("HEAD~1..HEAD") == ("HEAD~1..HEAD",)


def test_clean_slot_on_multi_commit_base_is_never_selected_as_changed(
    tmp_path, monkeypatch, capsys,
):
    """공유 base 의 마지막 커밋은 슬롯 소유 근거가 아니다 — 놀고 있는 슬롯을 뽑지 않는다."""
    home, _old, tip = _multi_commit_base_home(tmp_path / "multi-commit")
    external = _load("external_review_multi_commit_base")
    monkeypatch.setattr(external, "REPO", home)
    monkeypatch.setattr(
        external, "run_review",
        lambda *args, **kwargs: pytest.fail("외부 송신 전에 차단해야 한다"),
    )

    assert external.main(["--dry-run"]) == 1
    err = capsys.readouterr().err
    assert "diff worktree를 하나로 해소할 수 없습니다" in err
    assert "--base <공통 base ref>" in err
    # 폭 자체는 비어 있지 않다(추출이라면 리뷰했을 내용) — 소유 근거로 쓰지 않을 뿐이다.
    assert external._candidate_has_diff(tip, "HEAD~1..HEAD", ["src/module.py"]) is True


def test_commit_only_change_needs_explicit_anchor_in_multi_slot_home(
    tmp_path, monkeypatch, capsys,
):
    """커밋만 된 변경으로 슬롯을 고르려면 앵커 명시가 필요하고, 명시하면 그 슬롯이 확정된다."""
    home, _first, second = _dual_slot_home(tmp_path / "commit-only")
    base_commit = _git_out(home, "rev-parse", "HEAD")
    _commit_in_slot(second, "value = 2\n")
    external = _load("external_review_commit_only_slot")
    monkeypatch.setattr(external, "REPO", home)

    assert external.main(["--dry-run"]) == 1
    assert "--base <공통 base ref>" in capsys.readouterr().err

    assert external.main(["--dry-run", "--base", base_commit]) == 0
    err = capsys.readouterr().err
    assert f"diff_root={second.resolve()}" in err
    assert "검토 경로: ['src/module.py']" in err


def test_worktree_change_selects_its_slot_over_commit_only_slot(
    tmp_path, monkeypatch, capsys,
):
    """작업트리 변경을 가진 슬롯이 확정된다 — 커밋만 된 다른 슬롯은 경쟁 후보가 아니다."""
    home, first, second = _dual_slot_home(tmp_path / "stage-order")
    (first / "src" / "module.py").write_text("value = wip\n", encoding="utf-8")
    _commit_in_slot(second, "value = committed\n")
    external = _load("external_review_stage_order")
    monkeypatch.setattr(external, "REPO", home)

    assert external.main(["--dry-run"]) == 0
    assert f"diff_root={first.resolve()}" in capsys.readouterr().err


def test_slot_selection_oracle_is_sensitive_to_commit_fallback_as_evidence(
    tmp_path, monkeypatch, capsys,
):
    """폴백 단계를 소유 근거로 되돌리면 놀던 tip 슬롯이 뽑혀 공유 base 커밋이 송신된다."""
    home, _old, tip = _multi_commit_base_home(tmp_path / "fallback-evidence")
    external = _load("external_review_fallback_evidence")
    monkeypatch.setattr(external, "REPO", home)
    monkeypatch.setattr(external, "_slot_selection_bases", external._diff_bases)

    assert external.main(["--dry-run"]) == 0
    captured = capsys.readouterr()
    assert f"diff_root={tip.resolve()}" in captured.err
    assert "value = 2" in captured.out  # 아무도 이번에 만들지 않은 base 커밋이 리뷰 대상이 된다


# ── 교차 소유 검출은 review_paths 선언 유무와 무관 ─────────────────────────


def test_default_review_paths_cross_owned_slot_blocks_before_external_send(
    tmp_path, monkeypatch, capsys,
):
    """conf 가 review_paths 를 선언하지 않아도 교차 소유 형상은 같은 기준으로 차단한다."""
    home_a, home_b, _slot = _cross_owned_slot(
        tmp_path / "default-cross", declare_review_paths=False,
    )
    external = _load("external_review_default_cross_owned")
    monkeypatch.setattr(external, "REPO", home_a)
    monkeypatch.setattr(
        external, "run_review",
        lambda *args, **kwargs: pytest.fail("외부 송신 전에 차단해야 한다"),
    )

    assert external.main(["--dry-run"]) == 1
    err = capsys.readouterr().err
    assert "외부 송신 전에 중단합니다" in err
    assert str(home_a.resolve()) in err
    assert str(home_b.resolve()) in err


def test_explicit_paths_escape_default_paths_cross_owned_block(
    tmp_path, monkeypatch, capsys,
):
    """선언 없는 형상에서도 --paths 탈출구는 불변이다."""
    home_a, _home_b, slot = _cross_owned_slot(
        tmp_path / "default-escape", declare_review_paths=False,
    )
    external = _load("external_review_default_cross_owned_escape")
    monkeypatch.setattr(external, "REPO", home_a)

    assert external.main(["--dry-run", "--paths", str(slot / "src" / "a.py")]) == 0
    capsys.readouterr()


def test_default_paths_cross_owned_oracle_is_sensitive_to_declared_only_guard(
    tmp_path, monkeypatch, capsys,
):
    """판정을 옛 '선언된 review_paths 만' 기준으로 되돌리면 같은 형상이 rc=0 으로 통과한다."""
    home_a, _home_b, _slot = _cross_owned_slot(
        tmp_path / "declared-only", declare_review_paths=False,
    )
    external = _load("external_review_declared_only_guard")
    monkeypatch.setattr(external, "REPO", home_a)
    monkeypatch.setattr(
        external, "_scope_from_initial_pm_home", lambda **kwargs: False,
    )

    assert external.main(["--dry-run"]) == 0
    capsys.readouterr()
