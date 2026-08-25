"""묶음 종결(close) 파이프라인 + 순서 무관 판정 — `ticket_finish.py --cluster`.

여기서 지키는 성질은 넷이다.

  (1) 사설 참조 preflight 판정이 **미커밋 / 커밋 / 재배치 전 / 재배치 후** 네 형상에서 같은
      값이다. 순서를 규칙으로 지키던 자리를 판정 기준(통합 브랜치 도달 여부)이 대체한다.
  (2) 측정 폭이 통합 브랜치에서 흡수한 분량을 제외하고, 두 형식 소비자(numstat·unified diff)가
      같은 폭을 본다.
  (3) close 가 고정 여덟 단계를 순서대로 실행하고, 중간에서 멈춘 뒤 다시 실행하면 이미 끝난
      단계를 반복하지 않는다(부작용 카운터로 단언).
  (4) 크기 1 묶음이 티켓 하나 완료 기록과 같은 결과를 낸다(rc·board 결과·stage 범위).

실 git 으로 재현한다(DI 로 git 을 가짜로 만들지 않는다) — 조상 판정·재배치·머지·worktree 해소가
전부 git 의 실제 동작이라 가짜 러너로는 이 성질을 확인할 수 없다. 합성 사설 참조는 이 파일의
관례대로 **조각으로 조립**한다(완전한 사설 ID 리터럴을 소스에 남기지 않는다).
"""
from __future__ import annotations

import datetime
import importlib.util
import io
import contextlib
import json
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

requires_git = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git 바이너리 부재 — 실 git 통합 케이스 skip.",
)

# 픽스처 타임스탬프 — 저장만 되고 어느 판정에도 입력이 아니다(claim 시각·리스 시작 시각).
# 소스에 날짜 리터럴을 남기지 않으려고 실행 시점 값을 그대로 쓴다(출하 위생 · 값 무관).
_FIXTURE_STAMP = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")

_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
}

_INTEGRATION_BRANCH = "task/main"     # 통합 브랜치(장부 `base_branch`)
_CLUSTER_BRANCH = "task/wave"         # 묶음 브랜치(장부 `branch`)
_CLUSTER = "C-wave"
_SLOT = "work/code_1"
_ROUND_FILE = "01-architect.md"      # 라운드 사이드카 한 벌(동형 대조의 위치 값)
_CLOSED_STATUS = "closed"            # 장부 종결 표시(열거의 소유자는 board)

# 합성 사설 참조 — 조각 조립(소스에 완전한 사설 ID 리터럴 0).
_SYNTHETIC_REF = "T-" + "0" * 3 + "7"
_TICKET = "T-" + "9" * 3 + "1"
_TOUCH = ".project_manager/tools/close_target.py"
_BASE_BODY = '"""표본 shipping 모듈."""\n\n\ndef greet():\n    return "hi"\n'
# 안전 삭제 단위가 잡히지 않는 형태(괄호 안 혼합 문맥) — raw 축에만 걸리는 실측 형상이다.
_RAW_ONLY_LINE = (
    f'    # 완료 조건(DoD) 기록 게이트 (complete 차단 — {_SYNTHETIC_REF})\n'
)


def _load_tool(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def tf():
    return _load_tool("ticket_finish")


@pytest.fixture(scope="module")
def external():
    return _load_tool("external_review")


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch):
    for key, value in _GIT_IDENTITY.items():
        monkeypatch.setenv(key, value)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", check=False)


def _rev(root: Path, ref: str = "HEAD") -> str:
    return _git(root, "rev-parse", ref).stdout.strip()


def _short(root: Path, ref: str) -> str:
    return _git(root, "rev-parse", "--short", ref).stdout.strip()


def _commit(root: Path, message: str) -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)
    return _rev(root)


def _write_ticket(
    board_dir: Path, ticket: str, *, claimed_rev: str, status: str = "claimed",
    touches: tuple[str, ...] = (_TOUCH,), cluster: str = _CLUSTER,
    title: str = "종결 픽스처",
) -> Path:
    path = board_dir / "tickets" / status / f"{ticket}-close.md"
    touch_lines = "".join(f"- {value}\n" for value in touches)
    path.write_text(
        "---\n"
        f"id: {ticket}\ntitle: {title}\nstatus: {status}\n"
        f"claimed_by: t\nclaimed_at: '{_FIXTURE_STAMP}'\n"
        f"claimed_rev: {claimed_rev}\ncompleted_at: null\n"
        f"depends_on: []\nblocks: []\ntouches:\n{touch_lines}"
        f"estimate: small\ncluster: {cluster}\ntags: []\n---\n\n"
        f"# {ticket}\n\n## 목표\n표본\n\n"
        "## 완료 조건 (Definition of Done)\n- [x] 표본\n\n## 메모\n",
        encoding="utf-8",
    )
    return path


def _write_cluster_ledger(
    board_dir: Path, *, tickets: tuple[str, ...],
    base_branch: str | None = _INTEGRATION_BRANCH,
    branch: str | None = _CLUSTER_BRANCH, cluster: str = _CLUSTER,
) -> Path:
    directory = board_dir / "tickets" / "clusters"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{cluster}.md"
    path.write_text(
        "---\n"
        f"id: {cluster}\n"
        "tickets:\n" + "".join(f"- {tid}\n" for tid in tickets) +
        f"base_branch: {base_branch or 'null'}\n"
        f"branch: {branch or 'null'}\n"
        "spike: null\n"
        "budget:\n  architect: 1\n  developer_per_ticket: 1\n"
        "  code-reviewer: 1\n  fix: 1\n"
        "replans: []\n"
        "status: open\n"
        "---\n",
        encoding="utf-8",
    )
    return path


# ════════════════════════════════════════════════════════════════════════
# 판정 축 — 통합 브랜치 도달 여부(순서 무관)
# ════════════════════════════════════════════════════════════════════════

def _order_repo(tmp_path: Path) -> tuple[Path, str]:
    """통합 브랜치 + 묶음 브랜치를 가진 실 git — `(트리, 통합 브랜치 tip)`."""
    root = tmp_path / "order-repo"
    shutil.copytree(TOOLS, root / ".project_manager" / "tools")
    board_dir = root / ".project_manager" / "board"
    for status in ("open", "claimed", "blocked", "done"):
        (board_dir / "tickets" / status).mkdir(parents=True)
    _git(root, "init", "-q", "-b", _INTEGRATION_BRANCH)
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / _TOUCH).write_text(_BASE_BODY, encoding="utf-8")
    seed = _commit(root, "seed")
    _git(root, "checkout", "-q", "-b", _CLUSTER_BRANCH)
    _write_ticket(board_dir, _TICKET, claimed_rev=seed)
    _write_cluster_ledger(board_dir, tickets=(_TICKET,))
    return root, seed


def _finisher(tf, root: Path):
    return tf.TicketFinisher(
        board_py=root / ".project_manager" / "tools" / "board.py",
        task_workspace=root,
    )


def _block_with_stderr(tf, root: Path) -> tuple[str | None, str]:
    """(차단 문자열 또는 None, 그 실행이 낸 stderr) — 경고 축까지 값으로 본다."""
    buffer = io.StringIO()
    with contextlib.redirect_stderr(buffer):
        block = _finisher(tf, root)._default_private_ref_block(_TICKET)
    return block, buffer.getvalue()


def _offender_lines(block: str | None) -> list[str]:
    if block is None:
        return []
    return [line.strip() for line in block.splitlines()
            if line.lstrip().startswith("✗")]


def _advance_integration(root: Path, body: str, message: str) -> str:
    """통합 브랜치에 커밋 하나를 얹고 묶음 브랜치로 돌아온다(작업트리는 깨끗해야 한다)."""
    assert _git(root, "status", "--porcelain", "-uno").stdout.strip() == ""
    _git(root, "checkout", "-q", _INTEGRATION_BRANCH)
    (root / _TOUCH).write_text(body, encoding="utf-8")
    rev = _commit(root, message)
    _git(root, "checkout", "-q", _CLUSTER_BRANCH)
    return rev


@requires_git
def test_private_ref_verdict_is_identical_across_the_four_shapes(tf, tmp_path):
    """미커밋 / 커밋 / 재배치 전 / 재배치 후 네 형상이 **같은 판정·같은 좌표**를 낸다.

    이것이 순서 규칙 삭제의 선행 조건이다 — 한 형상이라도 다른 값을 내면 조작 순서가 판정의
    입력으로 남는다.
    """
    root, _seed = _order_repo(tmp_path)
    offending = _BASE_BODY.replace('    return "hi"\n',
                                   _RAW_ONLY_LINE + '    return "hi"\n')
    (root / _TOUCH).write_text(offending, encoding="utf-8")

    verdicts: list[tuple[bool, tuple[str, ...]]] = []

    # 형상 1 — 미커밋(작업트리에만 있다)
    block, _stderr = _block_with_stderr(tf, root)
    verdicts.append((block is not None, tuple(_offender_lines(block))))

    # 형상 2 — 묶음 브랜치에 커밋됐다
    _commit(root, "cluster work")
    block, _stderr = _block_with_stderr(tf, root)
    verdicts.append((block is not None, tuple(_offender_lines(block))))

    # 형상 3 — 통합 브랜치가 앞서 나갔다(재배치 전)
    _git(root, "checkout", "-q", _INTEGRATION_BRANCH)
    (root / "unrelated.txt").write_text("x\n", encoding="utf-8")
    _commit(root, "integration moves on")
    _git(root, "checkout", "-q", _CLUSTER_BRANCH)
    block, _stderr = _block_with_stderr(tf, root)
    verdicts.append((block is not None, tuple(_offender_lines(block))))

    # 형상 4 — 통합 브랜치 위로 재배치했다
    assert _git(root, "rebase", _INTEGRATION_BRANCH).returncode == 0
    block, _stderr = _block_with_stderr(tf, root)
    verdicts.append((block is not None, tuple(_offender_lines(block))))

    assert len({verdict for verdict, _lines in verdicts}) == 1, verdicts
    assert verdicts[0][0] is True                       # 네 형상 모두 차단이다
    coordinate = f"{_TOUCH}:5 {_SYNTHETIC_REF}"
    for _verdict, lines in verdicts:
        assert len(lines) == 1, lines
        assert lines[0].startswith(f"✗ {coordinate}"), lines


@requires_git
def test_line_the_integration_branch_already_had_leaves_the_width(tf, tmp_path):
    """통합 브랜치가 이미 가진 줄은 이 작업의 폭이 아니다 — 재배치로 흡수해도 무발화다.

    민감도로 대조한다: 통합 브랜치 선언을 지우면 같은 트리에서 판정이 **멈춘다**(기준이 없는
    실행이 조용히 통과하지 않는다 · 원래부터 조용해서 통과한 것이 아니다).
    """
    root, _seed = _order_repo(tmp_path)
    offending = _BASE_BODY.replace('    return "hi"\n',
                                   _RAW_ONLY_LINE + '    return "hi"\n')
    _advance_integration(root, offending, "absorbed offense")
    assert _git(root, "rebase", _INTEGRATION_BRANCH).returncode == 0

    block, stderr = _block_with_stderr(tf, root)
    assert block is None
    assert stderr == ""

    _write_cluster_ledger(root / ".project_manager" / "board", tickets=(_TICKET,),
                          base_branch=None)
    with pytest.raises(tf._CloseObservationFailure) as caught:
        _block_with_stderr(tf, root)
    assert "통합 브랜치(base_branch)를 선언하지 않았다" in str(caught.value)


@requires_git
def test_committed_novel_line_names_the_introducing_commit(tf, tmp_path):
    """통합 브랜치에 없는 **커밋된** 줄은 차단하고, 그 줄의 도입 커밋을 값으로 지목한다."""
    root, _seed = _order_repo(tmp_path)
    offending = _BASE_BODY.replace('    return "hi"\n',
                                   _RAW_ONLY_LINE + '    return "hi"\n')
    (root / _TOUCH).write_text(offending, encoding="utf-8")
    introduced = _commit(root, "cluster work")
    block, stderr = _block_with_stderr(tf, root)
    assert block is not None
    assert f"✗ {_TOUCH}:5 {_SYNTHETIC_REF} (통합 미반영 {_short(root, introduced)})" in block
    assert stderr == ""


@requires_git
def test_actionable_reference_is_blocked_whether_or_not_it_is_committed(tf, tmp_path):
    """actionable 참조는 커밋 여부와 무관하게 차단이다 — 기준 교체가 이 갈래를 풀지 않는다."""
    root, _seed = _order_repo(tmp_path)
    body = _BASE_BODY.replace(
        '    return "hi"\n',
        f'    # 사설 참조 표식 ({_SYNTHETIC_REF})\n    return "hi"\n',
    )
    (root / _TOUCH).write_text(body, encoding="utf-8")

    uncommitted, _stderr = _block_with_stderr(tf, root)
    _commit(root, "cluster work")
    committed, _stderr = _block_with_stderr(tf, root)

    expected = [f"✗ {_TOUCH}:5 {_SYNTHETIC_REF}"]
    assert _offender_lines(uncommitted) == expected
    assert _offender_lines(committed) == expected


@requires_git
def test_unresolvable_integration_branch_stops_the_judgment(tf, tmp_path):
    """선언한 통합 브랜치가 이 트리에 없으면 판정 기준이 없다 — 멈춘다."""
    root, _seed = _order_repo(tmp_path)
    _write_cluster_ledger(root / ".project_manager" / "board", tickets=(_TICKET,),
                          base_branch="task/absent")
    offending = _BASE_BODY.replace('    return "hi"\n',
                                   _RAW_ONLY_LINE + '    return "hi"\n')
    (root / _TOUCH).write_text(offending, encoding="utf-8")
    _commit(root, "cluster work")

    with pytest.raises(tf._CloseObservationFailure) as caught:
        _block_with_stderr(tf, root)

    assert "task/absent" in str(caught.value) and "찾지 못했다" not in str(caught.value)


def test_cli_takes_exactly_one_close_target(tf, monkeypatch, capsys):
    """완료 기록 대상은 하나다 — 둘 다 주거나 둘 다 없으면 거부한다(부작용 앞)."""
    monkeypatch.setattr(tf, "_pm_home_misanchor", lambda: None, raising=False)
    for argv in ([], [_TICKET, "--cluster", _CLUSTER]):
        with pytest.raises(SystemExit) as excinfo:
            tf._main(argv)
        assert excinfo.value.code == 2
        assert "완료 기록 대상은 하나다" in capsys.readouterr().err


# ════════════════════════════════════════════════════════════════════════
# 측정 폭 — 흡수분 제외 · 두 형식 소비자 동일
# ════════════════════════════════════════════════════════════════════════

_ABSORBED_MARKER = "absorbed_line"
_ABSORBED_TOUCH = ".project_manager/tools/close_absorbed.py"
_OWN_MARKER = "own_line"
_WIDTH_SCOPE = (_TOUCH, _ABSORBED_TOUCH)
_ABSORBED_LINES = 20
_OWN_LINES = 5


def _width_repo(tmp_path: Path) -> tuple[Path, str]:
    """통합 브랜치 흡수분 + 이 작업의 변경이 한 폭 안에 있는 트리 — `(트리, claim 시점 rev)`."""
    root, seed = _order_repo(tmp_path)
    # 통합 브랜치가 자기 변경을 얹는다(이 작업이 재배치로 흡수할 분량).
    _git(root, "checkout", "-q", _INTEGRATION_BRANCH)
    (root / _ABSORBED_TOUCH).write_text(
        "".join(f"# {_ABSORBED_MARKER} {index}\n" for index in range(_ABSORBED_LINES)),
        encoding="utf-8",
    )
    _commit(root, "integration adds lines")
    _git(root, "checkout", "-q", _CLUSTER_BRANCH)
    # 이 작업의 변경(자기 몫).
    (root / _TOUCH).write_text(
        _BASE_BODY + "".join(f"# {_OWN_MARKER} {index}\n" for index in range(_OWN_LINES)),
        encoding="utf-8",
    )
    _commit(root, "cluster work")
    assert _git(root, "rebase", _INTEGRATION_BRANCH).returncode == 0
    return root, seed


@requires_git
def test_width_excludes_what_the_integration_branch_already_had(external, tmp_path):
    """재배치로 흡수한 분량은 이 작업의 폭이 아니다 — 기준점을 claim 시점 rev 와 대조한다."""
    root, seed = _width_repo(tmp_path)

    anchor, note = external.integration_anchor(root, _INTEGRATION_BRANCH)
    assert note is None
    assert anchor == _rev(root, _INTEGRATION_BRANCH)

    integration_width = external.diff_line_total(
        root, "HEAD", list(_WIDTH_SCOPE), claimed_rev=anchor)
    claim_width = external.diff_line_total(
        root, "HEAD", list(_WIDTH_SCOPE), claimed_rev=seed)

    assert integration_width == _OWN_LINES                      # 자기 몫만
    assert claim_width == _OWN_LINES + _ABSORBED_LINES          # 옛 기준은 흡수분도 쟀다


@requires_git
def test_both_width_consumers_see_the_same_stage(external, tmp_path):
    """numstat 총량과 unified diff 원문이 **같은 폭**을 본다(정의 지점 하나)."""
    root, seed = _width_repo(tmp_path)
    anchor, _note = external.integration_anchor(root, _INTEGRATION_BRANCH)

    assert external._measure_stages("HEAD", anchor) == ((anchor, True),)

    numstat = external.measured_numstat_text(
        root, "HEAD", list(_WIDTH_SCOPE), claimed_rev=anchor)
    diff_text = external.measured_diff_text(
        root, "HEAD", list(_WIDTH_SCOPE), claimed_rev=anchor)
    assert external._sum_numstat(numstat) == _OWN_LINES
    assert diff_text.count(_OWN_MARKER) == _OWN_LINES
    assert _ABSORBED_MARKER not in numstat and _ABSORBED_MARKER not in diff_text

    old_diff = external.measured_diff_text(
        root, "HEAD", list(_WIDTH_SCOPE), claimed_rev=seed)
    assert _ABSORBED_MARKER in old_diff             # 민감도 — 옛 기준에서는 실린다


# ════════════════════════════════════════════════════════════════════════
# close — 고정 여덟 단계 · 재실행은 재개
# ════════════════════════════════════════════════════════════════════════

class _CloseEnv:
    """close 픽스처 한 벌 — PM 홈 · board git · 코드 git(통합 트리 + 슬롯 worktree)."""

    def __init__(self, tf, home: Path, code: Path, slot: Path, board_dir: Path,
                 ticket: str) -> None:
        self.tf = tf
        self.home = home
        self.code = code
        self.slot = slot
        self.board_dir = board_dir
        self.ticket = ticket
        self.board_calls: list[list[str]] = []
        self.delegate_calls: list[list[str]] = []
        self.release_calls: list[str] = []
        # 남은 complete 실패 횟수 — 완료 기록 중간 실패 뒤 재개를 값으로 보는 입력.
        self.complete_failures = 0

    # ── seam ────────────────────────────────────────────────────────
    def run_board(self, args: list[str]) -> tuple[int, str]:
        """board 완료를 흉내낸다 — 티켓을 done 으로 옮기고 호출을 센다."""
        self.board_calls.append(list(args))
        if args and args[0] == "complete":
            if self.complete_failures:
                self.complete_failures -= 1
                return 1, "board complete 실패(합성)"
            source = next(
                (path for status in ("claimed", "open")
                 for path in (self.board_dir / "tickets" / status).glob("T-*.md")),
                None,
            )
            if source is None:
                return 1, "ticket not found"
            target = self.board_dir / "tickets" / "done" / source.name
            source.replace(target)
            text = target.read_text(encoding="utf-8")
            target.write_text(text.replace("status: claimed", "status: done"),
                              encoding="utf-8")
        return 0, "ok"

    def run_delegate(self, args: list[str]) -> tuple[int, str]:
        self.delegate_calls.append(list(args))
        return 0, ""

    def finisher(self, finisher_class=None):
        """이 환경의 완료 기록 대역 — `finisher_class` 는 진짜 클래스를 이미 대역으로 바꾼
        테스트가 원본을 명시로 넘기는 자리다(대역이 대역을 만드는 것을 막는다)."""
        cls = finisher_class or self.tf.TicketFinisher
        return cls(
            board_py=self.board_dir.parent / "tools" / "board.py",
            log_file=self.home / ".project_manager" / "wiki" / "log" / "current.md",
            regression_cwd=str(self.slot),
            run_pytest_fn=lambda: (0, "1 passed in 0.01s"),
            run_board_fn=self.run_board,
            diff_cap_block_fn=lambda tid: None,
            private_ref_block_fn=lambda tid: None,
            dod_block_fn=lambda tid: None,
            residual_block_fn=lambda tid: None,
            self_axis_block_fn=lambda tid: None,
            affected_domain_fn=lambda tid: None,
        )

    def closer(self, **kwargs):
        values = dict(
            finisher=self.finisher(),
            board_py=self.board_dir.parent / "tools" / "board.py",
            slot=_SLOT,
            run_delegate_fn=self.run_delegate,
        )
        values.update(kwargs)
        return self.tf.ClusterCloser(_CLUSTER, **values)

    # ── 관측 ────────────────────────────────────────────────────────
    def slot_log(self) -> list[str]:
        return _git(self.slot, "log", "--format=%s").stdout.split("\n")

    def integration_log(self) -> list[str]:
        return _git(self.code, "log", "--format=%s", _INTEGRATION_BRANCH).stdout.split("\n")

    def home_log(self) -> list[str]:
        return _git(self.home, "log", "--format=%s").stdout.split("\n")

    def ledger(self) -> dict:
        text = (self.board_dir / "tickets" / "clusters" / f"{_CLUSTER}.md").read_text(
            encoding="utf-8")
        return {
            line.split(":", 1)[0].strip(): line.split(":", 1)[1].strip()
            for line in text.splitlines() if ":" in line and not line.startswith(" ")
        }

    def lease_state(self) -> str | None:
        path = self.home / ".project_manager" / ".local" / "worktree-leases.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        return data["leases"][0]["state"]

    def log_text(self) -> str:
        return (self.home / ".project_manager" / "wiki" / "log" / "current.md").read_text(
            encoding="utf-8")

    def rounds_entries(self) -> tuple[tuple[str, str], ...]:
        """라운드 사이드카의 `(board 기준 상대경로, 내용)` — 종결이 그 자리를 옮기지 않는다.

        사이드카 자리는 `tickets/rounds/<티켓>/<순번-역할>.md` 로 고정이라 그 깊이만 센다
        (재귀 tree-walk 를 쓰지 않는다 — repo 열거 seam 가드의 대상이 된다).
        """
        directory = self.board_dir / "tickets" / "rounds"
        return tuple(
            (path.relative_to(self.board_dir).as_posix(), path.read_text(encoding="utf-8"))
            for path in sorted(directory.glob("*/*.md"))
        )


def _build_close_env(tf, base: Path, monkeypatch) -> _CloseEnv:
    """close 픽스처 한 벌을 `base` 아래에 세운다 (같은 테스트가 두 벌을 세워 대조한다)."""
    base.mkdir(parents=True, exist_ok=True)
    tmp_path = base
    home = tmp_path / "home"
    code = tmp_path / "code"
    slot = home / "work" / "code_1"
    board_dir = home / ".project_manager" / "board"

    shutil.copytree(TOOLS, home / ".project_manager" / "tools")
    (home / ".project_manager" / "wiki" / "log").mkdir(parents=True)
    (home / ".project_manager" / "wiki" / "log" / "current.md").write_text(
        "# log\n", encoding="utf-8")
    (home / ".project_manager" / ".local").mkdir(parents=True)
    (home / ".gitignore").write_text("work/\n.project_manager/.local/\n",
                                     encoding="utf-8")

    # board git (+ bare remote — 기록 채널을 실제로 태운다)
    for status in ("open", "claimed", "blocked", "done"):
        (board_dir / "tickets" / status).mkdir(parents=True)
    bare = tmp_path / "board-remote.git"
    _git(tmp_path, "init", "--bare", "-q", "-b", "main", str(bare))
    _git(board_dir, "init", "-q", "-b", "main")
    _git(board_dir, "config", "user.email", "t@t")
    _git(board_dir, "config", "user.name", "t")

    # 코드 git — 통합 브랜치 트리 + 묶음 브랜치 슬롯 worktree
    code.mkdir(parents=True)
    _git(code, "init", "-q", "-b", _INTEGRATION_BRANCH)
    _git(code, "config", "user.email", "t@t")
    _git(code, "config", "user.name", "t")
    (code / "src").mkdir()
    (code / "src" / "app.py").write_text("seed = 1\n", encoding="utf-8")
    _commit(code, "code seed")
    slot.parent.mkdir(parents=True, exist_ok=True)
    assert _git(code, "worktree", "add", "-q", "-b", _CLUSTER_BRANCH, str(slot),
                _INTEGRATION_BRANCH).returncode == 0
    # 이 작업의 산출 — 슬롯에서 미커밋 상태로 둔다(완료 기록이 stage 하고 종결이 커밋한다).
    (slot / "src" / "app.py").write_text("seed = 1\nwork = 2\n", encoding="utf-8")

    ticket = _TICKET
    _write_ticket(board_dir, ticket, claimed_rev=_rev(code), touches=("src/app.py",))
    _write_cluster_ledger(board_dir, tickets=(ticket,))
    rounds = board_dir / "tickets" / "rounds" / ticket
    rounds.mkdir(parents=True)
    (rounds / _ROUND_FILE).write_text("## 라운드 표본\n", encoding="utf-8")
    _git(board_dir, "add", "-A")
    _git(board_dir, "commit", "-qm", "board seed")
    _git(board_dir, "remote", "add", "origin", str(bare))
    _git(board_dir, "push", "-q", "-u", "origin", "main")

    # PM 홈 git — board 를 서브모듈 포인터로 물고 있다(상위 status 에 안 보이는 그 형상).
    _git(home, "init", "-q", "-b", "main")
    _git(home, "config", "user.email", "t@t")
    _git(home, "config", "user.name", "t")
    _git(home, "-c", "advice.addEmbeddedRepo=false", "add", "-A")
    _git(home, "commit", "-qm", "home seed")

    # 리스 장부 — 종결이 반납할 슬롯.
    (home / ".project_manager" / ".local" / "worktree-leases.json").write_text(
        json.dumps({"leases": [{
            "slot": _SLOT, "repo": "code", "session": "t", "pid": 0,
            "started": _FIXTURE_STAMP, "state": "leased",
        }]}, ensure_ascii=False), encoding="utf-8")

    monkeypatch.setattr(tf, "REPO", home)
    monkeypatch.setattr(tf, "TOOLS_DIR", home / ".project_manager" / "tools")
    monkeypatch.setattr(tf, "BOARD_PY", home / ".project_manager" / "tools" / "board.py")
    monkeypatch.setattr(
        tf, "LOG_FILE", home / ".project_manager" / "wiki" / "log" / "current.md")
    monkeypatch.setattr(
        tf, "LEASES_FILE",
        home / ".project_manager" / ".local" / "worktree-leases.json")
    monkeypatch.setattr(tf, "_pm_home_misanchor", lambda: None, raising=False)
    return _CloseEnv(tf, home, code, slot, board_dir, ticket)


@pytest.fixture
def close_env(tf, tmp_path, monkeypatch) -> _CloseEnv:
    return _build_close_env(tf, tmp_path, monkeypatch)


@requires_git
def test_close_runs_the_eight_steps_and_leaves_no_hand_work(close_env, capsys):
    """성공 경로 — 여덟 단계가 순서대로 돌고 커밋·재배치·머지·반납·기록이 실제로 남는다."""
    env = close_env
    # 통합 브랜치가 앞서 나가 있다 — 재배치 단계가 무대상이 아니게 한다.
    (env.code / "other.txt").write_text("x\n", encoding="utf-8")
    _commit(env.code, "integration moves on")

    rc = env.closer().run()

    out = capsys.readouterr().out
    assert rc == 0, out
    order = [line.split("] ", 1)[1].removesuffix("...") for line in out.splitlines()
             if line.startswith("[") and "/8] " in line]
    assert order == [label for _key, label in env.tf.ClusterCloser.STEPS], order
    # 단계 3 — 완료 기록이 돌았다(board complete 1회).
    assert [args[0] for args in env.board_calls] == ["complete"]
    # 단계 4 — 슬롯 커밋 문안은 티켓 제목이다.
    assert env.slot_log()[0] == "종결 픽스처"
    # 단계 5 — 통합 브랜치가 앞서 얹은 커밋 위로 재배치됐다.
    assert "integration moves on" in env.slot_log()
    # 단계 6 — `--no-ff` 머지 문안.
    assert env.integration_log()[0] == f"{env.ticket} merge — 종결 픽스처"
    assert _git(env.code, "rev-parse", f"{_INTEGRATION_BRANCH}^2").returncode == 0
    # 단계 7 — 슬롯 반납.
    assert env.lease_state() == "idle"
    # 단계 8 — PM 홈 포인터·산출물 커밋.
    assert env.home_log()[0] == f"{env.ticket} board — 종결 픽스처"
    committed = _git(env.home, "show", "--name-only", "--format=", "HEAD").stdout
    assert ".project_manager/board" in committed
    assert ".project_manager/wiki/log/current.md" in committed
    # 종결이 하는 일을 사람에게 다시 시키지 않는다.
    assert "git commit — **경로를 명시**하라" not in out
    assert "종결 파이프라인이 실행한다" in out


@requires_git
def test_close_resumes_without_repeating_earlier_steps(close_env, capsys):
    """단계 중간에서 멈춘 뒤 다시 실행하면 앞 단계를 반복하지 않는다(부작용 카운터)."""
    env = close_env
    # 통합 브랜치가 같은 파일을 다르게 고쳐 재배치가 충돌하게 만든다.
    (env.code / "src" / "app.py").write_text("seed = 1\nconflict = 9\n",
                                             encoding="utf-8")
    _commit(env.code, "integration touches the same line")

    first = env.closer().run()
    capsys.readouterr()

    assert first == 1
    assert [args[0] for args in env.board_calls] == ["complete"]
    slot_commits = env.slot_log()
    assert slot_commits[0] == "종결 픽스처"
    # 충돌은 원상 복구된다 — 진행 중인 재배치를 남기지 않는다.
    assert not (env.slot / ".git").is_dir()      # worktree 는 gitdir 파일이다
    assert _git(env.slot, "status", "--porcelain").stdout.strip() == ""
    assert env.lease_state() == "leased"          # 뒤 단계는 돌지 않았다

    # 충돌을 사람이 해소한다(통합 브랜치 쪽을 되돌린다).
    (env.code / "src" / "app.py").write_text("seed = 1\n", encoding="utf-8")
    _commit(env.code, "integration reverts")

    second = env.closer().run()
    out = capsys.readouterr().out

    assert second == 0, out
    assert [args[0] for args in env.board_calls] == ["complete"]   # 완료 기록 미반복
    assert env.slot_log().count("종결 픽스처") == 1                 # 커밋 미반복
    assert "이미 done — 완료 기록 건너뜀" in out
    assert "커밋할 변경 없음 — 건너뜀" in out
    assert env.lease_state() == "idle"


@requires_git
def test_close_is_a_no_op_when_everything_is_already_done(close_env, capsys):
    """전부 끝난 묶음에 다시 실행하면 어느 단계도 새 부작용을 만들지 않는다."""
    env = close_env
    assert env.closer().run() == 0
    capsys.readouterr()
    home_before = env.home_log()
    integration_before = env.integration_log()
    release_before = env.lease_state()

    rc = env.closer().run()
    captured = capsys.readouterr()
    out = captured.out

    assert rc == 0, out + captured.err
    assert [args[0] for args in env.board_calls] == ["complete"]
    assert env.home_log() == home_before
    assert env.integration_log() == integration_before
    assert env.lease_state() == release_before
    assert "이미 반납됨" in out
    assert "이미 task/main 에 있다" in out or "이미 task/main 위에 있다" in out


@requires_git
def test_close_records_progress_in_the_cluster_ledger(close_env):
    """장부에 단계 진행과 종결 상태가 남는다 — 열거는 board 가 소유한 값이다."""
    env = close_env
    assert env.closer().run() == 0
    ledger = env.ledger()
    assert ledger["close_step"] == env.tf.ClusterCloser.STEPS[-1][0]
    assert ledger["status"] == "closed"


@requires_git
def test_close_stops_before_any_side_effect_when_the_confirmation_is_missing(
    close_env, capsys, monkeypatch,
):
    """기계 확인이 미충족이면 첫 부작용 앞에서 멈춘다 — board·git 어디도 건드리지 않는다."""
    env = close_env
    board = env.tf._board_module_at(env.board_dir.parent / "tools" / "board.py")
    ledger_path = board._internal_review_rounds_ledger()
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(json.dumps({env.ticket: {"rounds": [
        {"sequence": 1, "must_fix": 2, "verdict": 1},
    ]}}), encoding="utf-8")

    rc = env.closer().run()
    out = capsys.readouterr()

    assert rc == 1
    assert "final-fix 확인 입력 미충족" in out.err
    assert env.board_calls == []
    assert env.delegate_calls == []
    assert env.slot_log()[0] == "code seed"
    assert env.lease_state() == "leased"


@requires_git
def test_close_preflight_precedes_resolve_in_dry_run_and_actual(
    close_env, capsys, monkeypatch,
):
    """step 1은 confirmation 부재를 허용하는 입력 preflight, step 2가 실제 생성·처분 소유자다."""
    env = close_env
    assert [key for key, _label in env.tf.ClusterCloser.STEPS[:2]] == [
        "confirm", "resolve",
    ]

    dry_events: list[str] = []
    dry = env.closer(dry_run=True)
    monkeypatch.setattr(dry, "_pending_gates", lambda: [env.ticket])
    monkeypatch.setattr(dry, "_gate_ledger", lambda: {env.ticket: {}})
    monkeypatch.setattr(dry, "_delegate_supports_cluster", lambda: True)
    monkeypatch.setattr(
        dry._board, "_pm_verified_resolution_input_problem",
        lambda *_a, **_k: dry_events.append("preflight") or None,
    )
    monkeypatch.setattr(
        dry._board, "_pm_verified_evidence_problem",
        lambda *_a, **_k: pytest.fail("step 1이 post-resolution evidence를 선호출했다"),
    )
    assert dry._step_confirm() is None
    assert dry._step_resolve() is None
    assert dry_events == ["preflight"]
    assert env.delegate_calls == []
    assert "rounds resolve --cluster" in capsys.readouterr().out

    actual_events: list[str] = []

    def resolve(args):
        actual_events.append("resolve")
        return 0, "resolved"

    actual = env.closer(run_delegate_fn=resolve)
    monkeypatch.setattr(actual, "_pending_gates", lambda: [env.ticket])
    monkeypatch.setattr(actual, "_gate_ledger", lambda: {env.ticket: {}})
    monkeypatch.setattr(actual, "_delegate_supports_cluster", lambda: True)
    monkeypatch.setattr(
        actual._board, "_pm_verified_resolution_input_problem",
        lambda *_a, **_k: actual_events.append("preflight") or None,
    )
    assert actual._step_confirm() is None
    assert actual._step_resolve() is None
    assert actual_events == ["preflight", "resolve"]


@requires_git
def test_close_resolves_each_gate_when_the_bundle_surface_is_absent(
    close_env, capsys, monkeypatch,
):
    """묶음 처분 표면이 없으면 티켓별 처분을 반복한다(있으면 한 번) — seam 하나로 갈린다."""
    env = close_env
    closer = env.closer()
    monkeypatch.setattr(closer, "_delegate_supports_cluster", lambda: False)
    monkeypatch.setattr(closer, "_pending_gates", lambda: [env.ticket])

    assert closer._step_resolve() is None
    assert env.delegate_calls == [
        ["rounds", "resolve", "--gate", env.ticket, "--pm-verified"]]

    monkeypatch.setattr(closer, "_delegate_supports_cluster", lambda: True)
    env.delegate_calls.clear()
    assert closer._step_resolve() is None
    assert env.delegate_calls == [
        ["rounds", "resolve", "--cluster", _CLUSTER, "--pm-verified"]]


@requires_git
def test_close_task_identity_is_forwarded_to_resolve_without_ambient_env(
    close_env, monkeypatch,
):
    """task-mode에서 해소한 정체성을 resolve subprocess argv에도 직접 싣는다."""
    env = close_env
    monkeypatch.delenv("PM_SESSION_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_NAME", raising=False)
    closer = env.closer(
        task="main", board_identity_args=["--task", "main"],
    )
    monkeypatch.setattr(closer, "_delegate_supports_cluster", lambda: True)
    monkeypatch.setattr(closer, "_pending_gates", lambda: [env.ticket])

    assert closer._step_resolve() is None
    assert env.delegate_calls == [[
        "rounds", "resolve", "--cluster", _CLUSTER, "--pm-verified",
        "--task", "main",
    ]]


@requires_git
def test_close_refuses_to_move_a_branch_that_is_not_the_cluster_branch(close_env):
    """슬롯이 묶음 브랜치를 들고 있지 않으면 재배치·머지 앞에서 거부한다."""
    env = close_env
    closer = env.closer()
    _commit(env.slot, "cluster work")          # 묶음 브랜치에 머지할 것이 생긴다
    assert _git(env.slot, "checkout", "-q", "-b", "dev/side").returncode == 0

    rebase_block = closer._step_rebase()
    merge_block = closer._step_merge()

    for block in (rebase_block, merge_block):
        assert block is not None and "묶음 브랜치" in block and "dev/side" in block
    assert env.integration_log()[0] == "code seed"   # 통합 브랜치는 그대로다


@requires_git
def test_close_refuses_to_release_a_dirty_slot(close_env, capsys):
    """반납은 dirty 슬롯을 거부한다 — 자동 경로라고 산출물을 조용히 치우지 않는다."""
    env = close_env
    closer = env.closer()
    (env.slot / "stray.txt").write_text("x\n", encoding="utf-8")

    block = closer._step_release()

    assert block is not None and "슬롯 반납 실패" in block
    assert env.lease_state() == "leased"
    assert (env.slot / "stray.txt").is_file()


@requires_git
def test_close_dry_run_changes_nothing(close_env, capsys):
    """계획만 출력한다 — board·git·장부 어디에도 부작용이 없다."""
    env = close_env
    home_before = env.home_log()
    slot_before = env.slot_log()

    rc = env.closer(dry_run=True).run()
    out = capsys.readouterr().out

    assert rc == 0, out
    assert env.board_calls == []
    assert env.home_log() == home_before
    assert env.slot_log() == slot_before
    assert env.lease_state() == "leased"
    assert "close_step" not in env.ledger()


@requires_git
def test_size_one_cluster_matches_the_single_ticket_finish(close_env, capsys):
    """크기 1 묶음 종결과 티켓 하나 완료 기록이 같은 결과를 낸다(rc·board·stage 범위)."""
    env = close_env
    finisher = env.finisher()
    direct_rc = finisher.run(env.ticket, section=None, dry_run=False)
    direct_out = capsys.readouterr().out
    direct_board = [list(args) for args in env.board_calls]
    direct_staged = _git(env.slot, "diff", "--cached", "--name-only").stdout.split()
    direct_home_staged = _git(env.home, "diff", "--cached", "--name-only").stdout.split()

    # 같은 형상을 다시 세워 이번에는 묶음 종결로 돈다.
    _git(env.slot, "reset", "-q", "HEAD")
    _git(env.home, "reset", "-q", "HEAD")
    done = next((env.board_dir / "tickets" / "done").glob("T-*.md"))
    target = env.board_dir / "tickets" / "claimed" / done.name
    done.replace(target)
    target.write_text(target.read_text(encoding="utf-8").replace(
        "status: done", "status: claimed"), encoding="utf-8")
    env.board_calls.clear()

    closer = env.closer()
    cluster_rc = closer.run()
    cluster_out = capsys.readouterr().out
    cluster_staged = _git(env.slot, "show", "--name-only", "--format=", "HEAD").stdout.split()

    assert (direct_rc, cluster_rc) == (0, 0), cluster_out
    assert [args[0] for args in env.board_calls] == [args[0] for args in direct_board]
    assert direct_board[0][1] == env.ticket
    assert cluster_staged == direct_staged == ["src/app.py"]
    assert direct_home_staged == [".project_manager/wiki/log/current.md"]
    # 완료 기록 구간의 출력 줄은 그대로다(종결 단계만 안내 대신 실행이다).
    for line in ("[1/5] 회귀 실행 중...", "[3/5] board.py complete...",
                 "[4/5] git stage (선언 경로 스코프)..."):
        assert line in direct_out and line in cluster_out


# ════════════════════════════════════════════════════════════════════════
# 리뷰 송신 폭 — 실제 진입점(`_diff_cap_refusal`)이 통합 merge-base 로 잰다
# ════════════════════════════════════════════════════════════════════════

def _cap_refusal(external, root: Path, *, cap: int) -> tuple[str | None, str]:
    """리뷰 송신 서킷브레이커를 **실제 진입점으로** 부른다 — `(차단 문자열, stderr)`.

    helper 를 따로 부르면 진입점이 그 helper 에 무엇을 넘기는지가 검증되지 않는다(폭 기준이
    진입점에서만 갈린 실측이 있다)."""
    args = SimpleNamespace(ticket=_TICKET, gate=None, base="HEAD")
    buffer = io.StringIO()
    with contextlib.redirect_stderr(buffer):
        block = external._diff_cap_refusal(
            args, {"diff_cap.small": str(cap)},
            root=root, paths=list(_WIDTH_SCOPE), pm_home=root,
        )
    return block, buffer.getvalue()


def _cap_total(external, root: Path) -> int:
    """그 실행이 실제로 잰 총량 — 상한 0 이면 0 줄이 아닌 한 항상 차단이라 값이 나온다."""
    block, _stderr = _cap_refusal(external, root, cap=0)
    matched = re.search(r"diff (\d+)줄", block or "")
    assert matched is not None, block
    return int(matched.group(1))


@requires_git
def test_review_width_is_the_same_before_and_after_commit_and_rebase(external, tmp_path):
    """리뷰 송신 폭이 미커밋 / 커밋 / 재배치 전 / 재배치 후 네 형상에서 같은 값이다.

    옛 기준(claim 앵커)은 재배치 뒤 흡수분을 자기 폭으로 실어 형상 4 만 값이 뛰었다.
    """
    root, _seed = _order_repo(tmp_path)
    # 픽스처 board 파일을 묶음 브랜치에 먼저 커밋한다 — 통합 브랜치 쪽 커밋이 그것들을 통합
    # 브랜치에 실으면 묶음 브랜치로 돌아올 때 git 이 티켓·장부 파일을 지운다(조회 불능).
    _commit(root, "board fixture")
    # 통합 브랜치가 앞서 나간다 — 이 작업이 재배치로 흡수할 분량(자기 폭이 아니다).
    _git(root, "checkout", "-q", _INTEGRATION_BRANCH)
    (root / _ABSORBED_TOUCH).write_text(
        "".join(f"# {_ABSORBED_MARKER} {index}\n" for index in range(_ABSORBED_LINES)),
        encoding="utf-8",
    )
    _commit(root, "integration adds lines")
    _git(root, "checkout", "-q", _CLUSTER_BRANCH)
    # 이 작업의 변경(자기 몫).
    (root / _TOUCH).write_text(
        _BASE_BODY + "".join(f"# {_OWN_MARKER} {index}\n" for index in range(_OWN_LINES)),
        encoding="utf-8",
    )

    totals = [_cap_total(external, root)]                    # 형상 1 — 미커밋
    _commit(root, "cluster work")
    totals.append(_cap_total(external, root))                # 형상 2 — 커밋
    _git(root, "checkout", "-q", _INTEGRATION_BRANCH)
    (root / "unrelated.txt").write_text("x\n", encoding="utf-8")
    _commit(root, "integration moves on")
    _git(root, "checkout", "-q", _CLUSTER_BRANCH)
    totals.append(_cap_total(external, root))                # 형상 3 — 재배치 전
    assert _git(root, "rebase", _INTEGRATION_BRANCH).returncode == 0
    totals.append(_cap_total(external, root))                # 형상 4 — 재배치 후

    assert totals == [_OWN_LINES] * 4, totals


@requires_git
def test_review_width_refuses_when_the_integration_branch_is_undeclared(
    external, tmp_path,
):
    """통합 브랜치 선언이 없으면 리뷰 송신을 거부한다 — 다른 기준으로 재지 않는다."""
    root, _seed = _width_repo(tmp_path)

    declared_block, declared_stderr = _cap_refusal(external, root, cap=0)
    assert f"diff {_OWN_LINES}줄" in declared_block
    assert external.INTEGRATION_TIP_UNDECLARED_NOTE not in declared_stderr

    _write_cluster_ledger(root / ".project_manager" / "board", tickets=(_TICKET,),
                          base_branch=None)
    refusal, stderr = _cap_refusal(external, root, cap=0)

    assert "기준점을 해소하지 못했습니다" in refusal
    assert external.INTEGRATION_TIP_UNDECLARED_NOTE in refusal
    assert "diff" not in refusal          # 재지 않은 값을 문구에 싣지 않는다
    assert stderr == ""


@requires_git
def test_review_width_refuses_when_the_integration_branch_is_unresolvable(
    external, tmp_path,
):
    """선언한 통합 브랜치가 이 트리에 없어도 같은 거부다(옛 폭으로 접지 않는다)."""
    root, _seed = _width_repo(tmp_path)
    _write_cluster_ledger(root / ".project_manager" / "board", tickets=(_TICKET,),
                          base_branch="task/absent")

    block, stderr = _cap_refusal(external, root, cap=0)

    assert external.INTEGRATION_TIP_UNRESOLVED_NOTE.format(ref="task/absent") in block
    assert stderr == ""


# ════════════════════════════════════════════════════════════════════════
# 관측 실패 = 정지 (완료·clean·무대상 위장 금지)
# ════════════════════════════════════════════════════════════════════════

@requires_git
def test_close_stops_when_the_board_record_channel_cannot_commit(close_env, capsys):
    """board-git 로컬 커밋이 안 생기면 단계 8 에서 멈추고 종결 표시도 남기지 않는다.

    `False` 는 board 가 "이 경로가 아직 미커밋" 이라고 확정 보고한 값이다 — 경고로 강등하면
    board 에 없는 내용을 가리키는 포인터 커밋이 남는다.
    """
    env = close_env
    closer = env.closer()
    closer._board._board_git_sync_best_effort = lambda message, paths=None: False

    rc = closer.run()
    captured = capsys.readouterr()

    assert rc == 1
    assert "board-git 로컬 커밋이 생기지 않았다" in captured.err
    assert "[8/8]" in captured.err
    assert env.ledger()["status"] != _CLOSED_STATUS      # 관측 전 종결 표시 없음
    assert env.home_log()[0] == "home seed"              # 포인터 커밋도 없다

    # 기록 채널이 돌아오면 재실행이 그 단계부터 이어간다(앞 단계 미반복).
    second = env.closer().run()
    out = capsys.readouterr().out

    assert second == 0, out
    assert env.ledger()["status"] == _CLOSED_STATUS
    assert env.home_log()[0] == f"{env.ticket} board — 종결 픽스처"
    assert [args[0] for args in env.board_calls] == ["complete"]
    assert env.slot_log().count("종결 픽스처") == 1


@requires_git
def test_close_stops_when_a_working_tree_observation_fails(close_env, capsys, monkeypatch):
    """작업 트리 상태 조회가 실패하면 clean 으로 접지 않고 그 단계에서 멈춘다."""
    env = close_env
    finisher = env.finisher()
    closer = env.closer(finisher=finisher)

    def _unobservable(cwd):
        raise OSError("git status 실행 실패(합성)")

    monkeypatch.setattr(finisher, "_status_entries_at_fn", _unobservable)

    rc = closer.run()
    captured = capsys.readouterr()

    assert rc == 1
    assert "관측 실패" in captured.err
    assert "작업 트리 상태를 관측하지 못했다" in captured.err
    assert env.integration_log()[0] == "code seed"       # 머지는 돌지 않았다
    assert env.lease_state() == "leased"                 # 반납도 돌지 않았다


@requires_git
def test_close_stops_when_the_board_pointer_cannot_be_observed(close_env, capsys, monkeypatch):
    """포인터 조회가 실패하면 '서브모듈 아님'(무대상)으로 접지 않고 멈춘다."""
    env = close_env
    closer = env.closer()
    real_git_stdout = closer._git_stdout

    def _pointer_query_fails(cwd, args):
        if args[:2] == ["ls-files", "--stage"]:
            return 128, "fatal: 합성 실패"
        return real_git_stdout(cwd, args)

    monkeypatch.setattr(closer, "_git_stdout", _pointer_query_fails)

    rc = closer.run()
    captured = capsys.readouterr()

    assert rc == 1
    assert "board 포인터를 관측하지 못했다" in captured.err
    assert env.ledger()["status"] != _CLOSED_STATUS


@requires_git
def test_close_release_step_is_no_target_without_a_lease(close_env, capsys):
    """리스가 없는 슬롯은 **관측 성공·대상 없음** 이다 — 정지가 아니라 건너뜀이다."""
    env = close_env
    (env.home / ".project_manager" / ".local" / "worktree-leases.json").write_text(
        json.dumps({"leases": []}, ensure_ascii=False), encoding="utf-8")

    rc = env.closer().run()
    out = capsys.readouterr().out

    assert rc == 0, out
    assert f"슬롯 {_SLOT} 리스 없음 — 무대상" in out


# ════════════════════════════════════════════════════════════════════════
# 완료 기록 중간 실패 뒤 재개 — log·board·커밋이 정확히 한 번
# ════════════════════════════════════════════════════════════════════════

@requires_git
def test_close_leaves_one_log_entry_after_a_failed_board_complete(close_env, capsys):
    """board complete 가 한 번 실패해도 재실행이 log 스켈레톤을 다시 쌓지 않는다."""
    env = close_env
    env.complete_failures = 1

    first = env.closer().run()
    capsys.readouterr()

    assert first == 1
    assert env.log_text().count(f"| {env.ticket} — ") == 1
    assert env.slot_log()[0] == "code seed"            # 커밋 단계까지 가지 못했다

    second = env.closer().run()
    out = capsys.readouterr().out

    assert second == 0, out
    assert env.log_text().count(f"| {env.ticket} — ") == 1     # 중복 append 없음
    assert "스켈레톤 이미 있음" in out
    assert [args[0] for args in env.board_calls] == ["complete", "complete"]
    assert env.slot_log().count("종결 픽스처") == 1


# ════════════════════════════════════════════════════════════════════════
# 티켓 하나 = 크기 1 묶음 — 같은 코드 경로(네 값 동형)
# ════════════════════════════════════════════════════════════════════════

def _close_through_main(tf, base: Path, monkeypatch, argv: list[str],
                       finisher_class) -> tuple:
    """CLI 진입점으로 종결을 한 번 돌리고 대조할 네 값을 낸다.

    `(rc, board 결과, 라운드 파일 위치·내용, 장부 변경)` — 비준이 동형을 요구한 값 전부다.
    """
    env = _build_close_env(tf, base, monkeypatch)
    finisher = env.finisher(finisher_class)
    monkeypatch.setattr(tf, "TicketFinisher", lambda **kwargs: finisher)
    monkeypatch.setattr(tf, "_resolve_finish_slot", lambda repo, slot: (None, None))

    rc = tf._main(argv)

    return (rc, tuple(tuple(args) for args in env.board_calls),
            env.rounds_entries(), tuple(sorted(env.ledger().items())))


@requires_git
def test_the_ticket_form_and_the_cluster_form_take_the_same_path(tf, tmp_path,
                                                                 monkeypatch, capsys):
    """`ticket_finish T-NNNN` 과 `--cluster C` 가 같은 결과를 낸다(티켓용 별도 경로 0).

    티켓 하나는 크기 1 묶음이다 — 두 호출의 rc·board 결과·라운드 파일 위치·장부 변경이 값으로
    같아야 한다. 옛 형상에서는 티켓 호출만 장부 기록·반납·포인터 커밋 단계를 아예 돌지 않았다.
    """
    finisher_class = tf.TicketFinisher
    ticket_side = _close_through_main(
        tf, tmp_path / "ticket", monkeypatch, [_TICKET], finisher_class)
    cluster_side = _close_through_main(
        tf, tmp_path / "cluster", monkeypatch, ["--cluster", _CLUSTER], finisher_class)
    out = capsys.readouterr().out

    assert ticket_side == cluster_side, out
    rc, board, rounds, ledger = ticket_side
    assert rc == 0, out
    assert board == (("complete", _TICKET, "--tests-pass"),)
    assert rounds == ((f"tickets/rounds/{_TICKET}/{_ROUND_FILE}", "## 라운드 표본\n"),)
    assert dict(ledger)["close_step"] == tf.ClusterCloser.STEPS[-1][0]
    assert dict(ledger)["status"] == _CLOSED_STATUS


# ════════════════════════════════════════════════════════════════════════
# 잔여 판정 인구 — 커밋분 ∪ dirty · 같은 묶음 다른 멤버 선언 제외
# ════════════════════════════════════════════════════════════════════════

_MEMBER_A = "T-" + "9" * 3 + "2"     # 크기 2 묶음의 멤버 — 완료 기록 대상
_MEMBER_B = "T-" + "9" * 3 + "3"     # 같은 묶음 다른 멤버 — 그 선언은 인구에서 빠진다
_TOUCH_A = "src/a.py"
_TOUCH_B = "src/b.py"
_UNDECLARED = "src/loose.py"         # 어느 멤버도 선언하지 않은 경로
_SEED_BODY = "seed = 1\n"
_CHANGED_BODY = "seed = 1\nwork = 2\n"


def _residual_repo(tf, tmp_path: Path, monkeypatch, *, members: tuple[str, ...]) -> Path:
    """통합 브랜치 + 묶음 브랜치를 든 실 git — 잔여 판정을 실 호출로 본다.

    코드 트리와 PM 홈이 같은 임베디드 형상이라 계획이 하나다(선언 스코프·인구·묶음 장부가
    전부 이 트리에서 해소된다). 선언 세 종류를 미리 심는다 — 멤버 A 선언 · 멤버 B 선언 ·
    아무도 선언하지 않은 경로.
    """
    root = tmp_path / "residual-repo"
    shutil.copytree(TOOLS, root / ".project_manager" / "tools")
    board_dir = root / ".project_manager" / "board"
    for status in ("open", "claimed", "blocked", "done"):
        (board_dir / "tickets" / status).mkdir(parents=True)
    (root / ".project_manager" / "wiki" / "log").mkdir(parents=True)
    (root / ".project_manager" / "wiki" / "log" / "current.md").write_text(
        "# log\n", encoding="utf-8")
    (root / "src").mkdir()
    for path in (_TOUCH_A, _TOUCH_B, _UNDECLARED):
        (root / path).write_text(_SEED_BODY, encoding="utf-8")
    _git(root, "init", "-q", "-b", _INTEGRATION_BRANCH)
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    seed = _commit(root, "seed")
    _git(root, "checkout", "-q", "-b", _CLUSTER_BRANCH)
    _write_ticket(board_dir, _MEMBER_A, claimed_rev=seed, touches=(_TOUCH_A,))
    if _MEMBER_B in members:
        _write_ticket(board_dir, _MEMBER_B, claimed_rev=seed, touches=(_TOUCH_B,))
    _write_cluster_ledger(board_dir, tickets=members)
    monkeypatch.setattr(tf, "REPO", root)
    monkeypatch.setattr(tf, "TOOLS_DIR", root / ".project_manager" / "tools")
    monkeypatch.setattr(tf, "BOARD_PY", root / ".project_manager" / "tools" / "board.py")
    monkeypatch.setattr(
        tf, "LOG_FILE", root / ".project_manager" / "wiki" / "log" / "current.md")
    return root


def _commit_paths(root: Path, *paths: str) -> str:
    """선언한 경로만 커밋한다 — 인구가 커밋 여부로 갈리는지 보는 조작이다."""
    _git(root, "add", "--", *paths)
    _git(root, "commit", "-q", "-m", "work", "--", *paths)
    return _rev(root)


def _residual_verdict(tf, root: Path, ticket: str) -> tuple[str | None, str]:
    """(차단 문자열 또는 None, 그 실행이 낸 stderr) — 실 preflight 호출."""
    finisher = tf.TicketFinisher(
        board_py=root / ".project_manager" / "tools" / "board.py",
        log_file=root / ".project_manager" / "wiki" / "log" / "current.md",
        task_workspace=root,
    )
    buffer = io.StringIO()
    with contextlib.redirect_stderr(buffer):
        block = finisher._default_residual_block(ticket)
    return block, buffer.getvalue()


@requires_git
def test_residual_population_blocks_an_undeclared_change_either_way(
        tf, tmp_path, monkeypatch):
    """어느 멤버도 선언하지 않은 변경은 커밋 전에도 뒤에도 같은 차단이다.

    커밋이 회피 수단이면 산출을 커밋하는 내용 모델에서 이 게이트는 조작 순서에만 반응한다.
    """
    root = _residual_repo(tf, tmp_path, monkeypatch, members=(_MEMBER_A,))
    (root / _UNDECLARED).write_text(_CHANGED_BODY, encoding="utf-8")

    before, _before_err = _residual_verdict(tf, root, _MEMBER_A)
    _commit_paths(root, _UNDECLARED)
    after, _after_err = _residual_verdict(tf, root, _MEMBER_A)

    assert before is not None and _UNDECLARED in before
    assert after is not None and _UNDECLARED in after
    assert "통합 미반영 커밋 1건" in after
    assert "커밋은 회피 수단이 아니다" in after


@requires_git
def test_residual_population_passes_a_declared_change_either_way(tf, tmp_path, monkeypatch):
    """선언한 경로만 바꾼 깨끗한 완료 기록은 커밋 전에도 뒤에도 통과다(역방향)."""
    root = _residual_repo(tf, tmp_path, monkeypatch, members=(_MEMBER_A,))
    (root / _TOUCH_A).write_text(_CHANGED_BODY, encoding="utf-8")

    before, before_err = _residual_verdict(tf, root, _MEMBER_A)
    _commit_paths(root, _TOUCH_A)
    after, after_err = _residual_verdict(tf, root, _MEMBER_A)

    assert before is None and after is None
    assert before_err == "" and after_err == ""


@requires_git
def test_residual_population_excludes_a_sibling_member_commit(
        tf, tmp_path, monkeypatch):
    """크기 2 묶음에서 멤버 B 가 선언한 경로의 커밋은 멤버 A 의 완료 기록을 막지 않는다.

    묶음은 브랜치 하나를 공유하므로 이 제외가 없으면 크기 N 묶음은 전원이 서로를 막아 종결
    자체가 불가능하다. 반대로 아무도 선언하지 않은 커밋은 그대로 차단감이고, 그때에도 멤버 B
    의 경로는 목록에 없다.
    """
    root = _residual_repo(tf, tmp_path, monkeypatch,
                          members=(_MEMBER_A, _MEMBER_B))
    (root / _TOUCH_B).write_text(_CHANGED_BODY, encoding="utf-8")
    _commit_paths(root, _TOUCH_B)

    passed, err = _residual_verdict(tf, root, _MEMBER_A)

    assert passed is None
    assert err == ""

    (root / _UNDECLARED).write_text(_CHANGED_BODY, encoding="utf-8")
    _commit_paths(root, _UNDECLARED)
    blocked, _err = _residual_verdict(tf, root, _MEMBER_A)

    assert blocked is not None
    assert _UNDECLARED in blocked
    assert _TOUCH_B not in blocked


@requires_git
def test_residual_population_stops_without_an_integration_tip(
        tf, tmp_path, monkeypatch):
    """통합 tip 을 해소하지 못하면 인구가 성립하지 않는다 — dirty 인구로 접지 않고 멈춘다.

    같은 트리에서 통합 브랜치를 선언하면 같은 커밋이 차단감이다
    (`test_residual_population_blocks_an_undeclared_change_either_way`) — 원래부터 통과라서
    조용한 것이 아니다.
    """
    root = _residual_repo(tf, tmp_path, monkeypatch, members=(_MEMBER_A,))
    _write_cluster_ledger(root / ".project_manager" / "board",
                          tickets=(_MEMBER_A,), base_branch=None)
    (root / _UNDECLARED).write_text(_CHANGED_BODY, encoding="utf-8")
    _commit_paths(root, _UNDECLARED)

    with pytest.raises(tf._CloseObservationFailure) as caught:
        _residual_verdict(tf, root, _MEMBER_A)

    assert _MEMBER_A in str(caught.value)
    assert "통합 브랜치(base_branch)를 선언하지 않았다" in str(caught.value)


def test_status_seam_is_strict_and_the_non_blocking_report_absorbs_it(
        tf, tmp_path, capsys):
    """공유 seam 은 조회 실패를 그대로 올리고, 비차단 보고는 종전 출력 그대로다.

    접는 자리를 seam 에서 소비자로 옮긴 것이 요점이다 — 보고는 완료를 막지 않고, 종결의 선행
    조건 관측은 그 실패를 clean 과 구별한다.
    """
    def _failing(_args: list[str]) -> tuple[int, str]:
        return 128, "fatal: 합성 관측 실패"

    assert tf.status_entries(_failing) == ()
    with pytest.raises(tf.StatusObservationError):
        tf.status_entries(_failing, strict=True)

    finisher = tf.TicketFinisher(
        log_file=tmp_path / "log.md",
        status_entries_at_fn=lambda _cwd: tf.status_entries(_failing, strict=True),
    )
    finisher._report_dirty_after_stage((_TOUCH_A,), cwd=tmp_path)

    assert capsys.readouterr().out == (
        "  ✓ 스코프 밖 잔여 변경 없음 (staged·미스테이지 모두)\n")


@requires_git
def test_close_stops_when_the_status_query_returns_an_error_code(
        close_env, capsys, monkeypatch):
    """상태 조회가 rc≠0 을 내면 종결은 clean 으로 접지 않는다(strict 소비자).

    예외가 아니라 **rc** 로 실패하는 갈래다 — 그 값을 빈 목록으로 접으면 dirty 트리 위에서
    재배치·머지가 돈다.
    """
    env = close_env
    finisher = env.finisher()
    real_git_stdout_at = finisher._run_git_stdout_at_fn

    def _status_query_fails(cwd, args):
        if args[:1] == ["status"] and "--" not in args:
            return 128, "fatal: 합성 관측 실패"
        return real_git_stdout_at(cwd, args)

    monkeypatch.setattr(finisher, "_run_git_stdout_at_fn", _status_query_fails)

    rc = env.closer(finisher=finisher).run()
    captured = capsys.readouterr()

    assert rc == 1
    assert "작업 트리 상태를 관측하지 못했다" in captured.err
    assert env.integration_log()[0] == "code seed"       # 머지는 돌지 않았다
    assert env.lease_state() == "leased"                 # 반납도 돌지 않았다
