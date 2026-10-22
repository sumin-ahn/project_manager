"""추가 리뷰어 예산 설정 키 (층: domain)."""
from __future__ import annotations


# 엔진 기본 리뷰어 커맨드는 없다 — 대상은 구조화 tuple(`additional_reviewer.{harness,model}`)
# 해소 결과뿐이고, 모델을 고정하지 않는 실행 경로를 남기지 않는다.

# 리뷰어 호출의 시간 예산(무진행 상한 + 벽시계 백스톱)은 **해소된 대상의 하네스 프로필**이 소유한다
# (`watchdog.HARNESS_PROFILES` — legacy 기본 command가 `codex exec`이면 codex 축 값). 이 모듈에
# 별도 타임아웃 상수를 두지 않는 이유가 이 티켓의 편입 이유와 같다: **값이 두 군데면 규칙이 둘이
# 된다.** 평범한 diff 153~294초·13파일 대형 227초 — 구 기본 180초는 평상 대역 *안*이라 상시 타임아웃
# 구조였고, 900 으로 올려도 같은 구조였다(실측: **입력·모델이 완전히 동일한 리뷰 2회 중 하나는
# 900초 초과 kill·다른 하나는 `--timeout 1500` 으로 성공**). 그래서 값을 고르는 게임을 끝내고
# 판정 기준을 무진행으로 교체했고, 벽시계는 "감지기 자체가 고장난 경우"의 유한 상한으로 강등된다.
# 조정: 일회성 `--timeout`/`--idle-timeout` > local.conf `harness.<reviewer>.wall_timeout`/
# `.idle_timeout` > 아래 표면-flat legacy 키 > 프로필 선언.
# 게이트·예산 노브 키. 이름은 사람 역할 이름(추가 리뷰어) 축 하나로 수렴한다 — 모듈 파일명·raw
# 파일 접두는 기존 감사물의 안정 계약이라 기계 식별자로 그대로 남는다. **구키 감지 상수는 없다**:
# 구표기 잔존 판정은 공용 로더(`local_conf.assert_no_legacy`)가 conf 를 읽는 지점에서 한 번에
# 하고, 이 모듈이 그 표의 사본을 들지 않는다.
ADDITIONAL_REVIEWER_INCOMPLETE_ROUND_LIMIT_KEY = (
    "additional_reviewer.incomplete_rounds_max")
ADDITIONAL_REVIEWER_WAVE_BUDGET_KEY = "additional_reviewer.wave_budget"

ADDITIONAL_REVIEWER_TIMEOUT_KEY = "additional_reviewer.timeout"
ADDITIONAL_REVIEWER_IDLE_TIMEOUT_KEY = "additional_reviewer.idle_timeout"
ADDITIONAL_REVIEWER_PROGRESS_SIGNAL_KEY = "additional_reviewer.progress_signal"

# 알려진 reviewer CLI의 **실행 파일 + 옵션 계약**. 함수 밖 선언이라 새 CLI/형식 추가가 판정 코드
# 분기로 번지지 않는다. attr 값은 `conf_schema` 의 진행 신호 상수명이다.
_REVIEWER_PROGRESS_CONTRACTS = {
    "codex": {
        "default": "PROGRESS_SIGNAL_PLAINTEXT",
        "flags": {"--json": "PROGRESS_SIGNAL_EVENT_STREAM"},
        "options": {},
    },
    "claude": {
        "default": "PROGRESS_SIGNAL_NONE",
        "flags": {},
        "options": {
            ("--output-format", "stream-json"): "PROGRESS_SIGNAL_EVENT_STREAM",
        },
    },
    "opencode": {
        "default": "PROGRESS_SIGNAL_NONE",
        "flags": {},
        "options": {
            ("--format", "json"): "PROGRESS_SIGNAL_EVENT_STREAM",
        },
    },
}

# 미완 라운드 상한 — 판정 없이 끝난 호출(타임아웃·중단·오염 무효화)이 같은 게이트에서 상한
# 횟수를 채우면 이후 실행을 거부한다. 호출 횟수만 세던 판정 라운드 상한은 같은 범위를 보는 수렴
# 축과 중복이라 제거됐다(실 장부 53게이트에서 한 번도 발동하지 않았고, 발산은 수렴 축이 더 일찍
# 잡는다). 상한 값의 정본은 레지스트리다(`local_conf` · 아래 예산 노브 전부 동형).

# 수렴-형상 상한 — 코드 리뷰 라운드 수 상한(local.conf `additional_reviewer.rounds_max`).
# 이 축은 호출 횟수가 아니라 **장부의 must_fix 추이**로 수렴 여부를
# 본다: 상한을 넘겼거나(초과), 직전 라운드보다 must_fix 가 늘었으면(발산) 라운드를 더 쓰지 않는다.
# 실측에서 green 6건 중 5건이 2R 종결이고 관측된 3R 시도 8건은 모두 반려였다.
REVIEW_ROUNDS_MAX_KEY = "additional_reviewer.rounds_max"
