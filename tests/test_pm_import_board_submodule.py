"""pm-import --new --board-submodule — board 전용 git submodule 자동 셋업 (ADR-0033 · T-0297).

`--new --board-submodule --board-remote <url>` 는 board(tickets+areas)를 superproject inline 이
아니라 별도 git submodule(`.project_manager/board`)로 세운다: 빈 공유 remote 면 tickets 구조 +
areas.md 로 초기화 후 push(신규), 내용 있으면 재사용(2번째 유저 합류), `.gitmodules` ignore=all,
그리고 이후 board.py 조작이 submodule board 를 타깃한다.

**hermetic**: 임시 로컬 **bare remote**(`git init --bare`·네트워크 0)로 전 경로를 실측한다
([[feature-ship-needs-fresh-adopter-gate]]). git 부재 환경은 실 git 케이스를 skip(입력 검증
단위 테스트는 항상 실행). 실 github push 는 없다(로컬 remote 로만·비가역 부작용 0).
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

requires_git = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git 바이너리 부재 — 실 submodule 통합 케이스 skip(입력 검증 단위 테스트는 항상 실행).",
)

# hermetic git commit 을 위한 결정적 author/committer (실 사용자 config 불요·test_board_git_sync 동형).
_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
}


def _load_mod(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pm_import():
    return _load_mod("pm_import")


@pytest.fixture(autouse=True)
def _hermetic(pm_import, monkeypatch):
    """opencode `models` 라이브 호출 차단(설치 여부로 분기하지 않게) + 결정적 git identity."""
    monkeypatch.setattr(pm_import, "_real_models_runner", lambda: (False, []))
    for key, val in _GIT_IDENTITY.items():
        monkeypatch.setenv(key, val)


# ── git helpers ──────────────────────────────────────────────────────────────

def _git(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "protocol.file.allow=always", *args],
        cwd=str(cwd) if cwd else None, capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False)


def _make_bare(path: Path) -> Path:
    _git(["init", "--bare", "-q", str(path)])
    return path


def _seed_remote_with_ticket(bare: Path, tmp: Path, tid: str) -> None:
    """bare remote 에 board 내용(tickets/ + 구별 티켓 + areas.md)을 미리 push (2번째-유저 형상)."""
    clone = tmp / f"seed_{tid}"
    _git(["clone", "-q", str(bare), str(clone)])
    for status in ("open", "claimed", "blocked", "done"):
        (clone / "tickets" / status).mkdir(parents=True, exist_ok=True)
        (clone / "tickets" / status / ".gitkeep").touch()
    (clone / "tickets" / "_template.md").write_text(
        "---\nid: T-NNNN\ntitle: <제목>\nstatus: open\n---\n\n# T-NNNN\n", encoding="utf-8")
    (clone / "tickets" / "open" / f"{tid}-existing.md").write_text(
        f"---\nid: {tid}\ntitle: existing\nstatus: open\n---\n\n# {tid} — existing\n",
        encoding="utf-8")
    (clone / "areas.md").write_text("# Area Registry\n\n| repo |\n|---|\n| existing |\n",
                                    encoding="utf-8")
    _git(["add", "-A"], clone)
    _git(["commit", "-qm", "pre-existing board"], clone)
    _git(["push", "-q", "origin", "HEAD"], clone)
    shutil.rmtree(clone, ignore_errors=True)


def _seed_remote_non_board(bare: Path, tmp: Path) -> None:
    """bare remote 에 board 가 *아닌* 내용(tickets/·areas.md 부재)을 push — non-empty non-board 모의."""
    clone = tmp / "seed_nonboard"
    _git(["clone", "-q", str(bare), str(clone)])
    (clone / "README.md").write_text("random repo — not a board\n", encoding="utf-8")
    _git(["add", "-A"], clone)
    _git(["commit", "-qm", "random content"], clone)
    _git(["push", "-q", "origin", "HEAD"], clone)
    shutil.rmtree(clone, ignore_errors=True)


def _board(dest: Path, *args: str) -> subprocess.CompletedProcess:
    """dest 의 board.py 를 subprocess 로 실행 (submodule board 타깃 확인용)."""
    return subprocess.run(
        [sys.executable, str(dest / ".project_manager" / "tools" / "board.py"), *args],
        cwd=str(dest), capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, "PM_NONINTERACTIVE": "1", **_GIT_IDENTITY})


def _gitmodules_ignore(dest: Path) -> str:
    r = _git(["config", "-f", str(dest / ".gitmodules"), "--get",
              "submodule..project_manager/board.ignore"], cwd=dest)
    return r.stdout.strip()


def _config_ignore(dest: Path) -> str:
    r = _git(["config", "--get", "submodule..project_manager/board.ignore"], cwd=dest)
    return r.stdout.strip()


# ════════════════════════════════════════════════════════════════════════════
# 1. 빈 remote → submodule 셋업 + 구조 init + .gitmodules/ignore=all
# ════════════════════════════════════════════════════════════════════════════

@requires_git
def test_empty_remote_scaffolds_and_configures(pm_import, tmp_path):
    bare = _make_bare(tmp_path / "board.git")
    dest = tmp_path / "home"
    rc = pm_import.main(["--new", str(dest), "--harness", "claude", "--name", "H",
                         "--board-submodule", "--board-remote", str(bare)])
    assert rc == 0

    board = dest / ".project_manager" / "board"
    # submodule 형상 — board/.git(gitlink) + board/tickets 존재로 board_root() 가 board/ 로 해소.
    assert (board / ".git").exists()
    assert (board / "tickets").is_dir()
    for status in ("open", "claimed", "blocked", "done"):
        assert (board / "tickets" / status).is_dir(), f"status dir 누락: {status}"
    assert (board / "tickets" / "_template.md").is_file()
    assert (board / "tickets" / "README.md").is_file()
    assert (board / "areas.md").is_file()

    # .gitmodules 등록 + ignore=all (committed·공유 default) + .git/config ignore=all(per-clone).
    gm_text = (dest / ".gitmodules").read_text(encoding="utf-8")
    assert ".project_manager/board" in gm_text
    assert _gitmodules_ignore(dest) == "all"
    assert _config_ignore(dest) == "all"

    # 복사된 dormant wiki/tickets 는 제거(② 참조 형상 — board 는 submodule 에 산다).
    assert not (dest / ".project_manager" / "wiki" / "tickets").exists()

    # remote 가 실제로 seed 됐다(빈→non-empty·push 확인).
    assert _git(["rev-parse", "--verify", "HEAD"], bare).returncode == 0


@requires_git
def test_empty_remote_areas_scaffold_parses_canonical(pm_import, tmp_path):
    """seed 된 areas.md 헤더가 board 의 canonical 8칼럼과 일치(drift·파싱 가드)."""
    board_mod = _load_mod("board")
    bare = _make_bare(tmp_path / "board.git")
    dest = tmp_path / "home"
    assert pm_import.main(["--new", str(dest), "--harness", "claude", "--name", "H",
                           "--board-submodule", "--board-remote", str(bare)]) == 0
    areas = (dest / ".project_manager" / "board" / "areas.md").read_text(encoding="utf-8")
    assert board_mod._areas_header_line() in areas
    assert board_mod._areas_separator_line() in areas


# ════════════════════════════════════════════════════════════════════════════
# 2. 기존 내용 remote → 재사용(구조 init skip·합류)
# ════════════════════════════════════════════════════════════════════════════

@requires_git
def test_existing_remote_reused_not_reinitialized(pm_import, tmp_path):
    bare = _make_bare(tmp_path / "board.git")
    _seed_remote_with_ticket(bare, tmp_path, "T-0042")
    dest = tmp_path / "home"
    rc = pm_import.main(["--new", str(dest), "--harness", "claude", "--name", "H",
                         "--board-submodule", "--board-remote", str(bare)])
    assert rc == 0
    board = dest / ".project_manager" / "board"
    # 기존 board 내용을 그대로 재사용 — 우리 스캐폴드가 덮어쓰지 않는다(합류).
    assert (board / "tickets" / "open" / "T-0042-existing.md").is_file()
    assert "existing" in (board / "areas.md").read_text(encoding="utf-8")
    # 그래도 submodule 배선은 동일(ignore=all·wiki/tickets 제거).
    assert _gitmodules_ignore(dest) == "all"
    assert not (dest / ".project_manager" / "wiki" / "tickets").exists()


# ════════════════════════════════════════════════════════════════════════════
# 3. fail-loud 게이트 — 부작용 전 거부
# ════════════════════════════════════════════════════════════════════════════

def test_board_submodule_requires_remote(pm_import, tmp_path, capsys):
    dest = tmp_path / "home"
    rc = pm_import.main(["--new", str(dest), "--harness", "claude",
                         "--board-submodule"])  # --board-remote 없음
    assert rc == 1
    assert "--board-remote" in capsys.readouterr().err
    # 부작용 전 거부 — dest 미생성.
    assert not dest.exists()


def test_board_submodule_requires_new(pm_import, tmp_path, capsys):
    existing = tmp_path / "existing"
    existing.mkdir()
    rc = pm_import.main(["--into", str(existing), "--harness", "claude",
                         "--board-submodule", "--board-remote", "/some/board.git"])
    assert rc == 1
    assert "--new" in capsys.readouterr().err
    # board 미셋업(부작용 전 거부).
    assert not (existing / ".project_manager" / "board").exists()
    assert not (existing / ".gitmodules").exists()


def test_board_remote_without_submodule_rejected(pm_import, tmp_path, capsys):
    dest = tmp_path / "home"
    rc = pm_import.main(["--new", str(dest), "--harness", "claude",
                         "--board-remote", "/some/board.git"])  # --board-submodule 없음
    assert rc == 1
    assert "--board-submodule" in capsys.readouterr().err
    assert not dest.exists()


def test_board_remote_credential_in_url_rejected(pm_import, tmp_path, capsys):
    dest = tmp_path / "home"
    rc = pm_import.main(["--new", str(dest), "--harness", "claude", "--board-submodule",
                         "--board-remote", "https://user:pass@example.com/board.git"])
    assert rc == 1
    assert "거부" in capsys.readouterr().err
    assert not dest.exists()


def test_board_remote_leading_dash_rejected(pm_import, tmp_path):
    dest = tmp_path / "home"
    # `=` 형(argparse 가 값을 통과시킴) — leading-dash 값을 validate_upstream_value 가 거부해야 한다.
    rc = pm_import.main(["--new", str(dest), "--harness", "claude", "--board-submodule",
                         "--board-remote=-oProxyCommand=evil"])
    assert rc == 1
    assert not dest.exists()


# ════════════════════════════════════════════════════════════════════════════
# 3b. non-board / 부분-board 실패 경로 (codex must-fix + cleanup 커버리지)
# ════════════════════════════════════════════════════════════════════════════

@requires_git
def test_non_board_remote_rejected_wiki_preserved(pm_import, tmp_path, capsys):
    """non-empty 지만 non-board(tickets/·areas.md 부재) remote → 비0 실패 AND wiki/tickets 미삭제.

    must-fix 회귀: 검증이 없으면 seeded=False 로 흘러 submodule add + step4(wiki/tickets 제거)가
    실행돼 board/tickets 부재인데 폴백 wiki/tickets 마저 삭제된 *깨진 board* 를 rc0 로 낸다.
    """
    bare = _make_bare(tmp_path / "board.git")
    _seed_remote_non_board(bare, tmp_path)
    dest = tmp_path / "home"
    rc = pm_import.main(["--new", str(dest), "--harness", "claude", "--name", "H",
                         "--board-submodule", "--board-remote", str(bare)])
    assert rc != 0
    assert "유효한 board repo" in capsys.readouterr().err
    # 부작용 0 — board 미셋업 + 폴백 wiki/tickets 온전(깨진 board 아님).
    assert not (dest / ".project_manager" / "board").exists()
    assert (dest / ".project_manager" / "wiki" / "tickets" / "_template.md").is_file()


@requires_git
def test_submodule_add_failure_cleans_partial_board(pm_import, tmp_path, monkeypatch):
    """seed+push 성공 후 submodule add 실패 → _cleanup_partial_board 가 부분 board 를 0 으로 되돌린다.

    seed 는 원 remote 에 push 되지만, submodule add 직전 remote 를 *빈 것*으로 갈아 add 를 rc128
    (부분 board 잔재: board/·.git/modules/…/board)로 실패시킨다 — cleanup 경로를 실측 커버.
    """
    bare = tmp_path / "board.git"
    _make_bare(bare)
    dest = tmp_path / "home"
    real = pm_import._board_setup_git

    def wrapper(argv, cwd):
        if argv[:2] == ["submodule", "add"]:
            # seed+push 는 이미 끝남(remote non-empty). remote 를 빈 것으로 갈아 add 를 실패시킨다.
            shutil.rmtree(bare, ignore_errors=True)
            subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
        return real(argv, cwd)

    monkeypatch.setattr(pm_import, "_board_setup_git", wrapper)
    rc = pm_import.main(["--new", str(dest), "--harness", "claude", "--name", "H",
                         "--board-submodule", "--board-remote", str(bare)])
    assert rc != 0
    # cleanup: 부분 board 잔재 0.
    assert not (dest / ".project_manager" / "board").exists(), "board/ 잔재"
    assert not (dest / ".gitmodules").exists(), ".gitmodules 잔재"
    assert not (dest / ".git" / "modules" / ".project_manager" / "board").exists(), \
        ".git/modules 잔재"
    # step4(wiki/tickets 제거)는 submodule add 성공 후라 — 실패 시 미도달(트리 복사분 보존).
    assert (dest / ".project_manager" / "wiki" / "tickets" / "_template.md").is_file()


def _read_only_object_tree(root: Path) -> Path:
    """git object 캐시 형상 — read-only object 파일 + 쓰기 권한 없는 디렉터리.

    git 은 object·packfile 을 read-only 로 만든다. 파일만 read-only 로 두면 POSIX 는 부모
    디렉터리 권한만 보므로 그냥 지워진다 — 디렉터리 조합까지 넣어야 이 축이 Linux 에서도 red 로
    재현된다(Windows 는 파일 속성만으로도 삭제가 거부된다·`[WinError 5]`).
    """
    objects = root / "objects" / "10"
    objects.mkdir(parents=True, exist_ok=True)
    blob = objects / "a9500e"
    blob.write_bytes(b"packed object\n")
    os.chmod(blob, stat.S_IREAD)
    os.chmod(objects, stat.S_IREAD | stat.S_IEXEC)
    return objects


def test_partial_board_cleanup_removes_read_only_git_objects(pm_import, tmp_path, capsys):
    """부분 board 정리가 read-only git object 에 막히지 않는다 (`.git/modules` 잔재 폐쇄).

    옛 정리는 `shutil.rmtree(..., ignore_errors=True)` 라 실패가 흔적 없이 사라졌다 — Windows
    에서는 그래서 `.git/modules/.project_manager/board` 가 통째로 남고, 그 잔재가 fresh dest
    재시도까지 막는다(실측).
    """
    dest = tmp_path / "home"
    modules_board = dest / ".git" / "modules" / ".project_manager" / "board"
    board_dir = dest / ".project_manager" / "board"
    _read_only_object_tree(modules_board)
    _read_only_object_tree(board_dir / ".git")

    pm_import._cleanup_partial_board(dest)

    assert not modules_board.exists(), ".git/modules 잔재 — 정리 실패가 삼켜졌다"
    assert not board_dir.exists(), "board/ 잔재 — 정리 실패가 삼켜졌다"
    assert "정리 실패" not in capsys.readouterr().err


# ════════════════════════════════════════════════════════════════════════════
# 4. 셋업 후 board.py 조작이 submodule board 를 타깃 (fresh-adopter e2e)
# ════════════════════════════════════════════════════════════════════════════

@requires_git
def test_board_ops_target_submodule(pm_import, tmp_path):
    bare = _make_bare(tmp_path / "board.git")
    dest = tmp_path / "home"
    assert pm_import.main(["--new", str(dest), "--harness", "claude", "--name", "H",
                           "--board-submodule", "--board-remote", str(bare)]) == 0
    board = dest / ".project_manager" / "board"

    # list — submodule board(빈)를 읽고 rc 0.
    r = _board(dest, "list")
    assert r.returncode == 0, r.stderr

    # new — board-git 활성이라 미충전 본문은 draft 로 격리(submodule board/tickets/.drafts 타깃).
    r = _board(dest, "new", "Fresh adopter e2e ticket")
    assert r.returncode == 0, r.stderr
    drafts = list((board / "tickets" / ".drafts").glob("*.md"))
    assert drafts, "board submodule 의 .drafts 에 티켓이 생성돼야 함(new 가 submodule 타깃)"

    # claim — 커밋된 open 티켓을 claim → board/tickets/claimed 로 이동 + board-git sync(로컬 remote).
    tid = "T-9001"
    (board / "tickets" / "open" / f"{tid}-seed.md").write_text(
        f"---\nid: {tid}\ntitle: seed\nstatus: open\nclaimed_by: null\nclaimed_at: null\n"
        f"completed_at: null\ndepends_on: []\nblocks: []\ntouches: []\nestimate: small\n"
        f"tags: []\n---\n\n# {tid} — seed\n\n## 목표\nx\n", encoding="utf-8")
    _git(["add", "-A"], board)
    _git(["commit", "-qm", f"seed {tid}"], board)
    _git(["push", "-q", "origin", "HEAD"], board)
    r = _board(dest, "claim", tid)
    assert r.returncode == 0, r.stderr
    assert (board / "tickets" / "claimed" / f"{tid}-seed.md").exists()
    assert not (board / "tickets" / "open" / f"{tid}-seed.md").exists()


# ════════════════════════════════════════════════════════════════════════════
# 5. inline 기본(플래그 없음) 완전 무변경 (현행 --new 회귀)
# ════════════════════════════════════════════════════════════════════════════

@requires_git
def test_inline_default_unchanged(pm_import, tmp_path):
    dest = tmp_path / "home"
    rc = pm_import.main(["--new", str(dest), "--harness", "claude", "--name", "H"])
    assert rc == 0
    # board submodule 미셋업 — inline(legacy) 형상 그대로.
    assert not (dest / ".project_manager" / "board").exists()
    assert not (dest / ".gitmodules").exists()
    # inline board 는 wiki/tickets 안(현행).
    assert (dest / ".project_manager" / "wiki" / "tickets" / "_template.md").is_file()


@requires_git
def test_dry_run_no_board_side_effects(pm_import, tmp_path, capsys):
    bare = _make_bare(tmp_path / "board.git")
    dest = tmp_path / "home"
    rc = pm_import.main(["--new", str(dest), "--harness", "claude", "--name", "H",
                         "--board-submodule", "--board-remote", str(bare), "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "board submodule" in out  # 계획에 board submodule 명시.
    # dry-run = 파일/네트워크 미변경 — dest 미생성·remote 여전히 빈.
    assert not dest.exists()
    assert _git(["rev-parse", "--verify", "HEAD"], bare).returncode != 0


# ════════════════════════════════════════════════════════════════════════════
# 6. areas 스캐폴드 drift 가드 (board._AREAS_COLUMNS 미러)
# ════════════════════════════════════════════════════════════════════════════

def test_areas_scaffold_columns_mirror_board(pm_import):
    board_mod = _load_mod("board")
    assert pm_import._BOARD_AREAS_COLUMNS == board_mod._AREAS_COLUMNS
    scaffold = pm_import._board_areas_scaffold()
    assert board_mod._areas_header_line() in scaffold
    assert board_mod._areas_separator_line() in scaffold
