"""리뷰어 답신의 판정 해석 (층: domain).

판정 낱말 정확일치, 내부 리뷰 산문 판정·must-fix 추출과 진단, 기계 블록 축과 산문 축의 결합 판정을 소유한다.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from typing import NamedTuple

from pm_engine.domain import review_form


INTERNAL_DIAGNOSTIC_MISSING_VERDICT = "missing-verdict-word"
INTERNAL_DIAGNOSTIC_CONFLICTING_VERDICT = "conflicting-verdict-words"
INTERNAL_DIAGNOSTIC_PASS_WITHOUT_ZERO = "pass-without-zero-must-fix"
INTERNAL_DIAGNOSTIC_REJECT_WITHOUT_ITEMS = "reject-without-must-fix-items"

# 라운드 산출 bytes 의 두 판정 축(기계 블록 · 산문)이 서로 다른 값을 세운 형상 — 어느 쪽도
# 기록하지 않고 두 값을 함께 싣는다(판정 불능을 한쪽 채택으로 위장하지 않는다).
INTERNAL_DIAGNOSTIC_VERDICT_CONFLICT = "block-reply-verdict-conflict"

# 기계 블록 축이 판정을 세우지 못한 사유(블록 부재·손상·severity 미기재).
INTERNAL_DIAGNOSTIC_BLOCK_UNUSABLE = "block-axis-unusable"
INTERNAL_VERDICT_SOURCE_BLOCK = "block"
INTERNAL_VERDICT_SOURCE_REPLY = "reply"
_INTERNAL_MUST_FIX_HEADER_RE = re.compile(
    r"^\s{0,3}(?P<decorator>#{1,6}\s*|\*{2})?must[- ]fix\*{0,2}"
    r"(?:\s*\([^)]*\))?\s*(?P<colon>:)?[ \t]*(?P<inline>.*)$",
    re.IGNORECASE,
)
_INTERNAL_MUST_FIX_COUNT_DECLARATION_RE = re.compile(
    r"^\s{0,3}must[- ]fix\s+\d+\s*건(?:이)?\s+남았습니다\.?\s*$",
    re.IGNORECASE,
)
_INTERNAL_SECTION_HEADER_RE = re.compile(r"^\s{0,3}#{1,6}\s+\S")
_INTERNAL_BOLD_HEADER_RE = re.compile(r"^\s*\*\*[^*]+\*\*\s*:?[ \t]*$")
_INTERNAL_PLAIN_SECTION_RE = re.compile(
    r"^(?:나머지\s+판정|정상\s+확인\s+항목|지정\s+회귀|회귀|테스트|NEW)\s*:\s*$",
    re.IGNORECASE,
)

# 빈 줄 뒤 평문 절 제목은 문서 최상위(들여쓰기 0)만 받는다. 1~3칸 들여쓴 줄은 실측
# 실물처럼 최상위 must-fix 항목의 설명일 수 있어 절 경계로 자르면 계수가 6→1로 퇴행한다.
_INTERNAL_PLAIN_TEXT_RE = re.compile(r"^\S")
_INTERNAL_TABLE_ROW_RE = re.compile(r"^\s{0,3}\|")
_INTERNAL_LIST_ITEM_RE = re.compile(
    r"^(?P<indent> {0,3})(?:[-*+]|\d+[.)])\s+(?P<item>.+)$"
)
_INTERNAL_FENCE_RE = re.compile(
    r"^(?P<indent> {0,3})(?P<marker>`{3,}|~{3,})(?P<rest>.*)$"
)

# 이 토큰 tuple이 must-fix 0건 항목의 단일 진실이다. 파서 정규식과 reviewer preamble이
# 함께 읽으므로 허용 표기를 바꿀 때 안내만 뒤처질 수 없다. 첫 항목은 reviewer가 써야 할
# canonical 표준형이다.
_INTERNAL_NONE_ITEM_TOKENS: tuple[str, ...] = (
    "없음", "해당 없음", "n/a", "na", "none",
)


def _internal_none_item_pattern(token: str) -> str:
    """사람 표기의 공백만 유연하게 받는 0건 토큰 regex 조각."""
    return re.escape(token).replace(r"\ ", r"\s*")


_INTERNAL_NONE_ITEM_RE = re.compile(
    r"^(?:"
    + "|".join(_internal_none_item_pattern(token)
               for token in _INTERNAL_NONE_ITEM_TOKENS)
    + r")\.?$",
    re.IGNORECASE,
)


def _internal_canonical_verdict_forms() -> tuple[str, str]:
    """exact 허용집합과 parser 선호 순서의 교집합에서 한국어 표준 선언을 고른다."""

    def canonical(preferred: Sequence[str], allowed: Sequence[str]) -> str:
        token = next((word for word in preferred if word in allowed), None)
        if token is None:
            # exact 집합만 바뀐 경우에도 안내가 허용 밖 토큰을 만들지는 않는다.
            token = sorted(allowed, key=lambda word: (word.isascii(), word.casefold(), word))[0]
        return f"판정: {token}"

    return (
        canonical(_PASS_TOKENS, _PASS_VERDICT_TOKENS),
        canonical(_REJECT_TOKENS, _REJECT_VERDICT_TOKENS),
    )


class InternalReplyOutcome(NamedTuple):
    """terminal reply에서만 산출한 내부 리뷰 판정."""

    verdict: int | None
    must_fix_items: list[str] | None


class InternalReplyDiagnostic(NamedTuple):
    """판정 추출 실패의 기계 코드와 사람용 원인·재리뷰 처방."""

    code: str
    reason: str
    repair: str

    @property
    def message(self) -> str:
        return f"{self.reason} — {self.repair}"

    def as_record(self) -> dict[str, str]:
        return {
            "code": self.code,
            "reason": self.reason,
            "repair": self.repair,
            "message": self.message,
        }


class InternalReplyAssessment(NamedTuple):
    """한 축(산문 또는 기계 블록)의 판정과, unknown일 때의 구조화 진단."""

    outcome: InternalReplyOutcome
    diagnostic: InternalReplyDiagnostic | None


class InternalRoundVerdict(NamedTuple):
    """두 축을 결합한 라운드 마감 판정과 그 출처."""

    outcome: InternalReplyOutcome
    diagnostic: InternalReplyDiagnostic | None
    # 판정을 세운 축(`block`/`reply`) — 미상이면 None.
    source: str | None = None
    # 두 축이 상충했을 때 보존하는 양쪽 값.
    conflict: dict[str, str] | None = None
    # 판정은 섰지만 다른 축이 못 선 사유(경고로만 낸다 · 장부 판정 무영향).
    note: str | None = None


def _extract_internal_must_fix_items(reply: str) -> list[str] | None:
    """실 reviewer의 markdown heading/목록 형상에서 must-fix를 추출한다.

    **계수 단위는 must-fix 절의 코드펜스 밖 목록 후보 중 최소 들여쓰기 깊이에 있는 목록 행
    하나**다. 그보다 깊은 probe·근거 불릿, 표 행, fenced code 안 목록은 부모 항목의 설명이고
    별도 must-fix가 아니다. 절은 markdown/굵은 제목 또는 빈 줄 뒤 최상위 평문 제목에서 닫힌다.
    표·코드펜스는 제목이 아니다. 절 부재·빈 절은 None(미상), 명시 `없음`은 빈 목록이다.
    """
    lines = reply.splitlines()
    start = None
    inline = ""
    for index, line in enumerate(lines):
        if _INTERNAL_MUST_FIX_COUNT_DECLARATION_RE.fullmatch(line):
            # 실 리뷰 회신 형상. 선언의 숫자는 신뢰하지 않고 뒤 목록을 현행 규칙으로만 센다.
            start = index + 1
            break
        match = _INTERNAL_MUST_FIX_HEADER_RE.match(line)
        # 임의 본문 문장은 절 제목이 아니다. Markdown/굵은 decorator가 없으면 정확한 bare 제목
        # 또는 colon 제목만 받는다. 위의 좁은 실측 선언형만 별도 허용한다.
        if match and (
            match.group("decorator") is not None
            or match.group("colon") is not None
            or not match.group("inline").strip()
        ):
            start = index + 1
            inline = match.group("inline").strip()
            break
    if start is None:
        return None
    body: list[tuple[str, bool]] = [(inline, False)] if inline else []
    fence_char: str | None = None
    fence_length = 0
    for line in lines[start:]:
        fence = _INTERNAL_FENCE_RE.match(line)
        if fence_char is not None:
            body.append((line, True))
            if (
                fence is not None
                and fence.group("marker")[0] == fence_char
                and len(fence.group("marker")) >= fence_length
                and not fence.group("rest").strip()
            ):
                fence_char = None
                fence_length = 0
            continue
        if fence is not None:
            marker = fence.group("marker")
            fence_char = marker[0]
            fence_length = len(marker)
            body.append((line, True))
            continue
        if (
            _INTERNAL_SECTION_HEADER_RE.match(line)
            or _INTERNAL_BOLD_HEADER_RE.match(line)
            or _INTERNAL_PLAIN_SECTION_RE.match(line)
        ):
            break
        # 실 리뷰 회신 형상: bare `must-fix` 절 뒤의 `확인 결과`가 markdown decorator 없이
        # 다음 절을 연다. 빈 줄 직후의 비-목록 평문만 경계로 삼되, legacy 명시 0건 표기와
        # 표는 본문으로 보존한다. fence는 위 분기에서 먼저 소비하므로 이 규칙에 걸리지 않는다.
        previous_blank = bool(body) and not body[-1][0].strip()
        stripped = line.strip()
        if (
            previous_blank
            and stripped
            and _INTERNAL_PLAIN_TEXT_RE.match(line)
            and _INTERNAL_LIST_ITEM_RE.match(line) is None
            and _INTERNAL_TABLE_ROW_RE.match(line) is None
            and _INTERNAL_NONE_ITEM_RE.fullmatch(stripped) is None
        ):
            break
        body.append((line, False))

    semantic_lines = [line for line, fenced in body if not fenced]
    nonempty = [line.strip() for line in semantic_lines if line.strip()]
    if nonempty and all(_INTERNAL_NONE_ITEM_RE.fullmatch(line) for line in nonempty):
        return []

    candidates: list[tuple[int, re.Match[str]]] = []
    for index, (line, fenced) in enumerate(body):
        if fenced:
            continue
        match = _INTERNAL_LIST_ITEM_RE.match(line)
        if match is not None:
            candidates.append((index, match))
    if not candidates:
        return None
    top_indent = min(len(match.group("indent")) for _, match in candidates)

    items: list[str] = []
    explicit_none = False
    for line, fenced in body:
        match = None if fenced else _INTERNAL_LIST_ITEM_RE.match(line)
        if match is not None and len(match.group("indent")) == top_indent:
            item = match.group("item").strip()
            if _INTERNAL_NONE_ITEM_RE.fullmatch(item):
                explicit_none = True
                continue
            items.append(item)
        elif items and line.strip():
            items[-1] = f"{items[-1]} {line.strip()}"
    # `- 없음`/`- 해당 없음`도 명시 0건이다. 다만 실제 항목과 부재 표기가 섞인 모순은
    # unknown으로 남겨 통과 false-green을 만들지 않는다.
    if explicit_none:
        return [] if not items else None
    return items or None


# 재리뷰는 유료 호출이라 판정 불능의 기본 처방이 아니다. 두 축이 모두 판정을 못 세운
# 실행에서만 선택지로 제시하고, 그때도 비용과 대안(회수된 산출 직접 판정)을 함께 적는다.
_INTERNAL_REREVIEW_LAST_RESORT = (
    "재리뷰는 유료 호출이라 기본 행동이 아닙니다 — 먼저 회수된 라운드 파일을 읽고 PM이 "
    "직접 판정하세요(`rounds resolve --pm-verified`). 그래도 재리뷰가 필요하면:"
)

# 통과는 두 축 합의로만 인정한다 — 한 축만 통과인 실행의 처방은 "못 선 축을 채워라"다. 채울
# 대상은 축마다 다르므로 규칙 문장과 재리뷰 불요 문장만 공유하고 가운데만 갈린다.
_INTERNAL_PASS_BOTH_AXES_RULE = "통과는 기계 블록과 산문이 모두 통과일 때만 기록합니다 —"
_INTERNAL_PASS_NO_REREVIEW = (
    "이미 회수된 라운드 파일을 PM이 직접 판정해도 되며 재리뷰(유료 호출)는 필요 없습니다."
)

# 기계 블록만 통과인 실행 — 채울 축은 산문이다.
_INTERNAL_PASS_NEEDS_BOTH_AXES_REPAIR = (
    f"{_INTERNAL_PASS_BOTH_AXES_RULE} 라운드 파일 판정 절에 허용 선언형 한 줄과 "
    f"must-fix 0건 항목을 남기세요. {_INTERNAL_PASS_NO_REREVIEW}"
)

# 두 축이 서로 다른 값을 세운 실행 — 처방은 산출 일치이지 재리뷰가 아니다.
_INTERNAL_VERDICT_CONFLICT_REPAIR = (
    "회수된 라운드 파일에서 두 출처 중 무엇이 이 라운드의 실제 판정인지 확인해 산출을 "
    "일치시키세요. 어느 쪽도 장부에 기록하지 않았습니다."
)

# 기계 블록 축이 못 선 실행의 처방 — 허용 severity 값은 파서 enum에서만 파생한다.
_INTERNAL_BLOCK_UNUSABLE_REPAIR = (
    f"라운드 파일에 `{review_form.PM_REVIEW_BLOCK}` 블록을 정확히 하나 남기고 finding마다 severity"
    f"(<{'|'.join(review_form.PM_REVIEW_SEVERITIES)}>)를 채우세요."
)

# 산문만 통과인 실행 — 채울 축은 기계 블록이다. 반려 축은 이 처방을 타지 않는다.
_INTERNAL_PASS_NEEDS_BLOCK_AXIS_REPAIR = (
    f"{_INTERNAL_PASS_BOTH_AXES_RULE} {_INTERNAL_BLOCK_UNUSABLE_REPAIR} "
    f"{_INTERNAL_PASS_NO_REREVIEW}"
)


def _internal_reply_diagnostic(
    code: str,
    *,
    words: Sequence[str] = (),
) -> InternalReplyDiagnostic:
    """4종 unknown 사유와 수정 처방 — preamble과 같은 parser 원천을 소비한다."""
    canonical_pass, canonical_reject = _internal_canonical_verdict_forms()
    verdict_forms = f"`{canonical_pass}` 또는 `{canonical_reject}`"
    canonical_none = f"- {_INTERNAL_NONE_ITEM_TOKENS[0]}"
    if code == INTERNAL_DIAGNOSTIC_MISSING_VERDICT:
        return InternalReplyDiagnostic(
            code,
            "판정 낱말 없음",
            f"재리뷰 시 행 선두에 허용 선언형 하나({verdict_forms})를 쓰세요.",
        )
    if code == INTERNAL_DIAGNOSTIC_CONFLICTING_VERDICT:
        found = ", ".join(repr(word) for word in words) or "(빈 값)"
        return InternalReplyDiagnostic(
            code,
            f"판정 낱말 상충(통과·반려 혼재 또는 unknown 혼재: {found})",
            f"재리뷰 시 행 선두 선언을 허용형 하나({verdict_forms})만 남기세요.",
        )
    if code == INTERNAL_DIAGNOSTIC_PASS_WITHOUT_ZERO:
        return InternalReplyDiagnostic(
            code,
            "통과 선언인데 must-fix 0건 절 부재",
            "재리뷰 시 must-fix 절을 목록으로 쓰고 0건이면 "
            f"`{canonical_none}` 한 항목을 남기세요. 산문 `없습니다`는 0건으로 읽히지 않습니다.",
        )
    if code == INTERNAL_DIAGNOSTIC_REJECT_WITHOUT_ITEMS:
        return InternalReplyDiagnostic(
            code,
            "반려 선언인데 must-fix 항목 없음",
            "재리뷰 시 must-fix 절에 실제 수정 항목을 `- ...` 또는 `1. ...` 목록으로 남기세요.",
        )
    raise ValueError(f"unknown internal reply diagnostic code: {code}")


def _internal_reply_assessment(reply: str | None) -> InternalReplyAssessment:
    """terminal reply의 명시 판정/must-fix를 교차 검증하고 실패 원인을 보존한다."""
    unknown = InternalReplyOutcome(None, None)
    if not reply or not reply.strip():
        return InternalReplyAssessment(
            unknown,
            _internal_reply_diagnostic(INTERNAL_DIAGNOSTIC_MISSING_VERDICT),
        )
    words = verdict_words(reply)
    if not words:
        return InternalReplyAssessment(
            unknown,
            _internal_reply_diagnostic(
                INTERNAL_DIAGNOSTIC_MISSING_VERDICT,
            ),
        )
    kinds = [verdict_kind(word) for word in words]
    if (
        VERDICT_UNKNOWN in kinds
        or len(set(kinds)) != 1
    ):
        return InternalReplyAssessment(
            unknown,
            _internal_reply_diagnostic(
                INTERNAL_DIAGNOSTIC_CONFLICTING_VERDICT,
                words=words,
            ),
        )
    items = _extract_internal_must_fix_items(reply)
    kind = kinds[-1]
    if kind == VERDICT_PASS:
        # reply_extracted나 must_fix_items 필드 부재는 통과 증거가 아니다. 명시 "없음" 절까지
        # 있어야 0건 통과로 기록한다. 통과+실항목 모순도 무효다.
        if items == []:
            return InternalReplyAssessment(InternalReplyOutcome(0, []), None)
        return InternalReplyAssessment(
            unknown,
            _internal_reply_diagnostic(
                INTERNAL_DIAGNOSTIC_PASS_WITHOUT_ZERO,
            ),
        )
    if kind == VERDICT_REJECT:
        if items:
            return InternalReplyAssessment(InternalReplyOutcome(1, items), None)
        # 반려+"없음" 또는 절/목록 부재는 처방할 must-fix 근거가 없으므로 판정도 unknown이다.
        return InternalReplyAssessment(
            unknown,
            _internal_reply_diagnostic(
                INTERNAL_DIAGNOSTIC_REJECT_WITHOUT_ITEMS,
            ),
        )
    # verdict_kind의 선언 집합과 위 분기가 어긋나면 false-green 대신 상충으로 닫는다.
    return InternalReplyAssessment(
        unknown,
        _internal_reply_diagnostic(
            INTERNAL_DIAGNOSTIC_CONFLICTING_VERDICT,
            words=words,
        ),
    )


def _internal_reply_outcome(reply: str | None) -> InternalReplyOutcome:
    """기존 소비 API: 구조화 진단 중 판정값만 반환한다."""
    return _internal_reply_assessment(reply).outcome


def _internal_block_assessment(text: str | None) -> InternalReplyAssessment:
    """산출 bytes의 `pm-review-v1` 블록만으로 판정한다(산문 추론 0).

    `must_fix`는 **must-fix severity finding 수 + 해소되지 않은 confirmation 수**다.
    confirmation 축을 빼면 확인 전용 라운드(`findings: []` + 퇴행 확인)가 통과로 뒤집힌다.
    severity가 하나라도 비어 있으면(severity 이전 세대 산출) "반드시 고쳐야 하는가"를 블록만으로
    알 수 없으므로 **판정 불능**이다 — 0건 통과로 접지 않는다.

    스키마 판정은 전부 기존 strict 파서를 그대로 소비한다(값·key 집합 재기재 0).
    """
    reason = "산출 본문 없음"
    if text and text.strip():
        try:
            blocks = [
                block for block in review_form._pm_review_json_blocks(text)
                if block.kind == review_form.PM_REVIEW_BLOCK
            ]
            if len(blocks) != 1:
                raise review_form.PMReviewError(
                    "malformed",
                    f"{review_form.PM_REVIEW_BLOCK} block이 정확히 하나여야 합니다"
                    f"(발견 {len(blocks)}건)",
                )
            value = blocks[0].value
            review_form._pm_review_exact_keys(value, review_form.PM_REVIEW_PAYLOAD_KEYS, review_form.PM_REVIEW_BLOCK)
            version = review_form._pm_review_version(value, review_form.PM_REVIEW_BLOCK)
            if not isinstance(value["findings"], list) or not isinstance(
                value["confirmations"], list
            ):
                raise review_form.PMReviewError(
                    "malformed", "findings/confirmations는 JSON array여야 합니다",
                )
            findings = [
                review_form._pm_review_parse_finding(item, 0, version=version)
                for item in value["findings"]
            ]
            confirmations = [
                review_form._pm_review_parse_confirmation(item, 0)
                for item in value["confirmations"]
            ]
            unspecified = [item.id for item in findings if not item.severity]
            if unspecified:
                raise review_form.PMReviewError(
                    "malformed",
                    f"severity {review_form.PM_REVIEW_SEVERITY_UNSPECIFIED_LABEL} finding: "
                    + ", ".join(unspecified),
                )
            items = [
                f"{item.id} — {item.recommendation}" for item in findings
                if item.severity == review_form.PM_REVIEW_SEVERITY_MUST_FIX
            ] + [
                f"{item.id}({item.status}) — {item.evidence}"
                for item in confirmations
                if item.status != review_form.PM_REVIEW_CONFIRMATION_RESOLVED
            ]
            return InternalReplyAssessment(
                InternalReplyOutcome(1 if items else 0, items), None,
            )
        except review_form.PMReviewError as exc:
            reason = str(exc)
    return InternalReplyAssessment(
        InternalReplyOutcome(None, None),
        InternalReplyDiagnostic(
            INTERNAL_DIAGNOSTIC_BLOCK_UNUSABLE,
            f"기계 블록 축 판정 불능({reason})",
            _INTERNAL_BLOCK_UNUSABLE_REPAIR,
        ),
    )


def _internal_round_verdict(text: str | None) -> InternalRoundVerdict:
    """같은 산출 bytes의 기계 블록 축과 산문 축을 비대칭 규칙으로 결합한다.

    **반려는 한 축 단독으로 인정하고, 통과는 두 축이 모두 통과일 때만 인정한다.** 한 축만 통과인
    형상을 통과로 접으면 다른 축이 불능인 라운드가 처분 선언 없이 완료 게이트를 연다 — 실 corpus
    에서 한 축 단독으로만 판정이 서는 라운드는 전부 통과 방향이었다. 이 비대칭은 두 방향 모두에
    적용된다(블록만 통과 · 산문만 통과). 두 축이 각각 판정을 세우고 값이 다르면 어느 쪽도
    기록하지 않는다.

    입력은 board 리뷰 라운드 파일 bytes 하나다(판정 입력 = 라운드 파일 · 다른 기록 없음).
    """
    reply_axis = _internal_reply_assessment(text)
    block_axis = _internal_block_assessment(text)
    block, reply = block_axis.outcome, reply_axis.outcome
    unknown = InternalReplyOutcome(None, None)
    canonical_pass, canonical_reject = _internal_canonical_verdict_forms()

    if block.verdict is not None and reply.verdict is not None:
        if block.verdict != reply.verdict:
            conflict = {
                INTERNAL_VERDICT_SOURCE_BLOCK: (
                    f"{canonical_reject if block.verdict else canonical_pass}"
                    f"(must-fix {len(block.must_fix_items)}건)"
                ),
                INTERNAL_VERDICT_SOURCE_REPLY: (
                    f"{canonical_reject if reply.verdict else canonical_pass}"
                    f"(must-fix {len(reply.must_fix_items)}건)"
                ),
            }
            return InternalRoundVerdict(
                unknown,
                InternalReplyDiagnostic(
                    INTERNAL_DIAGNOSTIC_VERDICT_CONFLICT,
                    "두 판정 축 상충 — 기계 블록 "
                    f"{conflict[INTERNAL_VERDICT_SOURCE_BLOCK]} · 산문 "
                    f"{conflict[INTERNAL_VERDICT_SOURCE_REPLY]}",
                    _INTERNAL_VERDICT_CONFLICT_REPAIR,
                ),
                conflict=conflict,
            )
        # 두 축 합의 — 수와 항목 텍스트는 권위 블록 한 축에서 함께 만든다(수 == 항목 수).
        return InternalRoundVerdict(
            block, None, source=INTERNAL_VERDICT_SOURCE_BLOCK,
        )
    if block.verdict:
        return InternalRoundVerdict(
            block, None, source=INTERNAL_VERDICT_SOURCE_BLOCK,
        )
    reply_diagnostic = reply_axis.diagnostic or _internal_reply_diagnostic(
        INTERNAL_DIAGNOSTIC_MISSING_VERDICT,
    )
    if block.verdict is not None:
        # 블록만 통과 — 통과는 양축 합의가 필요하므로 미상으로 남긴다.
        return InternalRoundVerdict(
            unknown,
            reply_diagnostic._replace(
                reason=(
                    f"{reply_diagnostic.reason} · 기계 블록은 "
                    f"{canonical_pass}(must-fix {len(block.must_fix_items)}건)"
                ),
                repair=_INTERNAL_PASS_NEEDS_BOTH_AXES_REPAIR,
            ),
        )
    if reply.verdict is not None:
        if not reply.verdict:
            # **통과**는 두 축 합의로만 인정한다. 블록 축이 못 서고
            # 산문만 통과인 값을 기록하면 처분 선언 없이 완료 게이트가 열린다(false-green).
            # 반려 축은 이 규칙을 타지 않는다 — 반려는 한 축 단독으로도 인정한다.
            # 이 분기의 블록 축은 반드시 불능이라 진단이 실재한다(`_internal_block_assessment`는
            # 판정이 선 경우에만 진단을 비운다). 그 사유가 처방이 지목할 유일한 결측 증거다.
            return InternalRoundVerdict(
                unknown,
                InternalReplyDiagnostic(
                    INTERNAL_DIAGNOSTIC_BLOCK_UNUSABLE,
                    f"{block_axis.diagnostic.reason} · 산문은 "
                    f"{canonical_pass}(must-fix {len(reply.must_fix_items)}건)",
                    _INTERNAL_PASS_NEEDS_BLOCK_AXIS_REPAIR,
                ),
            )
        # 산문 축만 판정이 선다 — 값을 기록하고 블록 축이 왜 못 섰는지는 경고로만 남긴다.
        return InternalRoundVerdict(
            reply, None, source=INTERNAL_VERDICT_SOURCE_REPLY,
            note=(
                None if block_axis.diagnostic is None
                else block_axis.diagnostic.message
            ),
        )
    reason = reply_diagnostic.reason
    if block_axis.diagnostic is not None:
        reason = f"{reason} · {block_axis.diagnostic.reason}"
    return InternalRoundVerdict(
        unknown,
        reply_diagnostic._replace(
            reason=reason,
            repair=f"{_INTERNAL_REREVIEW_LAST_RESORT} {reply_diagnostic.repair}",
        ),
    )


# must-fix / 반려 판정 토큰 (보수적: 하나라도 감지 → 비-통과)
_REJECT_TOKENS: tuple[str, ...] = (
    "must-fix", "must fix", "MUST-FIX", "MUST FIX", "반려", "REJECT", "reject",
)
_PASS_TOKENS: tuple[str, ...] = (
    "통과", "PASS", "pass", "승인", "APPROVE", "approve", "lgtm", "LGTM",
)

# 판정 라인 자체의 인식 규칙 — 판정 파서와 echo 오염 검출이 **같은 함수**를 봐야 "검출기 ⊇ 파서"가
# 성립한다(파서가 집어 든 라인을 검출기가 못 보면 오염된 판정이 조용히 통과한다).
_VERDICT_LINE_RE = re.compile(r"^[ \t]*[*_#\s-]*판정[ \t]*:[ \t]*(\S+)")
_CODE_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")
_QUOTE_LINE_RE = re.compile(r"^\s*>")

# 판정값 허용 토큰 — **정확일치**다(부분 문자열 금지). 부분 문자열이면 프롬프트 템플릿 echo
# `판정: [통과 | 반려]`·부정형 `판정: 비통과`·선택지 나열 `판정: PASS/REJECT` 가 전부 '통과'로 읽혀
# false-green 이 난다(실측). 허용 목록에 없는 판정값은 통과도 반려도 아닌 **불명확**이다 —
# 리뷰어가 무엇을 말했는지 기계가 모르면 통과로 접지 않는다.
_PASS_VERDICT_TOKENS = frozenset({
    "통과", "PASS", "pass", "승인", "APPROVE", "approve", "LGTM", "lgtm",
})
_REJECT_VERDICT_TOKENS = frozenset({"반려", "REJECT", "reject"})

# 정확일치 전에 벗겨내는 것은 **강조/문장부호뿐**이다(`**통과**`·`통과.`). 괄호·슬래시는 남긴다 —
# 그게 템플릿 echo(`[통과`)와 선택지 나열(`PASS/REJECT`)을 불명확으로 세우는 신호다.
_VERDICT_WORD_TRIM = "*_`.,!;:"
VERDICT_PASS = "pass"
VERDICT_REJECT = "reject"
VERDICT_UNKNOWN = "unknown"


def verdict_kind(word: str) -> str:
    """판정값 낱말 → `pass`/`reject`/`unknown` (정확일치·파서와 검출기 공용)."""
    normalized = word.strip().strip(_VERDICT_WORD_TRIM)
    if normalized in _PASS_VERDICT_TOKENS:
        return VERDICT_PASS
    if normalized in _REJECT_VERDICT_TOKENS:
        return VERDICT_REJECT
    return VERDICT_UNKNOWN


def verdict_words(text: str) -> tuple[str, ...]:
    """판정 라인이 선언한 낱말들 — **행 선두 선언만**, 코드펜스 안·인용행은 제외한다.

    좁히는 규칙을 검출기에만 걸면 파서가 보는 라인을 검출기가 못 보게 되어 오염 판정이 새므로,
    파서와 검출기가 이 함수 하나를 공유한다. 인용/코드펜스 안의 판정 문구는 리뷰어 자신의 선언이
    아니라 인용물이라 어느 쪽에서도 판정으로 세지 않는다(리뷰 대상 diff에 든 판정 문안이 이 축의
    상시 오탐 원천이다).
    """
    words: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if _CODE_FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence or _QUOTE_LINE_RE.match(line):
            continue
        match = _VERDICT_LINE_RE.match(line)
        if match:
            words.append(match.group(1).strip())
    return tuple(words)
