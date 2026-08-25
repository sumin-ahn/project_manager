"""클러스터 장부 — `cluster new|show` · 티켓 `cluster` 필드 자동 귀속 · `cluster-*` lint.

여기서 지키는 성질은 넷이다.
  (1) `cluster new` 가 장부와 통합 브랜치를 만들고 멤버 티켓에 귀속을 박는다.
  (2) 발행(`new`)·승격(`promote`)이 그 티켓의 크기 1 장부를 만들고 기준 브랜치·예산을 박는다
      (활성 묶음에 자동으로 끼우지 않는다 — 묶는 것은 사람 선언뿐).
  (3) 필드도 장부도 없는 티켓은 **읽는 자리에서** 크기 1 로 접힌다(파일 마이그레이션 0).
  (4) 장부 관측(멤버 부재·통합 브랜치 부재·중복 귀속)은 advisory 로만 보인다(never-block).

hermetic 패턴은 `test_board_new_draft_gate.py` 와 동형 — 실 board git + bare remote 를 tmp 에
세우고 board 모듈의 `REPO` 를 그 tmp 로 재앵커한다. 통합 브랜치 축은 tmp 자신을 코드 git 으로
쓴다(분리 PM 홈에서 활성 슬롯이 없으면 이 트리가 코드 트리다).
"""
from __future__ import annotations

import argparse
import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

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
    return board, board_dir


def _new_args(title: str, **kwargs) -> argparse.Namespace:
    values = dict(title=title, touches=None, depends=None, tag=None,
                  estimate="small", prefix=None, user=None, session=None,
                  repo=None, slot=None, task=None)
    values.update(kwargs)
    return argparse.Namespace(**values)


def _cluster_args(action: str, name: str, **kwargs) -> argparse.Namespace:
    values = dict(cluster_cmd=action, name=name, tickets=None, spike=None,
                  repo=None, slot=None, task=None)
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
    # 통합 브랜치가 코드 git 에 실재하고 기점과 같은 커밋을 가리킨다.
    assert board._cluster_branch_state(str(board.REPO), "task/wave") is True
    tip = _git(["rev-parse", "task/wave"], board.REPO).stdout.strip()
    base_tip = _git(["rev-parse", _BASE_BRANCH], board.REPO).stdout.strip()
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
# 크기 1 장부 — 발행 · 승격 · 필드 부재
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
def test_ticket_without_the_field_reads_as_a_size_one_cluster(env):
    """필드도 장부도 없는 구세대 티켓 — 읽는 자리에서 크기 1 (파일 마이그레이션 0)."""
    board, board_dir = env
    legacy = board_dir / "tickets" / "open" / "T-0777-legacy.md"
    legacy.write_text(
        _TEMPLATE_TEXT.replace("T-NNNN", "T-0777").replace("<제목>", "구세대")
        .replace("cluster:\n", ""),
        encoding="utf-8", newline="\n")

    fm = board.load_ticket(legacy)[0]

    assert "cluster" not in fm
    assert board.ticket_cluster("T-0777", fm) == "C-T-0777"
    assert board.cluster_members("C-T-0777") == ("T-0777",)
    assert board.load_cluster("C-T-0777") is None
    assert board.lint_clusters() == []      # 장부가 없으니 관측할 것도 없다.


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
def test_lint_reports_member_branch_and_duplicate_as_advisory(env, capsys):
    board, board_dir = env
    first = _issue_ticket(board, "관측 멤버")
    capsys.readouterr()
    assert board.cmd_cluster(_cluster_args("new", "obs", tickets=first)) == 0
    capsys.readouterr()
    # 멤버 부재 — 장부가 board 에 없는 티켓을 가리킨다.
    ledger = board.load_cluster("C-obs")
    ledger["tickets"] = [first, "T-9998"]
    board.dump_cluster(ledger)
    # 통합 브랜치 부재 — 진행 중 묶음의 선언 브랜치가 코드 git 에 없다.
    _git(["branch", "-D", "task/obs"], board_dir.parent.parent)
    # 중복 귀속 — 다른 장부가 같은 티켓을 멤버로 담는다.
    board.dump_cluster(board._new_cluster_fm("C-shadow", [first]))

    findings = board.lint_clusters()

    kinds = {kind for _id, kind, _detail in findings}
    assert kinds == {
        board._CLUSTER_MEMBER_LINT_KIND,
        board._CLUSTER_BRANCH_LINT_KIND,
        board._CLUSTER_DUPLICATE_LINT_KIND,
    }, findings
    # 세 kind 전부 advisory 다 — `lint --gate` 차단 집합에 들지 않는다.
    assert kinds <= board._ADVISORY_LINT_KINDS
    duplicate = [item for item in findings
                 if item[1] == board._CLUSTER_DUPLICATE_LINT_KIND][0]
    assert duplicate[0] == first and "C-obs" in duplicate[2] and "C-shadow" in duplicate[2]


@requires_git
def test_lint_skips_the_branch_axis_for_closed_clusters(env, capsys):
    """종결 묶음의 통합 브랜치는 머지 뒤 지우는 것이 정상이라 결함으로 세지 않는다."""
    board, board_dir = env
    first = _issue_ticket(board, "종결 멤버")
    capsys.readouterr()
    assert board.cmd_cluster(_cluster_args("new", "closed", tickets=first)) == 0
    _git(["branch", "-D", "task/closed"], board_dir.parent.parent)
    ledger = board.load_cluster("C-closed")
    ledger["status"] = "closed"
    board.dump_cluster(ledger)

    assert board.lint_clusters() == []


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
    assert "branch: task/view (존재)" in out
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
def test_show_refuses_a_ticket_shaped_name_with_no_such_ticket(env, capsys):
    """장부 없는 크기 1 폴백은 **티켓 실재**를 본다 — 이름 모양만으로 멤버를 합성하지 않는다."""
    board, _board_dir = env

    rc = board.cmd_cluster(_cluster_args("show", "T-9999"))

    assert rc == 2
    assert "cluster not found: C-T-9999" in capsys.readouterr().err
    # 준비(`prepare --cluster C-T-9999`)가 거부하는 입력도 이 빈 값이다(같은 seam).
    assert board.cluster_members("C-T-9999") == ()


@requires_git
def test_show_refuses_the_self_name_of_a_ticket_that_declares_another_cluster(
        env, capsys):
    """다른 묶음을 선언한 티켓은 자기 이름의 가짜 묶음으로 열리지 않는다."""
    board, _board_dir = env
    first = _issue_ticket(board, "선언된 멤버")
    second = _issue_ticket(board, "선언된 멤버 둘")
    assert board.cmd_cluster(_cluster_args(
        "new", "declared", tickets=f"{first},{second}")) == 0
    capsys.readouterr()
    assert _ticket_fm(board, first)["cluster"] == "C-declared"
    assert not board.cluster_ledger_path(f"C-{first}").exists()

    rc = board.cmd_cluster(_cluster_args("show", first))

    assert rc == 2
    assert f"cluster not found: C-{first}" in capsys.readouterr().err
    # 크기 1 폴백이 여기서 열리면 선언한 적 없는 묶음이 준비 표면에도 생긴다.
    assert board.cluster_members(f"C-{first}") == ()
    assert board.cluster_members("C-declared") == (first, second)
