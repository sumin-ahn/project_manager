"""테스트 이식성 헬퍼 — 능력(capability) 탐지로 환경 의존 테스트를 skip.

플랫폼 문자열 하드코딩(`sys.platform == "win32"`)이 아니라 실제 능력을 탐지한다 —
권한 있는 Windows(개발자모드/관리자)·WSL·Linux/Mac 은 자연히 실행되고, symlink 를
못 만드는 환경에서만 skip 된다.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

# 능력 탐지는 **수집 시점**(`skipif` 인자 평가)이라 `tmp_path` 픽스처가 없다. 그래서 부모를
# 명시한다 — 프로젝트 안 per-clone 스크래치이며 루트 `conftest.py` 가 모듈 로드 시점에 이미
# 만들어 둔 그 자리다(pytest 임시 루트와 같은 디렉터리).
PROJECT_TEMP_ROOT = (
    Path(__file__).resolve().parents[1] / ".project_manager" / ".local" / "tmp"
)

_CAN_SYMLINK: bool | None = None  # 1회 탐지 결과 캐시.
_POSIX_MODE_SUPPORTED: bool | None = None
_GIT_SYMLINK_SUPPORTED: bool | None = None


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
        with tempfile.TemporaryDirectory(dir=PROJECT_TEMP_ROOT) as tmp:
            target = Path(tmp) / "target"
            target.mkdir()
            link = Path(tmp) / "link"
            os.symlink(target, link, target_is_directory=True)
            can = link.is_symlink()
    except (OSError, NotImplementedError):
        can = False

    _CAN_SYMLINK = can
    return can


def posix_mode_supported() -> bool:
    """chmod(0600)가 stat mode로 실제 왕복되는지 능력 기반으로 탐지한다."""
    global _POSIX_MODE_SUPPORTED
    if _POSIX_MODE_SUPPORTED is not None:
        return _POSIX_MODE_SUPPORTED

    supported = False
    try:
        with tempfile.TemporaryDirectory(dir=PROJECT_TEMP_ROOT) as tmp:
            probe = Path(tmp) / "mode-probe"
            probe.write_bytes(b"probe")
            probe.chmod(0o600)
            supported = probe.stat().st_mode & 0o777 == 0o600
    except (OSError, NotImplementedError):
        supported = False
    _POSIX_MODE_SUPPORTED = supported
    return supported


def posix_bash_supported() -> bool:
    """POSIX bash wrapper를 그대로 실행할 수 있는 환경인지 판정한다."""
    return os.name != "nt" and shutil.which("bash") is not None


def posix_filenames_supported() -> bool:
    """개행·별표 등 POSIX 파일명 회귀를 실행할 수 있는 환경인지 판정한다."""
    return os.name != "nt"


def git_symlink_supported() -> bool:
    """git checkout이 symlink index entry를 실제 symlink로 왕복하는지 탐지한다."""
    global _GIT_SYMLINK_SUPPORTED
    if _GIT_SYMLINK_SUPPORTED is not None:
        return _GIT_SYMLINK_SUPPORTED
    git = shutil.which("git")
    if git is None or not _can_symlink():
        _GIT_SYMLINK_SUPPORTED = False
        return False
    supported = False
    try:
        with tempfile.TemporaryDirectory(dir=PROJECT_TEMP_ROOT) as tmp:
            root = Path(tmp)
            source = root / "source"
            clone = root / "clone"
            source.mkdir()
            subprocess.run([git, "-C", str(source), "init", "-q"], check=True)
            (source / "target").write_text("target\n", encoding="utf-8")
            os.symlink("target", source / "link")
            subprocess.run([git, "-C", str(source), "add", "target", "link"], check=True)
            subprocess.run([
                git, "-C", str(source),
                "-c", "user.name=t", "-c", "user.email=t@x.invalid",
                "commit", "-qm", "symlink probe",
            ], check=True)
            subprocess.run([git, "clone", "-q", str(source), str(clone)], check=True)
            supported = (clone / "link").is_symlink()
    except (OSError, subprocess.SubprocessError):
        supported = False
    _GIT_SYMLINK_SUPPORTED = supported
    return supported
