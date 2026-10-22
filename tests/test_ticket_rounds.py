"""라운드 사이드카(`round_rules`·`round_store`·`round_seed`)의 경로·예약·판정·렌더 계약.

소비자(board·pm_delegate·additional_reviewer·ticket_finish)가 붙기 전의 기준선이라 이 파일은
모듈을 **단독으로** 로드해 실 파일시스템 위에서 검증한다.
"""
from __future__ import annotations

from pm_engine.domain import round_rules


import importlib.util
import json
import os
import threading
from pathlib import Path, PurePosixPath

import pytest
from pm_engine.domain import review_form, round_rules
from pm_engine.infra import delegate_ledger, fs, round_store
from pm_engine.app import round_seed

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

# 명세 파일 본문 — 라운드 모델에서는 역할 절이 없다(라운드는 사이드카 파일이다).
# frontmatter 는 T-0815 설계 근거 게이트(developer 시드 seam)가 `design:` 값을 읽는 유일한
# 입력이라 붙인다 — `design: done` + 충전된 설계 절이 이 파일의 관심사(라운드 사이드카 기계
# 자체) 밖의 근거를 미리 해소해, 아래 대다수 테스트가 공용 헬퍼(`_seed`/`_reserve`)로
# role="developer" 라운드를 세울 때마다 설계 근거를 따로 갖추지 않아도 되게 한다(무관 실패 0).
TICKET_TEXT = (
    "---\n"
    "id: T-XXXX\n"
    "design: done\n"
    "---\n"
    "# T-XXXX — 라운드 seam 테스트\n\n"
    "## 목표\n라운드 사이드카를 검증한다.\n\n"
    "## 설계\n"
    "- **경계 실측**: 기계 테스트 픽스처\n"
    "- **불변식**: 이 파일의 축 밖\n"
    "- **표면 상한**: 픽스처 1건\n"
    "- **테스트 전략**: 정상·실패 경로\n\n"
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


# 라운드 헤더 픽스처 날짜 — 헤더 문법만 판정에 쓰이므로 값 하나를 공용으로 재사용한다.
SEED_DATE = "2026-01-02"


def _seed(role, *, today=SEED_DATE):
    return round_seed.render_round_seed(role, TICKET_TEXT, today=today)


def _reserve(tickets_dir, ticket, role, *, content=None, today=SEED_DATE):
    return round_store.reserve_round(
        tickets_dir, ticket, role,
        content=_seed(role, today=today) if content is None else content,
        lock=threading.Lock(),
    )


# ── 경로 규약 ──────────────────────────────────────────────────────────────

def test_rounds_dir_is_a_fixed_sibling_of_the_status_directories(tickets_dir):
    assert round_store.rounds_dir(tickets_dir) == tickets_dir / "rounds"
    assert round_store.rounds_dir_for_ticket("T-0001", tickets_dir) == (
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
def test_ticket_id_that_is_not_a_single_path_segment_is_refused(tickets_dir, ticket_id):
    with pytest.raises(round_rules.RoundsError):
        round_store.rounds_dir_for_ticket(ticket_id, tickets_dir)


def test_round_relative_path_stays_short_for_windows_path_budget():
    relative = PurePosixPath("tickets") / "rounds" / "T-0749" / round_rules.round_filename(
        1, "code-reviewer"
    )
    assert str(relative) == "tickets/rounds/T-0749/01-code-reviewer.md"
    assert len(str(relative)) <= 60


# ── 파일명 문법 ────────────────────────────────────────────────────────────

def test_round_filename_zero_pads_to_two_digits_and_grows_beyond_ninety_nine():
    assert round_rules.round_filename(1, "developer") == "01-developer.md"
    assert round_rules.round_filename(12, "architect") == "12-architect.md"
    assert round_rules.round_filename(100, "code-reviewer") == "100-code-reviewer.md"


@pytest.mark.parametrize("ordinal", [0, -1, True, 1.0, "1"])
def test_round_filename_refuses_non_positive_integer_ordinals(ordinal):
    with pytest.raises(round_rules.RoundsError):
        round_rules.round_filename(ordinal, "developer")


def test_round_filename_refuses_roles_outside_the_round_role_set():
    with pytest.raises(round_rules.RoundsError):
        round_rules.round_filename(1, "dev")


@pytest.mark.parametrize(
    "name,expected",
    [
        ("01-developer.md", (1, "developer")),
        ("02-code-reviewer.md", (2, "code-reviewer")),
        ("03-additional-reviewer.md", (3, "additional-reviewer")),
        ("100-architect.md", (100, "architect")),
    ],
)
def test_parse_round_filename_accepts_canonical_names(name, expected):
    assert round_rules.parse_round_filename(name) == expected


def test_parse_round_filename_refuses_the_retired_role_name():
    """개칭 전 역할 이름은 읽기도 쓰기도 하지 않는다 — 옛 이름 호환은 남기지 않는다 (T-0887)."""
    assert round_rules.parse_round_filename("03-external-reviewer.md") is None
    assert not hasattr(round_rules, "READ_ONLY_ROLE_ALIASES")
    with pytest.raises(round_rules.RoundsError):
        round_rules.round_filename(3, "external-reviewer")


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
def test_parse_round_filename_refuses_everything_else(name):
    assert round_rules.parse_round_filename(name) is None


def test_parse_round_filename_is_the_inverse_of_round_filename():
    for ordinal in (1, 9, 10, 99, 100):
        for role in round_rules.ROLES:
            name = round_rules.round_filename(ordinal, role)
            assert round_rules.parse_round_filename(name) == (ordinal, role)


# ── 시드 렌더 (pm_delegate 재사용) ─────────────────────────────────────────

def test_round_seed_is_the_role_header_plus_the_delegate_skeleton():
    seed = round_seed.render_round_seed("developer", TICKET_TEXT, today="2026-01-02")
    assert seed.startswith("## 구현 보충 (developer · 2026-01-02)\n\n")
    assert seed == (
        "## 구현 보충 (developer · 2026-01-02)\n\n"
        + round_seed.render_ticket_growth_section_seed("developer", TICKET_TEXT)
    )


def test_review_round_seed_carries_the_review_block_skeleton():
    seed = round_seed.render_round_seed("code-reviewer", TICKET_TEXT, today="2026-01-02")
    assert seed.startswith("## 리뷰 (code-reviewer · 2026-01-02)\n\n")
    assert round_seed.render_ticket_growth_section_seed(
        "code-reviewer", TICKET_TEXT
    ) in seed


def test_round_seed_labels_are_the_authority_board_reads():
    """권위는 `round_rules.ROLE_LABELS` 이고 board 는 그 표를 파생으로 읽는다(복제 0)."""
    board = _load_tool("board")
    assert round_rules.ticket_round_role_labels() == round_rules.ROLE_LABELS
    assert tuple(round_rules.ticket_round_role_labels()) == round_rules.ROLES


def test_round_roles_match_the_delegate_round_role_authority():
    assert set(round_rules.ROLES) == set(delegate_ledger.TICKET_COPY_ROLES)


@pytest.mark.parametrize("today", ["2026-1-2", "20260102", "오늘", "", None])
def test_round_seed_refuses_a_header_date_outside_the_fixed_format(today):
    with pytest.raises(round_rules.RoundsError):
        round_seed.render_round_seed("developer", TICKET_TEXT, today=today)


def test_round_seed_refuses_roles_outside_the_round_role_set():
    with pytest.raises(round_rules.RoundsError):
        round_seed.render_round_seed("dev", TICKET_TEXT, today="2026-01-02")


# ── 예약 (채번 + 배타 생성) ────────────────────────────────────────────────

def test_reserve_round_creates_the_directory_and_numbers_from_one(tickets_dir):
    path = _reserve(tickets_dir, "T-0001", "architect")
    assert path == tickets_dir / "rounds" / "T-0001" / "01-architect.md"
    assert path.read_text(encoding="utf-8") == _seed("architect")


def test_reserve_round_numbers_across_roles_in_ticket_wide_order(tickets_dir):
    first = _reserve(tickets_dir, "T-0001", "architect")
    second = _reserve(tickets_dir, "T-0001", "developer")
    third = _reserve(tickets_dir, "T-0001", "code-reviewer")
    assert [path.name for path in (first, second, third)] == [
        "01-architect.md", "02-developer.md", "03-code-reviewer.md",
    ]


def test_reserve_round_writes_the_given_content_verbatim(tickets_dir):
    body = "## 추가 리뷰 (additional-reviewer · 2026-01-02)\n\n실제 회신 본문\n"
    path = _reserve(
        tickets_dir, "T-0001", "additional-reviewer", content=body,
    )
    assert path.read_bytes() == body.encode("utf-8")


def test_reserve_round_without_a_lock_is_refused_before_touching_the_disk(
    tickets_dir,
):
    with pytest.raises(round_rules.RoundsError, match="락"):
        round_store.reserve_round(
            tickets_dir, "T-0001", "developer",
            content=_seed("developer"), lock=None,
        )
    assert not (tickets_dir / "rounds").exists()


def test_reserve_round_numbers_and_creates_inside_the_caller_lock(tickets_dir):
    target = tickets_dir / "rounds" / "T-0001" / "01-developer.md"
    lock = RecordingLock(lambda: target.exists())
    round_store.reserve_round(
        tickets_dir, "T-0001", "developer",
        content=_seed("developer"), lock=lock,
    )
    assert lock.entered == 1
    assert lock.events == [("enter", False), ("exit", True)]


def test_concurrent_reservations_under_one_lock_get_unique_ordinals(tickets_dir):
    lock = threading.Lock()
    reserved: list[Path] = []
    errors: list[BaseException] = []
    roles = ("developer", "code-reviewer")
    # Jinja 환경의 첫 로드는 이 계약의 병렬 대상이 아니다. main thread에서 시드를 먼저
    # 렌더해 두고, worker는 단일 lock 아래의 채번/파일 예약만 동시에 실행한다.
    seeds = {role: _seed(role) for role in roles}

    def reserve(role: str) -> None:
        try:
            reserved.append(
                round_store.reserve_round(
                    tickets_dir, "T-0001", role,
                    content=seeds[role], lock=lock,
                )
            )
        except BaseException as exc:      # noqa: BLE001 — 스레드 실패를 본 스레드로 옮긴다.
            errors.append(exc)

    threads = [
        threading.Thread(target=reserve, args=(role,))
        for role in roles
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, errors
    ordinals = sorted(
        round_rules.parse_round_filename(path.name)[0] for path in reserved
    )
    assert ordinals == [1, 2]
    assert len(list((tickets_dir / "rounds" / "T-0001").iterdir())) == 2


def test_reserve_round_refuses_to_number_past_an_unparsable_neighbour(tickets_dir):
    directory = tickets_dir / "rounds" / "T-0001"
    directory.mkdir(parents=True)
    (directory / "notes.md").write_text("손으로 둔 파일\n", encoding="utf-8")
    with pytest.raises(round_rules.RoundsError, match="문법"):
        _reserve(tickets_dir, "T-0001", "developer")


def test_reserve_round_refuses_roles_outside_the_round_role_set(tickets_dir):
    with pytest.raises(round_rules.RoundsError):
        round_store.reserve_round(
            tickets_dir, "T-0001", "dev", content="x", lock=threading.Lock(),
        )


def test_reserve_round_refuses_to_overwrite_when_the_scan_was_stale(
    tickets_dir, monkeypatch,
):
    """채번이 틀려도 배타 생성이 최종 보루다 — 남의 라운드를 덮지 않고 loud 하게 멈춘다."""
    existing = _reserve(tickets_dir, "T-0001", "developer")
    before = existing.read_bytes()
    monkeypatch.setattr(round_store, "_scan_round_files", lambda directory: [])

    with pytest.raises(round_rules.RoundsError, match="예약 충돌"):
        round_store.reserve_round(
            tickets_dir, "T-0001", "developer",
            content="다른 시드\n", lock=threading.Lock(),
        )

    assert existing.read_bytes() == before


# ── 재개방 (같은 순번을 다시 연다) ─────────────────────────────────────────

def _architect_output(marker: str) -> str:
    """architect 라운드 산출 — 테스트 계약 블록 **한 벌**을 든 실제 회수 형상."""
    payload = json.dumps({
        "version": review_form.ARCHITECT_TEST_VERSION,
        "tests": [{
            "id": "AT-001", "target": "tests/test_ticket_rounds.py",
            "command": "python3 -m pytest tests/test_ticket_rounds.py -q",
            "expected": "passed", "negative": f"{marker} 없이 통과하면 계약이 헐겁다",
        }],
    }, ensure_ascii=False, separators=(",", ":"))
    return (
        f"## 설계 (architect · 2026-01-03)\n\n## 경계 실측\n- {marker}\n\n"
        "## 불변식\n- 한 단계 = 한 순번\n\n## 표면 상한\n- 재개방 1건\n\n"
        "## 테스트 전략\n- 정상·실패\n\n"
        f"```{review_form.ARCHITECT_TEST_BLOCK}\n{payload}\n```\n\n검토 판정: 설계 통과\n"
    )


def test_reopen_round_keeps_the_ordinal_and_replaces_the_file(
    tickets_dir, delegate,
):
    """이어 시킨 단계는 순번을 늘리지 않는다 — 같은 자리를 다시 열고 그 파일을 교체한다.

    변경 전에는 이 자리가 없어 이어 시킨 작업이 `max + 1` 로 새 파일을 받았고, 같은 역할이
    번호만 바꿔 반복됐다. 교체이므로 계약 블록은 한 벌 그대로다(절을 더하는 처방은 반려).
    """
    first = _reserve(tickets_dir, "T-0001", "architect")
    _reserve(tickets_dir, "T-0001", "developer")
    round_store.replace_round(first, _architect_output("1차 산출"))
    before = sorted(item.name for item in first.parent.iterdir())

    reopened = round_store.reopen_round(
        tickets_dir, "T-0001", "architect", ordinal=1, lock=threading.Lock(),
    )

    assert reopened == first
    assert sorted(item.name for item in first.parent.iterdir()) == before
    # 재개방이 여는 것은 자리뿐이다 — 슬롯 시드가 될 현재 내용은 손대지 않는다.
    assert reopened.read_text(encoding="utf-8") == _architect_output("1차 산출")

    round_store.replace_round(reopened, _architect_output("이어 시킨 산출"))

    assert sorted(item.name for item in first.parent.iterdir()) == before
    text = first.read_text(encoding="utf-8")
    assert "이어 시킨 산출" in text and "1차 산출" not in text
    assert len(delegate.parse_architect_tests(text)) == 1
    loaded = round_seed.load_rounds(tickets_dir, "T-0001", ticket_text=TICKET_TEXT)
    assert [(item.ordinal, item.role) for item in loaded] == [
        (1, "architect"), (2, "developer"),
    ]
    assert [problem.code for problem in round_seed.verify_rounds(
        tickets_dir, "T-0001", ticket_text=TICKET_TEXT,
    )] == [round_rules.PROBLEM_PENDING]      # 손대지 않은 02 하나뿐 — gap·dup 없음


def test_reopen_round_refuses_a_missing_or_mismatched_ordinal(tickets_dir):
    """없는 순번·다른 역할의 순번은 열지 않는다 — 한 순번은 한 역할이다."""
    _reserve(tickets_dir, "T-0001", "architect")
    before = sorted(item.name for item in (tickets_dir / "rounds" / "T-0001").iterdir())

    with pytest.raises(round_rules.RoundsError, match="재개방할 라운드가 없다"):
        round_store.reopen_round(
            tickets_dir, "T-0001", "developer", ordinal=2, lock=threading.Lock(),
        )
    with pytest.raises(round_rules.RoundsError, match="역할이 다르다"):
        round_store.reopen_round(
            tickets_dir, "T-0001", "developer", ordinal=1, lock=threading.Lock(),
        )
    with pytest.raises(round_rules.RoundsError, match="재개방할 라운드가 없다"):
        round_store.reopen_round(
            tickets_dir, "T-0001", "architect", ordinal=0, lock=threading.Lock(),
        )

    assert sorted(
        item.name for item in (tickets_dir / "rounds" / "T-0001").iterdir()
    ) == before


def test_reopen_round_without_a_lock_is_refused_before_touching_the_disk(
    tickets_dir,
):
    _reserve(tickets_dir, "T-0001", "architect")
    with pytest.raises(round_rules.RoundsError, match="락"):
        round_store.reopen_round(
            tickets_dir, "T-0001", "architect", ordinal=1, lock=None,
        )


def test_reopen_round_judges_inside_the_caller_lock(tickets_dir):
    """판정과 반환 사이가 예약과 같은 창이다 — 그 구간을 호출자 락이 덮는다."""
    reserved = _reserve(tickets_dir, "T-0001", "developer")
    lock = RecordingLock(lambda: reserved.exists())
    round_store.reopen_round(
        tickets_dir, "T-0001", "developer", ordinal=1, lock=lock,
    )
    assert lock.entered == 1
    assert lock.events == [("enter", True), ("exit", True)]


def test_reopen_round_refuses_an_ambiguous_duplicate_ordinal(tickets_dir):
    """한 순번을 둘이 쥔 상태에서는 어느 파일을 여는지 고르지 않는다."""
    directory = tickets_dir / "rounds" / "T-0001"
    directory.mkdir(parents=True)
    for role in ("architect", "developer"):
        (directory / f"01-{role}.md").write_text(
            f"## 산출 ({role} · 2026-01-03)\n\n내용\n", encoding="utf-8",
        )
    with pytest.raises(round_rules.RoundsError, match="순번 중복"):
        round_store.reopen_round(
            tickets_dir, "T-0001", "developer", ordinal=1, lock=threading.Lock(),
        )


def test_a_deleted_round_is_restored_from_the_board_git_record(
    tickets_dir, delegate,
):
    """빈 순번의 유일한 처방 — 기록에 남은 그 파일을 되쓰고 엔진 표식을 붙인다.

    엔진은 빈 순번을 만들지 않는다(포기는 최대 순번만 지우고 중간 순번은 보존한다). 사람이
    직접 지운 자리는 board git 이 든 그 bytes 를 공용 교체 seam 으로 되쓰면 채워지고, 엔진
    표식이 그 라운드를 판정 표면 밖에 세운다 — 되살린 시드가 `round-pending` 으로 남지 않고
    직전 산출로도 서지 않는다. 새 규약을 만들지 않는다는 것이 이 테스트의 내용이다.
    """
    for role in ("architect", "developer", "code-reviewer"):
        path = _reserve(tickets_dir, "T-0001", role)
        round_store.replace_round(path, f"## 산출 ({role} · 2026-01-03)\n\n내용\n")
    # board git 이 든 그 순번의 기록(여기서는 산출 없는 시드였다) + 사람이 직접 rm 한 상태.
    recorded = _seed("developer")
    target = tickets_dir / "rounds" / "T-0001" / "02-developer.md"
    target.unlink()
    assert [problem.code for problem in round_seed.verify_rounds(
        tickets_dir, "T-0001", ticket_text=TICKET_TEXT,
    )] == [round_rules.PROBLEM_GAP]

    round_store.replace_round(
        target, recorded + delegate.pm_review_refused_line("developer") + "\n",
    )

    assert round_seed.verify_rounds(tickets_dir, "T-0001", ticket_text=TICKET_TEXT) == []
    loaded = round_seed.load_rounds(tickets_dir, "T-0001", ticket_text=TICKET_TEXT)
    assert [(item.ordinal, item.role) for item in loaded] == [
        (1, "architect"), (2, "developer"), (3, "code-reviewer"),
    ]
    restored = next(item for item in loaded if item.ordinal == 2)
    assert not restored.pending
    assert round_rules.latest_round_of_role(loaded, "developer") is None


def test_temporary_round_path_stays_outside_the_round_name_grammar(tickets_dir):
    target = tickets_dir / "rounds" / "T-0001" / "01-developer.md"
    temporary = round_store._temporary_round_path(target)
    assert temporary.parent == target.parent          # 원자 rename 은 같은 파일시스템이어야 한다
    assert temporary.name.startswith(round_store.ROUND_TEMPORARY_PREFIX)
    assert temporary.name.endswith(round_store.ROUND_TEMPORARY_SUFFIX)
    assert round_rules.parse_round_filename(temporary.name) is None


def test_load_verify_and_reserve_survive_the_lockless_harvest_window(
    tickets_dir, monkeypatch,
):
    """회수(무락) 중간 파일이 디스크에 있는 창에서도 같은 티켓의 로드·판정·예약이 정상이다."""
    target = _reserve(tickets_dir, "T-0001", "developer")
    harvested = "## 구현 보충 (developer · 2026-01-03)\n\n회수 본문\n"
    seam = fs
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
            round_store.replace_round(target, harvested)
        except BaseException as exc:   # noqa: BLE001 — 스레드 실패를 본 스레드로 옮긴다.
            failure.append(exc)

    thread = threading.Thread(target=harvest)
    thread.start()
    try:
        assert inside_window.wait(30), "회수 창에 진입하지 못했다"
        # 창이 실재하는지 먼저 단언한다 — 임시 파일이 없으면 이 테스트는 아무것도 안 본다.
        residue = [
            item.name for item in target.parent.iterdir()
            if item.name.startswith(round_store.ROUND_TEMPORARY_PREFIX)
        ]
        assert len(residue) == 1, residue

        loaded = round_seed.load_rounds(tickets_dir, "T-0001", ticket_text=TICKET_TEXT)
        assert [(item.ordinal, item.role) for item in loaded] == [(1, "developer")]
        assert loaded[0].text != harvested        # 교체 전이라 리더는 옛 내용을 본다

        problems = round_seed.verify_rounds(tickets_dir, "T-0001", ticket_text=TICKET_TEXT)
        assert [problem.code for problem in problems] == [round_rules.PROBLEM_PENDING]

        reserved = round_store.reserve_round(
            tickets_dir, "T-0001", "code-reviewer",
            content=_seed("code-reviewer"), lock=threading.Lock(),
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
    tickets_dir,
):
    """점-접두 규약은 임시 파일뿐 아니라 외부 부산물(.DS_Store 류)도 표면 밖에 둔다."""
    path = _reserve(tickets_dir, "T-0001", "developer")
    round_store.replace_round(path, "## 구현 보충 (developer · 2026-01-03)\n\n회수\n")
    (path.parent / ".DS_Store").write_bytes(b"\x00")

    loaded = round_seed.load_rounds(tickets_dir, "T-0001", ticket_text=TICKET_TEXT)
    assert [item.role for item in loaded] == ["developer"]
    assert round_seed.verify_rounds(tickets_dir, "T-0001", ticket_text=TICKET_TEXT) == []


# ── 로드 ───────────────────────────────────────────────────────────────────

def test_load_rounds_of_a_ticket_without_a_rounds_directory_is_empty(tickets_dir):
    assert round_seed.load_rounds(tickets_dir, "T-0404", ticket_text=TICKET_TEXT) == []


def test_load_rounds_returns_ordinal_order_with_pending_marked(tickets_dir):
    _reserve(tickets_dir, "T-0001", "architect")
    developer = _reserve(tickets_dir, "T-0001", "developer")
    round_store.replace_round(developer, "## 구현 보충 (developer · 2026-01-03)\n\n실제 산출\n")

    loaded = round_seed.load_rounds(tickets_dir, "T-0001", ticket_text=TICKET_TEXT)
    assert [(item.ordinal, item.role, item.pending) for item in loaded] == [
        (1, "architect", True),
        (2, "developer", False),
    ]
    assert loaded[1].text == "## 구현 보충 (developer · 2026-01-03)\n\n실제 산출\n"


def test_load_rounds_is_loud_about_an_item_that_breaks_the_name_grammar(tickets_dir):
    _reserve(tickets_dir, "T-0001", "developer")
    (tickets_dir / "rounds" / "T-0001" / "01-developer.md.bak").write_text(
        "사본\n", encoding="utf-8",
    )
    with pytest.raises(round_rules.RoundsError, match="문법"):
        round_seed.load_rounds(tickets_dir, "T-0001", ticket_text=TICKET_TEXT)


def test_load_rounds_is_loud_about_a_directory_inside_the_rounds_directory(tickets_dir):
    _reserve(tickets_dir, "T-0001", "developer")
    (tickets_dir / "rounds" / "T-0001" / "02-developer.md").mkdir()
    with pytest.raises(round_rules.RoundsError, match="문법"):
        round_seed.load_rounds(tickets_dir, "T-0001", ticket_text=TICKET_TEXT)


# ── 무편집(산출 없음) 판정 ─────────────────────────────────────────────────

def test_untouched_seed_is_pending(tickets_dir):
    _reserve(tickets_dir, "T-0001", "developer")
    loaded = round_seed.load_rounds(tickets_dir, "T-0001", ticket_text=TICKET_TEXT)
    assert round_seed.round_is_pending(loaded[0], ticket_text=TICKET_TEXT) is True


def test_seed_with_only_a_different_header_date_is_still_pending(tickets_dir):
    path = _reserve(tickets_dir, "T-0001", "developer")
    round_store.replace_round(path, _seed("developer", today="2030-12-31"))
    loaded = round_seed.load_rounds(tickets_dir, "T-0001", ticket_text=TICKET_TEXT)
    assert loaded[0].pending is True


def test_seed_with_crlf_line_endings_is_still_pending(tickets_dir):
    path = _reserve(tickets_dir, "T-0001", "developer")
    round_store.replace_round(path, _seed("developer").replace("\n", "\r\n"))
    loaded = round_seed.load_rounds(tickets_dir, "T-0001", ticket_text=TICKET_TEXT)
    assert loaded[0].pending is True


def test_one_character_of_editing_ends_pending(tickets_dir):
    path = _reserve(tickets_dir, "T-0001", "developer")
    round_store.replace_round(path, _seed("developer") + "!")
    loaded = round_seed.load_rounds(tickets_dir, "T-0001", ticket_text=TICKET_TEXT)
    assert loaded[0].pending is False


def test_a_header_that_lost_its_date_is_not_pending(tickets_dir):
    path = _reserve(tickets_dir, "T-0001", "developer")
    seed = _seed("developer")
    round_store.replace_round(path, "## 구현 보충\n" + seed.partition("\n")[2])
    loaded = round_seed.load_rounds(tickets_dir, "T-0001", ticket_text=TICKET_TEXT)
    assert loaded[0].pending is False


def test_a_seed_with_a_custom_header_label_is_still_pending(tickets_dir):
    """헤더 라벨은 사람이 고르는 자리다(`section-add --label`) — 판정은 본문 골격만 본다."""
    path = _reserve(tickets_dir, "T-0001", "developer")
    seed = _seed("developer")
    round_store.replace_round(
        path, "## 구현 보충(2차) (developer · 2026-01-02)\n" + seed.partition("\n")[2],
    )
    loaded = round_seed.load_rounds(tickets_dir, "T-0001", ticket_text=TICKET_TEXT)
    assert loaded[0].pending is True


def _reviewer_output(finding_id: str = "F-001", role: str = "code-reviewer") -> str:
    """실산출 리뷰 라운드 — 자리표시자가 아닌 값이 든 `pm-review-v1` 블록 하나."""
    block = json.dumps({
        "version": 2,
        "findings": [{
            "id": finding_id, "class": "implementation-defect",
            "severity": "must-fix", "authority": "설계 §경계",
            "evidence": "probe rc=1", "recommendation": f"{finding_id} 수정",
            "design_change": False,
        }],
        "confirmations": [],
    }, ensure_ascii=False)
    return (
        f"## 리뷰 ({role} · 2026-01-03)\n\n"
        f"## must-fix\n- {finding_id}\n\n"
        "## 판정\n판정: 반려 · finding 1건(must-fix 1건)\n\n"
        f"```pm-review-v1\n{block}\n```\n"
    )


def test_pending_does_not_flip_when_an_earlier_round_of_the_same_role_is_harvested(
    tickets_dir,
):
    """같은 역할 병렬 2라운드 — 01 회수가 손대지 않은 02 의 판정을 뒤집지 않는다.

    판정 입력이 시점(다른 라운드의 현재 내용)에 기대면 02 가 "산출 있음"으로 읽혀
    `verify_rounds` 가 미회수를 보고하지 않고 조회의 `(산출 없음)` 표시도 사라진다.
    """
    first = _reserve(tickets_dir, "T-0001", "code-reviewer")
    second = _reserve(tickets_dir, "T-0001", "code-reviewer")
    untouched = second.read_bytes()

    round_store.replace_round(first, _reviewer_output("F-001"))

    loaded = round_seed.load_rounds(tickets_dir, "T-0001", ticket_text=TICKET_TEXT)
    assert second.read_bytes() == untouched, "판정 대상 파일이 바뀌었다(테스트 전제 붕괴)"
    assert [(item.ordinal, item.pending) for item in loaded] == [(1, False), (2, True)]

    problems = round_seed.verify_rounds(tickets_dir, "T-0001", ticket_text=TICKET_TEXT)
    assert [problem.code for problem in problems] == [round_rules.PROBLEM_PENDING]
    assert "02-code-reviewer.md" in problems[0].detail
    assert "--- 02-code-reviewer (산출 없음) ---" in round_rules.render_rounds_for_show(loaded)


def test_a_seed_whose_confirmation_ids_are_prefilled_is_pending(tickets_dir):
    """예약이 프리필한 실 finding ID 가 있어도 status·evidence 가 자리표시자면 산출이 없다."""
    first = _reserve(tickets_dir, "T-0001", "code-reviewer")
    round_store.replace_round(first, _reviewer_output("F-007"))
    loaded = round_seed.load_rounds(tickets_dir, "T-0001", ticket_text=TICKET_TEXT)

    seed = round_seed.render_round_seed(
        "code-reviewer", TICKET_TEXT, today="2026-01-04",
        previous_round=round_rules.previous_round_of_role(loaded, "code-reviewer"),
    )
    assert '"id":"F-007"' in seed.replace(" ", ""), "프리필이 일어나지 않았다(전제 붕괴)"
    _reserve(tickets_dir, "T-0001", "code-reviewer", content=seed)

    reloaded = round_seed.load_rounds(tickets_dir, "T-0001", ticket_text=TICKET_TEXT)
    assert [(item.ordinal, item.pending) for item in reloaded] == [(1, False), (2, True)]


def test_filling_the_confirmation_status_ends_pending(tickets_dir):
    """골격 대조는 ID 만 자유롭다 — 값을 채운 블록은 산출이다."""
    path = _reserve(tickets_dir, "T-0001", "code-reviewer")
    seed = path.read_text(encoding="utf-8")
    filled = seed.replace("<resolved|unresolved|regressed>", "resolved")
    assert filled != seed
    round_store.replace_round(path, filled)

    loaded = round_seed.load_rounds(tickets_dir, "T-0001", ticket_text=TICKET_TEXT)
    assert loaded[0].pending is False


def test_a_round_whose_review_block_is_broken_is_not_pending(tickets_dir):
    """블록을 읽지 못하면 골격이라 단정하지 않는다 — 산출 있음 쪽으로 남긴다."""
    path = _reserve(tickets_dir, "T-0001", "code-reviewer")
    seed = path.read_text(encoding="utf-8")
    round_store.replace_round(path, seed.replace('"confirmations"', '"confirmations'))

    loaded = round_seed.load_rounds(tickets_dir, "T-0001", ticket_text=TICKET_TEXT)
    assert loaded[0].pending is False


def test_a_seed_with_the_next_finding_id_is_still_pending(tickets_dir):
    """다음 finding ID 실값을 실은 새 시드도 산출이 없다 — 판정은 본문 자신의 값으로 재렌더한다."""
    first = _reserve(tickets_dir, "T-0001", "code-reviewer")
    round_store.replace_round(first, _reviewer_output("F-003"))
    loaded = round_seed.load_rounds(tickets_dir, "T-0001", ticket_text=TICKET_TEXT)

    seed = round_seed.render_round_seed(
        "code-reviewer", TICKET_TEXT, today=SEED_DATE,
        previous_round=round_rules.previous_round_of_role(loaded, "code-reviewer"),
        rounds=loaded,
    )
    assert '"id":"F-004"' in seed.replace(" ", ""), "다음 ID 주입이 없다(전제 붕괴)"
    _reserve(tickets_dir, "T-0001", "code-reviewer", content=seed)

    reloaded = round_seed.load_rounds(tickets_dir, "T-0001", ticket_text=TICKET_TEXT)
    assert [(item.ordinal, item.pending) for item in reloaded] == [(1, False), (2, True)]
    # CRLF 사본도 같은 골격이다.
    crlf = round_rules.Round(*reloaded[1][:3], seed.replace("\n", "\r\n"), False)
    assert round_seed.round_is_pending(crlf) is True
    # 한 글자만 채우면 산출이다.
    filled = round_rules.Round(
        *reloaded[1][:3], seed.replace("<resolved|unresolved|regressed>", "resolved"), False,
    )
    assert round_seed.round_is_pending(filled) is False


def test_an_engine_marked_round_is_not_pending_and_not_the_previous_output(
    tickets_dir, delegate,
):
    """엔진 표식이 붙은 라운드는 `pending` 을 배제하는 자리에서 함께 빠진다.

    표식이 붙는 순간 bytes 가 시드와 달라 `pending` 이 아니게 되므로, 두 배제가 같은 자리에
    없으면 종결된 예약이 직전 산출·프리필 공급원으로 선다.
    """
    first = _reserve(tickets_dir, "T-0001", "code-reviewer")
    round_store.replace_round(first, _reviewer_output("F-001"))
    second = _reserve(tickets_dir, "T-0001", "code-reviewer")
    marker = delegate.pm_review_refused_line("code-reviewer")
    round_store.replace_round(
        second, second.read_text(encoding="utf-8") + marker + "\n",
    )

    loaded = round_seed.load_rounds(tickets_dir, "T-0001", ticket_text=TICKET_TEXT)

    assert [(item.ordinal, item.pending) for item in loaded] == [(1, False), (2, False)]
    assert round_seed.verify_rounds(tickets_dir, "T-0001", ticket_text=TICKET_TEXT) == []
    assert round_rules.latest_round_of_role(loaded, "code-reviewer").ordinal == 1
    assert round_rules.previous_round_of_role(loaded, "code-reviewer")[0] == 1


def test_the_engine_marker_is_read_for_every_round_role(tickets_dir, delegate):
    """판독은 역할을 가리지 않는다 — 표식이 붙는 자리는 리뷰 채널만이 아니다."""
    for role in sorted(delegate_ledger.TICKET_COPY_ROLES):
        ticket = f"T-{9100 + sorted(delegate_ledger.TICKET_COPY_ROLES).index(role)}"
        landed = _reserve(tickets_dir, ticket, role)
        round_store.replace_round(
            landed,
            f"## 산출 ({role} · {SEED_DATE})\n\n실제 산출.\n",
        )
        marked = _reserve(tickets_dir, ticket, role)
        round_store.replace_round(
            marked,
            marked.read_text(encoding="utf-8")
            + delegate.pm_review_refused_line(role) + "\n",
        )

        loaded = round_seed.load_rounds(tickets_dir, ticket, ticket_text=TICKET_TEXT)
        assert round_seed.verify_rounds(tickets_dir, ticket, ticket_text=TICKET_TEXT) == [], role
        assert round_rules.latest_round_of_role(loaded, role).ordinal == 1, role


# ── 직전 라운드 규칙 (프리필·확인 대상의 단일 소유자) ───────────────────────

def test_latest_round_of_role_skips_rounds_without_output(tickets_dir):
    first = _reserve(tickets_dir, "T-0001", "code-reviewer")
    round_store.replace_round(first, _reviewer_output("F-001"))
    _reserve(tickets_dir, "T-0001", "code-reviewer")      # 예약만 (산출 없음)
    loaded = round_seed.load_rounds(tickets_dir, "T-0001", ticket_text=TICKET_TEXT)

    latest = round_rules.latest_round_of_role(loaded, "code-reviewer")
    assert (latest.ordinal, latest.pending) == (1, False)
    assert round_rules.previous_round_of_role(loaded, "code-reviewer") == (
        1, first.read_text(encoding="utf-8"),
    )


def test_previous_round_of_role_ignores_other_roles_and_empty_input(tickets_dir):
    developer = _reserve(tickets_dir, "T-0001", "developer")
    round_store.replace_round(developer, "## 구현 보충 (developer · 2026-01-03)\n\n산출\n")
    loaded = round_seed.load_rounds(tickets_dir, "T-0001", ticket_text=TICKET_TEXT)

    assert round_rules.previous_round_of_role(loaded, "code-reviewer") is None
    assert round_rules.latest_round_of_role([], "developer") is None
    assert round_rules.previous_round_of_role(loaded, "developer")[0] == 1


# ── 교체 (회수) ────────────────────────────────────────────────────────────

def test_replace_round_preserves_crlf_bytes_round_trip(tickets_dir):
    path = _reserve(tickets_dir, "T-0001", "developer")
    harvested = "## 구현 보충 (developer · 2026-01-03)\r\n\r\n회수 본문\r\n"
    round_store.replace_round(path, harvested)

    assert path.read_bytes() == harvested.encode("utf-8")
    loaded = round_seed.load_rounds(tickets_dir, "T-0001", ticket_text=TICKET_TEXT)
    assert loaded[0].text == harvested


def test_replace_round_leaves_no_temporary_residue(tickets_dir):
    path = _reserve(tickets_dir, "T-0001", "developer")
    round_store.replace_round(path, "## 구현 보충 (developer · 2026-01-03)\n\n회수\n")
    assert [item.name for item in path.parent.iterdir()] == ["01-developer.md"]


def test_replace_round_creates_the_file_when_it_is_missing(tickets_dir):
    directory = tickets_dir / "rounds" / "T-0001"
    directory.mkdir(parents=True)
    target = directory / "01-developer.md"
    round_store.replace_round(target, "## 구현 보충 (developer · 2026-01-03)\n\n회수\n")
    assert target.read_text(encoding="utf-8").endswith("회수\n")


# ── 판정 ───────────────────────────────────────────────────────────────────

def test_verify_rounds_is_clean_for_a_harvested_sequence(tickets_dir):
    for role in ("architect", "developer"):
        path = _reserve(tickets_dir, "T-0001", role)
        round_store.replace_round(path, f"## 산출 ({role} · 2026-01-03)\n\n내용\n")
    assert round_seed.verify_rounds(tickets_dir, "T-0001", ticket_text=TICKET_TEXT) == []


def test_verify_rounds_of_a_ticket_without_rounds_is_clean(tickets_dir):
    assert round_seed.verify_rounds(tickets_dir, "T-0404", ticket_text=TICKET_TEXT) == []


def test_verify_rounds_reports_a_gap_left_by_a_deleted_round(tickets_dir):
    for role in ("architect", "developer", "code-reviewer"):
        path = _reserve(tickets_dir, "T-0001", role)
        round_store.replace_round(path, f"## 산출 ({role} · 2026-01-03)\n\n내용\n")
    (tickets_dir / "rounds" / "T-0001" / "02-developer.md").unlink()

    problems = round_seed.verify_rounds(tickets_dir, "T-0001", ticket_text=TICKET_TEXT)
    assert [problem.code for problem in problems] == [round_rules.PROBLEM_GAP]
    assert "02" in problems[0].detail


def test_verify_rounds_reports_a_pending_round_as_information(tickets_dir):
    _reserve(tickets_dir, "T-0001", "developer")
    problems = round_seed.verify_rounds(tickets_dir, "T-0001", ticket_text=TICKET_TEXT)
    assert [problem.code for problem in problems] == [round_rules.PROBLEM_PENDING]
    assert "01-developer.md" in problems[0].detail


def test_verify_problem_codes_are_four_distinct_values():
    """소비자(lint·complete 게이트)가 한국어 detail 을 파싱하지 않고 코드로 심각도를 정한다."""
    codes = {
        round_rules.PROBLEM_NAME, round_rules.PROBLEM_GAP,
        round_rules.PROBLEM_DUPLICATE, round_rules.PROBLEM_PENDING,
    }
    assert codes == {"round-name", "round-gap", "round-dup", "round-pending"}


def test_verify_rounds_reports_a_name_that_breaks_the_grammar(tickets_dir):
    path = _reserve(tickets_dir, "T-0001", "developer")
    round_store.replace_round(path, "## 구현 보충 (developer · 2026-01-03)\n\n내용\n")
    (tickets_dir / "rounds" / "T-0001" / "notes.md").write_text("메모\n", encoding="utf-8")

    problems = round_seed.verify_rounds(tickets_dir, "T-0001", ticket_text=TICKET_TEXT)
    assert [problem.code for problem in problems] == [round_rules.PROBLEM_NAME]
    assert "notes.md" in problems[0].detail


def test_verify_rounds_reports_two_roles_holding_the_same_ordinal_as_a_duplicate(tickets_dir):
    directory = tickets_dir / "rounds" / "T-0001"
    directory.mkdir(parents=True)
    for role in ("architect", "developer"):
        (directory / f"01-{role}.md").write_text(
            f"## 산출 ({role} · 2026-01-03)\n\n내용\n", encoding="utf-8",
        )
    problems = round_seed.verify_rounds(tickets_dir, "T-0001", ticket_text=TICKET_TEXT)
    assert [problem.code for problem in problems] == [round_rules.PROBLEM_DUPLICATE]
    assert "01" in problems[0].detail
    assert "architect" in problems[0].detail and "developer" in problems[0].detail


def test_verify_rounds_does_not_call_a_trailing_gap_when_the_last_round_is_pending(
    tickets_dir,
):
    path = _reserve(tickets_dir, "T-0001", "architect")
    round_store.replace_round(path, "## 설계 (architect · 2026-01-03)\n\n내용\n")
    _reserve(tickets_dir, "T-0001", "code-reviewer")

    problems = round_seed.verify_rounds(tickets_dir, "T-0001", ticket_text=TICKET_TEXT)
    assert [problem.code for problem in problems] == [round_rules.PROBLEM_PENDING]


# ── show 렌더 ──────────────────────────────────────────────────────────────

def test_render_rounds_for_show_joins_in_ordinal_order_with_pending_marked(
    tickets_dir,
):
    architect = _reserve(tickets_dir, "T-0001", "architect")
    round_store.replace_round(architect, "## 설계 (architect · 2026-01-03)\n\n설계 산출\n")
    _reserve(tickets_dir, "T-0001", "developer")

    loaded = round_seed.load_rounds(tickets_dir, "T-0001", ticket_text=TICKET_TEXT)
    rendered = round_rules.render_rounds_for_show(loaded)

    assert rendered.startswith("--- 01-architect ---\n## 설계 (architect · 2026-01-03)")
    assert "--- 02-developer (산출 없음) ---\n" in rendered
    assert rendered.index("01-architect") < rendered.index("02-developer")


def test_render_rounds_for_show_of_nothing_is_empty():
    assert round_rules.render_rounds_for_show([]) == ""


def test_render_rounds_for_show_terminates_a_round_without_a_trailing_newline(tickets_dir):
    path = _reserve(tickets_dir, "T-0001", "developer")
    round_store.replace_round(path, "## 구현 보충 (developer · 2026-01-03)\n\n마지막 줄")
    loaded = round_seed.load_rounds(tickets_dir, "T-0001", ticket_text=TICKET_TEXT)
    assert round_rules.render_rounds_for_show(loaded).endswith("마지막 줄\n")


# ── 경계 (공용 파일 seam) ────────────────────────────────────────────────

def test_reads_and_replacements_go_through_the_shared_file_lock_seam(tickets_dir, monkeypatch):
    """판독·교체가 공용 seam 을 지난다 — 일반 `open`/`os.replace` 로 내려앉지 않는다."""
    path = _reserve(tickets_dir, "T-0001", "developer")
    seam = fs
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

    round_store.replace_round(path, "## 구현 보충 (developer · 2026-01-03)\n\n회수\n")
    round_seed.load_rounds(tickets_dir, "T-0001", ticket_text=TICKET_TEXT)

    assert calls == ["replace", "read"]


def test_reserved_round_file_is_readable_by_the_board_git_checkout(tickets_dir):
    """보드 git 이 추적하는 일반 파일이다 — 슬롯 사본과 달리 소유자 전용이 아니다."""
    path = _reserve(tickets_dir, "T-0001", "developer")
    if os.name == "nt":       # Windows 는 POSIX mode 비트를 그대로 두지 않는다.
        pytest.skip("POSIX 권한 비트 전용 단언")
    assert path.stat().st_mode & 0o777 == round_store.ROUND_FILE_MODE & ~_umask()


def _umask() -> int:
    current = os.umask(0)
    os.umask(current)
    return current
