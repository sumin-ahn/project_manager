"""테스트 픽스처용 canonical pytest 요약행 — 엔진 파서 문법의 단일 출처.

엔진 파서(`board._pytest_summary_line`)는 요약행을 **문법 완전 일치**로 고른다: 줄머리에서
outcome 목록이 시작하고 `in <초>s` 로 끝나야 한다. 그 종결 앵커가 있어야 실제 요약 *뒤에* 붙는
자식 하네스 로그(`child harness: 5 passed in 1.00s`)를 요약으로 오인하지 않는다.

픽스처가 `"1 passed"` 같은 축약 형태를 쓰면 그 순간은 통과하더라도(그 경로가 파서를 안 타면)
소비 경로가 파서를 타는 순간 무더기 red 가 되는 시한 픽스처가 된다. 형식을 여기 한 곳에 두고
픽스처가 소비하게 해 그 재발을 끊는다 — 이 헬퍼 산출이 엔진 문법을 만족한다는 사실 자체는
`test_board_regression.py` 가 못 박는다(헬퍼↔문법 합치).
"""
from __future__ import annotations

_DEFAULT_SECONDS = 0.01


def pytest_summary(passed: int = 1, *, failed: int = 0, skipped: int = 0,
                   deselected: int = 0, seconds: float = _DEFAULT_SECONDS,
                   newline: bool = True) -> str:
    """canonical `pytest -q` 요약행 — 예: `5 failed, 1467 passed, 24 deselected in 0.01s`.

    항목 순서는 실 pytest 표기를 따른다(failed → passed → skipped → deselected). 0 인 항목은
    싣지 않는다(실 pytest 동형). `newline=False` 면 개행 없이 줄만.
    """
    items: list[str] = []
    if failed:
        items.append(f"{failed} failed")
    items.append(f"{passed} passed")
    if skipped:
        items.append(f"{skipped} skipped")
    if deselected:
        items.append(f"{deselected} deselected")
    line = f"{', '.join(items)} in {seconds:.2f}s"
    return f"{line}\n" if newline else line
