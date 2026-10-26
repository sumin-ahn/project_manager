"""묶음 종결(close) 파이프라인 + 순서 무관 판정 — `ticket_finish.py --cluster`.

여기서 지키는 성질은 넷이다.

  (1) 사설 참조 preflight 판정이 **미커밋 / 커밋 / 재배치 전 / 재배치 후** 네 형상에서 같은
      값이다. 순서를 규칙으로 지키던 자리를 판정 기준(통합 브랜치 도달 여부)이 대체한다.
  (2) 측정 폭이 통합 브랜치에서 흡수한 분량을 제외하고, unified diff 원문이 단계 표가 정한
      폭을 본다(기준점은 통합 브랜치와의 merge-base · 해소 못 하면 사유와 함께 멈춘다).
  (3) close 가 첫 부작용 앞에서 막을 조건을 전건 보고하고, 고정 여섯 단계를 순서대로
      실행하며, 중간에서 멈춘 뒤 다시 실행하면 이미 끝난
      단계를 반복하지 않는다(부작용 카운터로 단언). 작업 트리는 묶음 기록의 브랜치를 쥔
      트리 하나이고, 종결은 대여 기록을 바꾸지 않는다.
  (4) 크기 1 묶음이 티켓 하나 완료 기록과 같은 결과를 낸다(rc·board 결과·stage 범위).
  (5) 멤버가 전부 처분(discarded)된 묶음은 코드 단계 없이 board 기록만으로 닫힌다.

실 git 으로 재현한다(DI 로 git 을 가짜로 만들지 않는다) — 조상 판정·재배치·머지·worktree 해소가
전부 git 의 실제 동작이라 가짜 러너로는 이 성질을 확인할 수 없다. 합성 사설 참조는 이 파일의
관례대로 **조각으로 조립**한다(완전한 사설 ID 리터럴을 소스에 남기지 않는다).
"""
from __future__ import annotations

from pm_engine.infra import review_diff

from pm_engine.cli import board_ticket

from pm_engine.app import cluster_binding


from pm_engine.infra import cluster_store


import datetime
import importlib.util
import inspect
import io
import contextlib
import json
import re
import shutil
import subprocess
from pathlib import Path

from pm_engine.domain import finish_rules

from pm_engine.infra import finish_git

import pytest

from _slot_seed import seed_slot
from pm_engine.app import round_seed

from pm_engine.app import ticket_access
from pm_engine.app import board_sync

from pm_engine.infra import pm_paths, ticket_store

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
# 픽스처 저장소는 엔진 사본을 커밋한다. 원본 트리의 `__pycache__` 가 실행 순서에 따라 일부만 있어
# 그대로 복사하면 테스트 중 새로 생긴 바이트코드가 미커밋 잔여로 잡힌다(새 clone 의 Windows 회귀 실측).
# 복사에서 빼고 픽스처 `.gitignore` 가 `__pycache__/` 를 무시한다.
_NO_BYTECODE = shutil.ignore_patterns("__pycache__")

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
_CLUSTER_NAME = "wave"                # 사람 표기 — 장부 id 는 board 접두 `C-` 를 붙인 값이다
_CLUSTER = f"C-{_CLUSTER_NAME}"
_SLOT = "work/code_1"
_ROUND_FILE = "01-architect.md"      # 라운드 사이드카 한 벌(동형 대조의 위치 값)
# 종결 전 멤버가 갖춘 라운드 — 구현 표본과 돌려받은 리뷰(판정 표면 · finding 0건 통과).
_DEVELOPER_ROUND_FILE = "02-developer.md"
_REVIEW_ROUND_FILE = "03-code-reviewer.md"
_CLOSED_STATUS = "closed"            # 장부 종결 표시(열거의 소유자는 board)
_DISCARDED_STATUS = "discarded"      # 처분 종결 상태 디렉토리(열거의 소유자는 board)

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
    return _load_tool("additional_reviewer")


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch):
    for key, value in _GIT_IDENTITY.items():
        monkeypatch.setenv(key, value)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", check=False)


def _lease_rows(home: Path) -> list[dict]:
    """리스 장부의 행 전체 — 대여 상태를 상태 하나가 아니라 행 값으로 본다."""
    path = home / ".project_manager" / ".local" / "worktree-leases.json"
    return json.loads(path.read_text(encoding="utf-8"))["leases"]


def _rev(root: Path, ref: str = "HEAD") -> str:
    return _git(root, "rev-parse", ref).stdout.strip()


def _short(root: Path, ref: str) -> str:
    return _git(root, "rev-parse", "--short", ref).stdout.strip()


def _git_index_bytes(root: Path) -> bytes:
    raw = _git(root, "rev-parse", "--git-path", "index").stdout.strip()
    path = Path(raw)
    return (path if path.is_absolute() else root / path).read_bytes()


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


def _review_round_text(findings: list[dict]) -> str:
    """돌려받은 리뷰 라운드 본문 — 기계 블록과 산문 두 판정 축이 같은 값을 말한다."""
    payload = json.dumps(
        {"version": 2, "findings": findings, "confirmations": []},
        ensure_ascii=False, separators=(",", ":"),
    )
    must_fix = "\n".join(f"- {item['id']}" for item in findings) or "- 없음"
    verdict = "반려" if findings else "통과"
    return (
        "## 리뷰 (code-reviewer · 2026-01-02)\n\n"
        f"## must-fix\n{must_fix}\n\n## 판정\n판정: {verdict}\n\n"
        f"```pm-review-v1\n{payload}\n```\n"
    )


def _must_fix_finding(finding_id: str = "F-001") -> dict:
    return {
        "id": finding_id, "class": "implementation-defect", "severity": "must-fix",
        "authority": "티켓 §목표", "evidence": f"{finding_id} 관측",
        "recommendation": f"{finding_id} 권고", "design_change": False,
    }


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
    shutil.copytree(TOOLS, root / ".project_manager" / "tools", ignore=_NO_BYTECODE)
    (root / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
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
    with pytest.raises(finish_rules._CloseObservationFailure) as caught:
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

    with pytest.raises(finish_rules._CloseObservationFailure) as caught:
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


def test_reconcile_cli_requires_explicit_repo_and_slot_and_rejects_task(
    tf, monkeypatch, capsys,
):
    """Recovery selector는 제품 repo+slot 둘 다 명시하며 task/암묵 해소를 받지 않는다."""
    monkeypatch.setattr(tf, "_pm_home_misanchor", lambda: None, raising=False)
    cases = (
        ["--cluster", _CLUSTER, "--reconcile-integrated", "--repo", "code"],
        ["--cluster", _CLUSTER, "--reconcile-integrated", "--task", "main"],
        ["--cluster", _CLUSTER, "--user-ack", _CLUSTER],
    )
    for argv in cases:
        with pytest.raises(SystemExit) as excinfo:
            tf._main(argv)
        assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "--repo <repo> --slot <N>" in err
    assert "--reconcile-integrated 전용" in err


# ════════════════════════════════════════════════════════════════════════
# 측정 폭 — 흡수분 제외 · 단계 표 · 앵커 해소
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

    anchor, note = review_diff.integration_anchor(root, _INTEGRATION_BRANCH)
    assert note is None
    assert anchor == _rev(root, _INTEGRATION_BRANCH)

    integration_width = review_diff.measured_diff_text(
        root, "HEAD", list(_WIDTH_SCOPE), claimed_rev=anchor)
    claim_width = review_diff.measured_diff_text(
        root, "HEAD", list(_WIDTH_SCOPE), claimed_rev=seed)

    assert integration_width.count(_OWN_MARKER) == _OWN_LINES  # 자기 몫만
    assert _ABSORBED_MARKER not in integration_width
    assert claim_width.count(_ABSORBED_MARKER) == _ABSORBED_LINES  # 옛 기준은 흡수분도 쟀다


@requires_git
def test_both_width_consumers_see_the_same_stage(external, tmp_path):
    """unified diff 원문이 단계 표(`_measure_stages`)가 정한 폭을 본다(정의 지점 하나)."""
    root, seed = _width_repo(tmp_path)
    anchor, _note = review_diff.integration_anchor(root, _INTEGRATION_BRANCH)

    assert review_diff._measure_stages("HEAD", anchor) == ((anchor, True),)

    diff_text = review_diff.measured_diff_text(
        root, "HEAD", list(_WIDTH_SCOPE), claimed_rev=anchor)
    assert diff_text.count(_OWN_MARKER) == _OWN_LINES
    assert _ABSORBED_MARKER not in diff_text

    old_diff = review_diff.measured_diff_text(
        root, "HEAD", list(_WIDTH_SCOPE), claimed_rev=seed)
    assert _ABSORBED_MARKER in old_diff             # 민감도 — 옛 기준에서는 실린다


# 통합 브랜치 앵커 해소 형상 — dev 커밋 2 + 통합 브랜치 전진(전파 커밋 2)을 흡수한 저장소.
_INTEGRATION = "task/main"      # 묶음 장부가 선언하는 통합 브랜치(폭의 기준)
_DEV_BRANCH = "dev/T-7301"      # 이 작업이 사는 브랜치
_WAVE_SRC_LINES = 320           # claim 시점 rev 이후 `src/` 에 쌓인 변경량(dev 커밋 2건 합)


def _wave_git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True, text=True, encoding="utf-8")


def _write(path: Path, lines: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"line{index}\n" for index in range(lines)), encoding="utf-8")


def _wave_repo(tmp_path: Path) -> tuple[Path, str]:
    """dev 커밋 2 + 통합 브랜치 전진(전파 커밋 2)을 흡수한 형상 저장소와 claim 시점 rev.

    리뷰 시점 트리는 clean 이고 마지막 커밋(통합 흡수)은 `src/` 를 안 건드린다 — 작업트리+직전
    커밋 한 칸으로 재던 옛 폭이 0 으로 접히는 바로 그 배치다. HEAD 는 dev 브랜치이고 통합
    브랜치는 이 작업을 아직 받지 않았다(그래서 merge-base 는 갈라진 지점이다)."""
    root = tmp_path / "code"
    root.mkdir()
    _wave_git(root, "init", "-q", "-b", _INTEGRATION)
    _wave_git(root, "config", "user.email", "t@e")
    _wave_git(root, "config", "user.name", "t")
    _write(root / "src" / "app.py", 5)
    _write(root / "docs" / "notes.md", 3)
    _wave_git(root, "add", "-A")
    _wave_git(root, "commit", "-qm", "seed")
    claim_rev = _rev(root)

    _wave_git(root, "checkout", "-q", "-b", _DEV_BRANCH)
    _write(root / "src" / "app.py", 5 + 200)
    _wave_git(root, "commit", "-qam", "dev 1")
    _write(root / "src" / "app.py", 5 + _WAVE_SRC_LINES)
    _write(root / "docs" / "notes.md", 3 + 50)
    _wave_git(root, "commit", "-qam", "dev 2")

    _wave_git(root, "checkout", "-q", _INTEGRATION)
    for index in (1, 2):
        _write(root / "docs" / f"propagation{index}.md", 4)
        _wave_git(root, "add", "-A")
        _wave_git(root, "commit", "-qm", f"propagation {index}")
    _wave_git(root, "checkout", "-q", _DEV_BRANCH)
    _wave_git(root, "merge", "-q", "--no-ff", "-m", "통합 흡수", _INTEGRATION)
    return root, claim_rev


def test_the_anchor_picks_one_worktree_inclusive_stage(external):
    """앵커가 있으면 단계는 하나다 — 그 기준점과 **현재 작업트리**의 차이(untracked 포함)."""
    assert review_diff._measure_stages("HEAD", "a" * 40) == (("a" * 40, True),)


def test_absent_anchor_keeps_the_old_stage_table(external):
    """앵커가 없으면 옛 폭 그대로다 — 작업트리 → 비면 직전 커밋 한 칸."""
    assert review_diff._measure_stages("HEAD", None) == (
        ("HEAD", True), ("HEAD~1..HEAD", False))


def test_explicit_base_owns_the_width_over_the_anchor(external):
    """`--base` 명시는 사용자가 고른 폭이라 앵커가 덮어쓰지 않는다."""
    assert review_diff._measure_stages("main", "a" * 40) == (("main", False),)


@requires_git
def test_the_anchor_is_the_merge_base_with_the_integration_branch(external, tmp_path):
    """앵커는 통합 브랜치와의 merge-base 다 — 사유 없이 통과한다."""
    root, _claim_rev = _wave_repo(tmp_path)
    expected = _git(root, "merge-base", _INTEGRATION, "HEAD").stdout.strip()

    assert review_diff.integration_anchor(root, _INTEGRATION) == (expected, None)


@requires_git
def test_a_branch_absent_from_this_tree_yields_a_loud_reason(external, tmp_path):
    """이 트리에 없는 브랜치는 **사유**를 낸다 — 다른 기준점으로 접지 않는다.

    값을 지어내 넘기면 `git diff <rev>` 가 rc≠0 이고 측정은 실패한 실행을 '변경 없음'으로
    접는다 — 조용한 0 줄(false-green)이 바로 이 축이 닫는 결함이다."""
    root, _claim_rev = _wave_repo(tmp_path)

    anchor, note = review_diff.integration_anchor(root, "task/absent")

    assert anchor is None and "해소하지 못했다" in note and "task/absent" in note


@pytest.mark.parametrize("value", ["", "--output=/tmp/x", "zz" * 20])
def test_an_unresolvable_reference_never_becomes_an_anchor(external, tmp_path, value):
    """빈 값·옵션 모양·존재하지 않는 값은 앵커가 되지 못한다(사유와 함께 멈춘다)."""
    anchor, note = review_diff.integration_anchor(tmp_path, value)
    assert anchor is None and note



# ════════════════════════════════════════════════════════════════════════
# close — 첫 부작용 앞 선검사 · 고정 여섯 단계 · 재실행은 재개
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

    def finisher(self, finisher_class=None, **overrides):
        """이 환경의 완료 기록 대역 — `finisher_class` 는 진짜 클래스를 이미 대역으로 바꾼
        테스트가 원본을 명시로 넘기는 자리다(대역이 대역을 만드는 것을 막는다).

        `overrides` 는 판정 DI 를 케이스가 골라 **진짜 판정으로 되돌리는** 자리다
        (`dod_block_fn=None` → 생성자 기본값 = 실제 DoD 판정).
        """
        cls = finisher_class or self.tf.TicketFinisher
        values = dict(
            board_py=self.board_dir.parent / "tools" / "board.py",
            log_file=self.home / ".project_manager" / "wiki" / "log" / "current.md",
            regression_cwd=str(self.slot),
            run_pytest_fn=lambda: (0, "1 passed in 0.01s"),
            run_board_fn=self.run_board,
            private_ref_block_fn=lambda tid: None,
            dod_block_fn=lambda tid: None,
            residual_block_fn=lambda tid: None,
            self_axis_block_fn=lambda tid: None,
            affected_domain_fn=lambda tid: None,
        )
        values.update(overrides)
        return cls(**values)

    def closer(self, *, cluster: str = _CLUSTER, closer_class=None, **kwargs):
        """이 환경의 종결 대역 — `cluster` 는 호출 표기 축(장부 id / 사람 이름 둘 다 받는다)."""
        values = dict(
            finisher=self.finisher(),
            board_py=self.board_dir.parent / "tools" / "board.py",
            slot=_SLOT,
            run_delegate_fn=self.run_delegate,
        )
        values.update(kwargs)
        cls = closer_class or self.tf.ClusterCloser
        return cls(cluster, **values)

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

    def lease_bytes(self) -> bytes:
        """리스 장부 원본 — 종결은 이 기록을 쓰지 않는다(실행 전후 바이트가 같다)."""
        return (self.home / ".project_manager" / ".local" /
                "worktree-leases.json").read_bytes()

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

    shutil.copytree(TOOLS, home / ".project_manager" / "tools", ignore=_NO_BYTECODE)
    (home / ".project_manager" / "wiki" / "log").mkdir(parents=True)
    (home / ".project_manager" / "wiki" / "log" / "current.md").write_text(
        "# log\n", encoding="utf-8")
    (home / ".project_manager" / ".local").mkdir(parents=True)
    (home / ".gitignore").write_text("work/\n.project_manager/.local/\n__pycache__/\n",
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
    # 묶음 브랜치의 부모 기록 — board `_cluster_ensure_branch` 가 만들 때 남기는 것과 같은 두 키.
    _git(code, "config", f"branch.{_CLUSTER_BRANCH}.pm-parent", _INTEGRATION_BRANCH)
    _git(code, "config", f"branch.{_CLUSTER_BRANCH}.pm-fork", _rev(code))
    # 이 작업의 산출 — 슬롯에서 미커밋 상태로 둔다(완료 기록이 stage 하고 종결이 커밋한다).
    (slot / "src" / "app.py").write_text("seed = 1\nwork = 2\n", encoding="utf-8")

    ticket = _TICKET
    _write_ticket(board_dir, ticket, claimed_rev=_rev(code), touches=("src/app.py",))
    _write_cluster_ledger(board_dir, tickets=(ticket,))
    rounds = board_dir / "tickets" / "rounds" / ticket
    rounds.mkdir(parents=True)
    (rounds / _ROUND_FILE).write_text("## 라운드 표본\n", encoding="utf-8")
    (rounds / _DEVELOPER_ROUND_FILE).write_text("## 구현 표본\n", encoding="utf-8")
    (rounds / _REVIEW_ROUND_FILE).write_text(_review_round_text([]), encoding="utf-8")
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

    # 리스 장부 — 종결이 작업 트리를 찾는 곳(행의 작업 폴더가 속한 저장소). 종결은 이 기록을
    # 읽기만 한다.
    (home / ".project_manager" / ".local" / "worktree-leases.json").write_text(
        json.dumps({"leases": [{
            "slot": _SLOT, "repo": "code", "session": "t", "started": _FIXTURE_STAMP, "state": "leased",
        }]}, ensure_ascii=False), encoding="utf-8")

    # 이 픽스처의 실행 세션은 리스 장부·`claimed_by` 와 같은 `t` 다. 선검사가 board 의 소유
    # 판정을 그대로 부르므로(사본 0) 세션이 해소돼야 각 케이스가 재려던 축만 남는다 — 세션
    # 미해소는 소유 거부라는 다른 축이다.
    monkeypatch.setenv("PM_SESSION_NAME", "t")
    monkeypatch.setattr(tf, "REPO", home)
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


def _prepare_reconcile(env: _CloseEnv, *, legacy: bool = False) -> tuple[str, str]:
    """반쪽 종결(open ledger + done ticket + 이미 commit된 제품 tree)을 만든다."""
    evidence = _commit(env.slot, "already integrated work")
    claimed_path = next((env.board_dir / "tickets" / "claimed").glob(f"{env.ticket}*.md"))
    text = claimed_path.read_text(encoding="utf-8").replace("status: claimed", "status: done")
    claimed_rev = next(
        line.split(":", 1)[1].strip() for line in text.splitlines()
        if line.startswith("claimed_rev:")
    )
    if legacy:
        text = "\n".join(
            line for line in text.splitlines() if not line.startswith("claimed_rev:")
        ) + "\n"
    done_path = env.board_dir / "tickets" / "done" / claimed_path.name
    done_path.write_text(text, encoding="utf-8")
    claimed_path.unlink()
    _git(env.board_dir, "add", "-A")
    _git(env.board_dir, "commit", "-qm", "ticket completed without cluster close")
    _git(env.board_dir, "push", "-q")
    assert _git(env.slot, "status", "--porcelain").stdout == ""
    return evidence, claimed_rev


def _reconcile_closer(
    env: _CloseEnv, evidence: str, *, legacy_anchor: str | None = None,
    **overrides,
):
    values = dict(
        reconcile_integrated=True,
        integrated_revisions=[f"{env.ticket}={evidence}"],
        legacy_base_revisions=(
            [f"{env.ticket}={legacy_anchor}"] if legacy_anchor is not None else []
        ),
        user_ack=_CLUSTER,
        explicit_slot=True,
    )
    values.update(overrides)
    return env.closer(**values)


@requires_git
def test_reconcile_integrated_records_exact_product_head_without_product_or_slot_write(
    close_env, capsys,
):
    """AT-002: all-done recovery는 exact 제품 HEAD를 감사하고 제품/slot을 쓰지 않는다."""
    env = close_env
    evidence, claimed_rev = _prepare_reconcile(env)
    # anchor 기대값은 준비가 심은 `claimed_rev` 를 full OID 로 정규화한 **상수**다 — 기록값을
    # 자기 자신과 대조하면 anchor 축이 통째로 틀려도 green 이 된다(X-006).
    anchor = _rev(env.slot, claimed_rev)
    assert re.fullmatch(r"[0-9a-f]{40,64}", anchor) and anchor != evidence
    product_before = (_rev(env.code), _git(env.code, "status", "--porcelain").stdout)
    slot_before = (
        _rev(env.slot), _git(env.slot, "--no-optional-locks", "status", "--porcelain").stdout,
        (env.slot / ".git").read_bytes(), _git_index_bytes(env.slot),
    )
    lease_before = (env.home / ".project_manager" / ".local" /
                    "worktree-leases.json").read_bytes()

    rc = _reconcile_closer(env, evidence).run()
    captured = capsys.readouterr()

    assert rc == 0, captured.out + captured.err
    assert product_before == (_rev(env.code), _git(env.code, "status", "--porcelain").stdout)
    assert slot_before == (
        _rev(env.slot), _git(env.slot, "--no-optional-locks", "status", "--porcelain").stdout,
        (env.slot / ".git").read_bytes(), _git_index_bytes(env.slot),
    )
    assert lease_before == (env.home / ".project_manager" / ".local" /
                            "worktree-leases.json").read_bytes()
    home = pm_paths.home_at(env.home)
    ledger = cluster_store.load_cluster(home, _CLUSTER)
    assert ledger["status"] == "closed"
    assert ledger["close_step"] == "board"
    assert ledger["closure"] == {
        "mode": "reconciled",
        "integration_head": evidence,
        "member_revisions": {env.ticket: evidence},
        "member_anchors": {
            env.ticket: {"anchor": anchor, "anchor_source": "claimed_rev"},
        },
    }
    assert env.board_calls == [] and env.delegate_calls == []
    assert env.lease_state() == "leased"
    assert "제품/slot write 0" in captured.out


@requires_git
def test_reconcile_reads_the_explicit_slot_not_the_finisher_tree(close_env, capsys):
    """AT-007: 복구 문의 제품 트리는 명시한 `--repo --slot` 슬롯이다 — finisher 트리가 아니다.

    정상 종결은 작업 트리를 묶음 기록의 브랜치로 찾아 finisher 에 넘기지만, 복구 문은 그 해소를
    타지 않는다. finisher 트리를 PM 홈으로 줘도 명시 슬롯(`work/code_1`)을 읽어 닫힌다.
    """
    env = close_env
    evidence, _anchor = _prepare_reconcile(env)
    closer = _reconcile_closer(
        env, evidence, finisher=env.finisher(regression_cwd=str(env.home)))
    assert closer._code_tree() == env.home           # 전제 — finisher 트리는 PM 홈이다

    rc = closer.run()

    captured = capsys.readouterr()
    assert rc == 0, captured.out + captured.err
    assert "PM 홈으로 강등됐다" not in captured.err
    home = pm_paths.home_at(env.home)
    ledger = cluster_store.load_cluster(home, _CLUSTER)
    assert ledger["status"] == _CLOSED_STATUS
    assert ledger["closure"]["member_revisions"] == {env.ticket: evidence}


@requires_git
def test_reconcile_integrated_fail_closed_matrix(tf, tmp_path, monkeypatch, capsys):
    """AT-003 · F-004 · X-002: 입력·graph·상태·repo·dirty·bookend 오류는 첫 write 전에 다 막힌다.

    각 케이스는 자기 3-repo 픽스처를 세우고 `(장부 bytes, 슬롯 HEAD, 리스 bytes)` 를 호출
    직전에 찍는다 — 거부는 rc=1 이면서 그 셋이 byte 불변이어야 하고, 어느 축에서 막혔는지도
    진단 문구로 구별돼야 한다(같은 rc 로 뭉뚱그리면 검사 하나를 지워도 행렬이 green 이다).
    """
    def snapshot(env):
        ledger = env.board_dir / "tickets" / "clusters" / f"{_CLUSTER}.md"
        lease = env.home / ".project_manager" / ".local" / "worktree-leases.json"
        return ledger.read_bytes(), _rev(env.slot), lease.read_bytes()

    def reject(name, build, fragment):
        """`build(env)` 가 만든 호출 = rc 1 · 세 축 byte 불변 · 진단 문구 일치."""
        env = _build_close_env(tf, tmp_path / name, monkeypatch)
        closer = build(env)
        before = snapshot(env)
        assert closer.run() == 1, f"{name}: 거부되지 않았다"
        assert snapshot(env) == before, f"{name}: 첫 write 전 거부인데 상태가 바뀌었다"
        err = capsys.readouterr().err
        assert fragment in err, f"{name}: 진단 축 불일치 — {fragment!r} 없음\n{err}"
        return env

    # ── 입력 전수성 ────────────────────────────────────────────────
    def missing_mapping(env):
        _prepare_reconcile(env)
        return env.closer(reconcile_integrated=True, integrated_revisions=[],
                          user_ack=_CLUSTER, explicit_slot=True)

    def duplicate_mapping(env):
        evidence, _anchor = _prepare_reconcile(env)
        return _reconcile_closer(env, evidence, integrated_revisions=[
            f"{env.ticket}={evidence}", f"{env.ticket}={evidence}"])

    def extra_mapping(env):
        evidence, _anchor = _prepare_reconcile(env)
        return _reconcile_closer(env, evidence, integrated_revisions=[
            f"{env.ticket}={evidence}", f"{_SYNTHETIC_REF}={evidence}"])

    reject("missing-mapping", missing_mapping, "evidence mapping 전수성 위반")
    reject("duplicate-mapping", duplicate_mapping, "mapping이 중복됐다")
    reject("extra-mapping", extra_mapping, f"여분=['{_SYNTHETIC_REF}']")

    # ── revision 해소 ──────────────────────────────────────────────
    def unresolvable_rev(env):
        _prepare_reconcile(env)
        return _reconcile_closer(env, "deadbee")

    def non_commit_object(env):
        _prepare_reconcile(env)
        tree_oid = _git(env.slot, "rev-parse", "HEAD^{tree}").stdout.strip()
        assert re.fullmatch(r"[0-9a-f]{40,64}", tree_oid), tree_oid
        return _reconcile_closer(env, tree_oid)

    reject("bad-rev", unresolvable_rev, "commit으로 해소하지 못했다")
    reject("non-commit-object", non_commit_object, "commit으로 해소하지 못했다")

    # ── 제품 graph 두 축 (recovery 가 존재하는 이유) ────────────────
    def evidence_not_in_head(env):
        _prepare_reconcile(env)
        # 통합 트리에만 있는 commit — 선택 슬롯 HEAD 의 조상이 아니다.
        (env.code / "side.txt").write_text("side\n", encoding="utf-8")
        side = _commit(env.code, "integration-only work")
        return _reconcile_closer(env, side)

    def evidence_before_anchor(env):
        evidence, claimed_rev = _prepare_reconcile(env)
        done_path = next((env.board_dir / "tickets" / "done").glob(f"{env.ticket}*.md"))
        # anchor 를 뒤 commit 으로 옮겨, HEAD 조상이지만 claim 이전인 evidence 를 만든다.
        done_path.write_text(
            done_path.read_text(encoding="utf-8").replace(
                f"claimed_rev: {claimed_rev}", f"claimed_rev: {evidence}"),
            encoding="utf-8")
        return _reconcile_closer(env, claimed_rev)

    reject("side-branch-evidence", evidence_not_in_head,
           "제품 exact HEAD의 조상이 아니다")
    reject("evidence-before-anchor", evidence_before_anchor, "anchor 이후가 아니다")

    # ── 장부·티켓 상태 ─────────────────────────────────────────────
    def not_all_done(env):
        # 산출만 커밋되고 완료 기록은 없는 상태 — 장부 open · 티켓 claimed.
        evidence = _commit(env.slot, "committed but never recorded")
        return _reconcile_closer(env, evidence)

    def non_open_status(env):
        evidence, _anchor = _prepare_reconcile(env)
        ledger_path = env.board_dir / "tickets" / "clusters" / f"{_CLUSTER}.md"
        ledger_path.write_text(
            ledger_path.read_text(encoding="utf-8").replace(
                "status: open", f"status: {env.tf._CLUSTER_STATUS_CLOSING}"),
            encoding="utf-8")
        return _reconcile_closer(env, evidence)

    def unreadable_catalog(env):
        # 귀속 유일성은 전 catalog 판독 위에서만 성립한다 — 손상 장부 하나가 곧 판정 불능이다.
        evidence, _anchor = _prepare_reconcile(env)
        broken = env.board_dir / "tickets" / "clusters" / "C-broken.md"
        broken.write_text(f"---\nid: C-broken\ntickets: [{env.ticket}\n---\n",
                          encoding="utf-8")
        return _reconcile_closer(env, evidence)

    reject("not-all-done", not_all_done, "all-done 상태가 아니다")
    reject("non-open-status", non_open_status, "recovery는 open all-done 전용이다")
    reject("unreadable-catalog", unreadable_catalog, "전수 확인할 수 없어")

    # ── legacy anchor · 선택 슬롯 ──────────────────────────────────
    def legacy_without_base(env):
        evidence, _anchor = _prepare_reconcile(env, legacy=True)
        return _reconcile_closer(env, evidence)

    def pm_home_anchor(env):
        # 복구할 제품 트리는 명시 슬롯이다 — 그 슬롯 값이 PM 홈 자신을 가리키면 거부다.
        evidence, _anchor = _prepare_reconcile(env)
        return _reconcile_closer(env, evidence, slot=".")

    def dirty_selected_slot(env):
        evidence, _anchor = _prepare_reconcile(env)
        (env.slot / "dirty.txt").write_text("preserve\n", encoding="utf-8")
        return _reconcile_closer(env, evidence)

    legacy_env = reject("legacy", legacy_without_base, "--legacy-base-rev가 필수다")
    # 같은 legacy 묶음은 명시 anchor 를 공급하면 통과하고 그 출처를 장부에 남긴다. 복구 문은
    # 명시 슬롯을 모듈 전역 `REPO` 기준으로 읽으므로 다음 케이스가 새 픽스처를 세우기 전에 돈다.
    legacy_evidence = _rev(legacy_env.slot)
    legacy_anchor = _rev(legacy_env.slot, "HEAD~1")
    assert _reconcile_closer(
        legacy_env, legacy_evidence, legacy_anchor=legacy_anchor).run() == 0
    home = pm_paths.home_at(legacy_env.home)
    closure = cluster_store.load_cluster(home, _CLUSTER)["closure"]
    assert closure["member_anchors"][legacy_env.ticket] == {
        "anchor": legacy_anchor, "anchor_source": "operator-supplied",
    }
    capsys.readouterr()

    reject("pm-home", pm_home_anchor, "PM 홈으로 강등됐다")
    dirty_env = reject("dirty", dirty_selected_slot, "선택 슬롯이 dirty")
    assert (dirty_env.slot / "dirty.txt").read_text(encoding="utf-8") == "preserve\n"

    # ── bookend drift 는 두 번째 snapshot 에서 감지하고 write 하지 않는다 ──
    env = _build_close_env(tf, tmp_path / "head-race", monkeypatch)
    evidence, _anchor = _prepare_reconcile(env)
    closer = _reconcile_closer(env, evidence)
    original = closer._validate_reconcile
    calls = 0

    def drifting_snapshot():
        nonlocal calls
        calls += 1
        closure, status, problem = original()
        if calls == 2 and closure is not None:
            closure = json.loads(json.dumps(closure))
            closure["integration_head"] = "f" * 40
        return closure, status, problem

    monkeypatch.setattr(closer, "_validate_reconcile", drifting_snapshot)
    before = snapshot(env)
    assert closer.run() == 1
    assert snapshot(env) == before
    assert "bookend" in capsys.readouterr().err


@requires_git
def test_reconcile_integrated_is_idempotent_and_preserves_other_dirty_slots(
    close_env, capsys,
):
    """AT-004: 같은 증거 재실행은 no-op이고 비선택 dirty slot은 byte 보존한다."""
    env = close_env
    evidence, anchor = _prepare_reconcile(env)
    other = env.home / "work" / "code_2"
    assert _git(
        env.code, "worktree", "add", "-q", "-b", "dirty-other", str(other),
        _INTEGRATION_BRANCH,
    ).returncode == 0
    (other / "sentinel.txt").write_text("do not touch\n", encoding="utf-8")
    lease_path = env.home / ".project_manager" / ".local" / "worktree-leases.json"
    leases = json.loads(lease_path.read_text(encoding="utf-8"))
    leases["leases"].append({
        "slot": "work/code_2", "repo": "code", "session": "other",
        "started": _FIXTURE_STAMP, "state": "leased",
    })
    lease_path.write_text(json.dumps(leases), encoding="utf-8")
    lease_before = lease_path.read_bytes()
    other_before = (
        _rev(other), _git(other, "--no-optional-locks", "status", "--porcelain").stdout,
        (other / "sentinel.txt").read_bytes(), _git_index_bytes(other),
    )

    # crash seam: ledger dump 뒤 board-git sync 전에 프로세스가 끊긴 상태를 합성한다.
    first_closer = _reconcile_closer(env, evidence)
    assert first_closer._resolve_members() is None
    closure, status, problem = first_closer._validate_reconcile()
    assert problem is None and status == "open"
    home = pm_paths.home_at(env.home)
    ledger = cluster_store.load_cluster(home, _CLUSTER)
    ledger.update({"closure": closure, "status": "closed", "close_step": "board"})
    cluster_store.dump_cluster(home, ledger)
    pre_resume_board_head = _rev(env.board_dir)

    assert first_closer.run() == 0
    assert _rev(env.board_dir) != pre_resume_board_head
    board_head = _rev(env.board_dir)
    home_head = _rev(env.home)
    slot_head = _rev(env.slot)
    assert _reconcile_closer(env, evidence).run() == 0
    assert (_rev(env.board_dir), _rev(env.home), _rev(env.slot)) == (
        board_head, home_head, slot_head,
    )
    assert lease_path.read_bytes() == lease_before
    assert other_before == (
        _rev(other), _git(other, "--no-optional-locks", "status", "--porcelain").stdout,
        (other / "sentinel.txt").read_bytes(), _git_index_bytes(other),
    )
    # 다른 유효 evidence도 이미 기록한 감사값과 다르면 fail-loud다.
    assert _reconcile_closer(env, anchor).run() == 1
    assert (_rev(env.board_dir), _rev(env.home), _rev(env.slot)) == (
        board_head, home_head, slot_head,
    )
    out = capsys.readouterr()
    assert "비선택 슬롯 보존" in out.out and "dirty(1)" in out.out


@requires_git
def test_reconcile_integrated_accepts_the_name_notation_of_the_normal_path(
    close_env, capsys,
):
    """X-004: 정상 경로가 받는 이름 표기(`wave`)로 불러도 장부 id 비교가 어긋나지 않는다."""
    env = close_env
    evidence, _anchor = _prepare_reconcile(env)

    rc = _reconcile_closer(
        env, evidence, cluster=_CLUSTER_NAME, user_ack=_CLUSTER_NAME).run()

    captured = capsys.readouterr()
    assert rc == 0, captured.out + captured.err
    home = pm_paths.home_at(env.home)
    ledger = cluster_store.load_cluster(home, _CLUSTER)
    assert ledger["status"] == _CLOSED_STATUS
    assert ledger["closure"]["member_revisions"] == {env.ticket: evidence}


@requires_git
def test_normal_close_rejects_open_all_done_cluster_before_side_effects(
    close_env, capsys,
):
    """F-001: open 장부 + 멤버 전원 done 은 정상 종결이 아니라 복구 전용이다.

    그 완료 기록은 이 파이프라인이 남긴 것이 아니다(1단계가 성공하면 장부는 `closing` 이 된다)
    — 무엇이 이미 통합됐는지 모르는 채 커밋·재배치·머지로 들어가지 않는다. 게이트는
    `open` + 전원 done 에만 걸리고 `closing` 재개는 그대로 통과한다.
    """
    env = close_env
    _evidence, _anchor = _prepare_reconcile(env)
    ledger_path = env.board_dir / "tickets" / "clusters" / f"{_CLUSTER}.md"
    before = (ledger_path.read_bytes(), _rev(env.slot), _rev(env.board_dir),
              _rev(env.home), env.integration_log(), env.lease_state())

    rc = env.closer().run()

    captured = capsys.readouterr()
    assert rc == 1, captured.out + captured.err
    assert "--reconcile-integrated" in captured.err
    assert "[1/6]" not in captured.out, "첫 단계가 이미 시작됐다(부작용 앞 게이트가 아님)"
    assert env.board_calls == [] and env.delegate_calls == []
    assert before == (ledger_path.read_bytes(), _rev(env.slot), _rev(env.board_dir),
                      _rev(env.home), env.integration_log(), env.lease_state())

    # 게이트는 좁다 — 같은 픽스처의 장부를 `closing` 으로 두면 정상 재개가 그대로 돈다.
    ledger_path.write_text(
        ledger_path.read_text(encoding="utf-8").replace(
            "status: open", f"status: {env.tf._CLUSTER_STATUS_CLOSING}"),
        encoding="utf-8")
    resumed = env.closer().run()
    resumed_out = capsys.readouterr()
    assert resumed == 0, resumed_out.out + resumed_out.err
    assert env.ledger()["status"] == _CLOSED_STATUS


@requires_git
def test_close_runs_the_six_steps_and_leaves_no_hand_work(close_env, capsys):
    """성공 경로 — 여섯 단계가 순서대로 돌고 커밋·재배치·머지·기록이 실제로 남는다(대여는 그대로)."""
    env = close_env
    # 통합 브랜치가 앞서 나가 있다 — 재배치 단계가 무대상이 아니게 한다.
    (env.code / "other.txt").write_text("x\n", encoding="utf-8")
    _commit(env.code, "integration moves on")
    lease_before = env.lease_bytes()

    rc = env.closer().run()

    out = capsys.readouterr().out
    assert rc == 0, out
    order = [line.split("] ", 1)[1].removesuffix("...") for line in out.splitlines()
             if line.startswith("[") and "/6] " in line]
    assert order == [label for _key, label, _pre in env.tf.ClusterCloser.STEPS], order
    # 단계 2 — 완료 기록이 돌았다(board complete 1회).
    assert [args[0] for args in env.board_calls] == ["complete"]
    # 단계 3 — 슬롯 커밋 문안은 티켓 제목이다.
    assert env.slot_log()[0] == "종결 픽스처"
    # 단계 4 — 통합 브랜치가 앞서 얹은 커밋 위로 재배치됐다.
    assert "integration moves on" in env.slot_log()
    # 단계 5 — `--no-ff` 머지 문안.
    assert env.integration_log()[0] == f"{env.ticket} merge — 종결 픽스처"
    assert _git(env.code, "rev-parse", f"{_INTEGRATION_BRANCH}^2").returncode == 0
    # 단계 6 — PM 홈 포인터·산출물 커밋.
    assert env.home_log()[0] == f"{env.ticket} board — 종결 픽스처"
    # 종결은 슬롯을 반납하지 않는다 — 대여 기록은 바이트 그대로다.
    assert env.lease_bytes() == lease_before
    committed = _git(env.home, "show", "--name-only", "--format=", "HEAD").stdout
    assert ".project_manager/board" in committed
    assert ".project_manager/wiki/log/current.md" in committed
    # 종결이 하는 일을 사람에게 다시 시키지 않는다.
    assert "git commit — **경로를 명시**하라" not in out
    assert "종결 파이프라인이 실행한다" in out


@requires_git
def test_close_of_an_all_discarded_cluster_runs_no_code_step(close_env, capsys):
    """AT-005: 멤버가 전부 처분된 묶음은 작업 트리를 찾지 않고 두 단계로 닫힌다.

    실측 형상(`C-delegate-model-single-truth-v1.7.12`)은 멤버 하나가 폐기됐고 장부가 선언한
    묶음 브랜치·통합 브랜치가 코드 git 에 없다. 머지할 코드가 없으므로 코드 네 단계는 대상이
    아니고, 제품 git 은 한 번도 바뀌지 않는다. 트리가 필요한 단계가 없으니 작업 트리 해소도
    돌지 않는다(장부의 브랜치가 git 에 없어도 멈추지 않는다). 대여 기록은 그대로다.
    """
    env = close_env
    _discard_ticket(env)
    # 폐기는 그 라운드의 산출을 버린 처분이라 슬롯에 남을 미커밋 편집도 없다.
    _git(env.slot, "checkout", "--", "src/app.py")
    _write_cluster_ledger(env.board_dir, tickets=(env.ticket,),
                          base_branch="task/absent-integration",
                          branch="task/absent-cluster")
    before = (_rev(env.code), _rev(env.slot), env.integration_log(), env.slot_log(),
              _git(env.slot, "status", "--porcelain").stdout)
    assert before[-1].strip() == ""
    lease_before = env.lease_bytes()

    rc = env.closer().run()

    out = capsys.readouterr().out
    assert rc == 0, out
    # 밟은 단계는 둘 — 코드 네 단계가 목록에서 빠졌다.
    order = [line.split("] ", 1)[1].removesuffix("...") for line in out.splitlines()
             if line.startswith("[") and "/2] " in line]
    assert order == ["기계 확인 생성·리뷰 게이트 처분", "board·포인터 커밋"], out
    assert "처분 종결 멤버 1" in out
    assert "작업 트리 →" not in out
    assert env.board_calls == [] and env.delegate_calls == []
    assert env.lease_bytes() == lease_before  # 대여는 그대로다 — 종결은 반납하지 않는다
    # 제품 git 은 어느 축도 바뀌지 않았다(커밋·재배치·머지 0회).
    assert before == (_rev(env.code), _rev(env.slot), env.integration_log(),
                      env.slot_log(), _git(env.slot, "status", "--porcelain").stdout)
    # 장부는 닫혔고 그 종결이 board-git·PM 홈에 기록됐다.
    assert env.ledger()["status"] == _CLOSED_STATUS
    assert env.ledger()["close_step"] == env.tf.ClusterCloser.STEPS[-1][0]
    assert env.home_log()[0].startswith(f"{env.ticket} board — ")


@requires_git
def test_a_live_member_still_requires_the_code_steps(tf, tmp_path, monkeypatch, capsys):
    """민감도 — 살아 있는 멤버가 있으면 같은 장부가 종전대로 브랜치를 요구한다.

    폐기 멤버 건너뛰기가 정상 종결의 판정을 느슨하게 만들지 않는다(멤버를 처분하지 않은 것
    하나만 다르고 장부·트리는 같다). 장부의 묶음 브랜치를 쥔 작업 트리가 없으므로 첫 부작용
    앞의 작업 트리 해소에서 멈춘다.
    """
    env = _build_close_env(tf, tmp_path / "live-member", monkeypatch)
    _write_cluster_ledger(env.board_dir, tickets=(env.ticket,),
                          base_branch="task/absent-integration",
                          branch="task/absent-cluster")
    ledger_before = (env.board_dir / "tickets" / "clusters" / f"{_CLUSTER}.md").read_bytes()
    lease_before = env.lease_bytes()

    rc = env.closer().run()

    captured = capsys.readouterr()
    assert rc == 1
    assert "[중단] 작업 트리 해소" in captured.err
    assert ("묶음 기록 branch task/absent-cluster 를 체크아웃한 작업 트리가 없다"
            in captured.err)
    assert "[1/6]" not in captured.out          # 첫 부작용 앞에서 막혔다
    assert env.board_calls == [] and env.lease_bytes() == lease_before
    assert (env.board_dir / "tickets" / "clusters" /
            f"{_CLUSTER}.md").read_bytes() == ledger_before


@requires_git
def test_close_preflight_reports_a_missing_integration_branch_in_the_found_tree(
    close_env, capsys,
):
    """찾은 작업 트리의 저장소에 장부 `base_branch` 가 없으면 선검사가 그 사유로 막는다.

    작업 트리 해소는 묶음 브랜치를 쥔 슬롯으로 통과한다. 통합 브랜치 부재는 첫 부작용 앞
    선검사(재배치)가 낸다 — 이 판정이 빠지면 완료 기록과 슬롯 커밋을 마친 뒤에야 멈춘다.
    """
    env = close_env
    _write_cluster_ledger(env.board_dir, tickets=(env.ticket,),
                          base_branch="task/absent-integration")
    logs_before = (env.slot_log(), env.integration_log(), env.home_log())
    lease_before = env.lease_bytes()

    rc = env.closer().run()

    captured = capsys.readouterr()
    assert rc == 1, captured.out + captured.err
    assert f"작업 트리 → {env.slot} (묶음 기록 branch {_CLUSTER_BRANCH})" in captured.out
    assert "종결 선검사" in captured.err
    assert ("통합 브랜치 task/absent-integration 를 이 코드 트리에서 찾지 못했다"
            in captured.err)
    assert "[1/6]" not in captured.out          # 첫 부작용 앞에서 막혔다
    assert env.board_calls == [] and env.delegate_calls == []
    assert (env.slot_log(), env.integration_log(), env.home_log()) == logs_before
    assert env.lease_bytes() == lease_before


# ── 작업 트리 해소 — 묶음 기록의 브랜치를 쥔 트리 정확히 하나 ─────────────────
#
# 작업 트리는 task 가 쥔 슬롯에서 고르지 않는다. 대여 기록의 작업 폴더가 속한 저장소에서 묶음
# 기록의 `branch`(없으면 `base_branch`)를 체크아웃한 작업 트리를 찾고, 정확히 하나일 때만 그
# 트리에서 돈다. 멈춤은 전부 첫 부작용 앞이다 — board 호출 0 · 통합 브랜치 불변 · 대여 기록 불변.


def _add_code_tree(env, name: str) -> Path:
    """코드 git 에 작업 트리 하나를 detached 로 더한다(브랜치는 케이스가 정한다)."""
    tree = env.home / "work" / name
    assert _git(env.code, "worktree", "add", "-q", "--detach", str(tree),
                _INTEGRATION_BRANCH).returncode == 0
    return tree


@requires_git
def test_task_holding_two_slots_closes_and_keeps_both_leases(close_env, monkeypatch, capsys):
    """AT-001: task 가 슬롯 둘(묶음 브랜치·합칠 곳)을 쥔 정상 형상에서 마무리가 끝까지 돈다.

    발단(T-0923 마무리): 옛 엔진은 작업 트리를 task 가 쥔 슬롯 집합에서 골라, 슬롯이 둘이면
    "통틀어 모호" 로 멈추고 머지를 한 번도 하지 않았다. 작업 트리는 묶음 기록 `branch` 를 쥔
    트리 하나로 정해지고, 합칠 곳 브랜치는 다른 슬롯이 쥔 채 머지를 받으며, 두 대여가 그대로 남는다.
    """
    env = close_env
    # 합칠 곳 브랜치를 코드 git 원 트리에서 슬롯 2 로 옮긴다 — task t 가 두 슬롯을 쥔 형상.
    assert _git(env.code, "checkout", "-q", "--detach").returncode == 0
    integration_slot = _add_code_tree(env, "code_2")
    assert _git(integration_slot, "checkout", "-q", _INTEGRATION_BRANCH).returncode == 0
    lease_path = env.home / ".project_manager" / ".local" / "worktree-leases.json"
    lease_path.write_text(json.dumps({"leases": [
        {"slot": _SLOT, "repo": "code", "session": "t", "started": _FIXTURE_STAMP,
         "state": "leased"},
        {"slot": "work/code_2", "repo": "code", "session": "t", "started": _FIXTURE_STAMP,
         "state": "leased"},
    ]}, ensure_ascii=False), encoding="utf-8")
    lease_before = env.lease_bytes()
    finisher = env.finisher()
    monkeypatch.setattr(env.tf, "TicketFinisher", lambda **kwargs: finisher)

    rc = env.tf._main(["--cluster", _CLUSTER, "--task", "t", "--no-pytest"])

    captured = capsys.readouterr()
    assert rc == 0, captured.out + captured.err
    order = [line.split("] ", 1)[1].removesuffix("...") for line in captured.out.splitlines()
             if line.startswith("[") and "/6] " in line]
    assert order == [label for _key, label, _pre in env.tf.ClusterCloser.STEPS], order
    assert f"작업 트리 → {env.slot} (묶음 기록 branch {_CLUSTER_BRANCH})" in captured.out
    assert env.integration_log()[0] == f"{env.ticket} merge — 종결 픽스처"
    assert _git(integration_slot, "symbolic-ref", "--short", "HEAD").stdout.strip() == \
        _INTEGRATION_BRANCH
    assert env.lease_bytes() == lease_before


@requires_git
def test_close_stops_when_no_tree_holds_the_ledger_branch(close_env, capsys):
    """AT-002: 묶음 브랜치를 쥔 작업 트리가 없으면 첫 부작용 전에 멈춘다(다른 트리를 고르지 않는다)."""
    env = close_env
    assert _git(env.slot, "checkout", "-q", "-b", "dev/side").returncode == 0
    integration_before = env.integration_log()
    lease_before = env.lease_bytes()

    rc = env.closer().run()

    captured = capsys.readouterr()
    assert rc == 1, captured.out + captured.err
    assert "[중단] 작업 트리 해소" in captured.err
    assert f"{_CLUSTER_BRANCH} 를 체크아웃한 작업 트리가 없다" in captured.err
    assert "종결 선검사" not in captured.err
    assert "[1/6]" not in captured.out
    assert env.board_calls == [] and env.delegate_calls == []
    assert env.integration_log() == integration_before
    assert env.lease_bytes() == lease_before


@requires_git
def test_close_stops_when_two_trees_hold_the_ledger_branch(close_env, capsys):
    """AT-003: 두 트리가 묶음 브랜치를 함께 쥐면(git 제약의 우회) 고르지 않고 멈춘다."""
    env = close_env
    other = _add_code_tree(env, "code_2")
    assert _git(other, "checkout", "-q", "--ignore-other-worktrees",
                _CLUSTER_BRANCH).returncode == 0
    integration_before = env.integration_log()
    lease_before = env.lease_bytes()

    rc = env.closer().run()

    captured = capsys.readouterr()
    assert rc == 1, captured.out + captured.err
    assert "[중단] 작업 트리 해소" in captured.err
    assert f"{_CLUSTER_BRANCH} 를 체크아웃한 작업 트리가 2개다" in captured.err
    assert "[1/6]" not in captured.out
    assert env.board_calls == [] and env.delegate_calls == []
    assert env.integration_log() == integration_before
    assert env.lease_bytes() == lease_before


@requires_git
def test_size_one_close_requires_explicit_cluster_binding(close_env, capsys):
    """branch 없는 크기 1 장부는 base 보유 트리가 있어도 첫 부작용 전에 거부한다."""
    env = close_env
    _write_cluster_ledger(env.board_dir, tickets=(env.ticket,), branch=None)
    _git(env.slot, "checkout", "--", "src/app.py")
    (env.code / "src" / "app.py").write_text("seed = 1\nwork = 2\n", encoding="utf-8")
    slot_before = env.slot_log()
    lease_before = env.lease_bytes()

    rc = env.closer().run()

    out = capsys.readouterr().out
    assert rc == 1, out
    assert env.board_calls == [] and env.delegate_calls == []
    assert env.integration_log()[0] == "code seed"
    assert env.slot_log() == slot_before
    assert env.lease_bytes() == lease_before


@requires_git
def test_close_never_picks_the_pm_home_tree(close_env, capsys):
    """AT-012: PM 홈은 찾는 곳이 아니다 — 홈이 `base_branch` 를 쥐어도 작업 트리 후보가 아니다.

    인자 없는 발행이 PM 홈 브랜치를 합칠 곳으로 적은 묶음에서 홈을 작업 트리로 잡으면 코드 없이
    완료된다. 홈은 대여 기록의 작업 폴더가 속한 저장소일 때만 목록에 든다.
    """
    env = close_env
    assert _git(env.home, "symbolic-ref", "--short", "HEAD").stdout.strip() == "main"
    _write_cluster_ledger(env.board_dir, tickets=(env.ticket,), base_branch="main", branch=None)
    home_before = env.home_log()
    lease_before = env.lease_bytes()

    rc = env.closer().run()

    captured = capsys.readouterr()
    assert rc == 1, captured.out + captured.err
    assert "[중단] 작업 트리 해소" in captured.err
    assert "제품 브랜치 미결속" in captured.err
    assert env.board_calls == [] and env.delegate_calls == []
    assert env.home_log() == home_before
    assert env.ledger()["status"] != _CLOSED_STATUS
    assert env.lease_bytes() == lease_before


@requires_git
def test_close_stops_when_the_ledger_branch_tree_is_gone_from_disk(close_env, capsys):
    """폴더가 디스크에서 사라지고 git 등록만 남은 자리는 작업 트리가 아니다.

    `git worktree list` 는 지운 슬롯 폴더를 그 브랜치와 함께 계속 낸다(prunable). 같은 저장소의
    다른 대여 폴더에서 목록을 보아도 그 등록은 후보가 아니다 — 없는 폴더를 작업 트리로 고르면
    선검사의 git 조회가 traceback 으로 죽는다. 첫 부작용 앞 작업 트리 해소에서 멈춘다.
    """
    env = close_env
    other = _add_code_tree(env, "code_2")
    lease_path = env.home / ".project_manager" / ".local" / "worktree-leases.json"
    lease_path.write_text(json.dumps({"leases": [
        {"slot": _SLOT, "repo": "code", "session": "t", "started": _FIXTURE_STAMP,
         "state": "leased"},
        {"slot": "work/code_2", "repo": "code", "session": "t", "started": _FIXTURE_STAMP,
         "state": "leased"},
    ]}, ensure_ascii=False), encoding="utf-8")
    shutil.rmtree(env.slot)
    # 묶음 브랜치를 쥔 트리는 지운 슬롯 하나뿐이었다 — 목록의 그 줄은 남은 등록에서 온다.
    listing = _git(other, "worktree", "list", "--porcelain").stdout
    assert f"branch refs/heads/{_CLUSTER_BRANCH}" in listing
    integration_before = env.integration_log()
    lease_before = env.lease_bytes()

    rc = env.closer().run()

    captured = capsys.readouterr()
    assert rc == 1, captured.out + captured.err
    assert "[중단] 작업 트리 해소" in captured.err
    assert (f"묶음 기록 branch {_CLUSTER_BRANCH} 를 체크아웃한 작업 트리가 없다"
            in captured.err)
    assert "작업 트리 →" not in captured.out
    assert "종결 선검사" not in captured.err
    assert "[1/6]" not in captured.out
    assert env.board_calls == [] and env.delegate_calls == []
    assert env.integration_log() == integration_before
    assert env.lease_bytes() == lease_before


@requires_git
def test_close_stops_when_two_trees_hold_the_integration_branch(close_env, capsys):
    """통합 브랜치를 두 트리가 함께 쥐면(git 제약의 우회) 머지할 트리를 고르지 않고 멈춘다.

    작업 트리 해소와 같은 규칙·같은 문구다 — 첫 트리를 고르면 머지할 트리가 목록 순서로
    정해진다. 작업 트리는 묶음 브랜치로 찾고, 멈춤은 첫 부작용 앞 선검사(머지)에서 난다.
    """
    env = close_env
    other = _add_code_tree(env, "code_2")
    assert _git(other, "checkout", "-q", "--ignore-other-worktrees",
                _INTEGRATION_BRANCH).returncode == 0
    integration_before = env.integration_log()
    lease_before = env.lease_bytes()

    rc = env.closer().run()

    captured = capsys.readouterr()
    assert rc == 1, captured.out + captured.err
    assert f"작업 트리 → {env.slot} (묶음 기록 branch {_CLUSTER_BRANCH})" in captured.out
    assert f"{_INTEGRATION_BRANCH} 를 체크아웃한 작업 트리가 2개다" in captured.err
    assert "[1/6]" not in captured.out
    assert env.board_calls == [] and env.delegate_calls == []
    assert env.integration_log() == integration_before
    assert env.lease_bytes() == lease_before


# ── 첫 부작용 앞 선검사 — 막는 조건을 한 번에 전부 낸다 ────────────────────


def _detach_integration_tree(env) -> None:
    """통합 브랜치를 든 트리를 없앤다 — 그 브랜치를 체크아웃한 작업 트리가 0이 된다."""
    assert _git(env.code, "checkout", "-q", "--detach").returncode == 0


def _reopen_ticket(env) -> None:
    """멤버를 claimed 밖(open)으로 되돌린다 — 완료 기록이 옮길 수 없는 상태."""
    claimed = next((env.board_dir / "tickets" / "claimed").glob(f"{env.ticket}*.md"))
    target = env.board_dir / "tickets" / "open" / claimed.name
    claimed.replace(target)
    target.write_text(
        target.read_text(encoding="utf-8").replace("status: claimed", "status: open"),
        encoding="utf-8")


def _discard_ticket(env) -> Path:
    """멤버를 처분 종결(discarded)로 옮긴다 — 완료 기록 대상도 코드 산출도 없는 상태."""
    claimed = next((env.board_dir / "tickets" / "claimed").glob(f"{env.ticket}*.md"))
    target = env.board_dir / "tickets" / "discarded" / claimed.name
    target.parent.mkdir(parents=True, exist_ok=True)
    claimed.replace(target)
    target.write_text(
        target.read_text(encoding="utf-8").replace(
            "status: claimed", f"status: {_DISCARDED_STATUS}"),
        encoding="utf-8")
    _git(env.board_dir, "add", "-A")
    _git(env.board_dir, "commit", "-qm", "member discarded")
    _git(env.board_dir, "push", "-q")
    return target


def _unmark_dod(env) -> None:
    """완료 조건을 미마감으로 되돌린다 — board 가 소유한 DoD 판정의 입력."""
    for status in ("claimed", "open"):
        for path in (env.board_dir / "tickets" / status).glob(f"{env.ticket}*.md"):
            path.write_text(
                path.read_text(encoding="utf-8").replace("- [x] 표본", "- [ ] 표본"),
                encoding="utf-8")


@requires_git
def test_close_preflight_reports_every_readonly_block_at_once(close_env, capsys):
    """AT-001: 읽기로 판정되는 차단 사유 셋을 동시에 심어도 **한 실행에 전건**이 나온다.

    옛 거동은 단계 루프·완료 기록 preflight 루프·`board complete` 세 층이 전부 첫 사유에서
    멈추는 것이라, 사람이 같은 명령을 사유 수만큼 다시 쳐야 첫 완주에 닿았다(실측).
    """
    env = close_env
    _unmark_dod(env)
    _reopen_ticket(env)
    _detach_integration_tree(env)
    before = (env.slot_log(), env.home_log(), env.integration_log(), env.lease_state())

    rc = env.closer(finisher=env.finisher(dod_block_fn=None)).run()

    captured = capsys.readouterr()
    assert rc == 1, captured.out + captured.err
    assert "종결 선검사" in captured.err
    reasons = [line for line in captured.err.splitlines()
               if re.match(r"^  \d+\. ", line)]
    assert len(reasons) >= 3, captured.err
    joined = captured.err
    assert "완료 조건(DoD) 미마감" in joined
    assert "claimed 가 아니다" in joined
    assert "체크아웃한 작업 트리를 찾지 못했다" in joined
    # 첫 부작용이 0 이다 — 단계 표시조차 찍히지 않는다.
    assert "[1/6]" not in captured.out
    assert env.board_calls == [] and env.delegate_calls == []
    assert before == (env.slot_log(), env.home_log(), env.integration_log(),
                      env.lease_state())


def _reason_bodies(reasons: list[str]) -> list[str]:
    """`[단계 이름] 사유` 에서 사유 본문만 뽑는다 — 중복 판정의 축은 라벨이 아니라 본문이다."""
    return [reason.split("] ", 1)[1] for reason in reasons]


def _step_label(tf, key: str) -> str:
    return next(label for step, label, _pre in tf.ClusterCloser.STEPS if step == key)


@requires_git
def test_close_preflight_reports_no_duplicate_reason(close_env):
    """두 단계가 같은 조건을 각자 판정해도 목록에는 한 번만 실린다(다른 사유는 안 잃는다).

    재배치와 머지는 기준 브랜치 선언과 이 트리가 든 브랜치를 둘 다 본다. 그대로 누적하면
    "막는 조건 N건" 의 N 이 부풀어 사람이 고칠 것을 두 개로 읽는다.
    """
    env = close_env

    # (1) 이 트리가 묶음 브랜치를 들고 있지 않다 — 재배치·머지가 같은 문장을 각자 낸다.
    _unmark_dod(env)
    _commit(env.slot, "cluster work")
    assert _git(env.slot, "checkout", "-q", "-b", "dev/side").returncode == 0
    closer = env.closer(finisher=env.finisher(dod_block_fn=None))
    assert closer._resolve_members() is None
    wrong_branch = closer._pre_rebase()
    assert len(wrong_branch) == 1 and wrong_branch[0] in closer._pre_merge()

    bodies = _reason_bodies(closer.preflight())

    assert bodies.count(wrong_branch[0]) == 1, bodies
    assert len(bodies) == len(set(bodies)), bodies
    # 접기가 라벨을 고르는 축 — 먼저 판정한 단계의 라벨이 남는다.
    assert closer.preflight()[bodies.index(wrong_branch[0])].startswith(
        f"[{_step_label(env.tf, 'rebase')}] ")
    # 서로 다른 사유는 하나도 잃지 않는다.
    assert any("완료 조건(DoD) 미마감" in body for body in bodies), bodies

    # (2) 기준 브랜치 미선언 — 두 단계가 조기 반환으로 같은 문장을 각자 낸다.
    _write_cluster_ledger(env.board_dir, tickets=(env.ticket,), base_branch=None)
    undeclared = env.closer()
    assert undeclared._resolve_members() is None
    assert undeclared._pre_rebase() == undeclared._pre_merge() != []

    bodies = _reason_bodies(undeclared.preflight())

    assert bodies.count(undeclared._pre_rebase()[0]) == 1, bodies
    assert len(bodies) == len(set(bodies)), bodies


@requires_git
def test_close_preflight_pass_then_pipeline_never_blocks_on_readonly_reason(
    close_env, capsys,
):
    """AT-002: 선검사를 통과한 종결은 읽기로 알 수 있는 사유로 중간에 멈추지 않는다.

    반대 축도 같은 자리에서 지킨다 — 커밋 전 슬롯 dirty 는 앞 단계가 해소하는 조건이라
    선검사 사유가 아니다(비단조 조건을 넣으면 정상 실행이 첫 줄에서 막힌다).
    """
    env = close_env
    (env.code / "other.txt").write_text("x\n", encoding="utf-8")
    _commit(env.code, "integration moves on")
    closer = env.closer()

    # 슬롯에 아직 미커밋 산출이 남은 첫 실행 형상 — 그래도 선검사 사유는 0이다.
    assert _git(env.slot, "status", "--porcelain").stdout.strip() != ""
    assert closer._resolve_members() is None
    assert closer.preflight() == []

    rc = closer.run()

    captured = capsys.readouterr()
    assert rc == 0, captured.out + captured.err
    assert "[중단]" not in captured.err
    assert env.lease_state() == "leased"


@requires_git
def test_close_preflight_preserves_resume_of_finished_steps(close_env, capsys, tmp_path,
                                                            monkeypatch):
    """AT-003: 끝난 단계는 차단 사유가 아니고, 아직 낼 부작용이 남았으면 그 면제가 꺼진다."""
    env = close_env
    assert env.closer().run() == 0
    capsys.readouterr()
    # 머지까지 끝난 뒤 통합 트리를 없애도 재실행은 통과한다 — 그 단계는 이미 끝났다.
    _detach_integration_tree(env)

    rc = env.closer().run()

    captured = capsys.readouterr()
    assert rc == 0, captured.out + captured.err

    # 반대 방향 — 커밋할 것이 남은 형상에서는 "이미 머지됨" 관측을 믿지 않는다.
    fresh = _build_close_env(env.tf, tmp_path / "pending-work", monkeypatch)
    _detach_integration_tree(fresh)
    pending = fresh.closer()
    assert pending._resolve_members() is None
    reasons = pending.preflight()
    assert any("체크아웃한 작업 트리를 찾지 못했다" in reason for reason in reasons), reasons


@requires_git
def test_close_steps_declare_preflight_membership(close_env):
    """AT-006: 단계마다 선검사 대상 여부가 선언이고, 선언 없는 단계는 그 자리에서 터진다.

    반납 단계와 그 의존처(반납 seam·대여 상태 조회·묶음 브랜치 삭제 시도)는 표면에 없다 —
    종결은 슬롯을 건드리지 않는다.
    """
    env = close_env
    closer_class = env.tf.ClusterCloser
    keys = [key for key, _label, _pre in closer_class.STEPS]

    assert keys == ["resolve", "record", "commit", "rebase", "merge", "board"]
    assert "confirm" not in keys
    for removed in ("_step_release", "_default_release", "_slot_state",
                    "_delete_cluster_branch"):
        assert not hasattr(closer_class, removed), removed
    assert "release_fn" not in inspect.signature(closer_class).parameters
    assert "반납" not in env.tf.build_parser().format_help()
    for key, _label, precheck in closer_class.STEPS:
        assert callable(getattr(closer_class, f"_step_{key}", None)), key
        assert precheck is None or callable(getattr(closer_class, precheck, None)), key

    class _UndeclaredStepCloser(closer_class):
        STEPS = (("merge", "통합 브랜치 머지"),)          # 선언 없는 2-튜플

    with pytest.raises(ValueError):
        env.closer(closer_class=_UndeclaredStepCloser).run()


@requires_git
def test_close_preflight_calls_the_same_judgement_functions(close_env, monkeypatch):
    """AT-005: 선검사는 판정을 재구현하지 않고 단계가 쓰는 그 함수를 부른다(사본 0)."""
    env = close_env
    calls: list[str] = []

    def _record(name):
        def _fn(tid, *_a, **_k):
            calls.append(f"{name}:{tid}")
            return None
        return _fn

    finisher = env.finisher(
        private_ref_block_fn=_record("private_ref"),
        dod_block_fn=_record("dod"),
        residual_block_fn=_record("residual"),
        self_axis_block_fn=lambda tid: pytest.fail("선검사가 pytest 를 도는 판정을 불렀다"),
    )
    closer = env.closer(finisher=finisher)

    ownership_calls: list[object] = []
    monkeypatch.setattr(
        cluster_binding, "cluster_complete_binding_problem",
        lambda _home, tid, fm, binding: calls.append(f"binding:{tid}") or None)
    ownership = ticket_access._ticket_ownership
    monkeypatch.setattr(
        ticket_access, "_ticket_ownership",
        lambda home, fm, **kwargs: ownership_calls.append((home, fm, kwargs)) or ownership(home, fm, **kwargs))
    monkeypatch.setattr(
        board_ticket, "_round_completion_problems",
        lambda home, tid, spec_text: calls.append(f"rounds:{tid}") or [])

    assert closer._resolve_members() is None
    assert closer.preflight() == []

    assert calls == [
        f"private_ref:{env.ticket}", f"dod:{env.ticket}",
        f"residual:{env.ticket}", f"binding:{env.ticket}", f"rounds:{env.ticket}",
    ]
    # 이 픽스처의 명시 task와 기본 user는 모두 None이다.
    assert len(ownership_calls) == 1
    assert ownership_calls[0] == (closer._home, ticket_store.load_ticket(
        ticket_store.find_ticket_exact(closer._home, env.ticket)[1])[0],
        {"task": None, "user": None})


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
    assert env.lease_state() == "leased"


@requires_git
def test_close_is_a_no_op_when_everything_is_already_done(close_env, capsys):
    """전부 끝난 묶음에 다시 실행하면 어느 단계도 새 부작용을 만들지 않는다."""
    env = close_env
    assert env.closer().run() == 0
    capsys.readouterr()
    home_before = env.home_log()
    integration_before = env.integration_log()
    lease_before = env.lease_bytes()

    rc = env.closer().run()
    captured = capsys.readouterr()
    out = captured.out

    assert rc == 0, out + captured.err
    assert [args[0] for args in env.board_calls] == ["complete"]
    assert env.home_log() == home_before
    assert env.integration_log() == integration_before
    assert env.lease_bytes() == lease_before
    assert "이미 task/main 에 있다" in out or "이미 task/main 위에 있다" in out


@requires_git
def test_close_records_progress_in_the_cluster_ledger(close_env):
    """장부에 단계 진행과 종결 상태가 남는다 — 열거는 board 가 소유한 값이다."""
    env = close_env
    assert env.closer().run() == 0
    ledger = env.ledger()
    assert ledger["close_step"] == env.tf.ClusterCloser.STEPS[-1][0]
    assert ledger["status"] == "closed"


def _write_review_round(env, text: str) -> Path:
    path = env.board_dir / "tickets" / "rounds" / env.ticket / _REVIEW_ROUND_FILE
    path.write_text(text, encoding="utf-8")
    _git(env.board_dir, "add", "-A")
    _git(env.board_dir, "commit", "-qm", "review round")
    _git(env.board_dir, "push", "-q")
    return path


def _append_disposition(env, decision: str) -> None:
    """멤버 명세 PM 영역에 F-001 처분 블록을 더한다(리뷰 라운드 03 대상)."""
    spec = next((env.board_dir / "tickets" / "claimed").glob(f"{env.ticket}*.md"))
    payload = json.dumps({
        "version": 1, "reviewer_role": "code-reviewer", "reviewer_ordinal": 3,
        "dispositions": [{
            "id": "F-001", "decision": decision, "reason": f"PM {decision} 근거",
            "scope": "F-001 범위" if decision == "accepted" else "", "prerequisite": "",
        }],
    }, ensure_ascii=False, separators=(",", ":"))
    spec.write_text(
        spec.read_text(encoding="utf-8")
        + f"\n```pm-review-disposition-v1\n{payload}\n```\n",
        encoding="utf-8",
    )
    _git(env.board_dir, "add", "-A")
    _git(env.board_dir, "commit", "-qm", "disposition")
    _git(env.board_dir, "push", "-q")


@requires_git
def test_close_refuses_an_undisposed_review_must_fix_then_passes_after_disposition(
    close_env, capsys,
):
    """AT-002 — 내부 기록 없이 리뷰 라운드 파일의 미처분 must-fix 가 첫 부작용 앞에서 막힌다.

    판정 입력 이전 전에는 이 형상의 `_pending_gates` 가 빈 목록이라 종결이 그대로 진행했다.
    처분(기각) 기입 뒤에는 같은 종결이 끝까지 간다(정상 사용 차단 없음).
    """
    env = close_env
    _write_review_round(env, _review_round_text([_must_fix_finding()]))
    assert not (env.home / ".project_manager" / ".local" / "internal_review_rounds.json").exists()

    rc = env.closer().run()
    out = capsys.readouterr()

    assert rc == 1, out.out + out.err
    assert "리뷰 확인 입력 미충족" in out.err and env.ticket in out.err
    assert env.board_calls == []
    assert env.delegate_calls == []
    assert env.slot_log()[0] == "code seed"
    assert env.lease_state() == "leased"

    _append_disposition(env, "rejected")
    rc = env.closer().run()
    out = capsys.readouterr()

    assert rc == 0, out.out + out.err
    assert [args[0] for args in env.board_calls] == ["complete"]
    assert env.delegate_calls == []          # 처분된 잔여는 resolve 대상이 아니다
    assert not (env.home / ".project_manager" / ".local" / "internal_review_rounds.json").exists()


@requires_git
@pytest.mark.parametrize("shape", ["absent", "pending"])
def test_close_preflight_refuses_a_member_without_a_harvested_review(
    close_env, capsys, shape,
):
    """AT-015 — 리뷰 라운드가 없거나 돌려받지 않은(시드 그대로) 멤버는 종결이 거부한다."""
    env = close_env
    review = env.board_dir / "tickets" / "rounds" / env.ticket / _REVIEW_ROUND_FILE
    if shape == "absent":
        review.unlink()
        expected = "code-reviewer 리뷰 라운드가 없습니다"
    else:
        spec = next((env.board_dir / "tickets" / "claimed").glob(f"{env.ticket}*.md"))
        review.write_text(
            round_seed.render_round_seed(
                "code-reviewer", spec.read_text(encoding="utf-8"), today="2026-01-02"),
            encoding="utf-8")
        expected = "돌려받지 않았습니다"
    _git(env.board_dir, "add", "-A")
    _git(env.board_dir, "commit", "-qm", f"review {shape}")
    _git(env.board_dir, "push", "-q")

    rc = env.closer().run()
    out = capsys.readouterr()

    assert rc == 1, out.out + out.err
    assert expected in out.err
    assert env.board_calls == [] and env.delegate_calls == []
    assert env.slot_log()[0] == "code seed"


def _mark_pm_direct(env, *, newline: str | None = None) -> None:
    """멤버 명세 frontmatter 에 `tier: pm-direct` 를 더한다(`newline` 은 쓸 때의 개행 표기)."""
    spec = next((env.board_dir / "tickets" / "claimed").glob(f"{env.ticket}*.md"))
    text = spec.read_text(encoding="utf-8")
    assert "estimate: small\n" in text
    spec.write_text(text.replace("estimate: small\n", "estimate: small\ntier: pm-direct\n", 1),
                    encoding="utf-8", newline=newline)
    _git(env.board_dir, "add", "-A")
    _git(env.board_dir, "commit", "-qm", "pm-direct tier")
    _git(env.board_dir, "push", "-q")


def _drop_rounds(env) -> None:
    """멤버 라운드 디렉터리를 통째로 지운다(위임 라운드를 연 적 없는 형상)."""
    shutil.rmtree(env.board_dir / "tickets" / "rounds" / env.ticket)
    _git(env.board_dir, "add", "-A")
    _git(env.board_dir, "commit", "-qm", "no rounds")
    _git(env.board_dir, "push", "-q")


@requires_git
def test_close_finishes_a_pm_direct_member_without_review_rounds(
    tf, tmp_path, monkeypatch, capsys,
):
    """PM-direct 멤버는 리뷰 라운드가 하나도 없을 때만 리뷰 없음 사유 없이 종결된다.

    역방향 둘: tier 없는 같은 형상은 리뷰 없음으로 막히고, PM-direct 라도 리뷰 라운드가 있으면
    미처분 must-fix 판정을 그대로 받는다.
    """
    pm_direct = _build_close_env(tf, tmp_path / "pm-direct", monkeypatch)
    _mark_pm_direct(pm_direct)
    _drop_rounds(pm_direct)

    rc = pm_direct.closer().run()
    out = capsys.readouterr()

    assert rc == 0, out.out + out.err
    assert "리뷰 라운드가 없습니다" not in out.err
    assert [args[0] for args in pm_direct.board_calls] == ["complete"]

    untiered = _build_close_env(tf, tmp_path / "untiered", monkeypatch)
    _drop_rounds(untiered)

    rc = untiered.closer().run()
    out = capsys.readouterr()

    assert rc == 1, out.out + out.err
    assert "code-reviewer 리뷰 라운드가 없습니다" in out.err
    assert untiered.board_calls == [] and untiered.delegate_calls == []

    reviewed = _build_close_env(tf, tmp_path / "pm-direct-reviewed", monkeypatch)
    _mark_pm_direct(reviewed)
    _write_review_round(reviewed, _review_round_text([_must_fix_finding()]))

    rc = reviewed.closer().run()
    out = capsys.readouterr()

    assert rc == 1, out.out + out.err
    assert "리뷰 확인 입력 미충족" in out.err and reviewed.ticket in out.err
    assert reviewed.board_calls == [] and reviewed.delegate_calls == []


@requires_git
def test_close_finishes_a_crlf_pm_direct_member_without_review_rounds(
    tf, tmp_path, monkeypatch, capsys,
):
    """CRLF 로 체크아웃된 PM-direct 명세도 같은 판정을 받는다(Windows autocrlf 형상).

    리뷰 없음 판정이 명세 원문(newline="")을 그대로 frontmatter 파서에 넘기면 첫 줄이
    `---\\r\\n` 이라 missing frontmatter 로 죽는다.
    """
    env = _build_close_env(tf, tmp_path / "pm-direct-crlf", monkeypatch)
    _mark_pm_direct(env, newline="\r\n")
    _drop_rounds(env)
    spec = next((env.board_dir / "tickets" / "claimed").glob(f"{env.ticket}*.md"))
    assert spec.read_bytes().startswith(b"---\r\n")

    rc = env.closer().run()
    out = capsys.readouterr()

    assert rc == 0, out.out + out.err
    assert "리뷰 라운드가 없습니다" not in out.err
    assert [args[0] for args in env.board_calls] == ["complete"]


@requires_git
def test_close_stops_before_any_side_effect_when_the_confirmation_is_missing(
    close_env, capsys,
):
    """accepted 잔여의 기계 확인 입력(verify 선언)이 없으면 첫 부작용 앞에서 멈춘다."""
    env = close_env
    _write_review_round(env, _review_round_text([_must_fix_finding()]))
    _append_disposition(env, "accepted")

    rc = env.closer().run()
    out = capsys.readouterr()

    assert rc == 1
    assert "리뷰 확인 입력 미충족" in out.err and "verify 선언이 없습니다" in out.err
    assert env.board_calls == []
    assert env.delegate_calls == []
    assert env.slot_log()[0] == "code seed"
    assert env.lease_state() == "leased"


@requires_git
def test_close_preflight_precedes_resolve_in_dry_run_and_actual(
    close_env, capsys, monkeypatch,
):
    """선검사가 confirmation 부재를 허용하는 입력 판정, step 1이 실제 생성 소유자다."""
    env = close_env
    first_key, _first_label, first_precheck = env.tf.ClusterCloser.STEPS[0]
    assert (first_key, first_precheck) == ("resolve", "_pre_resolve")

    dry_events: list[str] = []
    dry = env.closer(dry_run=True)
    assert dry._resolve_members() is None
    monkeypatch.setattr(
        board_ticket, "_review_resolution_input_problem",
        lambda *_a, **_k: dry_events.append("preflight") or None,
    )
    monkeypatch.setattr(
        board_ticket, "_review_completion_problem",
        lambda *_a, **_k: dry_events.append("pending") or "잔여",
    )
    assert dry._pre_resolve() == []
    assert dry._step_resolve() is None
    assert dry_events == ["preflight", "pending"]
    assert env.delegate_calls == []
    assert "rounds resolve --cluster" in capsys.readouterr().out

    actual_events: list[str] = []

    def resolve(args):
        actual_events.append("resolve")
        return 0, "resolved"

    actual = env.closer(run_delegate_fn=resolve)
    assert actual._resolve_members() is None
    monkeypatch.setattr(
        board_ticket, "_review_resolution_input_problem",
        lambda *_a, **_k: actual_events.append("preflight") or None,
    )
    monkeypatch.setattr(
        board_ticket, "_review_completion_problem", lambda *_a, **_k: "잔여",
    )
    assert actual._pre_resolve() == []
    assert actual._step_resolve() is None
    assert actual_events == ["preflight", "resolve"]


@requires_git
def test_close_resolves_the_cluster_once(close_env, monkeypatch):
    """처분은 묶음 표면 `rounds resolve --cluster` 한 번이다(티켓별 반복 경로 없음)."""
    env = close_env
    closer = env.closer()
    monkeypatch.setattr(closer, "_pending_gates", lambda: [env.ticket])

    assert closer._step_resolve() is None
    assert env.delegate_calls == [
        ["rounds", "resolve", "--cluster", _CLUSTER, "--pm-verified"]]
    for removed in ("_gate_ledger", "_resolve_argv_sets", "_delegate_supports_cluster"):
        assert not hasattr(env.tf.ClusterCloser, removed), removed




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
# 관측 실패 = 정지 (완료·clean·무대상 위장 금지)
# ════════════════════════════════════════════════════════════════════════

@requires_git
def test_close_stops_when_the_board_record_channel_cannot_commit(close_env, capsys, monkeypatch):
    """board-git 로컬 커밋이 안 생기면 마지막 단계에서 멈추고 종결 표시도 남기지 않는다.

    `False` 는 board 가 "이 경로가 아직 미커밋" 이라고 확정 보고한 값이다 — 경고로 강등하면
    board 에 없는 내용을 가리키는 포인터 커밋이 남는다.
    """
    env = close_env
    closer = env.closer()
    real_sync = board_sync._board_git_sync_best_effort
    monkeypatch.setattr(board_sync, "_board_git_sync_best_effort", lambda _home, message, paths=None: False)

    rc = closer.run()
    captured = capsys.readouterr()

    assert rc == 1
    assert "board-git 로컬 커밋이 생기지 않았다" in captured.err
    assert "[6/6]" in captured.err
    assert env.ledger()["status"] != _CLOSED_STATUS      # 관측 전 종결 표시 없음
    assert env.home_log()[0] == "home seed"              # 포인터 커밋도 없다

    # 기록 채널이 돌아오면 재실행이 그 단계부터 이어간다(앞 단계 미반복).
    monkeypatch.setattr(board_sync, "_board_git_sync_best_effort", real_sync)
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
    assert env.lease_state() == "leased"                 # 대여 기록은 그대로다


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
    first_log = env.log_text()
    assert first_log.count(f"| [[{env.ticket}]] — ") == 1
    assert first_log.count(f"| {env.ticket} — ") == 0
    assert env.slot_log()[0] == "code seed"            # 커밋 단계까지 가지 못했다

    second = env.closer().run()
    out = capsys.readouterr().out

    assert second == 0, out
    assert env.log_text() == first_log                   # 재개가 기존 entry 본문도 다시 쓰지 않는다
    assert env.log_text().count(f"| [[{env.ticket}]] — ") == 1  # 중복 append 없음
    assert env.log_text().count(f"| {env.ticket} — ") == 0
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

    # 이 대조의 축은 티켓형↔묶음형 동형이지 정체성 해소가 아니다 — 양쪽에 같은 task 를 실어
    # 무인자 해소(정체성 미해소 중단)를 축 밖으로 뺀다.
    rc = tf._main([*argv, "--task", "t"])  # 픽스처 티켓의 소유 task

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
    assert board == (("complete", _TICKET, "--tests-pass", "--cluster-close", _CLUSTER,
                      "--task", "t"),)
    assert rounds == (
        (f"tickets/rounds/{_TICKET}/{_ROUND_FILE}", "## 라운드 표본\n"),
        (f"tickets/rounds/{_TICKET}/{_DEVELOPER_ROUND_FILE}", "## 구현 표본\n"),
        (f"tickets/rounds/{_TICKET}/{_REVIEW_ROUND_FILE}", _review_round_text([])),
    )
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
_UNBORN_SKELETON_PRESCRIPTION = "git add -A"   # 뼈대 보고가 실어야 하는 처방
_CHANGED_BODY = "seed = 1\nwork = 2\n"


def _residual_repo(tf, tmp_path: Path, monkeypatch, *, members: tuple[str, ...]) -> Path:
    """통합 브랜치 + 묶음 브랜치를 든 실 git — 잔여 판정을 실 호출로 본다.

    코드 트리와 PM 홈이 같은 임베디드 형상이라 계획이 하나다(선언 스코프·인구·묶음 장부가
    전부 이 트리에서 해소된다). 선언 세 종류를 미리 심는다 — 멤버 A 선언 · 멤버 B 선언 ·
    아무도 선언하지 않은 경로.
    """
    root = tmp_path / "residual-repo"
    shutil.copytree(TOOLS, root / ".project_manager" / "tools", ignore=_NO_BYTECODE)
    (root / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
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
    # 멤버 A 도 자기 선언을 고친 상태 — 선언 변경 판정(`_declared_change_problem`)과 겹치지 않게 한다.
    (root / _TOUCH_A).write_text(_CHANGED_BODY, encoding="utf-8")

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
def test_declared_change_population_requires_a_member_change(tf, tmp_path, monkeypatch):
    """[[T-0959]] AT-005 — 선언 경로의 변경이 머지 인구(통합 tip 이후 커밋 ∪ 미커밋)에 하나도 없으면 완료 기록 거부다.

    크기 2 묶음에서 멤버 B 의 커밋만 있으면 멤버 A 는 잃은 경로를 이름에 담아 거부되고(B 의 경로는 없다), A 가
    자기 선언을 미커밋으로 고치거나 묶음 브랜치에 커밋하면 통과다. touches 가 빈 멤버는 판정 대상이 아니고,
    diff 조회가 실패하면 통과로 접지 않고 관측 실패로 멈춘다.
    """
    root = _residual_repo(tf, tmp_path, monkeypatch, members=(_MEMBER_A, _MEMBER_B))
    (root / _TOUCH_B).write_text(_CHANGED_BODY, encoding="utf-8")
    _commit_paths(root, _TOUCH_B)

    refused, _err = _residual_verdict(tf, root, _MEMBER_A)
    assert refused is not None and refused.startswith("완료 기록 거부"), refused
    assert _TOUCH_A in refused and _TOUCH_B not in refused

    (root / _TOUCH_A).write_text(_CHANGED_BODY, encoding="utf-8")
    assert _residual_verdict(tf, root, _MEMBER_A)[0] is None          # 미커밋 변경
    _commit_paths(root, _TOUCH_A)
    assert _residual_verdict(tf, root, _MEMBER_A)[0] is None          # 묶음 브랜치 커밋

    _write_ticket(root / ".project_manager" / "board", _MEMBER_A, claimed_rev=_rev(root), touches=())
    _git(root, "reset", "-q", "--hard", _INTEGRATION_BRANCH)          # 변경 0 · 선언 0
    assert _residual_verdict(tf, root, _MEMBER_A)[0] is None

    _write_ticket(root / ".project_manager" / "board", _MEMBER_A, claimed_rev=_rev(root), touches=(_TOUCH_A,))
    finisher = tf.TicketFinisher(
        board_py=root / ".project_manager" / "tools" / "board.py",
        log_file=root / ".project_manager" / "wiki" / "log" / "current.md",
        task_workspace=root,
        run_git_stdout_at_fn=lambda cwd, args: ((1, "") if args[:2] == ["diff", "--name-only"]
                                                else finish_git._default_run_git_stdout_at(cwd, args)),
    )
    with pytest.raises(finish_rules._CloseObservationFailure):
        finisher._declared_change_problem(_MEMBER_A, root)


@requires_git
def test_declared_new_home_file_without_code_tree_passes(tf, tmp_path, monkeypatch):
    """[[T-0959]] F-005 역방향 — 코드 트리가 홈 자신인 형상에서는 홈에 새로 쓴 선언 파일이 그대로 통과한다.

    홈 미추적 새 파일을 코드 몫으로 세는 규칙은 묶음에 별도 코드 트리가 있을 때만이다. 코드 트리가 없으면 그 파일이
    곧 이 종결이 커밋할 변경이다.
    """
    root = _residual_repo(tf, tmp_path, monkeypatch, members=(_MEMBER_A,))
    new_file = "notes/new-home-file.md"
    _write_ticket(root / ".project_manager" / "board", _MEMBER_A, claimed_rev=_rev(root), touches=(new_file,))
    (root / "notes").mkdir()
    (root / new_file).write_text("note\n", encoding="utf-8")

    assert _residual_verdict(tf, root, _MEMBER_A)[0] is None


@requires_git
def test_home_new_path_query_failure_stops(tf, tmp_path, monkeypatch):
    """[[T-0959]] F-005 (I7) — 홈에만 있는 선언의 추적 조회가 실패하면 추적 중으로 접지 않고 관측 실패로 멈춘다."""
    root = _residual_repo(tf, tmp_path, monkeypatch, members=(_MEMBER_A,))
    (root / "notes").mkdir()
    (root / "notes" / "new.md").write_text("note\n", encoding="utf-8")

    def run(cwd, args):
        if args[:1] == ["ls-files"]:
            return 128, "fatal: 합성 관측 실패"
        return finish_git._default_run_git_stdout_at(cwd, args)

    finisher = tf.TicketFinisher(board_py=root / ".project_manager" / "tools" / "board.py",
                                 log_file=root / ".project_manager" / "wiki" / "log" / "current.md",
                                 task_workspace=root, run_git_stdout_at_fn=run)
    with pytest.raises(finish_rules._CloseObservationFailure, match="git ls-files rc=128"):
        finisher._home_new_paths(["notes/new.md"])
    assert tf.TicketFinisher(board_py=root / ".project_manager" / "tools" / "board.py",
                             log_file=root / ".project_manager" / "wiki" / "log" / "current.md",
                             task_workspace=root)._home_new_paths(
        ["notes/new.md", _TOUCH_A, ".project_manager/wiki/log/new.md"]) == ["notes/new.md"]


@requires_git
def test_declared_change_new_git_queries_fail_loud(tf, tmp_path, monkeypatch):
    """[[T-0959]] F-007 (I7) — 선언 변경 판정이 새로 쓰는 조회가 실패하면 통과로 접지 않고 관측 실패로 멈춘다.

    세 갈래다: 지운 선언 경로의 추적 조회(`_tracked_in` 의 status), 미리 `git rm` 한 선언 삭제 조회(`stage_scope` 의
    `diff --cached`), 티켓 판독(`_declared_change_problem`). 어느 하나라도 빈 값으로 접히면 선언 변경이 다른 계획으로
    가거나 판정 대상에서 빠진다.
    """
    root = _residual_repo(tf, tmp_path, monkeypatch, members=(_MEMBER_A,))
    board_py = root / ".project_manager" / "tools" / "board.py"
    log_file = root / ".project_manager" / "wiki" / "log" / "current.md"

    def failing_at(predicate):
        def run(cwd, args):
            if predicate(args):
                return 128, "fatal: 합성 관측 실패"
            return finish_git._default_run_git_stdout_at(cwd, args)
        return run

    status_fails = failing_at(lambda args: args[:1] == ["status"] and "--untracked-files=no" in args)
    finisher = tf.TicketFinisher(board_py=board_py, log_file=log_file, task_workspace=root,
                                 run_git_stdout_at_fn=status_fails)
    (root / _TOUCH_A).unlink()
    with pytest.raises(finish_rules._CloseObservationFailure, match="추적 여부를 관측하지 못했다"):
        finisher._tracked_in(root, _TOUCH_A)

    staged_deletion_fails = failing_at(lambda args: args[:2] == ["diff", "--cached"])
    scope = tf.stage_scope(_MEMBER_A, board_py, log_file, lambda args: staged_deletion_fails(root, args),
                           repo=root, include_engine_outputs=False, touches=("src",))
    assert scope.pathspec == ()
    assert scope.error is not None and "staged 삭제를 관측하지 못했다" in scope.error, scope

    (ticket,) = (root / ".project_manager" / "board" / "tickets" / "claimed").glob(f"{_MEMBER_A}-*.md")
    ticket.write_text(ticket.read_text(encoding="utf-8").replace("touches:\n", "touches: [unclosed\n", 1),
                      encoding="utf-8")
    reader = tf.TicketFinisher(board_py=board_py, log_file=log_file, task_workspace=root)
    with pytest.raises(finish_rules._CloseObservationFailure, match="티켓을 읽지 못해"):
        reader._declared_change_problem(_MEMBER_A, root)


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

    with pytest.raises(finish_rules._CloseObservationFailure) as caught:
        _residual_verdict(tf, root, _MEMBER_A)

    assert _MEMBER_A in str(caught.value)
    assert "통합 브랜치(base_branch)를 선언하지 않았다" in str(caught.value)


# ════════════════════════════════════════════════════════════════════════
# 빈 기준선 — `pm_import --new` 직후(커밋 0 · 뼈대 미추적)의 첫 종결
# ════════════════════════════════════════════════════════════════════════

_UNBORN_BRANCH = "main"              # 신선 홈이 든 브랜치이자 장부의 base_branch
_OTHER_BRANCH = "task/other"         # 커밋 0이지만 HEAD 가 딴 브랜치인 대조 형상


def _unborn_repo(tf, tmp_path: Path, monkeypatch, *,
                 head_branch: str | None = None) -> Path:
    """`pm_import --new` 직후 형상 — 커밋 0 · 뼈대 전부 미추적 · 단일 clone 임베디드 홈.

    실 git 으로만 재현된다(HEAD 부재를 가짜 러너로 만들 수 없다). 장부는 발행이 만드는 크기 1
    형상 그대로다 — `base_branch` 는 이 트리가 든 브랜치, 묶음 브랜치는 미선언
    (`_cluster_publish_base_branch` 실측 형상). `head_branch` 를 주면 커밋 0 은 그대로 두고
    HEAD 만 딴 브랜치로 돌린다(빈 기준선 판정의 둘째 관측 대조).
    """
    root = tmp_path / "unborn-home"
    shutil.copytree(TOOLS, root / ".project_manager" / "tools", ignore=_NO_BYTECODE)
    board_dir = root / ".project_manager" / "board"
    for status in ("open", "claimed", "blocked", "done"):
        (board_dir / "tickets" / status).mkdir(parents=True)
    (root / ".project_manager" / "wiki" / "log").mkdir(parents=True)
    (root / ".project_manager" / "wiki" / "log" / "current.md").write_text(
        "# log\n", encoding="utf-8")
    (root / ".gitignore").write_text(".project_manager/.local/\n__pycache__/\n", encoding="utf-8")
    (root / "src").mkdir()
    for path in (_TOUCH_A, _UNDECLARED):
        (root / path).write_text(_SEED_BODY, encoding="utf-8")
    _git(root, "init", "-q", "-b", _UNBORN_BRANCH)
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    if head_branch is not None:
        _git(root, "symbolic-ref", "HEAD", f"refs/heads/{head_branch}")
    seed_slot(root, repo="home", session="t")
    _write_ticket(board_dir, _MEMBER_A, claimed_rev="null", touches=(_TOUCH_A,))
    _write_cluster_ledger(board_dir, tickets=(_MEMBER_A,),
                          base_branch=_UNBORN_BRANCH, branch=_UNBORN_BRANCH)
    monkeypatch.setenv("PM_SESSION_NAME", "t")
    monkeypatch.setattr(tf, "REPO", root)
    monkeypatch.setattr(tf, "BOARD_PY", root / ".project_manager" / "tools" / "board.py")
    monkeypatch.setattr(
        tf, "LOG_FILE", root / ".project_manager" / "wiki" / "log" / "current.md")
    monkeypatch.setattr(
        tf, "LEASES_FILE",
        root / ".project_manager" / ".local" / "worktree-leases.json")
    monkeypatch.setattr(tf, "_pm_home_misanchor", lambda: None, raising=False)
    return root


@requires_git
def test_residual_gate_reads_an_unborn_baseline_as_empty(tf, tmp_path, monkeypatch):
    """커밋 0 트리에서 미추적 뼈대는 이 티켓의 잔여가 아니다 — 차단 없이 값으로 보고한다.

    뼈대 전부를 잔여로 세면 어떤 첫 티켓도 닫히지 않는다. 통과가 조용하지도 않다 — 건수와
    처방(`git add`)이 stderr 한 줄로 남는다.
    """
    root = _unborn_repo(tf, tmp_path, monkeypatch)

    block, stderr = _residual_verdict(tf, root, _MEMBER_A)

    assert block is None
    assert "기준선 없음" in stderr and _UNBORN_SKELETON_PRESCRIPTION in stderr


@requires_git
def test_unborn_baseline_requires_head_on_the_declared_branch(tf, tmp_path, monkeypatch):
    """빈 기준선은 두 관측이 **함께** 참일 때뿐이다 — 하나라도 어긋나면 종전대로 멈춘다.

    (1) 커밋 0 이지만 HEAD 가 장부의 base_branch 가 아닌 다른 브랜치를 가리키는 트리와
    (2) 커밋이 있는데 그 base_branch 가 이 트리에 없는 트리 — 둘 다 판정 기준 미해소다.
    """
    detached_head = _unborn_repo(tf, tmp_path / "other-head", monkeypatch,
                                 head_branch=_OTHER_BRANCH)

    with pytest.raises(finish_rules._CloseObservationFailure) as caught:
        _residual_verdict(tf, detached_head, _MEMBER_A)
    assert _UNBORN_BRANCH in str(caught.value) and "이 코드 트리에 없다" in str(caught.value)

    committed = _residual_repo(tf, tmp_path / "committed", monkeypatch,
                               members=(_MEMBER_A,))
    _write_cluster_ledger(committed / ".project_manager" / "board",
                          tickets=(_MEMBER_A,), base_branch="task/absent")

    with pytest.raises(finish_rules._CloseObservationFailure) as caught:
        _residual_verdict(tf, committed, _MEMBER_A)
    assert "task/absent" in str(caught.value) and "이 코드 트리에 없다" in str(caught.value)


@requires_git
def test_fresh_single_clone_home_requires_explicit_binding(
        tf, tmp_path, monkeypatch, capsys):
    """커밋 0의 미결속 장부는 첫 호출에서 부작용 없이 명시 결속을 요구한다."""
    root = _unborn_repo(tf, tmp_path, monkeypatch)
    board_dir = root / ".project_manager" / "board"
    board_py = root / ".project_manager" / "tools" / "board.py"
    board_calls: list[list[str]] = []

    def _run_board(args: list[str]) -> tuple[int, str]:
        board_calls.append(list(args))
        if args and args[0] == "complete":
            source = next((board_dir / "tickets" / "claimed").glob("T-*.md"))
            target = board_dir / "tickets" / "done" / source.name
            source.replace(target)
            target.write_text(
                target.read_text(encoding="utf-8").replace(
                    "status: claimed", "status: done"),
                encoding="utf-8")
        return 0, "ok"

    finisher = tf.TicketFinisher(
        board_py=board_py,
        log_file=root / ".project_manager" / "wiki" / "log" / "current.md",
        task_workspace=root,
        run_pytest_fn=lambda: (0, "1 passed in 0.01s"),
        run_board_fn=_run_board,
        dod_block_fn=lambda tid: None,
        self_axis_block_fn=lambda tid: None,
        affected_domain_fn=lambda tid: None,
    )
    closer = tf.ClusterCloser(
        _CLUSTER, finisher=finisher, board_py=board_py,
        run_delegate_fn=lambda args: (0, ""),
    )
    _write_cluster_ledger(board_dir, tickets=(_MEMBER_A,), base_branch=_UNBORN_BRANCH, branch=None)
    leases = (root / ".project_manager/.local/worktree-leases.json").read_bytes()

    rc = closer.run()
    captured = capsys.readouterr()

    assert rc == 1, captured.out + captured.err
    assert "cluster new" in captured.err
    assert board_calls == []
    assert not list((board_dir / "tickets" / "done").glob(f"{_MEMBER_A}*.md"))
    assert list((board_dir / "tickets" / "claimed").glob(f"{_MEMBER_A}*.md"))
    assert _git(root, "rev-parse", "--verify", "--quiet", "HEAD").returncode != 0
    assert (root / ".project_manager/.local/worktree-leases.json").read_bytes() == leases
    assert _lease_rows(root)[0]["session"] == "t"


def test_status_query_failure_stops_residual_gate_and_report(tf, tmp_path, capsys):
    """[[T-0959]] F-002 — 상태 조회가 실패하면 잔여 판정과 stage 뒤 보고가 모두 관측 실패로 멈춘다.

    공유 seam 은 조회 실패를 그대로 올린다(`strict`). 소비자가 그 실패를 빈 목록으로 접으면 잔여 판정은 잔여
    없음으로 통과하고 stage 뒤 보고는 "스코프 밖 잔여 변경 없음" 을 출력한다 — 관측하지 못한 것을 clean 으로
    말하지 않는다.
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
    with pytest.raises(finish_rules._CloseObservationFailure, match="작업 트리 상태를 관측하지 못했다"):
        finisher._residual_split(tmp_path, (_TOUCH_A,), (), ticket_id=_MEMBER_A)
    with pytest.raises(finish_rules._CloseObservationFailure, match="작업 트리 상태를 관측하지 못했다"):
        finisher._report_dirty_after_stage((_TOUCH_A,), cwd=tmp_path)

    assert "스코프 밖 잔여 변경 없음" not in capsys.readouterr().out


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
    assert env.lease_state() == "leased"                 # 대여 기록은 그대로다



@requires_git
def test_t0930_unbound_ledger(close_env, capsys):
    env = close_env
    assert _git(env.code, "branch", "-m", "main").returncode == 0
    _write_cluster_ledger(env.board_dir, tickets=(env.ticket,), base_branch="main", branch=None)
    ledger = env.board_dir / "tickets/clusters" / f"{_CLUSTER}.md"
    before = (ledger.read_bytes(), env.lease_bytes(), env.home_log(), _rev(env.code), _rev(env.slot))
    assert env.closer().run() == 1
    err = capsys.readouterr().err
    assert "cluster new" in err
    assert env.board_calls == [] and env.delegate_calls == []
    assert (ledger.read_bytes(), env.lease_bytes(), env.home_log(), _rev(env.code), _rev(env.slot)) == before
    assert not list((env.board_dir / "tickets/done").glob("T-*.md"))
