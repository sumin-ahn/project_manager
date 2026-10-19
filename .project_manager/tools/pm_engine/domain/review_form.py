"""리뷰 블록 양식 (층: domain).

리뷰 채널과 finding ID 접두, `pm-review-*`·`pm-architect-tests-v1` fence 의 세대·키·값 문법, 블록 판독과 골격 렌더,
재현 커맨드 안전 경계, 엔진 표식 줄 판독을 소유한다.
"""
from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import NamedTuple

from pm_engine.domain import delegate_errors


INTERNAL_REVIEW_ROLE = "code-reviewer"

# 추가 리뷰어(additional_reviewer) 산출이 회수되는 역할. 하네스로 위임되는 역할이 아니라 엔진이
# 직접 쓰는 채널이라 `conf_schema.ROLE_CHOICES` 에는 없고 라운드 역할 집합에만 있다.
ADDITIONAL_REVIEWER_ROLE = "additional-reviewer"

# 티켓 게이트 리뷰 채널 — 두 채널의 finding 은 같은 delta/disposition 표면에서 판정된다.
REVIEW_ROLES: tuple[str, ...] = (INTERNAL_REVIEW_ROLE, ADDITIONAL_REVIEWER_ROLE)
PM_REVIEW_BLOCK = "pm-review-v1"
PM_REVIEW_DISPOSITION_BLOCK = "pm-review-disposition-v1"

# versioned fence 후보를 찾는 유일한 정규식 — 스캐너(`_pm_review_json_blocks`)가 이 상수만 쓴다
# (리터럴 재기재 없음). 들여쓰기·4중 backtick·미지원 라벨까지 후보로 잡아야 손상 fence 하나가
# 티켓 전역 스캔에서 조용히 빠지지 않는다.
_PM_REVIEW_FENCE_CANDIDATE_RE = re.compile(r"`{3,}(pm-review[^\s`]*)")

# fence 이름(`…-v1`)은 블록 **종류** 라벨이고, payload 의 `version` 이 스키마 세대다. severity 를
# 필수로 올리면서 세대를 2 로 승격했다 — 이미 봉인돼 손댈 수 없는 v1 블록(진행 중 티켓 실측
# 8건·라운드 산출)을 파서가 legacy 로 계속 읽어야 판정 표면이 현존 자산을 잠그지 않는다.
PM_REVIEW_VERSION = 3
PM_REVIEW_LEGACY_VERSION = 1
PM_REVIEW_SUPPORTED_VERSIONS: tuple[int, ...] = (
    PM_REVIEW_LEGACY_VERSION, 2, PM_REVIEW_VERSION,
)

# severity 를 요구하기 시작하는 세대. v1 블록은 부재를 허용하고 렌더가 '미기재'로 표기한다.
PM_REVIEW_SEVERITY_MIN_VERSION = 2
PM_REVIEW_SEVERITY_UNSPECIFIED_LABEL = "미기재"

# PM 판정 블록은 스키마가 그대로라 세대를 올리지 않는다(채널 필드는 선택 key 로 흡수).
PM_REVIEW_DISPOSITION_VERSION = PM_REVIEW_LEGACY_VERSION
PM_REVIEW_CLASSES: tuple[str, ...] = (
    "implementation-defect", "spec-violation", "design-proposal",
)
PM_REVIEW_DECISIONS: frozenset[str] = frozenset(
    {"accepted", "rejected", "decision-required"}
)
PM_REVIEW_CONFIRMATION_STATES: tuple[str, ...] = (
    "resolved", "unresolved", "regressed",
)

# 해소 상태 — 나머지 상태(미해소·퇴행)는 잔여 must-fix 다. 값을 손으로 다시 적지 않도록
# enum 첫 항목에서 파생하고, 회귀가 그 결속을 값으로 잠근다.
PM_REVIEW_CONFIRMATION_RESOLVED: str = PM_REVIEW_CONFIRMATION_STATES[0]

# 심각도 — `class`(결함 종류)와 다른 축이다. "반드시 고쳐야 하는가"를 블록만으로 판정하려면
# 값이 블록에 있어야 한다(산문 분류는 기계 입력이 아니다).
PM_REVIEW_SEVERITIES: tuple[str, ...] = (
    "must-fix", "should-fix", "suggestion",
)

# 반드시 고쳐야 하는 단계 — 위 확인 상태와 같은 파생 규칙이다(리터럴 재기재 0).
PM_REVIEW_SEVERITY_MUST_FIX: str = PM_REVIEW_SEVERITIES[0]

# finding ID 는 티켓 전역 유일이라 채널별 접두로 네임스페이스를 나눈다(판정 표면은 하나다).
PM_REVIEW_FINDING_ID_PREFIXES: dict[str, str] = {
    INTERNAL_REVIEW_ROLE: "F",
    ADDITIONAL_REVIEWER_ROLE: "X",
}

# JSON member collections live beside the value enums because the strict parsers and every
# machine-supplied skeleton must move together.  Tuples retain the canonical rendering order;
# `_pm_review_exact_keys` deliberately compares them as sets.
PM_REVIEW_PAYLOAD_KEYS: tuple[str, ...] = (
    "version", "findings", "confirmations",
)
PM_REVIEW_FINDING_KEYS: tuple[str, ...] = (
    "id", "class", "severity", "authority", "evidence", "recommendation",
    "fix_contract", "design_change",
)
PM_REVIEW_LEGACY_FINDING_KEYS: tuple[str, ...] = tuple(
    key for key in PM_REVIEW_FINDING_KEYS if key != "fix_contract"
)
PM_REVIEW_FIX_CONTRACT_KEYS: tuple[str, ...] = (
    "location", "failure", "design", "test", "command", "expected",
)
PM_REVIEW_CONFIRMATION_KEYS: tuple[str, ...] = (
    "id", "status", "evidence",
)
PM_REVIEW_DISPOSITION_PAYLOAD_KEYS: tuple[str, ...] = (
    "version", "reviewer_role", "reviewer_ordinal", "dispositions",
)
PM_REVIEW_FINDING_ZERO_PAYLOAD_KEYS: tuple[str, ...] = (
    "version", "reviewer_role", "reviewer_ordinal", "finding_zero",
)

# 채널 필드는 두 채널 도입 전 티켓에 없다. 엔진이 만드는 골격은 항상 싣고, 파서는 부재를
# code-reviewer 로 해석해 기존 티켓을 그대로 판정한다(소급 재작성 금지).
PM_REVIEW_DISPOSITION_ROLE_KEY = "reviewer_role"
PM_REVIEW_DISPOSITION_KEYS: tuple[str, ...] = (
    "id", "decision", "reason", "scope", "prerequisite",
)
_PM_REVIEW_ID_RE = re.compile(r"[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+")

# ── dev 재현 커맨드(verify) + PM 기계 확인(confirmation) ────────────
# 확인 라운드 reviewer 재투입을 기계 판정으로 대체한다. dev 는 accepted finding 마다 재현
# 커맨드·기대값·fix 전 실값을 developer 라운드 파일에 남기고(`pm-review-verify-v1`), PM 은
# 그것을 직접 실행해 명세의 PM 영역에 관측값과 함께 기록한다(`pm-review-confirmation-v1`).
# 두 블록 다 fence 이름 뒤 `-v1` 이 종류(kind) 라벨이고 payload 의 `version` 이 세대다 —
# disposition/finding 블록과 같은 관례(신규라 세대는 1 하나뿐).
PM_REVIEW_VERIFY_BLOCK = "pm-review-verify-v1"
PM_REVIEW_CONFIRMATION_BLOCK = "pm-review-confirmation-v1"
PM_REVIEW_VERIFY_VERSION = 1
PM_REVIEW_MACHINE_CONFIRMATION_VERSION = 1

# architect가 developer 착수 전에 확정하는 최소 테스트 계약. developer/fix harvest가 같은
# command·expected를 실제 실행하므로 산문 체크리스트와 실행 게이트가 갈리지 않는다.
ARCHITECT_TEST_BLOCK = "pm-architect-tests-v1"
ARCHITECT_TEST_VERSION = 1
ARCHITECT_TEST_PAYLOAD_KEYS: tuple[str, ...] = ("version", "tests")
ARCHITECT_TEST_ROW_KEYS: tuple[str, ...] = (
    "id", "target", "command", "expected", "negative",
)
PM_REVIEW_CONTRACT_PLACEHOLDER_WORDS: tuple[str, ...] = (
    "todo", "tbd", "placeholder", "n/a", "na", "none", "미정", "미기재",
)
_CONTRACT_PLACEHOLDER_RE = re.compile(
    r"<[^>\n]+>|^(?:"
    + "|".join(re.escape(word) for word in PM_REVIEW_CONTRACT_PLACEHOLDER_WORDS)
    + r")(?:\b|$)",
    re.IGNORECASE,
)
PM_REVIEW_CONCRETE_FIX_CONTRACT_RULE = (
    "모든 finding의 fix_contract 여섯 문자열은 구체값으로 채운다 — angle-bracket "
    "metavariable와 parser 금지어("
    + "·".join(PM_REVIEW_CONTRACT_PLACEHOLDER_WORDS)
    + ")를 쓰지 않는다."
)

# 이 문구를 바꾸기 전에 예약된 review seed도 무편집 상태로 계속 읽어야 한다. 새 항목은
# 문장을 바꿀 때만 맨 앞에 덧붙인다(CONFIRM scope 문구의 세대 호환과 같은 계약).
LEGACY_PM_REVIEW_CONCRETE_FIX_CONTRACT_RULES: tuple[str, ...] = ()
PM_REVIEW_VERIFY_PAYLOAD_KEYS: tuple[str, ...] = ("version", "verifications")
PM_REVIEW_VERIFY_ROW_KEYS: tuple[str, ...] = (
    "id", "machine_verifiable", "command", "expected", "before", "reason",
)

# 처방 빈틈 보고 사유 — "이 finding 은 처방에 빈틈이 있어 구현하지 않았다"는 dev 의 **기계
# 선언**이다. 이 값이 없으면 빈틈 보고와 태만이 둘 다 "행의 부재" 한 형상이라
# 기계로 구별되지 않는다.
PM_REVIEW_VERIFY_GAP_REASON = "prescription-gap"

# PM 이 같은 fix 단계에서 직접 닫는 finding 의 닫힌 교차-소유권 표식. disposition 과 developer
# verify 선언 두 축이 정확히 맞아야만 reviewer test/command 계약에서 제외한다.
PM_REVIEW_VERIFY_PM_OWNED_REASON = "pm-owned"
PM_REVIEW_PM_OWNED_SCOPE_PREFIX = "pm-owned:"
PM_REVIEW_PM_OWNED_CONFIRMATION_COMMAND = "pm-owned"

# `machine_verifiable=false` 일 때만 쓰는 닫힌 사유 — dev 선언(불변식 9)의 유일한 어휘.
PM_REVIEW_VERIFY_REASONS: tuple[str, ...] = (
    "design-judgment", "adversarial-probing", "not-reproducible",
    PM_REVIEW_VERIFY_GAP_REASON, PM_REVIEW_VERIFY_PM_OWNED_REASON,
)
PM_REVIEW_MACHINE_CONFIRMATION_PAYLOAD_KEYS: tuple[str, ...] = (
    "version", "round", "confirmations",
)
PM_REVIEW_MACHINE_CONFIRMATION_ROW_KEYS: tuple[str, ...] = (
    "id", "status", "command", "observed",
)

# 재현 커맨드 안전 경계(불변식 12) — 금지 토큰과 그 사람이 읽는 표기를 **한 상수**에 둔다.
# 실제 검사(`_pm_review_command_forbidden_token`)와 사용자 표시(`_pm_review_command_shape_hint`
# → 파서 오류 메시지·verify 골격의 `command` placeholder)가 전부 이 튜플에서 파생하므로 토큰을
# 넣고 빼면 파서 경계와 골격 문구가 함께 뒤집힌다(검사용/표시용 상수 두 벌 금지). 정규식 문자
# 클래스가 아니라 부분문자열 목록이라 `$(` 같은 두 글자 시퀀스도 같은 자리에 들어간다.
# 라벨이 겹치는 토큰(`\r`·`\n` → "개행")은 표시할 때 한 번만 낸다.
_PM_REVIEW_COMMAND_FORBIDDEN_TOKENS: tuple[tuple[str, str], ...] = (
    ("\n", "개행"), ("\r", "개행"),
    (";", "`;`"), ("&", "`&`"), ("|", "`|`"), (">", "`>`"), ("<", "`<`"),
    ("`", "백틱"), ("$(", "`$(`"),
)


def _pm_review_command_forbidden_token(command: str) -> str | None:
    """커맨드에 들어 있는 첫 금지 토큰(없으면 None) — 안전 경계 판정의 유일한 출처."""
    for token, _label in _PM_REVIEW_COMMAND_FORBIDDEN_TOKENS:
        if token in command:
            return token
    return None


def _pm_review_command_shape_hint() -> str:
    """불변식 12(재현 커맨드 안전 경계) 문구의 유일한 출처 — 금지 토큰 상수에서 파생한다."""
    labels: list[str] = []
    for _token, label in _PM_REVIEW_COMMAND_FORBIDDEN_TOKENS:
        if label not in labels:
            labels.append(label)
    return "금지 토큰(" + " ".join(labels) + ") 없는 단일 명령"


_PM_REVIEW_AUTHORITY_REF_RE = re.compile(
    r"\[\[(?:T-(?:[A-Za-z0-9]+-)*\d+|ADR-\d+|[^\]]*[Ss]pec[^\]]*)\]\]"
)

# 라운드를 판정 표면에서 빼는 기계 판독 표식 — **엔진만** 발행한다. 발행 자리는 둘이다: 단일
# 파일 시절의 회수 거부 산출을 그대로 옮겨 온 라운드와, 중간 순번이라 board 예약을 보존한 채
# 종결하는 포기(`abandon_ticket_copy`)다. 역할은 줄 안에 값으로 실린다 — 리뷰 채널뿐 아니라 어떤
# 역할의 라운드도 같은 문법 하나로 표식을 달고 판독은 역할을 가리지 않는다.
# 발행이 엔진 전용이므로 회수 검증은 같은 줄을 실은 외부 산출을 거부한다.
PM_REVIEW_REFUSED_MARKER = "pm-review-refused"

# 판정 기준은 엔진이 **발행하는 그 문법**에서 만든다 — 표식과 판독이 갈리지 않게(문법 일치는
# 역할 전수 회귀가 고정한다).
_PM_REVIEW_REFUSED_LINE_RE = re.compile(
    rf"\A<!-- {re.escape(PM_REVIEW_REFUSED_MARKER)} role=[^\s<>]+ -->\Z"
)


def pm_review_refused_marker_present(text: str) -> bool:
    """본문에 엔진 표식 줄이 있는가 — 역할 무관 단일 판독.

    `pending`(시드 그대로)을 배제하는 소비면들이 이 술어를 함께 본다. 표식이 붙은 라운드는
    바이트가 시드와 달라 `pending` 이 아니게 되므로, 이 판독이 없으면 종결된 라운드가 직전
    산출·판정 표면에 자리표시 골격째로 선다.
    """
    return any(
        _PM_REVIEW_REFUSED_LINE_RE.match(line) is not None
        for line in text.splitlines()
    )


# 확인 라운드의 스코프 문구 단일 진실 — 라운드 시드
# (`_render_review_round_seed_body` 의 HTML 주석)가 이 상수 하나를 embed한다. 손으로 다시 쓰면
# 시드와 무편집 판정이 갈린다.
CONFIRM_ROUND_SCOPE_RULE = (
    "이 라운드는 직전 must-fix의 해소 확인 전용이다 — 신규 탐색은 그 fix diff로 제한하고, "
    "신규 발견은 `NEW`로만 분리해 보고한다. 기존 finding 은 `confirmations` 로만 참조하고 "
    "`findings` 에는 신규 ID 만 쓴다 — 같은 ID 를 양쪽에 실으면 회수가 그 라운드를 거부한다."
)

# 이 문구를 바꾸기 **전에** 예약돼 아직 회수되지 않은 확인 라운드 시드는 board 에 옛 문장으로
# 남아 있다. 무편집 판정(`ticket_round_body_is_pending`)이 현재 문구만 대조하면 그 라운드가
# "산출 있음" 으로 뒤집혀 자리표시자 블록이 실 선언으로 읽히고 티켓 판정 표면을 막는다 —
# 업그레이드 창을 닫는 대조 후보다. 새 항목은 **문장을 바꿀 때만** 맨 앞에 덧붙인다.
LEGACY_CONFIRM_ROUND_SCOPE_RULES = (
    "이 라운드는 직전 must-fix의 해소 확인 전용이다 — 신규 탐색은 그 fix diff로 제한하고, "
    "신규 발견은 `NEW`로만 분리해 보고한다.",
)

# 다음 finding ID 안내 — 시드 주석과 사본 프리앰블이 같은 문장 하나를 쓴다(값 출처는
# `next_review_finding_id` 단일). 추가 리뷰어 채널이 프롬프트 산문으로 싣는 규칙과 같은 축이다.
NEXT_FINDING_ID_RULE = (
    "이번 라운드의 새 finding 은 `{next_id}` 부터 연번으로 매긴다 — finding ID 는 티켓 전역 "
    "유일이라 명세·앞 라운드에 이미 있는 번호를 다시 선언하면 이 라운드는 회수되지 않는다."
)


class PMReviewFinding(NamedTuple):
    id: str
    classification: str
    authority: str
    evidence: str
    recommendation: str
    design_change: bool
    reviewer_ordinal: int
    severity: str
    reviewer_role: str
    # v3 reviewer 산출에만 존재한다. 끝에 기본값을 둬 봉인된 v1/v2 라운드와
    # 기존 호출부를 읽는 호환성은 유지하되, 새 v3 파서는 아래에서 반드시 채운다.
    fix_contract: dict[str, str] | None = None


class PMReviewConfirmation(NamedTuple):
    id: str
    status: str
    evidence: str
    reviewer_ordinal: int
    reviewer_role: str


class PMReviewDisposition(NamedTuple):
    id: str
    decision: str
    reason: str
    scope: str
    prerequisite: str
    reviewer_ordinal: int
    reviewer_role: str


class PMReviewDelta(NamedTuple):
    """accepted-only delta + 확인 커서.

    `confirmation_cursor` 는 finding ID 별로 **다음 기계 확인이 넘어서야 하는 developer 라운드
    순번**이다(인과 floor 와 명세 전역 단조 커서의 최대). 파서만 아는 값이라 여기 실어 보낸다 —
    `verify-template` 이 이 값 없이 stale 을 판정하려면 같은 계산을 두 번 구현해야 하고, 그
    순간 한쪽이 렌더한 골격을 다른 쪽이 거부하는 왕복 비정합이 생긴다.
    """

    accepted: tuple[tuple[PMReviewFinding, PMReviewDisposition], ...]
    finding_zero: bool
    confirmation_cursor: tuple[tuple[str, int], ...] = ()


class PMReviewError(delegate_errors.DelegateError):
    """versioned reviewer/disposition 구조 또는 상태 전이 거부.

    `channels` 는 "pending" 코드에서만 채워지는 구조화 부가 데이터다 — 이 거부가 지목한
    (reviewer_role, ordinal) 쌍 전체(리뷰 라운드 결정). 처방 문구(`_pm_review_prescription`)가
    문자열을 재파싱하지 않고 이 값으로 실행 가능한 `--ordinal`/`--reviewer-role` 커맨드를
    바로 조립한다. `round_seed._render_round_seed_body` 를 건너면 `str(exc)` 로 문자열화되며
    이 구조는 사라진다 — 그 전(`RoundsError` 로 옮기기 전)에서만 유효하다."""

    def __init__(
        self, code: str, message: str,
        *, channels: tuple[tuple[str, int], ...] = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.channels = channels


# 판정을 못 세운 상태로 developer 라운드를 시드하면 채울 자리가 없는 라운드가 나가고, 두 단계
# 뒤 dev 태만처럼 표면화된다. 그래서 이 두 코드는 강등이 아니라 **거부**다. 범위를 더 넓히지
# 않는 근거는 실측이다 — 전 코드 거부는 기존 테스트 6건이 red 이고(대부분 `malformed` 리뷰 산출),
# 이 둘로 좁히면 그 red 가 사라진다. `malformed` 는 리뷰 산출 결함이라 회수 시점이 담당한다.
PM_REVIEW_SEED_BLOCKING_CODES: frozenset[str] = frozenset(
    {"pending", "decision-required"}
)


def _pm_review_prescription(
    code: str, ticket: str, *, channels: tuple[tuple[str, int], ...] = (),
) -> str:
    """판정 거부 코드별 처방 — CLI 거부와 시드 거부가 같은 문언 하나를 쓴다(복제 0).

    `channels` (리뷰 라운드 결정)가 있으면 "pending" 처방은 거부가 지목한 채널·ordinal
    실값으로 채널마다 실행 가능한 커맨드를 낸다 — `disposition-template` 의 `--ordinal`
    기본값은 **그 채널의 최신 라운드**라, 거부가 지목한 라운드가 최신이 아니면(예: 그 뒤에
    새 코드-리뷰 라운드가 열렸다) 안내대로 실행해도 "미판정 finding이 없습니다" 로 다시
    막힌다(실측으로 확인됨). 값이 없으면(다른 코드·구조 없는 옛 예외) 종전 일반 문구로
    fail-soft 한다."""
    if code == "pending" and channels:
        commands = "; ".join(
            "`python3 .project_manager/tools/pm_delegate.py review disposition-template "
            f"--ticket {ticket} --reviewer-role {role} --ordinal {ordinal}`"
            for role, ordinal in channels
        )
        return f"다음 골격을 생성해 PM이 finding ID를 전수 disposition하세요: {commands}"
    prescriptions = {
        # 라운드 축에서 판정을 막는 상태는 순번 유일성·연속성 하나뿐이라 처방도 하나다
        # 봉인·장부 시절의 문제별 처방 분기는 사라졌다.
        "unsealed": (
            "라운드 순번이 깨졌다 — `tickets/rounds/<ticket>/` 의 빠진 순번을 board git "
            "이력에서 복원하라(라운드 파일은 회수 후 불변이다)"
        ),
        "malformed": "reviewer 형식을 versioned block 계약에 맞춰 보정한 뒤 다시 판정하세요",
        "pending": (
            "다음 골격을 생성해 PM이 finding ID를 전수 disposition한 뒤 다시 실행하세요: "
            "`python3 .project_manager/tools/pm_delegate.py review "
            f"disposition-template --ticket {ticket}`"
            " (오류의 대상 채널이 여러 개면 채널마다 "
            f"`--reviewer-role <{ '|'.join(REVIEW_ROLES) }>`를 붙여 반복하세요)"
        ),
        "decision-required": "현재 티켓을 정지하고 필요한 사용자 결정을 요청하세요",
        "repeated-unresolved": (
            "추가 fix/review loop를 열지 말고 현재 티켓을 정지해 사용자에게 보고하세요"
        ),
    }
    return prescriptions[code]


class _PMReviewBlock(NamedTuple):
    kind: str
    start: int
    end: int
    value: dict


def _pm_review_nonempty_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PMReviewError("malformed", f"{field}는 비어 있지 않은 문자열이어야 합니다")
    return value.strip()


def _pm_review_contract_string(value: object, field: str) -> str:
    """v3/architect 계약의 문자열을 실값으로 제한한다.

    빈 값만 거부하면 엔진이 낸 ``<...>`` 골격이 구조화 필드 전체를
    채운 것처럼 보이고, 상관없는 green command로 회수를 닫을 수 있다.
    """
    text = _pm_review_nonempty_string(value, field)
    if _CONTRACT_PLACEHOLDER_RE.search(text):
        raise PMReviewError("malformed", f"{field}를 placeholder가 아닌 실값으로 채워야 합니다")
    return text


def _pm_review_pm_owned_contract_ids(
    delta, verify_rows, *, require_marked: bool = True,
) -> frozenset[str]:
    """PM-owned disposition/verify 쌍만 developer 계약 실행에서 제외한다.

    산문이나 finding 위치로 소유권을 추정하지 않는다. PM accepted scope의 닫힌 prefix와 이번
    developer 라운드의 채워진 false 선언이 같은 ID에서 동시에 있어야 한다. 어느 한쪽만 있으면
    일반 developer-owned finding을 우회하거나 PM-owned 작업을 개발자가 떠안는 형상이므로 loud
    거부한다.
    """
    accepted = {
        finding.id: disposition
        for finding, disposition in delta.accepted
    }
    marked = {
        finding_id
        for finding_id, disposition in accepted.items()
        if disposition.scope.startswith(PM_REVIEW_PM_OWNED_SCOPE_PREFIX)
    }
    declared = {
        finding_id
        for finding_id, row in verify_rows.items()
        if (
            not row.machine_verifiable
            and row.reason == PM_REVIEW_VERIFY_PM_OWNED_REASON
            and bool(row.expected)
        )
    }
    undeclared = sorted(marked - declared) if require_marked else []
    if undeclared:
        raise PMReviewError(
            "malformed",
            "PM-owned accepted scope와 developer verify 선언이 일치하지 않습니다: "
            f"{', '.join(undeclared)} — machine_verifiable=false · "
            f"reason={PM_REVIEW_VERIFY_PM_OWNED_REASON} · expected 실값이 필요합니다"
        )
    impostors = sorted(declared - marked)
    if impostors:
        raise PMReviewError(
            "malformed",
            "developer verify가 PM-owned를 선언했지만 accepted scope 표식이 없습니다: "
            f"{', '.join(impostors)} — scope는 "
            f"{PM_REVIEW_PM_OWNED_SCOPE_PREFIX!r} prefix로 시작해야 합니다"
        )
    return frozenset(marked & declared)


def _pm_review_is_pm_owned_binding(disposition, verify_row) -> bool:
    """한 finding의 strict PM-owned 이중 결속 — parser/resolve가 같은 술어를 쓴다."""
    return bool(
        disposition is not None
        and disposition.decision == "accepted"
        and disposition.scope.startswith(PM_REVIEW_PM_OWNED_SCOPE_PREFIX)
        and not verify_row.machine_verifiable
        and verify_row.reason == PM_REVIEW_VERIFY_PM_OWNED_REASON
        and verify_row.expected
    )


def _pm_review_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """JSON object의 raw member를 보존해 모든 깊이에서 중복 key를 거부한다."""
    value: dict[str, object] = {}
    for key, member in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON member: {key!r}")
        value[key] = member
    return value


def _pm_review_json_blocks(text: str) -> list[_PMReviewBlock]:
    """정확한 versioned fence만 읽고 손상/미종결/중첩을 fail-closed한다."""
    lines = text.splitlines(keepends=True)
    offsets: list[int] = []
    offset = 0
    for line in lines:
        offsets.append(offset)
        offset += len(line)
    blocks: list[_PMReviewBlock] = []
    index = 0
    supported = {
        PM_REVIEW_BLOCK, PM_REVIEW_DISPOSITION_BLOCK,
        PM_REVIEW_VERIFY_BLOCK, PM_REVIEW_CONFIRMATION_BLOCK,
    }
    while index < len(lines):
        line = lines[index].rstrip("\r\n")
        stripped = line.lstrip()
        fence_candidate = _PM_REVIEW_FENCE_CANDIDATE_RE.match(stripped)
        if fence_candidate is None:
            index += 1
            continue
        candidate = fence_candidate.group(1)
        if line != f"```{candidate}" or candidate not in supported:
            raise PMReviewError(
                "malformed",
                f"지원하지 않거나 손상된 review fence: {line!r}",
            )
        closing = index + 1
        while closing < len(lines) and lines[closing].rstrip("\r\n") != "```":
            if lines[closing].startswith("```"):
                raise PMReviewError("malformed", f"{candidate} block 안 fence 중첩")
            closing += 1
        if closing >= len(lines):
            raise PMReviewError("malformed", f"{candidate} block 종료 fence 누락")
        payload = "".join(lines[index + 1:closing])
        try:
            value = json.loads(payload, object_pairs_hook=_pm_review_json_object)
        except ValueError as exc:
            raise PMReviewError("malformed", f"{candidate} JSON 파싱 실패: {exc}") from exc
        if not isinstance(value, dict):
            raise PMReviewError("malformed", f"{candidate} payload는 JSON object여야 합니다")
        blocks.append(_PMReviewBlock(
            candidate,
            offsets[index],
            offsets[closing] + len(lines[closing]),
            value,
        ))
        index = closing + 1
    return blocks


def _pm_review_exact_keys(
    value: dict, expected: Sequence[str], label: str,
) -> None:
    expected_keys = set(expected)
    if set(value) != expected_keys:
        missing = sorted(expected_keys - set(value))
        extra = sorted(set(value) - expected_keys)
        raise PMReviewError(
            "malformed",
            f"{label} schema 불일치(missing={missing}, extra={extra})",
        )


def _pm_review_version(
    value: dict, label: str, *, allowed: Sequence[int] | None = None,
) -> int:
    """블록 payload 세대를 검증하고 그 값을 돌려준다(호출부가 세대별 규칙에 쓴다)."""
    accepted = tuple(PM_REVIEW_SUPPORTED_VERSIONS if allowed is None else allowed)
    version = value.get("version")
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version not in accepted
    ):
        expected = " 또는 ".join(str(item) for item in accepted)
        raise PMReviewError(
            "malformed", f"{label} version은 정수 {expected}이어야 합니다"
        )
    return version


def _pm_review_finding_id_prefix(reviewer_role: str) -> str:
    """채널의 finding ID 접두 — 미등록 역할은 review 채널이 아니므로 loud 실패다."""
    prefix = PM_REVIEW_FINDING_ID_PREFIXES.get(reviewer_role)
    if prefix is None:
        raise PMReviewError("malformed", f"review 채널이 아닌 역할: {reviewer_role}")
    return prefix


def _pm_review_assert_id_namespace(
    finding_id: str, reviewer_role: str, field: str,
) -> None:
    """채널 접두를 강제해 두 채널의 finding ID 네임스페이스가 겹치지 않게 한다."""
    prefix = _pm_review_finding_id_prefix(reviewer_role)
    if not finding_id.startswith(f"{prefix}-"):
        raise PMReviewError(
            "malformed",
            f"{field} 채널 접두 불일치: {finding_id!r} — {reviewer_role} 는 "
            f"{prefix}-NNN 형식이어야 합니다",
        )


def _pm_review_finding_keys(version: int) -> tuple[str, ...]:
    """세대별 finding key 집합 — v1 은 severity 이전 스키마다(부재 허용·존재도 허용)."""
    if version >= 3:
        return PM_REVIEW_FINDING_KEYS
    if version >= PM_REVIEW_SEVERITY_MIN_VERSION:
        return PM_REVIEW_LEGACY_FINDING_KEYS
    return tuple(key for key in PM_REVIEW_LEGACY_FINDING_KEYS if key != "severity")


def _pm_review_parse_finding(
    value: object, reviewer_ordinal: int,
    *, reviewer_role: str = INTERNAL_REVIEW_ROLE,
    version: int = PM_REVIEW_VERSION,
) -> PMReviewFinding:
    if not isinstance(value, dict):
        raise PMReviewError("malformed", "finding은 JSON object여야 합니다")
    # v1 블록은 severity 를 안 실었지만 실었을 수도 있다(전환기 산출). 두 key 집합 중
    # **하나와 정확히** 일치해야 하며, v2 는 severity 를 포함한 집합만 받는다.
    expected_keys = _pm_review_finding_keys(version)
    if version < PM_REVIEW_SEVERITY_MIN_VERSION and set(value) == set(
        PM_REVIEW_LEGACY_FINDING_KEYS
    ):
        expected_keys = PM_REVIEW_LEGACY_FINDING_KEYS
    _pm_review_exact_keys(value, expected_keys, "finding")
    finding_id = _pm_review_nonempty_string(value["id"], "finding.id")
    prefix = _pm_review_finding_id_prefix(reviewer_role)
    if _PM_REVIEW_ID_RE.fullmatch(finding_id) is None:
        raise PMReviewError(
            "malformed",
            f"finding.id 형식 불일치: {finding_id!r} (예: {prefix}-001)",
        )
    _pm_review_assert_id_namespace(finding_id, reviewer_role, "finding.id")
    classification = _pm_review_nonempty_string(value["class"], "finding.class")
    if classification not in PM_REVIEW_CLASSES:
        raise PMReviewError("malformed", f"finding.class 미지원: {classification!r}")
    # severity 는 v2 부터 필수다. 경계는 **블록 세대**이지 티켓 상태가 아니다 — 파서는 진행 중
    # 티켓의 옛 라운드 블록도 계속 읽고(봉인돼 손댈 수 없다), 그 블록은 v1 이라 부재가 정상이다.
    # 부재는 빈 문자열로 싣고 렌더가 '미기재'로 표기한다(산문 추론 금지).
    severity = ""
    if "severity" in value:
        severity = _pm_review_nonempty_string(value["severity"], "finding.severity")
        if severity not in PM_REVIEW_SEVERITIES:
            raise PMReviewError("malformed", f"finding.severity 미지원: {severity!r}")
    if not isinstance(value["design_change"], bool):
        raise PMReviewError("malformed", "finding.design_change는 boolean이어야 합니다")
    fix_contract: dict[str, str] | None = None
    if version >= 3:
        raw_contract = value.get("fix_contract")
        if not isinstance(raw_contract, dict):
            raise PMReviewError("malformed", "finding.fix_contract는 JSON object여야 합니다")
        _pm_review_exact_keys(
            raw_contract, PM_REVIEW_FIX_CONTRACT_KEYS, "finding.fix_contract",
        )
        fix_contract = {
            key: _pm_review_contract_string(
                raw_contract[key], f"finding.fix_contract.{key}",
            )
            for key in PM_REVIEW_FIX_CONTRACT_KEYS
        }
        _pm_review_assert_verify_command_shape(
            fix_contract["command"], "finding.fix_contract.command",
        )
    return PMReviewFinding(
        finding_id,
        classification,
        _pm_review_nonempty_string(value["authority"], "finding.authority"),
        _pm_review_nonempty_string(value["evidence"], "finding.evidence"),
        _pm_review_nonempty_string(value["recommendation"], "finding.recommendation"),
        value["design_change"],
        reviewer_ordinal,
        severity,
        reviewer_role,
        fix_contract,
    )


def _pm_review_parse_confirmation(
    value: object, reviewer_ordinal: int,
    *, reviewer_role: str = INTERNAL_REVIEW_ROLE,
) -> PMReviewConfirmation:
    if not isinstance(value, dict):
        raise PMReviewError("malformed", "confirmation은 JSON object여야 합니다")
    _pm_review_exact_keys(value, PM_REVIEW_CONFIRMATION_KEYS, "confirmation")
    finding_id = _pm_review_nonempty_string(value["id"], "confirmation.id")
    _pm_review_assert_id_namespace(finding_id, reviewer_role, "confirmation.id")
    status = _pm_review_nonempty_string(value["status"], "confirmation.status")
    if status not in PM_REVIEW_CONFIRMATION_STATES:
        raise PMReviewError("malformed", f"confirmation.status 미지원: {status!r}")
    return PMReviewConfirmation(
        finding_id,
        status,
        _pm_review_nonempty_string(value["evidence"], "confirmation.evidence"),
        reviewer_ordinal,
        reviewer_role,
    )


def _pm_review_parse_disposition(
    value: object, reviewer_ordinal: int,
    *, reviewer_role: str = INTERNAL_REVIEW_ROLE,
) -> PMReviewDisposition:
    if not isinstance(value, dict):
        raise PMReviewError("malformed", "disposition은 JSON object여야 합니다")
    _pm_review_exact_keys(value, PM_REVIEW_DISPOSITION_KEYS, "disposition")
    finding_id = _pm_review_nonempty_string(value["id"], "disposition.id")
    decision = _pm_review_nonempty_string(value["decision"], "disposition.decision")
    if decision not in PM_REVIEW_DECISIONS:
        raise PMReviewError("malformed", f"disposition.decision 미지원: {decision!r}")
    reason = _pm_review_nonempty_string(value["reason"], "disposition.reason")
    scope = value["scope"]
    prerequisite = value["prerequisite"]
    if not isinstance(scope, str) or not isinstance(prerequisite, str):
        raise PMReviewError("malformed", "disposition scope/prerequisite는 문자열이어야 합니다")
    scope, prerequisite = scope.strip(), prerequisite.strip()
    if decision == "accepted" and not scope:
        raise PMReviewError("malformed", f"accepted {finding_id}는 허용 scope가 필요합니다")
    if decision == "rejected" and scope:
        raise PMReviewError("malformed", f"rejected {finding_id}의 허용 scope는 비어야 합니다")
    if decision == "decision-required" and (scope or not prerequisite):
        raise PMReviewError(
            "malformed",
            f"decision-required {finding_id}는 빈 scope와 비어 있지 않은 prerequisite가 필요합니다",
        )
    return PMReviewDisposition(
        finding_id, decision, reason, scope, prerequisite, reviewer_ordinal,
        reviewer_role,
    )


def _pm_review_disposition_role(value: dict) -> str:
    """disposition block 의 채널 — 필드 부재는 code-reviewer 로 해석한다(기존 티켓 호환)."""
    raw = value.get(PM_REVIEW_DISPOSITION_ROLE_KEY, INTERNAL_REVIEW_ROLE)
    if raw not in REVIEW_ROLES:
        raise PMReviewError(
            "malformed",
            f"disposition {PM_REVIEW_DISPOSITION_ROLE_KEY} 미지원: {raw!r}",
        )
    return raw


def _pm_review_disposition_payload_keys(
    value: dict, base_keys: Sequence[str],
) -> tuple[str, ...]:
    """채널 필드가 없는 기존 블록은 그 key 를 뺀 집합으로 정확-일치 검증한다."""
    if PM_REVIEW_DISPOSITION_ROLE_KEY in value:
        return tuple(base_keys)
    return tuple(
        key for key in base_keys if key != PM_REVIEW_DISPOSITION_ROLE_KEY
    )


def _pm_review_seed_object(
    keys: Sequence[str], values: Mapping[str, object],
) -> dict[str, object]:
    """파서 key 상수의 현재 값 그대로 ordered skeleton object를 만든다."""
    return {key: values.get(key, "") for key in keys}


class _PMReviewRawPlaceholder(str):
    """골격 자리가 비문자열 값(JSON boolean 등)이어야 함을 표시한다.

    `_pm_review_render_json` 은 이 값을 다른 문자열처럼 따옴표로 감싸지 않고 그대로 낸다 —
    dev 가 "자리표시자만 갈아 끼우는" 정상적 편집을 해도(예: `<true|false>` → `true`) 결과가
    유효 JSON boolean 이 되게 하려는 것이다. `"<true|false>"` 처럼 따옴표 안에 두면 자리표시자만
    바꾼 결과가 문자열 `"true"` 가 돼 파서(boolean 요구)가 거부한다.
    """


def _pm_review_render_json(value: object) -> str:
    """`json.dumps` 대체 — `_PMReviewRawPlaceholder` 는 따옴표 없이 렌더하고 그 외에는
    `json.dumps(..., ensure_ascii=False, separators=(",", ":"))` 와 산출이 같다.

    versioned block 골격을 내는 모든 자리(`_pm_review_block_text`·`_pm_review_fenced_json`·
    disposition 템플릿)가 이 함수 하나만 거쳐 비문자열 자리 placeholder 표기 규칙이 갈리지
    않는다(클래스 폐쇄)."""
    if isinstance(value, _PMReviewRawPlaceholder):
        return str(value)
    if isinstance(value, dict):
        body = ",".join(
            f"{json.dumps(key, ensure_ascii=False)}:{_pm_review_render_json(item)}"
            for key, item in value.items()
        )
        return "{" + body + "}"
    if isinstance(value, list):
        return "[" + ",".join(_pm_review_render_json(item) for item in value) + "]"
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def render_architect_test_skeleton() -> str:
    """architect가 채울 필수 테스트 계약. 실행 게이트와 같은 key 집합에서 렌더한다."""
    row = _pm_review_seed_object(ARCHITECT_TEST_ROW_KEYS, {
        "id": "AT-001",
        "target": (
            "<테스트 파일/케이스 또는 검증 대상 · 첫 공백 구분 토큰 하나만 결속한다"
            "(여러 파일은 계약 행을 여러 개 쓴다)>"
        ),
        "command": "<shell 메타문자 없는 단일 테스트 명령>",
        "expected": "<성공 output에 포함될 짧은 문자열>",
        "negative": "<변경 전 실패하거나 거부돼야 하는 음성 사례>",
    })
    payload = _pm_review_seed_object(ARCHITECT_TEST_PAYLOAD_KEYS, {
        "version": ARCHITECT_TEST_VERSION,
        "tests": [row],
    })
    return f"```{ARCHITECT_TEST_BLOCK}\n{_pm_review_render_json(payload)}\n```\n"


def _pm_review_block_text(payload: Mapping[str, object]) -> str:
    """`pm-review-v1` fence 한 개를 렌더한다 — 골격과 기준선 프로브가 같은 표기를 쓴다."""
    return f"```{PM_REVIEW_BLOCK}\n" + _pm_review_render_json(payload) + "\n```\n"


def _pm_review_fenced_json(kind: str, payload: Mapping[str, object]) -> str:
    """`kind` fence 한 개를 렌더한다 — verify/confirmation 골격 공용(disposition 은 기존 자리)."""
    return (
        f"```{kind}\n"
        + _pm_review_render_json(payload)
        + "\n```\n"
    )


class PMReviewVerifyRow(NamedTuple):
    """dev 가 남기는 재현 커맨드 1행 — `machine_verifiable` 에 따라 나머지 필드 계약이 갈린다."""

    id: str
    machine_verifiable: bool
    command: str
    expected: str
    before: str
    reason: str


class PMReviewMachineConfirmation(NamedTuple):
    """PM 이 남기는 기계 확인 1행 — `round` 는 그 확인이 참조한 developer 라운드 순번이다."""

    id: str
    status: str
    command: str
    observed: str
    round: int


def _pm_review_assert_any_channel_id(finding_id: str, field: str) -> None:
    """verify/confirmation 행 id 가 알려진 리뷰 채널 접두 중 하나인지 — 채널별 assert 재사용.

    verify/confirmation 블록은 특정 리뷰 채널에 묶이지 않는다(developer 라운드·명세 PM 영역에
    각각 산다) — 그래서 `_pm_review_assert_id_namespace` 를 채널마다 시도해, 어느 한 채널
    접두와 일치하면 통과시킨다(같은 규칙 재사용 · 새 접두 어휘를 만들지 않는다)."""
    for role in REVIEW_ROLES:
        try:
            _pm_review_assert_id_namespace(finding_id, role, field)
            return
        except PMReviewError:
            continue
    raise PMReviewError(
        "malformed",
        f"{field} 채널 접두 불일치: {finding_id!r} — "
        f"{'/'.join(PM_REVIEW_FINDING_ID_PREFIXES.values())}-NNN 형식이어야 합니다",
    )


def _pm_review_assert_verify_command_shape(command: str, field: str) -> None:
    """불변식 12 — 재현 커맨드는 금지 토큰(개행·셸 메타문자) 없는 단일 명령이어야 한다."""
    if _pm_review_command_forbidden_token(command) is not None:
        raise PMReviewError(
            "malformed",
            f"{field} 는 {_pm_review_command_shape_hint()}이어야 합니다: {command!r}",
        )


def _pm_review_parse_verify_row(value: object) -> PMReviewVerifyRow:
    if not isinstance(value, dict):
        raise PMReviewError("malformed", "verify 행은 JSON object여야 합니다")
    _pm_review_exact_keys(value, PM_REVIEW_VERIFY_ROW_KEYS, PM_REVIEW_VERIFY_BLOCK)
    finding_id = _pm_review_nonempty_string(value["id"], "verify.id")
    _pm_review_assert_any_channel_id(finding_id, "verify.id")
    machine_verifiable = value["machine_verifiable"]
    if not isinstance(machine_verifiable, bool):
        raise PMReviewError("malformed", "verify.machine_verifiable은 boolean이어야 합니다")
    raw_fields = (value["command"], value["expected"], value["before"], value["reason"])
    if not all(isinstance(item, str) for item in raw_fields):
        raise PMReviewError(
            "malformed", "verify.command/expected/before/reason은 문자열이어야 합니다",
        )
    raw_command = raw_fields[0]
    if machine_verifiable:
        # 불변식 12(F-003 fix) — 개행·메타문자 검사는 raw command 에 먼저 하고, 공백 정규화
        # (strip)는 그 뒤다. strip 을 먼저 하면 선두/후미 개행이 검사 전에 지워져 malformed 가
        # 통과해버린다(위치 무관 거부 계약 위반).
        _pm_review_assert_verify_command_shape(raw_command, f"verify {finding_id}.command")
    command, expected, before, reason = (item.strip() for item in raw_fields)
    if machine_verifiable:
        if not command or not expected or not before:
            raise PMReviewError(
                "malformed",
                f"verify {finding_id}는 machine_verifiable=true 면 "
                "command/expected/before 가 필요합니다",
            )
        if reason:
            raise PMReviewError(
                "malformed", f"verify {finding_id}는 machine_verifiable=true 면 reason이 비어야 합니다",
            )
        if before == expected:
            raise PMReviewError(
                "malformed", f"verify {finding_id}의 before는 expected와 달라야 합니다",
            )
    else:
        if command or before:
            raise PMReviewError(
                "malformed",
                f"verify {finding_id}는 machine_verifiable=false 면 command/before가 비어야 합니다",
            )
        if not expected:
            raise PMReviewError(
                "malformed", f"verify {finding_id}는 expected(무엇이 참이어야 하는가)가 필요합니다",
            )
        if reason not in PM_REVIEW_VERIFY_REASONS:
            raise PMReviewError("malformed", f"verify {finding_id}.reason 미지원: {reason!r}")
    return PMReviewVerifyRow(finding_id, machine_verifiable, command, expected, before, reason)


def _pm_review_verify_row_is_unfilled(value: object) -> bool:
    """시드 자리표시자 그대로인 행 = 아직 **선언이 아니다**.

    판별 신호는 `machine_verifiable` 하나다 — 골격은 그 자리에 따옴표 없는 raw 자리표시자를 싣고
    (구조 스캔 전에 같은 자리 한 곳만 재-인용되어 문자열로 들어온다) 파서는 boolean 을 요구하므로,
    boolean 이 아닌 행은 어떤 선언도 담고 있지 않다. 이 행을 malformed 로
    올리면 (a) 같은 라운드에서 실제로 채워진 다른 ID 의 확인까지 통째로 막히고(라운드 파일은
    회수 뒤 불변이라 영구 차단이다) (b) 태만 처방("골격을 채우세요") 대신 형식 보정 처방이 나간다.
    선언이 없는 것으로 접으면 그 ID 는 분류기에서 `missing`(태만)이 되어 rc≠0 으로 남는다.
    """
    return isinstance(value, dict) and not isinstance(
        value.get("machine_verifiable"), bool
    )


# verify 골격의 boolean 자리 raw placeholder 토큰 — 렌더(`render_pm_review_verify_skeleton`)와
# 구조 스캔 전용 자기 인식(`_pm_review_requote_verify_placeholder`)이 이 상수 하나만 쓴다.
# 따옴표 없는 토큰이라 JSON 값 자리에 그대로 있으면 strict 파서가 거부하는데(의도),
# **손대지 않은 골격 자체**를 구조적으로 다시 인식해야 하는 두 자리(pending 판정·delta 의
# developer 라운드 fence 존재 스캔)만 이 상수로 임시 재-인용(re-quote)해 fence 구조를 본다.
_PM_REVIEW_MACHINE_VERIFIABLE_PLACEHOLDER = "<true|false>"
_PM_REVIEW_MACHINE_VERIFIABLE_KEY = "machine_verifiable"

# 재-인용 대상은 **딱 한 자리**다 — verify fence 안에서 `"machine_verifiable"` key 바로 뒤에
# 오는 raw placeholder 값. 앞의 `(?<!\\)` 는 다른 문자열 필드 안에 escape 된 같은 문구
# (`\"machine_verifiable\":<true|false>`)가 들어와도 값 자리로 오인하지 않게 한다. 전역 치환은
# 금지다 — `expected` 같은 정상 문자열 필드가 같은 토큰을 담으면 유효한 행이 malformed 로
# 죽는다(지연 파싱 불변식 역방향 퇴행).
_PM_REVIEW_MACHINE_VERIFIABLE_SLOT_RE = re.compile(
    r'(?<!\\)"' + re.escape(_PM_REVIEW_MACHINE_VERIFIABLE_KEY) + r'"([ \t]*:[ \t]*)'
    + re.escape(_PM_REVIEW_MACHINE_VERIFIABLE_PLACEHOLDER)
)


def _pm_review_requote_verify_placeholder(text: str) -> str:
    """구조 스캔 전용 전처리 — verify 골격의 raw placeholder(`machine_verifiable`)를 임시로
    다시 따옴표에 넣어 `_pm_review_json_blocks` 가 fence 존재/경계를 볼 수 있게 한다.

    치환 범위는 verify fence 안의 `machine_verifiable` 값 자리로 한정한다 — fence 밖 본문이나
    다른 필드(`expected` 등)에 같은 토큰이 들어 있어도 건드리지 않는다.

    이 재-인용은 fence 를 찾고 세는 **구조 스캔에만** 쓴다 — verify 행의 실제 값 검증
    (`_pm_review_parse_verify_row`)은 항상 이 함수를 거치지 않은 원문을 그대로 보므로 boolean
    타입 요구가 느슨해지지 않는다(관용이 아니라 자기 인식). dev 가 실제로 채운
    `true`/`false` 는 이미 유효 JSON 이라 이 치환이 아무 것도 바꾸지 않는다."""
    def requote(match: re.Match[str]) -> str:
        return (
            f'"{_PM_REVIEW_MACHINE_VERIFIABLE_KEY}"' + match.group(1)
            + f'"{_PM_REVIEW_MACHINE_VERIFIABLE_PLACEHOLDER}"'
        )

    open_fence = f"```{PM_REVIEW_VERIFY_BLOCK}"
    out: list[str] = []
    inside_verify_fence = False
    for line in text.splitlines(keepends=True):
        bare = line.rstrip("\r\n")
        if inside_verify_fence:
            if bare == "```":
                inside_verify_fence = False
            else:
                line = _PM_REVIEW_MACHINE_VERIFIABLE_SLOT_RE.sub(requote, line)
        elif bare == open_fence:
            inside_verify_fence = True
        out.append(line)
    return "".join(out)


# 시드가 프리필하는 값 자리 — key 집합은 파서 상수에서 파생한다. `id` 는 행 좌표이고
# `machine_verifiable` 은 **선언 신호**라 시드는 그 자리를 절대 채우지 않는다(불변식 1의 verify
# 확장): 그 한 자리가 자리표시자인 한 손대지 않은 시드는 값이 실려 있어도 산출 없음으로 읽힌다.
_PM_REVIEW_VERIFY_SEED_VALUE_KEYS: tuple[str, ...] = tuple(
    key for key in PM_REVIEW_VERIFY_ROW_KEYS
    if key not in ("id", _PM_REVIEW_MACHINE_VERIFIABLE_KEY)
)


def _pm_review_verify_seed_values(row: PMReviewVerifyRow) -> dict[str, str]:
    """선언 행에서 시드가 다시 실을 값 — 필드 이름이 곧 key 라 대응표를 따로 두지 않는다."""
    return {key: getattr(row, key) for key in _PM_REVIEW_VERIFY_SEED_VALUE_KEYS}


# 값 자리(4칸)는 프리필이 통째로 덮는 자리라 규칙을 거기 적으면 프리필된 라운드에서 규칙이
# 사라진다. 값 자리에 남는 것은 자리표시자 최소형뿐이고, 규칙은 블록 **위** 주석 한 줄이 소유한다
# (fence 밖이라 어떤 블록 파서의 정의역에도 들어가지 않는다).
PM_REVIEW_VERIFY_VALUE_RULES = (
    "reason 은 machine_verifiable=false 일 때만 채운다. "
    "expected 는 그 커맨드 output 에 그대로 나오는 짧은 부분 문자열만 적는다"
    "(수치·핵심 토큰 — 산문 설명은 이 자리가 아니라 라운드 본문에 쓴다). "
    "확인은 rc 0 이고 expected 가 출력에 포함돼야 통과다 — 부재 단언은 `grep -L`/`-c 0` 이 "
    "rc 1 이라 통과할 수 없으니 실제 잔여 건수를 기대값으로 쓴다."
)


def render_pm_review_verify_skeleton(
    rows: Sequence[tuple[str, Mapping[str, str] | None]],
) -> str:
    """accepted finding마다 재현 커맨드/기대값 1행 — key 집합·enum 은 파서 상수 파생.

    입력은 `(finding ID, 프리필 값 또는 None)` 이다. 프리필 값이 있으면 그 ID 의 **최신 선언**을
    그대로 다시 싣고(값 자리 4칸), 없으면 자리표시자 골격을 낸다 — 한 블록 안에 두 형상이 섞인다.
    `machine_verifiable` 은 어느 쪽이든 항상 자리표시자다(`_PM_REVIEW_VERIFY_SEED_VALUE_KEYS`).

    `machine_verifiable` 은 boolean 자리라 `_PMReviewRawPlaceholder` 로 따옴표 없이 낸다
    (axis 1). `command` placeholder 는 파서의 재현 커맨드 안전 경계 문구(`_pm_review_command_shape_
    hint`)를 그대로 소비해 금지 문자를 두 곳에 적지 않는다(axis 2). `expected` 의 부분 문자열
    계약(axis 3)과 `reason` 의 선언 조건, 확인 커맨드의 rc 규약은 프리필이 덮지 않는
    블록 위 주석(`PM_REVIEW_VERIFY_VALUE_RULES`)이 소유한다."""
    command_hint = (
        "<machine_verifiable=true 면 " + _pm_review_command_shape_hint()
        + "(cwd 무관하게 대상을 절대경로로 쓰고 `cd X &&` 는 쓰지 않는다 — `&`가 금지 문자다), "
        "아니면 빈 문자열>"
    )
    expected_hint = (
        "<machine_verifiable=true 면 fix 후 그 커맨드 output 의 기대값, false 면 무엇이 "
        f"참이어야 하는지 한 줄, reason={PM_REVIEW_VERIFY_GAP_REASON} 이면 빈틈 요지 한 줄>"
    )
    hints = {
        "command": command_hint,
        "expected": expected_hint,
        "before": "<machine_verifiable=true 면 fix 전 실값, 아니면 빈 문자열>",
        "reason": "<" + "|".join(PM_REVIEW_VERIFY_REASONS) + " 중 하나 또는 빈 문자열>",
    }
    seed_rows = [
        _pm_review_seed_object(PM_REVIEW_VERIFY_ROW_KEYS, {
            "id": finding_id,
            _PM_REVIEW_MACHINE_VERIFIABLE_KEY: _PMReviewRawPlaceholder(
                _PM_REVIEW_MACHINE_VERIFIABLE_PLACEHOLDER,
            ),
            **{key: (hints if prefill is None else prefill)[key]
               for key in _PM_REVIEW_VERIFY_SEED_VALUE_KEYS},
        })
        for finding_id, prefill in rows
    ]
    payload = _pm_review_seed_object(PM_REVIEW_VERIFY_PAYLOAD_KEYS, {
        "version": PM_REVIEW_VERIFY_VERSION,
        "verifications": seed_rows,
    })
    return (
        f"<!-- {PM_REVIEW_VERIFY_VALUE_RULES} -->\n"
        + _pm_review_fenced_json(PM_REVIEW_VERIFY_BLOCK, payload)
    )


def _pm_review_parse_machine_confirmation_row(
    value: object, *, round_ordinal: int,
) -> PMReviewMachineConfirmation:
    if not isinstance(value, dict):
        raise PMReviewError("malformed", "confirmation 행은 JSON object여야 합니다")
    _pm_review_exact_keys(
        value, PM_REVIEW_MACHINE_CONFIRMATION_ROW_KEYS, PM_REVIEW_CONFIRMATION_BLOCK,
    )
    finding_id = _pm_review_nonempty_string(value["id"], "confirmation.id")
    _pm_review_assert_any_channel_id(finding_id, "confirmation.id")
    status = _pm_review_nonempty_string(value["status"], "confirmation.status")
    if status not in PM_REVIEW_CONFIRMATION_STATES:
        raise PMReviewError("malformed", f"confirmation.status 미지원: {status!r}")
    command = _pm_review_nonempty_string(value["command"], "confirmation.command")
    observed = _pm_review_nonempty_string(value["observed"], "confirmation.observed")
    return PMReviewMachineConfirmation(finding_id, status, command, observed, round_ordinal)


def render_pm_review_block_skeleton(
    reviewer_role: str, confirmation_ids: Sequence[str] | None = None,
    next_finding_id: str | None = None,
) -> str:
    """채널의 `pm-review-v1` 골격 블록 — 값 enum·key 집합을 파서 상수에서만 파생한다.

    리뷰 절 골격(section-add)과 추가 리뷰어 프롬프트의 출력 형식이 같은 한 함수를 본다. 두 곳이
    스키마를 각자 적으면 한쪽만 갱신돼 회수가 조용히 malformed 로 떨어진다.

    `next_finding_id` 는 이 라운드가 쓸 첫 finding ID 실값이다. 미지정(기본)이면 자리표시자
    골격 그대로다 — 추가 리뷰어 프롬프트는 그 값을 산문 규칙으로 싣고 골격은 자리표시자를
    유지하므로, 기본 인자의 bytes 는 이 인자가 생겨도 변하지 않는다.
    """
    placeholder_id = f"{_pm_review_finding_id_prefix(reviewer_role)}-NNN"
    finding = _pm_review_seed_object(PM_REVIEW_FINDING_KEYS, {
        "id": next_finding_id or placeholder_id,
        "class": "<" + "|".join(PM_REVIEW_CLASSES) + ">",
        "severity": "<" + "|".join(PM_REVIEW_SEVERITIES) + ">",
        "fix_contract": _pm_review_seed_object(PM_REVIEW_FIX_CONTRACT_KEYS, {
            "location": "<잘못된 코드 위치(file:line 또는 symbol)>",
            "failure": "<현재 잘못된 거동과 재현 조건>",
            "design": "<수정 설계와 보존할 불변식>",
            "test": (
                "<추가할 회귀 테스트 파일/케이스 · 첫 공백 구분 토큰 하나만 결속한다"
                "(여러 파일은 계약 행을 여러 개 쓴다)>"
            ),
            "command": "<추가 테스트 실행 명령(shell 메타문자 없는 단일 명령)>",
            "expected": "<성공 output에 포함될 짧은 문자열>",
        }),
        "design_change": False,
    })
    confirmations = [
        _pm_review_seed_object(PM_REVIEW_CONFIRMATION_KEYS, {
            "id": finding_id,
            "status": "<" + "|".join(PM_REVIEW_CONFIRMATION_STATES) + ">",
        })
        for finding_id in (
            [placeholder_id] if confirmation_ids is None else confirmation_ids
        )
    ]
    payload = _pm_review_seed_object(PM_REVIEW_PAYLOAD_KEYS, {
        "version": PM_REVIEW_VERSION,
        "findings": [finding],
        "confirmations": confirmations,
    })
    return _pm_review_block_text(payload)
