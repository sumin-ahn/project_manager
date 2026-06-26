"""`board list --mine` 뷰 필터 단위 테스트 (T-0164·ADR-0033 ④·spike §2.D).

단일 공유 보드 위의 *렌즈*(별도 저장 아님)를 검증한다 — `--mine` 은 두 풀의 합집합:
  (a) 내 area 의 open — status=open ∧ 그 티켓 area 의 `area_owner` == 내 user.
  (b) 내 in-progress — `claimed_by` 의 *user*(rsplit 추출) == 내 user (상태 무관).
솔로 graceful(핵심): user 미상(None)이면 빈 보드 금지 → (a)=전체 open + (b)=내 슬롯 claim
으로 폴백(현행과 사실상 동등). 무플래그 `board list` 는 무변경(additive).

이 파일이 검증하는 계약:
  1. **prefix/area 매핑** `_ticket_prefix`·`_ticket_area_owner` — ID prefix→repo→area_owner.
  2. **claimed_by user 추출** `_claimed_by_user` — `<user>/<slot>` rsplit · 슬롯-only graceful.
  3. **`--mine` 필터** (a) area_owner==me open · (b) claimed_by.user==me · 솔로 폴백 · 무변경.

**hermetic 필수**: board.py 의 경로 전역(`REPO`·`AREAS_FILE`·`TICKETS_DIR` 등)은 import
시점에 실 repo 절대경로로 고정된다 — tmp 프로젝트로 monkeypatch 재지정하고 git 폴백은
`_git_config_email` 을 stub 해 실 git config/실 루트를 절대 건드리지 않는다
(test_board_identity.py·test_board_per_repo.py 의 hermetic 패턴 동류).
"""
from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"


def _load_board():
    spec = importlib.util.spec_from_file_location("board", TOOLS / "board.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_project(root: Path) -> None:
    tickets = root / ".project_manager" / "wiki" / "tickets"
    for status in ("open", "claimed", "blocked", "done"):
        (tickets / status).mkdir(parents=True, exist_ok=True)


@pytest.fixture
def board(tmp_path, monkeypatch):
    """fresh board 모듈 + IO 전역을 tmp 프로젝트로 재지정한 hermetic 인스턴스."""
    proj = tmp_path / "proj"
    _make_project(proj)
    mod = _load_board()
    pm = proj / ".project_manager"
    wiki = pm / "wiki"
    overrides = {
        "REPO": proj,
        "TICKETS_DIR": wiki / "tickets",
        "BOARD_FILE": wiki / "board.md",
        "LOG_FILE": wiki / "log" / "current.md",
        "STATUS_FILE": wiki / "status.md",
        "LOCAL_CONF": pm / "local.conf",
        "AREAS_FILE": pm / "areas.md",
        "LOCAL_DIR": pm / ".local",
        "BOARD_LOCK": pm / ".local" / "board.lock",
    }
    for name, val in overrides.items():
        monkeypatch.setattr(mod, name, val)
    (pm / ".local").mkdir(parents=True, exist_ok=True)
    # 기본: git 폴백 미설정(None) — 실 git config 누출 차단. 명시 테스트가 덮는다.
    monkeypatch.setattr(mod, "_git_config_email", lambda: None)
    return mod


def _write_conf(board, **kv) -> None:
    board.LOCAL_CONF.write_text(
        "".join(f"{k}={v}\n" for k, v in kv.items()), encoding="utf-8")


# 두 area(PAY→alice·ACC→bob) 의 신 스키마 레지스트리 — `--mine` (a) area_owner 풀 입력.
_AREAS = (
    "# Area Registry\n\n"
    "| repo | prefix | git | test_cmd | owner | base | protected | area_owner |\n"
    "|---|---|---|---|---|---|---|---|\n"
    "| service-a | PAY | g:a | pytest -q | reg | develop | main | alice |\n"
    "| service-b | ACC | g:b | pytest -q | reg | develop | main | bob |\n"
)


def _write_areas(board) -> None:
    board.AREAS_FILE.write_text(_AREAS, encoding="utf-8")


def _seed(board, tid, status, *, claimed_by=None, title="t"):
    """status 디렉토리에 최소 frontmatter 티켓을 박는다."""
    path = board.TICKETS_DIR / status / f"{tid}-seed.md"
    board.dump_ticket(path, {"id": tid, "title": title, "status": status,
                             "claimed_by": claimed_by, "depends_on": [],
                             "tags": []}, "# seed\n")
    return path


def _list_ids(board, capsys, **flags) -> list[str]:
    """cmd_list 를 돌려 출력에서 ticket ID 목록을 추출한다."""
    args = argparse.Namespace(status=flags.get("status"), tag=flags.get("tag"),
                              mine=flags.get("mine", False))
    rc = board.cmd_list(args)
    assert rc == 0
    out = capsys.readouterr().out
    ids = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("[") and "]" in line:
            ids.append(line.split("]", 1)[1].split()[0])
    return ids


# ════════════════════════════════════════════════════════════════════════
# prefix / area_owner 매핑 — ID → repo → area_owner
# ════════════════════════════════════════════════════════════════════════

def test_ticket_prefix_extracts_namespace(board):
    assert board._ticket_prefix("T-PAY-001") == "PAY"


def test_ticket_prefix_numeric_prefix(board):
    """숫자 포함 prefix(`P0`)도 해소 — `_next_id` 가 `re.escape(prefix)` 로 `T-P0-001`
    을 발행하고 prefix 문법(`_FAMILY_SCOPE_RE`·`[A-Za-z0-9_-]+`)이 숫자를 허용한다.
    구 정규식 `[A-Za-z]+` 는 이를 legacy(None) 로 오인했다(must-fix)."""
    assert board._ticket_prefix("T-P0-001") == "P0"


def test_ticket_prefix_family_scope_hyphen_prefix(board):
    """family-scope 형 하이픈 prefix(`service-a`)도 해소 — 발행측이 prefix 를 리터럴
    삽입해 `T-service-a-001` 을 내고, 역파서는 끝의 `-NNN` 만 떼 나머지를 prefix 로 잡는다.
    구 정규식은 하이픈을 prefix 에 못 넣어 None 이었다(must-fix)."""
    assert board._ticket_prefix("T-service-a-001") == "service-a"


def test_ticket_prefix_numeric_only_prefix(board):
    """순수 숫자 prefix(`123`)도 해소 — 등록 grammar(`pm_config._REPO_NAME_RE`·
    `^[A-Za-z0-9][A-Za-z0-9_-]*$`)가 `123` 을 허용·등록하므로 소비 grammar 도 정합해야 한다.

    round-3 must-fix: 소비 grammar 가 비-숫자 1개를 강제했을 때(`[A-Za-z0-9_-]*[A-Za-z_-]…`)
    `T-123-001` 이 prefix 로 인식 안 돼(None) `_ticket_prefix`/wikilink/bootstrap 이 어긋났다.
    이 단언이 수정 전 fail."""
    assert board._ticket_prefix("T-123-001") == "123"


def test_ticket_prefix_legacy_vs_numeric_prefix_structural(board):
    """legacy `T-NNNN`(하이픈 1개) → None · 숫자 prefix `T-123-001`(하이픈 2개) → '123'.

    구분은 **구조적**이다(prefix grammar 가 순수 숫자를 포함해도 충돌 없음): full-ID regex
    `^T-(prefix)-\\d+$` 가 *내부 하이픈*(prefix-NNN 2세그먼트)을 요구하므로 `T-0164`(하이픈 1개)
    는 legacy(None), `T-123-001`(하이픈 2개)는 prefix `123` 으로 정확히 갈린다(비충돌 핀)."""
    assert board._ticket_prefix("T-0164") is None      # legacy 4자리·하이픈 1개
    assert board._ticket_prefix("T-0001") is None      # legacy·하이픈 1개
    assert board._ticket_prefix("T-123-001") == "123"  # 숫자 prefix·하이픈 2개


def test_ticket_prefix_legacy_id_is_none(board):
    """legacy `T-NNNN`(prefix 없음) → None — full-ID regex 가 내부 하이픈을 요구하는데
    하이픈이 1개뿐이라 매칭 안 됨(구조적 구분)."""
    assert board._ticket_prefix("T-0164") is None
    assert board._ticket_prefix("") is None


# ════════════════════════════════════════════════════════════════════════
# _ticket_id_from_filename — 파일명 → canonical ID (prefixed 도 추출·T-0164 감사)
# ════════════════════════════════════════════════════════════════════════

def test_ticket_id_from_filename_prefixed(board):
    """prefixed 파일명에서 canonical ID 추출 — 숫자(`P0`)·하이픈(`service-a`) prefix 포함.

    `_next_id` 가 prefixed 파일(`T-PAY-001-결제.md`)을 만드므로 파일명 파서도 같은
    grammar(`_TICKET_ID_BODY`)여야 한다. 구 정규식 `T-(?:[A-Za-z]+-)?\\d+` 는 `P0`(숫자)·
    `service-a`(하이픈 2개) prefix 를 못 잡아 ID 를 잘못 잘랐다(T-0164 감사 round-3 클래스)."""
    assert board._ticket_id_from_filename("T-PAY-001-결제모듈.md") == "T-PAY-001"
    assert board._ticket_id_from_filename("T-P0-001-우선순위.md") == "T-P0-001"
    assert board._ticket_id_from_filename("T-service-a-001-서비스.md") == "T-service-a-001"


def test_ticket_id_from_filename_legacy(board):
    """legacy `T-NNNN-…` 파일명도 그대로 추출 — prefix 없는 4자리 ID."""
    assert board._ticket_id_from_filename("T-0036-foo.md") == "T-0036"
    assert board._ticket_id_from_filename("not-a-ticket.md") is None


def test_ticket_area_owner_via_prefix_to_repo(board):
    """ID prefix → areas.md repo → area_owner 해소."""
    _write_areas(board)
    assert board._ticket_area_owner("T-PAY-001") == "alice"
    assert board._ticket_area_owner("T-ACC-007") == "bob"


def test_ticket_area_owner_unregistered_prefix_is_none(board):
    _write_areas(board)
    assert board._ticket_area_owner("T-XYZ-001") is None


def test_ticket_area_owner_legacy_id_multi_area_is_none(board):
    """no-prefix ID + area 여러 개(multi-repo)는 area_owner 미해소 → None.

    no-prefix 티켓이 multi-repo 레지스트리(area 2개)에 떨어지면 어느 area 인지 모호 →
    None 유지(sole-area 폴백은 area 가 정확히 1개일 때만·기존 동작 보존)."""
    _write_areas(board)  # PAY·ACC 두 area
    assert board._ticket_area_owner("T-0164") is None


# 솔로 self-host(T-0123·prefix-불요)의 단일 area 레지스트리 — migration 이 area_owner 를 채운 상태.
# 솔로 보드의 티켓은 no-prefix(`T-NNNN`)다. area 가 정확히 1개이므로 그 area = 그 티켓의 area.
_SOLO_AREAS = (
    "# Area Registry\n\n"
    "| repo | prefix | git | test_cmd | owner | base | protected | area_owner |\n"
    "|---|---|---|---|---|---|---|---|\n"
    "| project_manager | project_manager | g:pm | pytest -q | reg | main | main | sumin |\n"
)


def test_ticket_area_owner_solo_no_prefix_resolves_sole_area(board):
    """**T-0164 실버그**: no-prefix 티켓(`T-0162`)이 단일 area 레지스트리에서 그 area 의
    area_owner 로 해소된다(솔로 self-host·prefix-불요).

    수정 전엔 `_ticket_prefix` None → 즉시 None 반환 → migration 으로 area_owner 가 채워진
    솔로 보드의 모든 open 티켓이 `--mine` (a)에서 사라졌다(이 단언이 수정 전 fail).
    """
    board.AREAS_FILE.write_text(_SOLO_AREAS, encoding="utf-8")
    assert board._ticket_area_owner("T-0162") == "sumin"


def test_ticket_area_owner_prefix_ticket_not_intercepted_by_sole_area(board):
    """단일 area 레지스트리라도 *prefix 가 있는* 티켓은 prefix→area 매핑 경로 그대로 —
    sole-area 폴백이 prefix 티켓을 가로채지 않는다(multi-repo 정합·무회귀).

    단일 area(prefix=project_manager·area_owner=sumin)에서 *다른* prefix 티켓(`T-PAY-001`)은
    미등록 prefix → None(sole-area 의 sumin 으로 잘못 매핑되지 않음)."""
    board.AREAS_FILE.write_text(_SOLO_AREAS, encoding="utf-8")
    # 등록된 prefix 티켓은 prefix 행에서 직접 읽는다(sole-area 폴백 미경유):
    assert board._ticket_area_owner("T-project_manager-001") == "sumin"
    # 미등록 prefix 는 sole-area 로 흘러내리지 않고 None(prefix 경로 유지):
    assert board._ticket_area_owner("T-PAY-001") is None


# 두 prefix(PAY·ACC)가 *같은 `repo` 칼럼값*(monorepo)을 공유하고 각자 다른 area_owner.
# areas registry 는 prefix-unique 만 보장하고 repo-unique 는 아니다 — 모노레포 같은 합법
# 형상. 직접-읽기 수정 전의 이중홉(prefix→row→repo→_repo_area_owner 재스캔)은 repo 로
# 재스캔해 *그 repo 의 첫 행*(PAY/alice) area_owner 를 돌려줘 ACC 도 alice 로 오인했다.
_SHARED_REPO_AREAS = (
    "# Area Registry\n\n"
    "| repo | prefix | git | test_cmd | owner | base | protected | area_owner |\n"
    "|---|---|---|---|---|---|---|---|\n"
    "| monorepo | PAY | g:m | pytest -q | reg | develop | main | alice |\n"
    "| monorepo | ACC | g:m | pytest -q | reg | develop | main | bob |\n"
)


def test_ticket_area_owner_shared_repo_resolves_per_prefix(board):
    """두 prefix 가 같은 `repo` 칼럼을 공유해도 prefix 별 정확한 area_owner 해소.

    이중홉(repo 재스캔) 회귀 방지(reviewer should-fix): repo-unique 가 아니므로 repo 로
    재스캔하면 monorepo 의 첫 행(PAY/alice) area_owner 가 ACC 에도 반환됐다. prefix 행에서
    직접 읽으면(line 737 선례 동형) PAY→alice·ACC→bob 으로 각자 맞게 나온다.
    """
    board.AREAS_FILE.write_text(_SHARED_REPO_AREAS, encoding="utf-8")
    assert board._ticket_area_owner("T-PAY-001") == "alice"
    # 이중홉 시절엔 monorepo 첫 행(alice)을 반환해 fail 했어야 하는 케이스:
    assert board._ticket_area_owner("T-ACC-001") == "bob"


def test_mine_shared_repo_filters_by_correct_area_owner(board, capsys):
    """--mine (a) 가 shared-repo 에서도 prefix 별 정확한 area_owner 로 거른다.

    bob 의 --mine 은 monorepo 의 ACC(bob) open 만 — PAY(alice) open 은 제외. 이중홉이면
    ACC 도 alice 로 오인해 bob 의 --mine 에서 빠졌을 것(또는 PAY 가 잘못 포함).
    """
    _write_conf(board, user="bob", session="pm-1")
    board.AREAS_FILE.write_text(_SHARED_REPO_AREAS, encoding="utf-8")
    _seed(board, "T-PAY-001", "open")  # alice area (같은 repo·다른 prefix)
    _seed(board, "T-ACC-001", "open")  # bob area
    ids = _list_ids(board, capsys, mine=True)
    assert ids == ["T-ACC-001"]


# ════════════════════════════════════════════════════════════════════════
# claimed_by user 추출 — rsplit 규약 · 슬롯-only graceful
# ════════════════════════════════════════════════════════════════════════

def test_claimed_by_user_rsplit(board):
    """`<user>/<slot>` 에서 마지막 `/` 로 user 분리."""
    assert board._claimed_by_user("alice/project_manager_1") == "alice"
    assert board._claimed_by_user("sumin.ahn.89@gmail.com/pm-1") \
        == "sumin.ahn.89@gmail.com"


def test_claimed_by_user_rsplit_handles_slash_in_user(board):
    """user 에 `/` 가 들어가도 마지막 `/` 가 slot 분리 → user 보존(rsplit)."""
    assert board._claimed_by_user("a/b/slot") == "a/b"


def test_claimed_by_user_slot_only_is_none(board):
    """`/` 없는 구 슬롯-only 값 → None (user 미상·(b) 매칭 제외·graceful)."""
    assert board._claimed_by_user("pm-1") is None


def test_claimed_by_user_empty_is_none(board):
    assert board._claimed_by_user(None) is None
    assert board._claimed_by_user("") is None


# ════════════════════════════════════════════════════════════════════════
# --mine 필터 (a) area_owner==me open
# ════════════════════════════════════════════════════════════════════════

def test_mine_includes_my_area_open(board, capsys):
    """(a) status=open ∧ area_owner==내 user 인 티켓만(다른 area open 제외)."""
    _write_conf(board, user="alice", session="pm-1")
    _write_areas(board)
    _seed(board, "T-PAY-001", "open")  # alice area
    _seed(board, "T-ACC-001", "open")  # bob area
    ids = _list_ids(board, capsys, mine=True)
    assert ids == ["T-PAY-001"]


def test_mine_includes_numeric_prefix_area_open(board, capsys):
    """(a) 숫자 포함 prefix(`P0`) area 의 open 도 --mine 에 잡힌다 — `_ticket_prefix`
    가 `T-P0-001` 의 prefix 를 해소해야 area_owner==alice 가 풀려 (a) 에 든다.

    must-fix 회귀: 구 정규식(`[A-Za-z]+`)이면 `T-P0-001` 이 legacy(None)로 빠져
    area_owner None → alice 의 --mine 에서 누락된다(이 단언이 수정 전 fail)."""
    areas = (
        "# Area Registry\n\n"
        "| repo | prefix | git | test_cmd | owner | base | protected | area_owner |\n"
        "|---|---|---|---|---|---|---|---|\n"
        "| platform0 | P0 | g:p0 | pytest -q | reg | develop | main | alice |\n"
    )
    board.AREAS_FILE.write_text(areas, encoding="utf-8")
    _write_conf(board, user="alice", session="pm-1")
    _seed(board, "T-P0-001", "open")  # 숫자 포함 prefix → alice area
    ids = _list_ids(board, capsys, mine=True)
    assert ids == ["T-P0-001"]


def test_mine_includes_numeric_only_prefix_area_open(board, capsys):
    """(a) 순수 숫자 prefix(`123`) area 의 open 도 --mine 에 잡힌다 (round-3 must-fix e2e).

    `_ticket_prefix("T-123-001")` 이 '123' 으로 풀려야 area_owner==alice 가 해소돼 (a) 에 든다.
    소비 grammar 가 등록 grammar 보다 좁으면(비-숫자 강제) `T-123-001` 이 legacy(None)로 빠져
    area_owner None → alice --mine 에서 누락(이 단언이 수정 전 fail).

    (T-0164 sole-area 폴백 후 정정): 이 레지스트리는 *단일 area* 라 no-prefix 티켓 seed 는
    sole-area 폴백으로 그 area 에 흡수된다 — no-prefix 제외 검증은 multi-area 레지스트리에서만
    유효하므로 `test_ticket_area_owner_legacy_id_multi_area_is_none` 으로 분리했다. 여기선 순수
    숫자 *prefix* 해소만 단언한다(prefix 티켓이 sole-area 폴백 미경유로 정확히 잡힘)."""
    areas = (
        "# Area Registry\n\n"
        "| repo | prefix | git | test_cmd | owner | base | protected | area_owner |\n"
        "|---|---|---|---|---|---|---|---|\n"
        "| svc123 | 123 | g:123 | pytest -q | reg | develop | main | alice |\n"
    )
    board.AREAS_FILE.write_text(areas, encoding="utf-8")
    _write_conf(board, user="alice", session="pm-1")
    assert board._ticket_area_owner("T-123-001") == "alice"  # prefix→area_owner 해소
    _seed(board, "T-123-001", "open")    # 순수 숫자 prefix → alice area
    ids = _list_ids(board, capsys, mine=True)
    assert ids == ["T-123-001"]


def test_mine_excludes_other_area_open(board, capsys):
    """다른 user 의 area open 은 --mine 에서 빠진다."""
    _write_conf(board, user="bob", session="pm-1")
    _write_areas(board)
    _seed(board, "T-PAY-001", "open")  # alice area
    _seed(board, "T-ACC-001", "open")  # bob area
    ids = _list_ids(board, capsys, mine=True)
    assert ids == ["T-ACC-001"]


def test_mine_area_open_only_for_open_status(board, capsys):
    """(a) 는 open 한정 — 같은 area 라도 done/claimed 는 (a) 로 안 들어온다."""
    _write_conf(board, user="alice", session="pm-1")
    _write_areas(board)
    _seed(board, "T-PAY-001", "open")
    _seed(board, "T-PAY-002", "done")   # 내 area 지만 done → (a) 제외, claim 도 없음
    ids = _list_ids(board, capsys, mine=True)
    assert ids == ["T-PAY-001"]


# ════════════════════════════════════════════════════════════════════════
# --mine 필터 (b) claimed_by.user==me (rsplit) · 상태 무관 연속성
# ════════════════════════════════════════════════════════════════════════

def test_mine_includes_my_claim_any_status(board, capsys):
    """(b) claimed_by.user==나 면 상태 무관(claimed/done)으로 포함 — 연속성."""
    _write_conf(board, user="alice", session="pm-1")
    _write_areas(board)
    _seed(board, "T-ACC-005", "claimed", claimed_by="alice/pm-1")  # 남의 area·내 claim
    _seed(board, "T-ACC-006", "done", claimed_by="alice/pm-1")
    ids = _list_ids(board, capsys, mine=True)
    assert set(ids) == {"T-ACC-005", "T-ACC-006"}


def test_mine_excludes_others_claim(board, capsys):
    """남이 claim 한 티켓(claimed_by.user≠나)은 --mine 제외."""
    _write_conf(board, user="alice", session="pm-1")
    _write_areas(board)
    _seed(board, "T-ACC-005", "claimed", claimed_by="bob/pm-2")
    ids = _list_ids(board, capsys, mine=True)
    assert ids == []


def test_mine_claim_uses_rsplit_user(board, capsys):
    """claimed_by user 추출이 rsplit('/',1)[0] — 이메일 user 도 정확 매칭."""
    _write_conf(board, user="sumin.ahn.89@gmail.com", session="project_manager_1")
    _write_areas(board)
    _seed(board, "T-ACC-009", "claimed",
          claimed_by="sumin.ahn.89@gmail.com/project_manager_1")
    ids = _list_ids(board, capsys, mine=True)
    assert ids == ["T-ACC-009"]


def test_mine_legacy_slot_only_claim_my_slot_included(board, capsys):
    """**round-4 must-fix**: user 해소 상태에서도 legacy 슬롯-only claim 이 *내 슬롯*이면 잡힌다.

    구 데이터(`claimed_by=<slot>`·user 차원 없음)를 마이그레이션 전에도 숨기지 않는다 —
    (b) 의 `or cb == my_slot` 갈래가 내 슬롯(my_slot==pm-1)의 슬롯-only claim 을 포함한다.
    이전 단순화(_claimed_by_user 만)는 이를 누락해 내 in-progress 가 --mine 에서 빠졌다.
    """
    _write_conf(board, user="alice", session="pm-1")
    _write_areas(board)
    _seed(board, "T-ACC-005", "claimed", claimed_by="pm-1")  # 슬롯-only·내 슬롯
    ids = _list_ids(board, capsys, mine=True)
    assert ids == ["T-ACC-005"]


def test_mine_legacy_slot_only_claim_other_slot_excluded(board, capsys):
    """user 해소 상태에서 *남의* 슬롯-only claim(다른 슬롯)은 (b) 제외 — 슬롯도 user 도 불일치."""
    _write_conf(board, user="alice", session="pm-1")
    _write_areas(board)
    _seed(board, "T-ACC-005", "claimed", claimed_by="pm-2")  # 남의 슬롯·user 차원 없음
    ids = _list_ids(board, capsys, mine=True)
    assert ids == []


def test_mine_is_union_of_area_open_and_my_claim(board, capsys):
    """(a) 내 area open + (b) 내 claim 의 합집합."""
    _write_conf(board, user="alice", session="pm-1")
    _write_areas(board)
    _seed(board, "T-PAY-001", "open")                              # (a)
    _seed(board, "T-ACC-005", "claimed", claimed_by="alice/pm-1")  # (b)
    _seed(board, "T-ACC-001", "open")                              # 남의 area open → 제외
    ids = _list_ids(board, capsys, mine=True)
    assert set(ids) == {"T-PAY-001", "T-ACC-005"}


# ════════════════════════════════════════════════════════════════════════
# 솔로 graceful — user None 폴백(빈 보드 아님)
# ════════════════════════════════════════════════════════════════════════

def test_mine_solo_user_none_not_empty(board, capsys):
    """user 미상(None) + areas.md 부재(솔로): --mine 이 빈 보드를 주지 않는다.

    폴백 = 전체 open + 내 슬롯 claim → 현행 list 와 사실상 동등(spike §2.D 핵심).
    """
    # local.conf 에 session 만(user 키 없음)·git 폴백 None(fixture)·areas.md 부재.
    _write_conf(board, session="pm-1")
    _seed(board, "T-0001", "open")
    _seed(board, "T-0002", "open")
    _seed(board, "T-0003", "claimed", claimed_by="pm-1")    # 내 슬롯 claim
    _seed(board, "T-0004", "claimed", claimed_by="other")   # 남의 슬롯 claim → 제외
    ids = _list_ids(board, capsys, mine=True)
    assert set(ids) == {"T-0001", "T-0002", "T-0003"}
    assert "T-0004" not in ids


def test_mine_solo_includes_all_open(board, capsys):
    """솔로 폴백 (a) = 전체 open(area_owner 필터 비적용·user 미상이라 소유 판정 불가)."""
    _write_conf(board, session="pm-1")
    _write_areas(board)  # areas 있어도 user 미상이면 area_owner 판정 안 함
    _seed(board, "T-PAY-001", "open")
    _seed(board, "T-ACC-001", "open")
    ids = _list_ids(board, capsys, mine=True)
    assert set(ids) == {"T-PAY-001", "T-ACC-001"}


def test_mine_solo_claim_matches_my_slot(board, capsys):
    """솔로 폴백 (b) = 내 슬롯(session_name)의 claim — 슬롯-only 값 매칭."""
    _write_conf(board, session="pm-1")
    _seed(board, "T-0003", "claimed", claimed_by="pm-1")
    _seed(board, "T-0004", "done", claimed_by="pm-2")   # 남의 슬롯 → 제외
    ids = _list_ids(board, capsys, mine=True)
    assert ids == ["T-0003"]


def test_mine_no_identity_excludes_others_user_slot_claim(board, capsys):
    """**pin ④**: my_user None(무-identity) 시 남의 `<user>/<slot>` claim 을 내 것으로 오인 안 함.

    `my_user is not None and cb_user == my_user` 가드가 핵심 — None 매칭(cb_user==None==my_user)
    으로 빠질 수 있는 자리를 막는다. claimed/done 인 남의 user-slot claim 은 status 가 open 도
    아니고 slot(my_slot=pm-1)도 안 맞으므로 제외돼야 한다(빈 결과).
    """
    _write_conf(board, session="pm-1")  # user 미상(None)
    _seed(board, "T-0004", "claimed", claimed_by="bob/pm-2")  # 남의 user-slot
    _seed(board, "T-0005", "done", claimed_by="carol/pm-3")   # 또 다른 남
    ids = _list_ids(board, capsys, mine=True)
    assert ids == []   # open 아님·내 슬롯 아님 → 무-identity 라도 안 잡힘


# ════════════════════════════════════════════════════════════════════════
# graceful degrade — 전역 area_owner_in_use 1개로 (a) 범위 결정 (T-0168 단순화)
# ════════════════════════════════════════════════════════════════════════

def test_area_owner_in_use_helper(board):
    """`_area_owner_in_use` — areas.md 에 non-empty area_owner 행이 ≥1 이면 True(전역·per-user 아님)."""
    _write_areas(board)  # PAY→alice · ACC→bob (둘 다 채워짐)
    assert board._area_owner_in_use() is True


def test_area_owner_in_use_no_registry(board):
    """areas.md 부재(솔로) → area_owner 운영 안 함 → False."""
    assert board._area_owner_in_use() is False


def test_area_owner_in_use_all_empty(board):
    """area_owner 칼럼이 전부 빈 값(미마이그레이션 채택자) → False — (a) 가 전체 open 으로 degrade."""
    areas = (
        "# Area Registry\n\n"
        "| repo | prefix | git | test_cmd | owner | base | protected | area_owner |\n"
        "|---|---|---|---|---|---|---|---|\n"
        "| service-a | PAY | g:a | pytest -q | reg | develop | main |  |\n"
        "| service-b | ACC | g:b | pytest -q | reg | develop | main |  |\n"
    )
    board.AREAS_FILE.write_text(areas, encoding="utf-8")
    assert board._area_owner_in_use() is False


def test_mine_solo_user_known_but_no_registry_not_empty(board, capsys):
    """**핵심 회귀 가드**: git email 있어 my_user non-None 이지만 areas.md 부재(솔로).

    흔한 솔로 케이스 — `git config user.email` 폴백으로 my_user 가 해소되지만 area_owner 가
    보드에 운영되지 않는다(_area_owner_in_use False). (a) 가 전체 open 으로 degrade 해야 한다
    — bootstrap 기본뷰 `list --mine` 보드가 비면 안 된다(빈 보드 금지·plain list 처럼).
    """
    board._git_config_email = lambda: "solo@example.com"  # type: ignore[assignment]
    _write_conf(board, session="pm-1")  # user 키 없음 → git email 폴백 탐
    _seed(board, "T-0001", "open")
    _seed(board, "T-0002", "open")
    _seed(board, "T-0003", "claimed", claimed_by="solo@example.com/pm-1")  # 내 user-claim
    _seed(board, "T-0004", "claimed", claimed_by="other/pm-2")             # 남의 claim
    ids = _list_ids(board, capsys, mine=True)
    # area_owner 미운영 → (a) 전체 open + (b) 내 user-claim. 빈 보드 아님.
    assert set(ids) == {"T-0001", "T-0002", "T-0003"}
    assert "T-0004" not in ids   # 남의 claim 은 (b) user 매칭 제외


def test_mine_user_known_area_owner_in_use_filters_to_my_areas(board, capsys):
    """area_owner 운영 중(PAY→alice·ACC→bob) + 내(carol) area 0개 → (a) 자연히 빔.

    T-0168 단순화의 **의도된 동작 변경**: area_owner 파티션이 운영 중이면(마이그레이션됨)
    (a) 는 area_owner==me 로 좁힌다. carol 소유 area 가 0개면 (a) 는 빈다 — 이건 회귀가 아니라
    '내 area 의 open 이 없음'이라는 올바른 결과(이전 per-user 폴백은 전체 open 으로 떨어졌으나,
    데이터 정합 후엔 좁히는 게 맞다·사용자 결정 2026-06-26).
    """
    board._git_config_email = lambda: "carol"  # type: ignore[assignment]
    _write_conf(board, session="pm-1")
    _write_areas(board)  # PAY→alice · ACC→bob (carol 소유 area 없음·둘 다 채워짐)
    _seed(board, "T-PAY-001", "open")
    _seed(board, "T-ACC-001", "open")
    ids = _list_ids(board, capsys, mine=True)
    # area_owner 운영 중 → (a) 가 carol area 로 좁힘 → 0개. carol claim 도 없음 → 빔.
    assert ids == []


def test_mine_user_with_owned_area_still_filters(board, capsys):
    """area_owner 운영 중이면 (a) 필터가 제대로 좁힌다 — multi-user 동작 유지 핀.

    alice 는 PAY area_owner → area_owner_in_use True → (a) 가 alice area 로 좁힘 → bob 의
    ACC open 은 제외돼야 한다.
    """
    board._git_config_email = lambda: "alice"  # type: ignore[assignment]
    _write_conf(board, session="pm-1")
    _write_areas(board)  # PAY→alice · ACC→bob
    _seed(board, "T-PAY-001", "open")  # alice area
    _seed(board, "T-ACC-001", "open")  # bob area → alice --mine 제외
    ids = _list_ids(board, capsys, mine=True)
    assert ids == ["T-PAY-001"]


def test_mine_solo_no_prefix_open_included_when_migrated(board, capsys):
    """**T-0164 실버그 e2e**: 솔로 self-host(단일 area·area_owner=me) + migration 으로
    area_owner 채워짐 + no-prefix open 티켓 → `--mine` (a)에 잡힌다.

    PM 이 실 데이터로 잡은 버그: migration 이 area_owner 를 채워 _area_owner_in_use True 가
    되면 (a) 가 area_owner==me 로 좁혀지는데, no-prefix 티켓은 _ticket_area_owner None 이라
    제외됐고 전체-open 폴백도 안 타 솔로 보드의 모든 open 티켓이 --mine 에서 사라졌다(claim 만
    보임). sole-area 폴백으로 no-prefix 티켓이 단일 area_owner==me 로 해소돼 (a)에 든다.
    이 단언이 수정 전 fail(=빈 (a))."""
    board._git_config_email = lambda: "sumin"  # type: ignore[assignment]
    _write_conf(board, session="project_manager_1")
    board.AREAS_FILE.write_text(_SOLO_AREAS, encoding="utf-8")  # 단일 area·area_owner=sumin
    _seed(board, "T-0162", "open")  # no-prefix open
    _seed(board, "T-0163", "open")  # no-prefix open
    ids = _list_ids(board, capsys, mine=True)
    # area_owner_in_use True(migrated) 인데도 sole-area 폴백으로 no-prefix open 이 (a)에 든다.
    assert set(ids) == {"T-0162", "T-0163"}


def test_mine_all_area_owner_empty_degrades_to_all_open(board, capsys):
    """미마이그레이션 채택자(area_owner 전부 빈 값)는 (a) 전체 open 으로 degrade — 안전 핀.

    areas.md 는 있으나 area_owner 칼럼이 비어 있으면(graceful-null 우회) _area_owner_in_use
    False → user 가 해소돼도 (a) 가 전체 open. migrate-identity 전 안전 degrade.
    """
    areas = (
        "# Area Registry\n\n"
        "| repo | prefix | git | test_cmd | owner | base | protected | area_owner |\n"
        "|---|---|---|---|---|---|---|---|\n"
        "| service-a | PAY | g:a | pytest -q | reg | develop | main |  |\n"
        "| service-b | ACC | g:b | pytest -q | reg | develop | main |  |\n"
    )
    board.AREAS_FILE.write_text(areas, encoding="utf-8")
    board._git_config_email = lambda: "alice"  # type: ignore[assignment]
    _write_conf(board, session="pm-1")
    _seed(board, "T-PAY-001", "open")
    _seed(board, "T-ACC-001", "open")
    ids = _list_ids(board, capsys, mine=True)
    assert set(ids) == {"T-PAY-001", "T-ACC-001"}


# ════════════════════════════════════════════════════════════════════════
# board list (무플래그) 무변경 — additive
# ════════════════════════════════════════════════════════════════════════

def test_list_without_mine_unchanged(board, capsys):
    """무플래그 list 는 전체(모든 status·area)를 그대로 — 필터 미적용."""
    _write_conf(board, user="alice", session="pm-1")
    _write_areas(board)
    _seed(board, "T-PAY-001", "open")
    _seed(board, "T-ACC-001", "open")
    _seed(board, "T-ACC-005", "claimed", claimed_by="bob/pm-2")
    _seed(board, "T-ACC-006", "done", claimed_by="bob/pm-2")
    ids = _list_ids(board, capsys, mine=False)
    assert set(ids) == {"T-PAY-001", "T-ACC-001", "T-ACC-005", "T-ACC-006"}


def test_list_without_mine_does_not_resolve_identity(board, capsys, monkeypatch):
    """무플래그 list 는 identity 해소를 호출하지 않는다(불필요 IO 회피·additive)."""
    _seed(board, "T-0001", "open")

    def _boom(*a, **k):
        raise AssertionError("user_name must not be called without --mine")

    monkeypatch.setattr(board, "user_name", _boom)
    ids = _list_ids(board, capsys, mine=False)
    assert ids == ["T-0001"]
