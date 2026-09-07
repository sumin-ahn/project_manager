"""번들 실행 파일의 진입 스크립트 — PyInstaller 가 여는 파일 하나.

PyInstaller 는 넘겨받은 스크립트를 **패키지 문맥 없이** `__main__` 으로 실행한다.
`manager/__main__.py` 를 그대로 주면 그 안의 `from . import deps` 가
`ImportError: attempted relative import with no known parent package` 로 죽는다(실측).
그래서 번들 진입은 패키지를 정상 import 하는 이 파일이고, 명령줄 실행
(`python3 -m manager`)은 그대로 `manager/__main__.py` 다 — 플래그 파싱은 한 곳에만 있다.
"""
from __future__ import annotations

import sys

from manager.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
