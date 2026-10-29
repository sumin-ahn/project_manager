"""설계와 리뷰의 계약 문법 (층: domain)."""
from __future__ import annotations

from typing import NamedTuple
from pathlib import PurePosixPath
from collections.abc import Sequence
from pm_engine.domain import delegate_errors
from pm_engine.domain import delegate_prompt_rules
from pm_engine.domain import delegate_run_rules
import json
from pm_engine.domain import review_form
from pm_engine.domain import review_state
from pm_engine.domain import review_verdict



def pm_review_refused_line(role: str) -> str:
    """그 역할 라운드에 붙는 표식 줄 — 발행이 이 함수 하나를 본다."""
    return f"<!-- {review_form.PM_REVIEW_REFUSED_MARKER} role={role} -->"


# 추가 리뷰어 채널 인스턴스 — 옛 산출에 이미 박혀 있는 그 줄이다(마이그레이션 없음).
ADDITIONAL_REVIEWER_REFUSED_LINE = pm_review_refused_line(review_form.ADDITIONAL_REVIEWER_ROLE)


def _round_text_with_refused_marker(text: str, role: str) -> str:
    """표식을 끝줄로 붙인 본문 — 개행 표기는 원문을 따른다(bytes 대조를 쓰는 자리들 때문)."""
    newline = "\r\n" if "\r\n" in text else "\n"
    separator = "" if text.endswith(("\n", "\r")) else newline
    return f"{text}{separator}{pm_review_refused_line(role)}{newline}"


def _round_text_without_refused_marker(text: str, role: str) -> str:
    """그 역할 표식을 끝에서 걷어 낸 본문 — 발행 이전 bytes 를 되돌리는 역함수.

    포기(`abandon_ticket_copy`)의 파괴 판정 기준선은 표식을 붙이기 **전** bytes 다. 재호출은
    이미 표식이 붙은 파일을 읽으므로 그 자리에서 기준선을 되돌리지 않으면, 엔진이 자기가 쓴
    줄을 "산출이 생겼다" 로 읽어 남은 정리를 영영 끝내지 못한다.
    """
    line = pm_review_refused_line(role)
    for terminator in ("\r\n", "\n", ""):
        suffix = f"{line}{terminator}"
        if text.endswith(suffix):
            return text[: -len(suffix)]
    return text


def validate_review_block(
    reply_text: str, *, reviewer_role: str = review_form.ADDITIONAL_REVIEWER_ROLE,
) -> str | None:
    """리뷰 산출의 `pm-review-v1` 블록을 회수 전에 검증한다(위반 사유 또는 None).

    채널은 finding ID 접두만 가른다 — 스키마·중복·자리 규칙은 두 채널이 같다.
    """
    try:
        blocks = review_form._pm_review_json_blocks(reply_text)
    except review_form.PMReviewError as exc:
        return str(exc)
    dispositions = [
        block for block in blocks if block.kind == review_form.PM_REVIEW_DISPOSITION_BLOCK
    ]
    if dispositions:
        return (
            f"리뷰어 산출에 {review_form.PM_REVIEW_DISPOSITION_BLOCK} 이 있습니다 — PM 판정 블록은 "
            "라운드 파일 밖 명세(PM 영역)가 소유합니다"
        )
    reviews = [block for block in blocks if block.kind == review_form.PM_REVIEW_BLOCK]
    if len(reviews) != 1:
        return f"{review_form.PM_REVIEW_BLOCK} block 이 정확히 하나가 아닙니다(발견 {len(reviews)}개)"
    value = reviews[0].value
    try:
        review_form._pm_review_exact_keys(value, review_form.PM_REVIEW_PAYLOAD_KEYS, review_form.PM_REVIEW_BLOCK)
        version = review_form._pm_review_version(value, review_form.PM_REVIEW_BLOCK)
        if not isinstance(value["findings"], list) or not isinstance(
            value["confirmations"], list
        ):
            raise review_form.PMReviewError("malformed", "findings/confirmations는 JSON array여야 합니다")
        parsed = [
            review_form._pm_review_parse_finding(
                item, 0, reviewer_role=reviewer_role, version=version,
            )
            for item in value["findings"]
        ]
        parsed += [
            review_form._pm_review_parse_confirmation(
                item, 0, reviewer_role=reviewer_role,
            )
            for item in value["confirmations"]
        ]
    except review_form.PMReviewError as exc:
        return str(exc)
    ids = [item.id for item in parsed]
    duplicated = sorted({item for item in ids if ids.count(item) > 1})
    if duplicated:
        # 같은 ID 를 `findings` 와 `confirmations` 양쪽에 실은 형상이 여기로 온다 — 사유가
        # 이름을 짚어야 리뷰어가 어느 항목을 옮겨야 하는지 그 자리에서 안다.
        return (
            f"finding/confirmation ID 중복: {', '.join(duplicated)} — 기존 finding 은 "
            "`confirmations` 로만 참조하고 `findings` 에는 신규 ID 만 씁니다"
        )
    return None


def collect_confirmable_finding_ids(
    ticket_text: str, reviewer_role: str, rounds: Sequence,
) -> list[str]:
    """확인 라운드가 `confirmations` 에 실을 수 있는 그 채널 ID (정렬).

    배제는 둘이고 리뷰 라운드 시드 프리필(`render_ticket_growth_section_seed`)과 **같은 규칙**
    이다: 회수 거부 라운드(판정 표면 밖)와 PM 이 `rejected` 로 판정한 ID(재등장을 표면이
    malformed 로 막는다). 두 채널이 서로 다른 목록을 보면 한쪽 리뷰어가 표면이 거부할 ID 를
    확인 대상으로 받는다.
    """
    declared = review_state.collect_review_finding_declarations(
        ticket_text, reviewer_role, rounds,
    )
    if not declared:
        return []
    rejected: set[str] = set()
    for item in review_state._pm_review_surface_rounds(rounds):
        if item.role != reviewer_role:
            continue
        try:
            rejected |= review_state._pm_review_rejected_finding_ids(
                ticket_text, reviewer_role=reviewer_role,
                reviewer_ordinal=item.ordinal,
            )
        except delegate_errors.DelegateError:
            continue        # 읽을 수 없는 판정 블록은 표면 파서가 loud 하게 잡는다.
    return sorted(declared - rejected)


def _review_id_collisions(
    body: str, existing_finding_ids: Sequence[str],
) -> list[str]:
    """회신 블록의 **신규 finding** ID 중 티켓에 이미 있는 것(확인 라운드는 대상 아님)."""
    existing = set(existing_finding_ids)
    if not existing:
        return []
    blocks = [
        block for block in review_form._pm_review_json_blocks(body)
        if block.kind == review_form.PM_REVIEW_BLOCK
    ]
    if len(blocks) != 1:
        return []
    findings = blocks[0].value.get("findings")
    if not isinstance(findings, list):
        return []
    collided = {
        item["id"] for item in findings
        if isinstance(item, dict) and isinstance(item.get("id"), str)
        and item["id"] in existing
    }
    return sorted(collided)


def _review_missing_confirmation_targets(
    body: str, declared_finding_ids: Sequence[str],
) -> list[str]:
    """회신 블록의 confirmation ID 중 티켓 판정 표면에 **없는** 것.

    판정 표면은 confirmation 이 선행 finding 을 참조할 것을 요구한다. 라운드가 파일 하나라
    차등 판정·반사실 프로브가 사라졌으므로, 표면에 없는 ID(회수 거부된 라운드의
    ID·환각 ID)를 실은 블록을 막는 것은 이 대조 하나다.
    """
    declared = set(declared_finding_ids)
    blocks = [
        block for block in review_form._pm_review_json_blocks(body)
        if block.kind == review_form.PM_REVIEW_BLOCK
    ]
    if len(blocks) != 1:
        return []
    confirmations = blocks[0].value.get("confirmations")
    if not isinstance(confirmations, list):
        return []
    missing = {
        item["id"] for item in confirmations
        if isinstance(item, dict) and isinstance(item.get("id"), str)
        and item["id"] not in declared
    }
    return sorted(missing)


def review_harvest_problem(
    reply_text: str, *, ticket_text: str, rounds: Sequence,
    reviewer_role: str = review_form.ADDITIONAL_REVIEWER_ROLE,
) -> str | None:
    """리뷰 산출을 라운드로 회수해도 되는지 판정한다 — 위반 사유 또는 None.

    두 회수 경로가 이 함수 하나를 부른다: 추가 리뷰어 회수(`additional_reviewer`)와 내부 채널 회수
    (`harvest_ticket_copy`). 채널이 가르는 것은 finding ID 접두뿐이고 사유·강도는 같다 — 판정
    표면이 하나라 한쪽만 관대하면 그쪽 산출이 표면을 막는다.

    거부한 산출은 판정 표면에 오르지 않되 사라지지도 않는다. 추가 리뷰어 채널은 라운드 파일을
    만들지 않고 raw 에 남기고, 내부 채널은 board 라운드 파일 bytes 를 그대로 두고 슬롯 run-dir 을
    유지한다 — 사본을 고쳐 같은 경로로 다시 회수할 수 있다.

    신규 finding ID 대조의 corpus 에서 시드 그대로인(`pending`) 라운드와 엔진 표식이 붙은
    라운드는 뺀다 — 그 라운드가 실은 ID 는 엔진이 넣은 자리표시이지 선언이 아니고, 회수 대상
    라운드 자신도 그 상태다.

    사유는 네 축이고 종류를 가리지 않고 같은 처리(거부)다: 엔진 전용 표식 선언 · `pm-review-v1`
    블록 규칙 위반(부재·중복·스키마·JSON 손상 · 같은 ID 를 `findings` 와 `confirmations` 양쪽
    기재) · 티켓에 이미 있는 finding ID 재선언 · 티켓 판정 표면에 없는 confirmation 대상.

    대조는 두 축이고 시야가 다르다. 신규 finding ID 는 **넓은** 스캔(산문 인용 포함)과,
    confirmation 대상은 **판정 표면 선언**과 맞춘다. 차등 판정·반사실 프로브는 없다 — 이 산출이
    다른 라운드의 판정을 오염시킬 자리가 없다(F-028 구조 폐쇄).
    """
    if review_form.pm_review_refused_marker_present(reply_text):
        return (
            f"엔진 전용 표식({review_form.PM_REVIEW_REFUSED_MARKER})을 회신이 선언했습니다 — 그 줄은 "
            "라운드를 판정 표면에서 빼므로 산출이 스스로 쓸 수 없습니다"
        )
    problem = validate_review_block(reply_text, reviewer_role=reviewer_role)
    if problem is not None:
        return problem
    existing_ids = review_state.collect_review_finding_ids(ticket_text, reviewer_role)
    for item in rounds:
        if (
            getattr(item, "pending", False)
            or review_form.pm_review_refused_marker_present(item.text)
        ):
            # 엔진이 시드에 넣은 ID 는 **선언이 아니다**. 회수 대상 라운드 자신이 이 목록에
            # 들어 있으므로(예약된 board 파일은 회수 전까지 시드 그대로다), 시드가 실은 다음 ID
            # 를 그대로 쓴 회신이 자기 자신과 충돌해 거부되는 자기 충돌을 여기서 닫는다.
            # 표식이 붙은 라운드도 같은 자리에서 뺀다 — 표식은 bytes 를 바꿔 `pending` 을
            # 무너뜨리지만 그 골격의 ID 는 여전히 엔진이 넣은 자리표시다(종결된 예약).
            continue
        existing_ids |= review_state.collect_review_finding_ids(item.text, reviewer_role)
    collisions = _review_id_collisions(reply_text, existing_ids)
    if collisions:
        return (
            f"finding ID 재선언: {', '.join(collisions)} — 티켓에 이미 있는 ID 라 이 라운드를 "
            "회수하지 않았습니다(다음 라운드에서 새 ID 로 다시 내십시오)"
        )
    missing = _review_missing_confirmation_targets(
        reply_text,
        review_state.collect_review_finding_declarations(
            ticket_text, reviewer_role, rounds,
        ),
    )
    if missing:
        return (
            f"confirmation 대상 finding 부재: {', '.join(missing)} — 티켓 판정 표면에 없는 "
            "ID 라 이 라운드를 회수하지 않았습니다(존재하지 않는 ID 이거나 회수되지 않은 "
            "산출의 ID 입니다 · 다음 라운드에서 실재하는 ID 로 다시 내십시오)"
        )
    return None


# ── 내부 code-reviewer 라운드 장부 ────────────────────────────────────────

_INTERNAL_MUST_FIX_CANONICAL_HEADER = "## must-fix"


def _internal_verdict_declaration_forms() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """external verdict 파서의 정확일치 토큰에서 preamble 선언형을 파생한다."""

    def declarations(tokens: Sequence[str]) -> tuple[str, ...]:
        # casefold가 같은 대소문자 변형도 parser가 각각 받으므로 생략하지 않는다.
        ordered = sorted(tokens, key=lambda token: (token.isascii(), token.casefold(), token))
        return tuple(f"판정: {token}" for token in ordered)

    return (
        declarations(review_verdict._PASS_VERDICT_TOKENS),
        declarations(review_verdict._REJECT_VERDICT_TOKENS),
    )


def _internal_review_format_preamble() -> str:
    """내부 reviewer가 보는 산출 계약 — 판정/0건 parser 원천에서 매번 합성."""
    pass_forms, reject_forms = _internal_verdict_declaration_forms()
    canonical_pass, canonical_reject = review_verdict._internal_canonical_verdict_forms()
    pass_examples = ", ".join(f"`{form}`" for form in pass_forms)
    reject_examples = ", ".join(f"`{form}`" for form in reject_forms)
    none_examples = ", ".join(
        f"`- {token}`" for token in review_verdict._INTERNAL_NONE_ITEM_TOKENS
    )
    canonical_none = f"- {review_verdict._INTERNAL_NONE_ITEM_TOKENS[0]}"
    return (
        "내부 리뷰 산출 형식(판정 파서 계약):\n"
        f"- 판정 선언은 행 선두에 `{canonical_pass}` 또는 `{canonical_reject}`로 정확히 "
        "한 번 쓴다. 강조·헤딩 접두는 허용하지만 "
        "인용문·코드펜스 안 선언은 판정으로 세지 않는다.\n"
        f"- must-fix 절은 markdown 제목 `{_INTERNAL_MUST_FIX_CANONICAL_HEADER}`로 열고 "
        "목록으로 쓴다. 0건이면 표준형 "
        f"`{canonical_none}` 한 항목을 남긴다(파서가 같은 원천에서 받는 0건 항목: "
        f"{none_examples}). 산문 `must-fix 없습니다`는 0건으로 읽히지 않는다.\n"
        "- 판정 낱말은 파서 허용 토큰 중 하나만 쓰며 통과·반려 또는 허용 밖 낱말을 "
        f"섞지 않는다(통과 허용형: {pass_examples}; 반려 허용형: {reject_examples}).\n"
        "- 같은 code-reviewer 사본 절에 section-add가 시드한 리뷰 골격을 그대로 채운다. "
        "키·상태·분류를 다시 쓰거나 골격 밖 schema를 만들지 않는다."
    )


class ArchitectTest(NamedTuple):
    id: str
    target: str
    command: str
    expected: str
    negative: str


def _contract_test_targets(
    value: str, field: str, *, required: bool = True,
) -> tuple[str, ...]:
    """계약이 지목한 repo-relative Python 테스트 대상 — 값의 **첫 토큰** 하나뿐이다.

    산문 전역을 훑으면 "옛 경로 대신 새 경로" 같은 서술의 옛 경로까지 diff 결속 대상이 된다 —
    이번 라운드가 손댈 자리가 아닌 파일을 회수가 요구하게 되므로, 대상은 값이 맨 앞에 적은
    하나로 고정한다. 여러 파일을 결속하려면 계약 행을 여러 개 쓴다.

    ``required`` 는 architect 축(설계 라운드가 확정하는 필수 테스트)의 규칙이다 —
    거기서는 테스트 대상이 계약이라 하나도 없으면 거부한다. reviewer fix 축은
    붙일 테스트가 없는 지적(죽은 코드 삭제)을 표현할 수 있어야 하므로 끈다.
    """
    head = value.split(maxsplit=1)
    match = delegate_run_rules._CONTRACT_TEST_TARGET_RE.match(head[0]) if head else None
    if match is not None:
        target = match.group("path")
        path = PurePosixPath(target)
        if path.is_absolute() or ".." in path.parts or path.parts[0] != "tests":
            raise delegate_errors.DelegateError(f"{field} 테스트 대상은 repo-relative tests/*.py여야 합니다: {target}")
        return (target,)
    if required:
        raise delegate_errors.DelegateError(
            f"{field}에 repo-relative 테스트 대상(tests/*.py)이 없습니다"
        )
    return ()


def _reviewer_fix_test_requirement(value: str, field: str) -> tuple[str, tuple[str, ...]]:
    """reviewer의 기존 시험 재실행 선언과 파일 변경 계약을 구분한다."""
    parts = value.split(maxsplit=1)
    prefix = review_form.PM_REVIEW_EXISTING_TEST_PREFIX
    if parts and parts[0].startswith(prefix):
        if parts[0] != prefix or len(parts) != 2:
            raise delegate_errors.DelegateError(
                f"{field}는 {prefix} 다음에 공백과 테스트 경로가 필요합니다"
            )
        return "existing", _contract_test_targets(parts[1], field)
    targets = _contract_test_targets(value, field, required=False)
    return ("changed" if targets else "location"), targets


def _contract_changed_paths(value: str, changed_paths: frozenset[str]) -> tuple[str, ...]:
    """계약 산문의 낱말 중 이번 diff 의 바뀐 경로와 글자 그대로 같은 것.

    fix 축의 작업 증명은 "계약이 적은 파일을 이번 라운드가 바꿨는가"다. 슬래시 없는
    낱말은 모양만으로 최상위 파일과 버전 번호·심볼·도메인을 가를 수 없어, 바뀐 경로와
    같은 낱말만 센다. 첫 등장 순서로, 중복 없이 돌려준다.
    """
    paths: list[str] = []
    for match in delegate_run_rules._CONTRACT_REPO_PATH_RE.finditer(value):
        target = match.group("path")
        if target in changed_paths and target not in paths:
            paths.append(target)
    return tuple(paths)


def parse_architect_tests(text: str) -> tuple[ArchitectTest, ...]:
    """architect 라운드의 유일한 테스트 계약을 엄격 파싱한다."""
    lines = text.splitlines()
    openings = [index for index, line in enumerate(lines) if line == f"```{review_form.ARCHITECT_TEST_BLOCK}"]
    malformed = [
        line for line in lines
        if review_form.ARCHITECT_TEST_BLOCK in line and line != f"```{review_form.ARCHITECT_TEST_BLOCK}"
    ]
    if malformed or len(openings) != 1:
        raise delegate_errors.DelegateError(
            f"architect 테스트 계약은 `{review_form.ARCHITECT_TEST_BLOCK}` block 정확히 1개여야 합니다"
        )
    start = openings[0]
    try:
        end = lines.index("```", start + 1)
    except ValueError as exc:
        raise delegate_errors.DelegateError("architect 테스트 계약 종료 fence가 없습니다") from exc
    if any(line.startswith("```") for line in lines[start + 1:end]):
        raise delegate_errors.DelegateError("architect 테스트 계약 안에 중첩 fence가 있습니다")
    try:
        payload = json.loads(
            "\n".join(lines[start + 1:end]), object_pairs_hook=review_form._pm_review_json_object,
        )
    except ValueError as exc:
        raise delegate_errors.DelegateError(f"architect 테스트 계약 JSON 파싱 실패: {exc}") from exc
    if not isinstance(payload, dict):
        raise delegate_errors.DelegateError("architect 테스트 계약 payload는 JSON object여야 합니다")
    try:
        review_form._pm_review_exact_keys(payload, review_form.ARCHITECT_TEST_PAYLOAD_KEYS, review_form.ARCHITECT_TEST_BLOCK)
    except review_form.PMReviewError as exc:
        raise delegate_errors.DelegateError(str(exc)) from exc
    if payload.get("version") != review_form.ARCHITECT_TEST_VERSION:
        raise delegate_errors.DelegateError(
            f"architect 테스트 계약 version은 {review_form.ARCHITECT_TEST_VERSION}이어야 합니다"
        )
    raw_tests = payload.get("tests")
    if not isinstance(raw_tests, list) or not raw_tests:
        raise delegate_errors.DelegateError("architect 테스트 계약에는 필수 테스트가 1건 이상 필요합니다")
    tests: list[ArchitectTest] = []
    seen: set[str] = set()
    for raw in raw_tests:
        if not isinstance(raw, dict):
            raise delegate_errors.DelegateError("architect test 행은 JSON object여야 합니다")
        try:
            review_form._pm_review_exact_keys(raw, review_form.ARCHITECT_TEST_ROW_KEYS, "architect test")
        except review_form.PMReviewError as exc:
            raise delegate_errors.DelegateError(str(exc)) from exc
        values: dict[str, str] = {}
        for key in review_form.ARCHITECT_TEST_ROW_KEYS:
            try:
                values[key] = review_form._pm_review_contract_string(
                    raw.get(key), f"architect test.{key}",
                )
            except review_form.PMReviewError as exc:
                raise delegate_errors.DelegateError(str(exc)) from exc
        if delegate_run_rules._ARCHITECT_TEST_ID_RE.fullmatch(values["id"]) is None:
            raise delegate_errors.DelegateError(f"architect test.id 형식 불일치: {values['id']!r}")
        if values["id"] in seen:
            raise delegate_errors.DelegateError(f"architect test.id 중복: {values['id']}")
        seen.add(values["id"])
        try:
            review_form._pm_review_assert_verify_command_shape(
                values["command"], f"architect test {values['id']}.command",
            )
        except review_form.PMReviewError as exc:
            raise delegate_errors.DelegateError(str(exc)) from exc
        _contract_test_targets(
            values["target"], f"architect test {values['id']}.target",
        )
        tests.append(ArchitectTest(**values))
    return tuple(tests)


def render_pm_review_disposition_template(
    ticket_text: str, rounds: Sequence, reviewer_ordinal: int | None = None,
    *, reviewer_role: str | None = None,
) -> str:
    """기존 판정을 보존하고 미판정 행을 채운 단일 PM disposition 골격을 렌더한다.

    입력은 명세(`ticket_text` — PM 판정 블록의 자리)와 라운드 목록이다. 채널(`reviewer_role`)
    미지정이면 두 리뷰 채널을 통틀어 **최신 라운드**의 채널을 쓴다 — PM 은 채널마다 다른 절차를
    밟지 않고 같은 명령으로 판정한다.
    """
    review_rounds = review_state._pm_review_surface_rounds(rounds)
    if reviewer_role is None:
        reviewer_role = (
            review_rounds[-1].role if review_rounds else review_form.INTERNAL_REVIEW_ROLE
        )
    elif reviewer_role not in review_form.REVIEW_ROLES:
        raise review_form.PMReviewError("malformed", f"review 채널이 아닌 역할: {reviewer_role}")
    reviewer_sections = [
        item for item in review_rounds if item.role == reviewer_role
    ]
    if not reviewer_sections:
        raise review_form.PMReviewError("malformed", f"{reviewer_role} 라운드가 없습니다")
    if reviewer_ordinal is None:
        section = reviewer_sections[-1]
    else:
        matches = [item for item in reviewer_sections if item.ordinal == reviewer_ordinal]
        if not matches:
            raise review_form.PMReviewError(
                "malformed",
                f"{reviewer_role} ordinal={reviewer_ordinal} 라운드가 없습니다",
            )
        section = matches[0]

    block = review_state._pm_review_block_for_round(section)
    value = block.value
    review_form._pm_review_exact_keys(value, review_form.PM_REVIEW_PAYLOAD_KEYS, review_form.PM_REVIEW_BLOCK)
    version = review_form._pm_review_version(value, review_form.PM_REVIEW_BLOCK)
    if not isinstance(value["findings"], list) or not isinstance(value["confirmations"], list):
        raise review_form.PMReviewError("malformed", "findings/confirmations는 JSON array여야 합니다")
    findings = [
        review_form._pm_review_parse_finding(
            item, section.ordinal, reviewer_role=section.role, version=version,
        )
        for item in value["findings"]
    ]
    confirmations = [
        review_form._pm_review_parse_confirmation(
            item, section.ordinal, reviewer_role=section.role,
        )
        for item in value["confirmations"]
    ]
    finding_ids = [item.id for item in findings]
    if len(finding_ids) != len(set(finding_ids)):
        raise review_form.PMReviewError("malformed", f"reviewer ordinal={section.ordinal} finding ID 중복")
    confirmation_ids = [item.id for item in confirmations]
    if len(confirmation_ids) != len(set(confirmation_ids)):
        raise review_form.PMReviewError(
            "malformed", f"reviewer ordinal={section.ordinal} confirmation ID 중복",
        )

    existing_block, existing_rows = review_state._pm_review_disposition_rows_for_ordinal(
        ticket_text, section.ordinal, reviewer_role=section.role,
    )
    existing_by_id = {parsed.id: raw for parsed, raw in existing_rows}
    if set(existing_by_id) - set(finding_ids):
        raise review_form.PMReviewError(
            "malformed",
            f"reviewer ordinal={section.ordinal} extra disposition ID: "
            f"{sorted(set(existing_by_id) - set(finding_ids))}",
        )
    if finding_ids and existing_block is not None and "finding_zero" in existing_block:
        raise review_form.PMReviewError("malformed", "finding이 있는 reviewer에 finding_zero disposition")

    # 골격의 시야는 대상 라운드 하나인데 판정 표면의 finding ID 유일성은 티켓 전역이다. 두
    # 시야가 갈리면 골격이 프리필한 ID 를 PM 이 채워도 `parse_pm_review_delta` 가 그 판정을
    # 되돌려보낸다. 그런 ID 가 하나라도 있으면 **골격을 내지 않는다** — 나머지 ID 만 부분
    # 출력해도 그 골격을 채운 산출은 같은 라운드 때문에 표면이 통째로 거부하므로(왕복 불변식은
    # 복구되지 않고 판정 대상 finding 만 조용히 사라진다) 이 라운드는 여기서 끝낸다.
    # 형상은 둘이다: 선행 같은 채널 라운드가 이미 선언한 ID(재선언), 그리고 이 라운드가 스스로
    # `confirmations` 에 실은 자기 ID(자기-확인). 판정 표면(`parse_pm_review_delta`)도 같은
    # 라운드를 malformed 로 막으므로 두 명령이 같은 상태에 같은 판정을 낸다.
    redeclared = review_state.collect_review_finding_declarations(
        ticket_text, section.role, rounds, before_ordinal=section.ordinal,
    )
    self_confirmed = set(confirmation_ids)
    refused_ids = [
        finding_id for finding_id in finding_ids
        if finding_id in redeclared or finding_id in self_confirmed
    ]
    if refused_ids:
        listed = ", ".join(
            f"{finding_id}("
            + ("티켓 전역 재선언" if finding_id in redeclared else "같은 라운드 자기-확인")
            + ")"
            for finding_id in refused_ids
        )
        raise review_form.PMReviewError(
            "malformed",
            f"reviewer {section.role} ordinal={section.ordinal}에 판정 표면이 받지 않는 "
            f"finding ID 가 있어 판정 골격을 내지 않습니다: {listed} — 기존 finding 은 "
            "`confirmations` 로만 참조하고 `findings` 에는 신규 ID 만 씁니다",
        )

    if not finding_ids:
        if confirmation_ids:
            raise delegate_errors.DelegateError(
                f"reviewer {section.role} ordinal={section.ordinal}은 confirmation-only "
                "라운드라 PM이 판정할 신규 finding이 없습니다"
            )
        if existing_block is not None:
            raise review_form.PMReviewError(
                "pending", f"reviewer ordinal={section.ordinal}은 이미 finding-zero 판정됨",
            )
        payload = review_form._pm_review_seed_object(review_form.PM_REVIEW_FINDING_ZERO_PAYLOAD_KEYS, {
            "version": review_form.PM_REVIEW_DISPOSITION_VERSION,
            review_form.PM_REVIEW_DISPOSITION_ROLE_KEY: section.role,
            "reviewer_ordinal": section.ordinal,
            "finding_zero": "accepted",
        })
    else:
        pending_ids = [
            finding_id for finding_id in finding_ids
            if finding_id not in existing_by_id
        ]
        if not pending_ids:
            raise review_form.PMReviewError(
                "pending", f"reviewer ordinal={section.ordinal}의 미판정 finding이 없습니다",
            )
        decision_placeholder = "<accepted|rejected>"
        dispositions = [
            review_form._pm_review_seed_object(
                review_form.PM_REVIEW_DISPOSITION_KEYS,
                existing_by_id.get(finding_id, {
                    "id": finding_id,
                    "decision": decision_placeholder,
                }),
            )
            for finding_id in finding_ids
        ]
        payload = review_form._pm_review_seed_object(review_form.PM_REVIEW_DISPOSITION_PAYLOAD_KEYS, {
            "version": review_form.PM_REVIEW_DISPOSITION_VERSION,
            review_form.PM_REVIEW_DISPOSITION_ROLE_KEY: section.role,
            "reviewer_ordinal": section.ordinal,
            "dispositions": dispositions,
        })
    return f"```{review_form.PM_REVIEW_DISPOSITION_BLOCK}\n" + review_form._pm_review_render_json(payload) + "\n```\n"


def render_pm_review_delta(ticket: str, delta: review_form.PMReviewDelta) -> str:
    """developer prompt에 붙이는 accepted-only 최소 renderer. 0건이면 빈 문자열.

    심각도는 블록이 단일 진실이라 여기서 그대로 표기한다 — dev 는 산문을 다시 읽지 않고 이
    렌더에서 우선순위를 안다.
    """
    if not delta.accepted:
        return ""
    lines = [f"## PM 승인 리뷰 delta — {ticket}", ""]
    for finding, disposition in delta.accepted:
        lines.extend([
            f"### {finding.id}",
            f"- 채널: {finding.reviewer_role}",
            f"- 심각도: {finding.severity or review_form.PM_REVIEW_SEVERITY_UNSPECIFIED_LABEL}",
            f"- 분류: {finding.classification}",
            f"- 권위 근거: {finding.authority}",
            f"- 관측 증거: {finding.evidence}",
            f"- reviewer 권고: {finding.recommendation}",
            f"- 설계 변경 여부: {'true' if finding.design_change else 'false'}",
            f"- PM 판정 근거: {disposition.reason}",
            f"- 허용 수정 범위: {disposition.scope}",
        ])
        if disposition.prerequisite:
            lines.append(f"- 선행 권위: {disposition.prerequisite}")
        lines.append("")
    body = "\n".join(lines).rstrip()
    return f"{body}\n\n{delegate_prompt_rules.PM_REVIEW_FIX_SCOPE_NOTICE}"


# ── verify-template — 기계 확인 대상 분류 + PM 확인 골격 ────────────


def render_pm_review_verify_template(template: review_state.PMReviewVerifyTemplate) -> str:
    """기계 확인 대상 행의 PM 확인 골격 — source round 별 블록을 순번 오름차순으로 낸다.

    `id`·`command` 는 그대로 옮기고 `status`·`observed` 는 자리표시자다(no-hand-retyping).
    `expected` 는 판정 참고용 안내 줄로만 보인다 — 확인 블록 스키마 자체에는 없는 필드다(단일
    진실은 verify 행 · 불변식 5 의 정신 상속).

    블록이 여럿인 이유는 확인 블록의 `round` 가 **그 행이 실린 developer 라운드**에 결속되기
    때문이다. 오름차순은 파서의 전역 단조 증가 규칙과 같은 순서라, 낸 순서대로 명세에 붙이면
    그대로 수용된다(왕복 정합).
    """
    if not template.machine_rows:
        return ""
    grouped: dict[int, list[review_form.PMReviewVerifyRow]] = {}
    for source_round, row in template.machine_rows:
        grouped.setdefault(source_round, []).append(row)
    blocks: list[str] = []
    for source_round in sorted(grouped):
        lines = [f"## 기계 확인 대상 — developer round {source_round}", ""]
        for row in grouped[source_round]:
            lines.append(f"- {row.id}: `{row.command}` → expected: {row.expected}")
        lines.append("")
        confirmations = [
            review_form._pm_review_seed_object(review_form.PM_REVIEW_MACHINE_CONFIRMATION_ROW_KEYS, {
                "id": row.id,
                "status": "<" + "|".join(review_form.PM_REVIEW_CONFIRMATION_STATES) + ">",
                "command": row.command,
                "observed": "<관측값>",
            })
            for row in grouped[source_round]
        ]
        payload = review_form._pm_review_seed_object(review_form.PM_REVIEW_MACHINE_CONFIRMATION_PAYLOAD_KEYS, {
            "version": review_form.PM_REVIEW_MACHINE_CONFIRMATION_VERSION,
            "round": source_round,
            "confirmations": confirmations,
        })
        lines.append(
            review_form._pm_review_fenced_json(review_form.PM_REVIEW_CONFIRMATION_BLOCK, payload).rstrip("\n")
        )
        blocks.append("\n".join(lines) + "\n")
    return "\n".join(blocks)


# ── 확인 커맨드 실행 — 엔진이 돌리고 엔진이 기입한다 ────────────────
# 확인 골격(`render_pm_review_verify_template`)이 내던 자리표시자 두 칸(`status`·`observed`)을
# 엔진이 실행 결과로 채운다. 사람이 커맨드를 옮겨 치고 관측값을 옮겨 적는 왕복이 사라지므로
# 옮겨 적기 drift 도 사라진다. 판정은 여전히 파서가 소유한다 — 여기서는 `expected ⊂ observed`
# 와 rc 를 그대로 재현할 뿐 완화하지 않는다(비-0 rc 는 통과로 접지 않는다).
#
# 안전 경계는 신설하지 않는다: 커맨드는 이미 "금지 토큰 없는 단일 명령"(불변식 12)이라
# `shell=False` + 인자 분해로 그대로 실행된다. 셸 해석을 열면 그 경계가 무의미해진다.
PM_REVIEW_CONFIRMATION_COMMAND_TIMEOUT_SEC = 1800
# 관측값 발췌 상한 — 명세에 그대로 실리는 값이라 회귀 전문을 담지 않는다. 기대값이 실제로
# 관측됐다면 그 자리를 중심으로 자르고(판정과 기입이 갈리지 않게), 아니면 꼬리를 남긴다
# (실패 진단은 대개 끝에 있다).
PM_REVIEW_CONFIRMATION_OBSERVED_LIMIT = 2000
_PM_REVIEW_OBSERVED_ELLIPSIS = "…"
# 명세 PM 영역에 기입하는 절 제목 — PM 관행이던 문자열을 **엔진 소유 상수**로 승격한다.
# 엔진이 그 절을 쓰기 시작하는 순간 제목은 관행이 아니라 기계 표면이다.
PM_REVIEW_CONFIRMATION_SECTION = "## PM 기계 확인"


def _pm_review_observed_excerpt(text: str, expected: str) -> str:
    """관측값 발췌 — 기대값이 있으면 그 자리를 포함하도록 자른다.

    판정(전문 기준)과 기입(발췌)이 갈리면 엔진이 resolved 로 적은 행을 파서가 malformed 로
    거부한다(왕복 정합 위반). 그래서 자르는 자리는 기대값 위치가 정한다.
    """
    limit = PM_REVIEW_CONFIRMATION_OBSERVED_LIMIT
    if len(text) <= limit:
        return text
    index = text.find(expected) if expected else -1
    if index < 0:
        return _PM_REVIEW_OBSERVED_ELLIPSIS + text[-limit:]
    end = min(len(text), max(index + len(expected), index + limit))
    start = max(0, end - limit)
    prefix = _PM_REVIEW_OBSERVED_ELLIPSIS if start > 0 else ""
    suffix = _PM_REVIEW_OBSERVED_ELLIPSIS if end < len(text) else ""
    return prefix + text[start:end] + suffix


def render_pm_review_confirmation_section(
    rows: Sequence[tuple[int, review_form.PMReviewMachineConfirmation]],
) -> str:
    """실행 결과를 명세 PM 영역에 붙일 절로 렌더한다 — 자리표시자 0.

    블록을 source round 별로 나누는 규칙·오름차순은 확인 골격(`render_pm_review_verify_template`)
    과 같다 — 파서의 전역 단조 증가 규칙과 같은 순서라 낸 순서대로 붙으면 그대로 수용된다.
    """
    if not rows:
        return ""
    grouped: dict[int, list[review_form.PMReviewMachineConfirmation]] = {}
    for source_round, row in rows:
        grouped.setdefault(source_round, []).append(row)
    sections: list[str] = []
    for source_round in sorted(grouped):
        sections.append(
            f"{PM_REVIEW_CONFIRMATION_SECTION} (developer round {source_round})\n\n"
            + _render_pm_review_confirmation_block(source_round, grouped[source_round])
        )
    return "\n".join(sections)


def _render_pm_review_confirmation_block(
    source_round: int, rows: Sequence[review_form.PMReviewMachineConfirmation],
) -> str:
    """한 developer round의 confirmation fence — 새 append와 기존 block merge 공용."""
    payload = review_form._pm_review_seed_object(review_form.PM_REVIEW_MACHINE_CONFIRMATION_PAYLOAD_KEYS, {
        "version": review_form.PM_REVIEW_MACHINE_CONFIRMATION_VERSION,
        "round": source_round,
        "confirmations": [
            review_form._pm_review_seed_object(review_form.PM_REVIEW_MACHINE_CONFIRMATION_ROW_KEYS, {
                "id": row.id,
                "status": row.status,
                "command": row.command,
                "observed": row.observed,
            })
            for row in rows
        ],
    })
    return review_form._pm_review_fenced_json(review_form.PM_REVIEW_CONFIRMATION_BLOCK, payload)


def _pm_review_confirmation_blocks_for_merge(
    text: str, *, confirmation_only: bool,
) -> tuple[tuple[review_form._PMReviewBlock, int, tuple[review_form.PMReviewMachineConfirmation, ...]], ...]:
    """confirmation block을 merge 가능한 strict 행으로 읽는다."""
    parsed: list[tuple[review_form._PMReviewBlock, int, tuple[review_form.PMReviewMachineConfirmation, ...]]] = []
    for block in review_form._pm_review_json_blocks(text):
        if block.kind != review_form.PM_REVIEW_CONFIRMATION_BLOCK:
            if confirmation_only:
                raise review_form.PMReviewError(
                    "malformed",
                    f"confirmation append 입력에 다른 review block이 있습니다: {block.kind}",
                )
            continue
        value = block.value
        review_form._pm_review_exact_keys(
            value, review_form.PM_REVIEW_MACHINE_CONFIRMATION_PAYLOAD_KEYS,
            review_form.PM_REVIEW_CONFIRMATION_BLOCK,
        )
        review_form._pm_review_version(
            value, review_form.PM_REVIEW_CONFIRMATION_BLOCK,
            allowed=(review_form.PM_REVIEW_MACHINE_CONFIRMATION_VERSION,),
        )
        source_round = value.get("round")
        if (
            not isinstance(source_round, int) or isinstance(source_round, bool)
            or source_round < 1
        ):
            raise review_form.PMReviewError(
                "malformed", "confirmation.round은 1 이상 정수여야 합니다",
            )
        raw_rows = value.get("confirmations")
        if not isinstance(raw_rows, list):
            raise review_form.PMReviewError("malformed", "confirmations는 JSON array여야 합니다")
        rows = tuple(
            review_form._pm_review_parse_machine_confirmation_row(
                raw, round_ordinal=source_round,
            )
            for raw in raw_rows
        )
        ids = [row.id for row in rows]
        if len(ids) != len(set(ids)):
            raise review_form.PMReviewError(
                "malformed", f"{review_form.PM_REVIEW_CONFIRMATION_BLOCK} round={source_round} ID 중복",
            )
        parsed.append((block, source_round, rows))
    if confirmation_only and not parsed:
        raise review_form.PMReviewError("malformed", "append할 confirmation block이 없습니다")
    return tuple(parsed)


def _merge_pm_review_confirmation_section(
    body: str, section: str, *, rounds: Sequence,
) -> tuple[str, bool]:
    """같은 developer round block에는 빠진 strict PM-owned 행만 원자 병합한다."""
    existing = _pm_review_confirmation_blocks_for_merge(
        body, confirmation_only=False,
    )
    incoming = _pm_review_confirmation_blocks_for_merge(
        section, confirmation_only=True,
    )
    existing_rounds = [source_round for _block, source_round, _rows in existing]
    incoming_rounds = [source_round for _block, source_round, _rows in incoming]
    if len(existing_rounds) != len(set(existing_rounds)):
        raise review_form.PMReviewError(
            "malformed", f"기존 confirmation round 중복: {existing_rounds}",
        )
    if len(incoming_rounds) != len(set(incoming_rounds)):
        raise review_form.PMReviewError(
            "malformed", f"append confirmation round 중복: {incoming_rounds}",
        )

    by_round = {
        source_round: (block, rows)
        for block, source_round, rows in existing
    }
    replacements: list[tuple[int, int, str]] = []
    additions: list[tuple[int, review_form.PMReviewMachineConfirmation]] = []
    changed = False
    for _incoming_block, source_round, rows in incoming:
        current = by_round.get(source_round)
        if current is None:
            additions.extend((source_round, row) for row in rows)
            changed = True
            continue
        block, existing_rows = current
        merged = list(existing_rows)
        existing_by_id = {row.id: row for row in existing_rows}
        round_changed = False
        for row in rows:
            present = existing_by_id.get(row.id)
            if present is not None:
                if present != row:
                    raise review_form.PMReviewError(
                        "malformed",
                        f"confirmation round={source_round} id={row.id} 기존 행과 충돌합니다",
                    )
                continue
            if not (
                row.status == "resolved"
                and row.command == review_form.PM_REVIEW_PM_OWNED_CONFIRMATION_COMMAND
            ):
                raise review_form.PMReviewError(
                    "malformed",
                    f"confirmation round={source_round} 기존 block에는 빠진 strict PM-owned "
                    f"terminal 행만 병합할 수 있습니다: {row.id}",
                )
            merged.append(row)
            existing_by_id[row.id] = row
            round_changed = True
        if round_changed:
            replacements.append((
                block.start, block.end,
                _render_pm_review_confirmation_block(source_round, merged),
            ))
            changed = True

    merged_body = body
    for start, end, replacement in sorted(replacements, reverse=True):
        merged_body = merged_body[:start] + replacement + merged_body[end:]
    if additions:
        merged_body = (
            merged_body.rstrip("\n") + "\n\n"
            + render_pm_review_confirmation_section(additions)
        )
    # merge 뒤 full parser가 disposition/verify 이중 결속과 spoof를 최종 판정한다. write는 이
    # 검증 뒤라 conflict/malformed가 원본 일부를 바꾸는 창이 없다.
    review_state.parse_pm_review_delta(merged_body, rounds)
    return merged_body, changed
