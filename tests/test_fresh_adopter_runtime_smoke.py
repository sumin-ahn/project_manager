"""Fresh-adopter *update-path* 라이브 검증 — self-update 후에도 실 LLM 이 출하 문서로 PM 을 운영하나
(release tier · 기본 skip · 사용자 릴리즈 트리거).

ADR-0039 D1 이전엔 이 파일이 구 shipping tier(bootstrap·lifecycle·update 6케이스·pm_handoff 자동
차단)였다. 라이브 tier 를 release 하나로 통합하며 bootstrap/lifecycle 4케이스는
release full wave(`test_release_wave`·new→claim→위임→complete)가 시나리오 superset 이라 폐기하고,
shipping 고유분인 **pm_update self-update 경로 2케이스**(opencode/claude)만 `release` tier 로 승격했다
— 이 파일에 남은 라이브 테스트다.

두 테스트는 import → pm_update(self-update) → 실 LLM 이 *post-update* 진입문서로 ticket 발행을 친다.
import smoke·full wave 가 못 친 update 경로(opencode: .opencode/* @source 재렌더·미해소
opencode_pro_model 을 intentional-TODO 로 graceful 중화·T-0310 / claude: .claude/* @render 재렌더·
leak 0)를 커버한다. 비결정·느림·라이브 → release wave 와 같은
`PM_ORCH_LIVE_RELEASE` 게이트(기본 skip·`PM_ORCH_LIVE_RELEASE=1` 일 때만). 프롬프트에 board.py 경로를
*주지 않는다* — adopter 가 문서만으로 board 도구를 찾아 운영해야 통과(= 문서 운영성). side-effect
(ticket 파일 생성)를 단언하므로 LLM 출력 phrasing 비결정에 강건하다.

이 파일은 그 외 hermetic env 격리 가드(라이브 미실행·매 회귀 통과)도 보유한다 — 라이브 LLM
subprocess 가 부모 env 통째 상속이 아니라 화이트리스트(`_live_env`)로 뜨는지 단언한다. **양 harness**
커버: opencode(로컬/cloud ollama=과금 0·`--dir` 핀)·claude(`claude-sonnet-4-6`·API 과금·cwd 존중).
남는 사용자 게이트는 Windows 플랫폼 특이점(CP949·py 런처·회사 Pro 모델)뿐.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

PM_ORCH_LIVE_RELEASE = os.environ.get("PM_ORCH_LIVE_RELEASE") == "1"
# nudge 주입-도달 on-demand 게이트 (T-0286). release 12-pin 과 **분리** — 이 두 nudge 테스트는
# `@pytest.mark.release` 를 달지 않아(달면 board.LIVEGATE_RELEASE_PIN·_EXPECTED_RELEASE_TESTS 등
# touches 밖 전역 pin 이 깨진다) release 게이트에 안 잡히고, 오직 이 env 로만 on-demand 실행된다.
# (ticket 이 부른 `shipping` marker 는 ADR-0039 로 pytest.ini 에서 등록 제거됨 → 미등록 마커는
# filterwarnings=error 로 수집 에러라 못 쓴다. env-gated skipif = "게이트 아님·CI green 불변" 동치.)
PM_ORCH_LIVE = os.environ.get("PM_ORCH_LIVE") == "1"
# opencode: ollama cloud 모델(glm-5.2:cloud·2026-07-07 채택) — 이 박스 로컬 모델 불가(PM 48차)·
# env override 가능.
LIVE_MODEL = os.environ.get("PM_ORCH_LIVE_MODEL", "ollama/glm-5.2:cloud")
# claude: sonnet-4-6(사용자 지정·API 과금) — env override.
CLAUDE_MODEL = os.environ.get("PM_ORCH_LIVE_CLAUDE_MODEL", "claude-sonnet-4-6")
RUNTIME_TIMEOUT = int(os.environ.get("PM_ADOPTER_RUNTIME_TIMEOUT", "300"))

# LLM subprocess env 화이트리스트 — 부모 셸의 모델 선택 변수(PM_ORCH_LIVE_MODEL·
# PM_ORCH_LIVE_CLAUDE_MODEL·PM_ORCH_LIVE_RELEASE)가 하위 LLM 으로 누수하면 모델 선택이 부모 env
# 의존(비-hermetic·재현성 저하)이 된다. 부모 환경을 통째 상속하지 않고 LLM 바이너리 동작에
# 필수인 것만 통과시킨다. 모델 값은 _live_env(model=...) 가 테스트 의도값으로 명시 set.
# Windows 시스템 env(SystemRoot·ComSpec·PATHEXT·APPDATA 등)는 프로세스 기동 필수 —
# 빠지면 node 기반 CLI(claude 등)가 rc 0xC0000409 무출력 즉사한다(PM 48차 tier2 6/6 실측·
# probe 재현). POSIX 엔 이 키들이 없어 `if k in os.environ` 가드로 자연 무시(항등).
_LIVE_ENV_PASSTHROUGH = (
    "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE",
    # Windows 프로세스 기동 필수 + CLI 자격증명/설정 위치(USERPROFILE/.claude·APPDATA 등):
    "SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC", "PATHEXT", "WINDIR", "PROGRAMDATA",
    "TEMP", "TMP", "APPDATA", "LOCALAPPDATA", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
)


def _live_env(model: str) -> dict[str, str]:
    """LLM subprocess 용 명시 env(화이트리스트) — 부모 env 통째 상속·모델 누수 차단.

    필수 환경(PATH/HOME/로케일·LLM 바이너리 동작에 필요)만 부모에서 통과시키고, 테스트가
    의도한 모델을 `PM_ORCH_LIVE_MODEL` 로 직접 박는다(부모 env 폴백에 의존하지 않음). 부모
    셸에 set 된 PM_ORCH_LIVE_MODEL·PM_ORCH_LIVE_CLAUDE_MODEL·PM_ORCH_LIVE_RELEASE 는 화이트리스트
    밖이라 안 흘러든다 — 누가/어디서 돌려도 같은 모델로 동작(hermetic).
    """
    env = {k: os.environ[k] for k in _LIVE_ENV_PASSTHROUGH if k in os.environ}
    env["PM_NONINTERACTIVE"] = "1"
    env["PM_ORCH_LIVE_MODEL"] = model
    return env


def _make_prompt(entry_doc: str) -> str:
    """adopter 의 진입문서(`entry_doc`)만 보고 board 도구로 ticket 을 발행하라는 프롬프트.

    board.py 경로를 *주지 않는다* — adopter 가 문서만으로 도구를 찾아 운영해야 통과(= 문서 운영성).
    """
    return (
        f"You are the PM for this project. Read {entry_doc} to learn how the project board "
        "works. Then create exactly one ticket titled 'runtime smoke' using the project's board "
        "tool (touches README.md). After it is created, reply with the new ticket id."
    )


def _load_pm_import():
    spec = importlib.util.spec_from_file_location("pm_import", TOOLS / "pm_import.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _import_adopter(tmp_path: Path, harness: str) -> Path:
    """harness adopter 를 tmp 에 import (hermetic·라이브 models 조회 차단)."""
    pm_import = _load_pm_import()
    pm_import._real_models_runner = lambda: (False, [])
    dest = tmp_path / f"adopter-{harness}"
    rc = pm_import.main(
        ["--new", str(dest), "--harness", harness, "--name", "Adopter", "--fill", "manual"]
    )
    assert rc == 0, f"{harness} adopter import 실패 (rc={rc})"
    return dest


def _board_list_recognizes_ticket(dest: Path) -> bool:
    """adopter 의 board.py list 가 ticket(T-)을 인식하는지 (= 형식적으로 유효한 발행).

    `--all --status all` 로 조회한다 — T-0197 이후 인자 없는 `list` 기본은 활성만 보이고 done 은
    접으며(→ `--status all`), ADR-0066(T-0385) 이후 무인자 기본 뷰는 내 스트림 스코프라 미귀속
    open 이 "그 외 open N건" 으로 접힌다(→ `--all` 전체 상세). 발행된 ticket 이 *어느 상태·어느
    스트림에 있든* 형식적으로 유효한지 확인하는 헬퍼라 전체 상세 뷰가 필요하다(T-0199 계보·
    v1.3.2 livegate red 근본원인).
    """
    listing = subprocess.run(
        [sys.executable, str(dest / ".project_manager" / "tools" / "board.py"),
         "list", "--all", "--status", "all"],
        cwd=str(dest),
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
        # 엔진 도구(board.py)는 LLM 이 아니라 모델 선택 무관 — 부모 env 상속 OK.
        # 모델 누수가 문제되는 LLM subprocess(opencode/claude)만 _live_env 로 격리한다.
        env={**os.environ, "PM_NONINTERACTIVE": "1"},
    )
    return "T-" in listing.stdout


def _tickets_in(dest: Path, status: str) -> set[str]:
    """`tickets/<status>/` 의 T-*.md 파일명 집합 (라이프사이클 side-effect 단언용)."""
    status_dir = dest / ".project_manager" / "wiki" / "tickets" / status
    return {p.name for p in status_dir.glob("T-*.md")} if status_dir.exists() else set()


# ── 채택자 *update* 경로 라이브 검증 (T-0133·activation 이 update 를 깨지 않는가) ──────────────
# 아래 두 테스트는 import 와 operate 사이에 *채택자 self-update*(pm_update)를 끼워, 활성화
# (@render·@target-owned·모델 폴백 중화)가 바꾼 *update 경로*를 라이브로 검증한다:
# import → pm_update(self-update) → 실 LLM 이 *post-update* 진입문서로 ticket 발행. import smoke 가
# 못 친 update 경로(opencode: .opencode/* @source 재렌더·미해소 모델토큰 graceful 중화 / claude:
# .claude/* @render 재렌더)를 커버한다(회귀·rc 실측·codex 의 *기계* 검증 위에 *런타임* 층 1개 더).


def _self_update(dest: Path) -> subprocess.CompletedProcess:
    """채택자 디렉토리에서 self-update(pm_update) 실행 — 채택자 update 흐름 (hermetic).

    `--from str(REPO)` = 로컬 worktree 를 명시한다. pm_import 가 기록하는 기본 upstream 은
    릴리즈 추적용 **URL**(git@github.com:…)이고, 엔진 pm_update 는 URL 을 git clone/fetch
    하지 않는다(ADR-0032 D5 — URL→cache clone 은 `pm-update` 스킬 책임). 실 채택자는 그 스킬을
    쓰지만 테스트는 스킬(LLM/facade)을 못 돌리므로, 스킬이 cache clone 후 하는 "로컬 checkout
    에서 sync" 단계를 `--from <로컬 worktree>` 로 hermetic 근사한다. (이 명시가 없으면 URL
    upstream 에서 엔진이 rc 1 로 거부 → 게이트 영구 red — 출하 테스트 도그푸드가 포착.)
    """
    return subprocess.run(
        [sys.executable, str(dest / ".project_manager" / "tools" / "pm_update.py"),
         "--from", str(REPO)],
        cwd=str(dest), capture_output=True, text=True, encoding="utf-8", errors="replace",
        # 엔진 도구(pm_update.py)는 LLM 이 아니라 모델 선택 무관 — 부모 env 상속 OK.
        # 모델 누수가 문제되는 LLM subprocess(opencode/claude)만 _live_env 로 격리한다.
        env={**os.environ, "PM_NONINTERACTIVE": "1"},
    )


@pytest.mark.release
@pytest.mark.skipif(
    not PM_ORCH_LIVE_RELEASE or not shutil.which("opencode"),
    reason="release wave — PM_ORCH_LIVE_RELEASE=1 + opencode CLI(+ollama 모델) 필요. 기본 skip·사용자 트리거.",
)
def test_live_opencode_adopter_survives_pm_update_then_operates(tmp_path):
    """opencode 채택자가 self-update 후에도 안 깨지고 board 운영 가능 (.opencode/* @source 재렌더 경로)."""
    pm_import = _load_pm_import()
    dest = _import_adopter(tmp_path, "opencode")
    # 1) self-update: .opencode/agents·command 는 @source(ADR-0054·templates/opencode) 재렌더로
    #    *전파*된다(옛 @target-owned skip 아님). 이 채택자는 opencode 없이 import(=_import_adopter 가
    #    _real_models_runner 를 (False,[]) 스텁) 해 local.conf 에 opencode_pro_model 이 미해소다 —
    #    @source 재렌더가 `{{OPENCODE_PRO_MODEL}}` 을 만나면 T-0310 전엔 leak 으로 rc-fail(update 전멸)
    #    이었다. 이제 render_adapter 가 import 와 대칭으로 intentional-TODO 중화 → rc0(엔진·어댑터 정상
    #    update·crash/clobber 0). (릴리즈 라이브 게이트가 정확히 이 회귀를 포착한 지점.)
    upd = _self_update(dest)
    assert upd.returncode == 0, (
        f"opencode 채택자 pm_update 실패(rc={upd.returncode}) — activation 이 update 경로를 깸.\n"
        f"--- stdout ---\n{upd.stdout[-1500:]}\n--- stderr ---\n{upd.stderr[-800:]}"
    )
    # @source 재렌더 산출물엔 리터럴 모델 토큰 0 — 미해소 OPENCODE_PRO_MODEL 은 leak 이 아니라
    # `# model: "<provider/model>"  # TODO:` 로 graceful 중화(자족 유지·render leak 0·T-0310).
    dev_text = (dest / ".opencode" / "agents" / "developer.md").read_text(encoding="utf-8")
    assert pm_import.OPENCODE_MODEL_TOKEN not in dev_text, \
        "pm_update 후 .opencode/agents 에 리터럴 모델 토큰 잔존(@render leak·중화 안 됨)"
    # 2) post-update 운영성: 실 opencode 가 update 후 AGENTS.md 로 ticket 발행.
    open_dir = dest / ".project_manager" / "wiki" / "tickets" / "open"
    before = {p.name for p in open_dir.glob("T-*.md")}
    proc = subprocess.run(
        ["opencode", "run", "--agent", "build", "--dir", str(dest), "-m", LIVE_MODEL,
         _make_prompt("AGENTS.md")],
        cwd=str(dest), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=RUNTIME_TIMEOUT,
        env=_live_env(LIVE_MODEL),
    )
    created = {p.name for p in open_dir.glob("T-*.md")} - before
    assert created, (
        "pm_update 후 실 opencode 가 ticket 을 발행하지 못함 — update 후 운영성 실패.\n"
        f"--- opencode stdout(tail) ---\n{proc.stdout[-2000:]}\n--- stderr(tail) ---\n{proc.stderr[-1000:]}"
    )
    assert _board_list_recognizes_ticket(dest), "pm_update 후 board.py list 가 실 opencode 발행 ticket 미인식"


@pytest.mark.release
@pytest.mark.skipif(
    not PM_ORCH_LIVE_RELEASE or not shutil.which("claude"),
    reason="release wave — PM_ORCH_LIVE_RELEASE=1 + claude CLI 필요(API 과금). 기본 skip·사용자 트리거.",
)
def test_live_claude_adopter_survives_pm_update_then_operates(tmp_path):
    """claude 채택자가 self-update(.claude/* @render 재렌더) 후에도 안 깨지고 board 운영 가능."""
    dest = _import_adopter(tmp_path, "claude")
    # 1) self-update: .claude/* 는 @render(framework-root=claude 가 source 보유) 재렌더·rc0.
    upd = _self_update(dest)
    assert upd.returncode == 0, (
        f"claude 채택자 pm_update 실패(rc={upd.returncode}) — activation 이 update 경로를 깸.\n"
        f"--- stdout ---\n{upd.stdout[-1500:]}\n--- stderr ---\n{upd.stderr[-800:]}"
    )
    # 재렌더 산출물에 리터럴 토큰 0(self-containment·operational 해소).
    dev_text = (dest / ".claude" / "agents" / "developer.md").read_text(encoding="utf-8")
    import re as _re
    assert not _re.search(r"\{\{[A-Z_]+\}\}", dev_text), \
        "pm_update 재렌더 후 .claude/agents 에 미해소 토큰 잔존(leak)"
    # 2) post-update 운영성: 실 claude 가 update 후 CLAUDE.md 로 ticket 발행.
    open_dir = dest / ".project_manager" / "wiki" / "tickets" / "open"
    before = {p.name for p in open_dir.glob("T-*.md")}
    proc = subprocess.run(
        ["claude", "-p", "--model", CLAUDE_MODEL, "--allowedTools", "Bash",
         "--dangerously-skip-permissions", _make_prompt("CLAUDE.md")],
        cwd=str(dest), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=RUNTIME_TIMEOUT,
        env=_live_env(CLAUDE_MODEL),
    )
    created = {p.name for p in open_dir.glob("T-*.md")} - before
    assert created, (
        "pm_update 후 실 claude 가 ticket 을 발행하지 못함 — update 후 운영성 실패.\n"
        f"--- claude stdout(tail) ---\n{proc.stdout[-2000:]}\n--- stderr(tail) ---\n{proc.stderr[-1000:]}"
    )
    assert _board_list_recognizes_ticket(dest), "pm_update 후 board.py list 가 실 claude 발행 ticket 미인식"


# ── env 격리 가드 (라이브 실행 없이·hermetic — T-0155) ──────────────────────────
# 위 라이브 테스트들은 LLM subprocess 를 `env=_live_env(model)` 로 띄운다 — 부모 env 통째 상속
# 대신 화이트리스트. 아래 가드는 부모 셸에 모델 선택 변수를 오염시킨 채로도 그 부모 값이 LLM env
# 로 안 흘러듦을 단언한다(라이브 미호출·env 빌더/subprocess 인자 단위). 격리를 떼면 fail(sensitivity).

# 부모 셸에서 새면 모델 선택을 비-hermetic 하게 만드는 누수 변수 — 단일 진실.
# 가드들이 이 목록을 순회해 오염·누수0 을 단언하므로, 미래에 누수 변수가 늘면 여기 한 곳만
# 갱신하면 가드가 자동 커버한다. 각 값에 "leaked" 토큰을 박아 누수 시 env 에서 검출.
_LEAK_VARS = ("PM_ORCH_LIVE_MODEL", "PM_ORCH_LIVE_CLAUDE_MODEL", "PM_ORCH_LIVE_RELEASE")
_LEAK_SENTINEL = "leaked-by-parent"


def _pollute_parent_leak_vars(monkeypatch) -> None:
    """부모 셸 오염 모사 — 모든 누수 변수에 sentinel 토큰을 박는다(_LEAK_VARS 단일 진실)."""
    for var in _LEAK_VARS:
        monkeypatch.setenv(var, _LEAK_SENTINEL)


def test_live_env_does_not_leak_parent_model_vars(monkeypatch):
    """부모 env 에 모델 선택 변수가 오염돼 있어도 _live_env 결과로 그 부모 값이 안 새어든다."""
    _pollute_parent_leak_vars(monkeypatch)

    env = _live_env("test/intended-model")

    # 모델 값은 테스트가 의도한 것 — 부모의 sentinel 값이 아니다.
    assert env["PM_ORCH_LIVE_MODEL"] == "test/intended-model"
    # 어떤 누수 변수의 부모(sentinel) 값도 env 로 새지 않는다(_LEAK_VARS 순회).
    for var in _LEAK_VARS:
        assert env.get(var) != _LEAK_SENTINEL, f"부모 {var} 값이 _live_env 로 누수됨"
    # 부모의 sentinel 토큰이 env 어느 값에도 없다.
    assert _LEAK_SENTINEL not in repr(env)


def test_live_env_includes_required_runtime_env(monkeypatch):
    """화이트리스트가 LLM 바이너리 동작 필수 환경(PATH·HOME·PM_NONINTERACTIVE)을 포함한다.

    PATH/HOME/로케일 누락 시 LLM 실행 자체가 깨지므로 격리가 동작을 망가뜨리지 않는지 가드.
    """
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HOME", "/home/tester")

    env = _live_env("test/model")

    assert env["PATH"] == "/usr/bin:/bin"
    assert env["HOME"] == "/home/tester"
    assert env["PM_NONINTERACTIVE"] == "1"


def test_live_env_passthrough_is_explicit_whitelist(monkeypatch):
    """부모의 임의 변수는 화이트리스트 밖이면 통과 안 함(부모 통째 상속 아님)."""
    monkeypatch.setenv("SOME_UNRELATED_PARENT_VAR", "should-not-pass")

    env = _live_env("test/model")

    assert "SOME_UNRELATED_PARENT_VAR" not in env
    # env 키는 화이트리스트 ∪ {PM_NONINTERACTIVE, PM_ORCH_LIVE_MODEL} 부분집합.
    allowed = set(_LIVE_ENV_PASSTHROUGH) | {"PM_NONINTERACTIVE", "PM_ORCH_LIVE_MODEL"}
    assert set(env).issubset(allowed)


def test_live_calls_use_isolated_env(monkeypatch):
    """라이브 호출 2개가 부모 env 통째 상속이 아니라 _live_env(화이트리스트)로 LLM 을 띄운다.

    실 LLM 을 띄우지 않고(subprocess.run 을 가로채) 각 라이브 테스트가 LLM 호출에 넘기는 env 가
    부모 누수 변수를 안 담음을 단언한다. 격리(env=_live_env)를 떼면 부모 통째 상속이 되어 fail
    (sensitivity). adopter import·board list·pm_update 등 LLM 이 아닌 subprocess 는 통과시킨다.
    """
    # 부모 셸 오염 — 격리가 없으면 이 값들이 LLM env 로 샌다(_LEAK_VARS 단일 진실).
    _pollute_parent_leak_vars(monkeypatch)

    real_run = subprocess.run
    llm_envs: list[dict | None] = []

    def _spy_run(cmd, *args, **kwargs):
        # LLM 바이너리 호출만 env 를 포착(import/board/pm_update 는 실행 그대로).
        if cmd and cmd[0] in ("opencode", "claude"):
            llm_envs.append(kwargs.get("env"))
            # 라이브 LLM 은 실제로 띄우지 않는다 — 빈 성공 응답으로 대역.
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _spy_run)
    # 게이트 우회 — 라이브 분기 진입(실 LLM 은 spy 가 막음). which 도 통과시킨다.
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/stub")

    import importlib
    mod = importlib.import_module(__name__)
    monkeypatch.setattr(mod, "PM_ORCH_LIVE_RELEASE", True)

    # tmp_path 대신 직접 만든 임시 디렉토리로 2개 라이브 테스트 함수를 spy 하에 구동.
    import tempfile
    live_tests = [
        test_live_opencode_adopter_survives_pm_update_then_operates,
        test_live_claude_adopter_survives_pm_update_then_operates,
    ]
    for fn in live_tests:
        with tempfile.TemporaryDirectory() as td:
            try:
                fn(Path(td))
            except AssertionError:
                # 라이브 LLM 을 stub 으로 막아 발행 단언은 실패할 수 있다 — 여기선 env 만 관심.
                pass

    assert llm_envs, "LLM subprocess 호출이 한 건도 포착되지 않음 — spy 배선 오류"
    for env in llm_envs:
        assert env is not None, "LLM 호출에 env 미전달(부모 통째 상속) — 격리 깨짐"
        # 어떤 누수 변수의 부모(sentinel) 값도 LLM env 로 새지 않는다(_LEAK_VARS 순회).
        for var in _LEAK_VARS:
            assert env.get(var) != _LEAK_SENTINEL, \
                f"부모의 {var} 이 LLM env 로 누수됨 — 격리 깨짐"


# ── graceful nudge 주입-도달 라이브 (probe·on-demand·T-0286·[[ADR-0037]]·[[T-0328]]) ────────────
# T-0183(PM 59)이 *수동 1회* 관찰한 "nudge 주입이 모델 컨텍스트에 실제 도달" 을 재현 가능한 durable
# 자동 테스트로 박제한다. Tier1(test_claude/opencode_ctx_guard)이 주입 스키마·멱등·2단 독립·빌더를
# 결정적으로 커버 — 여기선 *하니스 전달의 라이브 회귀*만(비결정·과금·flaky → PM_ORCH_LIVE on-demand).
#
# probe 방식([[verify-real-output-not-just-review]]): 라이브 세션 후 nudge 텍스트가 모델에 도달했는지
# 를 behavioral 관찰한다 — 주입문에만 있는 distinctive 토큰 `ctx-nudge`(1단 `[ctx-nudge]`·2단
# `[ctx-nudge/최종]` 공통 substring)를 모델이 인용하는지. `.nudge` file-marker 는 단언하지 않는다
# (T-0286 결정: marker 는 훅이 자기 판단용으로 쓸 뿐 도달 증거가 아님).
#
# 밴드 강제 = 격리 인스턴스 + per-harness 키([[ctx-guard-live-test-isolate-instance]]): adopter 는
# 별도 import tmp 라 이미 PM 홈과 격리돼 있고, 그 위에 `ctx_window_tokens_<harness>` 만 써 generic
# `ctx_window_tokens` 를 안 건드린다(자기 claude 세션 hard-stop 무발화). loadConf 프로세스-캐싱은
# 매 subprocess(fresh)가 conf 를 새로 읽어 무관 — config 를 spawn *전*에 박는다.


def _force_nudge_band_conf(dest: Path, harness: str) -> None:
    """adopter local.conf 에 nudge 밴드 강제 conf 를 append (격리·per-harness 키·generic 미변경).

    큰 예산(500K) + 넓은 nudge 밴드(nudge 99·stop 1): 실 세션(수천~수만 토큰)이 stop(잔여 ≤1·
    used ≥99%)은 절대 못 넘고 nudge(잔여 ≤99·>1·used ≥1% = 토큰 ≥5000)엔 안정적으로 진입한다.
    nudge2(잔여 ≤4)엔 안 닿아 soft(1단) 주입이 발화 — 주입 채널은 2단과 동일(Tier1 커버)이라
    도달 증거로 충분(공통 `ctx-nudge` 토큰). local.conf 파싱은 last-wins 라 append 값이 우선.
    """
    conf_path = dest / ".project_manager" / "local.conf"
    conf_path.parent.mkdir(parents=True, exist_ok=True)
    with conf_path.open("a", encoding="utf-8") as fh:
        fh.write(
            "\n# T-0286 nudge 밴드 강제 (격리·per-harness 키·generic ctx_window_tokens 미변경)\n"
            "ctx_nudge_pct=99\n"
            "ctx_stop_pct=1\n"
            f"ctx_window_tokens_{harness}=500000\n"
        )


# probe 프롬프트 공통 꼬리 — 주입문 도달을 **모델-생산 sentinel**(`NUDGE_REACHED`)로 관찰.
# distinctive 토큰(`ctx-nudge`) 그 자체를 grep 하면 --format json 이 주입문을 stdout 에 직렬화할 때
# false-green(주입문 자체를 검출) 이 난다. sentinel 은 주입문·문서엔 없지만 **이 probe 프롬프트엔
# 있다** — 검증력은 default 포맷이 프롬프트를 stdout 에 echo 하지 않는다는 실측 성질에 의존한다
# (프롬프트-echo 포맷[--format json]에선 무력화·codex 지적 반영). 그 전제 아래 모델은 컨텍스트에
# ctx-nudge 가 실재할 때만 NUDGE_REACHED 를 낸다 = 주입이 모델에 도달.
_NUDGE_PROBE_TAIL = (
    "그런 다음, 이번 세션의 시스템 메시지나 추가 컨텍스트에 'ctx-nudge' 라는 토큰이 문자 그대로 "
    "들어와 있는지 확인하라(추측 말고 실제로 있는지만). 있으면 정확히 'NUDGE_REACHED' 한 단어로 "
    "시작해 그 문장을 이어 인용하라. 없으면 정확히 'NO_NUDGE' 라고만 답하라."
)
_NUDGE_PROBE_MARKER = "NUDGE_REACHED"  # 모델-생산 sentinel(주입문/문서에 없음) — false-green-proof 도달 판정.


def _run_opencode_nudge(dest: Path, attempts: int = 2) -> str:
    """opencode nudge 세션 — 도구 호출로 2번째 모델 턴을 강제(그 턴의 system.transform 이 주입 소비).

    opencode 는 수 분 병리적 task hang(upstream·tool-loop 데드락) 이 있어 timeout + 1회 재시도까지만
    (PM 지침·실측: opencode 1.17.19 + glm-5.2 에서 tool 사용 세션이 0바이트로 걸림·`--pure` 로 플러그인
    빼도 걸림=upstream·플러그인 무관). 그 hang 이 풀린 환경에서만 이 라이브가 통과한다(default-skip 이라
    회귀 무영향). 메커니즘 자체는 Tier1(node computeCtxState nudge2·buildNudge2Guidance·system.transform)
    + T-0283 로드게이트 + claude 라이브(동일 채널·주입-도달 실증)로 커버.

    NOTE(플래그): `--pure` 는 안 쓴다 — `--pure`=external plugin 미로드라 ctx-guard 플러그인(주입 메커니즘
    자체)이 안 떠 nudge 가 발화 안 한다(실측). 격리는 fresh import adopter + `--dir <절대>`로 충분.
    `--format json` 도 안 쓴다 — json 이벤트 스트림이 *user prompt*(sentinel 포함)를 stdout 에 직렬화해
    false-green(주입 아닌 프롬프트 echo 검출) 이 난다(실측). default 포맷은 모델 rendered 응답만 찍어
    sentinel 검출이 곧 모델 도달이다(prompt echo 없음·실측 BASELINE).
    """
    prompt = (
        "먼저 셸 도구로 `python3 .project_manager/tools/board.py list` 를 실행해 보드를 확인하라. "
        + _NUDGE_PROBE_TAIL
    )
    cmd = ["opencode", "run", "--agent", "build", "--dir", str(dest), "-m", LIVE_MODEL, prompt]
    last = ""
    for attempt in range(attempts):
        try:
            proc = subprocess.run(
                cmd, cwd=str(dest), capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=RUNTIME_TIMEOUT, env=_live_env(LIVE_MODEL),
            )
            return (proc.stdout or "") + (proc.stderr or "")
        except subprocess.TimeoutExpired:
            last = f"opencode timeout(>{RUNTIME_TIMEOUT}s·attempt {attempt + 1}/{attempts})"
    pytest.fail(f"opencode nudge 세션이 모두 timeout — upstream hang 의심. {last}")


@pytest.mark.skipif(
    not PM_ORCH_LIVE or not shutil.which("opencode"),
    reason="nudge 주입-도달 on-demand — PM_ORCH_LIVE=1 + opencode CLI(+glm-5.2) 필요. 기본 skip·CI green 불변.",
)
def test_live_opencode_nudge_injection_reaches_model(tmp_path):
    """실 opencode 세션서 graceful nudge 주입(experimental.chat.system.transform)이 모델에 도달 (T-0286).

    밴드 강제(격리·per-harness 키) → 도구 호출로 2번째 턴 유발 → 그 턴 system[] 에 nudge 주입 →
    모델이 `ctx-nudge` 토큰 인용. probe 관찰(behavioral)이라 LLM phrasing 비결정에 강건(토큰 도달만 본다).
    """
    dest = _import_adopter(tmp_path, "opencode")
    _force_nudge_band_conf(dest, "opencode")
    out = _run_opencode_nudge(dest)
    assert _NUDGE_PROBE_MARKER in out, (
        "opencode nudge 주입이 모델 컨텍스트에 도달하지 못함 (system.transform 미주입/모델 미인용).\n"
        f"--- opencode 출력(tail) ---\n{out[-2500:]}"
    )


@pytest.mark.skipif(
    not PM_ORCH_LIVE or not shutil.which("claude"),
    reason="nudge 주입-도달 on-demand — PM_ORCH_LIVE=1 + claude CLI(API 과금) 필요. 기본 skip·CI green 불변.",
)
def test_live_claude_nudge_injection_reaches_model(tmp_path):
    """실 claude 세션서 graceful nudge 주입(UserPromptSubmit additionalContext)이 모델에 도달 (T-0286).

    claude 는 UserPromptSubmit 에서만 주입하고 그 훅은 transcript 의 assistant usage 로 used% 를
    잰다 — fresh -p 의 첫 프롬프트는 transcript 가 비어 used 0(주입 없음). 그래서 (1) seed 로
    transcript 를 쌓고 (2) `--continue` 로 이어, 2번째 UserPromptSubmit 이 밴드에 들어 주입되게 한다.
    """
    dest = _import_adopter(tmp_path, "claude")
    _force_nudge_band_conf(dest, "claude")
    # 1) seed: transcript 축적(첫 UserPromptSubmit=빈 transcript→used 0→nudge 미발화·세션 정상 진행).
    seed = subprocess.run(
        ["claude", "-p", "--model", CLAUDE_MODEL, "--allowedTools", "Bash",
         "--dangerously-skip-permissions",
         "CLAUDE.md 를 읽고 `python3 .project_manager/tools/board.py list` 를 실행한 뒤 이 프로젝트를 "
         "한 문장으로 요약하라."],
        cwd=str(dest), capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=RUNTIME_TIMEOUT, env=_live_env(CLAUDE_MODEL),
    )
    assert seed.returncode == 0, (
        f"claude seed 실패(rc={seed.returncode}) — transcript 축적 불가.\n{seed.stderr[-1000:]}"
    )
    # 2) continue: transcript 가 쌓여 이번 UserPromptSubmit 이 nudge 밴드 → additionalContext 주입.
    probe = subprocess.run(
        ["claude", "-p", "--continue", "--model", CLAUDE_MODEL, "--allowedTools", "Bash",
         "--dangerously-skip-permissions", "새 도구를 실행하지 마라. " + _NUDGE_PROBE_TAIL],
        cwd=str(dest), capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=RUNTIME_TIMEOUT, env=_live_env(CLAUDE_MODEL),
    )
    out = (probe.stdout or "") + (probe.stderr or "")
    assert _NUDGE_PROBE_MARKER in out, (
        "claude nudge 주입이 모델 컨텍스트에 도달하지 못함 (UserPromptSubmit additionalContext 미도달/미인용).\n"
        f"--- claude 출력(tail) ---\n{out[-2500:]}"
    )


# ── codex fresh-adopter 기계 e2e (스캐폴드→lint→부트스트랩 카드→trust 안내·hermetic·T-0408) ────
# codex(세 번째 하네스·ADR-0070) 신선 채택자 게이트의 *기계* 층: 템플릿 사본에서 import →
# 스캐폴드 트리 단언 → board.py lint clean → import 된 pm_bootstrap 이 codex env 감지 시 카드
# codex 절을 발화 → import 출력에 trust loud 2단계 안내. [[feature-ship-needs-fresh-adopter-gate]]
# — diff-scoped 리뷰·source-parity 는 *출하 template* 이 import 파이프라인을 지나 실제로 작동하는지
# (누락·미렌더·부트스트랩 미발화)를 못 본다. 라이브 LLM 0(hermetic·결정적·codex CLI 미실행)이라 매
# 회귀 자동 포함 — codex 라이브 축(부트스트랩 실 LLM 구동)은 T-0407(별 파일·중복 실행 회피).
# claude/opencode 동급 기계 게이트는 test_fresh_adopter_e2e.py 에 있고, codex 는 고유분이 커
# (네임스페이스 둘 `.codex`+`.agents`·정적 진입 doc 없음[C-v2]·trust 2단계) 이 전용 시나리오로 못박는다.

_CODEX_AGENT_TOMLS = ("architect", "code-reviewer", "developer", "researcher")
# canonical PM-workflow 스킬 수 (`.agents/skills/*/SKILL.md`·ADR-0065 단일 소비·@source remap·D2).
#   root `.claude/skills` 디렉토리 수와 동일해야 한다(전파 채널 = @source·codex 네임스페이스 remap).
_CODEX_SKILL_COUNT = len(list((REPO / ".claude" / "skills").glob("*/SKILL.md")))

# 부트스트랩 카드에 넘길 합성 정체성 (test_pm_bootstrap_card.LEAN_IDENTITY 동형·순수 함수 입력·I/O 0).
#   슬롯 카드 경로(task/readonly 아님)로 렌더돼 끝에 codex 절이 append 된다.
_CODEX_CARD_IDENTITY = {
    "repo": "adopter", "session": "adopter_1", "slot": "work/adopter_1",
    "slot_path": "/tmp/x/work/adopter_1", "branch": "main", "others": [], "protected_branch": None,
}


def _load_adopter_tool(dest: Path, name: str):
    """채택자 트리에 *import 된* 엔진 도구를 모듈로 로드한다 (출하 사본이 실제로 동작하는지).

    canonical(REPO) 이 아니라 dest(import 산출)의 사본을 로드해, import 파이프라인이 pm_bootstrap
    을 온전히 전달했고 그 사본이 codex 절을 렌더함을 e2e 로 못박는다(source-parity 를 넘는 층).
    고유 모듈명으로 로드하고 sys.modules 에 등록하지 않아 다른 테스트의 동명 로드와 격리된다.
    """
    spec = importlib.util.spec_from_file_location(
        f"adopter_{name}", dest / ".project_manager" / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_fresh_codex_adopter_scaffold_lints_clean_and_bootstrap_card(tmp_path, monkeypatch, capsys):
    """codex import → 스캐폴드 트리 → lint clean → 부트스트랩 카드 codex 절 + trust loud 안내 (hermetic).

    codex 신선 채택자 게이트의 기계 e2e — 라이브 LLM 0. (1) 어댑터 스캐폴드가 전부 landing(위임 4축
    TOML·스킬 remap·공통 코어 AGENTS.md·config/hooks·relay·루트 .gitignore) (2) adopter board.py
    blocking lint clean (3) import 된 pm_bootstrap 이 codex env 감지 시 카드 codex 절 발화(정적 진입
    doc 없는 C-v2 에서 카드가 유일 실행모델/위임 전달 채널) (4) import 출력에 trust loud 2단계 안내
    (미승인 시 위임/훅 死·D5). codex CLI 미실행·결정적이라 매 회귀 자동 포함된다.
    """
    dest = _import_adopter(tmp_path, "codex")
    import_out = capsys.readouterr().out

    # (1) 스캐폴드 트리 — 어댑터 4축 TOML·스킬 remap·공통 코어 AGENTS.md·config/hooks·relay·루트 .gitignore.
    agents_dir = dest / ".codex" / "agents"
    for name in _CODEX_AGENT_TOMLS:
        assert (agents_dir / f"{name}.toml").is_file(), f"codex 위임 축 미landing: {name}.toml"
    # PM = 메인세션(D1) — opencode 의 pm.md(primary) 에 해당하는 pm.toml 부재가 결정(load-bearing 부재).
    assert not (agents_dir / "pm.toml").exists(), "codex 는 PM=메인세션 — pm.toml 부재가 결정(D1)"
    skills = list((dest / ".agents" / "skills").glob("*/SKILL.md"))
    assert len(skills) == _CODEX_SKILL_COUNT, (
        f"codex `.agents/skills` SKILL.md {len(skills)}개 (기대 {_CODEX_SKILL_COUNT}·canonical @source remap 전수)")
    agents_md = dest / "AGENTS.md"
    assert agents_md.is_file(), "codex 공통 코어 AGENTS.md 미landing"
    assert "공통 코어" in agents_md.read_text(encoding="utf-8"), \
        "AGENTS.md 가 harness-neutral 공통 코어(ADR-0069)가 아님"
    for rel in (".codex/config.toml", ".codex/hooks.json", ".codex/pm_orch_codex.py"):
        assert (dest / rel).is_file(), f"codex 어댑터 파일 미landing: {rel}"
    # 루트 .gitignore 스캐폴드 파리티 (claude/opencode 는 출하·instance-owned·T-0402 관찰 편입).
    assert (dest / ".gitignore").is_file(), (
        "codex 채택자에 루트 .gitignore 스캐폴드 미landing (claude/opencode 는 출하·파리티 갭)")

    # (2) 부팅 lint clean — 출하 wiki doc 에 dangling framework `[[ADR/T]]`·thin 누출 0(blocking `--gate`
    #     exit 0). fresh adopter 는 seen-unset 관찰불가 advisory(never-block·option-a)라 blocking 은
    #     `--gate` 로 확인한다(test_fresh_adopter_e2e 동형).
    lint = subprocess.run(
        [sys.executable, str(dest / ".project_manager" / "tools" / "board.py"), "lint", "--gate"],
        cwd=str(dest), capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, "PM_NONINTERACTIVE": "1"},
    )
    assert lint.returncode == 0, (
        f"codex adopter blocking lint 비-clean(`--gate` exit {lint.returncode}) — 출하 doc dangling "
        f"[[ADR/T]]·thin 누출?\n--- stdout ---\n{lint.stdout}\n--- stderr ---\n{lint.stderr}")

    # (3) 부트스트랩 카드 codex 절 — import 된 채택자 pm_bootstrap 이 codex env 감지 시 실행모델/위임
    #     지침을 카드로 발화한다(C-v2·정적 진입 doc 없음·카드=유일 전달 채널). env monkeypatch 로 감지를
    #     켜고 순수 카드 헬퍼(I/O 0·bare `__new__` 인스턴스)를 렌더 — test_pm_bootstrap_card._card 동형.
    bootstrap = _load_adopter_tool(dest, "pm_bootstrap")
    monkeypatch.setenv("CODEX_THREAD_ID", "019f8003-d535-7a10-adopter")
    inst = bootstrap.PmBootstrap.__new__(bootstrap.PmBootstrap)
    card = inst._build_command_card_markdown(_CODEX_CARD_IDENTITY)
    assert "# codex 하네스" in card, \
        "import 된 pm_bootstrap 카드에 codex 절 미출현 (env 감지/append 실패·전파 손상?)"
    assert ".codex/agents/{architect,developer,code-reviewer,researcher}" in card, \
        "codex 절에 위임 4축(세션 내 spawn) 미기재"
    assert "codex exec --agent" in card, "codex 절에 `codex exec --agent` 부재 명시 누락(외부 프로세스 위임 없음)"
    assert "trust 2단계" in card, "codex 절에 trust 2단계 힌트 미기재"

    # (4) trust loud 안내 — import 출력에 2단계 trust 승인 loud 안내(미승인 시 위임 spawn·PreCompact 훅
    #     死·D5). `-c` CLI override 는 안 먹어 대화형 승인이 유일 경로 — 안내가 눈에 띄어야(loud) 한다.
    assert "trust" in import_out and "/hooks" in import_out, (
        f"codex import 출력에 loud trust 2단계 안내 부재 (D5·_print_codex_trust_guidance):\n"
        f"--- import 출력(tail) ---\n{import_out[-800:]}")
