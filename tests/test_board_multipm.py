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
import io
import json
import shutil
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
        "# T-NNNN — <제목>\n\n## 목표\n채워라.\n\n"
        "## 완료 조건 (Definition of Done)\n- [ ] 채워라.\n",
        encoding="utf-8",
    )


def _check_off_dod(board, tid: str) -> None:
    """claimed 티켓의 DoD 를 전항 체크(`- [ ]` → `- [x]`)한다 — 마감 전 PM 이 손으로 하는 일.

    complete 는 DoD 기록 게이트를 통과해야 하고(T-0596·T-0781), 템플릿 DoD 는 미체크로
    시작한다. lifecycle e2e 가 그 사람 단계를 그대로 재현한다.
    """
    (path,) = list((board.TICKETS_DIR / "claimed").glob(f"{tid}-*.md"))
    path.write_text(path.read_text(encoding="utf-8").replace("- [ ] ", "- [x] "),
                    encoding="utf-8")


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
    # ADR-0040: id_prefix 세션 유도가 session_name() 을 부르고 그게 env $PM_SESSION_NAME 을
    # 읽는다 — 실 PM 세션 env 가 hermetic 테스트로 새지 않게 제거(장부/local.conf 만이 세션 소스).
    monkeypatch.delenv("PM_SESSION_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_NAME", raising=False)
    # user-first(ADR-0056): 필터 뷰 my_user 는 `user_name()`(local.conf user= > git email) 로 해소된다.
    # git 폴백을 None 으로 stub 해 실 git config user.email 이 hermetic 테스트로 새지 않게 한다
    # (test_board_mine_view/scoping_isolation 동형·명시 테스트는 local.conf user= 로 덮는다).
    monkeypatch.setattr(mod, "_git_config_email", lambda: None)
    mod._proj = proj  # 테스트 편의 핸들 (tmp 프로젝트 루트)
    return mod


def _seed_lease(board, session: str, repo: str, *,
                slot: str | None = None, state: str = "leased") -> None:
    """리스 장부에 한 행을 심는다 (session_name count-based 유도·ADR-0040 D1·D3).

    board 는 worktree_pool 을 import 하지 않으므로 테스트도 파일 스키마로만 친다
    (`{"leases": [...]}`·worktree_pool.Lease.to_dict() 동형). 여러 번 부르면 append.
    """
    board.LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    data = {"leases": []}
    if board.LEASES_FILE.exists():
        data = json.loads(board.LEASES_FILE.read_text(encoding="utf-8"))
    data["leases"].append({
        "slot": slot or f"work/{session}", "repo": repo,
        "session": session, "state": state,
    })
    board.LEASES_FILE.write_text(json.dumps(data), encoding="utf-8")


# ── 헬퍼: 보드에 ticket 파일을 직접 심는다 (네임스페이싱 단위용) ──────────────

def _seed_ticket(board, tid: str, status: str = "open") -> Path:
    """`{tid}-slug.md` 빈 ticket 을 status 디렉토리에 심는다 (_next_id 카운트 대상)."""
    path = board.TICKETS_DIR / status / f"{tid}-seed.md"
    board.dump_ticket(path, {"id": tid, "title": "seed", "status": status},
                      "# seed\n")
    return path


def _issued_ids(board, status: str = "open") -> set[str]:
    """그 status 디렉토리 티켓들의 **발행 ID**(frontmatter) 집합.

    파일명 glob 으로 case 를 판정하지 않는다 — Windows·macOS 의 대소문자 무시 파일시스템에서
    `glob("T-aaa-*")` 는 `T-AAA-001-seed.md` 까지 매치해 case-분할 여부를 증명하지 못한다.
    """
    return {board._issued_ticket_id(p)
            for p in (board.TICKETS_DIR / status).glob("T-*.md")}


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
# 단위: id_prefix 유도 체인 (ADR-0040 D3 · T-0779 amend):
#   override > 세션 유도(areas) > count-based(단일 repo) > None
# local.conf `prefix=` 층은 폐지됐다 — prefix 는 areas.md 칼럼이 단일 진실이다.
# ════════════════════════════════════════════════════════════════════════

# ── layer 1: override (명시) ──────────────────────────────────────────────

def test_id_prefix_override_wins(board):
    """override 인자가 최우선 — areas 유도가 있어도 무시한다."""
    board.areas_append("ACC", "정산", "alice", repo="acc")
    assert board.id_prefix("PAY") == "PAY"


# ── 폐지된 층: local.conf prefix= 는 더 이상 소스가 아니다 (T-0779) ──────────

def test_id_prefix_ignores_local_conf_prefix_key(board):
    """등록 0 형상에 local.conf `prefix=ACC` 만 있어도 해소되지 않는다 → None(무prefix).

    실 init 이 만드는 conf 에는 이 키가 없다. 구 clone 에 남아 있는 키를 조용히 읽어
    네임스페이스를 정하던 폴백이 폐지됐음을 값으로 못박는다(조용한 폴백 0).
    """
    board.LOCAL_CONF.write_text("session=x\nprefix=ACC\n", encoding="utf-8")
    assert board.id_prefix(None) is None


# ── 미해소 → None (무prefix `T-NNNN` = `none` 카테고리) ────────────────────

def test_id_prefix_none_when_unset(board):
    """override 도 areas 등록도 lease 도 없으면 None (무prefix `none` 카테고리)."""
    # local.conf 부재.
    assert board.id_prefix(None) is None
    # local.conf 는 있지만 areas 등록이 없음 → 여전히 None.
    board.LOCAL_CONF.write_text("session=x\n", encoding="utf-8")
    assert board.id_prefix(None) is None


# ── layer 3: count-based (등록 repo 정확히 1개 → 그 prefix) ─────────────────

def test_id_prefix_single_registered_repo_count_based(board):
    """등록 repo 정확히 1개 + 세션 무바인딩 → 그 repo 의 prefix (count-based·ADR-0040 D3).

    local.conf prefix 도 없이 areas.md 단일 등록만으로 prefix 를 유도한다 — areas 가 단일
    진실이므로 등록 prefix 가 무시되던 갭을 닫는다(T-0123 Part A 의 '단일 self-host=legacy
    T-NNNN' 를 supersede). areas.md 자체가 없는 진짜 solo 는 layer 5(None)로 무변경.
    """
    board.areas_append("PAY", "결제", "alice", repo="pay")  # 등록 repo 1개(repo=pay·prefix=PAY)
    assert board.id_prefix(None) == "PAY"


def test_id_prefix_multi_repo_ambiguous_returns_none(board):
    """등록 repo ≥2 + 세션 무바인딩 → None(모호) — cmd_new fail-loud 을 유발(ADR-0040 D3)."""
    board.areas_append("PAY", "결제", "alice", repo="pay")
    board.areas_append("SHIP", "배송", "bob", repo="ship")
    # count-based 는 정확히 1개일 때만 유도. ≥2 는 세션 유도 없으면 모호 → None.
    assert board.id_prefix(None) is None


def test_id_prefix_multi_repo_ignores_local_conf_prefix(board):
    """등록 repo ≥2 면 local.conf prefix= 를 무시한다 → None (silent 오네임스페이스 차단).

    ADR-0040 핵심: per-clone `prefix=` 가 multi-repo 에서 남의 prefix 로 오귀속하던 클래스.
    T-0779 가 그 층 자체를 지웠으므로 등록 수와 무관하게 이 키는 소스가 아니다.
    """
    board.areas_append("PAY", "결제", "alice", repo="pay")
    board.areas_append("SHIP", "배송", "bob", repo="ship")
    board.LOCAL_CONF.write_text("prefix=PAY\n", encoding="utf-8")
    assert board.id_prefix(None) is None


# ── layer 2: 세션 유도 (`<repo>_<N>` → repo → areas.md 행 prefix) ───────────

def test_id_prefix_session_derives_prefix_from_areas(board):
    """세션 유도(layer 2): 바인딩 세션 `<repo>_<N>` → repo → areas.md 행 prefix.

    등록 repo 2개(count-based 는 모호)여도 세션이 `ship_1` 이면 repo `ship` → prefix SHIP.
    세션 유도가 count-based(모호 None)보다 우선함을 격리 검증한다.
    """
    board.areas_append("PAY", "결제", "alice", repo="pay")
    board.areas_append("SHIP", "배송", "bob", repo="ship")
    _seed_lease(board, "ship_1", repo="ship")   # 단일 lease → session_name = ship_1
    assert board.id_prefix(None) == "SHIP"


def test_id_prefix_session_derivation_non_slot_form_skips(board):
    """세션명이 `<repo>_<N>` 형태 아님(솔로 커스텀 `pm`) → 세션 유도 skip → 다음 층(count-based).

    끝 마디가 숫자가 아닌 커스텀 세션명은 repo 를 못 파싱하므로 layer 2 를 건너뛴다.
    등록 repo 1개이므로 count-based 로 그 prefix 를 유도한다.
    """
    board.areas_append("PAY", "결제", "alice", repo="pay")  # 등록 repo 1개
    _seed_lease(board, "pm", repo="pay")   # 커스텀 세션명(끝 마디 숫자 아님) → 파싱 skip
    assert board.id_prefix(None) == "PAY"


def test_id_prefix_session_repo_unregistered_falls_through(board):
    """세션 repo 가 areas.md 에 미등록 → 세션 유도 None → 다음 층으로 폴백.

    세션 `other_1` → repo `other` 인데 areas 엔 pay 만 등록 → layer 2 None →
    count-based(등록 1개) → PAY.
    """
    board.areas_append("PAY", "결제", "alice", repo="pay")
    _seed_lease(board, "other_1", repo="other")
    assert board.id_prefix(None) == "PAY"


# ── 단위: _repo_from_session (세션명 `<repo>_<N>` 역파싱) ───────────────────

@pytest.mark.parametrize("session,expected", [
    ("project_manager_1", "project_manager"),  # repo 명에 `_` 포함 — 마지막 `_숫자` 만 슬롯
    ("pay_1", "pay"),
    ("ship_12", "ship"),                        # 다자리 슬롯 번호
    ("a_2_3", "a_2"),                           # 마지막 `_숫자` 마디만 분리
    ("pm", None),                               # 커스텀 솔로 세션명(무 `_`)
    ("my-session", None),                       # 하이픈·무 `_숫자`
    ("foo_bar", None),                          # 끝 마디 비숫자
    ("_1", None),                               # repo 부분 빈
    ("", None),                                 # 빈 문자열
])
def test_repo_from_session_parses_slot_form(board, session, expected):
    """`<repo>_<N>` 만 repo 로 역파싱하고, 형태 밖은 None(세션 유도 skip)."""
    assert board._repo_from_session(session) == expected


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
# cmd_new 명시 prefix 정책 (ADR-0042 자유 입력 — 등록 제약 없음)
# ════════════════════════════════════════════════════════════════════════

def _new_args(title="t", prefix=None, user_ack=None):
    return argparse.Namespace(title=title, prefix=prefix, touches=None,
                              depends=None, tag=None, estimate="small",
                              user_ack=user_ack)


def test_cmd_new_allows_user_approved_unregistered_prefix(board):
    """ADR-0042 amend: 미등록 prefix도 사용자 값-결속 ack가 있으면 발행된다.

    구 모델(prefix=repo 네임스페이스)의 "미등록 prefix rc1" 가드를 폐지했다(ADR-0042 §3.1
    "등록 제약 없음"). prefix 는 이제 작업 카테고리라 새 카테고리를 즉석에서 붙일 수 있다 —
    입력측 sanity와 사용자 승인을 통과하면 등록 여부와 무관하게 `T-<p>-NNN` 발행.
    """
    board.areas_append("pay", "결제", "alice")  # pay 만 등록 — 그래도 미등록 acc 허용
    rc = board.cmd_new(_new_args(prefix="acc", user_ack="acc"))
    assert rc == 0
    created = list((board.TICKETS_DIR / "open").glob("T-acc-001-*.md"))
    assert len(created) == 1
    fm, _ = board.load_ticket(created[0])
    assert fm["id"] == "T-acc-001"


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
    err = capsys.readouterr().err
    assert "prefix 필요" in err
    assert "1순위로 사용자에게" in err and "승인을 요청" in err
    assert "승인한 사용자만" in err and "--user-ack <PFX>" in err
    assert "세션 자동 부착 금지" in err


def test_cmd_new_guard_allows_registered_prefix(board):
    """등록된 prefix → 정상 발행 (T-pay-001)."""
    board.areas_append("pay", "결제", "alice")
    rc = board.cmd_new(_new_args(prefix="pay"))
    assert rc == 0
    created = list((board.TICKETS_DIR / "open").glob("T-pay-001-*.md"))
    assert len(created) == 1
    fm, _ = board.load_ticket(created[0])
    assert fm["id"] == "T-pay-001"


def test_cmd_new_solo_no_registry_emits_legacy_id(board):
    """areas.md 부재(solo) → 가드 off, legacy T-NNNN 발행."""
    rc = board.cmd_new(_new_args(prefix=None))
    assert rc == 0
    created = list((board.TICKETS_DIR / "open").glob("T-0001-*.md"))
    assert len(created) == 1


def test_cmd_new_single_registered_repo_derives_prefix(board):
    """등록 repo 1개 + prefix 미명시 → count-based 로 그 repo 의 prefix 유도(ADR-0040 D3).

    **ADR-0040 D3 가 T-0123 Part A('단일 self-host = legacy T-NNNN')를 supersede** —
    areas.md 에 등록 repo 가 정확히 1개면 그 prefix 가 단일 진실이므로 `--prefix` 없이도
    자동 유도돼 `T-<prefix>-NNN` 을 발행한다(등록 prefix 가 무시되던 갭을 닫는다). 진짜
    solo(areas.md 자체가 없음)는 여전히 legacy T-NNNN — `test_cmd_new_solo_no_registry_emits_legacy_id`.
    """
    board.areas_append("PAY", "결제", "alice")  # 등록 repo 1개(repo=prefix=PAY)
    rc = board.cmd_new(_new_args(prefix=None))
    assert rc == 0
    created = list((board.TICKETS_DIR / "open").glob("T-PAY-001-*.md"))
    assert len(created) == 1
    fm, _ = board.load_ticket(created[0])
    assert fm["id"] == "T-PAY-001"


def test_cmd_new_multi_repo_session_bound_derives_prefix(board):
    """등록 repo ≥2 이어도 세션 바인딩(단일 lease)이면 repo→prefix 유도로 발행 성공(ADR-0040 D3).

    모호(≥2)여도 세션이 `service-b_1` 이면 repo `service-b`(prefix ACC)로 좁혀져 fail-loud 을
    피하고 T-ACC-001 을 발행한다 — silent 오귀속 없이 정확한 네임스페이스.
    """
    board.areas_append("PAY", "결제", "alice", repo="service-a")
    board.areas_append("ACC", "정산", "bob", repo="service-b")
    _seed_lease(board, "service-b_1", repo="service-b")
    rc = board.cmd_new(_new_args(prefix=None))
    assert rc == 0
    created = list((board.TICKETS_DIR / "open").glob("T-ACC-001-*.md"))
    assert len(created) == 1
    fm, _ = board.load_ticket(created[0])
    assert fm["id"] == "T-ACC-001"


def test_cmd_new_single_registered_repo_honors_explicit_prefix(board):
    """등록 repo 1개라도 명시 prefix(그 등록값)는 존중 → prefixed ID 발행.

    가드가 ≤1 에서 off 여도, 사용자가 그 등록 prefix 를 *명시*하면 prefixed `T-PFX-NNN` 을
    발행한다(prefix optional·명시 우선).
    """
    board.areas_append("pay", "결제", "alice")  # 등록 repo 1개
    rc = board.cmd_new(_new_args(prefix="pay"))
    assert rc == 0
    created = list((board.TICKETS_DIR / "open").glob("T-pay-001-*.md"))
    assert len(created) == 1
    fm, _ = board.load_ticket(created[0])
    assert fm["id"] == "T-pay-001"


# ════════════════════════════════════════════════════════════════════════
# cmd_init team 경로
# ════════════════════════════════════════════════════════════════════════

def _init_args(prefix=None, area=None, owner=None, repo=None, slot=None, user=None,
               user_ack=None):
    return argparse.Namespace(prefix=prefix, area=area, owner=owner,
                              repo=repo, slot=slot, user=user, user_ack=user_ack)


@pytest.fixture
def init_board(board, monkeypatch):
    """cmd_init 용 board — 실 git/stdin 부작용 헬퍼를 무해 stub 으로 차단한다.

    install_pre_push_hook 은 실 REPO 의 git hooks 를, prompt_additional_reviewer_optin 은
    stdin 을 건드린다. cmd_init 의 areas/local.conf 효과만 검증하려고 둘을 stub.
    PM_STATE_TEMPLATE 부재로 pm_state 생성은 자연히 skip 된다.
    """
    monkeypatch.setattr(board, "install_pre_push_hook", lambda: False)
    monkeypatch.setattr(board, "prompt_additional_reviewer_optin", lambda: None)
    return board


def _areas_row(board, repo: str) -> dict:
    """areas.md 에서 그 repo 행 dict — 등록 칼럼 값 단언용(헤더-인식 파서 재사용)."""
    _header, rows = board._parse_areas()
    matches = [row for row in rows if row.get("repo") == repo]
    assert len(matches) == 1, f"repo {repo!r} 행이 {len(matches)}개"
    return matches[0]


def test_cmd_init_prefixed_registers_row_and_writes_no_identity_keys(init_board):
    """init --prefix pay --area 결제 --owner alice → areas 등록행 1개 · conf 엔 정체성 키 0.

    prefix·session 은 per-clone conf 의 범위가 아니다(T-0779) — 등록은 areas.md 행,
    conf 는 operational 키만 갖는다.
    """
    rc = init_board.cmd_init(_init_args(
        prefix="pay", area="결제", owner="alice", user_ack="pay"))
    assert rc == 0
    # areas.md 등록행 — repo 칼럼 = 이 clone(루트 폴더명) · prefix 칼럼 = 카테고리(두 축 분리).
    assert init_board.registered_prefixes() == {"pay"}
    assert init_board.registered_repos() == {init_board.REPO.name}
    areas = init_board.AREAS_FILE.read_text(encoding="utf-8")
    assert f"| {init_board.REPO.name} | pay |  |  | alice |" in areas
    conf = init_board.LOCAL_CONF.read_text(encoding="utf-8")
    assert "prefix=" not in conf and "session=" not in conf


def test_cmd_init_prefixed_rerun_no_duplicate_areas(init_board):
    """이미 등록된 prefix 로 재실행 → areas.md 중복행 없음(멱등)·conf 정체성 키 여전히 0."""
    init_board.cmd_init(_init_args(
        prefix="pay", area="결제", owner="alice", user_ack="pay"))
    # 재실행: --area 없이도 통과해야 한다 (이미 등록).
    rc = init_board.cmd_init(_init_args(prefix="pay"))
    assert rc == 0
    areas = init_board.AREAS_FILE.read_text(encoding="utf-8")
    rows = [line for line in areas.splitlines()
            if line.startswith("| ") and not line.startswith("| repo |")]
    assert len(rows) == 1                       # 중복 등록 안 됨(행 1개 유지)
    assert rows[0].startswith(f"| {init_board.REPO.name} | pay |")
    conf = init_board.LOCAL_CONF.read_text(encoding="utf-8")
    assert "prefix=" not in conf and "session=" not in conf


def test_cmd_init_without_prefix_registers_repo_row_with_empty_prefix(init_board):
    """무prefix init 도 areas repo 행을 등록한다 — prefix 칼럼만 빈다(등록 0 형상 폐지).

    repo 이름은 clone 루트 폴더명에서 유도한다. `--area`·사용자 승인 없이 통과해야
    비대화형 온보딩(`pm_import.run_board_init`)이 인자 0으로 성립한다.
    """
    rc = init_board.cmd_init(_init_args())
    assert rc == 0
    assert init_board.registered_repos() == {init_board.REPO.name}
    assert init_board.registered_prefixes() == set()     # prefix 칼럼은 빈 채
    assert _areas_row(init_board, init_board.REPO.name)["prefix"] == ""
    # 무prefix 홈의 발행 형식은 `T-NNNN`(none 카테고리) 그대로.
    assert init_board.id_prefix(None) is None


def test_cmd_init_without_prefix_is_idempotent(init_board):
    """무prefix init 재실행 → repo 행 중복 없음(멱등)."""
    assert init_board.cmd_init(_init_args()) == 0
    assert init_board.cmd_init(_init_args()) == 0
    areas = init_board.AREAS_FILE.read_text(encoding="utf-8")
    assert areas.count(f"| {init_board.REPO.name} |") == 1


def test_cmd_init_repo_name_override_registers_that_name(init_board):
    """`--repo` 명시가 등록 repo 이름이 된다(폴더명 유도보다 우선)."""
    assert init_board.cmd_init(_init_args(repo="svc", slot=1)) == 0
    assert init_board.registered_repos() == {"svc"}


def test_cmd_init_new_prefix_without_area_rejected(init_board):
    """새 prefix(형식 정상)인데 --area 누락 → 거부(비0), areas.md 미생성.

    prefix 는 유효 형식(`new`)이라 sanity 를 통과하고, *그다음* --area 누락 가드에 걸린다
    (형식 reject 로 short-circuit 되지 않고 area-missing 경로를 실제로 탄다).
    """
    rc = init_board.cmd_init(_init_args(prefix="new", user_ack="new"))
    assert rc != 0
    assert not init_board.AREAS_FILE.exists()


def test_cmd_init_owner_defaults_to_session_name(init_board, monkeypatch):
    """--owner 누락 시 owner 가 session_name() 해소값으로 채워진다 (등록행에 반영).

    cmd_init 의 owner 기본값은 `session_name()`(override 없이) — env(PM_SESSION_NAME /
    CLAUDE_SESSION_NAME alias) > 단일-lease 유도 순으로 해소된다. 결정성을 위해 env 를
    고정해 그 값이 등록행 owner 로 들어가는지만 검증한다.
    """
    monkeypatch.setenv("CLAUDE_SESSION_NAME", "ambient-sess")
    rc = init_board.cmd_init(_init_args(prefix="acc", area="정산", user_ack="acc"))
    assert rc == 0
    areas = init_board.AREAS_FILE.read_text(encoding="utf-8")
    # 신 스키마: repo=clone 폴더명·prefix=카테고리·git/test_cmd 빈 값·owner=session_name() 해소값.
    assert f"| {init_board.REPO.name} | acc |  |  | ambient-sess |" in areas


# ── cmd_init area_owner 해소 (T-0161 델타·ADR-0033 ③·codex must-fix) ──────────
# `init --prefix` 등록행이 area_owner 칼럼을 항상 빈 값으로 남기던 갭의 durable 가드.
# 해소 우선순위는 cmd_repo_add 와 동형: `--user` > local.conf user= > git email > None.

def test_cmd_init_area_owner_from_explicit_user(init_board, monkeypatch):
    """--user 명시가 area_owner 칼럼에 박힌다 (local.conf user=·git 폴백보다 우선)."""
    # local.conf user= 와 git 폴백이 다른 값을 줘도 --user 가 이긴다.
    init_board.LOCAL_CONF.write_text("identity.user=conf-user\n", encoding="utf-8")
    monkeypatch.setattr(init_board, "_git_config_email", lambda: "git@x.com")
    rc = init_board.cmd_init(_init_args(
        prefix="pay", area="결제", owner="alice", user="carol", user_ack="pay"))
    assert rc == 0
    # 신 8칸 스키마 끝 칼럼 area_owner=carol → _repo_area_owner 로 확증(`--mine` 풀 입력).
    assert init_board._repo_area_owner(init_board.REPO.name) == "carol"


def test_cmd_init_area_owner_falls_back_to_local_conf(init_board, monkeypatch):
    """--user 미지정 → local.conf user= 로 area_owner 해소 (git 폴백보다 우선)."""
    init_board.LOCAL_CONF.write_text("identity.user=conf-user\n", encoding="utf-8")
    monkeypatch.setattr(init_board, "_git_config_email", lambda: "git@x.com")
    rc = init_board.cmd_init(_init_args(
        prefix="acc", area="정산", owner="bob", user_ack="acc"))
    assert rc == 0
    assert init_board._repo_area_owner(init_board.REPO.name) == "conf-user"


def test_cmd_init_area_owner_falls_back_to_git_email(init_board, monkeypatch):
    """--user·local.conf user= 둘 다 미설정 → git config user.email 로 area_owner 해소."""
    # cmd_init 이 local.conf 를 write 하기 전에 user_name() 을 부르므로, user 키 없는
    # local.conf 를 미리 둬 그 경로(부재→git 폴백)를 결정적으로 탄다.
    init_board.LOCAL_CONF.write_text("session=acc-pm\n", encoding="utf-8")
    monkeypatch.setattr(init_board, "_git_config_email", lambda: "dev@example.com")
    rc = init_board.cmd_init(_init_args(
        prefix="ord", area="주문", owner="carol", user_ack="ord"))
    assert rc == 0
    assert init_board._repo_area_owner(init_board.REPO.name) == "dev@example.com"


def test_cmd_init_owner_empty_when_session_unbound(init_board, monkeypatch):
    """세션 미바인딩(env·lease 없음)이면 owner 칼럼이 빈 채 등록된다 — init 은 fail-loud 안 한다.

    init 은 lease 장부·세션 바인딩이 아직 없는 부트스트랩 지점이라, 미해소를 거부하면
    비대화형 온보딩이 통째로 rc≠0 이 된다. 귀속 쓰기 게이트는 그 뒤 단계(`repo add`·claim)다.
    """
    monkeypatch.delenv("PM_SESSION_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_NAME", raising=False)
    assert init_board.cmd_init(_init_args()) == 0
    assert _areas_row(init_board, init_board.REPO.name)["owner"] == ""


def test_cmd_init_area_owner_graceful_when_user_unknown(init_board, monkeypatch):
    """user 미상(--user·local.conf·git 전부 없음) → area_owner 빈 칼럼·_repo_area_owner None.

    `--mine` 풀에 안 잡히지만 등록 자체는 graceful 진행(기존 슬롯-only 동작 보존).
    """
    init_board.LOCAL_CONF.write_text("session=s\n", encoding="utf-8")
    monkeypatch.setattr(init_board, "_git_config_email", lambda: None)
    rc = init_board.cmd_init(_init_args(
        prefix="inv", area="재고", owner="dave", user_ack="inv"))
    assert rc == 0
    assert init_board._repo_area_owner("inv") is None
    # owner(registrant)는 정상 기록 — area_owner 만 빈 값(두 칼럼 독립·overload 금지).
    row = init_board._areas_row_for_prefix("inv")
    assert row["owner"] == "dave"
    assert row["area_owner"] == ""


# ════════════════════════════════════════════════════════════════════════
# e2e 스모크: team init → multi new → solo new 공존(disjoint) → 1사이클
# ════════════════════════════════════════════════════════════════════════

def test_e2e_team_init_then_multi_and_solo_coexist(init_board, monkeypatch):
    """team init → T-pay-001 발행 → 같은 보드에서 solo new 도 T-NNNN 발행되어 공존.

    multi/solo disjoint 를 *실 명령 경로*(cmd_init/cmd_new)로 확증한다. 이어 claim·
    complete 1사이클이 크래시 없이 도는지(파일 이동·게이트 통과) 검증한다.
    """
    board = init_board

    # 1. team init — pay 등록 + local.conf prefix=pay.
    assert board.cmd_init(_init_args(
        prefix="pay", area="결제", owner="alice", user_ack="pay")) == 0
    assert board.registered_prefixes() == {"pay"}

    # 2. multi new — local.conf prefix=pay 로 해소되어 T-pay-001.
    assert board.cmd_new(_new_args(title="multi ticket")) == 0
    pay = list((board.TICKETS_DIR / "open").glob("T-pay-001-*.md"))
    assert len(pay) == 1

    # 3. solo new — 같은 보드에 override prefix="" 로? — solo 발행은 prefix 미지정 +
    #    가드 회피가 필요하다. multi 모드(areas.md 존재)에서 solo legacy ID 를 같은
    #    보드에 직접 심어 공존(disjoint)을 확증한다 (가드는 multi 모드에선 solo new 를
    #    거부하는 게 설계 — 공존은 _next_id 네임스페이스 분리로 보장됨).
    _seed_ticket(board, "T-0001")
    # solo 네임스페이스 next 는 T-pay-001 에 간섭받지 않는다.
    assert board._next_id(None) == "T-0002"
    # multi 네임스페이스 next 는 T-0001 에 간섭받지 않는다.
    assert board._next_id("pay") == "T-pay-002"

    # 4. list/claim/complete 1사이클 — 크래시 없이.
    assert board.cmd_list(argparse.Namespace(status=None, tag=None)) == 0

    pay_id = "T-pay-001"
    # 귀속 쓰기는 세션 명시가 전제다(폴백 폐지) — env 로 바인딩한다.
    monkeypatch.setenv("PM_SESSION_NAME", "pay_1")
    claim_args = argparse.Namespace(id=pay_id, session="pay-pm")
    assert board.cmd_claim(claim_args) == 0
    assert list((board.TICKETS_DIR / "claimed").glob(f"{pay_id}-*.md"))

    _check_off_dod(board, pay_id)
    complete_args = argparse.Namespace(
        id=pay_id, tests_pass=True, allow_missing_log=True, allow_untested=False,
        # lifecycle unit seam: public 사용자는 ticket_finish 묶음 종결만 쓴다.
        cluster_close=f"C-{pay_id}")
    assert board.cmd_complete(complete_args) == 0
    assert list((board.TICKETS_DIR / "done").glob(f"{pay_id}-*.md"))


def test_e2e_no_prefix_board_flow(init_board, monkeypatch):
    """무prefix 보드 — init(인자 0)·new·claim·complete 1사이클 무크래시.

    등록은 되지만(repo 행) prefix 칼럼이 비어 발행은 `T-NNNN`(none 카테고리)다.
    """
    board = init_board
    assert board.cmd_init(_init_args()) == 0
    assert board.registered_repos() == {board.REPO.name}   # 등록 0 형상 없음
    assert board.registered_prefixes() == set()            # 가드 신호 off

    assert board.cmd_new(_new_args(title="solo ticket")) == 0
    created = list((board.TICKETS_DIR / "open").glob("T-0001-*.md"))
    assert len(created) == 1

    monkeypatch.setenv("PM_SESSION_NAME", "pm_1")
    assert board.cmd_claim(argparse.Namespace(id="T-0001", session="pm")) == 0
    _check_off_dod(board, "T-0001")
    assert board.cmd_complete(argparse.Namespace(
        id="T-0001", tests_pass=True, allow_missing_log=True,
        allow_untested=False, cluster_close="C-T-0001")) == 0
    assert list((board.TICKETS_DIR / "done").glob("T-0001-*.md"))


# ════════════════════════════════════════════════════════════════════════
# init framing 라벨 회귀 (T-0085·ADR-0016) — multi-PM = N 세션 × M repo 한 개념.
# "팀(team)=다중-사람 협업" framing 제거 → multi-repo (N×M·prefix 네임스페이스).
# 머시너리(prefix·areas·네임스페이스·가드)는 불변(amend·supersede 아님) — 표면 라벨만 검증.
# ════════════════════════════════════════════════════════════════════════

def test_init_prefixed_label_names_repo_and_category(init_board, capsys):
    """prefix init 의 완료 라벨 = `repo <repo> · 카테고리 <prefix>` (협업 "팀" framing 제거).

    동작(areas 등록·prefix 네임스페이스)은 다른 테스트가 커버 — 여기선 *framing 라벨*만
    회귀 박제한다. ID 포맷 `T-<PFX>-NNN` 도 같이 출력되어야 한다.
    """
    rc = init_board.cmd_init(_init_args(
        prefix="pay", area="결제", owner="alice", user_ack="pay"))
    assert rc == 0
    out = capsys.readouterr().out
    assert f"repo {init_board.REPO.name} · 카테고리 pay" in out   # 등록 축(repo)·분류 축(카테고리)
    assert "T-pay-NNN" in out                  # 네임스페이스 ID 포맷 라벨
    assert "팀" not in out                      # 협업 framing 제거 (ADR-0016·ADR-0002 amend)


def test_init_no_prefix_label_is_none_category_not_solo(init_board, capsys):
    """prefix 없는 init 의 완료 라벨 = `카테고리 none(무prefix)` + `T-NNNN (none 카테고리)`.

    "solo" 는 별도 모드가 아니라 N=1 부분집합이므로 라벨에서 사라졌다(T-0779) — 갈리는 축은
    카테고리(prefix 유무)뿐이다.
    """
    rc = init_board.cmd_init(_init_args())
    assert rc == 0
    out = capsys.readouterr().out
    assert f"repo {init_board.REPO.name} · 카테고리 none(무prefix)" in out
    assert "T-NNNN (none 카테고리)" in out
    assert "solo" not in out and "팀" not in out
    assert init_board.cmd_new(_new_args(title="solo")) == 0
    assert list((init_board.TICKETS_DIR / "open").glob("T-0001-*.md"))


def test_init_namespaced_registers_and_emits_namespaced_id(init_board):
    """multi-repo(prefix) init → areas 등록 + 세션 유도로 네임스페이스 발행.

    머시너리 무파손의 핵심 증거: prefix 가 있으면 레지스트리 등록·네임스페이스
    ID(`T-<PFX>-NNN`) 발행이 그대로 동작한다. 미등록 명시 prefix 도 ADR-0042 자유 입력이라
    sanity 통과 시 발행된다(구 "미등록 거부" 가드 폐지).
    """
    assert init_board.cmd_init(_init_args(
        prefix="acc", area="정산", owner="bob", user_ack="acc")) == 0
    assert init_board.registered_prefixes() == {"acc"}     # 레지스트리 등록
    # 등록 prefix → 세션/count 유도로 네임스페이스 발행.
    assert init_board.cmd_new(_new_args(title="acc ticket")) == 0
    assert list((init_board.TICKETS_DIR / "open").glob("T-acc-001-*.md"))
    # 미등록 명시 prefix(형식 정상) → ADR-0042 자유 입력으로 발행(등록 제약 없음).
    assert init_board.cmd_new(_new_args(
        title="new cat", prefix="zzz", user_ack="zzz")) == 0
    assert list((init_board.TICKETS_DIR / "open").glob("T-zzz-001-*.md"))


# ════════════════════════════════════════════════════════════════════════
# prefix 입력 sanity (ADR-0042·T-0237·DoD 2) — 예약어 `none`·형식 [a-z0-9_]+
# ════════════════════════════════════════════════════════════════════════

def test_validate_prefix_helper(board):
    """`_validate_prefix` 단위 — 예약어(case-insensitive)/특수문자 거부·대소문자 통과 (ADR-0055)."""
    assert board._validate_prefix("none") is not None      # 예약어
    assert board._validate_prefix("NONE") is not None       # 예약어 (case-insensitive fold·ADR-0055)
    assert board._validate_prefix("None") is not None       # 예약어 (case-insensitive fold·ADR-0055)
    assert board._validate_prefix("pay-x") is not None       # 하이픈 (ID 구분자 충돌)
    assert board._validate_prefix("pay x") is not None       # 공백
    assert board._validate_prefix("") is not None            # 빈 문자열
    assert board._validate_prefix("pay") is None             # 정상 소문자
    assert board._validate_prefix("Foo") is None             # 대문자 허용 (ADR-0055)
    assert board._validate_prefix("AAA") is None             # 대문자 허용 (ADR-0055)
    assert board._validate_prefix("bill_ing") is None        # 언더스코어 허용
    assert board._validate_prefix("p2") is None              # 숫자 허용


def test_cmd_new_rejects_reserved_prefix_none(board, capsys):
    """`new --prefix none` → rc 1·부작용 0 (예약어 — 무prefix 1급 인자)."""
    rc = board.cmd_new(_new_args(prefix="none"))
    assert rc == 1
    assert "예약어" in capsys.readouterr().err
    # 어떤 status 디렉토리에도 티켓/draft 미발행(부작용 0).
    for status in ("open", "claimed", "blocked", "done"):
        assert list((board.TICKETS_DIR / status).glob("T-*.md")) == []


def test_cmd_new_accepts_uppercase_prefix(board):
    """`new --prefix AAA`(대문자) → rc 0·`T-AAA-001` 발행 (ADR-0055·case 허용·DoD 1)."""
    rc = board.cmd_new(_new_args(prefix="AAA", user_ack="AAA"))
    assert rc == 0
    created = list((board.TICKETS_DIR / "open").glob("T-AAA-001-*.md"))
    assert len(created) == 1
    fm, _ = board.load_ticket(created[0])
    assert fm["id"] == "T-AAA-001"


def test_cmd_new_accepts_valid_lowercase_prefix(board):
    """정상 소문자 prefix 는 통과 → prefixed ID 발행 (solo·레지스트리 부재)."""
    rc = board.cmd_new(_new_args(prefix="pay", user_ack="pay"))
    assert rc == 0
    assert list((board.TICKETS_DIR / "open").glob("T-pay-001-*.md"))


def test_cmd_init_rejects_reserved_prefix_none(init_board, capsys):
    """`init --prefix none` → rc 1·부작용 0 (areas 미생성)."""
    rc = init_board.cmd_init(_init_args(prefix="none", area="x", owner="me"))
    assert rc == 1
    assert "예약어" in capsys.readouterr().err
    assert not init_board.AREAS_FILE.exists()


def test_cmd_init_accepts_uppercase_prefix(init_board):
    """`init --prefix AAA`(대문자) → rc 0·areas 등록 (ADR-0055·case 허용)."""
    rc = init_board.cmd_init(_init_args(
        prefix="AAA", area="x", owner="me", user_ack="AAA"))
    assert rc == 0
    assert init_board.registered_prefixes() == {"AAA"}     # 등록 case 보존


# ════════════════════════════════════════════════════════════════════════
# prefix case-insensitivity (ADR-0055·T-0311·DoD) — 대소문자 허용 + fold 동일성
# + canonical case 보존. 단일 불변식: "prefix 비교는 case-insensitive, canonical case 보존".
# ════════════════════════════════════════════════════════════════════════

def test_fold_lookup_helper(board):
    """`_fold_lookup`은 fold-매치 canonical을 결정적으로 되찾는다 (ADR-0055)."""
    assert board._fold_lookup("aaa", {"AAA"}) == "AAA"      # fold 로 등록 case 되찾기
    assert board._fold_lookup("AAA", {"AAA"}) == "AAA"      # 정확 매치
    assert board._fold_lookup("zzz", {"AAA"}) is None       # 무매치


def test_next_id_case_fold_continues_existing_series(board):
    """`--prefix aaa` 는 기존 `T-AAA-*` 시리즈를 이어간다(case-fold 카운트·등록 case 보존·DoD 2)."""
    _seed_ticket(board, "T-AAA-003")
    # 소문자 입력이 대문자 시리즈로 fold → 등록 case `AAA` 로 이어 발행(신규 `T-aaa-*` 안 만듦).
    assert board._next_id("aaa") == "T-AAA-004"
    assert board._next_id("AAA") == "T-AAA-004"   # 대문자 입력도 동일


def test_cmd_new_lowercase_input_continues_uppercase_series(board):
    """실 명령 경로: `T-AAA-*` 보드에 `new --prefix aaa` → `T-AAA-002`(신규 `T-aaa-*` 없음·DoD 2).

    판정은 **발행 ID(frontmatter)** 로 한다 — 파일명 glob 은 대소문자 무시 파일시스템
    (Windows·macOS)에서 `T-aaa-*` 가 `T-AAA-*` 파일까지 매치해 "case-분할 없음"을 증명하지
    못한다(Windows 실측 실패 지점)."""
    _seed_ticket(board, "T-AAA-001")
    rc = board.cmd_new(_new_args(prefix="aaa", title="lower input"))
    assert rc == 0
    assert _issued_ids(board) == {"T-AAA-001", "T-AAA-002"}   # 등록 case 로 이어감·case-분할 없음


def test_next_prefixed_id_follows_frontmatter_not_the_filename_case(board):
    """시리즈 판정의 진실은 frontmatter ID 다 — 파일명 case 가 갈려도 발행 case 가 안 뒤집힌다.

    대소문자 무시 파일시스템에서는 `T-aaa-001-x.md` 쓰기가 기존 `T-AAA-001-x.md` **파일 안으로**
    들어가 파일명(`AAA`)과 내용(`aaa`)이 갈린다. 파일명으로 세면 발행 case 가 "그 파일이 어느
    이름으로 먼저 만들어졌는가"로 결정된다 — Linux 에서 그 형상을 직접 주입해 재현한다."""
    path = board.TICKETS_DIR / "open" / "T-AAA-001-seed.md"      # 파일명 case = AAA
    board.dump_ticket(path, {"id": "T-aaa-001", "title": "seed", "status": "open"},
                      "# seed\n")                               # 발행 ID case = aaa
    assert board._next_id("aaa") == "T-aaa-002"
    assert board._next_id("AAA") == "T-aaa-002"                 # fold 는 같은 시리즈


def test_next_prefixed_id_falls_back_to_the_filename_for_a_broken_id(board):
    """frontmatter `id` 가 ID 문법이 아니면 파일명으로 폴백한다 — 번호 누락은 곧 clobber."""
    board.dump_ticket(board.TICKETS_DIR / "open" / "T-AAA-007-seed.md",
                      {"id": "쓰레기값", "title": "seed", "status": "open"}, "# seed\n")
    assert board._next_id("AAA") == "T-AAA-008"


def test_next_prefixed_id_is_stable_across_case_split_pollution(board):
    """같은 번호의 case 오염(`T-AAA-001`+`T-aaa-001`)에서도 발행 case 가 결정적이다.

    canonical 은 최저 번호 티켓의 case 인데 동률이면 tie-break 가 필요하다 — 파일 순회 순서에
    맡기면 같은 보드가 실행마다 다른 case 를 발행한다(파일시스템 의존)."""
    _seed_ticket(board, "T-AAA-001")
    board.dump_ticket(board.TICKETS_DIR / "open" / "T-aaa-001-other.md",
                      {"id": "T-aaa-001", "title": "seed", "status": "open"}, "# seed\n")
    assert board._next_id("aaa") == board._next_id("AAA") == "T-AAA-002"


def test_id_prefix_override_resolves_registered_canonical_case(board):
    """override `aaa` 가 등록 `AAA` 로 fold-매치되면 등록 case `AAA` 로 해소 (ADR-0055·surface #4)."""
    board.areas_append("AAA", "결제", "alice")            # 대문자 등록
    assert board.id_prefix("aaa") == "AAA"                # 소문자 입력 → 등록 canonical
    assert board.id_prefix("AAA") == "AAA"                # 정확 case 유지
    assert board.id_prefix("zzz") == "zzz"                # 미등록 → 입력 그대로


def test_cmd_init_fold_reuses_registered_canonical_prefix(init_board, capsys):
    """`init --prefix aaa`는 등록 `AAA`를 canonical 재사용하고 중복행을 만들지 않는다."""
    assert init_board.cmd_init(_init_args(
        prefix="AAA", area="x", owner="me", user_ack="AAA")) == 0
    rc = init_board.cmd_init(_init_args(prefix="aaa", area="x", owner="me"))
    assert rc == 0
    assert "이미 카테고리 'AAA' 로 등록됨" in capsys.readouterr().out
    assert init_board.registered_prefixes() == {"AAA"}    # 새 `aaa` 행 안 생김(단일 등록 유지)


def test_next_id_lowercase_and_legacy_unaffected(board):
    """소문자 보드·legacy `T-NNNN` 은 case 변경에 무영향 (회귀 0·DoD 4)."""
    _seed_ticket(board, "T-pay-005")
    assert board._next_id("pay") == "T-pay-006"           # 소문자 시리즈 그대로 이어감
    _seed_ticket(board, "T-0009")
    assert board._next_id(None) == "T-0010"               # legacy 순번 그대로


def test_next_id_substring_prefix_boundary(board):
    """`AB` vs `ABC` 는 다른 네임스페이스 — `-` 경계 durable 가드(fold 해도 substring 오검출 금지·T-0311)."""
    _seed_ticket(board, "T-ABC-001")
    _seed_ticket(board, "T-ABC-002")
    assert board._next_id("AB") == "T-AB-001"             # ABC 시리즈에 간섭 안 받음
    _seed_ticket(board, "T-AB-005")
    assert board._next_id("AB") == "T-AB-006"             # AB 만 카운트
    assert board._next_id("ABC") == "T-ABC-003"           # ABC 만 카운트


def test_fold_key_helper(board):
    """`_fold_key` — 문자열 소문자 fold·None(무prefix) 보존 (source 매칭·collision 정규화 비교키·ADR-0055)."""
    assert board._fold_key("AAA") == "aaa"
    assert board._fold_key("aaa") == "aaa"
    assert board._fold_key(None) is None                  # legacy 무prefix 는 None 키
    assert board._fold_key("AB") != board._fold_key("ABC")   # substring 경계 보존


def test_cmd_init_fold_reuses_ticket_prefix_canonical_case(board, capsys):
    """미등록 `T-aaa-*` 티켓이 있으면 `init --prefix AAA`가 그 canonical case를 재사용한다.

    `board` 픽스처(areas 부재)에 티켓만 심고 init 대칭 가드가 티켓 prefix 까지 본다는 걸 확증한다.
    """
    _seed_ticket(board, "T-aaa-001")                      # 미등록·소문자 시리즈만 존재
    rc = board.cmd_init(_init_args(prefix="AAA", area="x", owner="me"))
    assert rc == 0
    assert board.registered_prefixes() == {"aaa"}


# ════════════════════════════════════════════════════════════════════════
# 자동시드 폐지 데이터측 (ADR-0042·T-0237·DoD 1·4) — 빈 prefix 등록 → T-NNNN 유지
# ════════════════════════════════════════════════════════════════════════

def test_registered_prefixes_ignores_empty_prefix_rows(board):
    """빈 prefix 로 등록된 repo 는 `registered_prefixes()` 에 안 잡히고 `registered_repos()` 에 잡힌다.

    repo add 자동시드 폐지 후 형상 — areas 행은 있으나 prefix 칼럼이 빈 값.
    """
    board.areas_append("", "", "me", repo="svc", git="g")   # 빈 prefix 등록(자동시드 폐지)
    assert board.registered_prefixes() == set()             # prefix 로는 안 셈
    assert board.registered_repos() == {"svc"}              # repo 로는 셈
    row = board._parse_areas()[1][0]
    assert row["repo"] == "svc"
    assert row["prefix"] == ""                              # prefix 칼럼 빈 값


def test_empty_prefix_areas_next_id_stays_legacy(board):
    """빈 prefix 만 등록된 보드 → id_prefix None → `_next_id(None)` = T-NNNN 유지 (② 회귀·DoD 4).

    ②(project_manager_dev)가 areas prefix 를 비우면 count-based 유도가 None 으로 떨어져
    다음 티켓이 `T-project_manager-001` 로 튀지 않고 legacy `T-NNNN` 을 지속한다.
    """
    board.areas_append("", "", "me", repo="project_manager", git="g")
    assert board.id_prefix() is None                         # count-based 유도 없음(빈 prefix)
    assert board._next_id(None) == "T-0001"
    # 실 명령 경로로도 확증 — prefix 미명시 new 는 legacy ID 발행.
    assert board.cmd_new(_new_args(title="t")) == 0
    assert list((board.TICKETS_DIR / "open").glob("T-0001-*.md"))


# ════════════════════════════════════════════════════════════════════════
# prefix list 현황 (ADR-0042·T-0237·DoD 3) — read-only·none/prefix별 개수·번호범위
# ════════════════════════════════════════════════════════════════════════

def test_cmd_prefix_list_mixed_fixture(board, capsys):
    """혼합 픽스처(T-NNNN + T-pay-NNN)에서 none/pay 별 개수·번호범위를 정확 출력 (read-only)."""
    _seed_ticket(board, "T-0001")
    _seed_ticket(board, "T-0005", status="done")
    _seed_ticket(board, "T-pay-001", status="claimed")
    _seed_ticket(board, "T-pay-003")
    rc = board.cmd_prefix_list(argparse.Namespace())
    assert rc == 0
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if ln.strip()]
    none_line = next(ln for ln in lines if ln.split()[0] == "none")
    pay_line = next(ln for ln in lines if ln.split()[0] == "pay")
    # none: 2건·범위 T-0001 ~ T-0005.
    assert none_line.split()[1] == "2"
    assert "T-0001" in none_line and "T-0005" in none_line
    # pay: 2건·범위 T-pay-001 ~ T-pay-003.
    assert pay_line.split()[1] == "2"
    assert "T-pay-001" in pay_line and "T-pay-003" in pay_line
    # read-only — 티켓 4개 그대로(발행/이동 0).
    all_ids = [p.name for s in ("open", "claimed", "blocked", "done")
               for p in (board.TICKETS_DIR / s).glob("T-*.md")]
    assert len(all_ids) == 4


def test_cmd_prefix_list_single_ticket_range_collapses(board, capsys):
    """단일 티켓 prefix 는 범위를 단일 ID 로 접어 출력(min==max)."""
    _seed_ticket(board, "T-pay-007")
    rc = board.cmd_prefix_list(argparse.Namespace())
    assert rc == 0
    pay_line = next(ln for ln in capsys.readouterr().out.splitlines()
                    if ln.strip() and ln.split()[0] == "pay")
    assert pay_line.split()[1] == "1"
    assert pay_line.count("T-pay-007") == 1        # 단일 ID(범위 `~` 없음)
    assert "~" not in pay_line


def test_cmd_prefix_list_empty_board(board, capsys):
    """티켓 0개 → `(no tickets)` (크래시 0·read-only)."""
    assert board.cmd_prefix_list(argparse.Namespace()) == 0
    assert "(no tickets)" in capsys.readouterr().out


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
# (단일-lease 유도) > None. local.conf `session=` 층은 T-0779 가 폐지했다(slot 종속 값이
# 프로젝트 공용 conf 에 있던 범위 오류). 미해소 시 귀속 쓰기(required=True)는
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


def test_session_name_ignores_local_conf_session(board, monkeypatch):
    """env·lease 없음 + local.conf `session=foo` → None (폴백 폐지·T-0779).

    구 clone 에 남은 키를 조용히 세션으로 승격하던 층이 사라졌다. 조회 surface 는 None,
    귀속 쓰기는 fail-loud 다(아래 required 테스트) — 조용한 폴백 0.
    """
    _clear_env(monkeypatch)
    _write_conf(board, "session=foo\n")
    assert board.session_name() is None


def test_session_name_required_fail_loud_carries_explicit_identity_remedy(board, monkeypatch):
    """`session=` 만 있는 conf 에서 귀속 쓰기 → fail-loud 문구(실값 단언·조용한 폴백 0).

    픽스처는 구 init 이 실제로 쓰던 2줄(주석 + `session=pm`)이다.
    """
    _clear_env(monkeypatch)
    _write_conf(board, "# per-clone 설정 (git-ignored). board.py init 생성. clone 마다 다름.\n"
                       "session=pm\n")
    with pytest.raises(SystemExit) as exc:
        board.session_name(required=True)
    assert str(exc.value) == board.UNREGISTERED_SESSION_ABORT
    assert "--repo <repo> --slot <N>" in str(exc.value)
    # 미등록 홈(행 0)에는 마이그레이션 처방이 함께 나온다 — 조용한 폴백이 없으므로 처방이 필수다.
    assert "pm-update" in str(exc.value)


def test_session_name_solo_unbound_returns_none(board, monkeypatch):
    """env·lease 모두 없음 → None (구 host-pid 폴백 제거·ADR-0040).

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
    """required=True + 미해소(solo 무바인딩) → fail-loud(SystemExit·`--repo/--slot` 안내)."""
    _clear_env(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        board.session_name(required=True)
    # 안내 문구에 `--repo <repo> --slot <N>` 형식이 들어간다(ADR-0057).
    assert "--repo" in str(exc.value)


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


# ── 등록은 장부 행이 한다 (유도 층 폐지) ─────────
# 등록 repo 수와 무관하게 "장부 행 0" 에서 세션 이름을 만들지 않는다. 슬롯을 하나만 쓰는 홈도
# 자기 자신을 가리키는 N=1 행으로 등록되고, 그 행이 단일-lease 층에서 해소된다.


def test_session_name_zero_rows_stays_unresolved_even_with_single_registration(
        board, monkeypatch):
    """등록 repo 1개 + 장부 부재 → 미해소. 유도 층을 되살리면 이 단언이 red."""
    _clear_env(monkeypatch)
    board.areas_append("SOLO", "단일", "alice", repo="solo")
    assert board.session_name() is None


def test_session_name_zero_rows_empty_list_stays_unresolved(board, monkeypatch):
    """등록 repo 1개 + `leases` 빈 배열 → 미해소(부재와 같은 판정)."""
    _clear_env(monkeypatch)
    board.areas_append("SOLO", "단일", "alice", repo="solo")
    _write_ledger(board)
    assert board.session_name() is None


def test_session_name_home_row_resolves_registered_home(board, monkeypatch):
    """홈 자신을 가리키는 N=1 행(`slot="."`) → 그 행의 session 으로 해소(단일-lease 층)."""
    _clear_env(monkeypatch)
    board.areas_append("SOLO", "단일", "alice", repo="solo")
    _write_ledger(board, {"slot": ".", "repo": "solo", "session": "solo_1"})
    assert board.session_name() == "solo_1"
    assert board.session_name(required=True) == "solo_1"


def test_session_name_single_lease_layer_resolves(board, monkeypatch):
    """장부에 leased 행이 정확히 1개면 그 값이 세션이다."""
    _clear_env(monkeypatch)
    board.areas_append("SOLO", "단일", "alice", repo="solo")
    _write_ledger(board, {"session": "other_1"})
    assert board.session_name() == "other_1"


def test_session_name_two_registrations_stay_unresolved(board, monkeypatch):
    """등록 repo 2개(장부는 부재/0행) → 미해소."""
    _clear_env(monkeypatch)
    board.areas_append("PAY", "결제", "alice", repo="pay")
    board.areas_append("SHIP", "배송", "bob", repo="ship")
    assert board.session_name() is None


def test_session_name_env_beats_ledger_row(board, monkeypatch):
    """장부 행이 있어도 env 가 있으면 env 승(우선순위 불변)."""
    monkeypatch.setenv("PM_SESSION_NAME", "from-pm-env")
    board.areas_append("SOLO", "단일", "alice", repo="solo")
    _write_ledger(board, {"slot": ".", "repo": "solo", "session": "solo_1"})
    assert board.session_name() == "from-pm-env"


def test_session_name_override_beats_ledger_row(board, monkeypatch):
    """장부 행이 있어도 override 인자가 있으면 override 승(해소 0층 불변)."""
    _clear_env(monkeypatch)
    board.areas_append("SOLO", "단일", "alice", repo="solo")
    _write_ledger(board, {"slot": ".", "repo": "solo", "session": "solo_1"})
    assert board.session_name("explicit") == "explicit"


def test_session_name_required_fail_loud_when_rows_are_ambiguous(board, monkeypatch):
    """등록 repo 1개라도 leased 행이 그 세션을 특정하지 못하면 required=True 는 fail-loud."""
    _clear_env(monkeypatch)
    board.areas_append("SOLO", "단일", "alice", repo="solo")
    _write_ledger(board, {"session": "stale_9", "state": "idle"})
    with pytest.raises(SystemExit):
        board.session_name(required=True)


# ── 손상 장부 board-level 역가드 ─────────────────
# 장부가 **손상**(읽기실패·JSON파손·스키마불일치)이면 행을 못 읽으므로 미해소로 떨어진다 —
# 손상을 "행 0" 으로 접어 이름을 지어내면 실제로 풀 행을 보유했던 홈이 오귀속된다.


def test_session_name_corrupt_json_ledger_stays_unresolved(board, monkeypatch):
    """손상 3형 ① JSON 파손 — required=False 는 None(이름을 지어내지 않는다)."""
    _clear_env(monkeypatch)
    board.areas_append("SOLO", "단일", "alice", repo="solo")
    board.LEASES_FILE.parent.mkdir(parents=True, exist_ok=True)
    board.LEASES_FILE.write_text("{not valid json", encoding="utf-8")
    assert board.session_name() is None


def test_session_name_corrupt_json_ledger_fail_loud_when_required(board, monkeypatch):
    """손상 3형 ① JSON 파손 — required=True 는 fail-loud(SystemExit) — 오귀속 rc0 금지."""
    _clear_env(monkeypatch)
    board.areas_append("SOLO", "단일", "alice", repo="solo")
    board.LEASES_FILE.parent.mkdir(parents=True, exist_ok=True)
    board.LEASES_FILE.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(SystemExit):
        board.session_name(required=True)


def test_session_name_schema_mismatch_ledger_fail_loud_when_required(board, monkeypatch):
    """손상 3형 ② 최상위 스키마 불일치(dict 아님) — required=True fail-loud."""
    _clear_env(monkeypatch)
    board.areas_append("SOLO", "단일", "alice", repo="solo")
    board.LEASES_FILE.parent.mkdir(parents=True, exist_ok=True)
    board.LEASES_FILE.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    with pytest.raises(SystemExit):
        board.session_name(required=True)


def test_session_name_read_failure_ledger_fail_loud_when_required(board, monkeypatch):
    """손상 3형 ③ 읽기 실패(경로가 디렉터리) — required=True fail-loud."""
    _clear_env(monkeypatch)
    board.areas_append("SOLO", "단일", "alice", repo="solo")
    board.LEASES_FILE.parent.mkdir(parents=True, exist_ok=True)
    board.LEASES_FILE.mkdir()
    with pytest.raises(SystemExit):
        board.session_name(required=True)


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
    """명시 --repo/--slot 이면 모호해도 fail-loud 하지 않는다(세션 해소 override 0층)."""
    _clear_env(monkeypatch)
    _write_ledger(board, {"session": "a_1"}, {"session": "b_1"})
    _seed_ticket(board, "T-0001", status="open")
    # 명시 --repo/--slot(kind=slot) → session_name 이 즉시 반환(SystemExit 없음). claim 이 정상
    # 진행돼 rc=0.
    rc = board.cmd_claim(argparse.Namespace(id="T-0001", repo="a", slot=1, user="me"))
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

class _FakeProc:
    """`subprocess.Popen` 반환 대역 — 줄 단위로 읽히는 stdout/stderr + 고정 rc.

    회귀 러너는 두 스트림을 스레드로 tee 하며 읽으므로(실시간 echo + 캡처), 대역도 이터러블
    스트림과 `wait()` 를 갖춘 프로세스 형태여야 한다.
    """

    def __init__(self, rc: int, stdout: str = "", stderr: str = ""):
        self.stdout = io.StringIO(stdout)
        self.stderr = io.StringIO(stderr)
        self._rc = rc

    def wait(self):
        return self._rc


class _FakeRun:
    """board.subprocess.Popen 대역 — 고정 returncode 를 돌려주고 호출을 기록한다.

    pytest 자식을 실기동하지 않고 rc 만 주입한다. `_git_head` 은 별도 mock 하므로 이
    대역은 회귀 pytest 호출만 받는다.
    """

    def __init__(self, rc: int):
        self.rc = rc
        self.calls: list[dict] = []

    def __call__(self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})
        return _FakeProc(self.rc)


@pytest.fixture
def reg_board(board, monkeypatch):
    """회귀 run 을 hermetic 하게 만든 board — 플래그/장부/HEAD 를 tmp·fake 로 격리.

    board 픽스처는 REGRESSION_FLAG·LOCAL_DIR·LEASES_FILE 을 monkeypatch 하지 않아 실 repo 를
    가리킨다 — 여기서 tmp 로 재지정한다. LEASES_FILE 은 부재(→`_active_slot_path` None →
    cwd=REPO 폴백)다. 트리는 **스위트가 있는 코드 트리**가 기본이다(`tests/` 실재) — 회귀 측정
    케이스의 전제다. 스위트 없는 트리를 요구하는 케이스는 `_without_suite(board)` 로 지운다.
    """
    (board._proj / "tests").mkdir(parents=True, exist_ok=True)
    local = board._proj / ".project_manager" / ".local"
    monkeypatch.setattr(board, "LOCAL_DIR", local)
    monkeypatch.setattr(board, "REGRESSION_FLAG", local / "regression.json")
    monkeypatch.setattr(board, "LEASES_FILE", local / "worktree-leases.json")  # 부재
    monkeypatch.setattr(board, "_git_head", lambda: "deadbeef01234567")
    monkeypatch.setattr(board, "_git_head_at", lambda _cwd: "deadbeef01234567")
    return board


def _run_args(**over):
    base = dict(action="run", cmd=None, ticket=None, touches=None)
    base.update(over)
    return argparse.Namespace(**base)


def _without_suite(board) -> None:
    """이 트리를 **스위트 없는 트리**(분리 형상 PM 홈)로 만든다 — `tests/` 를 지운다."""
    shutil.rmtree(board.REPO / "tests", ignore_errors=True)
    assert not (board.REPO / "tests").is_dir()


def test_regression_run_rc5_records_fail(reg_board, monkeypatch, capsys):
    """rc5(수집 0) full run → status='fail' 기록 + rc≠0 반환 (vacuous pass 근절·T-0220).

    이전엔 `rc in (0, 5)` 로 rc5 를 pass 로 삼켰다 — 그 회귀를 복원하면 이 단언이 깨진다.
    """
    monkeypatch.setattr(reg_board.subprocess, "Popen", _FakeRun(5))
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
    monkeypatch.setattr(reg_board.subprocess, "Popen", _FakeRun(0))
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
    monkeypatch.setattr(reg_board.subprocess, "Popen", _FakeRun(1))
    rc = reg_board.cmd_regression(_run_args())
    assert rc == 1
    data = json.loads(reg_board.REGRESSION_FLAG.read_text(encoding="utf-8"))
    assert data["status"] == "fail"
    assert data["rc"] == 1


def test_regression_run_rc5_surfaces_the_missing_suite_hint(
        reg_board, monkeypatch, capsys):
    """rc5 + 그 트리에 tests/ 부재 → **트리 사실**과 처방을 표면화 (T-0220 → T-0733 정정).

    종전 문구는 '활성 slot lease 미매칭 — PM_SESSION_NAME/local.conf 확인'이었다. 회귀 cwd 가
    lease/세션을 해소하던 시절의 진단이라, 슬롯 우회가 삭제된 지금은 정상 바인딩된 세션에게도
    거짓 처방이 된다(리뷰 F-008).
    """
    monkeypatch.setenv("PM_SESSION_NAME", "orch-dev-T0220")
    monkeypatch.setattr(reg_board.subprocess, "Popen", _FakeRun(5))
    _without_suite(reg_board)
    # 스위트 없는 트리라도 이 test_cmd 는 자기 경로를 명시하므로 실행 전 거부에 걸리지 않는다
    # (거부는 '이 트리를 대상으로 삼는' 형상만 — 여기선 rc5 진단 문구가 판정 대상이다).
    (reg_board.REPO / "src").mkdir(parents=True, exist_ok=True)
    reg_board.cmd_regression(_run_args(cmd="pytest src -q"))
    out = capsys.readouterr().out
    assert "`tests/` 가 없다" in out
    assert "--cwd" in out and "--task" in out
    assert "lease" not in out and "PM_SESSION_NAME" not in out   # 거짓 처방 재유입 차단


def test_regression_run_rc5_explicit_cwd_gets_no_tree_hint(
        reg_board, monkeypatch, capsys):
    """rc5 지만 명시 `--cwd`(override)면 채택자가 트리를 확정한 것이라 트리 힌트를 붙이지 않는다.

    수집 0 노트는 그대로(rc5=fail) 나오되, 트리 처방은 명시 경로에선 무의미하다.
    """
    monkeypatch.setenv("PM_SESSION_NAME", "orch-dev-T0220")
    monkeypatch.setattr(reg_board.subprocess, "Popen", _FakeRun(5))
    reg_board.cmd_regression(_run_args(cwd=str(reg_board._proj / "elsewhere")))
    out = capsys.readouterr().out
    assert "수집 0" in out               # rc5=fail 노트는 유지.
    assert "`tests/` 가 없다" not in out  # 명시 트리 → 처방 없음.


def test_regression_run_rc5_scoped_returns_fail(reg_board, monkeypatch, capsys):
    """scoped(touches) rc5 도 fail 반환 (advisory 경로도 rc0 만 pass·T-0220).

    scoped 는 플래그를 안 쓰지만 반환값/메시지는 full 과 같은 판정을 따른다.
    """
    monkeypatch.setattr(reg_board.subprocess, "Popen", _FakeRun(5))
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
    """subprocess.Popen 대역 — cwd 별 returncode 를 돌려주고 호출 cwd 를 기록한다.

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
        return _FakeProc(self.rc_by_cwd.get(cwd, self.default))


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

def test_regression_run_single_lease_uses_shared_flag_and_repo_cwd(
        reg_board, monkeypatch):
    """M1(단일 lease)·무명시 → 단일-슬롯 경로 유지: 공유 REGRESSION_FLAG 기록 + 이 트리(REPO) cwd.

    leased 1개면 all-or-nothing 순회로 안 빠진다(현행 결과 동일). 회귀 cwd 는 리스가 있어도
    push 되는 트리 자신이다 — 슬롯 우회는 삭제됐다(T-0733·`--cwd` 명시만 다른 트리를 지목).
    per-slot 플래그를 만들지 않는다.
    """
    _clear_env(monkeypatch)
    _write_ledger(reg_board, {"session": "solo_1"})   # leased 정확히 1개
    fake = _FakeRun(0)
    monkeypatch.setattr(reg_board.subprocess, "Popen", fake)
    rc = reg_board.cmd_regression(_run_args())
    assert rc == 0
    # 공유 플래그에 기록 (슬롯 순회 아님 — per-slot 플래그 부재).
    assert reg_board.REGRESSION_FLAG.exists()
    assert not (reg_board.LOCAL_DIR / "regression-solo_1.json").exists()
    # cwd = 이 트리(REPO) — 리스가 있어도 슬롯으로 우회하지 않는다.
    assert fake.calls[0]["kwargs"]["cwd"] == str(reg_board.REPO)


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
    monkeypatch.setattr(b.subprocess, "Popen", fake)
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
    monkeypatch.setattr(b.subprocess, "Popen", fake)
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
    monkeypatch.setattr(b.subprocess, "Popen", fake)
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
    monkeypatch.setattr(b.subprocess, "Popen", fake)
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
    monkeypatch.setattr(b.subprocess, "Popen",
                        _FakeRunByCwd({_slot_cwd(b, "B_1"): 1}))
    assert b.cmd_regression(_run_args()) == 1
    capsys.readouterr()
    # 2) 고쳐서 전 슬롯 green 으로 재run → 통과.
    monkeypatch.setattr(b.subprocess, "Popen", _FakeRunByCwd({}, default=0))
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
    monkeypatch.setattr(b.subprocess, "Popen", fake)
    rc = b.cmd_regression(_run_args())
    assert rc == 1
    # 전-슬롯 순회 — A_1·B_1 둘 다 run 대상(둘 다 missing).
    assert set(fake.cwds) == {_slot_cwd(b, "A_1"), _slot_cwd(b, "B_1")}
    assert "B_1" in capsys.readouterr().err


def test_regression_run_explicit_session_narrows_in_multi(
        multi_reg_board, monkeypatch):
    """M2+ 라도 CLI --repo/--slot 명시는 그 슬롯 단일 경로로 좁힌다 (문서화된 의도적 조작만 허용).

    좁히는 대상은 **디스패치**(전-슬롯 순회 → 실행 1회)와 그 슬롯 test_cmd 다. 회귀 cwd 는
    슬롯이 아니라 이 트리(REPO)다 — 슬롯 우회는 삭제됐고 다른 트리는 `--cwd`/`--task` 로 지목한다
    (T-0733).
    """
    b = multi_reg_board
    fake = _FakeRun(0)
    monkeypatch.setattr(b.subprocess, "Popen", fake)
    rc = b.cmd_regression(_run_args(repo="A", slot=1))
    assert rc == 0
    # 단일-슬롯 경로 — 공유 REGRESSION_FLAG·실행 1회(슬롯 순회 아님)·cwd 는 이 트리.
    assert len(fake.calls) == 1
    assert fake.calls[0]["kwargs"]["cwd"] == str(b.REPO)
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


# ════════════════════════════════════════════════════════════════════════
# T-0378 — 명시 `--cwd`(run) 대칭: eager actor 해소가 `--repo` 단독 + 활성 ≥2 에서
# SlotResolutionError 를 오발화하던 것을 soft 해소로 닫는다(livegate cf14d9b 동형·
# pm-release §2 readonly-핀 처방 `regression run --repo <r> --cwd <readonly>` 소진).
# --cwd 핀은 단일 위치라 M>1 순회도 건너뛰고, session 은 슬롯 test_cmd 유도에만 남는다
# (cwd override 최우선) — 명시 슬롯은 그 test_cmd 유지, 모호는 repo/local 폴백.
# ════════════════════════════════════════════════════════════════════════

def _seed_repo_two_slots(board, repo="proj"):
    """repo 하나에 활성 leased 슬롯 2개(<repo>_1·<repo>_2) — resolve_actor_slot 모호 전제.

    작업 슬롯 + readonly(leased) 슬롯이 같은 repo 활성 ≥2 가 되는 릴리즈 형상 재현.
    """
    _seed_lease(board, f"{repo}_1", repo, slot=f"work/{repo}_1")
    _seed_lease(board, f"{repo}_2", repo, slot=f"work/{repo}_2")


def test_actor_override_repo_ambiguous_hard_exits_reproduces_misfire(
        reg_board, monkeypatch):
    """재현 판정: `--repo` 단독 + 활성 ≥2 → `_actor_session_override`(soft=False) 가 SystemExit.

    v1.3.0 릴리즈가 잡은 결함 클래스(livegate cf14d9b)의 regression 판 — eager 해소가 `--cwd`
    핀을 무시하고 `--repo` 단독 actor 특정을 타 SlotResolutionError 로 오발화한다. soft=False
    는 현행 하드 실패라 **결함이 실재함을 박제**하고, soft=True 는 raise 대신 None(핀 존중)임을
    같은 자리에서 못박는다(수정의 핵심 seam).
    """
    _clear_env(monkeypatch)
    _seed_repo_two_slots(reg_board)
    args = argparse.Namespace(repo="proj", slot=None, task=None)
    with pytest.raises(SystemExit):
        reg_board._actor_session_override(args)            # soft=False (현행·오발화 재현)
    assert reg_board._actor_session_override(args, soft=True) is None


def test_regression_run_explicit_cwd_repo_ambiguous_no_misfire(
        reg_board, monkeypatch):
    """수정: 활성 ≥2 + `regression run --repo <r> --cwd <path>` → 오발화 없이 그 cwd 실행.

    eager 해소가 SlotResolutionError 로 죽던 것을 --cwd 핀이 soft 해소로 존중한다 —
    pm-release §2 처방(`--repo <r> --cwd <readonly>`)이 실제로 통과(dead-end 아님).
    """
    _clear_env(monkeypatch)
    _seed_repo_two_slots(reg_board)
    override = str(reg_board._proj / "readonly_slot")
    fake = _FakeRun(0)
    monkeypatch.setattr(reg_board.subprocess, "Popen", fake)
    rc = reg_board.cmd_regression(_run_args(repo="proj", cwd=override))  # 오발화면 SystemExit
    assert rc == 0
    # 단일 subprocess 가 그 cwd(override)에서 — M>1 순회 아님.
    assert len(fake.calls) == 1
    assert fake.calls[0]["kwargs"]["cwd"] == override


def test_regression_run_explicit_cwd_skips_multi_dispatch(
        multi_reg_board, monkeypatch):
    """--cwd 핀은 leased ≥2 라도 M>1 all-or-nothing 순회를 건너뛴다(명시 cwd=단일 위치).

    무명시면 전-슬롯 순회(2 subprocess)지만, --cwd 명시면 그 한 경로에서 단일 실행 +
    공유 REGRESSION_FLAG 기록(per-slot 플래그 안 만듦).
    """
    b = multi_reg_board
    override = str(b._proj / "pinned")
    fake = _FakeRun(0)
    monkeypatch.setattr(b.subprocess, "Popen", fake)
    rc = b.cmd_regression(_run_args(cwd=override))
    assert rc == 0
    assert len(fake.calls) == 1
    assert fake.calls[0]["kwargs"]["cwd"] == override
    assert b.REGRESSION_FLAG.exists()
    assert not (b.LOCAL_DIR / "regression-A_1.json").exists()


def test_regression_run_explicit_slot_cwd_keeps_slot_test_cmd(
        reg_board, monkeypatch):
    """--repo/--slot 명시 + --cwd → 그 슬롯 test_cmd 유지(soft 는 kind=slot 무영향).

    모호(--repo 단독)만 repo/local 폴백을 타고, 명시 슬롯은 슬롯 test_cmd 를 그대로 유도한다
    — 부분 대칭(결정 절 (a)): slot test_cmd 는 pickable 할 때만 유지.
    """
    _clear_env(monkeypatch)
    _seed_repo_two_slots(reg_board)
    # proj_1 슬롯에 test_cmd 바인딩(슬롯별 빌드변형).
    data = json.loads(reg_board.LEASES_FILE.read_text(encoding="utf-8"))
    for r in data["leases"]:
        if r["session"] == "proj_1":
            r["test_cmd"] = "SLOT1_CMD"
    reg_board.LEASES_FILE.write_text(json.dumps(data), encoding="utf-8")
    override = str(reg_board._proj / "pinned")
    fake = _FakeRun(0)
    monkeypatch.setattr(reg_board.subprocess, "Popen", fake)
    rc = reg_board.cmd_regression(_run_args(repo="proj", slot=1, cwd=override))
    assert rc == 0
    # 명시 슬롯(proj_1)의 test_cmd 가 실렸다(모호 폴백과 달리 pickable).
    assert fake.calls[0]["args"][0].startswith("SLOT1_CMD")
    assert fake.calls[0]["kwargs"]["cwd"] == override


def test_validate_prefix_rejects_leading_underscore(board):
    """codex T-0239 R4: `_foo` 는 소비측 grammar(첫 글자 영숫자·_TICKET_PREFIX_BODY)가 못 읽는
    ID(`T-_foo-001`)를 만들므로 입력측에서 거부 — 발행되면 list/relabel/next-id 가 전부 깨진다."""
    assert board._validate_prefix("_foo") is not None      # 거부.
    assert board._validate_prefix("foo_bar") is None        # 중간 underscore 는 허용.
    assert board._validate_prefix("f") is None               # 1글자 영숫자 허용.


# ════════════════════════════════════════════════════════════════════════
# 세션 뷰 격리 코어 — _ticket_is_mine 단일 predicate + --session/--slot user
# 유도 + multi_user 게이트 (T-0302·ADR-0053).
#
# 불변식: 세션 뷰(`--mine`/`--session`/`--slot`) 멤버십 = (내 claim) ∪ (내 소유 open).
# 타 사용자 미claim open 은 절대 미포함. degrade("전체 open=mine")는 solo(distinct user ≤1)에서만.
# 근원 = 옛 `if my_user is None or not area_owner_in_use: return True`(T1 my_user None·T2
# area_owner 미운영)가 다중사용자 보드서 타 사용자 open 을 노출하던 것 — 이 코어가 근절한다.
# ════════════════════════════════════════════════════════════════════════

# 두 사용자 per-user area — alice(repo alpha·prefix AL)·bob(repo beta·prefix BE).
# area_owner(alpha→alice·beta→bob)는 open 티켓 *소유* 정의(`_ticket_owner`)다 — user-first(ADR-0056)
# 후 querying identity 는 이걸로 유도하지 않고 항상 현재 사용자(local.conf user=)다.
_TWO_USER_AREAS = (
    "# Area Registry\n\n"
    "| repo | prefix | git | test_cmd | owner | base | protected | area_owner |\n"
    "|---|---|---|---|---|---|---|---|\n"
    "| alpha | AL | g:a | pytest -q | reg | develop | main | alice |\n"
    "| beta | BE | g:b | pytest -q | reg | develop | main | bob |\n"
)


def _seed_full(board, tid, status, *, created_by=None, claimed_by=None,
               title="seed"):
    """created_by/claimed_by 를 포함한 티켓을 심는다 (소유 유도·multi_user 신호 검증용)."""
    path = board.TICKETS_DIR / status / f"{tid}-seed.md"
    board.dump_ticket(path, {"id": tid, "title": title, "status": status,
                             "created_by": created_by, "claimed_by": claimed_by,
                             "depends_on": [], "tags": []}, "# seed\n")
    return path


def _mine_ids(board, capsys, **flags):
    """cmd_list 를 돌려 출력에서 ticket ID 목록을 뽑는다 (mine/repo/slot 렌즈·ADR-0057)."""
    args = argparse.Namespace(status=flags.get("status"), tag=flags.get("tag"),
                              mine=flags.get("mine", False),
                              all=flags.get("all", False), task=flags.get("task"),
                              repo=flags.get("repo"), slot=flags.get("slot"))
    rc = board.cmd_list(args)
    assert rc == 0
    ids = []
    for line in capsys.readouterr().out.splitlines():
        line = line.strip()
        if line.startswith("[") and "]" in line:
            ids.append(line.split("]", 1)[1].split()[0])
    return ids


# ── DoD (a): 타 사용자 미claim open 미포함 (--session·--mine) ──────────────────

def test_session_excludes_other_users_unclaimed_open(board, capsys):
    """**증상 직접 해소**: created_by=타인·claimed_by=null·open 이 `--repo/--slot` 결과서 제외.

    user-first (ADR-0056): `--repo/--slot`(ADR-0057) 의 querying identity = **현재 사용자**
    (local.conf user=alice). open 소유는 area_owner(alpha→alice·beta→bob)로 해소 → alice 세션은
    bob 소유 open(T-BE-001) 제외. (옛 T-0198 은 my_user 를 항상 None 으로 둬 area-open 이 전체
    open 으로 새던 것을 근절.)
    """
    board.AREAS_FILE.write_text(_TWO_USER_AREAS, encoding="utf-8")
    _write_conf(board, "identity.user=alice\nsession=alpha_1\n")
    _seed_full(board, "T-AL-001", "open", created_by="alice/alpha_1")   # alice 소유 open
    _seed_full(board, "T-BE-001", "open", created_by="bob/beta_1")      # bob 미claim open
    ids = _mine_ids(board, capsys, repo="alpha", slot=1)
    assert "T-BE-001" not in ids     # 타 사용자 미claim open 유출 차단(ADR-0056)
    assert ids == ["T-AL-001"]


def test_mine_excludes_other_users_unclaimed_open(board, capsys):
    """--mine 도 동일 — alice 의 --mine 은 bob 소유 open 을 제외한다(area_owner strict)."""
    board.AREAS_FILE.write_text(_TWO_USER_AREAS, encoding="utf-8")
    _write_conf(board, "identity.user=alice\nsession=alpha_1\n")
    _seed_full(board, "T-AL-001", "open", created_by="alice/alpha_1")
    _seed_full(board, "T-BE-001", "open", created_by="bob/beta_1")
    ids = _mine_ids(board, capsys, mine=True)
    assert ids == ["T-AL-001"]


# ── DoD (b): 내 claim + 내 소유 area open 포함 ────────────────────────────────

def test_mine_includes_my_claim_and_my_area_open(board, capsys):
    """(내 claim) ∪ (내 소유 open) — 남의 area 를 claim 한 것도, 내 area open 도 포함."""
    board.AREAS_FILE.write_text(_TWO_USER_AREAS, encoding="utf-8")
    _write_conf(board, "identity.user=alice\nsession=alpha_1\n")
    _seed_full(board, "T-AL-001", "open", created_by="alice/alpha_1")       # 내 area open
    _seed_full(board, "T-BE-005", "claimed", claimed_by="alice/alpha_1")    # 내 claim(남 area)
    _seed_full(board, "T-BE-001", "open", created_by="bob/beta_1")          # 타 사용자 open→제외
    ids = _mine_ids(board, capsys, mine=True)
    assert set(ids) == {"T-AL-001", "T-BE-005"}


def test_slot_view_open_is_created_session_not_area_owner(board, capsys):
    """`--repo X --slot N` open = 그 세션 **생성분만**(ADR-0067 생성-세션 스트림) — area_owner 소유
    해소가 아니다.

    옛 동작(T-0302): slot 뷰 open 은 area_owner 로 소유 해소(같은 area 의 bob-생성 open 도 alice
    소유로 포함). ADR-0067: open 은 `created_by` 세션이 alpha_1 인 것만 — 같은 area·같은 소유
    (area_owner=alice)여도 타 슬롯(alpha_2) 생성 open(T-AL-009)은 제외된다(PM 77 누출 fix)."""
    single = (
        "# Area Registry\n\n"
        "| repo | prefix | git | test_cmd | owner | base | protected | area_owner |\n"
        "|---|---|---|---|---|---|---|---|\n"
        "| alpha | AL | g:a | pytest -q | reg | develop | main | alice |\n"
    )
    board.AREAS_FILE.write_text(single, encoding="utf-8")
    _write_conf(board, "identity.user=alice\nsession=alpha_1\n")
    _seed_full(board, "T-AL-001", "open", created_by="alice/alpha_1")   # 그 세션 생성 → 상세
    _seed_full(board, "T-AL-009", "open", created_by="bob/alpha_2")     # 타 슬롯 생성 → 비노출(소유 무관)
    ids = _mine_ids(board, capsys, repo="alpha", slot=1)
    assert set(ids) == {"T-AL-001"}


# ── DoD (c): created_by 2차 폴백 (area_owner 미설정 채택자) ────────────────────

def test_mine_created_by_fallback_excludes_other_no_area_owner(board, capsys):
    """area_owner 미운영(채택자 미마이그) + 다중사용자 → created_by.user 2차 폴백으로 소유 판정.

    현행(버그·T2): area_owner 미운영이면 `not area_owner_in_use → return True` 로 전체 open 유출.
    fix 후: owner=created_by.user 로 bob open 제외(alice 것만)."""
    # areas.md 부재 → area_owner_in_use False → 소유는 created_by.user 로 해소.
    _write_conf(board, "identity.user=alice\nsession=alpha_1\n")
    _seed_full(board, "T-0001", "open", created_by="alice/alpha_1")
    _seed_full(board, "T-0002", "open", created_by="bob/beta_1")
    ids = _mine_ids(board, capsys, mine=True)
    assert ids == ["T-0001"]     # bob 의 open 은 created_by 폴백으로 제외


def test_created_by_fallback_bare_user(board, capsys):
    """`migrate-identity` backfill 은 부재 created_by 를 *슬롯 없는 순수 user* 로 채운다 —
    `_created_by_user` 가 `/` 없는 값을 user 로 읽어 그 소유를 살린다(bare `alice` == my_user)."""
    _write_conf(board, "identity.user=alice\nsession=alpha_1\n")
    _seed_full(board, "T-0001", "open", created_by="alice")          # bare user (backfill 형태)
    _seed_full(board, "T-0002", "open", created_by="bob")            # bare 타 사용자
    ids = _mine_ids(board, capsys, mine=True)
    assert ids == ["T-0001"]


# ── DoD (d): solo(distinct ≤1) all-open degrade 보존 (회귀 0·additive) ─────────

def test_solo_all_open_degrade_preserved(board, capsys):
    """solo(distinct user ≤1) — area_owner 미운영·소유 미해소 open 도 전체 표시(빈 보드 금지)."""
    _write_conf(board, "identity.user=alice\nsession=alpha_1\n")
    _seed_full(board, "T-0001", "open", created_by="alice/alpha_1")   # alice(유일 user)
    _seed_full(board, "T-0002", "open")                              # created_by 부재(소유 미상)
    ids = _mine_ids(board, capsys, mine=True)
    # distinct user = {alice} = 1 → solo → 미해소 open(T-0002) 도 degrade 로 표시.
    assert set(ids) == {"T-0001", "T-0002"}


def test_solo_no_identity_all_open_preserved(board, capsys):
    """무-identity solo(created_by/claimed_by user 0) — `--all` 전체 뷰는 전 활성 티켓 표시(무변경).

    ADR-0066(T-0385): 무인자 기본은 세션 스코프(내 스트림)로 바뀌어 무관 open 을 접으므로, "전체
    표시" 검증은 `--all`(기존 무인자 전체 뷰의 이관)로 돈다. `--mine` 솔로 degrade 는 별도 스위트."""
    _seed_full(board, "T-0001", "open")
    _seed_full(board, "T-0002", "open")
    _seed_full(board, "T-0003", "claimed", claimed_by="alpha_1")   # 슬롯-only(user 차원 없음)
    ids = _mine_ids(board, capsys, all=True)
    assert set(ids) == {"T-0001", "T-0002", "T-0003"}


# ── DoD (e): multi_user 미해소 strict-exclude ─────────────────────────────────

def test_multi_user_unresolved_owner_strict_excludes(board, capsys):
    """다중사용자(distinct ≥2) + 소유 미해소 open(created_by 부재·area_owner 미운영) → strict-exclude.

    현행(버그): area_owner 미운영 → 미해소 open 전체 노출. fix 후: 다중사용자면 미해소 open 제외."""
    _write_conf(board, "identity.user=alice\nsession=alpha_1\n")
    _seed_full(board, "T-0001", "open", created_by="alice/alpha_1")   # 내 소유 open
    _seed_full(board, "T-0002", "claimed", claimed_by="bob/beta_1")   # bob → 2번째 user 신호
    _seed_full(board, "T-0003", "open")                              # 소유 미상 open
    ids = _mine_ids(board, capsys, mine=True)
    assert "T-0003" not in ids     # 다중사용자 + 미해소 → strict-exclude
    assert ids == ["T-0001"]


def test_session_multi_user_no_area_owner_excludes_unowned_open(board, capsys):
    """`--repo/--slot`(ADR-0057) + area_owner 미운영 + 다중사용자 → 소유 미해소 open strict-exclude.

    my_user 를 유도할 areas 가 없어도(→None) multi_user 게이트가 미해소 open 유출을 막는다."""
    _seed_full(board, "T-0001", "open", created_by="alice/alpha_1")
    _seed_full(board, "T-0002", "open", created_by="bob/beta_1")
    ids = _mine_ids(board, capsys, repo="alpha", slot=1)
    assert "T-0002" not in ids     # 타 사용자 미claim open 유출 차단(솔로 degrade 미확장)


# ── 헬퍼 단위테스트 — _distinct_ticket_users·_ticket_owner·_created_by_user ──

def test_distinct_ticket_users_counts_created_and_claimed(board):
    """created_by/claimed_by 의 distinct user 를 센다 — 슬롯-only·미상은 제외."""
    _seed_full(board, "T-0001", "open", created_by="alice/alpha_1")
    _seed_full(board, "T-0002", "claimed", claimed_by="bob/beta_1")
    _seed_full(board, "T-0003", "open", created_by="alpha_2")    # 슬롯-only bare? → user 로 셈? 아래 주석
    # NB: bare `alpha_2` 는 `_created_by_user` 가 user 로 읽는다(backfill bare-user 살리기) →
    #     3번째 distinct. slot-only 는 claimed_by 경로에서만 제외된다.
    assert board._distinct_ticket_users() == 3


def test_distinct_ticket_users_slot_only_claim_excluded(board):
    """claimed_by 슬롯-only(`/` 없음)는 user 미상 → distinct 에 안 든다(솔로 신호 보존)."""
    _seed_full(board, "T-0001", "open", created_by="alice/alpha_1")
    _seed_full(board, "T-0002", "claimed", claimed_by="alpha_1")   # 슬롯-only
    assert board._distinct_ticket_users() == 1


def test_distinct_ticket_users_empty_board_zero(board):
    assert board._distinct_ticket_users() == 0


def test_created_by_user_extraction(board):
    assert board._created_by_user("alice/alpha_1") == "alice"
    assert board._created_by_user("alice") == "alice"          # bare = user(backfill)
    assert board._created_by_user("a/b/slot") == "a/b"          # 마지막 / 분리
    assert board._created_by_user("") is None
    assert board._created_by_user(None) is None


def test_ticket_owner_area_owner_first(board):
    """area_owner 운영 중이면 area_owner 1차 — created_by 와 갈려도 area_owner 승."""
    board.AREAS_FILE.write_text(_TWO_USER_AREAS, encoding="utf-8")
    fm = {"id": "T-AL-001", "created_by": "bob/beta_1"}   # created_by=bob 이지만 AL area=alice
    assert board._ticket_owner(fm, area_owner_in_use=True) == "alice"


def test_ticket_owner_created_by_fallback(board):
    """area_owner 미운영이면 created_by.user 2차 폴백."""
    fm = {"id": "T-0001", "created_by": "carol/x_1"}
    assert board._ticket_owner(fm, area_owner_in_use=False) == "carol"


def test_ticket_owner_none_when_unresolved(board):
    assert board._ticket_owner({"id": "T-0001"}, area_owner_in_use=False) is None


# NB (ADR-0056·T-0312): `_area_owner_from_session`/`_area_owner_for_single_area`(T-0198/T-0302)는
# querying identity 유도 배선이 폐기되며 함께 제거됐다 — area_owner 는 이제 open 소유 정의
# (`_ticket_owner`)로만 존속하고, 필터 뷰 my_user 는 항상 `user_name()`(현재 사용자)이다. 그
# 두 헬퍼의 단위테스트도 함께 삭제(dead-code 가드 없음). user-first 매칭·slot 교집합은 아래
# 세션-격리 스위트 + user-first 스위트가 검증한다.


# ════════════════════════════════════════════════════════════════════════
# user-first 뷰 스코프 — 현재 사용자 ∩ 슬롯 · 타 사용자 무유출 (ADR-0056·T-0312).
#
# 불변식(단일): 필터 뷰 = 현재 사용자(user_name()) 것. --mine = 내 것 전 슬롯 · --session/--slot
# = 내 것 ∩ 그 슬롯(claim: user AND slot·open: 슬롯무관 내 backlog). 타 사용자는 어떤 필터 뷰
# 에도 안 나온다(전체는 무필터 list). area_owner 설정/미설정 양쪽 커버 + solo degrade 회귀 0.
# ════════════════════════════════════════════════════════════════════════

def _seed_userfirst_board(board, *, areas: bool):
    """현재 사용자=alice·multi-user(alice+bob)·multi-slot 합성 보드를 심는다.

    alice: alpha_1 에 open(T-AL-001)+claim(T-AL-002) · alpha_2 에 claim(T-AL-003·타 슬롯).
    bob: beta_1 에 open(T-BE-001)+claim(T-BE-002·타 사용자). `areas`=True 면 area_owner 운영
    (alpha→alice·beta→bob)·False 면 레지스트리 부재(소유는 created_by.user 2차 폴백).
    """
    if areas:
        board.AREAS_FILE.write_text(_TWO_USER_AREAS, encoding="utf-8")
    _write_conf(board, "identity.user=alice\nsession=alpha_1\n")
    _seed_full(board, "T-AL-001", "open", created_by="alice/alpha_1")     # 내 open
    _seed_full(board, "T-AL-002", "claimed", claimed_by="alice/alpha_1")  # 내 claim·alpha_1
    _seed_full(board, "T-AL-003", "claimed", claimed_by="alice/alpha_2")  # 내 claim·alpha_2(타 슬롯)
    _seed_full(board, "T-BE-001", "open", created_by="bob/beta_1")        # 타 사용자 open
    _seed_full(board, "T-BE-002", "claimed", claimed_by="bob/beta_1")     # 타 사용자 claim


_BOB_TICKETS = {"T-BE-001", "T-BE-002"}


@pytest.mark.parametrize("areas", [True, False])
def test_userfirst_no_other_user_leak_in_any_filter_view(board, capsys, areas):
    """타 사용자 무유출 (area_owner 설정/미설정 양쪽): --mine·`--repo --slot`·`--repo`(ADR-0057)
    어디에도 bob 티켓 0."""
    _seed_userfirst_board(board, areas=areas)
    for flags in ({"mine": True}, {"repo": "alpha", "slot": 1}, {"repo": "alpha"}):
        ids = set(_mine_ids(board, capsys, **flags))
        assert not (_BOB_TICKETS & ids), f"{flags} 필터 뷰에 bob 티켓 유출(ADR-0056): {ids}"


def test_userfirst_mine_is_all_my_slots(board, capsys):
    """--mine = 내 것 **전 슬롯** — 내 open + 모든 슬롯의 내 claim(alpha_1·alpha_2). bob 무포함."""
    _seed_userfirst_board(board, areas=True)
    ids = set(_mine_ids(board, capsys, mine=True))
    assert ids == {"T-AL-001", "T-AL-002", "T-AL-003"}


def test_userfirst_session_intersects_slot_excludes_my_other_slot_claim(board, capsys):
    """**S2**: `--repo alpha --slot 1`(ADR-0057) = 내 것 ∩ 그 슬롯 — 내 open(슬롯무관) +
    alpha_1 claim만.

    타 슬롯의 내 claim(T-AL-003·alpha_2)은 `--repo alpha --slot 1` 엔 안 보이고(정상) --mine 엔
    보인다.
    """
    _seed_userfirst_board(board, areas=True)
    ids = set(_mine_ids(board, capsys, repo="alpha", slot=1))
    # 내 open(T-AL-001·슬롯무관 backlog) + 내 alpha_1 claim(T-AL-002). alpha_2 claim 제외.
    assert ids == {"T-AL-001", "T-AL-002"}
    assert "T-AL-003" not in ids                      # 타 슬롯의 내 claim → slot 뷰서 제외
    assert "T-AL-003" in set(_mine_ids(board, capsys, mine=True))  # --mine 엔 나옴(전 슬롯)


def test_userfirst_slot_number_intersects(board, capsys):
    """`--repo alpha --slot 2`(ADR-0057) = 내 것 ∩ 슬롯 _2 — 내 alpha_2 claim(T-AL-003)만·
    alpha_1 claim 제외·bob 무포함."""
    _seed_userfirst_board(board, areas=True)
    ids = set(_mine_ids(board, capsys, repo="alpha", slot=2))
    assert "T-AL-003" in ids                          # 내 claim·slot _2
    assert "T-AL-002" not in ids                      # 내 claim·slot _1 → --slot 2 제외
    assert not (_BOB_TICKETS & ids)


def test_userfirst_slot_view_session_stream_and_legacy_claim(board, capsys):
    """`--repo alpha --slot 1` 세션 뷰(ADR-0067): 그 세션 생성 open + 그 세션 claim 만.

    open 은 생성-세션 스트림이라 세션 부재 created_by(T-0001)는 비노출(옛 solo all-open degrade 는
    세션 뷰에 적용 안 됨). claim 은 legacy 슬롯-only(alpha_1·solo=not multi_user)도 exact 매칭이면
    포함·타 슬롯(alpha_2)은 제외."""
    _seed_full(board, "T-0001", "open")                              # created_by 부재 → 비노출(backfill 대상)
    _seed_full(board, "T-0002", "claimed", claimed_by="alpha_1")     # 슬롯-only·내 슬롯 → 상세
    _seed_full(board, "T-0003", "claimed", claimed_by="alpha_2")     # 슬롯-only·타 슬롯 → 제외
    ids = set(_mine_ids(board, capsys, repo="alpha", slot=1))
    assert ids == {"T-0002"}                          # 세션 생성 open 0 + 내 슬롯 legacy claim


def test_userfirst_slot_view_requires_user_and_session(board, capsys):
    """`--repo repo --slot 1` 세션 뷰는 user ∧ session 복합축이다.

    alice의 같은 세션 claim만 보이고 bob의 동명 세션 claim과 multi-user에서 모호한 legacy
    슬롯-only claim은 strict-exclude한다.
    """
    _write_conf(board, "identity.user=alice\n")
    _seed_full(board, "T-0001", "claimed", claimed_by="alice/repo_1")   # user-qualified·세션 repo_1
    _seed_full(board, "T-0002", "claimed", claimed_by="bob/repo_1")     # 타 user·같은 세션 → 제외
    _seed_full(board, "T-0003", "claimed", claimed_by="repo_1")         # legacy·multi-user → strict 제외
    _seed_full(board, "T-0004", "claimed", claimed_by="alice/repo_2")   # 타 세션(repo_2) → 제외
    ids = set(_mine_ids(board, capsys, repo="repo", slot=1))
    assert ids == {"T-0001"}


# ════════════════════════════════════════════════════════════════════════
# ADR-0053 #4 — anti-degrade loud-warn (cmd_list · T-0307).
#
# 다중사용자 보드에서 세션 격리가 조용히 티켓을 드롭(strict-exclude)하거나 정체성이 미해소면
# 목록 출력 *전* stderr 로 loud-warn 1줄(remedy 포함). solo(distinct user ≤1)는 무경고. stderr
# 라 stdout 목록 포맷은 무오염(회귀 파서·pm_bootstrap counts 무영향). "빈 warn spam 금지" —
# 소유 해소된 타 사용자 티켓만 제외되는 clean strict 는 경고하지 않는다.
# ════════════════════════════════════════════════════════════════════════

_WARN_MARK = "세션격리"          # loud-warn 식별 토큰(stderr 전용)
_REMEDY_INIT = "board init --owner"
_REMEDY_MIGRATE = "migrate-identity"
# T-0382: loud-warn 은 remedy 를 *실행 가능한 실값*으로 명시해야 한다(사용자가 커맨드를 기억할
# 필요 0·[[mechanize-dont-instruct-llm]]) — 정확한 backfill 커맨드 + 단일-세션 op 전제.
_REMEDY_MIGRATE_EXACT = "python3 .project_manager/tools/board.py migrate-identity --dry-run"
_REMEDY_SINGLE_SESSION = "단일-세션 op"


def _list_args(**flags):
    return argparse.Namespace(status=flags.get("status"), tag=flags.get("tag"),
                              mine=flags.get("mine", False),
                              all=flags.get("all", False), task=flags.get("task"),
                              repo=flags.get("repo"), slot=flags.get("slot"))


def _ids_from(out: str) -> list[str]:
    """cmd_list stdout 에서 ticket ID 만 추출 (`  [status] T-XXXX  …` 행)."""
    ids = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("[") and "]" in line:
            ids.append(line.split("]", 1)[1].split()[0])
    return ids


def test_cmd_list_loud_warn_on_strict_exclude(board, capsys, monkeypatch):
    """다중사용자 + 소유 미해소 open strict-exclude 발동 → stderr loud-warn 1줄(remedy 포함).

    my_user(alice) 는 해소됐지만 소유 미상 open(T-0003)이 다중사용자라서 drop 됐다 →
    실 drop 신호를 잡아 경고. stdout 목록은 무오염(T-0001 만·경고 문자열 부재).
    세션은 env 로 바인딩한다 — conf `session=` 폴백은 폐지됐고(T-0779), 미바인딩이면
    "(비바인딩)" 안내가 한 줄 더 붙어 이 시나리오(바인딩 세션)가 아니게 된다."""
    monkeypatch.setenv("PM_SESSION_NAME", "alpha_1")
    _write_conf(board, "identity.user=alice\n")
    _seed_full(board, "T-0001", "open", created_by="alice/alpha_1")   # 내 소유 open
    _seed_full(board, "T-0002", "claimed", claimed_by="bob/beta_1")   # bob → 2번째 user 신호
    _seed_full(board, "T-0003", "open")                              # 소유 미상 → strict-exclude
    rc = board.cmd_list(_list_args(mine=True))
    assert rc == 0
    cap = capsys.readouterr()
    # stderr: loud-warn + 두 remedy.
    assert _WARN_MARK in cap.err
    assert _REMEDY_INIT in cap.err
    assert _REMEDY_MIGRATE in cap.err
    assert cap.err.count("\n") == 1          # 정확히 1줄(spam 금지)
    # stdout: 목록 무오염 — 경고 문자열 부재·strict-exclude 로 T-0003 drop·내 소유만.
    assert _WARN_MARK not in cap.out
    assert _ids_from(cap.out) == ["T-0001"]


def test_cmd_list_loud_warn_on_identity_unresolved(board, capsys):
    """다중사용자 + 정체성 미해소(my_user None) → loud-warn. stdout(no tickets) 무오염.

    `--mine` user-단위 렌즈에서 areas 부재로 소유자 유도 실패(None) + 티켓
    created_by 2명(다중사용자) → 모든 open 이 strict-exclude. 미해소 정체성을 remedy 와 함께 경고.
    세션 뷰도 같은 종류의 실 드롭을 별도 predicate 재평가로 경고한다."""
    _seed_full(board, "T-0001", "open", created_by="alice/alpha_1")
    _seed_full(board, "T-0002", "open", created_by="bob/beta_1")
    rc = board.cmd_list(_list_args(mine=True))
    assert rc == 0
    cap = capsys.readouterr()
    assert _WARN_MARK in cap.err
    assert _REMEDY_INIT in cap.err
    # stdout: 전부 strict-exclude → (no tickets)·경고 문자열 부재.
    assert _WARN_MARK not in cap.out
    assert cap.out.strip() == "(no tickets)"


def test_cmd_list_solo_no_warn(board, capsys):
    """solo(distinct user ≤1) — 소유 미해소 open 도 degrade 로 포함·무경고(회귀 0)."""
    _write_conf(board, "identity.user=alice\nsession=alpha_1\n")
    _seed_full(board, "T-0001", "open", created_by="alice/alpha_1")   # 유일 user
    _seed_full(board, "T-0002", "open")                              # 소유 미상(degrade 포함)
    rc = board.cmd_list(_list_args(mine=True))
    assert rc == 0
    cap = capsys.readouterr()
    assert _WARN_MARK not in cap.err                 # solo → 무경고
    assert set(_ids_from(cap.out)) == {"T-0001", "T-0002"}


def test_cmd_list_clean_strict_no_warn(board, capsys):
    """다중사용자여도 *소유 해소된* 타 사용자 티켓만 제외되는 clean strict 는 무경고(빈 warn spam 금지).

    alice 의 --mine 은 bob 소유 open 을 제외하지만, 그 제외는 solo 에서도 제외될 소유-해소
    티켓이라 strict-exclude 신호가 아니다. my_user 도 해소됨 → 경고 없음."""
    board.AREAS_FILE.write_text(_TWO_USER_AREAS, encoding="utf-8")
    _write_conf(board, "identity.user=alice\nsession=alpha_1\n")
    _seed_full(board, "T-AL-001", "open", created_by="alice/alpha_1")  # 내 소유
    _seed_full(board, "T-BE-001", "open", created_by="bob/beta_1")     # bob 소유(해소됨)
    rc = board.cmd_list(_list_args(mine=True))
    assert rc == 0
    cap = capsys.readouterr()
    assert _WARN_MARK not in cap.err                 # clean strict → 무경고
    assert _ids_from(cap.out) == ["T-AL-001"]


def test_cmd_list_all_no_warn(board, capsys):
    """`--all` 전체 뷰(mine=False) — 격리 미적용이라 다중사용자여도 무경고·전체 표시(ADR-0066 이관).

    무인자 기본 뷰(default_view)는 세션 스코프라 이 검증은 `--all`(기존 무인자 전체 뷰)로 돈다."""
    _write_conf(board, "identity.user=alice\nsession=alpha_1\n")
    _seed_full(board, "T-0001", "open", created_by="alice/alpha_1")
    _seed_full(board, "T-0002", "open", created_by="bob/beta_1")
    rc = board.cmd_list(_list_args(all=True))
    assert rc == 0
    cap = capsys.readouterr()
    assert _WARN_MARK not in cap.err
    assert set(_ids_from(cap.out)) == {"T-0001", "T-0002"}   # 전체(필터 없음)


def test_default_view_warns_when_legacy_created_by_mimics_a_user(board, capsys):
    """legacy 세션-only created_by 때문에 다중사용자로 오판해 숨긴 open을 경고한다."""
    _write_conf(board, "identity.user=alice\nsession=alpha_1\n")
    _seed_full(board, "T-0001", "open", created_by="alpha_1")
    # 비활성 티켓의 실제 user와 legacy 세션 토큰이 서로 다른 user처럼 집계된다.
    _seed_full(board, "T-0002", "done", created_by="alice/other_1")

    rc = board.cmd_list(_list_args())
    assert rc == 0
    cap = capsys.readouterr()
    assert cap.out.strip() == "(no tickets)"
    assert _WARN_MARK in cap.err
    assert _REMEDY_MIGRATE_EXACT in cap.err

    rc = board.cmd_list(_list_args(all=True))
    assert rc == 0
    assert _ids_from(capsys.readouterr().out) == ["T-0001"]


def test_default_view_warns_when_solo_git_email_changed(board, capsys):
    """한 사용자의 과거·현재 email 스탬프가 공존해 현재 세션 open을 숨기면 경고한다."""
    _write_conf(board, "identity.user=new@example.com\nsession=alpha_1\n")
    _seed_full(
        board, "T-0001", "open", created_by="old@example.com/alpha_1")
    # 상태 기본값에서는 접히지만 distinct-user 판정에는 참여하는 현재 email 스탬프.
    _seed_full(
        board, "T-0002", "done", created_by="new@example.com/alpha_1")

    rc = board.cmd_list(_list_args())
    assert rc == 0
    cap = capsys.readouterr()
    assert cap.out.strip() == "(no tickets)"
    assert _WARN_MARK in cap.err
    assert "email" in cap.err

    rc = board.cmd_list(_list_args(all=True))
    assert rc == 0
    assert _ids_from(capsys.readouterr().out) == ["T-0001"]


# ── T-0382: solo email-변경 오판 재현 + strict-exclude 경보 remedy 기계화 (D18 L2 폐쇄) ──
#
# 솔로 사용자가 `git config user.email` 을 바꾸면(old@x→new@x) 옛 티켓 created_by=old@x 와 새
# 티켓 created_by=new@x 가 공존 → `_distinct_ticket_users`=2 → `multi_user=True` **오판** →
# strict-exclude 가 옛 open 을 `--mine` 뷰에서 드롭한다. 판정 휴리스틱 추가(정체성=데이터 밖
# 사실이라 solo/진짜-2인 구분 불가)는 기각(결정 절)하고, 발동 순간의 경보에 복구 커맨드를 기계
# 명시해 실사용자가 즉시 정합을 회복하게 한다. 아래는 그 현행 동작 명세화 + remedy 실값 lock.


def test_cmd_list_email_change_solo_misjudged_multi_user(board, capsys, monkeypatch):
    """재현: email-변경 solo 가 2인으로 오판돼 옛 open 이 --mine 서 드롭 + loud-warn 발화(remedy 실값).

    한 사람이 email 을 old@example.com → new@example.com 로 바꾼 상태 —
      - T-0001: 옛 email 로 스탬프된 open(created_by=old) → owner=old 해소·new 와 불일치라 드롭.
      - T-0002: 정체성 스탬프 이전 legacy open(created_by 부재) → solo 라면 all-open degrade 로
        보이던 것이, distinct=2 오판(multi_user)으로 strict-exclude 되어 **조용히** 드롭 → warn 트리거.
      - T-0003: 새 email 로 claim(claimed_by=new) → 내 것.
    distinct ticket-user = {old@example.com, new@example.com} = 2 → multi_user True(오판). --mine 은
    T-0003 만 남기고 옛 open 두 개를 드롭하며, 미해소 드롭을 잡아 loud-warn 을 낸다. 경보에는 backfill
    remedy 실값 + 단일-세션 op 전제가 실려야 한다. 세션은 env 로 바인딩한다(conf `session=`
    폴백 폐지·T-0779)."""
    monkeypatch.setenv("PM_SESSION_NAME", "alpha_1")
    _write_conf(board, "identity.user=new@example.com\n")
    _seed_full(board, "T-0001", "open", created_by="old@example.com/alpha_1")     # 옛 email open
    _seed_full(board, "T-0002", "open")                                          # legacy(정체성 전) open
    _seed_full(board, "T-0003", "claimed", claimed_by="new@example.com/alpha_1")  # 새 email claim
    rc = board.cmd_list(_list_args(mine=True))
    assert rc == 0
    cap = capsys.readouterr()
    # multi_user 오판 → 옛 open 두 개 드롭·새 claim 만 남음(현행 동작).
    assert _ids_from(cap.out) == ["T-0003"]
    assert "T-0001" not in cap.out and "T-0002" not in cap.out
    # loud-warn 발화 + remedy 기계 명시(정확한 커맨드·단일-세션 op·email 변경 사유 후보).
    assert _WARN_MARK in cap.err
    assert _REMEDY_MIGRATE_EXACT in cap.err
    assert _REMEDY_SINGLE_SESSION in cap.err
    assert "email" in cap.err
    assert cap.err.count("\n") == 1              # stderr 1줄(spam 금지·stdout 무오염)
    assert _WARN_MARK not in cap.out


def test_cmd_list_loud_warn_carries_exact_migrate_remedy(board, capsys):
    """loud-warn 문구에 migrate-identity backfill remedy **실값 커맨드**가 실린다(단언·회귀 lock).

    발동 조건(다중사용자 + 소유 미해소 open strict-exclude)에서, 사용자가 커맨드를 기억할 필요
    없이 즉시 붙여넣을 수 있는 정확한 backfill 명령 + 단일-세션 op 전제를 경보가 담는지 못박는다."""
    _write_conf(board, "identity.user=alice\nsession=alpha_1\n")
    _seed_full(board, "T-0001", "open", created_by="alice/alpha_1")
    _seed_full(board, "T-0002", "claimed", claimed_by="bob/beta_1")   # 2번째 user 신호
    _seed_full(board, "T-0003", "open")                              # 소유 미상 → strict-exclude
    rc = board.cmd_list(_list_args(mine=True))
    assert rc == 0
    err = capsys.readouterr().err
    assert _WARN_MARK in err
    assert _REMEDY_MIGRATE_EXACT in err          # 정확한 실값 커맨드
    assert _REMEDY_SINGLE_SESSION in err         # 단일-세션 op 전제(경보 문구 유지)
    assert _REMEDY_INIT in err                   # 진짜 다중사용자 경로도 병기


def test_cmd_list_genuine_multiuser_strict_exclude_unchanged(board, capsys):
    """판정 로직 무변경 회귀: 진짜 2인(서로 다른 사용자) 보드에서 strict-exclude 정상 발동 유지.

    remedy 문구 기계화(T-0382)는 경보 *surface* 만 손댄다 — `_distinct_ticket_users`·multi_user
    게이트·`_ticket_is_mine` 로직은 그대로다. 진짜 다중사용자면 타 사용자 소유 open 은 여전히
    --mine 서 제외되고, 미해소 드롭이 있으면 경보가 난다."""
    _write_conf(board, "identity.user=alice\nsession=alpha_1\n")
    _seed_full(board, "T-0001", "open", created_by="alice/alpha_1")   # 내 소유
    _seed_full(board, "T-0002", "open", created_by="bob/beta_1")      # bob 소유(해소됨) → strict 제외
    _seed_full(board, "T-0003", "open")                              # 소유 미상 → strict-exclude
    rc = board.cmd_list(_list_args(mine=True))
    assert rc == 0
    cap = capsys.readouterr()
    assert _ids_from(cap.out) == ["T-0001"]      # 내 소유만(bob·미상 제외)
    assert _WARN_MARK in cap.err                 # 미해소 드롭 → 경보 발화
