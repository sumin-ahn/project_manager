#!/usr/bin/env python3
"""CLI 콘솔 출력 인코딩을 실행 환경과 정합하는 공용 부트스트랩.

Windows 콘솔 codepage와 Python 텍스트 스트림을 함께 맞춘다. 모든 단계는 best-effort라
콘솔 핸들이 없거나 테스트 캡처 스트림이 ``reconfigure``를 지원하지 않아도 CLI 본동작을
막지 않는다.
"""
from __future__ import annotations

import atexit
import builtins
import codecs
import ctypes
import ntpath
import os
import sys
from ctypes import wintypes
from typing import Any


# 여러 CLI가 공유하는 엔진 의존성이므로 부분 전파 skew 가드에 편입한다.
ENGINE_REV = "v1.7.9"

_UTF8_CODEPAGE = 65001
_TH32CS_SNAPPROCESS = 0x00000002
_POWERSHELL_PROCESS_NAMES = frozenset({"powershell.exe", "pwsh.exe"})
# ``py``/``pyw``는 stdio를 그대로 통과시키는 런처다. 반면 ``python``/``pythonw``는
# 자식 출력을 캡처·디코드할 수 있는 실제 프로세스이므로 투명한 래퍼로 취급하지 않는다.
_PYTHON_WRAPPER_PROCESS_NAMES = frozenset({"py.exe", "pyw.exe"})
_MAX_ANCESTOR_DEPTH = 8
_PROCESS_STATE_KEY = "_project_manager_console_encoding_state_v1"
_TRANSLIT = {
    "—": "-",
    "–": "-",
    "→": "->",
    "←": "<-",
    "✓": "v",
    "✗": "x",
    "•": "*",
    "…": "...",
    "“": '"',
    "”": '"',
    "‘": "'",
    "’": "'",
    "⚠": "!",
}
_HINT_TEMPLATE = (
    "[console] PowerShell 부모 감지 · 콘솔 코드페이지 {cp} — 한글 완전 표시는 프로필에 "
    "[Console]::OutputEncoding=[Text.Encoding]::UTF8 (설정 시 이 안내 사라짐)"
)


def _process_state() -> dict[str, Any]:
    """모듈 재로드와 무관하게 인터프리터 수명 동안 공유되는 콘솔 상태를 반환한다.

    ``pm_config``와 ``worktree_pool``은 실제 CLI 진입에서 이 파일을 ``cache=False``로
    두 번 로드한다. 따라서 최초 codepage와 atexit/hint 중복 방지는 모듈 전역에 둘 수
    없다. builtins의 전용 키는 그 두 독립 모듈 인스턴스가 공유하는 최소 프로세스 저장소다.
    """
    state = getattr(builtins, _PROCESS_STATE_KEY, None)
    if not isinstance(state, dict):
        state = {
            "original_output_cp": None,
            "original_input_cp": None,
            "parent_name": None,
            "selected_encoding": None,
            "restore_registered": False,
            "hint_shown": False,
            "isatty": None,
            "applied": False,
            "streams": {
                "stdout": {
                    "isatty": None,
                    "selected_encoding": None,
                    "applied": False,
                },
                "stderr": {
                    "isatty": None,
                    "selected_encoding": None,
                    "applied": False,
                },
            },
        }
        setattr(builtins, _PROCESS_STATE_KEY, state)
    return state


def _probe_os_name() -> str:
    """Read the interpreter OS family through one injectable seam.

    The four Windows-branch functions below (`_set_console_codepage_utf8`,
    `_toolhelp_processes`, `_parent_process_name`, `_powershell_capture_encoding`)
    each gate on `os.name`. Tests that need the Windows branch on any host
    cannot rebind the global `os.name` directly — `pathlib` consults that same
    global to pick its flavour, and rebinding it mid-test breaks every path
    operation in the process (`NotImplementedError` on a `PosixPath` built
    under a monkeypatched `os.name == "nt"`). Routing the read through this
    function keeps the injection point inside this module instead of on the
    `os` global.
    """
    return os.name


def _get_kernel32() -> Any:
    """실 kernel32를 늦게 구한다. 테스트는 이 경계를 fake로 교체할 수 있다."""
    return ctypes.windll.kernel32


def _restore_console_codepages(
    kernel32: Any, output_cp: int | None, input_cp: int | None
) -> None:
    """엔진이 바꾼 콘솔 codepage를 독립적인 best-effort 호출로 원복한다."""
    # Python의 표준 스트림 최종 flush보다 atexit 콜백이 먼저 실행된다. 아직 버퍼에 남은
    # UTF-8/cp949 bytes가 원복된 codepage 아래에서 뒤늦게 나가지 않도록 먼저 비운다.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass
    try:
        if output_cp:
            kernel32.SetConsoleOutputCP(output_cp)
    except Exception:
        pass
    try:
        if input_cp:
            kernel32.SetConsoleCP(input_cp)
    except Exception:
        pass


def _set_console_codepage_utf8() -> None:
    """Windows 콘솔 codepage를 보관한 뒤 UTF-8로 맞추고 종료 원복을 등록한다."""
    if _probe_os_name() != "nt":
        return
    try:
        state = _process_state()
        kernel32 = _get_kernel32()
        output_cp = int(kernel32.GetConsoleOutputCP())
        input_cp = int(kernel32.GetConsoleCP())
        # 출력/입력 콘솔은 독립 핸들이다. 둘 다 0일 때만 할 일이 없고, 한쪽만 있으면
        # 그쪽의 최초값 보관·UTF-8 전환·원복을 계속 수행한다.
        if not output_cp and not input_cp:
            return

        # configure가 중복 호출돼도 최초 원본과 atexit 콜백 하나만 보존한다.
        if output_cp and state["original_output_cp"] is None:
            state["original_output_cp"] = output_cp
        if input_cp and state["original_input_cp"] is None:
            state["original_input_cp"] = input_cp

        output_changed = False
        input_changed = False
        if output_cp:
            try:
                output_changed = bool(kernel32.SetConsoleOutputCP(_UTF8_CODEPAGE))
            except Exception:
                pass
        if input_cp:
            try:
                input_changed = bool(kernel32.SetConsoleCP(_UTF8_CODEPAGE))
            except Exception:
                pass
        if (output_changed or input_changed) and not state["restore_registered"]:
            atexit.register(
                _restore_console_codepages,
                kernel32,
                state["original_output_cp"],
                state["original_input_cp"],
            )
            state["restore_registered"] = True
    except Exception:
        pass


def _toolhelp_processes(kernel32: Any | None = None) -> list[tuple[int, int, str]]:
    """Toolhelp32 snapshot을 ``(pid, parent_pid, exe)`` 목록으로 바꾼다.

    ``kernel32`` 인자는 Windows VM 없이도 동일한 ctypes 포인터 경계를 검증하기 위한
    주입 seam이다. WinAPI 실패는 빈 목록으로 축약한다.
    """
    if _probe_os_name() != "nt":
        return []
    try:
        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260),
            ]

        api = kernel32 if kernel32 is not None else _get_kernel32()
        create_snapshot = api.CreateToolhelp32Snapshot
        try:
            create_snapshot.restype = wintypes.HANDLE
        except Exception:
            pass
        snapshot = create_snapshot(_TH32CS_SNAPPROCESS, 0)
        invalid_handle = ctypes.c_void_p(-1).value
        if snapshot in (None, 0, -1, invalid_handle):
            return []

        entries: list[tuple[int, int, str]] = []
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            ok = api.Process32FirstW(snapshot, ctypes.byref(entry))
            while ok:
                entries.append(
                    (
                        int(entry.th32ProcessID),
                        int(entry.th32ParentProcessID),
                        str(entry.szExeFile),
                    )
                )
                ok = api.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            try:
                api.CloseHandle(snapshot)
            except Exception:
                pass
        return entries
    except Exception:
        return []


def _parent_process_name() -> str | None:
    """Python 래퍼를 건너뛴 첫 조상의 exe 이름을 소문자로 반환한다."""
    if _probe_os_name() != "nt":
        return None
    try:
        processes = {
            pid: (parent_pid, exe_name)
            for pid, parent_pid, exe_name in _toolhelp_processes()
        }
        current_pid = os.getpid()
        visited = {current_pid}
        for _ in range(_MAX_ANCESTOR_DEPTH):
            current = processes.get(current_pid)
            if current is None:
                return None
            parent_pid = current[0]
            if parent_pid in visited:
                return None
            visited.add(parent_pid)
            parent = processes.get(parent_pid)
            if parent is None or not parent[1]:
                return None
            parent_name = ntpath.basename(parent[1]).lower()
            if parent_name not in _PYTHON_WRAPPER_PROCESS_NAMES:
                return parent_name
            current_pid = parent_pid
        return None
    except Exception:
        return None


def _utf8_reader_requested() -> bool:
    """부모가 환경으로 UTF-8 stdio reader 계약을 명시했는지 반환한다."""
    io_encoding = os.environ.get("PYTHONIOENCODING", "").split(":", 1)[0].strip()
    if io_encoding:
        try:
            if codecs.lookup(io_encoding).name == "utf-8":
                return True
        except LookupError:
            pass
    return os.environ.get("PYTHONUTF8", "").strip() == "1"


def _powershell_capture_encoding() -> str | None:
    """비 UTF-8 콘솔을 읽는 PowerShell 부모이면 그 Python 코덱명을 반환한다."""
    state = _process_state()
    if _probe_os_name() != "nt":
        state["parent_name"] = None
        return None
    parent_name = _parent_process_name()
    state["parent_name"] = parent_name.lower() if parent_name else None
    original_output_cp = state["original_output_cp"]
    if (
        state["parent_name"] not in _POWERSHELL_PROCESS_NAMES
        or original_output_cp in (None, _UTF8_CODEPAGE)
        or _utf8_reader_requested()
    ):
        return None
    encoding = f"cp{original_output_cp}"
    try:
        codecs.lookup(encoding)
    except LookupError:
        return None
    return encoding


def _translit_error_handler(exc: UnicodeError) -> tuple[str, int]:
    """인코딩 불가 문자를 ASCII 대체표로 바꾸고 나머지만 ``?``로 표시한다."""
    if not isinstance(exc, UnicodeEncodeError):
        raise exc
    replacement = "".join(_TRANSLIT.get(char, "?") for char in exc.object[exc.start:exc.end])
    return replacement, exc.end


codecs.register_error("pm_translit", _translit_error_handler)


def _show_powershell_hint_once() -> None:
    """선택 코덱이 생긴 경우 PowerShell UTF-8 설정 안내를 프로세스당 한 번 출력한다."""
    state = _process_state()
    if state["hint_shown"] or os.environ.get("PM_CONSOLE_HINT") == "0":
        return
    state["hint_shown"] = True
    try:
        sys.stderr.write(_HINT_TEMPLATE.format(cp=state["original_output_cp"]) + "\n")
    except Exception:
        pass


def console_state() -> dict:
    """마지막 설정의 부모명·원 codepage와 스트림별 판정·적용 결과를 반환한다."""
    state = _process_state()
    streams = {
        name: dict(state["streams"][name])
        for name in ("stdout", "stderr")
    }
    return {
        "parent_name": state["parent_name"],
        "original_codepage": state["original_output_cp"],
        # 기존 stdout 중심 키는 호환을 위해 유지하되, applied 요약은 두 스트림이 모두
        # 성공한 경우에만 True다. 정확한 부분 적용 상태는 streams에서 확인한다.
        "selected_encoding": state["selected_encoding"],
        "isatty": state["isatty"],
        "applied": all(stream["applied"] for stream in streams.values()),
        "streams": streams,
    }


def _stream_isatty(stream: Any) -> bool:
    """스트림의 tty 여부를 보수적으로 읽는다. 미지원 캡처 스트림은 non-tty다."""
    try:
        return bool(stream.isatty())
    except Exception:
        return False


def configure_console_utf8() -> None:
    """codepage와 stdout/stderr를 현재 캡처 환경에 맞춘다 (best-effort)."""
    _set_console_codepage_utf8()
    capture_encoding = _powershell_capture_encoding()
    state = _process_state()
    stdout_isatty = _stream_isatty(sys.stdout)
    state["isatty"] = stdout_isatty
    state["selected_encoding"] = None
    state["applied"] = False
    state["streams"] = {
        "stdout": {
            "isatty": stdout_isatty,
            "selected_encoding": None,
            "applied": False,
        },
        "stderr": {
            "isatty": _stream_isatty(sys.stderr),
            "selected_encoding": None,
            "applied": False,
        },
    }
    capture_applied = False
    for name, stream in (("stdout", sys.stdout), ("stderr", sys.stderr)):
        stream_state = state["streams"][name]
        isatty = stream_state["isatty"]
        selected = capture_encoding if capture_encoding and not isatty else None
        encoding = selected or "utf-8"
        errors = "pm_translit" if selected else "replace"
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding=encoding, errors=errors)
                stream_state["applied"] = True
                if selected:
                    capture_applied = True
                    stream_state["selected_encoding"] = selected
                    if name == "stdout":
                        state["selected_encoding"] = selected
            except Exception:
                pass
    state["applied"] = all(
        stream_state["applied"] for stream_state in state["streams"].values()
    )
    if capture_applied:
        _show_powershell_hint_once()


def _flush_quietly(stream: Any) -> None:
    """스트림 flush 를 best-effort 로 시도한다 (닫힌/미지원 스트림도 출력을 막지 않는다)."""
    try:
        stream.flush()
    except Exception:
        pass


def write_machine_line(text: str, *, stream: Any = None) -> None:
    """기계 판독 한 줄을 콘솔 코덱 전환과 무관하게 UTF-8 로 내보낸다.

    ``configure_console_utf8`` 은 PowerShell 캡처(非tty)에서 텍스트 스트림을 콘솔 codepage
    (cp949 등)로 되돌리고 인코딩 불가 문자를 ``pm_translit`` 로 치환한다. 그 치환은 되돌릴 수
    없으므로 다른 프로세스가 파싱하는 출력(하네스 훅 엔벨로프·``--json`` 페이로드)은 텍스트
    레이어를 건너뛰고 UTF-8 bytes 를 직접 쓴다. 사람이 읽는 ``print`` 경로는 종전대로 둔다.

    ``.buffer`` 가 없는 스트림(테스트 캡처·``io.StringIO``)은 텍스트 write 로 폴백한다 —
    그 경로는 콘솔 코덱을 타지 않으므로 손실 표면이 아니다.
    """
    target = sys.stdout if stream is None else stream
    if target is None:
        # ``pythonw``/``pyw`` 기동은 표준 스트림이 없다 — 종전 ``print`` 처럼 무출력이 맞다
        # (여기서 AttributeError 로 죽으면 그 형상에서 CLI 전체가 실패한다).
        return
    line = text + "\n"
    buffer = getattr(target, "buffer", None)
    if buffer is None:
        target.write(line)
        _flush_quietly(target)
        return
    # 텍스트 레이어에 남아 있는 사람 출력이 bytes 뒤로 밀리지 않게 먼저 비운다(Windows 실측 축).
    _flush_quietly(target)
    buffer.write(line.encode("utf-8"))
    buffer.flush()
