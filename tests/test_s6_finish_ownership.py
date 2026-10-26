"""묶음 종결 선검사의 명시 소유 입력과 관측 실패 계약."""
from __future__ import annotations

import pytest

from _manager_home import tree_snapshot
import ticket_finish as finish
from pm_engine.app import cluster_binding, ticket_access
from pm_engine.cli import board_ticket
from pm_engine.domain import finish_rules, identity_args, ticket_rules
from pm_engine.infra import fs, pm_paths, ticket_store


_TICKET = "T-" + "8" * 4
_TASK = "checked-task"


@pytest.fixture
def ownership_home(tmp_path, monkeypatch):
    monkeypatch.delenv("PM_SESSION_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_NAME", raising=False)
    root = tmp_path / "home"
    tickets = root / ".project_manager/board/tickets/claimed"
    tickets.mkdir(parents=True)
    home = pm_paths.home_at(root)
    home.local_conf.write_text("identity.user=alice\n", encoding="utf-8")
    path = tickets / f"{_TICKET}-ownership.md"
    ticket_store.dump_ticket(path, {
        "id": _TICKET, "status": "claimed", "claimed_by": f"alice/{_TASK}",
    }, "## 완료 조건\n- [x] 구현\n")
    board_py = root / ".project_manager/tools/board.py"
    board_py.parent.mkdir()
    return home, path, board_py


def _closer(home, board_py, *, task=_TASK, board_identity_args=()):
    finisher = finish.TicketFinisher(
        board_py=board_py, log_file=home.root / "log.md",
        regression_cwd=str(home.root),
    )
    return finish.ClusterCloser(
        f"C-{_TICKET}", home=home, finisher=finisher, board_py=board_py,
        task=task, board_identity_args=board_identity_args,
    )


def test_explicit_task_and_default_user_reach_owner(ownership_home, monkeypatch):
    home, path, board_py = ownership_home
    identity_args.validate_task_name(_TASK)
    closer = _closer(
        home, board_py, board_identity_args=["--task", "transport-task"],
    )
    calls = []
    ownership = ticket_access._ticket_ownership

    def observe(selected_home, frontmatter, **kwargs):
        calls.append((selected_home, frontmatter, kwargs))
        return ownership(selected_home, frontmatter, **kwargs)

    monkeypatch.setattr(ticket_access, "_ticket_ownership", observe)
    assert closer._board_complete_problems(_TICKET) == []
    assert calls == [(home, ticket_store.load_ticket(path)[0],
                      {"task": _TASK, "user": None})]
    assert closer._board_identity_args == ["--task", "transport-task"]


def test_preflight_ignores_board_parser_file(ownership_home):
    home, _path, board_py = ownership_home
    marker = home.root / "parser-loaded"
    assert not board_py.exists()
    assert _closer(home, board_py)._board_complete_problems(_TICKET) == []
    for source in (
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('loaded')\n"
        "raise AssertionError('board parser must not be loaded')\n",
        "ENGINE_REV = 'different-revision'\n",
        "VALUE = 'no build_parser'\n",
    ):
        board_py.write_text(source, encoding="utf-8")
        assert _closer(home, board_py)._board_complete_problems(_TICKET) == []
        assert board_py.read_text(encoding="utf-8") == source
        assert not marker.exists()


def test_ownership_rejections_match_direct_verdict(ownership_home):
    home, path, board_py = ownership_home
    cases = (
        (f"alice/{_TASK}", _TASK, False, True),
        (f"alice/{_TASK}", "other-task", False, False),
        (f"bob/{_TASK}", _TASK, False, False),
        (f"alice/{_TASK}", None, False, False),
        (_TASK, _TASK, False, True),
        (_TASK, _TASK, True, False),
        (None, "../invalid", False, True),
    )
    for claimed_by, task, multi_user, accepted in cases:
        if multi_user:
            ticket_store.dump_ticket(path.with_name("T-7777-other.md"), {
                "id": "T-7777", "created_by": "alice/one", "claimed_by": "bob/two",
            }, "")
        frontmatter = {"id": _TICKET, "status": "claimed", "claimed_by": claimed_by}
        ticket_store.dump_ticket(path, frontmatter, "## 완료 조건\n- [x] 구현\n")
        verdict = ticket_access._ticket_ownership(home, frontmatter, task=task, user=None)
        assert verdict.ok is accepted
        expected = [] if accepted else [ticket_rules._ownership_rejection(
            "complete", _TICKET, verdict)]
        assert _closer(home, board_py, task=task)._board_complete_problems(_TICKET) == expected


def test_missing_ticket_remains_observation_failure(ownership_home):
    home, path, board_py = ownership_home
    missing = "T-" + "7" * 4
    # 파일명 prefix가 같아도 canonical ID가 다르면 정확 티켓 조회는 실패한다.
    ticket_store.dump_ticket(path.with_name(f"{missing}-other.md"), {"id": _TICKET}, "")
    before = path.read_bytes()
    with pytest.raises(finish_rules._CloseObservationFailure, match=f"{missing} 명세 파일"):
        _closer(home, board_py)._board_complete_problems(missing)
    assert path.read_bytes() == before


def test_observation_exception_is_not_empty_success(ownership_home, monkeypatch):
    home, _path, board_py = ownership_home
    closer = _closer(home, board_py)
    for owner, name, error in (
        (ticket_store, "find_ticket_exact", OSError("lookup failed")),
        (ticket_store, "load_ticket", ValueError("invalid frontmatter")),
        (fs, "read_text_shared", OSError("spec read failed")),
        (ticket_access, "_ticket_ownership", RuntimeError("ownership failed")),
    ):
        def fail(*args, **kwargs):
            raise error

        with monkeypatch.context() as patch:
            patch.setattr(owner, name, fail)
            with pytest.raises(type(error)) as raised:
                closer._board_complete_problems(_TICKET)
            assert raised.value is error


def test_all_three_problem_groups_are_collected(ownership_home):
    home, path, board_py = ownership_home
    frontmatter, body = ticket_store.load_ticket(path)
    frontmatter.update(cluster="C-other", claimed_by=f"bob/{_TASK}")
    ticket_store.dump_ticket(path, frontmatter, body)
    rounds = home.board_root / "tickets/rounds" / _TICKET
    rounds.mkdir(parents=True)
    (rounds / "01-architect.md").write_text("## 설계\n", encoding="utf-8")
    (rounds / "03-code-reviewer.md").write_text("## 리뷰\n", encoding="utf-8")
    spec = path.read_text(encoding="utf-8")
    binding = cluster_binding.cluster_complete_binding_problem(
        home, _TICKET, frontmatter, f"C-{_TICKET}")
    verdict = ticket_access._ticket_ownership(home, frontmatter, task=_TASK, user=None)
    round_problems = board_ticket._round_completion_problems(home, _TICKET, spec)
    assert binding is not None and not verdict.ok and round_problems
    before = tree_snapshot(home.root)
    assert _closer(home, board_py)._board_complete_problems(_TICKET) == [
        f"{_TICKET}: {binding}",
        ticket_rules._ownership_rejection("complete", _TICKET, verdict),
        *(f"{_TICKET}: {problem}" for problem in round_problems),
    ]
    assert tree_snapshot(home.root) == before
