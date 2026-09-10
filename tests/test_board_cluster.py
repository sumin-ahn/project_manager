"""클러스터 장부 — `cluster new|show` · 티켓 `cluster` 필드 자동 귀속 · `cluster-*` lint.

여기서 지키는 성질은 넷이다.
  (1) `cluster new` 가 장부와 통합 브랜치를 만들고 멤버 티켓에 귀속을 박는다.
  (2) 발행(`new`)·승격(`promote`)이 그 티켓의 크기 1 장부를 만들고 기준 브랜치·예산을 박는다
      (활성 묶음에 자동으로 끼우지 않는다 — 묶는 것은 사람 선언뿐).
  (3) 재개방(`reopen`)도 같은 자리에서 귀속하고, 멤버는 **장부만** 답한다(합성 0).
  (4) 장부 관측(멤버 부재·중복 귀속)은 advisory 로만 보인다(never-block · 장부만 읽는다).

hermetic 패턴은 `test_board_new_draft_gate.py` 와 동형 — 실 board git + bare remote 를 tmp 에
세우고 board 모듈의 `REPO` 를 그 tmp 로 재앵커한다. 코드 트리 축은 두 형상을 다 세운다:
`env` 는 PM 홈 자신이 코드 트리인 N=1 채택자(홈 슬롯 행 `slot="."`), `split_env` 는 PM 홈과
코드 트리가 **다른 디렉터리**인 adopter#0 형상이다. 겹친 형상 하나만 있으면 코드 트리를 PM
홈으로 접는 해소가 정답과 값으로 구분되지 않는다.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from _slot_seed import SLOT, SLOT_REPO, lease_row, seed_slot
from conftest import anchor_board_module

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

requires_git = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git 바이너리 부재 — 실 git 통합 케이스 skip.",
)

_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
}

# 코드 트리(통합 브랜치가 사는 git)의 기점 브랜치 — task 세션의 통합 브랜치와 같은 형상.
_BASE_BRANCH = "task/main"

# 발행 즉시 open/ 에 놓이는 채워진 템플릿(placeholder 0) — draft 격리 축을 타지 않는 발행.
_TEMPLATE_TEXT = (
    "---\n"
    "id: T-NNNN\n"
    "title: <제목>\n"
    "status: open\n"
    "created_by:\n"
    "claimed_by:\n"
    "claimed_at:\n"
    "completed_at:\n"
    "depends_on: []\n"
    "blocks: []\n"
    "touches: []\n"
    "estimate: small\n"
    "design: n/a\n"
    "cluster:\n"
    "tags: []\n"
    "---\n\n"
    "# T-NNNN — <제목>\n\n"
    "## 목표\n실제 목표를 채웠다.\n\n"
    "## 인터페이스\n실제 인터페이스 규격.\n\n"
    "## 결정\n실제 구현 방향.\n\n"
    "## 완료 조건 (Definition of Done)\n- [ ] 실제 산출물\n\n"
    "## 참고\n- 실제 참고 사항\n\n"
    "## 메모\n"
)

# 발행 게이트가 draft 로 남기는 미충전 템플릿(placeholder 그대로).
_DRAFT_TEMPLATE_TEXT = _TEMPLATE_TEXT.replace(
    "## 목표\n실제 목표를 채웠다.", "## 목표\n무엇을 만들 / 바꿀 / 검증할지 1~3 문장."
).replace("## 참고\n- 실제 참고 사항", "## 참고\n- 관련 ADR / spec: [[xxxxx]]")


def _load_tool(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          check=False)


def _make_board_git(root: Path, *, remote: Path, template: str = _TEMPLATE_TEXT) -> Path:
    """`<root>/.project_manager/board/` 에 실 board git(tickets/ + _template.md + remote) 을 만든다."""
    board = root / ".project_manager" / "board"
    for status in ("open", "claimed", "blocked", "done"):
        (board / "tickets" / status).mkdir(parents=True, exist_ok=True)
    (board / "tickets" / "_template.md").write_text(template, encoding="utf-8")
    _git(["init", "-q", "-b", "main"], board)
    _git(["remote", "add", "origin", str(remote)], board)
    _git(["add", "-A"], board)
    _git(["commit", "-qm", "board init"], board)
    _git(["push", "-q", "-u", "origin", "main"], board)
    return board


def _make_code_git(root: Path) -> None:
    """코드 트리 — 통합 브랜치의 기점(`task/main`)을 가진 실 git."""
    _git(["init", "-q", "-b", _BASE_BRANCH], root)
    (root / "code.txt").write_text("seed\n", encoding="utf-8", newline="\n")
    _git(["add", "--", "code.txt"], root)
    _git(["commit", "-qm", "code seed"], root)


@pytest.fixture
def board(tmp_path, monkeypatch):
    mod = _load_tool("board")
    anchor_board_module(mod, tmp_path, monkeypatch)
    # 코드 트리 해소(`_claim_code_tree`)가 읽는 장부도 tmp 로 재앵커한다 — 실 트리의 리스를
    # 읽으면 이 파일의 답이 실행 환경에 따라 바뀐다.
    monkeypatch.setattr(
        mod, "LEASES_FILE",
        tmp_path / ".project_manager" / ".local" / "worktree-leases.json")
    seed_slot(tmp_path)
    mod.BOARD_FILE.parent.mkdir(parents=True, exist_ok=True)
    mod.BOARD_FILE.touch()
    for key, val in _GIT_IDENTITY.items():
        monkeypatch.setenv(key, val)
    return mod


@pytest.fixture
def env(board, tmp_path):
    """board git + 코드 git 한 쌍 — 클러스터 축이 쓰는 두 저장소."""
    bare = tmp_path / "bare"
    _git(["init", "--bare", "-q", "-b", "main", str(bare)], tmp_path)
    board_dir = _make_board_git(tmp_path, remote=bare)
    _make_code_git(tmp_path)
    # 장부가 가리키는 작업 폴더에도 같은 기점의 코드 git 을 둔다 — 명시 정체성(`--repo`)은 그
    # 슬롯 트리로 해소되고, 인자 전무 경로는 종전대로 PM 홈 트리를 본다.
    (tmp_path / SLOT).mkdir(parents=True, exist_ok=True)
    _make_code_git(tmp_path / SLOT)
    return board, board_dir


# 분리 형상의 슬롯 수 — 실 PM 홈 장부와 같이 **여러 슬롯이 동시에 대여된** 상태다. 슬롯이
# 하나뿐이면 세션이 그 행 하나로 유도돼(`session_name` 단일-lease 층) 활성 슬롯이 늘 해소되고,
# 재발한 형상(세션 미해소 → 코드 없는 PM 홈으로 접힘)이 재현되지 않는다.
_SPLIT_SLOT_COUNT = 2
# 그 슬롯들이 공유하는 세션 값 — 실 장부와 같이 행마다 같은 값이라 유도가 성립하지 않는다.
_SPLIT_SESSION = "main"


@pytest.fixture
def split_env(board, tmp_path):
    """PM 홈 ≠ 코드 트리 — 통합 브랜치는 슬롯 worktree 의 git 에만 있다(adopter#0 형상).

    PM 홈은 자기 git(브랜치 `main`)이고 코드는 `work/proj_<N>` 슬롯의 별 git(기점 `task/main`)
    이다. 리스 장부는 그 슬롯들을 대여 중으로 적어 `--repo`/`--slot` 이 코드 트리를 지목하게
    한다. 겹친 형상(`env`)에서는 PM 홈을 코드 트리로 접어도 답이 같아 결함이 보이지 않는다.
    """
    bare = tmp_path / "bare"
    _git(["init", "--bare", "-q", "-b", "main", str(bare)], tmp_path)
    board_dir = _make_board_git(tmp_path, remote=bare)
    # PM 홈도 git 이지만 통합 브랜치의 기점(`task/main`)이 없다 — 코드가 없는 트리.
    _git(["init", "-q", "-b", "main"], tmp_path)
    (tmp_path / "home.txt").write_text("home\n", encoding="utf-8", newline="\n")
    _git(["add", "--", "home.txt"], tmp_path)
    _git(["commit", "-qm", "home seed"], tmp_path)
    slots = [tmp_path / "work" / f"{SLOT_REPO}_{n}"
             for n in range(1, _SPLIT_SLOT_COUNT + 1)]
    for slot in slots:
        slot.mkdir(parents=True)
        _make_code_git(slot)
    board.LEASES_FILE.write_text(
        json.dumps({"leases": [
            lease_row(slot=f"work/{SLOT_REPO}_{n}", session=_SPLIT_SESSION)
            for n in range(1, _SPLIT_SLOT_COUNT + 1)
        ]}, ensure_ascii=False), encoding="utf-8")
    return board, board_dir, slots[0]


def _new_args(title: str, **kwargs) -> argparse.Namespace:
    values = dict(title=title, touches=None, depends=None, tag=None,
                  estimate="small", prefix=None, user=None, session=None,
                  repo=None, slot=None, task=None)
    values.update(kwargs)
    return argparse.Namespace(**values)


def _subparser_help(parser: argparse.ArgumentParser, *path: str) -> str:
    """`board.py <path...>` 서브파서의 한 줄 help — 부모 액션의 pseudo-action 이 소유한다."""
    node = parser
    help_text = ""
    for name in path:
        sub = next(action for action in node._actions
                   if action.__class__.__name__ == "_SubParsersAction")
        help_text = next(
            choice for choice in sub._choices_actions if choice.dest == name).help or ""
        node = sub.choices[name]
    return help_text


def _cluster_args(action: str, name: str, **kwargs) -> argparse.Namespace:
    """`cluster new|show` 인자 — 코드 트리는 명시가 기본이다(`--repo <등록 repo>`).

    `cluster new` 는 트리를 추측하지 않으므로 정체성 없는 호출은 거부 케이스에서만 쓴다
    (`repo=None` 을 명시적으로 넘긴다).
    """
    values = dict(cluster_cmd=action, name=name, tickets=None, spike=None,
                  repo=SLOT_REPO, slot=None, task=None)
    values.update(kwargs)
    return argparse.Namespace(**values)


def _issue_ticket(board, title: str) -> str:
    """발행 1건 — 발행된 티켓 ID."""
    before = {path.name for path in _all_ticket_files(board)}
    assert board.cmd_new(_new_args(title)) == 0
    created = [path for path in _all_ticket_files(board) if path.name not in before]
    assert len(created) == 1, created
    return board._canonical_ticket_id(created[0])


def _all_ticket_files(board) -> list[Path]:
    tickets = board.tickets_dir()
    paths: list[Path] = []
    for status in (*board.STATUS_DIRS, ".drafts"):
        directory = tickets / status
        if directory.is_dir():
            paths.extend(sorted(directory.glob("T-*.md")))
    return paths


def _ticket_fm(board, tid: str) -> dict:
    _status, path = board.find_ticket_exact(tid)
    return board.load_ticket(path)[0]


def _write_legacy_ticket(board_dir: Path, tid: str, *, status: str = "done",
                         cluster: str | None = None,
                         discarded_from: str | None = None) -> Path:
    """장부 도입 이전 형상의 티켓 — 발행 경로를 타지 않고 `<status>/` 에 직접 놓는다."""
    text = (_TEMPLATE_TEXT.replace("T-NNNN", tid).replace("<제목>", "구세대")
            .replace("status: open", f"status: {status}"))
    text = (text.replace("cluster:\n", f"cluster: {cluster}\n") if cluster
            else text.replace("cluster:\n", ""))
    if discarded_from is not None:
        text = text.replace("tags: []", f"discarded_from: {discarded_from}\ntags: []")
    path = board_dir / "tickets" / status / f"{tid}-legacy.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    _git(["add", "-A"], board_dir)
    _git(["commit", "-qm", f"legacy {tid}"], board_dir)
    return path


def _reopen_args(tid: str, reason: str) -> argparse.Namespace:
    """`reopen` 서브파서가 만드는 Namespace — 정체성 인자가 없다(파서 실측)."""
    return argparse.Namespace(id=tid, reason=reason)


def _head_files(board_dir: Path) -> set[str]:
    """board git 마지막 커밋이 담은 경로들 — mutation 의 커밋 스코프."""
    out = _git(["show", "--name-only", "--pretty=format:", "HEAD"], board_dir).stdout
    return {line.strip() for line in out.splitlines() if line.strip()}


# ════════════════════════════════════════════════════════════════════════
# cluster new — 장부 · 통합 브랜치 · 멤버 귀속
# ════════════════════════════════════════════════════════════════════════

@requires_git
def test_cluster_new_writes_ledger_branch_and_membership(env, capsys):
    board, board_dir = env
    first = _issue_ticket(board, "첫 멤버")
    second = _issue_ticket(board, "둘째 멤버")
    capsys.readouterr()

    rc = board.cmd_cluster(_cluster_args(
        "new", "wave", tickets=f"{first},{second}", spike="raw/spikes/design.md"))

    assert rc == 0
    ledger = board.load_cluster("C-wave")
    assert ledger is not None
    assert ledger["id"] == "C-wave"
    assert ledger["tickets"] == [first, second]
    assert ledger["branch"] == "task/wave"
    assert ledger["base_branch"] == _BASE_BRANCH
    assert ledger["spike"] == "raw/spikes/design.md"
    assert ledger["budget"] == board.CLUSTER_BUDGET_DEFAULT
    assert "replans" not in ledger
    assert ledger["status"] == board.CLUSTER_STATUS_OPEN
    # 장부 파일은 STATUS_DIRS 밖 sibling 이다(상태 순회에 섞이지 않는다).
    assert board.cluster_ledger_path("C-wave") == (
        board_dir / "tickets" / "clusters" / "C-wave.md")
    # 통합 브랜치가 (명시 정체성이 가리킨) 작업 폴더 코드 git 에 실재하고 기점과 같다.
    code = board.REPO / SLOT
    assert board._cluster_branch_state(str(code), "task/wave") is True
    tip = _git(["rev-parse", "task/wave"], code).stdout.strip()
    base_tip = _git(["rev-parse", _BASE_BRANCH], code).stdout.strip()
    assert tip == base_tip
    # 멤버 티켓 frontmatter 가 같은 묶음을 가리킨다.
    assert _ticket_fm(board, first)["cluster"] == "C-wave"
    assert _ticket_fm(board, second)["cluster"] == "C-wave"
    assert board.cluster_members("C-wave") == (first, second)
    # 장부와 멤버 명세가 board-git 에 함께 실린다.
    committed = _git(["show", "--stat", "--name-only", "--format=", "HEAD"],
                     board_dir).stdout
    assert "tickets/clusters/C-wave.md" in committed, committed


@requires_git
def test_cluster_new_puts_the_branch_in_the_declared_code_tree(split_env, capsys):
    """분리 형상 — 통합 브랜치는 **선언된 슬롯의 git** 에 생기고 PM 홈엔 생기지 않는다.

    기점도 그 트리에서 읽는다: PM 홈의 현재 브랜치(`main`)가 아니라 코드 트리의 현재
    브랜치(`task/main`)가 장부에 실린다.
    """
    board, _board_dir, code = split_env
    first = _issue_ticket(board, "분리 형상 멤버")
    capsys.readouterr()

    rc = board.cmd_cluster(_cluster_args(
        "new", "split", tickets=first, repo=SLOT_REPO, slot=1))

    assert rc == 0
    ledger = board.load_cluster("C-split")
    assert ledger["branch"] == "task/split"
    assert ledger["base_branch"] == _BASE_BRANCH
    assert board._cluster_branch_state(str(code), "task/split") is True
    # PM 홈은 코드가 없는 트리다 — 여기에 브랜치가 생기면 아무도 못 쓴다.
    assert board._cluster_branch_state(str(board.REPO), "task/split") is False
    tip = _git(["rev-parse", "task/split"], code).stdout.strip()
    assert tip == _git(["rev-parse", _BASE_BRANCH], code).stdout.strip()


@requires_git
def test_cluster_new_refuses_when_the_code_tree_is_not_declared(split_env, capsys):
    """정체성 없는 `cluster new` 는 첫 부작용 앞에서 거부한다 — 트리를 추측하지 않는다.

    이 명령은 git 브랜치를 만들고 기점을 장부에 박는 쓰기다. 추측하면 PM 홈(코드 없는 트리)
    에 브랜치가 생기고 그 트리의 현재 브랜치가 기점으로 실린다(재발 5회의 형태).
    """
    board, _board_dir, code = split_env
    first = _issue_ticket(board, "미선언 트리")
    capsys.readouterr()

    rc = board.cmd_cluster(_cluster_args(
        "new", "bare", tickets=first, repo=None, slot=None, task=None))

    assert rc == 1
    err = capsys.readouterr().err
    assert "코드 트리를 명시로 받는다" in err
    assert "--repo" in err and "--task" in err
    # 첫 쓰기 앞 거부 — 장부도 브랜치도 멤버 귀속 변경도 없다.
    assert not board.cluster_ledger_path("C-bare").exists()
    assert board._cluster_branch_state(str(board.REPO), "task/bare") is False
    assert board._cluster_branch_state(str(code), "task/bare") is False
    assert _ticket_fm(board, first)["cluster"] == f"C-{first}"


def test_cluster_new_help_declares_the_code_tree_requirement(board):
    """`cluster new` 서브파서 help 가 코드 트리 명시 요구를 말한다.

    거부 판정은 `_cmd_cluster_new` 가 소유하고 help 는 그것을 인용만 한다. help 가 침묵하면
    `--help` 만 읽고 실행한 사람이 첫 쓰기 앞 rc=1 을 실행 시점에야 만난다.
    """
    help_text = _subparser_help(board.build_parser(), "cluster", "new")

    assert "--repo" in help_text and "--slot" in help_text
    assert "--task" in help_text
    assert "명시" in help_text


@requires_git
def test_cluster_new_absorbs_the_auto_size_one_ledgers(env, capsys):
    """발행이 만든 크기 1 장부는 흡수된다 — 빈 자동 장부는 남지 않는다."""
    board, _board_dir = env
    first = _issue_ticket(board, "흡수 대상")
    second = _issue_ticket(board, "흡수 대상 둘")
    capsys.readouterr()
    assert board.cluster_ledger_path(f"C-{first}").is_file()

    assert board.cmd_cluster(_cluster_args(
        "new", "absorb", tickets=f"{first},{second}")) == 0

    assert not board.cluster_ledger_path(f"C-{first}").exists()
    assert not board.cluster_ledger_path(f"C-{second}").exists()
    assert board.cluster_members("C-absorb") == (first, second)
    assert board._cluster_of_record(first) == "C-absorb"
    # 흡수는 board-git 에도 도달한다 — 빈 자동 장부가 공유 board 에 남지 않는다.
    tracked = _git(["ls-tree", "-r", "--name-only", "HEAD"], _board_dir).stdout
    assert f"tickets/clusters/C-{first}.md" not in tracked, tracked
    assert "tickets/clusters/C-absorb.md" in tracked, tracked
    # 중복 귀속 advisory 가 생기지 않는다(장부는 하나뿐).
    kinds = [kind for _id, kind, _detail in board.lint_clusters()]
    assert board._CLUSTER_DUPLICATE_LINT_KIND not in kinds


@requires_git
def test_cluster_new_is_loud_when_the_absorbed_ledger_cannot_be_removed(
        env, capsys, monkeypatch):
    """자동 장부 삭제 실패는 삼키지 않는다 — 삼키면 rc 0 뒤 중복 귀속이 남는다."""
    board, _board_dir = env
    first = _issue_ticket(board, "삭제 실패")
    second = _issue_ticket(board, "삭제 실패 둘")
    capsys.readouterr()
    auto = board.cluster_ledger_path(f"C-{first}")
    real_unlink = Path.unlink

    def _refuse_unlink(self, *args, **kwargs):
        if Path(self) == auto:
            raise PermissionError(13, "삭제 거부")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _refuse_unlink)

    rc = board.cmd_cluster(_cluster_args(
        "new", "absorbfail", tickets=f"{first},{second}"))

    assert rc == 1
    err = capsys.readouterr().err
    assert "옛 장부에서" in err and first in err and "중복 귀속" in err
    # 옛 장부는 멤버를 그대로 쥐고 있다 — 그 사실이 rc 와 lint 로 함께 보인다.
    assert auto.is_file()
    assert board.cluster_tickets(board.load_cluster(f"C-{first}")) == [first]
    assert board.cluster_members("C-absorbfail") == (first, second)
    kinds = {kind for _id, kind, _detail in board.lint_clusters()}
    assert board._CLUSTER_DUPLICATE_LINT_KIND in kinds


@requires_git
def test_cluster_new_refuses_unknown_member_without_writing(env, capsys):
    board, _board_dir = env
    known = _issue_ticket(board, "실재")
    capsys.readouterr()

    rc = board.cmd_cluster(_cluster_args(
        "new", "ghost", tickets=f"{known},T-9999"))

    assert rc == 2
    assert "ticket not found: T-9999" in capsys.readouterr().err
    assert not board.cluster_ledger_path("C-ghost").exists()
    # 거부는 첫 쓰기 앞이다 — 멤버 귀속도 브랜치도 생기지 않는다.
    assert _ticket_fm(board, known)["cluster"] == f"C-{known}"
    assert board._cluster_branch_state(str(board.REPO), "task/ghost") is False


@requires_git
def test_cluster_new_refuses_a_member_of_another_multi_cluster(env, capsys):
    board, _board_dir = env
    first = _issue_ticket(board, "선점 멤버")
    second = _issue_ticket(board, "선점 멤버 둘")
    third = _issue_ticket(board, "새 묶음 멤버")
    capsys.readouterr()
    assert board.cmd_cluster(_cluster_args(
        "new", "owner", tickets=f"{first},{second}")) == 0
    capsys.readouterr()

    rc = board.cmd_cluster(_cluster_args("new", "thief", tickets=f"{first},{third}"))

    assert rc == 1
    assert "이미 다른 묶음의 멤버" in capsys.readouterr().err
    assert not board.cluster_ledger_path("C-thief").exists()
    assert _ticket_fm(board, first)["cluster"] == "C-owner"


@requires_git
@pytest.mark.parametrize(
    ("name", "tickets", "fragment"),
    (
        ("bad name", "T-0001", "형식 위반"),
        ("dup", None, "필수"),
    ),
)
def test_cluster_new_rejects_bad_input_before_any_write(
        env, capsys, name, tickets, fragment):
    board, _board_dir = env

    rc = board.cmd_cluster(_cluster_args("new", name, tickets=tickets))

    assert rc == 1
    assert fragment in capsys.readouterr().err
    assert not board.clusters_dir().exists()


@requires_git
@pytest.mark.parametrize(
    ("name", "creatable"),
    (
        ("foo..bar", False),     # 이중 점
        ("foo.lock", False),     # `.lock` 끝
        ("foo.", False),         # 점 끝
        ("foo.bar", True),
        ("a.lockx", True),
        ("foo.lock.bar", True),  # 마디 끝이 아니면 금칙이 아니다
    ),
)
def test_cluster_name_predicate_matches_git_refname_rules(env, name, creatable):
    """이름 술어는 `git check-ref-format --branch <브랜치 이름>` 과 같은 답을 낸다."""
    board, _board_dir = env
    branch = board.cluster_branch_name(name)

    probe = _git(["check-ref-format", "--branch", branch], board.REPO)

    assert (probe.returncode == 0) is creatable, probe   # 기준값은 실 git 이다.
    assert (board._validate_cluster_name(name) is None) is creatable


@requires_git
@pytest.mark.parametrize("name", ("foo..bar", "foo.lock", "foo."))
def test_cluster_new_refuses_a_name_git_cannot_branch(env, capsys, name):
    """만들 수 없는 브랜치를 장부에 선언한 채 성공하지 않는다 — 첫 쓰기 앞에서 거부."""
    board, _board_dir = env
    first = _issue_ticket(board, "금칙 이름")
    capsys.readouterr()

    rc = board.cmd_cluster(_cluster_args("new", name, tickets=first))

    assert rc == 1
    assert "git 브랜치 이름 금칙" in capsys.readouterr().err
    assert not board.cluster_ledger_path(f"C-{name}").exists()
    assert board._cluster_branch_state(
        str(board.REPO), board.cluster_branch_name(name)) is not True
    # 멤버 귀속도 그대로다(부분 쓰기 없음).
    assert _ticket_fm(board, first)["cluster"] == f"C-{first}"


@requires_git
def test_cluster_new_refuses_an_existing_ledger(env, capsys):
    board, _board_dir = env
    first = _issue_ticket(board, "한 번")
    capsys.readouterr()
    assert board.cmd_cluster(_cluster_args("new", "once", tickets=first)) == 0
    capsys.readouterr()

    rc = board.cmd_cluster(_cluster_args("new", "once", tickets=first))

    assert rc == 1
    assert "이미 있는 클러스터 장부" in capsys.readouterr().err


@requires_git
def test_cluster_new_separates_internal_overlap_from_outside_overlap(env, capsys):
    """멤버끼리의 겹침은 묶음 근거라 별도 줄 — 후보 집합에서는 멤버 전부가 빠진다."""
    board, _board_dir = env
    first = _issue_ticket(board, "겹침 멤버")
    second = _issue_ticket(board, "겹침 멤버 둘")
    outsider = _issue_ticket(board, "묶음 밖")
    for tid in (first, second, outsider):
        _status, path = board.find_ticket_exact(tid)
        fm, body = board.load_ticket(path)
        fm["touches"] = [".project_manager/tools/board.py"]
        board.dump_ticket(path, fm, body)
    capsys.readouterr()

    assert board.cmd_cluster(_cluster_args(
        "new", "overlap", tickets=f"{first},{second}")) == 0

    err = capsys.readouterr().err
    assert "클러스터 내부 겹침" in err, err
    assert first in err and second in err
    # 묶음 밖 티켓과의 겹침은 종전 재료 줄로 남는다(멤버는 후보에서 빠진다).
    outside_line = [line for line in err.splitlines() if "건 겹침" in line]
    assert outside_line and outsider in outside_line[0], err


# ════════════════════════════════════════════════════════════════════════
# 크기 1 장부 — 발행 · 승격 · 재개방
# ════════════════════════════════════════════════════════════════════════

@requires_git
def test_new_creates_a_size_one_cluster_and_keeps_the_existing_stdout_lines(
        env, capsys):
    board, _board_dir = env

    assert board.cmd_new(_new_args("크기 1")) == 0

    captured = capsys.readouterr()
    tid = board._canonical_ticket_id(_all_ticket_files(board)[0])
    stdout_lines = [line for line in captured.out.splitlines() if line.strip()]
    assert stdout_lines[0].startswith(f"created {tid} (")
    assert stdout_lines[1].startswith("  → fill in 목표 / 완료 조건 / 참고")
    assert all("클러스터" not in line for line in stdout_lines)
    # 추가 고지는 stderr 1줄이다.
    assert f"크기 1 묶음 장부 생성: C-{tid}" in captured.err
    ledger = board.load_cluster(f"C-{tid}")
    assert ledger["tickets"] == [tid]
    assert ledger["branch"] is None       # 브랜치 선언은 `cluster new` 의 몫이다.
    # 판정 입력 두 값은 발행이 박는다 — 기준 브랜치는 발행 세션이 보는 코드 트리의 브랜치.
    assert ledger["base_branch"] == _BASE_BRANCH
    assert ledger["budget"] == board.CLUSTER_BUDGET_DEFAULT
    assert _ticket_fm(board, tid)["cluster"] == f"C-{tid}"


@requires_git
def test_new_keeps_its_own_ledger_while_a_declared_cluster_is_active(env, capsys):
    """활성 묶음이 있어도 새 티켓은 자기 장부를 갖는다 — 엔진은 묶지 않는다.

    자동 합류가 있으면 선언된 묶음이 뒤따르는 발행을 전부 빨아들여, 그 묶음의 예산·기준
    브랜치가 아무도 그렇게 선언한 적 없는 티켓의 판정을 대신한다.
    """
    board, _board_dir = env
    seed = _issue_ticket(board, "묶음 씨앗")
    assert board.cmd_cluster(_cluster_args("new", "live", tickets=seed)) == 0
    capsys.readouterr()

    issued = _issue_ticket(board, "합류 아님")

    assert _ticket_fm(board, issued)["cluster"] == f"C-{issued}"
    assert board.cluster_members("C-live") == (seed,)
    assert board.load_cluster(f"C-{issued}")["base_branch"] == _BASE_BRANCH


@requires_git
def test_cluster_members_without_ledger_is_empty(env, capsys):
    """멤버는 장부가 선언한다 — 이름 모양으로 멤버를 합성하지 않는다(구세대 티켓 포함).

    이름 해석(`ticket_cluster`)은 남는다 — 종결 티켓도 자기 묶음 id 는 갖는다. 사라진 것은
    멤버 합성이고, 그 빈 튜플이 조회 부재와 준비 거부의 입력이다.
    """
    board, board_dir = env
    legacy = _write_legacy_ticket(board_dir, "T-0777", status="open")
    fm = board.load_ticket(legacy)[0]

    assert "cluster" not in fm
    assert board.ticket_cluster("T-0777", fm) == "C-T-0777"
    assert board.load_cluster("C-T-0777") is None
    assert board.cluster_members("C-T-0777") == ()
    # 없는 티켓 ID 모양도 같은 빈 값이다(이름 모양은 입력이 아니다).
    assert board.cluster_members("C-T-9999") == ()
    assert board.lint_clusters() == []      # 장부가 없으니 관측할 것도 없다.

    # 선언된 묶음만 멤버를 답한다 — 멤버가 흡수된 자동 장부 이름도 빈 값이다.
    first = _issue_ticket(board, "선언된 멤버")
    second = _issue_ticket(board, "선언된 멤버 둘")
    assert board.cmd_cluster(_cluster_args(
        "new", "declared", tickets=f"{first},{second}")) == 0
    capsys.readouterr()

    assert board.cluster_members("C-declared") == (first, second)
    assert board.cluster_members(f"C-{first}") == ()


@requires_git
def test_reopen_legacy_done_creates_size_one_ledger(env, capsys):
    """장부 없는 done 티켓의 reopen — open 진입 자리에서 자기 장부를 만든다.

    장부 없이 open 으로 돌아오면 라운드 예산·완료 기록이 읽을 선언이 없어 그 티켓은 위임에서
    멈춘다(장부를 손으로 만들어야 종결되던 실증). 발행·승격과 같은 술어·같은 생성이다.
    """
    board, board_dir = env
    _write_legacy_ticket(board_dir, "T-0500")
    capsys.readouterr()

    rc = board.cmd_reopen(_reopen_args("T-0500", "오처리 복구"))

    assert rc == 0
    fm = _ticket_fm(board, "T-0500")
    assert fm["status"] == "open"
    assert fm["cluster"] == "C-T-0500"
    ledger = board.load_cluster("C-T-0500")
    assert ledger["tickets"] == ["T-0500"]
    assert ledger["base_branch"] == _BASE_BRANCH
    assert ledger["budget"] == board.CLUSTER_BUDGET_DEFAULT
    assert ledger["status"] == board.CLUSTER_STATUS_OPEN
    assert board.cluster_members("C-T-0500") == ("T-0500",)
    # 장부는 board-git 공유 파일이다 — 같은 커밋 스코프로 실린다(미커밋으로 남으면 다른
    # 클론에서 부재다).
    head = _head_files(board_dir)
    assert "tickets/clusters/C-T-0500.md" in head, head
    assert "tickets/open/T-0500-legacy.md" in head, head
    assert _git(["status", "--porcelain", "--", "tickets"], board_dir).stdout == ""


@requires_git
def test_reopen_keeps_declared_cluster_and_does_not_touch_ledger(env, capsys):
    """이미 귀속된 티켓의 reopen 은 장부를 손대지 않는다 — 덮으면 선언이 사라진다.

    무조건 귀속하면 같은 이름의 장부가 덮여 `status`·`budget`·`tickets` 가 기본값으로
    되돌아가고, 사람이 선언한 묶음의 멤버는 자기 이름 장부와 옛 장부에 이중으로 귀속된다.
    """
    board, board_dir = env
    _write_legacy_ticket(board_dir, "T-0501", cluster="C-declared")
    ledger_fm = board._new_cluster_fm(
        "C-declared", ["T-0501", "T-0502"], base_branch=_BASE_BRANCH,
        branch="task/declared")
    ledger_fm["status"] = "closed"
    ledger_path = board.dump_cluster(ledger_fm)
    before = ledger_path.read_bytes()
    _git(["add", "-A"], board_dir)
    _git(["commit", "-qm", "declared ledger"], board_dir)
    capsys.readouterr()

    rc = board.cmd_reopen(_reopen_args("T-0501", "묶음 재작업"))

    assert rc == 0
    assert _ticket_fm(board, "T-0501")["cluster"] == "C-declared"
    assert ledger_path.read_bytes() == before      # status·budget·tickets 전부 불변
    assert not board.cluster_ledger_path("C-T-0501").exists()
    assert board.cluster_members("C-declared") == ("T-0501", "T-0502")
    # 만지지 않은 파일은 커밋 스코프에도 없다.
    assert all(not name.startswith("tickets/clusters/")
               for name in _head_files(board_dir)), _head_files(board_dir)


@requires_git
def test_reopen_binds_to_an_existing_ledger_without_overwriting_it(env, capsys):
    """필드는 비었는데 장부가 이미 이 티켓을 담고 있으면 만들지 않고 역참조만 복구한다.

    구세대 티켓 중에는 종결을 위해 장부를 손으로 만든 자산이 있다(실 board 의 C-T-0866).
    필드 유무만 보고 같은 이름으로 새 장부를 만들면 그 장부가 기본값으로 덮여
    `base_branch`·`status`·`close_step` 이 사라지고, 멤버가 여럿이면 나머지 멤버가 장부에서
    떨어져 나간다. 귀속 판정은 필드와 장부 파일 두 축을 다 본다(`_cluster_of_record`).
    """
    board, board_dir = env
    _write_legacy_ticket(board_dir, "T-0503")
    ledger_fm = board._new_cluster_fm("C-T-0503", ["T-0503"], base_branch="task/main")
    ledger_fm["status"] = "closed"
    ledger_fm["close_step"] = "board"
    ledger_fm["replans"] = []
    ledger_path = board.dump_cluster(ledger_fm)
    before = ledger_path.read_bytes()
    _git(["add", "-A"], board_dir)
    _git(["commit", "-qm", "hand-made ledger"], board_dir)
    capsys.readouterr()

    rc = board.cmd_reopen(_reopen_args("T-0503", "구세대 재작업"))

    assert rc == 0
    fm = _ticket_fm(board, "T-0503")
    assert fm["status"] == "open"
    assert fm["cluster"] == "C-T-0503"            # 역참조만 복구한다.
    assert ledger_path.read_bytes() == before     # base_branch·status·close_step 전부 불변
    assert board.cluster_members("C-T-0503") == ("T-0503",)
    # 장부를 만지지 않았으므로 board-git 커밋 스코프에도 없다.
    assert all(not name.startswith("tickets/clusters/")
               for name in _head_files(board_dir)), _head_files(board_dir)


@requires_git
def test_reopen_to_draft_creates_no_ledger(env, capsys):
    """`discarded_from: draft` 복귀는 장부를 만들지 않는다 — 승격이 그 자리를 받는다.

    draft 명세는 공유 board 밖이라(`cmd_new` 와 같은 규칙) 지금 장부를 만들면 다른 클론에
    없는 티켓을 가리키는 장부가 공유된다.
    """
    board, board_dir = env
    _write_legacy_ticket(board_dir, "T-0502", status="discarded", discarded_from="draft")
    capsys.readouterr()

    rc = board.cmd_reopen(_reopen_args("T-0502", "draft 로 복귀"))

    assert rc == 0
    assert (board_dir / "tickets" / ".drafts" / "T-0502-legacy.md").exists()
    assert not board.clusters_dir().exists()
    assert not _ticket_fm(board, "T-0502").get("cluster")
    assert board.cluster_members("C-T-0502") == ()


@requires_git
def test_draft_defers_attribution_until_promote(board, tmp_path, capsys):
    """draft 는 공유 board 밖이라 승격 자리에서 귀속한다(공유 장부가 유령 멤버를 갖지 않게)."""
    bare = tmp_path / "bare-draft"
    _git(["init", "--bare", "-q", "-b", "main", str(bare)], tmp_path)
    board_dir = _make_board_git(tmp_path, remote=bare, template=_DRAFT_TEMPLATE_TEXT)
    _make_code_git(tmp_path)

    assert board.cmd_new(_new_args("미충전")) == 0
    draft = list((board_dir / "tickets" / ".drafts").glob("T-*.md"))[0]
    tid = board._canonical_ticket_id(draft)
    assert not board.clusters_dir().exists()
    assert not board.load_ticket(draft)[0].get("cluster")

    fm, _body = board.load_ticket(draft)
    board.dump_ticket(draft, fm, _TEMPLATE_TEXT.split("---\n", 2)[2]
                      .replace("T-NNNN", tid).replace("<제목>", "미충전"))
    capsys.readouterr()
    assert board.cmd_promote(argparse.Namespace(id=tid)) == 0

    assert _ticket_fm(board, tid)["cluster"] == f"C-{tid}"
    assert board.cluster_members(f"C-{tid}") == (tid,)
    assert board.load_cluster(f"C-{tid}")["base_branch"] == _BASE_BRANCH
    assert f"크기 1 묶음 장부 생성: C-{tid}" in capsys.readouterr().err


# ════════════════════════════════════════════════════════════════════════
# lint · show
# ════════════════════════════════════════════════════════════════════════

@requires_git
def test_lint_reports_member_and_duplicate_as_advisory(env, capsys):
    board, _board_dir = env
    first = _issue_ticket(board, "관측 멤버")
    capsys.readouterr()
    assert board.cmd_cluster(_cluster_args("new", "obs", tickets=first)) == 0
    capsys.readouterr()
    # 멤버 부재 — 장부가 board 에 없는 티켓을 가리킨다.
    ledger = board.load_cluster("C-obs")
    ledger["tickets"] = [first, "T-9998"]
    board.dump_cluster(ledger)
    # 중복 귀속 — 다른 장부가 같은 티켓을 멤버로 담는다.
    board.dump_cluster(board._new_cluster_fm("C-shadow", [first]))

    findings = board.lint_clusters()

    kinds = {kind for _id, kind, _detail in findings}
    assert kinds == {
        board._CLUSTER_MEMBER_LINT_KIND,
        board._CLUSTER_DUPLICATE_LINT_KIND,
    }, findings
    # 두 kind 다 advisory 다 — `lint --gate` 차단 집합에 들지 않는다.
    assert kinds <= board._ADVISORY_LINT_KINDS
    duplicate = [item for item in findings
                 if item[1] == board._CLUSTER_DUPLICATE_LINT_KIND][0]
    assert duplicate[0] == first and "C-obs" in duplicate[2] and "C-shadow" in duplicate[2]


@requires_git
def test_lint_clusters_reads_only_the_ledger(split_env, capsys):
    """lint 는 장부 파일만 읽는다 — 브랜치 실재를 재려면 트리를 추측해야 하기 때문이다.

    이 표면은 인자를 받지 않고 장부에도 코드 트리를 적는 자리가 없다. 추측하면 PM 홈(코드
    없는 트리)에서 "통합 브랜치 부재"가 상시 참이 된다 — 그래서 그 축 자체를 없앴다.
    """
    board, _board_dir, code = split_env
    first = _issue_ticket(board, "장부만 읽는다")
    capsys.readouterr()
    assert board.cmd_cluster(_cluster_args(
        "new", "ledger", tickets=first, repo=SLOT_REPO, slot=1)) == 0
    capsys.readouterr()
    assert board._cluster_branch_state(str(code), "task/ledger") is True
    # 코드 트리에서 통합 브랜치를 지우고, 멤버 부재 축은 살려 둔다(입력이 장부 안에 있다).
    _git(["checkout", "-q", _BASE_BRANCH], code)
    _git(["branch", "-D", "task/ledger"], code)
    ledger = board.load_cluster("C-ledger")
    ledger["tickets"] = [first, "T-9998"]
    board.dump_cluster(ledger)

    findings = board.lint_clusters()

    assert {kind for _id, kind, _detail in findings} == {
        board._CLUSTER_MEMBER_LINT_KIND}, findings
    assert all("통합 브랜치" not in detail for _id, _kind, detail in findings), findings
    assert not hasattr(board, "_CLUSTER_BRANCH_LINT_KIND")


@requires_git
def test_cluster_show_renders_the_declared_values(env, capsys):
    board, _board_dir = env
    first = _issue_ticket(board, "조회 멤버")
    capsys.readouterr()
    assert board.cmd_cluster(_cluster_args(
        "new", "view", tickets=first, spike="raw/spikes/x.md")) == 0
    capsys.readouterr()

    rc = board.cmd_cluster(_cluster_args("show", "view"))

    out = capsys.readouterr().out
    assert rc == 0
    assert "-- C-view (open · 멤버 1) --" in out
    assert f"base_branch: {_BASE_BRANCH}" in out
    assert "branch: task/view" in out
    # 실재 표기는 없다 — 이 표면은 트리를 들지 않는다(선언값 그대로).
    assert "(존재)" not in out and "(부재)" not in out and "판정 불능" not in out
    assert "spike: raw/spikes/x.md" in out
    assert "architect=1" in out and "fix=1" in out
    assert "replans:" not in out
    assert f"{first}  open  조회 멤버" in out


@requires_git
def test_cluster_show_reports_a_missing_ledger(env, capsys):
    board, _board_dir = env

    rc = board.cmd_cluster(_cluster_args("show", "nope"))

    assert rc == 2
    assert "cluster not found: C-nope" in capsys.readouterr().err


@requires_git
def test_cluster_show_without_ledger_is_not_found(env, capsys):
    """장부 없음은 rc 2 하나다 — 조회가 묶음을 합성하던 rc 0 경로는 없다.

    티켓이 실재하는 이름(구세대 티켓의 자기 이름)이 그 경로의 입력이었다 — 조회가 선언한 적
    없는 묶음을 만들어 보이면 준비 표면도 같은 유령을 받는다.
    """
    board, board_dir = env
    _write_legacy_ticket(board_dir, "T-0777", status="open")
    capsys.readouterr()

    rc = board.cmd_cluster(_cluster_args("show", "T-0777"))

    captured = capsys.readouterr()
    assert rc == 2
    assert "cluster not found: C-T-0777" in captured.err
    assert captured.out == ""       # 크기 1 해석 rc 0 경로가 남으면 여기 값이 나온다
    # 없는 티켓 ID 모양도 같은 답·같은 문구다(새 문구 0).
    assert board.cmd_cluster(_cluster_args("show", "T-9999")) == 2
    assert "cluster not found: C-T-9999" in capsys.readouterr().err
