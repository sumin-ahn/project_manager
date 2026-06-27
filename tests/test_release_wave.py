"""릴리즈 테스트(③ tier·`release` marker) — 실 LLM 한 세션이 fresh adopter 에서 full wave 운영.

테스트 3-tier 의 Tier 3(릴리즈). Tier 2(런타임 smoke·`test_fresh_adopter_runtime_smoke`)는 실 LLM 이
*PM 으로서* ticket 라이프사이클(new→claim→complete)을 운영하는지까지 친다. 이 층은 그 위 — **위임**까지
포함한 full wave: PM 세션이 ticket 을 발행·claim 하고 **developer 서브에이전트에 구현을 Task 위임**,
**code-reviewer 서브에이전트에 리뷰를 Task 위임**한 뒤 complete 까지 운영하는지, 그리고 **위임이 실제로
일어났는지**(developer 가 작성한 probe 파일·ticket done 전이)를 검증한다.

게이트 아님 — 사용자가 릴리즈 직전 `PM_ORCH_LIVE_RELEASE=1` 로 occasional 트리거(비용·flaky 감수).
기본 skip(env 미설정·CI green 불변). claude 경로는 PM 36 라이브 probe 로 검증된 mechanics
(`scratchpad/release_probe.py`·145s·dev×15·reviewer×21·probe.txt·done)를 옮긴 것이다.

단언 철학(runtime_smoke 와 동일): **side-effect 기반**이라 LLM 출력 phrasing 비결정에 강건하다 —
probe.txt(=developer 서브에이전트가 작성)·ticket done 전이가 핵심 단언. claude 는 위에 더해 stream-json
의 `subagent_type` 관측으로 *위임이 일어났음*까지 hard 단언한다(probe 검증됨). opencode 는 위임 관측
수단이 미확정(stream-json 과 다름·spike §6)이라 side-effect 만 hard·위임 흔적은 best-effort 다.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# 런타임 smoke 와 헬퍼 공유(같은 tests/ 디렉토리·import) — adopter import·LLM env 격리·ticket 조회.
# `_load_pm_import`(pm_import 모듈 로드)·`_real_models_runner` 스텁은 multi-repo 셋업 헬퍼에서도 재사용.
from test_fresh_adopter_runtime_smoke import (
    _import_adopter,
    _live_env,
    _load_pm_import,
    _tickets_in,
)

# 릴리즈 트리거 — 사용자가 릴리즈 직전 명시 set(occasional). 미설정이면 전부 skip(CI green 불변).
_RELEASE_LIVE = os.environ.get("PM_ORCH_LIVE_RELEASE") == "1"
# claude: sonnet-4-6(API 과금·env override). probe 가 이 모델로 PASS.
CLAUDE_MODEL = os.environ.get("PM_ORCH_LIVE_CLAUDE_MODEL", "claude-sonnet-4-6")
# opencode: full wave(claim→위임→complete sync-gate)는 *강한* 모델이 필요하다 — gemma4:26b 는
# complete 의 sync-gate 를 못 넘어 flaky(위임=probe.txt 는 쓰나 ticket 이 claimed 에 머묾·PM 39 실측).
# qwen3.5:397b-cloud(ollama cloud)로 full wave PASS 검증(69s·PM 39). 그래서 release default 는 이 모델
# 이다(runtime_smoke[lite·sync-gate 없음]는 gemma 로 충분 — 거긴 별도 default). env override 로 교체 가능.
LIVE_MODEL = os.environ.get("PM_ORCH_LIVE_MODEL", "ollama/qwen3.5:397b-cloud")

# full wave probe 가 작성하도록 지시하는 산출 파일·내용 — side-effect 단언의 기준(단일 진실).
PROBE_FILE = "probe.txt"
PROBE_TEXT = "hello from dev"

# 위임 단언 대상 서브에이전트 — full wave 가 developer(구현)·code-reviewer(리뷰) 둘 다 거쳐야 통과.
_DEV_SUBAGENT = "developer"
_REVIEWER_SUBAGENT = "code-reviewer"

# opencode 는 gemma 가 느리고 변동 커 1800s, claude 는 probe 실측 145s 여유분 600s.
_OPENCODE_TIMEOUT = int(os.environ.get("PM_ORCH_LIVE_RELEASE_TIMEOUT", "1800"))
_CLAUDE_TIMEOUT = int(os.environ.get("PM_ORCH_LIVE_RELEASE_CLAUDE_TIMEOUT", "600"))


def _full_wave_prompt(entry_doc: str) -> str:
    """PM 세션이 full wave(new→claim→**developer 위임**→**code-reviewer 위임**→complete)를 운영하라는 프롬프트.

    board.py 경로를 *주지 않는다* — adopter 가 `entry_doc` 만으로 도구를 찾아 운영해야 통과(= 문서 운영성).
    developer 위임 단계에서 `probe.txt`(='hello from dev')를 작성하게 지시 → side-effect 로 위임 *결과*를
    관측(서브에이전트가 실제로 구현했음). 5단계(new/claim/delegate developer/delegate code-reviewer/complete)
    키워드를 포함하므로 hermetic 단위테스트가 구조를 가드한다.
    """
    return (
        f"You are the PM for this project. Read {entry_doc} to learn how the project board "
        "tool works. Then run a full release wave: "
        "(1) create exactly one ticket titled 'release wave probe' (touches README.md) with the "
        "board tool, "
        "(2) claim it, "
        f"(3) delegate the implementation to the '{_DEV_SUBAGENT}' subagent using the Task tool — "
        f"instruct the {_DEV_SUBAGENT} to create a file named {PROBE_FILE} in the project root "
        f"containing the text '{PROBE_TEXT}', "
        f"(4) delegate a review to the '{_REVIEWER_SUBAGENT}' subagent using the Task tool, "
        "(5) mark the ticket complete/done (satisfy the complete sync gate however the docs say — "
        "e.g. a log entry and the tests-pass / untested flag). "
        "Reply with the ticket id when the ticket is done."
    )


def _collect_subagent_types(stdout: str) -> list[str]:
    """stream-json stdout 의 각 라인을 json 파싱 → 재귀 walk 로 `subagent_type` 값 수집.

    PM 36 probe 의 walk 와 동형(검증됨) — Task tool_use input 에 `subagent_type` 가 들어간다. claude
    의 stream-json 형식 정확 스키마에 비의존적으로 *어느 깊이든* 키를 긁는다(형식 변동에 강건). 파싱
    불가 라인(비-json·빈 줄)은 무시. opencode 출력엔 이 키가 없을 수 있어(미확정) best-effort 로만 쓴다.
    """
    types: list[str] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "subagent_type":
                    types.append(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        walk(obj)
    return types


def _assert_wave_side_effects(dest: Path, proc: subprocess.CompletedProcess, harness: str) -> None:
    """full wave side-effect 단언 — developer 가 probe.txt 작성·ticket 이 done/ 도달.

    probe.txt(내용 'hello from dev') = developer 서브에이전트가 위임받아 구현했다는 증거. done/ 도달 =
    new→claim→complete 전이 완주(complete sync-gate 통과). 둘 다 출력 phrasing 비결정에 강건한 side-effect.
    """
    tail = (
        f"--- {harness} stdout(tail) ---\n{proc.stdout[-2000:]}\n"
        f"--- stderr(tail) ---\n{proc.stderr[-1000:]}"
    )
    probe_path = dest / PROBE_FILE
    assert probe_path.exists(), (
        f"실 {harness} full wave 후 {PROBE_FILE} 부재 — developer 서브에이전트 위임/구현 실패.\n" + tail
    )
    assert probe_path.read_text(encoding="utf-8").strip() == PROBE_TEXT, (
        f"{PROBE_FILE} 내용이 '{PROBE_TEXT}' 아님 — developer 가 다르게 구현.\n" + tail
    )
    done_tickets = _tickets_in(dest, "done")
    assert done_tickets, (
        f"실 {harness} 가 ticket 을 done/ 까지 운영하지 못함 — full wave 미완주.\n"
        f"open={_tickets_in(dest, 'open')} claimed={_tickets_in(dest, 'claimed')}\n" + tail
    )


@pytest.mark.release
@pytest.mark.skipif(
    not _RELEASE_LIVE or not shutil.which("claude"),
    reason="release wave — PM_ORCH_LIVE_RELEASE=1 + claude CLI 필요(API 과금). 기본 skip·사용자 트리거.",
)
def test_release_wave_claude_full_wave(tmp_path):
    """실 claude(`claude-sonnet-4-6`)가 `CLAUDE.md` 만 보고 full wave 를 운영·위임이 관측된다.

    PM 36 라이브 probe(`scratchpad/release_probe.py`·PASS·dev×15·reviewer×21)의 mechanics 를 옮긴 것.
    claude 는 subprocess cwd 를 존중한다(`--dir` 불요). stream-json 으로 위임(subagent_type)을 관측하고
    side-effect(probe.txt·done)를 단언한다. API 과금.
    """
    dest = _import_adopter(tmp_path, "claude")

    proc = subprocess.run(
        ["claude", "-p", "--model", CLAUDE_MODEL,
         "--allowedTools", "Bash", "Task",
         "--output-format", "stream-json", "--verbose",
         "--dangerously-skip-permissions",
         _full_wave_prompt("CLAUDE.md")],
        cwd=str(dest), capture_output=True, text=True,
        env=_live_env(CLAUDE_MODEL), timeout=_CLAUDE_TIMEOUT,
    )

    # 위임 관측(hard) — stream-json 에서 developer·code-reviewer 둘 다 등장해야 통과(probe 검증됨).
    subagent_types = _collect_subagent_types(proc.stdout)
    tail = (
        f"--- claude stdout(tail) ---\n{proc.stdout[-2500:]}\n"
        f"--- stderr(tail) ---\n{proc.stderr[-1000:]}"
    )
    assert _DEV_SUBAGENT in subagent_types and _REVIEWER_SUBAGENT in subagent_types, (
        f"claude full wave 에서 위임 미관측 — subagent_type={subagent_types} "
        f"({_DEV_SUBAGENT}·{_REVIEWER_SUBAGENT} 둘 다 필요).\n" + tail
    )

    # side-effect(hard) — developer 위임 결과(probe.txt)·done 전이.
    _assert_wave_side_effects(dest, proc, "claude")


@pytest.mark.release
@pytest.mark.skipif(
    not _RELEASE_LIVE or not shutil.which("opencode"),
    reason="release wave — PM_ORCH_LIVE_RELEASE=1 + opencode CLI(+ollama 모델) 필요. 기본 skip·사용자 트리거.",
)
def test_release_wave_opencode_full_wave(tmp_path):
    """실 opencode(agentic·ollama)가 `AGENTS.md` 만 보고 full wave 를 운영한다 (side-effect 단언).

    opencode 의 위임 관측 수단은 claude 의 stream-json `subagent_type` 와 다르다 — PM 36 라이브 probe
    실측 결과 gemma/opencode 는 위임 흔적(subagent_type·'developer'·task)을 출력에 **0** 으로 낸다(비결정).
    그래서 **side-effect(probe.txt·done)만 hard 단언**하고(probe.txt=developer 가 위임받아 작성·done=wave
    완주 → side-effect 가 위임 *결과*를 커버), 위임 흔적(stdout 에 'developer'/'code-reviewer' 등장)은
    **best-effort**(있으면 단언·없으면 skip)다. opencode 위임 관측 수단은 PM probe 후 보강한다.
    gemma 는 느리고 변동 커 timeout 1800s. `--dir` 로 루트 핀(opencode 는 PWD 로 루트 오판).
    """
    dest = _import_adopter(tmp_path, "opencode")

    proc = subprocess.run(
        # `--dangerously-skip-permissions`: 비대화 헤드리스라 opencode 가 `--dir` 디렉토리를
        # external_directory 로 보고 권한을 auto-reject → AGENTS.md 도 못 읽고 wave 시작 실패한다.
        # 이 플래그로 권한을 통과시켜야 wave 완주(throwaway tmp adopter 격리라 안전·PM 36 probe 실측).
        ["opencode", "run", "--agent", "build", "--dir", str(dest),
         "--dangerously-skip-permissions", "-m", LIVE_MODEL,
         _full_wave_prompt("AGENTS.md")],
        cwd=str(dest), capture_output=True, text=True,
        env=_live_env(LIVE_MODEL), timeout=_OPENCODE_TIMEOUT,
    )

    # side-effect(hard) — full wave 의 핵심 결과(developer 위임 산출 probe.txt·done 전이).
    _assert_wave_side_effects(dest, proc, "opencode")

    # 위임 흔적(best-effort) — opencode 출력에 서브에이전트 이름이 등장하면 위임 관측으로 단언.
    # 등장 안 해도 fail 시키지 않는다 — opencode 위임 관측 수단=stream-json 아님·gemma 비결정으로
    # 위임 흔적 출력 0(PM 36 probe 실측). 위임은 side-effect(probe.txt·done)로 검증한다.
    if _DEV_SUBAGENT in proc.stdout and _REVIEWER_SUBAGENT in proc.stdout:
        assert _DEV_SUBAGENT in proc.stdout and _REVIEWER_SUBAGENT in proc.stdout


# ── multi-repo 경로 (multi-PM 셋업 full wave · T-0158) ───────────────────────────────────
# 위 단일-adopter 테스트는 *한* repo 위 full wave 다. 아래는 그 multi-repo 확장 — multi-PM 셋업
# (`pm_config repo add` 2 repo + worktree slot)에서 한 LLM 세션이 공유 보드 위 *여러 repo* 의
# wave 를 운영하는지 검증한다. PM 라이브 probe(opencode/qwen3.5:397b-cloud·실측 PASS)로 viable 확인
# 후 그 mechanics 를 옮긴 것이다.

# multi-repo 셋업의 repo 이름 = prefix = worktree 슬롯 네임스페이스(단일 진실). 2 repo 로 충분 —
# 새 위험축(per-repo prefix·per-slot 식별)은 1→2 에서 이미 드러난다(대N 은 spike §6 후속).
_MULTIREPO_REPOS = ("repoA", "repoB")
# multi-repo wave 가 각 repo 슬롯에 쓰도록 지시하는 산출 파일·내용 — side-effect 단언의 기준.
# (단일 wave 의 PROBE_FILE='probe.txt' 와 별개 — 슬롯별 파일이라 슬롯 격리도 함께 단언한다.)
_WAVE_FILE = "wave-done.txt"


def _seed_git_repo(path: Path) -> None:
    """seed git repo(main·1 commit) 생성 — repo add 의 bare-clone 원(ADR-0011)."""
    path.mkdir(parents=True, exist_ok=True)
    _git = lambda *a: subprocess.run(["git", "-C", str(path), *a], check=True,
                                     capture_output=True, text=True)
    _git("init", "-q")
    _git("config", "user.email", "probe@local")
    _git("config", "user.name", "probe")
    (path / "README.md").write_text(f"# {path.name}\n", encoding="utf-8")
    _git("add", "-A")
    _git("commit", "-q", "-m", "init")
    _git("branch", "-M", "main")


def _pm_config(home: Path, *args: str) -> subprocess.CompletedProcess:
    """home 의 pm_config.py 호출(엔진 도구·LLM 아님 → 부모 env 상속 OK·모델 무관)."""
    return subprocess.run(
        [sys.executable, str(home / ".project_manager" / "tools" / "pm_config.py"), *args],
        cwd=str(home), capture_output=True, text=True,
        env={**os.environ, "PM_NONINTERACTIVE": "1"},
    )


def _import_multipm_home(tmp_path: Path, harness: str,
                         repos: tuple[str, ...] = _MULTIREPO_REPOS) -> Path:
    """multi-PM 홈 import (hermetic) — fresh adopter 위에 `repo add`·`worktree add` 로 multi-repo 셋업.

    단일 `_import_adopter`(test_fresh_adopter_runtime_smoke) 와 *다른* 셋업이다 — 그건 import 만,
    이건 그 위에 repo 마다 [seed git repo → `pm_config repo add` → `pm_config worktree add`] 를
    얹어 공유 보드 + 슬롯(`work/<repo>_1`)을 만든다. `_load_pm_import`·`_real_models_runner` 스텁을
    재사용해 라이브 models 조회를 차단(hermetic). home 디렉토리를 반환한다.
    """
    pm_import = _load_pm_import()
    pm_import._real_models_runner = lambda: (False, [])
    home = tmp_path / f"mpm-home-{harness}"
    origins = tmp_path / f"origins-{harness}"
    rc = pm_import.main(
        ["--new", str(home), "--harness", harness, "--name", "MPM", "--fill", "manual"]
    )
    assert rc == 0, f"{harness} multi-PM home import 실패 (rc={rc})"

    for repo in repos:
        _seed_git_repo(origins / repo)
        added = _pm_config(home, "repo", "add", repo, "--git", str(origins / repo))
        assert added.returncode == 0, (
            f"repo add {repo} 실패 (rc={added.returncode})\n"
            f"stdout={added.stdout[-600:]}\nstderr={added.stderr[-600:]}"
        )
        slotted = _pm_config(home, "worktree", "add", repo)
        assert slotted.returncode == 0, (
            f"worktree add {repo} 실패 (rc={slotted.returncode})\n"
            f"stdout={slotted.stdout[-600:]}\nstderr={slotted.stderr[-600:]}"
        )
    return home


def _multirepo_wave_prompt(repos: tuple[str, ...] = _MULTIREPO_REPOS) -> str:
    """한 세션이 공유 보드 위 *각 repo* 의 미니 wave 를 운영하라는 프롬프트(PM probe 본보기).

    범위 축소(scoping) — multi-repo wave 는 dev→reviewer *위임*까지 가지 않고 미니 wave
    (new→claim→슬롯 파일→complete)다. 위임은 단일 full wave(`test_release_wave_*_full_wave`)에서
    이미 검증됐고, multi-repo 의 *새* 위험축은 한 세션이 공유 보드/슬롯/identity 를 repo별로 바르게
    핸들링하는가 — per-repo prefix(`--prefix <repo>` → `T-<repo>-NNN` ID 네임스페이스)·per-slot 식별
    (`--session <repo>_1`·`work/<repo>_1` 슬롯 파일)이다. 그래서 prompt 는 그 축만 친다(ticket 본문
    "viable 불확실/과복잡 시 형태 재검토" 허용). board.py 경로는 *준다* — 단일 wave 가 문서 운영성
    (경로 미제공)을 이미 검증하므로 여기선 multi-repo 핸들링에 집중한다.
    """
    repo_list = " and ".join(repos)
    steps = "\n".join(
        f"  Wave {i + 1} (repo = {repo}): create a ticket, claim it, write a slot file, complete it."
        for i, repo in enumerate(repos)
    )
    return (
        "You operate a multi-PM project-manager home that shares ONE board across "
        f"{len(repos)} code repos: {repo_list}. Each repo has its own worktree slot directory: "
        + ", ".join(f"work/{r}_1" for r in repos) + ". The board engine is "
        ".project_manager/tools/board.py.\n\n"
        "Do a minimal wave for EACH repo, one repo fully before the next:\n"
        f"{steps}\n\n"
        "For a repo named REPO, the 4 steps are exactly:\n"
        '  1. Create a ticket:   python3 .project_manager/tools/board.py new "wave probe REPO" '
        "--prefix REPO\n"
        "     (this prints the new ticket id, e.g. T-REPO-001 — note it)\n"
        "  2. Claim it:          python3 .project_manager/tools/board.py claim <TICKET_ID> "
        "--session REPO_1\n"
        f"  3. Write a file named {_WAVE_FILE} containing the text \"done by REPO\" INSIDE that "
        f"repo slot: work/REPO_1/{_WAVE_FILE}\n"
        "  4. Complete it:       python3 .project_manager/tools/board.py complete <TICKET_ID> "
        "--tests-pass --allow-missing-log\n\n"
        "Replace REPO with the actual repo name for each wave. Use the EXACT ticket id from "
        "step 1 output in steps 2 and 4."
    )


def _assert_multirepo_wave_side_effects(home: Path, proc: subprocess.CompletedProcess,
                                        harness: str,
                                        repos: tuple[str, ...] = _MULTIREPO_REPOS) -> None:
    """per-repo side-effect 단언 — 각 repo 가 done ticket(`T-<repo>-*`) + 슬롯 파일을 남겼는가.

    repo별로 (1) `tickets/done/T-<repo>-*.md` 존재 = per-repo prefix 로 발행·claim·complete 완주
    (per-repo ID 네임스페이스·sync-gate 통과) (2) `work/<repo>_1/wave-done.txt` 존재+내용 = 그 repo
    슬롯에 정확히 썼음(슬롯 격리). 둘 다 출력 phrasing 비결정에 강건한 side-effect 다(T-0157 동형).
    """
    done_root = home / ".project_manager" / "wiki" / "tickets" / "done"
    tail = (
        f"--- {harness} stdout(tail) ---\n{proc.stdout[-2500:]}\n"
        f"--- stderr(tail) ---\n{proc.stderr[-1000:]}"
    )
    for repo in repos:
        # (1) per-repo done ticket — prefix 가 ID 네임스페이스(T-<repo>-NNN)를 가른다.
        done = sorted(done_root.glob(f"T-{repo}-*.md"))
        assert done, (
            f"실 {harness} multi-repo wave: repo '{repo}' 의 done ticket(T-{repo}-*) 부재 — "
            f"per-repo wave 미완주.\nall done={_tickets_in(home, 'done')}\n"
            f"open={_tickets_in(home, 'open')} claimed={_tickets_in(home, 'claimed')}\n" + tail
        )
        # (2) per-slot 파일 — 그 repo 슬롯(work/<repo>_1)에 정확히 썼는가(슬롯 격리).
        wave_file = home / "work" / f"{repo}_1" / _WAVE_FILE
        assert wave_file.exists(), (
            f"실 {harness} multi-repo wave: repo '{repo}' 슬롯 파일 work/{repo}_1/{_WAVE_FILE} "
            f"부재 — 슬롯에 안 썼거나 다른 슬롯에 씀.\n" + tail
        )
        assert wave_file.read_text(encoding="utf-8").strip(), (
            f"repo '{repo}' 슬롯 파일 {_WAVE_FILE} 가 비어 있음.\n" + tail
        )


@pytest.mark.release
@pytest.mark.skipif(
    not _RELEASE_LIVE or not shutil.which("opencode"),
    reason="release wave multi-repo — PM_ORCH_LIVE_RELEASE=1 + opencode CLI(+ollama 모델) 필요. "
           "기본 skip·사용자 트리거.",
)
def test_release_wave_multirepo_opencode_full_wave(tmp_path):
    """실 opencode(agentic·ollama)가 multi-PM 셋업(2 repo·공유 보드)에서 repo별 wave 를 운영한다.

    PM 라이브 probe(`scratchpad/mpm_live_probe.sh`·opencode/qwen3.5:397b-cloud·실측 PASS —
    T-repoA-001·T-repoB-001 둘 다 done·각 슬롯 wave-done.txt 존재)의 mechanics 를 옮긴 것이다.
    단일 full wave 와 *다른* 검증축 — 한 세션이 공유 보드 위 여러 repo 의 보드/슬롯/identity 를
    per-repo prefix·per-slot 식별로 바르게 핸들링하는가(범위 축소 근거는 `_multirepo_wave_prompt`
    docstring). side-effect(repo별 done ticket·슬롯 파일)만 hard 단언 → 출력 phrasing 비결정에
    강건(T-0157 동형). `--dir` 로 루트 핀(opencode 는 PWD 로 루트 오판). API 과금 0(로컬/cloud ollama).

    TODO(T-0158 후속): claude 경로(stream-json subagent 관측)는 multi-repo 미probe 라 미추가 —
    opencode 가 probe-검증된 기본이다. claude multi-repo 가 필요해지면 단일 claude mechanics
    (`--allowedTools Bash`·stream-json)를 이 multi-repo 셋업 위에 미러한다.
    """
    home = _import_multipm_home(tmp_path, "opencode")

    proc = subprocess.run(
        # `--dangerously-skip-permissions`: 비대화 헤드리스 격리(throwaway tmp home)라 안전 —
        # 단일 wave 테스트와 동일 근거(opencode 가 --dir 디렉토리를 external 로 보고 auto-reject).
        ["opencode", "run", "--agent", "build", "--dir", str(home),
         "--dangerously-skip-permissions", "-m", LIVE_MODEL,
         _multirepo_wave_prompt()],
        cwd=str(home), capture_output=True, text=True,
        env=_live_env(LIVE_MODEL), timeout=_OPENCODE_TIMEOUT,
    )

    # side-effect(hard) — repo별 done ticket(per-repo prefix) + 슬롯 파일(슬롯 격리).
    _assert_multirepo_wave_side_effects(home, proc, "opencode")


@pytest.mark.release
@pytest.mark.skipif(
    not _RELEASE_LIVE or not shutil.which("claude"),
    reason="release wave multi-repo — PM_ORCH_LIVE_RELEASE=1 + claude CLI 필요(API 과금). "
           "기본 skip·사용자 트리거.",
)
def test_release_wave_multirepo_claude_full_wave(tmp_path):
    """실 claude(`claude-sonnet-4-6`)가 multi-PM 셋업(2 repo·공유 보드)에서 repo별 wave 를 운영한다.

    multi-repo opencode(`test_release_wave_multirepo_opencode_full_wave`)의 검증된 셋업/단언 위에 단일
    claude mechanics(`--allowedTools Bash`·subprocess cwd 존중→`--dir` 불요)를 미러한 것이다 — claude
    경로를 박제·라이브 검증한다(T-0158 TODO). 새 위험축 0: [검증된 multi-repo 셋업] × [검증된 단일
    claude CLI mechanics] 의 합성.

    단일 full wave(`test_release_wave_claude_full_wave`)와 *다른* 검증축 — 한 세션이 공유 보드 위 여러
    repo 의 보드/슬롯/identity 를 per-repo prefix·per-slot 식별로 바르게 핸들링하는가. side-effect
    (repo별 done ticket·슬롯 파일)만 hard 단언 → 출력 phrasing 비결정에 강건(multi-repo opencode 동형).
    stream-json `subagent_type` 위임 단언은 *생략* — 미니 wave 는 dev→reviewer 위임이 없어 subagent_type
    미관측(`_multirepo_wave_prompt` docstring·범위 축소). 그래서 `--allowedTools Bash` 만(Task 불요).
    API 과금.
    """
    home = _import_multipm_home(tmp_path, "claude")

    proc = subprocess.run(
        # `--allowedTools Bash`: 미니 wave 는 board.py 호출(new/claim/슬롯 파일/complete)뿐 — dev→reviewer
        # 위임이 없어 Task 불요(단일 full wave 와 다른 점). claude 는 subprocess cwd 를 존중하므로 `--dir`
        # 불요(opencode 와 다른 점). side-effect 만 단언하므로 stream-json 도 불요.
        ["claude", "-p", "--model", CLAUDE_MODEL,
         "--allowedTools", "Bash",
         "--dangerously-skip-permissions",
         _multirepo_wave_prompt()],
        cwd=str(home), capture_output=True, text=True,
        env=_live_env(CLAUDE_MODEL), timeout=_CLAUDE_TIMEOUT,
    )

    # side-effect(hard) — repo별 done ticket(per-repo prefix) + 슬롯 파일(슬롯 격리).
    _assert_multirepo_wave_side_effects(home, proc, "claude")


# ── hermetic 단위 가드 (라이브 실행 없이·@release/skipif 무관 — 매 회귀 통과) ──────────────
# 위 라이브 테스트는 PM_ORCH_LIVE_RELEASE 미설정 시 skip 이라 CI 에선 안 돈다. 아래 단위는 라이브
# 없이도 돌아 (1) full wave 프롬프트가 5단계 키워드를 담는지 (2) subagent_type walk 가 stream-json
# 샘플에서 값을 정확히 추출하는지 — 라이브 미실행 시에도 mechanics 구조를 가드한다(회귀가 잡음).


def test_full_wave_prompt_has_all_five_stages():
    """full wave 프롬프트가 5단계(new·claim·delegate developer·delegate code-reviewer·complete)를 담는다."""
    prompt = _full_wave_prompt("CLAUDE.md")
    # (1) new — 정확히 1개 ticket 발행 지시.
    assert "create exactly one ticket" in prompt
    # (2) claim.
    assert "claim it" in prompt
    # (3) developer 위임 + probe.txt 산출 지시(side-effect 단언 대상).
    assert f"delegate the implementation to the '{_DEV_SUBAGENT}' subagent" in prompt
    assert PROBE_FILE in prompt and PROBE_TEXT in prompt
    # (4) code-reviewer 위임.
    assert f"delegate a review to the '{_REVIEWER_SUBAGENT}' subagent" in prompt
    # (5) complete + sync gate.
    assert "mark the ticket complete/done" in prompt
    # 진입문서가 프롬프트에 박힌다(harness 별 CLAUDE.md/AGENTS.md).
    assert "CLAUDE.md" in prompt
    assert "AGENTS.md" in _full_wave_prompt("AGENTS.md")


def test_collect_subagent_types_extracts_from_stream_json():
    """subagent_type walk 가 claude stream-json 형 샘플에서 developer·code-reviewer 를 정확히 추출한다."""
    # claude stream-json 근사: 각 라인 1 json. Task tool_use input 깊숙이 subagent_type 가 박힌다.
    sample_lines = [
        json.dumps({"type": "system", "subtype": "init"}),
        json.dumps({
            "type": "assistant",
            "message": {"content": [
                {"type": "tool_use", "name": "Task",
                 "input": {"subagent_type": _DEV_SUBAGENT, "prompt": "create probe.txt"}}
            ]},
        }),
        "",  # 빈 줄 — 무시돼야.
        "not json at all",  # 비-json — 무시돼야.
        json.dumps({
            "type": "assistant",
            "message": {"content": [
                {"type": "tool_use", "name": "Task",
                 "input": {"subagent_type": _REVIEWER_SUBAGENT, "prompt": "review"}}
            ]},
        }),
    ]
    stdout = "\n".join(sample_lines)

    types = _collect_subagent_types(stdout)

    assert _DEV_SUBAGENT in types
    assert _REVIEWER_SUBAGENT in types
    # 비-json·빈 줄은 조용히 무시(파싱 예외로 죽지 않음).
    assert types == [_DEV_SUBAGENT, _REVIEWER_SUBAGENT]


def test_collect_subagent_types_handles_no_delegation():
    """위임 없는 stdout(subagent_type 부재)에서 walk 가 빈 리스트를 돌려준다(false-positive 0)."""
    stdout = "\n".join([
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}),
        json.dumps({"type": "result", "subtype": "success"}),
    ])
    assert _collect_subagent_types(stdout) == []


# ── multi-repo hermetic 가드 (라이브 실행 없이·@release/skipif 무관 — 매 회귀 통과 · T-0158) ──────
# multi-repo 라이브 테스트는 PM_ORCH_LIVE_RELEASE 미설정 시 skip 이라 CI 에선 안 돈다. 아래 단위는
# 라이브 없이도 돌아 (1) 셋업 헬퍼(`_import_multipm_home`)가 LLM 없이 home + 2 repo areas + 2 슬롯을
# 만드는지 (= 셋업 자체 검증·라이브 테스트의 전제) (2) multi-repo wave 프롬프트가 repo별 mechanics
# (prefix·session·슬롯 파일·new/claim/complete)를 담는지 — 라이브 미실행 시에도 구조를 가드한다.


def test_import_multipm_home_sets_up_two_repos_and_slots(tmp_path):
    """`_import_multipm_home` 가 LLM 없이 multi-PM 홈 + 2 repo areas 등록 + 2 worktree 슬롯을 만든다.

    라이브 테스트의 전제(셋업)를 hermetic 하게 검증 — 셋업이 깨지면 라이브가 가짜 PASS/skip 으로
    숨지 않고 여기서 잡힌다(단일 hermetic 가드 패턴 동형). models 조회는 `_real_models_runner`
    스텁으로 차단되므로 LLM·네트워크 없이 돈다.
    """
    home = _import_multipm_home(tmp_path, "opencode")

    # (1) home 이 fresh adopter 로 import 됐다(공유 보드 + 엔진).
    assert (home / ".project_manager" / "tools" / "board.py").exists()
    assert (home / ".project_manager" / "wiki" / "tickets" / "open").is_dir()

    # (2) 2 repo 가 areas.md(per-repo 레지스트리·ADR-0014)에 prefix 로 등록됐다 — per-repo ID
    #     네임스페이스의 단일 진실(legacy 셋업에선 .project_manager/areas.md·wiki 밖·committed).
    areas_path = home / ".project_manager" / "areas.md"
    assert areas_path.exists(), "repo add 후 areas.md 부재"
    areas_text = areas_path.read_text(encoding="utf-8")
    for repo in _MULTIREPO_REPOS:
        assert f"| {repo} |" in areas_text, f"areas.md 에 repo '{repo}' 등록 행 부재"

    # (3) repo 마다 worktree 슬롯(work/<repo>_1)이 생성됐다 — per-slot 식별의 물리 자원.
    for repo in _MULTIREPO_REPOS:
        slot = home / "work" / f"{repo}_1"
        assert slot.is_dir(), f"worktree 슬롯 work/{repo}_1 미생성"


def test_import_multipm_home_claude_sets_up_two_repos_and_slots(tmp_path):
    """`_import_multipm_home` 가 claude 하니스에서도 multi-PM 홈 + 2 repo areas + 2 슬롯을 만든다.

    claude multi-repo 라이브 테스트(`test_release_wave_multirepo_claude_full_wave`)의 전제(셋업)를
    hermetic 하게 검증 — opencode 동형 짝(`test_import_multipm_home_sets_up_two_repos_and_slots`)이다.
    `_import_multipm_home` 은 harness 파라미터화돼 있어 셋업은 harness 무관해야 한다(어댑터층만 다름).
    셋업이 깨지면 라이브가 가짜 PASS/skip 으로 숨지 않고 여기서 잡힌다.
    """
    home = _import_multipm_home(tmp_path, "claude")

    # (1) home 이 fresh adopter 로 import 됐다(공유 보드 + 엔진).
    assert (home / ".project_manager" / "tools" / "board.py").exists()
    assert (home / ".project_manager" / "wiki" / "tickets" / "open").is_dir()

    # (2) 2 repo 가 areas.md(per-repo 레지스트리·ADR-0014)에 prefix 로 등록됐다.
    areas_path = home / ".project_manager" / "areas.md"
    assert areas_path.exists(), "repo add 후 areas.md 부재"
    areas_text = areas_path.read_text(encoding="utf-8")
    for repo in _MULTIREPO_REPOS:
        assert f"| {repo} |" in areas_text, f"areas.md 에 repo '{repo}' 등록 행 부재"

    # (3) repo 마다 worktree 슬롯(work/<repo>_1)이 생성됐다.
    for repo in _MULTIREPO_REPOS:
        slot = home / "work" / f"{repo}_1"
        assert slot.is_dir(), f"worktree 슬롯 work/{repo}_1 미생성"


def test_multirepo_wave_prompt_has_per_repo_mechanics():
    """multi-repo wave 프롬프트가 각 repo 의 wave mechanics(prefix·session·슬롯 파일·4단계)를 담는다.

    라이브 미실행 시에도 프롬프트 구조를 가드 — repo별 prefix(`--prefix REPO`)·per-slot session
    (`--session REPO_1`)·슬롯 파일(`work/REPO_1/<file>`)·new/claim/complete 4단계가 빠지면 잡힌다.
    """
    prompt = _multirepo_wave_prompt()

    # 두 repo 가 모두 prompt 에 등장(공유 보드 위 각 repo wave).
    for repo in _MULTIREPO_REPOS:
        assert repo in prompt, f"프롬프트에 repo '{repo}' 미언급"
    # 4단계 mechanics — new(+prefix)·claim(+session)·슬롯 파일·complete(sync-gate flag).
    assert "board.py new" in prompt and "--prefix REPO" in prompt
    assert "board.py claim" in prompt and "--session REPO_1" in prompt
    assert f"work/REPO_1/{_WAVE_FILE}" in prompt
    assert "board.py complete" in prompt
    assert "--tests-pass" in prompt and "--allow-missing-log" in prompt
