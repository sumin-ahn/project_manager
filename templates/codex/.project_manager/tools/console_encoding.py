#!/usr/bin/env python3
"""CLI 콘솔 출력을 UTF-8로 정합하는 공용 부트스트랩.

Windows 콘솔 codepage와 Python 텍스트 스트림을 함께 맞춘다. 모든 단계는 best-effort라
콘솔 핸들이 없거나 테스트 캡처 스트림이 ``reconfigure``를 지원하지 않아도 CLI 본동작을
막지 않는다.
"""
from __future__ import annotations

import os
import sys


# 여러 CLI가 공유하는 엔진 의존성이므로 부분 전파 skew 가드에 편입한다.
ENGINE_REV = "v1.7.0"


def _set_console_codepage_utf8() -> None:
    """Windows 콘솔 입출력 codepage를 UTF-8(65001)로 맞춘다 (best-effort)."""
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass


def configure_console_utf8() -> None:
    """codepage와 stdout/stderr를 UTF-8로 정합한다.

    ``reconfigure`` 미지원 스트림은 건너뛰고, 지원하지만 실패하는 스트림도 best-effort로
    통과한다. stamped CLI의 부분 전파 skew는 로더의 기존 ``_verify_engine_rev``가 검증한다.
    """
    _set_console_codepage_utf8()
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
