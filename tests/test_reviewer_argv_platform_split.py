r"""reviewer_cmd argv 분해의 **실행 플랫폼 규칙**과 분해 규칙 사본 대조 (T-0722).

`additional_reviewer` 는 `reviewer_cmd`(local.conf 자유 문자열)를 argv 로 분해해 추가 리뷰어를 띄운다.
그 분해가 POSIX `shlex.split` 단독이면 Windows 실행 경로의 ``\`` 가 escape 로 소비돼
``C:\Users\pm\...\codex.exe`` 가 ``C:Userspm...codex.exe`` 가 되고, 실행은 `FileNotFoundError` 로
끝난다. 그 실패는 started=False 라 라운드가 환불되므로 **채택자에게는 아무 일도 없었던 것처럼**
보인다 — Windows 채택자는 교차검증이 한 번도 돌지 않은 상태로 게이트를 통과했다.

이 파일이 못 박는 것:

  1. 분해 규칙의 구현은 `board.py` 하나다(사본 0). 규칙 표식(`_SHELL_WINDOWS_PATH_SEPARATOR`)의
     소유 모듈을 값으로 확인하고, 다른 엔진 모듈에 사본이 생기면 red 다([[T-0712]] 의 `~user` 축과
     같은 대조).
  2. Windows 규칙은 실행 경로 구분자를 보존하고, POSIX 규칙은 종전 `shlex.split` 그대로다.
  3. `additional_reviewer` 는 그 공용 seam 을 **호출**한다(자기 사본 아님) — 로더를 갈아끼워 확인한다.
  4. Windows 경로를 담은 `reviewer_cmd` 가 러너까지 실행 가능한 argv 로 도착한다(POSIX 개발기에서
     플랫폼 seam 주입으로 태운다 · 주입이 실제로 걸렸는지 선-단언).
  5. 실행 파일 해소 실패는 loud 이고 "설정상 리뷰어 없음"과 **다른 진단**이다(무음 환불 0).
"""
from __future__ import annotations

import importlib.util
import os
import shlex
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

# 분해 규칙의 표식(Private Use Area 한 글자)과 그 규칙을 구현해도 되는 유일한 모듈.
# 소스 스캔은 두 표기를 함께 본다 — escape 로 적은 사본과 글자를 그대로 박은 사본 둘 다 걸린다.
WINDOWS_PATH_SEPARATOR_MARK = "\ue000"
WINDOWS_PATH_SEPARATOR_SOURCE = "\\ue000"
RULE_OWNER = "board.py"

# Windows 채택자 실측 형상 — 드라이브 문자 + 백슬래시 구분자. MAX_PATH(259) 를 넘기지 않는다.
WINDOWS_EXECUTABLE = r"C:\Users\pm\AppData\Local\Programs\Python\Python312\python.exe"
WINDOWS_REVIEWER_CMD = f"{WINDOWS_EXECUTABLE} -c pass"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def board():
    return _load("board")


@pytest.fixture
def external():
    return _load("additional_reviewer")


def _completed(rc: int, out: str = "판정: 통과") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["codex"], returncode=rc, stdout=out, stderr="")


# ── 1. 사본 대조 ────────────────────────────────────────────────────────────


def test_the_windows_argv_split_rule_has_exactly_one_implementation(board):
    """분해 규칙의 구현은 board 하나다 — 사본이 생기면 여기서 걸린다.

    소유 모듈에 그 규칙이 **있다는** 값 단언을 함께 둔다. 없으면 "어디에도 없다" 가 통과해 스캔이
    무의미해진다([[guard-must-cover-its-own-surface]]).
    """
    owners = set()
    for path in sorted(TOOLS.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        if (WINDOWS_PATH_SEPARATOR_MARK in source
                or WINDOWS_PATH_SEPARATOR_SOURCE in source):
            owners.add(path.name)

    assert RULE_OWNER in owners, "규칙 소유 모듈에서 보호 표식이 사라졌다(스캔 무효)"
    assert owners == {RULE_OWNER}, (
        f"Windows 경로 보호 분해 규칙 사본: {sorted(owners - {RULE_OWNER})} — "
        "board.split_command_argv 를 부르라")
    # 스캔이 본 소스 표기와 실행 값이 같은 글자임을 못 박는다(표기만 바꾼 사본 회피 방지).
    assert board._SHELL_WINDOWS_PATH_SEPARATOR == WINDOWS_PATH_SEPARATOR_MARK
    assert board.split_command_argv(WINDOWS_REVIEWER_CMD, windows=True)[0] == (
        WINDOWS_EXECUTABLE), "소유 모듈이 그 규칙을 실제로 구현하지 않는다"


def test_additional_reviewer_keeps_no_second_split_of_the_reviewer_command():
    """additional_reviewer 소스에 POSIX 전용 `shlex.split(reviewer_cmd)` 가 남아 있지 않다.

    한 자리만 고치고 나머지(진행신호·프로필 키·model 관측)를 남겨 두면 같은 커맨드가 표면마다
    다른 argv 로 읽힌다 — 그 비대칭이 Windows 에서 다시 무음 실패를 만든다.
    """
    source = (TOOLS / "additional_reviewer.py").read_text(encoding="utf-8")
    offenders = [
        line.strip() for line in source.splitlines()
        if "shlex.split(reviewer_cmd)" in line and not line.lstrip().startswith("#")
    ]
    assert offenders == [], offenders


def test_additional_reviewer_calls_the_shared_seam_instead_of_its_own_copy(external, monkeypatch):
    """분해는 board 공용 seam 을 **거쳐서만** 일어난다 — 로더를 갈아끼우면 결과가 따라 바뀐다."""
    class _StubBoard:
        seen: list[tuple[str, bool]] = []

        @staticmethod
        def split_command_argv(command, *, windows=None):
            _StubBoard.seen.append((command, windows))
            return ["stub-argv"]

    monkeypatch.setattr(external, "_load_board", lambda: _StubBoard)
    monkeypatch.setattr(external, "_running_on_windows", lambda: True)

    assert external._split_reviewer_argv("codex --model x") == ["stub-argv"]
    assert _StubBoard.seen == [("codex --model x", True)], (
        "분해가 board seam 을 타지 않았거나 플랫폼 판정을 넘기지 않았다")


def test_the_shared_seam_and_additional_reviewer_agree_token_for_token(board, external, monkeypatch):
    """같은 커맨드를 두 표면이 같은 argv 로 읽는다 (분해 결과 대조)."""
    commands = (
        WINDOWS_REVIEWER_CMD,
        r'"C:\Program Files\Codex\codex.exe" --model gpt-5.6',
        r"\\srv\share\codex.exe --model x",
        "codex --model gpt-5.6 exec",
    )
    for windows in (True, False):
        monkeypatch.setattr(external, "_running_on_windows", lambda: windows)
        for command in commands:
            assert external._split_reviewer_argv(command) == board.split_command_argv(
                command, windows=windows), (command, windows)


# ── 2. 플랫폼 규칙 ──────────────────────────────────────────────────────────


def test_windows_rule_preserves_executable_path_separators(board):
    """Windows 규칙은 드라이브/UNC 절대경로의 구분자를 보존한다 (인용 유무 무관)."""
    assert board.split_command_argv(WINDOWS_REVIEWER_CMD, windows=True) == [
        WINDOWS_EXECUTABLE, "-c", "pass"]
    assert board.split_command_argv(
        r'"C:\Program Files\Codex\codex.exe" --model gpt-5.6', windows=True) == [
        r"C:\Program Files\Codex\codex.exe", "--model", "gpt-5.6"]
    assert board.split_command_argv(r"\\srv\share\codex.exe -q", windows=True) == [
        r"\\srv\share\codex.exe", "-q"]
    assert WINDOWS_PATH_SEPARATOR_MARK not in "".join(
        board.split_command_argv(WINDOWS_REVIEWER_CMD, windows=True)), (
        "보호 표식이 argv 에 남으면 그 자체가 실행 불가 경로다")


def test_posix_rule_is_unchanged_shlex_split(board):
    """POSIX 실행 플랫폼에서는 종전 `shlex.split` 그대로다 — escape 의미를 바꾸지 않는다."""
    for command in ("/usr/bin/codex --model x", r"codex --sandbox 'a b' --flag x\ y",
                    WINDOWS_REVIEWER_CMD):
        assert board.split_command_argv(command, windows=False) == shlex.split(command)


def test_the_posix_rule_really_mangles_the_windows_path(board):
    """두 규칙이 **다른 결과**를 낸다는 값 단언 — 이 축이 실재함을 픽스처로 고정한다.

    이게 참이 아니게 되면(예: 픽스처에서 백슬래시가 사라지면) 위 Windows 단언은 아무것도 검증하지
    않는다([[guard-must-cover-its-own-surface]]).
    """
    mangled = board.split_command_argv(WINDOWS_REVIEWER_CMD, windows=False)
    assert mangled[0] != WINDOWS_EXECUTABLE
    assert "\\" not in mangled[0] and "/" not in mangled[0]


def test_platform_default_follows_the_running_platform(board, monkeypatch):
    """`windows=None` 은 실행 플랫폼(`os.name`)을 본다 — 주입 인자는 그 판정을 대체할 뿐이다."""
    monkeypatch.setattr(board, "_probe_os_name", lambda: "nt")
    assert board.split_command_argv(WINDOWS_REVIEWER_CMD)[0] == WINDOWS_EXECUTABLE
    monkeypatch.setattr(board, "_probe_os_name", lambda: "posix")
    assert board.split_command_argv(WINDOWS_REVIEWER_CMD)[0] != WINDOWS_EXECUTABLE


def test_probe_os_name_seam_defaults_to_the_real_interpreter_value(board):
    """기본 프로브는 전역 `os.name` 그대로다 — seam 추가가 동작을 바꾸지 않는다(T-0741)."""
    assert board._probe_os_name() == os.name


# ── 3. 러너까지 도달하는 argv ───────────────────────────────────────────────


def test_windows_reviewer_command_reaches_the_runner_as_an_executable_argv(
        external, monkeypatch):
    """Windows 경로 `reviewer_cmd` 가 러너에 실행 가능한 argv 로 도착한다 (수정 전 red).

    수정 전에는 argv[0] 이 ``C:Userspm...python.exe`` 로 뭉개져 `FileNotFoundError` →
    (ok, started) == (False, False) 였다.
    """
    # 선-단언 ①: 이 픽스처가 실제로 축을 태운다 — POSIX 규칙이면 argv[0] 이 뭉개진다.
    assert shlex.split(WINDOWS_REVIEWER_CMD)[0] != WINDOWS_EXECUTABLE
    monkeypatch.setattr(external, "_running_on_windows", lambda: True)
    # 선-단언 ②: 플랫폼 주입이 실제로 걸렸다(주입이 no-op 이면 가드가 시험되지 않는다).
    assert external._running_on_windows() is True

    seen: dict[str, list[str]] = {}

    def runner(argv, **kwargs):
        seen["argv"] = list(argv)
        return _completed(0)

    ok, _output, started = external._run_reviewer_ex(
        "prompt", WINDOWS_REVIEWER_CMD, 5, runner)

    assert (ok, started) == (True, True)
    assert seen["argv"] == [WINDOWS_EXECUTABLE, "-c", "pass"]


def test_real_subprocess_run_still_works_under_the_windows_rule(external, monkeypatch):
    """Windows 규칙을 켠 채 실 인터프리터를 태운다 — 규칙이 POSIX 경로를 깨지 않는다.

    실행 플랫폼과 무관하게 `_run_reviewer_ex` → `subprocess.run` 경로가 살아 있음을 실 프로세스로
    확인한다(POSIX 개발기에서 Windows 분해를 태우는 축과 같은 자리).

    커맨드는 엔진이 실제로 만드는 형상(`shlex.join`)으로 조립한다. 실 Windows 인터프리터 경로는
    `C:\\Program Files\\Python312\\python.exe` 처럼 공백을 포함하므로, 인용 없이 조립하면 규칙과
    무관하게 실행 파일이 두 토큰으로 갈린다. 인용된 형상이 Windows 규칙을 통과해도 공백을 유지하는지가
    이 회귀가 지켜야 할 값이다.
    """
    import sys

    monkeypatch.setattr(external, "_running_on_windows", lambda: True)
    command = shlex.join([sys.executable, "-c", "pass"])
    assert external._split_reviewer_argv(command)[0] == sys.executable, (
        "Windows 규칙이 인용된 인터프리터 경로를 단일 토큰으로 복원하지 못했다")
    ok, _output, started = external._run_reviewer_ex("prompt", command, 5, subprocess.run)
    assert (ok, started) == (True, True)


def test_the_watchdog_join_and_split_round_trip_keeps_the_executable(external, monkeypatch):
    """기본 러너는 argv 를 다시 `shlex.join` 해 설정을 해소한다 — 그 왕복이 경로를 잃지 않는다.

    왕복이 깨지면 Windows 에서 프로필 키가 뭉개진 실행 파일 이름이 되어, 진행신호 계약과 무진행
    상한이 조용히 "미지 CLI" 로 내려앉는다(같은 결함의 두 번째 표면).
    """
    monkeypatch.setattr(external, "_running_on_windows", lambda: True)
    argv = [r"C:\Users\pm\AppData\Local\Programs\codex\codex.exe", "exec", "--model", "gpt-5.6"]
    joined = shlex.join(argv)

    assert external._split_reviewer_argv(joined) == argv
    assert external.reviewer_name(joined) == "codex"        # 프로필 키가 살아난다


# ── 4. 진단 분리 (무음 환불 0) ──────────────────────────────────────────────


def test_unresolved_executable_is_loud_and_distinct_from_no_reviewer_configured(
        external, monkeypatch):
    """실행 파일 해소 실패와 "설정상 리뷰어 없음" 은 서로 다른 진단이다.

    한 문구로 합치면 "리뷰어를 안 깔았나 보다"로 흡수돼, 교차검증이 돌지 않은 실행이 정상 형상과
    같은 모양이 된다(30여 릴리즈 은닉된 경로).
    """
    monkeypatch.setattr(external, "_running_on_windows", lambda: True)

    def missing(argv, **kwargs):
        raise FileNotFoundError(argv[0])

    ok, output, started = external._run_reviewer_ex(
        "prompt", WINDOWS_REVIEWER_CMD, 5, missing)
    assert (ok, started) == (False, False)
    assert external.REVIEWER_LAUNCH_FAILURE_MARKER in output.answer
    assert external.NO_REVIEWER_CONFIGURED_MARKER not in output.answer
    assert "교차검증은 실행되지 않았습니다" in output.answer
    assert WINDOWS_EXECUTABLE in output.answer          # 무엇으로 띄우려 했는지

    ok, output, started = external._run_reviewer_ex("prompt", "   ", 5, missing)
    assert (ok, started) == (False, False)
    assert external.NO_REVIEWER_CONFIGURED_MARKER in output.answer
    assert external.REVIEWER_LAUNCH_FAILURE_MARKER not in output.answer


def test_a_mangled_executable_names_the_declared_command(external, monkeypatch):
    """선언 커맨드에 없는 실행 파일로 분해되면 진단이 그 대조를 낸다 (분해·인용 사고 식별).

    "설치 안 됨"과 "엔진이 커맨드를 잘못 분해함"을 사람이 그 자리에서 가를 수 있는 유일한 사실이다.
    """
    monkeypatch.setattr(external, "_running_on_windows", lambda: False)  # POSIX 규칙 = 훼손

    def missing(argv, **kwargs):
        raise FileNotFoundError(argv[0])

    _ok, output, _started = external._run_reviewer_ex(
        "prompt", WINDOWS_REVIEWER_CMD, 5, missing)
    assert "해소된 커맨드에 없는 실행 파일로 분해됐습니다" in output.answer
    assert WINDOWS_REVIEWER_CMD in output.answer

    # PATH 에 없는 평범한 커맨드는 그 대조를 붙이지 않는다 (선언과 시도가 같다).
    _ok, output, _started = external._run_reviewer_ex("prompt", "codex --model x", 5, missing)
    assert external.REVIEWER_LAUNCH_FAILURE_MARKER in output.answer
    assert "해소된 커맨드에 없는" not in output.answer
