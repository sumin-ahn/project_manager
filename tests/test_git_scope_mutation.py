"""공유 워킹트리 mutation = 선언된 경로 스코프 — **실 git** hermetic 가드 (ADR-0074·T-0425).

이 클래스(디자인 git blanket `add -A`)가 v1.4.1 까지 아무 테스트에도 안 잡힌 이유는 하나다 —
`tests/test_ticket_finish.py` 가 git seam 을 `run_git_fn=lambda args: (0, "")` 로 주입해
**argv 를 통째로 버렸다**. 그래서 `add -A`(blanket) 인지 스코프된 pathspec 인지 아무도 보지
않았다. 그 공백을 닫으려면 mock 이 아니라 **실 git 리포지토리의 산출물(index·HEAD)** 로
단언해야 한다(`tests/test_board_root.py` A5 · `tests/test_board_git_sync.py` 동형).

가드는 **세 축을 다 물어야 한다** — 좁히기는 양쪽으로 무너질 수 있고, 실제로 두 리뷰 게이트가
각각 반대편을 찾았다(codex=과다 stage / reviewer=누락·거짓 보고):
  1. **과다** — 스코프 *밖* 남의 것이 실리면 red. 디렉토리 pathspec 은 "선언대로" 이면서 동시에
     "남의 것 포함" 이 성립하는 함정이라, 디렉토리가 pathspec 에 살아남지 못함을 구조로 박는다.
  2. **누락** — 엔진이 *자기가 만든 산출물* 을 빠뜨리면 red. legacy(board 미분리·출하 템플릿
     기본)와 board 분리 형상 **둘 다** 에서 본다.
  3. **거짓 안심** — 보고 채널이 죽었는데 "이상 없음" 이 나오면 red. stage 판정기(board 모듈)가
     못 뜬 형상에서 stage 0 인데 "잔여 없음" 이 나오던 것이 실측 결함이었다.

**hermetic 필수**: 모듈 경로 전역(`REPO`)은 import 시점에 실 repo 절대경로로 굳으므로 함수
scope 로 새 모듈을 로드해 tmp 로 재지정한다. 실 git 이 필요한 케이스는 git 부재 환경에서 skip.
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
    reason="git 바이너리 부재 — 실 git 스코프 가드 skip(순수 프리미티브 단위테스트는 항상 실행).",
)

# hermetic commit 을 위한 결정적 author/committer (실 사용자 config 불요·test_board_git_sync 동형).
_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
}


def _load(name: str):
    """tools/ 의 모듈을 경로 로드한다 (패키지 아님·다른 board 테스트와 동일 규약)."""
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, encoding="utf-8", errors="replace", check=False)


def _git_init(root: Path) -> None:
    _git(["init", "-q", "-b", "main"], root)


def _git_commit_all(root: Path, message: str = "init") -> None:
    _git(["add", "-A"], root)
    _git(["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", message], root)


def _staged(repo: Path) -> set[str]:
    """index 에 올라간 경로 집합.

    `--no-renames` 필수 — rename 검출이 켜져 있으면 claimed→done 이동이 `R` 한 줄로 접혀
    **옛 경로(삭제)가 목록에서 사라진다**(그 상태로는 "이동이 통째로 실렸는가" 를 못 묻는다).
    `-z` 도 필수 — 기본 출력은 비-ASCII 경로를 인용 + 8진 이스케이프로 내서 실경로 비교가
    깨진다(엔진이 닫은 결함과 **같은 클래스가 테스트 헬퍼에서 재발**했다·실측으로 발견).
    """
    out = _git(["diff", "--cached", "--name-only", "--no-renames", "-z"], repo).stdout
    return {token for token in out.split("\0") if token}


def _untracked(repo: Path) -> set[str]:
    out = _git(["ls-files", "--others", "--exclude-standard", "-z"], repo).stdout
    return {token for token in out.split("\0") if token}


# ════════════════════════════════════════════════════════════════════════
# repo-중립 프리미티브 (board.py) — 판정 단일 구현
# ════════════════════════════════════════════════════════════════════════

@pytest.fixture
def board():
    return _load("board")


def test_scope_pathspec_drops_paths_outside_repo(board, tmp_path):
    """repo 밖 경로는 pathspec 에서 빠진다 (두-git 형상의 코드 worktree touches·파생물)."""
    repo = tmp_path / "home"
    repo.mkdir()
    inside = repo / "wiki" / "a.md"
    outside = tmp_path / "elsewhere" / "b.md"
    assert board.git_scope_pathspec(repo, [inside, outside]) == ("wiki/a.md",)


def test_scope_pathspec_dedups_preserving_order(board, tmp_path):
    repo = tmp_path / "home"
    repo.mkdir()
    a, b = repo / "a.md", repo / "b.md"
    assert board.git_scope_pathspec(repo, [a, b, a]) == ("a.md", "b.md")


def test_scope_pathspec_excludes_prefixes(board, tmp_path):
    """금지 구역(board-git 의 `tickets/.drafts/`)은 호출부가 prefix 로 준다 — repo 고유분."""
    repo = tmp_path / "b"
    repo.mkdir()
    keep = repo / "tickets" / "open" / "T-0001-x.md"
    drop = repo / "tickets" / ".drafts" / "T-0002-y.md"
    got = board.git_scope_pathspec(repo, [keep, drop],
                                   exclude_prefixes=("tickets/.drafts/",))
    assert got == ("tickets/open/T-0001-x.md",)


@requires_git
def test_scope_stageable_filters_unknown_paths_and_keeps_deleted_tracked(board, tmp_path):
    """미존재·미추적 경로만 걸러진다 — 추적 중이던 *삭제* 경로는 남는다(삭제도 커밋돼야 한다)."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git_init(repo)
    (repo / "kept.md").write_text("k\n", encoding="utf-8")
    (repo / "deleted.md").write_text("d\n", encoding="utf-8")
    _git_commit_all(repo)
    (repo / "deleted.md").unlink()

    got = board.git_scope_stageable(repo, ("kept.md", "deleted.md", "never/existed.md"))
    assert got == ("kept.md", "deleted.md")


@requires_git
def test_scope_stageable_never_returns_a_directory(board, tmp_path):
    """**디렉토리는 pathspec 에 살아남지 못한다** — 변경 파일로 펼쳐진다 (누출 채널 구조 폐쇄).

    `git add -A -- <dir>` 는 그 디렉토리 아래 **남의 미완성 편집까지** stage 한다. 그래서
    스코프 산출은 디렉토리를 그대로 통과시키지 않는다 — 다음 사람이 편의로 디렉토리를 넣어도
    파일 단위로 펼쳐져, 무엇이 실리는지 호출부 출력에 그대로 드러난다.
    """
    repo = tmp_path / "r"
    repo.mkdir()
    _git_init(repo)
    (repo / "docs").mkdir()
    (repo / "docs" / "kept.md").write_text("v1\n", encoding="utf-8")
    (repo / "docs" / "quiet.md").write_text("v1\n", encoding="utf-8")
    _git_commit_all(repo)
    (repo / "docs" / "kept.md").write_text("v2\n", encoding="utf-8")     # 변경됨
    (repo / "docs" / "fresh.md").write_text("new\n", encoding="utf-8")   # untracked

    got = board.git_scope_stageable(repo, ("docs",))
    assert "docs" not in got, f"디렉토리가 pathspec 에 살아남음(누출 채널): {got}"
    assert set(got) == {"docs/kept.md", "docs/fresh.md"}   # 변경분만·파일 단위
    assert "docs/quiet.md" not in got                      # 변경 없는 파일은 대상 아님


@requires_git
def test_scope_stageable_expands_brand_new_untracked_directory(board, tmp_path):
    """**통째로 새 untracked 디렉토리**도 파일로 펼쳐진다 — 전개가 조용히 실패하던 자리.

    `git status --porcelain` 기본값(`untracked-files=normal`)은 새 디렉토리를 `?? newdir/`
    **한 항목으로 접어서** 낸다 — 그러면 전개 결과가 파일이 아니라 *디렉토리* 라 그대로
    pathspec 에 남고 `git add -A -- newdir/` 가 되어, 막으려던 뭉뚱그리기가 되살아난다(PM 실
    git 재현). 기존 tracked 디렉토리만 검사하던 가드는 이 자리를 못 봤다 — **가드가 구현이
    성공하는 입력만 고르면 버그가 있는 지점엔 테스트가 없다.**
    """
    repo = tmp_path / "r"
    repo.mkdir()
    _git_init(repo)
    (repo / "seed.md").write_text("s\n", encoding="utf-8")
    _git_commit_all(repo)
    fresh = repo / "newdir"
    fresh.mkdir()                                   # 커밋에 없던 완전 신규 디렉토리
    (fresh / "mine.md").write_text("m\n", encoding="utf-8")
    (fresh / "deep").mkdir()
    (fresh / "deep" / "nested.md").write_text("n\n", encoding="utf-8")

    got = board.git_scope_stageable(repo, ("newdir",))
    assert "newdir" not in got and "newdir/" not in got, \
        f"신규 untracked 디렉토리가 pathspec 에 살아남음(뭉뚱그리기 부활): {got}"
    assert set(got) == {"newdir/mine.md", "newdir/deep/nested.md"}


@requires_git
def test_scope_stageable_expands_deleted_directory(board, tmp_path):
    """**통째로 삭제된** 디렉토리 선언도 파일 단위로 전개된다 — 선언한 삭제가 빠지면 안 된다.

    옛 게이트는 `is_dir()` 로만 전개를 걸어, 디렉토리가 이미 없으면 전개를 건너뛰었다. 이어지는
    추적 판정도 `ls-files -- src` 가 `src/a.py` 를 내놔 `src` 자체와 매칭되지 않아 경로가 통째로
    탈락 → **scope=()** 로 선언한 삭제가 커밋에서 조용히 빠졌다(reviewer 실측). "선언 경로만
    stage" 는 과다 stage 뿐 아니라 **선언된 삭제 누락 0** 까지여야 성립한다.
    """
    repo = tmp_path / "r"
    repo.mkdir()
    _git_init(repo)
    (repo / "src" / "sub").mkdir(parents=True)
    (repo / "src" / "a.py").write_text("a\n", encoding="utf-8")
    (repo / "src" / "sub" / "b.py").write_text("b\n", encoding="utf-8")
    (repo / "others.py").write_text("o\n", encoding="utf-8")
    _git_commit_all(repo)
    shutil.rmtree(repo / "src")                       # 선언 디렉토리 통째로 삭제
    (repo / "others.py").unlink()                     # 선언 밖 남의 삭제

    got = board.git_scope_stageable(repo, ("src",))
    assert set(got) == {"src/a.py", "src/sub/b.py"}, f"삭제 전개 실패: {got}"
    assert "src" not in got and "src/" not in got     # 디렉토리는 여전히 안 남는다
    assert "others.py" not in got                     # 선언 밖 삭제는 안 실린다


@requires_git
def test_finish_stages_deletion_of_declared_directory(tf, tmp_path, monkeypatch, capsys):
    """`touches` 디렉토리를 통째로 지운 리팩터에서 그 삭제가 전부 stage 된다 (e2e·실 git)."""
    root = _make_home_repo(tmp_path / "home")
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "src" / "pkg" / "old.py").write_text("old\n", encoding="utf-8")
    (root / "src" / "pkg" / "old2.py").write_text("old2\n", encoding="utf-8")
    _git_commit_all(root, "seed pkg")
    shutil.rmtree(root / "src" / "pkg")               # 선언 디렉토리 삭제
    (root / _OTHERS_WIP).write_text("# 남의 편집\n", encoding="utf-8")
    finisher = _make_finisher(tf, root, monkeypatch, touches=["src/pkg"])

    assert finisher.run("T-0001", section=None, dry_run=False) == 0
    staged = _staged(root)
    assert {"src/pkg/old.py", "src/pkg/old2.py"} <= staged, \
        f"선언 디렉토리의 삭제가 stage 안 됨: {sorted(staged)}"
    assert "src/pkg" not in staged
    assert _OTHERS_WIP not in staged                  # 선언 밖 남의 변경은 그대로 미스테이지


@requires_git
def test_scope_stageable_final_gate_drops_directory_even_if_expansion_fails(board, tmp_path):
    """전개가 **실패해도** 디렉토리는 최종 반환에 못 남는다 — 불변식이 전개에 의존하지 않는다.

    전개를 죽인(빈 목록/디렉토리 반환) 러너를 물려 "다음 번 조용한 전개 실패" 를 모사한다.
    이번 결함이 정확히 그 형상이었으므로, 같은 클래스가 또 나와도 마지막 관문이 잡아야 한다.
    """
    repo = tmp_path / "r"
    repo.mkdir()
    _git_init(repo)
    (repo / "docs").mkdir()
    (repo / "docs" / "a.md").write_text("a\n", encoding="utf-8")

    # 전개가 디렉토리(또는 `dir/` 표기)를 그대로 돌려주는 고장 모드.
    monkeyed = board._git_scope_expand_dir
    try:
        board._git_scope_expand_dir = lambda repo_, rel, run: [rel, rel + "/"]
        got = board.git_scope_stageable(repo, ("docs",))
    finally:
        board._git_scope_expand_dir = monkeyed
    assert got == (), f"전개 실패 시 디렉토리가 새어나감: {got}"


@requires_git
def test_scope_stageable_drops_paths_inside_nested_git(board, tmp_path):
    """중첩 git(서브모듈) 내부 경로는 제거된다 — 상위 repo `add` 의 rc=128 fatal 방지.

    board 분리 형상에서 `touches` 에 `.project_manager/board/...` 를 적으면 상위 repo 의
    `git add` 가 `fatal: … is in submodule` 로 죽어 **stage 전체가 실패** 했다(그 fatal 은
    finish 를 rc=1 로 중단시킨다). 대조로 raw add 의 fatal 을 같은 트리에서 실측한다.
    """
    repo = tmp_path / "r"
    repo.mkdir()
    _git_init(repo)
    (repo / "code.py").write_text("x = 1\n", encoding="utf-8")
    inner = repo / "nested"
    inner.mkdir()
    _git_init(inner)
    (inner / "in.md").write_text("i\n", encoding="utf-8")
    _git_commit_all(inner)
    _git_commit_all(repo)
    (repo / "code.py").write_text("x = 2\n", encoding="utf-8")
    (inner / "in.md").write_text("i2\n", encoding="utf-8")

    # 판정은 rc 로만 한다 — git 메시지는 로케일 번역이라(한국어 실측) 문자열 매칭 금물.
    raw = _git(["add", "-A", "--", "code.py", "nested/in.md"], repo)
    assert raw.returncode != 0, "서브모듈 내부 pathspec 이 fatal 이 아님 — 전제 붕괴"
    assert _staged(repo) == set(), "fatal 인데 일부가 stage 됨 — 전제 붕괴"

    got = board.git_scope_stageable(repo, ("code.py", "nested/in.md"))
    assert got == ("code.py",)
    assert _git(["add", "-A", "--", *got], repo).returncode == 0
    assert _staged(repo) == {"code.py"}


@requires_git
def test_scope_stageable_drops_gitignored_paths(board, tmp_path):
    """gitignored 경로는 제거된다 — **명시 pathspec 이 ignored 면 `add` 가 rc=1 에러**다.

    광역 `add -A` 는 ignored 를 조용히 건너뛰지만 pathspec 을 명시하면 에러이고
    `--ignore-errors` 로도 안 없어진다. 대조로 raw add 의 rc=1 을 같은 트리에서 실측한다.
    **추적 중인 파일은 ignore 규칙이 있어도 남아야 한다**(check-ignore 가 추적 파일을 보고하지
    않는 동작에 기댄다).
    """
    repo = tmp_path / "r"
    repo.mkdir()
    _git_init(repo)
    (repo / ".gitignore").write_text("local.conf\nboard.md\n", encoding="utf-8")
    (repo / "normal.md").write_text("n\n", encoding="utf-8")
    (repo / "board.md").write_text("tracked despite ignore\n", encoding="utf-8")
    _git(["add", "-f", "--", ".gitignore", "normal.md", "board.md"], repo)
    _git(["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"], repo)
    (repo / "local.conf").write_text("py=python3\n", encoding="utf-8")   # ignored·미추적
    (repo / "normal.md").write_text("n2\n", encoding="utf-8")
    (repo / "board.md").write_text("changed\n", encoding="utf-8")        # ignored 규칙·추적됨

    # 대조 실측: rc=1 이다(정상 경로는 stage 되지만 **rc≠0** 이라 호출부가 `[중단]` 으로 죽는다 —
    # pathspec 미매치의 rc=128 과 종류만 다를 뿐 결과는 같은 '기록 절반 + 보고 침묵'이다).
    raw = _git(["add", "-A", "--", "normal.md", "local.conf"], repo)
    assert raw.returncode != 0, "ignored 명시 pathspec 인데 add 가 성공 — 전제 붕괴"
    _git(["reset", "-q"], repo)      # 대조 실측이 남긴 index 를 되돌린다(이 tmp repo 한정)

    got = board.git_scope_stageable(repo, ("normal.md", "local.conf", "board.md"))
    assert "local.conf" not in got, f"gitignored 경로가 pathspec 에 남음: {got}"
    assert "normal.md" in got
    assert "board.md" in got, "추적 중인 파일이 ignore 규칙 때문에 빠짐(과잉 제외)"
    assert _git(["add", "-A", "--", *got], repo).returncode == 0
    assert _staged(repo) == {"normal.md", "board.md"}


@requires_git
def test_finish_survives_gitignored_touches(tf, tmp_path, monkeypatch, capsys):
    """`touches` 에 gitignored 경로가 섞여도 finish 가 죽지 않고 **loud 보고까지 도달**한다.

    필터가 없으면 `[4/5] [중단] git add 실패 (rc=1)` 로 rc=1 종료 — board complete 는 이미
    끝난 절반 상태인데 잔여 loud 보고도 [5/5] 안내도 안 나온다. 이 티켓이 만든 안전장치가
    가장 필요한 순간에 사라진다(reviewer 실측). 실형상 도달 가능 — 이 보드의 done 티켓 여러
    건이 `touches: .project_manager/local.conf`(gitignored)를 선언한다.
    """
    root = _make_home_repo(tmp_path / "home")
    (root / ".project_manager" / ".gitignore").write_text("local.conf\n", encoding="utf-8")
    _git_commit_all(root, "add gitignore")
    (root / ".project_manager" / "local.conf").write_text("py=python3\n", encoding="utf-8")
    (root / _TOUCHED_FILE).write_text("x = 2\n", encoding="utf-8")
    finisher = _make_finisher(
        tf, root, monkeypatch,
        touches=[_TOUCHED_FILE, ".project_manager/local.conf"])

    rc = finisher.run("T-0001", section=None, dry_run=False)
    out = capsys.readouterr().out
    assert rc == 0, f"gitignored touches 로 기록이 rc=1 로 죽었다:\n{out}"
    staged = _staged(root)
    assert _TOUCHED_FILE in staged and _LOG_FILE in staged
    assert ".project_manager/local.conf" not in staged
    assert "local.conf" not in out.split("[5/5]")[0].split("✓ git add")[-1] \
        if "✓ git add" in out else True         # stage 목록에 ignored 경로가 없다
    assert "[5/5] PM 이 손으로 할 잔여 작업" in out    # loud 채널이 살아 있다
    assert "스코프 밖 잔여 변경 없음" in out or "미스테이지 잔여" in out


@requires_git
def test_scope_stage_pathspec_survives_unmatched_path_in_real_git(board, tmp_path):
    """미매치 경로가 섞여도 `add` 가 rc=128 fatal 로 죽지 않는다 (필터가 앞단에서 제거).

    필터 없이 `git add -A -- never/existed.md` 를 주면 git 이 fatal 로 죽고 **아무것도 stage
    되지 않는다** — 그 실패 모드를 같은 repo 에서 대조로 실측해 필터의 존재 이유를 못박는다.
    """
    repo = tmp_path / "r"
    repo.mkdir()
    _git_init(repo)
    (repo / "kept.md").write_text("k\n", encoding="utf-8")

    raw = _git(["add", "-A", "--", "kept.md", "never/existed.md"], repo)
    assert raw.returncode != 0, "미매치 pathspec 인데 git 이 살아남음 — 전제 붕괴"
    assert _staged(repo) == set()

    pathspec = board.git_scope_stage_pathspec(
        repo, [repo / "kept.md", repo / "never" / "existed.md"])
    assert pathspec == ("kept.md",)
    assert _git(["add", "-A", "--", *pathspec], repo).returncode == 0
    assert _staged(repo) == {"kept.md"}


# ════════════════════════════════════════════════════════════════════════
# 잔여 판정 (ticket_finish 자체 파서) — index 열 · worktree 열
# ════════════════════════════════════════════════════════════════════════

@pytest.fixture
def tf():
    return _load("ticket_finish")


def test_split_dirty_sees_staged_out_of_scope(tf):
    """**이미 staged 된** 스코프 밖 변경을 본다 — worktree 열만 보면 안 보이던 누출 갈래."""
    entries = (("M ", "others/wip.md"),     # staged only (Y=공백) — 옛 판정이 놓치던 것
               ("M ", "mine/a.py"),         # staged·스코프 안
               (" M", "others/edit.md"),    # 미스테이지
               ("??", "others/new.md"))     # untracked
    staged_out, unstaged = tf.split_dirty(entries, ("mine/a.py",))
    assert staged_out == ("M  others/wip.md",)
    assert unstaged == (" M others/edit.md", "?? others/new.md")


def test_split_dirty_scope_covers_declared_directory(tf):
    """선언이 디렉토리면 그 아래 경로는 스코프 안으로 본다(보고가 자기 것을 오탐하지 않게)."""
    staged_out, _ = tf.split_dirty((("M ", "src/deep/a.py"),), ("src",))
    assert staged_out == ()


def test_split_dirty_excludes_all_project_manager_local_runtime_state(tf):
    """clone-local 런타임 트리는 파일 종류와 staged 여부에 관계없이 잔여가 아니다."""
    entries = (
        ("??", ".project_manager/.local/log.lock"),
        (" M", ".project_manager/.local/tasks/orch-dev/pm_state.md"),
        ("M ", ".project_manager/.local/worktree-leases.json"),
        ("??", ".project_manager/.local"),
        ("??", ".project_manager/.locality/keep.md"),
    )

    staged_out, unstaged = tf.split_dirty(entries, ("src",))

    assert staged_out == ()
    assert unstaged == ("?? .project_manager/.locality/keep.md",)


def test_parse_porcelain_z_keeps_both_columns_and_rename_target(board):
    """NUL 파서는 XY 두 열을 보존하고 rename 2토큰에서 **신규 경로**를 취한다 (공유 단일 구현)."""
    out = "M  a.md\0 D b.md\0?? c.md\0R  new.md\0old.md\0"
    assert board.git_parse_porcelain_z(out) == (
        ("M ", "a.md"), (" D", "b.md"), ("??", "c.md"), ("R ", "new.md"))


def test_parse_porcelain_z_keeps_non_ascii_and_space_paths_verbatim(board):
    """비-ASCII·공백 경로가 **원문 그대로** 나온다 — 인용/8진 이스케이프 0 (판정 입력이므로 필수)."""
    out = "M  wiki/ideas/0001-멀티-pm-티켓.md\0?? docs/a b.md\0"
    assert board.git_parse_porcelain_z(out) == (
        ("M ", "wiki/ideas/0001-멀티-pm-티켓.md"), ("??", "docs/a b.md"))


@requires_git
def test_status_entries_reads_non_ascii_paths_as_real_paths(tf, board, tmp_path):
    """실 git 에서 한글/공백 경로가 실경로로 읽힌다 — `-z` 없으면 8진 이스케이프가 들어온다.

    이 문자열이 `scope_covers` 비교 입력이라, 인용된 채로 들어오면 매칭이 깨져 *자기가 stage
    한 파일* 이 "스코프 밖" 으로 오보된다(reviewer 실측). 대조로 `-z` 없는 출력이 실제로
    이스케이프됨을 같은 트리에서 실측한다.
    """
    repo = tmp_path / "r"
    repo.mkdir()
    _git_init(repo)
    korean = "한글-파일.md"
    spaced = "a b.md"
    (repo / korean).write_text("k\n", encoding="utf-8")
    (repo / spaced).write_text("s\n", encoding="utf-8")

    raw = _git(["status", "--porcelain"], repo).stdout
    assert "\\355" in raw or '"' in raw, f"인용/이스케이프 전제 붕괴: {raw!r}"

    runner = lambda args: (lambda r: (r.returncode, r.stdout))(   # noqa: E731 — 테스트 러너
        subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                       text=True, encoding="utf-8", errors="replace", check=False))
    paths = {path for _code, path in tf.status_entries(runner, board)}
    assert korean in paths and spaced in paths, f"실경로로 안 읽힘: {paths}"


@requires_git
def test_parsing_seam_returns_stdout_only_while_diagnostic_seam_keeps_stderr(
        tf, tmp_path, monkeypatch):
    """기계 파싱 seam 은 **stdout 만**, 진단 seam 은 stderr 를 보존한다.

    파싱 경로가 stdout+stderr 합본을 먹으면 git 경고 한 줄이 그대로 '잔여 항목'·'추적 경로' 로
    둔갑한다. 관측으로 못박는다 — `git checkout -b` 는 rc=0 인데 안내를 **stderr** 로만 낸다.
    """
    root = _make_home_repo(tmp_path / "home")
    monkeypatch.setattr(tf, "REPO", root)
    finisher = tf.TicketFinisher(log_file=root / ".project_manager" / "wiki" / "log" / "current.md")

    rc_parse, out_parse = finisher._run_git_stdout_fn(["checkout", "-b", "probe-a"])
    assert rc_parse == 0 and out_parse.strip() == "", \
        f"파싱 seam 에 stderr 가 섞였다(가짜 항목 유입 경로): {out_parse!r}"
    rc_diag, out_diag = finisher._run_git_fn(["checkout", "-b", "probe-b"])
    assert rc_diag == 0 and "probe-b" in out_diag, \
        f"진단 seam 이 stderr 를 잃었다(실패 원인 표시 불가): {out_diag!r}"


@requires_git
def test_status_entries_ignores_stderr_noise_from_git(tf, tmp_path, monkeypatch, capsys):
    """git 이 stderr 로 낸 잡음이 **잔여 목록에 항목으로 섞이지 않는다** (파싱 seam 사용 확인).

    두 seam 을 서로 다르게 주입한다 — 합본 seam 에는 stderr 잡음을, 파싱 seam 에는 깨끗한
    출력을. 보고가 합본 seam 을 쓰면 잡음 줄이 잔여 목록에 뜬다.
    """
    root = _make_home_repo(tmp_path / "home")
    monkeypatch.setattr(tf, "REPO", root)
    monkeypatch.setattr(tf, "get_ticket_touches", lambda board_py, tid: [])
    noise = "warning: LF will be replaced by CRLF in noisy.md"

    finisher = tf.TicketFinisher(
        run_pytest_fn=lambda: (0, "1 passed in 0.1s"),
        run_board_fn=lambda args: (0, "ok"),
        run_git_fn=lambda args: (0, f"{noise}\n M real.md\0"),        # 합본(진단) seam
        run_git_stdout_fn=lambda args: (0, " M real.md\0"),           # 파싱 seam
        board_count_fn=lambda: 1,
        ticket_title_fn=lambda tid: "t",
        affected_domain_fn=lambda tid: None,
        log_file=root / ".project_manager" / "wiki" / "log" / "current.md",
    )
    assert finisher.run("T-0001", section=None, dry_run=False) == 0
    out = capsys.readouterr().out
    assert "real.md" in out
    assert noise not in out, "stderr 잡음이 잔여 목록에 항목으로 섞였다"


@requires_git
def test_status_entries_degraded_branch_reports_real_paths(tf, tmp_path):
    """board 미로드(degraded) 갈래도 **실경로**로 보고한다 — 8진 이스케이프 잔존 금지.

    판정엔 안 쓰이는 갈래지만, 여기만 인용/이스케이프가 남으면 방금 닫은 증상(복붙 불가)이
    이 경로에서 되살아난다.
    """
    repo = tmp_path / "r"
    repo.mkdir()
    _git_init(repo)
    (repo / "남의-한글-WIP.py").write_text("x\n", encoding="utf-8")

    runner = lambda args: (lambda r: (r.returncode, r.stdout))(   # noqa: E731 — 테스트 러너
        subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                       text=True, encoding="utf-8", errors="replace", check=False))
    paths = {path for _code, path in tf.status_entries(runner, None)}   # board=None → degraded
    assert "남의-한글-WIP.py" in paths, f"degraded 갈래가 이스케이프 경로를 냄: {paths}"


def test_status_entries_degraded_branch_consumes_rename_tokens(tf):
    """degraded 갈래도 rename **2토큰**을 소비한다 — 원본 토큰이 가짜 항목으로 뜨면 안 된다.

    `-z` 의 rename 항목은 `<코드> <신규>\\0<원본>\\0` 이라, 원본 토큰을 안 삼키면 코드 없는
    (`ol`, `d.md` 같은) 쓰레기 항목이 잔여 목록에 뜬다. 주 파서는 이미 처리한다 — 이 갈래만
    빠져 있었다.
    """
    out = "R  new.md\0old.md\0 M kept.md\0"
    entries = tf.status_entries(lambda args: (0, out), None)
    assert entries == (("R ", "new.md"), (" M", "kept.md"))


@requires_git
def test_status_entries_expands_new_untracked_directory(tf, board, tmp_path):
    """잔여 보고도 `-uall` — 새 untracked 디렉토리가 `?? dir/` 로 접히면 무엇이 빠졌는지 안 보인다."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git_init(repo)
    (repo / "seed.md").write_text("s\n", encoding="utf-8")
    _git_commit_all(repo)
    (repo / "raw" / "spikes").mkdir(parents=True)
    (repo / "raw" / "spikes" / "new.md").write_text("n\n", encoding="utf-8")

    runner = lambda args: (lambda r: (r.returncode, r.stdout))(   # noqa: E731 — 테스트 러너
        subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                       text=True, encoding="utf-8", errors="replace", check=False))
    paths = {path for _code, path in tf.status_entries(runner, board)}
    assert "raw/spikes/new.md" in paths, f"디렉토리로 접혀 파일이 안 보임: {paths}"
    assert "raw/" not in paths


# ════════════════════════════════════════════════════════════════════════
# ticket_finish stage — 축 1(과다) · 축 2(누락) · 축 3(거짓 안심)
# ════════════════════════════════════════════════════════════════════════
#
# PM 홈 모사 트리를 실 git 으로 세우고 finish 를 돌린다. mock 인 것은 pytest/board CLI/티켓
# 조회뿐 — **git 은 실물**이고, 판정도 실 board 프리미티브다.

_TOUCHED_FILE = "src/a.py"
_OTHERS_WIP = ".project_manager/wiki/roadmap.md"          # 남의 편집(추적 파일)
_OTHERS_UNTRACKED = ".project_manager/wiki/raw/spikes/other-spike.md"
_OTHERS_ADR = ".project_manager/wiki/decisions/0002-other-draft.md"  # 산출물 *디렉토리 안쪽*
_OTHERS_DOMAIN = ".project_manager/wiki/domain/other-page.md"
_LOG_FILE = ".project_manager/wiki/log/current.md"


def _make_home_repo(root: Path) -> Path:
    """PM 홈 형상(wiki 산출물 + 코드)의 실 git 리포지토리를 만든다 (초기 커밋 clean)."""
    wiki = root / ".project_manager" / "wiki"
    for sub in ("log", "decisions", "domain", "raw/spikes"):
        (wiki / sub).mkdir(parents=True, exist_ok=True)
    (root / "src").mkdir(parents=True)
    (wiki / "log" / "current.md").write_text("# 로그\n", encoding="utf-8")
    (wiki / "decisions" / "0001-x.md").write_text("# ADR\n", encoding="utf-8")
    (wiki / "domain" / "core.md").write_text("# domain\n", encoding="utf-8")
    # raw/spikes 를 *추적 상태*로 seed 한다 — git 은 untracked 디렉토리를 `?? dir/` 로 접어
    # 보고하므로, 디렉토리가 새것이면 잔여 보고에 파일명이 안 뜬다(보고 단언이 무뎌진다).
    (wiki / "raw" / "spikes" / "README.md").write_text("# spikes\n", encoding="utf-8")
    (wiki / "architecture.md").write_text("# arch\n", encoding="utf-8")
    (wiki / "status.md").write_text("# status\n", encoding="utf-8")
    (root / _OTHERS_WIP).write_text("# roadmap\n", encoding="utf-8")
    (root / _TOUCHED_FILE).write_text("x = 1\n", encoding="utf-8")
    _git_init(root)
    _git_commit_all(root)
    return root


def _dirty_the_tree(root: Path) -> None:
    """무관한 남의 변경을 심는다 — 추적 편집 · untracked · **엔진 산출물 디렉토리 안쪽** 2건."""
    (root / _TOUCHED_FILE).write_text("x = 2\n", encoding="utf-8")          # 내 작업(touches)
    (root / _OTHERS_WIP).write_text("# roadmap — 남이 편집 중\n", encoding="utf-8")
    (root / _OTHERS_UNTRACKED).write_text("# 남의 spike\n", encoding="utf-8")
    (root / _OTHERS_ADR).write_text("# 남이 쓰다 만 ADR\n", encoding="utf-8")
    (root / _OTHERS_DOMAIN).write_text("# 남이 쓰다 만 domain 페이지\n", encoding="utf-8")


def _make_finisher(tf, root: Path, monkeypatch, *, touches: list[str],
                   board_py: Path | None = None):
    """실 git(root)에서 도는 TicketFinisher — git 만 실물, 나머지는 DI 대역.

    `REPO` 를 tmp 홈으로 재지정하면 (1) 기본 git 러너의 cwd, (2) board 모듈 재-앵커
    (`board_root()`/`tickets_dir()`/`_board_git_enabled()`)가 모두 그 트리를 따라온다.
    티켓 `touches` 조회만 stub 한다 — 실 보드가 tmp 에 없을 수 있어서다(스코프 계산·필터·
    실제 `git add`·잔여 판정은 전부 실 코드 경로).
    """
    monkeypatch.setattr(tf, "REPO", root)
    monkeypatch.setattr(tf, "get_ticket_touches", lambda board_py, tid: list(touches))
    for key, val in _GIT_IDENTITY.items():
        monkeypatch.setenv(key, val)
    kwargs = {} if board_py is None else {"board_py": board_py}
    return tf.TicketFinisher(
        run_pytest_fn=lambda: (0, "100 passed, 0 deselected in 0.1s"),
        run_board_fn=lambda args: (0, "board ok"),
        board_count_fn=lambda: 10,
        ticket_title_fn=lambda tid: "스코프 테스트 티켓",
        affected_domain_fn=lambda tid: None,
        log_file=root / ".project_manager" / "wiki" / "log" / "current.md",
        **kwargs,
    )


# ── 축 1: 과다 stage (누출) ──────────────────────────────────────────────

@requires_git
def test_finish_stages_only_declared_paths(tf, tmp_path, monkeypatch, capsys):
    """선언(touches + 이 실행이 쓴 산출물)만 stage · 남의 것은 미스테이지로 남는다.

    특히 **엔진 산출물 디렉토리(decisions/·domain/) 안쪽의 남의 WIP** 가 실리면 red 다 —
    그 디렉토리들을 스코프에 통째로 넣으면 "선언대로" 이면서 동시에 "남의 것 포함" 이 성립해
    격리가 무효가 된다(codex must-fix). 그것들은 *다른 실행* 의 산출물이라 선언분이 아니다.
    """
    root = _make_home_repo(tmp_path / "home")
    _dirty_the_tree(root)
    finisher = _make_finisher(tf, root, monkeypatch, touches=[_TOUCHED_FILE])

    rc = finisher.run("T-0001", section=None, dry_run=False)
    assert rc == 0

    staged = _staged(root)
    assert staged == {_TOUCHED_FILE, _LOG_FILE}, f"스코프 밖이 함께 stage 됨: {sorted(staged)}"
    for leaked in (_OTHERS_WIP, _OTHERS_UNTRACKED, _OTHERS_ADR, _OTHERS_DOMAIN):
        assert leaked not in staged, f"남의 변경이 stage 됨(누출): {leaked}"
    assert _OTHERS_UNTRACKED in _untracked(root)


@requires_git
def test_finish_expands_declared_directory_into_files(tf, tmp_path, monkeypatch, capsys):
    """`touches` 가 디렉토리여도 pathspec 엔 **파일만** 나간다 — 무엇이 실리는지 출력에 뜬다.

    디렉토리를 그대로 `add -A -- <dir>` 하면 무엇이 실렸는지 아무도 모른 채 넘어간다. 파일로
    펼치면 (a) 선언 디렉토리 밖은 못 들어오고 (b) 들어온 파일이 목록으로 보인다.
    """
    root = _make_home_repo(tmp_path / "home")
    _dirty_the_tree(root)
    finisher = _make_finisher(tf, root, monkeypatch, touches=["src"])

    assert finisher.run("T-0001", section=None, dry_run=False) == 0
    out = capsys.readouterr().out
    staged = _staged(root)
    assert _TOUCHED_FILE in staged                       # 디렉토리 아래 변경 파일은 실린다
    assert _OTHERS_WIP not in staged                     # 선언 디렉토리 밖은 안 실린다
    assert f"      {_TOUCHED_FILE}" in out               # 파일 단위로 출력
    assert "      src\n" not in out                      # 디렉토리 자체는 pathspec 이 아니다


@requires_git
def test_finish_expands_brand_new_untracked_directory_touches(tf, tmp_path, monkeypatch,
                                                              capsys):
    """`touches` 가 **통째로 새 untracked 디렉토리** 여도 디렉토리가 pathspec 에 안 남는다.

    이 자리가 라운드 3 결함이다 — `status --porcelain` 기본값이 새 디렉토리를 `?? newpkg/` 로
    접어, 전개가 디렉토리를 그대로 돌려주고 `git add -A -- newpkg/` 로 **뭉뚱그려** stage 됐다.
    기존 tracked 디렉토리 케이스만 있던 가드는 이 입력을 안 골라서 통과했다.

    **선언 경계 semantics**: 선언 디렉토리 *안* 은 사람이 그은 범위라 그 아래 변경 파일이 실린다
    (엔진은 그 안에서 소유자를 구분할 수 없다). 대신 **파일 단위로 펼쳐 전부 출력** 해 무엇이
    실렸는지 PM 이 보게 하고, 선언 디렉토리 *밖* 은 절대 안 실린다.
    """
    root = _make_home_repo(tmp_path / "home")
    _dirty_the_tree(root)
    fresh = root / "newpkg"                     # 커밋에 없던 완전 신규 디렉토리
    fresh.mkdir()
    (fresh / "mine.py").write_text("m = 1\n", encoding="utf-8")
    (fresh / "sub").mkdir()
    (fresh / "sub" / "deep.py").write_text("d = 1\n", encoding="utf-8")
    finisher = _make_finisher(tf, root, monkeypatch, touches=["newpkg"])

    assert finisher.run("T-0001", section=None, dry_run=False) == 0
    out = capsys.readouterr().out
    staged = _staged(root)
    assert "newpkg" not in staged and "newpkg/" not in staged, \
        f"디렉토리가 그대로 stage 됨(뭉뚱그리기 부활): {sorted(staged)}"
    assert {"newpkg/mine.py", "newpkg/sub/deep.py"} <= staged
    # 선언 디렉토리 *밖* 남의 것은 실리지 않는다(과다 stage 축).
    for outside in (_OTHERS_WIP, _OTHERS_ADR, _OTHERS_DOMAIN, _OTHERS_UNTRACKED):
        assert outside not in staged, f"선언 디렉토리 밖이 stage 됨: {outside}"
    # 무엇이 실렸는지 **파일 단위로** 출력된다(디렉토리 한 줄로 뭉뚱그리지 않는다).
    assert "      newpkg/mine.py" in out and "      newpkg/sub/deep.py" in out
    assert "      newpkg\n" not in out


@requires_git
def test_finish_lists_every_file_inside_declared_new_directory(tf, tmp_path, monkeypatch,
                                                               capsys):
    """선언 디렉토리 **안** 의 무관 파일은 실리되 **한 줄씩 이름이 찍힌다**(뭉뚱그리기 0).

    엔진은 선언 디렉토리 안에서 "내 파일"과 "남의 파일"을 구분할 수단이 없다 — 디렉토리 선언은
    사람이 그은 경계이고, 경계를 좁히는 건 티켓 authoring 의 책임이다(ADR-0074 §Consequences).
    그래서 이 축의 방어는 *배제* 가 아니라 **가시성** 이다: 접힌 `newpkg/` 한 줄이 아니라 실제
    실리는 파일이 전부 출력돼, PM 이 커밋 전에 남의 것을 본다.
    (배제까지 원하면 선언 디렉토리를 **거부** 하는 선택지가 있고, 그건 solo/단일-repo 채택자의
    `touches: [tests/]` 를 통째로 under-stage 시키는 트레이드오프다 — 결정 사항.)
    """
    root = _make_home_repo(tmp_path / "home")
    fresh = root / "newpkg"
    fresh.mkdir()
    (fresh / "mine.py").write_text("m = 1\n", encoding="utf-8")
    (fresh / "남의-미완성.py").write_text("other = 1\n", encoding="utf-8")
    finisher = _make_finisher(tf, root, monkeypatch, touches=["newpkg"])

    assert finisher.run("T-0001", section=None, dry_run=False) == 0
    out = capsys.readouterr().out
    staged = _staged(root)
    assert "newpkg" not in staged, f"디렉토리로 뭉뚱그려 stage 됨: {sorted(staged)}"
    # 파일 단위 pathspec — 실린 항목이 전부 개별 경로로 드러난다(git 이 non-ASCII 를 quote 해도
    # `diff --cached` 는 실경로를 낸다).
    assert len(staged) == len([p for p in staged if "/" in p or p.endswith(".md")])
    assert "newpkg/mine.py" in staged
    assert "      newpkg/mine.py" in out
    assert f"  ✓ git add — 선언 경로 {len(staged)}개만 stage" in out


@requires_git
def test_finish_does_not_misreport_own_non_ascii_paths(tf, tmp_path, monkeypatch, capsys):
    """한글 경로를 stage 해 놓고 그걸 "스코프 밖 — 빼라" 로 오보하지 않는다 (판정 입력 정합).

    `status --porcelain` 이 비-ASCII 경로를 8진 이스케이프 + 인용으로 내면 그 문자열이
    `scope_covers` 비교에서 스코프와 안 맞아, **방금 자기가 stage 한 파일**을 남의 것으로
    분류하고 `git restore --staged` 를 지시한다(reviewer 실측 · PM 홈에 한글 아이디어/티켓
    경로 실재). 목록이 8진이라 복붙도 안 된다.
    """
    root = _make_home_repo(tmp_path / "home")
    korean = "src/한글-모듈.py"
    spaced = "src/공백 있는 파일.md"
    (root / korean).write_text("k = 1\n", encoding="utf-8")
    (root / spaced).write_text("s\n", encoding="utf-8")
    finisher = _make_finisher(tf, root, monkeypatch, touches=[korean, spaced])

    assert finisher.run("T-0001", section=None, dry_run=False) == 0
    out = capsys.readouterr().out
    staged = _staged(root)
    assert korean in staged and spaced in staged
    assert "스코프 밖 staged" not in out, f"자기가 stage 한 한글 경로를 남의 것으로 오보: {out}"
    assert "git restore --staged" not in out
    assert "\\3" not in out, "8진 이스케이프가 그대로 출력됨(복붙 불가)"
    assert "스코프 밖 잔여 변경 없음" in out


@requires_git
def test_finish_reports_preexisting_staged_out_of_scope(tf, tmp_path, monkeypatch, capsys):
    """**사전에 staged 된** 남의 변경을 보고한다 — 이 도구는 commit 을 PM 에게 넘기므로 실린다.

    `add` 만 좁히면 안 닫히는 갈래다(ADR-0074 "add 와 commit 양쪽"): index 에 남의 변경이 이미
    있으면 PM 의 `git commit` 에 그대로 실린다. 옛 보고는 worktree 열만 봐서 이 상태를
    "잔여 없음" 으로 표시했다 — **loud 보고가 조용한 누출을 가려주는** 형상이었다.
    """
    root = _make_home_repo(tmp_path / "home")
    _dirty_the_tree(root)
    _git(["add", "--", _OTHERS_WIP], root)          # 남이 미리 stage 해 둔 변경
    finisher = _make_finisher(tf, root, monkeypatch, touches=[_TOUCHED_FILE])

    assert finisher.run("T-0001", section=None, dry_run=False) == 0
    out = capsys.readouterr().out
    assert "스코프 밖 staged" in out, "사전 staged 누출이 보고에서 안 보임(거짓 안심)"
    assert "roadmap.md" in out
    assert "git restore --staged" in out             # remedy 동봉
    assert "잔여 변경 없음" not in out


# ── 축 2: 누락 (under-stage) ────────────────────────────────────────────

def _make_legacy_board(root: Path, tid: str = "T-0001") -> tuple[Path, Path]:
    """legacy 형상(board 미분리·**출하 템플릿 기본**)의 티켓 트리를 만든다 → (옛 경로, 새 경로)."""
    tickets = root / ".project_manager" / "wiki" / "tickets"
    for status in ("open", "claimed", "done", "blocked"):
        (tickets / status).mkdir(parents=True, exist_ok=True)
    old = tickets / "claimed" / f"{tid}-x.md"
    old.write_text(
        f"---\nid: {tid}\ntitle: t\nstatus: claimed\ntouches: []\n---\n\n# {tid} — t\n\n"
        # 완료 기록 경로는 DoD 게이트를 지난다 — 이 픽스처의 관측 축은 stage 스코프이므로
        # 마감된 DoD 를 심어 게이트를 통과시킨다.
        "## 완료 조건 (Definition of Done)\n- [x] 구현\n",
        encoding="utf-8")
    return old, tickets / "done" / f"{tid}-x.md"


@requires_git
def test_finish_stages_ticket_move_in_legacy_layout(tf, tmp_path, monkeypatch, capsys):
    """legacy 형상에서 **이 실행이 만든 티켓 이동(claimed→done)** 이 stage 된다 (누락 red).

    board 미분리 형상에선 티켓 이동이 홈 git 에 떨어진다 — 엔진 자신의 산출물이므로 선언분이다.
    빠지면 채택자가 매 finish 마다 손으로 `git add` 해야 한다(reviewer 실측 회귀).
    """
    root = _make_home_repo(tmp_path / "home")
    old, new = _make_legacy_board(root)
    _git_commit_all(root, "seed ticket")
    old.rename(new)                                  # board complete 가 한 이동(CLI 는 대역)
    (root / _TOUCHED_FILE).write_text("x = 2\n", encoding="utf-8")
    finisher = _make_finisher(tf, root, monkeypatch, touches=[_TOUCHED_FILE])

    assert finisher.run("T-0001", section=None, dry_run=False) == 0
    staged = _staged(root)
    assert ".project_manager/wiki/tickets/done/T-0001-x.md" in staged, \
        f"legacy 티켓 이동(새 경로)이 stage 안 됨: {sorted(staged)}"
    assert ".project_manager/wiki/tickets/claimed/T-0001-x.md" in staged, \
        f"legacy 티켓 이동(옛 경로 삭제)이 stage 안 됨: {sorted(staged)}"


@requires_git
def test_finish_skips_submodule_ticket_paths_in_split_layout(tf, tmp_path, monkeypatch,
                                                             capsys):
    """board 분리(서브모듈) 형상에선 티켓 경로가 스코프에서 빠지고 **fatal 도 안 난다**.

    티켓이 서브모듈 안이라 상위 repo 의 `add` 가 rc=128 로 죽는다 — 그 이동은 board-git 이
    자기 커밋으로 기록하므로 홈 git 의 선언분이 아니다. `touches` 에 board 경로가 섞여도
    stage 전체가 죽지 않아야 한다.
    """
    root = _make_home_repo(tmp_path / "home")
    board_dir = root / ".project_manager" / "board"
    for status in ("open", "claimed", "done", "blocked"):
        (board_dir / "tickets" / status).mkdir(parents=True, exist_ok=True)
    ticket = board_dir / "tickets" / "done" / "T-0001-x.md"
    ticket.write_text(
        "---\nid: T-0001\ntitle: t\nstatus: done\n---\n\n# T-0001 — t\n\n"
        # 완료 기록 경로의 DoD 게이트 통과분 — 이 픽스처의 관측 축은 stage 스코프다.
        "## 완료 조건 (Definition of Done)\n- [x] 구현\n",
        encoding="utf-8")
    _git_init(board_dir)
    _git_commit_all(board_dir, "board init")         # board/.git 존재 → 분리 형상
    # 상위 repo 의 index 에 gitlink 를 **기록** 해야 실 형상이다 — 그래야 상위 `add` 가
    # 서브모듈 내부 경로에 `fatal: … is in submodule`(rc=128) 을 낸다(미기록 중첩 repo 는
    # 그냥 무시돼 이 가드가 *이유 없이* 통과한다 — 실측으로 확인한 함정).
    _git_commit_all(root, "record board gitlink")
    (root / _TOUCHED_FILE).write_text("x = 2\n", encoding="utf-8")
    finisher = _make_finisher(
        tf, root, monkeypatch,
        touches=[_TOUCHED_FILE, ".project_manager/board/tickets/done/T-0001-x.md"])

    assert finisher.run("T-0001", section=None, dry_run=False) == 0, capsys.readouterr().out
    staged = _staged(root)
    assert _TOUCHED_FILE in staged and _LOG_FILE in staged
    assert not any(p.startswith(".project_manager/board") for p in staged), \
        f"서브모듈 경로가 상위 repo 에 stage 됨: {sorted(staged)}"


@requires_git
def test_finish_survives_touches_absent_from_this_repo(tf, tmp_path, monkeypatch, capsys):
    """이 repo 에 없는 `touches`(두-git 형상의 코드 worktree 경로)가 섞여도 fatal 로 안 죽는다."""
    root = _make_home_repo(tmp_path / "home")
    (root / _TOUCHED_FILE).write_text("x = 2\n", encoding="utf-8")
    finisher = _make_finisher(
        tf, root, monkeypatch,
        touches=[_TOUCHED_FILE, "tests/", "no/such/path.py"])

    assert finisher.run("T-0001", section=None, dry_run=False) == 0
    assert _TOUCHED_FILE in _staged(root)


# ── 축 3: 거짓 안심 (보고 채널) ─────────────────────────────────────────

@requires_git
def test_finish_fails_loud_when_scope_judge_unavailable(tf, tmp_path, monkeypatch, capsys):
    """stage 판정기(board 모듈)를 못 띄우면 **loud** — stage 0 을 조용히 정상처럼 넘기지 않는다.

    실제 형상: 실행 인터프리터엔 PyYAML 이 없고 venv 엔 있어 board *CLI* 는 성공하는데
    `import yaml` 하는 board 모듈 로드만 실패한다. 옛 코드는 scope=() → stage 0, 게다가
    잔여 보고까지 board 에 의존해 `✓ 잔여 없음` 이라는 **거짓 안심**을 냈다(reviewer 실측).
    """
    root = _make_home_repo(tmp_path / "home")
    _dirty_the_tree(root)
    finisher = _make_finisher(tf, root, monkeypatch, touches=[_TOUCHED_FILE],
                              board_py=root / "없는-board.py")

    assert finisher.run("T-0001", section=None, dry_run=False) == 0
    captured = capsys.readouterr()
    assert "스코프를 산출하지 못했다" in captured.err, "판정기 사망이 조용히 넘어감"
    assert _staged(root) == set()
    # 보고 채널은 board 와 무관하게 살아 있어야 한다 — 잔여를 전부 보여준다.
    assert "미스테이지 잔여" in captured.out
    assert "roadmap.md" in captured.out and "a.py" in captured.out
    assert "잔여 변경 없음" not in captured.out


@requires_git
def test_finish_reports_residual_dirty_loud(tf, tmp_path, monkeypatch, capsys):
    """스코프 밖에 남은 변경은 **출력에 뜬다** — 조용한 누락을 조용한 유출과 바꾸지 않는다."""
    root = _make_home_repo(tmp_path / "home")
    _dirty_the_tree(root)
    finisher = _make_finisher(tf, root, monkeypatch, touches=[_TOUCHED_FILE])

    assert finisher.run("T-0001", section=None, dry_run=False) == 0
    out = capsys.readouterr().out
    assert "미스테이지 잔여" in out
    assert "roadmap.md" in out and "other-spike.md" in out
    assert "0002-other-draft.md" in out and "other-page.md" in out
    # 마지막 줄에서 한 번 더 재고지 — [4/5] 보고가 이후 8줄에 묻히지 않게(loud 강화).
    assert "[완료] T-0001 기록 완료. ⚠ 미스테이지 잔여" in out


@requires_git
def test_finish_reports_clean_when_nothing_left(tf, tmp_path, monkeypatch, capsys):
    """스코프가 전부 덮으면 잔여 0 을 명시한다(보고가 항상 나온다 — 침묵 아님)."""
    root = _make_home_repo(tmp_path / "home")
    (root / _TOUCHED_FILE).write_text("x = 2\n", encoding="utf-8")
    finisher = _make_finisher(tf, root, monkeypatch, touches=[_TOUCHED_FILE])

    assert finisher.run("T-0001", section=None, dry_run=False) == 0
    out = capsys.readouterr().out
    assert "스코프 밖 잔여 변경 없음" in out
    assert "⚠ 미스테이지 잔여" not in out


@requires_git
def test_finish_dry_run_stages_nothing(tf, tmp_path, monkeypatch, capsys):
    """dry-run 은 pathspec preview 만 — index 를 건드리지 않는다(부작용 0 보존)."""
    root = _make_home_repo(tmp_path / "home")
    _dirty_the_tree(root)
    finisher = _make_finisher(tf, root, monkeypatch, touches=[_TOUCHED_FILE])

    assert finisher.run("T-0001", section=None, dry_run=True) == 0
    assert _staged(root) == set()
    assert "[dry-run] git add -A --" in capsys.readouterr().out


def test_engine_written_paths_are_this_runs_outputs_only(tf, tmp_path, monkeypatch):
    """선언원 ② = **이 실행이 쓴 것** — 다른 도구의 산출물 디렉토리를 열거하지 않는다.

    board 미로드(None)면 log 하나. 여기서 decisions/·domain/·ideas/ 가 나오면 그건 *다른
    실행* 의 산출물을 선언분으로 착각한 것이다(ADR-0074 must-fix 의 재발).
    """
    monkeypatch.setattr(tf, "REPO", tmp_path)
    log_file = tmp_path / ".project_manager" / "wiki" / "log" / "current.md"
    assert tf.engine_written_paths(None, "T-0001", log_file) == [log_file]


@requires_git
def test_finish_prints_pathspec_commit_guidance_at_runtime(tf, tmp_path, monkeypatch,
                                                           capsys):
    """PM 이 실제로 보는 **런타임 출력**이 경로 명시형 커밋 안내를 낸다 (문서만 고치면 반쪽 출하).

    소스 문자열을 assert 하면 안 된다 — `print` 를 `if False:` 아래로 옮겨 **안내가 아예 안
    나오게** 만들어도 통과한다(reviewer probe 로 실증된 teeth 0). 관측 가능한 산출(프로세스
    출력)으로만 묻는다.
    """
    root = _make_home_repo(tmp_path / "home")
    (root / _TOUCHED_FILE).write_text("x = 2\n", encoding="utf-8")
    finisher = _make_finisher(tf, root, monkeypatch, touches=[_TOUCHED_FILE])

    assert finisher.run("T-0001", section=None, dry_run=False) == 0
    out = capsys.readouterr().out
    assert "git commit — " in out, "커밋 안내가 런타임에 아예 안 나온다"
    guidance = next(line for line in out.splitlines() if "git commit — " in line)
    assert "-- " in guidance, f"bare commit 안내(경로 미명시): {guidance!r}"
    assert "경로를 명시" in guidance
    assert guidance == (
        '  git commit — **경로를 명시**하라: '
        '`git commit -m "<메시지>" -- <위 [4/5] 가 stage 한 경로들>` '
        '(메시지는 PM 이 작성 · Co-Authored-By: Claude 트레일러 포함)'
    )


# ════════════════════════════════════════════════════════════════════════
# _prefix_relabel — 과차단 폐기 + 만진 경로만 커밋 (실 git board)
# ════════════════════════════════════════════════════════════════════════

_TICKET_TEXT = (
    "---\n"
    "id: {tid}\n"
    "title: t\n"
    "status: open\n"
    "created: '2026-07-01'\n"
    "claimed_by: null\n"
    "depends_on: []\n"
    "blocks: []\n"
    "touches: []\n"
    "tags: []\n"
    "---\n\n# {tid} — t\n\n## 목표\nx\n"
)


def _make_board_git_home(root: Path, tid: str = "T-foo-001") -> Path:
    """board 분리 형상(`.project_manager/board/` = 별도 실 git) + wiki 트리를 만든다."""
    board_dir = root / ".project_manager" / "board"
    for status in ("open", "claimed", "blocked", "done"):
        (board_dir / "tickets" / status).mkdir(parents=True, exist_ok=True)
    (root / ".project_manager" / "wiki" / "log").mkdir(parents=True, exist_ok=True)
    (board_dir / "tickets" / "open" / f"{tid}-slug.md").write_text(
        _TICKET_TEXT.format(tid=tid), encoding="utf-8")
    (board_dir / "notes.md").write_text("v1\n", encoding="utf-8")   # 남의 추적 파일
    _git_init(board_dir)
    _git_commit_all(board_dir, "board init")
    return board_dir


@requires_git
def test_relabel_proceeds_with_dirty_home_and_commits_only_touched(tmp_path, monkeypatch,
                                                                   capsys):
    """홈 dirty 여도 relabel 진행(과차단 폐기) + 커밋엔 **만진 경로만** 실린다(누출 0).

    옛 동작은 홈 git 이 dirty 면 rc=1 로 전면 abort 했고(남의 WIP 로 내 작업이 막힘), 백업
    커밋은 board 전체를 쓸어담았다(남의 board WIP 가 함께 실림). 둘 다 "공유 트리를 나 혼자
    쓴다"는 같은 가정의 양면이라 함께 폐기한다(ADR-0074).
    """
    board = _load("board")
    root = tmp_path / "home"
    root.mkdir()
    board_dir = _make_board_git_home(root)
    monkeypatch.setattr(board, "REPO", root)
    monkeypatch.setattr(board, "BOARD_FILE", root / ".project_manager" / "wiki" / "board.md")
    monkeypatch.setattr(board, "LOCAL_DIR", root / ".project_manager" / ".local")
    monkeypatch.setattr(board, "BOARD_LOCK",
                        root / ".project_manager" / ".local" / "board.lock")
    for key, val in _GIT_IDENTITY.items():
        monkeypatch.setenv(key, val)
    # 홈 git 은 dirty 하다(남의 미커밋 wiki 편집) — 옛 게이트라면 여기서 abort 했다.
    monkeypatch.setattr(board, "_home_git_status_porcelain",
                        lambda: " M .project_manager/wiki/roadmap.md\n")
    # board 워킹트리에도 무관한 남의 변경을 심는다(추적 수정 + untracked).
    (board_dir / "notes.md").write_text("v2 — 남이 편집 중\n", encoding="utf-8")
    (board_dir / "scratch.md").write_text("남의 임시 파일\n", encoding="utf-8")

    rc = board.cmd_prefix_rename(argparse.Namespace(
        src="foo", dst="bar", dry_run=False, user_ack="bar"))
    assert rc == 0, capsys.readouterr().err

    out = capsys.readouterr().out
    assert "무관한 미커밋 변경" in out and "[중단]" not in out   # 안내는 하되 차단은 안 한다
    # 새 커밋에 실린 파일 = relabel 이 만진 것뿐 (rename 이라 옛 경로는 삭제로 함께 실린다 —
    # `--name-status` 는 rename 검출 시 R 한 줄로 접으므로 커밋 *트리* 로도 대조한다).
    committed = _git(["show", "--name-status", "--format=", "HEAD"], board_dir).stdout
    tree = _git(["ls-tree", "-r", "--name-only", "HEAD"], board_dir).stdout
    assert "T-bar-001-slug.md" in committed
    assert "tickets/open/T-bar-001-slug.md" in tree
    assert "T-foo-001-slug.md" not in tree, f"옛 경로 삭제가 커밋에 안 실림: {tree!r}"
    assert "notes.md" not in committed, f"남의 board WIP 가 백업 커밋에 실림: {committed!r}"
    assert "scratch.md" not in committed, f"남의 untracked 가 백업 커밋에 실림: {committed!r}"
    # 남의 변경은 여전히 미커밋으로 남아 있다(그 사람이 직접 다룬다).
    assert "notes.md" in _git(["status", "--porcelain"], board_dir).stdout
