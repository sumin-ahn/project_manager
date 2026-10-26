"""채택자 lint 훅 실행 (층: app).

`.project_manager/hooks/lint_*.py` 의 `check()` 를 불러 이슈 목록을 모은다. 엔진 밖 파일을 경로로 적재하는
패키지 안 유일한 곳이다(`tests/test_engine_structure.py` 의 경로 적재 허용 지점).
"""
from __future__ import annotations

import sys

from pm_engine.infra import pm_paths, repo_files


def _run_lint_hooks(home) -> list[tuple[str, str]]:
    """Discover & run instance lint hooks — .project_manager/hooks/lint_*.py.

    각 훅 모듈은 `check() -> list[str]`(이슈 detail 문자열)을 노출한다. fail-soft:
    로드/실행 실패·규격 불충족은 stderr 경고로 보고하고 계속한다(부분 실패가 lint 전체를
    막지 않음). 인스턴스가 엔진 코드를 안 건드리고 자기 검사를 더하는 seam — 프레임워크
    공통 검사(wikilink 등)는 엔진 내장(lint_wikilinks), 프로젝트 고유 검사는 여기로.
    """
    if not pm_paths.hooks_dir(home).is_dir():
        return []
    out: list[tuple[str, str]] = []
    for hook in sorted(pm_paths.hooks_dir(home).glob("lint_*.py")):
        try:
            mod = repo_files.load_module(hook, hook.name)
            check = getattr(mod, "check", None)
            if not callable(check):
                print(f"⚠️  lint hook {hook.name}: check() 미정의 — 건너뜀", file=sys.stderr)
                continue
            for detail in (check() or []):
                out.append((hook.stem, str(detail)))
        except Exception as exc:  # noqa: BLE001 — fail-soft: 한 훅 실패가 lint 를 막지 않음
            print(f"⚠️  lint hook {hook.name} 로드/실행 실패: {exc}", file=sys.stderr)
    return out
