# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — 관리자 실행 파일 하나(onefile).

`python3 manager/build.py` 가 부른다(직접 `pyinstaller manager/manager.spec` 로도 돈다).

이 파일이 정하는 것은 셋뿐이고, 값은 전부 `manager/build.py` 의 순수 함수에서 온다(번들 배치의
단일 진실 · 여기에 경로 문자열을 다시 적지 않는다):

  - 진입 스크립트 = `manager/entrypoint.py` (`manager/__main__.py` 를 그대로 주면 패키지 문맥이
    없어 상대 import 가 죽는다 — 그 사유는 진입 스크립트의 docstring 에 있다).
  - 번들 데이터 = `build.bundle_data_entries()` — **번들 안 상대 경로가 소스 상대 경로와 같다**.
  - 숨은 import = `build.hidden_imports()` — 데이터로 실리는 엔진 파일의 최상위 import(정적 분석
    밖)를 그 파일들에서 뽑은 값 + 각인 모듈. 이름을 여기에 다시 적지 않는다.
  - 이름 = `build.executable_stem()` (`pm-manager-<os>-<arch>` · Windows 는 PyInstaller 가 `.exe`).

콘솔을 붙인 채로 만든다(`console=True`): 의존이 없으면 안내 두 줄을 찍고 rc 3 으로 멈추는 것이
이 앱의 계약이고, `--version`·`--smoke` 도 표준 출력으로 답한다. 파이썬 인터프리터는 번들하지
않는다 — 프로젝트 사본과 `pm_import --new` 는 OS 기본 인터프리터로 돈다.
"""
import importlib.util
import os
from pathlib import Path

_SPEC_DIR = Path(SPECPATH).resolve()  # noqa: F821 — PyInstaller 가 넣어 주는 이름
_REPO_ROOT = _SPEC_DIR.parent


def _load_build_module():
    """`manager/build.py` 의 순수 함수 — 패키지를 import 하지 않고 파일 하나만 읽는다."""
    location = _SPEC_DIR / "build.py"
    spec = importlib.util.spec_from_file_location("_manager_build_rules", location)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build = _load_build_module()

analysis = Analysis(  # noqa: F821
    [str(build.ENTRY_SCRIPT)],
    pathex=[str(_REPO_ROOT)],
    hiddenimports=list(build.hidden_imports(_REPO_ROOT)),
    noarchive=False,
)
for source, prefix in build.bundle_data_entries(_REPO_ROOT):
    analysis.datas += Tree(  # noqa: F821
        source, prefix=os.path.normpath(prefix), excludes=list(build.BUNDLE_EXCLUDES))

pyz = PYZ(analysis.pure)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    name=build.executable_stem(),
    icon=str(build.icon_path(_REPO_ROOT, build.EXECUTABLE_ICON)),
    console=True,
    strip=False,
    upx=False,
)
