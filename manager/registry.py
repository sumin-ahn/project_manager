"""프로젝트 등록부 — 사용자 설정 폴더의 파일 하나(PM 홈 절대경로 목록).

관리자가 직접 여는 **유일한 쓰기 핸들**이다. 담는 값은 경로뿐이고 캐시·버전·현황을 두지
않는다 — 현황은 매번 그 사본의 `status --json` 으로 읽는다([[ADR-0099]]).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import CallError

REGISTRY_DIRECTORY_NAME = "project_manager"
REGISTRY_FILE_NAME = "projects.json"
# PM 홈의 표식 — 이 디렉터리가 있어야 사본을 부를 수 있다(엔진·장부·local.conf 가 여기 있다).
PM_HOME_MARKER = ".project_manager"


def registry_path(override: Path | str | None = None, *, os_name: str = os.name) -> Path:
    """등록부 파일 경로 — `--registry` 가 있으면 그것, 없으면 사용자 설정 폴더."""
    if override is not None:
        return Path(override).expanduser().resolve()
    if os_name == "nt":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            raise CallError("invalid-path",
                            "APPDATA 가 비어 있어 등록부 위치를 정할 수 없다 — `--registry <경로>` 로 지정하라.")
        base = Path(appdata)
    else:
        base = Path.home() / ".config"
    return base / REGISTRY_DIRECTORY_NAME / REGISTRY_FILE_NAME


def load(registry: Path | str | None = None) -> list[Path]:
    """등록된 PM 홈 목록. 파일이 없으면 빈 목록(등록 전 상태는 정상 형상)."""
    path = registry_path(registry)
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8")
    try:
        entries = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CallError("invalid-path",
                        f"등록부가 JSON 이 아니다: {path} ({exc})") from exc
    if not isinstance(entries, list) or any(not isinstance(entry, str) for entry in entries):
        raise CallError("invalid-path",
                        f"등록부는 PM 홈 절대경로 문자열의 배열이어야 한다: {path}")
    return [Path(entry) for entry in entries]


def add(path: Path | str, registry: Path | str | None = None) -> list[Path]:
    """PM 홈을 등록한다 — `<path>/.project_manager/` 가 있어야 하고 중복은 no-op."""
    home = Path(path).expanduser().resolve()
    if not (home / PM_HOME_MARKER).is_dir():
        raise CallError("invalid-path",
                        f"PM 홈이 아니다 ({PM_HOME_MARKER}/ 없음): {home}")
    entries = load(registry)
    if home in entries:
        return entries
    entries.append(home)
    _write(entries, registry)
    return entries


def remove(path: Path | str, registry: Path | str | None = None) -> list[Path]:
    """등록을 해제한다 — 프로젝트 폴더는 건드리지 않는다."""
    home = Path(path).expanduser().resolve()
    entries = load(registry)
    remaining = [entry for entry in entries if entry != home]
    if len(remaining) != len(entries):
        _write(remaining, registry)
    return remaining


def _write(entries: list[Path], registry: Path | str | None = None) -> None:
    """같은 디렉터리 tmp 에 쓴 뒤 `os.replace` 로 교체한다 — 반쯤 쓰인 등록부를 남기지 않는다."""
    path = registry_path(registry)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps([str(entry) for entry in entries], ensure_ascii=False, indent=2) + "\n"
    temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    temporary.write_text(payload, encoding="utf-8")
    try:
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
