"""`python3 -m manager` — 관리자 창 하나.

플래그 셋뿐이다: `--registry <경로>`(등록부 위치 덮어쓰기 · 테스트·게이트용) ·
`--version` · `--smoke`(창을 열어 화면·브리지를 한 번 확인하고 닫는다). 개발용 dev 서버
플래그는 두지 않는다 — 화면은 `npm run build -- --watch` 로 다시 만든다.
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
    parser.add_argument("--version", action="store_true", help="번들 엔진 rev 를 찍고 끝낸다")
    parser.add_argument("--smoke", action="store_true",
                        help="창을 열어 화면·브리지를 한 번 확인하고 닫는다 (사람 없이·rc 0=정상)")
    args = parser.parse_args(argv)

    if args.version:
        print(f"pm-manager {engine_rev()}")
        return 0

    message = deps.missing_renderer_message()
    if message is not None:
        print(message, file=sys.stderr)
        return deps.MISSING_RENDERER_RC

    # 창을 여는 모듈은 여기서 지연 import 한다 — pywebview 가 없을 때도 위 안내가 먼저 나와야 한다.
    if args.smoke:
        from . import smoke
        return smoke.run(registry_file=args.registry)
    from . import window
    return window.open_window(registry_file=args.registry)


if __name__ == "__main__":
    sys.exit(main())
