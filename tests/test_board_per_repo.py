"""per-repo 테스트/회귀 경로 + cwd seam 단위 테스트 (T-0058·ADR-0014).

ADR-0014 의 실 엔진 개조 — multi-PM 모델에서 회귀가 multi-PM 루트(코드 없음)의 단일 `test_cmd` 를
`cwd=REPO` 로 돌던 갭(spike §8-4 c) 해소. 이 파일이 검증하는 계약:

  1. **areas.md 신 스키마** `| repo | prefix | git | test_cmd | owner |` 파싱 +
     구 스키마 `| prefix | area | owner |` 하위호환 (헤더-인식 파서).
  2. **`_test_cmd` per-repo 해소** — 활성 prefix 의 areas.md 행 test_cmd → solo 폴백
     (local.conf test_cmd → pytest -q).
  3. **회귀 cwd seam** `_regression_cwd` — 주입 시 그 경로, 미주입 시 REPO.

**hermetic 필수**: board.py 의 경로 전역(`AREAS_FILE`·`LOCAL_CONF`·`REPO` 등)은 import
시점에 실 repo 절대경로로 고정된다 — tmp 프로젝트로 monkeypatch 재지정해 실 루트의
areas.md·local.conf 를 절대 읽거나 쓰지 않는다 (test_board_multipm.py 의 패턴 동류).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
REAL_AREAS = REPO / ".project_manager" / "areas.md"


def _load_board():
    """board.py 를 (패키지 아님) importlib 로 경로 로드 — test_board_multipm 과 동일."""
    spec = importlib.util.spec_from_file_location("board", TOOLS / "board.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def board(tmp_path, monkeypatch):
    """fresh board 모듈 + areas/local.conf/REPO 전역을 tmp 로 재지정한 hermetic 인스턴스.

    board.py 의 경로 전역은 import 시점에 실 REPO 기준으로 굳는다 — 함수 scope 로 매
    테스트마다 새로 로드해 setattr 로 tmp 에 묶는다. 이로써 실 루트의 areas.md·local.conf
    를 절대 건드리지 않는다.
    """
    pm = tmp_path / ".project_manager"
    pm.mkdir(parents=True, exist_ok=True)
    (pm / ".local").mkdir(parents=True, exist_ok=True)  # board_lock 의 lock 파일 위치
    mod = _load_board()
    overrides = {
        "REPO": tmp_path,
        "AREAS_FILE": pm / "areas.md",
        "LOCAL_CONF": pm / "local.conf",
        "LOCAL_DIR": pm / ".local",
        "BOARD_LOCK": pm / ".local" / "board.lock",
        "LEASES_FILE": pm / ".local" / "worktree-leases.json",  # T-0066 슬롯 test_cmd 레이어 read
    }
    for name, val in overrides.items():
        monkeypatch.setattr(mod, name, val)
    mod._tmp = tmp_path
    return mod


# per-repo 스키마 (ADR-0014·base 없음 — T-0075 이전): | repo | prefix | git | test_cmd | owner |
_NEW_SCHEMA = (
    "# Area Registry\n\n"
    "| repo | prefix | git | test_cmd | owner |\n"
    "|---|---|---|---|---|\n"
    "| service-a | PAY | git@github.com:me/a.git | pytest -q | alice |\n"
    "| service-b | ACC | git@github.com:me/b.git | go test ./... | bob |\n"
)

# base 스키마 (T-0075): | repo | prefix | git | test_cmd | owner | base |
_BASE_SCHEMA = (
    "# Area Registry\n\n"
    "| repo | prefix | git | test_cmd | owner | base |\n"
    "|---|---|---|---|---|---|\n"
    "| service-a | PAY | git@github.com:me/a.git | pytest -q | alice | develop |\n"
    "| service-b | ACC | git@github.com:me/b.git | go test ./... | bob |  |\n"
)

# 신 스키마 (T-0076): | repo | prefix | git | test_cmd | owner | base | protected |
_PROTECTED_SCHEMA = (
    "# Area Registry\n\n"
    "| repo | prefix | git | test_cmd | owner | base | protected |\n"
    "|---|---|---|---|---|---|---|\n"
    "| service-a | PAY | git@github.com:me/a.git | pytest -q | alice | develop | main,develop |\n"
    "| service-b | ACC | git@github.com:me/b.git | go test ./... | bob |  |  |\n"
)

# 구 스키마 (ADR-0005·멀티-CLONE): | prefix | area | owner |
_OLD_SCHEMA = (
    "# Area Registry\n\n"
    "| prefix | area | owner |\n"
    "|---|---|---|\n"
    "| PAY | 결제 | alice |\n"
    "| ACC | 정산 | bob |\n"
)

# 신 스키마 (T-0161·ADR-0033 ③): | … | protected | area_owner |
_AREA_OWNER_SCHEMA = (
    "# Area Registry\n\n"
    "| repo | prefix | git | test_cmd | owner | base | protected | area_owner |\n"
    "|---|---|---|---|---|---|---|---|\n"
    "| service-a | PAY | git@github.com:me/a.git | pytest -q | alice | develop | main | alice |\n"
    "| service-b | ACC | git@github.com:me/b.git | go test ./... | bob |  |  |  |\n"
)


# ════════════════════════════════════════════════════════════════════════
# areas.md 신 스키마 파싱 (헤더-인식)
# ════════════════════════════════════════════════════════════════════════

def test_parse_areas_new_schema_maps_columns(board):
    """신 스키마 파싱 — 헤더로 칼럼명→값을 매핑한다 (repo/prefix/git/test_cmd/owner)."""
    board.AREAS_FILE.write_text(_NEW_SCHEMA, encoding="utf-8")
    header, rows = board._parse_areas()
    assert header == ["repo", "prefix", "git", "test_cmd", "owner"]
    assert len(rows) == 2
    assert rows[0] == {
        "repo": "service-a", "prefix": "PAY",
        "git": "git@github.com:me/a.git", "test_cmd": "pytest -q", "owner": "alice",
    }
    assert rows[1]["test_cmd"] == "go test ./..."


def test_parse_areas_absent_registry_is_empty(board):
    """areas.md 부재 → ([], [])."""
    assert board._parse_areas() == ([], [])


def test_areas_row_for_prefix_resolves_active_repo(board):
    """활성 prefix 의 행을 반환한다 (per-repo 해소의 기반)."""
    board.AREAS_FILE.write_text(_NEW_SCHEMA, encoding="utf-8")
    row = board._areas_row_for_prefix("ACC")
    assert row is not None and row["repo"] == "service-b"
    assert board._areas_row_for_prefix("NOPE") is None


# ── 구 스키마 하위호환 ────────────────────────────────────────────────────

def test_registered_prefixes_new_schema(board):
    """신 스키마에서 prefix 칼럼(2번째)을 헤더로 찾아 수집한다."""
    board.AREAS_FILE.write_text(_NEW_SCHEMA, encoding="utf-8")
    assert board.registered_prefixes() == {"PAY", "ACC"}


def test_registered_prefixes_old_schema_backcompat(board):
    """구 스키마(prefix 칼럼 1번째)도 그대로 수집된다 — 하위호환."""
    board.AREAS_FILE.write_text(_OLD_SCHEMA, encoding="utf-8")
    assert board.registered_prefixes() == {"PAY", "ACC"}


def test_parse_areas_old_schema_missing_columns_empty(board):
    """구 스키마 행엔 test_cmd 칼럼이 없으므로 dict 에 그 키가 없다(누락=빈 폴백 대상)."""
    board.AREAS_FILE.write_text(_OLD_SCHEMA, encoding="utf-8")
    _header, rows = board._parse_areas()
    assert rows[0]["prefix"] == "PAY"
    assert "test_cmd" not in rows[0]          # 구 스키마엔 칼럼 자체가 없음
    assert rows[0].get("test_cmd") is None     # → per-repo 해소가 솔로로 폴백


# ════════════════════════════════════════════════════════════════════════
# areas_append — 신 스키마 write (per-repo 칼럼)
# ════════════════════════════════════════════════════════════════════════

def test_areas_append_writes_new_schema_header(board):
    """부재 시 신 스키마 헤더 생성 + 행 append (repo/prefix/git/test_cmd/owner)."""
    board.areas_append("PAY", "결제", "alice",
                       repo="service-a", git="git@x:a.git", test_cmd="pytest -q")
    text = board.AREAS_FILE.read_text(encoding="utf-8")
    assert "| repo | prefix | git | test_cmd | owner |" in text
    assert "| service-a | PAY | git@x:a.git | pytest -q | alice |" in text
    assert board.registered_prefixes() == {"PAY"}
    row = board._areas_row_for_prefix("PAY")
    assert row["repo"] == "service-a" and row["test_cmd"] == "pytest -q"


def test_areas_append_legacy_call_fills_empty_repo_columns(board):
    """기존 positional 호출(prefix, area, owner)도 동작 — repo=prefix·git/test_cmd 빈 값.

    cmd_init 의 기존 호출 시그니처 하위호환. area 칼럼은 신 스키마에 없어 무시한다.
    """
    board.areas_append("PAY", "결제", "alice")
    row = board._areas_row_for_prefix("PAY")
    assert row["repo"] == "PAY"        # repo 미지정 → prefix 를 repo 명으로
    assert row["git"] == ""
    assert row["test_cmd"] == ""        # 빈 값 → per-repo 해소가 솔로 폴백
    assert row["owner"] == "alice"


def test_areas_append_appends_without_duplicate_header(board):
    """두 번째 등록은 헤더 재생성 없이 행만 추가한다."""
    board.areas_append("PAY", "결제", "alice", repo="a", test_cmd="pytest -q")
    board.areas_append("ACC", "정산", "bob", repo="b", test_cmd="go test ./...")
    text = board.AREAS_FILE.read_text(encoding="utf-8")
    assert text.count("| repo | prefix | git | test_cmd | owner | base |") == 1
    assert board.registered_prefixes() == {"PAY", "ACC"}


# ════════════════════════════════════════════════════════════════════════
# areas.md base 칼럼 (T-0075) — 파싱·하위호환·areas_append·_repo_base
# ════════════════════════════════════════════════════════════════════════

def test_parse_areas_base_schema_maps_base_column(board):
    """신 스키마 파싱 — base 칼럼을 헤더로 매핑한다 (T-0075)."""
    board.AREAS_FILE.write_text(_BASE_SCHEMA, encoding="utf-8")
    header, rows = board._parse_areas()
    assert header == ["repo", "prefix", "git", "test_cmd", "owner", "base"]
    assert rows[0]["base"] == "develop"   # 명시 base
    assert rows[1]["base"] == ""          # 빈 base(부분 등록) → _repo_base None 폴백


def test_parse_areas_old_schema_has_no_base_key(board):
    """base 칼럼 없는 per-repo 레지스트리(T-0075 이전) → 행 dict 에 base 키 없음(하위호환)."""
    board.AREAS_FILE.write_text(_NEW_SCHEMA, encoding="utf-8")
    _header, rows = board._parse_areas()
    assert "base" not in rows[0]          # 칼럼 자체가 없음
    assert rows[0].get("base") is None     # → _repo_base None 폴백(worktree 현행 bare HEAD)


def test_areas_append_writes_base_column(board):
    """areas_append(base=) → base 칼럼에 기록 + 신 스키마 헤더 생성 (T-0075)."""
    board.areas_append("PAY", "", "alice",
                       repo="service-a", git="git@x:a.git", test_cmd="pytest -q",
                       base="develop")
    text = board.AREAS_FILE.read_text(encoding="utf-8")
    assert "| repo | prefix | git | test_cmd | owner | base |" in text
    assert "| service-a | PAY | git@x:a.git | pytest -q | alice | develop |" in text
    row = board._areas_row_for_prefix("PAY")
    assert row["base"] == "develop"


def test_areas_append_base_default_empty(board):
    """areas_append(base 미지정) → base 칼럼 빈 값 (부분 등록·하위호환·_repo_base None 폴백)."""
    board.areas_append("PAY", "", "alice", repo="service-a", test_cmd="pytest -q")
    row = board._areas_row_for_prefix("PAY")
    assert row["base"] == ""               # base 미지정 → 빈 칼럼
    assert board._repo_base("service-a") is None   # 빈 값 → None 폴백


def test_repo_base_resolves_from_areas(board):
    """_repo_base(repo) → areas.md 그 repo 의 base 브랜치 (T-0075)."""
    board.AREAS_FILE.write_text(_BASE_SCHEMA, encoding="utf-8")
    assert board._repo_base("service-a") == "develop"


def test_areas_append_base_to_old_header_preserves_base(board):
    """업그레이드 프로젝트(구 5칸 헤더) + areas_append(base=) → base 유실 0 (codex T-0075 게이트).

    T-0075 이전 areas.md(헤더에 base 칼럼 없음)에 `repo add --base` 가 6칸 row 를 append 하면
    헤더는 5칸 그대로다. 파서가 헤더 길이만큼만 매핑하면 6번째(base) 셀이 유실돼 worktree add 가
    base 를 못 쓴다 → `_parse_areas` 가 헤더 넘는 셀을 canonical 칼럼으로 이어 매핑해 보존한다.
    """
    board.AREAS_FILE.write_text(_NEW_SCHEMA, encoding="utf-8")   # 구 5칸 per-repo 헤더(base 없음)
    board.areas_append("ORD", "", "carol",
                       repo="service-c", git="git@x:c.git", test_cmd="pytest -q",
                       base="develop")
    # 헤더는 5칸 그대로지만 append 된 6칸 row 의 base 가 canonical 매핑으로 보존된다.
    assert board._repo_base("service-c") == "develop"
    # 기존 구 row(base 셀 없음)는 여전히 None 폴백(회귀 0).
    assert board._repo_base("service-a") is None


def test_areas_append_base_to_3col_multiclone_header_preserves_base(board):
    """3칸 멀티-clone 헤더(`prefix|area|owner`) + areas_append(base=) → base 보존 (codex T-0075 round2).

    3칸 구 스키마는 canonical prefix 가 *아니다*(`prefix` 가 1번째) — 헤더를 넘는 셀을 canonical
    순서로 이어 매핑하면 garbling 된다. 신 row 는 항상 완전 6칸 canonical 이므로 *셀 수 == canonical
    폭* 이면 헤더 무관 canonical 매핑 → `repo`/`base` 정확. 기존 3칸 row 는 자기 헤더로(무회귀).
    """
    board.AREAS_FILE.write_text(_OLD_SCHEMA, encoding="utf-8")   # 3칸 prefix|area|owner
    board.areas_append("ORD", "", "carol",
                       repo="service-c", git="git@x:c.git", test_cmd="pytest -q",
                       base="develop")
    assert board._repo_base("service-c") == "develop"            # 6칸 row → canonical, base 정확
    # 기존 3칸 멀티-clone row(PAY/ACC)는 자기 헤더로 그대로 수집(회귀 0).
    assert set(board.registered_prefixes()) >= {"PAY", "ACC", "ORD"}


def test_parse_areas_6col_base_row_under_5col_header_preserves_base(board):
    """직전 버전(T-0075)이 5칸 헤더 아래 append 한 *6칸* base row 가 보존된다 (codex T-0076 회귀).

    T-0076 이 `_AREAS_COLUMNS` 를 7칸으로 키운 뒤 `len(cells)==canonical폭` 만 보면, T-0075 엔진이
    이미 써둔 6칸 canonical row 가 다시 헤더(5칸) 매핑으로 떨어져 `base` 가 유실된다 → `len(cells) >
    len(header)`(헤더보다 넓은 = 신 스키마 row)로 매핑해 6칸·7칸 신 row 둘 다 canonical 보존.
    """
    # 5칸 per-repo 헤더(base/protected 칼럼 없음) + T-0075 가 append 한 6칸 base row.
    areas = (
        "# Area Registry\n\n"
        "| repo | prefix | git | test_cmd | owner |\n"
        "|---|---|---|---|---|\n"
        "| service-a | PAY | git@x:a.git | pytest -q | alice |\n"           # 구 5칸 row
        "| service-c | ORD | git@x:c.git | pytest -q | carol | develop |\n"  # T-0075 6칸 base row
    )
    board.AREAS_FILE.write_text(areas, encoding="utf-8")
    assert board._repo_base("service-c") == "develop"   # 6칸 row(>5칸 헤더) → canonical, base 보존
    assert board._repo_base("service-a") is None         # 구 5칸 row → base 없음(회귀 0)


def test_repo_base_empty_base_is_none(board):
    """빈 base 칼럼(부분 등록) → None (worktree add 가 현행 bare HEAD 동작·T-0075)."""
    board.AREAS_FILE.write_text(_BASE_SCHEMA, encoding="utf-8")
    assert board._repo_base("service-b") is None   # base 칼럼 빈 값


def test_repo_base_old_schema_is_none(board):
    """base 칼럼 없는 구 레지스트리 → None (하위호환·worktree 현행 동작·T-0075)."""
    board.AREAS_FILE.write_text(_NEW_SCHEMA, encoding="utf-8")  # base 칼럼 없음
    assert board._repo_base("service-a") is None


def test_repo_base_absent_repo_is_none(board):
    """미등록 repo → None (T-0075)."""
    board.AREAS_FILE.write_text(_BASE_SCHEMA, encoding="utf-8")
    assert board._repo_base("nope") is None


def test_repo_base_no_registry_is_none(board):
    """areas.md 부재(솔로) → None (T-0075)."""
    assert board._repo_base("service-a") is None


# ════════════════════════════════════════════════════════════════════════
# areas.md protected 칼럼 (T-0076) — 파싱·default 폴백·하위호환·areas_append·_repo_protected
# ════════════════════════════════════════════════════════════════════════

def test_parse_areas_protected_schema_maps_protected_column(board):
    """신 스키마 파싱 — protected 칼럼을 헤더로 매핑한다 (T-0076)."""
    board.AREAS_FILE.write_text(_PROTECTED_SCHEMA, encoding="utf-8")
    header, rows = board._parse_areas()
    assert header == ["repo", "prefix", "git", "test_cmd", "owner", "base", "protected"]
    assert rows[0]["protected"] == "main,develop"   # 명시 protected(쉼표분리)
    assert rows[1]["protected"] == ""               # 빈 protected → default 폴백


def test_default_protected_constant(board):
    """엔진 default 상수 = (main, master, develop) (T-0076)."""
    assert board.DEFAULT_PROTECTED == ("main", "master", "develop")


def test_repo_protected_resolves_explicit_list(board):
    """_repo_protected(repo) → areas.md 그 repo 의 protected 칼럼(쉼표분리·strip) (T-0076)."""
    board.AREAS_FILE.write_text(_PROTECTED_SCHEMA, encoding="utf-8")
    assert board._repo_protected("service-a") == ["main", "develop"]


def test_repo_protected_empty_column_falls_back_to_default(board):
    """빈 protected 칼럼(부분 등록) → DEFAULT_PROTECTED 폴백 (T-0076·_repo_base 와 다름)."""
    board.AREAS_FILE.write_text(_PROTECTED_SCHEMA, encoding="utf-8")
    assert board._repo_protected("service-b") == ["main", "master", "develop"]


def test_repo_protected_old_schema_falls_back_to_default(board):
    """protected 칼럼 없는 구 레지스트리(base/per-repo) → DEFAULT_PROTECTED 폴백 (하위호환·T-0076)."""
    board.AREAS_FILE.write_text(_BASE_SCHEMA, encoding="utf-8")  # protected 칼럼 없음
    assert board._repo_protected("service-a") == ["main", "master", "develop"]


def test_repo_protected_absent_repo_falls_back_to_default(board):
    """미등록 repo → DEFAULT_PROTECTED 폴백 (안전 기본값·T-0076)."""
    board.AREAS_FILE.write_text(_PROTECTED_SCHEMA, encoding="utf-8")
    assert board._repo_protected("nope") == ["main", "master", "develop"]


def test_repo_protected_no_registry_falls_back_to_default(board):
    """areas.md 부재(솔로) → DEFAULT_PROTECTED 폴백 (T-0076)."""
    assert board._repo_protected("service-a") == ["main", "master", "develop"]


def test_repo_protected_strips_and_drops_empty_tokens(board):
    """쉼표분리 토큰 strip + 빈 토큰 제거 — `main, , develop,` → [main, develop] (T-0076)."""
    schema = (
        "# Area Registry\n\n"
        "| repo | prefix | git | test_cmd | owner | base | protected |\n"
        "|---|---|---|---|---|---|---|\n"
        "| service-a | PAY | g | pytest -q | alice | develop | main, , develop, |\n"
    )
    board.AREAS_FILE.write_text(schema, encoding="utf-8")
    assert board._repo_protected("service-a") == ["main", "develop"]


def test_areas_append_writes_protected_column(board):
    """areas_append(protected=) → protected 칼럼에 기록 + 신 7칸 스키마 헤더 생성 (T-0076)."""
    board.areas_append("PAY", "", "alice",
                       repo="service-a", git="git@x:a.git", test_cmd="pytest -q",
                       base="develop", protected="main,develop")
    text = board.AREAS_FILE.read_text(encoding="utf-8")
    assert "| repo | prefix | git | test_cmd | owner | base | protected |" in text
    assert ("| service-a | PAY | git@x:a.git | pytest -q | alice | develop "
            "| main,develop |") in text
    assert board._repo_protected("service-a") == ["main", "develop"]


def test_areas_append_protected_default_empty_falls_back(board):
    """areas_append(protected 미지정) → 빈 칼럼 + _repo_protected DEFAULT 폴백 (T-0076)."""
    board.areas_append("PAY", "", "alice", repo="service-a", test_cmd="pytest -q")
    row = board._areas_row_for_prefix("PAY")
    assert row["protected"] == ""                # protected 미지정 → 빈 칼럼
    assert board._repo_protected("service-a") == ["main", "master", "develop"]


def test_areas_append_protected_to_base_header_preserves_protected(board):
    """업그레이드(구 6칸 base 헤더) + areas_append(protected=) → protected 보존 (canonical 폭 가드·T-0076).

    T-0075 base 헤더(6칸·protected 없음)에 7칸 row 가 append 되면, 헤더 길이만큼만 매핑 시
    7번째(protected) 셀이 유실된다 → `_parse_areas` 가 셀 수 == canonical 폭(7)이면 헤더 무관
    canonical 매핑(codex T-0075 게이트의 7칸 확장).
    """
    board.AREAS_FILE.write_text(_BASE_SCHEMA, encoding="utf-8")  # 구 6칸 base 헤더(protected 없음)
    board.areas_append("ORD", "", "carol",
                       repo="service-c", git="git@x:c.git", test_cmd="pytest -q",
                       base="develop", protected="main")
    # 헤더는 6칸 그대로지만 append 된 7칸 row 의 protected 가 canonical 매핑으로 보존된다.
    assert board._repo_protected("service-c") == ["main"]
    # 기존 구 base row(protected 셀 없음)는 여전히 DEFAULT 폴백(회귀 0).
    assert board._repo_protected("service-a") == ["main", "master", "develop"]


# ════════════════════════════════════════════════════════════════════════
# areas.md area_owner 칼럼 (T-0161·ADR-0033 ③) — 파싱·하위호환·areas_append·_repo_area_owner
# ════════════════════════════════════════════════════════════════════════

def test_parse_areas_area_owner_schema_maps_column(board):
    """신 8칸 스키마 파싱 — area_owner 칼럼을 헤더로 매핑한다 (T-0161)."""
    board.AREAS_FILE.write_text(_AREA_OWNER_SCHEMA, encoding="utf-8")
    header, rows = board._parse_areas()
    assert header == ["repo", "prefix", "git", "test_cmd", "owner", "base",
                      "protected", "area_owner"]
    assert rows[0]["area_owner"] == "alice"   # 명시 area_owner(단일 user 토큰)
    assert rows[1]["area_owner"] == ""        # 빈 area_owner → _repo_area_owner None 폴백


def test_areas_columns_canonical_includes_area_owner(board):
    """canonical 칼럼 순서 끝에 area_owner 가 추가됐다 (스키마 진화·T-0161)."""
    assert board._AREAS_COLUMNS == (
        "repo", "prefix", "git", "test_cmd", "owner", "base", "protected",
        "area_owner")


def test_repo_area_owner_resolves_from_areas(board):
    """_repo_area_owner(repo) → areas.md 그 repo 의 area_owner (T-0161)."""
    board.AREAS_FILE.write_text(_AREA_OWNER_SCHEMA, encoding="utf-8")
    assert board._repo_area_owner("service-a") == "alice"


def test_repo_area_owner_empty_column_is_none(board):
    """빈 area_owner 칼럼(부분 등록) → None (`--mine` 풀이 비소유 처리·T-0161)."""
    board.AREAS_FILE.write_text(_AREA_OWNER_SCHEMA, encoding="utf-8")
    assert board._repo_area_owner("service-b") is None


def test_repo_area_owner_old_schema_is_none(board):
    """area_owner 칼럼 없는 구 레지스트리(protected/base/per-repo) → None (하위호환·T-0161).

    ADR-0014 의 기존 owner 칼럼은 그대로 두고(overload 금지), area_owner 칼럼만 없으므로
    구 areas.md(7칸 protected·6칸 base·5칸 per-repo)는 무변경 동작 — area_owner None 폴백.
    """
    board.AREAS_FILE.write_text(_PROTECTED_SCHEMA, encoding="utf-8")  # 7칸·area_owner 없음
    assert board._repo_area_owner("service-a") is None
    board.AREAS_FILE.write_text(_BASE_SCHEMA, encoding="utf-8")       # 6칸·area_owner 없음
    assert board._repo_area_owner("service-a") is None
    board.AREAS_FILE.write_text(_NEW_SCHEMA, encoding="utf-8")        # 5칸·area_owner 없음
    assert board._repo_area_owner("service-a") is None


def test_repo_area_owner_does_not_overload_owner(board):
    """area_owner 는 ADR-0014 owner 와 별개 — owner(registrant)≠area_owner(user) (T-0161)."""
    board.AREAS_FILE.write_text(_AREA_OWNER_SCHEMA, encoding="utf-8")
    row = board._areas_row_for_prefix("PAY")
    assert row["owner"] == "alice"        # registrant(ADR-0014)
    assert row["area_owner"] == "alice"   # user 소유(별도 칼럼) — 값은 같아도 *다른 칼럼*
    # service-b: owner=bob 인데 area_owner 는 빈 값(두 칼럼이 독립임을 확증).
    row_b = board._areas_row_for_prefix("ACC")
    assert row_b["owner"] == "bob"
    assert row_b["area_owner"] == ""


def test_repo_area_owner_absent_repo_is_none(board):
    """미등록 repo → None (T-0161)."""
    board.AREAS_FILE.write_text(_AREA_OWNER_SCHEMA, encoding="utf-8")
    assert board._repo_area_owner("nope") is None


def test_repo_area_owner_no_registry_is_none(board):
    """areas.md 부재(솔로) → None (T-0161)."""
    assert board._repo_area_owner("service-a") is None


def test_areas_append_writes_area_owner_column(board):
    """areas_append(area_owner=) → area_owner 칼럼 기록 + 신 8칸 스키마 헤더 생성 (T-0161)."""
    board.areas_append("PAY", "", "alice",
                       repo="service-a", git="git@x:a.git", test_cmd="pytest -q",
                       base="develop", protected="main", area_owner="alice")
    text = board.AREAS_FILE.read_text(encoding="utf-8")
    assert ("| repo | prefix | git | test_cmd | owner | base | protected "
            "| area_owner |") in text
    assert ("| service-a | PAY | git@x:a.git | pytest -q | alice | develop "
            "| main | alice |") in text
    assert board._repo_area_owner("service-a") == "alice"


def test_areas_append_area_owner_default_empty(board):
    """areas_append(area_owner 미지정) → 빈 칼럼 + _repo_area_owner None 폴백 (T-0161)."""
    board.areas_append("PAY", "", "alice", repo="service-a", test_cmd="pytest -q")
    row = board._areas_row_for_prefix("PAY")
    assert row["area_owner"] == ""                       # 미지정 → 빈 칼럼
    assert board._repo_area_owner("service-a") is None    # 빈 값 → None 폴백


def test_areas_append_area_owner_to_protected_header_preserves(board):
    """업그레이드(구 7칸 protected 헤더) + areas_append(area_owner=) → area_owner 보존 (canonical 8칸 가드·T-0161).

    T-0076 protected 헤더(7칸·area_owner 없음)에 8칸 row 가 append 되면, 헤더 길이만큼만 매핑 시
    8번째(area_owner) 셀이 유실된다 → `_parse_areas` 가 셀 수 > 헤더(7칸 → 8칸 신 row)이면 헤더
    무관 canonical 매핑(codex T-0075 게이트의 8칸 확장).
    """
    board.AREAS_FILE.write_text(_PROTECTED_SCHEMA, encoding="utf-8")  # 구 7칸 protected 헤더(area_owner 없음)
    board.areas_append("ORD", "", "carol",
                       repo="service-c", git="git@x:c.git", test_cmd="pytest -q",
                       base="develop", protected="main", area_owner="carol")
    # 헤더는 7칸 그대로지만 append 된 8칸 row 의 area_owner 가 canonical 매핑으로 보존된다.
    assert board._repo_area_owner("service-c") == "carol"
    # 기존 구 protected row(area_owner 셀 없음)는 여전히 None 폴백(회귀 0).
    assert board._repo_area_owner("service-a") is None


# ════════════════════════════════════════════════════════════════════════
# _test_cmd — per-repo 해소 + 솔로 폴백
# ════════════════════════════════════════════════════════════════════════

def test_test_cmd_override_wins(board):
    """--cmd override 가 최우선 — areas/local.conf 무시."""
    board.AREAS_FILE.write_text(_NEW_SCHEMA, encoding="utf-8")
    board.LOCAL_CONF.write_text("prefix=PAY\n", encoding="utf-8")
    assert board._test_cmd("custom -x") == "custom -x"


def test_test_cmd_resolves_per_repo_from_active_prefix(board):
    """활성 prefix(local.conf)의 areas.md 행 test_cmd 를 해소한다 (per-repo).

    ACC 활성 → 그 repo 의 `go test ./...` 이 나와야 한다 (PAY 의 pytest 아님).
    이것이 ADR-0014 의 핵심 — 무력화(항상 local.conf 폴백) 시 이 단언이 FAIL.
    """
    board.AREAS_FILE.write_text(_NEW_SCHEMA, encoding="utf-8")
    board.LOCAL_CONF.write_text("prefix=ACC\ntest_cmd=pytest -q\n", encoding="utf-8")
    # local.conf test_cmd 가 pytest -q 라도, 활성 repo(ACC)의 areas test_cmd 가 우선.
    assert board._test_cmd(None) == "go test ./..."


def test_test_cmd_per_repo_distinct_prefixes(board):
    """prefix 가 다르면 다른 repo 의 test_cmd 가 나온다 (PAY→pytest, ACC→go)."""
    board.AREAS_FILE.write_text(_NEW_SCHEMA, encoding="utf-8")
    board.LOCAL_CONF.write_text("prefix=PAY\n", encoding="utf-8")
    assert board._test_cmd(None) == "pytest -q"


def test_test_cmd_solo_no_registry_falls_back_to_local_conf(board):
    """솔로(areas.md 부재) → local.conf test_cmd 폴백 (100% 하위호환)."""
    assert not board.AREAS_FILE.exists()
    board.LOCAL_CONF.write_text("test_cmd=pytest -q --strict\n", encoding="utf-8")
    assert board._test_cmd(None) == "pytest -q --strict"


def test_test_cmd_solo_no_local_conf_defaults_pytest(board):
    """솔로 + local.conf 부재 → 기본 `pytest -q`."""
    assert board._test_cmd(None) == "pytest -q"


def test_test_cmd_no_prefix_in_multi_mode_falls_back(board):
    """areas.md 존재하나 활성 prefix 미해소(local.conf prefix 없음) → 솔로 폴백."""
    board.AREAS_FILE.write_text(_NEW_SCHEMA, encoding="utf-8")
    board.LOCAL_CONF.write_text("test_cmd=fallback-cmd\n", encoding="utf-8")  # prefix 없음
    assert board._test_cmd(None) == "fallback-cmd"


def test_test_cmd_empty_areas_test_cmd_falls_back(board):
    """활성 repo 행은 있으나 test_cmd 칼럼이 빈 값 → 솔로 폴백 (부분 등록 하위호환).

    구 스키마(test_cmd 칼럼 없음)·`areas_append` 부분 등록(빈 test_cmd) 모두 이 경로.
    """
    board.AREAS_FILE.write_text(_OLD_SCHEMA, encoding="utf-8")  # test_cmd 칼럼 없음
    board.LOCAL_CONF.write_text("prefix=PAY\ntest_cmd=legacy-cmd\n", encoding="utf-8")
    assert board._test_cmd(None) == "legacy-cmd"


# ════════════════════════════════════════════════════════════════════════
# _active_slot_test_cmd / _test_cmd 슬롯 레이어 (T-0066 · ADR-0014 amend)
# board 는 worktree_pool 을 import 하지 않고 리스 장부 *파일* 을 직접 read.
# ════════════════════════════════════════════════════════════════════════

def _write_ledger(board, *leases):
    """리스 장부 파일(LEASES_FILE)에 엔트리를 직접 쓴다 (worktree_pool atomic write 동형 스키마).

    board 는 worktree_pool 을 import 하지 않으므로 테스트도 import 없이 파일 스키마로만 친다 —
    `{"leases": [...]}`. 각 엔트리 dict 는 worktree_pool.Lease.to_dict() 와 동형.
    """
    import json
    board.LEASES_FILE.parent.mkdir(parents=True, exist_ok=True)
    board.LEASES_FILE.write_text(json.dumps({"leases": list(leases)}), encoding="utf-8")


def _lease_row(*, slot, repo, session, state="leased", test_cmd=None):
    return {"slot": slot, "repo": repo, "branch": None, "session": session,
            "pid": 1, "started": "t", "state": state, "test_cmd": test_cmd}


def test_active_slot_test_cmd_matches_session_leased(board, monkeypatch):
    """활성 슬롯(session 매칭·state=leased)의 test_cmd 를 돌려준다."""
    monkeypatch.setattr(board, "session_name", lambda override=None: "me")
    _write_ledger(board, _lease_row(slot="work/A_1", repo="A", session="me",
                                    test_cmd="ctest -R hil1"))
    assert board._active_slot_test_cmd() == "ctest -R hil1"


def test_active_slot_test_cmd_absent_ledger_returns_none(board):
    """리스 장부 부재 → None (솔로/multi-PM-미배선 무영향·다음 레이어 폴백)."""
    assert not board.LEASES_FILE.exists()
    assert board._active_slot_test_cmd() is None


def test_active_slot_test_cmd_parse_failure_returns_none(board, monkeypatch):
    """장부 파싱 실패(손상 JSON) → None (fail-soft·에러로 죽지 않음)."""
    board.LEASES_FILE.parent.mkdir(parents=True, exist_ok=True)
    board.LEASES_FILE.write_text("{ not json", encoding="utf-8")
    assert board._active_slot_test_cmd() is None


def test_test_cmd_slot_layer_wins_over_areas(board, monkeypatch):
    """활성 슬롯 test_cmd 가 repo areas 보다 우선 — per-slot 레이어가 per-repo 위.

    PAY 활성(areas → pytest -q)이라도, 이 세션의 leased 슬롯 test_cmd 가 나와야 한다.
    무력화(슬롯 레이어 제거) 시 areas 의 pytest -q 가 나와 이 단언 FAIL.
    """
    monkeypatch.setattr(board, "session_name", lambda override=None: "me")
    board.AREAS_FILE.write_text(_NEW_SCHEMA, encoding="utf-8")
    board.LOCAL_CONF.write_text("prefix=PAY\n", encoding="utf-8")  # areas → pytest -q
    _write_ledger(board, _lease_row(slot="work/A_1", repo="A", session="me",
                                    test_cmd="make hil2"))
    assert board._test_cmd(None) == "make hil2"


def test_test_cmd_slot_layer_session_mismatch_ignored(board, monkeypatch):
    """다른 session 의 슬롯은 무시 — areas 로 폴백 (session 매칭 정확성).

    장부에 leased 슬롯이 있어도 그게 *다른* 세션이면 이 세션의 회귀명령이 아니다.
    """
    monkeypatch.setattr(board, "session_name", lambda override=None: "me")
    board.AREAS_FILE.write_text(_NEW_SCHEMA, encoding="utf-8")
    board.LOCAL_CONF.write_text("prefix=PAY\n", encoding="utf-8")
    _write_ledger(board, _lease_row(slot="work/A_1", repo="A", session="other",
                                    test_cmd="make hil2"))  # 다른 세션
    assert board._test_cmd(None) == "pytest -q"  # areas 폴백(슬롯 무시)


def test_test_cmd_slot_layer_non_leased_ignored(board, monkeypatch):
    """idle 슬롯(state!=leased)은 무시 — areas 로 폴백."""
    monkeypatch.setattr(board, "session_name", lambda override=None: "me")
    board.AREAS_FILE.write_text(_NEW_SCHEMA, encoding="utf-8")
    board.LOCAL_CONF.write_text("prefix=PAY\n", encoding="utf-8")
    _write_ledger(board, _lease_row(slot="work/A_1", repo="A", session="me",
                                    state="idle", test_cmd="make hil2"))
    assert board._test_cmd(None) == "pytest -q"  # idle 슬롯 무시 → areas


def test_test_cmd_slot_layer_empty_test_cmd_falls_back_to_areas(board, monkeypatch):
    """활성 슬롯은 매칭되나 test_cmd 가 빈/None → areas 폴백 (바인딩 없는 슬롯)."""
    monkeypatch.setattr(board, "session_name", lambda override=None: "me")
    board.AREAS_FILE.write_text(_NEW_SCHEMA, encoding="utf-8")
    board.LOCAL_CONF.write_text("prefix=PAY\n", encoding="utf-8")
    _write_ledger(board, _lease_row(slot="work/A_1", repo="A", session="me",
                                    test_cmd=None))  # 바인딩 없음
    assert board._test_cmd(None) == "pytest -q"  # areas 폴백


def test_test_cmd_slot_layer_absent_ledger_falls_back_to_areas(board, monkeypatch):
    """장부 부재 → areas 폴백 (슬롯 레이어 skip·per-repo 정상 동작)."""
    monkeypatch.setattr(board, "session_name", lambda override=None: "me")
    board.AREAS_FILE.write_text(_NEW_SCHEMA, encoding="utf-8")
    board.LOCAL_CONF.write_text("prefix=ACC\n", encoding="utf-8")  # areas → go test ./...
    assert not board.LEASES_FILE.exists()
    assert board._test_cmd(None) == "go test ./..."


def test_test_cmd_override_wins_over_slot(board, monkeypatch):
    """--cmd override 가 슬롯 레이어보다 우선 (최상위 우선순위 보존)."""
    monkeypatch.setattr(board, "session_name", lambda override=None: "me")
    _write_ledger(board, _lease_row(slot="work/A_1", repo="A", session="me",
                                    test_cmd="make hil2"))
    assert board._test_cmd("custom -x") == "custom -x"


def test_test_cmd_solo_no_ledger_unaffected(board):
    """솔로(장부 없음·areas 없음) → local.conf 폴백 — 슬롯 레이어 추가가 솔로 무영향."""
    assert not board.LEASES_FILE.exists()
    board.LOCAL_CONF.write_text("test_cmd=pytest -q --strict\n", encoding="utf-8")
    assert board._test_cmd(None) == "pytest -q --strict"


def test_test_cmd_slot_layer_distinct_slots_same_repo(board, monkeypatch):
    """같은 repo 의 두 슬롯이 서로 다른 test_cmd — 세션 매칭으로 *이* 세션 슬롯만 해소.

    실 동기(T-0066): 한 integration repo, 슬롯별 다른 HIL 빌드 타깃. 세션 me 가 work/A_2 를
    leased 했으면 A_2 의 test_cmd 가 나와야 한다(A_1 의 다른 세션 명령 아님).
    """
    monkeypatch.setattr(board, "session_name", lambda override=None: "me")
    _write_ledger(
        board,
        _lease_row(slot="work/A_1", repo="A", session="peer", test_cmd="make hil1"),
        _lease_row(slot="work/A_2", repo="A", session="me", test_cmd="make hil2"),
    )
    assert board._test_cmd(None) == "make hil2"


# ════════════════════════════════════════════════════════════════════════
# session_name local.conf 레이어 + default-session END-TO-END (T-0066 must-fix)
# 핵심 회귀 핀: 저장측(worktree_pool._default_session)과 매칭측(board.session_name)이
# local.conf session= 우선순위에서 *동형* 이어야 per-slot test_cmd 가 매칭된다.
# 옛 코드(저장측 local.conf 미반영)면 host-pid vs foo 로 어긋나 이 테스트가 실패한다.
# ════════════════════════════════════════════════════════════════════════

def test_session_name_reads_local_conf(board, monkeypatch):
    """board.session_name: env 없음 → local.conf `session=` (매칭측 우선순위 핀)."""
    monkeypatch.delenv("CLAUDE_SESSION_NAME", raising=False)
    board.LOCAL_CONF.write_text("session=foo\n", encoding="utf-8")
    assert board.session_name() == "foo"


def test_session_name_env_wins_over_local_conf(board, monkeypatch):
    """env 가 local.conf 보다 우선 (1층)."""
    monkeypatch.setenv("CLAUDE_SESSION_NAME", "from-env")
    board.LOCAL_CONF.write_text("session=foo\n", encoding="utf-8")
    assert board.session_name() == "from-env"


def _load_wp_for_board(board):
    """board 와 *같은 tmp 장부/local.conf* 를 공유하도록 worktree_pool 을 바인딩한다.

    board fixture 가 배선한 LEASES_FILE/LOCAL_CONF/REPO 를 그대로 worktree_pool 전역에
    꽂아, create_slot(저장측)이 쓴 장부를 board._active_slot_test_cmd(매칭측)가 읽게 한다 —
    end-to-end 로 두 모듈의 세션 해소 정합을 친다. board 는 worktree_pool 을 import 하지
    않으므로(ADR-0013) 테스트가 파일 경로로만 둘을 잇는다.
    """
    spec = importlib.util.spec_from_file_location("wp_e2e", TOOLS / "worktree_pool.py")
    wp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wp)
    proj = board._tmp
    local = proj / ".project_manager" / ".local"
    wp.REPO = proj
    wp.LOCAL_DIR = local
    wp.LEASES_FILE = board.LEASES_FILE              # 같은 장부 파일 공유
    wp.LEASES_LOCK = local / "worktree-leases.lock"
    wp.WORK_DIR = proj / "work"
    wp.REPOS_DIR = proj / ".repos"
    return wp


def test_default_session_e2e_local_conf_match(board, monkeypatch):
    """END-TO-END must-fix 핀 — env 없음·local.conf session=foo:

    worktree_pool.create_slot(session 미지정) → lease.session=foo 로 저장 →
    board._active_slot_test_cmd()(session_name→foo 매칭) 가 그 슬롯 test_cmd 반환.
    저장측이 local.conf session= 을 안 읽으면 lease.session=host-pid 라 board(foo)와
    어긋나 None → 이 단언 실패(옛 코드 회귀 검출).
    """
    monkeypatch.delenv("CLAUDE_SESSION_NAME", raising=False)
    board.LOCAL_CONF.write_text("session=foo\n", encoding="utf-8")
    wp = _load_wp_for_board(board)
    (board._tmp / ".repos").mkdir(parents=True, exist_ok=True)
    wp.bare_repo_path("A").mkdir(parents=True, exist_ok=True)  # bare 부재 가드 통과

    class _FakeGit:
        def __call__(self, argv):
            return (0, "")

    wp.create_slot("A", git_runner=_FakeGit(), test_cmd="make hil2")  # session 미지정 → _default_session
    # 매칭측: board.session_name() 도 local.conf → foo 로 해소돼야 슬롯이 매칭된다.
    assert board.session_name() == "foo"
    assert board._active_slot_test_cmd() == "make hil2"
    assert board._test_cmd(None) == "make hil2"


def test_default_session_e2e_mismatch_without_local_conf_layer(board, monkeypatch):
    """대조군 — 저장측이 host-pid(예: env 미설정·local.conf 없음)인데 매칭측이 foo 면 미스.

    local.conf 레이어가 두 측에 *일관* 적용돼야 함을 보인다: 장부엔 host-pid 세션 슬롯만
    있고 board 는 local.conf session=foo 로 매칭 → 매칭 실패 → None(폴백). 이는 비대칭
    버그의 *증상* 을 재현해, 통일 수정이 왜 필요한지 박는다.
    """
    import os
    import socket
    monkeypatch.delenv("CLAUDE_SESSION_NAME", raising=False)
    host_pid = f"{socket.gethostname()}-{os.getpid()}"
    _write_ledger(board, _lease_row(slot="work/A_1", repo="A",
                                    session=host_pid, test_cmd="make hil2"))
    board.LOCAL_CONF.write_text("session=foo\n", encoding="utf-8")  # board → foo
    assert board.session_name() == "foo"
    assert board._active_slot_test_cmd() is None  # host-pid 슬롯과 foo 매칭 안 됨


# ════════════════════════════════════════════════════════════════════════
# _active_slot_test_cmd fail-soft 가드 — 비-dict/비-list JSON (should-fix·isinstance)
# 장부 손상이 유효 JSON(list/str/num)이면 `.get`/순회 크래시 → None 폴백.
# ════════════════════════════════════════════════════════════════════════

def test_active_slot_test_cmd_non_dict_json_returns_none(board, monkeypatch):
    """장부가 dict 아닌 유효 JSON(list) → None (fail-soft·크래시 안 함)."""
    import json
    monkeypatch.setattr(board, "session_name", lambda override=None: "me")
    board.LEASES_FILE.parent.mkdir(parents=True, exist_ok=True)
    board.LEASES_FILE.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    assert board._active_slot_test_cmd() is None


def test_active_slot_test_cmd_scalar_json_returns_none(board, monkeypatch):
    """장부가 스칼라 JSON(숫자) → None (fail-soft)."""
    import json
    monkeypatch.setattr(board, "session_name", lambda override=None: "me")
    board.LEASES_FILE.parent.mkdir(parents=True, exist_ok=True)
    board.LEASES_FILE.write_text(json.dumps(42), encoding="utf-8")
    assert board._active_slot_test_cmd() is None


def test_active_slot_test_cmd_non_list_leases_returns_none(board, monkeypatch):
    """leases 키가 list 아님(dict) → None (순회 전 가드·fail-soft)."""
    import json
    monkeypatch.setattr(board, "session_name", lambda override=None: "me")
    board.LEASES_FILE.parent.mkdir(parents=True, exist_ok=True)
    board.LEASES_FILE.write_text(json.dumps({"leases": {"bad": "shape"}}), encoding="utf-8")
    assert board._active_slot_test_cmd() is None


# ════════════════════════════════════════════════════════════════════════
# _active_slot_path — 활성 슬롯 worktree 경로 해소 (T-0122 · ADR-0026)
# board 는 worktree_pool 을 import 하지 않고 리스 장부 *파일* 을 직접 read.
# ════════════════════════════════════════════════════════════════════════

def test_active_slot_path_matches_session_leased(board, monkeypatch):
    """활성 슬롯(session 매칭·state=leased)의 slot 을 REPO/slot 절대경로로 돌려준다."""
    monkeypatch.setattr(board, "session_name", lambda override=None: "me")
    _write_ledger(board, _lease_row(slot="work/A_1", repo="A", session="me"))
    assert board._active_slot_path() == str(board.REPO / "work/A_1")


def test_active_slot_path_absent_ledger_returns_none(board):
    """리스 장부 부재 → None (솔로 무영향·호출부가 REPO 폴백)."""
    assert not board.LEASES_FILE.exists()
    assert board._active_slot_path() is None


def test_active_slot_path_parse_failure_returns_none(board):
    """장부 파싱 실패(손상 JSON) → None (fail-soft·에러로 죽지 않음)."""
    board.LEASES_FILE.parent.mkdir(parents=True, exist_ok=True)
    board.LEASES_FILE.write_text("{ not json", encoding="utf-8")
    assert board._active_slot_path() is None


def test_active_slot_path_session_mismatch_returns_none(board, monkeypatch):
    """다른 session 의 leased 슬롯은 무시 → None (session 매칭 정확성)."""
    monkeypatch.setattr(board, "session_name", lambda override=None: "me")
    _write_ledger(board, _lease_row(slot="work/A_1", repo="A", session="other"))
    assert board._active_slot_path() is None


def test_active_slot_path_non_leased_returns_none(board, monkeypatch):
    """idle 슬롯(state!=leased)은 무시 → None."""
    monkeypatch.setattr(board, "session_name", lambda override=None: "me")
    _write_ledger(board, _lease_row(slot="work/A_1", repo="A", session="me", state="idle"))
    assert board._active_slot_path() is None


def test_active_slot_path_empty_slot_returns_none(board, monkeypatch):
    """매칭 행의 slot 이 비어 있으면 None (다음 레이어[REPO] 폴백)."""
    monkeypatch.setattr(board, "session_name", lambda override=None: "me")
    _write_ledger(board, _lease_row(slot=None, repo="A", session="me"))
    assert board._active_slot_path() is None


def test_active_slot_path_first_match_wins(board, monkeypatch):
    """ambiguous(매칭 다수)는 _active_slot_test_cmd 와 동일 규칙 — 첫 매칭."""
    monkeypatch.setattr(board, "session_name", lambda override=None: "me")
    _write_ledger(board,
                  _lease_row(slot="work/A_1", repo="A", session="me"),
                  _lease_row(slot="work/A_2", repo="A", session="me"))
    assert board._active_slot_path() == str(board.REPO / "work/A_1")


def test_active_slot_path_non_dict_json_returns_none(board, monkeypatch):
    """비-dict JSON → None (fail-soft 가드·isinstance)."""
    import json
    monkeypatch.setattr(board, "session_name", lambda override=None: "me")
    board.LEASES_FILE.parent.mkdir(parents=True, exist_ok=True)
    board.LEASES_FILE.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    assert board._active_slot_path() is None


# ════════════════════════════════════════════════════════════════════════
# _regression_cwd — cwd seam (주입 → 활성 슬롯 → REPO 기본)
# ════════════════════════════════════════════════════════════════════════

def test_regression_cwd_default_is_repo(board):
    """미주입+슬롯없음(솔로/multi-PM-미배선) → REPO 기본 (additive·솔로 무변경)."""
    assert not board.LEASES_FILE.exists()
    assert board._regression_cwd() == str(board.REPO)
    assert board._regression_cwd(None) == str(board.REPO)


def test_regression_cwd_injected_path_wins(board):
    """주입(CLI --cwd·worktree 경로) 시 그 경로를 쓴다."""
    worktree = str(board._tmp / "work" / "service-a_1")
    assert board._regression_cwd(worktree) == worktree


def test_regression_cwd_empty_string_falls_back_to_repo(board):
    """빈 문자열 주입은 falsy → REPO 폴백 (seam 의 방어적 기본)."""
    assert board._regression_cwd("") == str(board.REPO)


def test_regression_cwd_resolves_active_slot_when_no_override(board, monkeypatch):
    """override 없으면 lease 의 활성 슬롯 worktree 경로를 해소 (T-0122·ADR-0026)."""
    monkeypatch.setattr(board, "session_name", lambda override=None: "me")
    _write_ledger(board, _lease_row(slot="work/A_1", repo="A", session="me"))
    assert board._regression_cwd() == str(board.REPO / "work/A_1")


def test_regression_cwd_override_wins_over_active_slot(board, monkeypatch):
    """override 가 활성 슬롯보다 우선 (override > 슬롯 > REPO)."""
    monkeypatch.setattr(board, "session_name", lambda override=None: "me")
    _write_ledger(board, _lease_row(slot="work/A_1", repo="A", session="me"))
    injected = str(board._tmp / "elsewhere")
    assert board._regression_cwd(injected) == injected


def test_regression_cwd_no_matching_slot_falls_back_to_repo(board, monkeypatch):
    """장부에 이 세션 매칭 슬롯이 없으면 REPO 폴백 (additive)."""
    monkeypatch.setattr(board, "session_name", lambda override=None: "me")
    _write_ledger(board, _lease_row(slot="work/A_1", repo="A", session="other"))
    assert board._regression_cwd() == str(board.REPO)


# ════════════════════════════════════════════════════════════════════════
# hermetic 입증 — 실 루트 areas.md 무오염
# ════════════════════════════════════════════════════════════════════════

def test_real_root_areas_md_untouched(board):
    """이 모듈 실행이 실 루트 areas.md 를 만들지 않았음을 입증한다 (hermetic 가드).

    루트는 솔로(areas.md 부재). 앞선 테스트가 monkeypatch 없이 실 AREAS_FILE 에 썼다면
    여기서 실 루트 areas.md 가 존재할 것.
    """
    assert not REAL_AREAS.exists(), (
        f"실 루트 areas.md 가 생성됨 ({REAL_AREAS}) — hermetic 격리 위반")
