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
    overrides = {
        "REPO": proj,
        "TICKETS_DIR": wiki / "tickets",
        "TEMPLATE_FILE": wiki / "tickets" / "_template.md",
        "BOARD_FILE": wiki / "board.md",
        "LOG_FILE": wiki / "log" / "current.md",
        "STATUS_FILE": wiki / "status.md",
        "LOCAL_CONF": pm / "local.conf",
        "AREAS_FILE": pm / "areas.md",
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
    """areas.md 존재(multi 모드)인데 prefix 미해소 → 거부."""
    board.areas_append("PAY", "결제", "alice")
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


# ════════════════════════════════════════════════════════════════════════
# cmd_init team 경로
# ════════════════════════════════════════════════════════════════════════

def _init_args(prefix=None, area=None, owner=None, session=None):
    return argparse.Namespace(prefix=prefix, area=area, owner=owner, session=session)


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
# session_name 4단 우선순위 (T-0073) — 매칭측(board) ↔ 저장측(worktree_pool)·
# pm_config 와 동형. PM_SESSION_NAME(정식) > CLAUDE_SESSION_NAME(deprecated alias·
# silent) > local.conf session= > <host>-<pid>. 세 모듈이 어긋나면 per-slot
# test_cmd·claim 소유권이 미스된다(T-0066 함정).
# ════════════════════════════════════════════════════════════════════════

def _write_conf(board, text):
    board.LOCAL_CONF.write_text(text, encoding="utf-8")


def test_session_name_prefers_pm_env(board, monkeypatch):
    """`$PM_SESSION_NAME` 최우선 — alias·local.conf session= 무시 (T-0073)."""
    monkeypatch.setenv("PM_SESSION_NAME", "from-pm-env")
    monkeypatch.setenv("CLAUDE_SESSION_NAME", "from-alias")
    _write_conf(board, "session=from-conf\n")
    assert board.session_name() == "from-pm-env"


def test_session_name_claude_env_is_alias(board, monkeypatch):
    """`$CLAUDE_SESSION_NAME` 단독 → deprecated alias 로 조용히 동작 (T-0073 back-compat).

    `PM_SESSION_NAME` 미설정·구 변수만 설정된 기존 dogfooding/채택 환경이 깨지지 않아야
    한다 — alias 우선순위 2번, local.conf 보다 우선.
    """
    monkeypatch.delenv("PM_SESSION_NAME", raising=False)
    monkeypatch.setenv("CLAUDE_SESSION_NAME", "from-alias")
    _write_conf(board, "session=from-conf\n")
    assert board.session_name() == "from-alias"


def test_session_name_pm_wins_over_claude(board, monkeypatch):
    """둘 다 설정 시 `PM_SESSION_NAME` 승 (T-0073 마이그레이션 중 명시 우선)."""
    monkeypatch.setenv("PM_SESSION_NAME", "new")
    monkeypatch.setenv("CLAUDE_SESSION_NAME", "old")
    assert board.session_name() == "new"


def test_session_name_reads_local_conf_session(board, monkeypatch):
    """env(둘 다) 없음 → local.conf `session=` (저장측 worktree_pool 3층과 동형)."""
    monkeypatch.delenv("PM_SESSION_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_NAME", raising=False)
    _write_conf(board, "session=foo\n")
    assert board.session_name() == "foo"


def test_session_name_falls_back_to_host_pid(board, monkeypatch):
    """env(둘 다)·local.conf session= 모두 없음 → `<host>-<pid>` (4층 폴백)."""
    import os
    import socket
    monkeypatch.delenv("PM_SESSION_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_NAME", raising=False)
    # local.conf 없음(_make_project 는 local.conf 안 만듦).
    assert board.session_name() == f"{socket.gethostname()}-{os.getpid()}"


def test_session_name_override_beats_everything(board, monkeypatch):
    """override 인자가 env·local.conf 보다 우선 (해소 0층)."""
    monkeypatch.setenv("PM_SESSION_NAME", "from-pm-env")
    _write_conf(board, "session=from-conf\n")
    assert board.session_name("explicit") == "explicit"
