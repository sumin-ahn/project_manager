"""T-0636 신규-생성 축의 값-결속 사용자 승인 게이트.

prefix 3소스 합집합(areas·기발행 티켓·task 장부)/6개 CLI 호출형(new·init·task prefix·
rename·merge·reid)과 물리
worktree add 변형을 hermetic하게 검증한다. 기존 downstream 테스트에는 무차별 ack를
붙이지 않고, 이 파일에서 거부/승인/오결속을 명시적으로 나눈다.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


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
    overrides = {
        "REPO": project,
        "TICKETS_DIR": tickets,
        "TEMPLATE_FILE": tickets / "_template.md",
        "BOARD_FILE": wiki / "board.md",
        "LOG_FILE": wiki / "log" / "current.md",
        "STATUS_FILE": wiki / "status.md",
        "LOCAL_CONF": pm / "local.conf",
        "AREAS_FILE": pm / "areas.md",
        "LOCAL_DIR": local,
        "LEASES_FILE": local / "worktree-leases.json",
        "BOARD_LOCK": local / "board.lock",
        "PM_STATE_FILE": wiki / "pm_state.md",
        "PM_STATE_TEMPLATE": wiki / "pm_state.template.md",
    }
    for name, value in overrides.items():
        monkeypatch.setattr(module, name, value)
    monkeypatch.delenv("PM_SESSION_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_NAME", raising=False)
    monkeypatch.setattr(module, "_git_config_email", lambda: None)
    monkeypatch.setattr(module, "install_pre_push_hook", lambda: False)
    monkeypatch.setattr(module, "_board_git_enabled", lambda: False)
    module.invalidate_known_prefixes_cache()
    module._project = project
    return module


@pytest.fixture
def pm_config():
    return _load("pm_config_t0636", "pm_config.py")


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


def _new_args(prefix: str, user_ack: str | None):
    return argparse.Namespace(
        title="gate", prefix=prefix, user_ack=user_ack, touches=None, depends=None,
        tag=None, estimate="small", design=None, task=None, repo=None, slot=None,
        user=None,
    )


def _invoke_prefix_surface(board, pm_config, surface, prefix, user_ack, monkeypatch):
    if surface == "new":
        return board.cmd_new(_new_args(prefix, user_ack))
    if surface == "init":
        return board.cmd_init(argparse.Namespace(
            prefix=prefix, user_ack=user_ack, area="area", owner="owner", user=None,
            task=None, repo=None, slot=None,
        ))
    if surface == "task-prefix":
        return pm_config.cmd_task_prefix(
            argparse.Namespace(
                name="job", value=prefix, user_ack=user_ack,
            ),
            worktree_pool=_TaskPool(),
            board=board,
        )
    if surface == "reid":
        _seed_ticket(board, "T-0036")
        return board.cmd_reid(argparse.Namespace(
            old_id="T-0036", new_id=f"T-{prefix}-001", user_ack=user_ack,
            dry_run=False, repo=None, slot=None,
        ))
    # relabel gate는 실제 변경 맵이 있을 때만 열린다. source 티켓을 시드해 gate+락 안
    # fresh 재검증까지 실 파이프라인으로 탄다(no-op 승인 불요 정책과 혼동하지 않음).
    _seed_ticket(board, "T-old-001")
    if surface == "rename":
        return board.cmd_prefix_rename(argparse.Namespace(
            src="old", dst=prefix, user_ack=user_ack, dry_run=False,
        ))
    if surface == "merge":
        return board.cmd_prefix_merge(argparse.Namespace(
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
def test_prefix_surface_matrix_calls_shared_loader(
    board, pm_config, monkeypatch, capsys, surface, known, target, user_ack, expected,
):
    calls = []

    def shared_loader(_target=None):
        calls.append(surface)
        return frozenset(known)

    monkeypatch.setattr(board, "known_prefixes", shared_loader)
    # surface 성공 mutation이 loader monkeypatch의 cache_clear 속성을 요구하지 않게 하고,
    # 이 테스트는 오직 모든 표면이 같은 loader를 실제 호출하는지를 센다.
    monkeypatch.setattr(board, "invalidate_known_prefixes_cache", lambda: None)

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
    path = board.TICKETS_DIR / "open" / f"{ticket_id}-seed.md"
    board.dump_ticket(
        path,
        {"id": ticket_id, "title": "seed", "status": "open", "created": "2026-08-11"},
        f"# {ticket_id} — seed\n",
    )
    board.invalidate_known_prefixes_cache()


def test_known_prefixes_areas_source_alone(board):
    board.areas_append("AREA", "area", "owner")
    assert board.known_prefixes() == frozenset({"AREA"})


def test_known_prefixes_ticket_source_alone(board):
    _seed_ticket(board, "T-TICKET-001")
    assert board.known_prefixes() == frozenset({"TICKET"})


def test_known_prefixes_task_source_alone(board):
    board.LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    board.LEASES_FILE.write_text(
        json.dumps({"leases": [], "tasks": [{"name": "job", "prefix": "TASK"}]}),
        encoding="utf-8",
    )
    assert board.known_prefixes() == frozenset({"TASK"})


def test_known_prefixes_local_conf_prefix_key_is_not_a_source(board):
    """local.conf `prefix=` 는 승인 게이트의 소스가 아니다 (T-0779 · 3소스).

    해소 체인(`id_prefix`)에서 그 층을 지웠으므로 게이트 합집합에도 없어야 한다 — 남아
    있으면 도달 불가능한 clone-local 라벨을 whitelist 한다.
    """
    board.LOCAL_CONF.write_text("prefix=SOLO\n", encoding="utf-8")
    assert board.known_prefixes() == frozenset()


def test_three_source_subset_matrix_single_case_passes_and_split_case_fails(
    board, capsys,
):
    """3소스의 비어 있지 않은 7조합을 실파일로 구성해 canonical 경계를 전수한다.

    같은 ``AAA`` case만 있으면 조합 크기와 무관하게 단일 canonical로 통과한다. 두 case를
    넣은 4개 다중-source 조합은 모두 fail-loud여야 한다(비활성 소스가 없으므로 예외 0).
    """
    sources = ("areas", "tickets", "tasks")

    def reset_sources():
        board.AREAS_FILE.unlink(missing_ok=True)
        board.LEASES_FILE.unlink(missing_ok=True)
        for status in (*board.STATUS_DIRS, ".drafts"):
            status_dir = board.TICKETS_DIR / status
            if status_dir.exists():
                for path in status_dir.glob("T-*.md"):
                    path.unlink()
        board.invalidate_known_prefixes_cache()

    def seed_source(source, prefix):
        if source == "areas":
            board.areas_append(prefix, "area", "owner")
        elif source == "tickets":
            _seed_ticket(board, f"T-{prefix}-001")
        else:
            assert source == "tasks"
            board.LOCAL_DIR.mkdir(parents=True, exist_ok=True)
            board.LEASES_FILE.write_text(
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
        board.invalidate_known_prefixes_cache()
        assert board._prefix_target_snapshot("AaA", surface="matrix") == ("AAA", False)
        single_case_passes += 1
        capsys.readouterr()

        if len(selected) < 2:
            continue
        reset_sources()
        seed_source(selected[0], "AAA")
        for source in selected[1:]:
            seed_source(source, "aaa")
        board.invalidate_known_prefixes_cache()
        result = board._prefix_target_snapshot("AaA", surface="matrix")
        captured = capsys.readouterr()

        # 3소스 전부 활성이므로 다중-source split 은 예외 없이 fail-loud 다.
        assert result == (None, False), selected
        assert "case 변형이 2개 이상" in captured.err
        split_case_failures += 1

    assert (single_case_passes, split_case_failures) == (7, 4)


@pytest.mark.parametrize("derived", ["task", "session", "single-area"])
@pytest.mark.parametrize("split_case", [False, True], ids=["single-case", "split-case"])
def test_cmd_new_derived_prefixes_share_canonical_gate(
    board, capsys, derived, split_case,
):
    """task·세션·단일 areas 유도값도 명시 prefix와 같은 판정을 지난다."""
    task = None
    repo = None
    slot = None
    if derived == "task":
        board.LOCAL_DIR.mkdir(parents=True, exist_ok=True)
        board.LEASES_FILE.write_text(
            json.dumps({
                "leases": [],
                "tasks": [{"name": "job", "prefix": "AAA"}],
            }),
            encoding="utf-8",
        )
        task = "job"
    elif derived == "session":
        # 두 repo를 등록해 count-based 경로를 배제하고 명시 slot→repo→areas 유도를 강제한다.
        board.areas_append("AAA", "area", "owner", repo="svc")
        board.areas_append("OTHER", "other", "owner", repo="other")
        repo = "svc"
        slot = 1
    else:
        assert derived == "single-area"
        board.areas_append("AAA", "area", "owner", repo="svc")

    if split_case:
        _seed_ticket(board, "T-aaa-001")
    board.invalidate_known_prefixes_cache()
    args = argparse.Namespace(
        title=f"derived-{derived}", prefix=None, user_ack=None,
        touches=None, depends=None, tag=None, estimate="small", design=None,
        task=task, repo=repo, slot=slot, user=None,
    )

    rc = board.cmd_new(args)
    ids = {row["id"] for row in board._scan_prefix_tickets()}
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


def test_known_prefixes_ignores_local_conf_prefix_when_area_registered(board):
    board.LOCAL_CONF.write_text("prefix=SOLO\n", encoding="utf-8")
    board.areas_append("AREA", "area", "owner")
    assert board.known_prefixes() == frozenset({"AREA"})


def test_known_prefixes_corrupt_task_ledger_fails_loud(board, capsys):
    board.LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    board.LEASES_FILE.write_text('{"tasks": {}}', encoding="utf-8")
    assert board.require_prefix_user_ack("fresh", "fresh", surface="test") is None
    assert "신뢰할 수 없다" in capsys.readouterr().err


def test_known_prefixes_unreadable_ticket_fails_loud(board, capsys):
    broken = board.TICKETS_DIR / "open" / "T-BROKEN-001-bad.md"
    broken.write_text("---\nid: [unterminated\n---\n", encoding="utf-8")
    assert board.require_prefix_user_ack("fresh", "fresh", surface="test") is None
    err = capsys.readouterr().err
    assert "읽지 못한 티켓" in err and "clobber" in err


def test_areas_ticket_cross_case_conflict_scans_all_sources_and_blocks_new(
    board, monkeypatch, capsys,
):
    """areas fold-match가 있어도 티켓을 읽어 교차 case 오염을 발견하고 쓰기 0으로 막는다."""
    board.areas_append("AAA", "area", "owner")
    _seed_ticket(board, "T-aaa-001")
    real_scan = board._scan_prefix_tickets
    calls = {"n": 0}

    def counted_scan(skipped=None):
        calls["n"] += 1
        return real_scan(skipped)

    monkeypatch.setattr(board, "_scan_prefix_tickets", counted_scan)
    assert board.cmd_new(_new_args("AaA", "AaA")) == 1
    assert calls["n"] == 1
    assert {
        row["id"] for row in board._scan_prefix_tickets()
    } == {"T-aaa-001"}
    err = capsys.readouterr().err
    assert "case 변형이 2개 이상" in err
    assert "'AAA' ← areas.md" in err
    assert "'aaa' ← 기발행 티켓" in err


def test_known_prefixes_process_cache_and_explicit_invalidation(board, monkeypatch):
    calls = {"n": 0}
    real_scan = board._scan_prefix_tickets

    def counted_scan(skipped=None):
        calls["n"] += 1
        return real_scan(skipped)

    monkeypatch.setattr(board, "_scan_prefix_tickets", counted_scan)
    assert board.known_prefixes() == frozenset()
    assert board.known_prefixes() == frozenset()
    assert calls["n"] == 1
    board.invalidate_known_prefixes_cache()
    assert board.known_prefixes() == frozenset()
    assert calls["n"] == 2


def test_areas_mutation_invalidates_known_prefix_cache(board):
    assert board.known_prefixes() == frozenset()
    board.areas_append("LATER", "area", "owner")
    assert board.known_prefixes() == frozenset({"LATER"})


def test_case_variant_conflict_is_deterministic_and_lists_every_source(
    board, monkeypatch, capsys,
):
    """areas×티켓 case 변형은 순서 무관으로 전부+출처를 내고 막는다."""
    board.areas_append("AAA", "area", "owner")
    _seed_ticket(board, "T-aaa-001")
    # source map은 실제 4소스 snapshot으로 먼저 고정한다. known_prefixes만 서로 다른 순서의
    # iterable로 흔들어, require가 set 순회의 첫 매치를 canonical로 택하지 않음을 검증한다.
    board._known_prefix_inventory()
    real_known_prefixes = board.known_prefixes

    class ShuffledPool(set):
        def __init__(self, values):
            super().__init__(values)
            self.values = values

        def __iter__(self):
            return iter(self.values)

    outputs = []
    for values in (("AAA", "aaa"), ("aaa", "AAA"), ("AAA", "aaa")):
        monkeypatch.setattr(
            board, "known_prefixes",
            lambda _target=None, values=values: ShuffledPool(values),
        )
        assert board.require_prefix_user_ack("AaA", "AaA", surface="test") is None
        outputs.append(capsys.readouterr().err)

    assert outputs[0] == outputs[1] == outputs[2]
    err = outputs[0]
    assert "'AAA' ← areas.md" in err
    assert "'aaa' ← 기발행 티켓" in err
    assert "정리 처방" in err and "임의 변형 선택" in err

    monkeypatch.setattr(board, "known_prefixes", real_known_prefixes)
    assert board.cmd_new(_new_args("AaA", "AaA")) == 1
    assert "case 변형이 2개 이상" in capsys.readouterr().err


def test_cheap_case_conflict_preserves_known_sources_when_ticket_scan_is_broken(
    board, capsys,
):
    """전체 inventory가 손상 티켓으로 실패해도 값싼 출처 진단은 `소스 미상`으로 퇴행하지 않는다."""
    board.areas_append("AAA", "area", "owner")
    board.LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    board.LEASES_FILE.write_text(
        json.dumps({"leases": [], "tasks": [{"name": "job", "prefix": "aaa"}]}),
        encoding="utf-8",
    )
    broken = board.TICKETS_DIR / "open" / "T-BROKEN-001-bad.md"
    broken.write_text("---\nid: [unterminated\n---\n", encoding="utf-8")
    board.invalidate_known_prefixes_cache()

    assert board.require_prefix_user_ack("AaA", "AaA", surface="test") is None

    err = capsys.readouterr().err
    assert "case 변형이 2개 이상" in err
    assert "'AAA' ← areas.md" in err
    assert "'aaa' ← task 장부" in err
    assert "소스 미상" not in err
    assert "읽지 못한 티켓" in err


def _seed_ticket_without_cache_invalidation(board, ticket_id: str):
    """다른 프로세스가 현재 프로세스 cache를 모른 채 티켓을 만든 TOCTOU 형상."""
    path = board.TICKETS_DIR / "open" / f"{ticket_id}-race.md"
    board.dump_ticket(
        path,
        {"id": ticket_id, "title": "race", "status": "open", "created": "2026-08-11"},
        f"# {ticket_id} — race\n",
    )


@pytest.mark.parametrize(
    "surface", ["new", "init", "task-prefix", "rename", "merge", "reid"],
)
def test_prefix_writes_revalidate_fresh_snapshot_inside_lock(
    board, pm_config, monkeypatch, capsys, surface,
):
    """선판정 뒤 다른 case가 생기면 cache를 버린 락 안 재판정이 모든 쓰기 표면을 막는다."""
    pool = _TaskPool()
    if surface in {"rename", "merge", "reid"}:
        _seed_ticket(board, "T-old-001")

    real_lock = board.board_lock
    injected = {"done": False}

    @contextlib.contextmanager
    def racing_lock():
        with real_lock():
            if not injected["done"]:
                injected["done"] = True
                # 선판정은 `fresh`를 신규 canonical로 봤다. 다른 세션이 `FRESH`를 만든 뒤에도
                # 현 프로세스 cache는 stale인 채다 — 락 안 명시 무효화가 없으면 write가 진행된다.
                _seed_ticket_without_cache_invalidation(board, "T-FRESH-001")
            yield

    monkeypatch.setattr(board, "board_lock", racing_lock)

    if surface == "new":
        rc = board.cmd_new(_new_args("fresh", "fresh"))
    elif surface == "init":
        rc = board.cmd_init(argparse.Namespace(
            prefix="fresh", user_ack="fresh", area="area", owner="owner", user=None,
            task=None, repo=None, slot=None,
        ))
    elif surface == "task-prefix":
        rc = pm_config.cmd_task_prefix(
            argparse.Namespace(name="job", value="fresh", user_ack="fresh"),
            worktree_pool=pool,
            board=board,
        )
    elif surface == "rename":
        rc = board.cmd_prefix_rename(argparse.Namespace(
            src="old", dst="fresh", user_ack="fresh", dry_run=False,
        ))
    elif surface == "merge":
        rc = board.cmd_prefix_merge(argparse.Namespace(
            sources=["old"], into="fresh", user_ack="fresh",
            reorder_chronological=False, dry_run=False,
        ))
    else:
        rc = board.cmd_reid(argparse.Namespace(
            old_id="T-old-001", new_id="T-fresh-001", user_ack="fresh",
            dry_run=False, repo=None, slot=None,
        ))

    assert rc == 1
    assert injected["done"]
    assert pool.stored == []
    ids = {
        row["id"] for row in board._scan_prefix_tickets()
    }
    assert ids == ({"T-FRESH-001", "T-old-001"}
                   if surface in {"rename", "merge", "reid"} else {"T-FRESH-001"})
    assert not board.AREAS_FILE.exists()
    assert not board.LOCAL_CONF.exists()
    err = capsys.readouterr().err
    assert "선판정 'fresh'" in err and "fresh snapshot 'FRESH'" in err
    assert "쓰기 0" in err


@pytest.mark.parametrize("surface", ["rename", "merge", "reid"])
def test_relabel_dry_run_needs_no_ack_but_reports_execution_requirement(
    board, monkeypatch, capsys, surface,
):
    _seed_ticket(board, "T-old-001")

    @contextlib.contextmanager
    def forbidden_lock():
        raise AssertionError("dry-run must not acquire board_lock")
        yield

    monkeypatch.setattr(board, "board_lock", forbidden_lock)
    if surface == "rename":
        rc = board.cmd_prefix_rename(argparse.Namespace(
            src="old", dst="fresh", user_ack=None, dry_run=True,
        ))
    elif surface == "merge":
        rc = board.cmd_prefix_merge(argparse.Namespace(
            sources=["old"], into="fresh", user_ack=None,
            reorder_chronological=False, dry_run=True,
        ))
    else:
        rc = board.cmd_reid(argparse.Namespace(
            old_id="T-old-001", new_id="T-fresh-001", user_ack=None,
            dry_run=True, repo=None, slot=None,
        ))

    assert rc == 0
    assert {row["id"] for row in board._scan_prefix_tickets()} == {"T-old-001"}
    out = capsys.readouterr().out
    assert "[dry-run]" in out and "실행 시 새 prefix 'fresh'" in out
    assert "사용자 승인이 필요" in out


@pytest.mark.parametrize("surface", ["rename", "merge"])
def test_relabel_empty_map_noop_needs_no_ack_or_lock(
    board, monkeypatch, capsys, surface,
):
    @contextlib.contextmanager
    def forbidden_lock():
        raise AssertionError("empty-map no-op must not acquire board_lock")
        yield

    monkeypatch.setattr(board, "board_lock", forbidden_lock)
    if surface == "rename":
        rc = board.cmd_prefix_rename(argparse.Namespace(
            src="ghost", dst="fresh", user_ack=None, dry_run=False,
        ))
    else:
        rc = board.cmd_prefix_merge(argparse.Namespace(
            sources=["ghost"], into="fresh", user_ack=None,
            reorder_chronological=False, dry_run=False,
        ))

    assert rc == 0
    captured = capsys.readouterr()
    assert "변경 없음" in captured.out
    assert "사용자 명시 승인" not in captured.err


class _SlotPool:
    class InvalidTaskName(Exception):
        def __init__(self, name, reason):
            self.name = name
            self.reason = reason
            super().__init__(reason)

    def __init__(self):
        self.created = []

    def _validate_task_name(self, name, registered_repos=None):
        return None

    def find_task(self, name):
        return SimpleNamespace(name=name, prefix=None)

    def create_slot(self, repo, *, base=None, test_cmd=None, readonly=False, owner_task=None):
        role = "readonly" if readonly else "work"
        lease = SimpleNamespace(
            slot=f"work/{repo}_1", repo=repo, state="leased", role=role,
            session=owner_task, pid=None if readonly else 123, test_cmd=test_cmd,
        )
        self.created.append((repo, base, test_cmd, readonly, owner_task, lease))
        return lease

    def slot_path(self, slot):
        return Path("/tmp") / slot


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"readonly": True},
        {"task": "job"},
    ],
    ids=["normal", "readonly", "task"],
)
def test_worktree_add_variants_reject_without_ack(pm_config, extra, capsys):
    pool = _SlotPool()
    args = argparse.Namespace(repo="svc", test=None, user_ack=None, **extra)
    assert pm_config.cmd_worktree_add(args, worktree_pool=pool, board=object()) == 1
    assert pool.created == []
    assert "1순위: 사용자에게" in capsys.readouterr().err


def test_worktree_add_rejects_ack_bound_to_other_repo(pm_config, capsys):
    pool = _SlotPool()
    args = argparse.Namespace(repo="svc", test=None, user_ack="other")
    assert pm_config.cmd_worktree_add(args, worktree_pool=pool, board=object()) == 1
    assert pool.created == []
    assert "결속되지 않았다" in capsys.readouterr().err


@pytest.mark.parametrize(
    "readonly,task,expected_role",
    [
        (False, None, "work"),
        (True, None, "readonly"),
        (False, "job", "work"),
    ],
    ids=["normal", "readonly-role", "task-leased"],
)
def test_worktree_add_bound_ack_preserves_create_slot_semantics(
    pm_config, monkeypatch, capsys, readonly, task, expected_role,
):
    pool = _SlotPool()
    monkeypatch.setattr(pm_config, "_install_protected_hook_reporting", lambda *a, **k: True)
    monkeypatch.setattr(pm_config, "_render_task_slots", lambda *a, **k: None)
    args = argparse.Namespace(
        repo="svc", test=None, readonly=readonly, task=task, user_ack="svc",
    )

    assert pm_config.cmd_worktree_add(
        args, worktree_pool=pool, board=object(), is_tty=lambda: False,
    ) == 0

    assert len(pool.created) == 1
    repo, base, test_cmd, got_readonly, owner_task, lease = pool.created[0]
    assert (repo, base, test_cmd, got_readonly, owner_task) == (
        "svc", None, None, readonly, task,
    )
    assert lease.state == "leased" and lease.role == expected_role
    assert "[승인 감사]" in capsys.readouterr().out
