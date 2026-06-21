"""subprocess 텍스트 캡처 인코딩 sweep (T-0019 · C3+C5) 단위 테스트.

엔진 도구들이 `text=True` 로 git/pytest/하니스 stdout 을 캡처할 때 cp949(Windows
기본 콘솔 코덱)이 아니라 명시적 UTF-8 로 디코딩하는지를 검증한다. 인코딩 미지정이면
한글 커밋 메시지·diff·로그를 캡처하다 UnicodeDecodeError 로 크래시한다.

검증 축:
  - external_review: git diff 캡처 + 리뷰어 호출이 encoding="utf-8", errors="replace".
  - pm_import: 하니스 runner + board init 캡처가 encoding 명시.
  - ticket_finish / pm_handoff: _default_run_pytest/board/git 가 encoding 명시.
  - bench_weight: _run_subprocess 캡처가 encoding 명시.
  - C5: pm_import {{PY}} 치환·local.conf py= 가 board._detect_py() 탐지값(플랫폼별
        python/python3)을 쓰고 bare "python3" 를 하드코딩하지 않는다.

이 테스트들은 *수정 전* 코드(encoding 미지정 / DEFAULT_PY="python3" 하드코딩)에서
반드시 FAIL 한다 — 호출 인자에 encoding="utf-8" 를 단언하고, 탐지 라우팅을
shutil.which 패치로 강제한다(ambient PYTHONUTF8 가 버그를 가리지 못하게).
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
SCRIPTS = REPO / "scripts"


def _load(name: str, base: Path):
    spec = importlib.util.spec_from_file_location(name, base / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def external_review():
    return _load("external_review", TOOLS)


@pytest.fixture(scope="module")
def pm_import():
    return _load("pm_import", TOOLS)


@pytest.fixture(scope="module")
def ticket_finish():
    return _load("ticket_finish", TOOLS)


@pytest.fixture(scope="module")
def pm_handoff():
    return _load("pm_handoff", TOOLS)


@pytest.fixture(scope="module")
def bench_weight():
    return _load("bench_weight", SCRIPTS)


@pytest.fixture(scope="module")
def board():
    return _load("board", TOOLS)


@pytest.fixture(scope="module")
def pm_config():
    return _load("pm_config", TOOLS)


class _Recorder:
    """subprocess.run 대역 — 호출 kwargs 를 기록하고 한글 출력 CompletedProcess 를 돌려준다.

    한글이 포함된 stdout 을 반환해 캡처 경로가 깨지지 않는지도 간접 확인한다.
    """

    def __init__(self, stdout: str = "변경 요약: 한글 출력 — U+2014 포함\n"):
        self.calls: list[dict] = []
        self._stdout = stdout

    def __call__(self, argv, *args, **kwargs):
        self.calls.append(kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout=self._stdout, stderr="")


def _assert_utf8(kwargs: dict) -> None:
    assert kwargs.get("encoding") == "utf-8", (
        f"subprocess 캡처에 encoding='utf-8' 누락: {kwargs!r}"
    )
    assert kwargs.get("errors") == "replace", (
        f"subprocess 캡처에 errors='replace' 누락: {kwargs!r}"
    )


# ── external_review (run_fn DI 로 직접 주입) ────────────────────────────────


def test_extract_diff_passes_utf8_encoding(external_review):
    rec = _Recorder()
    external_review.extract_diff("main", ["foo.py"], run_fn=rec)
    assert rec.calls, "git diff 캡처 호출이 일어나지 않음"
    for kwargs in rec.calls:
        _assert_utf8(kwargs)


def test_extract_diff_head_path_passes_utf8(external_review):
    rec = _Recorder(stdout="")  # 빈 staged/unstaged → HEAD~1 폴백까지 모두 거침
    external_review.extract_diff("HEAD", ["foo.py"], run_fn=rec)
    assert len(rec.calls) >= 2
    for kwargs in rec.calls:
        _assert_utf8(kwargs)


def test_run_reviewer_passes_utf8_encoding(external_review):
    rec = _Recorder()
    ok, _ = external_review.run_reviewer("echo hi", reviewer_cmd="echo hi", run_fn=rec)
    assert rec.calls
    _assert_utf8(rec.calls[0])


# ── pm_import (모듈 subprocess.run monkeypatch) ─────────────────────────────


def test_pm_import_harness_runner_passes_utf8(pm_import, monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(pm_import.subprocess, "run", rec)
    pm_import._real_harness_runner(["claude", "-p", "분석 프롬프트"], "프롬프트")
    assert rec.calls
    _assert_utf8(rec.calls[0])


def test_pm_import_board_init_passes_utf8(pm_import, monkeypatch, tmp_path):
    board = tmp_path / ".project_manager" / "tools"
    board.mkdir(parents=True)
    (board / "board.py").write_text("# stub", encoding="utf-8")
    rec = _Recorder()
    monkeypatch.setattr(pm_import.subprocess, "run", rec)
    pm_import.run_board_init(tmp_path)
    assert rec.calls
    _assert_utf8(rec.calls[0])


# ── ticket_finish (_default_run_* 직접 호출) ────────────────────────────────


def test_ticket_finish_default_runs_pass_utf8(ticket_finish, monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(ticket_finish.subprocess, "run", rec)
    finisher = ticket_finish.TicketFinisher()
    finisher._default_run_pytest()
    finisher._default_run_board(["list"])
    finisher._default_run_git(["status"])
    assert len(rec.calls) == 3, "pytest/board/git 세 캡처가 모두 일어나야 함"
    for kwargs in rec.calls:
        _assert_utf8(kwargs)


# ── pm_handoff (_default_run_* 직접 호출) ──────────────────────────────────


def test_pm_handoff_default_runs_pass_utf8(pm_handoff, monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(pm_handoff.subprocess, "run", rec)
    handoff = pm_handoff.PmHandoff()
    handoff._default_run_pytest()
    handoff._default_run_git(["status"])
    assert len(rec.calls) == 2
    for kwargs in rec.calls:
        _assert_utf8(kwargs)


# ── bench_weight (_run_subprocess) ─────────────────────────────────────────


def test_bench_weight_subprocess_passes_utf8(bench_weight, monkeypatch):
    rec = _Recorder(stdout="usage 요약 — 한글\n")
    monkeypatch.setattr(bench_weight.subprocess, "run", rec)
    out = bench_weight._run_subprocess(["claude", "-p", "x"], env={})
    assert "한글" in out
    assert rec.calls
    _assert_utf8(rec.calls[0])


# ── C5: {{PY}} 치환이 board._detect_py() 탐지값을 쓴다 (bare python3 미하드코딩) ─


def test_pm_substitution_py_matches_detected(pm_import):
    """치환맵의 {{PY}} 가 _detected_py() 결과와 일치(하드코딩 상수 아님)."""
    sub = pm_import._substitution_map("Proj", REPO, "2026-06-14")
    assert sub["{{PY}}"] == pm_import._detected_py()


def _stub_interp_runs(monkeypatch):
    """후보 실행검증(_interp_runs → subprocess.run)을 결정화한다.

    T-0022 후 _detect_py 는 후보를 `subprocess.run([cmd,"--version"])` 으로 실행검증한다.
    pm_import._detected_py 는 board 를 fresh-load 하므로 전역 subprocess.run 을 patch 해
    그 fresh board 까지 흔든다 — 실 인터프리터 비의존. (os.name 은 건드리지 않는다: posix
    강제는 Windows 에서 pathlib 을 깨뜨림. 대신 fake_which 가 `py`·`python3` 를 부재로
    돌려 OS 무관하게 'python' 만 후보로 통과시킨다.)
    """
    import subprocess
    import types

    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(returncode=0))


def test_detected_py_routes_through_detection_not_hardcoded(pm_import, monkeypatch):
    """py·python3 부재·python 존재 환경을 강제하면 _detected_py 가 'python' 을 반환.

    수정 전(DEFAULT_PY='python3' 하드코딩)에서는 이 단언이 깨진다 — 탐지 경로(board.
    _detect_py)를 실제로 경유함을 증명. ambient PYTHONUTF8/PATH/OS 와 무관하게 결정적.
    """
    import shutil

    def fake_which(cmd):
        return f"/usr/bin/{cmd}" if cmd == "python" else None

    _stub_interp_runs(monkeypatch)
    monkeypatch.setattr(shutil, "which", fake_which)
    assert pm_import._detected_py() == "python"


def test_substitution_py_uses_python_when_python3_absent(pm_import, monkeypatch):
    """치환맵까지 탐지값이 전파되는지 — py·python3 부재 시 {{PY}} 가 'python'."""
    import shutil

    def fake_which(cmd):
        return f"/usr/bin/{cmd}" if cmd == "python" else None

    _stub_interp_runs(monkeypatch)
    monkeypatch.setattr(shutil, "which", fake_which)
    sub = pm_import._substitution_map("Proj", REPO, "2026-06-14")
    assert sub["{{PY}}"] == "python"
    assert sub["{{PY}}"] != "python3"


# ── board.cmd_regression: pytest 자식에 UTF-8 env 강제 (T-0024) ──────────────


class _RcRecorder:
    """subprocess.run 대역 — env kwargs 를 기록하고 returncode 0 을 돌려준다.

    pytest 자식을 실제 기동하지 않고 env 전달만 검증한다.
    """

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, *args, **kwargs):
        self.calls.append(kwargs)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")


def test_regression_run_child_forces_utf8_env(board, monkeypatch):
    """cmd_regression(run, scoped) 이 pytest 자식 env 에 UTF-8 강제·os.environ 보존.

    scoped 경로(touches 지정)는 subprocess.run 직후 반환 → 플래그 파일/_git_head 미경유.
    수정 전(env 미전달)에서는 이 단언이 깨진다.
    """
    rec = _RcRecorder()
    monkeypatch.setattr(board.subprocess, "run", rec)
    # os.environ 보존 검증용 마커 키.
    monkeypatch.setenv("T0024_SENTINEL", "preserved")
    args = argparse.Namespace(action="run", cmd=None, ticket=None,
                              touches="tests/test_subprocess_encoding.py")
    rc = board.cmd_regression(args)
    assert rc == 0
    assert rec.calls, "pytest 자식 subprocess.run 호출이 일어나지 않음"
    env = rec.calls[0].get("env")
    assert env is not None, f"자식에 env 미전달: {rec.calls[0]!r}"
    assert env.get("PYTHONUTF8") == "1", f"PYTHONUTF8=1 누락: {env!r}"
    assert env.get("PYTHONIOENCODING") == "utf-8", f"PYTHONIOENCODING=utf-8 누락: {env!r}"
    # 기존 os.environ 키 보존(병합이지 치환 아님).
    assert env.get("T0024_SENTINEL") == "preserved"
    assert "PATH" in env or "PATH" not in os.environ


# ── T-0068: Windows 콘솔 codepage UTF-8 셋업 (SetConsoleOutputCP/CP 65001) ────


class _FakeKernel32:
    """ctypes.windll.kernel32 대역 — SetConsole*CP 호출 인자를 기록한다."""

    def __init__(self):
        self.output_cp_calls: list[int] = []
        self.input_cp_calls: list[int] = []

    def SetConsoleOutputCP(self, cp):  # noqa: N802 (WinAPI 이름 보존)
        self.output_cp_calls.append(cp)
        return 1

    def SetConsoleCP(self, cp):  # noqa: N802 (WinAPI 이름 보존)
        self.input_cp_calls.append(cp)
        return 1


def _install_fake_ctypes(monkeypatch):
    """함수 내부의 `import ctypes` 가 받을 fake ctypes 모듈을 sys.modules 에 주입.

    POSIX 에는 ctypes.windll 이 없으므로(실 API 부재), windll.kernel32 만 흉내내는
    가짜 모듈로 갈아끼워 OS 무관하게 SetConsole*CP 호출을 관측한다.
    """
    import sys
    import types

    kernel32 = _FakeKernel32()
    fake = types.ModuleType("ctypes")
    fake.windll = types.SimpleNamespace(kernel32=kernel32)
    monkeypatch.setitem(sys.modules, "ctypes", fake)
    return kernel32


def test_board_codepage_set_on_windows(board, monkeypatch):
    """os.name=='nt' 에서 _set_console_codepage_utf8 가 65001 두 codepage 를 설정."""
    monkeypatch.setattr(board.os, "name", "nt")
    kernel32 = _install_fake_ctypes(monkeypatch)
    board._set_console_codepage_utf8()
    assert kernel32.output_cp_calls == [65001], "SetConsoleOutputCP(65001) 누락/오인자"
    assert kernel32.input_cp_calls == [65001], "SetConsoleCP(65001) 누락/오인자"


def test_board_codepage_noop_on_posix(board, monkeypatch):
    """os.name!='nt'(POSIX) 에서는 분기에 진입하지 않아 SetConsole*CP 미호출."""
    monkeypatch.setattr(board.os, "name", "posix")
    kernel32 = _install_fake_ctypes(monkeypatch)
    board._set_console_codepage_utf8()
    assert kernel32.output_cp_calls == [], "POSIX 에서 SetConsoleOutputCP 가 호출됨"
    assert kernel32.input_cp_calls == [], "POSIX 에서 SetConsoleCP 가 호출됨"


def test_pm_config_codepage_set_on_windows(pm_config, monkeypatch):
    """pm_config 도 동일 인라인 정의 — nt 에서 65001 설정(도구 간 정합)."""
    monkeypatch.setattr(pm_config.os, "name", "nt")
    kernel32 = _install_fake_ctypes(monkeypatch)
    pm_config._set_console_codepage_utf8()
    assert kernel32.output_cp_calls == [65001]
    assert kernel32.input_cp_calls == [65001]


def test_pm_config_codepage_noop_on_posix(pm_config, monkeypatch):
    monkeypatch.setattr(pm_config.os, "name", "posix")
    kernel32 = _install_fake_ctypes(monkeypatch)
    pm_config._set_console_codepage_utf8()
    assert kernel32.output_cp_calls == []
    assert kernel32.input_cp_calls == []


def test_codepage_best_effort_swallows_exception(board, monkeypatch):
    """ctypes 호출이 예외(콘솔 핸들 없음 등)를 던져도 조용히 통과(best-effort)."""
    import sys
    import types

    monkeypatch.setattr(board.os, "name", "nt")

    class _Boom:
        def SetConsoleOutputCP(self, cp):  # noqa: N802
            raise OSError("no console handle")

        def SetConsoleCP(self, cp):  # noqa: N802
            raise OSError("no console handle")

    fake = types.ModuleType("ctypes")
    fake.windll = types.SimpleNamespace(kernel32=_Boom())
    monkeypatch.setitem(sys.modules, "ctypes", fake)
    # 예외가 새어나오면 이 호출이 raise — pytest 가 실패로 잡는다.
    board._set_console_codepage_utf8()


# 9개 엔진 도구 모두 동일 인라인 정의를 보유(ADR isolation — 공유 모듈 없음).
_CODEPAGE_TOOLS = [
    "board", "pm_bootstrap", "pm_handoff", "pm_log", "ticket_finish",
    "pm_update", "pm_import", "pm_config", "external_review",
]


@pytest.mark.parametrize("tool_name", _CODEPAGE_TOOLS)
def test_every_tool_defines_codepage_helper(tool_name):
    """9개 도구 각자 _set_console_codepage_utf8 를 인라인 정의(전파 누락 가드)."""
    mod = _load(tool_name, TOOLS)
    assert hasattr(mod, "_set_console_codepage_utf8"), (
        f"{tool_name} 에 _set_console_codepage_utf8 정의 누락"
    )


# ── T-0068: .cmd forwarder 회귀 가드 (비-ASCII 0 + CRLF) ─────────────────────


_CMD_FORWARDERS = [
    REPO / "pm-config.cmd",
    REPO / "pm-import.cmd",
    REPO / "templates" / "claude_code" / "pm-config.cmd",
    REPO / "templates" / "claude_code" / "pm-update.cmd",
    REPO / "templates" / "opencode" / "pm-config.cmd",
    REPO / "templates" / "opencode" / "pm-update.cmd",
]


@pytest.mark.parametrize("cmd_path", _CMD_FORWARDERS, ids=lambda p: str(p.relative_to(REPO)))
def test_cmd_forwarder_is_ascii_only(cmd_path):
    """.cmd forwarder 가 비-ASCII 0 — cp949 cmd.exe 오파싱(한글 rem/em-dash) 차단."""
    data = cmd_path.read_bytes()
    nonascii = [b for b in data if b > 127]
    assert not nonascii, (
        f"{cmd_path.name} 에 비-ASCII 바이트 {len(nonascii)}개 — ASCII-only 회귀"
    )


@pytest.mark.parametrize("cmd_path", _CMD_FORWARDERS, ids=lambda p: str(p.relative_to(REPO)))
def test_cmd_forwarder_uses_crlf(cmd_path):
    """.cmd forwarder 의 모든 줄바꿈이 CRLF — Windows 배치 LF 회귀 차단."""
    data = cmd_path.read_bytes()
    lf = data.count(b"\n")
    crlf = data.count(b"\r\n")
    assert lf > 0, f"{cmd_path.name} 에 줄바꿈이 없음"
    assert lf == crlf, (
        f"{cmd_path.name}: bare LF 발견(CRLF={crlf}, LF={lf}) — CRLF 아닌 줄 있음"
    )


@pytest.mark.parametrize("cmd_path", _CMD_FORWARDERS, ids=lambda p: str(p.relative_to(REPO)))
def test_cmd_forwarder_uses_windows_null_device(cmd_path):
    """.cmd forwarder 의 `where` 침묵 probe 가 **Windows null device(`nul`)** 를 쓴다 — POSIX
    `/dev/null` 회귀 차단(cp949 cmd.exe 가 `/dev/null` 을 `dev\\null` 파일로 오해 → 탐지 깨짐).

    ASCII-only 화(T-0068) 중 동작 라인 redirect 를 POSIX 형으로 잘못 바꾼 회귀를 빨간불로 잡는다.
    """
    text = cmd_path.read_text(encoding="ascii")
    assert "/dev/null" not in text, (
        f"{cmd_path.name}: POSIX `/dev/null` 발견 — Windows 배치는 `>nul 2>nul` 이어야 한다"
    )
    # 인터프리터 탐지 침묵 probe(`where ... >nul`)가 살아있는지 — Windows null device 사용 확인.
    assert ">nul" in text or ">NUL" in text, (
        f"{cmd_path.name}: `>nul` 침묵 redirect 가 없음 — `where` probe 가 출력을 샌다"
    )


# ── T-0068: .sh forwarder 는 LF 유지 (POSIX 회귀 가드) ───────────────────────


@pytest.mark.parametrize("sh_name", ["pm-config.sh", "pm-import.sh"])
def test_sh_forwarder_stays_lf(sh_name):
    """.sh forwarder 는 CR 0 — CRLF 오염되면 POSIX shebang/exec 가 깨진다."""
    data = (REPO / sh_name).read_bytes()
    assert b"\r" not in data, f"{sh_name} 에 CR 바이트 — LF 유지여야 함"


# ── T-0068: .gitattributes EOL 룰 존재 단언 ──────────────────────────────────


def test_gitattributes_enforces_cmd_crlf():
    """루트 .gitattributes 가 *.cmd eol=crlf / *.sh eol=lf 룰을 강제(체크아웃 가드)."""
    text = (REPO / ".gitattributes").read_text(encoding="utf-8")
    assert "*.cmd text eol=crlf" in text, "*.cmd eol=crlf 룰 누락"
    assert "*.bat text eol=crlf" in text, "*.bat eol=crlf 룰 누락"
    assert "*.sh text eol=lf" in text, "*.sh eol=lf 룰 누락"
