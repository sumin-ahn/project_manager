"""라운드 사이드카 seam(`ticket_rounds.py`)의 경로·예약·판정·렌더 계약.

소비자(board·pm_delegate·external_review·ticket_finish)가 붙기 전의 기준선이라 이 파일은
모듈을 **단독으로** 로드해 실 파일시스템 위에서 검증한다.
"""
from __future__ import annotations

import ast
import importlib.util
import os
import threading
from pathlib import Path, PurePosixPath

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

# 명세 파일 본문 — 라운드 모델에서는 역할 절이 없다(라운드는 사이드카 파일이다).
TICKET_TEXT = (
    "# T-XXXX — 라운드 seam 테스트\n\n"
    "## 목표\n라운드 사이드카를 검증한다.\n\n"
    "## 완료 조건 (Definition of Done)\n- [ ] 실제 동작 검증\n"
)


def _load_tool(name: str):
    spec = importlib.util.spec_from_file_location(
        f"{name}_under_test", TOOLS / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def rounds():
    return _load_tool("ticket_rounds")


@pytest.fixture(scope="module")
def delegate():
    return _load_tool("pm_delegate")


@pytest.fixture()
def tickets_dir(tmp_path):
    directory = tmp_path / "tickets"
    (directory / "open").mkdir(parents=True)
    return directory


class RecordingLock:
    """진입/해제 시점의 관측을 남기는 락 스텁 — 예약이 락 **안**에서 일어나는지 본다."""

    def __init__(self, observe):
        self.observe = observe
        self.events: list[tuple[str, object]] = []
        self.entered = 0

    def __enter__(self):
        self.entered += 1
        self.events.append(("enter", self.observe()))
        return self

    def __exit__(self, *_exc):
        self.events.append(("exit", self.observe()))
        return False


def _seed(rounds, role, *, today="2026-01-02"):
    return rounds.render_round_seed(role, TICKET_TEXT, today=today)


def _reserve(rounds, tickets_dir, ticket, role, *, content=None, today="2026-01-02"):
    return rounds.reserve_round(
        tickets_dir, ticket, role,
        content=_seed(rounds, role, today=today) if content is None else content,
        lock=threading.Lock(),
    )


# ── 경로 규약 ──────────────────────────────────────────────────────────────

def test_rounds_dir_is_a_fixed_sibling_of_the_status_directories(rounds, tickets_dir):
    assert rounds.rounds_dir(tickets_dir) == tickets_dir / "rounds"
    assert rounds.rounds_dir_for_ticket("T-0001", tickets_dir) == (
        tickets_dir / "rounds" / "T-0001"
    )


@pytest.mark.parametrize(
    "ticket_id",
    [
        "", ".", "..", "../escape", "a/b", "a\\b",
        "C:x",          # Windows drive-relative — POSIX 구분자 없이 상위로 나간다
        "C:",
        "T-1:a",        # 대체 데이터 스트림 표기
        "CON", "nul", "LPT1",   # Windows 예약 장치명
        "T-0001 ", "T-0001.",   # Windows 가 조용히 떼는 후행 공백·점
    ],
)
def test_ticket_id_that_is_not_a_single_path_segment_is_refused(rounds, tickets_dir, ticket_id):
    with pytest.raises(rounds.RoundsError):
        rounds.rounds_dir_for_ticket(ticket_id, tickets_dir)


def test_round_relative_path_stays_short_for_windows_path_budget(rounds):
    relative = PurePosixPath("tickets") / "rounds" / "T-0749" / rounds.round_filename(
        1, "code-reviewer"
    )
    assert str(relative) == "tickets/rounds/T-0749/01-code-reviewer.md"
    assert len(str(relative)) <= 60


# ── 파일명 문법 ────────────────────────────────────────────────────────────

def test_round_filename_zero_pads_to_two_digits_and_grows_beyond_ninety_nine(rounds):
    assert rounds.round_filename(1, "developer") == "01-developer.md"
    assert rounds.round_filename(12, "architect") == "12-architect.md"
    assert rounds.round_filename(100, "code-reviewer") == "100-code-reviewer.md"


@pytest.mark.parametrize("ordinal", [0, -1, True, 1.0, "1"])
def test_round_filename_refuses_non_positive_integer_ordinals(rounds, ordinal):
    with pytest.raises(rounds.RoundsError):
        rounds.round_filename(ordinal, "developer")


def test_round_filename_refuses_roles_outside_the_round_role_set(rounds):
    with pytest.raises(rounds.RoundsError):
        rounds.round_filename(1, "dev")


@pytest.mark.parametrize(
    "name,expected",
    [
        ("01-developer.md", (1, "developer")),
        ("02-code-reviewer.md", (2, "code-reviewer")),
        ("03-external-reviewer.md", (3, "external-reviewer")),
        ("100-architect.md", (100, "architect")),
    ],
)
def test_parse_round_filename_accepts_canonical_names(rounds, name, expected):
    assert rounds.parse_round_filename(name) == expected


@pytest.mark.parametrize(
    "name",
    [
        "1-developer.md",        # zero-pad 없음
        "001-developer.md",      # 과잉 pad
        "00-developer.md",       # 0번 순번
        "01-x.md",               # 역할 아님
        "01-developer.txt",      # 확장자
        "01-developer",          # 확장자 없음
        "developer-01.md",       # 순서 반전
        "01_developer.md",       # 구분자
        "-01-developer.md",
        "README.md",
        "",
    ],
)
def test_parse_round_filename_refuses_everything_else(rounds, name):
    assert rounds.parse_round_filename(name) is None


def test_parse_round_filename_is_the_inverse_of_round_filename(rounds):
    for ordinal in (1, 9, 10, 99, 100):
        for role in rounds.ROLES:
            name = rounds.round_filename(ordinal, role)
            assert rounds.parse_round_filename(name) == (ordinal, role)


# ── 시드 렌더 (pm_delegate 재사용) ─────────────────────────────────────────

def test_round_seed_is_the_role_header_plus_the_delegate_skeleton(rounds, delegate):
    seed = rounds.render_round_seed("developer", TICKET_TEXT, today="2026-01-02")
    assert seed.startswith("## 구현 보충 (developer · 2026-01-02)\n\n")
    assert seed == (
        "## 구현 보충 (developer · 2026-01-02)\n\n"
        + delegate.render_ticket_growth_section_seed("developer", TICKET_TEXT)
    )


def test_review_round_seed_carries_the_review_block_skeleton(rounds, delegate):
    seed = rounds.render_round_seed("code-reviewer", TICKET_TEXT, today="2026-01-02")
    assert seed.startswith("## 리뷰 (code-reviewer · 2026-01-02)\n\n")
    assert delegate.render_ticket_growth_section_seed(
        "code-reviewer", TICKET_TEXT
    ) in seed


def test_round_seed_labels_match_the_board_role_labels(rounds):
    """권위는 `ticket_rounds.ROLE_LABELS` — board 표가 파생으로 뒤집힐 때까지 일치를 못박는다."""
    board = _load_tool("board")
    assert rounds.ROLE_LABELS == board.TICKET_GROWTH_ROLE_LABELS
    assert rounds.ROLES == tuple(board.TICKET_GROWTH_ROLE_LABELS)


def test_round_roles_match_the_delegate_round_role_authority(rounds, delegate):
    assert set(rounds.ROLES) == set(delegate.TICKET_COPY_ROLES)


@pytest.mark.parametrize("today", ["2026-1-2", "20260102", "오늘", "", None])
def test_round_seed_refuses_a_header_date_outside_the_fixed_format(rounds, today):
    with pytest.raises(rounds.RoundsError):
        rounds.render_round_seed("developer", TICKET_TEXT, today=today)


def test_round_seed_refuses_roles_outside_the_round_role_set(rounds):
    with pytest.raises(rounds.RoundsError):
        rounds.render_round_seed("dev", TICKET_TEXT, today="2026-01-02")


# ── 예약 (채번 + 배타 생성) ────────────────────────────────────────────────

def test_reserve_round_creates_the_directory_and_numbers_from_one(rounds, tickets_dir):
    path = _reserve(rounds, tickets_dir, "T-0001", "architect")
    assert path == tickets_dir / "rounds" / "T-0001" / "01-architect.md"
    assert path.read_text(encoding="utf-8") == _seed(rounds, "architect")


def test_reserve_round_numbers_across_roles_in_ticket_wide_order(rounds, tickets_dir):
    first = _reserve(rounds, tickets_dir, "T-0001", "architect")
    second = _reserve(rounds, tickets_dir, "T-0001", "developer")
    third = _reserve(rounds, tickets_dir, "T-0001", "code-reviewer")
    assert [path.name for path in (first, second, third)] == [
        "01-architect.md", "02-developer.md", "03-code-reviewer.md",
    ]


def test_reserve_round_writes_the_given_content_verbatim(rounds, tickets_dir):
    body = "## 추가 리뷰 (external-reviewer · 2026-01-02)\n\n실제 회신 본문\n"
    path = _reserve(
        rounds, tickets_dir, "T-0001", "external-reviewer", content=body,
    )
    assert path.read_bytes() == body.encode("utf-8")


def test_reserve_round_without_a_lock_is_refused_before_touching_the_disk(
    rounds, tickets_dir,
):
    with pytest.raises(rounds.RoundsError, match="락"):
        rounds.reserve_round(
            tickets_dir, "T-0001", "developer",
            content=_seed(rounds, "developer"), lock=None,
        )
    assert not (tickets_dir / "rounds").exists()


def test_reserve_round_numbers_and_creates_inside_the_caller_lock(rounds, tickets_dir):
    target = tickets_dir / "rounds" / "T-0001" / "01-developer.md"
    lock = RecordingLock(lambda: target.exists())
    rounds.reserve_round(
        tickets_dir, "T-0001", "developer",
        content=_seed(rounds, "developer"), lock=lock,
    )
    assert lock.entered == 1
    assert lock.events == [("enter", False), ("exit", True)]


def test_concurrent_reservations_under_one_lock_get_unique_ordinals(rounds, tickets_dir):
    lock = threading.Lock()
    reserved: list[Path] = []
    errors: list[BaseException] = []

    def reserve(role: str) -> None:
        try:
            reserved.append(
                rounds.reserve_round(
                    tickets_dir, "T-0001", role,
                    content=_seed(rounds, role), lock=lock,
                )
            )
        except BaseException as exc:      # noqa: BLE001 — 스레드 실패를 본 스레드로 옮긴다.
            errors.append(exc)

    threads = [
        threading.Thread(target=reserve, args=(role,))
        for role in ("developer", "code-reviewer")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, errors
    ordinals = sorted(
        rounds.parse_round_filename(path.name)[0] for path in reserved
    )
    assert ordinals == [1, 2]
    assert len(list((tickets_dir / "rounds" / "T-0001").iterdir())) == 2


def test_reserve_round_refuses_to_number_past_an_unparsable_neighbour(rounds, tickets_dir):
    directory = tickets_dir / "rounds" / "T-0001"
    directory.mkdir(parents=True)
    (directory / "notes.md").write_text("손으로 둔 파일\n", encoding="utf-8")
    with pytest.raises(rounds.RoundsError, match="문법"):
        _reserve(rounds, tickets_dir, "T-0001", "developer")


def test_reserve_round_refuses_roles_outside_the_round_role_set(rounds, tickets_dir):
    with pytest.raises(rounds.RoundsError):
        rounds.reserve_round(
            tickets_dir, "T-0001", "dev", content="x", lock=threading.Lock(),
        )


def test_reserve_round_refuses_to_overwrite_when_the_scan_was_stale(
    rounds, tickets_dir, monkeypatch,
):
    """채번이 틀려도 배타 생성이 최종 보루다 — 남의 라운드를 덮지 않고 loud 하게 멈춘다."""
    existing = _reserve(rounds, tickets_dir, "T-0001", "developer")
    before = existing.read_bytes()
    monkeypatch.setattr(rounds, "_scan_round_files", lambda directory: [])

    with pytest.raises(rounds.RoundsError, match="예약 충돌"):
        rounds.reserve_round(
            tickets_dir, "T-0001", "developer",
            content="다른 시드\n", lock=threading.Lock(),
        )

    assert existing.read_bytes() == before


def test_temporary_round_path_stays_outside_the_round_name_grammar(rounds, tickets_dir):
    target = tickets_dir / "rounds" / "T-0001" / "01-developer.md"
    temporary = rounds._temporary_round_path(target)
    assert temporary.parent == target.parent          # 원자 rename 은 같은 파일시스템이어야 한다
    assert temporary.name.startswith(rounds.ROUND_TEMPORARY_PREFIX)
    assert temporary.name.endswith(rounds.ROUND_TEMPORARY_SUFFIX)
    assert rounds.parse_round_filename(temporary.name) is None


def test_load_verify_and_reserve_survive_the_lockless_harvest_window(
    rounds, tickets_dir, monkeypatch,
):
    """회수(무락) 중간 파일이 디스크에 있는 창에서도 같은 티켓의 로드·판정·예약이 정상이다."""
    target = _reserve(rounds, tickets_dir, "T-0001", "developer")
    harvested = "## 구현 보충 (developer · 2026-01-03)\n\n회수 본문\n"
    seam = rounds._load_file_lock()
    original_replace = seam.atomic_replace
    inside_window = threading.Event()
    release = threading.Event()
    failure: list[BaseException] = []

    def blocking_replace(source, destination):
        inside_window.set()
        assert release.wait(30), "회수 창 해제 신호 누락"
        return original_replace(source, destination)

    monkeypatch.setattr(seam, "atomic_replace", blocking_replace)

    def harvest() -> None:
        try:
            rounds.replace_round(target, harvested)
        except BaseException as exc:   # noqa: BLE001 — 스레드 실패를 본 스레드로 옮긴다.
            failure.append(exc)

    thread = threading.Thread(target=harvest)
    thread.start()
    try:
        assert inside_window.wait(30), "회수 창에 진입하지 못했다"
        # 창이 실재하는지 먼저 단언한다 — 임시 파일이 없으면 이 테스트는 아무것도 안 본다.
        residue = [
            item.name for item in target.parent.iterdir()
            if item.name.startswith(rounds.ROUND_TEMPORARY_PREFIX)
        ]
        assert len(residue) == 1, residue

        loaded = rounds.load_rounds(tickets_dir, "T-0001", ticket_text=TICKET_TEXT)
        assert [(item.ordinal, item.role) for item in loaded] == [(1, "developer")]
        assert loaded[0].text != harvested        # 교체 전이라 리더는 옛 내용을 본다

        problems = rounds.verify_rounds(tickets_dir, "T-0001", ticket_text=TICKET_TEXT)
        assert [problem.code for problem in problems] == [rounds.PROBLEM_PENDING]

        reserved = rounds.reserve_round(
            tickets_dir, "T-0001", "code-reviewer",
            content=_seed(rounds, "code-reviewer"), lock=threading.Lock(),
        )
        assert reserved.name == "02-code-reviewer.md"
    finally:
        release.set()
        thread.join(30)

    assert not failure, failure
    assert target.read_bytes() == harvested.encode("utf-8")
    assert sorted(item.name for item in target.parent.iterdir()) == [
        "01-developer.md", "02-code-reviewer.md",
    ]


def test_stray_dot_prefixed_files_are_not_rounds_and_do_not_break_anything(
    rounds, tickets_dir,
):
    """점-접두 규약은 임시 파일뿐 아니라 외부 부산물(.DS_Store 류)도 표면 밖에 둔다."""
    path = _reserve(rounds, tickets_dir, "T-0001", "developer")
    rounds.replace_round(path, "## 구현 보충 (developer · 2026-01-03)\n\n회수\n")
    (path.parent / ".DS_Store").write_bytes(b"\x00")

    loaded = rounds.load_rounds(tickets_dir, "T-0001", ticket_text=TICKET_TEXT)
    assert [item.role for item in loaded] == ["developer"]
    assert rounds.verify_rounds(tickets_dir, "T-0001", ticket_text=TICKET_TEXT) == []


# ── 로드 ───────────────────────────────────────────────────────────────────

def test_load_rounds_of_a_ticket_without_a_rounds_directory_is_empty(rounds, tickets_dir):
    assert rounds.load_rounds(tickets_dir, "T-0404", ticket_text=TICKET_TEXT) == []


def test_load_rounds_returns_ordinal_order_with_pending_marked(rounds, tickets_dir):
    _reserve(rounds, tickets_dir, "T-0001", "architect")
    developer = _reserve(rounds, tickets_dir, "T-0001", "developer")
    rounds.replace_round(developer, "## 구현 보충 (developer · 2026-01-03)\n\n실제 산출\n")

    loaded = rounds.load_rounds(tickets_dir, "T-0001", ticket_text=TICKET_TEXT)
    assert [(item.ordinal, item.role, item.pending) for item in loaded] == [
        (1, "architect", True),
        (2, "developer", False),
    ]
    assert loaded[1].text == "## 구현 보충 (developer · 2026-01-03)\n\n실제 산출\n"


def test_load_rounds_is_loud_about_an_item_that_breaks_the_name_grammar(rounds, tickets_dir):
    _reserve(rounds, tickets_dir, "T-0001", "developer")
    (tickets_dir / "rounds" / "T-0001" / "01-developer.md.bak").write_text(
        "사본\n", encoding="utf-8",
    )
    with pytest.raises(rounds.RoundsError, match="문법"):
        rounds.load_rounds(tickets_dir, "T-0001", ticket_text=TICKET_TEXT)


def test_load_rounds_is_loud_about_a_directory_inside_the_rounds_directory(rounds, tickets_dir):
    _reserve(rounds, tickets_dir, "T-0001", "developer")
    (tickets_dir / "rounds" / "T-0001" / "02-developer.md").mkdir()
    with pytest.raises(rounds.RoundsError, match="문법"):
        rounds.load_rounds(tickets_dir, "T-0001", ticket_text=TICKET_TEXT)


# ── 무편집(산출 없음) 판정 ─────────────────────────────────────────────────

def test_untouched_seed_is_pending(rounds, tickets_dir):
    _reserve(rounds, tickets_dir, "T-0001", "developer")
    loaded = rounds.load_rounds(tickets_dir, "T-0001", ticket_text=TICKET_TEXT)
    assert rounds.round_is_pending(loaded[0], ticket_text=TICKET_TEXT) is True


def test_seed_with_only_a_different_header_date_is_still_pending(rounds, tickets_dir):
    path = _reserve(rounds, tickets_dir, "T-0001", "developer")
    rounds.replace_round(path, _seed(rounds, "developer", today="2030-12-31"))
    loaded = rounds.load_rounds(tickets_dir, "T-0001", ticket_text=TICKET_TEXT)
    assert loaded[0].pending is True


def test_seed_with_crlf_line_endings_is_still_pending(rounds, tickets_dir):
    path = _reserve(rounds, tickets_dir, "T-0001", "developer")
    rounds.replace_round(path, _seed(rounds, "developer").replace("\n", "\r\n"))
    loaded = rounds.load_rounds(tickets_dir, "T-0001", ticket_text=TICKET_TEXT)
    assert loaded[0].pending is True


def test_one_character_of_editing_ends_pending(rounds, tickets_dir):
    path = _reserve(rounds, tickets_dir, "T-0001", "developer")
    rounds.replace_round(path, _seed(rounds, "developer") + "!")
    loaded = rounds.load_rounds(tickets_dir, "T-0001", ticket_text=TICKET_TEXT)
    assert loaded[0].pending is False


def test_a_header_that_lost_its_date_is_not_pending(rounds, tickets_dir):
    path = _reserve(rounds, tickets_dir, "T-0001", "developer")
    seed = _seed(rounds, "developer")
    rounds.replace_round(path, "## 구현 보충\n" + seed.partition("\n")[2])
    loaded = rounds.load_rounds(tickets_dir, "T-0001", ticket_text=TICKET_TEXT)
    assert loaded[0].pending is False


# ── 교체 (회수) ────────────────────────────────────────────────────────────

def test_replace_round_preserves_crlf_bytes_round_trip(rounds, tickets_dir):
    path = _reserve(rounds, tickets_dir, "T-0001", "developer")
    harvested = "## 구현 보충 (developer · 2026-01-03)\r\n\r\n회수 본문\r\n"
    rounds.replace_round(path, harvested)

    assert path.read_bytes() == harvested.encode("utf-8")
    loaded = rounds.load_rounds(tickets_dir, "T-0001", ticket_text=TICKET_TEXT)
    assert loaded[0].text == harvested


def test_replace_round_leaves_no_temporary_residue(rounds, tickets_dir):
    path = _reserve(rounds, tickets_dir, "T-0001", "developer")
    rounds.replace_round(path, "## 구현 보충 (developer · 2026-01-03)\n\n회수\n")
    assert [item.name for item in path.parent.iterdir()] == ["01-developer.md"]


def test_replace_round_creates_the_file_when_it_is_missing(rounds, tickets_dir):
    directory = tickets_dir / "rounds" / "T-0001"
    directory.mkdir(parents=True)
    target = directory / "01-developer.md"
    rounds.replace_round(target, "## 구현 보충 (developer · 2026-01-03)\n\n회수\n")
    assert target.read_text(encoding="utf-8").endswith("회수\n")


# ── 판정 ───────────────────────────────────────────────────────────────────

def test_verify_rounds_is_clean_for_a_harvested_sequence(rounds, tickets_dir):
    for role in ("architect", "developer"):
        path = _reserve(rounds, tickets_dir, "T-0001", role)
        rounds.replace_round(path, f"## 산출 ({role} · 2026-01-03)\n\n내용\n")
    assert rounds.verify_rounds(tickets_dir, "T-0001", ticket_text=TICKET_TEXT) == []


def test_verify_rounds_of_a_ticket_without_rounds_is_clean(rounds, tickets_dir):
    assert rounds.verify_rounds(tickets_dir, "T-0404", ticket_text=TICKET_TEXT) == []


def test_verify_rounds_reports_a_gap_left_by_a_deleted_round(rounds, tickets_dir):
    for role in ("architect", "developer", "code-reviewer"):
        path = _reserve(rounds, tickets_dir, "T-0001", role)
        rounds.replace_round(path, f"## 산출 ({role} · 2026-01-03)\n\n내용\n")
    (tickets_dir / "rounds" / "T-0001" / "02-developer.md").unlink()

    problems = rounds.verify_rounds(tickets_dir, "T-0001", ticket_text=TICKET_TEXT)
    assert [problem.code for problem in problems] == [rounds.PROBLEM_GAP]
    assert "02" in problems[0].detail


def test_verify_rounds_reports_a_pending_round_as_information(rounds, tickets_dir):
    _reserve(rounds, tickets_dir, "T-0001", "developer")
    problems = rounds.verify_rounds(tickets_dir, "T-0001", ticket_text=TICKET_TEXT)
    assert [problem.code for problem in problems] == [rounds.PROBLEM_PENDING]
    assert "01-developer.md" in problems[0].detail


def test_verify_problem_codes_are_four_distinct_values(rounds):
    """소비자(lint·complete 게이트)가 한국어 detail 을 파싱하지 않고 코드로 심각도를 정한다."""
    codes = {
        rounds.PROBLEM_NAME, rounds.PROBLEM_GAP,
        rounds.PROBLEM_DUPLICATE, rounds.PROBLEM_PENDING,
    }
    assert codes == {"round-name", "round-gap", "round-dup", "round-pending"}


def test_verify_rounds_reports_a_name_that_breaks_the_grammar(rounds, tickets_dir):
    path = _reserve(rounds, tickets_dir, "T-0001", "developer")
    rounds.replace_round(path, "## 구현 보충 (developer · 2026-01-03)\n\n내용\n")
    (tickets_dir / "rounds" / "T-0001" / "notes.md").write_text("메모\n", encoding="utf-8")

    problems = rounds.verify_rounds(tickets_dir, "T-0001", ticket_text=TICKET_TEXT)
    assert [problem.code for problem in problems] == [rounds.PROBLEM_NAME]
    assert "notes.md" in problems[0].detail


def test_verify_rounds_reports_two_roles_holding_the_same_ordinal_as_a_duplicate(rounds, tickets_dir):
    directory = tickets_dir / "rounds" / "T-0001"
    directory.mkdir(parents=True)
    for role in ("architect", "developer"):
        (directory / f"01-{role}.md").write_text(
            f"## 산출 ({role} · 2026-01-03)\n\n내용\n", encoding="utf-8",
        )
    problems = rounds.verify_rounds(tickets_dir, "T-0001", ticket_text=TICKET_TEXT)
    assert [problem.code for problem in problems] == [rounds.PROBLEM_DUPLICATE]
    assert "01" in problems[0].detail
    assert "architect" in problems[0].detail and "developer" in problems[0].detail


def test_verify_rounds_does_not_call_a_trailing_gap_when_the_last_round_is_pending(
    rounds, tickets_dir,
):
    path = _reserve(rounds, tickets_dir, "T-0001", "architect")
    rounds.replace_round(path, "## 설계 (architect · 2026-01-03)\n\n내용\n")
    _reserve(rounds, tickets_dir, "T-0001", "code-reviewer")

    problems = rounds.verify_rounds(tickets_dir, "T-0001", ticket_text=TICKET_TEXT)
    assert [problem.code for problem in problems] == [rounds.PROBLEM_PENDING]


# ── show 렌더 ──────────────────────────────────────────────────────────────

def test_render_rounds_for_show_joins_in_ordinal_order_with_pending_marked(
    rounds, tickets_dir,
):
    architect = _reserve(rounds, tickets_dir, "T-0001", "architect")
    rounds.replace_round(architect, "## 설계 (architect · 2026-01-03)\n\n설계 산출\n")
    _reserve(rounds, tickets_dir, "T-0001", "developer")

    loaded = rounds.load_rounds(tickets_dir, "T-0001", ticket_text=TICKET_TEXT)
    rendered = rounds.render_rounds_for_show(loaded)

    assert rendered.startswith("--- 01-architect ---\n## 설계 (architect · 2026-01-03)")
    assert "--- 02-developer (산출 없음) ---\n" in rendered
    assert rendered.index("01-architect") < rendered.index("02-developer")


def test_render_rounds_for_show_of_nothing_is_empty(rounds):
    assert rounds.render_rounds_for_show([]) == ""


def test_render_rounds_for_show_terminates_a_round_without_a_trailing_newline(rounds, tickets_dir):
    path = _reserve(rounds, tickets_dir, "T-0001", "developer")
    rounds.replace_round(path, "## 구현 보충 (developer · 2026-01-03)\n\n마지막 줄")
    loaded = rounds.load_rounds(tickets_dir, "T-0001", ticket_text=TICKET_TEXT)
    assert rounds.render_rounds_for_show(loaded).endswith("마지막 줄\n")


# ── 경계 (모듈이 로드하는 형제) ────────────────────────────────────────────

def test_module_loads_only_the_file_lock_and_delegate_siblings():
    """board 는 로드하지 않는다 — 소비 방향이 board → 이 모듈이라 반대 로드는 순환이다."""
    tree = ast.parse(
        (TOOLS / "ticket_rounds.py").read_text(encoding="utf-8"),
        filename="ticket_rounds.py",
    )
    loaded = {
        node.args[1].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_load_module_from_path"
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
    }
    assert loaded == {"file_lock.py", "pm_delegate.py"}


def test_reads_and_replacements_go_through_the_shared_file_lock_seam(rounds, tickets_dir, monkeypatch):
    """판독·교체가 공용 seam 을 지난다 — 일반 `open`/`os.replace` 로 내려앉지 않는다."""
    path = _reserve(rounds, tickets_dir, "T-0001", "developer")
    seam = rounds._load_file_lock()
    calls: list[str] = []

    def record_read(*args, **kwargs):
        calls.append("read")
        return original_read(*args, **kwargs)

    def record_replace(*args, **kwargs):
        calls.append("replace")
        return original_replace(*args, **kwargs)

    original_read = seam.read_text_shared
    original_replace = seam.atomic_replace
    monkeypatch.setattr(seam, "read_text_shared", record_read)
    monkeypatch.setattr(seam, "atomic_replace", record_replace)

    rounds.replace_round(path, "## 구현 보충 (developer · 2026-01-03)\n\n회수\n")
    rounds.load_rounds(tickets_dir, "T-0001", ticket_text=TICKET_TEXT)

    assert calls == ["replace", "read"]


def test_reserved_round_file_is_readable_by_the_board_git_checkout(rounds, tickets_dir):
    """보드 git 이 추적하는 일반 파일이다 — 슬롯 사본과 달리 소유자 전용이 아니다."""
    path = _reserve(rounds, tickets_dir, "T-0001", "developer")
    if os.name == "nt":       # Windows 는 POSIX mode 비트를 그대로 두지 않는다.
        pytest.skip("POSIX 권한 비트 전용 단언")
    assert path.stat().st_mode & 0o777 == rounds.ROUND_FILE_MODE & ~_umask()


def _umask() -> int:
    current = os.umask(0)
    os.umask(current)
    return current
