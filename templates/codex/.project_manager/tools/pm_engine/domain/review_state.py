"""티켓 리뷰 상태 판정 (층: domain).

명세와 라운드에 쌓인 finding 선언·PM 처분·확인을 모아 accepted delta, 확인 대상 분류, 리뷰 잔여와 완료·종결
입력 판정, finding ID 채번, 리뷰 요약 렌더를 낸다.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from typing import NamedTuple

from pm_engine.domain import delegate_errors, markdown_text, review_form, review_verdict, ticket_rules


# 위임 라운드 없이 PM 이 직접 편집하는 티켓 등급 — 리뷰 라운드가 없으면 종결이 리뷰를 요구하지 않는다.
PM_DIRECT_TIER = "pm-direct"

# 이어 시키는 단계를 **같은 순번**에 다시 여는 유일한 통로 — 값은 그 라운드의 ordinal 이다.
# 요청이 없으면 준비는 종전대로 새 순번을 채번한다(암묵 재개방 없음 · 같은 문구/CLI 단일 출처).
PREPARE_REOPEN_ORDINAL_FLAG = "--reopen-ordinal"


def collect_review_finding_ids(ticket_text: str, reviewer_role: str) -> set[str]:
    """티켓 본문에 이미 등장한 채널 finding ID 전부(관용 스캔).

    엄격 파서를 쓰지 않는다 — 구 스키마·손상 블록이 섞여 있어도 ID 재사용은 막아야 하고, 산문
    인용까지 포함해 **보수적으로 넓게** 잡는 편이 충돌보다 안전하다(다음 번호가 커질 뿐이다).
    """
    prefix = review_form._pm_review_finding_id_prefix(reviewer_role)
    pattern = re.compile(
        rf"(?<![A-Za-z0-9_-]){re.escape(prefix)}-(\d{{1,6}})(?![A-Za-z0-9_-])"
    )
    return {f"{prefix}-{match.group(1)}" for match in pattern.finditer(ticket_text)}


def next_review_finding_id(
    ticket_text: str, reviewer_role: str, rounds: Sequence = (),
) -> str:
    """이 채널이 다음 라운드에 쓸 첫 finding ID — 명세+라운드의 기존 최대 번호 + 1.

    추가 리뷰어 세션은 라운드마다 fresh 라 스스로 이전 ID 를 모른다. 프롬프트에 실값을 실어야
    2라운드가 같은 ID 를 재선언해 delta 를 영구 malformed 로 만들지 않는다.

    `rounds` 는 이 티켓의 라운드 목록이다 — 라운드가 파일로 갈렸으므로 명세만 훑으면 이미 쓴
    번호를 못 본다. 단일 텍스트 조각을 재느라 부르는 자리(회신 1건 검사)는 기본값으로 둔다.
    """
    prefix = review_form._pm_review_finding_id_prefix(reviewer_role)
    seen = collect_review_finding_ids(ticket_text, reviewer_role)
    for item in rounds:
        if review_form.pm_review_refused_marker_present(item.text):
            # 표식이 붙은 라운드는 종결된 예약이라 그 골격의 ID 를 아무도 선언하지 않았다.
            # 회수 corpus 가 같은 술어로 그 라운드를 빼므로 여기서도 세지 않아야, 시드가 싣는
            # 번호와 회수가 허용하는 번호가 갈리지 않는다(표식은 `pending` 을 무너뜨린다).
            continue
        seen |= collect_review_finding_ids(item.text, reviewer_role)
    numbers = [int(finding_id.split("-", 1)[1]) for finding_id in seen]
    return f"{prefix}-{(max(numbers) + 1 if numbers else 1):03d}"


def _pm_review_refused_rounds(rounds: Sequence) -> set[tuple[str, int]]:
    """엔진이 **표식**을 남긴 라운드의 키 집합(판정 표면 제외 대상).

    스키마 위반·ID 재선언처럼 판정 표면에 올릴 수 없는 산출은 경고와 함께 평문으로만 남는다.
    그 라운드를 판정 대상으로 세면 "최신 리뷰 라운드에 블록이 없다"로 티켓 전체 delta 가 막혀,
    거부한 라운드가 오히려 회수된 자산을 잠근다. 판정 표면에서만 제외하고 산출은 보존한다.

    판정 기준은 산문 문자열이 아니라 **엔진이 발행한 표식 줄**이고 역할을 가리지 않는다. 발행
    자리는 둘이다 — 단일 파일 시절의 거부 산출을 옮겨 온 라운드와, 중간 순번이라 board 예약을
    보존한 채 종결한 포기(`abandon_ticket_copy`)다. 역할로 좁히면 뒤의 것이 판독되지 않아 종결된
    라운드가 자리표시 골격째로 표면에 선다. 표식을 실은 회신은 회수가 거부한다
    (`review_harvest_problem`).
    """
    return {
        (item.role, item.ordinal)
        for item in rounds
        if review_form.pm_review_refused_marker_present(item.text)
    }


def _pm_review_surface_rounds(rounds: Sequence) -> list:
    """판정 표면에 오르는 리뷰 라운드 — 순번 순 · 회수 거부 라운드·시드 그대로인 라운드 제외.

    `item.pending`(kill·미회수로 산출이 비어 있는 예약)은 실을 내용이 없다 — 그 골격의
    자리표시 블록(finding.class 등)을 판정 대상으로 세면 malformed 로 읽힌다. 제외이지
    거부가 아니다: rc 를 바꾸지 않고 조용히 표면 밖으로 뺀다.
    """
    refused = _pm_review_refused_rounds(rounds)
    return [
        item for item in sorted(rounds, key=lambda entry: entry.ordinal)
        if item.role in review_form.REVIEW_ROLES
        and (item.role, item.ordinal) not in refused
        and not item.pending
    ]


def collect_review_finding_declarations(
    ticket_text: str, reviewer_role: str, rounds: Sequence,
    *, before_ordinal: int | None = None,
) -> set[str]:
    """판정 표면에 **실재하는** 그 채널 finding ID — 회수 거부되지 않은 라운드의 블록 선언만.

    `collect_review_finding_ids` 와 시야가 다르다. 저쪽은 ID 재사용을 막으려고 산문 인용까지
    넓게 잡지만(다음 번호가 커질 뿐이다), 이쪽은 confirmation 이 참조할 수 있는 finding 만
    센다 — 판정 표면 규칙(`parse_pm_review_delta` 의 "confirmation이 선행 finding ID를
    참조")과 **같은 시야**여야 회수 게이트가 통과시킨 블록이 delta 에서 malformed 가 되지 않는다.

    블록 스캔은 라운드 단위 관용 판정이다 — 다른 라운드의 손상이 이 대조를 눈멀게 하면 안 되고,
    읽지 못한 라운드는 선언으로 세지 않는다(대조는 fail-closed 쪽으로 기운다).

    `before_ordinal` 을 주면 그 순번 **앞** 라운드의 선언만 센다. 판정 표면은 ID 를 먼저 선언한
    라운드에 귀속하므로, 한 라운드가 실은 ID 가 그 라운드의 신규 선언인지 재선언인지는 이 시야
    로만 갈린다.
    """
    prefix = review_form._pm_review_finding_id_prefix(reviewer_role)
    declared: set[str] = set()
    for item in _pm_review_surface_rounds(rounds):
        if item.role != reviewer_role:
            continue
        if before_ordinal is not None and item.ordinal >= before_ordinal:
            continue
        try:
            blocks = _pm_review_section_review_blocks(item)
        except review_form.PMReviewError:
            continue
        for block in blocks:
            findings = block.value.get("findings")
            if not isinstance(findings, list):
                continue
            for entry in findings:
                if not isinstance(entry, dict):
                    continue
                finding_id = entry.get("id")
                if not isinstance(finding_id, str):
                    continue
                if (finding_id.startswith(f"{prefix}-")
                        and review_form._PM_REVIEW_ID_RE.fullmatch(finding_id) is not None):
                    declared.add(finding_id)
    return declared


def _pm_review_verify_round_declarations(
    round,
) -> tuple[dict[str, review_form.PMReviewVerifyRow], tuple[str, ...]]:
    """그 developer 라운드가 남긴 것 — (채워진 verify 행, 자리표시자 그대로인 ID).

    블록이 없으면 둘 다 비어 있다(accepted 0건이던 최초 라운드). 자리표시자 행은 **선언이
    아니지만 관측이기는 하다** — 시드가 그 ID 를 요구했는데 이번 라운드가 아무 선언도 남기지
    않았다는 관측이라, 누적 장부가 앞 라운드의 선언을 최신값으로 계속 쓰지 않게 하는 입력이다
    (`_pm_review_latest_verify_rows` 의 tombstone).

    같은 라운드에서 한 ID 가 자리표시자와 채워진 행으로 둘 다 나오면 채워진 행이 이긴다 —
    선언이 실제로 있는 쪽이 그 라운드의 관측이다.
    """
    # 손대지 않은 골격은 boolean 자리가 raw 자리표시자라 그대로는 유효 JSON 이 아니다. 여기서
    # 그 한 자리만 재-인용해 블록을 열어야 "자리표시자 그대로 = 선언 없음(태만)" 관측이 성립한다
    # — 재-인용 없이 malformed 로 올리면 같은 라운드의 채워진 행까지 통째로 막힌다. 재-인용은
    # `machine_verifiable` 값 자리 하나로 한정되고 실제 boolean 값은 건드리지 않으므로, 문자열
    # `"true"` 를 쓴 행은 여전히 boolean 요구에 걸려 거부된다(관용 추가 0).
    blocks = [
        block for block in review_form._pm_review_json_blocks(
            review_form._pm_review_requote_verify_placeholder(round.text))
        if block.kind == review_form.PM_REVIEW_VERIFY_BLOCK
    ]
    if not blocks:
        return {}, ()
    if len(blocks) != 1:
        raise review_form.PMReviewError(
            "malformed",
            f"developer ordinal={round.ordinal}에는 {review_form.PM_REVIEW_VERIFY_BLOCK} block이 "
            "최대 하나여야 합니다",
        )
    value = blocks[0].value
    review_form._pm_review_exact_keys(value, review_form.PM_REVIEW_VERIFY_PAYLOAD_KEYS, review_form.PM_REVIEW_VERIFY_BLOCK)
    review_form._pm_review_version(value, review_form.PM_REVIEW_VERIFY_BLOCK, allowed=(review_form.PM_REVIEW_VERIFY_VERSION,))
    if not isinstance(value["verifications"], list):
        raise review_form.PMReviewError("malformed", "verifications는 JSON array여야 합니다")
    rows: dict[str, review_form.PMReviewVerifyRow] = {}
    unfilled: list[str] = []
    for item in value["verifications"]:
        if review_form._pm_review_verify_row_is_unfilled(item):
            finding_id = item.get("id")
            if isinstance(finding_id, str) and finding_id.strip():
                unfilled.append(finding_id.strip())
            continue
        row = review_form._pm_review_parse_verify_row(item)
        if row.id in rows:
            raise review_form.PMReviewError(
                "malformed", f"developer ordinal={round.ordinal} verify ID 중복: {row.id}",
            )
        rows[row.id] = row
    return rows, tuple(
        finding_id for finding_id in dict.fromkeys(unfilled) if finding_id not in rows
    )


def _pm_review_verify_rows_for_round(round) -> dict[str, review_form.PMReviewVerifyRow]:
    """그 developer 라운드의 **채워진** verify 행 — 기계 확인 결속(confirmation)의 시야.

    자리표시자 그대로인 행은 여기 없다(선언 없음). 그래서 확인 결속은 "그 ID 의 행이 없다"로
    거부되고, 부분만 채워진 라운드에서도 채워진 ID 의 기계 확인은 살아남는다.
    """
    rows, _unfilled = _pm_review_verify_round_declarations(round)
    return rows


def _pm_review_confirmation_floor(
    finding: review_form.PMReviewFinding,
    reviewer_confirmations: Sequence,
    machine_confirmations: Sequence,
) -> int:
    """그 finding 의 다음 확인이 **넘어서야 하는** 순번 — 선언 라운드와 기존 확인 중 가장 뒤.

    파서의 결속 검사(늦게 적은 과거 round 가 최신 관측을 덮어쓰지 못하게)와 `verify-template` 의
    stale 판정이 이 한 함수를 본다. 두 곳이 각자 계산하면 한쪽이 렌더한 골격을 다른 쪽이
    거부한다(왕복 정합 위반).
    """
    floor = finding.reviewer_ordinal
    for item in reviewer_confirmations:
        floor = max(floor, item.reviewer_ordinal)
    for item in machine_confirmations:
        floor = max(floor, item.round)
    return floor


class _RoundView(NamedTuple):
    """판정에 필요한 라운드 3필드 — `round_rules.Round` 와 attribute 호환 부분집합.

    라운드 파일을 아직 적재하지 않은 자리(직전 라운드 본문만 손에 든 시드 렌더)가 같은 판정
    함수를 그대로 쓰게 하는 얇은 어댑터다.
    """

    role: str
    ordinal: int
    text: str


def _pm_review_block_for_round(round) -> review_form._PMReviewBlock:
    """이 라운드 파일의 유일한 reviewer block — 없거나 둘 이상이면 malformed."""
    matches = [
        block for block in review_form._pm_review_json_blocks(round.text)
        if block.kind == review_form.PM_REVIEW_BLOCK
    ]
    if len(matches) != 1:
        raise review_form.PMReviewError(
            "malformed",
            f"reviewer ordinal={round.ordinal}에는 {review_form.PM_REVIEW_BLOCK} block이 정확히 하나여야 합니다",
        )
    return matches[0]


def _pm_review_round_ids(round) -> list[str]:
    """한 reviewer block에 등장한 finding/confirmation ID를 순서 보존해 반환한다."""
    value = _pm_review_block_for_round(round).value
    review_form._pm_review_exact_keys(value, review_form.PM_REVIEW_PAYLOAD_KEYS, review_form.PM_REVIEW_BLOCK)
    version = review_form._pm_review_version(value, review_form.PM_REVIEW_BLOCK)
    if not isinstance(value["findings"], list) or not isinstance(value["confirmations"], list):
        raise review_form.PMReviewError("malformed", "findings/confirmations는 JSON array여야 합니다")
    findings = [
        review_form._pm_review_parse_finding(
            item, round.ordinal, reviewer_role=round.role, version=version,
        )
        for item in value["findings"]
    ]
    confirmations = [
        review_form._pm_review_parse_confirmation(
            item, round.ordinal, reviewer_role=round.role,
        )
        for item in value["confirmations"]
    ]
    ids = [item.id for item in findings] + [item.id for item in confirmations]
    if len(ids) != len(set(ids)):
        raise review_form.PMReviewError(
            "malformed", f"reviewer ordinal={round.ordinal} finding/confirmation ID 중복",
        )
    return ids


def _pm_review_rejected_finding_ids(
    ticket_text: str, *, reviewer_role: str, reviewer_ordinal: int,
) -> set[str]:
    """그 채널·ordinal 에서 PM 이 `rejected` 로 판정한 finding ID.

    확인 라운드가 참조하면 안 되는 ID 다 — 판정 표면이 재등장을 malformed 로 막는다. 시드 프리필과
    추가 리뷰어 골격·확인 근거 필터가 이 한 함수를 봐야 배제 규칙이 채널마다 갈리지 않는다.
    """
    _block, rows = _pm_review_disposition_rows_for_ordinal(
        ticket_text, reviewer_ordinal, reviewer_role=reviewer_role,
    )
    return {parsed.id for parsed, _raw in rows if parsed.decision == "rejected"}


def _pm_review_disposition_rows_for_ordinal(
    ticket_text: str, reviewer_ordinal: int,
    *, reviewer_role: str = review_form.INTERNAL_REVIEW_ROLE,
) -> tuple[dict | None, list[tuple[review_form.PMReviewDisposition, dict]]]:
    """한 reviewer 채널·ordinal의 유일한 PM block과 검증된 raw disposition 행을 반환한다."""
    label = f"reviewer {reviewer_role} ordinal={reviewer_ordinal}"
    matches: list[review_form._PMReviewBlock] = []
    for block in review_form._pm_review_json_blocks(ticket_text):
        if block.kind != review_form.PM_REVIEW_DISPOSITION_BLOCK:
            continue
        ordinal = block.value.get("reviewer_ordinal")
        if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 0:
            raise review_form.PMReviewError(
                "malformed", "disposition reviewer_ordinal은 0 이상 정수여야 합니다",
            )
        if (ordinal, review_form._pm_review_disposition_role(block.value)) == (
            reviewer_ordinal, reviewer_role,
        ):
            matches.append(block)
    if len(matches) > 1:
        raise review_form.PMReviewError("malformed", f"{label} disposition block 중복")
    if not matches:
        return None, []

    value = matches[0].value
    review_form._pm_review_version(
        value, review_form.PM_REVIEW_DISPOSITION_BLOCK,
        allowed=(review_form.PM_REVIEW_DISPOSITION_VERSION,),
    )
    if "finding_zero" in value:
        review_form._pm_review_exact_keys(
            value,
            review_form._pm_review_disposition_payload_keys(
                value, review_form.PM_REVIEW_FINDING_ZERO_PAYLOAD_KEYS,
            ),
            review_form.PM_REVIEW_DISPOSITION_BLOCK,
        )
        if value["finding_zero"] != "accepted":
            raise review_form.PMReviewError(
                "malformed", f"{label} finding_zero disposition 불일치",
            )
        return value, []

    review_form._pm_review_exact_keys(
        value,
        review_form._pm_review_disposition_payload_keys(
            value, review_form.PM_REVIEW_DISPOSITION_PAYLOAD_KEYS,
        ),
        review_form.PM_REVIEW_DISPOSITION_BLOCK,
    )
    if not isinstance(value["dispositions"], list):
        raise review_form.PMReviewError("malformed", "dispositions는 JSON array여야 합니다")
    rows: list[tuple[review_form.PMReviewDisposition, dict]] = []
    for raw in value["dispositions"]:
        parsed = review_form._pm_review_parse_disposition(
            raw, reviewer_ordinal, reviewer_role=reviewer_role,
        )
        assert isinstance(raw, dict)  # parser가 바로 위에서 object schema를 검증했다.
        rows.append((parsed, raw))
    parsed_ids = [parsed.id for parsed, _raw in rows]
    if len(parsed_ids) != len(set(parsed_ids)):
        raise review_form.PMReviewError("malformed", f"{label} disposition ID 중복")
    return value, rows


def parse_pm_review_delta(ticket_text: str, rounds: Sequence) -> review_form.PMReviewDelta:
    """reviewer 제안→PM disposition→확인 상태를 accepted-only delta로 축약한다.

    입력은 둘이다 — 명세(`ticket_text`)가 PM disposition 블록의 자리이고, reviewer 블록은
    라운드 파일에만 있다. 두 리뷰 채널(내부 code-reviewer·추가 additional-reviewer)을 같은
    표면에서 판정하며 순번은 티켓 전역, finding ID 는 접두로 갈려 티켓 전역 유일이다.

    라운드가 파일로 갈려 "블록이 어느 절 안에 있나" 를 좌표로 물을 일이 없다 — 리뷰 블록은
    리뷰 라운드 파일 안, PM 판정 블록은 명세 안이고, 자리를 어긴 블록은 그 자리에서 막힌다.
    """
    reviewer_sections = _pm_review_surface_rounds(rounds)
    refused = _pm_review_refused_rounds(rounds)
    review_by_channel: dict[tuple[str, int], review_form._PMReviewBlock] = {}
    disposition_blocks: dict[tuple[str, int], review_form._PMReviewBlock] = {}

    # developer 라운드의 verify 행 내용은 **여기서 파싱하지 않는다** — 아직 아무 기계 확인도
    # 참조하지 않은 라운드의 자리표시자/오류 verify 블록이 delta 전체를 막으면 표시면(harvest·
    # review delta·verify-template 의 "지금 accepted 가 뭔가" 질의)까지 판정면 엄격 규칙에
    # 전염된다. placement(developer 라운드 안에만)는 구조 검사라 여기서 그대로 하고, row 내용
    # 파싱은 아래 `_verify_rows_for_developer_round`(confirmation 결속을 실제로 요구하는
    # 순간)로 미룬다. developer 라운드는 구조 스캔 전용 재-인용을 거쳐야 손대지 않은
    # verify 골격(raw placeholder)도 fence 존재를 볼 수 있다 — row 값은 여기서 읽지 않으므로
    # (`continue`) 재-인용이 값 검증에 새지 않는다.
    dev_rounds_by_ordinal: dict[int, object] = {}
    pending_dev_ordinals: set[int] = set()
    for item in sorted(rounds, key=lambda entry: entry.ordinal):
        channel = (item.role, item.ordinal)
        if channel in refused:
            continue              # 거부 표식이 있는 라운드는 통째로 판정 표면 밖이다.
        # 이 순회는 developer verify 배치 검사·`dev_rounds_by_ordinal` 구축을 겸해 seam
        # (`_pm_review_surface_rounds`)을 거치지 않는다 — 시드 그대로인 라운드는 같은
        # 축(`item.pending`)으로 여기서도 배제해야 골격의 자리표시 블록이 실 선언으로 안 읽힌다.
        # 배제 규칙은 역할과 무관하다(pending = 회수 전 = 아직 산출이 아니다 · 누적 시야
        # `_pm_review_latest_verify_rows` 와 같은 축) — developer 라운드는 순번만 남겨,
        # 그 순번을 참조한 기계 확인이 "선언 누락(dev 태만)" 이 아니라 "아직 산출이 아닌
        # 라운드" 로 진단되게 한다.
        if item.pending:
            if item.role == "developer":
                pending_dev_ordinals.add(item.ordinal)
            continue
        scan_text = (
            review_form._pm_review_requote_verify_placeholder(item.text)
            if item.role == "developer" else item.text
        )
        for block in review_form._pm_review_json_blocks(scan_text):
            if block.kind == review_form.PM_REVIEW_VERIFY_BLOCK:
                if item.role != "developer":
                    raise review_form.PMReviewError(
                        "malformed",
                        f"{review_form.PM_REVIEW_VERIFY_BLOCK}은 developer 라운드 파일 안에만 있어야 합니다",
                    )
                continue           # verify 행 파싱은 confirmation 결속 시점으로 미룬다.
            if block.kind != review_form.PM_REVIEW_BLOCK:
                raise review_form.PMReviewError(
                    "malformed",
                    f"{block.kind}은 라운드 파일 밖 명세의 PM 영역에 있어야 합니다",
                )
            if item.role not in review_form.REVIEW_ROLES:
                raise review_form.PMReviewError(
                    "malformed",
                    f"{review_form.PM_REVIEW_BLOCK}은 리뷰 역할 라운드 안에 있어야 합니다"
                    f"(허용 채널: {', '.join(review_form.REVIEW_ROLES)})",
                )
            if channel in review_by_channel:
                raise review_form.PMReviewError(
                    "malformed",
                    f"reviewer {channel[0]} ordinal={channel[1]} review block 중복",
                )
            review_by_channel[channel] = block
        if item.role == "developer":
            dev_rounds_by_ordinal[item.ordinal] = item

    _verify_rows_cache: dict[int, dict[str, review_form.PMReviewVerifyRow]] = {}

    def _verify_rows_for_developer_round(round_ordinal: int) -> dict[str, review_form.PMReviewVerifyRow] | None:
        """그 순번이 **산출 있는** developer 라운드가 아니면 None, 맞으면 verify 행(지연 파싱)."""
        target = dev_rounds_by_ordinal.get(round_ordinal)
        if target is None:
            return None
        if round_ordinal not in _verify_rows_cache:
            _verify_rows_cache[round_ordinal] = _pm_review_verify_rows_for_round(target)
        return _verify_rows_cache[round_ordinal]

    confirmation_blocks: list[review_form._PMReviewBlock] = []
    for block in review_form._pm_review_json_blocks(ticket_text):
        if block.kind == review_form.PM_REVIEW_BLOCK:
            raise review_form.PMReviewError(
                "malformed",
                f"{review_form.PM_REVIEW_BLOCK}은 리뷰 역할 라운드 안에 있어야 합니다"
                f"(허용 채널: {', '.join(review_form.REVIEW_ROLES)})",
            )
        if block.kind == review_form.PM_REVIEW_VERIFY_BLOCK:
            raise review_form.PMReviewError(
                "malformed",
                f"{review_form.PM_REVIEW_VERIFY_BLOCK}은 developer 라운드 파일 안에만 있어야 합니다",
            )
        if block.kind == review_form.PM_REVIEW_CONFIRMATION_BLOCK:
            confirmation_blocks.append(block)
            continue
        value = block.value
        if not isinstance(value.get("reviewer_ordinal"), int) or isinstance(
            value.get("reviewer_ordinal"), bool
        ) or value["reviewer_ordinal"] < 0:
            raise review_form.PMReviewError("malformed", "disposition reviewer_ordinal은 0 이상 정수여야 합니다")
        channel = (review_form._pm_review_disposition_role(value), value["reviewer_ordinal"])
        if channel in disposition_blocks:
            raise review_form.PMReviewError(
                "malformed",
                f"reviewer {channel[0]} ordinal={channel[1]} disposition block 중복",
            )
        disposition_blocks[channel] = block

    if not reviewer_sections or not review_by_channel:
        raise review_form.PMReviewError("malformed", "versioned reviewer finding block이 없습니다")
    latest_reviewer = reviewer_sections[-1]
    if (latest_reviewer.role, latest_reviewer.ordinal) not in review_by_channel:
        raise review_form.PMReviewError(
            "malformed",
            f"최신 {latest_reviewer.role} 라운드에 versioned finding block이 없습니다",
        )

    findings: dict[str, review_form.PMReviewFinding] = {}
    confirmations: dict[str, list[review_form.PMReviewConfirmation]] = {}
    zero_channels: set[tuple[str, int]] = set()
    for channel in sorted(review_by_channel):
        role, ordinal = channel
        label = f"reviewer {role} ordinal={ordinal}"
        block = review_by_channel[channel]
        value = block.value
        review_form._pm_review_exact_keys(value, review_form.PM_REVIEW_PAYLOAD_KEYS, review_form.PM_REVIEW_BLOCK)
        version = review_form._pm_review_version(value, review_form.PM_REVIEW_BLOCK)
        if not isinstance(value["findings"], list) or not isinstance(value["confirmations"], list):
            raise review_form.PMReviewError("malformed", "findings/confirmations는 JSON array여야 합니다")
        parsed_findings = [
            review_form._pm_review_parse_finding(
                item, ordinal, reviewer_role=role, version=version,
            )
            for item in value["findings"]
        ]
        parsed_confirmations = [
            review_form._pm_review_parse_confirmation(item, ordinal, reviewer_role=role)
            for item in value["confirmations"]
        ]
        local_ids = [item.id for item in parsed_findings]
        if len(local_ids) != len(set(local_ids)):
            raise review_form.PMReviewError("malformed", f"{label} finding ID 중복")
        confirmation_ids = [item.id for item in parsed_confirmations]
        if len(confirmation_ids) != len(set(confirmation_ids)):
            raise review_form.PMReviewError("malformed", f"{label} confirmation ID 중복")
        for finding in parsed_findings:
            if finding.id in findings:
                raise review_form.PMReviewError("malformed", f"티켓 안 finding ID 재선언: {finding.id}")
            findings[finding.id] = finding
        for confirmation in parsed_confirmations:
            if confirmation.id not in findings or confirmation.id in local_ids:
                raise review_form.PMReviewError(
                    "malformed", f"confirmation이 선행 finding ID를 참조하지 않음: {confirmation.id}"
                )
            confirmations.setdefault(confirmation.id, []).append(confirmation)
        if not parsed_findings and not parsed_confirmations:
            section = next(
                item for item in reviewer_sections
                if (item.role, item.ordinal) == channel
            )
            if review_verdict._internal_reply_outcome(section.text) != review_verdict.InternalReplyOutcome(0, []):
                raise review_form.PMReviewError(
                    "malformed", f"finding 0 {label}은 통과+must-fix 0건 선언과 모순"
                )
            zero_channels.add(channel)

    # confirmation 결속은 accepted disposition scope까지 봐야 PM-owned 이중 표식을 판정할 수
    # 있다. disposition을 먼저 엄격 파싱하되, pending/decision-required 상태 판정은 아래의 기존
    # 자리에서 유지한다.
    dispositions: dict[str, review_form.PMReviewDisposition] = {}
    accepted_zero: set[tuple[str, int]] = set()
    for channel, block in disposition_blocks.items():
        role, ordinal = channel
        label = f"reviewer {role} ordinal={ordinal}"
        if channel not in review_by_channel:
            raise review_form.PMReviewError(
                "malformed", f"disposition이 없는 리뷰 라운드를 참조: {label}",
            )
        value = block.value
        if "finding_zero" in value:
            review_form._pm_review_exact_keys(
                value,
                review_form._pm_review_disposition_payload_keys(
                    value, review_form.PM_REVIEW_FINDING_ZERO_PAYLOAD_KEYS,
                ),
                review_form.PM_REVIEW_DISPOSITION_BLOCK,
            )
            review_form._pm_review_version(
                value, review_form.PM_REVIEW_DISPOSITION_BLOCK,
                allowed=(review_form.PM_REVIEW_DISPOSITION_VERSION,),
            )
            if channel not in zero_channels or value["finding_zero"] != "accepted":
                raise review_form.PMReviewError("malformed", f"{label} finding_zero disposition 불일치")
            accepted_zero.add(channel)
            continue
        review_form._pm_review_exact_keys(
            value,
            review_form._pm_review_disposition_payload_keys(
                value, review_form.PM_REVIEW_DISPOSITION_PAYLOAD_KEYS,
            ),
            review_form.PM_REVIEW_DISPOSITION_BLOCK,
        )
        review_form._pm_review_version(
            value, review_form.PM_REVIEW_DISPOSITION_BLOCK,
            allowed=(review_form.PM_REVIEW_DISPOSITION_VERSION,),
        )
        if not isinstance(value["dispositions"], list):
            raise review_form.PMReviewError("malformed", "dispositions는 JSON array여야 합니다")
        source_ids = {
            finding.id for finding in findings.values()
            if (finding.reviewer_role, finding.reviewer_ordinal) == channel
        }
        parsed = [
            review_form._pm_review_parse_disposition(item, ordinal, reviewer_role=role)
            for item in value["dispositions"]
        ]
        parsed_ids = [item.id for item in parsed]
        if len(parsed_ids) != len(set(parsed_ids)):
            raise review_form.PMReviewError("malformed", f"{label} disposition ID 중복")
        if set(parsed_ids) - source_ids:
            raise review_form.PMReviewError(
                "malformed",
                f"{label} extra disposition ID: {sorted(set(parsed_ids)-source_ids)}",
            )
        for disposition in parsed:
            dispositions[disposition.id] = disposition

    # 기계 확인 — 명세 PM 영역의 `pm-review-confirmation-v1` 블록. verify 행과의 결속
    # (같은 id·command)이 stale 방지다 — 회차가 다른 verify 행을 실수로 가리키면 malformed.
    # round 결속(F-002 fix): (a) round 는 confirmation_blocks 전역에서 단조 증가하는 고유 키다
    # — 재사용·역순은 명세 안 누적 순서(=이 블록들을 스캔한 문서 순서) 위반으로 막는다.
    # (b) 각 확인 행은 그 id 의 source finding 이 선언된 reviewer round 보다, 그리고 그 id 의
    # 기존 reviewer confirmation·기계 확인 round 보다 뒤인 developer round 만 참조할 수 있다 —
    # 늦게 적은 과거 round 참조가 최신 관측을 덮어쓰지 못하게 한다(불변식 6 시간축 보존).
    machine_confirmations: dict[str, list[review_form.PMReviewMachineConfirmation]] = {}
    last_confirmation_round: int | None = None
    for block in confirmation_blocks:
        value = block.value
        review_form._pm_review_exact_keys(
            value, review_form.PM_REVIEW_MACHINE_CONFIRMATION_PAYLOAD_KEYS, review_form.PM_REVIEW_CONFIRMATION_BLOCK,
        )
        review_form._pm_review_version(
            value, review_form.PM_REVIEW_CONFIRMATION_BLOCK,
            allowed=(review_form.PM_REVIEW_MACHINE_CONFIRMATION_VERSION,),
        )
        round_ordinal = value.get("round")
        if (
            not isinstance(round_ordinal, int) or isinstance(round_ordinal, bool)
            or round_ordinal < 1
        ):
            raise review_form.PMReviewError(
                "malformed", "confirmation.round은 1 이상 정수여야 합니다",
            )
        if last_confirmation_round is not None and round_ordinal <= last_confirmation_round:
            raise review_form.PMReviewError(
                "malformed",
                f"{review_form.PM_REVIEW_CONFIRMATION_BLOCK} round={round_ordinal}가 명세 누적 순서상 "
                f"단조 증가가 아닙니다(직전 round={last_confirmation_round}) — round 는 티켓 "
                "전역 고유 키이며 재사용·역순 참조는 malformed",
            )
        last_confirmation_round = round_ordinal
        verify_rows = _verify_rows_for_developer_round(round_ordinal)
        if verify_rows is None:
            reason = (
                "아직 회수되지 않은 developer 라운드입니다(시드 그대로 · 산출 없음)"
                if round_ordinal in pending_dev_ordinals
                else "developer 라운드가 아닙니다"
            )
            raise review_form.PMReviewError(
                "malformed",
                f"{review_form.PM_REVIEW_CONFIRMATION_BLOCK} round={round_ordinal}는 {reason}",
            )
        if not isinstance(value.get("confirmations"), list):
            raise review_form.PMReviewError("malformed", "confirmations는 JSON array여야 합니다")
        local_ids: list[str] = []
        for raw in value["confirmations"]:
            row = review_form._pm_review_parse_machine_confirmation_row(raw, round_ordinal=round_ordinal)
            local_ids.append(row.id)
            if row.id not in findings:
                raise review_form.PMReviewError(
                    "malformed",
                    f"{review_form.PM_REVIEW_CONFIRMATION_BLOCK} id={row.id}가 알려진 finding ID를 "
                    "참조하지 않습니다",
                )
            source_finding = findings[row.id]
            confirmation_floor = _pm_review_confirmation_floor(
                source_finding,
                confirmations.get(row.id, ()),
                machine_confirmations.get(row.id, ()),
            )
            if round_ordinal <= confirmation_floor:
                raise review_form.PMReviewError(
                    "malformed",
                    f"{review_form.PM_REVIEW_CONFIRMATION_BLOCK} id={row.id}의 round={round_ordinal}가 "
                    f"그 finding 의 선언/기존 확인(ordinal={confirmation_floor}) 보다 뒤가 "
                    "아닙니다 — 선-선언 round 차용이거나 과거 round 로 최신 관측을 덮어씁니다",
                )
            verify_row = verify_rows.get(row.id)
            if verify_row is None:
                raise review_form.PMReviewError(
                    "malformed",
                    f"{review_form.PM_REVIEW_CONFIRMATION_BLOCK} id={row.id}가 round={round_ordinal}의 "
                    "verify 행과 결속되지 않습니다(id 없음)",
                )
            pm_owned = review_form._pm_review_is_pm_owned_binding(
                dispositions.get(row.id), verify_row,
            )
            if not verify_row.machine_verifiable:
                if not pm_owned:
                    raise review_form.PMReviewError(
                        "malformed",
                        f"{review_form.PM_REVIEW_CONFIRMATION_BLOCK} id={row.id}는 "
                        "machine_verifiable=false 이며 strict pm-owned 이중 결속도 없어 "
                        "reviewer 확인 전용입니다",
                    )
                if (
                    row.status != "resolved"
                    or row.command != review_form.PM_REVIEW_PM_OWNED_CONFIRMATION_COMMAND
                    or row.observed != verify_row.expected
                ):
                    raise review_form.PMReviewError(
                        "malformed",
                        f"{review_form.PM_REVIEW_CONFIRMATION_BLOCK} id={row.id} PM-owned terminal 확인은 "
                        f"status=resolved · command={review_form.PM_REVIEW_PM_OWNED_CONFIRMATION_COMMAND!r} · "
                        "observed=verify.expected와 정확히 일치해야 합니다",
                    )
            else:
                if verify_row.command != row.command:
                    raise review_form.PMReviewError(
                        "malformed",
                        f"{review_form.PM_REVIEW_CONFIRMATION_BLOCK} id={row.id}가 round={round_ordinal}의 "
                        "verify 행과 결속되지 않습니다(command 불일치)",
                    )
                if row.status == "resolved" and verify_row.expected not in row.observed:
                    raise review_form.PMReviewError(
                        "malformed",
                        f"{review_form.PM_REVIEW_CONFIRMATION_BLOCK} id={row.id} resolved 인데 expected가 "
                        "observed에 없습니다",
                    )
            machine_confirmations.setdefault(row.id, []).append(row)
        if len(local_ids) != len(set(local_ids)):
            raise review_form.PMReviewError(
                "malformed", f"{review_form.PM_REVIEW_CONFIRMATION_BLOCK} round={round_ordinal} ID 중복",
            )

    pending = sorted(set(findings) - set(dispositions))
    pending_zero = sorted(zero_channels - accepted_zero)
    if pending or pending_zero:
        # 구조화 채널 좌표(리뷰 라운드 결정) — 처방이 문자열을 재파싱하지 않고 이 쌍으로
        # 실행 가능한 --ordinal/--reviewer-role 커맨드를 조립한다. 아래 사람용 문구
        # (pending_channels) 는 이 쌍을 다시 문자열로 접어 **기존과 같은 정렬 규칙**(문자열
        # 사전순)으로 낸다 — 관측 가능한 메시지는 바이트 그대로 보존한다.
        pending_channel_pairs = sorted(
            {
                (findings[finding_id].reviewer_role, findings[finding_id].reviewer_ordinal)
                for finding_id in pending
            }
            | set(pending_zero)
        )
        pending_channels = sorted(
            f"{role}[{ordinal}]" for role, ordinal in pending_channel_pairs
        )
        raise review_form.PMReviewError(
            "pending",
            f"PM 미판정 finding={pending}; finding-zero 리뷰 라운드="
            f"{[f'{role}[{ordinal}]' for role, ordinal in pending_zero]}; "
            f"대상 채널={pending_channels}",
            channels=tuple(pending_channel_pairs),
        )
    required = sorted(
        finding_id for finding_id, disposition in dispositions.items()
        if disposition.decision == "decision-required"
    )
    if required:
        raise review_form.PMReviewError("decision-required", f"선행 권위 결정이 필요한 finding: {required}")
    redisplayed_rejected = sorted(
        finding_id for finding_id in set(confirmations) | set(machine_confirmations)
        if dispositions[finding_id].decision == "rejected"
    )
    if redisplayed_rejected:
        raise review_form.PMReviewError(
            "malformed", f"rejected finding ID가 확인 라운드에 재등장: {redisplayed_rejected}"
        )

    accepted: list[tuple[review_form.PMReviewFinding, review_form.PMReviewDisposition]] = []
    repeated: list[str] = []
    for finding_id, finding in findings.items():
        disposition = dispositions[finding_id]
        if disposition.decision != "accepted":
            continue
        if finding.classification == "design-proposal" or finding.design_change:
            if review_form._PM_REVIEW_AUTHORITY_REF_RE.search(disposition.prerequisite) is None:
                raise review_form.PMReviewError(
                    "decision-required",
                    f"accepted 설계 finding {finding_id}의 선행 ticket/spec/ADR wikilink가 없습니다",
                )
        # 확인 이력은 하나의 시간축이다(불변식 6) — reviewer 확인(라운드 순번)과 기계 확인
        # (참조한 developer 라운드 순번)을 같은 정렬 키로 섞는다. 두 축이 같은 티켓 안에서
        # 순번을 공유하지 않아(라운드 예약이 역할 무관 전역이다) 값 충돌이 없다.
        events = [
            (item.reviewer_ordinal, item.status)
            for item in confirmations.get(finding_id, ())
        ] + [
            (item.round, item.status)
            for item in machine_confirmations.get(finding_id, ())
        ]
        events.sort(key=lambda pair: pair[0])
        statuses = [status for _ordinal, status in events]
        trailing_unresolved = 0
        for status in reversed(statuses):
            if status not in {"unresolved", "regressed"}:
                break
            trailing_unresolved += 1
        if trailing_unresolved >= 2:
            repeated.append(finding_id)
        elif not statuses or statuses[-1] != "resolved":
            accepted.append((finding, disposition))
    if repeated:
        raise review_form.PMReviewError(
            "repeated-unresolved", f"동일 accepted finding 2회 연속 미해소/퇴행: {sorted(repeated)}"
        )
    # 확인 커서 = max(이 ID 의 인과 floor, 명세 전역 단조 커서). 새 확인 블록은 명세 끝에 붙으므로
    # 두 제약을 다 넘어야 파서가 받는다.
    cursor_baseline = last_confirmation_round or 0
    confirmation_cursor = tuple(
        (
            finding_id,
            max(cursor_baseline, _pm_review_confirmation_floor(
                finding,
                confirmations.get(finding_id, ()),
                machine_confirmations.get(finding_id, ()),
            )),
        )
        for finding_id, finding in findings.items()
    )
    return review_form.PMReviewDelta(tuple(accepted), bool(zero_channels), confirmation_cursor)


class PMReviewVerifyTemplate(NamedTuple):
    """`review verify-template` 1회 판정 — accepted finding 을 다섯 갈래로 나눈다(전 상태 분류).

    판정 자료는 라운드 하나가 아니라 **티켓 전역 누적**이다 — ID 별 최신 verify 행이 그 ID 의
    현재 선언이고 뒤 행이 앞 행을 이긴다. 그래서 라운드를 나눠 고친 티켓도, 빈틈 보고로 한 라운드를
    넘긴 티켓도 앞 라운드의 선언을 잃지 않는다.

    버킷마다 그 선언이 실린 developer 라운드 순번을 함께 싣는다 — PM 이 어느 라운드의 선언을
    보고 있는지가 메시지와 확인 블록의 `round` 에 그대로 필요하다.
    """

    machine_rows: tuple[tuple[int, review_form.PMReviewVerifyRow], ...]   # (source round, 확인 가능 행)
    reviewer_required: tuple[tuple[str, int, str], ...]       # (id, source round, 사유)
    gap: tuple[tuple[str, int, str], ...]                     # (id, source round, 빈틈 요지)
    stale: tuple[tuple[str, int, int], ...]                   # (id, source round, 확인 커서)
    missing: tuple[str, ...]                                  # 선언이 어느 라운드에도 없는 id
    # 열린 accepted 전건 × (그 ID 의 최신 선언 값 또는 None) — 다음 시드가 실을 행 그대로.
    # 기본값이 없다: 값을 넘기지 않은 조립은 빈 프리필로 접히지 않고 TypeError 로 터진다.
    prefill_rows: tuple[tuple[str, dict[str, str] | None], ...]
    # strict dual binding된 PM-owned 행 — resolve가 reviewer/re-review 없이 terminal 확인을 쓴다.
    pm_owned_rows: tuple[tuple[int, review_form.PMReviewVerifyRow], ...] = ()

    def seed_prefill_rows(self) -> tuple[tuple[str, dict[str, str] | None], ...]:
        """다음 developer 라운드 시드가 실을 verify 행 — **열린 accepted 전건**이다.

        버킷으로 좁히지 않는다. PM 이 fix 라운드의 수정 범위를 좁히면 그 라운드 시드에서 다른
        finding 의 행이 빠지고, 개발자는 그 행을 다시 보지 못한 채 라운드를 닫는다 — 앞 라운드의
        기대값이 낡아도 아무도 다시 재지 않는다(그 사건이 이 규칙의 기원이다). 재개방 상한은
        유한하다: 확인으로 닫힌 finding 은 `delta.accepted` 에서 이미 빠지므로 여기 오지 않는다.

        값이 있는 행은 그 ID 의 최신 선언(command·expected·before·reason)이고, `machine_verifiable`
        은 렌더가 항상 자리표시자로 낸다 — 개발자가 그 자리를 채우는 것이 곧 재선언이라 확인
        커서가 그 라운드로 올라가고, PM 의 기계 확인이 열린 accepted 전건을 다시 실행한다.
        """
        return self.prefill_rows


def _pm_review_latest_verify_rows(
    rounds: Sequence, *, through_ordinal: int | None = None,
) -> dict[str, tuple[int, review_form.PMReviewVerifyRow]]:
    """ID 별 **최신** verify 행과 그 행이 실린 developer 라운드 순번(누적 장부).

    시야는 티켓 전역이다 — 모든 developer 라운드를 순번 오름차순으로 훑어 뒤 관측이 앞 관측을
    이기게 둔다. 산출 없는 라운드(`pending`)는 자리표시자 골격뿐이라 스캔 대상이 아니다
    (`latest_round_of_role` 의 배제 규칙과 같다). `through_ordinal` 은 그 순번 시점까지의 누적으로
    시야를 자른다(CLI `--round`).

    관측은 두 종류다. 채워진 행은 그 ID 의 현재 선언이 되고, **자리표시자 그대로인 행은
    tombstone(선언 없음)** 이 되어 앞 라운드의 선언을 지운다. 이 구별이 없으면 산출이 있는
    라운드에서 시드가 요구한 행을 그냥 비워 둔 태만이 앞 라운드의 빈틈 보고를 최신 선언으로
    물려받아 정상 종료로 통과한다. 행 자체가 없는 조용한 라운드는 그 ID 에 대한 관측이 아니라
    무활동이라 앞 선언이 그대로 이월된다 — 두 형상을 가르는 신호가 시드가 실제로 심어 둔
    자리표시자 행의 유무다.
    """
    latest: dict[str, tuple[int, review_form.PMReviewVerifyRow]] = {}
    for item in sorted(rounds, key=lambda entry: entry.ordinal):
        if item.role != "developer" or getattr(item, "pending", False):
            continue
        if through_ordinal is not None and item.ordinal > through_ordinal:
            continue
        rows, unfilled = _pm_review_verify_round_declarations(item)
        for finding_id in unfilled:
            latest.pop(finding_id, None)
        for finding_id, row in rows.items():
            latest[finding_id] = (item.ordinal, row)
    return latest


def pm_review_verify_template(
    ticket_text: str, rounds: Sequence, *, round_ordinal: int | None = None,
) -> PMReviewVerifyTemplate:
    """accepted finding 을 5분류한다 — 이 티켓의 **유일한** 확인 대상 분류기(PM 자의 0).

    분류 입력은 dev 선언(`machine_verifiable`·`reason`) + 엔진 파생(설계 축 finding) + 확인
    커서뿐이다. 산문은 보지 않는다(선언만이 신호). 쓰는 쪽(시드 프리필)과 읽는 쪽(판정 요구)이
    이 함수 하나를 소비해야 시드가 요구한 것과 판정이 요구하는 것이 갈리지 않는다.

    `round_ordinal` 을 주면 그 developer 라운드 **시점까지의** 누적으로 판정한다(과거 재현).
    """
    delta = parse_pm_review_delta(ticket_text, rounds)
    if round_ordinal is not None and not any(
        item.role == "developer" and item.ordinal == round_ordinal for item in rounds
    ):
        raise review_form.PMReviewError(
            "malformed", f"developer ordinal={round_ordinal} 라운드가 없습니다",
        )
    latest_rows = _pm_review_latest_verify_rows(rounds, through_ordinal=round_ordinal)
    cursors = dict(delta.confirmation_cursor)

    machine_rows: list[tuple[int, review_form.PMReviewVerifyRow]] = []
    reviewer_required: list[tuple[str, int, str]] = []
    pm_owned_rows: list[tuple[int, review_form.PMReviewVerifyRow]] = []
    gap: list[tuple[str, int, str]] = []
    stale: list[tuple[str, int, int]] = []
    missing: list[str] = []
    prefill_rows: list[tuple[str, dict[str, str] | None]] = []
    accepted_ids = {finding.id for finding, _disposition in delta.accepted}
    pm_owned_ids = review_form._pm_review_pm_owned_contract_ids(
        delta, {
            finding_id: entry[1] for finding_id, entry in latest_rows.items()
            if finding_id in accepted_ids
        },
        require_marked=False,
    )
    for finding, _disposition in delta.accepted:
        entry = latest_rows.get(finding.id)
        # 시드 프리필은 버킷과 무관하게 **열린 accepted 전건**이다 — 분류는 PM 확인 대상을
        # 정하고, 프리필은 다음 라운드가 다시 봐야 할 행을 정한다(두 물음이 다르다).
        contract_prefill = None
        if finding.fix_contract is not None:
            contract_prefill = {
                "command": finding.fix_contract["command"],
                "expected": finding.fix_contract["expected"],
                "before": finding.fix_contract["failure"],
                "reason": "",
            }
        prefill_rows.append((
            finding.id,
            contract_prefill if entry is None else review_form._pm_review_verify_seed_values(entry[1]),
        ))
        if entry is None:
            missing.append(finding.id)
            continue
        source_round, row = entry
        if row.reason == review_form.PM_REVIEW_VERIFY_GAP_REASON:
            # 빈틈 보고가 먼저다 — 설계 축이든 아니든 dev 의 최신 선언은 "이번에 구현하지
            # 않았다" 이고, PM 이 읽어야 하는 것도 그쪽이다(확인 채널이 아니라 상태).
            gap.append((finding.id, source_round, row.expected))
            continue
        if finding.id in pm_owned_ids:
            pm_owned_rows.append((source_round, row))
            continue
        if not row.machine_verifiable:
            reviewer_required.append((
                finding.id, source_round,
                f"dev 선언 기계 판정 불가({row.reason}) — reviewer 확인 전용",
            ))
            continue
        cursor = cursors.get(finding.id, 0)
        if source_round <= cursor:
            # 행은 있으나 확인 창을 지났다 — 이 순번으로 확인 블록을 쓰면 파서가 거부한다.
            stale.append((finding.id, source_round, cursor))
            continue
        machine_rows.append((source_round, row))
    machine_rows.sort(key=lambda pair: pair[0])   # source round 오름차순(왕복 순서) · 라운드 안 순서 보존
    return PMReviewVerifyTemplate(
        tuple(machine_rows), tuple(reviewer_required), tuple(gap), tuple(stale),
        tuple(missing), tuple(prefill_rows), tuple(pm_owned_rows),
    )


def _pm_review_machine_confirmation_count(
    ticket_text: str, *, reviewer_role: str,
) -> int:
    """그 채널의 `pm-review-confirmation-v1` machine/PM-owned 확인 행 수(표시면 관용 카운트).

    `pm_verified_evidence_problem` 호출 시점에는 `parse_pm_review_delta` 가 이미 같은
    블록들을 엄격 검증했으므로, 여기서는 재검증 없이 개수만 센다. 채널은 **필수**다 — 그
    채널 finding ID 접두(예: `X-`)로 시작하는 확인 행만 세고, 접두를 무시하고 전부 세는
    무스코프 경로는 두지 않는다(다른 채널의 기계 확인이 이 채널 게이트를 여는 구멍이다).
    미등록 역할은 `_pm_review_finding_id_prefix` 가 loud 실패시킨다."""
    prefix = f"{review_form._pm_review_finding_id_prefix(reviewer_role)}-"
    total = 0
    for block in review_form._pm_review_json_blocks(ticket_text):
        if block.kind != review_form.PM_REVIEW_CONFIRMATION_BLOCK:
            continue
        rows = block.value.get("confirmations")
        if not isinstance(rows, list):
            continue
        total += sum(
            1 for row in rows
            if isinstance(row, dict) and isinstance(row.get("id"), str)
            and row["id"].startswith(prefix)
        )
    return total


def pm_verified_resolution_input_problem(
    ticket_text: str,
    rounds: Sequence,
    *,
    reviewer_role: str,
    surface_floor: int | None,
) -> str | None:
    """resolve 전 read-only preflight — final-fix 확인 입력이 완전하고 실행 가능한가.

    아직 confirmation/pm-verified가 없는 것이 정상인 자리다. machine 행은 안전한 command 계약이,
    PM-owned 행은 strict scope+false verify 이중 결속이 있어야 ready다. 다른 false/gap/missing/stale은
    reviewer 재투입으로 우회하지 않고 이 단계에서 멈춘다. 이미 accepted 잔여가 0이면 새로 만들
    확인이 없으므로 post-resolution 증거 판정을 그대로 요구한다.
    """
    if reviewer_role not in review_form.REVIEW_ROLES:
        raise review_form.PMReviewError(
            "malformed",
            f"pm-verified preflight 채널이 review 채널이 아닙니다: {reviewer_role!r}",
        )
    try:
        delta = parse_pm_review_delta(ticket_text, rounds)
        template = pm_review_verify_template(ticket_text, rounds)
    except review_form.PMReviewError as exc:
        return f"delta/verify 입력 파싱 실패[{exc.code}]: {exc}"
    channel_accepted = [
        finding for finding, _disposition in delta.accepted
        if finding.reviewer_role == reviewer_role
    ]
    if not isinstance(surface_floor, int) or isinstance(surface_floor, bool):
        return f"{reviewer_role} 채널 표면 잔여 하한을 확인할 수 없어 차단합니다(미상)"
    declared = collect_review_finding_declarations(ticket_text, reviewer_role, rounds)
    if len(declared) < surface_floor:
        return (
            f"{reviewer_role} 채널 판정 표면 finding {len(declared)}건이 잔여 "
            f"{surface_floor}건에 못 미칩니다 — 전건 처분 후 다시 preflight 하세요"
        )
    if not channel_accepted:
        return pm_verified_evidence_problem(
            ticket_text, rounds,
            reviewer_role=reviewer_role, surface_floor=surface_floor,
        )

    accepted_ids = {finding.id for finding in channel_accepted}
    missing = sorted(accepted_ids & set(template.missing))
    if missing:
        return f"final-fix verify 선언이 없습니다: {', '.join(missing)}"
    gaps = sorted(row[0] for row in template.gap if row[0] in accepted_ids)
    if gaps:
        return f"처방 빈틈 finding은 terminal 확인할 수 없습니다: {', '.join(gaps)}"
    reviewer_only = sorted(
        row[0] for row in template.reviewer_required if row[0] in accepted_ids
    )
    if reviewer_only:
        return (
            "임의 machine_verifiable=false finding은 terminal 확인할 수 없습니다: "
            f"{', '.join(reviewer_only)}"
        )
    stale = sorted(row[0] for row in template.stale if row[0] in accepted_ids)
    if stale:
        return f"final-fix verify 선언이 stale입니다: {', '.join(stale)}"
    ready = {
        row.id for _source_round, row in (
            *template.machine_rows, *template.pm_owned_rows,
        )
        if row.id in accepted_ids
    }
    uncovered = sorted(accepted_ids - ready)
    if uncovered:
        return f"terminal 확인 입력으로 분류되지 않은 finding: {', '.join(uncovered)}"
    return None


def pm_verified_evidence_problem(
    ticket_text: str,
    rounds: Sequence,
    *,
    reviewer_role: str,
    surface_floor: int | None,
) -> str | None:
    """`pm-verified` 완료 처분의 발동 조건(증거) — 선언·완료 재검증 공용 · **채널 스코프 필수**.

    선언 시점과 완료 재검증 시점이 **같은 함수**를 본다. 조건은 상한·쿼터가 아니라 증거다:
    delta 가 정상 파싱돼야 한다.

    판정은 언제나 한 채널 안에서만 한다(다른 채널의 accepted 잔여도, 다른 채널의 기계 확인도
    보지 않는다 — 채널 격리): 그 채널의 accepted 잔여가 없어야 하고, 그 채널 판정 표면
    finding 수가 `surface_floor`(그 채널 장부의 잔여 must-fix 건수) 이상이어야 하며, 그 채널에
    accepted 판정이 한 번이라도 있었을 때만 그 채널 terminal 확인이 1건 이상 필요하다(확인할 것이
    없으면 확인을 요구하지 않는다).

    두 인자 모두 **키워드 필수**다 — 생략은 조용한 전역 판정이 아니라 `TypeError` 이고, review
    채널이 아닌 값(`None` 포함)은 `PMReviewError` 다(fail-loud). 생산 호출부는 리뷰 라운드 잔여
    판정(`review_completion_problem` · `INTERNAL_REVIEW_ROLE`)과 추가 리뷰어 release 게이트
    (`ADDITIONAL_REVIEWER_ROLE`) 둘뿐이고, `surface_floor` 가 정수가 아니면(잔여 '미상')
    차단한다(fail-closed).

    machine 행은 마지막 fix의 기계 확인, 엄격한 `pm-owned:` scope + false verify 이중 결속 행은
    PM-owned terminal 확인이 종결 증거다. 재설계나 추가 reviewer 라운드로 우회하지 않는다.
    """
    if reviewer_role not in review_form.REVIEW_ROLES:
        raise review_form.PMReviewError(
            "malformed",
            f"pm-verified 증거 판정 채널이 review 채널이 아닙니다: {reviewer_role!r}",
        )
    try:
        delta = parse_pm_review_delta(ticket_text, rounds)
    except review_form.PMReviewError as exc:
        return f"delta 파싱 실패[{exc.code}]: {exc}"
    channel_accepted = [
        finding for finding, _disposition in delta.accepted
        if finding.reviewer_role == reviewer_role
    ]
    if channel_accepted:
        remaining = ", ".join(finding.id for finding in channel_accepted)
        return f"{reviewer_role} 채널 PM 판정 accepted 잔여가 있습니다: {remaining}"
    declared = collect_review_finding_declarations(ticket_text, reviewer_role, rounds)
    if not isinstance(surface_floor, int) or isinstance(surface_floor, bool):
        return f"{reviewer_role} 채널 표면 잔여 하한을 확인할 수 없어 차단합니다(미상)"
    if len(declared) < surface_floor:
        return (
            f"{reviewer_role} 채널 판정 표면 finding {len(declared)}건이 잔여 "
            f"{surface_floor}건에 못 미칩니다 — 전건 처분 후 다시 선언하세요"
        )
    rejected: set[str] = set()
    for item in _pm_review_surface_rounds(rounds):
        if item.role != reviewer_role:
            continue
        rejected |= _pm_review_rejected_finding_ids(
            ticket_text, reviewer_role=reviewer_role, reviewer_ordinal=item.ordinal,
        )
    had_accepted = bool(declared - rejected)
    if had_accepted and _pm_review_machine_confirmation_count(
        ticket_text, reviewer_role=reviewer_role,
    ) < 1:
        return f"{reviewer_role} 채널의 기계 확인(pm-review-confirmation-v1) 기록이 없습니다"
    return None


def _pm_review_section_review_blocks(round) -> list[review_form._PMReviewBlock]:
    """라운드 파일 안에서만 review block 을 스캔한다 — 다른 라운드의 손상이 새지 않는다."""
    return [
        block for block in review_form._pm_review_json_blocks(round.text)
        if block.kind == review_form.PM_REVIEW_BLOCK
    ]


def _pm_review_summary_section_rows(
    round, label: str, *, pm_area: str,
) -> list[str]:
    """한 리뷰 라운드의 요약 줄 — 이 라운드의 파싱 실패는 이 라운드 안에서만 접힌다."""
    blocks = _pm_review_section_review_blocks(round)
    if not blocks:
        return [f"  {label} versioned block 없음"]   # versioned block 도입 전 라운드.
    if len(blocks) != 1:
        raise review_form.PMReviewError(
            "malformed",
            f"reviewer ordinal={round.ordinal}에는 {review_form.PM_REVIEW_BLOCK} block이 "
            "정확히 하나여야 합니다",
        )
    value = blocks[0].value
    review_form._pm_review_exact_keys(value, review_form.PM_REVIEW_PAYLOAD_KEYS, review_form.PM_REVIEW_BLOCK)
    version = review_form._pm_review_version(value, review_form.PM_REVIEW_BLOCK)
    if not isinstance(value["findings"], list) or not isinstance(
        value["confirmations"], list
    ):
        raise review_form.PMReviewError("malformed", "findings/confirmations는 JSON array여야 합니다")
    findings = [
        review_form._pm_review_parse_finding(
            item, round.ordinal, reviewer_role=round.role, version=version,
        )
        for item in value["findings"]
    ]
    confirmations = [
        review_form._pm_review_parse_confirmation(
            item, round.ordinal, reviewer_role=round.role,
        )
        for item in value["confirmations"]
    ]
    _block, disposition_rows = _pm_review_disposition_rows_for_ordinal(
        pm_area, round.ordinal, reviewer_role=round.role,
    )
    decisions = {parsed.id: parsed.decision for parsed, _raw in disposition_rows}
    if not findings and not confirmations:
        return [f"  {label} finding 0건"]
    rows = [
        f"  {label} {finding.id} "
        f"severity={finding.severity or review_form.PM_REVIEW_SEVERITY_UNSPECIFIED_LABEL} "
        f"class={finding.classification} "
        f"PM={decisions.get(finding.id, '미판정')}"
        for finding in findings
    ]
    rows += [
        f"  {label} {confirmation.id} 확인={confirmation.status}"
        for confirmation in confirmations
    ]
    return rows


def _pm_review_summary_rows(ticket_text: str, rounds: Sequence) -> list[str]:
    """리뷰 라운드별 finding 을 (채널·순번·심각도·분류·판정 상태) 한 줄로 축약한다.

    표시면이라 관용 판정이다 — 한 라운드의 블록이 옛 스키마이거나 JSON 이 깨져도 그 라운드 한
    줄만 경고로 접고 나머지 요약은 그대로 낸다. PM 판정 블록의 출처는 명세 전문이다(판정 블록은
    정의상 라운드 파일 밖이다).
    """
    refused = _pm_review_refused_rounds(rounds)
    rows: list[str] = []
    for item in sorted(rounds, key=lambda entry: entry.ordinal):
        if item.role not in review_form.REVIEW_ROLES:
            continue
        label = f"{item.role}[{item.ordinal}]"
        if (item.role, item.ordinal) in refused:
            # 엔진이 거부한 라운드는 산출 조회만 열고 판정 표면에서는 뺀다.
            rows.append(f"  {label} 회수 거부(판정 표면 제외)")
            continue
        try:
            rows.extend(
                _pm_review_summary_section_rows(item, label, pm_area=ticket_text)
            )
        except delegate_errors.DelegateError as exc:
            rows.append(f"  {label} ⚠ 요약 불가: {exc}")
    return rows


def render_pm_review_summary(ticket_text: str, rounds: Sequence) -> str:
    """`board.py show` 가 붙이는 리뷰 블록 요약(사람용 렌더는 블록에서만 파생).

    표시면이라 파싱 실패로 티켓 조회를 깨지 않는다 — 사유를 그 자리에 loud 하게 남긴다.
    라운드 안 블록의 실패(옛 스키마·JSON 손상·개수 위반)는 그 라운드 한 줄로 접히고, 여기서
    접는 것은 명세의 PM 판정 블록 손상처럼 **목록 자체**를 읽을 수 없게 하는 형상뿐이다.
    """
    try:
        rows = _pm_review_summary_rows(ticket_text, rounds)
    except delegate_errors.DelegateError as exc:
        return f"-- 리뷰 finding 요약 --\n  ⚠ 요약 불가: {exc}\n"
    if not rows:
        return ""
    return "-- 리뷰 finding 요약 --\n" + "\n".join(rows) + "\n"


class ReviewResidual(NamedTuple):
    """마지막 판정 표면 code-reviewer 라운드의 리뷰 잔여 — 판정 입력은 그 라운드 파일 하나다."""

    ordinal: int
    # 0 · N · None(미상 — 두 판정 축이 판정을 세우지 못했다).
    must_fix: int | None
    # 미상일 때 `_internal_round_verdict` 의 진단(사유 — 처방).
    diagnostic: str | None


def review_residual(rounds: Sequence) -> ReviewResidual | None:
    """리뷰 잔여 — 판정 표면 code-reviewer 라운드 중 최대 순번 하나의 판정.

    판정 표면 라운드가 없으면 None 이다(리뷰 없음·미회수·전부 폐기). 판정은 라운드 파일 bytes 의
    두 축 결합(`_internal_round_verdict`)이고 다른 기록은 읽지 않는다.
    """
    surface = [
        item for item in _pm_review_surface_rounds(rounds)
        if item.role == review_form.INTERNAL_REVIEW_ROLE
    ]
    if not surface:
        return None
    last = surface[-1]
    assessment = review_verdict._internal_round_verdict(last.text)
    outcome = assessment.outcome
    if outcome.verdict == 0:
        must_fix: int | None = 0
    elif outcome.verdict == 1 and outcome.must_fix_items is not None:
        must_fix = len(outcome.must_fix_items)
    else:
        must_fix = None
    diagnostic = (
        assessment.diagnostic.message
        if must_fix is None and assessment.diagnostic is not None else None
    )
    return ReviewResidual(last.ordinal, must_fix, diagnostic)


def _review_residual_problem(residual: ReviewResidual, problem: str) -> str:
    """잔여가 처분·확인되지 않은 사유 — 라운드 좌표·잔여·판정 사유·진단을 한 문구로."""
    label = (
        "미상(판정 무효)" if residual.must_fix is None
        else f"must-fix {residual.must_fix}건"
    )
    message = (
        f"code-reviewer 리뷰 라운드 {residual.ordinal:02d} 의 잔여 {label} 가 처분·확인되지 "
        f"않았습니다 — {problem}"
    )
    if residual.diagnostic is not None:
        message += f"\n판정 추출 진단: {residual.diagnostic}"
    return message


def review_completion_problem(ticket_text: str, rounds: Sequence) -> str | None:
    """완료 판정의 리뷰 잔여 — 잔여가 처분·확인되지 않았으면 사유, 아니면 None.

    판정 표면 리뷰 라운드가 없거나 잔여가 0 이면 대상이 아니다. 잔여가 N 이거나 미상이면
    `pm_verified_evidence_problem`(표면 하한 = 그 잔여)이 통과해야 한다 — 미상은 하한이 서지
    않아 차단된다(판정 불능은 통과가 아니다). 판정 입력 손상도 차단 사유다.
    """
    try:
        residual = review_residual(rounds)
        if residual is None or residual.must_fix == 0:
            return None
        problem = pm_verified_evidence_problem(
            ticket_text, rounds,
            reviewer_role=review_form.INTERNAL_REVIEW_ROLE, surface_floor=residual.must_fix,
        )
    except review_form.PMReviewError as exc:
        return f"리뷰 잔여 판정 입력 손상[{exc.code}]: {exc}"
    if problem is None:
        return None
    return _review_residual_problem(residual, problem)


def review_resolution_input_problem(ticket_text: str, rounds: Sequence) -> str | None:
    """종결의 확인·처분 입력 선검사 — 리뷰를 돌려받은 멤버만 종결로 넘어간다.

    리뷰 라운드가 없거나, 최대 순번 리뷰 라운드를 돌려받지 않았거나(시드 그대로), 판정 표면
    리뷰 라운드가 없으면(전부 폐기) 각각 사유와 처방을 낸다. 잔여 0 이면 None, 그 밖은
    `pm_verified_resolution_input_problem`(표면 하한 = 잔여)이 판정한다.
    명세 frontmatter `tier` 가 pm-direct 이고 리뷰 라운드가 하나도 없으면 리뷰 없음 사유를 내지
    않는다. 리뷰 라운드가 있으면 tier 와 무관하게 위 판정을 그대로 받는다.
    """
    reviews = sorted(
        (item for item in rounds if item.role == review_form.INTERNAL_REVIEW_ROLE),
        key=lambda item: item.ordinal,
    )
    if not reviews:
        frontmatter, _body = ticket_rules._parse_ticket_text(
            markdown_text._normalized_newlines(ticket_text), "<티켓 명세>")
        if frontmatter.get("tier") == PM_DIRECT_TIER:
            return None
        return (
            "code-reviewer 리뷰 라운드가 없습니다 — 묶음 종결은 돌려받은 리뷰가 있어야 "
            "진행합니다. 같은 하네스면 `pm_delegate.py ticket prepare --cluster C-<ticket-group> "
            "--role code-reviewer --cwd <슬롯>` 로 준비해 서브에이전트로, 다르면 "
            "`pm_delegate.py --role code-reviewer --cluster C-<ticket-group> --cwd <슬롯>` 로 리뷰하세요"
        )
    last = reviews[-1]
    if last.pending:
        return (
            f"code-reviewer 리뷰 라운드 {last.ordinal:02d} 를 돌려받지 않았습니다(시드 그대로) — "
            "`pm_delegate.py ticket harvest --copy <run-dir> --cwd <슬롯>` 로 돌려받으세요"
            "(`pm_delegate.py ticket copies --unharvested` 가 사본 경로를 냅니다)"
        )
    try:
        residual = review_residual(rounds)
        if residual is None:
            return (
                "판정 표면에 오른 code-reviewer 리뷰 라운드가 없습니다(전부 폐기) — "
                f"`pm_delegate.py ticket prepare --cluster C-<ticket-group> --role code-reviewer "
                f"{PREPARE_REOPEN_ORDINAL_FLAG} {last.ordinal} --cwd <슬롯>` 로 재개방해 "
                "리뷰를 다시 돌려받으세요"
            )
        if residual.must_fix == 0:
            return None
        problem = pm_verified_resolution_input_problem(
            ticket_text, rounds,
            reviewer_role=review_form.INTERNAL_REVIEW_ROLE, surface_floor=residual.must_fix,
        )
    except review_form.PMReviewError as exc:
        return f"리뷰 잔여 판정 입력 손상[{exc.code}]: {exc}"
    if problem is None:
        return None
    return _review_residual_problem(residual, problem)
