"""추가 리뷰어 diff 서킷브레이커의 **측정 폭** 회귀 (claim 앵커).

옛 폭은 작업트리(스테이징+언스테이징+untracked) → 비면 직전 커밋 한 칸이었다. dev 브랜치를
`--no-ff` merge 로 흡수하고 전파 커밋이 뒤따르는 형상에서는 finish/리뷰 시점 트리가 clean 이고
마지막 커밋이 티켓 경로를 안 건드려, 상한을 넘긴 wave 가 0 줄로 통과했다(실측). 이 파일은 그
형상을 실 git 저장소로 재현하고, claim 시점 rev 를 앵커로 쓴 폭이 누적을 잡는지 본다.

여기서 지키는 성질:
  - 앵커가 있으면 폭은 `claim 시점 rev → 현재 작업트리` 한 단계다(merge 흡수분 포함).
  - untracked 신규 파일은 앵커 폭에서도 여전히 세어진다(stage 전 대형 신규 파일 우회 폐쇄).
  - `--base` 명시는 사용자가 고른 폭이라 앵커가 덮어쓰지 않는다.
  - 앵커를 못 쓰는 티켓(구 티켓·해소 불가 rev)은 옛 폭으로 재되 **경고 1줄**이 남는다.
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
    reason="git 바이너리 부재 — 실 git 통합 케이스 skip.",
)

# claim 시점 rev 이후 `src/` 에 쌓인 실제 변경량(dev 커밋 2건 합) — 상한 small(300) 초과분.
_WAVE_SRC_LINES = 320


def _load(name: str = "external_review"):
    spec = importlib.util.spec_from_file_location(f"diff_cap_{name}", TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def external():
    return _load("external_review")


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True, text=True, encoding="utf-8")


def _write(path: Path, lines: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"line{index}\n" for index in range(lines)), encoding="utf-8")


def _rev(root: Path, ref: str = "HEAD") -> str:
    return _git(root, "rev-parse", ref).stdout.strip()


def _wave_repo(tmp_path: Path) -> tuple[Path, str]:
    """claim → dev 커밋 2 → `merge --no-ff` → 전파 커밋 2 형상 저장소와 claim 시점 rev.

    finish/리뷰 시점 트리는 clean 이고 마지막 커밋(전파)은 `src/` 를 안 건드린다 — 옛 폭이
    0 으로 접히는 바로 그 배치다."""
    root = tmp_path / "code"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@e")
    _git(root, "config", "user.name", "t")
    _write(root / "src" / "app.py", 5)
    _write(root / "docs" / "notes.md", 3)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "seed")
    claim_rev = _rev(root)

    _git(root, "checkout", "-q", "-b", "dev/T-7301")
    _write(root / "src" / "app.py", 5 + 200)
    _git(root, "commit", "-qam", "dev 1")
    _write(root / "src" / "app.py", 5 + _WAVE_SRC_LINES)
    _write(root / "docs" / "notes.md", 3 + 50)
    _git(root, "commit", "-qam", "dev 2")

    _git(root, "checkout", "-q", "main")
    _git(root, "merge", "-q", "--no-ff", "-m", "merge dev/T-7301", "dev/T-7301")
    for index in (1, 2):
        _write(root / "docs" / f"propagation{index}.md", 4)
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", f"propagation {index}")
    return root, claim_rev


def _pm_home(tmp_path: Path, ticket_id: str, frontmatter: str) -> Path:
    """ticket 한 건만 가진 tmp PM 홈 — 상한·앵커 입력의 출처."""
    home = tmp_path / "pm"
    tickets = home / ".project_manager" / "board" / "tickets" / "claimed"
    tickets.mkdir(parents=True, exist_ok=True)
    (tickets / f"{ticket_id}-wave.md").write_text(
        f"---\nid: {ticket_id}\n{frontmatter}touches:\n- src/\n---\n\n# 본문\n",
        encoding="utf-8")
    return home


def _refusal_args(**overrides) -> argparse.Namespace:
    defaults = {"ticket": "T-7301", "gate": None, "base": "HEAD"}
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


# ══ 폭 단계 표 ══════════════════════════════════════════════════════════════


def test_claim_anchor_picks_one_worktree_inclusive_stage(external):
    """앵커가 있으면 단계는 하나다 — 그 rev 와 **현재 작업트리**의 차이(untracked 포함)."""
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
def test_anchor_resolves_a_commit_that_exists_in_this_tree(external, tmp_path):
    """이 트리에서 해소되는 rev 만 앵커다 — 사유 없이 통과한다."""
    root, claim_rev = _wave_repo(tmp_path)
    assert external.claim_anchor(root, claim_rev) == (claim_rev, None)


@requires_git
def test_unknown_rev_falls_back_with_a_loud_reason(external, tmp_path):
    """다른 저장소의 rev·다시 쓰인 히스토리는 **사유와 함께** 옛 폭으로 접힌다.

    그대로 넘기면 `git diff <rev>` 가 rc≠0 이고 측정은 실패한 실행을 '변경 없음'으로 접는다 —
    조용한 0 줄(false-green)이 바로 이 티켓이 닫는 결함이다."""
    root, _claim_rev = _wave_repo(tmp_path)
    anchor, note = external.claim_anchor(root, "b" * 40)
    assert anchor is None and "해소하지 못함" in note


@pytest.mark.parametrize("value", ["", None, "--output=/tmp/x", "HEAD~1", "zz" * 20])
def test_absent_or_malformed_anchor_never_reaches_git(external, tmp_path, value):
    """빈 값·sha 가 아닌 값은 git argv 로 넘기지 않는다(옵션처럼 보이는 값 차단)."""
    anchor, note = external.claim_anchor(tmp_path, value)
    assert anchor is None and note


def test_absent_anchor_note_names_the_underestimation(external):
    """구 티켓 안내는 '폭 과소 측정 가능'을 스스로 말한다 — 조용한 통과가 없다."""
    assert "과소 측정" in external.CLAIMED_REV_ABSENT_NOTE


# ══ 측정 총량 ═══════════════════════════════════════════════════════════════


@requires_git
def test_merge_wave_is_invisible_to_the_old_width(external, tmp_path):
    """옛 폭은 merge 기반 wave 를 0 으로 잰다 — 이 파일이 닫는 결함의 재현."""
    root, _claim_rev = _wave_repo(tmp_path)
    assert external.diff_line_total(root, "HEAD", ["src/"]) == 0


@requires_git
def test_claim_anchor_measures_the_whole_merge_wave(external, tmp_path):
    """앵커 폭은 dev 커밋 2건이 merge 로 들어온 누적을 그대로 잰다."""
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


# ══ frontmatter 입력 ════════════════════════════════════════════════════════


def test_claimed_rev_is_read_from_ticket_frontmatter(external, tmp_path):
    """앵커 입력은 board frontmatter 의 `claimed_rev` 다 (부재·비-문자열은 None)."""
    home = _pm_home(tmp_path, "T-7301", f"claimed_rev: {'a' * 40}\n")
    _pm_home(tmp_path, "T-7302", "")
    assert external.parse_ticket_claimed_rev("T-7301", pm_home=home) == "a" * 40
    assert external.parse_ticket_claimed_rev("T-7302", pm_home=home) is None
    assert external.parse_ticket_claimed_rev("T-9999", pm_home=home) is None


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
def test_gate_without_an_anchor_warns_before_it_passes(external, tmp_path, capsys):
    """앵커 없는 구 티켓은 옛 폭으로 재되 통과가 조용하지 않다 — 경고 1줄."""
    root, _claim_rev = _wave_repo(tmp_path)
    home = _pm_home(tmp_path, "T-7301", "estimate: small\n")

    block = external._diff_cap_refusal(
        _refusal_args(), {}, root=root, paths=["src/"], pm_home=home)

    assert block is None
    assert "claimed_rev 없음" in capsys.readouterr().err


@requires_git
def test_free_form_gate_stays_silent_and_off(external, tmp_path, capsys):
    """티켓이 아닌 자유 문자열 게이트는 상한이 없어 가드도 경고도 없다(무영향)."""
    root, _claim_rev = _wave_repo(tmp_path)

    block = external._diff_cap_refusal(
        _refusal_args(ticket=None, gate="wave4-b1"), {}, root=root,
        paths=["src/"], pm_home=tmp_path / "pm")

    assert block is None
    assert capsys.readouterr().err == ""
