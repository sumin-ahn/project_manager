#!/usr/bin/env python3
"""local.conf 키 표 CLI 진입 파일. 구현은 pm_engine 에 있다.

`--keys`·`--keys --json`·`--render-adapter-block <python|js|sh>` 를 `pm_engine.cli.local_conf.main` 에
넘긴다. 관리자는 프로젝트 사본마다 이 파일을 `--keys --json` 으로 부른다.
"""
import os
import sys

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
from pm_engine.cli import local_conf as cli  # noqa: E402
from pm_engine.infra import console  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    console.configure_console_utf8()
    return cli.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
