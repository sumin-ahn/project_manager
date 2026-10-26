"""추가 리뷰어 예산 설정 키 (층: domain)."""
from __future__ import annotations

from typing import NamedTuple
from pathlib import Path
import hashlib
import re
from pm_engine.domain import reviewer_result
import uuid

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


# 수렴 차단 사유 — 안내 문구의 라벨이자 판정 반환값(호출부가 사유별로 갈리지 않게 한 축).
CONVERGENCE_DIVERGING = "diverging"
CONVERGENCE_CAP_UNRESOLVED = "cap-unresolved"
CONVERGENCE_CAP_REACHED = "cap-reached"
_CONVERGENCE_REASONS: dict[str, str] = {
    CONVERGENCE_DIVERGING: "직전 라운드 대비 must_fix 증가(발산) — 상한 전 조기 차단",
    CONVERGENCE_CAP_UNRESOLVED: "라운드 상한 도달 · must_fix 미해소",
    CONVERGENCE_CAP_REACHED: "라운드 상한 도달",
}


# 측정 제외 subtree — 기계 생성·설치 산출물을 손작업 diff 와 같은 대상으로 보지 않는다.
# `templates/<타깃>/.project_manager/`는 그 대부분이 pm_update 가 내보낸 엔진 사본이고,
# `.opencode/node_modules/`는 패키지 설치 트리다. 완료 기록의 사설 참조 검사가 이 제외로 손작업
# 표면만 본다(엔진 사본의 같은 줄을 출하 타깃 수만큼 다시 지목하지 않는다). 제외는 **subtree 단위**라
# templates 안의 manifest 밖 손편집 파일도 함께 빠지지만, 오차의 방향은 **측정 축소**다.
# template subtree 자체의 정합은 drift-0 가드가 따로 지키고,
# payload와 리뷰어 거울은 아래의 좁은 manifest 예외만 보존하고 나머지 측정 제외분을 함께 뺀다.
#
# 이 정규식은 `.project_manager/`(순수 엔진 사본)만 본다 — `templates/<타깃>/`의 나머지(어댑터층
# `.claude/`·`.codex/`·`.opencode/`·flavor 루트 `*.md/*.sh/*.cmd`)는 `pm_update --all-targets` 가
# 마찬가지로 기계로 써내는데도 이 정규식 밖이라 손작업으로 계상됐다(689줄 실측으로 드러난 부수
# 발견). 그 어댑터층 판정은 경로 패턴을 추가로 하드코딩하지 않고 `_adapter_layer_machine_mirror`가
# 그 타깃 engine.manifest 의 `@source=` 방향에서 파생한다 — 이 정규식이 아래 함수보다 먼저
# 걸러내는 subtree(=`.project_manager/`)는 그대로 두고, 그 밖의 templates 경로만 매니페스트로 넘긴다.
_MACHINE_MIRROR_RE = re.compile(
    r"^templates/[^/]+/\.project_manager/|^\.opencode/node_modules/"
)

# wave(세션) 단위 총 라운드 예산 — 게이트별 상한과 **별개** 축이다. 게이트 상한만 있으면 비용이
# 티켓 수 × 라운드 상한으로 확장되므로, 전 게이트 합계 호출을 이 예산으로 묶는다. 기본 24 는
# 게이트당 4라운드 × 동시 진행 6티켓 어림이고 실측 세션당 라운드(~50)보다 낮게 잡아 PM 이 중간에
# `--rounds-report`로 수렴 상태를 점검하는 관측점을 만든다. local.conf
# additional_reviewer.wave_budget 로 조정 가능.

# 라운드 상한 초과 전용 종료 코드 (기존 0=통과·1=반려/실패/오류·2=argparse·3=예약 과 구분).
# 실행 전 거부라 리뷰어는 호출되지 않는다(과금 없음). wave 예산 소진도 같은 축(호출 전
# 예산 거부)이라 같은 rc 를 쓴다 — 호출부가 "예산 때문에 안 나갔다"를 한 코드로 판별한다.
EXIT_ROUND_LIMIT_EXCEEDED = 4


# 빈-diff fail-loud 안내.
# 검토 경로에 tracked 변경이 없어 diff 가 비면 codex 는 "변경 없음"을 통과로 판정해 가짜
# 통과(false-green)를 낸다. codex 호출 전에 이 메시지로 fail-loud 한다 (우회 플래그 없음).
_EMPTY_DIFF_GUIDANCE = (
    "오류: 리뷰할 diff 가 없습니다 (검토 경로에 tracked 변경 없음).\n"
    "  빈 diff 를 리뷰하면 추가 리뷰어가 '변경 없음'을 통과로 판정해 가짜 통과(false-green)가\n"
    "  발생합니다 — 추가 리뷰어를 호출하지 않고 중단합니다.\n"
    "  · 첫 provenance의 `diff_root`가 실 변경 worktree인지 확인하고, 필요하면 그 repo를 가리키는\n"
    "    절대 `--paths <경로>`로 다시 실행하세요. 프로세스 cwd는 diff 판정 입력이 아닙니다.\n"
    "  · 신규 파일만 변경했다면 먼저 `git add` 후 재실행하세요 (diff 는 tracked 변경만 봅니다)."
)


def _empty_diff_guidance(paths: Sequence[str], *, root: Path) -> str:
    """콤마 nargs 오용이 실제로 의심될 때만 기존 빈-diff 안내에 형식 진단을 보탠다."""
    suspicious: list[str] = []
    for raw in paths:
        if "," not in raw or (root / raw).exists():
            continue
        suspicious.append(raw)
    if not suspicious:
        return _EMPTY_DIFF_GUIDANCE
    rendered = ", ".join(repr(item) for item in suspicious)
    return (
        _EMPTY_DIFF_GUIDANCE
        + "\n  · --paths는 공백 구분 인자입니다. 콤마가 든 미존재 경로를 감지했습니다: "
        + rendered
        + "\n    예: --paths a.py b.py (자동 교정하지 않음 — 콤마가 파일명인 경우를 보존)."
    )

# 미완 라운드 상한 초과 fail-loud 안내. 같은 게이트에서 판정 없이 끝난 호출이 상한을 채우면
# diff 추출·추가 리뷰어 호출 전에 이 안내로 차단한다.
#
# 이 상한의 성격은 **무한 루프 차단(anti-loop pause)**이다. 연장 승인(`--ack-rounds`)은 폐지됐다 —
# "반례가 진짜니까 계속"을 사람이 승인하는 구조 자체가 비용 누수였다(실측 12라운드). 상한에
# 닿으면 현재 티켓을 정지하고 사용자에게 보고한다.
_ROUND_LIMIT_GUIDANCE = (
    "오류: 추가 리뷰어 미완 라운드 상한 도달 — 게이트 {gate} · "
    "count={unacked}(판정 {verdicts} · 미완 {incomplete})\n"
    "  (미완 재시도 상한 {incomplete_limit}). 무한 라운드 차단이라 "
    "초과분은 기계가 멈춥니다 — 자의 우회 불가.\n"
    "  · **미완**은 판정이 없던 호출입니다 — 타임아웃·중단뿐 아니라 **오염 진단으로 무효화된 "
    "판정**도 이 축에 들어갑니다(판정 표면과 같은 규칙이라 두 표면이 갈리지 않습니다).\n"
    "  · 먼저 `--rounds-report` 로 라운드별 산출과 수렴 상황을 확인하세요.\n"
    "  · 현재 티켓을 정지하고 사용자에게 보고하세요 — 라운드 연장 승인(`--ack-rounds`)은 "
    "폐지됐습니다.\n"
    "  · 미완 재시도 상한은 local.conf "
    "`additional_reviewer.incomplete_rounds_max` 로 조정합니다.\n"
    "  (장부: {ledger} · count={count})"
)

# 수렴-형상 차단 안내 (라운드 상한 rc 를 그대로 쓴다 — 호출 전 예산 거부라 같은 축).
# 라운드 수는 **완료 산출 + 진행 중 예약**이다 — 상한 판정과 같은 수를 보여야 "상한에 닿았는데
# 왜 막히나"를 문구가 스스로 답한다(동시 실행이 이미 나가 있는 형상).
_CONVERGENCE_GUIDANCE = (
    "오류: 리뷰 수렴 게이트 차단 — 게이트 {gate} · {reason}\n"
    "  라운드 {rounds}(완료 {completed} · 진행 중 예약 {inflight}) / 상한 {limit} · "
    "must_fix 추이 [{series}]\n"
    "  · 라운드를 더 쓰지 않습니다. 현재 티켓을 정지하고 남은 지적을 사용자에게 보고하세요.\n"
    "  · 라운드별 산출은 `--rounds-report --gate {gate}` 로 확인하세요.\n"
    "  · 상한 조정은 local.conf `{knob}` (기본 {default}).\n"
    "  (장부: {ledger})"
)

# `--ack-rounds` 폐지 안내 — 플래그는 인자표에 남겨 두고(모르는 인자 오류 대신 처방을 낸다)
# 호출 자체를 거부한다. 연장 승인 경로가 남아 있으면 상한이 상한이 아니게 된다.
_ACK_ROUNDS_REMOVED_GUIDANCE = (
    "오류: `--ack-rounds`(라운드 연장 승인)는 폐지됐습니다 — 이 실행은 아무것도 하지 않았습니다.\n"
    "  · 라운드 상한·수렴 상한에 걸리면 현재 티켓을 정지하고 사용자에게 보고합니다.\n"
    "  · wave 예산 재개(`--ack-wave`)는 그대로입니다 — 별개 축입니다.\n"
    "  · 현재 수렴 상황은 `--rounds-report --gate <T-NNNN>` 로 확인하세요."
)

# ── 게이트 회계 자동 유도 (`--ticket` → `--gate`) ─────────────────────────────
# `--ticket` 만 준 실행은 리뷰를 정상 호출·과금하면서 라운드 예약·기록·상한 회계를 통째로
# 건너뛰었다(실측: 하루 8 라운드 넘게가 장부에 0건). 그 조용한 무기록은 반려 must-fix 가
# 릴리즈 차단 표면(`board.py livegate record`)에 도달하지 못하게 만드는 구멍이라, 기본값을
# **기록**으로 뒤집는다 — `--ticket` 은 이미 이번 검토가 어느 티켓의 것인지 말하고 있으므로 그
# 값을 게이트로 쓰면 회계를 붙이는 데 사람의 기억이 필요 없다([[mechanize-dont-instruct-llm]]:
# "게이트를 같이 붙여라"는 규율 기억은 실측에서 실패했다). 회계 밖 자문 실행은 명시
# opt-out(`--no-gate`)으로만 연다.
#
# 고지 **출력 자리**는 해소 결과를 말하는 자리다 — 유도 자체는 인자 파싱 직후에 하지만(뒤따르는
# 게이트 검사·프롬프트·예약이 모두 그 값을 쓴다), 문구는 stderr **첫 줄 = config provenance** 계약을
# 깨지 않게 provenance 직후에 낸다. 빈 diff 로 아직 끝날 수 있는 예약 전 자리이므로,
# 장부에 붙었다는 확정형이 아니라 실행 조건형으로만 말한다.
_GATE_DERIVED_NOTICE = (
    "게이트 자동 유도: --gate {gate} (--ticket 값에서 유도 — 이 실행이 리뷰어를 부르면 라운드 회계가 "
    "게이트 {gate}에 붙습니다). "
    "회계 밖 자문 실행은 --no-gate 로 명시하세요."
)
# 미리보기는 예약도 기록도 하지 않는다 — 같은 문구를 쓰면 "이번 실행이 기록된다"는 오보가 된다.
_GATE_DERIVED_DRY_RUN_NOTICE = (
    "게이트 자동 유도: --gate {gate} (--ticket 값에서 유도 — 미리보기라 이번 실행은 기록·집계하지 "
    "않고, 실 호출 때 라운드 회계가 이 게이트에 붙습니다)."
)

# 회계 밖 실행의 loud 표기 — 침묵이 이 함정의 본체였으므로 명시 opt-out 실행은 자기
# 상태를 스스로 말한다. 아무 선택도 없는 실 호출은 아래 `_GATE_ACCOUNTING_REQUIRED_GUIDANCE`로
# 거부하므로, 이 조건형 경고는 `--no-gate` 경로에서만 나온다.
# 예약 자리(호출 확정 전)의 문구는 **조건형**이다: 이 뒤로 환경 준비·스폰이 남아 있어 "호출했다"고
# 확정하면 스폰 전에 중단된 실행이 이미 찍힌 고지와 모순된다. 확정 표기는 실행이 끝난 뒤 판정
# 블록(`print_summary`)의 게이트 줄이 낸다.
_UNACCOUNTED_RUN_TAIL = (
    "리뷰어를 부르면 라운드 장부에 기록되지 않고 라운드·wave 예산도 쓰지 않습니다 — 그 라운드에서 나온 "
    "반려 must-fix 는 릴리즈 차단(`board.py livegate record`)이 읽는 장부에 남지 않습니다.\n"
    "  · 회계에 넣으려면 `--ticket <T-NNNN>`(게이트 자동 유도) 또는 `--gate <게이트>` 로 "
    "실행하세요."
)
_UNACCOUNTED_OPT_OUT_NOTICE = (
    "경고: `--no-gate` 명시 opt-out — 이 실행이 " + _UNACCOUNTED_RUN_TAIL
)

# 실 실행은 회계에 넣거나 명시적으로 빠져야 한다. 어느 쪽도 고르지 않은 실행을 경고만 하고
# 진행하면 티켓 누락 한 번이 라운드 장부·livegate 누락으로 이어진다. dry-run·조회·처분은
# 리뷰어를 부르지 않아 이 검사에 도달하지 않는다.
_GATE_ACCOUNTING_REQUIRED_GUIDANCE = (
    "오류: 실 호출에는 게이트 회계 지정 또는 명시 opt-out 이 필요합니다 — "
    "이 실행은 호출하지 않았습니다.\n"
    "  · 라운드 회계에 넣으려면 `--ticket <T-NNNN>`(게이트 자동 유도) 또는 "
    "`--gate <게이트>` 를 지정하세요.\n"
    "  · 회계 밖 자문 실행이면 `--no-gate` 를 명시하세요."
)

# 판정 블록의 게이트 줄 — 회계 밖 실행은 **끝난 뒤** 여기서 확정형으로 말한다. stderr 경고는 로그를
# 안 읽으면 사라지지만 PM 은 판정 블록을 반드시 읽는다(오염 진단·실패 사유와 같은 근거).
_SUMMARY_UNACCOUNTED_GATE = "(없음 — 회계 밖·라운드 장부 미기록)"

# `--gate` 와 `--no-gate` 동시 지정 안내 — 한 실행이 "기록한다"와 "기록하지 않는다"를 동시에
# 뜻할 수 없다. 경고 후 한쪽을 골라 실행하면 그 선택이 조용한 자의 판정이 되므로 부작용 0
# 지점에서 거부한다(`--ack-rounds` 폐지와 같은 자리·같은 규율).
_GATE_OPT_OUT_CONFLICT_GUIDANCE = (
    "오류: `--no-gate` 와 `--gate {gate}` 는 함께 쓸 수 없습니다 — 이 실행은 아무것도 "
    "하지 않았습니다.\n"
    "  · `--gate` 는 이 라운드를 장부에 기록하겠다는 지정이고, `--no-gate` 는 기록하지 않겠다는 "
    "opt-out 입니다.\n"
    "  · 회계에 넣으려면 `--no-gate` 를 빼고, 회계 밖 자문이면 `--gate` 를 빼세요."
)


# wave 예산 소진 fail-loud 안내. 게이트별 상한을 통과한 호출이라도 wave 합계가 예산을 채우면
# 같은 자리(추가 리뷰어 호출 전)에서 같은 rc 로 막는다 — 게이트마다 상한을 새로 받는 구조에서는
# 티켓 수만큼 라운드가 늘어나기 때문이다. 성격은 라운드 상한과 같은 anti-loop pause 다.
_WAVE_BUDGET_GUIDANCE = (
    "오류: wave 예산 소진 — `--ack-wave` 로 재개(예산 리셋)\n"
    "  게이트 {gate} · wave spent={spent} (예산 {budget} · wave 시작 {started})\n"
    "  · 게이트별 라운드 상한과 **별개**인 wave 단위 총 라운드 예산입니다 — 티켓 수 × 라운드 "
    "상한으로 라운드가 확장되는 것을 막습니다.\n"
    "  · 먼저 `--rounds-report` 로 라운드별 산출을 확인하세요.\n"
    "  · 같은 범위가 정상 수렴 중이면 PM 이 자율로 `--ack-wave` 를 붙여 재개합니다 "
    "(spent 를 0 으로 리셋):\n"
    "      python3 .project_manager/tools/additional_reviewer.py --gate {gate} --ack-wave [기존 옵션]\n"
    "  · 수렴이 안 되고 있으면(같은 지적 반복·범위 발산) 사용자에게 보고하세요.\n"
    "  · 예산 조정은 local.conf `additional_reviewer.wave_budget`.\n"
    "  (장부: {ledger})"
)


# ── 게이트 처분 선언(`--resolve-gate`) 안내 ────────────────────────────────
# 라운드 상한으로 종결된 게이트의 잔여 must-fix 를 **어떻게 소화했는지** 장부에 남기는 표면이다.
# 선언 자체가 판정이 아니라 *기록*이고, 릴리즈 차단(`board.py livegate record`)은 그 기록 사실만
# 읽는다 — "사소하니 넘어간다"는 자의 판정이 들어갈 자리를 어느 쪽에도 만들지 않는다.
_RESOLVE_GATE_MODE_GUIDANCE = (
    "오류: `--resolve-gate {gate}` 에는 `--pm-verified`가 필요합니다 — 이 실행은 아무것도 "
    "하지 않았습니다.\n"
    "  · PM 판정 표면의 기계 확인 증거로만 해소합니다(재호출 없음 — accepted 잔여 "
    "0·잔여 must-fix 건수만큼 전건 처분·필요 시 기계 확인 1건 이상).\n"
    "  · 후속 티켓·후속 게이트로 잔여를 넘기는 처분은 지원하지 않습니다."
)

_RESOLVE_GATE_REQUIRED_GUIDANCE = (
    "오류: `{flag}` 는 `--resolve-gate <게이트>` 와 함께 써야 합니다 — 처분할 게이트가 없으면 "
    "선언할 사실이 없습니다.\n"
    "      python3 .project_manager/tools/additional_reviewer.py --resolve-gate <게이트> {flag}"
    "{value}"
)

_RESOLVE_GATE_PM_VERIFIED_GUIDANCE = (
    "오류: 게이트 {gate} 에는 pm-verified 처분을 선언할 수 없습니다 — {detail}\n"
    "  · `--pm-verified` 는 PM 판정 표면(리뷰 delta)이 그 채널 accepted 잔여 없이, 그 채널 잔여 "
    "must-fix 건수만큼 전건 처분되고, accepted 가 있었을 때만 기계 확인이 1건 이상 기록됐을 때만 "
    "선언됩니다.\n"
    "  · 표면 상태는 `python3 .project_manager/tools/pm_delegate.py review delta --ticket {gate}` "
    "로 확인하세요."
)

_RESOLVE_GATE_UNKNOWN_GUIDANCE = (
    "오류: 게이트 {gate} 는 라운드 장부에 기록이 없습니다 — 처분할 잔여가 없습니다.\n"
    "  · 처분 선언의 입력은 장부의 기록 사실뿐입니다(그 게이트로 실제 돈 라운드).\n"
    "  · 게이트 이름을 `--rounds-report` 로 확인하세요 (장부: {ledger})."
)

_RESOLVE_GATE_DRY_RUN_GUIDANCE = (
    "오류: `--resolve-gate {gate}` 는 `--dry-run` 과 함께 쓸 수 없습니다 — 이 실행은 아무것도 "
    "하지 않았습니다.\n"
    "  · 처분 선언은 장부에 사실을 남기는 것이 목적이라 미리보기가 성립하지 않습니다 "
    "(호출은 어차피 없습니다).\n"
    "  · 지금 상태만 보려면 `--rounds-report --gate {gate}` 를 쓰세요."
)

_RESOLVE_GATE_NO_RESIDUAL_GUIDANCE = (
    "오류: 게이트 {gate} 에는 처분할 잔여 must-fix 가 없습니다 (최종 라운드 must_fix={residual}).\n"
    "  · 처분 선언은 **반려로 끝난** 게이트의 잔여를 소화하는 기록입니다 — 잔여가 없으면 릴리즈 "
    "게이트도 이 게이트를 보지 않습니다(무대상).\n"
    "  · 현재 상태는 `--rounds-report --gate {gate}` 로 확인하세요."
)


def _target_rev_fingerprint(diff: str) -> str:
    """선택된 diff 본문의 content revision (`sha256:<hex>`).

    git HEAD 는 working tree의 staged/unstaged/untracked 변경을 식별하지 못한다. 내부 측정한
    UTF-8 diff 바이트를 직접 해시하면 같은 미수정 diff의 동시 리뷰는 같은 좌표, 수정 뒤 리뷰는 새
    좌표를 얻는다."""
    return "sha256:" + hashlib.sha256(diff.encode("utf-8")).hexdigest()


# ── wave 예산 절 (전 게이트 합계) ───────────────────────────────────────────

WAVE_SECTION_KEY = "wave"

# 장부 최상위에서 wave 절이 차지하는 **예약 키** — 게이트 이름으로 쓸 수 없다. 같은 키를 게이트가
# 쓰면 `_gate_entry` 와 `_wave_state` 가 한 항목을 서로 덮어써 게이트 count 는 저장되지 않고 wave
# 예산은 매 실행 0 으로 되살아난다(두 상한이 조용히 무력화). 게이트 이름은 형식 강제 대상이 아니라
# 자유 문자열이 실사용이므로(장부 실측: `wave4-b1`·`wave4-b3r2`) `T-NNNN` 강제 대신 **예약 키만**
# 거부한다 — 기존 이름은 하나도 깨지지 않고 충돌 가능성만 사라진다.
_RESERVED_LEDGER_KEYS: frozenset[str] = frozenset({WAVE_SECTION_KEY})

# 게이트 항목임을 알아보는 필드 — 예약 키 자리에 이런 항목이 들어 있으면 예약 키 도입 *이전에*
# 그 이름을 게이트로 쓴 장부다(교체 사실을 조용히 넘기지 않고 알린다).
_GATE_ENTRY_MARKERS: tuple[str, ...] = ("count", "records", "rounds")

_RESERVED_GATE_GUIDANCE = (
    "오류: `{flag} {gate}` 는 라운드 장부의 예약 키라 게이트 이름으로 쓸 수 없습니다 "
    "(예약 키: {keys}).\n"
    "  · 그 키는 장부 최상위에서 wave 예산 절이 씁니다 — 같은 이름의 게이트는 게이트 집계와 "
    "wave 예산이 서로 덮어써 라운드 상한·예산이 둘 다 조용히 무력화됩니다.\n"
    "  · 다른 게이트 이름으로 다시 실행하세요 (이름 형식 제약은 없습니다 — 예약 키만 거부)."
)


def _reserved_gate_error(gate: str | None, *, flag: str = "--gate") -> str | None:
    """게이트 이름이 장부 예약 키면 차단 안내를 돌려준다 (아니면 None).

    판정 입력은 이름 하나뿐이라 호출·장부 접근 **전에** 부를 수 있다 — 거부된 실행은 호출도
    장부 변경도 남기지 않는다. 게이트 이름을 받는 표면이 여럿이라(`--gate`·`--resolve-gate`)
    어느 플래그가 걸렸는지는 인자로 받는다 — 판정 규칙은 한 곳이다."""
    if gate is None or gate not in _RESERVED_LEDGER_KEYS:
        return None
    return _RESERVED_GATE_GUIDANCE.format(
        flag=flag, gate=gate, keys=", ".join(sorted(_RESERVED_LEDGER_KEYS)),
    )


def _new_wave_id() -> str:
    """wave 세대 식별자 — 리셋마다 새로 발급한다 (환불이 남의 세대를 깎지 않게)."""
    return uuid.uuid4().hex


def _wave_frame_corruption(raw: object) -> str | None:
    """wave 절 **자체**가 못 쓸 형상인지 (절 부재·정상이면 None).

    비-dict 값이나 예약 키를 차지한 옛 게이트 항목은 id/started 를 포함해 아무 좌표도 신뢰할 수
    없는 경우다 — 소비량은 라운드 이력에서 다시 센다."""
    if raw is None or (isinstance(raw, dict) and not any(
        marker in raw for marker in _GATE_ENTRY_MARKERS
    )):
        return None
    if not isinstance(raw, dict):
        return f"`{WAVE_SECTION_KEY}` 절 형식 오류({type(raw).__name__})"
    return f"예약 키 `{WAVE_SECTION_KEY}` 자리의 옛 게이트 항목"


def _wave_spent_corruption(spent: object) -> str | None:
    """`spent` 값이 신뢰할 수 없는지 — 비정수와 **음수**가 같은 축이다.

    음수를 정상 정수로 받아 `max(0, …)` 로 접으면 `spent: -1` 한 줄 편집이 승인 없이 hard 예산을
    되돌린다(무통보 재개방). 소비량은 셀 수 없는 값이지 0 이 아니므로 손상으로 판정한다."""
    if spent is None:
        return None
    if isinstance(spent, bool) or not isinstance(spent, int):
        return f"`{WAVE_SECTION_KEY}.spent` 값 손상({spent!r})"
    if spent < 0:
        return f"`{WAVE_SECTION_KEY}.spent` 음수({spent})"
    return None


def _wave_corruption_note(raw: object) -> str | None:
    """wave 절 손상 진단 한 곳 — 프레임 축과 spent 축을 합산한다 (정상·부재면 None).

    `_wave_state` 의 복구 판정과 호출부의 "복구값을 저장할까" 판정이 **같은 술어**를 봐야 한다 —
    갈리면 고친 값을 안 쓰거나(경고만 반복) 멀쩡한 장부를 거부 경로에서 덮어쓴다."""
    frame = _wave_frame_corruption(raw)
    if frame is not None:
        return frame
    return _wave_spent_corruption(raw.get("spent") if isinstance(raw, dict) else None)


def _recorded_round_outcomes(ledger: dict, *, since: str | None = None) -> int:
    """장부에 남은 라운드 산출 수 — `since`(UTC ISO 시각) 이후로 좁힐 수 있으면 좁힌다.

    손상된 `spent` 를 0 으로 접지 않기 위한 **데이터 근거 재계산**이다. 산출은 실제 호출된 라운드
    에만 남으므로(스폰 전 실패는 환불·미기록) 소비량의 하한이고, 범위를 좁힐 근거가 없으면
    (wave 시작 시각까지 손상) 전체를 세어 보수적으로 남는다 — 손상이 예산을 여는 방향으로는
    작동하지 않게 한다. 같은 형식의 UTC ISO 문자열은 사전순 비교가 곧 시간순이다."""
    total = 0
    for name in _gate_names(ledger):
        entry = ledger.get(name)
        rounds = entry.get("rounds") if isinstance(entry, dict) else None
        if not isinstance(rounds, list):
            continue
        for row in rounds:
            if not isinstance(row, dict):
                continue
            timestamp = row.get("ts")
            if since is None or (isinstance(timestamp, str) and timestamp >= since):
                total += 1
    return total


def _gate_names(ledger: dict) -> list[str]:
    """장부의 게이트 키만 정렬해 돌려준다 — 예약 키(`wave`)는 게이트가 아니다."""
    return sorted(key for key in ledger if key != WAVE_SECTION_KEY)


def _reset_wave(ledger: dict) -> dict:
    """`--ack-wave` — ack 시점에 예산을 리셋한다(다음 호출이 새 시작 시각을 찍는다).

    새 세대 id 를 발급한다 — 리셋 전에 예약한 실행의 환불이 이 wave 의 예산을 깎지 못하게."""
    ledger[WAVE_SECTION_KEY] = {"id": _new_wave_id(), "started": None, "spent": 0}
    return ledger[WAVE_SECTION_KEY]


def _round_has_verdict(result: dict) -> bool:
    """리뷰어 결과에 통과 또는 must-fix/반려 판정이 실제로 있었는지 판별한다.

    오염 진단이 붙은 출력은 판정으로 세지 않는다 — 판정 표면(all_pass·any_must_fix)에서 무효화한
    것을 라운드 장부에서만 판정으로 세면 두 표면이 갈린다."""
    if not result.get("ok") or result.get("contamination"):
        return False
    verdict = result.get("verdict")
    if not isinstance(verdict, dict):
        return False
    return bool(verdict.get("has_pass") or verdict.get("has_must_fix"))


# ── 라운드별 산출 기록 ──────────────────────────────────────────────────────
# 라운드 count 만으로는 "그 라운드가 실결함을 냈는가"를 기계로 확인할 수 없어, 게이트 심도 대비
# 비용 적정성 판단이 PM 자기보고에 의존했다. 산출은 **새 파서 없이** 기존 판정 결과에서만 파생한다
# (rc 판정 + 이미 있는 must-fix 파서). 기록은 무조건이고, 셀 근거가 없으면 null 로 남긴다 —
# 기록 실패로 리뷰를 막지 않는다(hard 거부는 예산 축 하나뿐).


def _round_must_fix_items(result: dict) -> list[str] | None:
    """이번 라운드가 지적한 must-fix 항목 **텍스트** (셀 근거가 없으면 None · "없음" 표기는 []).

    판정이 무효한 라운드(실패·타임아웃·오염 진단)는 세지 않는다 — 판정 표면에서 무효화한 출력을
    산출 장부에서만 결함으로 세면 두 표면이 갈린다(`_round_has_verdict` 와 같은 규칙). 세는 대상은
    **회신 채널**(`answer`)뿐이다: 진행 로그에는 프롬프트와 diff 원문이 그대로 실려 있어 그것까지
    보면 리뷰 대상 코드의 문구가 결함 수로 둔갑한다.

    **섹션 부재와 "없음" 표기는 다르다.** 형식을 지킨 응답의 `must-fix: 없음` 은 리뷰어가 결함이
    없다고 *말한* 것이라 []이고, must-fix 섹션이 아예 없는 응답은 아무 말도 없던 것이라 None 이다
    (항목 근거 없이 반려만 있는 응답을 '결함 0건 반려'로 박제하면 비용 판단이 거짓이 된다).
    섹션 인식은 판정 파서와 **같은 정규식**(`_MUST_FIX_SECTION_RE`)을 쓰고, 항목 추출은 판정
    파서와 **같은 함수**(`_extract_must_fix_items`)를 쓴다 — 개수와 텍스트가 다른 규칙으로 갈리면
    장부의 건수와 근거가 어긋난다."""
    if not _round_has_verdict(result):
        return None
    answer = result.get("answer")
    if not isinstance(answer, str):
        return None
    if not reviewer_result._MUST_FIX_SECTION_RE.search(answer):
        return None
    items = reviewer_result._extract_must_fix_items(answer)
    return [] if reviewer_result._is_none_items(items) else items


def _must_fix_count(result: dict) -> int | None:
    """이번 라운드가 지적한 must-fix 항목 수 (셀 근거가 없으면 None).

    셈의 입력은 항목 텍스트 추출(`_round_must_fix_items`)과 **같은 한 함수**다 — 건수와 텍스트가
    각자 파싱하면 장부의 `must_fix` 와 기록 근거가 서로 다른 라운드를 말하게 된다."""
    items = _round_must_fix_items(result)
    return None if items is None else len(items)


def _format_must_fix_series(series: Sequence[int | None]) -> str:
    """must_fix 추이 표기 — 셀 근거가 없던 라운드는 '미상'(0 과 구분)."""
    return " → ".join("미상" if value is None else str(value) for value in series) or "없음"


# 승인 고지 — 같은 승인이라도 **재개된 실행**과 **남은 축이 막아 거부된 실행**의 문구가 달라야
# 한다. rc 4 로 끝나는 실행이 "재개"를 말하면 stderr 가 종료 코드와 어긋난 loud 오보가 된다
# (승인 자체는 저장되므로 다음 실행이 이어받는다).
_APPROVAL_RESUMED_VERB = "재개"
_APPROVAL_REFUSED_VERB = "기록(이번 실행은 남은 축이 막아 거부)"


def _approval_notes(
    *, wave_reset: bool, wave_budget: int, resumed: bool,
) -> list[str]:
    """이번 실행이 적용한 승인의 stderr 고지 문구 (적용분이 없으면 빈 목록).

    승인 축은 wave 예산 하나뿐이다 — 라운드 연장 승인(`--ack-rounds`)은 폐지됐다."""
    verb = _APPROVAL_RESUMED_VERB if resumed else _APPROVAL_REFUSED_VERB
    notes: list[str] = []
    if wave_reset:
        notes.append(f"wave 예산 승인 {verb}: spent 리셋 (예산 {wave_budget}).")
    return notes


# ── 예산 게이트: 확인→예약 (호출보다 **앞**) ────────────────────────────────
# 두 예산(게이트 라운드 상한·wave)의 확인과 예약은 raw 선점·스폰보다 먼저다. 이미
# 상한에 닿은 호출이 raw 자리와 산출 디렉터리를 만들었다 지우면 "차단된 호출은 아무것도 하지
# 않는다"가 거짓이 된다 — 정리가 끝까지 성공한다는 보장도 없어(정리 실패는 loud 경고로 남는다)
# 호출도 못 할 실행이 산출 자리를 남길 수 있다.
#
# 순서를 뒤집는 대가는 하나뿐이다: 예약 뒤·스폰 전에 끝나는 구간(리뷰어 환경 준비)이
# 생긴다. 그 구간은 호출이 확실히 없으므로 마감 시점의 `started=False` 환불과 **같은 조건**
# 이고, 같은 기계(`_refund_reserved_round`)를 같은 락 아래에서 재사용해 되돌린다
# (`_release_round_reservation`). 구간 전체의 소유는 `_PreSpawnReservation` 하나가 진다 — 어떤
# 예외로 나가든 한 번만 환불하고, 스폰 직전에 소유권을 마감 경로로 넘긴다.


class RoundBudget(NamedTuple):
    """이번 실행의 예산 판정 결과 — 거부 rc 또는 예약 identity.

    `refused_rc` 가 채워진 결과는 **그 자리에서 끝나는 실행**이다(raw·스폰·과금 없음).
    통과한 실행 중 `--gate` 지정분만 예약 좌표(gate·round_id·sequence·wave_id)를 갖는다 —
    `--gate` 없는 실행은 종전대로 장부 밖이라 세 값이 비어 있는 통과 결과다.

    `sequence` 는 **예약 시점** 순번이다: 마감 때 레코드를 되찾아 읽으면 그 사이 집계 창이
    비워진 실행만 순번을 잃는다. `wave_id` 는 예약 시점 wave 세대라
    환불이 그 세대에만 유효하다(리셋된 새 wave 의 예산을 옛 실패가 깎지 못한다).

    """

    refused_rc: int | None = None
    gate: str | None = None
    round_id: str | None = None
    sequence: int | None = None
    started_at: str | None = None
    target_rev: str | None = None
    wave_id: str | None = None

    @property
    def reserved(self) -> bool:
        """되돌리거나 마감할 예약이 실제로 있는지."""
        return self.gate is not None and self.round_id is not None


class GateDerivation(NamedTuple):
    """게이트 유도 결과 — 거부 안내 / 유도 고지. **출력은 호출부가** 제 자리에서 한다.

    고지를 여기서 찍지 않는 이유는 자리 때문이다: 유도는 인자 파싱 직후여야 하고(뒤따르는 게이트
    검사·프롬프트·예약이 그 값을 쓴다) 고지는 stderr 첫 줄 provenance 뒤여야 한다."""

    refusal: str | None = None
    notice: str | None = None


def _first_output_line(result: dict) -> str:
    """실패 사유 1줄(없으면 빈 문자열) — 판정 블록과 환불 경고가 같은 문장을 쓰게 하는 공용 seam."""
    head = str(result.get("output") or "").strip().splitlines()
    return head[0][:200] if head else ""


# ── 종료 코드 결정 ────────────────────────────────────────────────────────


def determine_exit_code(result: dict) -> int:
    """failed→1(FALLBACK), any_must_fix→1, all_pass→0, 판정불명확→1(보수적)."""
    if result["failed"] or result["any_must_fix"]:
        return 1
    if result["all_pass"]:
        return 0
    return 1
