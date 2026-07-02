r"""run_tests_hook.sh PostToolUse 훅 런타임 smoke — 경로 판정(형식정규화 + containment + .py 게이트).

이 훅(claude 어댑터)은 Write/Edit 후 프로젝트 안 *.py 가 편집되면 회귀(pytest)를 자동 실행한다.
편집 파일 경로는 hook stdin JSON 의 `.tool_input.file_path`(폴백 `.tool_response.filePath`)에서 오고,
"프로젝트 안 .py 인가" 판정을 python 이 한다(T-0210). bash case 의 리터럴 접두 매칭은 경로 *형식* 에
민감해, 실제 Windows 하네스가 native `C:\...` 를 보내면 Git Bash pwd 형(`/c/...`)인 repo_root 와
불일치→rc0 silent skip 했다(false-green). 이 훅은 그간 런타임 커버리지 0 이었다.

계약:
  · 프로젝트 안 .py file_path        → pytest 실행 + {"systemMessage": "tests: ..."} 출력 (rc0)
  · 프로젝트 밖 .py / 비-.py          → rc0 무출력 (skip)
  · malformed / empty / 비-dict / 무경로키 / 비-string 경로 → rc0 무출력 (graceful)
  · 세 경로 형식(native C:\ · 드라이브 C:/ · mount /c/)이 동일 canonical 로 수렴 (Windows)

전부 hermetic — tmp 에 최소 repo(.claude/ 훅 사본 + tests/ 더미)를 만들고 그 안에서만 실행한다(실 repo
무오염). stdin JSON 은 json.dumps 로 만들어 실제 하네스가 보내는 것과 동일한 유효 이스케이프를 준다
(native 역슬래시는 JSON 에서 `\\` 로 escape 된다 — raw 삽입은 invalid JSON 이 되어버린다).
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
HOOK = REPO / "templates" / "claude_code" / ".claude" / "run_tests_hook.sh"

# subprocess 는 CreateProcess 검색순상 System32\bash.exe(WSL 런처)를 PATH 의 Git Bash 보다 먼저 집는데
# WSL bash 는 `/mnt/c/…` 마운트라 Windows-form 경로를 못 연다 — shutil.which("bash")(=PATH 순=Git Bash)
# 절대경로로 실행해 일관된 POSIX 셸을 쓴다(test_pm_import_facade 와 동일 소스·Linux 는 /usr/bin/bash).
BASH = shutil.which("bash")
requires_bash = pytest.mark.skipif(BASH is None, reason="bash 부재 — POSIX/Git Bash 훅 e2e 불가")

IS_WINDOWS = os.name == "nt"


def _make_repo(root: Path) -> Path:
    """훅이 기대하는 최소 repo — `.claude/` 훅 사본 + pytest 발화 대상 tests/ 더미.

    훅은 자기 위치(.claude/)에서 repo_root 를 self-resolve 하므로 이 root 가 곧 repo_root(실 repo 무오염).
    """
    claude_dir = root / ".claude"
    claude_dir.mkdir(parents=True)
    hook_copy = claude_dir / "run_tests_hook.sh"
    hook_copy.write_text(HOOK.read_text(encoding="utf-8"), encoding="utf-8")
    hook_copy.chmod(0o755)
    tests_dir = root / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_dummy.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    return hook_copy


def _hook_env(shim_parent: Path) -> dict:
    """훅의 python3/python 후보가 이 테스트를 돌리는 인터프리터(=pytest 보유)로 해소되게 PATH 구성.

    없으면 훅이 인터프리터 부재로 silent skip 해 pytest 발화 단언이 무의미해진다. 중첩 pytest 가
    바깥 실행 옵션에 오염되지 않게 PYTEST_* 환경변수도 제거한다.
    """
    env = dict(os.environ)
    for key in ("PYTEST_ADDOPTS", "PYTEST_CURRENT_TEST", "PYTEST_PLUGINS",
                "PYTEST_XDIST_WORKER", "PYTEST_XDIST_WORKER_COUNT"):
        env.pop(key, None)
    if IS_WINDOWS:
        # 실 python.exe 디렉토리를 PATH 최상단에 둔다(WindowsApps 가짜 shim 을 앞질러) — python3.exe 는
        # 통상 부재라 python 후보로 폴백된다(가짜 python3 shim 은 훅의 --version 실행검증에서 걸러진다).
        prepend = os.path.dirname(sys.executable)
    else:
        # POSIX: 러너 인터프리터를 python3/python 으로 symlink 해 후보 해소를 보장(hermetic).
        shim = shim_parent / "_interp_shim"
        shim.mkdir(parents=True, exist_ok=True)
        for name in ("python3", "python"):
            link = shim / name
            if not link.exists():
                os.symlink(sys.executable, link)
        prepend = str(shim)
    env["PATH"] = prepend + os.pathsep + env.get("PATH", "")
    return env


def _run(hook: Path, payload, env: dict) -> subprocess.CompletedProcess:
    """훅을 Git Bash 로 실행하고 payload 를 stdin JSON 으로 준다.

    payload 가 dict/list 면 json.dumps(하네스와 동일 escape), str 이면 그대로 준다(malformed/empty 케이스).
    """
    stdin = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run(
        # Git Bash 는 argv 의 `\\` 를 escape 로 소실한다 → as_posix(forward-slash)로 넘긴다(POSIX 무변경).
        [BASH, Path(hook).as_posix()],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=120,
    )


# ── (전제) 실 훅 파일 존재 + jq 의존 0 ──────────────────────────────────────────

def test_hook_present_and_no_jq():
    """실 훅 파일 존재 + jq 참조 0(Windows 기본 부재→silent no-op 회귀 방지·T-0210 DoD)."""
    assert HOOK.is_file(), f"run_tests_hook.sh 부재: {HOOK}"
    assert "jq" not in HOOK.read_text(encoding="utf-8"), "jq 의존 잔존"


# ── 프로젝트 안 .py → pytest 발화 (native 형·양 플랫폼서 의미 있는 단언) ────────

@requires_bash
def test_in_project_py_fires_pytest(tmp_path):
    r"""프로젝트 안 .py file_path(플랫폼 native 형) → pytest 실행 + systemMessage 출력.

    Windows 에선 str(path)=native `C:\...` 이고 훅 repo_root 는 Git Bash pwd 형(`/c/...`) — 이 형식
    불일치가 곧 T-0210 must-fix 회귀(native 경로 silent skip)의 정확한 조건이다. POSIX 에선 native=
    forward-slash 라 동일 케이스가 자연히 성립(양 플랫폼서 의미 있음).
    """
    repo = tmp_path / "proj"
    hook = _make_repo(repo)
    env = _hook_env(tmp_path)
    src = str(repo / "somemodule.py")  # native on Windows, posix on POSIX

    proc = _run(hook, {"tool_input": {"file_path": src}}, env)
    assert proc.returncode == 0, f"rc={proc.returncode}\n{proc.stderr}"
    assert "systemMessage" in proc.stdout, (
        f"pytest 미발화(systemMessage 없음): {proc.stdout!r}\nstderr={proc.stderr!r}"
    )
    assert "tests:" in proc.stdout, proc.stdout


@requires_bash
def test_fallback_tool_response_filepath_fires(tmp_path):
    """file_path 부재 시 tool_response.filePath 폴백 경로로도 프로젝트 안 .py 면 발화(필터 시맨틱 보존)."""
    repo = tmp_path / "proj"
    hook = _make_repo(repo)
    env = _hook_env(tmp_path)
    src = str(repo / "viaresp.py")

    proc = _run(hook, {"tool_response": {"filePath": src}}, env)
    assert proc.returncode == 0, proc.stderr
    assert "systemMessage" in proc.stdout, proc.stdout


# ── skip 계약: 프로젝트 밖·비-py·malformed·무경로·비-string → rc0 무출력 ────────

@requires_bash
@pytest.mark.parametrize("payload", [
    pytest.param("__OUTSIDE__", id="out-of-project-py"),
    pytest.param("__PREFIX_SIBLING__", id="prefix-sibling-py"),
    pytest.param("__NONPY__", id="in-project-non-py"),
    pytest.param("{not valid json", id="malformed-json"),
    pytest.param("", id="empty-stdin"),
    pytest.param([1, 2, 3], id="non-dict-array"),
    pytest.param({"foo": "bar"}, id="dict-no-path-keys"),
    pytest.param({"tool_input": {"file_path": None}}, id="file_path-non-string"),
])
def test_skip_contract_no_output(tmp_path, payload):
    """편집 대상이 프로젝트 안 .py 가 아니면(또는 파싱 불가면) rc0·무출력(회귀 미실행)."""
    repo = tmp_path / "proj"
    hook = _make_repo(repo)
    env = _hook_env(tmp_path)

    if payload == "__OUTSIDE__":
        # 프로젝트 밖 형제 디렉토리의 .py (접두 유사 오탐 방지: proj vs outside_proj).
        payload = {"tool_input": {"file_path": str(tmp_path / "outside_proj" / "mod.py")}}
    elif payload == "__PREFIX_SIBLING__":
        # 고전적 prefix 경계 함정: `proj` 의 문자열-접두 형제 `proj2` — containment 의
        # `+ os.sep` 경계가 없으면 startswith 오탐으로 발화한다 (T-0210 r2 reviewer suggestion).
        payload = {"tool_input": {"file_path": str(tmp_path / "proj2" / "mod.py")}}
    elif payload == "__NONPY__":
        payload = {"tool_input": {"file_path": str(repo / "notes.txt")}}

    proc = _run(hook, payload, env)
    assert proc.returncode == 0, f"rc={proc.returncode}\n{proc.stderr}"
    assert proc.stdout.strip() == "", f"skip 이어야 하는데 출력 발생: {proc.stdout!r}"


# ── Windows 3-형식 수렴: native C:\ · 드라이브 C:/ · mount /c/ 전부 발화 ────────

@requires_bash
@pytest.mark.skipif(not IS_WINDOWS, reason="Windows 경로 형식(C:\\·C:/·/c/) 매트릭스 — POSIX 무의미")
@pytest.mark.parametrize("form", ["native", "drive", "mount"])
def test_windows_path_forms_all_fire(tmp_path, form):
    r"""하네스가 어떤 형식으로 보내도(native `C:\` 가 실제 형) 동일 canonical 로 수렴해 발화.

    repo_root 는 훅이 Git Bash pwd 로 mount 형(`/c/...`)으로 self-resolve 한다 — file_path 세 형식이
    모두 그와 매칭돼야 한다. round-1 결함은 native/drive 가 mount repo_root 와 불일치해 skip 하던 것.
    """
    repo = tmp_path / "proj"
    hook = _make_repo(repo)
    env = _hook_env(tmp_path)

    native = str(repo / "mod.py")                       # C:\...\proj\mod.py
    if form == "native":
        fp = native
    elif form == "drive":
        fp = native.replace("\\", "/")                  # C:/.../proj/mod.py
    else:  # mount — Git Bash pwd 형 (/c/.../proj/mod.py)
        fp = "/" + native[0].lower() + native[2:].replace("\\", "/")

    proc = _run(hook, {"tool_input": {"file_path": fp}}, env)
    assert proc.returncode == 0, f"rc={proc.returncode}\n{proc.stderr}"
    assert "systemMessage" in proc.stdout, (
        f"{form} 형식 미발화(canonical 수렴 실패): {proc.stdout!r}\nstderr={proc.stderr!r}"
    )
