"""설치 공통 jsonc (domain)."""
from __future__ import annotations

from typing import NamedTuple
import json
import re

# quoted-string 원소 추출 (escape-aware) — 등록-확인을 substring 이 아니라 *정확 원소* 대조로
# `.opencode/pm-instructions.md.bak` 같은 suffix 나 문자열-내
# 부분일치를 "이미 등록"으로 오인하지 않게 한다.
_JSONC_STRING_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')


# 최상위(depth==1) `"instructions"` 키 + 배열 여는 `[` — brace-depth 스캐너가 이 위치에서 match.
_INSTR_KEY_RE = re.compile(r'"instructions"\s*:\s*\[')


def _mask_jsonc_comments(text: str) -> str:
    """jsonc 주석(`//…`·`/* */`)을 같은 길이 공백(개행 보존)으로 마스킹 — **오프셋 보존**(원본과 1:1).

    문자열 리터럴은 존중한다 — 문자열 안의 `//`(예: `$schema` URL `https://…`)는 주석이 아니므로
    마스킹하지 않는다. 탐지/삽입 위치를 이 마스킹본에서 구하고 실제 write 는 원본에 같은 오프셋으로
    적용해, 주석-아웃된 `"instructions"`/경로를 오탐 없이 걸러내면서 원본 주석·서식을 보존한다."""
    out: list[str] = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:  # escape — 다음 문자 그대로(짝으로 소비)
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":  # 라인 주석 → EOL 까지 blank
            j = i
            while j < n and text[j] != "\n":
                out.append(" ")
                j += 1
            i = j
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":  # 블록 주석 → `*/` 까지 blank(개행 보존)
            j = i
            while j < n and not (text[j] == "*" and j + 1 < n and text[j + 1] == "/"):
                out.append("\n" if text[j] == "\n" else " ")
                j += 1
            if j < n:  # 닫는 `*/`
                out.append("  ")
                j += 2
            i = j
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _strip_jsonc_trailing_commas(text: str) -> str:
    """`}`/`]` 앞의 후행 쉼표를 공백 1글자로 치환 — **길이 보존**(원본과 1:1).

    jsonc 는 후행 쉼표를 허용하지만 `json.loads` 는 거부한다. 채택자가 배열·객체에서 항목 하나를
    손으로 지우면 바로 그 형상이 되고, 그 파일이 통째로 빈 문서로 읽히면 선언 키가 전부 없는
    것으로 실린다(유효한 파일을 오판).

    문자열 리터럴은 존중한다 — 문자열 안의 `,` 는 문법 쉼표가 아니다. 주석은 마스킹
    (`_mask_jsonc_comments`)이 이미 같은 길이 공백으로 바꾼 뒤라 공백 건너뛰기에 흡수된다.
    치환이 1:1 이라 마스킹의 오프셋 보존 계약이 이 변환 뒤에도 그대로다."""
    out = list(text)
    i, n = 0, len(text)
    in_str = False
    comma: int | None = None            # 뒤 문자를 아직 못 본 최근 쉼표 위치
    while i < n:
        char = text[i]
        if in_str:
            if char == "\\" and i + 1 < n:   # escape — 다음 문자를 짝으로 소비
                i += 2
                continue
            if char == '"':
                in_str = False
            i += 1
            continue
        if char == '"':
            in_str = True
            comma = None
        elif char == ",":
            comma = i
        elif not char.isspace():
            if char in "}]" and comma is not None:
                out[comma] = " "
            comma = None
        i += 1
    return "".join(out)


# ── jsonc 키 경로 편집 (하네스 설정 반영 · `pm_import.apply_harness_ctx`) ──────────
# 위 두 함수와 같은 규칙이다: 위치는 주석을 가린 사본(오프셋 == 원본)에서 찾고 원본의 같은
# 오프셋을 고친다. 재직렬화하지 않으므로 주석·키 순서·들여쓰기·개행 표기가 그대로 남는다.
# JSON 은 JSONC 의 부분집합이라 `.claude/settings.json` 도 같은 함수로 고친다.
_JSONC_WHITESPACE = " \t\r\n"


_JSONC_INDENT_STEP = "  "


class _JsoncMember(NamedTuple):
    """객체 멤버 하나의 좌표 — 키 따옴표 시작·값 리터럴의 [시작, 끝)."""
    key: str
    key_start: int
    value_start: int
    value_end: int


def _jsonc_skip_whitespace(masked: str, index: int) -> int:
    while index < len(masked) and masked[index] in _JSONC_WHITESPACE:
        index += 1
    return index


def _jsonc_value_end(masked: str, start: int) -> int:
    """값 리터럴 시작 오프셋 → 끝 오프셋(배타) — 문자열·중첩 괄호를 존중한다."""
    length = len(masked)
    if start >= length:
        raise ValueError("jsonc 값 자리가 비어 있다(파일 끝)")
    first = masked[start]
    index = start + 1
    if first == '"':
        while index < length:
            if masked[index] == "\\":
                index += 2
                continue
            if masked[index] == '"':
                return index + 1
            index += 1
        raise ValueError(f"jsonc 문자열이 닫히지 않았다 (오프셋 {start})")
    if first in "{[":
        depth, in_string = 1, False
        while index < length:
            char = masked[index]
            if in_string:
                if char == "\\":
                    index += 2
                    continue
                if char == '"':
                    in_string = False
            elif char == '"':
                in_string = True
            elif char in "{[":
                depth += 1
            elif char in "}]":
                depth -= 1
                if depth == 0:
                    return index + 1
            index += 1
        raise ValueError(f"jsonc 괄호가 닫히지 않았다 (오프셋 {start})")
    while index < length and masked[index] not in ",}]" + _JSONC_WHITESPACE:
        index += 1
    if first in ",}]" + _JSONC_WHITESPACE:
        raise ValueError(f"jsonc 값 자리에 값이 없다 (오프셋 {start})")
    return index


def _jsonc_object_members(masked: str, open_index: int) -> tuple[list[_JsoncMember], int]:
    """`{` 오프셋 → (멤버 목록, 닫는 `}` 오프셋). 후행 쉼표를 허용한다."""
    members: list[_JsoncMember] = []
    index = _jsonc_skip_whitespace(masked, open_index + 1)
    while True:
        if index >= len(masked):
            raise ValueError(f"jsonc 객체가 닫히지 않았다 (오프셋 {open_index})")
        if masked[index] == "}":
            return members, index
        if masked[index] != '"':
            raise ValueError(f"jsonc 객체 키 자리에 문자열이 없다 (오프셋 {index})")
        key_end = _jsonc_value_end(masked, index)
        key = json.loads(masked[index:key_end])
        colon = _jsonc_skip_whitespace(masked, key_end)
        if colon >= len(masked) or masked[colon] != ":":
            raise ValueError(f"jsonc 키 {key!r} 뒤에 `:` 가 없다 (오프셋 {colon})")
        value_start = _jsonc_skip_whitespace(masked, colon + 1)
        value_end = _jsonc_value_end(masked, value_start)
        members.append(_JsoncMember(key, index, value_start, value_end))
        index = _jsonc_skip_whitespace(masked, value_end)
        if index < len(masked) and masked[index] == ",":
            index = _jsonc_skip_whitespace(masked, index + 1)
            continue
        if index < len(masked) and masked[index] == "}":
            return members, index
        raise ValueError(f"jsonc 멤버 {key!r} 뒤에 `,` 나 `}}` 가 없다 (오프셋 {index})")


def _jsonc_member_text(path: tuple[str, ...], literal: str, indent: str, newline: str) -> str:
    """`path[0]` 멤버 하나의 텍스트 — 남은 조각은 한 줄에 멤버 하나인 중첩 객체로 만든다."""
    key = json.dumps(path[0], ensure_ascii=False)
    if len(path) == 1:
        return f"{key}: {literal}"
    inner = indent + _JSONC_INDENT_STEP
    return (f"{key}: {{{newline}{inner}"
            f"{_jsonc_member_text(path[1:], literal, inner, newline)}{newline}{indent}}}")


def _jsonc_inline_member_text(path: tuple[str, ...], literal: str) -> str:
    """한 줄에 멤버를 늘어놓은 객체에 넣는 멤버 — 줄을 새로 만들지 않는다."""
    key = json.dumps(path[0], ensure_ascii=False)
    if len(path) == 1:
        return f"{key}: {literal}"
    return f"{key}: {{{_jsonc_inline_member_text(path[1:], literal)}}}"


def _jsonc_line_indent(text: str, offset: int) -> str:
    """`offset` 이 든 줄의 앞 공백."""
    line_start = text.rfind("\n", 0, offset) + 1
    line = text[line_start:]
    return line[:len(line) - len(line.lstrip(" \t"))]


def _jsonc_set_value(text: str, path: tuple[str, ...], value: int) -> tuple[str, str | None]:
    """jsonc 텍스트의 키 경로에 정수 값을 쓴다 — (새 텍스트, 옛 값 리터럴 또는 None).

    키가 있으면 값 리터럴 자리만 바꾼다(같은 키가 여럿이면 읽는 쪽이 쓰는 마지막 것). 없으면 그
    객체의 마지막 멤버 뒤에 쉼표와 함께 넣고, 경로 중간의 없는 객체는 차례로 만든다. 새 멤버의
    들여쓰기는 형제 멤버를 따르고, 형제가 없으면 여는 괄호 줄 들여쓰기에 2칸을 더한다. 줄바꿈은
    파일의 개행 표기를 따른다. 경로 조각은 문서 키 그대로다(점·슬래시가 든 모델 id 를 가르지
    않는다). 경로가 객체가 아닌 값을 지나거나 문법이 깨졌으면 `ValueError` 다 — 채택자 값을
    덮어써서 지우지 않는다.
    """
    if not path:
        raise ValueError("jsonc 키 경로가 비어 있다")
    masked = _mask_jsonc_comments(text)
    newline = "\r\n" if "\r\n" in text else "\n"
    literal = json.dumps(value)
    open_index = _jsonc_skip_whitespace(masked, 0)
    if open_index >= len(masked) or masked[open_index] != "{":
        raise ValueError("jsonc 최상위가 객체가 아니다")
    for depth, key in enumerate(path):
        members, close_index = _jsonc_object_members(masked, open_index)
        found = [member for member in members if member.key == key]
        if not found:
            return _jsonc_insert_member(
                text, open_index, close_index, members, path[depth:], literal,
                newline), None
        member = found[-1]
        if depth == len(path) - 1:
            old = text[member.value_start:member.value_end]
            return text[:member.value_start] + literal + text[member.value_end:], old
        if masked[member.value_start] != "{":
            raise ValueError(
                f"jsonc `{'.'.join(path[:depth + 1])}` 가 객체가 아니다 — 그 아래에 쓸 수 없다")
        open_index = member.value_start
    raise AssertionError("unreachable")


def _jsonc_insert_member(text: str, open_index: int, close_index: int,
                         members: list[_JsoncMember], path: tuple[str, ...], literal: str,
                         newline: str) -> str:
    """객체(`{`…`}`)에 `path[0]` 멤버를 넣은 텍스트 — 형제가 있으면 마지막 형제 뒤다."""
    if members:
        last = members[-1]
        line_start = text.rfind("\n", 0, last.key_start) + 1
        sibling_indent = text[line_start:last.key_start]
        if sibling_indent.strip(" \t"):
            piece = ", " + _jsonc_inline_member_text(path, literal)
        else:
            piece = ("," + newline + sibling_indent
                     + _jsonc_member_text(path, literal, sibling_indent, newline))
        return text[:last.value_end] + piece + text[last.value_end:]
    parent_indent = _jsonc_line_indent(text, open_index)
    indent = parent_indent + _JSONC_INDENT_STEP
    member = newline + indent + _jsonc_member_text(path, literal, indent, newline)
    if text[open_index + 1:close_index].strip(_JSONC_WHITESPACE):
        # 빈 객체 안에 주석만 있다 — 주석을 지우지 않게 여는 괄호 바로 뒤에 넣는다.
        return text[:open_index + 1] + member + text[open_index + 1:]
    return text[:open_index + 1] + member + newline + parent_indent + text[close_index:]
