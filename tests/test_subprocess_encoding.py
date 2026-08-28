"""subprocess 텍스트 캡처 인코딩 sweep (T-0019 · C3+C5) 단위 테스트.

엔진 도구들이 `text=True` 로 git/pytest/하니스 stdout 을 캡처할 때 cp949(Windows
기본 콘솔 코덱)이 아니라 명시적 UTF-8 로 디코딩하는지를 검증한다. 인코딩 미지정이면
한글 커밋 메시지·diff·로그를 캡처하다 UnicodeDecodeError 로 크래시한다.

검증 축:
  - additional_reviewer: git diff 캡처 + 리뷰어 호출이 encoding="utf-8", errors="replace".
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
import ast
import builtins
import importlib.util
import io
import os
import subprocess
from pathlib import Path

import pytest

from _textio import write_lf

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
SCRIPTS = REPO / "scripts"


def _load(name: str, base: Path):
    spec = importlib.util.spec_from_file_location(name, base / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def additional_reviewer():
    return _load("additional_reviewer", TOOLS)


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


@pytest.fixture
def console_encoding():
    state_key = "_project_manager_console_encoding_state_v1"
    if hasattr(builtins, state_key):
        delattr(builtins, state_key)
    module = _load("console_encoding", TOOLS)
    try:
        yield module
    finally:
        if hasattr(builtins, state_key):
            delattr(builtins, state_key)


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


# ── additional_reviewer (run_fn DI 로 직접 주입) ────────────────────────────────


def test_extract_diff_passes_utf8_encoding(additional_reviewer):
    rec = _Recorder()
    additional_reviewer.extract_diff("main", ["foo.py"], run_fn=rec)
    assert rec.calls, "git diff 캡처 호출이 일어나지 않음"
    for kwargs in rec.calls:
        _assert_utf8(kwargs)


def test_extract_diff_head_path_passes_utf8(additional_reviewer):
    rec = _Recorder(stdout="")  # 빈 staged/unstaged → HEAD~1 폴백까지 모두 거침
    additional_reviewer.extract_diff("HEAD", ["foo.py"], run_fn=rec)
    assert len(rec.calls) >= 2
    for kwargs in rec.calls:
        _assert_utf8(kwargs)


def test_run_reviewer_passes_utf8_encoding(additional_reviewer):
    rec = _Recorder()
    ok, _ = additional_reviewer.run_reviewer("echo hi", reviewer_cmd="echo hi", run_fn=rec)
    assert rec.calls
    _assert_utf8(rec.calls[0])


# ── pm_import ──────────────────────────────────────────────────────────────


def test_pm_import_harness_runner_uses_utf8_watchdog(pm_import, monkeypatch):
    """fill 실 워치독의 Popen 경계가 UTF-8/replace를 실제로 전달한다."""
    calls = []

    def recording_forbidden_popen(argv, **kwargs):
        calls.append((argv, kwargs))
        raise FileNotFoundError("spawn 차단 sentinel")

    monkeypatch.setattr(pm_import.subprocess, "Popen", recording_forbidden_popen)
    ok, _ = pm_import._real_harness_runner(
        ["claude", "-p", "분석 프롬프트"], "프롬프트")

    assert ok is False
    assert len(calls) == 1, "워치독 우회 또는 중복 spawn"
    _assert_utf8(calls[0][1])
    assert calls[0][1]["stdout"] is subprocess.PIPE
    assert calls[0][1]["stderr"] is subprocess.PIPE


def test_pm_import_board_init_passes_utf8(pm_import, monkeypatch, tmp_path):
    tools = tmp_path / ".project_manager" / "tools"
    tools.mkdir(parents=True)
    (tools / "pm_config.py").write_text("# stub", encoding="utf-8")
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
    """subprocess.Popen 대역 — env kwargs 를 기록하고 returncode 0 을 돌려준다.

    pytest 자식을 실제 기동하지 않고 env 전달만 검증한다. 회귀 러너는 출력을 tee(실시간 echo +
    캡처)하려고 `Popen` 으로 띄우므로 대역도 스트림 + `wait()` 를 갖춘 프로세스 형태다.
    """

    class _Proc:
        def __init__(self):
            self.stdout = io.StringIO("")
            self.stderr = io.StringIO("")

        def wait(self):
            return 0

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, *args, **kwargs):
        self.calls.append(kwargs)
        return self._Proc()


def _regression_child_call(calls: list[dict]) -> dict:
    """기록된 자식 호출 중 **회귀 러너의 것**을 고른다 — test_cmd 를 `shell` 로 띄우는 그 자식.

    `cmd_regression` 진입이 띄우는 자식은 회귀 러너 하나가 아니다: 훅이 순수 해소 위치에 없으면
    훅 위치 **권위 해소**(`git rev-parse --git-path hooks`)가 그 앞에서 돈다. 훅을 안 깐 채택자
    체크아웃이 정확히 그 형상이라(Windows 실측), 첫 호출을 무조건 회귀 자식으로 보면 가드가
    엉뚱한 자식의 kwargs(`env` 없는 git 질의)를 판정해 상시 red 가 된다.
    """
    shell_calls = [call for call in calls if call.get("shell")]
    assert len(shell_calls) == 1, f"회귀 자식(shell) 호출을 특정하지 못함: {calls!r}"
    return shell_calls[0]


@pytest.mark.parametrize("pre_push_hook_installed", [True, False],
                         ids=["hook-resolved", "hook-unresolved"])
def test_regression_run_child_forces_utf8_env(board, monkeypatch, tmp_path,
                                              pre_push_hook_installed):
    """cmd_regression(run, scoped) 이 pytest 자식 env 에 UTF-8 강제·os.environ 보존.

    scoped 경로(touches 지정)는 자식 실행 직후 반환 → 플래그 파일/_git_head 미경유.
    수정 전(env 미전달)에서는 이 단언이 깨진다.

    **두 형상을 모두 태운다**: 훅 위치가 순수 해소되는 트리(회귀 자식이 유일한 자식)와, 훅이
    없어 권위 해소 git 질의가 **먼저** 도는 트리(채택자 fresh clone·Windows VM 실측). 후자에서
    가드가 부수 자식을 판정하지 않는지까지 잠근다.
    """
    if pre_push_hook_installed:
        hooks = tmp_path / "h"
        hooks.mkdir()
        write_lf(hooks / "pre-push", "#!/bin/sh\nexit 0\n")   # 서명 없는 남의 훅 = 무영향
        monkeypatch.setattr(board, "_pure_hooks_dir", lambda: hooks)
    else:
        # 순수 해소가 위치를 확정 못 하는 형상 — 엔진이 git 에 훅 위치를 묻는다(부수 자식).
        monkeypatch.setattr(board, "_pure_hooks_dir", lambda: None)
    rec = _RcRecorder()
    monkeypatch.setattr(board.subprocess, "Popen", rec)
    # os.environ 보존 검증용 마커 키.
    monkeypatch.setenv("T0024_SENTINEL", "preserved")
    args = argparse.Namespace(action="run", cmd=None, ticket=None,
                              touches="tests/test_subprocess_encoding.py")
    rc = board.cmd_regression(args)
    assert rc == 0
    assert rec.calls, "pytest 자식 subprocess 호출이 일어나지 않음"
    # 주입 선-단언 — 의도한 형상이 **실제로** 태워졌는지 먼저 못박는다(형상이 안 서면 가드가
    # 아무것도 시험하지 않는다).
    incidental = [call for call in rec.calls if not call.get("shell")]
    if pre_push_hook_installed:
        assert incidental == [], f"부수 자식이 없어야 하는 형상인데 떴다: {incidental!r}"
    else:
        assert incidental, "훅 위치 권위 해소(git) 자식이 안 떴다 — 형상이 서지 않음"
        assert not rec.calls[0].get("shell"), "부수 자식이 회귀 자식보다 먼저여야 형상이 맞다"
    child = _regression_child_call(rec.calls)
    env = child.get("env")
    assert env is not None, f"자식에 env 미전달: {child!r}"
    assert env.get("PYTHONUTF8") == "1", f"PYTHONUTF8=1 누락: {env!r}"
    assert env.get("PYTHONIOENCODING") == "utf-8", f"PYTHONIOENCODING=utf-8 누락: {env!r}"
    # 기존 os.environ 키 보존(병합이지 치환 아님).
    assert env.get("T0024_SENTINEL") == "preserved"
    assert "PATH" in env or "PATH" not in os.environ


# ── T-0068: Windows 콘솔 codepage UTF-8 셋업 (SetConsoleOutputCP/CP 65001) ────


class _FakeKernel32:
    """ctypes.windll.kernel32 대역 — 원 codepage와 Set 호출을 기록한다."""

    def __init__(self, output_cp=949, input_cp=949):
        self.output_cp = output_cp
        self.input_cp = input_cp
        self.output_cp_calls: list[int] = []
        self.input_cp_calls: list[int] = []

    def GetConsoleOutputCP(self):  # noqa: N802 (WinAPI 이름 보존)
        return self.output_cp

    def GetConsoleCP(self):  # noqa: N802 (WinAPI 이름 보존)
        return self.input_cp

    def SetConsoleOutputCP(self, cp):  # noqa: N802 (WinAPI 이름 보존)
        self.output_cp_calls.append(cp)
        return 1

    def SetConsoleCP(self, cp):  # noqa: N802 (WinAPI 이름 보존)
        self.input_cp_calls.append(cp)
        return 1


def _install_fake_ctypes(monkeypatch, console_encoding):
    """kernel32 조회 seam에 대역을 주입해 OS 무관하게 SetConsole*CP를 관측한다."""
    kernel32 = _FakeKernel32()
    monkeypatch.setattr(console_encoding, "_get_kernel32", lambda: kernel32)
    return kernel32


def test_common_codepage_set_on_windows(console_encoding, monkeypatch):
    """os.name=='nt' 에서 _set_console_codepage_utf8 가 65001 두 codepage 를 설정."""
    monkeypatch.setattr(console_encoding, "_probe_os_name", lambda: "nt")
    kernel32 = _install_fake_ctypes(monkeypatch, console_encoding)
    console_encoding._set_console_codepage_utf8()
    assert kernel32.output_cp_calls == [65001], "SetConsoleOutputCP(65001) 누락/오인자"
    assert kernel32.input_cp_calls == [65001], "SetConsoleCP(65001) 누락/오인자"


def test_common_codepage_noop_on_posix(console_encoding, monkeypatch):
    """os.name!='nt'(POSIX) 에서는 분기에 진입하지 않아 SetConsole*CP 미호출."""
    monkeypatch.setattr(console_encoding, "_probe_os_name", lambda: "posix")
    kernel32 = _install_fake_ctypes(monkeypatch, console_encoding)
    console_encoding._set_console_codepage_utf8()
    assert kernel32.output_cp_calls == [], "POSIX 에서 SetConsoleOutputCP 가 호출됨"
    assert kernel32.input_cp_calls == [], "POSIX 에서 SetConsoleCP 가 호출됨"


def test_codepage_best_effort_swallows_exception(console_encoding, monkeypatch):
    """ctypes 호출이 예외(콘솔 핸들 없음 등)를 던져도 조용히 통과(best-effort)."""
    monkeypatch.setattr(console_encoding, "_probe_os_name", lambda: "nt")

    class _Boom:
        def SetConsoleOutputCP(self, cp):  # noqa: N802
            raise OSError("no console handle")

        def SetConsoleCP(self, cp):  # noqa: N802
            raise OSError("no console handle")

    monkeypatch.setattr(console_encoding, "_get_kernel32", lambda: _Boom())
    # 예외가 새어나오면 이 호출이 raise — pytest 가 실패로 잡는다.
    console_encoding._set_console_codepage_utf8()


def test_common_stream_reconfigure_is_guarded_and_best_effort(console_encoding, monkeypatch):
    """지원 스트림은 utf-8/replace로 바꾸고, 미지원·실패 스트림은 CLI를 막지 않는다."""
    class _Records:
        def __init__(self):
            self.calls = []

        def reconfigure(self, **kwargs):
            self.calls.append(kwargs)

    class _Raises:
        def reconfigure(self, **kwargs):
            raise OSError("capture stream refuses reconfigure")

    stdout = _Records()
    monkeypatch.setattr(console_encoding.sys, "stdout", stdout)
    monkeypatch.setattr(console_encoding.sys, "stderr", _Raises())
    monkeypatch.setattr(console_encoding, "_probe_os_name", lambda: "posix")
    console_encoding.configure_console_utf8()
    assert stdout.calls == [{"encoding": "utf-8", "errors": "replace"}]

    # hasattr 가드: reconfigure 자체가 없는 스트림도 그대로 통과한다.
    monkeypatch.setattr(console_encoding.sys, "stdout", object())
    console_encoding.configure_console_utf8()


def _has_main_guard(tree: ast.AST) -> bool:
    """AST에 ``if __name__ == "__main__"`` 진입 가드가 있는지."""
    return any(
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "__name__"
        and any(
            isinstance(comparator, ast.Constant) and comparator.value == "__main__"
            for comparator in node.test.comparators
        )
        for node in ast.walk(tree)
    )


def test_console_codepage_helper_has_single_definition():
    """codepage 제어 구현은 공용 모듈 한 곳뿐이다(복붙 재도입 차단)."""
    definitions = []
    for path in TOOLS.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "_set_console_codepage_utf8"
            for node in ast.walk(tree)
        ):
            definitions.append(path.name)
    assert definitions == ["console_encoding.py"]


def test_every_main_entrypoint_calls_common_console_helper():
    """tools/*.py의 현재·미래 ``__main__`` 전수가 main 최상위 선행부에서 공용 helper를 호출한다."""
    entrypoints = {}
    for path in TOOLS.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if path.name == "python_floor.py":
            # 실제 엔진보다 먼저 구 Python(2.7 포함)에서 실행되는 ASCII-only bootstrap probe.
            # console_encoding.py 자체는 3.11 엔진이므로 이 probe가 먼저 하한을 판정해야 한다.
            continue
        if _has_main_guard(tree):
            entrypoints[path.name] = tree

    assert entrypoints, "__main__ 진입점 스캔 결과가 비었다(공허 가드)"
    missing = []
    for filename, tree in sorted(entrypoints.items()):
        main_defs = [
            node for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "main"
        ]
        if len(main_defs) != 1:
            missing.append(f"{filename}: top-level main 정의 {len(main_defs)}개")
            continue
        main_tree = main_defs[0]

        scan_body = list(main_tree.body)
        if (
            scan_body
            and isinstance(scan_body[0], ast.Expr)
            and isinstance(scan_body[0].value, ast.Constant)
            and isinstance(scan_body[0].value.value, str)
        ):
            scan_body = scan_body[1:]
        # Marked engine-skew를 traceback 대신 사용자 진단+rc로 번역하는 진입점은 loader와
        # helper를 최외곽 try 안에 둔다. try.body의 선행 순서를 동일한 계약으로 검사한다.
        if len(scan_body) == 1 and isinstance(scan_body[0], ast.Try):
            scan_body = scan_body[0].body

        def direct_call(stmt):
            if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Call):
                return stmt.value
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
                return stmt.value
            return None

        def call_name(call):
            if isinstance(call.func, ast.Name):
                return call.func.id
            if isinstance(call.func, ast.Attribute):
                return call.func.attr
            return None

        helper_indexes = [
            index
            for index, stmt in enumerate(scan_body)
            if isinstance(stmt, ast.Expr)
            and (call := direct_call(stmt)) is not None
            and call_name(call) == "configure_console_utf8"
        ]
        if len(helper_indexes) != 1:
            missing.append(f"{filename}: main 최상위 helper 호출 {len(helper_indexes)}개")
            continue

        helper_index = helper_indexes[0]
        leading = scan_body[:helper_index]
        if (
            leading
            and isinstance(leading[0], ast.Expr)
            and isinstance(leading[0].value, ast.Constant)
            and isinstance(leading[0].value.value, str)
        ):
            leading = leading[1:]  # 함수 docstring은 출력/dispatch가 아닌 메타데이터
        allowed_loader_calls = {
            "_load_module_from_path",
            "spec_from_file_location",
            "module_from_spec",
            "exec_module",
            "_verify_engine_rev",
        }
        unexpected = []
        for stmt in leading:
            call = direct_call(stmt)
            name = call_name(call) if call is not None else type(stmt).__name__
            if name not in allowed_loader_calls:
                unexpected.append(name)
        loads_common_file = any(
            isinstance(node, ast.Constant) and node.value == "console_encoding.py"
            for stmt in scan_body[:helper_index + 1]
            for node in ast.walk(stmt)
        )
        if unexpected or not loads_common_file:
            missing.append(
                f"{filename}: helper 전 비-로더 statement={unexpected}, "
                f"console_encoding 로드={loads_common_file}"
            )

    assert not missing, (
        "__main__ 보유 도구의 main() 최상위 선행 console helper 관용구 위반: "
        f"{missing}; 스캔 전수={sorted(entrypoints)}. 각 main() 첫 동작에 "
        "`_load_module_from_path(... console_encoding.py, verifier/allow_unverified=...) "
        "→ _console_encoding.configure_console_utf8()`를 "
        "parser/print/dispatch보다 먼저 넣어라."
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
