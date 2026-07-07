r"""ctx 래퍼(.sh) 런타임 smoke — statusline/ctx_stop 발화의 *래퍼 경유* 자동 가드 (T-0215).

`test_claude_ctx_guard.py` 는 backbone `.py`(ctx_guard·ctx_statusline·ctx_stop_hook)를 importlib
로 직접 로드해 검증한다 — 하지만 실제 배선은 settings.json → **래퍼(.sh)** → 인터프리터 self-resolve
→ `.py` exec 다(T-0202). 그 *래퍼 경유* 런타임 경로엔 자동 가드가 0 이었다: PM 48차 Windows 실측에서
죽어 있던 층이 정확히 이 래퍼(rc126)인데, [[T-0209]] fix 후 자동 가드는 run_tests_hook 만 생겼고
([[T-0210]]), ctx 래퍼 2종은 수동 실측뿐이었다.

회귀 클래스: 래퍼가 인터프리터를 *실행검증*(`--version` rc)이 아니라 구 *존재검증*(`command -v
python3`)으로 되돌리면, Windows WindowsApps 가짜 python3 shim(command -v 통과·실행 시 rc126)을 못
걸러 `.py` 가 발화되지 못하고 hard-stop 게이트가 조용히 죽는다. 이 테스트는 래퍼 경유 발화를
결정적으로 박제해 그 회귀를 잡는다(개발 중 sensitivity 실측: 래퍼를 구 패턴으로 되돌리면 이 박스에서
statusline/stop 이 발화 실패 → 여기 단언이 fail).

하니스는 `test_run_tests_hook.py` 패턴을 미러한다: `shutil.which("bash")` 절대경로(WSL 런처 아닌
Git Bash·Windows-form 경로 일관)·hermetic tmp repo(.claude/ 래퍼+backbone 사본, 실 repo 무오염)·
인터프리터 PATH 구성. stdin JSON 은 json.dumps 로 만들어 실제 하네스와 동일한 유효 이스케이프를 준다.

POSIX 의미 보존: 래퍼는 python3→python 폴백이라 POSIX 에선 python3(=러너 인터프리터 심링크)로
`.py` 를 exec 한다 — statusline `ctx 50%`·stop block/deny/pass 단언이 동일하게 유효하다. transcript
mount(`/c/..`)형은 Windows Git-Bash 전용이라 POSIX 에선 native 문자열과 동일(마운트 개념 없음).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CLAUDE = REPO / "templates" / "claude_code" / ".claude"

# 래퍼(.sh) 2종 + 그들이 exec 하는 backbone(.py) + 공유 코어 — hermetic repo 에 통째로 복사한다.
_STATUSLINE_WRAPPER = "ctx_statusline.sh"
_STOP_WRAPPER = "ctx_stop_hook.sh"
_CLAUDE_FILES = (
    _STATUSLINE_WRAPPER, _STOP_WRAPPER,
    "ctx_statusline.py", "ctx_stop_hook.py", "ctx_guard.py",
)

# subprocess 는 CreateProcess 검색순상 System32\bash.exe(WSL 런처)를 PATH 의 Git Bash 보다 먼저 집는데
# WSL bash 는 `/mnt/c/…` 마운트라 Windows-form 경로를 못 연다 — shutil.which("bash")(=PATH 순=Git Bash)
# 절대경로로 실행해 일관된 POSIX 셸을 쓴다(test_run_tests_hook 과 동일 소스·Linux 는 /usr/bin/bash).
BASH = shutil.which("bash")
requires_bash = pytest.mark.skipif(BASH is None, reason="bash 부재 — POSIX/Git Bash 래퍼 e2e 불가")

IS_WINDOWS = os.name == "nt"

# transcript 점유 토큰 — 190_000 / 기본 윈도 200_000 = 95% used → 잔여 5 <= stop 20(기본·T-0207) → stop.
_STOP_INPUT_TOKENS = 190_000
# statusline ok 밴드 — current_usage 100_000 / 기본 예산 200_000 = 50% used → 잔여 50 > nudge 30 →
# ok(회색 `ctx 50%`·정지문구 없음). ADR-0041: 분모=예산·used_tokens=current_usage(물리 window% 폐기).
_OK_USED_TOKENS = 100_000


# ── hermetic repo + 인터프리터 env (test_run_tests_hook 미러) ──────────────────

def _make_ctx_repo(tmp_path: Path) -> tuple[Path, Path]:
    """래퍼가 기대하는 최소 repo — `.claude/` 에 래퍼+backbone 사본 + repo_root 앵커.

    래퍼는 자기 위치(.claude/)에서 hook_dir 를 self-resolve 해 옆 `.py` 를 exec 하고, backbone 은
    `ctx_guard.repo_root` 로 `.project_manager/local.conf` 있는 최근접 조상을 root 로 본다. 그 앵커를
    tmp root 에 둬 marker(`.project_manager/.local/ctx-stop/*.done`)가 실 트리로 새지 않게 격리한다.
    """
    root = tmp_path / "proj"
    claude = root / ".claude"
    claude.mkdir(parents=True)
    for name in _CLAUDE_FILES:
        dst = claude / name
        dst.write_text((CLAUDE / name).read_text(encoding="utf-8"), encoding="utf-8")
        if name.endswith(".sh"):
            dst.chmod(0o755)
    pm = root / ".project_manager"
    pm.mkdir()
    (pm / "local.conf").write_text("", encoding="utf-8")  # repo_root 앵커 + 기본 임계(빈 conf).
    return root, claude


def _hook_env(shim_parent: Path) -> dict:
    """래퍼의 python3/python 후보가 이 테스트 러너 인터프리터로 해소되게 PATH 구성.

    없으면 래퍼가 인터프리터 부재로 rc0 조용 통과해 발화 단언이 무의미해진다(그게 T-0209 결함의
    한 형태다). Windows 는 실 python.exe 디렉토리를 PATH 최상단에 둬 WindowsApps 가짜 shim 을
    앞지른다(python3.exe 통상 부재→python 후보 폴백·가짜 shim 은 래퍼 --version 실행검증서 걸러짐).
    POSIX 는 러너 인터프리터를 python3/python 으로 symlink 해 후보 해소를 보장(hermetic).
    """
    env = dict(os.environ)
    if IS_WINDOWS:
        prepend = os.path.dirname(sys.executable)
    else:
        shim = shim_parent / "_interp_shim"
        shim.mkdir(parents=True, exist_ok=True)
        for name in ("python3", "python"):
            link = shim / name
            if not link.exists():
                os.symlink(sys.executable, link)
        prepend = str(shim)
    env["PATH"] = prepend + os.pathsep + env.get("PATH", "")
    return env


def _run(wrapper: Path, payload, env: dict) -> subprocess.CompletedProcess:
    """래퍼를 Git Bash 로 실행하고 payload 를 stdin JSON 으로 준다.

    payload 가 dict 면 json.dumps(하네스와 동일 escape), str 이면 그대로 준다(malformed 케이스).
    """
    stdin = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run(
        # Git Bash 는 argv 의 `\\` 를 escape 로 소실한다 → as_posix(forward-slash)로 넘긴다(POSIX 무변경).
        [BASH, Path(wrapper).as_posix()],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=120,
    )


def _write_transcript(root: Path, input_tokens: int) -> Path:
    """backbone `context_tokens_from_transcript` 계약의 JSONL — message.usage.input_tokens.

    가장 최근 usable usage 의 입력 토큰합 = 현재 컨텍스트 점유(ctx_guard.py 참조).
    """
    tx = root / "transcript.jsonl"
    tx.write_text(
        json.dumps({"message": {"usage": {"input_tokens": input_tokens}}}) + "\n",
        encoding="utf-8",
    )
    return tx


def _transcript_arg(tx: Path, form: str) -> str:
    """transcript_path 문자열을 form 에 맞게 — native(플랫폼 native) / mount(Git-Bash `/c/..`).

    POSIX 엔 마운트 개념이 없어 mount 도 native 문자열과 동일(str(tx)).
    """
    native = str(tx)
    if form == "native" or not IS_WINDOWS:
        return native
    return "/" + native[0].lower() + native[2:].replace("\\", "/")


def _fires(form: str) -> bool:
    """이 form 의 transcript_path 를 backbone `Path()` 가 읽어 stop 이 발화하는가.

    backbone 은 native 만 해소(`Path()`) — Windows mount(`/c/..`)형은 미해소돼 used 0(ok·무발화).
    POSIX 엔 마운트 형이 native 와 같은 문자열이라 항상 발화(형식 커버 의미·ticket §인터페이스).
    """
    return not (IS_WINDOWS and form == "mount")


# ── (전제) 실 래퍼 파일 존재 ──────────────────────────────────────────────────

def test_wrappers_present():
    """실 래퍼 파일 존재 — 배선 대상(settings.json 이 이 .sh 를 가리킴)."""
    assert (CLAUDE / _STATUSLINE_WRAPPER).is_file(), f"{_STATUSLINE_WRAPPER} 부재"
    assert (CLAUDE / _STOP_WRAPPER).is_file(), f"{_STOP_WRAPPER} 부재"


# ── statusline 래퍼: used% → `ctx N%` 발화 ────────────────────────────────────

@requires_bash
def test_statusline_wrapper_emits_ctx_pct(tmp_path):
    """stdin current_usage 100K(=50% of 200K 예산) → stdout `ctx 50%`(회색·ok 밴드)·rc0 — 래퍼 경유 발화."""
    root, claude = _make_ctx_repo(tmp_path)
    env = _hook_env(tmp_path)

    proc = _run(claude / _STATUSLINE_WRAPPER,
                {"context_window": {"current_usage": {"input_tokens": _OK_USED_TOKENS}}}, env)
    assert proc.returncode == 0, f"rc={proc.returncode}\n{proc.stderr}"
    assert "ctx 50%" in proc.stdout, (
        f"statusline 미발화(`ctx 50%` 없음): {proc.stdout!r}\nstderr={proc.stderr!r}"
    )
    assert "정지" not in proc.stdout, f"ok 밴드인데 정지 문구 출현: {proc.stdout!r}"


@requires_bash
def test_statusline_wrapper_malformed_graceful(tmp_path):
    """malformed stdin → rc0·`ctx 0%` 폴백(항상 한 줄 방출)·traceback 0."""
    root, claude = _make_ctx_repo(tmp_path)
    env = _hook_env(tmp_path)

    proc = _run(claude / _STATUSLINE_WRAPPER, "{not valid json", env)
    assert proc.returncode == 0, f"rc={proc.returncode}\n{proc.stderr}"
    assert "ctx 0%" in proc.stdout, f"malformed 폴백(`ctx 0%`) 실패: {proc.stdout!r}"
    assert "Traceback" not in proc.stderr, f"비정상 종료(traceback): {proc.stderr!r}"


# ── ctx_stop 래퍼: stop 밴드 3분기 (block / deny / 핸드오프 통과) ───────────────
# backbone 계약([[ADR-0038]] hard-stop·T-0205 핸드오프 예외)을 *래퍼 경유* 재확인.

@requires_bash
def test_stop_wrapper_userpromptsubmit_blocks(tmp_path):
    """transcript 95% + UserPromptSubmit(비-핸드오프) → `{"decision":"block"}`·rc0·STOP marker."""
    root, claude = _make_ctx_repo(tmp_path)
    env = _hook_env(tmp_path)
    tx = _write_transcript(root, _STOP_INPUT_TOKENS)
    stdin = {
        "transcript_path": str(tx),
        "session_id": "sess-ups",
        "hook_event_name": "UserPromptSubmit",
    }

    proc = _run(claude / _STOP_WRAPPER, stdin, env)
    assert proc.returncode == 0, f"rc={proc.returncode}\n{proc.stderr}"
    data = json.loads(proc.stdout)  # UserPromptSubmit 은 top-level block 스키마.
    assert data["decision"] == "block", f"block 스키마 아님: {proc.stdout!r}"
    assert "/pm-handoff" in data["reason"], data["reason"]
    marker = root / ".project_manager" / ".local" / "ctx-stop" / "sess-ups.done"
    assert marker.exists(), "STOP marker 미박제(relay 회전 신호)"


@requires_bash
def test_stop_wrapper_pretooluse_denies(tmp_path):
    """transcript 95% + PreToolUse(새 작업 도구) → deny JSON·rc0."""
    root, claude = _make_ctx_repo(tmp_path)
    env = _hook_env(tmp_path)
    tx = _write_transcript(root, _STOP_INPUT_TOKENS)
    stdin = {
        "transcript_path": str(tx),
        "session_id": "sess-ptu",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "ls -la"},  # 핸드오프 allow-list 밖 = 새 작업.
    }

    proc = _run(claude / _STOP_WRAPPER, stdin, env)
    assert proc.returncode == 0, f"rc={proc.returncode}\n{proc.stderr}"
    data = json.loads(proc.stdout)
    hso = data["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] == "deny", f"deny 아님: {proc.stdout!r}"
    assert "ctx-stop" in hso["permissionDecisionReason"], hso["permissionDecisionReason"]


@requires_bash
def test_stop_wrapper_handoff_prompt_passes(tmp_path):
    """transcript 95% + UserPromptSubmit prompt `/pm-handoff` → rc0·무출력(T-0205 예외)·marker.

    stop 밴드에서도 핸드오프 트리거 prompt 는 통과(락아웃 해소) — 통과해도 STOP marker 는 박힌다.
    """
    root, claude = _make_ctx_repo(tmp_path)
    env = _hook_env(tmp_path)
    tx = _write_transcript(root, _STOP_INPUT_TOKENS)
    stdin = {
        "transcript_path": str(tx),
        "session_id": "sess-hp",
        "hook_event_name": "UserPromptSubmit",
        "prompt": "/pm-handoff",
    }

    proc = _run(claude / _STOP_WRAPPER, stdin, env)
    assert proc.returncode == 0, f"rc={proc.returncode}\n{proc.stderr}"
    assert proc.stdout.strip() == "", f"핸드오프 통과인데 출력 발생: {proc.stdout!r}"
    marker = root / ".project_manager" / ".local" / "ctx-stop" / "sess-hp.done"
    assert marker.exists(), "통과 변형에서도 STOP marker 는 박혀야 함(회전 신호)"


@requires_bash
def test_stop_wrapper_malformed_graceful(tmp_path):
    """malformed stdin → rc0·무출력(무경로→used 0→ok)·traceback 0."""
    root, claude = _make_ctx_repo(tmp_path)
    env = _hook_env(tmp_path)

    proc = _run(claude / _STOP_WRAPPER, "{not valid json", env)
    assert proc.returncode == 0, f"rc={proc.returncode}\n{proc.stderr}"
    assert proc.stdout.strip() == "", f"정지 신호 없어야 함: {proc.stdout!r}"
    assert "Traceback" not in proc.stderr, f"비정상 종료(traceback): {proc.stderr!r}"


# ── transcript_path 형식 커버: native / mount ─────────────────────────────────

@requires_bash
@pytest.mark.parametrize("form", ["native", "mount"])
def test_stop_wrapper_transcript_form(tmp_path, form):
    r"""transcript_path 를 native(`C:\..`)·mount(`/c/..`) 두 형식으로 — backbone Path() 는 native 처리.

    native → 해소·발화(block). Windows mount 형은 현행 backbone 이 미해소(used 0→ok)라 발화하지
    않지만, 그 *무발화 자체는 계약이 아니라 incidental* — 실하네스는 native 만 보내고, backbone 이
    후일 mount 정규화를 얻으면 발화하는 게 옳다. 그래서 Windows mount 분기는 graceful(rc0·
    traceback 0)만 단언한다 (T-0215 reviewer should-fix — "옳은 동작의 부재" 박제 회피).
    POSIX 엔 마운트 형이 native 와 동일 문자열이라 발화. 어느 형식이든 래퍼는 rc0 로 graceful.
    """
    root, claude = _make_ctx_repo(tmp_path)
    env = _hook_env(tmp_path)
    tx = _write_transcript(root, _STOP_INPUT_TOKENS)
    stdin = {
        "transcript_path": _transcript_arg(tx, form),
        "session_id": f"sess-form-{form}",
        "hook_event_name": "UserPromptSubmit",
    }

    proc = _run(claude / _STOP_WRAPPER, stdin, env)
    assert proc.returncode == 0, f"rc={proc.returncode}\n{proc.stderr}"
    marker = root / ".project_manager" / ".local" / "ctx-stop" / f"sess-form-{form}.done"
    if _fires(form):
        data = json.loads(proc.stdout)
        assert data["decision"] == "block", f"{form} 형식 미발화: {proc.stdout!r}"
        assert marker.exists()
    else:
        # Windows mount 형: graceful-only — 무발화를 계약으로 박제하지 않는다(backbone 이 mount
        # 정규화를 얻어 발화하게 되면 그건 개선이지 회귀가 아님). crash/traceback 없음만 단언.
        assert "Traceback" not in proc.stderr, f"mount 형식 크래시: {proc.stderr!r}"
        if proc.stdout.strip():
            # 발화했다면(미래 backbone 개선) 유효한 block JSON 이어야 한다 — 깨진 출력만 금지.
            assert json.loads(proc.stdout)["decision"] == "block"
