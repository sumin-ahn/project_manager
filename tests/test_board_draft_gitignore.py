"""board `.gitignore` 에 `tickets/.drafts/` 편입 — dirty 게이트 draft 오탐 소멸 (T-0632).

draft 티켓(`tickets/.drafts/`)은 **설계상 board-git 미커밋**이다(ADR-0049 authoring flow). 그
의도는 mutation 축에만 박혀 있었고(`_BOARD_GIT_SCOPE_EXCLUDE`·`_BOARD_GIT_DRAFT_PATHSPEC`)
관측 축엔 선언이 없어, draft 파일이 untracked 로 남아 dirty-tree 를 재는 소비자
(`pm_handoff` [0/7] 게이트·`git status`)가 매번 "부기 누락 잔여" 로 오탐했다 — 핸드오프마다
`--ack-dirty` override 를 요구했다(PM 36 실측).

여기서 검증하는 것:
  - **내용 판정** — `_drafts_ignore_declared` 가 실제 표기(후행 `/`·선행 `/`·`**/`·basename)를
    인정하고, 주석 줄은 선언으로 세지 않으며, 부정(`!`)은 채택자 결정으로 존중한다.
  - **backfill** — `_ensure_board_gitignore` 가 부재→생성 / 항목 누락→보강 / 정합→no-op(멱등)
    이고 기존 내용을 덮어쓰지 않는다. 비-board-git 형상(legacy·솔로)은 100% 무영향.
  - **배선** — commit funnel(`_board_git_stage_and_commit`)이 보강분을 그 스코프 커밋에 싣고,
    사용자가 편집 중인 `.gitignore` 는 대신 커밋하지 않는다.
  - **실측(값-연결)** — 보강 후 draft 가 실 git 의 `--exclude-standard` 관측과 pm_handoff
    dirty 판정 양쪽에서 사라진다. draft 자체는 여전히 커밋되지 않는다.

hermetic: 실 git 은 tmp 안에서만 쓰고(네트워크 0), git 바이너리 부재 환경은 실-git 케이스만
skip 한다(내용 판정 단위 테스트는 항상 실행).
"""
from __future__ import annotations

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

# hermetic git commit 을 위한 결정적 author/committer (test_areas_merge_union 동형).
_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
}

_TICKET_TEXT = (
    "---\nid: {tid}\ntitle: t\nstatus: open\nclaimed_by: null\nclaimed_at: null\n"
    "completed_at: null\ndepends_on: []\nblocks: []\ntouches: []\nestimate: small\n"
    "tags: []\n---\n\n# {tid} — t\n\n## 목표\nx\n"
)


def _load_mod(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def board(tmp_path, monkeypatch):
    """REPO 를 tmp 로 재지정한 fresh board 모듈 (실 루트 미접촉·test_areas_merge_union 동형)."""
    mod = _load_mod("board")
    monkeypatch.setattr(mod, "REPO", tmp_path)
    monkeypatch.setattr(mod, "BOARD_LOCK",
                        tmp_path / ".project_manager" / ".local" / "board.lock")
    for key, val in _GIT_IDENTITY.items():
        monkeypatch.setenv(key, val)
    return mod


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, encoding="utf-8", errors="replace", check=False)


def _make_board_dir(board_mod, root: Path, *, real_git: bool = False,
                    ticket: str | None = None) -> Path:
    """`<root>/.project_manager/board/` 에 board 분리 형상을 만든다 (tickets/ + areas.md + .git).

    `real_git=False` 면 `.git` 을 빈 파일로 둔다 — `_ensure_board_gitignore`/`board_root()` 는
    *존재*만 보므로 git 바이너리 없이도 분리 형상을 정확히 모사한다(내용 판정 테스트용).
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
        _git(["add", "-A"], board_dir)
        _git(["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "board init"],
             board_dir)
    else:
        (board_dir / ".git").write_text("gitdir: ../../.git/modules/board\n", encoding="utf-8")
    return board_dir


def _write_draft(board_dir: Path, tid: str = "T-9999") -> Path:
    drafts = board_dir / "tickets" / ".drafts"
    drafts.mkdir(parents=True, exist_ok=True)
    path = drafts / f"{tid}-draft.md"
    path.write_text(_TICKET_TEXT.format(tid=tid), encoding="utf-8")
    return path


def _head_files(board_dir: Path) -> list[str]:
    return _git(["ls-tree", "-r", "--name-only", "HEAD"], board_dir).stdout.split()


def _untracked_unignored(board_dir: Path) -> list[str]:
    """dirty 게이트가 쓰는 바로 그 관측 — `ls-files --others --exclude-standard`."""
    out = _git(["ls-files", "--others", "--exclude-standard"], board_dir).stdout
    return [line for line in out.splitlines() if line.strip()]


# ════════════════════════════════════════════════════════════════════════
# 1. 내용 판정 — `.gitignore` 한 줄이 draft 디렉토리를 가리키는가
# ════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("line", [
    "tickets/.drafts/",
    "tickets/.drafts",
    "/tickets/.drafts/",
    "/tickets/.drafts",
    ".drafts/",
    ".drafts",
    "**/.drafts/",
])
def test_declared_forms_are_recognized(board, line):
    """실제 채택자가 쓸 수 있는 표기는 전부 '이미 다뤄짐' 으로 읽는다(중복 append 0)."""
    assert board._drafts_ignore_declared(line + "\n") is True


@pytest.mark.parametrize("line", [
    "*.log",
    "board.md",
    "tickets/",
    "drafts/",
    "/.drafts",
    ".drafts.md",
    "tickets/.drafts.bak",
])
def test_unrelated_patterns_are_not_recognized(board, line):
    """다른 경로 선언은 draft 선언이 아니다 — 미보강(거짓 정상)으로 새지 않는다."""
    assert board._drafts_ignore_declared(line + "\n") is False


def test_comment_line_is_not_a_declaration(board):
    """주석 줄은 선언이 아니다 — 우리 블록의 설명 주석이 자기 자신을 '선언됨' 으로 읽지 않게."""
    assert board._drafts_ignore_declared("# tickets/.drafts/ 는 미커밋\n") is False


def test_negation_counts_as_adopter_declaration(board):
    """부정(`!`)은 draft 를 추적하겠다는 채택자 결정 — 뒤에 우리 줄을 붙여 뒤집지 않는다.

    gitignore 는 last-match-wins 라 append 가 곧 override 다(비파괴 원칙 위반).
    """
    assert board._drafts_ignore_declared("!tickets/.drafts/\n") is True


def test_own_block_is_self_declaring(board):
    """배포 블록 자신은 '선언됨' 으로 읽힌다 — 멱등(2회차 no-write)의 뿌리."""
    assert board._drafts_ignore_declared(board._BOARD_GITIGNORE_BLOCK) is True


def test_ignore_pattern_matches_actual_drafts_location(board, tmp_path):
    """**lockstep 가드**: 리터럴 ignore 패턴 = 실제 `drafts_dir()` 의 board 상대 경로.

    세 축(관측 ignore·mutation 금지 구역·legacy pathspec)이 같은 경로를 말해야 한다. draft
    디렉토리가 옮겨지면 여기서 red 가 난다(선언만 남고 오탐이 되살아나는 조용한 drift 차단).
    """
    board_dir = _make_board_dir(board, tmp_path)
    rel = board.drafts_dir().resolve().relative_to(board_dir.resolve()).as_posix()

    assert board._BOARD_GIT_DRAFT_IGNORE_PATTERN == rel + "/"
    assert board._BOARD_DRAFT_IGNORE_FORMS == (rel, rel.rsplit("/", 1)[-1])
    assert board._BOARD_GIT_DRAFT_IGNORE_PATTERN in board._BOARD_GIT_SCOPE_EXCLUDE
    assert f":!{rel}" in board._BOARD_GIT_DRAFT_PATHSPEC


# ════════════════════════════════════════════════════════════════════════
# 2. backfill — `_ensure_board_gitignore` 멱등 보강
# ════════════════════════════════════════════════════════════════════════

def test_ensure_noop_on_legacy_inline(board, tmp_path):
    """board 미분리(legacy·솔로) → no-op·파일 생성 0 (100% 무영향)."""
    (tmp_path / ".project_manager" / "wiki").mkdir(parents=True)

    assert board._ensure_board_gitignore() is False

    assert not (tmp_path / ".project_manager" / "wiki" / ".gitignore").exists()
    assert not (tmp_path / ".gitignore").exists()


@requires_git
def test_ensure_creates_gitignore_in_board_git(board, tmp_path):
    """부재 → 생성."""
    board_dir = _make_board_dir(board, tmp_path, real_git=True)

    assert board._ensure_board_gitignore() is True

    text = (board_dir / ".gitignore").read_text(encoding="utf-8")
    assert board._drafts_ignore_declared(text) is True
    assert board._BOARD_GIT_DRAFT_IGNORE_PATTERN in text


@requires_git
def test_ensure_backfills_when_entry_missing(board, tmp_path):
    """항목 누락 → 보강. 기존 `.gitignore` 는 덮어쓰지 않고 **append 로만**(채택자 규칙 보존)."""
    board_dir = _make_board_dir(board, tmp_path, real_git=True)
    custom = "# 채택자 규칙\n*.local\nscratch/\n"
    (board_dir / ".gitignore").write_text(custom, encoding="utf-8")
    _git(["add", "--", ".gitignore"], board_dir)
    _git(["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "seed ignore"],
         board_dir)

    assert board._ensure_board_gitignore() is True

    text = (board_dir / ".gitignore").read_text(encoding="utf-8")
    assert text.startswith(custom), "기존 내용이 앞에 그대로 보존돼야 한다"
    assert "*.local" in text and "scratch/" in text
    assert board._drafts_ignore_declared(text) is True


@requires_git
def test_ensure_is_idempotent_no_duplicate_line(board, tmp_path):
    """정합 → no-op. 두 번째 호출은 False + 파일 무변경(줄 중복 0)."""
    board_dir = _make_board_dir(board, tmp_path, real_git=True)
    assert board._ensure_board_gitignore() is True
    first = (board_dir / ".gitignore").read_text(encoding="utf-8")

    assert board._ensure_board_gitignore() is False

    second = (board_dir / ".gitignore").read_text(encoding="utf-8")
    assert second == first
    assert second.count(board._BOARD_GIT_DRAFT_IGNORE_PATTERN + "\n") == 1


def test_ensure_respects_existing_custom_form(board, tmp_path):
    """채택자가 이미 (다른 표기로) 무시하고 있으면 아무 것도 쓰지 않는다."""
    board_dir = _make_board_dir(board, tmp_path)
    custom = ".drafts/\n"
    (board_dir / ".gitignore").write_text(custom, encoding="utf-8")

    assert board._ensure_board_gitignore() is False
    assert (board_dir / ".gitignore").read_text(encoding="utf-8") == custom


def test_ensure_respects_negated_declaration(board, tmp_path):
    """채택자가 draft 추적을 명시(`!`)했으면 그 결정을 뒤집지 않는다(비파괴)."""
    board_dir = _make_board_dir(board, tmp_path)
    custom = "*.tmp\n!tickets/.drafts/\n"
    (board_dir / ".gitignore").write_text(custom, encoding="utf-8")

    assert board._ensure_board_gitignore() is False
    assert (board_dir / ".gitignore").read_text(encoding="utf-8") == custom


@requires_git
def test_ensure_appends_cleanly_when_file_lacks_trailing_newline(board, tmp_path):
    """줄바꿈 없이 끝난 파일도 마지막 줄과 붙지 않게 이어붙인다."""
    board_dir = _make_board_dir(board, tmp_path, real_git=True)
    (board_dir / ".gitignore").write_text("*.local", encoding="utf-8")
    _git(["add", "--", ".gitignore"], board_dir)
    _git(["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "seed ignore"],
         board_dir)

    assert board._ensure_board_gitignore() is True

    text = (board_dir / ".gitignore").read_text(encoding="utf-8")
    assert "*.local\n" in text
    assert board._drafts_ignore_declared(text) is True


@requires_git
def test_ensure_root_files_reports_only_what_it_wrote(board, tmp_path):
    """`_ensure_board_root_files` 는 **이번 호출이 실제로 쓴** 파일명만 돌려준다."""
    _make_board_dir(board, tmp_path, real_git=True)

    assert board._ensure_board_root_files() == (".gitattributes", ".gitignore")
    assert board._ensure_board_root_files() == ()


@requires_git
def test_missing_rule_in_dirty_gitignore_is_not_appended_or_committed(
        board, tmp_path, capsys):
    """규칙 누락 + 사용자 dirty `.gitignore` → no-write·loud, mutation 커밋에도 WIP 미동반."""
    board_dir = _make_board_dir(board, tmp_path, real_git=True, ticket="T-0001")
    ignore = board_dir / ".gitignore"
    committed = "# 채택자 기본 규칙\n*.local\n"
    ignore.write_text(committed, encoding="utf-8")
    _git(["add", "--", ".gitignore"], board_dir)
    _git(["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "seed ignore"],
         board_dir)
    wip = committed + "# 아직 작업 중\n*.wip\n"
    ignore.write_text(wip, encoding="utf-8")

    assert board._ensure_board_gitignore() is False
    assert ignore.read_text(encoding="utf-8") == wip
    assert "사용자 WIP" in capsys.readouterr().err

    ticket = board_dir / "tickets" / "open" / "T-0001-t.md"
    ticket.write_text(ticket.read_text(encoding="utf-8") + "\nedited\n", encoding="utf-8")
    assert board._board_git_stage_and_commit("mutation", (ticket,)) is True
    assert _git(["show", "HEAD:.gitignore"], board_dir).stdout == committed
    assert _git(["status", "--porcelain", "--", ".gitignore"], board_dir).stdout.rstrip() == \
        " M .gitignore"


@requires_git
def test_staged_gitignore_deletion_is_preserved_without_backfill(
        board, tmp_path, capsys):
    """index 삭제(`git rm --cached`) WIP는 파일이 남아도 신규 배포로 오인하지 않는다."""
    board_dir = _make_board_dir(board, tmp_path, real_git=True, ticket="T-0001")
    attrs = board_dir / ".gitattributes"
    ignore = board_dir / ".gitignore"
    custom = "# 채택자 기본 규칙\n*.local\n"
    attrs.write_text(board._BOARD_GITATTRIBUTES_BLOCK, encoding="utf-8")
    ignore.write_text(custom, encoding="utf-8")
    _git(["add", "--", ".gitattributes", ".gitignore"], board_dir)
    _git(["commit", "-qm", "seed root files"], board_dir)
    _git(["rm", "--cached", "--", ".gitignore"], board_dir)
    ticket = board_dir / "tickets" / "open" / "T-0001-t.md"
    ticket.write_text(ticket.read_text(encoding="utf-8") + "\nedited\n", encoding="utf-8")

    assert board._board_git_stage_and_commit("mutation", (ticket,)) is True

    assert ignore.read_text(encoding="utf-8") == custom
    assert board._drafts_ignore_declared(custom) is False
    assert _git(["show", "HEAD:.gitignore"], board_dir).stdout == custom
    changed = _git(["show", "--name-only", "--format=", "HEAD"], board_dir).stdout.splitlines()
    assert ".gitignore" not in changed, "사용자 staged 삭제가 mutation 커밋에 포함됨."
    assert "사용자 WIP" in capsys.readouterr().err


@requires_git
def test_worktree_gitignore_deletion_is_preserved_without_backfill(
        board, tmp_path, capsys):
    """index에는 남은 워킹트리 삭제 WIP는 backfill로 되살리거나 커밋하지 않는다."""
    board_dir = _make_board_dir(board, tmp_path, real_git=True, ticket="T-0001")
    attrs = board_dir / ".gitattributes"
    ignore = board_dir / ".gitignore"
    custom = "# 채택자 기본 규칙\n*.local\n"
    attrs.write_text(board._BOARD_GITATTRIBUTES_BLOCK, encoding="utf-8")
    ignore.write_text(custom, encoding="utf-8")
    _git(["add", "--", ".gitattributes", ".gitignore"], board_dir)
    _git(["commit", "-qm", "seed root files"], board_dir)
    ignore.unlink()
    ticket = board_dir / "tickets" / "open" / "T-0001-t.md"
    ticket.write_text(ticket.read_text(encoding="utf-8") + "\nedited\n", encoding="utf-8")

    assert board._board_git_stage_and_commit("mutation", (ticket,)) is True

    assert not ignore.exists(), "사용자 워킹트리 삭제가 backfill로 되돌아옴."
    assert _git(["show", "HEAD:.gitignore"], board_dir).stdout == custom
    changed = _git(["show", "--name-only", "--format=", "HEAD"], board_dir).stdout.splitlines()
    assert ".gitignore" not in changed, "사용자 워킹트리 삭제가 mutation 커밋에 포함됨."
    warning = capsys.readouterr().err
    assert "삭제 WIP" in warning and ".gitignore" in warning


def test_symlink_gitignore_is_rejected_without_following_target(board, tmp_path, capsys):
    """`.gitignore` symlink 는 board 밖 target 을 절대 수정하지 않고 loud 거부한다."""
    board_dir = _make_board_dir(board, tmp_path)
    outside = tmp_path / "outside-ignore"
    original = "*.outside\n"
    outside.write_text(original, encoding="utf-8")
    link = board_dir / ".gitignore"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlink 생성 불가: {exc}")

    assert board._ensure_board_gitignore() is False
    assert link.is_symlink()
    assert outside.read_text(encoding="utf-8") == original
    assert "symlink" in capsys.readouterr().err


def test_non_regular_gitignore_is_rejected_loudly(board, tmp_path, capsys):
    """directory 등 비정규 `.gitignore` 도 쓰기 없이 사유를 표면화한다."""
    board_dir = _make_board_dir(board, tmp_path)
    (board_dir / ".gitignore").mkdir()

    assert board._ensure_board_gitignore() is False
    assert (board_dir / ".gitignore").is_dir()
    assert "비정규 파일" in capsys.readouterr().err


# ════════════════════════════════════════════════════════════════════════
# 3. 배선 — commit funnel 이 보강분을 그 스코프 커밋에 싣는다
# ════════════════════════════════════════════════════════════════════════

@requires_git
def test_backfill_rides_scoped_mutation_commit(board, tmp_path):
    """스코프 커밋 pathspec 에 `.gitignore` 가 실린다 — 빠지면 영구 미커밋(배포 사망).

    보강 호출은 commit funnel 안에 있으므로, pathspec 이 그 파일을 빠뜨리면 엔진 산출물이
    board 에 미커밋으로 눌러앉아 이 티켓이 없애려는 오탐을 스스로 만든다.
    """
    board_dir = _make_board_dir(board, tmp_path, real_git=True, ticket="T-0001")
    ticket = board_dir / "tickets" / "open" / "T-0001-t.md"
    ticket.write_text(ticket.read_text(encoding="utf-8") + "\nedited\n", encoding="utf-8")

    assert board._board_git_stage_and_commit("mutation", (ticket,)) is True

    tracked = _head_files(board_dir)
    assert ".gitignore" in tracked, "backfill 이 스코프 커밋에 실리지 않음 — 영구 미커밋."
    assert board._drafts_ignore_declared(
        _git(["show", "HEAD:.gitignore"], board_dir).stdout) is True
    assert _git(["status", "--porcelain"], board_dir).stdout.strip() == ""


@requires_git
def test_user_edited_gitignore_is_not_swept_into_mutation(board, tmp_path):
    """사용자가 편집 중인 `.gitignore` 는 티켓 mutation 이 **대신 커밋하지 않는다**.

    이미 정합인 파일은 ensure 가 no-write 라 pathspec 에도 오르지 않는다(누출 동형 차단).
    """
    board_dir = _make_board_dir(board, tmp_path, real_git=True, ticket="T-0001")
    ignore = board_dir / ".gitignore"
    ignore.write_text(board._BOARD_GITIGNORE_BLOCK, encoding="utf-8")
    _git(["add", "-A"], board_dir)
    _git(["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "seed ignore"],
         board_dir)
    ignore.write_text(board._BOARD_GITIGNORE_BLOCK + "# 작업 중인 내 규칙\n*.wip\n",
                      encoding="utf-8")
    ticket = board_dir / "tickets" / "open" / "T-0001-t.md"
    ticket.write_text(ticket.read_text(encoding="utf-8") + "\nedited\n", encoding="utf-8")

    assert board._board_git_stage_and_commit("mutation", (ticket,)) is True

    assert "*.wip" not in _git(["show", "HEAD:.gitignore"], board_dir).stdout, \
        "사용자의 미완성 .gitignore 편집이 mutation 커밋에 실림 — 누출 동형."
    porcelain = _git(["status", "--porcelain", "--", ".gitignore"], board_dir).stdout
    assert porcelain.rstrip("\n") == " M .gitignore", \
        f"사용자 편집이 미커밋 그대로 남아 있어야 한다: {porcelain!r}"


@requires_git
def test_backfill_commit_excludes_drafts(board, tmp_path):
    """보강분을 싣는 커밋에 미충전 draft 가 딸려가지 않는다 (금지 구역 유지)."""
    board_dir = _make_board_dir(board, tmp_path, real_git=True, ticket="T-0001")
    _write_draft(board_dir)
    ticket = board_dir / "tickets" / "open" / "T-0001-t.md"
    ticket.write_text(ticket.read_text(encoding="utf-8") + "\nedited\n", encoding="utf-8")

    assert board._board_git_stage_and_commit("mutation", (ticket,)) is True

    tracked = _head_files(board_dir)
    assert ".gitignore" in tracked
    assert not [t for t in tracked if t.startswith("tickets/.drafts")], \
        f"커밋에 draft 유출: {tracked}"


@requires_git
def test_ensure_fail_soft_on_non_utf8_gitignore(board, tmp_path):
    """비-UTF8 `.gitignore` 는 보강을 포기할 뿐 mutation commit 을 깨지 않는다 (fail-soft)."""
    board_dir = _make_board_dir(board, tmp_path, real_git=True)
    raw = b"\xff\xfe tickets/.drafts/\n"
    (board_dir / ".gitignore").write_bytes(raw)

    assert board._ensure_board_gitignore() is False
    assert (board_dir / ".gitignore").read_bytes() == raw
    assert board._board_git_stage_and_commit("mutation") is True   # commit 은 정상 진행.


# ════════════════════════════════════════════════════════════════════════
# 4. 실측 — 보강 후 draft 가 dirty 관측에서 사라진다 (값-연결)
# ════════════════════════════════════════════════════════════════════════

@requires_git
def test_draft_disappears_from_untracked_observation(board, tmp_path):
    """보강 전 draft 는 untracked 로 보이고, 보강 후에는 안 보인다 (오탐의 역전)."""
    board_dir = _make_board_dir(board, tmp_path, real_git=True, ticket="T-0001")
    draft = _write_draft(board_dir)
    before = _untracked_unignored(board_dir)
    assert any(p.startswith("tickets/.drafts/") for p in before), \
        f"전제 실측 실패 — draft 가 untracked 로 안 보임: {before}"

    ticket = board_dir / "tickets" / "open" / "T-0001-t.md"
    ticket.write_text(ticket.read_text(encoding="utf-8") + "\nedited\n", encoding="utf-8")
    assert board._board_git_stage_and_commit("mutation", (ticket,)) is True

    assert _untracked_unignored(board_dir) == []
    assert _git(["status", "--porcelain"], board_dir).stdout.strip() == ""
    assert draft.is_file(), "draft 파일 자체는 그대로 남아야 한다(ignore 는 삭제가 아니다)."


@requires_git
def test_pm_handoff_dirty_gate_sees_no_draft_residue(board, tmp_path):
    """`pm_handoff` dirty-tree 판정(그 게이트의 실제 seam)이 draft 를 잔여로 보지 않는다.

    소비 지점을 직접 태운다 — board 쪽 관측만 재면 게이트가 다른 축을 본다는 사실을 놓친다.
    """
    handoff = _load_mod("pm_handoff")
    board_dir = _make_board_dir(board, tmp_path, real_git=True, ticket="T-0001")
    _write_draft(board_dir)
    dirty_before = handoff._dirty_paths_in_tree(str(board_dir), handoff._module_run_git)
    assert dirty_before and any(p.startswith("tickets/.drafts/") for p in dirty_before), \
        f"전제 실측 실패 — 게이트가 draft 를 잔여로 안 봄: {dirty_before}"

    ticket = board_dir / "tickets" / "open" / "T-0001-t.md"
    ticket.write_text(ticket.read_text(encoding="utf-8") + "\nedited\n", encoding="utf-8")
    assert board._board_git_stage_and_commit("mutation", (ticket,)) is True

    assert handoff._dirty_paths_in_tree(str(board_dir), handoff._module_run_git) == []
