"""Fresh-adopter e2e 게이트 — import → lint clean → ticket 라이프사이클 (파생 3-harness · 기계층).

[[feature-ship-needs-fresh-adopter-gate]]: diff-scoped 리뷰·root 테스트는 *출하 template* 의
dangling framework wikilink·placeholder 누락·작동 여부를 못 본다(drift-0=engine 만). 이 테스트는
깨끗한 디렉토리에 파생 HARNESSES 전부를 **실제 import** 해 (a) adopter 인스턴스 `board.py lint` 가 clean
(adopter 엔 ADR 이 없으니 출하 doc 에 framework `[[ADR-NNNN]]` 가 새면 *여기서* dangling 으로 터진다)
· (b) ticket new→claim→complete 라이프사이클이 작동함을 못박는다. tests/ 평범 테스트라 매 회귀·매
push(pre-push 훅)에 자동 포함된다.

**기계층 게이트다.** harness-중립 engine(board·pm_import)만 구동 — 라이브 LLM·네트워크 0(토큰 0·
결정적). claude/opencode *LLM 이 문서를 읽고 실제 PM 을 운영* 하는 **런타임** 검증은 라이브 harness 가
필요해 여기서 하지 않는다 (사용자 환경 파일럿 후속 — relay live smoke[`PM_RELAY_LIVE`·skip]와 같은
클래스). `--fill manual` 이라 `{{OPENCODE_PRO_MODEL}}`·자유서술 placeholder 는 TODO 로 남는 게
정상(LLM-fill 경로는 라이브라 별개)이며 lint/workflow 에 무영향.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from _settings_portability import portability_failures, referenced_hook_paths
from _harness_matrix import HARNESSES, entry_docs as _entry_docs

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

def _board_status_dirs() -> tuple[str, ...]:
    """엔진 `board.STATUS_DIRS`(open/claimed/blocked/done) 파생 — lifecycle 이 요구하는 ticket 상태
    디렉토리를 손-열거 4종 하드코딩하지 않는다(이 티켓 원칙). `blocked` 만 누락된 반쪽 T-0433 수정도
    잡는다(3종만 검사하면 그 반쪽이 라이브 경로를 타고 green 이 됐다·round3 S)."""
    spec = importlib.util.spec_from_file_location("_board_status_probe", TOOLS / "board.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return tuple(mod.STATUS_DIRS)


_TICKET_STATUS_DIRS = _board_status_dirs()


def _load_pm_import():
    spec = importlib.util.spec_from_file_location("pm_import", TOOLS / "pm_import.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pm_import():
    mod = _load_pm_import()
    # opencode import 가 라이브 `opencode models` 를 호출하지 않게 고정 — hermetic(설치 여부 무관·토큰 0).
    mod._real_models_runner = lambda: (False, [])
    return mod


def _load_pm_update():
    spec = importlib.util.spec_from_file_location("pm_update", TOOLS / "pm_update.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pm_update():
    return _load_pm_update()


def _board(dest: Path, *args: str) -> subprocess.CompletedProcess:
    """imported 트리의 board.py 를 동일 인터프리터로 subprocess 호출 (cwd=dest·비대화형·capture)."""
    return subprocess.run(
        [sys.executable, str(dest / ".project_manager" / "tools" / "board.py"), *args],
        cwd=str(dest),
        capture_output=True,
        text=True,
        # 엔진 출력은 UTF-8(한글 포함) — 부모 콘솔 로케일(Windows cp949)로 디코드하면
        # reader-thread 가 UnicodeDecodeError 로 죽어 stdout=None → 명시 utf-8 로 캡처.
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PM_NONINTERACTIVE": "1"},
    )


@pytest.mark.parametrize("harness", HARNESSES)
def test_fresh_adopter_imports_lints_clean_and_runs_workflow(pm_import, tmp_path, harness):
    """깨끗한 import → adopter lint clean → ticket new/claim/complete 작동 (전 하네스·codex 포함)."""
    dest = tmp_path / f"adopter-{harness}"
    rc = pm_import.main(
        ["--new", str(dest), "--harness", harness, "--name", "Adopter", "--fill", "manual"]
    )
    assert rc == 0, f"{harness} import 실패 (rc={rc})"
    assert (dest / ".project_manager" / "tools" / "board.py").is_file()

    # (a0·T-0283) opencode: ctx-guard shim↔core co-presence (load-bearing 커플링 가드). 어댑터 파일은
    #   pm_import(rglob 전체트리 byte-copy)로만 출하된다 — manifest 미등재·self-update 채널 없음
    #   (@target-owned 등재는 upstream=claude 루트에 .opencode/ 부재라 skip). 진입점 shim
    #   (plugins/ctx-guard.js)이 core(lib/ctx-guard-core.cjs)를 import 하므로, 한쪽만 landing 하면
    #   opencode 가 플러그인을 로드 못 한다(nudge/stop 死) → 실 출하 산출물에서 *함께* 도착을 못박는다
    #   ([[feature-ship-needs-fresh-adopter-gate]] — source parity 로는 실 landing 을 보증 못 함).
    if harness == "opencode":
        shim = dest / ".opencode" / "plugins" / "ctx-guard.js"
        core = dest / ".opencode" / "lib" / "ctx-guard-core.cjs"
        assert shim.is_file(), f"opencode import 에 ctx-guard shim 미landing: {shim}"
        assert core.is_file(), (
            f"opencode import 에 ctx-guard core 미landing: {core} — shim 이 이걸 import 하므로 로드 깨짐"
        )

    # (a) adopter 인스턴스 blocking lint clean — `.project_manager/wiki/` 트리의 dangling framework
    #     wikilink·thin·depends 누출은 `_ADVISORY_LINT_KINDS` 밖(blocking)이라 `--gate`(exit≠0)로 잡힌다.
    #     fresh adopter 는 baseline-set(import 가 upstream_rev 기록)·seen-unset(seen 은 pm-update 관찰
    #     때만) 이라 adapter-drift **관찰불가 advisory** 가 발화하는 게 정상이다(never-block·pm-update
    #     nudge·option-a·T-0305) — advisory 는 `--gate` 를 차단 안 하므로 blocking clean 은 `--gate` 로
    #     확인하고, 무인자 lint(advisory 표면화)는 아래에서 actionable 여부만 본다.
    lint = _board(dest, "lint", "--gate")
    assert lint.returncode == 0, (
        f"{harness} adopter blocking lint 비-clean(`--gate` exit≠0) — wiki 출하 doc 에 dangling "
        f"[[ADR/T]]·thin·depends 누출?\n--- stdout ---\n{lint.stdout}\n--- stderr ---\n{lint.stderr}"
    )
    # 무인자 lint 는 fresh adopter seen-unset 이라 adapter-drift 관찰불가 advisory 로 exit 1(표면화)이
    #   정상 — advisory 가 명확·actionable(pm-update 안내)인지 확인한다(silent stale 아님·option-a).
    surfaced = _board(dest, "lint")
    assert "관찰불가" in surfaced.stdout and "pm-update" in surfaced.stdout, (
        f"{harness}: fresh adopter seen-unset 관찰불가 advisory 가 actionable(pm-update 안내) 하지 않음\n"
        f"--- stdout ---\n{surfaced.stdout}"
    )

    # (a') 루트 진입문서(CLAUDE.md/AGENTS.md·lite)는 `board.py lint` 스캔 *밖*이다 — 직접 스캔.
    #      adopter 엔 framework object 가 없으니 `[[ADR-/T-/idea-N]]` 가 있으면 곧 dangling.
    # 진입문서 = 하네스 루트 doc + lite 변형(엔진 ADD_HARNESS_ADAPTER 에서 파생·손-열거 아님).
    framework_wikilink = re.compile(r"\[\[(?:ADR-\d|T-\d|idea-\d)")
    root_doc, lite_doc = _entry_docs(harness)
    # primary 루트 doc(HARNESS_ROOT_DOC)은 채택자 진입점 — **반드시 실재**(부재면 fail). 옛 loop 은
    #   primary 부재까지 `is_file()` 로 조용히 skip 해, 루트 doc 미출하 회귀를 놓쳤다(MF1 companion).
    primary = dest / root_doc
    assert primary.is_file(), f"{harness} primary 진입문서 {root_doc} 미출하 (채택자 루트 doc 부재)"
    docs_to_scan = [primary]
    # lite 변형만 부재-skip 허용 — full 무게축(codex 포함)은 .lite 미출하가 정상.
    lite = dest / lite_doc
    if lite.is_file():
        docs_to_scan.append(lite)
    for doc in docs_to_scan:
        hits = framework_wikilink.findall(doc.read_text(encoding="utf-8"))
        assert not hits, (
            f"{harness} 진입문서 {doc.name} 에 framework wikilink {hits} — adopter 엔 해당 객체가 "
            f"없어 dangling. 출하 진입문서는 plain text 로 (ADR-NNNN).")

    # (b) ticket 라이프사이클 — 파생 HARNESSES 전부가 new → claim → complete 를 green으로 완료한다.
    #     T-0433 이후 모든 template은 STATUS_DIRS 스캐폴드를 출하하며, board.py는 상태-dir 부재도
    #     mkdir-before-write로 자가 복구한다. 사전 분기 없이 실제 lifecycle을 실행해 두 계약을 함께 검증한다.
    tickets_root = dest / ".project_manager" / "wiki" / "tickets"
    lifecycle_dirs = [tickets_root / s for s in _TICKET_STATUS_DIRS]

    new = _board(dest, "new", "adopter smoke", "--touches", "README.md")
    assert new.returncode == 0, (
        f"{harness} `board.py new` 실패(rc={new.returncode}) — ticket 상태 스캐폴드 또는 "
        f"mkdir-before-write 계약 회귀.\n--- stderr ---\n{new.stderr}")

    # bare list = 세션 기본 뷰(ADR-0067) — 솔로/무바인딩이면 user-단위 폴백이라 방금 내가 만든 open 이
    # 상세로 나온다. 타 세션분은 완전 비노출(접힘 카운트 "그 외 open N건" 줄은 ADR-0067 로 제거됨).
    mine = _board(dest, "list", "--status", "open")
    assert mine.returncode == 0, f"{harness} `board.py list` 실패: {mine.stderr}"
    assert re.search(r"T-\d+", mine.stdout), (
        f"{harness} bare list(세션 기본 뷰)에 내가 만든 open 미표시:\n{mine.stdout}")
    assert "그 외 open" not in mine.stdout, (
        f"{harness} ADR-0067 접힘 카운트 줄이 제거됐어야(타 세션분 완전 비노출):\n{mine.stdout}")
    listing = _board(dest, "list", "--all", "--status", "open")
    assert listing.returncode == 0, f"{harness} `board.py list --all` 실패: {listing.stderr}"
    m = re.search(r"T-\d+", listing.stdout)
    assert m, f"{harness} 발행된 ticket 을 list --all 에서 못 찾음:\n{listing.stdout}"
    tid = m.group(0)

    claim = _board(dest, "claim", tid, "--repo", "pilot", "--slot", "1")
    assert claim.returncode == 0, f"{harness} `board.py claim {tid}` 실패: {claim.stderr}"

    done = _board(
        dest, "complete", tid, "--tests-pass", "--allow-missing-log", "--allow-untested"
    )
    assert done.returncode == 0, f"{harness} `board.py complete {tid}` 실패: {done.stderr}"

    # (b') 스캐폴드 **완결성** hard assert — lifecycle 이 실제로 돈 *뒤*, 채택자 트리에 엔진
    #      `STATUS_DIRS` 4종이 전부 실존하는지 본다(손-열거 아닌 파생·round4 S 승계). lifecycle 은
    #      open/claimed/done 만 밟으므로 이 단언이 없으면 `blocked` 만 누락된 **반쪽 T-0433 수정**
    #      (또는 board.py 지연-생성만 랜딩해 dir 이 스캐폴드로는 안 오는 경우)이 full green 으로
    #      축복된다. T-0433 의 DoD 는 A+B+C 전부라, 반쪽 상태에서 red 인 게 정합이다.
    missing_dirs = [d.name for d in lifecycle_dirs if not d.is_dir()]
    assert not missing_dirs, (
        f"{harness} ticket 상태-dir 불완전: {missing_dirs} 미실존 — lifecycle(open→claimed→done)은 "
        f"통과했지만 출하 스캐폴드가 STATUS_DIRS {list(_TICKET_STATUS_DIRS)} 를 다 갖추지 못했다"
        "(반쪽 파리티·T-0433). lifecycle 이 안 밟는 상태-dir 도 채택자는 즉시 쓴다(blocked 이행).")


@pytest.mark.parametrize("harness", HARNESSES)
def test_harness_templates_ship_ticket_status_scaffold(pm_import, harness):
    """파생 HARNESSES 축의 모든 template이 README와 상태-dir keep 파일을 출하한다."""
    (template_dir,) = pm_import.HARNESS_TEMPLATE_DIRS[harness]
    tickets = REPO / "templates" / template_dir / ".project_manager" / "wiki" / "tickets"
    assert (tickets / "README.md").is_file(), f"{harness}: tickets/README.md 미출하"
    missing = [status for status in _TICKET_STATUS_DIRS
               if not (tickets / status / ".gitkeep").is_file()]
    assert not missing, (
        f"{harness}: ticket 상태 스캐폴드 .gitkeep 미출하: {missing} — 새 하네스는 HARNESSES "
        "파생 축에 자동 편입되어 이 가드를 통과해야 한다")


def test_missing_ticket_status_dirs_self_repair_through_full_lifecycle(pm_import, tmp_path):
    """상태 dir가 없어도 new→block→unblock→claim→complete가 자가 복구한다.

    Sensitivity: dump_ticket 또는 move_item의 mkdir-before-write를 되돌리면 이 fixture는
    즉시 FileNotFoundError로 red가 된다.
    """
    dest = tmp_path / "adopter-missing-status-dirs"
    rc = pm_import.main(
        ["--new", str(dest), "--harness", "codex", "--name", "Adopter", "--fill", "manual"])
    assert rc == 0
    tickets = dest / ".project_manager" / "wiki" / "tickets"
    for status in _TICKET_STATUS_DIRS:
        (tickets / status).rename(tickets / f"removed-{status}")

    new = _board(dest, "new", "repair smoke", "--touches", "README.md")
    assert new.returncode == 0, new.stderr
    tid = re.search(r"T-\d+", new.stdout).group(0)
    block = _board(dest, "block", tid, "--reason", "repair fixture")
    assert block.returncode == 0, block.stderr
    unblock = _board(dest, "unblock", tid)
    assert unblock.returncode == 0, unblock.stderr
    claim = _board(dest, "claim", tid, "--repo", "pilot", "--slot", "1")
    assert claim.returncode == 0, claim.stderr
    done = _board(dest, "complete", tid, "--tests-pass", "--allow-missing-log", "--allow-untested")
    assert done.returncode == 0, done.stderr
    assert all((tickets / status).is_dir() for status in _TICKET_STATUS_DIRS)


# ── 멀티-유저 훅 경로 portability 가드 (T-0191 · v1.0.x 운영버그 #5) ──────────────
# import 가 {{PROJECT_ROOT}} 를 절대경로로 박으면 git-공유 시 다른 머신에서 훅이 깨진다
# (alice 절대경로 커밋 → bob pull → 그 경로 없음 → 훅 무음 실패·ctx-stop 안전게이트 死).
# settings.json 훅/PreCompact 은 런타임 머신별 해소 ${CLAUDE_PROJECT_DIR}, run_tests_hook.sh 는
# self-resolve 라 *렌더된* 결과에 절대경로/{{PROJECT_ROOT}} 가 남으면 안 된다(fresh-adopter 게이트).

def test_fresh_adopter_hook_paths_are_machine_portable(pm_import, tmp_path):
    """claude import 후 settings.json/run_tests_hook.sh 에 절대경로·{{PROJECT_ROOT}} 잔존 0."""
    dest = tmp_path / "adopter-portable"
    rc = pm_import.main(
        ["--new", str(dest), "--harness", "claude", "--name", "Adopter", "--fill", "manual"]
    )
    assert rc == 0, f"import 실패 (rc={rc})"
    dest_abs = str(dest.resolve())

    settings_text = (dest / ".claude" / "settings.json").read_text(encoding="utf-8")
    run_tests_text = (dest / ".claude" / "run_tests_hook.sh").read_text(encoding="utf-8")

    for fname, text in (("settings.json", settings_text), ("run_tests_hook.sh", run_tests_text)):
        assert "{{PROJECT_ROOT}}" not in text, (
            f"{fname} 에 미치환 {{{{PROJECT_ROOT}}}} 잔존 — portable 형이 아님")
        assert dest_abs not in text, (
            f"{fname} 에 import 절대경로({dest_abs}) 박제 — git 공유 시 다른 머신서 훅 깨짐. "
            "settings.json=$CLAUDE_PROJECT_DIR / run_tests=self-resolve 를 써라.")

    # settings.json 훅 명령(hooks.*)은 런타임 머신별 해소를 쓴다 (절대경로 미박제).
    data = json.loads(settings_text)
    hook_cmds = [
        h.get("command", "")
        for event_hooks in data.get("hooks", {}).values()
        for block in event_hooks
        for h in block.get("hooks", [])
    ]
    assert hook_cmds, "settings.json 에 훅 명령 없음"
    for cmd in hook_cmds:
        assert "CLAUDE_PROJECT_DIR" in cmd or cmd.startswith("./"), (
            f"훅 명령이 머신별 해소(${{CLAUDE_PROJECT_DIR}})·상대경로 미사용: {cmd!r}")

    # run_tests_hook.sh 는 치환 토큰 0 (완전 self-contained·모든 머신 byte-identical).
    assert "{{" not in run_tests_text, "run_tests_hook.sh 에 치환 토큰 잔존 (self-resolve 아님)"


# ── adopter 출하 위생: 프레임워크-내부 최상위 README 미출하 (T-0192 · v1.0.x 운영버그 #6) ──
# 템플릿 트리 최상위 README.md 는 "어댑터 타깃" 프레임워크-내부 문서(`../../README.md`·
# `../opencode/README.md` 상대링크)라 adopter 트리에선 dangling. adopter 로 복사되면 안 된다.
# 하위 `.project_manager/wiki/*/README.md`(wiki 구조 안내)는 adopter-facing 이라 유지.

@pytest.mark.parametrize("harness", ["claude", "opencode", "both"])
def test_fresh_adopter_excludes_framework_internal_readme(pm_import, tmp_path, harness):
    """import 후 최상위 README.md 미출하 · 하위 wiki README 유지 · dangling 프레임워크 링크 0."""
    dest = tmp_path / f"adopter-readme-{harness}"
    rc = pm_import.main(
        ["--new", str(dest), "--harness", harness, "--name", "Adopter", "--fill", "manual"]
    )
    assert rc == 0, f"{harness} import 실패 (rc={rc})"

    # 최상위 README.md 는 출하 안 됨 (프레임워크-내부 어댑터-타깃 doc·dangling 링크 포함).
    assert not (dest / "README.md").exists(), (
        f"{harness}: 프레임워크-내부 최상위 README.md 가 adopter 로 출하됨 "
        "(COPY_EXCLUDE_RELPATHS 로 제외해야 함)")

    # 하위 wiki 구조 안내 README 는 유지 (adopter-facing·정확 relpath 만 제외).
    assert (dest / ".project_manager" / "wiki" / "tickets" / "README.md").exists(), (
        f"{harness}: wiki/tickets/README.md 가 실수로 제외됨 (최상위만 제외해야 함)")

    # adopter 트리 어디에도 프레임워크-상대(sibling 트리) dangling 링크가 남지 않는다.
    for md in dest.rglob("*.md"):
        text = md.read_text(encoding="utf-8")
        assert "../opencode/README.md" not in text and "../claude_code/README.md" not in text, (
            f"{harness}: {md.relative_to(dest)} 에 프레임워크-상대 dangling 링크 잔존")


# ── 출하 @render 스킬/command materialize 가드 (T-0142/T-0143 — 신규 스킬 회귀) ──────
# `board.py lint` clean 은 파일 *부재* 를 못 잡는다(없어도 clean). 출하 스킬이 fresh import 에서
# 조용히 누락/미렌더되는 회귀를 source 템플릿 트리 기준 전수 대조로 박는다. PM 33 에서 신규
# pm-update/pm-env 스킬을 추가하며 ephemeral smoke 로만 확인했던 갭의 durable 화 ([[feature-ship-needs-fresh-adopter-gate]]).
# operational 토큰(import 가 *항상* 해소)만 검사 — free-form·{{OPENCODE_PRO_MODEL}} 는 manual fill TODO 라 제외.

_OPERATIONAL_TOKENS = re.compile(r"\{\{(?:PY|PROJECT_NAME|PROJECT_TAGLINE|TEST_CMD)\}\}")

# harness → (source 출하 스킬 트리, adopter 상대경로, 디렉토리형 여부[<name>/SKILL.md])
# 양 하네스 모두 canonical `.claude/skills/<name>/SKILL.md` 를 소비한다(ADR-0065 단일 소비·opencode
# `.opencode/command` 수기 사본 채널 은퇴·T-0364). opencode 출하 미러도 `.claude/skills` 디렉토리형.
#   codex 는 canonical 스킬을 `.agents/skills/<name>/SKILL.md` 로 remap 소비한다(ADR-0054/0065·@source).
_RENDER_SKILL_SRC = {
    "claude": (REPO / "templates" / "claude_code" / ".claude" / "skills", ".claude/skills", True),
    "opencode": (REPO / "templates" / "opencode" / ".claude" / "skills", ".claude/skills", True),
    "codex": (REPO / "templates" / "codex" / ".agents" / "skills", ".agents/skills", True),
}
_NEW_SKILLS = {h: {"pm-update", "pm-env"} for h in _RENDER_SKILL_SRC}

# 하네스 축은 파생(HARNESSES)이되 스킬 소스 경로는 하네스별 config(트리 remap 이 달라 손으로 못 지움).
#   신규 하네스가 _RENDER_SKILL_SRC 를 안 채우면 collection 이 loud 로 죽어 편입을 강제한다.
assert set(HARNESSES) <= set(_RENDER_SKILL_SRC), (
    f"신규 하네스가 _RENDER_SKILL_SRC 에 미등록: {set(HARNESSES) - set(_RENDER_SKILL_SRC)}")


def _skill_names(root: Path, is_dir: bool) -> set[str]:
    if not root.is_dir():
        return set()
    if is_dir:
        return {p.name for p in root.iterdir() if (p / "SKILL.md").is_file()}
    return {p.name for p in root.glob("*.md")}


@pytest.mark.parametrize("harness", HARNESSES)
def test_fresh_adopter_render_skills_materialize(pm_import, tmp_path, harness):
    """fresh import 가 출하 @render 스킬/command 전부를 materialize + operational 토큰 해소 (전 하네스).

    source 출하 트리의 모든 스킬이 adopter 에 도착하는지 전수 대조한다 — 어떤 출하 스킬이라도
    누락/미렌더되면 여기서 터진다(신규 추가 자동 커버). 신규 pm-update/pm-env 는 명시 backstop.
    """
    src_dir, dest_rel, is_dir = _RENDER_SKILL_SRC[harness]
    dest = tmp_path / f"adopter-{harness}"
    rc = pm_import.main(
        ["--new", str(dest), "--harness", harness, "--name", "Adopter", "--fill", "manual"]
    )
    assert rc == 0, f"{harness} import 실패 (rc={rc})"

    expected = _skill_names(src_dir, is_dir)
    materialized = _skill_names(dest / dest_rel, is_dir)

    # (a) 전수 materialize — source 출하 스킬 전부 adopter 도착.
    missing = expected - materialized
    assert not missing, f"{harness}: fresh import 에 출하 스킬/command 누락 {missing} (@render 전파 실패)"

    # (b) 신규 스킬 명시 backstop (T-0142 pm-update · T-0143 pm-env).
    new = _NEW_SKILLS[harness]
    assert new <= materialized, f"{harness}: 신규 스킬 {new - materialized} fresh import 부재"

    # (c) operational 토큰 해소 — {{PY}}·{{PROJECT_NAME}} 등이 import 후 남으면 깨진 스킬.
    for name in expected:
        f = (dest / dest_rel / name / "SKILL.md") if is_dir else (dest / dest_rel / name)
        leaked = _OPERATIONAL_TOKENS.findall(f.read_text(encoding="utf-8"))
        assert not leaked, f"{harness}: {name} 에 미해소 operational 토큰 {set(leaked)} (렌더 실패)"


# ── adapter-drift lint real-file 발화 가드 (T-0141 — 실 local.conf 경로) ───────────
# unit(test_board_lint)은 local_config() 를 stub 한다. 이 테스트는 *실제 import 된* local.conf 의
# 2키(upstream_rev baseline=import 기록 · upstream_seen_rev 주입)로 drift-lint 가 발화하고
# `--gate` 는 never-block(exit 0) 임을 real-file 경로로 박는다.

@pytest.mark.parametrize("harness", HARNESSES)
def test_fresh_adopter_drift_lint_fires_on_real_local_conf(pm_import, tmp_path, harness):
    """실 local.conf 2키로 adapter-drift advisory 발화 + never-block (전 하네스·engine 중립)."""
    dest = tmp_path / f"adopter-{harness}"
    rc = pm_import.main(
        ["--new", str(dest), "--harness", harness, "--name", "Adopter", "--fill", "manual"]
    )
    assert rc == 0, f"{harness} import 실패 (rc={rc})"
    conf = dest / ".project_manager" / "local.conf"
    conf_txt = conf.read_text(encoding="utf-8")
    # import 가 upstream_rev baseline 을 기록했어야 한다(origin 도출·drift 기준점).
    assert any(l.startswith("upstream_rev=") for l in conf_txt.splitlines()), \
        f"{harness}: import 가 upstream_rev baseline 미기록 (drift-lint 입력 부재)"

    # seen 미기록 → 관찰불가 advisory 발화 (option-a·T-0305·never-block). fresh adopter 는 항상
    #   baseline-set·seen-unset 이라 첫 lint 가 관찰불가 advisory 를 낸다 — 아직 upstream 미관찰이라
    #   drift 판정 불가인 *정직한 상태표현*이자 pm-update nudge(silent stale 근절). 과거 silent [] 를 대체.
    observ = _board(dest, "lint")
    assert "adapter-drift" in observ.stdout, f"{harness}: seen 미기록 관찰불가 advisory 미발화\n{observ.stdout}"
    assert "관찰불가" in observ.stdout and "pm-update" in observ.stdout, (
        f"{harness}: 관찰불가 advisory 가 명확·actionable(pm-update 안내) 하지 않음\n{observ.stdout}")
    # never-block — seen-unset 관찰불가도 `--gate` 종료코드 0.
    observ_gate = _board(dest, "lint", "--gate")
    assert observ_gate.returncode == 0, (
        f"{harness}: 관찰불가 advisory 가 `--gate` 차단(never-block 위배·exit {observ_gate.returncode})\n{observ_gate.stdout}")

    # seen≠baseline 주입 → 실 drift 발화(관찰불가 아닌 **방향-중립 불일치** 메시지·T-0413).
    #   lint 는 git 을 안 하므로 두 rev 의 선후를 모른다 — "이후 변경됨" 단정은 관찰값이 baseline 의
    #   조상일 때 거짓이라 폐기됐다(② 실측). 불일치 사실 + 양쪽 rev 만 알린다.
    conf.write_text(conf_txt + "upstream_seen_rev=ffff0000baselinedifferent\n", encoding="utf-8")
    fired = _board(dest, "lint")
    assert "adapter-drift" in fired.stdout, f"{harness}: 인위 drift 인데 adapter-drift 미발화\n{fired.stdout}"
    assert "불일치" in fired.stdout and "관찰불가" not in fired.stdout, \
        f"{harness}: 실 drift(방향-중립 불일치) 메시지 부재\n{fired.stdout}"
    assert "이후 변경됨" not in fired.stdout, \
        f"{harness}: 방향 단정 메시지 잔존(거짓 경보 클래스)\n{fired.stdout}"

    # never-block — advisory 라 `--gate` 종료코드 0.
    gated = _board(dest, "lint", "--gate")
    assert gated.returncode == 0, (
        f"{harness}: adapter-drift 가 `--gate` 를 차단(never-block 위배·exit {gated.returncode})\n{gated.stdout}"
    )


# ── import 된 adopter settings.json portable + 훅 배선 실재 (T-0202 · claude harness) ──
# settings.json + 훅 래퍼(.sh)·스크립트(.py)는 engine.manifest **밖**(인스턴스 소유)이라 pm_import 가
# 토큰 치환 없이 *verbatim 복사* 한다(render 채널 부재·A안 portable-by-construction). 이 층은 그 복사본이
# (a) 유효 JSON·치환 토큰 0·머신-특정 절대경로 0 + (b) settings.json 이 가리키는 훅 파일이 adopter 트리에
# 실재 + `.sh` 실행비트(copy2 mode 보존) 임을 실증한다 — parity 가드(source 트리)를 넘어 import 파이프라인이
# 이 성질을 verbatim 으로 전달함을 못박는 층(clone-and-go·다른 머신 재-import 불요). portable 판정 로직은
# _settings_portability 헬퍼로 parity 가드와 공유(구조적 JSON 순회·POSIX/드라이브 절대경로).


def test_fresh_adopter_settings_portable_and_hooks_wired(pm_import, tmp_path):
    """claude import 후 settings.json 이 portable(유효 JSON·토큰 0·절대경로 0) + 훅 파일 실재·실행비트."""
    dest = tmp_path / "adopter-settings"
    rc = pm_import.main(
        ["--new", str(dest), "--harness", "claude", "--name", "Adopter", "--fill", "manual"]
    )
    assert rc == 0, f"import 실패 (rc={rc})"

    text = (dest / ".claude" / "settings.json").read_text(encoding="utf-8")

    # (a) portable-by-construction — 유효 JSON·치환 토큰 0·머신-특정 절대경로(POSIX/드라이브) 0.
    json.loads(text)  # invalid escape(Windows 절대경로) 시 raise
    failures = portability_failures(text)
    assert not failures, f"import 된 settings.json portable-by-construction 위반: {failures}"
    # import 절대경로 박제 없음 — dest 경로가 새면 git 공유 시 다른 머신서 훅 깨짐(명시 belt-and-suspenders).
    assert str(dest.resolve()) not in text, (
        "import 된 settings.json 에 import 절대경로 박제 — git 공유 시 다른 머신서 훅 깨짐")

    # (b) 훅/statusLine 참조 파일이 adopter 트리에 실재 + `.sh` 실행비트 (${CLAUDE_PROJECT_DIR}/ 제거).
    refs = referenced_hook_paths(text)
    assert refs, "import 된 settings.json 에서 훅/statusLine 참조 경로 0"
    for rel in refs:
        target = dest / rel
        assert target.is_file(), f"settings.json 이 가리키는 훅 파일이 adopter 에 부재: {rel}"
        if rel.endswith(".sh"):
            assert os.access(target, os.X_OK), (
                f"import 된 {rel} 실행비트 소실 (copy2 mode 보존 실패?) — 훅 실행 불가")


# ── add-harness 라이브-안전 e2e: 어댑터 네임스페이스만 추가·기존 트리 바이트 불변 (T-0271 · ADR-0048) ──
# raw 재-import 의 파괴성(실측 5-file clobber: wiki/engine/claude/CLAUDE.md 를 덮음)이 재발하지 않음을
# *전체 트리 diff* 로 못박는다. 단위 테스트(test_pm_import·T-0269)는 대표 파일 3~4개를 spot-check 하지만,
# 이 e2e 는 fresh import 된 실 인스턴스의 전 트리를 스냅샷 → add-harness → 재스냅샷 해 (a) 추가된 relpath
# 가 추가 harness 어댑터 네임스페이스(.opencode/** ∪ AGENTS.md) 밖으로 한 개도 안 새고 · (b) 기존 relpath
# (wiki `.project_manager/**`·엔진·타 harness `.claude/**`·root doc)가 바이트 단위 불변·0 삭제임을 전수
# 대조한다. add-harness 는 render-only(라이브 LLM 0·`opencode models` seam 은 pm_import fixture 가 stub)라
# 결정적 — 최초 import 와 동일 in-process 경로로 구동한다(운영 진입 `pm_config add-harness` 는 별 subprocess
# 라 stub 미상속·live 조회 위험 → 이 게이트의 hermetic 계약과 맞지 않는다).

# 어댑터 네임스페이스 상한(추가 relpath ⊆ 이 집합). 값 shape = `(adapter_dirs: tuple, root_doc)` —
# 엔진 `pm_import.ADD_HARNESS_ADAPTER` 와 동형(ADR-0070 D5·비준 2026-07-21). codex 는 어댑터
# 네임스페이스가 **둘**(`.codex`+`.agents`)이라 dirs-튜플로 일반화하고 claude/opencode 는 단일-원소.
# claude add 의 @render 제외(.claude/agents·skills)는 추가 파일을 *줄일* 뿐이라 subset 단언엔
# 무영향 — 상한 predicate 로 충분하다.
_ADD_HARNESS_NS_BOUND = {
    "opencode": ((".opencode",), "AGENTS.md"),
    "claude": ((".claude",), "CLAUDE.md"),
    "codex": ((".codex", ".agents"), "AGENTS.md"),
}


# 스냅샷 제외 트리 컴포넌트 — VCS/캐시 산출물 + pm_import 백업. `.pm_import_backups/` 는 add-harness 가
# root doc(AGENTS.md) 충돌을 만나 backup+copy 할 때 생기는 **안전 아티팩트**다 (⚠ add-harness 경로는
# main import 와 달리 ensure_backup_dir_gitignored 를 안 태워 git-ignore 미보장 — 엔진 위생 갭·별도 티켓 후보) —
# opencode↔codex 는 공통 코어 AGENTS.md 가 byte-identical 이라, git-미커밋 fresh 채택자에선 git-safe
# skip 이 아니라 backup+identical-rewrite 로 처리된다(라이브 클로버 아님·백업이 원본 보존·재기록은
# 동일 바이트). 백업 아티팩트는 어댑터 네임스페이스도 실 트리도 아니라 스냅샷에서 뺀다 — *실* 파일
# (AGENTS.md)의 바이트 불변은 아래 (3) 이 그대로 검증하므로 라이브-안전 판정력은 유지된다.
_SNAPSHOT_EXCLUDE_PARTS = ("__pycache__", ".git", ".pm_import_backups")


def _snapshot_tree(root: Path) -> dict[str, bytes]:
    """트리의 모든 파일 relpath(posix) → 바이트 스냅샷 (__pycache__·.git·백업 등 산출물 제외)."""
    snap: dict[str, bytes] = {}
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if any(part in _SNAPSHOT_EXCLUDE_PARTS for part in rel.parts):
            continue
        snap[rel.as_posix()] = p.read_bytes()
    return snap


def _in_adapter_ns_bound(rel: str, added_harness: str) -> bool:
    """rel(dst relpath)이 *추가되는 harness* 어댑터 네임스페이스 상한 안인가 (독립 참조 규칙).

    adapter_dirs 는 튜플(codex 는 `.codex`+`.agents` 둘) — 하나라도 매칭하면 네임스페이스 안이다.
    """
    adapter_dirs, root_doc = _ADD_HARNESS_NS_BOUND[added_harness]
    return rel == root_doc or any(rel.startswith(d + "/") for d in adapter_dirs)


@pytest.mark.parametrize("base,added", [
    ("claude", "opencode"), ("opencode", "claude"),   # 1차 실측 clobber + 대칭
    ("claude", "codex"), ("codex", "claude"),          # codex ↔ claude (진입 doc 상이·신규 추가)
    ("opencode", "codex"), ("codex", "opencode"),      # codex ↔ opencode (공통 코어 AGENTS.md 수렴 skip)
])
def test_fresh_adopter_add_harness_adds_only_adapter_namespace(pm_import, tmp_path, base, added):
    """fresh import(base) → add-harness(added): 추가는 어댑터 네임스페이스뿐·기존 트리 바이트 불변.

    ADR-0048 라이브-안전 불변식의 e2e 층 — raw 재-import 5-file clobber 재발을 실 인스턴스 전체 트리
    diff 로 못박는다(단위 spot-check 보완). 1차 param(claude→opencode)이 실측 clobber 시나리오,
    2차(opencode→claude)는 대칭 검증. codex(세 번째 하네스·ADR-0070)는 양방향 편입([[cross-cutting-
    breaking-blast-radius]] — dual-namespace `.codex`+`.agents`·claude/opencode 공존 미가드 방지):
    claude↔codex 는 진입 doc 이 상이(신규 추가)하고, opencode↔codex 는 공통 코어 `AGENTS.md` 가
    byte-identical 이라 add-harness 가 그 root doc 을 git-safe skip(수렴)하는 게 정상 — 아래 sanity 가
    "신규 추가 또는 byte-identical 무변" 둘 다 허용해 그 수렴을 함께 못박는다(D3 C-v2).
    """
    dest = tmp_path / f"adopter-{base}-add-{added}"
    rc = pm_import.main(
        ["--new", str(dest), "--harness", base, "--name", "Adopter", "--fill", "manual"]
    )
    assert rc == 0, f"{base} import 실패 (rc={rc})"

    # add-harness 직전 전체 트리 바이트 스냅샷.
    before = _snapshot_tree(dest)
    # 공허참 방지 — 스냅샷이 실제로 라이브-파괴 원천(wiki dev-state·엔진·base 어댑터)을 담았다.
    assert any(r.startswith(".project_manager/wiki/") for r in before), \
        f"{base}: 스냅샷에 wiki dev-state 부재 (공허 테스트?)"
    assert ".project_manager/tools/board.py" in before, f"{base}: 스냅샷에 엔진 board.py 부재"
    base_dirs, base_doc = _ADD_HARNESS_NS_BOUND[base]
    assert base_doc in before and any(r.startswith(d + "/") for d in base_dirs for r in before), \
        f"{base}: 스냅샷에 base 어댑터({'/'.join(base_dirs)}/**·{base_doc}) 부재"

    # 라이브-안전 add-harness — render-only·hermetic(models seam = pm_import fixture stub). 소스는
    # 명시 REPO(=프레임워크 checkout·채택자의 `--from`/path upstream 과 동형). T-0282 이후 add-harness
    # 는 소스를 명시 --from > dest upstream(path) > dest(templates) 순으로 해소하는데, fresh 인스턴스는
    # upstream 을 *URL*(origin)로 기록하므로 자동 해소 대상이 아니다 — 채택자는 로컬 checkout 을 명시한다.
    # 이 e2e 의 초점은 소스 해소가 아니라 복사 스코프/바이트 불변이라 REPO 를 명시해 결정화한다.
    plan = pm_import.add_harness(dest, added, dry_run=False, source_root=REPO)
    assert plan, f"{base}→{added}: add-harness plan 이 비어 있다."

    after = _snapshot_tree(dest)
    before_rels, after_rels = set(before), set(after)
    added_rels = after_rels - before_rels
    removed_rels = before_rels - after_rels

    # (1) 삭제 0 — 기존 파일이 사라지면 안 된다.
    assert not removed_rels, f"{base}→{added}: add-harness 가 기존 파일 삭제: {sorted(removed_rels)}"

    # (2) 추가된 relpath ⊆ 추가 harness 어댑터 네임스페이스뿐 (그 밖은 한 개도 안 샌다).
    outside = sorted(r for r in added_rels if not _in_adapter_ns_bound(r, added))
    assert outside == [], (
        f"{base}→{added}: add-harness 가 어댑터 네임스페이스 밖 파일 추가(라이브-안전 위반): {outside}")
    # sanity — 어댑터가 실제로 추가됐다(스코프가 맞는 트리를 잡았다는 방증).
    add_dirs, add_doc = _ADD_HARNESS_NS_BOUND[added]
    # root doc 은 둘 중 하나여야 한다: (a) 신규(추가되는 harness 의 진입 doc 이 base 에 없음) → added_rels
    #   에 있어야 하고, (b) 이미 byte-identical 로 존재(opencode↔codex 공통 코어 AGENTS.md 수렴·D3 C-v2)
    #   → add-harness 가 재추가하지 않음(git-safe skip 또는 backup+동일-재기록·_snapshot_tree 주석) → 따라서
    #   추가(added_rels)가 아니라 *live 바이트 무변*으로 확인한다(위 (3) 도 재확인). 누락/실제변경은 red.
    if add_doc in before:
        assert before[add_doc] == after[add_doc], (
            f"{base}→{added}: 공통 코어 root doc {add_doc} 이 변경됨(byte-parity 수렴 skip 이 아니라 덮어씀)")
    else:
        assert add_doc in added_rels, f"{base}→{added}: root doc {add_doc} 미추가"
    assert any(r.startswith(d + "/") for d in add_dirs for r in added_rels), \
        f"{base}→{added}: 어댑터 dir({'/'.join(add_dirs)}/**) 미추가"

    # (3) 기존 relpath 전부 바이트 불변 (wiki `.project_manager/**`·엔진·타 harness·root doc 0 변경).
    changed = sorted(r for r in before_rels & after_rels if before[r] != after[r])
    assert changed == [], (
        f"{base}→{added}: add-harness 가 기존 파일을 변경(byte diff≠0·5-file clobber 재발): {changed}")


# ── T-0308: fresh opencode 채택자 drift-0 e2e (pm_import↔pm_update 전파 게이트·B-freshadopter) ──
# T-0305 의 self-update e2e(test_pm_update.py)는 ManifestEntry/plan/apply 레벨이다. 이 층은 그와 상보인
# **fresh-adopter 각도**: 실 pm_import 로 opencode 채택자를 만들고 → 엔진(프레임워크) 어댑터/드라이버를
# mutate → 실 pm_update self-update(main())로 채택자에 전파됨을 못박고, 동시에 pm_import 렌더 산출 ==
# pm_update 렌더 산출(drift-0·[[verify-engine-template-propagation]])임을 입증한다. hermetic: 라이브 LLM
# 0(--opencode-model 결정적 치환·models seam 은 pm_import fixture stub)·네트워크 0(read_upstream_rev
# stub·framework 는 비-git)·라이브 하니스 0. [[release-run-all-three-tiers]] 의 machine half — T-0304 라이브
# composite 와 축이 다르다(재현 가능·on-demand 아님).
#
# 채택자가 opencode self-update 를 돌리려면 local.conf 에 opencode_pro_model 이 있어야 한다 — @render 가
# `{{OPENCODE_PRO_MODEL}}` 를 재유도하는데 미보유면 pm_render._assert_no_leak 가 크래시(자족 산출물 위반).
# --opencode-model 결정적 flag 로 import 가 agents 치환 + local.conf 기록 → pm_update 재렌더가 동일
# 리터럴로 해소(drift-0 성립·크래시 0). 라이브 모델 조회 없이 결정적이다.
_OPENCODE_MODEL = "anthropic/claude-test-t0308"


def _build_opencode_framework(tmp_path: Path) -> Path:
    """REPO 로부터 mutable opencode 프레임워크 소스를 만든다 (import + self-update 소스로 재사용).

    opencode 채택자 engine.manifest 가 참조하는 root-상대 경로 전부를 담아 실 프레임워크 루트 레이아웃을
    재현한다: 엔진(`.project_manager/`·tools·wiki methodology + 템플릿·engine.manifest)·`.gitattributes` +
    root `.claude/skills`(PM-workflow 스킬 canonical·bare @render root-sourced·ADR-0065 단일 소비) +
    @source 어댑터 canonical(`templates/opencode/.opencode/*`·`templates/opencode/.project_manager/
    engine.manifest`). 이 트리면 pm_import(`templates/opencode/` 읽기)·pm_update self-update(root
    `.project_manager/` + root `.claude/skills` + @source remap 읽기) 둘 다 결정적으로 rc0 동작한다(엔진
    어느 항목도 missing 아님). REPO 를 손대지 않도록 복사본을 쓴다 — 엔진 mutate 를 이 복사본에 가한다(격리)."""
    framework = tmp_path / "framework"
    ignore = shutil.ignore_patterns("__pycache__", ".git", "node_modules")
    shutil.copytree(REPO / ".project_manager", framework / ".project_manager", ignore=ignore)
    shutil.copytree(REPO / "templates" / "opencode",
                    framework / "templates" / "opencode", ignore=ignore)
    # root `.claude/skills` — opencode 매니페스트의 bare `.claude/skills @render` 소스(root-sourced·
    #   claude_code 와 동일). 단일 소비(ADR-0065)라 opencode self-update 가 이 canonical 을 읽는다.
    shutil.copytree(REPO / ".claude" / "skills", framework / ".claude" / "skills", ignore=ignore)
    shutil.copy2(REPO / ".gitattributes", framework / ".gitattributes")
    return framework


def test_fresh_opencode_adopter_engine_mutate_propagates_and_render_drift0(
        pm_import, pm_update, tmp_path, monkeypatch):
    """fresh opencode import → 엔진 mutate → pm_update self-update 전파 + pm_import↔pm_update 렌더 drift-0.

    (A) 엔진(프레임워크) PM-workflow 스킬(`.claude/skills` @render·ADR-0065 단일 소비) + lib 드라이버
        (byte-copy·engine-mirror) 를 mutate → 실 pm_update self-update 가 채택자로 전파(스킬→`.claude/skills`·
        driver→`.opencode/lib`). (B) mutate 전 self-update 는 전파 트리를 한 바이트도 안 바꾼다 = pm_import
        렌더 산출 == pm_update 렌더 산출(drift-0·같은 소스→같은 산출).
    (C) lib/ctx-guard-core.cjs(engine-mirror driver·T-0305 hook/driver 전파화)가 채택자에 도달.

    (B) 의 no-op 은 (A) 의 mutate-전파와 결합돼 비-공허하다 — (A) 가 채널이 살아있음(mutate 가 실제로 전파)을,
    (B) 가 동일 소스에선 두 렌더 채널이 byte-identical(재기록 diff 0)임을 각각 못박는다.
    """
    framework = _build_opencode_framework(tmp_path)

    # hermetic self-update: read_upstream_rev(라이브 git baseline)를 stub + pm_update 가 이 stub 된
    #   pm_import 를 쓰게 배선(서브프로세스/네트워크 0·framework 는 비-git 이라 어차피 None 이지만 명시 격리).
    monkeypatch.setattr(pm_import, "read_upstream_rev", lambda *a, **k: None)
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)

    # fresh opencode 채택자 — 결정적 모델 flag(라이브 opencode 미실행·models seam 은 fixture stub).
    dest = tmp_path / "adopter-opencode"
    rc = pm_import.main([
        "--new", str(dest), "--harness", "opencode", "--name", "Adopter",
        "--fill", "manual", "--opencode-model", _OPENCODE_MODEL, "--from", str(framework),
    ])
    assert rc == 0, f"opencode import 실패 (rc={rc})"
    conf = (dest / ".project_manager" / "local.conf").read_text(encoding="utf-8")
    assert f"opencode_pro_model={_OPENCODE_MODEL}" in conf, (
        "import 가 opencode_pro_model 을 local.conf 에 기록 안 함 — self-update @render 가 "
        "{{OPENCODE_PRO_MODEL}} 를 재유도 못 해 _assert_no_leak 크래시(drift-0 전제 붕괴).")

    # self-update dest = self-location(pm_update.REPO) → 채택자로 고정. source = --from framework(명시).
    monkeypatch.setattr(pm_update, "REPO", dest)

    def _self_update() -> int:
        return pm_update.main(["--from", str(framework)])

    # 전파 대상 스냅샷: `.opencode`(agents/lib/plugins/pm_orch) + `.claude/skills`(단일 소비 스킬·ADR-0065)
    #   — 공허참 방지 sanity 포함. command 채널은 은퇴(T-0364)라 스냅샷 대상에서 빠진다.
    before = _snapshot_tree(dest / ".opencode")
    before_skills = _snapshot_tree(dest / ".claude" / "skills")
    assert any(r.startswith("agents/") for r in before), \
        "opencode 어댑터 스냅샷에 agents 부재(공허 테스트?)"
    assert "lib/ctx-guard-core.cjs" in before, \
        "engine-mirror driver(ctx-guard-core.cjs) 스냅샷 부재 — T-0305 lib 전파 대상 확인 불가"
    assert "pm-env/SKILL.md" in before_skills, \
        "PM-workflow 스킬 스냅샷에 pm-env/SKILL.md 부재(단일 소비 스킬 미출하·공허 테스트?)"

    # (B) drift-0 — mutate 전 self-update 는 `.opencode/**`·`.claude/skills/**` 를 한 바이트도 안 바꾼다.
    #     같은 소스(framework)에서 pm_import 가 낸 산출과 pm_update 가 낸 산출이 byte-identical 이면 재기록 0.
    #     두 렌더 채널이 갈렸다면 self-update 가 파일을 다른 바이트로 덮어 여기서 diff 로 터진다.
    assert _self_update() == 0, "fresh opencode 채택자 self-update 가 rc0 아님(@render 크래시?)"
    after_noop = _snapshot_tree(dest / ".opencode")
    after_noop_skills = _snapshot_tree(dest / ".claude" / "skills")
    drifted = sorted(r for r in before if before[r] != after_noop.get(r))
    drifted += sorted("skills/" + r for r in before_skills if before_skills[r] != after_noop_skills.get(r))
    assert drifted == [], (
        f"pm_import↔pm_update 렌더 drift(같은 소스인데 self-update 가 전파 트리 재기록): {drifted}")
    assert set(after_noop) == set(before) and set(after_noop_skills) == set(before_skills), \
        "self-update 가 전파 파일을 추가/삭제(같은 소스 no-op 위반)"

    # (A)+(C) 엔진 mutate → 전파. 스킬(`.claude/skills` @render·ADR-0065)·lib(byte-copy·engine-mirror
    #   driver) 한 곳씩 sentinel. 스킬 소스는 root `.claude/skills`(bare @render root-sourced).
    sentinel = "SENTINEL_T0308_ENGINE_MUTATE"
    skill_src = framework / ".claude" / "skills" / "pm-env" / "SKILL.md"
    lib_src = framework / "templates" / "opencode" / ".opencode" / "lib" / "ctx-guard-core.cjs"
    skill_src.write_text(skill_src.read_text(encoding="utf-8") + f"\n<!-- {sentinel} -->\n", encoding="utf-8")
    lib_src.write_text(lib_src.read_text(encoding="utf-8") + f"\n// {sentinel}\n", encoding="utf-8")

    assert _self_update() == 0, "엔진 mutate 후 self-update 가 rc0 아님"
    skill_dest = (dest / ".claude" / "skills" / "pm-env" / "SKILL.md").read_text(encoding="utf-8")
    lib_dest = (dest / ".opencode" / "lib" / "ctx-guard-core.cjs").read_text(encoding="utf-8")
    assert sentinel in skill_dest, (
        "엔진 PM-workflow 스킬(`.claude/skills` @render·단일 소비) 변경이 채택자 `.claude/skills` 로 "
        "전파 안 됨 (전파 채널 끊김·ADR-0065)")
    assert sentinel in lib_dest, (
        "엔진 lib 드라이버(engine-mirror·T-0305 hook/driver 전파화) 변경이 채택자 `.opencode/lib` 로 "
        "전파 안 됨 (hook/driver 미도달·frozen 재발)")
