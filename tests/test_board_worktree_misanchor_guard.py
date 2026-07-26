"""PM-홈 앵커 도구의 worktree cwd 오실행 가드 (T-0345).

board 계열 도구(`board.py new/promote/claim/complete`·`ticket_finish`)를 PM 홈(②)의 등록
worktree(`work/<repo>_<N>`) cwd 에서 실행하면 도구가 cwd 기준 자기-앵커(REPO)로 그 worktree
트리에 조용히 착지해 stray 산출을 낸다 — PM 71 한 세션 3회 실측: (1) `board.py lint` 오경보
(2) `ticket_finish` 가 stray `wiki/log/current.md` (3) `board.py new` 가 잘못된 ID
네임스페이스의 stray `T-0001`. worktree(①)는 코드 전용·board 는 PM 홈(②) 소유(ADR-0027).

이 테스트는:
  - detector(`_pm_home_worktree_misanchor`·3중 conjunction)의 정확성(오탐 0),
  - 쓰기-경로 가드가 실측 3사례를 fail-loud 로 닫음(실 git worktree 재현 픽스처),
  - 읽기 경로(lint)·PM 홈·솔로/standalone·①-자체 board 사용 무회귀
를 검증한다. 도구는 패키지가 아니므로 importlib 동적 로드(관용구).
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
BOARD_PY = TOOLS / "board.py"
TICKET_FINISH_PY = TOOLS / "ticket_finish.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_board():
    return _load("board_misanchor_test", BOARD_PY)


def _load_tf():
    return _load("tf_misanchor_test", TICKET_FINISH_PY)


# ── fixture helpers ──────────────────────────────────────────────────────

def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    }
    # encoding 명시 — text=True 만 주면 로캘 코덱으로 디코딩해 CP949 콘솔(Windows)에서
    # 비-ASCII 경로/메시지가 UnicodeDecodeError 를 낼 수 있다(엔진 subprocess 관례와 동일).
    return subprocess.run(["git", *args], cwd=str(cwd), env=env,
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=True)


def _make_real_board(pm_dir: Path, *, split: bool = True) -> Path:
    """`.project_manager`(pm_dir) 에 *실* board(T-*.md 1개)를 만든다.

    split=True → board/tickets (ADR-0033 ① 분리·submodule), False → wiki/tickets (legacy).
    """
    base = (pm_dir / "board" / "tickets") if split else (pm_dir / "wiki" / "tickets")
    for status in ("open", "claimed", "blocked", "done"):
        (base / status).mkdir(parents=True, exist_ok=True)
    (base / "done" / "T-0100-x.md").write_text("---\nid: T-0100\n---\n# x\n", encoding="utf-8")
    return base


def _make_scaffold_board(pm_dir: Path) -> None:
    """worktree 출하 형상: wiki/tickets 빈 scaffold (README/.gitkeep 만·실 티켓 0)."""
    base = pm_dir / "wiki" / "tickets"
    for status in ("open", "claimed", "blocked", "done"):
        (base / status).mkdir(parents=True, exist_ok=True)
        (base / status / ".gitkeep").write_text("", encoding="utf-8")
    (base / "README.md").write_text("scaffold\n", encoding="utf-8")


def _make_pm_home_with_worktree(tmp_path: Path) -> tuple[Path, Path]:
    """실측 재현 토폴로지: PM 홈(실 board 소유) + 등록 worktree(`work/<repo>_1`·*실* linked git
    worktree·빈 board scaffold). 반환 (pm_home, worktree)."""
    pm_home = tmp_path / "project_manager"
    (pm_home / ".project_manager").mkdir(parents=True)
    _make_real_board(pm_home / ".project_manager", split=True)

    # 소스 repo → `git worktree add` 로 *실* linked worktree (git-dir ≠ git-common-dir).
    src = tmp_path / ".repos" / "product"
    src.mkdir(parents=True)
    _git(["init", "-q", "-b", "main"], src)
    (src / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(["add", "-A"], src)
    _git(["commit", "-qm", "seed"], src)
    worktree = pm_home / "work" / "product_1"
    _git(["worktree", "add", "-q", str(worktree)], src)

    # worktree 는 코드 전용·board 미소유 → 빈 scaffold 만(실 티켓 0).
    _make_scaffold_board(worktree / ".project_manager")
    return pm_home, worktree


def _fake_runner(git_dir: str, common_dir: str):
    """`_is_linked_worktree` 용 git runner 대역 — --git-dir/--git-common-dir 에 캔값 반환."""
    def run(cmd, **kwargs):
        arg = cmd[-1]
        val = {"--git-dir": git_dir, "--git-common-dir": common_dir}[arg]
        return types.SimpleNamespace(returncode=0, stdout=val + "\n", stderr="")
    return run


# board 모듈의 쓰기-타깃 module 상수(REPO 파생이 아니라 import 시점 고정) — REPO 만 monkeypatch
# 하면 이들은 여전히 실 트리를 가리켜, 게이트가 (회귀로) 미발화하면 실 worktree 를 오염시킨다
# (바로 이 티켓이 닫는 클래스가 테스트 자신을 물었던 실제 사례). 아래로 전부 fixture 트리로
# 재바인딩해 어떤 mutation 이 돌더라도 실 트리 오염 0(hermetic).
_WRITE_TARGET_CONSTS: dict[str, str] = {
    "IDEAS_DIR": "wiki/ideas",
    "DECISIONS_DIR": "wiki/decisions",
    "SPECS_DIR": "wiki/specs",
    "ARCHITECTURE_FILE": "wiki/architecture.md",
    "HOOKS_DIR": "hooks",
    "BOARD_FILE": "wiki/board.md",
    "LOG_FILE": "wiki/log/current.md",
    "STATUS_FILE": "wiki/status.md",
    "LOCAL_CONF": "local.conf",
    "TICKETS_DIR": "wiki/tickets",
    "TEMPLATE_FILE": "wiki/tickets/_template.md",
    "AREAS_FILE": "areas.md",
    "PM_STATE_FILE": "wiki/pm_state.md",
    "PM_STATE_TEMPLATE": "wiki/pm_state.template.md",
    "LOCAL_DIR": ".local",
    "REGRESSION_FLAG": ".local/regression.json",
    "LIVEGATE_FLAG": ".local/livegate.json",
    "BOARD_LOCK": ".local/board.lock",
    "LEASES_FILE": ".local/worktree-leases.json",
    "DOMAIN_PY": "tools/domain.py",
}


def _isolate_board_module(b, monkeypatch, root: Path) -> None:
    """board 모듈 REPO + 모든 쓰기-타깃 상수를 fixture `root` 로 격리한다(hermetic).

    게이트가 정상 발화하면 명령이 아예 안 도므로 무의미하지만, 게이트가 회귀로 미발화해도
    실 트리 대신 fixture 로만 쓰게 해 테스트가 실 worktree 를 오염시키지 못하게 한다(방어)."""
    pm = root / ".project_manager"
    monkeypatch.setattr(b, "REPO", root)
    for name, rel in _WRITE_TARGET_CONSTS.items():
        monkeypatch.setattr(b, name, pm / rel, raising=False)
    # 재바인딩 대상 파일들이 쓰일 base 디렉토리를 미리 만든다(refresh=board.md·log append 등).
    for d in (pm, pm / "wiki", pm / "wiki" / "log", pm / ".local"):
        d.mkdir(parents=True, exist_ok=True)


@pytest.fixture
def pm_home_worktree(tmp_path):
    return _make_pm_home_with_worktree(tmp_path)


# ── _has_real_board ───────────────────────────────────────────────────────

def test_has_real_board_split_true(tmp_path):
    b = _load_board()
    pm = tmp_path / ".project_manager"
    _make_real_board(pm, split=True)
    assert b._has_real_board(pm) is True


def test_has_real_board_legacy_wiki_true(tmp_path):
    b = _load_board()
    pm = tmp_path / ".project_manager"
    _make_real_board(pm, split=False)
    assert b._has_real_board(pm) is True


def test_has_real_board_scaffold_only_false(tmp_path):
    # 빈 scaffold(worktree 출하 형상)는 '실 board' 아님 — 이게 False 여야 worktree 가 flag 됨.
    b = _load_board()
    pm = tmp_path / ".project_manager"
    _make_scaffold_board(pm)
    assert b._has_real_board(pm) is False


def test_has_real_board_absent_false(tmp_path):
    b = _load_board()
    assert b._has_real_board(tmp_path / ".project_manager") is False


# ── _is_linked_worktree (runner 주입·hermetic) ────────────────────────────

def test_is_linked_worktree_differing_dirs_true():
    b = _load_board()
    r = _fake_runner("/x/.git/worktrees/w1", "/x/.git")
    assert b._is_linked_worktree(Path("/anywhere"), runner=r) is True


def test_is_linked_worktree_same_dirs_false():
    b = _load_board()
    r = _fake_runner("/x/.git", "/x/.git")
    assert b._is_linked_worktree(Path("/anywhere"), runner=r) is False


def test_is_linked_worktree_git_error_false():
    b = _load_board()

    def r(cmd, **kwargs):
        return types.SimpleNamespace(returncode=128, stdout="", stderr="not a git repo")

    assert b._is_linked_worktree(Path("/anywhere"), runner=r) is False


def test_is_linked_worktree_runner_raises_false():
    b = _load_board()

    def r(cmd, **kwargs):
        raise FileNotFoundError("git")

    assert b._is_linked_worktree(Path("/x"), runner=r) is False


def test_is_linked_worktree_real_worktree_true(pm_home_worktree):
    # 실 git worktree 는 default(실) runner 로도 linked True.
    b = _load_board()
    _pm_home, worktree = pm_home_worktree
    assert b._is_linked_worktree(worktree) is True


def test_is_linked_worktree_real_pm_home_false(pm_home_worktree):
    # PM 홈(main checkout 아님·비-git tmp) → False.
    b = _load_board()
    pm_home, _worktree = pm_home_worktree
    assert b._is_linked_worktree(pm_home) is False


# ── T-0465 read anchor display ───────────────────────────────────────────

def _read_argv(subcommand: str) -> list[str]:
    """각 read leaf의 최소 실행 argv. 새 read leaf는 이 표가 아니라 parametrize에 먼저 잡힌다."""
    argv = subcommand.split()
    if subcommand == "show":
        argv.append("T-0100")
    return argv


@pytest.mark.parametrize("subcommand", sorted(_load_board()._READ_SUBCOMMANDS))
def test_every_read_subcommand_surfaces_real_worktree_anchor(
        pm_home_worktree, monkeypatch, capsys, subcommand):
    """REPO가 정하는 실제 등록 worktree 앵커를 모든 read dispatch가 첫 줄에 표시한다.

    cwd를 바꾸지 않는다. 이 검증 축은 cwd가 아니라 dispatch의 `_READ_SUBCOMMANDS` 전수와
    실제 git worktree 판정이다.
    """
    b = _load_board()
    _pm_home, worktree = pm_home_worktree
    _isolate_board_module(b, monkeypatch, worktree)

    b.main(_read_argv(subcommand))
    first_line = capsys.readouterr().out.splitlines()[0]
    assert first_line == f"repo 앵커: {worktree} (worktree)"


def test_read_anchor_labels_only_real_board_owner_as_pm_home(
        pm_home_worktree, monkeypatch, capsys):
    """실 board 소유 PM 홈은 PM 홈, 일반 clone처럼 증거 없는 앵커는 비단언으로 표시한다."""
    b = _load_board()
    pm_home, _worktree = pm_home_worktree
    _isolate_board_module(b, monkeypatch, pm_home)
    assert b.main(["list"]) == 0
    assert capsys.readouterr().out.splitlines()[0] == f"repo 앵커: {pm_home} (PM 홈)"

    plain_clone = pm_home.parent / "plain-clone"
    plain_clone.mkdir()
    _git(["init", "-q", "-b", "main"], plain_clone)
    _isolate_board_module(b, monkeypatch, plain_clone)
    assert b.main(["list"]) == 0
    assert capsys.readouterr().out.splitlines()[0] == f"repo 앵커: {plain_clone} (역할 미상)"


@pytest.mark.parametrize("subcommand", sorted(_load_board()._MUTATION_SUBCOMMANDS))
def test_mutation_dispatch_never_prints_read_anchor(monkeypatch, subcommand):
    """모든 mutation leaf는 display-only read 앵커를 타지 않는다(rc/차단 경로 불변)."""
    b = _load_board()
    parts = subcommand.split()
    args = argparse.Namespace(
        cmd=parts[0],
        idea_cmd=parts[1] if parts[0] == "idea" else None,
        prefix_cmd=parts[1] if parts[0] == "prefix" else None,
        fn=lambda _args: 0,
    )
    monkeypatch.setattr(b, "build_parser", lambda: types.SimpleNamespace(parse_args=lambda _argv: args))
    monkeypatch.setattr(b, "_guard_worktree_misanchor", lambda _action: False)
    monkeypatch.setattr(
        b, "_print_read_anchor",
        lambda: pytest.fail(f"mutation {subcommand!r} printed a read anchor"),
    )
    assert b.main([]) == 0


# ── _pm_home_worktree_misanchor (3중 conjunction·runner 주입) ──────────────

def test_detector_flags_registered_worktree(tmp_path):
    b = _load_board()
    pm_home = tmp_path / "pm"
    _make_real_board(pm_home / ".project_manager", split=True)
    wt = pm_home / "work" / "product_1"
    _make_scaffold_board(wt / ".project_manager")
    r = _fake_runner("/x/.git/worktrees/product_1", "/x/.git")   # linked
    assert b._pm_home_worktree_misanchor(wt, runner=r) == pm_home


def test_detector_none_when_not_linked_worktree(tmp_path):
    # conjunction #2 실패: linked 아님 → None (솔로/standalone 무영향).
    b = _load_board()
    pm_home = tmp_path / "pm"
    _make_real_board(pm_home / ".project_manager", split=True)
    wt = pm_home / "work" / "product_1"
    _make_scaffold_board(wt / ".project_manager")
    r = _fake_runner("/x/.git", "/x/.git")   # not linked
    assert b._pm_home_worktree_misanchor(wt, runner=r) is None


def test_detector_none_when_anchor_owns_real_board(tmp_path):
    # conjunction #1 실패: 앵커가 *자기* 실 board 소유 → 정당(①-자체 board 사용 존중·flag 안 함).
    b = _load_board()
    pm_home = tmp_path / "pm"
    _make_real_board(pm_home / ".project_manager", split=True)
    wt = pm_home / "work" / "product_1"
    _make_real_board(wt / ".project_manager", split=False)   # worktree 가 board 소유
    r = _fake_runner("/x/.git/worktrees/product_1", "/x/.git")   # linked 이지만 board 소유
    assert b._pm_home_worktree_misanchor(wt, runner=r) is None


def test_detector_none_when_no_pm_home_ancestor(tmp_path):
    # conjunction #3 실패: linked·board 없음이나 상위 PM 홈(실 board) 없음 → None.
    b = _load_board()
    wt = tmp_path / "lonely" / "work" / "product_1"
    _make_scaffold_board(wt / ".project_manager")
    r = _fake_runner("/x/.git/worktrees/product_1", "/x/.git")
    assert b._pm_home_worktree_misanchor(wt, runner=r) is None


def test_detector_none_when_nested_but_unregistered(tmp_path):
    # 오탐 경계(reviewer should-fix): 무관 프레임워크 PM 홈(실 board) *하위*에 우연히 중첩된
    # linked worktree — 그 홈이 anchor 를 등록하지 않음(work/<name> 아님·git-common-dir 이 홈 밖).
    # 조건 3 등록 확인이 없으면 엉뚱한 pm_home 을 안내(오탐) → 이제 None.
    b = _load_board()
    pm_home = tmp_path / "unrelated_pm"
    _make_real_board(pm_home / ".project_manager", split=True)
    nested = pm_home / "vendor" / "someproj"          # work/<name> 관례 아님
    _make_scaffold_board(nested / ".project_manager")
    r = _fake_runner("/elsewhere/.git/worktrees/x", "/elsewhere/.git")   # common-dir 홈 밖·linked
    assert b._pm_home_worktree_misanchor(nested, runner=r) is None


def test_detector_flags_when_common_dir_under_pm_home(tmp_path):
    # 등록 확인 (b): work/<name> 관례가 아니어도 git-common-dir 이 pm_home 하위(ADR-0027
    # `<pm_home>/.repos/<repo>.git`)면 등록으로 인정 → pm_home 안내(오탐 아님).
    b = _load_board()
    pm_home = tmp_path / "pm"
    _make_real_board(pm_home / ".project_manager", split=True)
    nested = pm_home / "checkout"                      # work/ 관례 아님
    _make_scaffold_board(nested / ".project_manager")
    common = str(pm_home / ".repos" / "p.git")         # pm_home 하위
    r = _fake_runner(common + "/worktrees/x", common)
    assert b._pm_home_worktree_misanchor(nested, runner=r) == pm_home


def test_registers_worktree_branches(tmp_path):
    # _registers_worktree 세 갈래 직접 검증: (a) work/<name> · (b) common-dir 하위 · 둘 다 아님.
    b = _load_board()
    pm_home = tmp_path / "pm"
    assert b._registers_worktree(pm_home, pm_home / "work" / "repo_1",
                                 runner=_fake_runner("/x", "/x")) is True          # (a)
    common = str(pm_home / ".repos" / "p.git")
    assert b._registers_worktree(pm_home, pm_home / "checkout",
                                 runner=_fake_runner(common + "/wt", common)) is True  # (b)
    assert b._registers_worktree(pm_home, pm_home / "vendor" / "x",
                                 runner=_fake_runner("/z/.git", "/z/.git")) is False    # 둘 다 아님


def test_detector_flags_real_worktree_default_runner(pm_home_worktree):
    # 실 git worktree + default(실) runner 로 end-to-end detector 발화.
    b = _load_board()
    pm_home, worktree = pm_home_worktree
    assert b._pm_home_worktree_misanchor(worktree) == pm_home


# ── mutation dispatch 게이트: 전 mutation subcommand fail-loud (실 git worktree) ──

# 모든 mutation subcommand 의 최소 유효 argv — worktree cwd main() 실행 시 dispatch 게이트가
# 전수 fail-loud 하는지 확인(실측 사례 2·3 은 이 중 new·ticket_finish; 나머지는 클래스 확장).
_MUTATION_ARGVS = [
    ["new", "stray 유발"],
    ["promote", "T-0100"],
    ["claim", "T-0100"],
    ["complete", "T-0100"],
    ["block", "T-0100", "--reason", "r"],
    ["unclaim", "T-0100"],
    ["unblock", "T-0100"],
    ["init"],
    ["migrate-identity"],
    ["promote-scope", "somedoc.md", "--to", "shared"],
    ["reid", "T-0100", "T-0101"],
    ["refresh"],
    ["verified-at-backfill"],
    ["verified-at-repin", "--repo", "self"],
    ["idea", "new", "아이디어"],
    ["idea", "promote", "0001"],
    ["idea", "kill", "0001"],
    ["prefix", "rename", "AAA", "BBB"],
    ["prefix", "strip", "AAA"],
    ["prefix", "merge", "AAA", "--into", "BBB"],
    ["prefix", "delete", "AAA"],
]


def test_mutation_argv_list_covers_all_mutations():
    """_MUTATION_ARGVS 가 _MUTATION_SUBCOMMANDS 전수를 커버 — 신규 mutation 이 분류되면 여기도
    argv 를 추가하도록 강제(fail-loud 재현 테스트 누락 방지)."""
    b = _load_board()
    covered = {b._resolved_subcommand(b.build_parser().parse_args(a)) for a in _MUTATION_ARGVS}
    assert covered == set(b._MUTATION_SUBCOMMANDS)


@pytest.mark.parametrize("argv", _MUTATION_ARGVS, ids=lambda a: " ".join(a[:2]))
def test_mutation_dispatch_fails_loud_from_worktree(pm_home_worktree, monkeypatch, capsys, argv):
    """모든 mutation subcommand 를 worktree cwd(실 linked worktree)에서 main() 실행 → dispatch
    게이트가 fail-loud(rc 1)·명령 로직 미실행(stray 산출 0).

    board 모듈을 fixture 로 완전 격리(_isolate_board_module) — 게이트가 회귀로 미발화해도 실
    트리 대신 fixture 로만 쓰게 해 테스트가 실 worktree 를 오염시키지 못하게 한다."""
    b = _load_board()
    pm_home, worktree = pm_home_worktree
    _isolate_board_module(b, monkeypatch, worktree)   # 호출 시점 앵커 = worktree (오실행 재현)
    rc = b.main(argv)
    assert rc == 1
    err = capsys.readouterr().err
    assert "worktree" in err
    assert str(pm_home) in err   # 안내에 실제 PM 홈 경로
    # 명령 로직 미실행 확증: (격리된) worktree 트리에 stray 티켓/idea 생성 0
    pm = worktree / ".project_manager"
    assert list((pm / "wiki" / "tickets" / "open").glob("T-*.md")) == []
    assert list((pm / "wiki" / "ideas" / "open").glob("*.md")) == []


def test_new_from_worktree_creates_no_stray_ticket(pm_home_worktree, monkeypatch, capsys):
    # 실측 사례 (3): worktree cwd `board.py new` → stray `wiki/tickets/T-0001` 미생성.
    b = _load_board()
    pm_home, worktree = pm_home_worktree
    _isolate_board_module(b, monkeypatch, worktree)
    rc = b.main(["new", "stray 유발"])
    assert rc == 1
    open_dir = worktree / ".project_manager" / "wiki" / "tickets" / "open"
    assert list(open_dir.glob("T-*.md")) == []   # stray 티켓 0
    # 파생 board.md·드래프트도 안 생김(게이트가 명령 진입 전 차단)
    assert not (worktree / ".project_manager" / "wiki" / "board.md").exists()
    capsys.readouterr()


def test_ticket_finish_from_worktree_fails_loud_no_stray_log(pm_home_worktree,
                                                             monkeypatch, capsys):
    # 실측 사례 (2): worktree cwd `ticket_finish` → stray `wiki/log/current.md` 미생성.
    tf = _load_tf()
    pm_home, worktree = pm_home_worktree
    monkeypatch.setattr(tf, "REPO", worktree)
    monkeypatch.setattr(tf, "BOARD_PY", BOARD_PY)   # detector deep-import 은 실 board.py
    log_file = worktree / ".project_manager" / "wiki" / "log" / "current.md"
    monkeypatch.setattr(tf, "LOG_FILE", log_file)
    rc = tf.main(["T-0100"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "worktree" in err
    assert str(pm_home) in err
    assert not log_file.exists()   # stray log 0 (부기 어떤 단계도 착지 전 중단)


# ── 실측 사례 (1) lint 오경보: 근본 detect·읽기 경로 무-게이트 ───────────────

def test_lint_falsealarm_root_detected_but_read_paths_ungated(pm_home_worktree, monkeypatch, capsys):
    # 실측 사례 (1·PM 70): worktree cwd `board.py lint` 가 render-leak 등 오경보.
    # 근본(misanchor)은 이제 detector 가 잡는다. 단 §결정: 읽기 경로(lint 등)는 무-게이트 —
    # read 는 _MUTATION_SUBCOMMANDS 밖이라 dispatch 게이트가 발화하지 않고 그대로 read-only 동작.
    b = _load_board()
    pm_home, worktree = pm_home_worktree
    # (a) 근본 misanchor 가 detector 로 식별됨(오경보 명령의 근본은 이제 잡힌다)
    assert b._pm_home_worktree_misanchor(worktree) == pm_home
    # (b) lint(오경보 명령)·기타 read 는 mutation 분류 밖 → dispatch 게이트 미발화(§결정: read 무변경)
    for name in ("lint", "list", "show"):
        assert name in b._READ_SUBCOMMANDS and name not in b._MUTATION_SUBCOMMANDS
    # (c) 행위 확증: worktree 에서 read 명령(list)은 게이트 통과·misanchor 중단 메시지 없음
    _isolate_board_module(b, monkeypatch, worktree)
    rc = b.main(["list"])
    out = capsys.readouterr()
    assert "worktree(코드 전용) 트리에서 실행" not in (out.err + out.out)
    assert rc == 0


# ── 메타 가드: mutation 분류 전수·dispatch 결정 (미래 누락 클래스 폐쇄) ─────────

def _all_registered_subcommands(parser) -> set[str]:
    """argparse 파서에서 등록된 모든 subcommand(idea/prefix 서브그룹은 `<group> <sub>` 점표기)를
    전수 열거한다 — 분류 상수 대조용."""
    names: set[str] = set()
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        for name, subparser in action.choices.items():
            nested = [a for a in subparser._actions
                      if isinstance(a, argparse._SubParsersAction)]
            if nested:
                for na in nested:
                    for sub_name in na.choices:
                        names.add(f"{name} {sub_name}")
            else:
                names.add(name)
    return names


def test_every_subcommand_is_classified():
    """신규 mutation subcommand 추가 시 가드 누락 클래스를 잡는다 — 실 등록 subcommand 전수가
    (mutation ∪ read ∪ sidecar) 와 정확히 일치해야 한다(미분류/유령분류 즉시 fail)."""
    b = _load_board()
    registered = _all_registered_subcommands(b.build_parser())
    classified = (set(b._MUTATION_SUBCOMMANDS)
                  | set(b._READ_SUBCOMMANDS) | set(b._SIDECAR_SUBCOMMANDS))
    assert registered == classified, (
        f"미분류(신규 mutation 가능성): {sorted(registered - classified)} · "
        f"유령 분류(제거된 명령): {sorted(classified - registered)}"
    )


def test_classification_sets_disjoint():
    b = _load_board()
    m, r, s = (set(b._MUTATION_SUBCOMMANDS),
               set(b._READ_SUBCOMMANDS), set(b._SIDECAR_SUBCOMMANDS))
    assert m.isdisjoint(r) and m.isdisjoint(s) and r.isdisjoint(s)


def test_dispatch_gate_decision_matches_classification():
    """dispatch 게이트의 발화 결정(_resolved_subcommand ∈ _MUTATION_SUBCOMMANDS)이 분류대로인지 —
    mutation 은 발화·read/sidecar 는 미발화. sidecar(regression/livegate)는 *실행 없이* 결정만 확인."""
    b = _load_board()

    def _fires(argv):
        return b._resolved_subcommand(b.build_parser().parse_args(argv)) in b._MUTATION_SUBCOMMANDS

    for argv in (["new", "t"], ["claim", "T-0100"], ["refresh"], ["init"],
                 ["migrate-identity"], ["promote-scope", "f.md", "--to", "shared"],
                 ["reid", "T-0100", "T-0101"], ["idea", "new", "t"],
                 ["prefix", "rename", "AAA", "BBB"]):
        assert _fires(argv), argv
    for argv in (["list"], ["show", "T-0100"], ["lint"],
                 ["regression", "check"], ["livegate", "check"],
                 ["idea", "list"], ["prefix", "list"]):
        assert not _fires(argv), argv


# ── 오탐 0: PM 홈·솔로/standalone 무회귀 ──────────────────────────────────

def test_pm_home_cwd_no_false_positive(tmp_path, monkeypatch, capsys):
    # PM 홈(실 board 소유·worktree 아님) → detector None·guard 무발화.
    b = _load_board()
    pm_home = tmp_path / "pmhome"
    _make_real_board(pm_home / ".project_manager", split=True)
    assert b._pm_home_worktree_misanchor(pm_home) is None
    monkeypatch.setattr(b, "REPO", pm_home)
    assert b._guard_worktree_misanchor("board.py new") is False
    assert capsys.readouterr().err == ""


def test_solo_standalone_first_ticket_no_false_positive(tmp_path, monkeypatch, capsys):
    # 솔로/standalone: 일반 git repo(worktree 아님)·빈 board scaffold(첫 티켓 전) → None.
    b = _load_board()
    solo = tmp_path / "solo"
    (solo / ".project_manager").mkdir(parents=True)
    _make_scaffold_board(solo / ".project_manager")
    _git(["init", "-q", "-b", "main"], solo)
    assert b._is_linked_worktree(solo) is False
    assert b._pm_home_worktree_misanchor(solo) is None
    monkeypatch.setattr(b, "REPO", solo)
    assert b._guard_worktree_misanchor("board.py new") is False
    assert capsys.readouterr().err == ""


def test_non_git_tree_no_false_positive(tmp_path):
    # 비-git 트리(standalone) → fail-soft None (오탐 0).
    b = _load_board()
    plain = tmp_path / "plain"
    (plain / ".project_manager").mkdir(parents=True)
    _make_scaffold_board(plain / ".project_manager")
    assert b._is_linked_worktree(plain) is False
    assert b._pm_home_worktree_misanchor(plain) is None


def test_pm_home_cwd_new_proceeds_past_guard(tmp_path, monkeypatch, capsys):
    # 오탐 0 회귀(dispatch): PM 홈에서 `board.py new` 는 게이트를 통과해 실제로 티켓을 발행한다.
    b = _load_board()
    pm_home = tmp_path / "pmhome"
    _make_real_board(pm_home / ".project_manager", split=True)
    _isolate_board_module(b, monkeypatch, pm_home)
    (pm_home / ".project_manager" / ".local").mkdir(parents=True, exist_ok=True)
    # 템플릿 배치 (cmd_new 가 load_ticket(template_file()) 로 읽음)
    tmpl = pm_home / ".project_manager" / "board" / "tickets" / "_template.md"
    tmpl.write_text("---\nid: T-NNNN\ntitle: <제목>\ntouches: []\n---\n# T-NNNN — <제목>\n\n## 목표\n채움.\n",
                    encoding="utf-8")
    rc = b.main(["new", "정상 발행"])   # 실 CLI 경로(dispatch 게이트 포함)
    out = capsys.readouterr()
    assert rc == 0   # 게이트 통과 → 정상 발행
    assert "worktree(코드 전용) 트리에서 실행" not in (out.err + out.out)
    open_dir = pm_home / ".project_manager" / "board" / "tickets" / "open"
    assert len(list(open_dir.glob("T-*.md"))) == 1


def test_read_anchor_is_first_line_even_when_stderr_present(tmp_path):
    """앵커는 **실제 파이프 형상**에서도 첫 줄이다 — stdout 버퍼링 회귀 가드 (T-0465·codex 게이트).

    in-process `capsys` 테스트는 stdout 버퍼링을 거치지 않아 이 클래스를 구조적으로 못 본다:
    stdout 은 파이프/리다이렉션에서 블록 버퍼링되는데 stderr 는 unbuffered 라, `print(...)` 에
    `flush=True` 가 없으면 stderr 가 앵커보다 먼저 흘러나온다(실측: `show T-9999 2>&1 | head` 가
    not-found 를 선행 출력). 그래서 이 테스트만 **subprocess + stderr 병합**으로 순서를 고정한다.
    """
    # 존재할 수 없는 ID 를 쓴다 — 실재하는 ID 를 쓰면 stderr 가 안 나와 이 테스트가 조용히
    # 무의미해진다(codex 게이트 지적). 아래에서 rc≠0 과 stderr 비어있지 않음을 함께 단언해,
    # 전제가 깨지면 통과가 아니라 loud 실패가 되게 한다.
    missing_id = "T-0000-DOES-NOT-EXIST"
    result = subprocess.run(
        [sys.executable, str(BOARD_PY), "show", missing_id],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False, stdin=subprocess.DEVNULL,
    )
    assert result.returncode != 0, "전제 붕괴 — 없는 ID 조회가 성공했다(테스트 무의미)"
    assert result.stderr.strip(), "전제 붕괴 — stderr 가 비어 순서 검증이 무의미하다"
    # 실제 병합 순서를 보려면 stdout·stderr 가 **한 파이프**로 들어와야 한다. `shell=True` +
    # `2>&1` 대신 `stderr=STDOUT` 을 쓴다 — 셸 무의존이라 Windows 에서도 같고 인자 인용 문제도 없다.
    # `encoding` 명시 필수: 자식은 UTF-8 을 내보내는데 text=True 만 주면 로캘 코덱으로 디코딩해
    # CP949 콘솔에서 한글 앵커가 UnicodeDecodeError 를 낸다(엔진 subprocess 관례와 동일).
    merged = subprocess.run(
        [sys.executable, str(BOARD_PY), "show", missing_id],
        cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
        check=False, stdin=subprocess.DEVNULL,
    ).stdout
    assert result.stdout.splitlines()[0].startswith("repo 앵커: ")
    assert merged.splitlines()[0].startswith("repo 앵커: "), (
        f"stderr 가 앵커보다 먼저 나왔다(버퍼링 회귀) — 첫 줄: {merged.splitlines()[0]!r}"
    )
