"""Fresh-adopter e2e 게이트 — import → lint clean → ticket 라이프사이클 (양 harness · 기계층).

[[feature-ship-needs-fresh-adopter-gate]]: diff-scoped 리뷰·root 테스트는 *출하 template* 의
dangling framework wikilink·placeholder 누락·작동 여부를 못 본다(drift-0=engine 만). 이 테스트는
깨끗한 디렉토리에 양 harness 를 **실제 import** 해 (a) adopter 인스턴스 `board.py lint` 가 clean
(adopter 엔 ADR 이 없으니 출하 doc 에 framework `[[ADR-NNNN]]` 가 새면 *여기서* dangling 으로 터진다)
· (b) ticket new→claim→complete 라이프사이클이 작동함을 못박는다. tests/ 평범 테스트라 매 회귀·매
push(pre-push 훅)에 자동 포함된다.

**기계층 게이트다.** harness-중립 engine(board·pm_import)만 구동 — 라이브 LLM·네트워크 0(토큰 0·
결정적). claude/opencode *LLM 이 문서를 읽고 실제 PM 을 운영* 하는 **런타임** 검증은 라이브 harness 가
필요해 여기서 하지 않는다 (사용자 환경 파일럿 후속 — relay live smoke[`PM_ORCH_LIVE`·skip]와 같은
클래스). `--fill manual` 이라 `{{OPENCODE_PRO_MODEL}}`·자유서술 placeholder 는 TODO 로 남는 게
정상(LLM-fill 경로는 라이브라 별개)이며 lint/workflow 에 무영향.
"""
from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"


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


def _board(dest: Path, *args: str) -> subprocess.CompletedProcess:
    """imported 트리의 board.py 를 동일 인터프리터로 subprocess 호출 (cwd=dest·비대화형·capture)."""
    return subprocess.run(
        [sys.executable, str(dest / ".project_manager" / "tools" / "board.py"), *args],
        cwd=str(dest),
        capture_output=True,
        text=True,
        env={**os.environ, "PM_NONINTERACTIVE": "1"},
    )


@pytest.mark.parametrize("harness", ["claude", "opencode"])
def test_fresh_adopter_imports_lints_clean_and_runs_workflow(pm_import, tmp_path, harness):
    """깨끗한 import → adopter lint clean → ticket new/claim/complete 작동 (harness 별)."""
    dest = tmp_path / f"adopter-{harness}"
    rc = pm_import.main(
        ["--new", str(dest), "--harness", harness, "--name", "Adopter", "--fill", "manual"]
    )
    assert rc == 0, f"{harness} import 실패 (rc={rc})"
    assert (dest / ".project_manager" / "tools" / "board.py").is_file()

    # (a) adopter 인스턴스 `board.py lint` clean — `.project_manager/wiki/` 트리의 dangling
    #     framework wikilink·thin·depends 누출이 여기서 터진다(adopter 엔 ADR 없음).
    lint = _board(dest, "lint")
    assert lint.returncode == 0, (
        f"{harness} adopter `board.py lint` 비-clean — wiki 출하 doc 에 dangling [[ADR/T]]·thin 누출?\n"
        f"--- stdout ---\n{lint.stdout}\n--- stderr ---\n{lint.stderr}"
    )

    # (a') 루트 진입문서(CLAUDE.md/AGENTS.md·lite)는 `board.py lint` 스캔 *밖*이다 — 직접 스캔.
    #      adopter 엔 framework object 가 없으니 `[[ADR-/T-/idea-N]]` 가 있으면 곧 dangling.
    entry_docs = {"claude": ["CLAUDE.md", "CLAUDE.lite.md"],
                  "opencode": ["AGENTS.md", "AGENTS.lite.md"]}[harness]
    framework_wikilink = re.compile(r"\[\[(?:ADR-\d|T-\d|idea-\d)")
    for name in entry_docs:
        doc = dest / name
        if not doc.is_file():  # full 무게축은 .lite 미출하 — 자연 부재.
            continue
        hits = framework_wikilink.findall(doc.read_text(encoding="utf-8"))
        assert not hits, (
            f"{harness} 진입문서 {name} 에 framework wikilink {hits} — adopter 엔 해당 객체가 "
            f"없어 dangling. 출하 진입문서는 plain text 로 (ADR-NNNN).")

    # (b) ticket 라이프사이클 — new → claim → complete 가 adopter 엔진에서 작동.
    new = _board(dest, "new", "adopter smoke", "--touches", "README.md")
    assert new.returncode == 0, f"{harness} `board.py new` 실패: {new.stderr}"

    listing = _board(dest, "list", "--status", "open")
    assert listing.returncode == 0, f"{harness} `board.py list` 실패: {listing.stderr}"
    m = re.search(r"T-\d+", listing.stdout)
    assert m, f"{harness} 발행된 ticket 을 list 에서 못 찾음:\n{listing.stdout}"
    tid = m.group(0)

    claim = _board(dest, "claim", tid, "--session", "pilot")
    assert claim.returncode == 0, f"{harness} `board.py claim {tid}` 실패: {claim.stderr}"

    done = _board(
        dest, "complete", tid, "--tests-pass", "--allow-missing-log", "--allow-untested"
    )
    assert done.returncode == 0, f"{harness} `board.py complete {tid}` 실패: {done.stderr}"


# ── 출하 @render 스킬/command materialize 가드 (T-0142/T-0143 — 신규 스킬 회귀) ──────
# `board.py lint` clean 은 파일 *부재* 를 못 잡는다(없어도 clean). 출하 스킬이 fresh import 에서
# 조용히 누락/미렌더되는 회귀를 source 템플릿 트리 기준 전수 대조로 박는다. PM 33 에서 신규
# pm-update/pm-env 스킬을 추가하며 ephemeral smoke 로만 확인했던 갭의 durable 화 ([[feature-ship-needs-fresh-adopter-gate]]).
# operational 토큰(import 가 *항상* 해소)만 검사 — free-form·{{OPENCODE_PRO_MODEL}} 는 manual fill TODO 라 제외.

_OPERATIONAL_TOKENS = re.compile(r"\{\{(?:PY|PROJECT_NAME|PROJECT_TAGLINE|TEST_CMD)\}\}")

# harness → (source 출하 스킬 트리, adopter 상대경로, 디렉토리형 여부[claude=<name>/SKILL.md · opencode=<name>.md])
_RENDER_SKILL_SRC = {
    "claude": (REPO / "templates" / "claude_code" / ".claude" / "skills", ".claude/skills", True),
    "opencode": (REPO / "templates" / "opencode" / ".opencode" / "command", ".opencode/command", False),
}
_NEW_SKILLS = {"claude": {"pm-update", "pm-env"}, "opencode": {"pm-update.md", "pm-env.md"}}


def _skill_names(root: Path, is_dir: bool) -> set[str]:
    if not root.is_dir():
        return set()
    if is_dir:
        return {p.name for p in root.iterdir() if (p / "SKILL.md").is_file()}
    return {p.name for p in root.glob("*.md")}


@pytest.mark.parametrize("harness", ["claude", "opencode"])
def test_fresh_adopter_render_skills_materialize(pm_import, tmp_path, harness):
    """fresh import 가 출하 @render 스킬/command 전부를 materialize + operational 토큰 해소 (양 harness).

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

@pytest.mark.parametrize("harness", ["claude", "opencode"])
def test_fresh_adopter_drift_lint_fires_on_real_local_conf(pm_import, tmp_path, harness):
    """실 local.conf 2키로 adapter-drift advisory 발화 + never-block (양 harness·engine 중립)."""
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

    # seen 미기록 → graceful(발화 안 함).
    clean = _board(dest, "lint")
    assert "adapter-drift" not in clean.stdout, f"{harness}: seen 미기록인데 drift 발화(graceful 실패)"

    # seen≠baseline 주입 → 발화.
    conf.write_text(conf_txt + "upstream_seen_rev=ffff0000baselinedifferent\n", encoding="utf-8")
    fired = _board(dest, "lint")
    assert "adapter-drift" in fired.stdout, f"{harness}: 인위 drift 인데 adapter-drift 미발화\n{fired.stdout}"

    # never-block — advisory 라 `--gate` 종료코드 0.
    gated = _board(dest, "lint", "--gate")
    assert gated.returncode == 0, (
        f"{harness}: adapter-drift 가 `--gate` 를 차단(never-block 위배·exit {gated.returncode})\n{gated.stdout}"
    )
