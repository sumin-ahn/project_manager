"""pytest 출력 요약행 판독 (층: domain).

요약행 문법·outcome 수·실행 수·직전 기록 대비 수집 증감 문장. 출력 문자열만 읽는다.
"""
from __future__ import annotations

import re


# ── FULL 게이트 수집 증감 (직전 기록 대비 · 막지 않는다) ─────────────────
# 채택자가 선언하던 수집 하한은 폐지했다 — 스위트 규모의 함수라 선언값이
# 실측의 절반에 머물러 스위트 절반이 사라져도 통과했다(느슨한 안심). 대신 직전 회귀 기록의
# 수집수와 이번 수집수의 증감을 회귀 줄에 함께 낸다. 변화율이 `COLLECTION_DELTA_ALERT_PCT`
# 이상이면 눈에 띄게 표시하되 **rc 는 바꾸지 않는다** — 적절한 변경인지는 사람이 판단한다.
# rc5(수집 0)는 그대로 red 다(하한이 아니라 "테스트가 한 개도 안 돌았다"는 별개 사실).
COLLECTION_DELTA_ALERT_PCT = 0.01          # 이 비율 이상이면 증감 문장에 경고 표기를 단다.

# ── pytest 요약행 파서 (엔진 공용 seam) ────────────────────────────────────
# 요약행은 **끝에서부터** 줄 단위로 찾는다. 캡처된 pytest 출력에는 테스트 자신이 찍은
# `3 passed` 류 로그가 섞이고, 출력 전체를 `re.search` 하면 그 로그를 요약으로 오판한다.
# 도구마다 복제돼 있던 첫-매칭 파서를 여기로 승격했다 — 수집 증감 표시·릴리즈 라이브 게이트
# ·부트스트랩 회귀 dump·ticket 마감 회귀 판정·핸드오프 회귀 1줄이 모두 이 seam 을
# 쓴다. 다른 도구는 이 모듈을 import 해 읽는다 — **로컬 사본 금지**(사본은
# 첫-매칭 오판을 그대로 되살린다).
#
# 오염은 **양방향**이다: 요약 *앞* 로그는 끝에서-탐색이, 요약 *뒤* 로그(소비처 넷은 stdout+
# stderr 를 병합해 먹인다 — 자식 하네스의 stderr 한 줄이 꼬리에 붙는다)는 요약행 문법 완전
# 일치가 막는다. 두 축 모두 이 seam 안에 있어 소비처가 자동 상속한다.

# pytest 요약행 outcome 종류 — 실행 수 = 이 값들의 합. `deselected`(quarantine `--deselect`·
# 마커 필터)는 *돌지 않은* 수라 세지 않는다. `xfailed`/`xpassed` 는 패턴이 `<수> <종류>` 라
# `failed`/`passed` 에 겹쳐 잡히지 않는다(각자 독립 카운트).
_PYTEST_OUTCOME_KINDS = ("passed", "failed", "errors?", "skipped", "xfailed", "xpassed")


def _pytest_outcome_match(line: str, kind: str) -> re.Match[str] | None:
    """요약행에서 outcome 한 종류(`N passed`)의 정규식 매치 — 없으면 None.

    `kind` 는 pytest 표기 그대로의 패턴 조각이라 단/복수 겸용(`errors?`)도 그대로 받는다.
    이 한 지점이 outcome 표기의 단일 정의다(카운트·요약 문자열 절단이 함께 쓴다).
    """
    return re.search(rf"(\d+) {kind}\b", line or "")


def _pytest_outcome_count(line: str, kind: str) -> int | None:
    """요약행에서 outcome 한 종류의 수 — 그 종류가 없으면 None(수 0 과 구분)."""
    match = _pytest_outcome_match(line, kind)
    return int(match.group(1)) if match else None


def _pytest_outcome_counts(line: str,
                           kinds: tuple[str, ...] = _PYTEST_OUTCOME_KINDS) -> list[int]:
    """한 줄에서 pytest outcome 카운트를 뽑는다 (`7577 passed, 12 skipped …` → [7577, 12]).

    `kinds` 로 셀 종류를 좁힌다 — 소비처마다 *무엇을 실행으로 보는지* 가 다르다(수집 증감 표시는
    6종 전부·릴리즈 라이브 게이트는 마커 안에서 돈 3종).
    """
    counts: list[int] = []
    for kind in kinds:
        count = _pytest_outcome_count(line, kind)
        if count is not None:
            counts.append(count)
    return counts


# 요약행 **전체 문법** — outcome 목록 + `in <소요시간>` 종결. 끝에서-탐색만으로는 부족하다:
# 실제 요약 *뒤에* wrapper/자식 하네스가 `child harness: 5 passed in 1.00s` 류를 찍으면(소비처는
# stdout+stderr 병합 출력을 먹인다) 그게 마지막 카운트 줄이라 요약으로 뽑혀 증감·릴리즈 pin 이
# 우회된다. 실측된 그 오염 줄은 `in X.XXs` 종결까지 갖췄으므로 **줄머리 앵커가 필수**다 — 판정을
# "카운트를 포함한다"가 아니라 "요약행 문법에 완전히 일치한다"로 세운다. 허용 형태: 구분선
# 장식(`==== … ====`)·긴 실행의 `(0:06:28)` 꼬리·구 pytest 의 `in 0.30 seconds`.
_PYTEST_SUMMARY_ITEM = r"\d+ [a-z]+"
_PYTEST_SUMMARY_RE = re.compile(
    r"^=*\s*"                                                       # 선택적 구분선
    rf"{_PYTEST_SUMMARY_ITEM}(?:\s*,\s*{_PYTEST_SUMMARY_ITEM})*"    # `N kind`(, `N kind`)*
    r"\s+in\s+\d+(?:\.\d+)?\s*(?:s|seconds?)"                       # 소요시간 종결 앵커
    r"(?:\s*\(\d+:\d{2}:\d{2}\))?"                                  # 긴 실행 `(0:06:28)`
    r"\s*=*\s*$"
)

# 색 출력(`--color=yes`·`PY_COLORS=1`)은 요약행에 ANSI 를 섞는다 — 문법 판정 전에 걷어낸다
# (안 걷으면 정상 요약이 문법 불일치로 떨어져 `unverified-collection` false-RED 가 된다).
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _is_pytest_summary_line(line: str) -> bool:
    """그 줄이 pytest 요약행 문법에 **완전히** 일치하나 (카운트 포함 여부가 아니다)."""
    return bool(_PYTEST_SUMMARY_RE.match(_ANSI_ESCAPE_RE.sub("", line or "").strip()))


def _pytest_summary_line(output: str) -> str | None:
    """pytest 출력의 요약행 — **끝에서부터** 첫 *요약행 문법* 줄 (없으면 None).

    두 겹으로 거른다:
      1. **끝에서-탐색** — 캡처 출력에는 테스트/자식 러너가 찍은 `3 passed` 류 로그가 섞이고,
         앞에서부터 찾으면 그 로그를 요약으로 오판한다.
      2. **문법 완전 일치**(`_is_pytest_summary_line`) — 실제 요약 *뒤에* 오는 wrapper 로그
         (`[post] 8000 passed in 900s`)까지 끝에서-탐색이 집어삼키는 구멍을 닫는다. 카운트
         나열 + `in <초>s` 종결이라는 요약행 고유 형태를 만족해야 한다.
    """
    for line in reversed((output or "").splitlines()):
        if _is_pytest_summary_line(line) and _pytest_outcome_counts(line):
            return line.strip()
    return None


def _collected_count(output: str) -> int | None:
    """pytest 요약행에서 *실제 실행된* 테스트 수를 센다 (파싱 실패 시 None).

    N = passed + failed + error(s) + skipped + xfailed + xpassed. 요약행이 없으면
    (수집 0 "no tests ran"·비-pytest test_cmd) None — 그때는 증감 문장이 비고 판정은
    현행 rc 신호에 맡긴다(fail-soft).
    """
    line = _pytest_summary_line(output)
    if line is None:
        return None
    return sum(_pytest_outcome_counts(line))


def _pytest_summary_tail(output: str) -> str | None:
    """요약행의 `N passed` 부터 줄 끝까지 (요약행/그 표기가 없으면 None).

    사람이 읽는 회귀 1줄 표기(인계 프롬프트·log entry)가 소비한다 — 줄 전체가 아니라 카운트
    지점부터 잘라 쓰던 표기를 보존하되, 자를 대상 줄은 끝에서-탐색으로 고른다.
    """
    line = _pytest_summary_line(output)
    if line is None:
        return None
    match = _pytest_outcome_match(line, "passed")
    return line[match.start():].strip() if match else None


def collection_delta_text(collected: int | None, previous: int | None) -> str:
    """직전 회귀 기록 대비 수집수 증감 문장 — ` (직전 N · +D[ · P% ⚠])` (비교 불가면 빈 문자열).

    막는 판정이 아니라 표시다. 스위트가 갑자기 줄었는지(수집 경로 파손·대량 skip) 늘었는지를
    회귀 줄에서 바로 보고 사람이 판단한다 — 어떤 증감도 rc 를 바꾸지 않는다.

    둘 중 하나라도 정수가 아니면(첫 실행·기록 손상·수집수 파싱 실패) 빈 문자열이다. 증감을
    지어내지 않는다. 직전이 0 이면 비율을 내지 않고 차이만 낸다(0 나눗셈 금지). 변화율이
    `COLLECTION_DELTA_ALERT_PCT` 이상이면 비율과 경고 표기를 덧붙인다.
    """
    if not isinstance(collected, int) or isinstance(collected, bool):
        return ""
    if not isinstance(previous, int) or isinstance(previous, bool):
        return ""
    diff = collected - previous
    body = f" (직전 {previous} · {diff:+d}"
    if previous and abs(diff) / previous >= COLLECTION_DELTA_ALERT_PCT:
        return body + f" · {abs(diff) / previous * 100:.1f}% ⚠)"
    return body + ")"


# 라이브 게이트의 "실행 수" 로 세는 outcome 종류 — 마커 안에서 *돈* 케이스만.
# `deselected`(release 마커 밖)·`skipped` 는 실행분이 아니라 제외한다.
_LIVEGATE_RAN_KINDS = ("passed", "failed", "errors?")


def _livegate_ran_count(output: str) -> int:
    """`pytest -m release -q` 요약행에서 *실제 실행된* release 테스트 수(수집 N)를 센다.

    N = passed + failed + error(s). deselected 는 release 마커 밖이라 세지 않는다. "no tests
    ran"(exit5)처럼 요약에 카운트가 없으면 0. 이 수집 N 을 pin 과 대조해 마커 소실·wrong-cwd
    로 인한 false-green(수집 위장)을 차단한다 — rc0 만으로는 "0개 수집됐지만 red 아님"을
    green 으로 삼킬 수 있다. 대상 줄은 공용 seam 이 **끝에서** 찾는다(라이브 wave 가 캡처
    출력에 찍는 `N passed` 로그를 요약으로 오판하면 pin 대조가 통째로 어긋난다).
    """
    line = _pytest_summary_line(output)
    if line is None:
        return 0
    return sum(_pytest_outcome_counts(line, _LIVEGATE_RAN_KINDS))
