"""티켓 상태 디렉토리 집합(`STATUS_DIRS`)의 단일 진실 — board 밖 소비처 3곳 (T-0839).

`board.py` 가 상태를 추가하면(`discarded` — 처분 종결·T-0781) 밖에서 그 집합을 다시 적은 자리는
조용히 옛 집합에 머문다. crash 가 없어 아무도 못 보고, 그 상태의 티켓만 표면에서 사라진다:

  - `external_review._find_ticket_file` — 그 상태의 리뷰 대상을 "board 에 없음"으로 fail-loud
  - `external_review._owns_real_board` — 그 상태만 가진 PM 홈을 빈 scaffold 로 오판
  - `pm_log` 장부 census — 그 상태 티켓이 핸드오프 스냅샷 집계에서 누락
  - `pm_bootstrap` dump — `if status in counts` 가드가 그 상태 행을 버림

여기서 고정하는 것은 **값의 단일 진실**이다: (1) 세 소비처의 상태 집합 seam 이 board 값과
같고(사본이면 red), (2) 정의를 한 곳에서 바꾸면 세 소비처의 판정이 손대지 않고 함께 바뀌며,
(3) 종전 4버킷 렌더 bytes 는 그대로고 새 상태는 뒤에 붙기만 한다. 존재 검사가 아니라 값 단언이다.
"""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

# 이 티켓이 소유한 소비처 — board 밖에서 상태 집합을 읽는 자리. 나머지 하드코딩(`pm_import`·
# `pm_update`·`ticket_finish`)은 각자 무변경 근거가 따로 있어 이 티켓 범위 밖이다.
STATUS_DIRS_CONSUMERS: tuple[str, ...] = ("external_review", "pm_log", "pm_bootstrap")

# 주입용 가짜 상태 — board 가 아직 모르는 이름이어야 "정의를 바꾸면 따라온다"를 증명한다.
FAKE_STATUS = "quarantined"


def _load(name: str):
    """도구 모듈을 (패키지 아님) importlib 로 경로 로드 — 다른 엔진 테스트와 같은 규약."""
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def board():
    return _load("board")


@pytest.fixture
def external(board):
    mod = _load("external_review")
    return mod


@pytest.fixture
def pm_log():
    return _load("pm_log")


@pytest.fixture
def bootstrap():
    return _load("pm_bootstrap")


def _seed_ticket(tickets_dir: Path, status: str, ticket_id: str) -> Path:
    path = tickets_dir / status / f"{ticket_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nid: {ticket_id}\ntouches:\n- tests\n---\n\n# {ticket_id}\n", encoding="utf-8",
    )
    return path


# ── (1) 세 소비처의 seam 값 == board 단일 진실 ────────────────────────────────

@pytest.mark.parametrize("tool", STATUS_DIRS_CONSUMERS)
def test_status_dirs_seam_equals_board_value(board, tool):
    """소비처의 `_status_dirs()` 가 board `STATUS_DIRS` 와 **값이 같다**.

    사본을 되살리면(예 `discarded` 빠진 4튜플) 여기서 값이 갈려 red 다 — 존재 검사가 아니라
    board 실값과의 등가 단언이다.
    """
    mod = _load(tool)
    assert mod._status_dirs() == tuple(board.STATUS_DIRS)
    # 사본이 남아 있었다면 통과했을 옛 집합이 지금은 board 값이 아님을 함께 못박는다.
    assert mod._status_dirs() != ("open", "claimed", "blocked", "done")


# ── (2) external_review — 경로 해소·실-board 판정이 board 집합을 따른다 ──────

def test_external_review_resolves_ticket_in_every_status_dir(external, board, tmp_path):
    """모든 `STATUS_DIRS` 상태의 티켓이 리뷰 대상 경로로 해소된다 (사본이면 `discarded` 에서 red)."""
    tickets = tmp_path / ".project_manager" / "board" / "tickets"
    for index, status in enumerate(board.STATUS_DIRS):
        _seed_ticket(tickets, status, f"T-90{index:02d}")

    for index, status in enumerate(board.STATUS_DIRS):
        resolved = external._find_ticket_file(f"T-90{index:02d}", pm_home=tmp_path)
        assert resolved == tickets / status / f"T-90{index:02d}.md"


def test_external_review_owns_real_board_matches_board_for_every_status(
    external, board, tmp_path,
):
    """상태 하나만 가진 board 를 external_review 와 board 가 **같게** 판정한다.

    `discarded` 만 가진 PM 홈은 board 기준 실 board 다 — 옛 사본은 이를 빈 scaffold(False)로
    오판했다(가드가 엉뚱한 앵커를 통과시키는 축).
    """
    for index, status in enumerate(board.STATUS_DIRS):
        pm_dir = tmp_path / f"home{index}" / ".project_manager"
        _seed_ticket(pm_dir / "board" / "tickets", status, "T-0001")
        assert external._owns_real_board(pm_dir) is True
        assert external._owns_real_board(pm_dir) is board._has_real_board(pm_dir)

    empty = tmp_path / "scaffold" / ".project_manager"
    (empty / "board" / "tickets" / "open").mkdir(parents=True)
    assert external._owns_real_board(empty) is False


def test_external_review_follows_injected_status_without_code_change(
    external, board, tmp_path, monkeypatch,
):
    """board 정의에 상태를 주입하면 경로 해소·실-board 판정이 **손대지 않고** 따라온다."""
    monkeypatch.setattr(board, "STATUS_DIRS", (*board.STATUS_DIRS, FAKE_STATUS))
    monkeypatch.setattr(external, "_load_board", lambda: board)

    tickets = tmp_path / ".project_manager" / "board" / "tickets"
    _seed_ticket(tickets, FAKE_STATUS, "T-9100")

    assert external._status_dirs()[-1] == FAKE_STATUS
    assert external._find_ticket_file("T-9100", pm_home=tmp_path) == (
        tickets / FAKE_STATUS / "T-9100.md")
    assert external._owns_real_board(tmp_path / ".project_manager") is True


# ── (3) pm_log 장부 census ───────────────────────────────────────────────────

def _census_line(pm_log_mod, pm_home: Path) -> str:
    line = [
        row for row in pm_log_mod._ledger_section(pm_home, "main").splitlines()
        if row.startswith("- board tickets: ")
    ]
    assert len(line) == 1, f"census 줄 유일성 — {line!r}"
    return line[0]


def test_pm_log_census_counts_every_status_dir(pm_log, board, tmp_path):
    """census 가 `STATUS_DIRS` 전 상태를 센다 — 상태별로 **다른 수**를 넣어 값으로 확인한다."""
    tickets = tmp_path / ".project_manager" / "board" / "tickets"
    expected = {}
    for index, status in enumerate(board.STATUS_DIRS, start=1):
        for serial in range(index):
            _seed_ticket(tickets, status, f"T-{index}{serial:03d}")
        expected[status] = index

    _tickets_dir, counts = pm_log._ticket_counts(tmp_path)
    assert counts == expected

    rendered = _census_line(pm_log, tmp_path)
    # 종전 4버킷 표기·순서는 bytes 그대로고, 새 상태는 뒤에 붙기만 한다(기존 소비자 무손상).
    assert rendered.startswith("- board tickets: open 1 / claimed 2 / blocked 3 / done 4")
    assert rendered.endswith(" / discarded 5")


def test_pm_log_census_follows_injected_status_without_code_change(
    pm_log, board, tmp_path, monkeypatch,
):
    """상태 집합에 하나 주입하면 census 집계·렌더가 손대지 않고 그 버킷을 낸다."""
    monkeypatch.setattr(
        pm_log, "_status_dirs", lambda: (*board.STATUS_DIRS, FAKE_STATUS))
    tickets = tmp_path / ".project_manager" / "board" / "tickets"
    _seed_ticket(tickets, FAKE_STATUS, "T-9200")
    _seed_ticket(tickets, FAKE_STATUS, "T-9201")

    _tickets_dir, counts = pm_log._ticket_counts(tmp_path)
    assert counts[FAKE_STATUS] == 2
    assert _census_line(pm_log, tmp_path).endswith(f" / {FAKE_STATUS} 2")


# ── (4) pm_bootstrap dump 파서·렌더 ─────────────────────────────────────────

def _board_line(status: str, ticket_id: str) -> str:
    return f"  [{status:<7}] {ticket_id}  제목  -  tag\n"


def test_pm_bootstrap_dump_counts_and_renders_every_status_dir(bootstrap, board):
    """board list 출력의 모든 `STATUS_DIRS` 행이 카운트되고 dump 줄에 표기된다.

    옛 골격(`if status in counts`)은 `discarded` 행을 조용히 버렸다 — 여기선 값으로 잡힌다.
    """
    output = "".join(
        _board_line(status, f"T-93{index:02d}")
        for index, status in enumerate(board.STATUS_DIRS)
    )
    counts = bootstrap.parse_board_counts(output)
    assert counts == dict.fromkeys(board.STATUS_DIRS, 1)

    rendered = bootstrap._format_board_counts_line(counts, "mine")
    # 종전 4버킷 표기·순서(done 머리)는 bytes 그대로, 새 상태는 꼬리에 추가만.
    assert rendered.startswith(
        "- done: 1 (mine) / open: 1 (mine) / claimed: 1 (mine) / blocked: 1 (mine)")
    assert rendered.endswith(" / discarded: 1 (mine)")


def test_pm_bootstrap_dump_follows_injected_status_without_code_change(
    bootstrap, board, monkeypatch,
):
    """상태 집합에 하나 주입하면 파서 골격과 dump 줄이 손대지 않고 그 버킷을 낸다."""
    monkeypatch.setattr(
        bootstrap, "_status_dirs", lambda: (*board.STATUS_DIRS, FAKE_STATUS))
    output = _board_line(FAKE_STATUS, "T-9400") + _board_line("open", "T-9401")

    counts = bootstrap.parse_board_counts(output)
    assert counts[FAKE_STATUS] == 1 and counts["open"] == 1
    assert bootstrap._format_board_counts_line(counts, "mine").endswith(
        f" / {FAKE_STATUS}: 1 (mine)")


def test_pm_bootstrap_still_ignores_status_board_does_not_know(bootstrap, board):
    """board 가 모르는 토큰은 여전히 무시된다 — 파생이 골격을 넓히는 게 아니다(오탐 0)."""
    output = _board_line("archived", "T-9500") + _board_line("open", "T-9501")
    counts = bootstrap.parse_board_counts(output)
    assert counts == dict.fromkeys(board.STATUS_DIRS, 0) | {"open": 1}


# ── (5) 남은 하드코딩 0 (사본 재발 차단 · AST 기반) ─────────────────────────

# 옛 가드(정규식·flat 문자열 나열만 시야)는 dict 골격·pair 목록·f-string 서브스크립트 연쇄
# 3 shape 를 놓쳤다(가드 시야 < 실제 표면). AST 로 리터럴 컨테이너(Tuple/List/Set/Dict)와
# f-string(JoinedStr) 을 직접 순회해 네 shape 를 모두 본다. 상태 이름이 3개 이상 모이면 사본으로
# 본다(`("open", "claimed")` 같은 *정당하게 좁은* 필터는 여전히 통과 — 집합 복제가 아니라 그
# 표면이 의도적으로 고른 부분집합이다).
_COPY_THRESHOLD = 3


_EXEMPT_FUNCTION = "_build_json"
_EXEMPT_DICT_KEY = "board"


def _exempt_dict_ids(tree: ast.AST) -> frozenset[int]:
    """`pm_bootstrap._build_json` 의 `board` 하위호환 4키 스키마만 위치/구조로 특정한다.

    둘 다 맞아야 예외다 — (1) 함수 이름이 `_EXEMPT_FUNCTION`(`_build_json`) 이고 (2) 그 함수
    몸통 안에서 어떤 dict 리터럴이 문자열 키 `_EXEMPT_DICT_KEY`(`"board"`) 의 값으로 곧바로
    중첩된 dict 리터럴이다. 다른 파일·다른 함수의 동형 dict(같은 4상태 키를 가진 손사본)는 이
    조건에 안 걸려 여전히 잡힌다 — 파일명이나 "값이 Call" 같은 값 종류로 면제하지 않는다.
    """
    exempt: set[int] = set()
    for func in ast.walk(tree):
        if not (isinstance(func, ast.FunctionDef) and func.name == _EXEMPT_FUNCTION):
            continue
        for node in ast.walk(func):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values):
                if (
                    isinstance(key, ast.Constant) and key.value == _EXEMPT_DICT_KEY
                    and isinstance(value, ast.Dict)
                ):
                    exempt.add(id(value))
    return frozenset(exempt)


def _literal_container_strings(
    node: ast.AST, exempt_ids: frozenset[int] = frozenset(),
) -> list[str]:
    """Tuple/List/Set/Dict 리터럴 안의 문자열 상수를 재귀 수집한다.

    리터럴 컨테이너만 타고 내려간다(Call/Name/Subscript 등 임의 표현식은 보지 않는다) — 그래야
    `("done", "open")` 같은 하드코딩 골격과 `[s for s in STATUS_DIRS]` 같은 파생 표현식이 갈린다.
    Dict 는 **키가 문자열 리터럴이면 값 노드 종류와 무관하게** 그 키를 센다 — `{"open":
    count("open"), ...}` 처럼 값이 `Call` 인 손사본도 상태 키 자체가 하드코딩이면 잡아야 한다
    (T-0839 라운드 4 반려: 옛 "값도 리터럴" 조건은 이 shape 를 전부 면제했다). 유일한 예외는
    `id(node)` 가 `exempt_ids` 에 있는 dict — `_exempt_dict_ids()` 가 위치/구조로 특정한
    `pm_bootstrap._build_json` 의 `board` 하위호환 4키 스키마뿐이다.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        found: list[str] = []
        for elt in node.elts:
            found.extend(_literal_container_strings(elt, exempt_ids))
        return found
    if isinstance(node, ast.Dict):
        if id(node) in exempt_ids:
            return []
        found = []
        for key, value in zip(node.keys, node.values):
            if key is None:
                continue
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                found.append(key.value)
        return found
    return []


def _fstring_subscript_keys(node: ast.JoinedStr) -> list[str]:
    """f-string 안의 `expr['key']` 서브스크립트 키 문자열만 수집한다.

    f-string 의 프로즈 텍스트(`f"done {...}"` 의 "done " 자체)는 보지 않는다 — 옛 4연 f-string
    (`f"done {counts['done']} … blocked {counts['blocked']}"`)은 정확히 이 subscript 키로 상태
    이름을 실었으므로 이 좁은 시야로도 잡힌다. 인접 f-string 리터럴은 파서가 이미 하나의
    `JoinedStr` 로 묶으므로(암묵적 문자열 연결) `ast.walk` 로 그 서브트리 전체를 본다.
    """
    keys: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Subscript):
            key_node = child.slice
            if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str):
                keys.append(key_node.value)
    return keys


def _status_literal_groups(source: str) -> list[tuple[str, ...]]:
    """소스에서 상태 집합 사본 후보 리터럴 그룹을 뽑는다 — 4 shape 통합 시야.

    각 Tuple/List/Set/Dict/JoinedStr 노드가 독립된 그룹이다(중첩 리터럴도 `ast.walk` 가 별도
    노드로 방문하므로 이중 집계가 아니다). `exempt_ids`(`_exempt_dict_ids()`)에 든 dict 노드만
    빈 그룹으로 접는다 — 그 dict 의 하위 리터럴(예 `**(dict if … else {})` 의 조건부 dict)은
    `ast.walk` 가 별도 노드로 계속 방문하므로 정보 손실이 없다.
    """
    tree = ast.parse(source)
    exempt_ids = _exempt_dict_ids(tree)
    groups: list[tuple[str, ...]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Tuple, ast.List, ast.Set, ast.Dict)):
            groups.append(tuple(_literal_container_strings(node, exempt_ids)))
        elif isinstance(node, ast.JoinedStr):
            groups.append(tuple(_fstring_subscript_keys(node)))
    return groups


def _find_status_set_copies(source: str, known: set[str]) -> list[tuple[str, ...]]:
    """`known` 상태 이름이 `_COPY_THRESHOLD` 개 이상 모인 리터럴 그룹만 사본으로 판정한다."""
    return [
        group for group in _status_literal_groups(source)
        if len(known.intersection(group)) >= _COPY_THRESHOLD
    ]


@pytest.mark.parametrize("tool", STATUS_DIRS_CONSUMERS)
def test_no_hardcoded_status_set_left_in_consumers(board, tool):
    """소비처 소스에 상태 집합 리터럴 사본이 하나도 남아 있지 않다 (값 단언·재발 차단).

    역방향 확인: 현재 파생 구현(`_status_dirs()`·`pm_bootstrap._board_count_parts` 등)은 이
    가드를 통과한다 — `pm_bootstrap._build_json` 의 JSON top-level 4키 dict(값이 `Call` 인
    하위호환 고정 스키마)도 `_exempt_dict_ids()` 가 위치/구조(함수 `_build_json` 안·`"board"`
    키의 값)로 특정해 면제하므로 오탐되지 않는다 — 값 종류(Call 여부)로 면제하지 않는다.
    """
    source = (TOOLS / f"{tool}.py").read_text(encoding="utf-8")
    copies = _find_status_set_copies(source, set(board.STATUS_DIRS))
    assert copies == [], f"{tool}.py 에 상태 집합 사본이 남아 있다: {copies}"


# 옛 가드가 놓친 3 shape(dict 골격·pair 목록·f-string 서브스크립트 연쇄) + 기존에 잡던 flat 튜플
# + T-0839 라운드 4 반려 대상인 call_valued_dict(값이 `Call` 인 dict 손사본 — "값도 리터럴" 조건이
# 던 옛 AST 가드는 이 shape 를 전부 면제했다). 마지막 항목은 reviewer 관측 증거의 예시 그대로다.
_REINTRODUCED_COPY_SHAPES: dict[str, str] = {
    "dict_skeleton": 'counts = {"done": 0, "open": 0, "claimed": 0, "blocked": 0}\n',
    "pair_list": (
        'PAIRS = (("done", "done"), ("open", "open"), '
        '("claimed", "claimed"), ("blocked", "blocked"))\n'
    ),
    "fstring_chain": (
        "board_summary = (\n"
        "    f\"done {counts['done']} ({_scope}) / open {counts['open']} ({_scope}) / \"\n"
        "    f\"claimed {counts['claimed']} ({_scope}) / blocked {counts['blocked']} ({_scope}).\"\n"
        ")\n"
    ),
    "flat_tuple": 'STATUS_DIRS = ("open", "claimed", "blocked", "done")\n',
    "call_valued_dict": (
        'counts = {"open": count("open"), "claimed": count("claimed"), '
        '"blocked": count("blocked"), "done": count("done")}\n'
    ),
}


@pytest.mark.parametrize("shape", sorted(_REINTRODUCED_COPY_SHAPES))
def test_hardcoded_status_guard_detects_a_reintroduced_copy(board, shape):
    """가드 민감도 — 5 shape 각각을 되살린 소스는 잡힌다(가드가 늘 green 이 아님을 실측).

    옛 정규식 가드는 `dict_skeleton`·`pair_list`·`fstring_chain` 3 shape 를 놓쳤다(시야가 flat
    문자열 나열만 봤다). 라운드 3 의 AST 가드는 그 셋을 잡았지만 `call_valued_dict`(상태 키는
    리터럴, 값은 `Call`)는 "키·값 둘 다 리터럴" 조건 때문에 여전히 놓쳤다(라운드 4 반려 F-002) —
    지금은 값 노드 종류와 무관하게 키만 보므로 다섯 shape 모두 잡힌다. 이 shape 가 red 인 동시에
    `test_no_hardcoded_status_set_left_in_consumers` 가 현재 3 소비처(진짜 파생 구현) green 을
    유지하는 것이 F-002 민감도의 두 절반이다 — 소비처 쪽엔 `pm_bootstrap._build_json` 의
    Call-valued 고정 스키마 dict 가 실존해, 이 가드 완화가 그것까지 잡지 않는지를 실측한다.
    """
    source = _REINTRODUCED_COPY_SHAPES[shape]
    copies = _find_status_set_copies(source, set(board.STATUS_DIRS))
    assert len(copies) == 1, f"{shape} shape 를 놓쳤다: {copies}"


def test_pm_update_ticket_scan_dirs_equal_board_status_dirs_plus_drafts(board):
    """pm_update 의 티켓 스캔 디렉토리 리터럴은 board STATUS_DIRS 전체 + `.drafts` 와 값이 같다."""
    mod = _load("pm_update")
    assert mod._BOARD_TICKET_SCAN_DIRS == (*board.STATUS_DIRS, ".drafts")


def test_pm_import_seeded_status_dirs_equal_board_status_dirs(board):
    """pm_import 가 신규 공유 board 에 스캐폴드하는 상태 디렉토리는 board STATUS_DIRS 와 값이 같다."""
    mod = _load("pm_import")
    assert mod._SEEDED_STATUS_DIRS == board.STATUS_DIRS
