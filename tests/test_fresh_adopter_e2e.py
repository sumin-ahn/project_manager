"""Fresh-adopter e2e 게이트 — import → lint clean → ticket 라이프사이클 (양 harness · 기계층).

[[feature-ship-needs-fresh-adopter-gate]]: diff-scoped 리뷰·root 테스트는 *출하 template* 의
dangling framework wikilink·placeholder 누락·작동 여부를 못 본다(drift-0=engine 만). 이 테스트는
깨끗한 디렉토리에 양 harness 를 **실제 import** 해 (a) adopter 인스턴스 `board.py lint` 가 clean
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
import subprocess
import sys
from pathlib import Path

import pytest
from _settings_portability import portability_failures, referenced_hook_paths

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
        # 엔진 출력은 UTF-8(한글 포함) — 부모 콘솔 로케일(Windows cp949)로 디코드하면
        # reader-thread 가 UnicodeDecodeError 로 죽어 stdout=None → 명시 utf-8 로 캡처.
        encoding="utf-8",
        errors="replace",
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

# 어댑터 네임스페이스 상한(추가 relpath ⊆ 이 집합). claude add 의 @render 제외(.claude/agents·skills)는
# 추가 파일을 *줄일* 뿐이라 subset 단언엔 무영향 — 상한 predicate 로 충분하다.
_ADD_HARNESS_NS_BOUND = {
    "opencode": (".opencode", "AGENTS.md"),
    "claude": (".claude", "CLAUDE.md"),
}


def _snapshot_tree(root: Path) -> dict[str, bytes]:
    """트리의 모든 파일 relpath(posix) → 바이트 스냅샷 (__pycache__·.git 등 stale/VCS 산출물 제외)."""
    snap: dict[str, bytes] = {}
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if any(part in ("__pycache__", ".git") for part in rel.parts):
            continue
        snap[rel.as_posix()] = p.read_bytes()
    return snap


def _in_adapter_ns_bound(rel: str, added_harness: str) -> bool:
    """rel(dst relpath)이 *추가되는 harness* 어댑터 네임스페이스 상한 안인가 (독립 참조 규칙)."""
    adapter_dir, root_doc = _ADD_HARNESS_NS_BOUND[added_harness]
    return rel == root_doc or rel.startswith(adapter_dir + "/")


@pytest.mark.parametrize("base,added", [("claude", "opencode"), ("opencode", "claude")])
def test_fresh_adopter_add_harness_adds_only_adapter_namespace(pm_import, tmp_path, base, added):
    """fresh import(base) → add-harness(added): 추가는 어댑터 네임스페이스뿐·기존 트리 바이트 불변.

    ADR-0048 라이브-안전 불변식의 e2e 층 — raw 재-import 5-file clobber 재발을 실 인스턴스 전체 트리
    diff 로 못박는다(단위 spot-check 보완). 1차 param(claude→opencode)이 실측 clobber 시나리오,
    2차(opencode→claude)는 대칭 검증.
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
    base_dir, base_doc = _ADD_HARNESS_NS_BOUND[base]
    assert base_doc in before and any(r.startswith(base_dir + "/") for r in before), \
        f"{base}: 스냅샷에 base 어댑터({base_dir}/**·{base_doc}) 부재"

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
    add_dir, add_doc = _ADD_HARNESS_NS_BOUND[added]
    assert add_doc in added_rels, f"{base}→{added}: root doc {add_doc} 미추가"
    assert any(r.startswith(add_dir + "/") for r in added_rels), f"{base}→{added}: {add_dir}/** 미추가"

    # (3) 기존 relpath 전부 바이트 불변 (wiki `.project_manager/**`·엔진·타 harness·root doc 0 변경).
    changed = sorted(r for r in before_rels & after_rels if before[r] != after[r])
    assert changed == [], (
        f"{base}→{added}: add-harness 가 기존 파일을 변경(byte diff≠0·5-file clobber 재발): {changed}")
