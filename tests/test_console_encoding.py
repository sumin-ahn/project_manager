"""T-0690: PowerShell 캡처용 콘솔 인코딩·원복·대체표 회귀 테스트."""
from __future__ import annotations

import ast
import builtins
import importlib.util
import os
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
MODULE_PATH = TOOLS / "console_encoding.py"
CONFTEST_PATH = REPO / "tests" / "conftest.py"
PROCESS_STATE_KEY = "_project_manager_console_encoding_state_v1"

# PM의 Win11 실측 체인: python.exe -> py.exe -> powershell.exe -> cmd.exe -> sshd.exe.
REAL_PY_LAUNCHER_CHAIN = [
    (10, 0, r"C:\Windows\System32\OpenSSH\sshd.exe"),
    (20, 10, r"C:\Windows\System32\cmd.exe"),
    (30, 20, r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"),
    (40, 30, r"C:\Windows\py.exe"),
    (50, 40, r"C:\Users\u\AppData\Local\Programs\Python\Python312\python.exe"),
]
DIRECT_PYTHON_CHAIN = [
    (10, 0, r"C:\Windows\System32\OpenSSH\sshd.exe"),
    (20, 10, r"C:\Windows\System32\cmd.exe"),
    (30, 20, r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"),
    (50, 30, r"C:\Users\u\AppData\Local\Programs\Python\Python312\python.exe"),
]
CAPTURING_PYTHON_PARENT_CHAIN = [
    (10, 0, r"C:\Windows\System32\OpenSSH\sshd.exe"),
    (20, 10, r"C:\Windows\System32\cmd.exe"),
    (30, 20, r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"),
    (40, 30, r"C:\Windows\py.exe"),
    (45, 40, r"C:\Users\u\AppData\Local\Programs\Python\Python312\python.exe"),
    (50, 45, r"C:\Users\u\AppData\Local\Programs\Python\Python312\python.exe"),
]


def _load_module(name="console_encoding_t0690"):
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _conftest_declares_ambient_pythonutf8() -> bool:
    """`tests/conftest.py` 소스에 ``os.environ["PYTHONUTF8"] = "1"`` 대입문이 있는지 AST 로 본다.

    값 단언(``os.environ.get("PYTHONUTF8") == "1"``)만으로는 conftest 가 심은 게 아니라
    ambient 셸이 이미 ``PYTHONUTF8=1`` 로 pytest 를 기동한 형상과 구분하지 못한다(F-001 —
    ambient PYTHONUTF8=1 형상에서 이 대입문을 지워도 값 단언만으로는 28 passed 로 false-green
    이었다). 소스 자체에 이 대입문이 있는지를 값과 별개로 고정해, 대입문이 사라지면 ambient
    셸이 우연히 ``PYTHONUTF8=1`` 이어도 이 단언은 그것과 무관하게 red 가 되게 한다.
    """
    tree = ast.parse(CONFTEST_PATH.read_text(encoding="utf-8"), filename=str(CONFTEST_PATH))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Subscript):
            continue
        target_base = target.value
        is_os_environ = (
            isinstance(target_base, ast.Attribute)
            and target_base.attr == "environ"
            and isinstance(target_base.value, ast.Name)
            and target_base.value.id == "os"
        )
        if not is_os_environ:
            continue
        key = target.slice
        if isinstance(key, ast.Constant) and key.value == "PYTHONUTF8":
            if isinstance(node.value, ast.Constant) and node.value.value == "1":
                return True
    return False


@pytest.fixture
def console_encoding():
    """프로세스 저장소도 격리해 각 테스트가 최초 CLI 진입을 모델링한다."""
    if hasattr(builtins, PROCESS_STATE_KEY):
        delattr(builtins, PROCESS_STATE_KEY)
    module = _load_module()
    try:
        yield module
    finally:
        if hasattr(builtins, PROCESS_STATE_KEY):
            delattr(builtins, PROCESS_STATE_KEY)


def test_probe_os_name_seam_defaults_to_the_real_interpreter_value(console_encoding):
    """기본 프로브는 전역 `os.name` 그대로다 — seam 추가가 동작을 바꾸지 않는다(T-0741)."""
    assert console_encoding._probe_os_name() == os.name


class _FakeKernel32:
    """console codepage와 Toolhelp32 snapshot을 함께 대역한다."""

    def __init__(self, output_cp=949, input_cp=949, entries=()):
        self.output_cp = output_cp
        self.input_cp = input_cp
        self.entries = list(entries)
        self.index = 0
        self.output_cp_calls: list[int] = []
        self.input_cp_calls: list[int] = []
        self.closed: list[int] = []
        self.events: list[str] = []

    def GetConsoleOutputCP(self):  # noqa: N802
        return self.output_cp

    def GetConsoleCP(self):  # noqa: N802
        return self.input_cp

    def SetConsoleOutputCP(self, cp):  # noqa: N802
        self.events.append(f"set-output:{cp}")
        self.output_cp_calls.append(cp)
        self.output_cp = cp
        return 1

    def SetConsoleCP(self, cp):  # noqa: N802
        self.events.append(f"set-input:{cp}")
        self.input_cp_calls.append(cp)
        self.input_cp = cp
        return 1

    def CreateToolhelp32Snapshot(self, flags, pid):  # noqa: N802
        assert flags == 0x00000002
        assert pid == 0
        return 17

    def _fill(self, pointer):
        pid, parent_pid, exe_name = self.entries[self.index]
        entry = pointer._obj
        entry.th32ProcessID = pid
        entry.th32ParentProcessID = parent_pid
        entry.szExeFile = exe_name

    def Process32FirstW(self, handle, pointer):  # noqa: N802
        assert handle == 17
        if not self.entries:
            return 0
        self.index = 0
        self._fill(pointer)
        return 1

    def Process32NextW(self, handle, pointer):  # noqa: N802
        assert handle == 17
        self.index += 1
        if self.index >= len(self.entries):
            return 0
        self._fill(pointer)
        return 1

    def CloseHandle(self, handle):  # noqa: N802
        self.closed.append(handle)
        return 1


class _Stream:
    def __init__(self, tty=False, events=None, reconfigure_error=None):
        self.tty = tty
        self.events = events
        self.reconfigure_error = reconfigure_error
        self.reconfigure_calls: list[dict] = []
        self.writes: list[str] = []
        self.flush_count = 0

    def isatty(self):
        return self.tty

    def reconfigure(self, **kwargs):
        if self.reconfigure_error is not None:
            raise self.reconfigure_error
        self.reconfigure_calls.append(kwargs)

    def write(self, text):
        self.writes.append(text)

    def flush(self):
        self.flush_count += 1
        if self.events is not None:
            self.events.append("flush")


def _install_windows(monkeypatch, module, kernel32, stdout=None, stderr=None):
    monkeypatch.setattr(module, "_probe_os_name", lambda: "nt")
    monkeypatch.setattr(module.os, "getpid", lambda: 50)
    monkeypatch.setattr(module, "_get_kernel32", lambda: kernel32)
    if stdout is not None:
        monkeypatch.setattr(module.sys, "stdout", stdout)
    if stderr is not None:
        monkeypatch.setattr(module.sys, "stderr", stderr)
    # 각 테스트가 PowerShell의 콘솔 CP reader를 모델링하도록, pytest 실행 부모가 준
    # Python UTF-8 환경은 기본적으로 지운다. UTF-8 reader 테스트만 이를 다시 설정한다.
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    monkeypatch.delenv("PYTHONUTF8", raising=False)


def test_codepages_are_saved_set_and_restored_once(console_encoding, monkeypatch):
    kernel32 = _FakeKernel32(output_cp=949, input_cp=437)
    callbacks = []
    _install_windows(monkeypatch, console_encoding, kernel32)
    monkeypatch.setattr(
        console_encoding.atexit,
        "register",
        lambda function, *args: callbacks.append((function, args)),
    )

    console_encoding._set_console_codepage_utf8()
    console_encoding._set_console_codepage_utf8()

    assert kernel32.output_cp_calls == [65001, 65001]
    assert kernel32.input_cp_calls == [65001, 65001]
    assert len(callbacks) == 1
    callback, args = callbacks[0]
    callback(*args)
    assert kernel32.output_cp_calls[-1] == 949
    assert kernel32.input_cp_calls[-1] == 437


@pytest.mark.parametrize(
    ("output_cp", "input_cp", "expected_output", "expected_input"),
    [
        (0, 949, [], [65001]),
        (949, 0, [65001], []),
        (0, 0, [], []),
    ],
)
def test_output_and_input_codepages_are_handled_independently(
    console_encoding, monkeypatch, output_cp, input_cp, expected_output, expected_input
):
    kernel32 = _FakeKernel32(output_cp=output_cp, input_cp=input_cp)
    callbacks = []
    _install_windows(monkeypatch, console_encoding, kernel32)
    monkeypatch.setattr(
        console_encoding.atexit,
        "register",
        lambda function, *args: callbacks.append((function, args)),
    )

    console_encoding._set_console_codepage_utf8()

    assert kernel32.output_cp_calls == expected_output
    assert kernel32.input_cp_calls == expected_input
    assert len(callbacks) == bool(expected_output or expected_input)


def test_restore_flushes_both_streams_before_codepage_changes(console_encoding, monkeypatch):
    events = []
    stdout = _Stream(events=events)
    stderr = _Stream(events=events)
    kernel32 = _FakeKernel32()
    kernel32.events = events
    monkeypatch.setattr(console_encoding.sys, "stdout", stdout)
    monkeypatch.setattr(console_encoding.sys, "stderr", stderr)

    console_encoding._restore_console_codepages(kernel32, 949, 437)

    assert events == ["flush", "flush", "set-output:949", "set-input:437"]


@pytest.mark.parametrize(
    ("chain", "tty", "expected_encoding", "reader_encoding", "hint_count"),
    [
        pytest.param(REAL_PY_LAUNCHER_CHAIN, False, "cp949", "cp949", 1,
                     id="real-py-launcher-chain-capture"),
        pytest.param(REAL_PY_LAUNCHER_CHAIN, True, "utf-8", "utf-8", 0,
                     id="real-py-launcher-chain-console"),
        pytest.param(DIRECT_PYTHON_CHAIN, False, "cp949", "cp949", 1,
                     id="direct-parent-capture"),
        pytest.param(DIRECT_PYTHON_CHAIN, True, "utf-8", "utf-8", 0,
                     id="direct-parent-console"),
    ],
)
def test_powershell_process_shape_and_destination_matrix(
    console_encoding,
    monkeypatch,
    chain,
    tty,
    expected_encoding,
    reader_encoding,
    hint_count,
):
    """실측/직접 부모 형상 각각에서 캡처와 콘솔 bytes가 독자 코덱으로 왕복한다."""
    kernel32 = _FakeKernel32(entries=chain)
    stdout = _Stream(tty=tty)
    stderr = _Stream(tty=tty)
    callbacks = []
    _install_windows(monkeypatch, console_encoding, kernel32, stdout, stderr)
    monkeypatch.setattr(
        console_encoding.atexit,
        "register",
        lambda function, *args: callbacks.append((function, args)),
    )

    console_encoding.configure_console_utf8()

    expected = {
        "encoding": expected_encoding,
        "errors": "pm_translit" if expected_encoding == "cp949" else "replace",
    }
    assert stdout.reconfigure_calls == [expected]
    assert stderr.reconfigure_calls == [expected]
    assert kernel32.output_cp_calls == [65001]
    assert len(stderr.writes) == hint_count
    sample = "repo 앵커: 한글 · 완료"
    emitted = sample.encode(expected_encoding, errors=expected["errors"])
    assert emitted.decode(reader_encoding) == sample
    assert console_encoding.console_state() == {
        "parent_name": "powershell.exe",
        "original_codepage": 949,
        "selected_encoding": "cp949" if not tty else None,
        "isatty": tty,
        "applied": True,
        "streams": {
            "stdout": {
                "isatty": tty,
                "selected_encoding": "cp949" if not tty else None,
                "applied": True,
            },
            "stderr": {
                "isatty": tty,
                "selected_encoding": "cp949" if not tty else None,
                "applied": True,
            },
        },
    }
    assert len(callbacks) == 1


@pytest.mark.parametrize(
    ("stdout_tty", "stderr_tty"),
    [
        pytest.param(False, True, id="stdout-pipe-stderr-tty"),
        pytest.param(True, False, id="stdout-tty-stderr-pipe"),
    ],
)
def test_stdout_and_stderr_apply_and_report_their_own_isatty_decision(
    console_encoding, monkeypatch, stdout_tty, stderr_tty
):
    kernel32 = _FakeKernel32(entries=REAL_PY_LAUNCHER_CHAIN)
    stdout = _Stream(tty=stdout_tty)
    stderr = _Stream(tty=stderr_tty)
    _install_windows(monkeypatch, console_encoding, kernel32, stdout, stderr)

    console_encoding.configure_console_utf8()

    expected_stdout = "utf-8" if stdout_tty else "cp949"
    expected_stderr = "utf-8" if stderr_tty else "cp949"
    assert stdout.reconfigure_calls == [{
        "encoding": expected_stdout,
        "errors": "replace" if stdout_tty else "pm_translit",
    }]
    assert stderr.reconfigure_calls == [{
        "encoding": expected_stderr,
        "errors": "replace" if stderr_tty else "pm_translit",
    }]
    assert len(stderr.writes) == 1
    assert console_encoding.console_state()["streams"] == {
        "stdout": {
            "isatty": stdout_tty,
            "selected_encoding": None if stdout_tty else "cp949",
            "applied": True,
        },
        "stderr": {
            "isatty": stderr_tty,
            "selected_encoding": None if stderr_tty else "cp949",
            "applied": True,
        },
    }


def test_capturing_python_parent_is_not_skipped_to_powershell(
    console_encoding, monkeypatch
):
    """python 부모가 UTF-8로 읽는 캡처 형상에서는 cp949 전환을 하지 않는다."""
    kernel32 = _FakeKernel32(entries=CAPTURING_PYTHON_PARENT_CHAIN)
    stdout = _Stream(tty=False)
    stderr = _Stream(tty=False)
    _install_windows(monkeypatch, console_encoding, kernel32, stdout, stderr)

    console_encoding.configure_console_utf8()

    expected = {"encoding": "utf-8", "errors": "replace"}
    assert stdout.reconfigure_calls == [expected]
    assert stderr.reconfigure_calls == [expected]
    assert stderr.writes == []
    assert console_encoding.console_state()["parent_name"] == "python.exe"
    assert console_encoding.console_state()["selected_encoding"] is None


@pytest.mark.parametrize(
    ("env_name", "env_value"),
    [
        pytest.param("PYTHONIOENCODING", "UTF-8:strict", id="pythonioencoding"),
        pytest.param("PYTHONUTF8", "1", id="pythonutf8"),
    ],
)
def test_python_utf8_reader_environment_suppresses_capture_codec_switch(
    console_encoding, monkeypatch, env_name, env_value
):
    """프로세스 체인과 별개로 부모가 준 UTF-8 reader 계약을 이중 방어로 존중한다."""
    kernel32 = _FakeKernel32(entries=REAL_PY_LAUNCHER_CHAIN)
    stdout = _Stream(tty=False)
    stderr = _Stream(tty=False)
    _install_windows(monkeypatch, console_encoding, kernel32, stdout, stderr)
    monkeypatch.setenv(env_name, env_value)

    console_encoding.configure_console_utf8()

    expected = {"encoding": "utf-8", "errors": "replace"}
    assert stdout.reconfigure_calls == [expected]
    assert stderr.reconfigure_calls == [expected]
    assert stderr.writes == []
    assert console_encoding.console_state()["parent_name"] == "powershell.exe"
    assert console_encoding.console_state()["selected_encoding"] is None


def test_conftest_declares_ambient_python_utf8_for_pytest_process():
    """`tests/conftest.py`(T-0762)가 이 pytest 프로세스 자기 env 에 UTF-8 reader 계약을 심었다.

    Windows 단독 실행(-n 없음·PowerShell 부모)에서 엔진 CLI `main()` 을 in-process 로 호출하는
    테스트가 `configure_console_utf8()` 을 거쳐 pytest 캡처 스트림을 cp949 로 오염시키는 결함을,
    엔진의 기존 탈출구 `_utf8_reader_requested()` 가 자식 env 가 아니라 **호출 프로세스 자신의**
    `os.environ` 을 읽는 값으로 닫는다. 발화 여부는 아래
    `test_ambient_pytest_utf8_reader_declaration_suppresses_capture_codec_switch` 가 잇는다.

    **F-001**: 값 단언(``os.environ.get`` 확인)만으로는 conftest 가 심은 게 아니라 ambient
    셸이 이미 ``PYTHONUTF8=1`` 로 pytest 를 기동한 형상과 구분하지 못해, 대입문을 지워도 값이
    우연히 ``"1"`` 이면 false-green 이었다 — 소스 존재 단언(AST)을 값 단언과 병행한다.
    """
    assert _conftest_declares_ambient_pythonutf8(), (
        'tests/conftest.py 소스에 os.environ["PYTHONUTF8"] = "1" 대입문이 없다 — T-0762 선언이 '
        "삭제/변형됐다(값이 ambient 셸에서 우연히 1 이어도 이 단언은 소스 자체를 본다)"
    )
    assert os.environ.get("PYTHONUTF8") == "1", (
        "ambient PYTHONUTF8 이 서 있지 않다 — tests/conftest.py 의 T-0762 선언이 없어졌다"
    )


def test_ambient_pytest_utf8_reader_declaration_suppresses_capture_codec_switch(
    console_encoding, monkeypatch
):
    """conftest 가 심은 ambient PYTHONUTF8=1(T-0762)을 지우지 않은 채로도 cp949 전환이 억제된다.

    이 파일의 다른 테스트는 `_install_windows()` 가 각 테스트마다 ambient
    PYTHONUTF8/PYTHONIOENCODING 을 지워 PowerShell 콘솔 CP reader 형상을 독립적으로 재현한다
    (그 헬퍼 주석 참고) — 그래서 conftest 선언과 무관하게 성립한다. 이 테스트만 PYTHONUTF8 의
    delenv 를 건너뛰어, ambient 선언 **그 자체가 값으로 발화**하는지를 고정한다:
    `tests/conftest.py` 의 ``os.environ["PYTHONUTF8"] = "1"`` 선언을 지우면 이 테스트가 red 로
    돌아간다(cp949 로 reconfigure 되어 `selected_encoding` 이 ``"cp949"`` 가 된다).

    **F-001**: PYTHONIOENCODING 은 명시적으로 delenv 한다 — `_utf8_reader_requested()` 는
    PYTHONIOENCODING=utf-8 도 같은 축으로 존중하므로, 그걸 지우지 않으면 ambient 셸에
    PYTHONIOENCODING=utf-8 만 있어도(PYTHONUTF8 대입문이 없어도) 이 테스트가 green 이 되는
    혼선(PYTHONUTF8 축을 검증한다는 착각)이 생긴다.
    """
    kernel32 = _FakeKernel32(entries=REAL_PY_LAUNCHER_CHAIN)
    stdout = _Stream(tty=False)
    stderr = _Stream(tty=False)
    monkeypatch.setattr(console_encoding, "_probe_os_name", lambda: "nt")
    monkeypatch.setattr(console_encoding.os, "getpid", lambda: 50)
    monkeypatch.setattr(console_encoding, "_get_kernel32", lambda: kernel32)
    monkeypatch.setattr(console_encoding.sys, "stdout", stdout)
    monkeypatch.setattr(console_encoding.sys, "stderr", stderr)
    # 의도적으로 `_install_windows()` 를 쓰지 않는다 — ambient PYTHONUTF8 을 지우면 conftest
    # 선언이 살아 있는 실제 pytest 프로세스 형상을 재현하지 못한다. PYTHONIOENCODING 만 지워
    # (F-001) 그 축의 ambient 가 이 테스트를 PYTHONUTF8 축과 무관하게 통과시키지 못하게 한다
    # (ambient PYTHONIOENCODING=utf-8 형상에서 PYTHONUTF8 대입문을 지워도 green 이었다).
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)

    console_encoding.configure_console_utf8()

    expected = {"encoding": "utf-8", "errors": "replace"}
    assert stdout.reconfigure_calls == [expected]
    assert stderr.reconfigure_calls == [expected]
    assert stderr.writes == []
    assert console_encoding.console_state()["parent_name"] == "powershell.exe"
    assert console_encoding.console_state()["selected_encoding"] is None


def test_console_state_reports_partial_stream_application(console_encoding, monkeypatch):
    kernel32 = _FakeKernel32(entries=REAL_PY_LAUNCHER_CHAIN)
    stdout = _Stream(tty=False, reconfigure_error=OSError("stdout refused"))
    stderr = _Stream(tty=False)
    _install_windows(monkeypatch, console_encoding, kernel32, stdout, stderr)

    console_encoding.configure_console_utf8()

    state = console_encoding.console_state()
    assert state["applied"] is False
    assert state["streams"] == {
        "stdout": {"isatty": False, "selected_encoding": None, "applied": False},
        "stderr": {"isatty": False, "selected_encoding": "cp949", "applied": True},
    }


@pytest.mark.parametrize("owner_name", ["pm_config", "worktree_pool"])
def test_actual_cache_false_entry_pattern_preserves_process_state_across_two_loads(
    console_encoding, monkeypatch, owner_name
):
    """두 실제 CLI의 loader로 새 모듈을 2회 만들며 최초 CP·코덱·hint를 보존한다."""
    owner_spec = importlib.util.spec_from_file_location(
        f"{owner_name}_t0690", TOOLS / f"{owner_name}.py"
    )
    owner = importlib.util.module_from_spec(owner_spec)
    owner_spec.loader.exec_module(owner)
    first = owner._load_module_from_path(
        MODULE_PATH,
        "console_encoding.py",
        verifier=owner._verify_engine_rev,
        cache=False,
    )
    second = owner._load_module_from_path(
        MODULE_PATH,
        "console_encoding.py",
        verifier=owner._verify_engine_rev,
        cache=False,
    )
    assert first is not second

    kernel32 = _FakeKernel32(entries=REAL_PY_LAUNCHER_CHAIN)
    stdout = _Stream(tty=False)
    stderr = _Stream(tty=False)
    callbacks = []
    for module in (first, second):
        _install_windows(monkeypatch, module, kernel32, stdout, stderr)
        monkeypatch.setattr(
            module.atexit,
            "register",
            lambda function, *args: callbacks.append((function, args)),
        )

    first.configure_console_utf8()
    second.configure_console_utf8()

    expected = {"encoding": "cp949", "errors": "pm_translit"}
    assert stdout.reconfigure_calls == [expected, expected]
    assert stderr.reconfigure_calls == [expected, expected]
    assert len(stderr.writes) == 1
    assert len(callbacks) == 1
    assert first.console_state()["original_codepage"] == 949
    assert second.console_state()["selected_encoding"] == "cp949"
    callback, args = callbacks[0]
    callback(*args)
    assert kernel32.output_cp == 949
    assert kernel32.input_cp == 949


def test_unknown_windows_codepage_keeps_utf8_and_reports_not_selected(
    console_encoding, monkeypatch
):
    kernel32 = _FakeKernel32(output_cp=708, input_cp=708, entries=REAL_PY_LAUNCHER_CHAIN)
    stdout = _Stream(tty=False)
    stderr = _Stream(tty=False)
    _install_windows(monkeypatch, console_encoding, kernel32, stdout, stderr)

    console_encoding.configure_console_utf8()

    expected = {"encoding": "utf-8", "errors": "replace"}
    assert stdout.reconfigure_calls == [expected]
    assert stderr.reconfigure_calls == [expected]
    assert stderr.writes == []
    assert console_encoding.console_state()["selected_encoding"] is None


def test_failed_capture_reconfigure_is_not_reported_as_applied(console_encoding, monkeypatch):
    kernel32 = _FakeKernel32(entries=REAL_PY_LAUNCHER_CHAIN)
    error = OSError("capture stream refuses reconfigure")
    stdout = _Stream(tty=False, reconfigure_error=error)
    stderr = _Stream(tty=False, reconfigure_error=error)
    _install_windows(monkeypatch, console_encoding, kernel32, stdout, stderr)

    console_encoding.configure_console_utf8()

    assert stderr.writes == []
    assert console_encoding.console_state()["selected_encoding"] is None
    assert console_encoding.console_state()["applied"] is False


@pytest.mark.parametrize("parent_name", ["cmd.exe", "bash.exe", None])
def test_non_powershell_parent_has_no_capture_encoding(
    console_encoding, monkeypatch, parent_name
):
    monkeypatch.setattr(console_encoding, "_probe_os_name", lambda: "nt")
    console_encoding._process_state()["original_output_cp"] = 949
    monkeypatch.setattr(console_encoding, "_parent_process_name", lambda: parent_name)

    assert console_encoding._powershell_capture_encoding() is None


def test_translit_handler_maps_known_symbols_and_falls_back_to_question_mark(
    console_encoding,
):
    assert "—✓→🙂".encode("ascii", errors="pm_translit") == b"-v->?"
    assert "–←✗•…“”‘’⚠".encode("ascii", errors="pm_translit") == b'-<-x*...\"\"\'\'!'
    assert "한글·".encode("cp949", errors="pm_translit").decode("cp949") == "한글·"


def test_non_windows_functions_are_noop_and_streams_stay_utf8(console_encoding, monkeypatch):
    stdout = _Stream()
    stderr = _Stream()
    monkeypatch.setattr(console_encoding, "_probe_os_name", lambda: "posix")
    monkeypatch.setattr(
        console_encoding,
        "_get_kernel32",
        lambda: pytest.fail("POSIX에서 kernel32 경계에 진입함"),
    )
    monkeypatch.setattr(console_encoding.sys, "stdout", stdout)
    monkeypatch.setattr(console_encoding.sys, "stderr", stderr)

    assert console_encoding._parent_process_name() is None
    assert console_encoding._powershell_capture_encoding() is None
    console_encoding.configure_console_utf8()

    assert stdout.reconfigure_calls == [{"encoding": "utf-8", "errors": "replace"}]
    assert stderr.reconfigure_calls == [{"encoding": "utf-8", "errors": "replace"}]
    assert stderr.writes == []


def test_console_hint_can_be_suppressed(console_encoding, monkeypatch):
    kernel32 = _FakeKernel32(entries=REAL_PY_LAUNCHER_CHAIN)
    stderr = _Stream(tty=False)
    _install_windows(monkeypatch, console_encoding, kernel32, _Stream(tty=False), stderr)
    monkeypatch.setenv("PM_CONSOLE_HINT", "0")

    console_encoding.configure_console_utf8()

    assert stderr.reconfigure_calls == [{"encoding": "cp949", "errors": "pm_translit"}]
    assert stderr.writes == []
