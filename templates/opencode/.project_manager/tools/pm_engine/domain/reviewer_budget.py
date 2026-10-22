"""추가 리뷰어 예산 설정 키 (층: domain)."""
from __future__ import annotations

from typing import Sequence
from pm_engine.domain import review_form
from pm_engine.domain import review_state


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


# generic 맥락 헤더 (review_context.local.md 부재 시)
_AUTHORITATIVE_TICKET_CONTEXT = (
    "티켓 §결정·§설계·PM 판정 블록은 **권위 있는 확정 사항**이다 — 그 결정을 되돌리라는 지적은 "
    "`design-proposal` 로 분류하고 must-fix 로 내지 마라. must-fix 는 티켓 목표·결정 안에서의 "
    "결함에 한한다."
)

_DEFAULT_CONTEXT_HEADER = f"""\
## 리뷰 맥락

리뷰 입력 좌표의 명령으로 diff를 직접 읽고 코드리뷰하라. 프로젝트 고유 맥락(`.project_manager/review_context.local.md`)이
설정돼 있으면 그 기준을 우선한다.

{_AUTHORITATIVE_TICKET_CONTEXT}
"""

# 출력 형식 블록 (parse_verdict 가 의존 — 리뷰어 무관 공통)
_OUTPUT_FORMAT_BLOCK = """\
### 출력 형식 (필수)
아래 형식으로 응답하라:

판정: [통과 | 반려]

**must-fix** (반드시 수정):
- (없으면 "없음" · 있으면 아래 블록의 finding ID 만 나열한다)

**suggestion** (권장):
- (없으면 "없음" · 있으면 아래 블록의 finding ID 만 나열한다)

"""

# 구조화 블록 요구 — 스키마는 엔진 파서 상수에서만 파생한다(여기서 다시 적지 않는다).
_VERSIONED_BLOCK_HEADER = """\
### 구조화 판정 블록 (필수)
위 산문 뒤에 아래 스키마의 블록을 **정확히 하나** 출력하라. 엔진이 이 회신 전문을 게이트 티켓의
`additional-reviewer` 라운드 파일로 회수하고, PM 은 이 블록으로 판정한다 — 증거·권고·심각도는 블록이
단일 진실이므로 산문에 같은 항목을 다시 서술하지 마라.

"""
_VERSIONED_BLOCK_RULES = """\
- `id` 는 이 채널 전용 접두 `{prefix}-` 를 쓰고 **이번 라운드는 {next_id} 부터** 매긴다 —
  티켓 전역 유일이라 본문에 이미 있는 ID 를 다시 쓰면 이 라운드는 회수되지 않는다.
- `severity` 가 "반드시 고쳐야 하는가"의 단일 진실이다(가장 높은 값 `{top_severity}` 는 산문
  must-fix 절과 같은 건수여야 한다).
- `authority` 는 티켓 §목표/§결정 또는 `[[T-NNNN]]`·`[[ADR-NNNN]]` 같은 권위 근거를 적는다.
- 직전 라운드 지적의 해소 확인 라운드는 그 `{prefix}-` ID 를 `confirmations` 에 싣는다.
- `fix_contract`의 여섯 문자열은 모두 **구체값**이어야 한다. angle-bracket metavariable와
  parser 금지어({placeholder_words})를 쓰지 마라. 경로는 실제 repo-relative 경로를, 명령은
  그대로 실행 가능한 단일 명령을 적는다.
- 구조화 블록의 JSON 문자열 값에는 `<` 또는 `>` 문자를 아예 쓰지 마라. metavariable가
  필요하면 꺾쇠 없이 `ticket ID`처럼 평문으로 적는다.
- 유효한 구체값 예: location=`.project_manager/tools/additional_reviewer.py:_VERSIONED_BLOCK_RULES` ·
  failure=`금지어 안내가 parser 목록과 어긋난다` · design=`parser 상수에서 안내를 렌더한다` ·
  test=`tests/test_additional_reviewer_ticket_harvest.py` ·
  command=`python3 -m pytest tests/test_additional_reviewer_ticket_harvest.py -q` · expected=`passed`.
- 위 티켓 본문에 이미 있는 블록(지난 라운드 산출·시드 골격)을 회신에 **재인용하지 마라** —
  이 회신에는 네가 이번에 낸 블록 하나만 있어야 한다.
- 블록이 없거나 스키마를 어기면 이 라운드는 회수되지 않는다(라운드 파일 없음 · 종료코드 ≠ 0).

"""


def _versioned_block_requirement(
    next_finding_id: str | None = None,
    confirmation_ids: Sequence[str] | None = None,
) -> str:
    """추가 리뷰어 채널의 구조화 블록 요구 — 골격·접두·ID 실값을 엔진에서 렌더한다.

    리뷰어 세션은 라운드마다 fresh 라 이전 라운드의 ID 를 모른다. 회수 대상 티켓에서 읽어 온
    **다음 ID 실값**(`_next_external_finding_id`)을 골격에 싣는다. 실값이 없으면(회수 대상 없는
    실행) 첫 ID·placeholder 로 돌려준다.
    """
    role = review_form.ADDITIONAL_REVIEWER_ROLE
    next_id = next_finding_id or review_state.next_review_finding_id("", role)
    rules = _VERSIONED_BLOCK_RULES.format(
        prefix=review_form.PM_REVIEW_FINDING_ID_PREFIXES[role],
            # 표기는 호출부가 정할 수 있다 — 대상이 티켓 하나면 ID 실값 하나이고, 묶음이면
            # 그 값이 티켓마다 다르다(그 실값은 각 라운드 파일 시드가 이미 들고 있다).
        next_id=f"`{next_id}`",
        top_severity=review_form.PM_REVIEW_SEVERITIES[0],
        placeholder_words="·".join(
            review_form.PM_REVIEW_CONTRACT_PLACEHOLDER_WORDS
        ),
    )
    return (
        _VERSIONED_BLOCK_HEADER
        + rules
        + review_form.PM_REVIEW_FIX_TEST_RULE
        + "\n\n"
        + review_form.render_pm_review_block_skeleton(role, confirmation_ids)
        + "\n"
    )
