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
CHANGELOG = REPO / "CHANGELOG.md"
MANAGER_ENTRY_MARK = "**프로젝트 관리자 앱 `pm-manager`**"

SECTION_HEADING = "### 3. CHANGELOG → main push → tag → GitHub Release"
NEXT_SECTION_HEADING = "### 4. adopter#0 흡수"
MANAGER_STEP_NUMBER = 4
RELEASE_RUNNERS = ("ubuntu-latest", "windows-latest", "macos-latest")
ASSET_NAMES = ("pm-manager-linux-x86_64", "pm-manager-windows-x86_64.exe",
               "pm-manager-macos-arm64")
STEP_HEAD = re.compile(r"^(\d+)\. \*\*(.+?)\*\*")
RELEASE_HEADING = re.compile(r"^## \[\d+\.\d+\.\d+\]", re.MULTILINE)


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


def _manager_changelog_entry() -> str:
    """관리자 항목이 든 가장 최근 릴리즈 판의 그 한 줄 — 실행 파일만 받은 사용자가 읽는 자리다.

    릴리즈 절차가 `[Unreleased]` 를 `## [X.Y.Z] - <날짜>` 로 확정하고 그 위에 새 빈
    `[Unreleased]` 를 얹는다(`pm_role.md` §릴리즈 절차 2). 그래서 제목 문자열을 박지 않고
    릴리즈 판을 위에서부터 읽어 관리자 항목이 든 첫 판을 집는다 — `[Unreleased]` 는 건너뛴다.
    맨 위 판만 보면 관리자 항목이 없는 패치 릴리즈마다 이 단언이 깨진다.
    """
    text = CHANGELOG.read_text(encoding="utf-8")
    for match in RELEASE_HEADING.finditer(text):
        end = text.find("\n## ", match.start() + 1)
        section = text[match.start():end if end != -1 else len(text)]
        for line in section.splitlines():
            if MANAGER_ENTRY_MARK in line:
                return line
    raise AssertionError(f"릴리즈 판 어디에도 관리자 항목({MANAGER_ENTRY_MARK})이 없다")


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


def test_the_changelog_manager_entry_states_the_pyyaml_premise():
    """인터프리터 합의의 다른 면 — 실행 파일에 엔진 런타임 의존이 없다는 말의 사용자 쪽 결과다.

    새 프로젝트 적용 2단계(`pm-config init` → `board.py`)는 OS 기본 인터프리터로 도는데
    `board.py` 가 최상위에서 yaml 을 부른다. 그 전제가 없으면 실행 파일만 받은 사용자는
    적용이 끝나지 않는 이유를 릴리즈 노트에서 찾을 수 없다.
    """
    entry = _manager_changelog_entry()

    assert "PyYAML" in entry
    assert "OS 기본 인터프리터" in entry and "새 프로젝트 적용" in entry


def test_the_asset_names_the_card_promises_are_the_ones_the_build_makes():
    from manager import build

    made = {build.executable_filename(system=system, machine=machine)
            for system, machine in (("Linux", "x86_64"), ("Windows", "AMD64"),
                                    ("Darwin", "arm64"))}
    assert made == set(ASSET_NAMES)


def _pre_push_section(card: Path) -> str:
    text = card.read_text(encoding="utf-8")
    start = text.index("### 2. livegate record")
    return text[start:text.index(SECTION_HEADING, start)]


@pytest.mark.parametrize("card", (ROOT_CARD, *SHIPPED_CARDS),
                         ids=lambda path: path.relative_to(REPO).as_posix())
def test_release_body_template_contract(card):
    step = _steps(_release_section(card))[3]
    blocks = re.findall(r"^   ```text\n(.*?)^   ```", step, re.MULTILINE | re.DOTALL)
    assert len(blocks) == 2
    install, update = blocks
    assert "https://github.com/sumin-ahn/project_manager" in install
    assert "vX.Y.Z 태그로 clone" in install and "해당 태그의 ADOPT.md" in install
    assert "첫 커밋까지 수행" in install
    assert "작업 폴더 생성은 명령만 제시하고 사용자 승인을 받은 뒤 실행" in install
    assert "대상 vX.Y.Z의 CHANGELOG [X.Y.Z] 절에 있는 업그레이드 노트를 먼저 읽는다" in update
    assert update.index("업그레이드 노트를 먼저") < update.index("upstream show") < update.index("./pm-update.sh")
    assert "해당 버전 업그레이드 노트 순서에 따라" in update
    assert "sync-adapter-config --check의 rc=0" in update
    assert "적용 전에 차이를 제시" in update
    assert "적용한 항목과 결과를 보고" in update
    assert re.search(r"v\d+\.\d+\.\d+", install + update) is None
    assert "설치 프롬프트 → 업데이트 프롬프트 → CHANGELOG `[X.Y.Z]` 절 순서" in step
    assert "각각 독립된 `text` 코드 블록" in step
    assert "해당 버전 절의 제목과 본문은 그대로 포함" in step
    assert "실제 릴리즈 태그로 채우며" in step and "버전을 일치시킨다" in step
    assert "과거 릴리즈의 특수 처방" in step and "고정하지 않고" in step
    assert _fenced_commands(step)[-1] == (
        "gh release create vX.Y.Z --notes-file <완성한 릴리즈 본문 파일> --verify-tag")
    assert "<CHANGELOG 해당 절 추출>" not in step


@pytest.mark.parametrize("card", (ROOT_CARD, *SHIPPED_CARDS),
                         ids=lambda path: path.relative_to(REPO).as_posix())
def test_windows_manual_window_check_contract(card):
    section = _pre_push_section(card)
    check = section[section.index("#### Windows 관리자 창 확인 (사람 확인)"):]
    assert "공개 main push 승인 요청 전에 PM이 실제 Windows 데스크톱" in check
    assert "같은 릴리즈 커밋의 코드 트리" in check
    assert "관리자 실행 의존성을 설치" in check
    assert "해당 커밋에서 `manager/ui/dist`를 빌드" in check
    assert "코드 트리 루트에서 실행" in check
    assert "사용자 등록부를 사용하지 않고" in check
    assert "이번 확인 전용으로 아직 존재하지 않는 임시 `projects.json` 절대경로" in check
    assert _fenced_commands(check) == [
        'python -m manager --smoke --registry "<이번 확인 전용 임시 projects.json 절대경로>"']
    assert "창 표시와 자동 종료를 확인" in check
    assert "Windows 버전·검증 커밋 SHA·실행 명령·종료 코드 `rc=0`·`smoke ok` 출력을 보고" in check
    assert "미실행 또는 실패 시 공개 main push 승인 요청을 중단한다" in check
    assert "수정으로 커밋이 바뀌면 같은 확인을 다시 실행" in check
    assert "소스 smoke 결과는 livegate 기록에 넣지 않는다" in check
    assert "공개 후 관리자 실행 파일 3종의 CI 확인도 별도로 수행" in check
    assert "새 기계 게이트·설정 키·우회 옵션은 추가하지 않는다" in check


@pytest.mark.parametrize("card", SHIPPED_CARDS,
                         ids=lambda path: path.relative_to(REPO).as_posix())
def test_every_shipped_card_carries_the_same_pre_push_section(card):
    assert _pre_push_section(card) == _pre_push_section(ROOT_CARD)
