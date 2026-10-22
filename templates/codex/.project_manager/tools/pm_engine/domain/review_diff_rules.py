"""Git diff 경로 해석 (층: domain)."""
from __future__ import annotations


_GIT_PATH_INVALID = object()
_GIT_PATH_MISSING = object()
_GIT_PATH_DEV_NULL = object()


def _git_c_unquote(value: str) -> str:
    """Git의 큰따옴표 C-quote 경로 하나를 UTF-8 문자열로 복원한다.

    Git은 ``core.quotePath`` 기본값에서 비 ASCII 바이트를 ``\\ooo`` 8진 escape로 쓴다.
    따라서 escape를 문자 단위로 치환하면 mojibake가 된다. 먼저 원래 바이트열을 복원한 뒤
    UTF-8로 엄격하게 decode한다. 손상되거나 비 UTF-8인 표기는 호출자가 fail-closed 처리한다.
    """
    if len(value) < 2 or value[0] != '"' or value[-1] != '"':
        raise ValueError("Git C-quote 경로가 큰따옴표로 닫히지 않았습니다")
    decoded = bytearray()
    named_escapes = {
        "a": 0x07, "b": 0x08, "t": 0x09, "n": 0x0A,
        "v": 0x0B, "f": 0x0C, "r": 0x0D,
    }
    index = 1
    while index < len(value) - 1:
        char = value[index]
        if char != "\\":
            decoded.extend(char.encode("utf-8"))
            index += 1
            continue
        index += 1
        if index >= len(value) - 1:
            raise ValueError("Git C-quote 경로의 escape가 끝나지 않았습니다")
        escaped = value[index]
        if escaped in ('"', "\\"):
            decoded.append(ord(escaped))
            index += 1
        elif escaped in named_escapes:
            decoded.append(named_escapes[escaped])
            index += 1
        elif escaped in "01234567":
            octal = value[index:index + 3]
            if len(octal) != 3 or any(digit not in "01234567" for digit in octal):
                raise ValueError("Git C-quote 경로의 8진 escape가 3자리가 아닙니다")
            decoded.append(int(octal, 8))
            index += 3
        else:
            raise ValueError(f"알 수 없는 Git C-quote escape: \\{escaped}")
    return decoded.decode("utf-8", errors="strict")


def _quoted_git_path_atom(value: str) -> tuple[str, str]:
    """문자열 선두의 C-quoted atom과 나머지를 분리한다."""
    if not value.startswith('"'):
        raise ValueError("C-quoted atom이 아닙니다")
    index = 1
    while index < len(value):
        if value[index] == "\\":
            index += 2
            continue
        if value[index] == '"':
            atom = value[:index + 1]
            return _git_c_unquote(atom), value[index + 1:]
        index += 1
    raise ValueError("Git C-quote 경로가 닫히지 않았습니다")


def _diff_header_paths(line: str) -> tuple[str, str] | None:
    """구조적으로 명확한 ``diff --git`` 헤더의 old/new 경로만 복원한다.

    공백 경로의 ``a/x y b/x y``는 헤더만으로 경계를 정할 수 없으므로 반환하지 않는다. 두 atom이
    각각 공백 없는 값이거나 C-quote로 경계가 명시된 경우만 보조 근거로 쓴다.
    """
    value = line[len("diff --git "):].rstrip("\r\n")
    atoms: list[str] = []
    position = 0
    try:
        for _ in range(2):
            while position < len(value) and value[position].isspace():
                position += 1
            if position >= len(value):
                return None
            if value[position] == '"':
                atom, remainder = _quoted_git_path_atom(value[position:])
                consumed = len(value[position:]) - len(remainder)
                position += consumed
            else:
                end = position
                while end < len(value) and not value[end].isspace():
                    end += 1
                atom = value[position:end]
                position = end
            atoms.append(atom)
        if value[position:].strip():
            return None
    except (UnicodeDecodeError, ValueError):
        return None
    if not atoms[0].startswith("a/") or not atoms[1].startswith("b/"):
        return None
    return atoms[0][2:], atoms[1][2:]


def _metadata_git_path(
    line: str, marker: str, *, side_prefix: str | None = None,
    tab_suffix: bool = False,
) -> str | object:
    """``---``/``+++``/rename 메타데이터 한 줄의 경로를 복원한다."""
    value = line[len(marker):].rstrip("\r\n")
    try:
        if value.startswith('"'):
            path, remainder = _quoted_git_path_atom(value)
            if remainder and not (tab_suffix and remainder.startswith("\t")):
                return _GIT_PATH_INVALID
        else:
            path = value.split("\t", 1)[0] if tab_suffix else value
    except (UnicodeDecodeError, ValueError):
        return _GIT_PATH_INVALID
    if path == "/dev/null":
        return _GIT_PATH_DEV_NULL
    if side_prefix is not None:
        prefix = side_prefix + "/"
        if not path.startswith(prefix):
            return _GIT_PATH_INVALID
        path = path[len(prefix):]
    return path if path else _GIT_PATH_INVALID


def _unique_metadata_path(values: list[str | object]) -> str | object:
    """한쪽 메타데이터가 가리키는 단 하나의 path/dev-null 상태를 확정한다."""
    if _GIT_PATH_INVALID in values:
        return _GIT_PATH_INVALID
    paths = {value for value in values if isinstance(value, str)}
    has_dev_null = _GIT_PATH_DEV_NULL in values
    if len(paths) > 1 or (paths and has_dev_null):
        return _GIT_PATH_INVALID
    if paths:
        return next(iter(paths))
    if has_dev_null:
        return _GIT_PATH_DEV_NULL
    return _GIT_PATH_MISSING


def _diff_block_path(block: list[str]) -> str | None:
    """Git diff block의 판정 경로를 메타데이터 우선으로 유일하게 확정한다.

    destination 경로를 우선하되 신규/삭제의 ``/dev/null``은 경로로 보지 않고 반대편을 쓴다.
    헤더는 공백 없는 두 atom 또는 C-quoted 두 atom일 때, 해당 side의 구조화 메타데이터가 없을
    때만 폴백한다. 공백 헤더를 임의 분할하지 않는다.
    """
    if not block or not block[0].startswith("diff --git "):
        return None
    old_values: list[str | object] = []
    new_values: list[str | object] = []
    for line in block[1:]:
        if line.startswith("@@") or line.startswith("GIT binary patch"):
            break
        if line.startswith("--- "):
            old_values.append(_metadata_git_path(
                line, "--- ", side_prefix="a", tab_suffix=True,
            ))
        elif line.startswith("+++ "):
            new_values.append(_metadata_git_path(
                line, "+++ ", side_prefix="b", tab_suffix=True,
            ))
        elif line.startswith("rename from ") or line.startswith("copy from "):
            marker = "rename from " if line.startswith("rename from ") else "copy from "
            old_values.append(_metadata_git_path(line, marker))
        elif line.startswith("rename to ") or line.startswith("copy to "):
            marker = "rename to " if line.startswith("rename to ") else "copy to "
            new_values.append(_metadata_git_path(line, marker))

    header_paths = _diff_header_paths(block[0])
    if not old_values and header_paths is not None:
        old_values.append(header_paths[0])
    if not new_values and header_paths is not None:
        new_values.append(header_paths[1])
    old_path = _unique_metadata_path(old_values)
    new_path = _unique_metadata_path(new_values)
    if _GIT_PATH_INVALID in (old_path, new_path):
        return None
    if isinstance(new_path, str):
        return new_path
    if new_path is _GIT_PATH_DEV_NULL and isinstance(old_path, str):
        return old_path
    if new_path is _GIT_PATH_MISSING and isinstance(old_path, str):
        return old_path
    return None
