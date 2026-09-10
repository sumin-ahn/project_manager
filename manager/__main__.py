"""`python3 -m manager` — 관리자 창 하나.

플래그 넷뿐이다: `--registry <경로>`(등록부 위치 덮어쓰기 · 테스트·게이트용) · `--version` ·
`--smoke`(창을 열어 화면·브리지를 한 번 확인하고 닫는다) · `--port <번호>`(같은 프로세스 서버가
설 포트 · 생략하면 빈 포트 자동). 바인딩은 `127.0.0.1` 고정이고 주소를 받는 옵션은 없다.
개발용 dev 서버 플래그는 두지 않는다 — 화면은 `npm run build -- --watch` 로 다시 만든다.
"""
from __future__ import annotations

import argparse
import sys

from . import deps, engine_rev, load_engine_module


def main(argv: list[str] | None = None) -> int:
    load_engine_module("console_encoding.py").configure_console_utf8()
    parser = argparse.ArgumentParser(
        prog="python3 -m manager", description="PM 프로젝트 관리자 (창 하나)")
    parser.add_argument("--registry", metavar="<경로>", default=None,
                        help="등록부(projects.json) 위치 덮어쓰기 (기본: 사용자 설정 폴더)")
    parser.add_argument("--port", metavar="<번호>", type=int, default=0,
                        help="창과 브라우저가 함께 붙는 로컬 서버 포트 (기본: 빈 포트 자동)")
    parser.add_argument("--version", action="store_true", help="번들 엔진 rev 를 찍고 끝낸다")
    parser.add_argument("--smoke", action="store_true",
                        help="창을 열어 화면·브리지를 한 번 확인하고 닫는다 (사람 없이·rc 0=정상)")
    args = parser.parse_args(argv)

    if args.version:
        print(f"pm-manager {engine_rev()}")
        return 0

    message, rc = deps.missing_renderer_message()
    if message is not None:
        print(message)
        return rc

    # 창을 여는 모듈은 여기서 지연 import 한다 — pywebview 가 없을 때도 위 안내가 먼저 나와야 한다.
    if args.smoke:
        from . import smoke
        return smoke.run(registry_file=args.registry, port=args.port)
    from . import window
    return window.open_window(registry_file=args.registry, port=args.port)


if __name__ == "__main__":
    sys.exit(main())
