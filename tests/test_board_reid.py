"""board.py `reid <OLD-ID> <NEW-ID>` 단위테스트 (T-0259·ADR-0042 관리도구 per-ticket 확장).

단일 티켓의 ID 재부여(번호·prefix 변경·역방향)를 CLI 레벨로 검증한다 — 파일명·frontmatter·
**전 참조**(depends_on/blocks·본문 wikilink·slug 파일명) 무손실 relabel. reid 는 prefix
rename/merge(T-0239)와 *같은 파이프라인*(`_prefix_relabel`)을 재사용하므로(새 rewrite 엔진 없음)
여기선 reid 고유의 인터페이스·가드(NEW-ID sanity·collision·타세션 claim·번호/prefix 변경·멱등)에
집중한다. 토큰단위 rewriter 경계 규칙(T-0238)은 대표 케이스만 재확인한다(prefix 테스트가 커버).

**hermetic 필수**: board.py 경로 전역(REPO·TICKETS_DIR·… )을 tmp 프로젝트로 monkeypatch 재지정한다
(test_board_prefix_cli 패턴 동류). 홈 git 가드는 기본 clean(`""`)로 monkeypatch 해 환경 git 유무와
무관하게 결정적이다 — 가드 자체의 dirty-abort 는 전용 테스트가 override 로 검증한다.
"""
from __future__ import annotations

import argparse
import contextlib
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
    (root / ".project_manager" / "wiki" / "log").mkdir(parents=True, exist_ok=True)
    (root / ".project_manager" / "wiki" / "decisions").mkdir(parents=True, exist_ok=True)


@pytest.fixture
def board(tmp_path, monkeypatch):
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
        "LOCAL_DIR": local,
        "BOARD_LOCK": local / "board.lock",
    }
    for name, val in overrides.items():
        monkeypatch.setattr(mod, name, val)
    monkeypatch.delenv("PM_SESSION_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_NAME", raising=False)
    # 홈 git 가드 기본 clean — 환경 git 유무와 무관하게 relabel 이 진행되게(가드 자체는 전용 테스트).
    monkeypatch.setattr(mod, "_home_git_status_porcelain", lambda: "")
    return mod


# ── 티켓/참조 심기 헬퍼 ────────────────────────────────────────────────────

def _seed_ticket(board, tid: str, *, created: str = "2026-07-01",
                 status: str = "open", body: str = "",
                 claimed_by: str | None = None,
                 depends_on: list[str] | None = None,
                 blocks: list[str] | None = None) -> Path:
    """`{tid}-slug.md` 티켓을 심는다 (claimed_by·depends_on·blocks 지정 가능)."""
    path = board.TICKETS_DIR / status / f"{tid}-slug.md"
    fm: dict = {"id": tid, "title": "t", "status": status, "created": created}
    if claimed_by is not None:
        fm["claimed_by"] = claimed_by
    if depends_on is not None:
        fm["depends_on"] = depends_on
    if blocks is not None:
        fm["blocks"] = blocks
    board.dump_ticket(path, fm, f"# {tid} — t\n{body}\n")
    return path


def _seed_wiki(board, name: str, text: str) -> Path:
    p = board.REPO / ".project_manager" / "wiki" / "decisions" / name
    p.write_text(text, encoding="utf-8")
    return p


def _ids_on_disk(board) -> set[str]:
    """디스크의 모든 티켓 파일명에서 canonical ID 를 추출한다 (파일 rename 검증용·.drafts 포함)."""
    out = set()
    for status in (*board.STATUS_DIRS, ".drafts"):
        for p in (board.TICKETS_DIR / status).glob("T-*.md"):
            tid = board._ticket_id_from_filename(p.name)
            if tid:
                out.add(tid)
    return out


def _ns(old_id: str, new_id: str, *, dry_run: bool = False,
        repo: str | None = None, slot: int | None = None,
        user_ack: str | None = None) -> argparse.Namespace:
    return argparse.Namespace(
        old_id=old_id, new_id=new_id, dry_run=dry_run, repo=repo, slot=slot,
        user_ack=user_ack,
    )


# ════════════════════════════════════════════════════════════════════════
# NEW-ID 형식 sanity — `_is_valid_ticket_id` (발행 문법·prefix 자유 입력)
# ════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("tid", [
    "T-0036", "T-0250", "T-1", "T-PAY-001", "T-finance-036", "T-service-a-001",
    "T-123-001", "T-x_y-009",
])
def test_is_valid_ticket_id_accepts_issue_grammar(board, tid):
    assert board._is_valid_ticket_id(tid)


@pytest.mark.parametrize("tid", [
    "", "foo", "T-", "T-PAY", "PAY-1", "T-0036-slug", "0036", "T-PAY-", "tT-0001",
    # codex R3: `$` 는 trailing newline 앞에서도 매치 → 개행-포함 ID 가 파일명/frontmatter/참조에
    # 깨진 채 기록될 위험. `\A…\Z` 앵커가 문자열 끝에서만 종료하도록 거른다.
    "T-0250\n", "T-0250\r\n", "T-finance-036\n", "\nT-0250", "T-025\n0",
])
def test_is_valid_ticket_id_rejects_non_ids(board, tid):
    assert not board._is_valid_ticket_id(tid)


# ════════════════════════════════════════════════════════════════════════
# 번호/prefix 변경 — 파일명·frontmatter relabel
# ════════════════════════════════════════════════════════════════════════

def test_reid_number_change_renames_and_updates_frontmatter(board):
    _seed_ticket(board, "T-0036")
    rc = board.cmd_reid(_ns("T-0036", "T-0250"))
    assert rc == 0
    assert _ids_on_disk(board) == {"T-0250"}
    renamed = board.TICKETS_DIR / "open" / "T-0250-slug.md"
    fm, _ = board.load_ticket(renamed)
    assert fm["id"] == "T-0250"


def test_reid_adds_prefix(board):
    _seed_ticket(board, "T-0036")
    rc = board.cmd_reid(_ns("T-0036", "T-finance-036", user_ack="finance"))
    assert rc == 0
    assert _ids_on_disk(board) == {"T-finance-036"}


def test_reid_changes_prefix(board):
    _seed_ticket(board, "T-PAY-001")
    rc = board.cmd_reid(_ns("T-PAY-001", "T-ACC-001", user_ack="ACC"))
    assert rc == 0
    assert _ids_on_disk(board) == {"T-ACC-001"}


def test_reid_strips_prefix_reverse(board):
    """역방향 — prefix 제거(T-finance-036 → T-0036)."""
    _seed_ticket(board, "T-finance-036")
    rc = board.cmd_reid(_ns("T-finance-036", "T-0036"))
    assert rc == 0
    assert _ids_on_disk(board) == {"T-0036"}


def test_reid_accepts_approved_uppercase_prefix(board):
    """reid 소비 문법은 대문자 legacy prefix를 받고, 신설은 값-결속 승인을 요구한다."""
    _seed_ticket(board, "T-0036")
    rc = board.cmd_reid(_ns("T-0036", "T-PAY-036", user_ack="PAY"))
    assert rc == 0
    assert _ids_on_disk(board) == {"T-PAY-036"}


def test_reid_existing_prefix_fold_match_is_frictionless_and_canonical(board):
    """기존 `PAY`에 `pay`로 reid하면 ack 없이 canonical case로 재사용한다."""
    _seed_ticket(board, "T-PAY-002")
    _seed_ticket(board, "T-0036")
    rc = board.cmd_reid(_ns("T-0036", "T-pay-003"))
    assert rc == 0
    assert _ids_on_disk(board) == {"T-PAY-002", "T-PAY-003"}


def test_reid_rejects_new_hyphen_prefix_even_with_user_ack(board, capsys):
    _seed_ticket(board, "T-0036")

    rc = board.cmd_reid(_ns(
        "T-0036", "T-new-label-001", user_ack="new-label",
    ))

    assert rc == 1
    assert _ids_on_disk(board) == {"T-0036"}
    assert "신규 prefix 'new-label' 형식 위반" in capsys.readouterr().err


def test_reid_reuses_existing_legacy_hyphen_prefix(board):
    _seed_ticket(board, "T-new-label-002")
    _seed_ticket(board, "T-0036")

    rc = board.cmd_reid(_ns("T-0036", "T-new-label-001"))

    assert rc == 0
    assert _ids_on_disk(board) == {"T-new-label-001", "T-new-label-002"}


def test_reid_preserves_slug_in_filename(board):
    path = board.TICKETS_DIR / "open" / "T-0036-my-detailed-slug.md"
    board.dump_ticket(path, {"id": "T-0036", "title": "t", "status": "open"},
                      "# T-0036 — t\n")
    rc = board.cmd_reid(_ns("T-0036", "T-0250"))
    assert rc == 0
    assert (board.TICKETS_DIR / "open" / "T-0250-my-detailed-slug.md").exists()
    assert not path.exists()


def test_reid_numeric_ending_slug_preserves_slug_on_rename(board):
    """codex R2 MF-2: 숫자로 끝나는 legacy slug(`T-0036-fix-123.md`·fm id `T-0036`) reid 시
    파일명 rename 정확(slug `fix-123` 보존)·frontmatter/본문/wiki 참조 rewrite 정합.

    파일명-only 파서는 `T-0036-fix-123` 을 prefixed ID 로 오인해 rename 을 누락(파일명↔content
    불일치) → frontmatter `id:` 1차 진실(`_canonical_ticket_id`)로 해소. 이 fix 전엔 파일명이
    `T-0036-fix-123.md` 로 남아 renamed.exists() 가 False(sensitivity)."""
    path = board.TICKETS_DIR / "open" / "T-0036-fix-123.md"     # legacy T-0036 + 숫자-끝 slug.
    board.dump_ticket(path, {"id": "T-0036", "title": "t", "status": "open"},
                      "# T-0036 — t\nrefs [[T-0036]]\n")
    wiki = _seed_wiki(board, "0001-x.md", "see [[T-0036]]\n")
    rc = board.cmd_reid(_ns("T-0036", "T-0250"))
    assert rc == 0
    renamed = board.TICKETS_DIR / "open" / "T-0250-fix-123.md"
    assert renamed.exists() and not path.exists()              # 파일명 rename·slug 보존.
    fm, body = board.load_ticket(renamed)
    assert fm["id"] == "T-0250"
    assert "[[T-0250]]" in body and "T-0036" not in body       # 본문 참조 rewrite.
    assert wiki.read_text(encoding="utf-8") == "see [[T-0250]]\n"


def test_reid_ticket_in_done_status(board):
    _seed_ticket(board, "T-0036", status="done")
    rc = board.cmd_reid(_ns("T-0036", "T-0250"))
    assert rc == 0
    assert (board.TICKETS_DIR / "done" / "T-0250-slug.md").exists()


def test_reid_ticket_in_drafts(board):
    (board.TICKETS_DIR / ".drafts").mkdir(exist_ok=True)
    draft = _seed_ticket(board, "T-0036", status=".drafts")
    rc = board.cmd_reid(_ns("T-0036", "T-0250"))
    assert rc == 0
    assert (board.TICKETS_DIR / ".drafts" / "T-0250-slug.md").exists()
    assert not draft.exists()


# ════════════════════════════════════════════════════════════════════════
# 참조 rewrite 정확성 — depends_on/blocks·wikilink·경계
# ════════════════════════════════════════════════════════════════════════

def test_reid_rewrites_depends_on_and_blocks_in_other_ticket(board):
    _seed_ticket(board, "T-0036")
    other = _seed_ticket(board, "T-0040", depends_on=["T-0036"], blocks=["T-0036"])
    rc = board.cmd_reid(_ns("T-0036", "T-0250"))
    assert rc == 0
    fm, _ = board.load_ticket(other)
    assert fm["depends_on"] == ["T-0250"]
    assert fm["blocks"] == ["T-0250"]


def test_reid_rewrites_wikilink_and_inline_in_wiki(board):
    _seed_ticket(board, "T-0036")
    wiki = _seed_wiki(board, "0001-x.md", "see [[T-0036]] and bare T-0036 here\n")
    rc = board.cmd_reid(_ns("T-0036", "T-0250"))
    assert rc == 0
    assert wiki.read_text(encoding="utf-8") == "see [[T-0250]] and bare T-0250 here\n"


def test_reid_rewrites_reference_in_body_of_other_ticket(board):
    _seed_ticket(board, "T-0036")
    other = _seed_ticket(board, "T-0041", body="이 작업은 [[T-0036]] 뒤에 온다·(T-0036) 참고")
    rc = board.cmd_reid(_ns("T-0036", "T-0250"))
    assert rc == 0
    _, body = board.load_ticket(other)
    assert "[[T-0250]]" in body and "(T-0250)" in body
    assert "T-0036" not in body


def test_reid_boundary_no_false_substitution(board):
    """경계 오치환 0 — T-0036 relabel 이 T-00361·T-0036abc·fooT-0036·T-0036-2·T-0036_x 를 안 건드림."""
    _seed_ticket(board, "T-0036")
    wiki = _seed_wiki(
        board, "0002-boundary.md",
        "hit [[T-0036]] T-0036 ; skip T-00361 T-0036abc fooT-0036 T-0036-2 T-0036_x\n")
    rc = board.cmd_reid(_ns("T-0036", "T-0250"))
    assert rc == 0
    assert wiki.read_text(encoding="utf-8") == (
        "hit [[T-0250]] T-0250 ; skip T-00361 T-0036abc fooT-0036 T-0036-2 T-0036_x\n")


# ════════════════════════════════════════════════════════════════════════
# 정적 가드 — src≠dst · NEW-ID 형식
# ════════════════════════════════════════════════════════════════════════

def test_reid_same_old_new_rejected(board, capsys):
    _seed_ticket(board, "T-0036")
    rc = board.cmd_reid(_ns("T-0036", "T-0036"))
    assert rc == 1
    assert "같다" in capsys.readouterr().err
    assert _ids_on_disk(board) == {"T-0036"}


def test_reid_invalid_new_id_format_rejected(board, capsys):
    _seed_ticket(board, "T-0036")
    rc = board.cmd_reid(_ns("T-0036", "not-an-id"))
    assert rc == 1
    assert "형식 위반" in capsys.readouterr().err
    assert _ids_on_disk(board) == {"T-0036"}  # 무변경


def test_reid_invalid_new_id_checked_before_old_lookup(board, capsys):
    """형식 sanity 는 값싼 정적 거부라 OLD 실재 여부와 무관하게 먼저 rc1 (부작용 0)."""
    rc = board.cmd_reid(_ns("T-9999", "bad"))
    assert rc == 1
    assert "형식 위반" in capsys.readouterr().err


@pytest.mark.parametrize(
    "malformed_old",
    ["T-*", "T-00?6", "T-[0-9]*", "T-0036-*", "not-an-id", "T-"],
)
def test_reid_invalid_old_id_format_rejected(board, capsys, malformed_old):
    """OLD-ID 도 형식 선검증 (codex T-0259 must-fix).

    find_ticket 은 glob(f"{id}-*.md") 기반 — 메타문자 든 OLD 가 임의 티켓에 매치돼도
    rewrite 는 리터럴 키라 no-op 인데 rc0 성공처럼 끝나는 silent-noop 을 원천 차단한다.
    """
    _seed_ticket(board, "T-0036")
    rc = board.cmd_reid(_ns(malformed_old, "T-0250"))
    assert rc == 1
    assert "OLD-ID" in capsys.readouterr().err
    assert _ids_on_disk(board) == {"T-0036"}  # 무변경·무 rename


def test_reid_glob_old_id_does_not_silently_match_existing(board, capsys):
    """`T-*` 가 실재 T-0036 에 glob-매치해 성공처럼 끝나는 경로가 없어야 한다 (sensitivity)."""
    _seed_ticket(board, "T-0036")
    rc = board.cmd_reid(_ns("T-*", "T-0250"))
    assert rc == 1
    ids = _ids_on_disk(board)
    assert "T-0036" in ids and "T-0250" not in ids


def test_reid_glob_matches_numeric_prefix_ticket_but_canonical_mismatch_aborts(board, capsys):
    """codex R2 MF-1: 유효 canonical OLD(`T-0036`)라도 glob 오매치는 canonical 검증이 차단.

    legacy `T-0036` 은 부재하지만 숫자-prefix 티켓 `T-0036-001`(파일 `T-0036-001-slug.md`)이
    `find_ticket("T-0036")` 의 `T-0036-*.md` glob 에 걸린다 — frontmatter 우선 canonical ID 가
    old_id 와 정확히 같지 않으면 실제 대상이 아니므로 rc2 abort·무변경(리터럴-키 rewrite silent-noop
    방지). 형식 가드(`_is_valid_ticket_id`)는 `T-0036` 을 통과시키므로 이 검증이 유일한 방벽이다."""
    _seed_ticket(board, "T-0036-001")   # 파일 T-0036-001-slug.md — T-0036-*.md glob 에 걸림.
    rc = board.cmd_reid(_ns("T-0036", "T-0250"))
    assert rc == 2
    assert "찾을 수 없다" in capsys.readouterr().err
    assert _ids_on_disk(board) == {"T-0036-001"}   # 무변경 (T-0036-001 은 그대로).


def test_reid_legacy_and_numeric_prefix_coexist_targets_exact_canonical(board):
    """codex R4 (false-negative): legacy `T-0036` 과 숫자-prefix `T-0036-001` 이 *공존*할 때
    reid 는 정확히 canonical `T-0036` 만 바꿔야 한다.

    `find_ticket` 은 `T-0036-*.md` glob 의 첫 매치만 반환 — 디렉토리 순서상 `T-0036-001-slug.md`
    가 먼저 잡히면 canonical mismatch 로 실재하는 `T-0036` 을 놓치고 abort 했다(false-negative).
    전 티켓 canonical 스캔으로 정확 매치를 골라 legacy 만 relabel·숫자-prefix 는 불변."""
    _seed_ticket(board, "T-0036")        # 파일 T-0036-slug.md (canonical T-0036)
    _seed_ticket(board, "T-0036-001")    # 파일 T-0036-001-slug.md (canonical T-0036-001)
    rc = board.cmd_reid(_ns("T-0036", "T-0250"))
    assert rc == 0
    assert _ids_on_disk(board) == {"T-0250", "T-0036-001"}   # legacy 만 변경·숫자-prefix 불변
    assert (board.TICKETS_DIR / "open" / "T-0250-slug.md").exists()
    assert (board.TICKETS_DIR / "open" / "T-0036-001-slug.md").exists()


# ════════════════════════════════════════════════════════════════════════
# 상태 가드 — OLD 실재 · NEW collision
# ════════════════════════════════════════════════════════════════════════

def test_reid_old_not_found_aborts(board, capsys):
    _seed_ticket(board, "T-0036")
    rc = board.cmd_reid(_ns("T-9999", "T-0250"))
    assert rc == 2
    assert "찾을 수 없다" in capsys.readouterr().err
    assert _ids_on_disk(board) == {"T-0036"}  # 무변경


def test_reid_collision_with_existing_aborts(board, capsys):
    _seed_ticket(board, "T-0001")
    _seed_ticket(board, "T-0002")
    rc = board.cmd_reid(_ns("T-0001", "T-0002"))
    assert rc == 1
    assert "collision" in capsys.readouterr().err
    assert _ids_on_disk(board) == {"T-0001", "T-0002"}  # 무변경


def test_reid_collision_normalizes_zero_pad_width(board, capsys):
    """collision 은 문자열이 아니라 (prefix, 논리번호)로 판정 — T-002 와 T-0002 는 같은 논리 ID."""
    _seed_ticket(board, "T-0002")
    _seed_ticket(board, "T-foo-005")
    rc = board.cmd_reid(_ns("T-foo-005", "T-002"))
    assert rc == 1
    assert "collision" in capsys.readouterr().err
    assert _ids_on_disk(board) == {"T-0002", "T-foo-005"}


def test_reid_collision_with_draft_ticket_aborts(board, capsys):
    """NEW 미존재 검사는 전 상태 디렉토리 + .drafts 를 포함한다."""
    (board.TICKETS_DIR / ".drafts").mkdir(exist_ok=True)
    _seed_ticket(board, "T-0250", status=".drafts")
    _seed_ticket(board, "T-0036")
    rc = board.cmd_reid(_ns("T-0036", "T-0250"))
    assert rc == 1
    assert "collision" in capsys.readouterr().err
    assert _ids_on_disk(board) == {"T-0036", "T-0250"}


# ════════════════════════════════════════════════════════════════════════
# 타 세션 claim 가드 — 단일세션 op
# ════════════════════════════════════════════════════════════════════════

def test_reid_other_session_claim_aborts(board, capsys):
    _seed_ticket(board, "T-0036", status="claimed", claimed_by="alice/other_1")
    rc = board.cmd_reid(_ns("T-0036", "T-0250", repo="mine", slot=1))
    assert rc == 1
    err = capsys.readouterr().err
    assert "claim 중" in err and "단일세션" in err
    assert _ids_on_disk(board) == {"T-0036"}  # 무변경


def test_reid_own_session_claim_allowed(board):
    _seed_ticket(board, "T-0036", status="claimed", claimed_by="me/proj_1")
    rc = board.cmd_reid(_ns("T-0036", "T-0250", repo="proj", slot=1))
    assert rc == 0
    renamed = board.TICKETS_DIR / "claimed" / "T-0250-slug.md"
    fm, _ = board.load_ticket(renamed)
    assert fm["id"] == "T-0250"
    assert fm["claimed_by"] == "me/proj_1"   # claim 은 보존(reid 는 ID 만 바꾼다)


def test_reid_own_session_claim_slot_only_allowed(board):
    """claimed_by 가 slot-only(`proj_1`·user 미상)여도 슬롯 매칭이면 허용."""
    _seed_ticket(board, "T-0036", status="claimed", claimed_by="proj_1")
    rc = board.cmd_reid(_ns("T-0036", "T-0250", repo="proj", slot=1))
    assert rc == 0
    assert _ids_on_disk(board) == {"T-0250"}


def test_reid_claimed_without_session_aborts(board, capsys):
    """claim 중인데 세션 미해소(None)면 소유 증명 불가 → 안전하게 abort."""
    _seed_ticket(board, "T-0036", status="claimed", claimed_by="alice/other_1")
    rc = board.cmd_reid(_ns("T-0036", "T-0250"))  # --session 없음·env/lease/local.conf 다 부재
    assert rc == 1
    assert "claim 중" in capsys.readouterr().err
    assert _ids_on_disk(board) == {"T-0036"}


def test_reid_open_ticket_needs_no_session(board):
    """open(미claim) 티켓은 세션 없이도 reid 가능 — claim 가드는 claimed 티켓 한정."""
    _seed_ticket(board, "T-0036")
    rc = board.cmd_reid(_ns("T-0036", "T-0250"))
    assert rc == 0
    assert _ids_on_disk(board) == {"T-0250"}


# ════════════════════════════════════════════════════════════════════════
# dry-run — 규모 preview·쓰기 0
# ════════════════════════════════════════════════════════════════════════

def test_reid_dry_run_no_write(board, capsys):
    _seed_ticket(board, "T-0036")
    _seed_wiki(board, "0001-x.md", "ref [[T-0036]]\n")
    rc = board.cmd_reid(_ns("T-0036", "T-0250", dry_run=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "[dry-run]" in out and "reid" in out
    assert "T-0036 → T-0250" in out
    assert _ids_on_disk(board) == {"T-0036"}  # 무변경


def test_reid_dry_run_reports_scale(board, capsys):
    _seed_ticket(board, "T-0036")
    _seed_wiki(board, "0001-x.md", "ref [[T-0036]] and T-0036\n")
    rc = board.cmd_reid(_ns("T-0036", "T-0250", dry_run=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "refs" in out and "파일명 rename" in out


# ════════════════════════════════════════════════════════════════════════
# 멱등성 — 재실행 no-op
# ════════════════════════════════════════════════════════════════════════

def test_reid_second_identical_run_is_noop(board, capsys):
    """1회 적용 후 같은 reid 재실행 → OLD 소멸이라 rc2·추가 변경 0(이중 적용 없음)."""
    _seed_ticket(board, "T-0036")
    wiki = _seed_wiki(board, "0001-x.md", "ref [[T-0036]]\n")
    assert board.cmd_reid(_ns("T-0036", "T-0250")) == 0
    after_first = _ids_on_disk(board)
    wiki_after_first = wiki.read_text(encoding="utf-8")
    capsys.readouterr()
    rc = board.cmd_reid(_ns("T-0036", "T-0250"))   # 재실행 — OLD 없음.
    assert rc == 2
    assert "찾을 수 없다" in capsys.readouterr().err
    assert _ids_on_disk(board) == after_first == {"T-0250"}
    assert wiki.read_text(encoding="utf-8") == wiki_after_first   # 참조 재변경 0.


# ════════════════════════════════════════════════════════════════════════
# 홈 git dirty = 안내(차단 아님·상속·_prefix_relabel·ADR-0074)
# ════════════════════════════════════════════════════════════════════════

def test_reid_home_git_dirty_does_not_abort(board, monkeypatch, capsys):
    """남의 미커밋 변경이 있어도 reid 는 진행된다 — 과차단 폐기(ADR-0074·상속)."""
    _seed_ticket(board, "T-0036")
    monkeypatch.setattr(board, "_home_git_status_porcelain", lambda: " M wiki/x.md\n")
    rc = board.cmd_reid(_ns("T-0036", "T-0250"))
    assert rc == 0
    assert "무관한 미커밋 변경" in capsys.readouterr().out
    assert _ids_on_disk(board) == {"T-0250"}  # dirty 여도 적용됨


def test_reid_home_git_dirty_does_not_block_dry_run(board, monkeypatch, capsys):
    _seed_ticket(board, "T-0036")
    monkeypatch.setattr(board, "_home_git_status_porcelain", lambda: " M wiki/x.md\n")
    rc = board.cmd_reid(_ns("T-0036", "T-0250", dry_run=True))
    assert rc == 0
    assert "[dry-run]" in capsys.readouterr().out


# ════════════════════════════════════════════════════════════════════════
# board-git 백업 (상속·_prefix_relabel) — reid 라벨 verb 정합
# ════════════════════════════════════════════════════════════════════════

def test_reid_board_git_backup_commit_when_separated(board, monkeypatch, capsys):
    calls = {"commit": []}
    monkeypatch.setattr(board, "_board_git_enabled", lambda: True)
    monkeypatch.setattr(board, "_board_git_head", lambda: "deadbeefcafe0000")
    monkeypatch.setattr(
        board, "_board_git_stage_and_commit",
        lambda msg, paths=None: calls["commit"].append((msg, paths)) or True)
    ticket = _seed_ticket(board, "T-0036")
    rc = board.cmd_reid(_ns("T-0036", "T-0250"))
    assert rc == 0
    # noun="reid"·verb="" → op="reid" (double-space 없음).
    assert [msg for msg, _ in calls["commit"]] == ["reid T-0036 → T-0250"]
    # 스코프 커밋(ADR-0074) — 옛/새 티켓 경로가 스코프에 들어간다(board 전체 아님).
    scoped = {Path(p).name for p in calls["commit"][0][1]}
    assert ticket.name in scoped
    assert any(name.startswith("T-0250") for name in scoped)
    out = capsys.readouterr().out
    assert "백업 rev" in out and "deadbeefcafe" in out


def test_reid_board_git_legacy_skips_with_guidance(board, monkeypatch, capsys):
    monkeypatch.setattr(board, "_board_git_enabled", lambda: False)
    _seed_ticket(board, "T-0036")
    rc = board.cmd_reid(_ns("T-0036", "T-0250"))
    assert rc == 0
    assert "legacy" in capsys.readouterr().out
    assert _ids_on_disk(board) == {"T-0250"}  # relabel 은 그래도 적용


# ════════════════════════════════════════════════════════════════════════
# 락 직렬화 (상속·_prefix_relabel) — 단일 board_lock·재진입 없음
# ════════════════════════════════════════════════════════════════════════

@contextlib.contextmanager
def _lock_depth_spy(board, monkeypatch):
    real_lock = board.board_lock
    rec = {"count": 0, "depth": 0, "max_depth": 0, "changed_in_lock": False}

    @contextlib.contextmanager
    def _spied():
        rec["count"] += 1
        rec["depth"] += 1
        rec["max_depth"] = max(rec["max_depth"], rec["depth"])
        before = _ids_on_disk(board)
        try:
            with real_lock():
                yield
        finally:
            if _ids_on_disk(board) != before:
                rec["changed_in_lock"] = True
            rec["depth"] -= 1

    monkeypatch.setattr(board, "board_lock", _spied)
    yield rec


def test_reid_mutation_runs_under_single_board_lock(board, monkeypatch):
    _seed_ticket(board, "T-0036")
    with _lock_depth_spy(board, monkeypatch) as rec:
        rc = board.cmd_reid(_ns("T-0036", "T-0250"))
    assert rc == 0
    assert rec["count"] >= 1
    assert rec["max_depth"] == 1           # 재진입 없음(데드락 위험 0)
    assert rec["changed_in_lock"]
    assert _ids_on_disk(board) == {"T-0250"}


def test_reid_dry_run_takes_no_lock(board, monkeypatch, capsys):
    _seed_ticket(board, "T-0036")
    with _lock_depth_spy(board, monkeypatch) as rec:
        rc = board.cmd_reid(_ns("T-0036", "T-0250", dry_run=True))
    assert rc == 0
    assert rec["count"] == 0
    assert "[dry-run]" in capsys.readouterr().out
    assert _ids_on_disk(board) == {"T-0036"}


# ════════════════════════════════════════════════════════════════════════
# CLI 배선 — argparse reid 서브커맨드 → cmd_reid 디스패치
# ════════════════════════════════════════════════════════════════════════

def test_cli_reid_dispatches_to_cmd_reid(board):
    parser = board.build_parser()
    args = parser.parse_args([
        "reid", "T-0036", "T-finance-036", "--user-ack", "finance", "--dry-run",
    ])
    assert args.fn is board.cmd_reid
    assert args.old_id == "T-0036"
    assert args.new_id == "T-finance-036"
    assert args.user_ack == "finance"
    assert args.dry_run is True


def test_cli_reid_session_flag_parsed(board):
    parser = board.build_parser()
    args = parser.parse_args(["reid", "T-0036", "T-0250", "--repo", "proj", "--slot", "1"])
    assert args.repo == "proj"
    assert args.slot == 1
    assert args.dry_run is False
