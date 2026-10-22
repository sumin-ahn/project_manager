"""T-0636 신규-생성 축의 값-결속 사용자 승인 게이트.

prefix 3소스 합집합(areas·기발행 티켓·task 장부)/6개 CLI 호출형(new·init·task prefix·
rename·merge·reid)과 물리
worktree add 변형을 hermetic하게 검증한다. 기존 downstream 테스트에는 무차별 ack를
붙이지 않고, 이 파일에서 거부/승인/오결속을 명시적으로 나눈다.
"""
from __future__ import annotations

from pm_engine.cli import board_init
from pm_engine.cli import board_prefix
from pm_engine.cli import board_ticket
from pm_engine.infra import pm_paths

from pm_engine.app import repo_protection
from pm_engine.infra import git

from _pool_seams import bind_pool

import argparse
import contextlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import ensure_product_slot
from pm_engine.domain import ticket_rules
from pm_engine.infra import git, pre_push_hook, ticket_store

from pm_engine.app import areas_registry
from pm_engine.infra import fs
from pm_engine.infra import ticket_store

from pm_engine.infra import lease_store
from pm_engine.infra import board_git

from pm_engine.infra import areas_store


REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def board(tmp_path, monkeypatch):
    project = tmp_path / "project"
    pm = project / ".project_manager"
    wiki = pm / "wiki"
    tickets = wiki / "tickets"
    local = pm / ".local"
    for status in ("open", "claimed", "blocked", "done"):
        (tickets / status).mkdir(parents=True, exist_ok=True)
    (wiki / "log").mkdir(parents=True, exist_ok=True)
    (tickets / "_template.md").write_text(
        "---\nid: T-NNNN\ntitle: <제목>\nstatus: open\n"
        "created: YYYY-MM-DD\nclaimed_by:\nclaimed_at:\ncompleted_at:\n"
        "depends_on: []\nblocks: []\ntouches: []\nestimate: small\ntags: []\n"
        "---\n\n# T-NNNN — <제목>\n\n## 목표\n구현한다.\n",
        encoding="utf-8",
    )
    module = _load(f"board_t0636_{id(tmp_path)}", "board.py")
    overrides = {"REPO": project}
    for name, value in overrides.items():
        monkeypatch.setattr(module, name, value)
    monkeypatch.delenv("PM_SESSION_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_NAME", raising=False)
    monkeypatch.setattr(lease_store, "_git_config_email", lambda _home: None)
    monkeypatch.setattr(pre_push_hook, "install_pre_push_hook", lambda _home: False)
    monkeypatch.setattr(board_git, "_board_git_enabled", lambda _home: False)
    areas_registry.invalidate_known_prefixes_cache()
    module._project = project
    return module


@pytest.fixture
def pm_config(board):
    mod = _load("pm_config_t0636", "pm_config.py")
    mod.REPO = board.REPO
    return mod


class _TaskPool:
    class InvalidTaskName(Exception):
        def __init__(self, name, reason):
            self.name = name
            self.reason = reason
            super().__init__(reason)

    def __init__(self):
        self.stored = []

    def _validate_task_name(self, name, registered_repos=None):
        return None

    def set_task_prefix(self, name, prefix):
        self.stored.append((name, prefix))
        return SimpleNamespace(name=name, prefix=prefix)


# 발행은 `--task` 와 그 task 가 보유한 제품 폴더가 필요하다(T-0962).
_TASK = "gate-task"


def _new_args(prefix: str, user_ack: str | None):
    return argparse.Namespace(
        title="gate", prefix=prefix, user_ack=user_ack, touches=None, depends=None,
        tag=None, estimate="small", design=None, task=_TASK, repo=None, slot=None,
        user=None,
    )


def _new(board, prefix: str, user_ack: str | None) -> int:
    ensure_product_slot(board, task=_TASK)
    return board_ticket.cmd_new(board._home(), _new_args(prefix, user_ack))


def _invoke_prefix_surface(board, pm_config, surface, prefix, user_ack, monkeypatch):
    if surface == "new":
        return _new(board, prefix, user_ack)
    if surface == "init":
        return board_init.cmd_init(board._home(), argparse.Namespace(
            prefix=prefix, user_ack=user_ack, area="area", owner="owner", user=None,
            task=None, repo=None, slot=None,
        ))
    if surface == "task-prefix":
        bind_pool(monkeypatch, _TaskPool())
        return pm_config.cmd_task_prefix(
            argparse.Namespace(
                name="job", value=prefix, user_ack=user_ack,
            ),

        )
    if surface == "reid":
        _seed_ticket(board, "T-0036")
        return board_prefix.cmd_reid(board._home(), argparse.Namespace(
            old_id="T-0036", new_id=f"T-{prefix}-001", user_ack=user_ack,
            dry_run=False, repo=None, slot=None,
        ))
    # relabel gate는 실제 변경 맵이 있을 때만 열린다. source 티켓을 시드해 gate+락 안
    # fresh 재검증까지 실 파이프라인으로 탄다(no-op 승인 불요 정책과 혼동하지 않음).
    _seed_ticket(board, "T-old-001")
    if surface == "rename":
        return board_prefix.cmd_prefix_rename(board._home(), argparse.Namespace(
            src="old", dst=prefix, user_ack=user_ack, dry_run=False,
        ))
    if surface == "merge":
        return board_prefix.cmd_prefix_merge(board._home(), argparse.Namespace(
            sources=["old"], into=prefix, user_ack=user_ack,
            reorder_chronological=False, dry_run=False,
        ))
    raise AssertionError(surface)


@pytest.mark.parametrize(
    "surface", ["new", "init", "task-prefix", "rename", "merge", "reid"],
)
@pytest.mark.parametrize(
    "known,target,user_ack,expected",
    [
        ({"Known"}, "known", None, 0),
        (set(), "fresh", None, 1),
        (set(), "fresh", "fresh", 0),
        (set(), "fresh", "other", 1),
    ],
    ids=["existing-fold", "new-no-ack", "new-bound-ack", "new-wrong-ack"],
)
def test_prefix_surface_matrix_calls_shared_loader(board, pm_config, monkeypatch, capsys, surface, known, target, user_ack, expected):
    calls = []

    def shared_loader(_home, _target=None):
        calls.append(surface)
        return frozenset(known)

    monkeypatch.setattr(areas_registry, "known_prefixes", shared_loader)
    # surface 성공 mutation이 loader monkeypatch의 cache_clear 속성을 요구하지 않게 하고,
    # 이 테스트는 오직 모든 표면이 같은 loader를 실제 호출하는지를 센다.
    monkeypatch.setattr(areas_registry, "invalidate_known_prefixes_cache", lambda: None)

    rc = _invoke_prefix_surface(
        board, pm_config, surface, target, user_ack, monkeypatch,
    )

    assert rc == expected
    # new/init/task-prefix = 선판정 + 락 안 fresh 재판정. rename/merge/reid는 preview용 canonical
    # snapshot + 실제 맵 확인 뒤 선판정 + 락 안 fresh 재판정의 3회다. monkeypatch loader라
    # 프로세스 cache를 의도적으로 우회해 호출 경계를 직접 센다.
    expected_calls = 3 if surface in {"rename", "merge", "reid"} else 2
    assert calls == [surface] * expected_calls
    captured = capsys.readouterr()
    if not known and user_ack == target:
        assert "[승인 감사]" in captured.out and "값-결속" in captured.out
    elif not known:
        assert "1순위: 사용자에게" in captured.err
        assert "현재 카테고리" in captured.err
        assert "부차 수단" in captured.err


def _seed_ticket(board, ticket_id: str):
    path = pm_paths.tickets_dir(board._home()) / "open" / f"{ticket_id}-seed.md"
    ticket_store.dump_ticket(
        path,
        {"id": ticket_id, "title": "seed", "status": "open", "created": "2026-08-11"},
        f"# {ticket_id} — seed\n",
    )
    areas_registry.invalidate_known_prefixes_cache()


def test_known_prefixes_areas_source_alone(board):
    areas_registry.areas_append(board._home(), "AREA", "area", "owner")
    assert areas_registry.known_prefixes(board._home()) == frozenset({"AREA"})


def test_known_prefixes_ticket_source_alone(board):
    _seed_ticket(board, "T-TICKET-001")
    assert areas_registry.known_prefixes(board._home()) == frozenset({"TICKET"})


def test_known_prefixes_task_source_alone(board):
    board._home().local_dir.mkdir(parents=True, exist_ok=True)
    pm_paths.leases_file(board._home()).write_text(
        json.dumps({"leases": [], "tasks": [{"name": "job", "prefix": "TASK"}]}),
        encoding="utf-8",
    )
    assert areas_registry.known_prefixes(board._home()) == frozenset({"TASK"})


def test_known_prefixes_local_conf_prefix_key_is_not_a_source(board):
    """local.conf `prefix=` 는 승인 게이트의 소스가 아니다 (T-0779 · 3소스).

    해소 체인(`id_prefix`)에서 그 층을 지웠으므로 게이트 합집합에도 없어야 한다 — 남아
    있으면 도달 불가능한 clone-local 라벨을 whitelist 한다.
    """
    board._home().local_conf.write_text("prefix=SOLO\n", encoding="utf-8")
    assert areas_registry.known_prefixes(board._home()) == frozenset()


def test_three_source_subset_matrix_single_case_passes_and_split_case_fails(
    board, capsys,
):
    """3소스의 비어 있지 않은 7조합을 실파일로 구성해 canonical 경계를 전수한다.

    같은 ``AAA`` case만 있으면 조합 크기와 무관하게 단일 canonical로 통과한다. 두 case를
    넣은 4개 다중-source 조합은 모두 fail-loud여야 한다(비활성 소스가 없으므로 예외 0).
    """
    sources = ("areas", "tickets", "tasks")

    def reset_sources():
        pm_paths.areas_file(board._home()).unlink(missing_ok=True)
        pm_paths.leases_file(board._home()).unlink(missing_ok=True)
        for status in (*ticket_rules.STATUS_DIRS, ".drafts"):
            status_dir = pm_paths.tickets_dir(board._home()) / status
            if status_dir.exists():
                for path in status_dir.glob("T-*.md"):
                    path.unlink()
        areas_registry.invalidate_known_prefixes_cache()

    def seed_source(source, prefix):
        if source == "areas":
            areas_registry.areas_append(board._home(), prefix, "area", "owner")
        elif source == "tickets":
            _seed_ticket(board, f"T-{prefix}-001")
        else:
            assert source == "tasks"
            board._home().local_dir.mkdir(parents=True, exist_ok=True)
            pm_paths.leases_file(board._home()).write_text(
                json.dumps({
                    "leases": [],
                    "tasks": [{"name": "job", "prefix": prefix}],
                }),
                encoding="utf-8",
            )

    single_case_passes = 0
    split_case_failures = 0
    for mask in range(1, 1 << len(sources)):
        selected = [source for index, source in enumerate(sources) if mask & (1 << index)]

        reset_sources()
        for source in selected:
            seed_source(source, "AAA")
        areas_registry.invalidate_known_prefixes_cache()
        assert areas_registry._prefix_target_snapshot(board._home(), "AaA", surface="matrix") == ("AAA", False)
        single_case_passes += 1
        capsys.readouterr()

        if len(selected) < 2:
            continue
        reset_sources()
        seed_source(selected[0], "AAA")
        for source in selected[1:]:
            seed_source(source, "aaa")
        areas_registry.invalidate_known_prefixes_cache()
        result = areas_registry._prefix_target_snapshot(board._home(), "AaA", surface="matrix")
        captured = capsys.readouterr()

        # 3소스 전부 활성이므로 다중-source split 은 예외 없이 fail-loud 다.
        assert result == (None, False), selected
        assert "case 변형이 2개 이상" in captured.err
        split_case_failures += 1

    assert (single_case_passes, split_case_failures) == (7, 4)


@pytest.mark.parametrize("derived", ["task", "single-area"])
@pytest.mark.parametrize("split_case", [False, True], ids=["single-case", "split-case"])
def test_cmd_new_derived_prefixes_share_canonical_gate(
    board, capsys, derived, split_case,
):
    """task·단일 areas 유도값도 명시 prefix와 같은 판정을 지난다(세션 유도층은 없다·T-0962)."""
    task = None
    repo = None
    slot = None
    if derived == "task":
        from _git_fixture import init_git_repo
        tree = init_git_repo(board.REPO / "work/svc_1", commit="task prefix fixture")
        assert git._git_head_at(str(tree))
        assert git._cluster_current_branch(str(tree)) == "main"
        board._home().local_dir.mkdir(parents=True, exist_ok=True)
        pm_paths.leases_file(board._home()).write_text(
            json.dumps({
                "leases": [{"slot": "work/svc_1", "repo": "svc", "session": "job",
                            "state": "leased", "role": "work"}],
                "tasks": [{"name": "job", "prefix": "AAA"}],
            }),
            encoding="utf-8",
        )
        task = "job"
    else:
        assert derived == "single-area"
        areas_registry.areas_append(board._home(), "AAA", "area", "owner", repo="svc")
        task = _TASK
        ensure_product_slot(board, task=task)

    if split_case:
        _seed_ticket(board, "T-aaa-001")
    areas_registry.invalidate_known_prefixes_cache()
    args = argparse.Namespace(
        title=f"derived-{derived}", prefix=None, user_ack=None,
        touches=None, depends=None, tag=None, estimate="small", design=None,
        task=task, repo=repo, slot=slot, user=None,
    )

    rc = board_ticket.cmd_new(board._home(), args)
    ids = {row["id"] for row in areas_registry._scan_prefix_tickets(board._home())}
    captured = capsys.readouterr()

    if split_case:
        assert rc == 1
        assert ids == {"T-aaa-001"}
        assert "case 변형이 2개 이상" in captured.err
        assert "'AAA'" in captured.err and "'aaa'" in captured.err
    else:
        assert rc == 0
        assert ids == {"T-AAA-001"}
        assert "case 변형이 2개 이상" not in captured.err
        if derived == "task":
            fm, _ = ticket_store.load_ticket(ticket_store.find_ticket_exact(board._home(), "T-AAA-001")[1])
            assert fm["created_by"].split("/")[-1] == "job"


def test_known_prefixes_ignores_local_conf_prefix_when_area_registered(board):
    board._home().local_conf.write_text("prefix=SOLO\n", encoding="utf-8")
    areas_registry.areas_append(board._home(), "AREA", "area", "owner")
    assert areas_registry.known_prefixes(board._home()) == frozenset({"AREA"})


def test_known_prefixes_corrupt_task_ledger_fails_loud(board, capsys):
    board._home().local_dir.mkdir(parents=True, exist_ok=True)
    pm_paths.leases_file(board._home()).write_text('{"tasks": {}}', encoding="utf-8")
    assert areas_registry.require_prefix_user_ack(board._home(), "fresh", "fresh", surface="test") is None
    assert "신뢰할 수 없다" in capsys.readouterr().err


def test_known_prefixes_unreadable_ticket_fails_loud(board, capsys):
    broken = pm_paths.tickets_dir(board._home()) / "open" / "T-BROKEN-001-bad.md"
    broken.write_text("---\nid: [unterminated\n---\n", encoding="utf-8")
    assert areas_registry.require_prefix_user_ack(board._home(), "fresh", "fresh", surface="test") is None
    err = capsys.readouterr().err
    assert "읽지 못한 티켓" in err and "clobber" in err


def test_areas_ticket_cross_case_conflict_scans_all_sources_and_blocks_new(board, monkeypatch, capsys):
    """areas fold-match가 있어도 티켓을 읽어 교차 case 오염을 발견하고 쓰기 0으로 막는다."""
    areas_registry.areas_append(board._home(), "AAA", "area", "owner")
    _seed_ticket(board, "T-aaa-001")
    real_scan = areas_registry._scan_prefix_tickets
    calls = {"n": 0}

    def counted_scan(_home, skipped=None):
        calls["n"] += 1
        return real_scan(_home, skipped)

    monkeypatch.setattr(areas_registry, "_scan_prefix_tickets", counted_scan)
    assert _new(board, "AaA", "AaA") == 1
    assert calls["n"] == 1
    assert {
        row["id"] for row in areas_registry._scan_prefix_tickets(board._home())
    } == {"T-aaa-001"}
    err = capsys.readouterr().err
    assert "case 변형이 2개 이상" in err
    assert "'AAA' ← areas.md" in err
    assert "'aaa' ← 기발행 티켓" in err


def test_known_prefixes_process_cache_and_explicit_invalidation(board, monkeypatch):
    calls = {"n": 0}
    real_scan = areas_registry._scan_prefix_tickets

    def counted_scan(_home, skipped=None):
        calls["n"] += 1
        return real_scan(_home, skipped)

    monkeypatch.setattr(areas_registry, "_scan_prefix_tickets", counted_scan)
    assert areas_registry.known_prefixes(board._home()) == frozenset()
    assert areas_registry.known_prefixes(board._home()) == frozenset()
    assert calls["n"] == 1
    areas_registry.invalidate_known_prefixes_cache()
    assert areas_registry.known_prefixes(board._home()) == frozenset()
    assert calls["n"] == 2


def test_areas_mutation_invalidates_known_prefix_cache(board):
    assert areas_registry.known_prefixes(board._home()) == frozenset()
    areas_registry.areas_append(board._home(), "LATER", "area", "owner")
    assert areas_registry.known_prefixes(board._home()) == frozenset({"LATER"})


def test_case_variant_conflict_is_deterministic_and_lists_every_source(board, monkeypatch, capsys):
    """areas×티켓 case 변형은 순서 무관으로 전부+출처를 내고 막는다."""
    areas_registry.areas_append(board._home(), "AAA", "area", "owner")
    _seed_ticket(board, "T-aaa-001")
    # source map은 실제 4소스 snapshot으로 먼저 고정한다. known_prefixes만 서로 다른 순서의
    # iterable로 흔들어, require가 set 순회의 첫 매치를 canonical로 택하지 않음을 검증한다.
    areas_registry._known_prefix_inventory(board._home())
    real_known_prefixes = areas_registry.known_prefixes

    class ShuffledPool(set):
        def __init__(self, values):
            super().__init__(values)
            self.values = values

        def __iter__(self):
            return iter(self.values)

    outputs = []
    for values in (("AAA", "aaa"), ("aaa", "AAA"), ("AAA", "aaa")):
        monkeypatch.setattr(
            areas_registry, "known_prefixes",
            lambda _home, _target=None, values=values: ShuffledPool(values),
        )
        assert areas_registry.require_prefix_user_ack(board._home(), "AaA", "AaA", surface="test") is None
        outputs.append(capsys.readouterr().err)

    assert outputs[0] == outputs[1] == outputs[2]
    err = outputs[0]
    assert "'AAA' ← areas.md" in err
    assert "'aaa' ← 기발행 티켓" in err
    assert "정리 처방" in err and "임의 변형 선택" in err

    monkeypatch.setattr(areas_registry, "known_prefixes", real_known_prefixes)
    assert _new(board, "AaA", "AaA") == 1
    assert "case 변형이 2개 이상" in capsys.readouterr().err


def test_cheap_case_conflict_preserves_known_sources_when_ticket_scan_is_broken(
    board, capsys,
):
    """전체 inventory가 손상 티켓으로 실패해도 값싼 출처 진단은 `소스 미상`으로 퇴행하지 않는다."""
    areas_registry.areas_append(board._home(), "AAA", "area", "owner")
    board._home().local_dir.mkdir(parents=True, exist_ok=True)
    pm_paths.leases_file(board._home()).write_text(
        json.dumps({"leases": [], "tasks": [{"name": "job", "prefix": "aaa"}]}),
        encoding="utf-8",
    )
    broken = pm_paths.tickets_dir(board._home()) / "open" / "T-BROKEN-001-bad.md"
    broken.write_text("---\nid: [unterminated\n---\n", encoding="utf-8")
    areas_registry.invalidate_known_prefixes_cache()

    assert areas_registry.require_prefix_user_ack(board._home(), "AaA", "AaA", surface="test") is None

    err = capsys.readouterr().err
    assert "case 변형이 2개 이상" in err
    assert "'AAA' ← areas.md" in err
    assert "'aaa' ← task 장부" in err
    assert "소스 미상" not in err
    assert "읽지 못한 티켓" in err


def _seed_ticket_without_cache_invalidation(board, ticket_id: str):
    """다른 프로세스가 현재 프로세스 cache를 모른 채 티켓을 만든 TOCTOU 형상."""
    path = pm_paths.tickets_dir(board._home()) / "open" / f"{ticket_id}-race.md"
    ticket_store.dump_ticket(
        path,
        {"id": ticket_id, "title": "race", "status": "open", "created": "2026-08-11"},
        f"# {ticket_id} — race\n",
    )


@pytest.mark.parametrize(
    "surface", ["new", "init", "task-prefix", "rename", "merge", "reid"],
)
def test_prefix_writes_revalidate_fresh_snapshot_inside_lock(board, pm_config, monkeypatch, capsys, surface):
    """선판정 뒤 다른 case가 생기면 cache를 버린 락 안 재판정이 모든 쓰기 표면을 막는다."""
    pool = bind_pool(monkeypatch, _TaskPool())
    if surface in {"rename", "merge", "reid"}:
        _seed_ticket(board, "T-old-001")

    real_lock = fs.board_lock
    injected = {"done": False}

    @contextlib.contextmanager
    def racing_lock(_home):
        with real_lock(_home):
            if not injected["done"]:
                injected["done"] = True
                # 선판정은 `fresh`를 신규 canonical로 봤다. 다른 세션이 `FRESH`를 만든 뒤에도
                # 현 프로세스 cache는 stale인 채다 — 락 안 명시 무효화가 없으면 write가 진행된다.
                _seed_ticket_without_cache_invalidation(board, "T-FRESH-001")
            yield

    monkeypatch.setattr(fs, "board_lock", racing_lock)

    if surface == "new":
        rc = _new(board, "fresh", "fresh")
    elif surface == "init":
        rc = board_init.cmd_init(board._home(), argparse.Namespace(
            prefix="fresh", user_ack="fresh", area="area", owner="owner", user=None,
            task=None, repo=None, slot=None,
        ))
    elif surface == "task-prefix":
        bind_pool(monkeypatch, pool)
        rc = pm_config.cmd_task_prefix(
            argparse.Namespace(name="job", value="fresh", user_ack="fresh"),

        )
    elif surface == "rename":
        rc = board_prefix.cmd_prefix_rename(board._home(), argparse.Namespace(
            src="old", dst="fresh", user_ack="fresh", dry_run=False,
        ))
    elif surface == "merge":
        rc = board_prefix.cmd_prefix_merge(board._home(), argparse.Namespace(
            sources=["old"], into="fresh", user_ack="fresh",
            reorder_chronological=False, dry_run=False,
        ))
    else:
        rc = board_prefix.cmd_reid(board._home(), argparse.Namespace(
            old_id="T-old-001", new_id="T-fresh-001", user_ack="fresh",
            dry_run=False, repo=None, slot=None,
        ))

    assert rc == 1
    assert injected["done"]
    assert pool.stored == []
    ids = {
        row["id"] for row in areas_registry._scan_prefix_tickets(board._home())
    }
    assert ids == ({"T-FRESH-001", "T-old-001"}
                   if surface in {"rename", "merge", "reid"} else {"T-FRESH-001"})
    assert not pm_paths.areas_file(board._home()).exists()
    assert not board._home().local_conf.exists()
    err = capsys.readouterr().err
    assert "선판정 'fresh'" in err and "fresh snapshot 'FRESH'" in err
    assert "쓰기 0" in err


@pytest.mark.parametrize("surface", ["rename", "merge", "reid"])
def test_relabel_dry_run_needs_no_ack_but_reports_execution_requirement(board, monkeypatch, capsys, surface):
    _seed_ticket(board, "T-old-001")

    @contextlib.contextmanager
    def forbidden_lock(_home):
        raise AssertionError("dry-run must not acquire board_lock")
        yield

    monkeypatch.setattr(fs, "board_lock", forbidden_lock)
    if surface == "rename":
        rc = board_prefix.cmd_prefix_rename(board._home(), argparse.Namespace(
            src="old", dst="fresh", user_ack=None, dry_run=True,
        ))
    elif surface == "merge":
        rc = board_prefix.cmd_prefix_merge(board._home(), argparse.Namespace(
            sources=["old"], into="fresh", user_ack=None,
            reorder_chronological=False, dry_run=True,
        ))
    else:
        rc = board_prefix.cmd_reid(board._home(), argparse.Namespace(
            old_id="T-old-001", new_id="T-fresh-001", user_ack=None,
            dry_run=True, repo=None, slot=None,
        ))

    assert rc == 0
    assert {row["id"] for row in areas_registry._scan_prefix_tickets(board._home())} == {"T-old-001"}
    out = capsys.readouterr().out
    assert "[dry-run]" in out and "실행 시 새 prefix 'fresh'" in out
    assert "사용자 승인이 필요" in out


@pytest.mark.parametrize("surface", ["rename", "merge"])
def test_relabel_empty_map_noop_needs_no_ack_or_lock(board, monkeypatch, capsys, surface):
    @contextlib.contextmanager
    def forbidden_lock(_home):
        raise AssertionError("empty-map no-op must not acquire board_lock")
        yield

    monkeypatch.setattr(fs, "board_lock", forbidden_lock)
    if surface == "rename":
        rc = board_prefix.cmd_prefix_rename(board._home(), argparse.Namespace(
            src="ghost", dst="fresh", user_ack=None, dry_run=False,
        ))
    else:
        rc = board_prefix.cmd_prefix_merge(board._home(), argparse.Namespace(
            sources=["ghost"], into="fresh", user_ack=None,
            reorder_chronological=False, dry_run=False,
        ))

    assert rc == 0
    captured = capsys.readouterr()
    assert "변경 없음" in captured.out
    assert "사용자 명시 승인" not in captured.err


class _SlotPool:
    class SlotFoldersRefused(RuntimeError):
        pass

    def slot_folders(self, slot, **kwargs):
        return getattr(self, "last_folders", None)

    class InvalidTaskName(Exception):
        def __init__(self, name, reason):
            self.name = name
            self.reason = reason
            super().__init__(reason)

    def __init__(self, bare_root=None):
        self.created = []
        # 그 repo 의 공유 bare 자리([[T-0903]]) — `cmd_worktree_add` 가 사본 루트 ignore 규칙을
        # 심는다. 실 `.repos` 로 새지 않게 테스트가 준 임시 루트 안에서만 답한다.
        self.bare_root = bare_root

    def bare_repo_path(self, repo):
        assert self.bare_root is not None, "bare_root 미설정 — 실 `.repos` 를 건드릴 뻔했다"
        path = Path(self.bare_root) / f"{repo}.git"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _validate_task_name(self, name, registered_repos=None):
        return None

    def find_task(self, name):
        return SimpleNamespace(name=name, prefix=None)

    def create_slot(self, repo, *, base, test_cmd=None, readonly=False, owner_task=None, folders=None):
        role = "readonly" if readonly else "work"
        # 확정 상태는 엔진과 같은 세 갈래 — task 명의 leased · readonly 무소유 leased · 그 밖 무소유 idle.
        state = "leased" if (readonly or owner_task is not None) else "idle"
        lease = SimpleNamespace(
            slot=f"work/{repo}_1", repo=repo, state=state, role=role,
            session=owner_task if owner_task is not None else "",
            pid=None if readonly else 123, test_cmd=test_cmd,
        )
        self.created.append((repo, base, test_cmd, readonly, owner_task, lease))
        return lease

    def slot_path(self, slot):
        return Path("/tmp") / slot

    def find_task_strict(self, value):
        return self.find_task(value)


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"readonly": True},
        {"task": "job"},
    ],
    ids=["normal", "readonly", "task"],
)
def test_worktree_add_variants_reject_without_ack(pm_config, monkeypatch, extra, capsys):
    pool = bind_pool(monkeypatch, _SlotPool())
    args = argparse.Namespace(repo="svc", test=None, user_ack=None, **extra)
    bind_pool(monkeypatch, pool)
    assert pm_config.cmd_worktree_add(args) == 1
    assert pool.created == []
    assert "1순위: 사용자에게" in capsys.readouterr().err


def test_worktree_add_rejects_ack_bound_to_other_repo(pm_config, monkeypatch, capsys):
    pool = bind_pool(monkeypatch, _SlotPool())
    args = argparse.Namespace(repo="svc", test=None, user_ack="other")
    bind_pool(monkeypatch, pool)
    assert pm_config.cmd_worktree_add(args) == 1
    assert pool.created == []
    assert "결속되지 않았다" in capsys.readouterr().err


@pytest.mark.parametrize(
    "readonly,task,expected_role,expected_state",
    [
        (False, None, "work", "idle"),
        (True, None, "readonly", "leased"),
        (False, "job", "work", "leased"),
    ],
    ids=["normal", "readonly-role", "task-leased"],
)
def test_worktree_add_bound_ack_preserves_create_slot_semantics(pm_config, monkeypatch, capsys, tmp_path, readonly, task, expected_role, expected_state):
    pool = bind_pool(monkeypatch, _SlotPool(bare_root=tmp_path / ".repos"))
    monkeypatch.setattr(repo_protection, "_install_protected_hook_reporting", lambda *a, **k: True)
    monkeypatch.setattr(pm_config, "_render_task_slots", lambda *a, **k: None)
    args = argparse.Namespace(
        repo="svc", test=None, readonly=readonly, task=task, user_ack="svc",
    )
    # 슬롯 생성은 repo 등록 base 를 부모로 기록한다 — 그 base 만 답하는 board.
    monkeypatch.setattr(areas_store, "_repo_base", lambda _home, repo: {"svc": "main"}.get(repo))

    bind_pool(monkeypatch, pool)
    assert pm_config.cmd_worktree_add(
        args,  is_tty=lambda: False,
    ) == 0

    assert len(pool.created) == 1
    repo, base, test_cmd, got_readonly, owner_task, lease = pool.created[0]
    assert (repo, base, test_cmd, got_readonly, owner_task) == (
        "svc", "main", None, readonly, task,
    )
    assert lease.state == expected_state and lease.role == expected_role
    assert "[승인 감사]" in capsys.readouterr().out
