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

import ast
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
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
# glm-5.2:cloud(ollama cloud) 강한 모델을 release default 로 쓴다(2026-07-07 채택·라이브 실측은
# 릴리즈 wave). runtime_smoke[lite·sync-gate 없음]는 gemma 로 충분 — 거긴 별도 default.
# env override 로 교체 가능.
LIVE_MODEL = os.environ.get("PM_ORCH_LIVE_MODEL", "ollama/glm-5.2:cloud")

# full wave probe 가 작성하도록 지시하는 산출 파일·내용 — side-effect 단언의 기준(단일 진실).
PROBE_FILE = "probe.txt"
PROBE_TEXT = "hello from dev"

# 위임 단언 대상 서브에이전트 — full wave 가 developer(구현)·code-reviewer(리뷰) 둘 다 거쳐야 통과.
_DEV_SUBAGENT = "developer"
_REVIEWER_SUBAGENT = "code-reviewer"

# opencode 는 gemma 가 느리고 변동 커 1800s, claude 는 probe 실측 145s 여유분 600s.
_OPENCODE_TIMEOUT = int(os.environ.get("PM_ORCH_LIVE_RELEASE_TIMEOUT", "1800"))
_CLAUDE_TIMEOUT = int(os.environ.get("PM_ORCH_LIVE_RELEASE_CLAUDE_TIMEOUT", "600"))

_TOOLS = Path(__file__).resolve().parents[1] / ".project_manager" / "tools"


def _load_pm_relay():
    """엔진 pm_relay(첫-이벤트 워치독)를 importlib 로 로드 (T-0336·release 라이브 헬퍼용)."""
    spec = importlib.util.spec_from_file_location("pm_relay", _TOOLS / "pm_relay.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_opencode_live(argv, *, cwd, env, timeout):
    """opencode 라이브 호출을 엔진 첫-이벤트 워치독으로 감싼다 (T-0336).

    startup network fetch stall(PM 70)에 무한 hang 하지 않도록 첫-이벤트 감시 + 유한 재시도.
    소진 시 StallWatchdogError → 테스트 **fail-loud**(라이브 환경 문제 가시화). overall_timeout 은
    기존 turn 상한(1800s) 유지 — mid-turn(정상 긴 생성) 침묵은 그 백스톱이 담당. subprocess.run 과
    동일한 CompletedProcess(returncode·stdout·stderr)를 반환해 side-effect 단언은 무변경."""
    engine = _load_pm_relay()
    return engine.run_with_first_event_watchdog(
        argv,
        first_event_timeout=engine.first_event_timeout_default(),
        overall_timeout=timeout,
        retries=engine.stall_retries_default(),
        cwd=str(cwd),
        env=env,
    )


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
        cwd=str(dest), capture_output=True, text=True, encoding="utf-8", errors="replace",
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

    proc = _run_opencode_live(
        # `--dangerously-skip-permissions`: 비대화 헤드리스라 opencode 가 `--dir` 디렉토리를
        # external_directory 로 보고 권한을 auto-reject → AGENTS.md 도 못 읽고 wave 시작 실패한다.
        # 이 플래그로 권한을 통과시켜야 wave 완주(throwaway tmp adopter 격리라 안전·PM 36 probe 실측).
        ["opencode", "run", "--agent", "build", "--dir", str(dest),
         "--dangerously-skip-permissions", "-m", LIVE_MODEL,
         _full_wave_prompt("AGENTS.md")],
        cwd=str(dest), env=_live_env(LIVE_MODEL), timeout=_OPENCODE_TIMEOUT,
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
# wave 를 운영하는지 검증한다. PM 라이브 probe(opencode·ollama cloud 모델·실측 PASS)로 viable 확인
# 후 그 mechanics 를 옮긴 것이다.

# multi-repo 셋업의 repo 이름 = prefix = worktree 슬롯 네임스페이스(단일 진실). 2 repo 로 충분 —
# 새 위험축(per-repo prefix·per-slot 식별)은 1→2 에서 이미 드러난다(대N 은 spike §6 후속).
# 이름은 **소문자**여야 한다 — prefix sanity(`_validate_prefix`·[a-z0-9_]+·ADR-0042/T-0237)가
# `--prefix <repo>` 를 검증하므로(대문자면 rc1). 라이브 LLM 은 아래 프롬프트의 "REPO" 를 실 repo
# 이름(소문자)으로 치환해 `--prefix repoa` 를 발행한다 → sanity 통과.
_MULTIREPO_REPOS = ("repoa", "repob")
# multi-repo wave 가 각 repo 슬롯에 쓰도록 지시하는 산출 파일·내용 — side-effect 단언의 기준.
# (단일 wave 의 PROBE_FILE='probe.txt' 와 별개 — 슬롯별 파일이라 슬롯 격리도 함께 단언한다.)
_WAVE_FILE = "wave-done.txt"


def _seed_git_repo(path: Path) -> None:
    """seed git repo(main·1 commit) 생성 — repo add 의 bare-clone 원(ADR-0011)."""
    path.mkdir(parents=True, exist_ok=True)
    _git = lambda *a: subprocess.run(["git", "-C", str(path), *a], check=True,
                                     capture_output=True, text=True, encoding="utf-8", errors="replace")
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
        cwd=str(home), capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, "PM_NONINTERACTIVE": "1"},
    )


def _import_multipm_home(tmp_path: Path, harness: str,
                         repos: tuple[str, ...] = _MULTIREPO_REPOS,
                         *, area_owners: dict[str, str] | None = None) -> Path:
    """multi-PM 홈 import (hermetic) — fresh adopter 위에 `repo add`·`worktree add` 로 multi-repo 셋업.

    단일 `_import_adopter`(test_fresh_adopter_runtime_smoke) 와 *다른* 셋업이다 — 그건 import 만,
    이건 그 위에 repo 마다 [seed git repo → `pm_config repo add` → `pm_config worktree add`] 를
    얹어 공유 보드 + 슬롯(`work/<repo>_1`)을 만든다. `_load_pm_import`·`_real_models_runner` 스텁을
    재사용해 라이브 models 조회를 차단(hermetic). home 디렉토리를 반환한다.

    `area_owners`(선택·`{repo: user}`): 그 repo 의 areas.md `area_owner`(그 area 의 user 소유)를
    `pm_config repo add --user <user>` 로 distinct 2 user 로 스탬프한다 — multi-USER composite
    (`_import_multiuser_home`·T-0309). 미지정(기본·None)이면 현행 동작(빈 area_owner·단일 user).
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
        add_args = ["repo", "add", repo, "--git", str(origins / repo)]
        # area_owners 지정 시 그 repo 의 area_owner(=그 area 의 user 소유)를 `--user` 로 스탬프한다
        # (multi-USER composite). areas.md `area_owner` 칼럼은 `_ticket_owner`(open 소유)의 소유 유도
        # 소스다 — distinct 2 user 여야 세션 뷰가 strict-exclude(섞임 격리)로 돈다. querying identity 는
        # user-first(ADR-0056)로 현재 사용자다(area_owner-derived 폐기).
        if area_owners and repo in area_owners:
            add_args += ["--user", area_owners[repo]]
        added = _pm_config(home, *add_args)
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
    (`--repo <repo> --slot 1`·`work/<repo>_1` 슬롯 파일)이다. 그래서 prompt 는 그 축만 친다(ticket 본문
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
        "--repo REPO --slot 1\n"
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

    PM 라이브 probe(`scratchpad/mpm_live_probe.sh`·opencode·ollama cloud 모델·실측 PASS —
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

    proc = _run_opencode_live(
        # `--dangerously-skip-permissions`: 비대화 헤드리스 격리(throwaway tmp home)라 안전 —
        # 단일 wave 테스트와 동일 근거(opencode 가 --dir 디렉토리를 external 로 보고 auto-reject).
        ["opencode", "run", "--agent", "build", "--dir", str(home),
         "--dangerously-skip-permissions", "-m", LIVE_MODEL,
         _multirepo_wave_prompt()],
        cwd=str(home), env=_live_env(LIVE_MODEL), timeout=_OPENCODE_TIMEOUT,
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
        cwd=str(home), capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=_live_env(CLAUDE_MODEL), timeout=_CLAUDE_TIMEOUT,
    )

    # side-effect(hard) — repo별 done ticket(per-repo prefix) + 슬롯 파일(슬롯 격리).
    _assert_multirepo_wave_side_effects(home, proc, "claude")


# ── multi-USER composite 경로 (2 user 공유보드 뷰 섞임 격리 · release-marked 라이브 half · T-0309) ──
# 위 multi-repo 테스트는 *한* user(단일 정체성)가 여러 repo 슬롯을 운영하는 축이다. 아래는 그 직교
# 축 — **2 distinct user(alice/bob)가 공유 보드에서 각자 티켓을 만들고 세션 뷰가 서로 섞이지 않는가**
# (ADR-0053 세션격리 불변식). T-0304(`test_board_scoping_isolation`)가 이 불변식을 *무-LLM* durable
# 로 못박고(실 board API create→view), 이 라이브 half 는 실 opencode 세션이 각 identity 로 티켓을
# 생성한 뒤 뷰 섞임을 side-effect 로 검증한다(라이브 opencode end-to-end·release tier).

# 각 identity = (repo, prefix, session, user). prefix(al/be)는 repo(alpha/beta)와 별개 축(ID
# 네임스페이스·소문자 sanity `_validate_prefix`) — `test_board_scoping_isolation._seed_composite` 와
# 동일 매핑을 라이브 홈에 재현한다. areas.md `area_owner` 칼럼이 open 티켓 소유(`_ticket_owner`·
# alpha→alice·beta→bob)를 정의한다(user-first·ADR-0056: querying identity 는 현재 사용자).
_MULTIUSER_REPOS = ("alpha", "beta")
_MULTIUSER_AREA_OWNERS = {"alpha": "alice", "beta": "bob"}
_MULTIUSER_IDENTITIES = (
    # (repo,   prefix, session,   user)
    ("alpha", "al", "alpha_1", "alice"),
    ("beta",  "be", "beta_1",  "bob"),
)


def _import_multiuser_home(tmp_path: Path, harness: str) -> Path:
    """multi-USER 홈 — 2 repo(alpha/beta)에 distinct area_owner(alice/bob)를 등록한 multi-PM 셋업.

    `_import_multipm_home` 을 재사용하되 `area_owners` 로 repo add 에 `--user <owner>` 를 실어
    areas.md `area_owner` 칼럼을 alpha→alice·beta→bob 로 distinct 2 user 로 세팅한다(=
    `test_board_scoping_isolation._seed_composite` 의 areas 매핑을 라이브 홈에 재현). LLM 없이 도는
    hermetic 셋업(`_real_models_runner` 스텁 상속). home 을 반환한다.
    """
    return _import_multipm_home(tmp_path, harness, repos=_MULTIUSER_REPOS,
                                area_owners=_MULTIUSER_AREA_OWNERS)


def _multiuser_wave_prompt(identities: tuple = _MULTIUSER_IDENTITIES) -> str:
    """각 identity(alice/bob)가 공유 보드에서 자기 정체성으로 [미claim open + claim] 티켓을 만들라는 프롬프트.

    섞임 격리를 실증하려면 각 user 가 (i) 미claim open (다른 user 뷰가 절대 유출하면 안 되는 대상) +
    (ii) claim 티켓 (자기 뷰엔 열람) 을 남겨야 한다. 각 board 조작에 `--user <user>` 를 실어 티켓
    귀속(created_by/claimed_by user)을 distinct 2 user 로 스탬프한다 — 이게 `_distinct_ticket_users`
    다중사용자 신호(≥2)를 세워 세션 뷰가 strict-exclude(degrade 아님)로 돌게 한다. `--prefix` 로 ID
    네임스페이스(T-al-*/T-be-*)를 가르고, claim 은 `--repo <repo> --slot 1` 로 슬롯을 박는다.
    (`new` 는 `--repo`/`--slot` 인자가 없다 — created_by 슬롯은 무관하고 `--user` 가 귀속 user 를 정한다.)
    board.py 경로는 준다 — 새 위험축은 identity 귀속·뷰 격리이지 문서 운영성이 아니다(단일 full wave 커버).
    """
    blocks = []
    for repo, prefix, session, user in identities:
        slot = session.rsplit("_", 1)[-1]  # session `<repo>_<N>` → 슬롯 N (ADR-0057 --repo/--slot)
        blocks.append(
            f"Person {user} (prefix {prefix}, session {session}) — do these 3 commands:\n"
            f'  1. python3 .project_manager/tools/board.py new "open probe {user}" '
            f"--prefix {prefix} --user {user}\n"
            f"     (prints a ticket id like T-{prefix}-001 — this is {user}'s UNCLAIMED open; "
            f"do NOT claim it)\n"
            f'  2. python3 .project_manager/tools/board.py new "wip probe {user}" '
            f"--prefix {prefix} --user {user}\n"
            f"     (prints a SECOND id, e.g. T-{prefix}-002 — note it)\n"
            f"  3. python3 .project_manager/tools/board.py claim <SECOND_ID> "
            f"--user {user} --repo {repo} --slot {slot}\n"
        )
    body = "\n".join(blocks)
    people = " and ".join(u for _r, _p, _s, u in identities)
    return (
        f"You operate ONE shared project-manager board used by {len(identities)} different "
        f"people: {people}. The board engine is .project_manager/tools/board.py. Act as EACH "
        "person in turn and create their tickets with THEIR identity flags EXACTLY as written — "
        "the --user and --prefix flags decide who owns each ticket, so never omit them. Finish "
        "one person completely before starting the next.\n\n"
        f"{body}\n"
        "Substitute the EXACT ticket id printed by each `new` command into the matching `claim` "
        f"command. Reply 'done' when all {2 * len(identities)} tickets exist."
    )


def _board_list(home: Path, *args: str) -> subprocess.CompletedProcess:
    """home 의 board.py list 를 subprocess 로 호출(엔진 도구·LLM 아님 → 부모 env OK).

    격리 판정은 **테스트가 직접** board.py 를 돌려 실 산출(뷰)을 파싱한다 — LLM 출력 phrasing
    비결정에 강건(side-effect 단언·T-0157 동형). `--repo <repo> --slot 1` 은 아무것도 안 바꾸는 뷰 렌즈.
    """
    return subprocess.run(
        [sys.executable, str(home / ".project_manager" / "tools" / "board.py"), "list", *args],
        cwd=str(home), capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, "PM_NONINTERACTIVE": "1"},
    )


def _set_home_user(home: Path, user: str) -> None:
    """home local.conf 에 `user=` 를 append(last-wins) — 필터 뷰 querying identity 를 그 user 로 스탬프.

    user-first(ADR-0056·T-0312): `list --repo <repo> --slot <N>` 은 **현재 사용자 ∩ 슬롯**이라, 각 identity 의
    세션 뷰는 *그 user 로* 조회해야 자기 claim 이 보인다(옛 area_owner-derivation 폐기 — `--repo`/`--slot` 이
    area_owner 로 user 를 유도하지 않는다). `load_local_config` 는 KEY 마지막 값 채택이라 append 가
    이긴다(`_append_tiny_ctx_window` 동형). machine composite(`test_board_scoping_isolation`
    `_write_conf(user=…)`)의 라이브 짝.
    """
    conf = home / ".project_manager" / "local.conf"
    conf.write_text(conf.read_text(encoding="utf-8") + f"\nuser={user}\n", encoding="utf-8")


def _parse_list_rows(stdout: str) -> list[tuple[str, str]]:
    """board.py list 출력에서 (status, ticket_id) 목록을 파싱 (`  [status ] T-...  title …`).

    `test_board_scoping_isolation._view` 동형 파싱을 subprocess stdout 에 적용한다 — `[` 로 시작하는
    행만, `]` 뒤 첫 토큰이 ticket id. 비-행(헤더·`(no tickets)`·경고)은 조용히 무시.
    """
    rows: list[tuple[str, str]] = []
    for line in stdout.splitlines():
        s = line.strip()
        if s.startswith("[") and "]" in s:
            status = s[1:s.index("]")].strip()
            rest = s.split("]", 1)[1].split()
            if rest:
                rows.append((status, rest[0]))
    return rows


def _assert_multiuser_view_isolation(home: Path, proc: subprocess.CompletedProcess,
                                     harness: str,
                                     identities: tuple = _MULTIUSER_IDENTITIES) -> None:
    """multi-USER 뷰 섞임 격리 단언 — 각 user 세션 뷰가 타 user 티켓을 미열람·자기 것만 열람.

    테스트가 직접 board.py list 를 돌려(side-effect·LLM phrasing 비결정 강건) 실 뷰를 파싱한다:
      (전제) 두 user 가 실제로 티켓을 만들었고(wave 완주) 각자 미claim open 을 남겼다 — 미claim open
             이 없으면 섞임 assert 가 공허해진다(degrade 가 유출할 대상 open 이 실재해야 catch).
      (a) alice 세션(alpha_1) 뷰 = T-al-* 만·bob(T-be-*) **미열람** · bob 세션(beta_1) 뷰 = 역.
      (c) 각자 자기(T-<own>-*) 열람 (섞임 격리가 자기 것까지 지우는 over-exclude 도 잡음).
    ID prefix(T-al-*/T-be-*)가 소유 user 와 1:1 대응(각 user 가 자기 --prefix 로 발행)이라, "alice 뷰에
    T-be-* 0" = alice 세션이 bob 소유 티켓을 유출 안 함 = ADR-0053 세션격리. degrade(전체 open=mine)면
    alice 뷰에 bob 미claim open(T-be-*)이 섞여 이 단언이 red 로 잡는다(실 격리 검증·verify-real-output).
    """
    tail = (f"--- {harness} stdout(tail) ---\n{proc.stdout[-2500:]}\n"
            f"--- stderr(tail) ---\n{proc.stderr[-1000:]}")

    # (전제) 전체 보드 — 두 user 가 티켓을 만들었고 각자 미claim open 을 남겼나.
    # 전제 확인 = 전체 보드 상세 — ADR-0066 이후 무인자 기본 뷰는 내 스트림 스코프(미귀속 open 접힘)
    # 이라 `--all` 로 전체를 본다(v1.3.2 livegate red 근본원인).
    full = _board_list(home, "--all", "--status", "all")
    assert full.returncode == 0, (
        f"board.py list --status all rc={full.returncode}\n{full.stderr[-800:]}\n" + tail)
    all_rows = _parse_list_rows(full.stdout)
    for _repo, prefix, _session, user in identities:
        owned = [tid for _st, tid in all_rows if tid.startswith(f"T-{prefix}-")]
        assert owned, (
            f"실 {harness} multiuser wave: user '{user}'(prefix {prefix}) 티켓 미생성 — wave 미완주.\n"
            f"all={all_rows}\n" + tail)
        owned_open = [tid for st, tid in all_rows
                      if tid.startswith(f"T-{prefix}-") and st == "open"]
        assert owned_open, (
            f"실 {harness} multiuser wave: user '{user}' 미claim open(T-{prefix}-*) 부재 — 섞임 "
            f"격리 assert 가 공허해진다(유출 대상 open 이 실재해야 catch).\nall={all_rows}\n" + tail)

    # (a)·(c) 각 identity 세션 뷰 — 타 user 미열람 · 자기 열람.
    prefixes = [p for _r, p, _s, _u in identities]
    for repo, prefix, session, user in identities:
        slot = session.rsplit("_", 1)[-1]  # `<repo>_<N>` → 슬롯 N
        # user-first(ADR-0056·T-0312): 세션 뷰 querying identity = 현재 사용자. 각 identity 의
        # `--repo <repo> --slot <N>` 뷰는 *그 user 로* 조회해야 자기 슬롯 claim 이 보인다(area_owner-
        # derivation 폐기). 그 user 로 스탬프 후 조회 — 아니면 over-exclude(자기 claim 도 안 보임).
        _set_home_user(home, user)
        view = _board_list(home, "--repo", repo, "--slot", slot)
        assert view.returncode == 0, (
            f"board.py list --repo {repo} --slot {slot} rc={view.returncode}\n{view.stderr[-800:]}\n" + tail)
        ids = {tid for _st, tid in _parse_list_rows(view.stdout)}
        others = [op for op in prefixes if op != prefix]
        leaked = {tid for tid in ids if any(tid.startswith(f"T-{op}-") for op in others)}
        assert not leaked, (
            f"실 {harness}: {user} 세션({session}) 뷰에 타 user 티켓 유출 {sorted(leaked)} — 세션 뷰 "
            f"섞임(ADR-0053 위반·degrade 재현).\n뷰={sorted(ids)}\n" + tail)
        assert any(tid.startswith(f"T-{prefix}-") for tid in ids), (
            f"실 {harness}: {user} 세션({session}) 뷰가 자기 티켓(T-{prefix}-*)을 미열람 — 섞임 격리가 "
            f"자기 것까지 지움(over-exclude).\n뷰={sorted(ids)}\n" + tail)

    # task 렌즈(`--task <이름>`·T-0365·[[ADR-0059]] Decision 10) 라이브 커버 — 기계 composite
    # (`test_board_scoping_isolation` 의 task 축 surface)와 **짝**(decision: 둘 중 하나만 갱신하면
    # 라이브/기계 뷰가 어긋난다). 라이브 wave 는 slot-mode claim(claimed_by=<user>/<repo>_1)만 남기므로
    # 그 위에서 task 렌즈가 (i) 타 user 무유출 (ii) slot claim 을 task 이름으로 안 끌어옴(⑥ 기계 판별·
    # claimed_by 재사용) 을 실증한다. task 바인딩 claim 이 없는 fresh task 명은 (claim 0 + 내 소유 open
    # backlog) 로 좁혀져야 한다 — 무필터 전체 보드로 새지 않음(핸들러가 `--task` 를 실 소비).
    owned_open_by_prefix = {p: {tid for st, tid in all_rows
                                if tid.startswith(f"T-{p}-") and st == "open"}
                            for _r, p, _s, _u in identities}
    owned_claimed_by_prefix = {p: {tid for st, tid in all_rows
                                   if tid.startswith(f"T-{p}-") and st == "claimed"}
                               for _r, p, _s, _u in identities}
    for repo, prefix, session, user in identities:
        _set_home_user(home, user)
        # ⑥ 예약(task 명 ≠ <repo>_<N>)에 안 걸리는 자유 task 명 — slot 토큰(<repo>_1)과 겹치지
        # 않아야 판별 검증이 유효(예 `alpha_1` 을 주면 slot 토큰과 우연 일치해 공허해진다).
        fresh_task = f"{prefix}-probe-task"
        tview = _board_list(home, "--task", fresh_task)
        assert tview.returncode == 0, (
            f"board.py list --task {fresh_task} rc={tview.returncode}\n{tview.stderr[-800:]}\n" + tail)
        tids = {tid for _st, tid in _parse_list_rows(tview.stdout)}
        others = [op for op in prefixes if op != prefix]
        leaked = {tid for tid in tids if any(tid.startswith(f"T-{op}-") for op in others)}
        assert not leaked, (
            f"실 {harness}: {user} task 렌즈(--task {fresh_task})에 타 user 티켓 유출 {sorted(leaked)} — "
            f"task-aware 세션 격리 위반(ADR-0059).\n뷰={sorted(tids)}\n" + tail)
        # slot claim(<user>/<repo>_1·T-{prefix}-* claimed)은 task 이름과 안 겹쳐(⑥) task 렌즈에서
        # 걸러진다 — 남는 건 claim 0 + 내 소유 open backlog. 미claim open 은 실재(전제 assert)라 뷰가 안 빈다.
        assert not (owned_claimed_by_prefix[prefix] & tids), (
            f"실 {harness}: {user} task 렌즈(--task {fresh_task})에 slot claim "
            f"{sorted(owned_claimed_by_prefix[prefix] & tids)} 유입 — ⑥ 기계 판별 실패(slot 토큰을 task 로 매칭).\n"
            f"뷰={sorted(tids)}\n" + tail)
        assert tids == owned_open_by_prefix[prefix], (
            f"실 {harness}: {user} task 렌즈(--task {fresh_task}) != 내 소유 open backlog "
            f"{sorted(owned_open_by_prefix[prefix])} — 필터 미소비(silent no-op·전체 보드 유출) 또는 "
            f"over-exclude.\n뷰={sorted(tids)}\n" + tail)


@pytest.mark.release
@pytest.mark.skipif(
    not _RELEASE_LIVE or not shutil.which("opencode"),
    reason="release wave multiuser composite — PM_ORCH_LIVE_RELEASE=1 + opencode CLI(+ollama 모델) 필요. "
           "기본 skip·사용자 트리거.",
)
def test_release_wave_multiuser_composite_opencode(tmp_path):
    """실 opencode 세션이 공유 보드에서 2 distinct user(alice/bob)로 티켓을 만들고 세션 뷰가 섞이지 않음을 라이브 실증.

    T-0304(`test_board_scoping_isolation`)의 무-LLM composite 게이트의 **라이브 opencode 짝**(ADR-0053).
    셋업(`_import_multiuser_home`): 공유 보드 홈에 repo alpha(area_owner alice)·beta(area_owner bob)를
    distinct user 로 등록. opencode(agentic·ollama glm-5.2)가 `_multiuser_wave_prompt` 로 각 identity 의
    [미claim open + claim] 티켓을 자기 `--user`/`--prefix`/`--repo`/`--slot` 으로 만든다.

    격리 판정은 **테스트가 직접** board.py list 를 돌려(side-effect·LLM phrasing 비결정 강건) —
    alice 세션(alpha_1) 뷰는 bob 티켓(T-be-*) 미열람·자기(T-al-*) 열람, bob 세션(beta_1) 뷰는 역 —
    즉 **뷰가 섞이지 않음**을 실 산출로 assert(`_assert_multiuser_view_isolation`). `--dir` 로 루트 핀
    (opencode 는 PWD 로 루트 오판). API 과금 0(로컬/cloud ollama). cross-slot 축은 T-0304 기계 게이트 커버.
    """
    home = _import_multiuser_home(tmp_path, "opencode")

    proc = _run_opencode_live(
        # `--dangerously-skip-permissions`: 비대화 헤드리스 격리(throwaway tmp home)라 안전 —
        # multi-repo 라이브 테스트와 동일 근거(opencode 가 --dir 디렉토리를 external 로 보고 auto-reject).
        ["opencode", "run", "--agent", "build", "--dir", str(home),
         "--dangerously-skip-permissions", "-m", LIVE_MODEL,
         _multiuser_wave_prompt()],
        cwd=str(home), env=_live_env(LIVE_MODEL), timeout=_OPENCODE_TIMEOUT,
    )

    # side-effect(hard) — 각 user 세션 뷰가 타 user 티켓을 미열람·자기만 열람(뷰 섞임 0).
    _assert_multiuser_view_isolation(home, proc, "opencode")


# ── hard-stop 락아웃/실발화 라이브 단언 (ADR-0038 D4/T-D · T-0190) ─────────────────────────
# 위 wave 테스트는 정상 컨텍스트에서 도는 full wave 다. 아래는 그 *경계* — hard-stop machinery
# (ADR-0038)가 실 claude transcript 위에서 실제로 발화하고, 락아웃 예외(T-0205)가 성립하는지를
# 라이브로 못박는다. 기계 단위 테스트(test_claude_ctx_guard)가 로직을 결정적으로 커버하지만,
# transcript-slug 탐색·실 transcript 의 100% 판정·래퍼(.sh) exec 발화는 실 하니스 형상에서만
# 드러나는 갭이다([[verify-real-output-not-just-review]]·설계검증 allow-list 렌즈).

# ctx 예산 극소 설정 — 실 transcript 의 첫 턴이 곧장 stop 밴드(잔여 0)에 들도록. local.conf
# 에 이 값을 *append* 해 마지막-줄이 이긴다(last-wins·load_local_config 규칙·PM 47 실측).
_TINY_CTX_WINDOW = 2000
# hard-stop 훅 stdin 세션 id — marker 파일명(`<sid>.done`)의 단일 진실.
_HARD_STOP_SID = "release-hard-stop-probe"
# stop 밴드에서 통과하는 유일한 UserPromptSubmit prompt(T-0205 핸드오프 예외) vs 계속 block 되는 것.
_HANDOFF_PROMPT = "/pm-handoff"
_NON_HANDOFF_PROMPT = "다른 일 해줘"


def _append_tiny_ctx_window(dest: Path) -> None:
    """adopter local.conf 에 극소 ctx_window_tokens 를 append(last-wins) — 즉발 hard-stop.

    import 기본 local.conf 는 이미 ctx_window_tokens=200000 을 담는다 — append 한 극소값이
    *마지막 줄* 로 이겨(load_local_config 는 KEY 마지막 값 채택) 첫 실 턴이 잔여 0 = stop 밴드.
    """
    conf_path = dest / ".project_manager" / "local.conf"
    conf_path.write_text(
        conf_path.read_text(encoding="utf-8") + f"\nctx_window_tokens={_TINY_CTX_WINDOW}\n",
        encoding="utf-8",
    )


def _claude_project_slug(cwd: Path) -> str:
    """claude Code transcript 디렉토리 slug — cwd 절대경로의 비영숫자를 '-' 로 치환.

    실측: `/home/u/.../project_manager` → `-home-u-...-project-manager`(`/`·`_`·`.` 모두 `-`).
    """
    return re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def _find_claude_transcript(dest: Path, *, not_before: float = 0.0) -> Path | None:
    """turn1 이 남긴 실 transcript(`~/.claude/projects/<cwd-slug>/*.jsonl`) 최신본을 찾는다.

    1차: cwd(=dest) slug 디렉토리 직접 glob. 2차 폴백(resolve/치환 엣지 대비): dest.name slug 로
    끝나는 프로젝트 디렉토리 안을 훑는다. 못 찾으면 None(호출부가 명확 assert).

    `not_before`(test 시작 시각): 폴백이 dest.name('adopter-claude')만으로 매칭하면 **과거 run 의
    잔재 transcript** 를 집어 primary-miss 를 가릴 수 있다(reviewer should-fix) — 이번 run 생성분
    (mtime >= not_before)만 후보로 스코프해 stale false-green 을 차단한다.

    참고(비정리·누적): turn1 transcript 는 사용자 홈(`~/.claude/projects/<tmp-slug>/`)에 남고 이
    테스트는 정리하지 않는다 — tmp-unique slug 라 세션 간 간섭 없음·순수 축적만(release-only 수용).
    """
    projects = Path.home() / ".claude" / "projects"
    candidates = list((projects / _claude_project_slug(dest)).glob("*.jsonl"))
    if not candidates and projects.is_dir():
        tail = _claude_project_slug(Path(dest.name))  # 예: 'adopter-claude'
        for pdir in projects.iterdir():
            if pdir.is_dir() and pdir.name.endswith(tail):
                candidates.extend(pdir.glob("*.jsonl"))
    candidates = [p for p in candidates if p.stat().st_mtime >= not_before]
    return max(candidates, key=lambda p: p.stat().st_mtime) if candidates else None


def _load_adopter_ctx_guard(dest: Path):
    """adopter 가 실제로 쓰는 `.claude/ctx_guard.py` 를 로드 — 같은 machinery 로 % 판정 재현."""
    path = dest / ".claude" / "ctx_guard.py"
    spec = importlib.util.spec_from_file_location("adopter_ctx_guard", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fire_stop_hook(dest: Path, stdin_payload: dict) -> subprocess.CompletedProcess:
    """adopter 의 `.claude/ctx_stop_hook.sh` 래퍼를 하니스처럼 발화(stdin JSON·rc/stdout 그대로).

    claude Code 가 훅을 부르는 방식(래퍼 exec·stdin 에 hook JSON)을 그대로 재현한다 — 래퍼가
    인터프리터 self-resolve 후 ctx_stop_hook.py 를 exec. 엔진-측 스크립트라 LLM 아님·부모 env OK.
    bash 절대경로 경유 스폰 — Windows CreateProcess 는 shebang 스크립트를 직접 실행 못 한다
    (WinError 193·PM 48차 tier3 실측). POSIX 는 shebang 이 bash 라 동치 (test_run_tests_hook 패턴).
    """
    bash = shutil.which("bash") or "bash"
    return subprocess.run(
        [bash, str(dest / ".claude" / "ctx_stop_hook.sh")],
        input=json.dumps(stdin_payload),
        cwd=str(dest), capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, "PM_NONINTERACTIVE": "1"},
    )


@pytest.mark.release
@pytest.mark.skipif(
    not _RELEASE_LIVE or not shutil.which("claude"),
    reason="release wave hard-stop — PM_ORCH_LIVE_RELEASE=1 + claude CLI 필요(API 과금). "
           "기본 skip·사용자 트리거.",
)
def test_release_wave_claude_hard_stop_lockout_exception(tmp_path):
    """실 claude transcript 로 hard-stop 이 발화하고 락아웃 예외(T-0205)가 성립하는지 라이브 단언.

    레시피(PM 47 라이브 probe 실증·2026-07-02): fresh claude adopter import → local.conf 에
    ctx_window_tokens=2000 append(극소 예산·last-wins) → **turn1: 실 claude 1콜**(비용 절제 —
    단 1회)로 CLAUDE.md 를 읽고 요약시켜 transcript 를 인플레이션 → 실 transcript(`~/.claude/
    projects/<cwd-slug>/*.jsonl`)가 극소 window 대비 used=100%/stop 으로 판정되는지
    (`ctx_guard.context_used_pct_from_transcript` 로 실증) → adopter 래퍼(`.claude/
    ctx_stop_hook.sh`)를 하니스 형상 stdin JSON + 실 transcript_path 로 발화해 단언:
      1. PreToolUse + 새 작업(Bash `ls`) → deny JSON(`permissionDecision == "deny"`).
      2. UserPromptSubmit + 비-핸드오프 prompt → block JSON + reason 에 `/pm-handoff` 안내 포함
         (락아웃 계약 — 새 작업 진입 차단하되 탈출 커맨드 안내).
      3. UserPromptSubmit + `"/pm-handoff"` → **무출력 rc0 통과**(T-0205 fix — 이 예외가 없으면
         stop 후 전면 block 으로 핸드오프 진입 자체가 봉쇄되는 락아웃이었다·사용자 실측).
      4. STOP marker `.done` 실박제(`.project_manager/.local/ctx-stop/<sid>.done`) — hard-stop 이
         *실제로* 발화했다는 증거(mis-wire=가짜 게이트 방어·[[verify-real-output-not-just-review]]).

    **왜 래퍼-발화 방식인가(설계 결정)**: `claude -p --continue` full-e2e 로 실제 block/통과까지
    PM probe 로 확증됐으나 테스트엔 넣지 않는다 — turn2 LLM 콜은 추가 과금·비결정을 낳고, 래퍼-발화가
    실 transcript 위에서 계약(deny/block/pass/marker)을 결정적으로 전부 커버한다(turn1 1콜만 라이브).

    claude 는 subprocess cwd 를 존중한다(`--dir` 불요). API 과금(turn1 1콜).
    """
    dest = _import_adopter(tmp_path, "claude")
    _append_tiny_ctx_window(dest)
    test_start = time.time()  # transcript 탐색 스코프(과거 run 잔재 배제·reviewer should-fix).

    # turn1 — 실 claude 1콜로 transcript 인플레이션(요약 지시). Read 도구로 진입문서를 읽게 허용.
    turn1_prompt = (
        "Read CLAUDE.md and the key docs it references, then write a detailed multi-paragraph "
        "summary of how this project's board tool and PM workflow operate."
    )
    turn1 = subprocess.run(
        ["claude", "-p", "--model", CLAUDE_MODEL,
         "--allowedTools", "Bash", "Read",
         "--dangerously-skip-permissions", turn1_prompt],
        cwd=str(dest), capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=_live_env(CLAUDE_MODEL), timeout=_CLAUDE_TIMEOUT,
    )

    transcript = _find_claude_transcript(dest, not_before=test_start)
    assert transcript is not None, (
        "turn1 후 실 claude transcript 를 못 찾음 — hard-stop 판정 근거 부재.\n"
        f"찾은 slug={_claude_project_slug(dest)}  projects={Path.home() / '.claude' / 'projects'}\n"
        f"--- claude stdout(tail) ---\n{turn1.stdout[-1500:]}\n"
        f"--- stderr(tail) ---\n{turn1.stderr[-800:]}"
    )

    # 실 transcript 가 극소 window 대비 used=100%/stop 으로 판정되는지(같은 machinery 로 실증).
    ctx_guard = _load_adopter_ctx_guard(dest)
    used = ctx_guard.context_used_pct_from_transcript(str(transcript), _TINY_CTX_WINDOW)
    assert used == 100, (
        f"실 transcript 가 used=100% 로 판정되지 않음(used={used}·window={_TINY_CTX_WINDOW}) — "
        f"stop 밴드 진입 실패.\ntranscript={transcript}"
    )

    base_stdin = {"transcript_path": str(transcript), "session_id": _HARD_STOP_SID}

    # (1) PreToolUse + 새 작업(Bash ls) → deny.
    deny = _fire_stop_hook(dest, {
        **base_stdin, "hook_event_name": "PreToolUse",
        "tool_name": "Bash", "tool_input": {"command": "ls -la"},
    })
    assert deny.returncode == 0 and deny.stdout.strip(), (
        f"PreToolUse 새 작업에 훅이 출력 없음 — deny 미발화.\nstdout={deny.stdout!r} stderr={deny.stderr!r}"
    )
    deny_out = json.loads(deny.stdout)
    assert deny_out["hookSpecificOutput"]["permissionDecision"] == "deny", (
        f"새 작업 도구가 deny 되지 않음: {deny_out}"
    )

    # (2) UserPromptSubmit + 비-핸드오프 prompt → block + reason 에 `/pm-handoff` 안내(락아웃 계약).
    block = _fire_stop_hook(dest, {
        **base_stdin, "hook_event_name": "UserPromptSubmit", "prompt": _NON_HANDOFF_PROMPT,
    })
    assert block.returncode == 0 and block.stdout.strip(), (
        f"UserPromptSubmit 비-핸드오프에 훅이 출력 없음 — block 미발화.\n"
        f"stdout={block.stdout!r} stderr={block.stderr!r}"
    )
    block_out = json.loads(block.stdout)
    assert block_out["decision"] == "block", f"비-핸드오프 prompt 가 block 되지 않음: {block_out}"
    assert _HANDOFF_PROMPT in block_out["reason"], (
        f"block reason 에 탈출 커맨드({_HANDOFF_PROMPT}) 안내 누락 — 락아웃(계약 위반): {block_out['reason']!r}"
    )

    # (3) UserPromptSubmit + `/pm-handoff` → 무출력 rc0 통과(T-0205 락아웃 예외).
    handoff = _fire_stop_hook(dest, {
        **base_stdin, "hook_event_name": "UserPromptSubmit", "prompt": _HANDOFF_PROMPT,
    })
    assert handoff.returncode == 0 and handoff.stdout.strip() == "", (
        f"stop 밴드 `/pm-handoff` prompt 가 통과(무출력)하지 않음 — 락아웃 재현(T-0205 회귀).\n"
        f"rc={handoff.returncode} stdout={handoff.stdout!r} stderr={handoff.stderr!r}"
    )

    # (4) STOP marker `.done` 실박제 — hard-stop 이 실제로 발화했다는 증거(mis-wire 방어).
    marker = dest / ".project_manager" / ".local" / "ctx-stop" / f"{_HARD_STOP_SID}.done"
    assert marker.exists(), (
        f"STOP marker {marker} 부재 — hard-stop 미발화(가짜 게이트).\n"
        f"ctx-stop 디렉토리: {list((dest / '.project_manager' / '.local' / 'ctx-stop').glob('*')) if (dest / '.project_manager' / '.local' / 'ctx-stop').exists() else '(없음)'}"
    )


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
    """multi-repo wave 프롬프트가 각 repo 의 wave mechanics(prefix·slot·슬롯 파일·4단계)를 담는다.

    라이브 미실행 시에도 프롬프트 구조를 가드 — repo별 prefix(`--prefix REPO`)·per-slot 정체성
    (`--repo REPO --slot 1`)·슬롯 파일(`work/REPO_1/<file>`)·new/claim/complete 4단계가 빠지면 잡힌다.
    """
    prompt = _multirepo_wave_prompt()

    # 두 repo 가 모두 prompt 에 등장(공유 보드 위 각 repo wave).
    for repo in _MULTIREPO_REPOS:
        assert repo in prompt, f"프롬프트에 repo '{repo}' 미언급"
    # 4단계 mechanics — new(+prefix)·claim(+repo/slot)·슬롯 파일·complete(sync-gate flag).
    assert "board.py new" in prompt and "--prefix REPO" in prompt
    assert "board.py claim" in prompt and "--repo REPO --slot 1" in prompt
    # negative backstop — 구 actor 플래그가 프롬프트에 재유입되면 라이브 없이 여기서 red
    # (ADR-0057 BREAKING·T-0324 릴리즈 blocker 재발 방지·[[cross-cutting-breaking-blast-radius]]).
    assert "--session" not in prompt and "--worktree-slot" not in prompt
    assert f"work/REPO_1/{_WAVE_FILE}" in prompt
    assert "board.py complete" in prompt
    assert "--tests-pass" in prompt and "--allow-missing-log" in prompt


# ── multi-USER hermetic 가드 (라이브 실행 없이·@release/skipif 무관 — 매 회귀 통과 · T-0309) ─────
# multi-USER 라이브 테스트는 PM_ORCH_LIVE_RELEASE 미설정 시 skip 이라 CI 에선 안 돈다. 아래 단위는
# 라이브 없이도 돌아 (1) 셋업 헬퍼(`_import_multiuser_home`)가 2 repo 를 distinct area_owner
# (alpha→alice·beta→bob)로 등록하는지 (= 섞임 격리의 전제·라이브가 가짜 PASS 로 숨지 않게)
# (2) wave 프롬프트가 identity 귀속 mechanics 를 담는지 (3) 뷰 파서가 (status,id)를 정확히 뽑는지 —
# 라이브 미실행 시에도 구조·셋업을 가드한다(회귀가 잡음).


def test_import_multiuser_home_sets_up_two_distinct_area_owners(tmp_path):
    """`_import_multiuser_home` 가 LLM 없이 2 repo 를 distinct area_owner(alpha→alice·beta→bob)로 등록한다.

    라이브 multiuser composite 테스트의 전제(셋업)를 hermetic 하게 검증 — area_owner 가 distinct 2
    user 로 안 서면 세션 뷰가 solo degrade 로 돌아 섞임 격리가 무의미해지고 라이브가 가짜 PASS 로
    숨는다(여기서 잡힌다). areas.md 의 repo→area_owner 매핑(alpha→alice·beta→bob·open 소유
    `_ticket_owner`)을 못박는다. models 조회는 `_real_models_runner` 스텁 차단.
    """
    home = _import_multiuser_home(tmp_path, "opencode")

    # (1) multi-PM 홈 + 2 슬롯(work/alpha_1·work/beta_1) — per-slot 식별의 물리 자원.
    assert (home / ".project_manager" / "areas.md").exists(), "repo add 후 areas.md 부재"
    for repo in _MULTIUSER_REPOS:
        assert (home / "work" / f"{repo}_1").is_dir(), f"worktree 슬롯 work/{repo}_1 미생성"

    # (2) areas.md 가 repo→area_owner 를 distinct 2 user 로 매핑(alpha→alice·beta→bob·행 스코프 단언).
    areas_lines = (home / ".project_manager" / "areas.md").read_text(encoding="utf-8").splitlines()
    for repo, owner in _MULTIUSER_AREA_OWNERS.items():
        row = next((l for l in areas_lines if f"| {repo} |" in l), None)
        assert row is not None, f"areas.md 에 repo '{repo}' 행 부재"
        assert f"| {owner} |" in row, (
            f"areas.md repo '{repo}' 행의 area_owner 가 '{owner}' 아님 — distinct 2 user 미설정.\n{row}")

    # (3) 두 area_owner 가 서로 다름 = multi_user 신호의 전제(≥2 distinct → strict-exclude).
    assert len(set(_MULTIUSER_AREA_OWNERS.values())) == 2


def test_multiuser_wave_prompt_has_per_identity_mechanics():
    """multiuser wave 프롬프트가 각 identity 의 귀속 mechanics(--user·--prefix·미claim open·claim --repo/--slot)를 담는다.

    라이브 미실행 시에도 프롬프트 구조를 가드 — `--user <user>`(귀속 user·multi_user 신호)·`--prefix`
    (ID 네임스페이스)·미claim open(유출 대상)·claim `--repo <repo> --slot 1`(슬롯)이 빠지면 잡힌다.
    """
    prompt = _multiuser_wave_prompt()
    for repo, prefix, session, user in _MULTIUSER_IDENTITIES:
        slot = session.rsplit("_", 1)[-1]
        assert user in prompt, f"프롬프트에 user '{user}' 미언급"
        assert f"--prefix {prefix}" in prompt, f"프롬프트에 --prefix {prefix} 누락"
        assert f"--user {user}" in prompt, f"프롬프트에 --user {user} 누락(귀속 user 미스탬프)"
        assert f"--repo {repo} --slot {slot}" in prompt, f"프롬프트에 --repo {repo} --slot {slot} 누락(claim 슬롯)"
    assert "board.py new" in prompt and "board.py claim" in prompt
    # 미claim open(유출 대상) + claim 둘 다 지시 — 섞임 격리의 catch 대상 open 필요.
    assert "open probe" in prompt and "do NOT claim" in prompt
    # negative backstop — 구 actor 플래그 재유입 시 라이브 없이 red(T-0324 재발 방지).
    assert "--session" not in prompt and "--worktree-slot" not in prompt


def test_parse_list_rows_extracts_status_and_id():
    """`_parse_list_rows` 가 board.py list 출력에서 (status, id)를 정확히 파싱한다(비-행 무시)."""
    # cmd_list 출력 근사(`  [{status:7s}] {id}  {title}  {claimed}  {tags}`·board.py:cmd_list).
    sample = (
        "open tickets:\n"
        "  [open   ] T-al-001  open probe alice           alice/alpha_1     \n"
        "  [claimed] T-be-002  wip probe bob               bob/beta_1        \n"
        "(no tickets)\n"
        "random noise line without bracket\n"
    )

    rows = _parse_list_rows(sample)

    assert ("open", "T-al-001") in rows
    assert ("claimed", "T-be-002") in rows
    # 비-행(`(no tickets)`·헤더·노이즈)은 조용히 무시(파싱 예외 0).
    assert rows == [("open", "T-al-001"), ("claimed", "T-be-002")]


# ── marker-수집 가드 (기계·항상 실행·@release/skipif 무관 — 매 회귀 통과 · T-0190) ────────────
# 릴리즈 게이트는 `pytest -m release` 로 라이브 서브셋을 선택한다. 마커가 소실(데코레이터 삭제)·
# 개명(다른 이름)되면 그 테스트는 selection 에서 조용히 빠지고, 게이트는 "0개 수집·exit5" 를
# false-green 으로 삼킨다 — pytest.ini strict-marker 는 *오타* 만 잡지 *소실* 은 못 잡는다. 그래서
# 마커 달린 테스트 함수 수를 pin 해, 마커가 사라지거나 이름이 바뀌면 이 기계 가드가 즉시 red 로
# 잡는다(T-0159 보완). 기대값은 테스트가 늘 때 의도적으로 함께 갱신한다.
# release tier 는 ADR-0039 D1 로 라이브 tier 를 하나로 통합한 것이라, 마커가 여러 파일(이 파일·
# test_fresh_adopter_runtime_smoke·test_command_card_usability·test_pm_worktree_live)에 걸쳐 있다 —
# AST 수집은 `_RELEASE_TEST_FILES` 의 모든 파일을 스캔한다.

_RELEASE_TEST_FILES = (
    Path(__file__),
    Path(__file__).parent / "test_fresh_adopter_runtime_smoke.py",
    Path(__file__).parent / "test_command_card_usability.py",
    Path(__file__).parent / "test_pm_worktree_live.py",
    Path(__file__).parent / "test_pm_release_live.py",
)
# 마커 소실/개명을 잡는 안전망 — 라이브 테스트를 의도적으로 추가할 때만 함께 올린다.
# 6(이 파일: full/multirepo × claude/opencode + hard-stop + multiuser-composite opencode·T-0309)
# + 2(runtime_smoke: pm_update opencode/claude)
# + 2(command_card_usability: claude/opencode 카드 사용성·ADR-0046·T-0255)
# + 2(pm_worktree_live: claude/opencode 스킬 라이브 하네스·ADR-0050·T-0278)
# + 2(pm_release_live: claude hard/opencode best-effort 릴리즈 스킬 라이브·ADR-0049·T-0349).
# ⚠ 커플드-pin: 이 값을 올리면 touches 밖의 전역 pin 도 함께 정합돼야 `livegate record`(수집
#   N==pin)가 통과한다(orchestrator 가 갱신·test_command_card_usability.py 주석 참조):
#   board.LIVEGATE_RELEASE_PIN · tests/test_board_livegate.py(하드코딩 fake/assert) ·
#   tests/test_worktree_pool.py(_LIVEGATE_RELEASE_PIN 미러) · templates/*/board.py(pm_update 전파).
_EXPECTED_RELEASE_TESTS = 14


def _pytest_marker_name(decorator) -> str | None:
    """데코레이터 AST 노드 → `pytest.mark.<name>` 의 <name> (그 형태가 아니면 None).

    `@pytest.mark.release`(bare Attribute)·`@pytest.mark.skipif(...)`(Call)·
    `@pytest.mark.parametrize(...)` 모두 처리 — Call 이면 `.func` 를 본다.
    """
    node = decorator.func if isinstance(decorator, ast.Call) else decorator
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "mark"
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "pytest"
    ):
        return node.attr
    return None


def _count_marked_tests(path: Path, marker: str) -> int:
    """`path` 의 모듈-레벨 테스트 함수 중 `@pytest.mark.<marker>` 가 달린 개수 (AST 파싱)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return sum(
        1
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(_pytest_marker_name(d) == marker for d in node.decorator_list)
    )


def test_release_pin_matches_board_livegate_pin():
    """pin 단일진실 교차 단언 (T-0221/T-0222 접점·PM 배선) — release 케이스 수가 바뀌면
    board.LIVEGATE_RELEASE_PIN(livegate record 의 수집 게이트)도 함께 바뀌어야 한다.
    한쪽만 갱신하면 여기서 red — livegate 가 구 pin 으로 신규/삭제 케이스를 위장 통과시키는
    드리프트를 차단한다."""
    board_py = Path(__file__).resolve().parents[1] / ".project_manager" / "tools" / "board.py"
    spec = importlib.util.spec_from_file_location("_board_pin_check", board_py)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.LIVEGATE_RELEASE_PIN == _EXPECTED_RELEASE_TESTS


def test_release_marker_count_is_pinned():
    """`release` 마커 테스트 수(`_RELEASE_TEST_FILES` 합)가 고정값과 일치 — 마커 소실/개명 시 게이트 false-green 방어.

    근거(2026-07-02 실측): 릴리즈 게이트가 wrong-cwd + 잔재 tests/ 로 0개 수집·exit5 를 조용히
    내는 false-green 이 실제 발생. `-m release` selection 에서 마커가 빠진 테스트는 조용히 안 돌고,
    그 부재를 게이트가 못 본다. 이 수집-수 pin 이 마커 소실/개명 클래스를 red 로 세운다(T-0159 보완).
    ADR-0039 로 라이브 tier 가 하나(release)라, `_RELEASE_TEST_FILES` 파일들의 마커를 합산해 pin 한다.
    """
    actual = sum(_count_marked_tests(f, "release") for f in _RELEASE_TEST_FILES)
    assert actual == _EXPECTED_RELEASE_TESTS, (
        f"`release` 마커 테스트 수 {actual} != 기대 {_EXPECTED_RELEASE_TESTS} — 마커 소실/개명 "
        f"의심(게이트 selection 에서 조용히 누락될 위험). 라이브 테스트를 의도적으로 늘렸다면 "
        f"_EXPECTED_RELEASE_TESTS 를 함께 갱신하라."
    )
