"""테스트 픽스처의 텍스트 I/O를 플랫폼과 무관하게 만드는 공용 seam."""
from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path


def write_lf(path: Path, text: str, *, encoding: str = "utf-8") -> int:
    """바이트 민감 픽스처를 OS 기본 개행과 무관하게 LF로 기록한다."""
    return path.write_text(text, encoding=encoding, newline="\n")


def write_crlf(path: Path, text: str, *, encoding: str = "utf-8") -> int:
    """CRLF 체크아웃(`core.autocrlf=true`)·플랫폼 텍스트 쓰기를 LF 환경에서 재현한다.

    개행 표기에 민감한 가드가 LF 환경(Linux CI)에서도 CRLF 축을 실제로 태우게 하는 픽스처 writer다.
    """
    return path.write_text(text, encoding=encoding, newline="\r\n")


def normalize_newlines(text: str) -> str:
    """실 자식/콘솔 출력의 CRLF와 CR을 비교용 LF로 정규화한다."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def normalize_newline_bytes(data: bytes) -> bytes:
    """바이트 산출물의 CRLF·CR을 비교용 LF로 정규화한다.

    기대값을 ``read_text``(universal newline)로, 실측값을 ``read_bytes``(원본 표기 보존)로 만들어
    비교하면 CRLF 체크아웃에서 내용이 같아도 항상 불일치한다. 두 값을 **이 층**에서 만들면
    개행 표기와 무관한 내용 동일성을 판정한다(내용이 한 글자라도 다르면 여전히 red).
    """
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


_CRLF_BYTES = b"\r\n"
_LF_BYTES = b"\n"


def dominant_newline_bytes(data: bytes, default: bytes = _LF_BYTES) -> bytes:
    """바이트 본문의 지배 개행 표기 — 다수결, 동수면 첫 등장, 개행 0이면 ``default``.

    엔진의 표기 보존 규칙(`pm_import.dominant_newline`·`pm_update._dominant_newline`)과 같은
    판정을 **테스트 쪽에서 독립 구현**한다. 엔진 함수를 그대로 빌려 판정하면 그 함수가 무너질 때
    기대값과 실측값이 함께 무너져 대조가 조용히 통과한다([[guard-must-cover-its-own-surface]]).
    """
    crlf = data.count(_CRLF_BYTES)
    lf = data.count(_LF_BYTES) - crlf
    if crlf == 0 and lf == 0:
        return default
    if crlf != lf:
        return _CRLF_BYTES if crlf > lf else _LF_BYTES
    first = data.find(_LF_BYTES)
    return _CRLF_BYTES if first > 0 and data[first - 1:first] == b"\r" else _LF_BYTES


def renotated(lf_bytes: bytes, notation: bytes) -> bytes:
    """LF 정규화 기대 bytes를 ``notation`` 표기로 되돌린다 (byte-exact 대조용 기대값)."""
    return lf_bytes if notation == _LF_BYTES else lf_bytes.replace(_LF_BYTES, notation)


def write_matching_newlines(
    path: Path, text: str, *, like: bytes, encoding: str = "utf-8"
) -> int:
    """``like``의 지배 개행 표기로 쓴다 — 픽스처가 대상 파일의 표기를 바꾸지 않게 한다.

    표기를 보존하는 엔진에서는 "픽스처가 내용을 손상시킨다"와 "픽스처가 표기를 바꾼다"가 다른
    축이다. 플랫폼 기본 텍스트 쓰기(`write_text`)는 Windows에서 후자를 함께 일으켜, 엔진이 바뀐
    표기를 충실히 보존한 결과가 byte 비교에서 "갱신 실패"로 보였다(T-0724 실측).
    """
    newline = "\r\n" if dominant_newline_bytes(like) == _CRLF_BYTES else "\n"
    return path.write_text(text, encoding=encoding, newline=newline)


def utf8_child_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """실 Python 자식이 locale과 무관하게 UTF-8 stdio를 쓰는 환경을 반환한다."""
    env = dict(os.environ if base is None else base)
    env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
    return env
