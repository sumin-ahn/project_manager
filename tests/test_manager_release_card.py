"""릴리즈 카드의 관리자 자산 단계와 그것이 가리키는 워크플로 (T-0914 AT-003).

릴리즈 절차 §3 은 다섯 단계다: main push → tag → `gh release create` → **관리자 실행 파일 3종
도착 확인** → 릴리즈 완결 확인. 새 4번은 손 업로드 절차가 아니라 **도착 확인**이다 — 자산은
`release-manager.yml` 이 올린다. 그 카드는 루트 하나를 고치고 `pm_update.py --all-targets` 로
출하 사본에 내보내므로, 여기서 실 파일 전수를 대조한다(손으로 고친 사본은 여기서 red 다).

`--smoke` 세 OS 결과는 livegate 기록에 넣지 않는다(그 스키마는 `pytest -m release` 축이다).
CI job 성공·실패가 기계 기록이고 카드는 그 결과를 본다.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
ROOT_CARD = REPO / ".claude" / "skills" / "pm-release" / "SKILL.md"
# 출하되는 같은 카드 전부 — 세 템플릿의 스킬 사본 + opencode 슬래시 팔레트 사본.
SHIPPED_CARDS = (
    REPO / "templates/claude_code/.claude/skills/pm-release/SKILL.md",
    REPO / "templates/codex/.agents/skills/pm-release/SKILL.md",
    REPO / "templates/opencode/.claude/skills/pm-release/SKILL.md",
    REPO / "templates/opencode/.opencode/command/pm-release.md",
)
WORKFLOW = REPO / ".github" / "workflows" / "release-manager.yml"
REGRESSION_WORKFLOW = REPO / ".github" / "workflows" / "regression.yml"

SECTION_HEADING = "### 3. CHANGELOG → main push → tag → GitHub Release"
NEXT_SECTION_HEADING = "### 4. adopter#0 흡수"
MANAGER_STEP_NUMBER = 4
RELEASE_RUNNERS = ("ubuntu-latest", "windows-latest", "macos-latest")
ASSET_NAMES = ("pm-manager-linux-x86_64", "pm-manager-windows-x86_64.exe",
               "pm-manager-macos-arm64")
STEP_HEAD = re.compile(r"^(\d+)\. \*\*(.+?)\*\*")


def _release_section(card: Path) -> str:
    text = card.read_text(encoding="utf-8")
    start = text.index(SECTION_HEADING)
    return text[start:text.index(NEXT_SECTION_HEADING, start)]


def _steps(section: str) -> dict[int, str]:
    """§3 의 번호 항목 — {번호: 그 항목의 줄들}."""
    steps: dict[int, str] = {}
    current: int | None = None
    for line in section.splitlines():
        matched = STEP_HEAD.match(line)
        if matched:
            current = int(matched.group(1))
            steps[current] = ""
        if current is not None:
            steps[current] += line + "\n"
    return steps


def _fenced_commands(block: str) -> list[str]:
    """그 항목의 ```bash 블록 안 명령 줄(주석·빈 줄 제외)."""
    commands: list[str] = []
    inside = False
    for line in block.splitlines():
        if line.strip().startswith("```"):
            inside = not inside
            continue
        stripped = line.strip()
        if inside and stripped and not stripped.startswith("#"):
            commands.append(stripped)
    return commands


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _triggers(document: dict) -> dict:
    """`on:` — YAML 1.1 에서 참(True)으로 읽히는 그 키."""
    return document.get("on", document.get(True))


# ── ① 카드 §3 의 순서와 새 단계 ──────────────────────────────────────────────


def test_the_release_section_has_five_steps_and_the_manager_step_sits_before_the_close():
    steps = _steps(_release_section(ROOT_CARD))

    assert sorted(steps) == [1, 2, 3, 4, 5]
    assert "gh release create" in steps[3]
    assert "관리자 실행 파일 3종" in steps[MANAGER_STEP_NUMBER]
    assert "릴리즈 완결 확인" in steps[5]


def test_the_manager_step_only_reads_and_never_uploads_by_hand():
    step = _steps(_release_section(ROOT_CARD))[MANAGER_STEP_NUMBER]

    commands = _fenced_commands(step)
    assert commands, "확인 명령이 없다"
    assert all(command.startswith(("gh run list", "gh run view", "gh release view"))
               for command in commands), commands
    assert "워크플로가 올린다" in step


def test_the_manager_step_names_the_workflow_and_the_three_assets():
    step = _steps(_release_section(ROOT_CARD))[MANAGER_STEP_NUMBER]

    assert "release-manager.yml" in step
    assert "release: types: [published]" in step
    assert "--clobber" in step
    assert "pm-manager-{linux,windows,macos}-*" in step


def test_the_manager_step_says_what_to_do_when_a_job_is_red():
    step = _steps(_release_section(ROOT_CARD))[MANAGER_STEP_NUMBER]

    assert "gh run rerun" in step


def test_the_smoke_results_are_not_recorded_in_the_livegate():
    text = ROOT_CARD.read_text(encoding="utf-8")

    assert "livegate 기록에 넣지 않는다" in text
    assert re.search(r"livegate[^\n]*record[^\n]*smoke", text) is None


# ── ② 출하 사본 전수 대조 (pm_update --all-targets) ──────────────────────────


@pytest.mark.parametrize("card", SHIPPED_CARDS, ids=lambda path: path.relative_to(REPO).as_posix())
def test_every_shipped_card_carries_the_same_release_section(card):
    assert card.is_file(), f"출하 카드 부재: {card}"

    assert _release_section(card) == _release_section(ROOT_CARD)


# ── ③ 카드가 가리키는 워크플로 ──────────────────────────────────────────────


def test_the_workflow_wakes_on_a_published_release_only():
    """태그 push 로 걸면 Release 객체가 아직 없어 업로드가 깨진다."""
    triggers = _triggers(_workflow())

    assert triggers == {"release": {"types": ["published"]}}


def test_the_matrix_is_the_three_release_runners():
    matrix = _workflow()["jobs"]["build"]["strategy"]["matrix"]["include"]

    assert [entry["os"] for entry in matrix] == list(RELEASE_RUNNERS)
    assert len(matrix) == 3


def test_only_linux_runs_the_smoke_under_a_virtual_display():
    matrix = _workflow()["jobs"]["build"]["strategy"]["matrix"]["include"]

    launchers = {entry["os"]: entry["launcher"] for entry in matrix}
    assert launchers["ubuntu-latest"] == "xvfb-run -a"
    assert launchers["windows-latest"] == "" and launchers["macos-latest"] == ""


def test_the_workflow_builds_smokes_and_uploads_in_that_order():
    steps = _workflow()["jobs"]["build"]["steps"]

    named = [step["name"] for step in steps if "name" in step]
    assert named.index("build") < named.index("smoke") < named.index("upload asset")
    upload = next(step for step in steps if step.get("name") == "upload asset")
    assert "gh release upload" in upload["run"] and "--clobber" in upload["run"]
    assert "github.event.release.tag_name" in upload["run"]
    smoke = next(step for step in steps if step.get("name") == "smoke")
    assert "--smoke" in smoke["run"] and "--registry" in smoke["run"]


def test_the_workflow_installs_the_build_requirements_file():
    steps = _workflow()["jobs"]["build"]["steps"]

    runs = "\n".join(step.get("run", "") for step in steps)
    assert "manager/requirements-build.txt" in runs
    assert "manager/build.py" in runs


def test_the_regression_workflow_is_not_the_place_for_release_assets():
    """제품 회귀 CI 는 이 티켓이 건드리지 않는다."""
    regression = yaml.safe_load(REGRESSION_WORKFLOW.read_text(encoding="utf-8"))

    assert set(_triggers(regression)) == {"push", "pull_request"}
    assert "pyinstaller" not in REGRESSION_WORKFLOW.read_text(encoding="utf-8").lower()


def test_the_asset_names_the_card_promises_are_the_ones_the_build_makes():
    from manager import build

    made = {build.executable_filename(system=system, machine=machine)
            for system, machine in (("Linux", "x86_64"), ("Windows", "AMD64"),
                                    ("Darwin", "arm64"))}
    assert made == set(ASSET_NAMES)
