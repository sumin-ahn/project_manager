"""multi-PM 코어 단위 + e2e 스모크 테스트 (T-0055).

board.py 의 multi-PM 경로(영역별 ID 네임스페이싱·areas 레지스트리·cmd_new prefix 가드·
solo↔multi disjoint)는 구현·문서화는 됐으나 *직접 테스트가 0* 이다 (board 테스트는 lint·
portability 뿐). solo 는 매일 도그푸딩되지만 multi 는 이 repo 에서 실행된 적도 없어 잠재
버그가 있어도 안 잡힌다 — 이 파일이 그 검증 갭을 닫는다.

**hermetic 필수**: board.py 모듈 전역(`TICKETS_DIR`·`STATUS_DIRS`·`LOCAL_CONF`·`AREAS_FILE`·
`REPO` 등)이 import 시점에 실 repo 절대경로로 고정된다 — 이를 tmp 프로젝트로 monkeypatch
재지정해 실 루트의 areas.md·tickets/·local.conf 를 절대 읽거나 쓰지 않는다
(test_pm_update.py 의 REPO monkeypatch hermetic 패턴 동류).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
REAL_AREAS = REPO / ".project_manager" / "areas.md"


def _load_board():
    """board.py 를 (패키지 아님) importlib 로 경로 로드 — test_portability 와 동일."""
    spec = importlib.util.spec_from_file_location("board", TOOLS / "board.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_project(root: Path) -> None:
    """tmp 프로젝트 골격 — tickets/{open,claimed,blocked,done}/ + _template.md.

    board.py 가 필요로 하는 디렉토리 레이아웃만 만든다. areas.md·local.conf 는 각 테스트가
    필요시 직접 생성(레지스트리 *존재* 자체가 multi-PM 신호이므로 기본 부재여야 한다).
    """
    tickets = root / ".project_manager" / "wiki" / "tickets"
    for status in ("open", "claimed", "blocked", "done"):
        (tickets / status).mkdir(parents=True, exist_ok=True)
    # cmd_new 가 읽는 본문 템플릿 — 최소 frontmatter + placeholder 토큰.
    (tickets / "_template.md").write_text(
        "---\n"
        "id: T-NNNN\n"
        "title: <제목>\n"
        "status: open\n"
        "created: YYYY-MM-DD\n"
        "claimed_by:\n"
        "claimed_at:\n"
        "completed_at:\n"
        "depends_on: []\n"
        "blocks: []\n"
        "touches: []\n"
        "estimate: small\n"
        "tags: []\n"
        "---\n\n"
        "# T-NNNN — <제목>\n\n## 목표\n채워라.\n",
        encoding="utf-8",
    )


@pytest.fixture
def board(tmp_path, monkeypatch):
    """fresh board 모듈 + 모든 IO 전역을 tmp 프로젝트로 재지정한 hermetic 인스턴스.

    board.py 의 경로 전역은 import 시점에 실 REPO 기준 절대경로로 굳는다 — 함수 scope 로
    매 테스트마다 새로 로드해 setattr 로 tmp 에 묶는다. 이로써 실 루트의 areas.md·tickets/·
    local.conf 를 절대 건드리지 않는다.
    """
    proj = tmp_path / "proj"
    _make_project(proj)
    mod = _load_board()
    pm = proj / ".project_manager"
    wiki = pm / "wiki"
    local = pm / ".local"
    overrides = {
        "REPO": proj,
        "TICKETS_DIR": wiki / "tickets",
        "TEMPLATE_FILE": wiki / "tickets" / "_template.md",
        "BOARD_FILE": wiki / "board.md",
        "LOG_FILE": wiki / "log" / "current.md",
        "STATUS_FILE": wiki / "status.md",
        "LOCAL_CONF": pm / "local.conf",
        "AREAS_FILE": pm / "areas.md",
        # 리스 장부(session_name count-based 유도·ADR-0040 D1) — tmp 로 재지정해 hermetic
        # (부재 = leased 0 = solo·기본). 단일-lease 유도 테스트는 여기에 장부를 직접 쓴다.
        "LOCAL_DIR": local,
        "LEASES_FILE": local / "worktree-leases.json",
        "PM_STATE_FILE": wiki / "pm_state.md",
        "PM_STATE_TEMPLATE": wiki / "pm_state.template.md",
    }
    for name, val in overrides.items():
        monkeypatch.setattr(mod, name, val)
    mod._proj = proj  # 테스트 편의 핸들 (tmp 프로젝트 루트)
    return mod


# ── 헬퍼: 보드에 ticket 파일을 직접 심는다 (네임스페이싱 단위용) ──────────────

def _seed_ticket(board, tid: str, status: str = "open") -> Path:
    """`{tid}-slug.md` 빈 ticket 을 status 디렉토리에 심는다 (_next_id 카운트 대상)."""
    path = board.TICKETS_DIR / status / f"{tid}-seed.md"
    board.dump_ticket(path, {"id": tid, "title": "seed", "status": status},
                      "# seed\n")
    return path


# ════════════════════════════════════════════════════════════════════════
# 단위: _next_id 네임스페이싱
# ════════════════════════════════════════════════════════════════════════

def test_next_id_solo_first_is_four_digit(board):
    """빈 보드 solo (prefix=None) → T-0001 (4자리)."""
    assert board._next_id(None) == "T-0001"


def test_next_id_prefixed_first_is_three_digit(board):
    """빈 보드 multi (prefix='PAY') → T-PAY-001 (3자리)."""
    assert board._next_id("PAY") == "T-PAY-001"


def test_next_id_solo_increments(board):
    """기존 T-0007 존재 시 solo next → T-0008."""
    _seed_ticket(board, "T-0007")
    assert board._next_id(None) == "T-0008"


def test_next_id_prefixed_increments(board):
    """기존 T-PAY-005 존재 시 prefixed next → T-PAY-006."""
    _seed_ticket(board, "T-PAY-005")
    assert board._next_id("PAY") == "T-PAY-006"


def test_next_id_counts_across_status_dirs(board):
    """카운트는 모든 status 디렉토리를 가로질러 max 를 본다 (claimed/done 포함)."""
    _seed_ticket(board, "T-0003", status="done")
    _seed_ticket(board, "T-0009", status="claimed")
    assert board._next_id(None) == "T-0010"


# ── disjoint (핵심) — solo 와 multi 네임스페이스 카운트 간섭 0 ────────────────

def test_next_id_solo_multi_disjoint(board):
    """T-0009(solo)와 T-PAY-001(multi) 공존 시 두 네임스페이스가 서로 안 센다.

    legacy regex `T-(\\d+)-` 는 `T-PAY-001` 을 매치하지 않아야 하고(그러면 solo 카운트가
    오염), prefixed regex `T-PAY-(\\d+)-` 는 `T-0009` 를 안 세야 한다. 각자 독립 +1.

    seed 를 *비대칭*(solo 9 > multi 1)으로 둬 prefixed-방향 회귀를 단독으로 잡는다 —
    prefixed 가 solo 의 더 높은 9 를 잘못 세면 `T-PAY-010` 이 되므로 `T-PAY-002` 단언이 FAIL.
    (solo-방향은 `test_next_id_legacy_regex_ignores_prefixed_files` 가 커버.)
    """
    _seed_ticket(board, "T-0009")
    _seed_ticket(board, "T-PAY-001")

    # solo 는 T-PAY-001 을 안 세므로 T-0009 다음인 T-0010.
    assert board._next_id(None) == "T-0010"
    # multi 는 T-0009 를 안 세므로(아니면 T-PAY-010) T-PAY-001 다음인 T-PAY-002.
    assert board._next_id("PAY") == "T-PAY-002"


def test_next_id_legacy_regex_ignores_prefixed_files(board):
    """오직 T-PAY-* 만 있을 때 solo next 는 여전히 T-0001 (prefixed 를 안 셈)."""
    _seed_ticket(board, "T-PAY-001")
    _seed_ticket(board, "T-PAY-002")
    assert board._next_id(None) == "T-0001"


def test_next_id_distinct_prefixes_disjoint(board):
    """서로 다른 두 prefix(PAY·ACC)도 네임스페이스가 독립이다."""
    _seed_ticket(board, "T-PAY-001")
    _seed_ticket(board, "T-PAY-002")
    _seed_ticket(board, "T-ACC-001")
    assert board._next_id("PAY") == "T-PAY-003"
    assert board._next_id("ACC") == "T-ACC-002"


# ════════════════════════════════════════════════════════════════════════
# 단위: id_prefix 3분기 (override > local.conf prefix= > None)
# ════════════════════════════════════════════════════════════════════════

def test_id_prefix_override_wins(board):
    """override 인자가 최우선 — local.conf prefix 가 있어도 무시한다."""
    board.LOCAL_CONF.write_text("prefix=ACC\n", encoding="utf-8")
    assert board.id_prefix("PAY") == "PAY"


def test_id_prefix_from_local_conf(board):
    """override 없으면 local.conf 의 prefix= 를 쓴다."""
    board.LOCAL_CONF.write_text("session=x\nprefix=ACC\n", encoding="utf-8")
    assert board.id_prefix(None) == "ACC"


def test_id_prefix_none_when_unset(board):
    """override 도 local.conf prefix 도 없으면 None (legacy solo)."""
    # local.conf 부재.
    assert board.id_prefix(None) is None
    # local.conf 는 있지만 prefix 키 없음 → 여전히 None.
    board.LOCAL_CONF.write_text("session=x\n", encoding="utf-8")
    assert board.id_prefix(None) is None


# ════════════════════════════════════════════════════════════════════════
# 단위: registered_prefixes (areas.md 파싱)
# ════════════════════════════════════════════════════════════════════════

def test_registered_prefixes_absent_registry_is_empty(board):
    """areas.md 자체가 없으면 set() — 레지스트리 부재 = solo 모드 신호."""
    assert board.registered_prefixes() == set()


def test_registered_prefixes_parses_rows_excluding_header(board):
    """데이터 행의 prefix 만 수집하고 헤더행(`| prefix |`)은 제외한다."""
    board.AREAS_FILE.write_text(
        "# Area Registry\n\n"
        "| prefix | area | owner |\n"
        "|---|---|---|\n"
        "| PAY | 결제 | alice |\n"
        "| ACC | 정산 | bob |\n",
        encoding="utf-8",
    )
    assert board.registered_prefixes() == {"PAY", "ACC"}


def test_registered_prefixes_empty_registry_body(board):
    """헤더만 있고 데이터 행이 없으면 set()."""
    board.AREAS_FILE.write_text(
        "# Area Registry\n\n| prefix | area | owner |\n|---|---|---|\n",
        encoding="utf-8",
    )
    assert board.registered_prefixes() == set()


# ════════════════════════════════════════════════════════════════════════
# 단위: areas_append (생성·append·append-only)
# ════════════════════════════════════════════════════════════════════════

def test_areas_append_creates_with_header(board):
    """areas.md 부재 시 헤더를 만들고 행을 append 한다 (ADR-0014 신 스키마).

    레거시 positional 호출(prefix, area, owner)은 repo=prefix·git/test_cmd 빈 값으로
    신 스키마 행을 쓴다 (per-repo 레지스트리·하위호환). area 칼럼은 신 스키마에 없어 무시.
    """
    assert not board.AREAS_FILE.exists()
    board.areas_append("PAY", "결제", "alice")
    assert board.AREAS_FILE.exists()
    text = board.AREAS_FILE.read_text(encoding="utf-8")
    assert "| repo | prefix | git | test_cmd | owner |" in text  # 신 스키마 헤더
    assert "| PAY | PAY |  |  | alice |" in text                  # repo=prefix 기본
    assert board.registered_prefixes() == {"PAY"}


def test_areas_append_appends_to_existing(board):
    """기존 areas.md 에 행을 추가한다 (헤더 재생성 없이)."""
    board.areas_append("PAY", "결제", "alice")
    board.areas_append("ACC", "정산", "bob")
    text = board.AREAS_FILE.read_text(encoding="utf-8")
    # 헤더는 한 번만.
    assert text.count("| repo | prefix | git | test_cmd | owner |") == 1
    assert board.registered_prefixes() == {"PAY", "ACC"}


def test_areas_append_is_append_only(board):
    """append-only — 같은 prefix 를 두 번 등록해도 기존 행을 지우지 않는다."""
    board.areas_append("PAY", "결제", "alice")
    before = board.AREAS_FILE.read_text(encoding="utf-8")
    board.areas_append("PAY", "결제-v2", "carol")
    after = board.AREAS_FILE.read_text(encoding="utf-8")
    assert before in after          # 기존 내용 보존 (덮어쓰기 아님)
    assert "| PAY | PAY |  |  | alice |" in after
    assert "| PAY | PAY |  |  | carol |" in after


# ════════════════════════════════════════════════════════════════════════
# cmd_new 가드 (areas.md 존재 시 미등록 prefix 거부)
# ════════════════════════════════════════════════════════════════════════

def _new_args(title="t", prefix=None):
    return argparse.Namespace(title=title, prefix=prefix, touches=None,
                              depends=None, tag=None, estimate="small")


def test_cmd_new_guard_rejects_unregistered_prefix(board, capsys):
    """areas.md 존재 + 미등록 prefix → 비0 + stderr 안내, ticket 미발행."""
    board.areas_append("PAY", "결제", "alice")  # PAY 만 등록
    rc = board.cmd_new(_new_args(prefix="ACC"))
    assert rc != 0
    err = capsys.readouterr().err
    assert "미등록" in err
    # 아무 ticket 도 생성되지 않아야 한다.
    assert list((board.TICKETS_DIR / "open").glob("T-*.md")) == []


def test_cmd_new_guard_rejects_missing_prefix_in_multi_mode(board, capsys):
    """등록 repo ≥2(진짜 multi 모드)인데 prefix 미해소 → 거부.

    가드 기준이 areas.md *존재* → 등록 repo *개수* 로 바뀌었다(단일 self-host 마찰 해소).
    repo 2개여야 진짜 ID 충돌 가능 → prefix 강제.
    """
    board.areas_append("PAY", "결제", "alice")
    board.areas_append("ACC", "정산", "bob")  # 등록 repo 2개 → multi 모드
    # override 없음·local.conf prefix 없음 → id_prefix None.
    rc = board.cmd_new(_new_args(prefix=None))
    assert rc != 0
    assert "prefix 필요" in capsys.readouterr().err


def test_cmd_new_guard_allows_registered_prefix(board):
    """등록된 prefix → 정상 발행 (T-PAY-001)."""
    board.areas_append("PAY", "결제", "alice")
    rc = board.cmd_new(_new_args(prefix="PAY"))
    assert rc == 0
    created = list((board.TICKETS_DIR / "open").glob("T-PAY-001-*.md"))
    assert len(created) == 1
    fm, _ = board.load_ticket(created[0])
    assert fm["id"] == "T-PAY-001"


def test_cmd_new_solo_no_registry_emits_legacy_id(board):
    """areas.md 부재(solo) → 가드 off, legacy T-NNNN 발행."""
    rc = board.cmd_new(_new_args(prefix=None))
    assert rc == 0
    created = list((board.TICKETS_DIR / "open").glob("T-0001-*.md"))
    assert len(created) == 1


def test_cmd_new_single_registered_repo_emits_legacy_id(board):
    """등록 repo 1개 + prefix 미명시 → 가드 off(충돌 없음), legacy T-NNNN 발행.

    가드 기준이 areas.md *존재* → 등록 repo *개수* 로 바뀐 핵심(Part A): 단일 self-host
    (등록 repo 1개)는 ID 충돌이 없으니 solo `T-NNNN` 을 그대로 쓴다 — areas.md 1행만으로
    multi-PM prefix 마찰을 떠안지 않게.
    """
    board.areas_append("project_manager", "프레임워크", "alice")  # 등록 repo 1개
    rc = board.cmd_new(_new_args(prefix=None))
    assert rc == 0
    created = list((board.TICKETS_DIR / "open").glob("T-0001-*.md"))
    assert len(created) == 1
    fm, _ = board.load_ticket(created[0])
    assert fm["id"] == "T-0001"


def test_cmd_new_single_registered_repo_honors_explicit_prefix(board):
    """등록 repo 1개라도 명시 prefix(그 등록값)는 존중 → prefixed ID 발행.

    가드가 ≤1 에서 off 여도, 사용자가 그 등록 prefix 를 *명시*하면 prefixed `T-PFX-NNN` 을
    발행한다(prefix optional·명시 우선).
    """
    board.areas_append("PAY", "결제", "alice")  # 등록 repo 1개
    rc = board.cmd_new(_new_args(prefix="PAY"))
    assert rc == 0
    created = list((board.TICKETS_DIR / "open").glob("T-PAY-001-*.md"))
    assert len(created) == 1
    fm, _ = board.load_ticket(created[0])
    assert fm["id"] == "T-PAY-001"


# ════════════════════════════════════════════════════════════════════════
# cmd_init team 경로
# ════════════════════════════════════════════════════════════════════════

def _init_args(prefix=None, area=None, owner=None, session=None, user=None):
    return argparse.Namespace(prefix=prefix, area=area, owner=owner,
                              session=session, user=user)


@pytest.fixture
def init_board(board, monkeypatch):
    """cmd_init 용 board — 실 git/stdin 부작용 헬퍼를 무해 stub 으로 차단한다.

    install_pre_push_hook 은 실 REPO 의 git hooks 를, prompt_external_review_optin 은
    stdin 을 건드린다. cmd_init 의 areas/local.conf 효과만 검증하려고 둘을 stub.
    PM_STATE_TEMPLATE 부재로 pm_state 생성은 자연히 skip 된다.
    """
    monkeypatch.setattr(board, "install_pre_push_hook", lambda: False)
    monkeypatch.setattr(board, "prompt_external_review_optin", lambda: None)
    return board


def test_cmd_init_team_registers_and_writes_conf(init_board):
    """init --prefix PAY --area 결제 --owner alice → areas 등록행 1개 + local.conf prefix=PAY."""
    rc = init_board.cmd_init(_init_args(prefix="PAY", area="결제", owner="alice"))
    assert rc == 0
    # areas.md 등록행 (ADR-0014 신 스키마 — repo=prefix·git/test_cmd 빈 값).
    assert init_board.registered_prefixes() == {"PAY"}
    areas = init_board.AREAS_FILE.read_text(encoding="utf-8")
    assert "| PAY | PAY |  |  | alice |" in areas
    # local.conf prefix=.
    conf = init_board.LOCAL_CONF.read_text(encoding="utf-8")
    assert "prefix=PAY" in conf


def test_cmd_init_team_rerun_no_duplicate_areas(init_board):
    """이미 등록된 prefix 로 재실행 → areas.md 중복행 없음, local.conf 만 갱신."""
    init_board.cmd_init(_init_args(prefix="PAY", area="결제", owner="alice"))
    # 재실행: --area 없이도 통과해야 한다 (이미 등록).
    rc = init_board.cmd_init(_init_args(prefix="PAY", session="pay-pm2"))
    assert rc == 0
    areas = init_board.AREAS_FILE.read_text(encoding="utf-8")
    assert areas.count("| PAY |") == 1          # 중복 등록 안 됨
    conf = init_board.LOCAL_CONF.read_text(encoding="utf-8")
    assert "prefix=PAY" in conf
    assert "session=pay-pm2" in conf            # local.conf 갱신됨


def test_cmd_init_new_prefix_without_area_rejected(init_board):
    """새 prefix 인데 --area 누락 → 거부(비0), areas.md 미생성."""
    rc = init_board.cmd_init(_init_args(prefix="NEW"))
    assert rc != 0
    assert not init_board.AREAS_FILE.exists()


def test_cmd_init_owner_defaults_to_session_name(init_board, monkeypatch):
    """--owner 누락 시 owner 가 session_name() 해소값으로 채워진다 (등록행에 반영).

    cmd_init 의 owner 기본값은 `session_name()`(override 없이) — args.session 이 아니라
    env CLAUDE_SESSION_NAME / local.conf session / host-pid 순으로 해소된다. 결정성을 위해
    env 를 고정해 그 값이 등록행 owner 로 들어가는지만 검증한다.
    """
    monkeypatch.setenv("CLAUDE_SESSION_NAME", "ambient-sess")
    rc = init_board.cmd_init(_init_args(prefix="ACC", area="정산"))
    assert rc == 0
    areas = init_board.AREAS_FILE.read_text(encoding="utf-8")
    # 신 스키마(ADR-0014): repo=prefix·git/test_cmd 빈 값·owner=session_name() 해소값.
    assert "| ACC | ACC |  |  | ambient-sess |" in areas


# ── cmd_init area_owner 해소 (T-0161 델타·ADR-0033 ③·codex must-fix) ──────────
# `init --prefix` 등록행이 area_owner 칼럼을 항상 빈 값으로 남기던 갭의 durable 가드.
# 해소 우선순위는 cmd_repo_add 와 동형: `--user` > local.conf user= > git email > None.

def test_cmd_init_area_owner_from_explicit_user(init_board, monkeypatch):
    """--user 명시가 area_owner 칼럼에 박힌다 (local.conf user=·git 폴백보다 우선)."""
    # local.conf user= 와 git 폴백이 다른 값을 줘도 --user 가 이긴다.
    init_board.LOCAL_CONF.write_text("user=conf-user\n", encoding="utf-8")
    monkeypatch.setattr(init_board, "_git_config_email", lambda: "git@x.com")
    rc = init_board.cmd_init(_init_args(prefix="PAY", area="결제", owner="alice", user="carol"))
    assert rc == 0
    # 신 8칸 스키마 끝 칼럼 area_owner=carol → _repo_area_owner 로 확증(`--mine` 풀 입력).
    assert init_board._repo_area_owner("PAY") == "carol"


def test_cmd_init_area_owner_falls_back_to_local_conf(init_board, monkeypatch):
    """--user 미지정 → local.conf user= 로 area_owner 해소 (git 폴백보다 우선)."""
    init_board.LOCAL_CONF.write_text("user=conf-user\n", encoding="utf-8")
    monkeypatch.setattr(init_board, "_git_config_email", lambda: "git@x.com")
    rc = init_board.cmd_init(_init_args(prefix="ACC", area="정산", owner="bob"))
    assert rc == 0
    assert init_board._repo_area_owner("ACC") == "conf-user"


def test_cmd_init_area_owner_falls_back_to_git_email(init_board, monkeypatch):
    """--user·local.conf user= 둘 다 미설정 → git config user.email 로 area_owner 해소."""
    # cmd_init 이 local.conf 를 write 하기 전에 user_name() 을 부르므로, user 키 없는
    # local.conf 를 미리 둬 그 경로(부재→git 폴백)를 결정적으로 탄다.
    init_board.LOCAL_CONF.write_text("session=acc-pm\n", encoding="utf-8")
    monkeypatch.setattr(init_board, "_git_config_email", lambda: "dev@example.com")
    rc = init_board.cmd_init(_init_args(prefix="ORD", area="주문", owner="carol"))
    assert rc == 0
    assert init_board._repo_area_owner("ORD") == "dev@example.com"


def test_cmd_init_area_owner_graceful_when_user_unknown(init_board, monkeypatch):
    """user 미상(--user·local.conf·git 전부 없음) → area_owner 빈 칼럼·_repo_area_owner None.

    `--mine` 풀에 안 잡히지만 등록 자체는 graceful 진행(기존 슬롯-only 동작 보존).
    """
    init_board.LOCAL_CONF.write_text("session=s\n", encoding="utf-8")
    monkeypatch.setattr(init_board, "_git_config_email", lambda: None)
    rc = init_board.cmd_init(_init_args(prefix="INV", area="재고", owner="dave"))
    assert rc == 0
    assert init_board._repo_area_owner("INV") is None
    # owner(registrant)는 정상 기록 — area_owner 만 빈 값(두 칼럼 독립·overload 금지).
    row = init_board._areas_row_for_prefix("INV")
    assert row["owner"] == "dave"
    assert row["area_owner"] == ""


# ════════════════════════════════════════════════════════════════════════
# e2e 스모크: team init → multi new → solo new 공존(disjoint) → 1사이클
# ════════════════════════════════════════════════════════════════════════

def test_e2e_team_init_then_multi_and_solo_coexist(init_board):
    """team init → T-PAY-001 발행 → 같은 보드에서 solo new 도 T-NNNN 발행되어 공존.

    multi/solo disjoint 를 *실 명령 경로*(cmd_init/cmd_new)로 확증한다. 이어 claim·
    complete 1사이클이 크래시 없이 도는지(파일 이동·게이트 통과) 검증한다.
    """
    board = init_board

    # 1. team init — PAY 등록 + local.conf prefix=PAY.
    assert board.cmd_init(_init_args(prefix="PAY", area="결제", owner="alice")) == 0
    assert board.registered_prefixes() == {"PAY"}

    # 2. multi new — local.conf prefix=PAY 로 해소되어 T-PAY-001.
    assert board.cmd_new(_new_args(title="multi ticket")) == 0
    pay = list((board.TICKETS_DIR / "open").glob("T-PAY-001-*.md"))
    assert len(pay) == 1

    # 3. solo new — 같은 보드에 override prefix="" 로? — solo 발행은 prefix 미지정 +
    #    가드 회피가 필요하다. multi 모드(areas.md 존재)에서 solo legacy ID 를 같은
    #    보드에 직접 심어 공존(disjoint)을 확증한다 (가드는 multi 모드에선 solo new 를
    #    거부하는 게 설계 — 공존은 _next_id 네임스페이스 분리로 보장됨).
    _seed_ticket(board, "T-0001")
    # solo 네임스페이스 next 는 T-PAY-001 에 간섭받지 않는다.
    assert board._next_id(None) == "T-0002"
    # multi 네임스페이스 next 는 T-0001 에 간섭받지 않는다.
    assert board._next_id("PAY") == "T-PAY-002"

    # 4. list/claim/complete 1사이클 — 크래시 없이.
    assert board.cmd_list(argparse.Namespace(status=None, tag=None)) == 0

    pay_id = "T-PAY-001"
    claim_args = argparse.Namespace(id=pay_id, session="pay-pm")
    assert board.cmd_claim(claim_args) == 0
    assert list((board.TICKETS_DIR / "claimed").glob(f"{pay_id}-*.md"))

    complete_args = argparse.Namespace(
        id=pay_id, tests_pass=True, allow_missing_log=True, allow_untested=False)
    assert board.cmd_complete(complete_args) == 0
    assert list((board.TICKETS_DIR / "done").glob(f"{pay_id}-*.md"))


def test_e2e_solo_board_no_registry_legacy_flow(init_board):
    """레지스트리 없는 solo 보드 — init(솔로)·new·claim·complete 1사이클 무크래시."""
    board = init_board
    # solo init (prefix 없음) — areas.md 안 만들어져야 한다.
    assert board.cmd_init(_init_args()) == 0
    assert not board.AREAS_FILE.exists()

    assert board.cmd_new(_new_args(title="solo ticket")) == 0
    created = list((board.TICKETS_DIR / "open").glob("T-0001-*.md"))
    assert len(created) == 1

    assert board.cmd_claim(argparse.Namespace(id="T-0001", session="pm")) == 0
    assert board.cmd_complete(argparse.Namespace(
        id="T-0001", tests_pass=True, allow_missing_log=True,
        allow_untested=False)) == 0
    assert list((board.TICKETS_DIR / "done").glob("T-0001-*.md"))


# ════════════════════════════════════════════════════════════════════════
# init framing 라벨 회귀 (T-0085·ADR-0016) — multi-PM = N 세션 × M repo 한 개념.
# "팀(team)=다중-사람 협업" framing 제거 → multi-repo (N×M·prefix 네임스페이스).
# 머시너리(prefix·areas·네임스페이스·가드)는 불변(amend·supersede 아님) — 표면 라벨만 검증.
# ════════════════════════════════════════════════════════════════════════

def test_init_namespaced_label_is_multi_repo_not_team(init_board, capsys):
    """prefix init 의 완료 라벨 = `multi-repo · <prefix>` (협업 "팀" framing 제거).

    동작(areas 등록·prefix 네임스페이스)은 다른 테스트가 커버 — 여기선 *새 framing 라벨*만
    회귀 박제한다. ID 포맷 `T-<PFX>-NNN` 도 같이 출력되어야 한다.
    """
    rc = init_board.cmd_init(_init_args(prefix="PAY", area="결제", owner="alice"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "multi-repo · PAY" in out          # 새 framing (N×M 네임스페이스)
    assert "T-PAY-NNN" in out                  # 네임스페이스 ID 포맷 라벨
    assert "팀" not in out                      # 협업 framing 제거 (ADR-0016·ADR-0002 amend)


def test_init_solo_label_is_n1_m1(init_board, capsys):
    """prefix 없는 init 의 완료 라벨 = `solo (N=1·M=1)` + legacy `T-NNNN`.

    solo 경로는 N=1·M=1 trivial 경로 — 오버헤드 0·legacy ID. 새 framing 라벨 회귀.
    """
    rc = init_board.cmd_init(_init_args())
    assert rc == 0
    out = capsys.readouterr().out
    assert "solo (N=1·M=1)" in out             # 새 framing (trivial 경로 명시)
    assert "T-NNNN (legacy)" in out            # legacy ID 포맷 보존
    assert "팀" not in out


def test_init_solo_no_registry_no_guard(init_board):
    """solo(N=1·M=1) init → areas.md 부재 → cmd_new 가드 off → legacy T-NNNN 발행.

    머시너리 무파손의 핵심 증거: prefix 없는 trivial 경로는 레지스트리를 만들지 않고
    (가드 비활성) legacy 네임스페이스로 발행한다(ADR-0016 N=1·M=1 = 오버헤드 0).
    """
    assert init_board.cmd_init(_init_args()) == 0
    assert not init_board.AREAS_FILE.exists()              # 레지스트리 미생성
    assert init_board.registered_prefixes() == set()       # 가드 신호 off
    assert init_board.cmd_new(_new_args(title="solo")) == 0
    assert list((init_board.TICKETS_DIR / "open").glob("T-0001-*.md"))


def test_init_namespaced_registers_and_activates_guard(init_board):
    """multi-repo(prefix) init → areas 등록 + 가드 활성 → 네임스페이스 발행·미등록 거부.

    머시너리 무파손의 핵심 증거: prefix 가 있으면 레지스트리 등록(가드 활성)·네임스페이스
    ID(`T-<PFX>-NNN`) 발행이 그대로 동작하고, areas 존재 시 prefix 없는 new 는 거부된다.
    """
    assert init_board.cmd_init(_init_args(prefix="ACC", area="정산", owner="bob")) == 0
    assert init_board.registered_prefixes() == {"ACC"}     # 레지스트리 등록(가드 활성)
    # 등록 prefix → 네임스페이스 발행.
    assert init_board.cmd_new(_new_args(title="acc ticket")) == 0
    assert list((init_board.TICKETS_DIR / "open").glob("T-ACC-001-*.md"))
    # areas 존재 + 미등록 prefix → 가드가 거부(머시너리 불변).
    assert init_board.cmd_new(_new_args(title="bad", prefix="ZZZ")) != 0


# ════════════════════════════════════════════════════════════════════════
# hermetic 입증: 실 루트 areas.md 가 테스트로 생기지 않았는지
# ════════════════════════════════════════════════════════════════════════

def test_real_root_areas_md_untouched(board):
    """이 테스트 모듈 실행이 실 루트 areas.md 를 만들지 않았음을 입증한다.

    루트는 solo(areas.md 부재)다. 앞선 테스트들이 monkeypatch 없이 실 AREAS_FILE 에
    썼다면 여기서 실 루트 areas.md 가 존재할 것 — hermetic 격리의 회귀 가드.
    """
    assert not REAL_AREAS.exists(), (
        f"실 루트 areas.md 가 생성됨 ({REAL_AREAS}) — hermetic 격리 위반")


# ════════════════════════════════════════════════════════════════════════
# session_name count-based 유도 (ADR-0040 D1·T-0073 층위 amend) — 매칭측(board) ↔
# 저장측(worktree_pool)·pm_config 와 동형. 명시 > $PM_SESSION_NAME(정식) >
# $CLAUDE_SESSION_NAME(deprecated alias·silent) > lease 장부 leased 1개면 그 session
# (단일-lease 유도) > (장부 부재·leased 0 = solo) local.conf session= > None.
# leased ≥2 면 local.conf 층 건너뜀(silent 오귀속 차단). 미해소 시 귀속 쓰기(required=True)는
# fail-loud, surface(required=False)는 None(호출부 "(비바인딩)"). 세 모듈이 어긋나면
# per-slot test_cmd·claim 소유권이 미스된다(T-0066 함정).
# ════════════════════════════════════════════════════════════════════════

def _write_conf(board, text):
    board.LOCAL_CONF.write_text(text, encoding="utf-8")


def _write_ledger(board, *rows):
    """리스 장부 파일(`LEASES_FILE`)에 leased/idle 행을 직접 쓴다 (count-based 유도 전제).

    worktree_pool 을 import 하지 않고(board 격리 동형) 최소 스키마(session/state/slot/repo)만
    담는다 — session_name 은 state=="leased" 행의 session 만 센다.
    """
    board.LEASES_FILE.parent.mkdir(parents=True, exist_ok=True)
    leases = [{"slot": f"work/{r['session']}", "repo": "r",
               "session": r["session"], "state": r.get("state", "leased")}
              for r in rows]
    board.LEASES_FILE.write_text(json.dumps({"leases": leases}), encoding="utf-8")


def _clear_env(monkeypatch):
    monkeypatch.delenv("PM_SESSION_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_NAME", raising=False)


def test_session_name_prefers_pm_env(board, monkeypatch):
    """`$PM_SESSION_NAME` 최우선 — alias·lease·local.conf session= 무시 (ADR-0040)."""
    monkeypatch.setenv("PM_SESSION_NAME", "from-pm-env")
    monkeypatch.setenv("CLAUDE_SESSION_NAME", "from-alias")
    _write_conf(board, "session=from-conf\n")
    _write_ledger(board, {"session": "leased-sess"})
    assert board.session_name() == "from-pm-env"


def test_session_name_claude_env_is_alias(board, monkeypatch):
    """`$CLAUDE_SESSION_NAME` 단독 → deprecated alias 로 조용히 동작 (back-compat).

    `PM_SESSION_NAME` 미설정·구 변수만 설정된 기존 dogfooding/채택 환경이 깨지지 않아야
    한다 — alias 우선순위 2번, lease·local.conf 보다 우선.
    """
    monkeypatch.delenv("PM_SESSION_NAME", raising=False)
    monkeypatch.setenv("CLAUDE_SESSION_NAME", "from-alias")
    _write_conf(board, "session=from-conf\n")
    assert board.session_name() == "from-alias"


def test_session_name_pm_wins_over_claude(board, monkeypatch):
    """둘 다 설정 시 `PM_SESSION_NAME` 승 (마이그레이션 중 명시 우선)."""
    monkeypatch.setenv("PM_SESSION_NAME", "new")
    monkeypatch.setenv("CLAUDE_SESSION_NAME", "old")
    assert board.session_name() == "new"


def test_session_name_single_lease_derives_session(board, monkeypatch):
    """env 없음·leased 슬롯 정확히 1개 → 그 session 유도 (ADR-0040 count-based)."""
    _clear_env(monkeypatch)
    _write_ledger(board, {"session": "project_manager_1"})
    assert board.session_name() == "project_manager_1"


def test_session_name_single_lease_wins_over_local_conf(board, monkeypatch):
    """단일-lease 값과 local.conf 값이 다르면 유도값(lease) 승 (저장 쪽지보다 파생 진실)."""
    _clear_env(monkeypatch)
    _write_conf(board, "session=stale-conf\n")
    _write_ledger(board, {"session": "derived-1"})
    assert board.session_name() == "derived-1"


def test_session_name_two_leases_skips_local_conf_returns_none(board, monkeypatch):
    """leased ≥2 (모호) → local.conf 층 건너뜀 → None (silent 오귀속 차단·ADR-0040)."""
    _clear_env(monkeypatch)
    _write_conf(board, "session=some-conf\n")   # 있어도 무시돼야 한다(건너뜀)
    _write_ledger(board, {"session": "a_1"}, {"session": "b_1"})
    assert board.session_name() is None


def test_session_name_idle_leases_not_counted(board, monkeypatch):
    """idle 행은 count 대상 아님 — leased 1개(+idle 다수)면 그 leased session 유도."""
    _clear_env(monkeypatch)
    _write_ledger(board, {"session": "live_1"},
                  {"session": "old_2", "state": "idle"},
                  {"session": "old_3", "state": "idle"})
    assert board.session_name() == "live_1"


def test_session_name_reads_local_conf_session(board, monkeypatch):
    """env·lease 없음(장부 부재 = solo) → local.conf `session=` (legacy 폴백·후방호환)."""
    _clear_env(monkeypatch)
    _write_conf(board, "session=foo\n")
    # 장부 미작성 → leased 0 = solo → local.conf.
    assert board.session_name() == "foo"


def test_session_name_solo_unbound_returns_none(board, monkeypatch):
    """env·lease·local.conf session= 모두 없음 → None (구 host-pid 폴백 제거·ADR-0040).

    `<host>-<pid>` 최종 폴백은 세션-귀속 아닌 국소 용처(worktree_pool lease 취득)에만 잔존 —
    board 의 귀속 해소에선 미해소=None(surface required=False)이다.
    """
    _clear_env(monkeypatch)
    # local.conf 없음·장부 없음.
    assert board.session_name() is None


def test_session_name_override_beats_everything(board, monkeypatch):
    """override 인자가 env·lease·local.conf 보다 우선 (해소 0층)."""
    monkeypatch.setenv("PM_SESSION_NAME", "from-pm-env")
    _write_conf(board, "session=from-conf\n")
    _write_ledger(board, {"session": "leased-1"})
    assert board.session_name("explicit") == "explicit"


def test_session_name_required_fail_loud_when_unresolved(board, monkeypatch):
    """required=True + 미해소(solo 무바인딩) → fail-loud(SystemExit·`--session` 안내)."""
    _clear_env(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        board.session_name(required=True)
    # 안내 문구에 `--session <repo>_<N>` 형식이 들어간다.
    assert "--session" in str(exc.value)


def test_session_name_required_fail_loud_when_ambiguous(board, monkeypatch):
    """required=True + leased ≥2 (모호) → fail-loud (local.conf 있어도 오귀속 안 함)."""
    _clear_env(monkeypatch)
    _write_conf(board, "session=some-conf\n")
    _write_ledger(board, {"session": "a_1"}, {"session": "b_1"})
    with pytest.raises(SystemExit):
        board.session_name(required=True)


def test_session_name_required_resolves_does_not_exit(board, monkeypatch):
    """required=True 라도 해소되면(단일-lease) fail-loud 없이 그 값 반환."""
    _clear_env(monkeypatch)
    _write_ledger(board, {"session": "only_1"})
    assert board.session_name(required=True) == "only_1"


# ── 귀속 쓰기(claim) fail-loud 배선 + surface(list --mine) 비바인딩 (ADR-0040 D1) ──

def test_cmd_claim_fail_loud_when_session_ambiguous(board, monkeypatch):
    """cmd_claim(귀속 쓰기·required=True) — leased ≥2 모호 + --session 없음 → fail-loud.

    세션 해소가 cmd_claim 최상단이라 find_ticket/board-git 이전에 SystemExit — silent
    오귀속(남의 세션으로 claim) 대신 즉시 `--session` 명시를 요구한다.
    """
    _clear_env(monkeypatch)
    _write_conf(board, "session=some-conf\n")   # 있어도 무시(모호 → 건너뜀)
    _write_ledger(board, {"session": "a_1"}, {"session": "b_1"})
    with pytest.raises(SystemExit):
        board.cmd_claim(argparse.Namespace(id="T-0001", session=None, user=None))


def test_cmd_claim_explicit_session_bypasses_fail_loud(board, monkeypatch):
    """명시 --session 이면 모호해도 fail-loud 하지 않는다(세션 해소 override 0층)."""
    _clear_env(monkeypatch)
    _write_ledger(board, {"session": "a_1"}, {"session": "b_1"})
    _seed_ticket(board, "T-0001", status="open")
    # 명시 session → session_name 이 즉시 반환(SystemExit 없음). claim 이 정상 진행돼 rc=0.
    rc = board.cmd_claim(argparse.Namespace(id="T-0001", session="a_1", user="me"))
    assert rc == 0


def test_cmd_list_mine_unbound_surfaces_unbind_note(board, monkeypatch, capsys):
    """list --mine + 세션 미바인딩(surface·required=False) → stderr "(비바인딩)"·크래시 없음.

    stdout 티켓 목록은 area-open 으로 graceful degrade(빈 보드 금지)하고, 안내는 stderr 로
    분리해 목록 포맷을 오염시키지 않는다(ADR-0040).
    """
    _clear_env(monkeypatch)   # 장부·local.conf 없음 → session_name None
    _seed_ticket(board, "T-0001", status="open")
    rc = board.cmd_list(argparse.Namespace(mine=True, session=None, slot=None,
                                           tag=None, status=None))
    assert rc == 0
    assert "비바인딩" in capsys.readouterr().err


# ════════════════════════════════════════════════════════════════════════
# regression run: vacuous-pass 근절 (T-0220) — rc5(수집 0·"no tests ran")는
# pass 로 기록하지 않는다. pass = rc0 한정. 미매칭 REPO 폴백 + tests/ 부재 시
# 세션 해소 힌트를 시끄럽게 표면화(침묵 폴백이 상시 vacuous green 이던 것 근절·PM 49차).
# ════════════════════════════════════════════════════════════════════════

class _FakeRun:
    """board.subprocess.run 대역 — 고정 returncode 를 돌려주고 호출을 기록한다.

    pytest 자식을 실기동하지 않고 rc 만 주입한다. `_git_head` 은 별도 mock 하므로 이
    대역은 회귀 pytest 호출만 받는다.
    """

    def __init__(self, rc: int):
        self.rc = rc
        self.calls: list[dict] = []

    def __call__(self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})
        return types.SimpleNamespace(returncode=self.rc)


@pytest.fixture
def reg_board(board, monkeypatch):
    """회귀 run 을 hermetic 하게 만든 board — 플래그/장부/HEAD 를 tmp·fake 로 격리.

    board 픽스처는 REGRESSION_FLAG·LOCAL_DIR·LEASES_FILE 을 monkeypatch 하지 않아 실 repo 를
    가리킨다 — 여기서 tmp 로 재지정한다. LEASES_FILE 은 부재(→`_active_slot_path` None →
    cwd=REPO 폴백)·REPO(=tmp proj)엔 `tests/` 없음 → rc5 폴백 힌트 조건이 성립한다.
    """
    local = board._proj / ".project_manager" / ".local"
    monkeypatch.setattr(board, "LOCAL_DIR", local)
    monkeypatch.setattr(board, "REGRESSION_FLAG", local / "regression.json")
    monkeypatch.setattr(board, "LEASES_FILE", local / "worktree-leases.json")  # 부재
    monkeypatch.setattr(board, "_git_head", lambda: "deadbeef01234567")
    return board


def _run_args(**over):
    base = dict(action="run", cmd=None, ticket=None, touches=None)
    base.update(over)
    return argparse.Namespace(**base)


def test_regression_run_rc5_records_fail(reg_board, monkeypatch, capsys):
    """rc5(수집 0) full run → status='fail' 기록 + rc≠0 반환 (vacuous pass 근절·T-0220).

    이전엔 `rc in (0, 5)` 로 rc5 를 pass 로 삼켰다 — 그 회귀를 복원하면 이 단언이 깨진다.
    """
    monkeypatch.setattr(reg_board.subprocess, "run", _FakeRun(5))
    rc = reg_board.cmd_regression(_run_args())
    assert rc == 1, "rc5 는 fail — cmd_regression 이 0(pass)을 반환하면 안 된다"
    data = json.loads(reg_board.REGRESSION_FLAG.read_text(encoding="utf-8"))
    assert data["status"] == "fail", f"rc5 가 pass 로 기록됨: {data!r}"
    assert data["rc"] == 5
    out = capsys.readouterr().out
    assert "fail (rc=5" in out
    assert "수집 0" in out


def test_regression_run_rc0_records_pass_unchanged(reg_board, monkeypatch, capsys):
    """rc0 full run → status='pass' 기록 + 0 반환 (회귀 무변경·T-0220).

    pass = rc0 한정으로 좁혔어도 정상 green 경로는 그대로여야 한다.
    """
    monkeypatch.setattr(reg_board.subprocess, "run", _FakeRun(0))
    rc = reg_board.cmd_regression(_run_args())
    assert rc == 0
    data = json.loads(reg_board.REGRESSION_FLAG.read_text(encoding="utf-8"))
    assert data["status"] == "pass"
    assert data["rc"] == 0
    out = capsys.readouterr().out
    assert "pass (rc=0)" in out
    assert "수집 0" not in out  # rc0 엔 rc5 진단 노트가 붙지 않는다.


def test_regression_run_rc1_records_fail(reg_board, monkeypatch):
    """rc1(실 실패) full run → status='fail' (일반 실패는 회귀 무변경)."""
    monkeypatch.setattr(reg_board.subprocess, "run", _FakeRun(1))
    rc = reg_board.cmd_regression(_run_args())
    assert rc == 1
    data = json.loads(reg_board.REGRESSION_FLAG.read_text(encoding="utf-8"))
    assert data["status"] == "fail"
    assert data["rc"] == 1


def test_regression_run_rc5_repo_fallback_surfaces_session_hint(
        reg_board, monkeypatch, capsys):
    """rc5 + REPO 폴백(lease 미매칭) + tests/ 부재 → 세션 해소 힌트 표면화 (T-0220).

    훅 env 에 세션 정체성이 없어 침묵 폴백 → 상시 vacuous green 이던 것을 시끄럽게 만든다.
    힌트에 해소된 session 값과 PM_SESSION_NAME/local.conf 안내가 들어간다.
    """
    monkeypatch.setenv("PM_SESSION_NAME", "orch-dev-T0220")
    monkeypatch.setattr(reg_board.subprocess, "run", _FakeRun(5))
    reg_board.cmd_regression(_run_args())
    out = capsys.readouterr().out
    assert "활성 slot lease 미매칭" in out
    assert "session=`orch-dev-T0220`" in out
    assert "PM_SESSION_NAME" in out
    assert "local.conf" in out


def test_regression_run_rc5_explicit_cwd_no_fallback_hint(
        reg_board, monkeypatch, capsys):
    """rc5 지만 명시 `--cwd`(override)면 폴백이 아니므로 세션 힌트를 붙이지 않는다.

    수집 0 노트는 그대로(rc5=fail) 나오되, '미매칭 폴백' 힌트는 override 경로에선 무의미하다.
    """
    monkeypatch.setenv("PM_SESSION_NAME", "orch-dev-T0220")
    monkeypatch.setattr(reg_board.subprocess, "run", _FakeRun(5))
    reg_board.cmd_regression(_run_args(cwd=str(reg_board._proj / "elsewhere")))
    out = capsys.readouterr().out
    assert "수집 0" in out               # rc5=fail 노트는 유지.
    assert "활성 slot lease 미매칭" not in out  # override 는 폴백 아님 → 힌트 없음.


def test_regression_run_rc5_scoped_returns_fail(reg_board, monkeypatch, capsys):
    """scoped(touches) rc5 도 fail 반환 (advisory 경로도 rc0 만 pass·T-0220).

    scoped 는 플래그를 안 쓰지만 반환값/메시지는 full 과 같은 판정을 따른다.
    """
    monkeypatch.setattr(reg_board.subprocess, "run", _FakeRun(5))
    rc = reg_board.cmd_regression(_run_args(touches="tests/test_board_multipm.py"))
    assert rc == 1
    out = capsys.readouterr().out
    assert "fail (rc=5" in out
    assert "수집 0" in out


def test_regression_check_rc5_recorded_blocks_push(reg_board, monkeypatch, capsys):
    """rc5 로 기록된 fail 플래그를 `check` 가 HEAD-매칭 RED 로 차단 + rc 표면화 (run/check 일관).

    check 는 플래그의 status 만 신뢰한다 — rc5 가 fail 로 기록되므로 push 가 막히고,
    RED 메시지가 rc=5·수집 0 사유를 드러낸다.
    """
    reg_board.LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    reg_board.REGRESSION_FLAG.write_text(json.dumps(
        {"head": "deadbeef01234567", "status": "fail", "rc": 5,
         "scope": "full", "ts": "2026-07-03T00:00:00+00:00"}), encoding="utf-8")
    rc = reg_board.cmd_regression(argparse.Namespace(action="check"))
    assert rc == 1
    err = capsys.readouterr().err
    assert "RED" in err
    assert "rc=5" in err
    assert "수집 0" in err


# ════════════════════════════════════════════════════════════════════════
# M>1 회귀 슬롯 all-or-nothing (ADR-0040 D2·b-1) — leased ≥2·무명시면 전 leased
# 슬롯 순회: 슬롯별 check-first(저비용·기록 baseline) 후 stale/red 만 run,
# 하나라도 red 면 push 차단. 단일 lease(M1)·솔로(M0)는 현행 단일-슬롯 경로
# (공유 REGRESSION_FLAG) 무변경. 훅은 --session 을 못 넘겨 이 경로로 해소된다.
# ════════════════════════════════════════════════════════════════════════

class _FakeRunByCwd:
    """subprocess.run 대역 — cwd 별 returncode 를 돌려주고 호출 cwd 를 기록한다.

    M>1 슬롯 순회는 슬롯마다 다른 cwd 로 pytest 를 띄우므로, 슬롯별 rc(green/red)를 cwd
    로 주입한다(`_FakeRun` 의 다중-cwd 확장). `_git_head_at` 은 별도 mock 하므로 이 대역은
    회귀 pytest 호출만 받는다.
    """

    def __init__(self, rc_by_cwd: dict[str, int], default: int = 0):
        self.rc_by_cwd = rc_by_cwd
        self.default = default
        self.cwds: list[str] = []

    def __call__(self, *args, **kwargs):
        cwd = kwargs.get("cwd")
        self.cwds.append(cwd)
        return types.SimpleNamespace(returncode=self.rc_by_cwd.get(cwd, self.default))


def _slot_cwd(board, session: str) -> str:
    """M>1 순회가 이 세션 슬롯에 해소할 cwd (`REPO / work/<session>`·_write_ledger 규약)."""
    return str(board.REPO / "work" / session)


def _seed_slot_flag(board, session: str, head: str,
                    status: str = "pass", rc: int = 0) -> None:
    """슬롯별 회귀 플래그를 직접 심는다 (check-first 의 baseline 상태 주입)."""
    flag = board._regression_flag_for(session)
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text(json.dumps(
        {"head": head, "status": status, "rc": rc, "scope": "full",
         "session": session, "ts": "2026-07-03T00:00:00+00:00"}), encoding="utf-8")


@pytest.fixture
def multi_reg_board(reg_board, monkeypatch):
    """reg_board + 2 leased 슬롯(A_1·B_1)·슬롯별 HEAD mock — M>1 all-or-nothing 순회 전제.

    env 를 비워 session_name None(모호 M>1)을 강제하고, `_git_head_at` 을 cwd→HEAD dict 로
    mock 한다(각 슬롯 worktree 는 독립 commit). subprocess(pytest)는 각 테스트가 주입한다.
    """
    _clear_env(monkeypatch)
    _write_ledger(reg_board, {"session": "A_1"}, {"session": "B_1"})
    heads = {_slot_cwd(reg_board, "A_1"): "HEAD_A",
             _slot_cwd(reg_board, "B_1"): "HEAD_B"}
    monkeypatch.setattr(reg_board, "_git_head_at", lambda cwd: heads.get(cwd, "HEAD_?"))
    reg_board._heads = heads
    return reg_board


# ── M0(솔로)/M1(단일 lease) — 현행 단일-슬롯 경로 무변경 ──────────────────────

def test_regression_run_single_lease_uses_shared_flag_and_slot_cwd(
        reg_board, monkeypatch):
    """M1(단일 lease)·무명시 → 단일-슬롯 경로 유지: 공유 REGRESSION_FLAG 기록 + 슬롯 cwd.

    leased 1개면 all-or-nothing 순회로 안 빠지고(현행 결과 동일), _regression_cwd 가 그 슬롯
    worktree 로 해소된다(session_name 단일-lease 유도). per-slot 플래그를 만들지 않는다.
    """
    _clear_env(monkeypatch)
    _write_ledger(reg_board, {"session": "solo_1"})   # leased 정확히 1개
    fake = _FakeRun(0)
    monkeypatch.setattr(reg_board.subprocess, "run", fake)
    rc = reg_board.cmd_regression(_run_args())
    assert rc == 0
    # 공유 플래그에 기록 (슬롯 순회 아님 — per-slot 플래그 부재).
    assert reg_board.REGRESSION_FLAG.exists()
    assert not (reg_board.LOCAL_DIR / "regression-solo_1.json").exists()
    # cwd = 그 슬롯 worktree (단일-lease 유도값을 threading).
    assert fake.calls[0]["kwargs"]["cwd"] == _slot_cwd(reg_board, "solo_1")


# ── reviewer 방어가드 (`and sess`) — sess None 시 손상 행 false-match 차단 ──────

def test_active_slot_none_session_ignores_corrupt_null_row(board, monkeypatch):
    """sess None(모호 M>1·미바인딩) + 손상 행(session:null·state:leased) → false-match 안 함.

    reviewer 방어가드(`and sess`): `row.get('session')==None==sess` 로 손상 행에 오매칭해
    엉뚱한 test_cmd/cwd 를 뽑던 클래스를 차단한다. session_name None 을 강제(env·conf·유효
    lease 없음)한다.
    """
    _clear_env(monkeypatch)
    board.LEASES_FILE.parent.mkdir(parents=True, exist_ok=True)
    board.LEASES_FILE.write_text(json.dumps(
        {"leases": [{"session": None, "state": "leased", "slot": "work/x",
                     "test_cmd": "SHOULD_NOT_MATCH"}]}), encoding="utf-8")
    assert board.session_name() is None           # 손상 행은 count 대상 아님 → 미바인딩.
    assert board._active_slot_test_cmd() is None   # 가드 없으면 SHOULD_NOT_MATCH 오매칭.
    assert board._active_slot_path() is None


# ── M2+ run: 전 green 통과 · 1 red 차단 · stale만 run (check-first) ─────────────

def test_regression_multi_run_all_missing_runs_all_green(
        multi_reg_board, monkeypatch, capsys):
    """M2+ run·양 슬롯 미기록 → 둘 다 pytest(green) → 슬롯별 pass 기록 + rc0 (all green)."""
    b = multi_reg_board
    fake = _FakeRunByCwd({}, default=0)   # 모든 슬롯 rc0
    monkeypatch.setattr(b.subprocess, "run", fake)
    rc = b.cmd_regression(_run_args())
    assert rc == 0
    # skip 없음·2 슬롯 모두 run (각 슬롯 cwd 로).
    assert set(fake.cwds) == {_slot_cwd(b, "A_1"), _slot_cwd(b, "B_1")}
    assert len(fake.cwds) == 2
    # 슬롯별 플래그가 각 슬롯 HEAD 로 pass 기록.
    for s in ("A_1", "B_1"):
        data = json.loads(b._regression_flag_for(s).read_text(encoding="utf-8"))
        assert data["status"] == "pass"
        assert data["head"] == b._heads[_slot_cwd(b, s)]
    out = capsys.readouterr().out
    assert "skip(green) 0 · run 2" in out
    assert "전 슬롯 green" in out


def test_regression_multi_run_one_red_blocks_push(
        multi_reg_board, monkeypatch, capsys):
    """M2+ run·한 슬롯 red → rc1(push 차단)·종합 메시지에 red 슬롯 명시 (all-or-nothing)."""
    b = multi_reg_board
    fake = _FakeRunByCwd({_slot_cwd(b, "B_1"): 1}, default=0)  # B_1 만 red
    monkeypatch.setattr(b.subprocess, "run", fake)
    rc = b.cmd_regression(_run_args())
    assert rc == 1
    err = capsys.readouterr().err
    assert "RED" in err
    assert "B_1" in err          # 어느 슬롯이 red 인지 명시(디버깅 동선).
    assert "push 차단" in err


def test_regression_multi_run_check_first_skips_green_runs_missing(
        multi_reg_board, monkeypatch, capsys):
    """M2+ run·A_1 green 기록(HEAD 일치) → skip, B_1 미기록 → 그 슬롯만 run (재실행 억제)."""
    b = multi_reg_board
    _seed_slot_flag(b, "A_1", head="HEAD_A", status="pass")  # green → skip
    fake = _FakeRunByCwd({}, default=0)
    monkeypatch.setattr(b.subprocess, "run", fake)
    rc = b.cmd_regression(_run_args())
    assert rc == 0
    # A_1(green) 재실행 안 함 — B_1 만 pytest.
    assert fake.cwds == [_slot_cwd(b, "B_1")]
    assert "skip(green) 1 · run 1" in capsys.readouterr().out


def test_regression_multi_run_stale_head_reruns(
        multi_reg_board, monkeypatch, capsys):
    """M2+ run·A_1 플래그 HEAD 가 현 HEAD 와 불일치(stale) → green skip 아님·그 슬롯만 재실행."""
    b = multi_reg_board
    _seed_slot_flag(b, "A_1", head="OLD_HEAD", status="pass")  # stale (HEAD_A ≠ OLD_HEAD)
    _seed_slot_flag(b, "B_1", head="HEAD_B", status="pass")    # green → skip
    fake = _FakeRunByCwd({}, default=0)
    monkeypatch.setattr(b.subprocess, "run", fake)
    rc = b.cmd_regression(_run_args())
    assert rc == 0
    assert fake.cwds == [_slot_cwd(b, "A_1")]   # stale 만 run
    assert "skip(green) 1 · run 1" in capsys.readouterr().out


# ── M2+ check: 전 green 통과 · stale/red/missing 차단 (슬롯 명시) ──────────────

def test_regression_multi_check_all_green_passes(multi_reg_board, capsys):
    """M2+ check·양 슬롯 green 기록(HEAD 일치·pass) → rc0 (pytest 미실행·저비용)."""
    b = multi_reg_board
    _seed_slot_flag(b, "A_1", head="HEAD_A", status="pass")
    _seed_slot_flag(b, "B_1", head="HEAD_B", status="pass")
    rc = b.cmd_regression(argparse.Namespace(action="check"))
    assert rc == 0
    assert "전 슬롯 green" in capsys.readouterr().out


def test_regression_multi_check_one_stale_blocks_and_names(multi_reg_board, capsys):
    """M2+ check·A_1 green·B_1 stale(HEAD 불일치) → rc1 + 미검증 슬롯·상태 명시 (push 차단)."""
    b = multi_reg_board
    _seed_slot_flag(b, "A_1", head="HEAD_A", status="pass")
    _seed_slot_flag(b, "B_1", head="OLD", status="pass")   # stale
    rc = b.cmd_regression(argparse.Namespace(action="check"))
    assert rc == 1
    err = capsys.readouterr().err
    assert "B_1=stale" in err
    assert "push 차단" in err


def test_regression_multi_check_missing_flag_blocks(multi_reg_board, capsys):
    """M2+ check·한 슬롯 기록 부재(missing) → rc1 (전 슬롯 green 아니면 차단)."""
    b = multi_reg_board
    _seed_slot_flag(b, "A_1", head="HEAD_A", status="pass")
    # B_1 플래그 없음 → missing.
    rc = b.cmd_regression(argparse.Namespace(action="check"))
    assert rc == 1
    assert "B_1=missing" in capsys.readouterr().err


def test_regression_multi_check_red_flag_blocks(multi_reg_board, capsys):
    """M2+ check·한 슬롯 red 기록(HEAD 일치·fail) → rc1 (red 은 green 아님·명시)."""
    b = multi_reg_board
    _seed_slot_flag(b, "A_1", head="HEAD_A", status="pass")
    _seed_slot_flag(b, "B_1", head="HEAD_B", status="fail", rc=1)
    rc = b.cmd_regression(argparse.Namespace(action="check"))
    assert rc == 1
    assert "B_1=red" in capsys.readouterr().err


def test_regression_multi_run_recovers_after_fix(
        multi_reg_board, monkeypatch, capsys):
    """M2+ run 후 check 일관 — 한 번 red 였던 슬롯이 green 으로 재기록되면 check 가 통과.

    all-or-nothing 의 run→check 사이클: run 이 슬롯별 플래그를 갱신하므로 후속 check 가
    그 baseline 을 그대로 신뢰한다(per-slot 플래그가 서로 안 덮임).
    """
    b = multi_reg_board
    # 1) B_1 red 로 run → 차단.
    monkeypatch.setattr(b.subprocess, "run",
                        _FakeRunByCwd({_slot_cwd(b, "B_1"): 1}))
    assert b.cmd_regression(_run_args()) == 1
    capsys.readouterr()
    # 2) 고쳐서 전 슬롯 green 으로 재run → 통과.
    monkeypatch.setattr(b.subprocess, "run", _FakeRunByCwd({}, default=0))
    assert b.cmd_regression(_run_args()) == 0
    capsys.readouterr()
    # 3) check 가 두 슬롯 green baseline 을 신뢰 → rc0.
    assert b.cmd_regression(argparse.Namespace(action="check")) == 0
    assert "전 슬롯 green" in capsys.readouterr().out


# ── ambient env 로 M>1 게이트 우회 차단 (codex must-fix·ADR-0040 b-1) ──────────
# 훅 프로세스가 PM_SESSION_NAME/CLAUDE_SESSION_NAME 을 상속해도 leased ≥2 면 자기 슬롯 단일
# 경로로 좁혀지지 않는다 — 게이트 좁히기는 CLI --session 명시(의도적 조작)만 허용.

def test_regression_multi_check_ignores_env_session(
        multi_reg_board, monkeypatch, capsys):
    """M2+ check·PM_SESSION_NAME=A_1 상속 + B_1 missing → 전-슬롯 게이트 유지·rc1 (env 우회 차단).

    ambient env 세션이 훅에 상속돼도 A_1 단일 경로로 좁혀 B_1 의 missing 을 통과시키지 않는다.
    """
    b = multi_reg_board
    monkeypatch.setenv("PM_SESSION_NAME", "A_1")   # ambient env — 디스패치 판정서 무시돼야 함
    _seed_slot_flag(b, "A_1", head="HEAD_A", status="pass")
    # B_1 플래그 없음 → missing.
    rc = b.cmd_regression(argparse.Namespace(action="check"))
    assert rc == 1
    assert "B_1=missing" in capsys.readouterr().err


def test_regression_multi_check_ignores_claude_env_alias(
        multi_reg_board, monkeypatch, capsys):
    """M2+ check·CLAUDE_SESSION_NAME(구 alias) 상속도 동일 — 게이트 우회 안 됨 (env 전면 제외)."""
    b = multi_reg_board
    monkeypatch.setenv("CLAUDE_SESSION_NAME", "A_1")
    _seed_slot_flag(b, "A_1", head="HEAD_A", status="pass")
    rc = b.cmd_regression(argparse.Namespace(action="check"))
    assert rc == 1
    assert "B_1=missing" in capsys.readouterr().err


def test_regression_multi_run_ignores_env_session(
        multi_reg_board, monkeypatch, capsys):
    """M2+ run·PM_SESSION_NAME=B_1 상속 + B_1 red → 전-슬롯 순회·rc1 (env 로 자기 슬롯 우회 안 됨)."""
    b = multi_reg_board
    monkeypatch.setenv("PM_SESSION_NAME", "B_1")   # 자기 슬롯으로 좁히려는 env — 무시.
    fake = _FakeRunByCwd({_slot_cwd(b, "B_1"): 1}, default=0)  # B_1 red·A_1 green
    monkeypatch.setattr(b.subprocess, "run", fake)
    rc = b.cmd_regression(_run_args())
    assert rc == 1
    # 전-슬롯 순회 — A_1·B_1 둘 다 run 대상(둘 다 missing).
    assert set(fake.cwds) == {_slot_cwd(b, "A_1"), _slot_cwd(b, "B_1")}
    assert "B_1" in capsys.readouterr().err


def test_regression_run_explicit_session_narrows_in_multi(
        multi_reg_board, monkeypatch):
    """M2+ 라도 CLI --session 명시는 그 슬롯 단일 경로로 좁힌다 (문서화된 의도적 조작만 허용)."""
    b = multi_reg_board
    fake = _FakeRun(0)
    monkeypatch.setattr(b.subprocess, "run", fake)
    rc = b.cmd_regression(_run_args(session="A_1"))
    assert rc == 0
    # 단일-슬롯 경로 — 공유 REGRESSION_FLAG·A_1 cwd 하나만(슬롯 순회 아님).
    assert len(fake.calls) == 1
    assert fake.calls[0]["kwargs"]["cwd"] == _slot_cwd(b, "A_1")
    assert b.REGRESSION_FLAG.exists()
    assert not (b.LOCAL_DIR / "regression-A_1.json").exists()


def test_regression_multi_check_message_carries_rc5_hint(
        multi_reg_board, capsys):
    """M2+ check 실패 메시지에 슬롯 rc + rc5 수집0 힌트 (codex sug — 단일-슬롯 진단 균질화)."""
    b = multi_reg_board
    _seed_slot_flag(b, "A_1", head="HEAD_A", status="pass")
    _seed_slot_flag(b, "B_1", head="HEAD_B", status="fail", rc=5)  # red·수집 0
    rc = b.cmd_regression(argparse.Namespace(action="check"))
    assert rc == 1
    err = capsys.readouterr().err
    assert "B_1=red(rc=5·수집0)" in err
