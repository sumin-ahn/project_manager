"""areas.md `merge=union` 배포 — 두 형상(inline·board 분리) 다 동시 등록 안전 (T-0418).

areas.md 는 append-only 레지스트리라 두 clone 의 동시 등록이 merge 에서 **양쪽 행을 모두
보존**해야 한다 — 그 보장은 git 내장 `merge=union` 드라이버에 기대고, 선언은 **areas.md 를 담은
git** 의 `.gitattributes` 에 있어야 유효하다. board-git 분리(ADR-0033 ①) 후 areas.md 는 board
submodule(별도 git) 안으로 옮겨졌는데 그 git 엔 `.gitattributes` 가 없어 union 이 조용히
사라졌다(`git -C .project_manager/board check-attr merge -- areas.md` = unspecified·실측).

여기서 검증하는 것:
  - **판정은 파일 내용으로** — `_gitattributes_merge_attr` last-match-wins 파싱(런타임
    `git check-attr` 호출 0·비용/이식성).
  - **seed**: `pm_import.setup_board_submodule` 이 신규 board 에 `.gitattributes` 를 만든다.
  - **backfill**: 이미 만들어진 board 는 `board._ensure_board_gitattributes` 가 **멱등 보강**
    (기존 내용·채택자 커스텀 줄 보존·덮어쓰기 없음) — board git commit funnel + `init` 에서.
  - **실측**: 보강 후 `check-attr merge -- areas.md` 가 실제로 `union` 으로 해소된다.
  - **advisory**: 미배포면 `areas-merge-union`(never-block)이 표면화되고 보강 후 해소된다.
  - **inline 회귀**: 루트 `.gitattributes` 의 `.project_manager/areas.md merge=union` 유지.

hermetic: 실 git 은 tmp 안에서만 쓰고(네트워크 0), git 바이너리 부재 환경은 실-git 케이스만
skip 한다(내용 판정 단위 테스트는 항상 실행).
"""
from __future__ import annotations

import argparse
import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

requires_git = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git 바이너리 부재 — 실 git 케이스 skip(내용 판정 단위 테스트는 항상 실행).",
)

# hermetic git commit 을 위한 결정적 author/committer (test_board_git_sync 동형).
_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
}


def _load_mod(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def board(tmp_path, monkeypatch):
    """REPO 를 tmp 로 재지정한 fresh board 모듈 (실 루트 미접촉·test_board_git_sync 동형)."""
    mod = _load_mod("board")
    monkeypatch.setattr(mod, "REPO", tmp_path)
    monkeypatch.setattr(mod, "BOARD_LOCK", tmp_path / ".project_manager" / ".local" / "board.lock")
    for key, val in _GIT_IDENTITY.items():
        monkeypatch.setenv(key, val)
    return mod


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, encoding="utf-8", errors="replace", check=False)


_TICKET_TEXT = (
    "---\nid: {tid}\ntitle: t\nstatus: open\nclaimed_by: null\nclaimed_at: null\n"
    "completed_at: null\ndepends_on: []\nblocks: []\ntouches: []\nestimate: small\n"
    "tags: []\n---\n\n# {tid} — t\n\n## 목표\nx\n"
)


def _make_board_dir(board_mod, root: Path, *, real_git: bool = False,
                    remote: Path | None = None, ticket: str | None = None) -> Path:
    """`<root>/.project_manager/board/` 에 board 분리 형상을 만든다 (tickets/ + areas.md + .git).

    `real_git=False` 면 `.git` 을 빈 파일로 둔다 — `_ensure_board_gitattributes`/`board_root()`
    는 *존재*만 보므로 git 바이너리 없이도 분리 형상을 정확히 모사한다(내용 판정 테스트용).
    `remote` 를 주면 origin 등록 + main push(upstream) 로 pull/push 경로까지 살린다(claim 용).
    """
    board_dir = root / ".project_manager" / "board"
    for status in ("open", "claimed", "blocked", "done"):
        (board_dir / "tickets" / status).mkdir(parents=True, exist_ok=True)
    (board_dir / "areas.md").write_text(
        "# Area Registry\n\n"
        + board_mod._areas_header_line() + "\n"
        + board_mod._areas_separator_line() + "\n",
        encoding="utf-8")
    if ticket:
        (board_dir / "tickets" / "open" / f"{ticket}-t.md").write_text(
            _TICKET_TEXT.format(tid=ticket), encoding="utf-8")
    if real_git:
        _git(["init", "-q", "-b", "main"], board_dir)
        if remote is not None:
            _git(["remote", "add", "origin", str(remote)], board_dir)
        _git(["add", "-A"], board_dir)
        _git(["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "board init"],
             board_dir)
        if remote is not None:
            _git(["push", "-q", "-u", "origin", "main"], board_dir)
    else:
        (board_dir / ".git").write_text("gitdir: ../../.git/modules/board\n", encoding="utf-8")
    return board_dir


def _check_attr_merge(board_dir: Path) -> str:
    """`git check-attr merge -- areas.md` 의 값 (실측 — 엔진은 이걸 런타임에 부르지 않는다)."""
    out = _git(["check-attr", "merge", "--", "areas.md"], board_dir).stdout.strip()
    return out.rsplit(":", 1)[-1].strip()


# ════════════════════════════════════════════════════════════════════════
# 1. 내용 판정 — `.gitattributes` 파싱 (check-attr 호출 0)
# ════════════════════════════════════════════════════════════════════════

def test_union_detected_for_board_pattern(board):
    assert board._areas_union_declared(
        "areas.md merge=union\n", board._BOARD_AREAS_ATTR_TARGETS) is True


def test_union_detected_for_rooted_board_pattern(board):
    """선두 `/` 형(`/areas.md`)도 같은 파일을 가리키므로 인정한다."""
    assert board._areas_union_declared(
        "/areas.md merge=union\n", board._BOARD_AREAS_ATTR_TARGETS) is True


def test_union_absent_when_no_declaration(board):
    text = "# 주석뿐\n\n*.cmd text eol=crlf\n"
    assert board._areas_union_declared(text, board._BOARD_AREAS_ATTR_TARGETS) is False
    assert board._gitattributes_merge_attr(text, board._BOARD_AREAS_ATTR_TARGETS) is None


def test_later_unset_overrides_union(board):
    """git 은 뒤 줄이 앞 줄을 덮는다(last-match-wins) — `-merge` 가 뒤면 union 아님."""
    text = "areas.md merge=union\nareas.md -merge\n"
    assert board._areas_union_declared(text, board._BOARD_AREAS_ATTR_TARGETS) is False
    assert board._gitattributes_merge_attr(text, board._BOARD_AREAS_ATTR_TARGETS) == ""


def test_commented_declaration_is_not_counted(board):
    assert board._areas_union_declared(
        "# areas.md merge=union\n", board._BOARD_AREAS_ATTR_TARGETS) is False


def test_trailing_hash_invalidates_line_like_git(board):
    """`#` 은 줄 *끝* 주석이 아니다 — git 은 그 줄을 무시하고(unspecified) 우리도 무효로 본다.

    거짓 정상 방지: 선언된 줄로 읽으면 실제로는 union 이 없는데 advisory 가 침묵하고 backfill 이
    no-op 이 된다(union 상실이 조용히 굳음·reviewer 실측 대조).
    """
    text = "areas.md merge=union # 이건 주석이 아니다\n"
    assert board._gitattributes_merge_attr(text, board._BOARD_AREAS_ATTR_TARGETS) is None
    assert board._areas_union_declared(text, board._BOARD_AREAS_ATTR_TARGETS) is False


def test_hash_invalidated_line_does_not_shadow_valid_declaration(board):
    """무효 줄은 *무시*될 뿐 앞선 유효 선언을 지우지 않는다(git 동형·last-match 는 유효 줄끼리)."""
    text = "areas.md merge=union\nareas.md -merge # 무효 줄\n"
    assert board._areas_union_declared(text, board._BOARD_AREAS_ATTR_TARGETS) is True


@pytest.mark.parametrize("glob_line", ["*.md -merge", "* -merge", "areas.* -merge"])
def test_trailing_simple_glob_unset_cancels_union(board, glob_line):
    """단순 glob 의 후행 unset 은 union 을 취소한다 — git 실측(`unset`)과 일치.

    `*.md`·`*`·`areas.*` 는 실제 `.gitattributes` 에 흔한 형태다. 이걸 못 보면 union 이 실제로는
    없는데 advisory 가 침묵하는 **거짓 정상**(보호 상실)이 된다.
    """
    text = f"areas.md merge=union\n{glob_line}\n"
    assert board._areas_union_declared(text, board._BOARD_AREAS_ATTR_TARGETS) is False


@pytest.mark.parametrize("glob_line", ["*.md merge=union", "* merge=union",
                                       "areas.* merge=union"])
def test_simple_glob_declaration_is_recognized(board, glob_line):
    """단순 glob 으로 *선언* 한 union 도 인정한다(git 실측 `union`) — 불필요한 중복 append 방지."""
    assert board._areas_union_declared(
        glob_line + "\n", board._BOARD_AREAS_ATTR_TARGETS) is True


def test_simple_glob_matches_inline_target_by_basename(board):
    """슬래시 없는 패턴은 git 처럼 basename 에도 걸린다 (inline 형상 `.project_manager/areas.md`)."""
    assert board._areas_union_declared(
        "areas.* merge=union\n", board._INLINE_AREAS_ATTR_TARGETS) is True


def test_unrelated_glob_does_not_match(board):
    """무관한 glob 은 안 걸린다 — 남의 선언을 우리 것으로 오독하지 않는다."""
    assert board._areas_union_declared(
        "tickets/*.md merge=union\n", board._BOARD_AREAS_ATTR_TARGETS) is False
    assert board._areas_union_declared(
        "areas.md merge=union\ntickets/ -merge\n",
        board._BOARD_AREAS_ATTR_TARGETS) is True   # git 실측도 union 유지.


def test_known_limitation_double_star_pattern(board):
    """**남는 한계 박제**: `**` 복합 패턴은 판정하지 못한다 (git 패턴 엔진 재구현 없이는 불가).

    - `**/areas.md -merge` 후행 unset → git 은 `unset` 이지만 우리는 True (거짓 정상·잔여 위험).
    - `**/areas.md merge=union` 선언 → 우리는 False (거짓 경보·안전 방향·중복 append 뿐).
    현재 동작을 고정해 한계 범위를 명시적으로 남긴다 — 넓히려면 판정기를 바꿔야 한다.
    """
    assert board._areas_union_declared(
        "areas.md merge=union\n**/areas.md -merge\n",
        board._BOARD_AREAS_ATTR_TARGETS) is True
    assert board._areas_union_declared(
        "**/areas.md merge=union\n", board._BOARD_AREAS_ATTR_TARGETS) is False


def test_inline_pattern_uses_project_manager_path(board):
    """inline 형상 선언(`.project_manager/areas.md`)은 board 패턴과 섞이지 않는다."""
    text = ".project_manager/areas.md merge=union\n"
    assert board._areas_union_declared(text, board._INLINE_AREAS_ATTR_TARGETS) is True
    assert board._areas_union_declared(text, board._BOARD_AREAS_ATTR_TARGETS) is False


def test_root_gitattributes_keeps_inline_union_declaration(board):
    """**회귀 가드**: 루트 `.gitattributes` 의 inline 선언은 유지된다(board 쪽은 *추가*·제거 아님).

    inline(비-서브모듈) 채택자는 이 선언이 유일한 union 배포처다(engine.manifest 로 배포).
    """
    text = (REPO / ".gitattributes").read_text(encoding="utf-8")
    assert board._areas_union_declared(text, board._INLINE_AREAS_ATTR_TARGETS) is True


# ════════════════════════════════════════════════════════════════════════
# 2. backfill — `_ensure_board_gitattributes` 멱등 보강
# ════════════════════════════════════════════════════════════════════════

def test_ensure_noop_on_legacy_inline(board, tmp_path):
    """board 미분리(legacy·솔로) → no-op·파일 생성 0 (100% 무영향)."""
    (tmp_path / ".project_manager" / "wiki").mkdir(parents=True)
    assert board._ensure_board_gitattributes() is False
    assert not (tmp_path / ".project_manager" / "wiki" / ".gitattributes").exists()
    assert not (tmp_path / ".gitattributes").exists()


def test_ensure_creates_gitattributes_in_board_git(board, tmp_path):
    board_dir = _make_board_dir(board, tmp_path)
    assert board._ensure_board_gitattributes() is True
    text = (board_dir / ".gitattributes").read_text(encoding="utf-8")
    assert board._areas_union_declared(text, board._BOARD_AREAS_ATTR_TARGETS) is True


def test_ensure_is_idempotent_no_duplicate_line(board, tmp_path):
    """두 번째 호출은 False + 파일 무변경 (멱등 no-write — 줄 중복 0)."""
    board_dir = _make_board_dir(board, tmp_path)
    assert board._ensure_board_gitattributes() is True
    first = (board_dir / ".gitattributes").read_text(encoding="utf-8")
    assert board._ensure_board_gitattributes() is False
    second = (board_dir / ".gitattributes").read_text(encoding="utf-8")
    assert second == first
    assert second.count("areas.md merge=union") == 1


def test_ensure_preserves_adopter_custom_lines(board, tmp_path):
    """기존 `.gitattributes` 를 덮어쓰지 않고 **append 로만** 보강한다 (채택자 규칙 보존)."""
    board_dir = _make_board_dir(board, tmp_path)
    custom = "# 채택자 규칙\n*.md text eol=lf\ntickets/** linguist-generated\n"
    (board_dir / ".gitattributes").write_text(custom, encoding="utf-8")

    assert board._ensure_board_gitattributes() is True

    text = (board_dir / ".gitattributes").read_text(encoding="utf-8")
    assert text.startswith(custom), "기존 내용이 앞에 그대로 보존돼야 한다"
    assert "*.md text eol=lf" in text and "tickets/** linguist-generated" in text
    assert board._areas_union_declared(text, board._BOARD_AREAS_ATTR_TARGETS) is True


def test_ensure_appends_cleanly_when_file_lacks_trailing_newline(board, tmp_path):
    """줄바꿈 없이 끝난 파일도 마지막 줄과 붙지 않게 이어붙인다."""
    board_dir = _make_board_dir(board, tmp_path)
    (board_dir / ".gitattributes").write_text("*.md text eol=lf", encoding="utf-8")

    assert board._ensure_board_gitattributes() is True

    text = (board_dir / ".gitattributes").read_text(encoding="utf-8")
    assert "*.md text eol=lf\n" in text
    assert board._areas_union_declared(text, board._BOARD_AREAS_ATTR_TARGETS) is True


def test_ensure_respects_existing_custom_union_declaration(board, tmp_path):
    """채택자가 이미 (다른 표기로) union 을 선언했으면 아무 것도 쓰지 않는다."""
    board_dir = _make_board_dir(board, tmp_path)
    custom = "/areas.md merge=union\n"
    (board_dir / ".gitattributes").write_text(custom, encoding="utf-8")

    assert board._ensure_board_gitattributes() is False
    assert (board_dir / ".gitattributes").read_text(encoding="utf-8") == custom


# ════════════════════════════════════════════════════════════════════════
# 3. 실측 — check-attr 가 board git 에서 union 으로 해소된다
# ════════════════════════════════════════════════════════════════════════

@requires_git
def test_check_attr_resolves_union_after_backfill(board, tmp_path):
    """보강 전 `unspecified` → 보강 후 `union` (티켓 재현 실측의 역전)."""
    board_dir = _make_board_dir(board, tmp_path, real_git=True)
    assert _check_attr_merge(board_dir) == "unspecified"

    assert board._ensure_board_gitattributes() is True

    assert _check_attr_merge(board_dir) == "union"


@requires_git
def test_stage_and_commit_backfills_and_commits_gitattributes(board, tmp_path):
    """board git commit funnel 이 backfill 하고 그 commit 에 `.gitattributes` 를 싣는다.

    기존 board(seed 재실행 없음)가 다음 mutation 에서 자연 정합되는 경로 — commit 에 실려야
    push/pull 로 공유 remote·다른 clone 까지 전파된다.
    """
    board_dir = _make_board_dir(board, tmp_path, real_git=True)
    assert not (board_dir / ".gitattributes").exists()

    assert board._board_git_stage_and_commit("mutation") is True

    tracked = _git(["ls-files", "--", ".gitattributes"], board_dir).stdout.strip()
    assert tracked == ".gitattributes", "backfill 이 commit 에 실려야 remote 로 전파된다"
    assert _check_attr_merge(board_dir) == "union"


@requires_git
def test_init_leaves_board_clean_and_claim_not_blocked(board, tmp_path, monkeypatch):
    """`board.py init` 이 board 를 dirty 로 남기지 않는다 — clone→init→claim 온보딩 회귀.

    backfill 을 commit 하지 않는 지점(init)에서 쓰면 `?? .gitattributes` 로 남아 claim STRICT 의
    dirty 가드가 *엔진이 만든 파일*을 사용자 편집으로 오인해 claim 을 막는다(reviewer 실측).
    배포는 write→stage→commit 이 한 호출에 닫히는 funnel 단일 채널이므로 init 은 board 를
    건드리지 않고, 뒤따르는 claim 이 정상 진행하며 그 commit 에 backfill 이 실린다.
    """
    bare = tmp_path / "bare-init"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(bare)], check=True)
    board_dir = _make_board_dir(board, tmp_path, real_git=True, remote=bare, ticket="T-0001")
    monkeypatch.setattr(board, "LOCAL_CONF", tmp_path / "local.conf")
    monkeypatch.setattr(board, "PM_STATE_FILE", tmp_path / "pm_state.md")
    monkeypatch.setattr(board, "PM_STATE_TEMPLATE", tmp_path / "missing-template.md")
    monkeypatch.setattr(board, "install_pre_push_hook", lambda: False)
    monkeypatch.setattr(board, "prompt_external_review_optin", lambda: None)

    assert board.cmd_init(argparse.Namespace(
        prefix=None, area=None, owner=None, session="pm")) == 0

    porcelain = _git(["status", "--porcelain"], board_dir).stdout.strip()
    assert porcelain == "", f"init 이 board 를 dirty 로 남김: {porcelain!r}"
    # prefetch 가 차단 없이 anchor 를 낸다(ADR-0073 — 옛 dirty sentinel 비교의 후신).
    assert board._board_git_claim_prefetch("T-0001").block is None

    assert board.cmd_claim(argparse.Namespace(
        id="T-0001", repo="me", slot=1, user="me")) == 0
    assert (board_dir / "tickets" / "claimed" / "T-0001-t.md").exists()
    assert _check_attr_merge(board_dir) == "union", "claim commit 이 backfill 을 싣지 않았다"


@requires_git
def test_backfill_commit_excludes_drafts(board, tmp_path):
    """backfill 을 싣는 commit 에 미충전 draft 가 딸려가지 않는다 (T-0198 pathspec 유지)."""
    board_dir = _make_board_dir(board, tmp_path, real_git=True)
    drafts = board_dir / "tickets" / ".drafts"
    drafts.mkdir(parents=True, exist_ok=True)
    (drafts / "T-9999-draft.md").write_text("draft\n", encoding="utf-8")

    assert board._board_git_stage_and_commit("mutation") is True

    tracked = _git(["ls-files"], board_dir).stdout.split()
    assert ".gitattributes" in tracked
    assert not [t for t in tracked if t.startswith("tickets/.drafts")], \
        f"backfill commit 에 draft 유출: {tracked}"


def test_dirty_claim_guidance_names_the_blocking_files(board, tmp_path, monkeypatch, capsys):
    """잔여 dirty 차단 안내가 **막고 있는 파일을 지목**한다 — 일괄 `add -A` 유도 금지 (ADR-0073).

    T-0418 시절 이 테스트는 "안내가 draft 제외 pathspec(`add -A -- . ':!tickets/.drafts'`)을
    쓰는가" 를 지켰다. ADR-0073 이 그 안내 자체를 폐기했다 — 공유 board 에서 그 커맨드는 **남의
    미완성 편집을 사용자가 대신 커밋**하게 만들기 때문이다(draft 유출과 같은 클래스). 지켜야 할
    성질은 그대로 남는다: *일괄 쓸어담기로 유도하지 않는다*. 그래서 판정을 "무엇을 커밋하라고
    하는가" 로 옮긴다 — 실제로 막고 있는 경로만 지목해야 한다.
    """
    blocked = board._ClaimPrefetch(
        block=board._CLAIM_BLOCK_DIRTY, behind=2,
        dirty=((" M", "tickets/open/T-0002-other.md"), ("??", "areas.md")))
    monkeypatch.setattr(board, "_board_git_claim_prefetch", lambda tid: blocked)

    assert board.cmd_claim(argparse.Namespace(
        id="T-0001", repo="me", slot=1, user="me")) == 1

    err = capsys.readouterr().err
    assert "offline 아님" in err        # 원인 정확(네트워크 문제로 오판 금지) — 기존 의미 보존.
    assert "2 커밋" in err, f"behind 수치 미노출: {err!r}"
    assert "tickets/open/T-0002-other.md" in err and "areas.md" in err, \
        f"막고 있는 파일을 지목하지 않음: {err!r}"
    assert "add -A" in err and "쓸어담지 마라" in err, \
        f"일괄 add -A 금지 경고가 없음: {err!r}"
    assert "add -A --" not in err, f"맨 `add -A` 커맨드로 유도함: {err!r}"


# ════════════════════════════════════════════════════════════════════════
# 4. advisory — 미배포 표면화 (never-block·내용 판정)
# ════════════════════════════════════════════════════════════════════════

def test_advisory_fires_when_board_git_lacks_declaration(board, tmp_path):
    _make_board_dir(board, tmp_path)
    findings = board.lint_areas_merge_union()
    assert len(findings) == 1
    _label, kind, detail = findings[0]
    assert kind == "areas-merge-union"
    assert "merge=union" in detail


def test_advisory_is_never_blocking(board):
    """`--gate` 종료코드에 기여하지 않는다(advisory 등재)."""
    assert "areas-merge-union" in board._ADVISORY_LINT_KINDS


def test_advisory_resolved_after_backfill(board, tmp_path):
    _make_board_dir(board, tmp_path)
    assert board.lint_areas_merge_union()
    board._ensure_board_gitattributes()
    assert board.lint_areas_merge_union() == []


def test_advisory_silent_for_inline_with_root_declaration(board, tmp_path):
    """inline 형상 + 루트 선언 있음 → 무발화 (레거시 채택자 정상)."""
    (tmp_path / ".project_manager").mkdir(parents=True)
    (tmp_path / ".project_manager" / "areas.md").write_text("# Area Registry\n", encoding="utf-8")
    (tmp_path / ".gitattributes").write_text(
        ".project_manager/areas.md merge=union\n", encoding="utf-8")
    assert board.lint_areas_merge_union() == []


def test_advisory_fires_for_inline_without_root_declaration(board, tmp_path):
    """inline 형상인데 루트 선언이 없으면(선언 제거·미배포 clone) 표면화한다."""
    (tmp_path / ".project_manager").mkdir(parents=True)
    (tmp_path / ".project_manager" / "areas.md").write_text("# Area Registry\n", encoding="utf-8")
    findings = board.lint_areas_merge_union()
    assert len(findings) == 1
    assert findings[0][1] == "areas-merge-union"
    assert ".project_manager/areas.md merge=union" in findings[0][2]


def test_advisory_silent_without_areas_file(board, tmp_path):
    """areas.md 부재(솔로 미등록) → 무발화."""
    assert board.lint_areas_merge_union() == []


def test_lint_tickets_includes_areas_merge_union(board, tmp_path, monkeypatch):
    """`board lint` 집계에 배선돼 실제로 표면화된다(호출 누락 가드)."""
    monkeypatch.setattr(board, "lint_areas_merge_union",
                        lambda: [("areas.md", "areas-merge-union", "x")])
    for name in ("lint_dependencies", "lint_bodies", "lint_ideas", "lint_status",
                 "lint_wikilinks", "lint_unstable_refs", "lint_scopes", "lint_domain",
                 "lint_adr_lifecycle", "lint_adr_author", "lint_architecture_freshness",
                 "lint_status_freshness", "lint_domain_freshness", "lint_adapter_drift",
                 "lint_render_leak", "lint_unmigrated_overlay", "lint_areas_duplicate_repo"):
        monkeypatch.setattr(board, name, list)
    assert board.lint_tickets() == [("areas.md", "areas-merge-union", "x")]


# ════════════════════════════════════════════════════════════════════════
# 5. seed — pm_import 신규 board 스캐폴드 + drift 가드
# ════════════════════════════════════════════════════════════════════════

def test_import_scaffold_mirrors_board_block(board):
    """pm_import 미러 상수가 board 의 배포 블록과 동일(문구 drift 가드·둘 다 stdlib-only)."""
    pm_import = _load_mod("pm_import")
    assert pm_import._BOARD_GITATTRIBUTES_SCAFFOLD == board._BOARD_GITATTRIBUTES_BLOCK
    assert board._areas_union_declared(
        pm_import._BOARD_GITATTRIBUTES_SCAFFOLD, board._BOARD_AREAS_ATTR_TARGETS) is True


@requires_git
def test_new_board_submodule_seed_deploys_union(board, tmp_path, monkeypatch):
    """`pm-import --new --board-submodule` seed 된 board 가 곧바로 union 으로 해소된다."""
    pm_import = _load_mod("pm_import")
    monkeypatch.setattr(pm_import, "_real_models_runner", lambda: (False, []))
    for key, val in _GIT_IDENTITY.items():
        monkeypatch.setenv(key, val)
    bare = tmp_path / "board.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    dest = tmp_path / "home"

    assert pm_import.main(["--new", str(dest), "--harness", "claude", "--name", "H",
                           "--board-submodule", "--board-remote", str(bare)]) == 0

    board_dir = dest / ".project_manager" / "board"
    assert (board_dir / ".gitattributes").is_file()
    assert _check_attr_merge(board_dir) == "union"
    # seed 는 remote 에도 실린다 — 합류하는 다른 clone 이 같은 보장을 받는다.
    assert _git(["cat-file", "-e", "HEAD:.gitattributes"], bare).returncode == 0


@requires_git
def test_ensure_fail_soft_on_non_utf8_gitattributes(board, tmp_path):
    """비-UTF8 `.gitattributes` 는 보강을 포기할 뿐 mutation commit 을 깨지 않는다 (fail-soft).

    `_ensure_board_gitattributes` 는 board git commit funnel 에서 불리므로, 어떤 예외도
    ticket mutation 을 터뜨려선 안 된다. advisory 도 같은 규약으로 무발화한다 — 내용을 읽을 수
    없으면 배포 여부가 *미상* 이지 미배포 단정이 아니고, lint 는 예외로 깨지지 않는다.
    """
    board_dir = _make_board_dir(board, tmp_path, real_git=True)
    (board_dir / ".gitattributes").write_bytes(b"\xff\xfe areas.md merge=union\n")

    assert board._ensure_board_gitattributes() is False
    assert board._board_git_stage_and_commit("mutation") is True   # commit 은 정상 진행.
    assert board.lint_areas_merge_union() == []                    # lint 도 안 깨진다(무발화).
