"""셸 명령 판정 규칙 (층: domain).

셸 문자열 해석(주석·heredoc·줄 이음·분리자·리다이렉션), simple command 해석, 파일 삭제 형태,
엔진·pytest 호출 모양, Windows 경로 구분자 보호. 판정 흐름(`judge_*`)은 board 가 소유한다.
"""
from __future__ import annotations

import fnmatch
import re
import shlex
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import NamedTuple

from pm_engine.domain import git_policy


# 인자 문자열을 셸·git 명령으로 다시 실행하는 명령. 그 인자 속 git 은 파서가 호출 좌표를 풀 수 없다.
_SHELL_INDIRECT_EXECUTORS: frozenset[str] = frozenset({
    "sh", "bash", "zsh", "dash", "ksh", "fish", "eval", "xargs", "find", "alias", "watch",
    "parallel",
})

# 인라인 코드 옵션(``-c``/``-e`` 등)이 있을 때만 인자를 코드로 실행하는 인터프리터.
_SHELL_INLINE_CODE_INTERPRETERS = re.compile(r"(?:python(?:\d+(?:\.\d+)*)?|py|node|perl|ruby)")

_SHELL_INLINE_CODE_OPTION = re.compile(r"-[A-Za-z]*[ceE]")

_SHELL_COMMAND_WRAPPERS: frozenset[str] = frozenset({
    "env", "command", "exec", "nohup", "nice", "timeout", "sudo",
})

_SHELL_ASSIGNMENT_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.DOTALL)


def _shell_command_word_index(values: Sequence[str]) -> int:
    """앞 할당·실행 wrapper·그 옵션을 걷어 command word 위치를 찾는다(값 소비 옵션 뒤 값은 거기서 멈춘다)."""
    index = 0
    while index < len(values) and (
        _SHELL_ASSIGNMENT_WORD.fullmatch(values[index])
        or Path(values[index]).name in _SHELL_COMMAND_WRAPPERS
        or values[index].startswith("-") or values[index].isdigit()
    ):
        index += 1
    return index


def _shell_indirect_executor(values: Sequence[str], index: int) -> tuple[str, list[str]] | None:
    """command word 가 인자를 셸·코드로 다시 실행하는 명령이면 (이름, 인자), 아니면 None."""
    name = Path(values[index]).name
    arguments = list(values[index + 1:])
    inline_code = bool(_SHELL_INLINE_CODE_INTERPRETERS.fullmatch(name)) and any(
        _SHELL_INLINE_CODE_OPTION.fullmatch(value) for value in arguments
    )
    if name not in _SHELL_INDIRECT_EXECUTORS and not inline_code:
        return None
    return name, arguments


# ── git 이 아닌 파일 명령의 슬롯·공용 저장소 삭제·이동 ─────────────────────────────────────
# 손 git 거부를 파일 명령(rm·mv·find -delete·rsync --delete)이나 인터프리터 코드의 삭제 API 로
# 돌아가지 못하게 같은 판정 자리에서 본다. 보호 대상은 PM 홈의 슬롯 폴더(`work/<slot>`)·그 git
# 연결(`<slot>/.git`)·공용 저장소(`.repos/`)다. 슬롯 안의 일반 파일은 보호 대상이 아니다.
# 경로 자체를 지우는 명령 — 대상 경로를 정적으로 못 풀면 파괴 형태로 보고 거부한다.
_PATH_REMOVAL_COMMANDS: frozenset[str] = frozenset({"rm", "rmdir", "unlink", "shred"})

_FILE_REMOVAL_COMMANDS: frozenset[str] = _PATH_REMOVAL_COMMANDS | {"mv", "find", "rsync"}

# 인터프리터 코드 속 파일 삭제·이동 API(마지막 점 뒤 이름).
_FILE_REMOVAL_API = re.compile(
    r"(?:^|\.)(rmtree|remove|removedirs|unlink|rmdir|rmSync|rmdirSync|unlinkSync|rm_rf|rm_r"
    r"|rename|replace|move|renameSync)$"
)

_FILE_MOVE_API_NAMES: frozenset[str] = frozenset({"rename", "replace", "move", "renameSync"})

# 간접 실행 문자열의 낱말 — 따옴표·여는 괄호·쉼표는 구분자이고, 명령 경계 문자는 따로 낸다.
_INDIRECT_WORD = re.compile(r"[;&|)\]}\n]|[^\s;&|()\[\]{},'\"`]+")

_INDIRECT_BOUNDARY: frozenset[str] = frozenset({";", "&", "|", ")", "]", "}", "\n"})

_INDIRECT_CWD_CHANGE = re.compile(r"(?<![A-Za-z0-9_])(?:cd|pushd|popd|chdir)(?![A-Za-z0-9_])")

# xargs 가 입력으로 붙이는 대상 자리표시 — `$` 가 들어 있어 정적으로 풀 수 없는 경로가 된다.
_XARGS_OPERAND_PLACEHOLDER = "$XARGS"

_MV_VALUE_OPTIONS: frozenset[str] = frozenset({"-t", "--target-directory", "-S", "--suffix"})

_FIND_EXEC_ACTIONS: frozenset[str] = frozenset({"-exec", "-execdir", "-ok", "-okdir"})

_FIND_VALUE_OPTIONS: frozenset[str] = frozenset({"-maxdepth", "-mindepth", "-newer", "-type",
                                                 "-mtime", "-mmin", "-size", "-user", "-group",
                                                 "-perm"})

# 이름·경로로 대상을 좁히는 find 식. 이 식이 있으면 그 패턴이 .git·.repos 에 걸릴 때만 본다.
_FIND_NAME_PREDICATES: frozenset[str] = frozenset({
    "-name", "-iname", "-path", "-ipath", "-wholename", "-iwholename", "-regex", "-iregex",
})

_RSYNC_VALUE_OPTIONS: frozenset[str] = frozenset({
    "-e", "--rsh", "--exclude", "--include", "--filter", "-f", "--exclude-from",
    "--include-from", "--files-from", "--backup-dir", "--suffix", "--chmod", "--rsync-path",
    "--log-file", "--partial-dir", "--temp-dir", "-T", "--compare-dest", "--copy-dest",
    "--link-dest", "--password-file", "--port", "--timeout", "--bwlimit", "--max-size",
    "--min-size", "--out-format", "--info", "--debug", "-M", "--remote-option",
})

_RSYNC_REMOTE_PATH = re.compile(r"^(?:rsync://|[^/]+:)")

# 보호 대상 종류와 그 거부 문구·경우.
_PROTECTED_SLOT = "slot"

_PROTECTED_SLOT_GIT = "slot-git"

_PROTECTED_SHARED = "shared"

# 그 안의 항목을 건드려도 보호 대상이 망가지는 종류(슬롯 폴더 안 일반 파일은 정상 작업이다).
_PROTECTED_INTERNAL_KINDS: frozenset[str] = frozenset({_PROTECTED_SLOT_GIT, _PROTECTED_SHARED})

_PROTECTED_KIND_ORDER: tuple[str, ...] = (_PROTECTED_SLOT, _PROTECTED_SLOT_GIT, _PROTECTED_SHARED)

_PROTECTED_KIND_FORMS: dict[str, tuple[str, str]] = {
    _PROTECTED_SLOT: ("슬롯 폴더", git_policy._GIT_GUARD_CASE_SLOT_REMOVE),
    _PROTECTED_SLOT_GIT: ("슬롯의 git 연결(.git)", git_policy._GIT_GUARD_CASE_ASK_USER),
    _PROTECTED_SHARED: ("공용 저장소(.repos)", git_policy._GIT_GUARD_CASE_ASK_USER),
}


class _RemovalTarget(NamedTuple):
    token: str
    # self=경로 자체를 지우거나 옮김 · contents=그 아래 항목을 지움 · into=그 안에 써 넣음(mv 목적지)
    scope: str


class _RemovalShape(NamedTuple):
    verb: str
    targets: tuple[_RemovalTarget, ...]
    # 대상 경로를 정적으로 못 풀 때 파괴 형태로 거부할지(삭제) · 판정하지 않을지(mv).
    unresolved_is_destructive: bool


def _removal_arguments(
    args: Sequence[str], value_options: frozenset[str],
) -> tuple[list[str], dict[str, str | None]]:
    """(operand 목록, {옵션 이름: 값}) — ``--`` 뒤는 전부 operand 다."""
    operands: list[str] = []
    options: dict[str, str | None] = {}
    options_open = True
    index = 0
    while index < len(args):
        token = args[index]
        if options_open and token == "--":
            options_open = False
        elif options_open and token.startswith("-") and token != "-":
            name, separator, value = token.partition("=")
            if not separator and name in value_options and index + 1 < len(args):
                index += 1
                options[name] = args[index]
            else:
                options[name] = value if separator else None
        else:
            operands.append(token)
        index += 1
    return operands, options


def _find_pattern_may_select_protected(pattern: str) -> bool:
    return any(
        name in pattern or fnmatch.fnmatchcase(name, pattern)
        or fnmatch.fnmatchcase(f"x/{name}", pattern)
        for name in (".git", ".repos")
    )


def _find_removal_shape(args: Sequence[str]) -> _RemovalShape | None:
    index = 0
    while index < len(args) and (args[index] in {"-H", "-L", "-P"} or args[index].startswith("-O")):
        index += 1
    if index < len(args) and args[index] == "-D":
        index += 2
    starts: list[str] = []
    while index < len(args) and not args[index].startswith("-") and args[index] not in {
        "(", "!", ")", ",",
    }:
        starts.append(args[index])
        index += 1
    expression = list(args[index:])
    destructive = False
    patterns: list[str] = []
    position = 0
    while position < len(expression):
        token = expression[position]
        if token in _FIND_EXEC_ACTIONS:
            command = expression[position + 1] if position + 1 < len(expression) else ""
            destructive = destructive or Path(command).name in _PATH_REMOVAL_COMMANDS | {"mv"}
            while position < len(expression) and expression[position] not in {";", "+"}:
                position += 1
        elif token == "-delete":
            destructive = True
        elif token in _FIND_NAME_PREDICATES and position + 1 < len(expression):
            position += 1
            patterns.append(expression[position])
        elif token in _FIND_VALUE_OPTIONS:
            position += 1
        position += 1
    if not destructive:
        return None
    # 이름·경로로 좁힌 삭제는 그 패턴이 .git·.repos 를 고를 수 있을 때만 보호 대상을 건드린다.
    if patterns and not any(_find_pattern_may_select_protected(value) for value in patterns):
        return None
    return _RemovalShape(
        "일괄 삭제", tuple(_RemovalTarget(token, "contents") for token in (starts or ["."])), True,
    )


def _rsync_removal_shape(args: Sequence[str]) -> _RemovalShape | None:
    operands, options = _removal_arguments(args, _RSYNC_VALUE_OPTIONS)
    delete = any(name.startswith("--delete") or name == "--del" for name in options)
    remove_sources = "--remove-source-files" in options
    if len(operands) < 2 or not (delete or remove_sources):
        return None
    local = [not _RSYNC_REMOTE_PATH.match(token) for token in operands]
    targets: list[_RemovalTarget] = []
    if delete and local[-1]:
        targets.append(_RemovalTarget(operands[-1], "contents"))
    if remove_sources:
        targets.extend(
            _RemovalTarget(token, "contents")
            for token, is_local in zip(operands[:-1], local[:-1]) if is_local
        )
    return _RemovalShape("동기화 삭제", tuple(targets), True) if targets else None


def _file_removal_shape(argv: Sequence[str]) -> _RemovalShape | None:
    """파일 명령 argv 가 경로를 지우거나 옮기는 형태면 그 대상, 아니면 None."""
    if not argv:
        return None
    name = Path(argv[0]).name
    args = list(argv[1:])
    if name in _PATH_REMOVAL_COMMANDS:
        operands, _options = _removal_arguments(args, frozenset())
        if not operands:
            return None
        return _RemovalShape("지우기", tuple(_RemovalTarget(token, "self") for token in operands), True)
    if name == "mv":
        operands, options = _removal_arguments(args, _MV_VALUE_OPTIONS)
        destination = options.get("-t") or options.get("--target-directory")
        if destination is None and len(operands) > 1:
            destination = operands.pop()
        targets = [_RemovalTarget(token, "self") for token in operands]
        if destination is not None:
            targets.append(_RemovalTarget(destination, "into"))
        return _RemovalShape("옮기기", tuple(targets), False) if targets else None
    if name == "find":
        return _find_removal_shape(args)
    if name == "rsync":
        return _rsync_removal_shape(args)
    return None


def _protected_removal_kind(
    target: Path, scope: str, entries: Sequence[tuple[Path, str]],
) -> str | None:
    kinds: set[str] = set()
    for path, kind in entries:
        inside_internal = kind in _PROTECTED_INTERNAL_KINDS and target.is_relative_to(path)
        if scope == "self":
            hit = target == path or path.is_relative_to(target) or inside_internal
        elif scope == "contents":
            hit = (path != target and path.is_relative_to(target)) or inside_internal
        else:
            hit = inside_internal
        if hit:
            kinds.add(kind)
    return next((kind for kind in _PROTECTED_KIND_ORDER if kind in kinds), None)


def _shell_file_removal_argv(command: Sequence[str]) -> tuple[str, ...] | None:
    """simple command 가 (실행 wrapper 뒤의) 파일 삭제·이동 명령이면 그 argv."""
    values = list(command)
    start = 0
    while start < len(values) and _SHELL_ASSIGNMENT_WORD.fullmatch(values[start]):
        start += 1
    if start >= len(values):
        return None
    if Path(values[start]).name in _SHELL_COMMAND_WRAPPERS:
        # wrapper 옵션 문법은 넓다 — 뒤에서 처음 나오는 파일 명령을 실행 대상으로 본다.
        for candidate in range(start + 1, len(values)):
            if Path(values[candidate]).name in _FILE_REMOVAL_COMMANDS:
                return tuple(values[candidate:])
        return None
    return tuple(values[start:]) if Path(values[start]).name in _FILE_REMOVAL_COMMANDS else None


def _indirect_file_removal_argvs(text: str, *, stdin_operands: bool) -> list[tuple[str, ...]]:
    """셸·인터프리터 코드 문자열 속 파일 삭제·이동 호출 argv(삭제 API 는 rm/mv 형태로 옮긴다)."""
    words = [_normalize_shell_path_notation(word) for word in _INDIRECT_WORD.findall(text)]
    argvs: list[tuple[str, ...]] = []
    for index, word in enumerate(words):
        if word in _INDIRECT_BOUNDARY:
            continue
        end = index + 1
        while end < len(words) and words[end] not in _INDIRECT_BOUNDARY:
            end += 1
        rest = tuple(words[index + 1:end])
        name = Path(word).name
        if name in _FILE_REMOVAL_COMMANDS:
            extra = (_XARGS_OPERAND_PLACEHOLDER,) if (
                stdin_operands and name in _PATH_REMOVAL_COMMANDS
            ) else ()
            argvs.append((word, *rest, *extra))
            continue
        api = _FILE_REMOVAL_API.search(word)
        if api is not None:
            argvs.append(("mv" if api.group(1) in _FILE_MOVE_API_NAMES else "rm", "--", *rest))
    return argvs


def _shell_indirect_file_removal_argvs(command: Sequence[str]) -> tuple[list[tuple[str, ...]], bool]:
    """간접 실행 명령 인자 속 파일 삭제·이동 argv 와, 그 코드가 실행 폴더를 바꾸는지."""
    values = list(command)
    index = _shell_command_word_index(values)
    if index >= len(values):
        return [], False
    executor = _shell_indirect_executor(values, index)
    if executor is None:
        return [], False
    name, arguments = executor
    text = " ".join(arguments)
    return (
        _indirect_file_removal_argvs(text, stdin_operands=name == "xargs"),
        _INDIRECT_CWD_CHANGE.search(text) is not None,
    )


class _EngineInvocation(NamedTuple):
    kind: str
    script: str | None
    args: tuple[str, ...]
    # 따옴표로 묶인 문자열 안에서 찾은 호출이다. 그 문자열을 무엇이 실행하는지는 정적으로 모른다.
    quoted: bool


def _engine_marker(value: str) -> str | None:
    """낱말이 엔진 스크립트 경로면 ``engine``, pytest 실행 파일이면 ``pytest``, 아니면 None."""
    path = Path(value)
    if path.suffix == ".py" and path.parts[-3:-1] == (".project_manager", "tools"):
        return "engine"
    if path.name.lower() in {"pytest", "pytest.exe"}:
        return "pytest"
    return None


def _engine_invocation_details(
    _cwd: str | Path, argv: Sequence[str],
) -> tuple[_EngineInvocation, ...]:
    """argv 의 엔진·pytest 표지 낱말마다 호출 하나를 내고, 그 표지 뒤 낱말을 호출 인자로 본다.

    표지는 ``.project_manager/tools/<이름>.py`` 경로 낱말과 ``pytest`` 낱말(``python -m pytest`` 의
    ``pytest`` 포함)이다. 표지 앞에 붙은 인터프리터·옵션·감싸는 명령은 보지 않는다. 앞 낱말이 표지로
    읽혀도 뒤의 실제 호출이 가려지지 않도록 첫 표지에서 멈추지 않는다. 표지가 아닌 낱말이 간접 실행
    문자열의 낱말 규칙으로 여러 낱말이 되면(따옴표로 묶인 명령 글 등) 그 안의 표지도 모두 찾고
    ``quoted`` 로 표시한다.
    """
    values = [str(value) for value in argv]
    found: list[_EngineInvocation] = []
    for index, value in enumerate(values):
        kind = _engine_marker(value)
        if kind is not None:
            found.append(_EngineInvocation(
                kind, value if kind == "engine" else None, tuple(values[index + 1:]), False,
            ))
            continue
        words = _INDIRECT_WORD.findall(value)
        if len(words) > 1:
            found.extend(
                inner._replace(quoted=True) for inner in _engine_invocation_details(_cwd, words)
            )
    return tuple(found)


def _pytest_targets_tests(args: Sequence[str]) -> bool:
    """pytest argv가 상대 ``tests/`` tree를 명시하는지 판정한다."""
    for raw in args:
        if not raw or raw.startswith("-"):
            continue
        normalized = raw.replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        if normalized.rstrip("/") == "tests" or normalized.startswith("tests/"):
            return True
    return False


def _unexpanded_shell_parameters(value: str) -> tuple[str, ...]:
    """경로 token에 셸이 아직 확장하지 않은 parameter 표기를 찾는다."""
    pattern = re.compile(
        r"\$(?:\{[^}\r\n]+\}|[A-Za-z_][A-Za-z0-9_]*|[0-9]+|[@*#?$!_-])"
    )
    return tuple(dict.fromkeys(match.group(0) for match in pattern.finditer(value)))


class _ShellGitInvocation(NamedTuple):
    cwd: str
    argv: tuple[str, ...]
    certain: bool
    # 실행 위치(cwd)만의 확실성. ``certain`` 은 여기에 셸 확장(``$``·backtick)까지 없어야 참이다.
    # 파괴 형태 판정은 대상 위치만 필요하므로 이 값을 쓴다(인자 속 변수는 형태 판정이 따로 본다).
    target_repo_certain: bool


class _ShellParseResult(NamedTuple):
    invocations: tuple[_ShellGitInvocation, ...]
    engine_invocations: tuple[_ShellGitInvocation, ...]
    # 파일 삭제·이동 명령(직접·간접 실행) 호출. ``target_repo_certain`` 은 상대 경로를 풀 실행 위치의 확실성이다.
    removal_invocations: tuple[_ShellGitInvocation, ...]
    persistent_cd: bool
    uncertain: bool
    reason: str


def _merge_shell_readings(readings: Sequence[_ShellParseResult]) -> _ShellParseResult:
    """같은 명령을 여러 셸 문법으로 읽은 결과를 합친다. 첫 읽기는 그대로 두고 뒤 읽기에서 새로 나온 호출만 잇는다.

    판정은 합친 호출 모두에서 가장 엄한 것이 이긴다. ``cd`` 남음·구문 불확실은 한 읽기라도 참이면 참이다.
    읽기가 하나면 결과는 그 읽기와 같다."""
    first, later = readings[0], readings[1:]

    def calls(field: str) -> tuple[_ShellGitInvocation, ...]:
        kept = getattr(first, field)
        return kept + tuple(dict.fromkeys(
            call for reading in later for call in getattr(reading, field) if call not in kept))

    return _ShellParseResult(
        calls("invocations"), calls("engine_invocations"), calls("removal_invocations"),
        any(reading.persistent_cd for reading in readings),
        any(reading.uncertain for reading in readings),
        "; ".join(dict.fromkeys(reading.reason for reading in readings if reading.reason)),
    )


class _ShellGitCommand(NamedTuple):
    """wrapper를 걷어낸 git command word와 그 해석 확실성."""

    index: int
    certain: bool
    target_repo_env_override: bool


def _shell_git_command(segment: Sequence[str]) -> _ShellGitCommand | None:
    """simple-command의 command position을 해소한다.

    quote fragment는 앞선 POSIX shlex가 이미 한 word로 결합한다. ``env``/``command``의
    지원 grammar는 정확히 소비하고, 미지원 option 뒤의 git 후보는 실행 여부가 불명확하므로
    ``certain=False``로 surface한다. 임의 일반 argv 안의 git은 여전히 데이터다.
    """
    values = list(segment)

    def _assignment(value: str) -> bool:
        return re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*=.*", value, flags=re.DOTALL,
        ) is not None

    def _is_git(value: str) -> bool:
        return Path(value).name == "git"

    def _uncertain_git(start: int, override: bool) -> _ShellGitCommand | None:
        for candidate in range(start, len(values)):
            if _is_git(values[candidate]):
                return _ShellGitCommand(candidate, False, override)
        return None

    index = 0
    target_repo_env_override = False
    # POSIX assignment word는 command name 앞에 올 수 있다. 이후 임의 argv의 ``git``은
    # echo/printf 데이터일 뿐이므로 절대 실행으로 승격하지 않는다.
    while index < len(values) and _assignment(values[index]):
        target_repo_env_override = target_repo_env_override or values[index].startswith(
            ("GIT_WORK_TREE=", "GIT_DIR=")
        )
        index += 1

    # 실행 wrapper는 중첩 가능하다(``exec env -i command git …``). 각 grammar가 다음
    # command word까지 index를 전진시키고 같은 loop에서 다시 해소한다.
    while index < len(values):
        if _is_git(values[index]):
            return _ShellGitCommand(index, True, target_repo_env_override)
        wrapper = Path(values[index]).name

        if wrapper == "env":
            index += 1
            option_phase = True
            while index < len(values):
                token = values[index]
                if option_phase and token == "--":
                    option_phase = False
                    index += 1
                    continue
                if _assignment(token):
                    target_repo_env_override = target_repo_env_override or token.startswith(
                        ("GIT_WORK_TREE=", "GIT_DIR=")
                    )
                    index += 1
                    continue
                if option_phase and token in {
                    "-i", "--ignore-environment", "-0", "--null", "--debug",
                }:
                    index += 1
                    continue
                if option_phase and token in {
                    "-u", "--unset", "-C", "--chdir", "-a", "--argv0",
                }:
                    if index + 1 >= len(values):
                        return None
                    # child cwd 변경은 현재 대상 저장소와 같다고 증명할 수 없다.
                    if token in {"-C", "--chdir"}:
                        return _uncertain_git(index + 2, target_repo_env_override)
                    index += 2
                    continue
                if option_phase and token.startswith(("--unset=", "--chdir=", "--argv0=")):
                    if token.startswith("--chdir="):
                        return _uncertain_git(index + 1, target_repo_env_override)
                    index += 1
                    continue
                if option_phase and token.startswith("-"):
                    return _uncertain_git(index + 1, target_repo_env_override)
                break
            continue

        if wrapper == "command":
            index += 1
            while index < len(values):
                token = values[index]
                if token in {"--", "-p"}:
                    index += 1
                    continue
                if token in {"-v", "-V"}:
                    return None
                if token.startswith("-"):
                    return _uncertain_git(index + 1, target_repo_env_override)
                break
            continue

        if wrapper == "exec":
            index += 1
            while index < len(values):
                token = values[index]
                if token in {"--", "-c", "-l"}:
                    index += 1
                    continue
                if token == "-a":
                    if index + 1 >= len(values):
                        return None
                    index += 2
                    continue
                if token.startswith("-"):
                    return _uncertain_git(index + 1, target_repo_env_override)
                break
            continue

        if wrapper == "nohup":
            index += 1
            if index < len(values) and values[index] == "--":
                index += 1
            elif index < len(values) and values[index].startswith("-"):
                return _uncertain_git(index + 1, target_repo_env_override)
            continue

        if wrapper == "nice":
            index += 1
            while index < len(values):
                token = values[index]
                if token == "--":
                    index += 1
                    break
                if token in {"-n", "--adjustment"}:
                    if index + 1 >= len(values):
                        return None
                    index += 2
                    continue
                if token.startswith("--adjustment=") or re.fullmatch(r"-\d+", token):
                    index += 1
                    continue
                if token.startswith("-"):
                    return _uncertain_git(index + 1, target_repo_env_override)
                break
            continue

        if wrapper == "timeout":
            index += 1
            while index < len(values):
                token = values[index]
                if token == "--":
                    index += 1
                    break
                if token in {"-k", "--kill-after", "-s", "--signal"}:
                    if index + 1 >= len(values):
                        return None
                    index += 2
                    continue
                if token in {"--foreground", "--preserve-status", "--verbose"}:
                    index += 1
                    continue
                if token.startswith(("--kill-after=", "--signal=")):
                    index += 1
                    continue
                if token.startswith("-"):
                    return _uncertain_git(index + 1, target_repo_env_override)
                break
            if index >= len(values):
                return None
            index += 1  # DURATION
            continue

        if wrapper == "sudo":
            index += 1
            if index < len(values) and values[index] == "--":
                index += 1
            elif index < len(values) and values[index].startswith("-"):
                # sudo option grammar는 플랫폼별로 넓고 option value가 command처럼 보일 수 있다.
                return _uncertain_git(index + 1, target_repo_env_override)
            while index < len(values) and _assignment(values[index]):
                target_repo_env_override = target_repo_env_override or values[index].startswith(
                    ("GIT_WORK_TREE=", "GIT_DIR=")
                )
                index += 1
            continue

        if any(marker in values[index] for marker in ("$", "`")):
            # command word 자체가 parameter/command expansion이면 런타임에 exec류 wrapper가
            # 될 수 있다. 뒤의 git은 argv 데이터일 수도 있어 deny할 수 없지만, mutation 후보를
            # ok로 숨기지도 않고 uncertainty로 surface한다. echo/printf 등 명백한 command의
            # 일반 argv는 이 분기에 오지 않는다.
            return _uncertain_git(index + 1, target_repo_env_override)

        return None
    return None


def _normalize_shell_line_continuations(command: str, rule: "_ShellSyntax") -> str:
    """quote/heredoc data 밖의 escape 글자-newline 을 그 셸의 줄 이음으로 합친다.

    escape 글자는 문법마다 다르다(bash ``\\``·PowerShell 백틱·cmd ``^``). heredoc body는 caller가
    먼저 제거한다. quote 내부 bytes는 delimiter/data 의미를 보존하기 위해 그대로 두며,
    command-word quote fragment 결합은 후속 shlex에 맡긴다.
    """
    output: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(command):
        char = command[index]
        if quote is None and char == rule.escape:
            if index + 1 < len(command) and command[index + 1] == "\n":
                index += 2
                continue
            if command[index + 1:index + 3] == "\r\n":
                index += 3
                continue
            # quote 밖 escaped quote는 literal word byte다. 다음 loop에서 quote state를
            # 열면 뒤의 실제 continuation/git command를 quote data로 오인한다.
            output.append(char)
            if index + 1 < len(command):
                index += 1
                output.append(command[index])
            index += 1
            continue
        output.append(char)
        if quote is None and char in rule.quotes:
            quote = char
        elif quote == char:
            quote = None
        elif quote in rule.escaped_quotes and char == rule.escape and index + 1 < len(command):
            index += 1
            output.append(command[index])
        index += 1
    return "".join(output)


_SHELL_COMMAND_SEPARATORS = frozenset({"&&", "||", ";", "\n", "|"})

_SHELL_WINDOWS_PATH_SEPARATOR = "\ue000"

# 어댑터가 넘기는 셸 값. 그 명령을 실행할 셸이고, 하네스가 셸을 알려 주지 않으면 unknown 이다.
SHELL_BASH = "bash"              # bash·sh·zsh (Windows 의 Git Bash 포함)
SHELL_POWERSHELL = "powershell"  # Windows PowerShell 5.1·PowerShell 7
SHELL_CMD = "cmd"                # cmd.exe
SHELL_UNKNOWN = "unknown"

_SHELL_WINDOWS_ABSOLUTE_PATH = re.compile(
    r"(?<![A-Za-z0-9_])(?:[A-Za-z]:\\|\\\\)[^ \t\r\n;&|<>\"']+"
)


def _protect_shell_windows_path_separators(command: str) -> str:
    """POSIX shlex가 Windows 절대경로의 ``\\``를 escape로 소비하지 않게 표시한다.

    이 단계는 정규화하지 않는다. 경로형 토큰임이 드러난 drive/UNC 표기의 구분자만 보존하고,
    실제 ``/`` 변환은 shlex 토큰화 뒤 simple-command가 토큰을 소비할 때 수행한다.
    """
    return _SHELL_WINDOWS_ABSOLUTE_PATH.sub(
        lambda match: match.group(0).replace("\\", _SHELL_WINDOWS_PATH_SEPARATOR),
        command,
    )


def _mark_every_backslash(command: str) -> str:
    """PowerShell·cmd 의 ``\\`` 는 escape 가 아니라 글자 그대로라 모두 경로 구분자 표시로 바꾼다."""
    return command.replace("\\", _SHELL_WINDOWS_PATH_SEPARATOR)


# cmd: 큰따옴표 안 글은 그대로 두고, 따옴표 밖 ``^`` 는 지우고 다음 글자를 남긴다.
_CMD_QUOTED_TEXT_OR_CARET = re.compile(r'("[^"]*"?)|\^(.?)', re.DOTALL)


def _protect_cmd_text(command: str) -> str:
    """cmd 읽기 준비. 따옴표 밖 ``^`` 를 지우고(다음 글자는 남긴다) ``\\`` 를 경로 구분자 표시로 바꾼다."""
    return _CMD_QUOTED_TEXT_OR_CARET.sub(r"\1\2", _mark_every_backslash(command))


class _ShellSyntax(NamedTuple):
    """셸 한 종류의 읽기 규칙. 가드가 쓰는 만큼만 담는다(따옴표·escape 글자·주석·명령 구분 기호·역슬래시·펼침 글자)."""
    escape: str                     # 따옴표 밖에서 다음 글자를 글자 그대로 만드는 글자(줄 이음 포함)
    lexer_escape: str               # shlex 에 주는 escape 글자. cmd 는 준비 단계에서 지우므로 비운다
    quotes: frozenset[str]          # 따옴표 글자
    escaped_quotes: frozenset[str]  # 안에서 escape 글자가 통하는 따옴표
    comment: str                    # 낱말 머리에서 주석을 여는 글자(없으면 빈 글)
    operators: dict[str, str]       # 명령 구분 기호 → 판정이 쓰는 구분 기호
    protect: Callable[[str], str]   # 낱말 나누기 전에 경로 구분자 ``\\`` 를 표시로 바꾸는 준비
    expansion: str                  # 명령에 있으면 읽기를 불확실로 두는 펼침 글자(변수·명령 치환)


_BASH_OPERATORS = {separator: separator for separator in _SHELL_COMMAND_SEPARATORS}

_SHELL_SYNTAX_RULES: dict[str, _ShellSyntax] = {
    SHELL_BASH: _ShellSyntax(
        "\\", "\\", frozenset("'\""), frozenset('"'), "#", _BASH_OPERATORS,
        _protect_shell_windows_path_separators, "$`",
    ),
    SHELL_POWERSHELL: _ShellSyntax(
        "`", "`", frozenset("'\""), frozenset('"'), "#", _BASH_OPERATORS, _mark_every_backslash, "$",
    ),
    # cmd 는 ``&`` 가 순차 실행이고 ``#`` 주석과 작은따옴표가 없다. ``;`` 는 명령을 나누지 않지만 내장 명령
    # (``rmdir`` 등)이 ``;``·``,`` 를 인자 구분으로 읽어 두 대상을 모두 처리하므로, ``;`` 를 명령 구분으로 읽어
    # 앞 대상을 놓치지 않는다. ``,`` 는 보통 인자에 흔해 나누지 않는다.
    SHELL_CMD: _ShellSyntax(
        "^", "", frozenset('"'), frozenset(), "", {**_BASH_OPERATORS, "&": ";"}, _protect_cmd_text, "$`",
    ),
}

# 셸 값 → 읽을 문법. 셸을 모르면 세 문법으로 모두 읽고 호출을 합쳐 가장 엄한 판정을 쓴다.
SHELL_READINGS: dict[str, tuple[str, ...]] = {
    SHELL_BASH: (SHELL_BASH,),
    SHELL_POWERSHELL: (SHELL_POWERSHELL,),
    SHELL_CMD: (SHELL_CMD,),
    SHELL_UNKNOWN: (SHELL_BASH, SHELL_POWERSHELL, SHELL_CMD),
}


def _normalize_shell_path_notation(token: str) -> str:
    """토큰화가 끝난 경로 구분자 표시 토큰(bash 읽기의 drive/UNC 절대경로, PowerShell·cmd 읽기의 모든 ``\\``)을
    POSIX 표기로 정규화한다."""
    windows_path = (
        _SHELL_WINDOWS_PATH_SEPARATOR in token
        or re.match(r"^(?:[A-Za-z]:\\|\\\\)", token) is not None
    )
    if not windows_path:
        return token
    return re.sub(
        r"/+", "/",
        token.replace(_SHELL_WINDOWS_PATH_SEPARATOR, "/").replace("\\", "/"),
    )


def restore_shell_windows_path_separators(token: str) -> str:
    """보호 표시를 원래 ``\\``로 되돌린다 — **실행용** argv 복원 경로.

    판정용 경로는 `_normalize_shell_path_notation` 이 ``/`` 표기로 접지만, 실행 argv 는 채택자가
    선언한 바이트 그대로여야 한다(표시가 남으면 그 자체가 실행 불가 경로다).
    """
    return token.replace(_SHELL_WINDOWS_PATH_SEPARATOR, "\\")


def _git_prefilter_text(command: str) -> str:
    """성능 선필터용 완화 text. quote fragment false-negative만 제거한다.

    실제 실행 여부는 반드시 shell parser가 판정한다. 따라서 quote 제거로 데이터가 후보가
    되어도 차단으로 승격되지 않는다.
    """
    return command.replace("'", "").replace('"', "")


def _persistent_cd_prefilter(text: str) -> bool:
    """top-level simple-command 경계의 ``cd`` 후보만 값싼 선필터로 찾는다."""
    separators = "|".join(
        re.escape(value)
        for value in sorted(_SHELL_COMMAND_SEPARATORS, key=len, reverse=True)
    )
    return re.search(rf"(?:^|(?:{separators}))\s*cd(?=\s)", text) is not None


def _strip_shell_comments(command: str, rule: _ShellSyntax) -> str:
    """quote/escape 밖 command-word 경계의 주석을 newline은 보존해 제거한다(cmd 는 주석이 없다)."""
    output: list[str] = []
    quote: str | None = None
    escaped = False
    word_start = True
    index = 0
    while index < len(command):
        char = command[index]
        if escaped:
            output.append(char)
            escaped = False
            word_start = False
            index += 1
            continue
        if quote is not None:
            output.append(char)
            if char == quote:
                quote = None
            elif char == rule.escape and quote in rule.escaped_quotes:
                escaped = True
            index += 1
            continue
        if char == rule.escape:
            output.append(char)
            escaped = True
            index += 1
            continue
        if char in rule.quotes:
            output.append(char)
            quote = char
            word_start = False
            index += 1
            continue
        if char == rule.comment and word_start:
            while index < len(command) and command[index] != "\n":
                index += 1
            if index < len(command):
                output.append("\n")
                index += 1
            word_start = True
            continue
        output.append(char)
        word_start = char.isspace() or char in ";&|()"
        index += 1
    return "".join(output)


def _heredoc_declarations(line: str) -> tuple[list[tuple[str, bool]], bool]:
    """한 command line의 quote/comment 밖 실제 heredoc operator를 token 단위로 읽는다."""
    try:
        lexer = shlex.shlex(
            _strip_shell_comments(line, _SHELL_SYNTAX_RULES[SHELL_BASH]), posix=True, punctuation_chars="<>",
        )
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except (TypeError, ValueError):
        return [], True
    found: list[tuple[str, bool]] = []
    unknown = False
    for index, token in enumerate(tokens):
        if token != "<<":
            continue
        if index + 1 >= len(tokens):
            unknown = True
            continue
        word = tokens[index + 1]
        strip_tabs = word.startswith("-")
        word = word[1:] if strip_tabs else word
        found.append((word, strip_tabs))
    return found, unknown


def _strip_shell_heredoc_bodies(command: str) -> tuple[str, bool]:
    """인식 가능한 heredoc body를 newline 자리만 남기고 제거한다.

    body의 ``git``은 데이터이지 실행 command가 아니다. delimiter를 끝까지 소비하지 못하거나
    ``<<``가 있는데 선언을 인식하지 못하면 uncertainty를 반환해 caller가 warn으로 surface한다.
    """
    lines = command.splitlines(keepends=True)
    output: list[str] = []
    pending: list[tuple[str, bool]] = []
    uncertain = False
    for line in lines:
        if pending:
            word, strip_tabs = pending[0]
            candidate = line.rstrip("\r\n")
            if strip_tabs:
                candidate = candidate.lstrip("\t")
            output.append("\n" if line.endswith(("\n", "\r")) else "")
            if candidate == word:
                pending.pop(0)
            continue
        declarations, unknown = _heredoc_declarations(line)
        uncertain = uncertain or unknown
        output.append(line)
        pending.extend(declarations)
    if pending:
        uncertain = True
    return "".join(output), uncertain


def _shell_segments(command: str, syntax: str) -> tuple[list[list[str]], list[str]] | None:
    """shell 문자열을 ``syntax`` 문법으로 simple-command segment와 그 사이 operator로 나눈다."""
    rule = _SHELL_SYNTAX_RULES[syntax]
    separator_chars = "".join(sorted({char for separator in rule.operators for char in separator}))
    try:
        lexer = shlex.shlex(
            rule.protect(_strip_shell_comments(command, rule)), posix=True,
            punctuation_chars=separator_chars + "<>",
        )
        lexer.escape = rule.lexer_escape
        lexer.quotes = "".join(sorted(rule.quotes))
        lexer.escapedquotes = "".join(sorted(rule.escaped_quotes))
        lexer.whitespace_split = True
        # unquoted newline은 ``;``와 같은 순차 command 경계다. 기본 shlex whitespace로
        # 버리면 두 git 호출이 한 argv로 합쳐져 뒤 호출을 놓친다. quote 안 newline은 token에 남는다.
        lexer.whitespace = " \t\r"
        lexer.commenters = ""
        tokens = list(lexer)
    except (TypeError, ValueError):
        return None
    segments: list[list[str]] = []
    operators: list[str] = []
    current: list[str] = []
    for token in tokens:
        if token and set(token) == {"\n"}:
            token = "\n"
        if token in rule.operators:
            segments.append(current)
            operators.append(rule.operators[token])
            current = []
        elif token and all(ch in separator_chars for ch in token):
            # case/and-or 확장 연산자 등 미지원 shell 문법은 해석하지 않는다(never-block).
            return None
        else:
            current.append(token)
    segments.append(current)
    return segments, operators


_SHELL_REDIRECTION_OPERATORS = frozenset({
    "<", ">", "<<", ">>", "<<<", "<>", ">|", "<&", ">&",
})


def _shell_without_redirections(segment: Sequence[str]) -> tuple[list[str], bool]:
    """simple-command argv에서 redirection operator와 target을 제거한다.

    redirection target은 command argv/pathspec이 아니다. fd prefix(``2>``)는 shlex에서 별도
    숫자 token으로 갈라지므로 operator 바로 앞 숫자도 제거한다. 복잡/불완전 redirection도
    target을 git pathspec으로 승격하지 않는 쪽으로 fail-open한다.
    """
    cleaned: list[str] = []
    redirected = False
    index = 0
    values = list(segment)
    while index < len(values):
        token = values[index]
        if token in _SHELL_REDIRECTION_OPERATORS:
            redirected = True
            if cleaned and cleaned[-1].isdigit():
                cleaned.pop()
            index += 2  # operator + redirection target
            continue
        cleaned.append(token)
        index += 1
    return cleaned, redirected


def _shell_check(operator: str | None, status: bool | None) -> bool | None:
    if operator is None or operator in _SHELL_COMMAND_SEPARATORS - {"&&", "||", "|"}:
        return True
    if operator == "&&":
        return status
    if operator == "||":
        return None if status is None else not status
    return True


_SHELL_CWD_CHANGERS: frozenset[str] = frozenset({"cd", "pushd", "popd"})


def _shell_hidden_target_change(words: Sequence[str]) -> bool:
    """셸 파서가 호출 좌표에 반영하지 못하는 git 대상 변경이 있는지.

    괄호 subshell·명령 치환(``(cd x && …)``·``$(cd x …)``·backtick) 속 cd 는 뒤 git 의 실제 위치를
    바꾸는데 파서는 그 cd 를 command 로 읽지 않는다. ``export GIT_DIR=…`` 같은 셸 변수 설정도 뒤의
    모든 git 대상을 바꾼다. 둘 다 대상을 정적으로 정할 수 없는 호출로 본다.
    """
    group_opened = False
    for word in words:
        if "GIT_DIR=" in word or "GIT_WORK_TREE=" in word:
            return True
        name = word.lstrip("$(`")
        if (group_opened or name != word) and name in _SHELL_CWD_CHANGERS:
            return True
        group_opened = not name
    return False


def _shell_removal_calls(
    target_repos: set[Path], cwd_certain: bool, command: Sequence[str],
) -> list[_ShellGitInvocation]:
    """simple command 하나의 파일 삭제·이동 호출(직접 명령 + 간접 실행 인자 속 호출)."""
    values = [_normalize_shell_path_notation(value) for value in command]
    argvs: list[tuple[tuple[str, ...], bool]] = []
    direct = _shell_file_removal_argv(values)
    if direct is not None:
        argvs.append((direct, cwd_certain))
    indirect, changes_cwd = _shell_indirect_file_removal_argvs(values)
    argvs.extend((argv, cwd_certain and not changes_cwd) for argv in indirect)
    return [
        _ShellGitInvocation(str(target_repo), argv, certain, certain)
        for argv, certain in argvs
        for target_repo in sorted(target_repos, key=str)
    ]


def _bootstrap_task_argument(args: Sequence[str]) -> str | None:
    for index, token in enumerate(args):
        if token == "--task" and index + 1 < len(args):
            return args[index + 1]
        if token.startswith("--task="):
            return token.split("=", 1)[1]
    return None
