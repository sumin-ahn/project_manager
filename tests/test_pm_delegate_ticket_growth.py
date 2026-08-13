"""T-0676 — slot 티켓 성장 사본 prepare/harvest 경계."""
from __future__ import annotations

import importlib.util
import contextlib
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
PM_DELEGATE = TOOLS / "pm_delegate.py"


def _load_pd():
    spec = importlib.util.spec_from_file_location("pm_delegate_growth", PM_DELEGATE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def pd():
    return _load_pd()


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
    )


def _ticket_text(ticket: str, sections: list[tuple[str, str]]) -> str:
    body = [
        "---\n", f"id: {ticket}\n", "title: 성장 사본\n", "status: claimed\n",
        "created: '2026-08-13'\n", "created_by: test\n", "claimed_by: test/slot\n",
        "claimed_at: '2026-08-13T00:00:00+00:00'\n", "completed_at: null\n",
        "depends_on: []\n", "blocks: []\n", "touches: []\n", "estimate: medium\n",
        "design: 'waived: test'\n", "tags: []\n", "---\n",
        f"# {ticket}\n\n## 목표\n성장 marker 평문 `pm-ticket-section:start/end role=<role>` 설명.\n",
    ]
    labels = {"developer": "구현 보충", "code-reviewer": "리뷰", "architect": "설계"}
    for role, content in sections:
        body += [
            f"\n<!-- pm-ticket-section:start role={role} -->\n",
            f"## {labels[role]} ({role} · 2026-08-13)\n\n",
            content,
            f"<!-- pm-ticket-section:end role={role} -->\n",
        ]
    return "".join(body)


@pytest.fixture
def growth_env(tmp_path, pd, monkeypatch):
    pm_home = tmp_path / "pm-home"
    slot = tmp_path / "slot"
    pm_tools = pm_home / ".project_manager" / "tools"
    pm_tools.mkdir(parents=True)
    for source in TOOLS.glob("*.py"):
        shutil.copy2(source, pm_tools / source.name)
    tickets = pm_home / ".project_manager" / "wiki" / "tickets" / "claimed"
    tickets.mkdir(parents=True)
    (pm_home / ".project_manager" / ".local").mkdir(parents=True)
    slot.mkdir()
    assert _git(slot, "init", "-q").returncode == 0
    (slot / "tracked.txt").write_text("seed\n", encoding="utf-8")
    assert _git(slot, "add", "tracked.txt").returncode == 0
    monkeypatch.setenv("GIT_AUTHOR_NAME", "growth")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "growth@test.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "growth")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "growth@test.invalid")
    assert _git(slot, "commit", "-qm", "seed").returncode == 0
    # focused helper tests do not exercise board-git remote sync/render.
    monkeypatch.setattr(pd, "_load_board_for_repo", lambda _repo: _fixture_board(pd, pm_home))
    return pm_home, slot, tickets


def _fixture_board(pd, pm_home: Path):
    board = pd._load_module_from_path(
        pm_home / ".project_manager" / "tools" / "board.py",
        "board.py", verifier=pd._verify_engine_rev,
    )
    board.REPO = pm_home
    board.LOCAL_DIR = pm_home / ".project_manager" / ".local"
    board.BOARD_LOCK = board.LOCAL_DIR / "board.lock"
    board._growth_mutation_sync = lambda _message, _path: True
    return board


def _write_ticket(tickets: Path, ticket: str, sections: list[tuple[str, str]]) -> Path:
    path = tickets / f"{ticket}-growth.md"
    path.write_text(_ticket_text(ticket, sections), encoding="utf-8")
    return path


def _replace_content(pd, path: Path, role: str, ordinal: int, content: str) -> None:
    text = path.read_text(encoding="utf-8")
    section = pd._ticket_role_section(text, role, ordinal=ordinal)
    path.write_text(
        text[:section.content_start] + content + text[section.content_end:],
        encoding="utf-8",
    )


@pytest.mark.parametrize("role", ["developer", "code-reviewer", "architect"])
def test_prepare_edit_harvest_round_trip_and_git_hidden(growth_env, pd, role):
    pm_home, slot, tickets = growth_env
    ticket = "T-1001"
    source = _write_ticket(tickets, ticket, [(role, "")])
    plan = pd.prepare_ticket_copy(ticket=ticket, role=role, cwd=slot, pm_home=pm_home)

    assert plan.path.is_relative_to(slot)
    assert stat.S_IMODE(plan.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(plan.baseline_path.stat().st_mode) == 0o400
    assert stat.S_IMODE(plan.path.with_name(pd.TICKET_COPY_TAG_NAME).stat().st_mode) == 0o400
    assert _git(slot, "status", "--short").stdout == ""
    _replace_content(pd, plan.path, role, 0, f"{role} 사실 기록\n")
    first = pd.harvest_ticket_copy(copy_path=plan.path, cwd=slot, pm_home=pm_home, capability=plan.capability)
    assert first == pd.TicketHarvestResult(True, True)
    assert f"{role} 사실 기록" in source.read_text(encoding="utf-8")
    assert plan.path.exists() and plan.baseline_path.exists()
    second = pd.harvest_ticket_copy(copy_path=plan.path, cwd=slot, pm_home=pm_home, capability=plan.capability)
    assert second == pd.TicketHarvestResult(False, True)


def test_same_role_recall_targets_latest_and_preserves_pm_home_drift(growth_env, pd):
    pm_home, slot, tickets = growth_env
    source = _write_ticket(
        tickets, "T-1002", [("developer", "old round\n"), ("developer", "")],
    )
    plan = pd.prepare_ticket_copy(
        ticket="T-1002", role="developer", cwd=slot, pm_home=pm_home,
    )
    _replace_content(pd, plan.path, "developer", 1, "new round facts\n")
    # 준비 뒤 다른 역할 절 append/drift는 그대로 보존되어야 한다.
    current = source.read_text(encoding="utf-8")
    source.write_text(current + "\nPM parallel note outside prepared section\n", encoding="utf-8")
    assert pd.harvest_ticket_copy(copy_path=plan.path, cwd=slot, pm_home=pm_home, capability=plan.capability).changed
    final = source.read_text(encoding="utf-8")
    assert "old round" in final and "new round facts" in final
    assert "PM parallel note outside prepared section" in final


def test_crlf_prepare_edit_harvest_and_idempotent_preserve_newlines(growth_env, pd):
    pm_home, slot, tickets = growth_env
    source = tickets / "T-1019-growth.md"
    original = _ticket_text("T-1019", [("developer", "")]).replace("\n", "\r\n")
    source.write_bytes(original.encode("utf-8"))
    plan = pd.prepare_ticket_copy(
        ticket="T-1019", role="developer", cwd=slot, pm_home=pm_home,
    )
    copy_text = plan.path.read_bytes().decode("utf-8")
    section = pd._ticket_role_section(copy_text, "developer")
    edited = (
        copy_text[:section.content_start]
        + "CRLF facts\r\n"
        + copy_text[section.content_end:]
    )
    plan.path.write_bytes(edited.encode("utf-8"))
    first = pd.harvest_ticket_copy(
        copy_path=plan.path, cwd=slot, pm_home=pm_home,
        capability=plan.capability,
    )
    assert first == pd.TicketHarvestResult(True, True)
    after = source.read_bytes()
    assert b"CRLF facts\r\n" in after
    assert b"\n" not in after.replace(b"\r\n", b"")
    second = pd.harvest_ticket_copy(
        copy_path=plan.path, cwd=slot, pm_home=pm_home,
        capability=plan.capability,
    )
    assert second == pd.TicketHarvestResult(False, True)
    assert source.read_bytes() == after


def test_stale_same_section_refuses_without_overwrite(growth_env, pd):
    pm_home, slot, tickets = growth_env
    source = _write_ticket(tickets, "T-1003", [("developer", "")])
    plan = pd.prepare_ticket_copy(ticket="T-1003", role="developer", cwd=slot, pm_home=pm_home)
    _replace_content(pd, plan.path, "developer", 0, "agent content\n")
    _replace_content(pd, source, "developer", 0, "parallel PM content\n")
    with pytest.raises(pd.DelegateError, match="stale overwrite"):
        pd.harvest_ticket_copy(copy_path=plan.path, cwd=slot, pm_home=pm_home, capability=plan.capability)
    assert "parallel PM content" in source.read_text(encoding="utf-8")
    assert plan.path.exists() and plan.baseline_path.exists()


def test_newer_same_role_section_refuses_old_round_harvest(growth_env, pd):
    pm_home, slot, tickets = growth_env
    source = _write_ticket(tickets, "T-1006", [("developer", "")])
    plan = pd.prepare_ticket_copy(ticket="T-1006", role="developer", cwd=slot, pm_home=pm_home)
    _replace_content(pd, plan.path, "developer", 0, "late old agent\n")
    source.write_text(
        source.read_text(encoding="utf-8")
        + "\n<!-- pm-ticket-section:start role=developer -->\n"
        + "## 재구현 (developer · 2026-08-13)\n\n"
        + "<!-- pm-ticket-section:end role=developer -->\n",
        encoding="utf-8",
    )
    with pytest.raises(pd.DelegateError, match="새 성장 절"):
        pd.harvest_ticket_copy(copy_path=plan.path, cwd=slot, pm_home=pm_home, capability=plan.capability)
    assert "late old agent" not in source.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "mutator,pattern",
    [
        (lambda text: text.replace("## 목표", "## changed outside"), "절 밖 bytes"),
        (lambda text: text.replace("<!-- pm-ticket-section:end role=developer -->", ""), "end 누락"),
        (lambda text: text.replace("<!-- pm-ticket-section:end role=developer -->", "<!-- pm-ticket-section:end role=architect -->"), "역할 불일치"),
        (lambda text: text.replace("<!-- pm-ticket-section:start role=developer -->", "<!-- pm-ticket-section:start role=developer -->\n<!-- pm-ticket-section:start role=developer -->"), "중첩"),
    ],
)
def test_tamper_and_marker_damage_fail_loud_preserving_files(growth_env, pd, mutator, pattern):
    pm_home, slot, tickets = growth_env
    source = _write_ticket(tickets, "T-1004", [("developer", "")])
    plan = pd.prepare_ticket_copy(ticket="T-1004", role="developer", cwd=slot, pm_home=pm_home)
    plan.path.write_text(mutator(plan.path.read_text(encoding="utf-8")), encoding="utf-8")
    before = source.read_bytes()
    with pytest.raises(pd.DelegateError, match=pattern):
        pd.harvest_ticket_copy(copy_path=plan.path, cwd=slot, pm_home=pm_home, capability=plan.capability)
    assert source.read_bytes() == before
    assert plan.path.exists() and plan.baseline_path.exists()


def test_forged_bundle_cannot_redirect_harvest_to_another_ticket(growth_env, pd):
    """동일 uid가 bundle mode를 풀어 baseline/metadata를 함께 위조해도 PM trust가 막는다."""
    pm_home, slot, tickets = growth_env
    first = _write_ticket(tickets, "T-1012", [("code-reviewer", "")])
    second = _write_ticket(tickets, "T-1013", [("code-reviewer", "")])
    plan = pd.prepare_ticket_copy(
        ticket="T-1012", role="code-reviewer", cwd=slot, pm_home=pm_home,
    )
    second_bytes = second.read_bytes()
    forged_copy = second.read_text(encoding="utf-8")
    section = pd._ticket_role_section(forged_copy, "code-reviewer")
    forged_copy = (
        forged_copy[:section.content_start]
        + "위조된 cross-ticket review\n"
        + forged_copy[section.content_end:]
    )
    metadata = json.loads(plan.metadata_path.read_text(encoding="utf-8"))
    metadata["source_relpath"] = second.relative_to(pm_home).as_posix()
    metadata["baseline_sha256"] = hashlib.sha256(second_bytes).hexdigest()
    plan.baseline_path.chmod(0o600)
    plan.metadata_path.chmod(0o600)
    plan.baseline_path.write_bytes(second_bytes)
    plan.metadata_path.write_text(
        pd._ticket_copy_metadata_bytes(metadata).decode("utf-8"),
        encoding="utf-8",
    )
    trust_dir = pm_home / pd.TICKET_COPY_TRUST_REL_ROOT / metadata["run_id"]
    for target, payload in (
        (trust_dir / pd.TICKET_COPY_BASELINE_NAME, second_bytes),
        (trust_dir / pd.TICKET_COPY_METADATA_NAME, pd._ticket_copy_metadata_bytes(metadata)),
    ):
        target.chmod(0o600)
        target.write_bytes(payload)
    plan.path.write_text(forged_copy, encoding="utf-8")
    first_before, second_before = first.read_bytes(), second.read_bytes()
    with pytest.raises(pd.DelegateError, match="capability MAC 검증 실패"):
        pd.harvest_ticket_copy(copy_path=plan.path, cwd=slot, pm_home=pm_home, capability=plan.capability)
    assert first.read_bytes() == first_before
    assert second.read_bytes() == second_before


def test_harvest_requeries_plan_ticket_canonical_exactly_once(growth_env, pd):
    pm_home, slot, tickets = growth_env
    source = _write_ticket(tickets, "T-1014", [("developer", "")])
    plan = pd.prepare_ticket_copy(
        ticket="T-1014", role="developer", cwd=slot, pm_home=pm_home,
    )
    _replace_content(pd, plan.path, "developer", 0, "agent facts\n")
    duplicate = source.with_name("T-1014-duplicate.md")
    duplicate.write_bytes(source.read_bytes())
    before = source.read_bytes()
    with pytest.raises(pd.DelegateError, match="canonical ticket 재조회.*found=2"):
        pd.harvest_ticket_copy(copy_path=plan.path, cwd=slot, pm_home=pm_home, capability=plan.capability)
    assert source.read_bytes() == before


def test_capability_replay_and_path_substitution_fail(growth_env, pd):
    pm_home, slot, tickets = growth_env
    first = _write_ticket(tickets, "T-1015", [("developer", "")])
    second = _write_ticket(tickets, "T-1016", [("developer", "")])
    first_plan = pd.prepare_ticket_copy(
        ticket="T-1015", role="developer", cwd=slot, pm_home=pm_home,
    )
    second_plan = pd.prepare_ticket_copy(
        ticket="T-1016", role="developer", cwd=slot, pm_home=pm_home,
    )
    before = first.read_bytes(), second.read_bytes()
    with pytest.raises(pd.DelegateError, match="capability MAC 검증 실패"):
        pd.harvest_ticket_copy(
            copy_path=second_plan.path, cwd=slot, pm_home=pm_home,
            capability=first_plan.capability,
        )
    substituted = first_plan.path.with_name("ticket-T-1016.md")
    substituted.write_bytes(first_plan.path.read_bytes())
    with pytest.raises(pd.DelegateError, match="사본 경로 ticket/role/run 불일치"):
        pd.harvest_ticket_copy(
            copy_path=substituted, cwd=slot, pm_home=pm_home,
            capability=first_plan.capability,
        )
    assert (first.read_bytes(), second.read_bytes()) == before


def test_solo_same_uid_full_replica_forgery_fails_before_pm_drift_overwrite(
        pd, monkeypatch, tmp_path):
    solo = tmp_path / "solo"
    tools = solo / ".project_manager" / "tools"
    tools.mkdir(parents=True)
    for source_tool in TOOLS.glob("*.py"):
        shutil.copy2(source_tool, tools / source_tool.name)
    tickets = solo / ".project_manager" / "wiki" / "tickets" / "claimed"
    tickets.mkdir(parents=True)
    (solo / ".project_manager" / ".local").mkdir(parents=True)
    source = _write_ticket(tickets, "T-1018", [("code-reviewer", "")])
    assert _git(solo, "init", "-q").returncode == 0
    monkeypatch.setattr(pd, "_load_board_for_repo", lambda _repo: _fixture_board(pd, solo))
    plan = pd.prepare_ticket_copy(
        ticket="T-1018", role="code-reviewer", cwd=solo, pm_home=solo,
    )
    _replace_content(pd, plan.path, "code-reviewer", 0, "forged agent review\n")
    _replace_content(pd, source, "code-reviewer", 0, "parallel PM review\n")
    metadata = json.loads(plan.metadata_path.read_text(encoding="utf-8"))
    trust_dir = solo / pd.TICKET_COPY_TRUST_REL_ROOT / metadata["run_id"]
    forged_meta = dict(metadata)
    forged_meta["ordinal"] = metadata["ordinal"] + 1
    forged_meta_bytes = pd._ticket_copy_metadata_bytes(forged_meta)
    for path, payload in (
        (plan.baseline_path, plan.path.read_bytes()),
        (plan.metadata_path, forged_meta_bytes),
        (plan.path.with_name(pd.TICKET_COPY_TAG_NAME), b"z" * 32),
        (trust_dir / pd.TICKET_COPY_BASELINE_NAME, plan.path.read_bytes()),
        (trust_dir / pd.TICKET_COPY_METADATA_NAME, forged_meta_bytes),
        (trust_dir / pd.TICKET_COPY_TAG_NAME, b"z" * 32),
    ):
        path.chmod(0o600)
        path.write_bytes(payload)
    before = source.read_bytes()
    with pytest.raises(pd.DelegateError, match="capability MAC 검증 실패"):
        pd.harvest_ticket_copy(
            copy_path=plan.path, cwd=solo, pm_home=solo,
            capability=plan.capability,
        )
    assert source.read_bytes() == before
    assert b"parallel PM review" in before


def test_idempotent_reharvest_retries_sync_after_atomic_write_crash(
        growth_env, pd, monkeypatch):
    pm_home, slot, tickets = growth_env
    source = _write_ticket(tickets, "T-1017", [("developer", "")])
    plan = pd.prepare_ticket_copy(
        ticket="T-1017", role="developer", cwd=slot, pm_home=pm_home,
    )
    _replace_content(pd, plan.path, "developer", 0, "crash-recovery facts\n")
    board = pd._load_board_for_repo(pm_home)
    calls = []
    board._growth_mutation_sync = lambda *_args: calls.append(True) or False
    monkeypatch.setattr(pd, "_load_board_for_repo", lambda _repo: board)
    first = pd.harvest_ticket_copy(
        copy_path=plan.path, cwd=slot, pm_home=pm_home,
        capability=plan.capability,
    )
    board._growth_mutation_sync = lambda *_args: calls.append(True) or True
    second = pd.harvest_ticket_copy(
        copy_path=plan.path, cwd=slot, pm_home=pm_home,
        capability=plan.capability,
    )
    assert first == pd.TicketHarvestResult(True, False)
    assert second == pd.TicketHarvestResult(False, True)
    assert len(calls) == 2 and "crash-recovery facts" in source.read_text(encoding="utf-8")


def test_plain_marker_discussion_is_not_data(pd):
    text = "문서의 `pm-ticket-section:start/end role=<role>` 문법 설명\n"
    assert pd._ticket_growth_sections(text) == []


def test_prepare_uses_filename_identity_when_frontmatter_is_corrupt(growth_env, pd):
    """기존 scope fail-soft처럼 손상 frontmatter도 filename+실 marker 사본은 준비한다."""
    pm_home, slot, tickets = growth_env
    source = _write_ticket(tickets, "T-1011", [("developer", "")])
    source.write_text(
        source.read_text(encoding="utf-8").replace("id: T-1011", "id: T-CORRUPTED"),
        encoding="utf-8",
    )
    plan = pd.prepare_ticket_copy(
        ticket="T-1011", role="developer", cwd=slot, pm_home=pm_home,
    )
    assert plan.path.read_bytes() == source.read_bytes()


def test_prepare_lookup_and_read_share_board_lock(pd, monkeypatch, tmp_path):
    pm_home = tmp_path / "pm"
    slot = tmp_path / "slot"
    source = pm_home / ".project_manager" / "wiki" / "tickets" / "claimed" / "T-1010-x.md"
    source.parent.mkdir(parents=True)
    source.write_text(_ticket_text("T-1010", [("developer", "")]), encoding="utf-8")
    slot.mkdir()
    assert _git(slot, "init", "-q").returncode == 0
    state = {"locked": False, "lookup_locked": False, "read_locked": False}

    class FakeBoard:
        @contextlib.contextmanager
        def board_lock(self):
            assert not state["locked"]
            state["locked"] = True
            try:
                yield
            finally:
                state["locked"] = False

        def tickets_dir(self):
            state["lookup_locked"] = state["locked"]
            return source.parents[1]

        @staticmethod
        def _ticket_id_from_filename(_name):
            return "T-1010"

    monkeypatch.setattr(pd, "_load_board_for_repo", lambda _repo: FakeBoard())
    original_read_bytes = Path.read_bytes

    def tracked_read_bytes(path):
        if path == source:
            state["read_locked"] = state["locked"]
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", tracked_read_bytes)
    plan = pd.prepare_ticket_copy(
        ticket="T-1010", role="developer", cwd=slot, pm_home=pm_home,
    )
    assert state == {"locked": False, "lookup_locked": True, "read_locked": True}
    assert plan.path.exists()


def test_reviewer_codex_keeps_worktree_read_only_and_grants_only_copy_dir(
        pd, monkeypatch, tmp_path):
    temp_root = tmp_path / "system-temp"
    cwd = tmp_path / "worktree"
    copy = cwd / pd.TICKET_COPY_REL_ROOT / "T-2001" / "code-reviewer" / ("a" * 32) / "ticket-T-2001.md"
    temp_root.mkdir()
    copy.parent.mkdir(parents=True)
    copy.write_text("copy", encoding="utf-8")
    monkeypatch.setattr(pd, "_gettempdir", lambda: str(temp_root))
    argv, _stdin, prompt_path, read_tmp = pd._prepare_attempt_transport(
        "codex", "gpt-x", None, "code-reviewer", cwd, "review",
        ticket_copy_path=copy,
    )
    try:
        assert "-s" not in argv and "workspace-write" not in argv
        profile = next(
            argv[index + 1] for index, token in enumerate(argv[:-1])
            if token == "-c" and argv[index + 1].startswith("permissions.")
        )
        assert '":root"="read"' in profile
        assert f'"{copy.parent}"="write"' in profile
        assert f'"{cwd}"="write"' not in profile
        assert argv[argv.index("-C") + 1] == str(read_tmp.path)
    finally:
        pd._cleanup_attempt_transport(prompt_path, read_tmp)


@pytest.mark.parametrize("harness,model", [("claude", "sonnet"), ("opencode", "prov/m")])
def test_reviewer_non_codex_ticket_copy_fails_closed(
        pd, monkeypatch, tmp_path, harness, model):
    temp_root = tmp_path / "system-temp"
    cwd = tmp_path / "worktree"
    copy = cwd / pd.TICKET_COPY_REL_ROOT / "T-2002" / "code-reviewer" / ("b" * 32) / "ticket-T-2002.md"
    temp_root.mkdir()
    copy.parent.mkdir(parents=True)
    copy.write_text("copy", encoding="utf-8")
    monkeypatch.setattr(pd, "_gettempdir", lambda: str(temp_root))
    with pytest.raises(pd.DelegateError, match="검증된 permission seam"):
        pd._prepare_attempt_transport(
            harness, model, None, "code-reviewer", cwd, "review",
            ticket_copy_path=copy,
        )


def test_claude_reviewer_ticket_copy_rejects_before_runner_spawn(
        pd, monkeypatch, tmp_path):
    temp_root = tmp_path / "system-temp"
    cwd = tmp_path / "worktree"
    copy = cwd / pd.TICKET_COPY_REL_ROOT / "T-2020" / "code-reviewer" / ("c" * 32) / "ticket-T-2020.md"
    temp_root.mkdir()
    copy.parent.mkdir(parents=True)
    copy.write_text("copy", encoding="utf-8")
    monkeypatch.setattr(pd, "_gettempdir", lambda: str(temp_root))
    spawned = []

    def runner(*_args, **_kwargs):
        spawned.append(True)
        raise AssertionError("Claude reviewer ticket-copy must fail before runner")

    with pytest.raises(pd.DelegateError, match="검증된 permission seam"):
        pd._execute_attempt(
            harness="claude", model="sonnet", reasoning=None,
            role="code-reviewer", cwd=cwd, prompt="review", timeout=30,
            output_dir=tmp_path / "raw", run_fn=runner, attempt="primary",
            ticket_copy_path=copy,
        )
    assert spawned == []


def test_resume_fallback_and_fresh_prompts_share_same_copy_preamble(pd, tmp_path):
    copy = tmp_path / "ticket-T-3001.md"
    plan = pd.TicketCopyPlan(
        copy, tmp_path / "baseline.md", tmp_path / "metadata.json", tmp_path,
        tmp_path, "T-3001", "developer",
    )
    for payload in ("resume delta", "fallback full", "fresh retry full"):
        composed = pd._with_ticket_copy_preamble(payload, plan)
        assert str(copy) in composed
        assert composed.endswith(payload)
        assert composed.count(str(copy)) == 1
    existing = pd._ticket_copy_preamble(plan) + "\n\nfull"
    assert pd._with_ticket_copy_preamble(existing, plan) == existing


def test_symlink_copy_root_and_outside_copy_are_rejected(growth_env, pd, tmp_path):
    pm_home, slot, tickets = growth_env
    _write_ticket(tickets, "T-1005", [("architect", "")])
    machine_root = slot / pd.TICKET_COPY_REL_ROOT
    machine_root.parent.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    machine_root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(pd.DelegateError, match="symlink"):
        pd.prepare_ticket_copy(ticket="T-1005", role="architect", cwd=slot, pm_home=pm_home)
    rogue = outside / "ticket-T-1005.md"
    rogue.write_text("x", encoding="utf-8")
    with pytest.raises(pd.DelegateError, match="containment"):
        pd.harvest_ticket_copy(copy_path=rogue, cwd=slot, pm_home=pm_home, capability=b"x" * 32)


def test_linked_worktree_git_common_exclude_preserves_no_newline(pd, tmp_path):
    primary = tmp_path / "primary"
    linked = tmp_path / "linked"
    primary.mkdir()
    assert _git(primary, "init", "-q").returncode == 0
    (primary / "tracked").write_text("x\n", encoding="utf-8")
    assert _git(primary, "add", "tracked").returncode == 0
    assert _git(
        primary, "-c", "user.name=test", "-c", "user.email=test@example.invalid",
        "commit", "-qm", "seed",
    ).returncode == 0
    assert _git(primary, "worktree", "add", "-q", "-b", "linked", str(linked)).returncode == 0
    common = Path(_git(linked, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip())
    exclude = common / "info" / "exclude"
    exclude.write_bytes(b"existing-without-newline")
    pd._ensure_ticket_copy_ignored(linked)
    content = exclude.read_bytes()
    assert content.startswith(b"existing-without-newline\n")
    assert content.endswith(pd.TICKET_COPY_IGNORE_PATTERN.encode() + b"\n")
    pd._ensure_ticket_copy_ignored(linked)
    assert exclude.read_bytes() == content


def test_git_exclude_symlink_fails_before_external_append(pd, tmp_path):
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    repo.mkdir()
    assert _git(repo, "init", "-q").returncode == 0
    common = Path(_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip())
    exclude = common / "info" / "exclude"
    before = outside.read_bytes() if outside.exists() else None
    exclude.unlink()
    exclude.symlink_to(outside)
    with pytest.raises(pd.DelegateError, match="lexical 좌표 불일치|regular file"):
        pd._ensure_ticket_copy_ignored(repo)
    assert not outside.exists() and before is None


def test_git_exclude_hardlink_victim_is_not_written(pd, tmp_path):
    repo = tmp_path / "repo"
    victim = tmp_path / "victim"
    repo.mkdir()
    victim.write_bytes(b"external-victim")
    assert _git(repo, "init", "-q").returncode == 0
    common = Path(_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip())
    exclude = common / "info" / "exclude"
    exclude.unlink()
    try:
        os.link(victim, exclude)
    except OSError as exc:
        pytest.skip(f"hardlink unsupported on this filesystem: {exc}")
    before = victim.read_bytes()
    with pytest.raises(pd.DelegateError, match="단일-link"):
        pd._ensure_ticket_copy_ignored(repo)
    assert victim.read_bytes() == before


def test_git_exclude_unsupported_fd_boundary_writes_nothing(pd, monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    assert _git(repo, "init", "-q").returncode == 0
    common = Path(_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip())
    exclude = common / "info" / "exclude"
    before = exclude.read_bytes()
    monkeypatch.setattr(pd.os, "supports_dir_fd", frozenset())
    with pytest.raises(pd.DelegateError, match="안전 경계 미지원"):
        pd._ensure_ticket_copy_ignored(repo)
    assert exclude.read_bytes() == before


def test_git_exclude_no_lock_backend_seam_writes_nothing(pd, monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    assert _git(repo, "init", "-q").returncode == 0
    common = Path(_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip())
    exclude = common / "info" / "exclude"
    before = exclude.read_bytes()

    class NoLock:
        @staticmethod
        def exclusive_lock_supported():
            return False

        @staticmethod
        def acquire_exclusive(_fd):
            raise AssertionError("unsupported seam must reject before lock acquire")

        @staticmethod
        def release_exclusive(_fd):
            raise AssertionError("unsupported seam never acquired")

    monkeypatch.setattr(pd, "_load_file_lock", lambda: NoLock())
    with pytest.raises(pd.DelegateError, match="배타락 primitive"):
        pd._ensure_ticket_copy_ignored(repo)
    assert exclude.read_bytes() == before
    assert not exclude.with_name("exclude.pm-delegate.lock").exists()


def test_ticket_cli_parser_help_and_dispatch(pd, monkeypatch, capsys, tmp_path):
    slot = tmp_path / "slot"
    slot.mkdir()
    monkeypatch.setattr(pd, "_ticket_cli_owner", lambda _cwd: tmp_path / "pm")
    fake = pd.TicketCopyPlan(
        slot / "ticket-T-2000.md", slot / "baseline.md", slot / "metadata.json",
        slot, tmp_path / "pm", "T-2000", "developer", b"c" * 32,
    )
    monkeypatch.setattr(pd, "prepare_ticket_copy", lambda **_kwargs: fake)
    assert pd.main(["ticket", "prepare", "--ticket", "T-2000", "--role", "developer", "--cwd", str(slot)]) == 0
    prepared = json.loads(capsys.readouterr().out)
    assert prepared == {"copy": str(fake.path), "capability": fake.capability.hex()}
    received = []
    monkeypatch.setattr(
        pd, "harvest_ticket_copy",
        lambda **kwargs: received.append(kwargs) or pd.TicketHarvestResult(False, True),
    )
    monkeypatch.setattr(pd.sys, "stdin", io.StringIO(fake.capability.hex() + "\n"))
    assert pd.main([
        "ticket", "harvest", "--copy", str(fake.path), "--cwd", str(slot),
        "--capability-stdin",
    ]) == 0
    harvested = json.loads(capsys.readouterr().out)
    assert harvested["changed"] is False and harvested["sync_ready"] is True
    assert received[0]["capability"] == fake.capability
    assert fake.capability.hex() not in json.dumps(harvested)
    monkeypatch.setattr(
        pd, "harvest_ticket_copy",
        lambda **_kwargs: (_ for _ in ()).throw(pd.DelegateError("sealed damage")),
    )
    monkeypatch.setattr(pd.sys, "stdin", io.StringIO(fake.capability.hex() + "\n"))
    assert pd.main([
        "ticket", "harvest", "--copy", str(fake.path), "--cwd", str(slot),
        "--capability-stdin",
    ]) == 1
    assert fake.capability.hex() not in capsys.readouterr().err
    with pytest.raises(SystemExit) as exc:
        pd.main(["ticket", "--help"])
    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    assert "prepare" in help_text and "harvest" in help_text
    assert "capability" not in help_text.lower() or "stdin" in help_text.lower()


@pytest.mark.parametrize(
    "runner_mode",
    [
        "ok", "nonzero", "exception", "ok-harvest-fail", "exception-harvest-fail",
        "ok-harvest-runtime", "exception-harvest-runtime",
        "ok-harvest-skew", "exception-harvest-skew",
    ],
)
def test_cross_main_always_harvests_and_preserves_copy(
        pd, monkeypatch, tmp_path, capsys, runner_mode):
    """subprocess rc/예외와 무관하게 cross 후처리가 같은 보존 사본을 harvest한다."""
    cwd = tmp_path / "slot"
    cwd.mkdir()
    prompt = cwd / "prompt.md"
    prompt.write_text("단일 티켓 작업", encoding="utf-8")
    copy = cwd / ".project_manager" / ".local" / "delegate-ticket-copies" / "T-3000" / "developer" / "run" / "ticket-T-3000.md"
    copy.parent.mkdir(parents=True)
    copy.write_text("preserved", encoding="utf-8")
    plan = pd.TicketCopyPlan(
        copy, copy.with_name("baseline.md"), copy.with_name("metadata.json"),
        cwd, cwd, "T-3000", "developer", b"k" * 32,
    )
    prepared = []
    harvested = []
    runner_error = RuntimeError("fake runner exploded")

    real_er = pd._load_external_review()
    monkeypatch.setattr(real_er, "repo_root_from_cwd", lambda _cwd: cwd)
    monkeypatch.setattr(real_er, "resolve_pm_home_for_repo", lambda *_a, **_k: cwd)
    monkeypatch.setattr(real_er, "_owns_real_board", lambda _pm: False)
    monkeypatch.setattr(pd, "_load_external_review", lambda: real_er)
    monkeypatch.setattr(pd, "_cwd_in_git_repo", lambda *_a, **_k: True)
    monkeypatch.setattr(pd, "local_config", lambda: {
        "delegate_enabled": "true",
        "delegate.developer.harness": "codex",
        "delegate.developer.model": "gpt-x",
    })
    monkeypatch.setattr(pd, "cold_reinjection_record", lambda *_a, **_k: None)
    monkeypatch.setattr(pd, "check_local_conf_divergence", lambda *_a, **_k: (cwd, None, real_er))
    monkeypatch.setattr(pd, "begin_scope_audit", lambda *_a, **_k: None)
    monkeypatch.setattr(pd, "report_scope_audit", lambda *_a, **_k: None)
    monkeypatch.setattr(pd, "prepare_ticket_copy", lambda **kwargs: prepared.append(kwargs) or plan)
    def harvest(**kwargs):
        harvested.append(kwargs)
        if runner_mode.endswith("harvest-fail"):
            raise pd.DelegateError("simultaneous harvest damage")
        if runner_mode.endswith("harvest-runtime"):
            raise RuntimeError("runtime harvest damage")
        if runner_mode.endswith("harvest-skew"):
            skew = RuntimeError("marked harvest engine skew")
            skew._engine_rev_skew = True
            raise skew
        return pd.TicketHarvestResult(True, True)

    monkeypatch.setattr(pd, "harvest_ticket_copy", harvest)

    seen_prompt = []

    def runner(_argv, *, stdin_text, **_kwargs):
        seen_prompt.append(stdin_text)
        if runner_mode.startswith("exception"):
            raise runner_error
        return {
            "returncode": 0 if runner_mode == "ok" else 9,
            "stdout": (
                '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}\n'
                if runner_mode.startswith("ok") else ""
            ),
            "stderr": "", "timed_out": False,
        }

    argv = [
        "--role", "developer", "--prompt-file", str(prompt), "--cwd", str(cwd),
        "--ticket", "T-3000", "--output-dir", str(tmp_path / "raw"),
    ]
    if runner_mode.startswith("exception"):
        with pytest.raises(RuntimeError, match="fake runner exploded") as caught:
            pd.main(argv, run_fn=runner)
        assert caught.value is runner_error
    elif runner_mode == "ok-harvest-skew":
        with pytest.raises(RuntimeError, match="marked harvest engine skew") as caught:
            pd.main(argv, run_fn=runner)
        assert pd._is_engine_rev_skew(caught.value)
    else:
        expected = 0 if runner_mode == "ok" else 1
        assert pd.main(argv, run_fn=runner) == expected
    assert len(prepared) == 1 and len(harvested) == 1
    assert harvested[0]["copy_path"] == copy
    assert copy.read_text(encoding="utf-8") == "preserved"
    assert str(copy) in seen_prompt[0]
    assert "자기 역할 절만" in seen_prompt[0]
    captured = capsys.readouterr()
    token = plan.capability.hex()
    assert token not in captured.out and token not in captured.err
    assert all(token not in prompt_text for prompt_text in seen_prompt)
    assert all(token not in path.read_text(encoding="utf-8")
               for path in (tmp_path / "raw").glob("*.txt"))
    if "harvest-" in runner_mode and runner_mode != "ok-harvest-skew":
        err = captured.err
        expected_damage = (
            "marked harvest engine skew" if runner_mode.endswith("harvest-skew")
            else (
                "runtime harvest damage"
                if runner_mode.endswith("harvest-runtime")
                else "simultaneous harvest damage"
            )
        )
        assert expected_damage in err and "새 prepare/위임" in err
        if runner_mode.startswith("exception"):
            assert "RuntimeError: fake runner exploded" in err


def test_cross_prepare_failure_is_loud_before_primary_or_fallback_spawn(
        pd, monkeypatch, tmp_path, capsys):
    """사본 경계가 없으면 무사본 실행으로 degrade하지 않고 전송 0회로 닫는다."""
    cwd = tmp_path / "slot"
    cwd.mkdir()
    prompt = cwd / "prompt.md"
    prompt.write_text("단일 티켓 작업", encoding="utf-8")
    real_er = pd._load_external_review()
    monkeypatch.setattr(real_er, "repo_root_from_cwd", lambda _cwd: cwd)
    monkeypatch.setattr(real_er, "resolve_pm_home_for_repo", lambda *_a, **_k: cwd)
    monkeypatch.setattr(real_er, "_owns_real_board", lambda _pm: False)
    monkeypatch.setattr(pd, "_load_external_review", lambda: real_er)
    monkeypatch.setattr(pd, "_cwd_in_git_repo", lambda *_a, **_k: True)
    monkeypatch.setattr(pd, "local_config", lambda: {
        "delegate_enabled": "true",
        "delegate.developer.harness": "codex",
        "delegate.developer.model": "gpt-x",
        "delegate.developer.fallback.harness": "claude",
        "delegate.developer.fallback.model": "sonnet",
    })
    monkeypatch.setattr(
        pd, "check_local_conf_divergence", lambda *_a, **_k: (cwd, None, real_er),
    )
    monkeypatch.setattr(
        pd, "prepare_ticket_copy",
        lambda **_kwargs: (_ for _ in ()).throw(
            pd.DelegateError("developer 성장 marker 없음")
        ),
    )
    spawned = []

    def runner(*_args, **_kwargs):
        spawned.append(True)
        raise AssertionError("prepare 실패 뒤 외부 실행 금지")

    rc = pd.main([
        "--role", "developer", "--prompt-file", str(prompt), "--cwd", str(cwd),
        "--ticket", "T-3010", "--output-dir", str(tmp_path / "raw"),
    ], run_fn=runner)
    assert rc == 1 and spawned == []
    err = capsys.readouterr().err
    assert "위임 티켓 사본 준비 실패" in err and "성장 marker 없음" in err


def test_role_docs_and_delegate_card_pin_growth_contract():
    developer = (REPO / ".claude" / "agents" / "developer.md").read_text(encoding="utf-8")
    reviewer = (REPO / ".claude" / "agents" / "code-reviewer.md").read_text(encoding="utf-8")
    architect = (REPO / ".claude" / "agents" / "architect.md").read_text(encoding="utf-8")
    card = (REPO / ".claude" / "skills" / "pm-dev-delegate" / "SKILL.md").read_text(encoding="utf-8")
    assert "구현 방식·변경 지점" in developer and "빈틈을 메운 판단" in developer
    assert "설계·구현 보충 포함" in reviewer and "must-fix/should-fix/suggestion" in reviewer
    assert "경계 실측·불변식·표면 상한·테스트 전략" in architect and "재설계 절" in architect
    assert "ticket prepare" in card and "ticket harvest" in card
    assert "finally" in card and "--dry-run`은 무부수효과" in card
    assert "Claude와 OpenCode의 code-reviewer+ticket-copy는 spawn 전에 fail-loud" in card
    engine = _load_pd()
    rendered_help = engine.build_arg_parser().format_help()
    assert all(term in rendered_help for term in (
        "검증된 Codex", "permission profile만 지원", "Claude/OpenCode", "fail-loud",
    ))
    for text in (developer, reviewer, architect):
        assert "자기평가" in text and "marker" in text and "PM 홈 티켓" in text
