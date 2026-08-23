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
from _harness_matrix import (
    HARNESSES,
    HARNESS_ADAPTER_DIRS,
    HARNESS_ROOT_DOC,
    _PM_IMPORT,
    entry_docs as _entry_docs,
)
from _repo_owned_inventory import OWNED, repo_owned_paths

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

_RAW_PM_SKILLS = (
    "pm-adr", "pm-bootstrap", "pm-dev-delegate", "pm-env", "pm-handoff",
    "pm-qa", "pm-regression", "pm-release", "pm-review", "pm-ticket",
    "pm-update", "pm-wave-claim", "pm-wave-finish", "pm-worktree",
)
_RAW_SLASH_ENTRY = re.compile(
    r"(?<![A-Za-z0-9_.>/\-])/(?P<skill>"
    + "|".join(sorted(_RAW_PM_SKILLS, key=len, reverse=True))
    + r")(?![A-Za-z0-9_>/\-]|\.[A-Za-z0-9_])"
)


def _expected_opencode_command(skill_text: str, skill_name: str) -> str:
    canonical = "(references/operational-details.md)"
    assert skill_text.count(canonical) == 1
    return skill_text.replace(
        canonical,
        f"(../../.claude/skills/{skill_name}/references/operational-details.md)"
    )


def _codex_readable_text_paths(dest: Path) -> list[Path]:
    roots = [dest / "AGENTS.md", dest / "README.md", dest / ".agents", dest / ".codex",
             dest / ".project_manager" / "wiki"]
    paths = []
    for root in roots:
        if root.is_file():
            paths.append(root)
        elif root.is_dir():
            paths.extend(
                path
                for path in repo_owned_paths(
                    dest, root.relative_to(dest), mode=OWNED
                )
                if path.is_file()
            )
    return sorted(set(paths))


def _raw_unannotated_slash_entries(path: Path) -> list[tuple[int, str]]:
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []
    issues = []
    for match in _RAW_SLASH_ENTRY.finditer(text):
        line_start = text.rfind("\n", 0, match.start()) + 1
        line_end = text.find("\n", match.end())
        line_end = len(text) if line_end < 0 else line_end
        line = text[line_start:line_end]
        local_start = match.start() - line_start
        left_tick = line.rfind("`", 0, local_start)
        right_tick = line.find("`", match.end() - line_start)
        if left_tick >= 0 and right_tick >= 0:
            label = re.match(r"\(([^)]*)\)", line[right_tick + 1:])
            if label and "codex" not in label.group(1).split("·"):
                continue
        issues.append((text.count("\n", 0, match.start()) + 1, match.group(0)))
    return issues

def _check_off_dod(dest: Path, tid: str) -> Path:
    """채택자 트리의 claimed 티켓 DoD 를 전항 체크(`- [ ]` → `- [x]`)하고 그 경로를 돌려준다.

    complete 는 DoD 기록 게이트(T-0596)를 통과해야 하고, 출하 `_template.md` 의 DoD 4항은 미체크로
    시작한다 — 채택자 PM 이 마감 전 손으로 하는 일을 lifecycle 테스트가 그대로 재현한다.
    """
    claimed = dest / ".project_manager" / "wiki" / "tickets" / "claimed"
    (path,) = list(claimed.glob(f"{tid}-*.md"))
    path.write_text(
        path.read_text(encoding="utf-8").replace("- [ ] ", "- [x] "), encoding="utf-8", newline="\n")
    return path


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


def test_fresh_adopter_default_installs_every_registered_harness_adapter(
        pm_import, tmp_path):
    """``--harness`` 생략은 ``all``과 같아 등록된 세 하네스 어댑터를 전부 설치한다."""
    dest = tmp_path / "adopter-default-all"

    rc = pm_import.main([
        "--new", str(dest), "--name", "Default All", "--fill", "manual",
    ])

    assert rc == 0, f"default import 실패 (rc={rc})"
    assert set(HARNESSES) == set(pm_import.REGISTERED_HARNESSES)
    assert len(HARNESSES) == 3, "현재 출하 계약은 claude·opencode·codex 세 하네스다"
    for harness in HARNESSES:
        for adapter_dir in HARNESS_ADAPTER_DIRS[harness]:
            assert (dest / adapter_dir).is_dir(), (
                f"default import 에 {harness} 어댑터 {adapter_dir} 미설치"
            )
        assert (dest / HARNESS_ROOT_DOC[harness]).is_file(), (
            f"default import 에 {harness} 루트 진입문서 미설치"
        )

    receipt = json.loads(
        (dest / pm_import.INSTALL_RECEIPT_RELPATH).read_text(encoding="utf-8")
    )
    assert receipt["harnesses"] == list(pm_import.REGISTERED_HARNESSES)


def test_fresh_adopter_default_folds_shared_agents_entry_to_neutral_source(
        pm_import, tmp_path):
    """3하네스 기본 공존은 opencode+codex 공유 AGENTS.md를 중립 codex 원본 하나로 접는다."""
    dest = tmp_path / "adopter-default-shared-entry"

    assert pm_import.main([
        "--new", str(dest), "--name", "Shared Entry", "--fill", "manual",
    ]) == 0

    receipt = json.loads(
        (dest / pm_import.INSTALL_RECEIPT_RELPATH).read_text(encoding="utf-8")
    )
    assert receipt["instance_owned_templates"]["AGENTS.md"] == {
        "weight": "full",
        "source": "templates/codex/AGENTS.md",
    }
    agents = (dest / "AGENTS.md").read_text(encoding="utf-8")
    assert "`/pm-bootstrap`(opencode) / `$pm-bootstrap`(codex)" in agents


def test_default_import_partial_templates_error_suggests_narrow_harness(
        pm_import, tmp_path, capsys):
    """무인자=all인데 source가 일부 flavor만 가지면 단일 하네스 탈출구를 함께 안내한다."""
    source = tmp_path / "partial-framework"
    (source / "templates" / "claude_code").mkdir(parents=True)

    rc = pm_import.main([
        "--new", str(tmp_path / "adopter-partial"), "--from", str(source),
        "--name", "Partial",
    ])

    captured = capsys.readouterr()
    assert rc == 1
    assert "소스 어댑터 트리 없음" in captured.err
    assert "또는 --harness claude 처럼 설치할 하네스를 좁혀라" in captured.err


@pytest.mark.parametrize("weight", ("full", "lite"))
@pytest.mark.parametrize(
    "selection", ("codex", "claude,codex", "codex,opencode", "all")
)
def test_real_install_codex_readable_surfaces_have_no_unannotated_slash_entries(
        pm_import, tmp_path, selection, weight):
    """실제 import 조합의 Codex 가 읽는 전체 문서 표면을 renderer와 독립 스캐너로 검사한다."""
    dest = tmp_path / f"notation-{selection.replace(',', '-')}-{weight}"
    assert pm_import.main([
        "--new", str(dest), "--harness", selection, "--name", "Notation Probe",
        "--weight", weight, "--fill", "manual",
    ]) == 0
    paths = _codex_readable_text_paths(dest)
    assert paths and any(".project_manager/wiki/" in path.as_posix() for path in paths)
    failures = {
        path.relative_to(dest).as_posix(): issues
        for path in paths
        if (issues := _raw_unannotated_slash_entries(path))
    }
    assert not failures, f"실 설치본 Codex-readable 문서의 slash 오표기 잔존: {failures}"


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

    # 소유 게이트(T-0781) — claim 정체성이 아닌 세션의 complete 는 채택자 사본에서도 거부되고,
    # 거부 문구가 이동 경로(unclaim→claim · --takeover)를 지목해야 한다.
    foreign = _board(
        dest, "complete", tid, "--tests-pass", "--allow-missing-log", "--allow-untested",
        "--repo", "pilot", "--slot", "9",
    )
    assert foreign.returncode != 0, (
        f"{harness}: 타 세션이 남의 claim 을 complete 했다 — 소유 게이트가 채택자 사본에 없다.\n"
        f"--- stdout ---\n{foreign.stdout}")
    assert all(token in foreign.stderr for token in ("소유", "unclaim", "--takeover")), (
        f"{harness}: 소유 거부 문구가 이동 경로를 안 지목함:\n{foreign.stderr}")

    # DoD 기록 게이트(T-0596) — 출하 template 의 미체크 DoD 로는 complete 가 막혀야 한다.
    # 채택자 형상에서 게이트가 실제로 무는지 여기서 확인한다(엔진만 고치고 template 전파를
    # 빠뜨리면 이 단언이 red — 반쪽 출하 방지). complete 는 claim 과 **같은 정체성**으로 부른다.
    blocked = _board(
        dest, "complete", tid, "--tests-pass", "--allow-missing-log", "--allow-untested",
        "--repo", "pilot", "--slot", "1",
    )
    assert blocked.returncode != 0, (
        f"{harness}: 미체크 DoD 인데 complete 가 통과함 — DoD 게이트가 채택자 사본에 없다.\n"
        f"--- stdout ---\n{blocked.stdout}")
    assert "DoD 미체크" in blocked.stderr, (
        f"{harness}: 차단 사유가 DoD 로 안 나옴:\n{blocked.stderr}")

    _check_off_dod(dest, tid)
    done = _board(
        dest, "complete", tid, "--tests-pass", "--allow-missing-log", "--allow-untested",
        "--repo", "pilot", "--slot", "1",
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


# ── new→promote→claim→complete 4단계 완주 (T-0784 공백 5) ───────────────────
# 위 lifecycle 은 new→claim→complete(3단계) — `promote` 를 한 번도 안 밟는다. `new` 가
# draft 격리로 빠지는 형상은 board-git 활성일 때뿐(`_board_git_enabled()`) 이라, 이 케이스만
# 로컬 bare remote 로 `--board-submodule` 을 실제로 세운다(`test_pm_import_board_submodule.py::
# test_board_ops_target_submodule` 과 동형 셋업 — 이미 통과가 확인된 패턴 재사용). git 부재
# 환경은 skip.

_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
}

requires_git = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git 바이너리 부재 — board-submodule 4단계 완주 케이스 skip.",
)


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-c", "protocol.file.allow=always", *args],
                          cwd=str(cwd), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=False)


# 5절 전부 채운(placeholder 0) 대체 본문 — draft 를 promote 게이트에 태우기 위한 재작성.
_FOUR_STEP_FILLED_BODY = (
    "## 목표\n실제 목표 문장이다.\n\n"
    "## 인터페이스\n실제 규격이다.\n\n"
    "## 결정\n실제 방향이다.\n\n"
    "## 완료 조건 (Definition of Done)\n- [ ] 실제 산출물\n\n"
    "## 참고\n- 실제 참고\n"
)


@requires_git
def test_new_promote_claim_complete_four_step_lifecycle(pm_import, tmp_path, monkeypatch):
    """new(draft)→promote→claim→complete 4단계 완주 — 매 단계 rc·상태 디렉터리를 단언한다."""
    for key, val in _GIT_IDENTITY.items():
        monkeypatch.setenv(key, val)
    bare = tmp_path / "board.git"
    _git(["init", "--bare", "-q", str(bare)], tmp_path)
    dest = tmp_path / "adopter-4step"
    rc = pm_import.main([
        "--new", str(dest), "--harness", "claude", "--name", "FourStep",
        "--board-submodule", "--board-remote", str(bare), "--fill", "manual",
    ])
    assert rc == 0, f"board-submodule import 실패 (rc={rc})"
    board_tickets = dest / ".project_manager" / "board" / "tickets"

    # 1단계 — new: 제목만 있는 미충전 본문은 board-git 활성 하에서 draft 로 격리된다(open/ 창 0).
    new = _board(dest, "new", "4단계 완주 스모크")
    assert new.returncode == 0, f"new 실패: {new.stderr}"
    drafts = list((board_tickets / ".drafts").glob("T-*.md"))
    assert len(drafts) == 1, f"draft 격리 실패(1건 기대): {drafts}"
    draft_path = drafts[0]
    m = re.search(r"(T-\S+?)-", draft_path.name)
    assert m, f"draft 파일명에서 ID 를 못 뽑음: {draft_path.name}"
    tid = m.group(1)
    assert not list(board_tickets.glob(f"open/{tid}-*.md")), (
        "draft 인데 open/ 에도 나타남 — 격리 실패")

    # promote 게이트(placeholder/thin)를 통과시키려고 frontmatter 는 보존한 채 본문만
    # 5절 전부 채운 텍스트로 갈아끼운다(board.py 재로드 없이 순수 텍스트 스플릿).
    raw = draft_path.read_text(encoding="utf-8")
    _blank, frontmatter, _old_body = raw.split("---\n", 2)
    draft_path.write_text(
        f"---\n{frontmatter}---\n\n# {tid} — 4단계 완주 스모크\n\n{_FOUR_STEP_FILLED_BODY}",
        encoding="utf-8", newline="\n")

    # 2단계 — promote: draft → open/ 이동 + board-git 커밋.
    promote = _board(dest, "promote", tid)
    assert promote.returncode == 0, f"promote 실패: {promote.stderr}"
    assert list(board_tickets.glob(f"open/{tid}-*.md")), "promote 성공인데 open/ 에 없음"
    assert not list((board_tickets / ".drafts").glob(f"{tid}-*.md")), "promote 후에도 draft 잔존"

    # 3단계 — claim: open/ → claimed/. 대상 정확히 1개·출발(open/) 0개까지 단언한다(F-003 —
    # 존재만 보면 copy-without-delete 회귀도 green 이 된다).
    claim = _board(dest, "claim", tid, "--repo", "pilot", "--slot", "1")
    assert claim.returncode == 0, f"claim 실패: {claim.stderr}"
    claimed_matches = list(board_tickets.glob(f"claimed/{tid}-*.md"))
    assert len(claimed_matches) == 1, (
        f"claim 성공인데 claimed/ 에 정확히 1개가 아님: {claimed_matches}")
    assert not list(board_tickets.glob(f"open/{tid}-*.md")), (
        "claim 성공인데 open/ 에 원본이 남음 — copy-without-delete 회귀.")

    # DoD 체크 후 4단계 — complete: claimed/ → done/. 대상 정확히 1개·출발(claimed/) 0개까지
    # 단언한다(F-003 — 동형 근거).
    claimed_path = claimed_matches[0]
    claimed_path.write_text(
        claimed_path.read_text(encoding="utf-8").replace("- [ ] ", "- [x] "),
        encoding="utf-8", newline="\n")
    complete = _board(
        dest, "complete", tid, "--tests-pass", "--allow-missing-log",
        "--allow-untested", "--repo", "pilot", "--slot", "1")
    assert complete.returncode == 0, f"complete 실패: {complete.stderr}"
    done_matches = list(board_tickets.glob(f"done/{tid}-*.md"))
    assert len(done_matches) == 1, (
        f"complete 성공인데 done/ 에 정확히 1개가 아님: {done_matches}")
    assert not list(board_tickets.glob(f"claimed/{tid}-*.md")), (
        "complete 성공인데 claimed/ 에 원본이 남음 — copy-without-delete 회귀.")


# ── bare 귀속 조작(홈 슬롯 행) ────────────────────────────────────────────
# 위 lifecycle 은 `claim tid --repo pilot --slot 1`(명시)로 돈다 — 카드가 지시하는 **bare**
# 형태(`board.py claim T-NNNN`)를 기계층이 한 번도 안 밟는 구조적 사각이었다. fresh 채택자는
# import 가 홈 자신을 첫 슬롯 행으로 등록하므로, 카드 형태 그대로 첫 시도가 그 행의 정체성으로
# rc0 임을 여기서 못박는다.


def test_fresh_adopter_bare_claim_resolves_via_home_slot_row(pm_import, tmp_path, monkeypatch):
    """fresh 채택자(등록 repo 1개)에서 명시 플래그 없는 bare `claim` 이 홈 슬롯 행으로 rc0.

    import 가 홈 자신을 첫 슬롯 행(`slot="."`·`session=<repo>_1`)으로 등록하므로 정체성은
    장부 행에서 온다 — "행이 없다"에서 유도하지 않는다.

    env `PM_SESSION_NAME`/`CLAUDE_SESSION_NAME` 누출을 monkeypatch 로 제거하고 실행한다(부모
    pytest 프로세스가 우연히 값을 들고 있어도 이 서브프로세스가 그걸 상속해 오검출하지 않게 —
    `_board` 헬퍼는 `os.environ` 을 그대로 전달하므로 이 delenv 가 그 상속 경로를 닫는다).
    """
    monkeypatch.delenv("PM_SESSION_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_NAME", raising=False)
    dest = tmp_path / "adopter-bare-claim"
    rc = pm_import.main(
        ["--new", str(dest), "--harness", "claude", "--name", "Adopter", "--fill", "manual"]
    )
    assert rc == 0, f"import 실패 (rc={rc})"
    ledger = dest / ".project_manager" / ".local" / "worktree-leases.json"
    assert ledger.exists(), "fresh import 가 홈 슬롯 행을 등록해야 한다(등록 지점)"
    rows = json.loads(ledger.read_text(encoding="utf-8"))["leases"]
    assert [(row["slot"], row["session"], row["state"]) for row in rows] == [
        (".", f"{dest.name}_1", "leased")
    ], f"홈 행이 canonical 값이 아니다: {rows}"

    new = _board(dest, "new", "bare claim probe", "--touches", "README.md")
    assert new.returncode == 0, f"`board.py new` 실패: {new.stderr}"
    listing = _board(dest, "list", "--all", "--status", "open")
    assert listing.returncode == 0, f"`board.py list --all` 실패: {listing.stderr}"
    m = re.search(r"T-\d+", listing.stdout)
    assert m, f"발행된 ticket 을 list --all 에서 못 찾음:\n{listing.stdout}"
    tid = m.group(0)

    # 카드 형태 그대로 — 명시 --repo/--slot 없이 bare claim.
    claim = _board(dest, "claim", tid)
    assert claim.returncode == 0, (
        f"카드 형태 bare `board.py claim {tid}` 실패(rc={claim.returncode}) — 홈 슬롯 행 "
        f"해소 회귀.\n--- stdout ---\n{claim.stdout}\n--- stderr ---\n{claim.stderr}"
    )
    assert f"{dest.name}_1" in claim.stdout, (
        f"claim 귀속이 홈 행의 session 값이 아니다:\n{claim.stdout}")


def test_fresh_adopter_bare_claim_stays_unresolved_when_a_second_slot_is_leased(
        pm_import, tmp_path, monkeypatch):
    """역가드: 활성 슬롯이 2개면(홈 행 + 타 슬롯) bare claim 은 fail-loud — 해소가 "활성 lease
    정확히 1개" 를 넘어 느슨해지지 않았음을 fresh 채택자 실 import 산출물에서 못박는다
    (adopter#0/multi-PM 형상 축소판)."""
    monkeypatch.delenv("PM_SESSION_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_NAME", raising=False)
    dest = tmp_path / "adopter-bare-claim-pool"
    rc = pm_import.main(
        ["--new", str(dest), "--harness", "claude", "--name", "Adopter", "--fill", "manual"]
    )
    assert rc == 0, f"import 실패 (rc={rc})"

    new = _board(dest, "new", "bare claim pool probe", "--touches", "README.md")
    assert new.returncode == 0, f"`board.py new` 실패: {new.stderr}"
    listing = _board(dest, "list", "--all", "--status", "open")
    m = re.search(r"T-\d+", listing.stdout)
    assert m, f"발행된 ticket 을 list --all 에서 못 찾음:\n{listing.stdout}"
    tid = m.group(0)

    # 풀 형상 시뮬 — 등록된 홈 행 옆에 다른 세션(타 슬롯)의 leased 행 1개를 장부에 직접 심는다
    # (장부 writer 규약과 무관하게 "활성 슬롯 2개" 만 검증하면 되므로 최소 스키마 직접 write).
    leases_file = dest / ".project_manager" / ".local" / "worktree-leases.json"
    ledger = json.loads(leases_file.read_text(encoding="utf-8"))
    ledger["leases"].append(
        {"slot": f"work/{dest.name}_2", "repo": dest.name,
         "session": f"{dest.name}_2", "state": "leased"}
    )
    leases_file.write_text(json.dumps(ledger), encoding="utf-8")

    claim = _board(dest, "claim", tid)
    assert claim.returncode != 0, (
        f"활성 슬롯 2개인데 bare claim 이 rc0 — 해소가 '정확히 1개' 조건을 넘어 느슨해졌다"
        f"(과결속).\n--- stdout ---\n{claim.stdout}")
    assert "세션 미해소" in claim.stderr, (
        f"차단 사유가 세션 미해소 fail-loud 안내가 아님:\n{claim.stderr}")


# ── 라운드 파일 모델 (ADR-0090) — 어댑터 문구 · 준비→회수 1사이클 ─────────────
# 티켓 산출은 명세 파일 안 역할 절이 아니라 `tickets/rounds/<T-NNNN>/NN-<역할>.md` 라운드 파일
# 하나다. 어댑터가 옛 모델을 지시하면 에이전트는 존재하지 않는 자리를 찾는다(문서가 곧 실행 지시).

# 단일 파일 컨테이너 시절 어휘 — 위임 문서 표면에 하나라도 남으면 red.
#   엔진 `.project_manager/tools/` 는 마이그레이션 진단 문구로 이 낱말을 legitimately 쓰므로
#   스캔 밖이고, 이 표면은 "에이전트·PM 이 읽고 그대로 실행하는 문서"로 한정한다.
_ROUND_MODEL_STALE_TOKENS = (
    "pm-ticket-section", "pm-ticket-seal", "seal-backfill",
    "--transfer-from", "--capability-stdin", "ticket-copy", "ticket_copies",
    "자기 절", "역할 절", ".growth",
)
# 위임 문서 표면 = 역할 카드 + 위임 스킬/슬래시 command + 위임 스킬 references + opencode PM
# 지침 + 방법론 2종 + lite 진입문서.
_DELEGATION_SKILLS = ("pm-dev-delegate", "pm-ticket")
_ADAPTER_NAMESPACES = tuple(
    sorted({d for dirs in HARNESS_ADAPTER_DIRS.values() for d in dirs})
)


def _delegation_docs(root: Path) -> list[Path]:
    """`root` 트리의 위임 문서 전부 — 어댑터 네임스페이스는 엔진 매핑에서 파생(손-열거 아님)."""
    found: list[Path] = []
    for namespace in _ADAPTER_NAMESPACES:
        base = root / namespace
        if not base.is_dir():
            continue
        agents = base / "agents"
        if agents.is_dir():
            found += [
                path for path in sorted(agents.iterdir())
                if path.is_file() and path.suffix in (".md", ".toml")
            ]
        for skill in _DELEGATION_SKILLS:
            found += [
                path for path in (
                    base / "skills" / skill / "SKILL.md",
                    base / "command" / f"{skill}.md",
                ) if path.is_file()
            ]
            references = base / "skills" / skill / "references"
            if references.is_dir():
                found += [
                    path for path in sorted(references.glob("*.md")) if path.is_file()
                ]
        instructions = base / "pm-instructions.md"
        if instructions.is_file():
            found.append(instructions)
    wiki = root / ".project_manager" / "wiki"
    found += [
        path for path in (wiki / "pm_role.md", wiki / "pm_playbook.md") if path.is_file()
    ]
    found += [path for path in sorted(root.glob("*.lite.md")) if path.is_file()]
    return sorted(set(found))


def _stale_round_model_hits(paths) -> dict:
    hits = {}
    for path in paths:
        text = path.read_text(encoding="utf-8")
        found = [token for token in _ROUND_MODEL_STALE_TOKENS if token in text]
        if found:
            hits[str(path)] = found
    return hits


def _template_roots() -> list[Path]:
    """`templates/<dir>` 단일-어댑터 타깃 루트 (엔진 HARNESS_TEMPLATE_DIRS 파생)."""
    return [
        REPO / "templates" / dirs[0]
        for dirs in _PM_IMPORT.HARNESS_TEMPLATE_DIRS.values() if len(dirs) == 1
    ]


def test_delegation_docs_drop_single_file_container_vocabulary():
    """canonical + 3 타깃 위임 문서에 옛 단일 파일 컨테이너 어휘가 0이다 (ADR-0090).

    가드 시야를 표면과 **독립으로 대조**한다 — 파생 글롭이 조용히 줄면(디렉토리 개명·미출하)
    스캔 0으로 vacuous green 이 되므로, 각 루트가 실제로 내놓아야 하는 좌표를 따로 단언한다.
    """
    roots = [REPO, *_template_roots()]
    scanned = {root: _delegation_docs(root) for root in roots}

    # (a) 시야 자기검증 — 루트마다 역할 카드와 위임 스킬/command 를 최소 1개씩 봐야 한다.
    for root, docs in scanned.items():
        assert docs, f"위임 문서 스캔 0건: {root} — 파생 글롭이 표면을 놓쳤다"
        relative = {path.relative_to(root).as_posix() for path in docs}
        assert any("/agents/" in rel for rel in relative), (
            f"{root}: 역할 카드가 스캔에 없다 — {sorted(relative)}"
        )
        assert any(
            rel.endswith(f"{skill}/SKILL.md") or rel.endswith(f"command/{skill}.md")
            for skill in _DELEGATION_SKILLS for rel in relative
        ), f"{root}: 위임 스킬/command 가 스캔에 없다 — {sorted(relative)}"
        assert {".project_manager/wiki/pm_role.md",
                ".project_manager/wiki/pm_playbook.md"} <= relative, (
            f"{root}: 방법론 문서가 스캔에 없다 — {sorted(relative)}"
        )

    # (b) 실제 판정 — 옛 어휘 0.
    hits = _stale_round_model_hits(
        [path for docs in scanned.values() for path in docs]
    )
    assert not hits, (
        "위임 문서에 단일 파일 컨테이너 시절 어휘 잔존 — 라운드 파일 모델(ADR-0090)과 어긋난다: "
        f"{hits}"
    )


@pytest.mark.parametrize("harness", HARNESSES)
def test_fresh_adopter_delegation_docs_drop_stale_vocabulary(pm_import, tmp_path, harness):
    """실 import 산출물의 위임 문서에도 옛 어휘가 0이다 (전파 누락 = 채택자만 red 방지)."""
    dest = tmp_path / f"round-docs-{harness}"
    assert pm_import.main(
        ["--new", str(dest), "--harness", harness, "--name", "Adopter", "--fill", "manual"]
    ) == 0
    docs = _delegation_docs(dest)
    assert docs, f"{harness}: 실 설치본에 위임 문서가 하나도 없다 — 어댑터 미출하?"
    hits = _stale_round_model_hits(docs)
    assert not hits, f"{harness} 실 설치본 위임 문서에 옛 어휘 잔존: {hits}"


def _delegate_cli(dest: Path, *args: str) -> subprocess.CompletedProcess:
    """imported 트리의 pm_delegate.py 를 동일 인터프리터로 호출 (cwd=dest·비대화형·capture)."""
    return subprocess.run(
        [sys.executable, str(dest / ".project_manager" / "tools" / "pm_delegate.py"), *args],
        cwd=str(dest), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        env={**os.environ, "PM_NONINTERACTIVE": "1"},
    )


def _git_init_adopter(dest: Path) -> None:
    """채택자 트리를 git 트리로 만든다 — `ticket prepare` 의 ignore 규칙 검증 전제."""
    for args in (
        ["init", "-q"],
        ["add", "-A"],
        ["-c", "user.name=adopter", "-c", "user.email=adopter@test.invalid",
         "commit", "-qm", "seed"],
    ):
        done = subprocess.run(
            ["git", "-C", str(dest), *args], capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        assert done.returncode == 0, f"git {args[0]} 실패: {done.stderr}"


@pytest.mark.parametrize("harness", HARNESSES)
def test_fresh_adopter_runs_one_round_prepare_harvest_cycle(pm_import, tmp_path, harness):
    """실 import 트리에서 `ticket prepare` → 라운드 파일 편집 → `ticket harvest` → `show` 1사이클.

    기계층이다(라이브 LLM 0) — 에이전트가 할 편집을 테스트가 직접 한다. 이 사이클이 채택자
    사본에서 돌아야 위임이 성립하므로, 엔진만 고치고 template 전파를 빠뜨리면 여기서 red 다.
    """
    dest = tmp_path / f"round-cycle-{harness}"
    assert pm_import.main(
        ["--new", str(dest), "--harness", harness, "--name", "Adopter", "--fill", "manual"]
    ) == 0
    _git_init_adopter(dest)

    new = _board(dest, "new", "round cycle probe", "--touches", "README.md")
    assert new.returncode == 0, f"{harness} `board.py new` 실패: {new.stderr}"
    listing = _board(dest, "list", "--all", "--status", "open")
    match = re.search(r"T-\d+", listing.stdout)
    assert match, f"{harness} 발행 ticket 미발견:\n{listing.stdout}"
    tid = match.group(0)
    claim = _board(dest, "claim", tid, "--repo", "pilot", "--slot", "1")
    assert claim.returncode == 0, f"{harness} `board.py claim` 실패: {claim.stderr}"

    # T-0815 설계 근거 게이트 — architect 라운드도 `design: done` 도 없는 신규 티켓은
    # developer 라운드를 준비할 수 없다(면제 경로는 폐지됐다). 이 e2e 의 관심사는 준비→편집→
    # 회수 왕복이라, 설계 절을 채우고 setter 로 상태를 올려 근거를 갖춘다.
    claimed = list((dest / ".project_manager" / "wiki" / "tickets" / "claimed").glob(
        f"{tid}-*.md"))
    assert len(claimed) == 1, f"{harness} claim 뒤 명세 1건이어야 한다: {claimed}"
    ticket_body = claimed[0].read_text(encoding="utf-8")
    assert "## 설계" in ticket_body, f"{harness} 출하 템플릿에 설계 절이 없다"
    filled_section = (
        "## 설계\n"
        "- **경계 실측**: 채택자 e2e 픽스처\n"
        "- **불변식**: 준비→회수 왕복 보존\n"
        "- **표면 상한**: 라운드 파일 1개\n"
        "- **테스트 전략**: 정상 왕복\n\n"
    )
    head, _sep, tail = ticket_body.partition("## 설계\n")
    _skeleton, done_sep, rest = tail.partition("## 완료 조건")
    assert done_sep, f"{harness} 출하 템플릿 절 구성이 예상과 다르다"
    claimed[0].write_text(
        head + filled_section + done_sep + rest, encoding="utf-8", newline="\n")
    design = _board(dest, "design", tid, "done")
    assert design.returncode == 0, f"{harness} `board.py design` 실패: {design.stderr}"

    prepared = _delegate_cli(
        dest, "ticket", "prepare", "--ticket", tid, "--role", "developer",
        "--cwd", str(dest),
    )
    assert prepared.returncode == 0, (
        f"{harness} `ticket prepare` 실패(rc={prepared.returncode}) — 채택자 사본에서 라운드 준비가 "
        f"안 된다.\n--- stdout ---\n{prepared.stdout}\n--- stderr ---\n{prepared.stderr}"
    )
    plan = json.loads(prepared.stdout.strip().splitlines()[-1])
    round_file = Path(plan["copy"])
    run_dir = Path(plan["run_dir"])
    # 순번 zero-pad 폭의 이름 문법은 엔진(ticket_rounds)이 단일 진실 — 여기서 재타이핑하지
    # 않고 glob 으로 찾아 응답 `copy` 와 일치하는지만 대조한다.
    role_rounds = sorted(run_dir.glob("*-developer.md"))
    assert role_rounds == [round_file], (
        f"{harness}: run-dir 라운드 파일이 응답 copy 와 다르다: {role_rounds} vs {round_file}"
    )
    # run-dir 에서 쓸 수 있는 건 라운드 파일 하나고 나머지는 읽기 전용 입력이다.
    assert sorted(item.name for item in run_dir.iterdir()) == [
        round_file.name, "rounds", "spec.md"
    ]

    sentinel = "ROUND_CYCLE_PERSISTED"
    round_file.write_text(
        round_file.read_text(encoding="utf-8") + f"\n{sentinel}\n",
        encoding="utf-8", newline="",
    )
    harvested = _delegate_cli(
        dest, "ticket", "harvest", "--copy", str(round_file), "--cwd", str(dest),
    )
    assert harvested.returncode == 0, (
        f"{harness} `ticket harvest` 실패(rc={harvested.returncode})\n"
        f"--- stdout ---\n{harvested.stdout}\n--- stderr ---\n{harvested.stderr}"
    )
    assert json.loads(harvested.stdout.strip().splitlines()[-1])["changed"] is True
    assert not run_dir.exists(), f"{harness}: 회수 뒤에도 run-dir 이 남음 — run 이 안 닫혔다"

    board_round = (
        dest / ".project_manager" / "wiki" / "tickets" / "rounds" / tid / round_file.name
    )
    assert board_round.is_file(), f"{harness}: board 라운드 파일 부재: {board_round}"
    assert sentinel in board_round.read_text(encoding="utf-8")

    shown = _board(dest, "show", tid)
    assert shown.returncode == 0, f"{harness} `board.py show` 실패: {shown.stderr}"
    assert sentinel in shown.stdout, (
        f"{harness}: `show` 가 회수된 라운드를 표시하지 않는다 — 명세만 출력?\n{shown.stdout}"
    )


@pytest.mark.parametrize("harness", HARNESSES)
def test_fresh_adopter_central_loader_survives_self_update(pm_import, tmp_path, harness):
    """세 flavor 실출하 사본이 중앙 loader를 포함하고 self-update 뒤에도 직접 CLI로 동작한다."""
    dest = tmp_path / f"central-loader-{harness}"
    assert pm_import.main(
        ["--new", str(dest), "--harness", harness, "--name", "Adopter", "--fill", "manual"]
    ) == 0
    seam = dest / ".project_manager" / "tools" / "repo_owned_files.py"
    assert seam.is_file() and "def load_module(" in seam.read_text(encoding="utf-8")

    updated = subprocess.run(
        [
            sys.executable,
            str(dest / ".project_manager" / "tools" / "pm_update.py"),
            "--from",
            str(REPO),
        ],
        cwd=dest,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PM_NONINTERACTIVE": "1"},
        timeout=60,
    )
    assert updated.returncode == 0, (
        f"{harness} self-update failed\nstdout={updated.stdout}\nstderr={updated.stderr}"
    )
    lint = _board(dest, "lint", "--gate")
    assert lint.returncode == 0, (
        f"{harness} post-update board bootstrap failed\nstdout={lint.stdout}\nstderr={lint.stderr}"
    )


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
    """상태 dir가 없어도 new→block→unblock→claim→complete→reopen→discard가 자가 복구한다.

    처분 종결(`discarded/`·T-0781)까지 포함해 STATUS_DIRS 전수를 lifecycle 로 되살린다 —
    채택자 트리에서 신규 종결 디렉토리가 실제로 만들어지는지가 이 축의 관측 지점이다.

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
    _check_off_dod(dest, tid)   # DoD 기록 게이트(T-0596) — 미체크면 complete 가 막힌다.
    # complete 는 claim 과 같은 정체성으로 부른다 — 소유 대조(T-0781).
    done = _board(dest, "complete", tid, "--tests-pass", "--allow-missing-log",
                  "--allow-untested", "--repo", "pilot", "--slot", "1")
    assert done.returncode == 0, done.stderr
    reopened = _board(dest, "reopen", tid, "--reason", "오처리 복구 스모크")
    assert reopened.returncode == 0, reopened.stderr
    discarded = _board(dest, "discard", tid, "dropped", "--reason", "폐기 스모크")
    assert discarded.returncode == 0, discarded.stderr
    assert list((tickets / "discarded").glob(f"{tid}-*.md")), "처분 종결이 discarded/ 로 안 갔다"
    assert all((tickets / status).is_dir() for status in _TICKET_STATUS_DIRS)


# ── 멀티-유저 훅 경로 portability 가드 (T-0191 · v1.0.x 운영버그 #5) ──────────────
# import 가 {{PROJECT_ROOT}} 를 절대경로로 박으면 git-공유 시 다른 머신에서 훅이 깨진다
# (alice 절대경로 커밋 → bob pull → 그 경로 없음 → 훅 무음 실패·ctx-stop 안전게이트 死).
# settings.json 훅/PreCompact 은 런타임 머신별 해소 ${CLAUDE_PROJECT_DIR} 를 쓰므로 *렌더된*
# 결과에 절대경로/{{PROJECT_ROOT}} 가 남으면 안 된다(fresh-adopter 게이트).

def test_fresh_adopter_hook_paths_are_machine_portable(pm_import, tmp_path):
    """claude import 후 settings.json 에 절대경로·{{PROJECT_ROOT}} 잔존 0."""
    dest = tmp_path / "adopter-portable"
    rc = pm_import.main(
        ["--new", str(dest), "--harness", "claude", "--name", "Adopter", "--fill", "manual"]
    )
    assert rc == 0, f"import 실패 (rc={rc})"
    dest_abs = str(dest.resolve())

    settings_text = (dest / ".claude" / "settings.json").read_text(encoding="utf-8")

    assert "{{PROJECT_ROOT}}" not in settings_text, (
        "settings.json 에 미치환 {{PROJECT_ROOT}} 잔존 — portable 형이 아님")
    assert dest_abs not in settings_text, (
        f"settings.json 에 import 절대경로({dest_abs}) 박제 — git 공유 시 다른 머신서 훅 깨짐. "
        "$CLAUDE_PROJECT_DIR 를 써라.")

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


# ── adopter 출하 위생: 프레임워크-내부 최상위 README 미출하 (T-0192 · v1.0.x 운영버그 #6) ──
# 템플릿 트리 최상위 README.md 는 "어댑터 타깃" 프레임워크-내부 문서(`../../README.md`·
# `../opencode/README.md` 상대링크)라 adopter 트리에선 dangling. adopter 로 복사되면 안 된다.
# 하위 `.project_manager/wiki/*/README.md`(wiki 구조 안내)는 adopter-facing 이라 유지.

# 축 = 등록된 단일 하네스 + registry 파생 ``all``. README 미출하 불변식은 import 모드와 무관하게
# 성립하므로(top README는 어느 선택으로도 출하 금지) 전체 선택까지 태운다. 임의 콤마 조합의
# 순서/중복/합집합은 ``test_pm_import``의 집합 parser·e2e가 담당한다.
_README_HARNESS_ARGS = (*HARNESSES, "all")


@pytest.mark.parametrize("harness", _README_HARNESS_ARGS)
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


def test_readme_axis_is_derived_and_covers_all_keyword():
    """README 미출하 축이 등록 하네스와 파생 전체 선택을 커버함을 못박는다.

    codex 는 정당 대상(templates/codex/README.md 존재하나 top README 미출하·wiki README 유지·
    dangling 0·실측). 파생이라 새 단일 하네스/콤보 키가 자동 편입된다(T-0434 가짜-하네스 패턴).
    """
    assert set(_README_HARNESS_ARGS) == {*_PM_IMPORT.HARNESS_TEMPLATE_DIRS, "all"}
    assert {"claude", "codex", "opencode", "all"} <= set(_README_HARNESS_ARGS)


# ── 출하 @render 스킬/command materialize 가드 (T-0142/T-0143 — 신규 스킬 회귀) ──────
# `board.py lint` clean 은 파일 *부재* 를 못 잡는다(없어도 clean). 출하 스킬이 fresh import 에서
# 조용히 누락/미렌더되는 회귀를 source 템플릿 트리 기준 전수 대조로 박는다. PM 33 에서 신규
# pm-update/pm-env 스킬을 추가하며 ephemeral smoke 로만 확인했던 갭의 durable 화 ([[feature-ship-needs-fresh-adopter-gate]]).
# operational 토큰(import 가 *항상* 해소)만 검사 — free-form·{{OPENCODE_PRO_MODEL}} 는 manual fill TODO 라 제외.

_OPERATIONAL_TOKENS = re.compile(r"\{\{(?:PY|PROJECT_NAME|PROJECT_TAGLINE|TEST_CMD)\}\}")

# harness → (source 출하 스킬 트리, adopter 상대경로, 디렉토리형 여부[<name>/SKILL.md])
# 양 하네스 모두 canonical `.claude/skills/<name>/SKILL.md` 를 소비한다. opencode는
# 모델 skill tool 미러와 canonical에서 생성한 `.opencode/command` 사람 진입 사본을 둘 다 출하한다(T-0674).
#   codex 는 canonical 스킬을 `.agents/skills/<name>/SKILL.md` 로 remap 소비한다(ADR-0054/0065·@source).
_RENDER_SKILL_SRC = {
    "claude": (REPO / "templates" / "claude_code" / ".claude" / "skills", ".claude/skills", True),
    "opencode": (REPO / "templates" / "opencode" / ".claude" / "skills", ".claude/skills", True),
    "codex": (REPO / "templates" / "codex" / ".agents" / "skills", ".agents/skills", True),
}
_NEW_SKILLS = {h: {"pm-update", "pm-env"} for h in _RENDER_SKILL_SRC}
_OPENCODE_COMMAND_SRC = REPO / "templates" / "opencode" / ".opencode" / "command"

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

    if harness == "opencode":
        expected_commands = {p.name for p in _OPENCODE_COMMAND_SRC.glob("*.md")}
        command_dir = dest / ".opencode" / "command"
        materialized_commands = {p.name for p in command_dir.glob("*.md")}
        assert len(expected_commands) == 15
        assert materialized_commands == expected_commands, (
            f"opencode: fresh import command 사본 누락/잉여 — "
            f"누락={sorted(expected_commands - materialized_commands)}, "
            f"잉여={sorted(materialized_commands - expected_commands)}")
        for filename in expected_commands:
            command = command_dir / filename
            skill = dest / ".claude" / "skills" / command.stem / "SKILL.md"
            assert command.read_text(encoding="utf-8") == _expected_opencode_command(
                skill.read_text(encoding="utf-8"), command.stem
            ), f"fresh import command drift: {filename}"
            details = command.parent / (
                f"../../.claude/skills/{command.stem}/references/operational-details.md"
            )
            assert details.is_file(), f"fresh import command detail missing: {filename}"
            assert not _OPERATIONAL_TOKENS.findall(command.read_text(encoding="utf-8"))


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
    assert any(l.startswith("upstream.rev=") for l in conf_txt.splitlines()), \
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
    conf.write_text(conf_txt + "upstream.seen_rev=ffff0000baselinedifferent\n", encoding="utf-8", newline="\n")
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
# 엔진 `pm_import.ADD_HARNESS_ADAPTER` 와 동형(ADR-0070 D5·비준 2026-07-21)이되 **파생**(손-복제
# 아님·T-0453·아래 _derive_ns_bound). codex 는 어댑터 네임스페이스가 **둘**(`.codex`+`.agents`)이라
# dirs-튜플로 일반화하고 claude/opencode 는 단일-원소. claude add 의 @render 제외(.claude/agents·
# skills)는 추가 파일을 *줄일* 뿐이라 subset 단언엔 무영향 — 상한 predicate 로 충분하다.
def _derive_ns_bound(adapter_dirs, root_doc):
    """T-0429 파생 API(`HARNESS_ADAPTER_DIRS`·`HARNESS_ROOT_DOC`)를 per-harness `(adapter_dirs,
    root_doc)` 상한으로 재조합 — 엔진 ADD_HARNESS_ADAPTER 와 동형이되 손-복제가 아니라 파생이라
    새 하네스가 자동 편입된다(손-복제였으면 4번째서 KeyError·T-0453)."""
    return {h: (adapter_dirs[h], root_doc[h]) for h in adapter_dirs}


_ADD_HARNESS_NS_BOUND = _derive_ns_bound(HARNESS_ADAPTER_DIRS, HARNESS_ROOT_DOC)


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


def _add_harness_order_pairs(harnesses) -> list[tuple[str, str]]:
    """base ≠ added 전 순서쌍(N×(N−1)) — add-harness 라이브-안전 e2e 축.

    파생 축 HARNESSES(T-0429)의 순열 — 손-열거 6쌍을 대체한다(T-0434). 하네스 3종이면 6쌍
    (claude↔opencode = 1차 실측 clobber + 대칭 · claude↔codex = 진입 doc 상이·신규 추가 ·
    opencode↔codex = 공통 코어 AGENTS.md byte-수렴 skip), 4종이면 12쌍으로 **자동** 확장된다.
    test_pm_import.py `_ADD_HARNESS_APPLY_PAIRS` 와 동일 idiom(같은 파생원·의미 축).
    """
    return [(base, added) for base in harnesses for added in harnesses if base != added]


_ADD_HARNESS_PAIRS = _add_harness_order_pairs(HARNESSES)


@pytest.mark.parametrize("base,added", _ADD_HARNESS_PAIRS)
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

    # (2) 추가된 relpath ⊆ (추가 harness 어댑터 네임스페이스 ∪ flavor `@render` 선언·cross-ns 의존물)
    #   — 그 밖(flavor 미선언)은 한 개도 안 샌다([[T-0456]] R25). opencode flavor 의 `.claude/skills`
    #   (ADR-0065 네이티브 소비·`.opencode` 밖)는 codex host(미소유)에 반드시 복사되므로(R18→R25 반전)
    #   namespace 상한만으론 오탐 — flavor 선언 경로를 상한에 포함한다. claude host + opencode 는
    #   `.claude/skills` 가 host-소유(before 에 이미 존재)라 added_rels 밖이고, flavor 미선언 stray 는
    #   여전히 red 라 clobber 탐지력은 유지된다.
    cross_ns_allowed = pm_import._flavor_render_relpaths(
        REPO / "templates" / pm_import.HARNESS_TEMPLATE_DIRS[added][0])
    outside = sorted(
        r for r in added_rels
        if not _in_adapter_ns_bound(r, added)
        and not any(r == p or r.startswith(p + "/") for p in cross_ns_allowed))
    assert outside == [], (
        f"{base}→{added}: add-harness 가 네임스페이스·flavor @render 밖 파일 추가(라이브-안전 위반): {outside}")
    # sanity — 어댑터가 실제로 추가됐다(스코프가 맞는 트리를 잡았다는 방증).
    add_dirs, add_doc = _ADD_HARNESS_NS_BOUND[added]
    # 공유 root doc은 추가 뒤 두 하네스가 실제로 읽으므로 서로 다른 진입 표기를 병기한다.
    if add_doc in before:
        if {base, added} == {"codex", "opencode"}:
            root_text = after[add_doc].decode("utf-8")
            assert "`$pm-bootstrap`(codex)" in root_text
            assert "`/pm-bootstrap`(opencode)" in root_text
        else:
            assert before[add_doc] == after[add_doc]
    else:
        assert add_doc in added_rels, f"{base}→{added}: root doc {add_doc} 미추가"
    assert any(r.startswith(d + "/") for d in add_dirs for r in added_rels), \
        f"{base}→{added}: 어댑터 dir({'/'.join(add_dirs)}/**) 미추가"

    # (3) 기존 relpath 바이트 불변. manifest와 설치 하네스가 함께 읽는 표기 문서만 예외다.
    #   단 **`engine.manifest` 는 예외**([[T-0456]]): add-harness 가 자기가 레이다운한 guest 어댑터의
    #   `@render` 를 인스턴스 manifest 에 **append-only 등재**한다(dev-state metadata·네임스페이스 밖이나
    #   T-0456 이 sanction — manifest-파생 overlay 스캔·render 가 guest 를 커버하게 하는 근본 배선).
    #   그 한 파일만 예외로 빼고 나머지 clobber 0 을 유지하며, manifest 변경은 append-only(기존 내용
    #   보존)만 허용한다(guest 라인 없는 added=claude 는 무변도 정상).
    #   **설치 기록도 예외**다: add-harness 는 자기가 추가한 하네스를 인스턴스 설치 기록에 박제해,
    #   이후 판정(`installed_harnesses`·pm_update 표기 독자)이 증거 추론 대신 그 기록을 읽게 한다
    #   (dev-state metadata·manifest 와 같은 성질). 이 파일의 변경은 아래에서 "하네스 목록이 정확히
    #   base∪added 로 늘었는가" 로 못박는다(허용만 하면 그 파일의 탐지력이 0 이 된다).
    #   **어댑터 config 원장도 같은 예외**다(T-0585): add-harness 는 자기가 레이다운한 instance-owned
    #   config 의 template 해시를 원장에 남겨, 다음 동기가 "채택자가 손댔는가" 를 판정할 수 있게 한다
    #   (dev-state metadata·설치 기록과 같은 성질). 보존된 편집분은 애초에 기록되지 않으므로 이 예외가
    #   clobber 를 가리지 않는다 — 원장의 기록 규칙 자체는 `test_pm_import.py` 의 절이 못박는다.
    _MANIFEST_REL = ".project_manager/engine.manifest"
    _RECEIPT_REL = pm_import.INSTALL_RECEIPT_RELPATH.as_posix()
    _BASELINE_REL = pm_import.ADAPTER_BASELINE_RELPATH.as_posix()
    notation_shared = {
        ".project_manager/wiki/README.md",
        ".project_manager/wiki/pm_role.md",
        ".project_manager/wiki/pm_playbook.md",
        ".project_manager/wiki/pm_state.template.md",
        ".project_manager/wiki/tickets/_template.md",
        ".project_manager/wiki/raw/spikes/_template.md",
        ".project_manager/wiki/domain/_template.md",
        # manifest 미소유 출하 seed + 템플릿이 만든 인스턴스 파일도 같은 표기 축이다(T-0541):
        #   하네스를 추가하면 이 둘도 두 독자 표기로 재렌더된다(계획 표시·백업 후 변경). 옛 코드는
        #   이들만 canonical 표기로 남겨 다중 하네스 인스턴스에 잘못된 호출법을 출하했다.
        ".project_manager/wiki/raw/README.md",
        ".project_manager/wiki/pm_state.md",
    }
    if {base, added} == {"codex", "opencode"}:
        notation_shared.add("AGENTS.md")
    changed = sorted(
        r for r in before_rels & after_rels
        if before[r] != after[r]
        and r not in (_MANIFEST_REL, _RECEIPT_REL, _BASELINE_REL)
        and r not in notation_shared
    )
    assert changed == [], (
        f"{base}→{added}: add-harness 가 기존 파일을 변경(engine.manifest·설치 기록·config 원장 "
        f"외·byte diff≠0·clobber 재발): {changed}")

    # (3-a) 설치 기록의 변경 내용 — 정확히 base∪added(registry 순서)여야 한다. 표기 독자 집합이
    #   여기서 갈리므로 하나라도 빠지면 이후 공유 문서 재렌더가 그 하네스 표기를 지운다.
    receipt_after = json.loads(after[_RECEIPT_REL].decode("utf-8"))["harnesses"]
    assert receipt_after == [h for h in pm_import.REGISTERED_HARNESSES if h in {base, added}], (
        f"{base}→{added}: 설치 기록이 base∪added 가 아님: {receipt_after}")

    # (3-b) 예외 2경로는 "바이트 변경 허용"에서 끝내지 않고 **무엇이 바뀌었는지** 못박는다 —
    #   허용만 하면 그 두 파일의 clobber 탐지력이 0 이 된다(치환·fill 오염도 통과). 변경 줄은
    #   전부 두 하네스 label 병기여야 하고 줄 수는 불변이어야 한다(표기 렌더 외 변경 0).
    entry_prefix = {"claude": "/", "codex": "$", "opencode": "/"}
    mixed_notation = entry_prefix[base] != entry_prefix[added]
    rerendered_paths = (
        ".project_manager/wiki/raw/README.md", ".project_manager/wiki/pm_state.md")
    for rel in rerendered_paths:
        if rel not in before or rel not in after or before[rel] == after[rel]:
            continue
        before_lines = before[rel].decode("utf-8").splitlines()
        after_lines = after[rel].decode("utf-8").splitlines()
        assert len(before_lines) == len(after_lines), \
            f"{base}→{added}: {rel} 줄 수 변화 — 표기 렌더 외 변경(치환·fill 오염?)"
        diff_lines = [a for b, a in zip(before_lines, after_lines) if b != a]
        assert diff_lines, f"{base}→{added}: {rel} 바이트만 다르고 줄 diff 0(정합 붕괴)"
        for line in diff_lines:
            assert f"({base})" in line and f"({added})" in line, (
                f"{base}→{added}: {rel} 의 변경 줄이 병기 표기가 아님(표기 외 변경): {line}")
    if mixed_notation:
        # 비공허 — 표기가 갈리는 조합에서는 두 축이 반드시 따라온다(옛 코드는 둘 다 안 따라왔다).
        #   ① 템플릿 생성 산출물(pm_state.md) ② manifest 미소유 출하 seed(raw/README.md·base 가
        #   출하하는 경우만 존재). 둘 중 하나라도 빼면 이 e2e 가 red 가 된다(변이 실측).
        state_rel = ".project_manager/wiki/pm_state.md"
        assert before[state_rel] != after[state_rel], (
            f"{base}→{added}: 생성 산출물 {state_rel} 표기가 안 따라옴(조용한 잔존 재발)")
        seed_rel = ".project_manager/wiki/raw/README.md"
        if seed_rel in before:
            assert before[seed_rel] != after[seed_rel], (
                f"{base}→{added}: manifest 미소유 출하 seed {seed_rel} 표기가 안 따라옴 "
                "(조용한 잔존 재발)")
    if _MANIFEST_REL in before and before[_MANIFEST_REL] != after[_MANIFEST_REL]:
        mf_before = before[_MANIFEST_REL].decode("utf-8")
        mf_after = after[_MANIFEST_REL].decode("utf-8")
        assert mf_after.startswith(mf_before), (
            f"{base}→{added}: engine.manifest 변경이 append-only 아님(기존 내용 훼손·T-0456 위반)")


# Windows 체크아웃(`core.autocrlf=true`)이 CRLF 로 바꾸는 텍스트 확장자 — 이 게이트가 그 형상을
# Linux 에서 재현할 때 상류·채택자 **양쪽**에 같은 집합을 적용한다(한쪽만 바꾸면 byte-copy 채널이
# 정상 동작으로 diff 를 내 판정이 흐려진다).
_CRLF_CHECKOUT_SUFFIXES = (
    ".md", ".py", ".cjs", ".js", ".json", ".jsonc", ".toml", ".txt", ".manifest", ".conf")


def _to_crlf(path: Path) -> bytes:
    """그 파일을 CRLF 체크아웃 형상으로 바꾸고 bytes 를 돌려준다 (Windows `core.autocrlf=true`)."""
    raw = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    path.write_bytes(raw)
    return raw


def _stray_lf(payload: bytes) -> int:
    """CRLF 를 걷어낸 뒤 남는 LF 개수 — 0 이면 그 파일은 CRLF 단일 표기다."""
    return payload.replace(b"\r\n", b"").count(b"\n")


def test_add_harness_preserves_crlf_manifest_notation(pm_import, tmp_path):
    """CRLF 체크아웃 manifest 도 **append-only + 표기 보존** (T-0709).

    Windows 채택자는 `core.autocrlf=true` + `.gitattributes` 무규칙이라 워킹트리 전 파일이 CRLF 다
    (`git ls-files --eol` = `i/lf w/crlf`). 옛 코드는 guest 절 등재에서 manifest 를 universal-newline
    으로 읽고 `newline="\\n"` 으로 되써, 우리가 append 한 절 말고 **파일 전체**가 LF 로 뒤집혔다 —
    `after.startswith(before)`(T-0456 append-only 바이트 불변식)가 그 자리에서 깨지고 채택자
    워킹트리엔 손대지 않은 줄까지 전면 diff 가 났다. 이 게이트는 그 형상을 Linux 에서 재현한다.

    claude→opencode 는 실측 clobber 시나리오이자 guest 절이 실제로 등재되는 쌍이다(등재 0 이면
    표기 판정이 공허해지므로 아래에서 변경 자체를 단언한다)."""
    dest = tmp_path / "adopter-crlf-manifest"
    assert pm_import.main(
        ["--new", str(dest), "--harness", "claude", "--name", "Adopter", "--fill", "manual"]
    ) == 0, "claude import 실패"

    manifest = dest / ".project_manager" / "engine.manifest"
    before = _to_crlf(manifest)
    assert _stray_lf(before) == 0, "픽스처가 CRLF 단일 표기를 못 만들었다(공허 게이트)"

    assert pm_import.add_harness(dest, "opencode", dry_run=False, source_root=REPO), \
        "add-harness plan 이 비어 있다"

    after = manifest.read_bytes()
    assert after != before, "guest 절 등재가 아예 없다 — 표기 판정이 공허해진다"
    assert after.startswith(before), (
        "CRLF manifest 의 변경이 append-only 아님(기존 bytes 훼손·T-0456 위반)")
    assert _stray_lf(after) == 0, (
        "add-harness 가 CRLF manifest 를 LF 로 뒤집었다(표기 비보존 재발) — "
        "덧붙이는 guest 절도 그 파일 표기로 렌더돼야 한다")


def _guest_engine_rows(pm_update, dest: Path) -> dict[str, object]:
    """dest guest 절의 **엔진 행**(비-`@render`) → {경로: ManifestEntry}."""
    return {
        str(entry).replace("\\", "/"): entry
        for entry in pm_update._dest_guest_manifest_entries(dest)
        if not getattr(entry, "render", False)
    }


def _guest_render_paths(pm_update, dest: Path) -> list[str]:
    """dest guest 절의 **렌더물 행**(`@render`) 경로 — 재렌더 채널 대상."""
    return sorted(
        str(entry).replace("\\", "/")
        for entry in pm_update._dest_guest_manifest_entries(dest)
        if getattr(entry, "render", False)
    )


@pytest.mark.parametrize("base,added", _ADD_HARNESS_PAIRS)
def test_add_harness_guest_engine_files_sync_on_pm_update(
        pm_import, pm_update, tmp_path, monkeypatch, base, added):
    """add-harness 로 얹은 guest 하네스의 **엔진 파일**이 pm-update 로 상류와 수렴한다 (T-0574 ⑬).

    결함 형상: add-harness 는 복사만 하고 manifest 에 안 올렸고, 등재 유일분(`@render` 행)마저
    update 계획에서 전량 차감돼 `.codex/pm_orch_codex.py`·`.opencode/lib`·claude ctx 가드가 설치
    시점 사본으로 영구 동결됐다(`pm_relay` 코어와 짝인 engine-mirror → 코어↔드라이버 skew).

    전 순서쌍에서: 채택자 사본을 stale 로 만들고 self-update → (a) 엔진 파일이 upstream 과 **byte
    일치** · (b) 어댑터 렌더물도 같은 채널로 **재렌더**(stale 사본 소멸) · (c) 어느 manifest 에도
    없는 instance-owned 설정은 byte **불변**."""
    dest = tmp_path / f"guest-engine-{base}-{added}"
    assert pm_import.main(
        ["--new", str(dest), "--harness", base, "--name", "Adopter", "--fill", "manual"]
    ) == 0
    pm_import.add_harness(dest, added, dry_run=False, source_root=REPO)

    engine_rows = _guest_engine_rows(pm_update, dest)
    assert engine_rows, f"{base}->{added}: guest 엔진 행 미등재(동기 채널 부재)"

    # 상류 대응이 실재하는 좌표를 stale 로 만든다 — 이번 동기가 실제로 덮는지 본다. 디렉토리
    #   엔트리는 직계 파일만 본다(하위 트리 전수 순회는 `_snapshot_tree` 축이 담당).
    stale_marker = "# STALE ADOPTER COPY (guest engine sync gate)\n"
    compared: dict[str, Path] = {}
    for rel, entry in engine_rows.items():
        source_path = REPO / (getattr(entry, "source_rel", None) or rel)
        dest_path = dest / rel
        if source_path.is_file() and dest_path.is_file():
            pairs = [(dest_path, source_path)]
        elif source_path.is_dir() and dest_path.is_dir():
            pairs = [(dest_path / child.name, child)
                     for child in sorted(source_path.iterdir()) if child.is_file()]
        else:
            pairs = []
        for mirror, source in pairs:
            if not mirror.is_file():
                continue
            compared[mirror.relative_to(dest).as_posix()] = source
            mirror.write_text(stale_marker, encoding="utf-8", newline="\n")
    assert compared, f"{base}->{added}: 대조 가능한 guest 엔진 파일 0(공허 게이트)"

    # 불변이어야 할 축 — 어댑터 렌더물(guest `@render`)과 instance-owned 설정. 전 트리 스냅샷
    #   (`_snapshot_tree`)으로 잡아 "그 경로 아래 아무것도 안 바뀌었다" 를 파일 단위로 확인한다.
    #   `ADD_HARNESS_CREATE_IF_ABSENT` 만 쓰면 claude/opencode 가 빈 집합이라 instance-owned 축이
    #   codex 2쌍에서만 실효한다 — 어느 manifest 에도 없는 실 설정 파일을 **채택자 로컬 내용으로**
    #   심어 6쌍 전부에서 byte 불변을 단언한다(전파 대상 아님을 못박는 축).
    local_config_marker = "// LOCAL ADOPTER CONFIG (must not be touched by update)\n"
    planted = []
    for rel in (".claude/settings.json", ".opencode/opencode.jsonc"):
        target = dest / rel
        if target.is_file():
            target.write_text(local_config_marker, encoding="utf-8", newline="\n")
            planted.append(rel)
    assert planted, f"{base}->{added}: instance-owned 설정 심기 0(공허 축)"
    instance_owned = sorted({*pm_import.ADD_HARNESS_CREATE_IF_ABSENT[added], *planted})

    # 렌더물 축 — guest `@render` 아래 실 파일도 stale 로 만든다. 렌더물은 conf 파생이라 update 가
    #   다시 렌더해야 하고(설치 시점 사본 동결 금지), 손편집은 그때 되돌아간다.
    render_paths = _guest_render_paths(pm_update, dest)
    stale_render = sorted(
        rel for rel in _snapshot_tree(dest)
        if any(rel == p or rel.startswith(p + "/") for p in render_paths)
    )
    assert stale_render, f"{base}->{added}: guest 렌더물 파일 0(공허 축)"
    for rel in stale_render:
        (dest / rel).write_text(stale_marker, encoding="utf-8", newline="\n")
    before = _snapshot_tree(dest)

    def _untouchable(rel: str) -> bool:
        return rel in instance_owned

    assert any(_untouchable(rel) for rel in before), \
        f"{base}->{added}: 불변 축 대상 0(공허 대조)"

    monkeypatch.setattr(pm_update, "REPO", dest)
    monkeypatch.setenv("PM_NONINTERACTIVE", "1")
    monkeypatch.setattr(pm_import, "read_upstream_rev", lambda *a, **k: None)
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)
    assert pm_update.main(["--from", str(REPO)]) == 0, f"{base}->{added}: self-update rc≠0"

    after = _snapshot_tree(dest)
    unsynced = sorted(
        rel for rel, source in compared.items()
        if after.get(rel) != source.read_bytes())
    assert unsynced == [], (
        f"{base}->{added}: guest 엔진 파일이 상류와 수렴하지 않음(영구 동결): {unsynced}")
    changed = sorted(
        rel for rel, payload in before.items()
        if _untouchable(rel) and after.get(rel) != payload)
    assert changed == [], (
        f"{base}->{added}: instance-owned 파일이 update 채널에 변경됨(불가침 위반): {changed}")
    frozen_render = sorted(
        rel for rel in stale_render
        if after.get(rel) == stale_marker.encode("utf-8"))
    assert frozen_render == [], (
        f"{base}->{added}: guest 렌더물이 재렌더를 못 받아 설치 시점 사본으로 동결: {frozen_render}")


def test_add_harness_pairs_are_derived_permutation():
    """add-harness e2e 쌍이 파생 축 HARNESSES 순열(N×(N−1))임을 못박는다 — 손-열거 6쌍 아님(T-0434).

    실축(HARNESSES=3종)은 6쌍이되, 가짜 4번째 하네스 축이면 12쌍으로 자동 확장·4번째가 base·added
    양방향으로 편입된다(T-0429 가짜-하네스 패턴 재사용). production `_add_harness_order_pairs` 를
    그대로 태워 constant 와 test 가 어긋나지 않는다.
    """
    assert _ADD_HARNESS_PAIRS == _add_harness_order_pairs(HARNESSES)
    assert len(_ADD_HARNESS_PAIRS) == len(HARNESSES) * (len(HARNESSES) - 1)
    assert all(base != added for base, added in _ADD_HARNESS_PAIRS)  # 자기쌍 없음
    # 가짜 4번째 하네스 → N×(N−1) 자동 확장·양방향 편입.
    fake = (*HARNESSES, "fourth")
    got = _add_harness_order_pairs(fake)
    assert len(got) == len(fake) * (len(fake) - 1)
    assert ("fourth", HARNESSES[0]) in got and (HARNESSES[0], "fourth") in got


def test_add_harness_ns_bound_is_derived_and_covers_pair_axis():
    """어댑터 네임스페이스 상한이 파생이고 add-harness 쌍 축 전 하네스를 커버함을 못박는다 —
    손-복제였으면 4번째서 KeyError(T-0453 이 마감한 커플링·[[cross-cutting-breaking-blast-radius]]).

    (a) 파생 값이 T-0429 파생원과 일치(shape·값 보존·엔진 ADD_HARNESS_ADAPTER 동형) · (b)
    `_ADD_HARNESS_PAIRS` 의 모든 하네스가 상한에 존재(KeyError 0) · (c) 가짜 4번째 하네스가
    파생원에 추가되면 상한도 자동 편입(T-0434 가짜-하네스 검증 패턴 재사용).
    """
    # (a) 값 보존 — 파생이 엔진 상수와 동형.
    for h in HARNESSES:
        adapter_dirs, root_doc = _ADD_HARNESS_NS_BOUND[h]
        assert adapter_dirs == HARNESS_ADAPTER_DIRS[h]
        assert root_doc == HARNESS_ROOT_DOC[h]
    # (b) 쌍 축 전 하네스가 상한에 존재 — add-harness 파라미터가 KeyError 없이 돈다.
    #     ⚠ load-bearing 은 이 (b)다 — (a)는 파생값을 같은 파생원과 비교라 값-tautological.
    #     실 decay(신규 하네스가 상한에 안 옴)는 (b)가 red 로 잡는다(리뷰어 sensitivity 실측).
    pair_harnesses = {h for pair in _ADD_HARNESS_PAIRS for h in pair}
    assert pair_harnesses <= set(_ADD_HARNESS_NS_BOUND), (
        f"쌍 축 하네스가 네임스페이스 상한에 없음(KeyError 위험): "
        f"{pair_harnesses - set(_ADD_HARNESS_NS_BOUND)}")
    # (c) 가짜 4번째 하네스 → 파생원에 있으면 상한도 자동 편입(손-복제였으면 수동 추가 필요).
    #     adapter_dirs 값 = dir 명 튜플(HARNESS_ADAPTER_DIRS[h] shape) — 예 (".fourth",).
    fake_dirs = {**HARNESS_ADAPTER_DIRS, "fourth": (".fourth",)}
    fake_docs = {**HARNESS_ROOT_DOC, "fourth": "AGENTS.md"}
    fake_bound = _derive_ns_bound(fake_dirs, fake_docs)
    assert fake_bound["fourth"] == ((".fourth",), "AGENTS.md")
    assert set(HARNESSES) < set(fake_bound)


# ── T-0308: fresh opencode 채택자 drift-0 e2e (pm_import↔pm_update 전파 게이트·B-freshadopter) ──
# T-0305 의 self-update e2e(test_pm_update.py)는 ManifestEntry/plan/apply 레벨이다. 이 층은 그와 상보인
# **fresh-adopter 각도**: 실 pm_import 로 opencode 채택자를 만들고 → 엔진(프레임워크) 어댑터/드라이버를
# mutate → 실 pm_update self-update(main())로 채택자에 전파됨을 못박고, 동시에 pm_import 렌더 산출 ==
# pm_update 렌더 산출(drift-0·[[verify-engine-template-propagation]])임을 입증한다. hermetic: 라이브 LLM
# 0(--opencode-model 결정적 치환·models seam 은 pm_import fixture stub)·네트워크 0(read_upstream_rev
# stub·framework 는 비-git)·라이브 하니스 0. [[release-run-all-three-tiers]] 의 machine half — T-0304 라이브
# composite 와 축이 다르다(재현 가능·on-demand 아님).
#
# 채택자가 opencode self-update 를 돌리려면 local.conf 에 harness.opencode.pro_model 이 있어야 한다 — @render 가
# `{{OPENCODE_PRO_MODEL}}` 를 재유도하는데 미보유면 pm_render._assert_no_leak 가 크래시(자족 산출물 위반).
# --opencode-model 결정적 flag 로 import 가 agents 치환 + local.conf 기록 → pm_update 재렌더가 동일
# 리터럴로 해소(drift-0 성립·크래시 0). 라이브 모델 조회 없이 결정적이다.
_OPENCODE_MODEL = "anthropic/claude-test-t0308"


def _build_opencode_framework(tmp_path: Path) -> Path:
    """REPO 로부터 mutable opencode 프레임워크 소스를 만든다 (import + self-update 소스로 재사용).

    opencode 채택자 engine.manifest 가 참조하는 root-상대 경로 전부를 담아 실 프레임워크 루트 레이아웃을
    재현한다: 엔진(`.project_manager/`·tools·wiki methodology + 템플릿·engine.manifest)·`.gitattributes` +
    root `.claude/skills`(PM-workflow 스킬 canonical·bare @render root-sourced) +
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
    #   claude_code 와 동일). opencode self-update의 skill/command 두 채널이 이 canonical을 읽는다.
    shutil.copytree(REPO / ".claude" / "skills", framework / ".claude" / "skills", ignore=ignore)
    shutil.copy2(REPO / ".gitattributes", framework / ".gitattributes")
    return framework


def test_fresh_opencode_adopter_engine_mutate_propagates_and_render_drift0(
        pm_import, pm_update, tmp_path, monkeypatch):
    """fresh opencode import → 엔진 mutate → pm_update self-update 전파 + pm_import↔pm_update 렌더 drift-0.

    (A) 엔진(프레임워크) PM-workflow canonical 스킬(`.claude/skills` @render) + lib 드라이버
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
    assert f"harness.opencode.pro_model={_OPENCODE_MODEL}" in conf, (
        "import 가 harness.opencode.pro_model 을 local.conf 에 기록 안 함 — self-update @render 가 "
        "{{OPENCODE_PRO_MODEL}} 를 재유도 못 해 _assert_no_leak 크래시(drift-0 전제 붕괴).")

    # self-update dest = self-location(pm_update.REPO) → 채택자로 고정. source = --from framework(명시).
    monkeypatch.setattr(pm_update, "REPO", dest)

    def _self_update() -> int:
        return pm_update.main(["--from", str(framework)])

    # 전파 대상 스냅샷: `.opencode`(agents/command/lib/plugins/pm_orch) +
    # `.claude/skills`(모델 skill tool) 두 채널 전부 — 공허참 방지 sanity 포함.
    before = _snapshot_tree(dest / ".opencode")
    before_skills = _snapshot_tree(dest / ".claude" / "skills")
    assert any(r.startswith("agents/") for r in before), \
        "opencode 어댑터 스냅샷에 agents 부재(공허 테스트?)"
    assert "lib/ctx-guard-core.cjs" in before, \
        "engine-mirror driver(ctx-guard-core.cjs) 스냅샷 부재 — T-0305 lib 전파 대상 확인 불가"
    assert "command/pm-env.md" in before, \
        "opencode 슬래시 command 스냅샷 부재(T-0674 전파 표면 확인 불가)"
    assert "pm-env/SKILL.md" in before_skills, \
        "PM-workflow 스킬 스냅샷에 pm-env/SKILL.md 부재(skill tool 채널 미출하·공허 테스트?)"

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
    skill_src.write_text(skill_src.read_text(encoding="utf-8") + f"\n<!-- {sentinel} -->\n", encoding="utf-8", newline="\n")
    lib_src.write_text(lib_src.read_text(encoding="utf-8") + f"\n// {sentinel}\n", encoding="utf-8", newline="\n")

    assert _self_update() == 0, "엔진 mutate 후 self-update 가 rc0 아님"
    skill_dest = (dest / ".claude" / "skills" / "pm-env" / "SKILL.md").read_text(encoding="utf-8")
    command_dest = (dest / ".opencode" / "command" / "pm-env.md").read_text(encoding="utf-8")
    lib_dest = (dest / ".opencode" / "lib" / "ctx-guard-core.cjs").read_text(encoding="utf-8")
    assert sentinel in skill_dest, (
        "엔진 PM-workflow canonical 스킬(`.claude/skills` @render) 변경이 채택자 `.claude/skills` 로 "
        "전파 안 됨 (전파 채널 끊김·ADR-0065)")
    assert sentinel in command_dest and command_dest == _expected_opencode_command(
        skill_dest, "pm-env"
    ), (
        "canonical 스킬 변경이 `.opencode/command/pm-env.md`로 갱신되지 않았거나 "
        "skill↔command 렌더 산출이 drift(T-0674).")
    assert sentinel in lib_dest, (
        "엔진 lib 드라이버(engine-mirror·T-0305 hook/driver 전파화) 변경이 채택자 `.opencode/lib` 로 "
        "전파 안 됨 (hook/driver 미도달·frozen 재발)")

    # (D) CRLF 체크아웃 채택자에서도 drift-0 (T-0709). Windows 는 `core.autocrlf=true` 라 상류
    #     checkout 과 채택자 트리가 **둘 다** CRLF 다(`git ls-files --eol` = `i/lf w/crlf`).
    #     옛 코드는 렌더 판정을 bytes 로 재고(표기만 달라도 '변경') LF 로 되써, 같은 소스인데
    #     매 sync 가 전파 트리를 통째로 재기록했다(채택자 워킹트리 전면 diff). 판정은 개행
    #     정규화 후, 쓰기는 표기 보존이어야 byte-copy 채널과 렌더 채널이 함께 무변경이 된다.
    # `.local/` 은 per-clone 런타임 산출(읽기전용 사본 포함)이라 체크아웃 표기 축이 아니다.
    crlf_skip = (*_SNAPSHOT_EXCLUDE_PARTS, ".local")
    for root in (framework, dest):
        for path in sorted(root.rglob("*")):
            if (path.is_file() and path.suffix in _CRLF_CHECKOUT_SUFFIXES
                    and not any(part in crlf_skip for part in path.parts)):
                _to_crlf(path)
    crlf_before = {
        path: path.read_bytes()
        for root in (dest / ".opencode", dest / ".claude" / "skills")
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }
    assert crlf_before, "CRLF 로 바꾼 전파 파일 0 — 공허 게이트"
    assert any(_stray_lf(payload) == 0 and b"\r\n" in payload
               for payload in crlf_before.values()), "픽스처가 CRLF 를 못 만들었다(공허 게이트)"
    assert _self_update() == 0, "CRLF 채택자 self-update 가 rc0 아님"
    crlf_drift = sorted(
        path.relative_to(dest).as_posix()
        for path, payload in crlf_before.items() if path.read_bytes() != payload)
    assert crlf_drift == [], (
        "CRLF 채택자에서 self-update 가 전파 트리를 되썼다(표기만 다른 파일을 '변경'으로 판정 "
        f"또는 LF 로 되쓰기): {crlf_drift}")
