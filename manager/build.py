#!/usr/bin/env python3
"""관리자 실행 파일 하나를 만든다 — 각인 → 화면 빌드 → 화면 테스트 → PyInstaller.

    python3 manager/build.py        # 산출: dist/pm-manager-<os>-<arch>[.exe]

단계는 넷이고 순서가 있다:

1. **각인** — 빌드 checkout 의 origin URL 을 `manager/_build_info.py`(git-ignored)에 쓴다. 이
   값이 새 프로젝트 적용(`pm_import.py --new --upstream …`)의 기본값이라, 각인 없이 만든 실행
   파일은 새 프로젝트에 갱신 경로를 주지 못한다(번들이 풀린 임시 폴더가 upstream 으로 박힌다).
   URL 은 손으로 박지 않는다 — 엔진 `pm_import.derive_origin_url` 이 읽는 그 값 그대로다.
2. **화면 빌드**(`npm ci` → `npm run build`) — 창이 여는 것은 Vite 산출물 하나다.
3. **화면 테스트**(`npm test`) — 실행 파일에 실을 화면을 먼저 재고 넣는다.
4. **PyInstaller**(`manager/manager.spec`) — onefile 실행 파일 1개.

번들에 실리는 트리와 실행 파일 이름은 이 파일의 순수 함수가 정한다(`manager.spec` 이 그것을
읽는다). 파이썬 인터프리터는 번들하지 않는다 — 프로젝트 사본과 `pm_import --new` 는 OS 기본
인터프리터(또는 그 사본의 `runtime.py`)로 돈다.
"""
from __future__ import annotations

import ast
import importlib.util
import platform
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "manager"
UI_ROOT = PACKAGE_ROOT / "ui"
SPEC_FILE = PACKAGE_ROOT / "manager.spec"
BUILD_INFO_FILE = PACKAGE_ROOT / "_build_info.py"
ENTRY_SCRIPT = PACKAGE_ROOT / "entrypoint.py"
DIST_ROOT = REPO_ROOT / "dist"
WORK_ROOT = REPO_ROOT / "build"

# 번들에 실리는 트리 — 여기 적힌 **소스 상대 경로가 곧 번들 상대 경로**다(경로 분기 0).
# `.project_manager/tools/` 는 관리자가 in-process 로 읽는 엔진 사본, `templates/*` 는
# `pm_import --new` 가 복사할 하네스 트리, `manager/ui/dist` 는 창이 여는 화면이다.
BUNDLED_TREES = (
    ".project_manager/tools",
    "templates/claude_code",
    "templates/codex",
    "templates/opencode",
    "manager/ui/dist",
)
# 번들에 실을 것이 아닌 개발 잔재 — 트리를 훑는 것은 PyInstaller 의 `Tree` 다.
BUNDLE_EXCLUDES = ("__pycache__", "*.pyc", "node_modules")
# 이름 문자열로만 열리는 각인 모듈(`bridge_update.BUILD_INFO_MODULE`) — 소스에 그 import 문이
# 없어 파일에서 뽑을 수 없는 유일한 이름이다.
IMPRINT_MODULE = "manager._build_info"

EXECUTABLE_PREFIX = "pm-manager"
WINDOWS_SUFFIX = ".exe"
# `platform.system()` / `platform.machine()` → 산출 이름의 두 토막. 모르는 값은 이름을 지어내지
# 않고 멈춘다(빌드 대상은 ubuntu x86_64 · windows x86_64 · macos arm64 셋이다).
OS_TAGS = {"Linux": "linux", "Windows": "windows", "Darwin": "macos"}
ARCHITECTURE_TAGS = {
    "x86_64": "x86_64", "AMD64": "x86_64", "amd64": "x86_64",
    "arm64": "arm64", "aarch64": "arm64",
}


def _tag(table: dict[str, str], value: str, axis: str) -> str:
    if value not in table:
        raise ValueError(
            f"빌드 대상이 아닌 {axis}: {value!r} (아는 값: {sorted(table)})")
    return table[value]


def executable_stem(*, system: str | None = None, machine: str | None = None) -> str:
    """확장자 없는 실행 파일 이름 — PyInstaller `EXE(name=…)` 가 쓰는 값."""
    system = platform.system() if system is None else system
    machine = platform.machine() if machine is None else machine
    return (f"{EXECUTABLE_PREFIX}-{_tag(OS_TAGS, system, '운영체제')}"
            f"-{_tag(ARCHITECTURE_TAGS, machine, '아키텍처')}")


def executable_filename(*, system: str | None = None, machine: str | None = None) -> str:
    """릴리즈 자산 이름 — Windows 만 `.exe` 가 붙는다(PyInstaller 가 붙이는 그 확장자)."""
    system = platform.system() if system is None else system
    stem = executable_stem(system=system, machine=machine)
    return stem + WINDOWS_SUFFIX if system == "Windows" else stem


def bundle_data_entries(repo_root: Path | str) -> tuple[tuple[str, str], ...]:
    """번들 데이터 매핑 — `(소스 절대경로, 번들 안 상대 디렉터리)` 쌍.

    번들 안 상대 디렉터리는 소스 상대 경로와 **같다**. 그래서 번들에서도 관리자는 자기
    위치에서 `.project_manager/tools/`·`templates/`·`manager/ui/dist` 를 소스 트리와 똑같이
    찾는다(`bundle_root()` 한 자리만 갈린다).
    """
    root = Path(repo_root)
    return tuple((str(root / relative), relative) for relative in BUNDLED_TREES)


def top_level_import_names(source: str) -> set[str]:
    """파이썬 소스 하나가 **모듈 본문**에서 부르는 import 이름 후보.

    `import a.b` 는 `a.b` 하나, `from a import b` 는 `a` 와 `a.b` 둘이다 — 뒤엣것의 `b` 가
    서브모듈인지 그 모듈의 이름인지는 문법으로 갈리지 않아(`from ctypes import wintypes` ↔
    `from pathlib import Path`) 해소는 부르는 쪽이 한다. 함수 안 import 는 최상위가 아니다.
    """
    names: set[str] = set()
    for node in ast.parse(source).body:
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def _is_standard_library_module(name: str) -> bool:
    """그 이름이 실제 표준 라이브러리 **모듈**인가 (이름 후보를 여기서 거른다)."""
    if name.split(".")[0] not in sys.stdlib_module_names:
        return False
    try:
        return importlib.util.find_spec(name) is not None
    except ModuleNotFoundError:
        # 부모가 패키지가 아니다 = 서브모듈이 아니라 그 모듈이 내보내는 이름(`pathlib.Path`).
        return False


def bundled_engine_imports(repo_root: Path | str,
                           trees: tuple[str, ...] = BUNDLED_TREES) -> tuple[str, ...]:
    """번들 트리의 `.py` 가 최상위에서 부르는 표준 라이브러리 모듈 이름 전부(정렬).

    그 파일들은 모듈이 아니라 **데이터**로 실리고(`BUNDLED_TREES`) 런타임에 `load_engine_module`
    이 exec 하므로, 그 import 는 PyInstaller 정적 분석이 보지 못한다. 그래서 이름으로 세워야
    하는데 목록을 손으로 적으면 반드시 뒤처진다 — 파일에서 뽑는다.

    표준 라이브러리만인 것은 인터프리터 합의다: 실행 파일에 엔진의 런타임 의존(PyYAML)은 들어
    가지 않는다. 그것을 부르는 엔진 파일(`board.py`·`pm_adr.py`)은 관리자가 in-process 로 열지
    않고 프로젝트 사본의 인터프리터가 서브프로세스로 부른다.
    """
    root = Path(repo_root)
    candidates: set[str] = set()
    for relative in trees:
        for source_file in sorted((root / relative).rglob("*.py")):
            candidates |= top_level_import_names(source_file.read_text(encoding="utf-8"))
    return tuple(sorted(name for name in candidates if _is_standard_library_module(name)))


def hidden_imports(repo_root: Path | str) -> tuple[str, ...]:
    """PyInstaller `hiddenimports` — 번들 엔진 파일의 최상위 import + 각인 모듈."""
    return (*bundled_engine_imports(repo_root), IMPRINT_MODULE)


def build_info_source(upstream_url: str) -> str:
    """각인 모듈 `manager/_build_info.py` 의 내용 — 읽는 곳이 있는 값 하나뿐이다.

    관리자 버전 표기의 단일 진실은 번들 엔진의 rev(`manager.engine_rev()`)라 여기 없다.
    """
    return (
        '"""빌드가 각인한 값 — 이 파일은 `manager/build.py` 가 만든다(git-ignored·손으로 고치지 않는다)."""\n'
        f"UPSTREAM_URL = {upstream_url!r}\n"
    )


def _engine_module(filename: str):
    """번들 엔진 모듈 하나 — 관리자와 같은 로더를 지난다(경로 import 를 두 번 쓰지 않는다)."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from manager import load_engine_module

    return load_engine_module(filename)


def imprint_build_info() -> Path:
    """빌드 checkout 의 origin URL 을 각인한다."""
    upstream_url = _engine_module("pm_import.py").derive_origin_url(REPO_ROOT)
    if not upstream_url:
        raise SystemExit(
            f"[중단] 빌드 checkout 의 origin 을 읽지 못했다: {REPO_ROOT}\n"
            "  각인 없이 만든 실행 파일은 새 프로젝트에 갱신 경로를 주지 못한다 "
            "(번들이 풀린 임시 폴더가 upstream 으로 박힌다).")
    BUILD_INFO_FILE.write_text(build_info_source(upstream_url), encoding="utf-8")
    print(f"[build] 각인 {BUILD_INFO_FILE.name}: {upstream_url}", flush=True)
    return BUILD_INFO_FILE


def run(argv: list[str], *, cwd: Path) -> None:
    """단계 하나 — 출력은 그대로 흘리고, 실패하면 그 rc 로 멈춘다."""
    print(f"[build] {' '.join(argv)}  (cwd={cwd})", flush=True)
    completed = subprocess.run(argv, cwd=str(cwd))
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)


def npm(*arguments: str) -> list[str]:
    """`npm` 한 줄 — Windows 의 `npm.cmd` 는 PATH 검색으로만 찾아진다."""
    executable = shutil.which("npm")
    if executable is None:
        raise SystemExit("[중단] npm 을 찾지 못했다 — 화면(manager/ui)은 Node 툴체인으로 만든다.")
    return [executable, *arguments]


def run_pyinstaller() -> None:
    """PyInstaller 를 **이 인터프리터 안에서** 부른다 — 실패는 그대로 올라온다.

    서브프로세스로 부르려면 자기 인터프리터 경로(`sys.executable`)를 써야 하는데, 그 이름은
    관리자 패키지 어디에도 두지 않는다(번들에서 그것은 관리자 실행 파일 자신이고, 회귀가
    패키지 전수로 그 이름을 막는다). in-process 호출은 그 축을 아예 만들지 않는다.
    """
    import PyInstaller.__main__

    print(f"[build] pyinstaller {SPEC_FILE}", flush=True)
    PyInstaller.__main__.run([str(SPEC_FILE), "--noconfirm",
                              "--distpath", str(DIST_ROOT), "--workpath", str(WORK_ROOT)])


def main() -> int:
    imprint_build_info()
    try:
        run(npm("ci"), cwd=UI_ROOT)
        run(npm("run", "build"), cwd=UI_ROOT)
        run(npm("test"), cwd=UI_ROOT)
        run_pyinstaller()
    finally:
        # 각인은 **빌드 시각의 입력**이다. 소스 트리에 남기면 그 트리에서 `python3 -m manager` 를
        # 돌릴 때 라이브 origin 대신 각인값이 이긴다(그 자리는 번들 안에만 있어야 한다).
        BUILD_INFO_FILE.unlink(missing_ok=True)

    artifact = DIST_ROOT / executable_filename()
    if not artifact.is_file():
        raise SystemExit(f"[중단] 실행 파일이 만들어지지 않았다: {artifact}")
    print(f"[build] 실행 파일 {artifact} ({artifact.stat().st_size / 1024 / 1024:.1f} MiB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
