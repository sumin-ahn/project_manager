"""테스트 픽스처의 텍스트 I/O를 플랫폼과 무관하게 만드는 공용 seam."""
from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path


def write_lf(path: Path, text: str, *, encoding: str = "utf-8") -> int:
    """바이트 민감 픽스처를 OS 기본 개행과 무관하게 LF로 기록한다."""
    return path.write_text(text, encoding=encoding, newline="\n")


def normalize_newlines(text: str) -> str:
    """실 자식/콘솔 출력의 CRLF와 CR을 비교용 LF로 정규화한다."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def utf8_child_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """실 Python 자식이 locale과 무관하게 UTF-8 stdio를 쓰는 환경을 반환한다."""
    env = dict(os.environ if base is None else base)
    env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
    return env
