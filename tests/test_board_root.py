"""board_root graceful 탐지 + board/ 분리 경로 전환 단위/회귀 테스트 (T-0162·ADR-0033 ①).

board(tickets+areas)를 `.project_manager/board/`(submodule)로 분리할 수 있게 엔진이 board 루트를
*실측*으로 탐지하는지 검증한다. 핵심 안전속성(머지 조건):

  - **legacy(board/ 부재) 100% 무변경**: board_root()/tickets_dir()/areas_file() 가 모두 현
    위치(wiki/·areas.md)로 해소된다 → 기존 1688 테스트 green 유지. 이 파일이 그 fallback 을
    *직접* 단언한다(상수→함수 전환이 legacy 경로를 안 바꿈을 박제).
  - **board/ 존재 시 board 루트**: `.project_manager/board/tickets` 가 dir 이면 board/ 루트로
    갈리고 areas 는 board/ *안*으로(조건분기), wikilink lint 가 board ticket 본문도 스캔한다.
  - **board.py ↔ pm_config 경로 대칭**: pm_config 은 areas 를 board 함수에 위임(자체 해소 0·
    ADR-0013 isolation) → 둘이 같은 파일을 가리킨다(repo add 가 다른 파일 쓰면 보드 깨짐).
  - **A5 누출-0 git 회귀**: hermetic tmp-git 으로 `ignore=all` 의 board↔design 누출 0 구조를
    박제(board.py 코드 무관·순수 git 동작 — smtest_v3 의 durable 승격).

**hermetic 필수**: board.py 경로 전역(`REPO` 등)은 import 시점에 실 repo 절대경로로 굳는다 —
함수 scope 로 매 테스트마다 새 모듈을 로드해 `REPO` 를 tmp 로 재지정한다(다른 board 테스트의
monkeypatch 패턴 동류). board_root/areas_file 가 *함수*라 monkeypatch 된 tmp REPO 를 매 호출
따라간다(상수면 실 REPO 에 굳어 tmp 미추종).
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
    reason="git 바이너리 부재 — 실 git 통합 케이스(A5) skip(board_root 단위 테스트는 항상 실행).",
)


def _load_board():
    """board.py 를 (패키지 아님) importlib 로 경로 로드 — test_board_multipm 과 동일 규약."""
    spec = importlib.util.spec_from_file_location("board", TOOLS / "board.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def board(tmp_path, monkeypatch):
    """fresh board 모듈 + REPO 를 tmp 로 재지정한 hermetic 인스턴스 (실 루트 미접촉)."""
    mod = _load_board()
    monkeypatch.setattr(mod, "REPO", tmp_path)
    mod._tmp = tmp_path
    return mod


def _make_board_dir(root: Path) -> Path:
    """`.project_manager/board/tickets/{open,...}` 를 만들어 board/ 분리 형상을 모사한다."""
    board_dir = root / ".project_manager" / "board"
    for status in ("open", "claimed", "blocked", "done"):
        (board_dir / "tickets" / status).mkdir(parents=True, exist_ok=True)
    return board_dir


# ════════════════════════════════════════════════════════════════════════
# A1 — board_root graceful 탐지 + legacy 무변경 fallback
# ════════════════════════════════════════════════════════════════════════

def test_board_root_legacy_falls_back_to_wiki(board):
    """board/ 부재(legacy·솔로·미마이그) → board_root() == REPO/.project_manager/wiki (무변경)."""
    assert board.board_root() == board._tmp / ".project_manager" / "wiki"


def test_tickets_dir_legacy_is_wiki_tickets(board):
    """legacy → tickets_dir() == wiki/tickets (현 위치·기존 1688 테스트가 의존하는 경로)."""
    assert board.tickets_dir() == board._tmp / ".project_manager" / "wiki" / "tickets"


def test_template_file_legacy_is_wiki_template(board):
    """legacy → template_file() == wiki/tickets/_template.md (현 위치)."""
    assert board.template_file() == (
        board._tmp / ".project_manager" / "wiki" / "tickets" / "_template.md")


def test_areas_file_legacy_is_project_manager_areas(board):
    """legacy → areas_file() == .project_manager/areas.md (wiki *밖*·현 위치·특수 조건분기)."""
    assert board.areas_file() == board._tmp / ".project_manager" / "areas.md"


def test_board_root_present_switches_to_board_dir(board):
    """`.project_manager/board/tickets` 가 dir 이면 board_root() == .project_manager/board."""
    _make_board_dir(board._tmp)
    assert board.board_root() == board._tmp / ".project_manager" / "board"


def test_tickets_dir_present_is_board_tickets(board):
    """board/ 존재 → tickets_dir() == board/tickets (board 루트 추종)."""
    _make_board_dir(board._tmp)
    assert board.tickets_dir() == board._tmp / ".project_manager" / "board" / "tickets"


def test_template_file_present_is_board_template(board):
    """board/ 존재 → template_file() == board/tickets/_template.md."""
    _make_board_dir(board._tmp)
    assert board.template_file() == (
        board._tmp / ".project_manager" / "board" / "tickets" / "_template.md")


def test_areas_file_present_moves_inside_board(board):
    """board/ 존재 → areas_file() == board/areas.md (submodule *안*·조건분기·legacy 와 다름)."""
    _make_board_dir(board._tmp)
    assert board.areas_file() == board._tmp / ".project_manager" / "board" / "areas.md"


def test_board_root_ignores_bare_board_dir_without_tickets(board):
    """`board/` 가 있어도 그 안에 `tickets/` 가 없으면 *legacy* 로 본다 (탐지는 tickets dir 실측).

    부분/오인 디렉토리(예: board/ 만 mkdir 됨)에 끌려가지 않게 — install_pre_push_hook 의
    git-path 실측 패턴 동형. 빈 board/ 로는 절대 새 경로로 가지 않는다(graceful)."""
    (board._tmp / ".project_manager" / "board").mkdir(parents=True)
    assert board.board_root() == board._tmp / ".project_manager" / "wiki"
    assert board.areas_file() == board._tmp / ".project_manager" / "areas.md"


def test_board_root_follows_monkeypatched_repo_lazily(board):
    """board_root 가 *함수*라 REPO monkeypatch 를 매 호출 따라간다 (import-time 굳음 아님).

    상수였다면 실 REPO 에 굳어 hermetic 테스트가 깨진다 — 함수 seam 의 핵심 회귀 박제."""
    # board/ 없을 때 legacy, board/ 만들면 즉시 board 루트 — 같은 모듈 인스턴스에서 동적 전환.
    assert board.board_root().name == "wiki"
    _make_board_dir(board._tmp)
    assert board.board_root().name == "board"


# ════════════════════════════════════════════════════════════════════════
# A2 — board.py ↔ pm_config 경로 대칭 (repo add 가 같은 areas 파일을 써야 함)
# ════════════════════════════════════════════════════════════════════════

def _load_pm_config():
    spec = importlib.util.spec_from_file_location("pm_config", TOOLS / "pm_config.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_pm_config_delegates_areas_to_board_no_own_constant():
    """pm_config 은 자체 AREAS_FILE 상수를 갖지 않는다 — areas 경로 해소를 board 에 위임한다.

    ADR-0013 isolation: pm_config 이 areas 경로를 *독립* 해소하면 board_root 분리 시 board.py
    와 어긋나 repo add(board.areas_append)와 조회(board._parse_areas)가 다른 파일을 본다. 자체
    상수 부재 = 동형 추종 보장(단일 진실 = board.areas_file)."""
    pm_config = _load_pm_config()
    assert not hasattr(pm_config, "AREAS_FILE"), \
        "pm_config 이 자체 AREAS_FILE 을 정의 — board 와 areas 경로가 갈릴 수 있다(repo add 클로버)."


def test_pm_config_repo_add_writes_board_resolved_areas_legacy(board, tmp_path, monkeypatch):
    """repo add(board.areas_append) 가 쓰는 areas 파일 == board.areas_file() (legacy 대칭).

    pm_config.cmd_repo_add 는 board.areas_append 로 위임하므로, board 가 areas 경로를 옮기면
    pm_config 도 자동 추종한다 — 둘이 *같은* 파일을 가리킴을 직접 단언(대칭 가드)."""
    # legacy: areas_append 가 board.areas_file() 위치에 쓴다.
    (board._tmp / ".project_manager" / ".local").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(board, "BOARD_LOCK", board._tmp / ".project_manager" / ".local" / "board.lock")
    assert not board.areas_file().exists()
    board.areas_append("PAY", "결제", "alice")
    assert board.areas_file().exists(), "areas_append 가 board.areas_file() 위치에 쓰지 않음."
    assert board.areas_file() == board._tmp / ".project_manager" / "areas.md"


def test_areas_append_follows_board_root_when_separated(board, monkeypatch):
    """board/ 분리 시 areas_append 가 board/areas.md(board 안)에 쓴다 — 코드 git 오염 0 형상."""
    _make_board_dir(board._tmp)
    (board._tmp / ".project_manager" / ".local").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(board, "BOARD_LOCK", board._tmp / ".project_manager" / ".local" / "board.lock")
    board.areas_append("PAY", "결제", "alice")
    inside = board._tmp / ".project_manager" / "board" / "areas.md"
    outside = board._tmp / ".project_manager" / "areas.md"
    assert inside.exists(), "board/ 분리 시 areas 가 board 안에 안 쓰임 — 대칭/조건분기 깨짐."
    assert not outside.exists(), "board/ 분리인데 legacy 위치(wiki 밖)에도 씀 — 누출."


# ════════════════════════════════════════════════════════════════════════
# A3 — wikilink lint 두 루트 cross-ref (board ticket 본문도 스캔)
# ════════════════════════════════════════════════════════════════════════

def _wire_wiki(board, root: Path) -> Path:
    """legacy wiki 레이아웃(tickets/ideas/decisions) — _collect_wikilink_files 가 읽는 구조."""
    wiki = root / ".project_manager" / "wiki"
    for status in ("open", "claimed", "blocked", "done"):
        (wiki / "tickets" / status).mkdir(parents=True, exist_ok=True)
    for status in ("open", "promoted", "killed"):
        (wiki / "ideas" / status).mkdir(parents=True, exist_ok=True)
    (wiki / "decisions").mkdir(parents=True, exist_ok=True)
    return wiki


def test_collect_wikilink_files_legacy_no_duplicate(board, monkeypatch):
    """legacy(board_root==wiki): tickets_dir() union 이 중복을 안 만든다 (set dedup → no-op).

    legacy 면 tickets_dir() 가 이미 wiki.rglob 에 포함된다 — dedup 으로 중복 0 을 박제."""
    wiki = _wire_wiki(board, board._tmp)
    tk = wiki / "tickets" / "open" / "T-0001-x.md"
    tk.write_text("# t\n[[ADR-0001]]\n", encoding="utf-8")
    files = board._collect_wikilink_files()
    # 같은 ticket 파일이 한 번만(중복 없음).
    matches = [p for p in files if p.name == "T-0001-x.md"]
    assert len(matches) == 1, f"legacy 에서 ticket 파일 중복 수집 — dedup 미동작: {matches}"


def test_collect_wikilink_files_includes_board_tickets_when_separated(board):
    """board/ 분리 시 board/tickets 본문이 wikilink 스캔 대상에 union 된다.

    board ticket 이 wiki/ 밖으로 빠지면 그 `[[ADR-NNNN]]` 가 wiki-only 스캔에선 안 보여
    dangling 미검출 — board/tickets 도 스캔함을 박제."""
    _make_board_dir(board._tmp)  # board/tickets/* 생성
    # wiki/ 는 비어 있어도 됨(설계축만) — 여기선 board ticket 만 둔다.
    board_tk = board._tmp / ".project_manager" / "board" / "tickets" / "open" / "T-0009-y.md"
    board_tk.write_text("# t\n[[ADR-9999]]\n", encoding="utf-8")
    files = board._collect_wikilink_files()
    assert any(p.name == "T-0009-y.md" for p in files), \
        "board/ 분리 시 board ticket 본문이 wikilink 스캔에서 누락 — dangling 미검출 갭."


def test_lint_wikilinks_detects_board_ticket_dangling(board, monkeypatch):
    """board/ 분리된 ticket 본문의 dangling [[ADR-NNNN]] 를 lint 가 잡는다 (두 루트 cross-ref).

    A3 의 실효 검증 — 실재하지 않는 ADR 을 board ticket 이 참조하면 dangling 으로 surface."""
    _make_board_dir(board._tmp)
    wiki = board._tmp / ".project_manager" / "wiki"
    (wiki / "decisions").mkdir(parents=True, exist_ok=True)
    (wiki / "ideas" / "open").mkdir(parents=True, exist_ok=True)
    (wiki / "ideas" / "promoted").mkdir(parents=True, exist_ok=True)
    (wiki / "ideas" / "killed").mkdir(parents=True, exist_ok=True)
    # 실재 ADR 은 없음 → board ticket 의 [[ADR-7777]] 은 dangling 이어야 한다. ticket 은 valid
    # frontmatter 를 가져야 _all_tickets() 파싱을 통과한다(missing-frontmatter 예외 방지).
    board_tk = board._tmp / ".project_manager" / "board" / "tickets" / "open" / "T-0010-z.md"
    board_tk.write_text(
        "---\nid: T-0010\ntitle: z\nstatus: open\n---\n\n"
        "# T-0010 — z\nsee [[ADR-7777]] for detail\n",
        encoding="utf-8")
    issues = board.lint_wikilinks()
    assert any("7777" in name or "ADR-7777" in detail
               for name, _kind, detail in issues), \
        f"board ticket 의 dangling [[ADR-7777]] 미검출 — 두 루트 cross-ref 갭: {issues}"


# ════════════════════════════════════════════════════════════════════════
# A4 — _configure_board_submodule (ignore=all setup·멱등·fail-soft)
# ════════════════════════════════════════════════════════════════════════

def test_configure_board_submodule_noop_when_not_separated(board):
    """board/.git 부재(솔로·legacy) → no-op·False (git 미실행·솔로 100% 무영향)."""
    assert board._configure_board_submodule() is False


def test_board_submodule_name_none_without_gitmodules(board):
    """.gitmodules 부재 → _board_submodule_name() None (fail-soft·_configure no-op)."""
    assert board._board_submodule_name() is None


# ════════════════════════════════════════════════════════════════════════
# A5 — 누출-0 git 회귀 (형상 회귀·실 git·board.py 코드 무관·smtest_v3 durable 승격)
# ════════════════════════════════════════════════════════════════════════
# ADR-0033 ①의 "ignore=all = 코드 git 누출 0" 구조를 *순수 git 동작*으로 박제한다. board.py
# 함수를 부르지 않고 git 의 submodule ignore=all 행동만 단언한다 — board PM-commit 이 design
# (코드) git 의 status/add 를 오염하지 않음을 형상 회귀로 못박는다.

def _git(args: list[str], cwd: Path, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          env=env, check=False)


@requires_git
def test_board_submodule_ignore_all_zero_leak(tmp_path):
    """hermetic tmp-git: board(submodule) 전진이 design(superproject) status/add 를 오염 안 함.

    케이스(ADR-0033 ①·smtest_v3 durable):
      1. bare board remote + design(superproject) + `git submodule add` + ignore=all 설정.
      2. board 전진(board commit) → design `git status --porcelain` == "" (ignore=all clean).
      3. design `git add -A` → staged 에 board gitlink 미포함(design 파일만).
      4. `submodule update` footgun(board/ 핀 SHA reset) 동작 단언 + 복구(`git -C board pull`).
    """
    import os
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           # local file 경로 submodule clone 허용(테스트 hermetic — CVE-2022-39253 가드 우회).
           "GIT_ALLOW_PROTOCOL": "file:ext:ssh:git:http:https"}

    # ── 1) bare board remote + seed 1 commit (default branch=main 고정) ──
    bare = tmp_path / "bare-board"
    assert _git(["init", "--bare", "-q", "-b", "main", str(bare)], tmp_path, env).returncode == 0
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(["init", "-q", "-b", "main"], seed, env)
    (seed / "board-file.txt").write_text("v1\n", encoding="utf-8")
    _git(["add", "-A"], seed, env)
    assert _git(["commit", "-qm", "board init"], seed, env).returncode == 0
    assert _git(["push", "-q", str(bare), "HEAD:main"], seed, env).returncode == 0

    # ── 2) design(superproject) + submodule add board + ignore=all ──
    design = tmp_path / "design"
    design.mkdir()
    _git(["init", "-q", "-b", "master"], design, env)
    (design / "code.txt").write_text("design v1\n", encoding="utf-8")
    _git(["add", "-A"], design, env)
    _git(["commit", "-qm", "design init"], design, env)
    # local-file submodule clone 은 `-c protocol.file.allow=always` 를 add 시점에 줘야 한다.
    add = _git(["-c", "protocol.file.allow=always", "submodule", "add", "-q",
                str(bare), ".project_manager/board"], design, env)
    assert add.returncode == 0, f"submodule add 실패: {add.stderr}"
    _git(["commit", "-qm", "add board submodule"], design, env)

    board_sub = design / ".project_manager" / "board"
    # 실측 확정 키: submodule.<.gitmodules-path>.ignore (path == .project_manager/board).
    set_ignore = _git(["config", "submodule..project_manager/board.ignore", "all"], design, env)
    assert set_ignore.returncode == 0
    assert _git(["config", "--get", "submodule..project_manager/board.ignore"],
                design, env).stdout.strip() == "all"

    # ── 3) board 전진(submodule commit + push to board remote) → design status clean ──
    # ADR-0033 실 흐름: board PM-commit 은 board remote 로 push 된다(코드 git 과 별개 채널).
    # push 해 둬야 5)의 footgun 복구(pull)가 v2 를 되찾을 수 있다(전진분이 remote 에 존재).
    (board_sub / "board-file.txt").write_text("v2 — board advanced\n", encoding="utf-8")
    _git(["add", "-A"], board_sub, env)
    assert _git(["commit", "-qm", "board advance"], board_sub, env).returncode == 0
    assert _git(["push", "-q", str(bare), "HEAD:main"], board_sub, env).returncode == 0
    status = _git(["status", "--porcelain"], design, env)
    assert status.stdout == "", \
        f"ignore=all 인데 board 전진이 design status 에 새어나옴(누출): {status.stdout!r}"

    # ── 4) design git add -A → board gitlink 미스테이지 (design 파일만) ──
    # 4a) design 변경 없이 add -A → staged 비어 있음(gitlink 제외).
    _git(["add", "-A"], design, env)
    staged_none = _git(["diff", "--cached", "--name-only"], design, env)
    assert staged_none.stdout.strip() == "", \
        f"board gitlink 이 우발 stage 됨(누출): {staged_none.stdout!r}"
    # 4b) design 파일 변경 + add -A → *오직* design 파일만 staged(gitlink 제외).
    (design / "code.txt").write_text("design v2\n", encoding="utf-8")
    _git(["add", "-A"], design, env)
    staged = _git(["diff", "--cached", "--name-only"], design, env).stdout.split()
    assert "code.txt" in staged, "design 파일 변경이 staged 되지 않음."
    assert ".project_manager/board" not in staged, \
        f"board gitlink 이 design 변경과 함께 stage 됨(누출): {staged}"
    _git(["reset", "-q"], design, env)

    # ── 5) submodule update footgun: board/ 가 핀 SHA 로 reset → 복구 가능 ──
    # design 의 index 는 아직 board v1 SHA 를 핀하고 있다(전진을 commit 안 함). `submodule
    # update` 는 board/ 를 그 핀 SHA(v1)로 *되돌린다* — board v2 작업이 detach/유실되는 footgun.
    advanced_head = _git(["rev-parse", "HEAD"], board_sub, env).stdout.strip()
    upd = _git(["-c", "protocol.file.allow=always", "submodule", "update",
                "--", ".project_manager/board"], design, env)
    assert upd.returncode == 0, f"submodule update 실패: {upd.stderr}"
    pinned_head = _git(["rev-parse", "HEAD"], board_sub, env).stdout.strip()
    assert pinned_head != advanced_head, \
        "submodule update 가 board/ 를 핀 SHA 로 안 되돌림 — footgun 전제 깨짐."
    assert (board_sub / "board-file.txt").read_text(encoding="utf-8") == "v1\n", \
        "submodule update 후 board/ 가 v1(핀 SHA) 상태가 아님."
    # 복구: board/ 에서 remote 를 다시 당기면(`pull`) 전진분(v2)을 되찾는다.
    pull = _git(["pull", "-q", str(bare), "main"], board_sub, env)
    assert pull.returncode == 0, f"board pull 복구 실패: {pull.stderr}"
    assert (board_sub / "board-file.txt").read_text(encoding="utf-8") == "v2 — board advanced\n", \
        "pull 복구 후에도 board v2 전진분을 못 되찾음."
