"""테스트 이식성 헬퍼 — 능력(capability) 탐지로 환경 의존 테스트를 skip.

플랫폼 문자열 하드코딩(`sys.platform == "win32"`)이 아니라 실제 능력을 탐지한다 —
권한 있는 Windows(개발자모드/관리자)·WSL·Linux/Mac 은 자연히 실행되고, symlink 를
못 만드는 환경에서만 skip 된다.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

_CAN_SYMLINK: bool | None = None  # 1회 탐지 결과 캐시.


def _can_symlink() -> bool:
    """이 환경에서 `os.symlink` 가 실제로 동작하는지 탐지(결과 캐시).

    Windows 는 symlink 생성에 개발자모드/관리자 권한이 필요해 `OSError: [WinError 1314]`
    가 난다. tmp 디렉토리에 실제 symlink 를 시도해 성공 여부로 판단한다 — 플랫폼이 아니라
    능력을 본다.
    """
    global _CAN_SYMLINK
    if _CAN_SYMLINK is not None:
        return _CAN_SYMLINK

    can = False
    try:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            target.mkdir()
            link = Path(tmp) / "link"
            os.symlink(target, link, target_is_directory=True)
            can = link.is_symlink()
    except (OSError, NotImplementedError):
        can = False

    _CAN_SYMLINK = can
    return can
