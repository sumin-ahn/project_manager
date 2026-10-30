"""Linux 독 아이콘 등록 — 창 이름 고정과 `.desktop` 파일·아이콘 파일 두기.

GNOME Shell 은 창의 `WM_CLASS` 와 맞는 `.desktop` 파일에서 아이콘을 찾고, 맞는 파일이 없으면
기본 아이콘을 쓴다. 창 이름은 GTK 를 처음 초기화할 때의 프로그램 이름으로 정해지고 실행 방식에
따라 달라지므로(`python3 -m manager` 는 `__main__.py`, 실행 파일은 그 파일 이름), 프로그램 이름을
`APP_ID` 로 고정하고 같은 이름의 `.desktop` 파일과 아이콘을 사용자 데이터 폴더에 둔다.

관리자가 직접 여는 쓰기 핸들은 등록부(`registry.py`)와 이 모듈, 둘이다. 이 모듈이 쓰는 것은
사용자 데이터 폴더의 두 파일뿐이고 프로젝트 폴더에는 쓰지 않는다.

`sys.executable` 을 읽는 자리도 이 모듈의 `register` 하나다. 등록 파일의 실행 명령은 관리자
자신을 다시 띄우는 명령이라 그 값이 관리자 자신(번들이면 실행 파일, 소스면 그 인터프리터)이어야
한다. 엔진을 부를 인터프리터로는 쓰지 않는다.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from . import REPO_ROOT

APP_ID = "pm-manager"  # 프로그램 이름 · 등록 파일 이름 · StartupWMClass · Icon · 아이콘 파일 이름
ENTRY_DIRECTORY = "applications"
ICON_DIRECTORY = Path("icons") / "hicolor" / "256x256" / "apps"
FILE_MODE = 0o644
EXEC_QUOTED_SPECIALS = '"`$\\'


def set_program_name() -> None:
    """GLib 프로그램 이름을 `APP_ID` 로 정한다 — GTK 를 처음 초기화하기 전에 불러야 한다."""
    from gi.repository import GLib

    GLib.set_prgname(APP_ID)


def data_home() -> Path:
    """사용자 데이터 폴더 — `XDG_DATA_HOME` 이 절대경로면 그 값, 아니면 `~/.local/share`."""
    configured = os.environ.get("XDG_DATA_HOME", "")
    if configured and Path(configured).is_absolute():
        return Path(configured)
    return Path.home() / ".local" / "share"


def launch_command(*, executable: str, frozen: bool,
                   repo_root: Path) -> tuple[list[str], Path | None]:
    """관리자를 다시 띄우는 (명령, 작업 폴더). 번들은 실행 파일 하나, 소스는 `-m manager`."""
    if frozen:
        return [executable], None
    return [executable, "-m", "manager"], repo_root


def _quote_exec_program(program: str) -> str:
    """`Exec` 의 실행 파일 경로 — 큰따옴표로 감싸고 특수 문자 앞에 `\\` 를, `%` 는 `%%` 로."""
    escaped = "".join("\\" + char if char in EXEC_QUOTED_SPECIALS else char for char in program)
    return '"' + escaped.replace("%", "%%") + '"'


def _escape_value(value: str) -> str:
    """`.desktop` 문자열 값 규칙 — `\\` 는 두 번, 줄바꿈·탭·CR 은 기호로."""
    return (value.replace("\\", "\\\\").replace("\n", "\\n")
            .replace("\t", "\\t").replace("\r", "\\r"))


def entry_text(argv: list[str], workdir: Path | None, name: str) -> str:
    """등록 파일 본문. `workdir` 이 없으면 `Path` 줄을 쓰지 않는다."""
    command = " ".join([_quote_exec_program(argv[0]), *argv[1:]])
    lines = ["[Desktop Entry]", "Type=Application", f"Name={_escape_value(name)}",
             f"Exec={_escape_value(command)}"]
    if workdir is not None:
        lines.append(f"Path={_escape_value(str(workdir))}")
    lines += [f"Icon={APP_ID}", f"StartupWMClass={APP_ID}", "Terminal=false",
              "Categories=Development;"]
    return "\n".join(lines) + "\n"


def _write_if_changed(path: Path, content: bytes) -> bool:
    """내용이 다를 때만 같은 폴더의 임시 파일에 쓴 뒤 바꿔 넣는다. 썼으면 True."""
    if path.is_file() and path.read_bytes() == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
        os.chmod(temporary, FILE_MODE)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return True


def register(icon_png: bytes, name: str, data_dir: Path) -> list[Path]:
    """등록 파일과 아이콘 파일을 둔다. 쓴 경로를 [등록 파일, 아이콘 파일] 순서로 돌려준다."""
    argv, workdir = launch_command(
        executable=sys.executable, frozen=hasattr(sys, "_MEIPASS"), repo_root=REPO_ROOT)
    targets = [
        (data_dir / ENTRY_DIRECTORY / f"{APP_ID}.desktop",
         entry_text(argv, workdir, name).encode("utf-8")),
        (data_dir / ICON_DIRECTORY / f"{APP_ID}.png", icon_png),
    ]
    return [path for path, content in targets if _write_if_changed(path, content)]
