"""추가 리뷰어 diff 서킷브레이커의 **측정 폭** 회귀 (통합 브랜치 앵커).

옛 폭은 작업트리(스테이징+언스테이징+untracked) → 비면 직전 커밋 한 칸이었다. dev 커밋이
쌓이고 마지막 커밋이 티켓 경로를 안 건드리면 상한을 넘긴 작업이 0 줄로 통과했다(실측). 이
파일은 그 형상을 실 git 저장소로 재현하고, 묶음 장부가 선언한 통합 브랜치와의 merge-base 를
기준점으로 쓴 폭이 누적을 잡는지 본다.

여기서 지키는 성질:
  - 앵커가 있으면 폭은 `merge-base(통합 브랜치, HEAD) → 현재 작업트리` 한 단계다.
  - untracked 신규 파일은 앵커 폭에서도 여전히 세어진다(stage 전 대형 신규 파일 우회 폐쇄).
  - `--base` 명시는 사용자가 고른 폭이라 앵커가 덮어쓰지 않는다.
  - 기준점을 해소하지 못한 티켓(장부 미선언·이 트리에 없는 브랜치)은 **거부**다 — 다른 기준
    으로 접지 않는다.
"""
from __future__ import annotations

import argparse
import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import write_cluster_ledger

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

_INTEGRATION = "task/main"      # 묶음 장부가 선언하는 통합 브랜치(폭의 기준)
_DEV_BRANCH = "dev/T-7301"      # 이 작업이 사는 브랜치

requires_git = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git 바이너리 부재 — 실 git 통합 케이스 skip.",
)

# claim 시점 rev 이후 `src/` 에 쌓인 실제 변경량(dev 커밋 2건 합) — 상한 small(300) 초과분.
_WAVE_SRC_LINES = 320


def _load(name: str = "additional_reviewer"):
    spec = importlib.util.spec_from_file_location(f"diff_cap_{name}", TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def external():
    return _load("additional_reviewer")


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True, text=True, encoding="utf-8")


def _write(path: Path, lines: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"line{index}\n" for index in range(lines)), encoding="utf-8")


def _rev(root: Path, ref: str = "HEAD") -> str:
    return _git(root, "rev-parse", ref).stdout.strip()


def _wave_repo(tmp_path: Path) -> tuple[Path, str]:
    """dev 커밋 2 + 통합 브랜치 전진(전파 커밋 2)을 흡수한 형상 저장소와 claim 시점 rev.

    리뷰 시점 트리는 clean 이고 마지막 커밋(통합 흡수)은 `src/` 를 안 건드린다 — 작업트리+직전
    커밋 한 칸으로 재던 옛 폭이 0 으로 접히는 바로 그 배치다. HEAD 는 dev 브랜치이고 통합
    브랜치는 이 작업을 아직 받지 않았다(그래서 merge-base 는 갈라진 지점이다)."""
    root = tmp_path / "code"
    root.mkdir()
    _git(root, "init", "-q", "-b", _INTEGRATION)
    _git(root, "config", "user.email", "t@e")
    _git(root, "config", "user.name", "t")
    _write(root / "src" / "app.py", 5)
    _write(root / "docs" / "notes.md", 3)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "seed")
    claim_rev = _rev(root)

    _git(root, "checkout", "-q", "-b", _DEV_BRANCH)
    _write(root / "src" / "app.py", 5 + 200)
    _git(root, "commit", "-qam", "dev 1")
    _write(root / "src" / "app.py", 5 + _WAVE_SRC_LINES)
    _write(root / "docs" / "notes.md", 3 + 50)
    _git(root, "commit", "-qam", "dev 2")

    _git(root, "checkout", "-q", _INTEGRATION)
    for index in (1, 2):
        _write(root / "docs" / f"propagation{index}.md", 4)
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", f"propagation {index}")
    _git(root, "checkout", "-q", _DEV_BRANCH)
    _git(root, "merge", "-q", "--no-ff", "-m", "통합 흡수", _INTEGRATION)
    return root, claim_rev


def _pm_home(tmp_path: Path, ticket_id: str, frontmatter: str, *,
             base_branch: str | None = _INTEGRATION) -> Path:
    """ticket 한 건과 그 묶음 장부를 가진 tmp PM 홈 — 상한·앵커 입력의 출처.

    장부 판독은 이 홈의 board 사본이 보는 트리를 봐야 하므로 엔진 사본도 함께 둔다(형제 사본을
    쓰면 이 도구가 사는 트리의 board 를 읽어 다른 장부를 본다). `base_branch=None` 은 선언이
    없는 장부(판정 기준 부재) 형상이다."""
    home = tmp_path / "pm"
    tools = home / ".project_manager" / "tools"
    if not tools.exists():
        shutil.copytree(TOOLS, tools)
    tickets = home / ".project_manager" / "board" / "tickets" / "claimed"
    tickets.mkdir(parents=True, exist_ok=True)
    if base_branch is not None:
        write_cluster_ledger(
            home / ".project_manager" / "board", ticket_id,
            base_branch=base_branch)
    (tickets / f"{ticket_id}-wave.md").write_text(
        f"---\nid: {ticket_id}\n{frontmatter}touches:\n- src/\n---\n\n# 본문\n",
        encoding="utf-8")
    return home


def _refusal_args(**overrides) -> argparse.Namespace:
    defaults = {"ticket": "T-7301", "gate": None, "base": "HEAD"}
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


# ══ 폭 단계 표 ══════════════════════════════════════════════════════════════


def test_the_anchor_picks_one_worktree_inclusive_stage(external):
    """앵커가 있으면 단계는 하나다 — 그 기준점과 **현재 작업트리**의 차이(untracked 포함)."""
    assert external._measure_stages("HEAD", "a" * 40) == (("a" * 40, True),)


def test_absent_anchor_keeps_the_old_stage_table(external):
    """앵커가 없으면 옛 폭 그대로다 — 작업트리 → 비면 직전 커밋 한 칸."""
    assert external._measure_stages("HEAD", None) == (
        ("HEAD", True), ("HEAD~1..HEAD", False))


def test_explicit_base_owns_the_width_over_the_anchor(external):
    """`--base` 명시는 사용자가 고른 폭이라 앵커가 덮어쓰지 않는다."""
    assert external._measure_stages("main", "a" * 40) == (("main", False),)


# ══ 앵커 해소 (조용한 0 줄 금지) ═════════════════════════════════════════════


@requires_git
def test_the_anchor_is_the_merge_base_with_the_integration_branch(external, tmp_path):
    """앵커는 통합 브랜치와의 merge-base 다 — 사유 없이 통과한다."""
    root, _claim_rev = _wave_repo(tmp_path)
    expected = _git(root, "merge-base", _INTEGRATION, "HEAD").stdout.strip()

    assert external.integration_anchor(root, _INTEGRATION) == (expected, None)


@requires_git
def test_a_branch_absent_from_this_tree_yields_a_loud_reason(external, tmp_path):
    """이 트리에 없는 브랜치는 **사유**를 낸다 — 다른 기준점으로 접지 않는다.

    값을 지어내 넘기면 `git diff <rev>` 가 rc≠0 이고 측정은 실패한 실행을 '변경 없음'으로
    접는다 — 조용한 0 줄(false-green)이 바로 이 축이 닫는 결함이다."""
    root, _claim_rev = _wave_repo(tmp_path)

    anchor, note = external.integration_anchor(root, "task/absent")

    assert anchor is None and "해소하지 못했다" in note and "task/absent" in note


@pytest.mark.parametrize("value", ["", "--output=/tmp/x", "zz" * 20])
def test_an_unresolvable_reference_never_becomes_an_anchor(external, tmp_path, value):
    """빈 값·옵션 모양·존재하지 않는 값은 앵커가 되지 못한다(사유와 함께 멈춘다)."""
    anchor, note = external.integration_anchor(tmp_path, value)
    assert anchor is None and note


# ══ 측정 총량 ═══════════════════════════════════════════════════════════════


@requires_git
def test_merge_wave_is_invisible_to_the_old_width(external, tmp_path):
    """옛 폭은 merge 기반 wave 를 0 으로 잰다 — 이 파일이 닫는 결함의 재현."""
    root, _claim_rev = _wave_repo(tmp_path)
    assert external.diff_line_total(root, "HEAD", ["src/"]) == 0


@requires_git
def test_the_anchor_width_measures_the_whole_commit_run(external, tmp_path):
    """앵커 폭은 dev 커밋 2건의 누적을 그대로 잰다."""
    root, claim_rev = _wave_repo(tmp_path)
    assert external.diff_line_total(
        root, "HEAD", ["src/"], claimed_rev=claim_rev) == _WAVE_SRC_LINES


@requires_git
def test_uncommitted_work_rides_the_anchor_width(external, tmp_path):
    """미커밋 변경도 같은 폭에 들어온다 — 커밋 누적 + 작업트리 한 폭이다."""
    root, claim_rev = _wave_repo(tmp_path)
    _write(root / "src" / "app.py", 5 + _WAVE_SRC_LINES + 7)

    assert external.diff_line_total(
        root, "HEAD", ["src/"], claimed_rev=claim_rev) == _WAVE_SRC_LINES + 7


@requires_git
def test_untracked_new_file_is_still_measured_under_the_anchor(external, tmp_path):
    """앵커 폭에서도 untracked 신규 파일을 센다 — stage 전 대형 신규 파일 우회가 안 열린다."""
    root, claim_rev = _wave_repo(tmp_path)
    _write(root / "src" / "new_module.py", 40)

    assert external.diff_line_total(
        root, "HEAD", ["src/"], claimed_rev=claim_rev) == _WAVE_SRC_LINES + 40


@requires_git
def test_machine_mirror_exclusion_survives_the_new_width(external, tmp_path):
    """제외 규칙은 폭이 바뀌어도 그대로다 — 기계 mirror 는 앵커 폭에서도 안 센다."""
    root, claim_rev = _wave_repo(tmp_path)
    mirror = root / "templates" / "codex" / ".project_manager" / "tools" / "board.py"
    _write(mirror, 500)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "mirror 전파")

    assert external.diff_line_total(
        root, "HEAD", ["src/", "templates/"], claimed_rev=claim_rev) == _WAVE_SRC_LINES


# ══ 게이트 판정 (진입 검사) ══════════════════════════════════════════════════


@requires_git
def test_gate_blocks_the_merge_wave_it_used_to_pass(external, tmp_path):
    """상한을 넘긴 merge wave 가 리뷰어 호출 전에 잡힌다 (옛 폭에선 통과하던 형상)."""
    root, claim_rev = _wave_repo(tmp_path)
    home = _pm_home(tmp_path, "T-7301",
                    f"estimate: small\nclaimed_rev: {claim_rev}\n")

    block = external._diff_cap_refusal(
        _refusal_args(), {}, root=root, paths=["src/"], pm_home=home)

    assert block is not None
    assert f"{_WAVE_SRC_LINES}줄" in block and "300줄" in block


@requires_git
def test_gate_honours_an_explicit_base_over_the_anchor(external, tmp_path, capsys):
    """`--base` 명시면 그 폭으로 잰다 — 앵커도, 앵커 경고도 끼어들지 않는다."""
    root, claim_rev = _wave_repo(tmp_path)
    home = _pm_home(tmp_path, "T-7301",
                    f"estimate: small\nclaimed_rev: {claim_rev}\n")

    block = external._diff_cap_refusal(
        _refusal_args(base="HEAD~1..HEAD"), {}, root=root, paths=["src/"],
        pm_home=home)

    assert block is None
    assert "측정 폭" not in capsys.readouterr().err


@requires_git
def test_gate_refuses_when_the_ledger_declares_no_integration_branch(
    external, tmp_path, capsys,
):
    """장부 선언이 없으면 리뷰 호출을 거부한다 — 다른 기준으로 재지 않는다."""
    root, _claim_rev = _wave_repo(tmp_path)
    home = _pm_home(tmp_path, "T-7301", "estimate: small\n", base_branch=None)

    block = external._diff_cap_refusal(
        _refusal_args(), {}, root=root, paths=["src/"], pm_home=home)

    assert block is not None
    assert "기준점을 해소하지 못했습니다" in block
    assert "통합 브랜치(base_branch) 선언이 없다" in block
    assert capsys.readouterr().err == ""


@requires_git
def test_gate_refuses_when_the_declared_branch_is_absent_from_this_tree(
    external, tmp_path,
):
    """선언은 있는데 이 트리에 없는 브랜치도 같은 거부다(조용한 0 줄 금지)."""
    root, _claim_rev = _wave_repo(tmp_path)
    home = _pm_home(tmp_path, "T-7301", "estimate: small\n",
                    base_branch="task/absent")

    block = external._diff_cap_refusal(
        _refusal_args(), {}, root=root, paths=["src/"], pm_home=home)

    assert block is not None and "task/absent" in block


@requires_git
def test_free_form_gate_stays_silent_and_off(external, tmp_path, capsys):
    """티켓이 아닌 자유 문자열 게이트는 상한이 없어 가드도 경고도 없다(무영향)."""
    root, _claim_rev = _wave_repo(tmp_path)

    block = external._diff_cap_refusal(
        _refusal_args(ticket=None, gate="wave4-b1"), {}, root=root,
        paths=["src/"], pm_home=tmp_path / "pm")

    assert block is None
    assert capsys.readouterr().err == ""
