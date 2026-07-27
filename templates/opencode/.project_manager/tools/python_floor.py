#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""실제 스크립트 디스패치와 동형인 Python 런타임 하한 probe.

Windows ``py`` 런처는 ``py -c``와 ``py <script>``에 서로 다른 Python을 고를 수 있다.
이 파일은 엔진 진입점과 같은 shebang을 가지며 Python 2.7에서도 파싱되므로, 후보가 실제
엔진을 실행할 때 선택할 버전과 같은 디스패치 경로에서 명확한 버전/종료코드를 돌려준다.
"""
from __future__ import print_function

import sys


# engine_rev.MIN_PYTHON 미러. tests/test_python_floor.py가 skew를 차단한다.
MIN_PYTHON = (3, 11)


def main():
    current = sys.version_info[:2]
    print("Python %d.%d" % current)
    return 0 if current >= MIN_PYTHON else 1


if __name__ == "__main__":
    sys.exit(main())
