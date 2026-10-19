"""라운드 시드와 산출 없음 판정 (층: app).

역할별 골격 렌더(설계 근거·확인 대상 프리필·미회수 developer 경고 포함), 시드 그대로 판정, 그 판정을 싣는
라운드 읽기·검증을 소유한다.
"""
from __future__ import annotations

import sys
from pathlib import Path
from collections.abc import Mapping, Sequence

from pm_engine.domain import delegate_errors, design_rules, markdown_text, review_form, review_state, round_rules
from pm_engine.infra import fs, round_store


def render_round_seed(
    role: str, ticket_text: str, *, today: str,
    previous_round: tuple[int, str] | None = None, rounds: "Sequence" = (),
) -> str:
    """라운드 파일의 시드 — 첫 줄 헤더 + 역할 골격 본문.

    헤더 형식은 `## <라벨> (<role> · <YYYY-MM-DD>)` 로 종전 역할 절 헤더와 같다. 골격 본문은
    `review_form` 의 골격 렌더를 부른다(시드 문구의 단일 진실은 그쪽이다).

    `previous_round` 는 **같은 역할의 직전 라운드** `(순번, 본문)` 이다 — 리뷰 역할 골격이
    확인 대상 finding ID 를 프리필하는 유일한 입력이고, 예약 시점에만 쓴다. 없으면
    자리표시자다. 무편집 판정(`_text_is_pending`)은 이 값을 입력으로 쓰지 않는다.

    `rounds` 는 이 티켓의 라운드 전체(이 예약 이전)다 — developer 골격이 확인 대상 분류기를
    돌려 verify 행을 프리필하는 유일한 입력이다. 호출자(`prepare_ticket_copy`·
    `cmd_section_add`)는 이미 `existing` 을 들고 있어 인자만 추가한다(신규 로드 0).

    판정을 세우지 못한 상태의 developer 라운드는 `RoundsError` 로 거부된다 — 예약 전이므로
    라운드 파일도 장부 행도 남지 않는다.
    """
    return (
        round_rules.render_round_header(role, today=today) + "\n\n"
        + _render_round_seed_body(role, ticket_text, previous_round, rounds)
    )


def _render_round_seed_body(
    role: str, ticket_text: str, previous_round: tuple[int, str] | None = None,
    rounds: "Sequence" = (),
) -> str:
    """역할 골격 본문 — 시드 렌더의 거부를 이 모듈의 오류형으로 옮긴다.

    골격 렌더는 판정을 세우지 못한 상태(PM 미판정·선행 결정 필요)를 거부한다. 그 거부는 예약
    **전**에 일어나므로 잔여가 없고, 두 진입점(`ticket prepare`·`board section-add`)이 같은 규칙을
    쓴다. 번역이 없으면 board 쪽은 `RoundsError` 만 잡으므로 같은 거부가 traceback 이 된다.
    """
    try:
        return render_ticket_growth_section_seed(
            role, ticket_text, previous_round=previous_round, rounds=rounds,
        )
    except delegate_errors.DelegateError as exc:
        raise round_rules.RoundsError(str(exc)) from exc


def _text_is_pending(role: str, text: str) -> bool:
    """이 파일 하나만 보고 "시드 그대로(산출 없음)"를 판정한다 — 시점 독립.

    첫 줄은 날짜와 사람이 고른 라벨이 드는 자리라 **문법만** 본다(라벨·날짜 자유). 그 아래
    본문은 이 모듈이 내용 구조로 판정한다 — 자리표시자만 든 골격이면 산출이
    없다. 판정 입력에 board 의 다른 라운드도 명세도 넣지 않는다: 넣으면 같은 역할 앞 라운드의
    회수가 손대지 않은 뒤 라운드의 판정을 뒤집는다(회수면과 표시면이 서로 다른 답을 낸다).
    """
    first_line, separator, rest = text.partition("\n")
    if not separator:
        return False
    if round_rules._round_header_re(role).fullmatch(first_line.rstrip("\r")) is None:
        return False
    body = markdown_text._normalized_newlines(rest)
    if not body.startswith("\n"):
        return False        # 헤더와 본문 사이 빈 줄까지가 시드 형식이다.
    return ticket_round_body_is_pending(role, body[1:])


def round_is_pending(round: round_rules.Round, *, ticket_text: str = "") -> bool:
    """이 라운드가 아직 시드 그대로(산출 없음)인가.

    `ticket_text` 는 판정 입력이 아니다 — 소비자 호출 좌표를 유지하기 위한 자리이고, 판정은
    라운드 파일 내용만 본다.
    """
    return _text_is_pending(round.role, round.text)


def load_rounds(
    tickets_dir: Path | str, ticket_id: str, *, ticket_text: str = "",
) -> list[round_rules.Round]:
    """티켓의 라운드를 순번 순으로 읽는다 (없으면 빈 목록).

    판독은 공유 읽기 seam 을 지나고 개행은 원문 그대로 둔다(`newline=""`) — 회수된 bytes 를
    보존해 되쓰는 지점들이 그 구분에 기대 있다. 무편집 판정은 라운드마다 **자기 파일 하나**만
    보므로 라운드 사이 순서 의존이 없다.

    `ticket_text` 는 판정 입력이 아니다 — 소비자 호출 좌표를 유지하기 위한 자리다.
    """
    directory = round_store.rounds_dir_for_ticket(ticket_id, tickets_dir)
    rounds: list[round_rules.Round] = []
    for ordinal, role, path in round_store._scan_round_files(directory):
        text = fs.read_text_shared(
            path, encoding="utf-8", newline="",
        )
        rounds.append(
            round_rules.Round(
                ordinal=ordinal, role=role, path=path, text=text,
                pending=_text_is_pending(role, text),
            )
        )
    return rounds


def verify_rounds(
    tickets_dir: Path | str, ticket_id: str, *, ticket_text: str = "",
) -> list[round_rules.RoundProblem]:
    """라운드 디렉터리의 현재 상태만 보고 판정한다 (장부 없음).

    `round-name` 은 파일명 문법·역할 집합 위반, `round-dup` 은 같은 순번을 둘 이상이 쥔 상태
    (라운드 순서가 모호하다), `round-gap` 은 1..N 의 빈틈(삭제 의심), `round-pending` 은 시드
    그대로인 라운드다. **완료 게이트가 red 로 보는 것은 순번 유일성·연속성이 깨진 둘
    (`round-gap`·`round-dup`)** 이고 나머지 둘은 표시용이다(심각도는 소비자 소유).

    점-접두 항목은 라운드가 아니라 엔진 임시 산출·부산물이라 판정하지 않는다. 판독 실패는
    삼키지 않는다. `ticket_text` 는 판정 입력이 아니다 — 소비자 호출 좌표를 유지하기 위한
    자리다(산출 없음 판정은 라운드 파일 내용만 본다).
    """
    directory = round_store.rounds_dir_for_ticket(ticket_id, tickets_dir)
    problems: list[round_rules.RoundProblem] = []
    valid: list[tuple[int, str, Path]] = []
    if directory.is_dir():
        for entry in sorted(directory.iterdir(), key=lambda item: item.name):
            if round_store._is_engine_temporary(entry.name):
                continue
            parsed = round_rules.parse_round_filename(entry.name) if entry.is_file() else None
            if parsed is None:
                problems.append(
                    round_rules.RoundProblem(
                        round_rules.PROBLEM_NAME,
                        f"라운드 파일 이름 문법 위반: {entry.name}",
                    )
                )
                continue
            valid.append((parsed[0], parsed[1], entry))
    valid.sort(key=lambda item: (item[0], item[1]))

    roles_by_ordinal: dict[int, list[str]] = {}
    for ordinal, role, _path in valid:
        roles_by_ordinal.setdefault(ordinal, []).append(role)
    for ordinal, roles in sorted(roles_by_ordinal.items()):
        if len(roles) > 1:
            problems.append(
                round_rules.RoundProblem(
                    round_rules.PROBLEM_DUPLICATE,
                    f"순번 중복 {ordinal:0{round_rules.ROUND_ORDINAL_WIDTH}d}: {', '.join(sorted(roles))}",
                )
            )
    if roles_by_ordinal:
        missing = [
            f"{number:0{round_rules.ROUND_ORDINAL_WIDTH}d}"
            for number in range(1, max(roles_by_ordinal) + 1)
            if number not in roles_by_ordinal
        ]
        if missing:
            problems.append(
                round_rules.RoundProblem(
                    round_rules.PROBLEM_GAP,
                    f"순번 빈틈(삭제 의심): {', '.join(missing)}",
                )
            )
    for ordinal, role, path in valid:
        text = fs.read_text_shared(
            path, encoding="utf-8", newline="",
        )
        if _text_is_pending(role, text):
            problems.append(
                round_rules.RoundProblem(
                    round_rules.PROBLEM_PENDING,
                    f"산출 없음(시드 그대로): {round_rules.round_filename(ordinal, role)}",
                )
            )
    return problems


# 시드가 이 규칙을 미리 고지한다 — 회수 거부로만 알게 되는 형식은 고지 실패다. 주석은 이 절
# **위**에 둔다: 절 안에 넣으면 아래 파서가 비어 있지 않은 줄 3개를 보고 형식 위반으로 읽는다.
DEVELOPER_REGRESSION_RULE = (
    "이 절의 비어 있지 않은 줄은 커맨드·결과 두 행뿐이다. 커맨드는 백틱으로 끝나고 "
    "결과는 rc=0 을 포함한다."
)

# 리뷰 라운드가 딛고 서는 발판 역할 — 리뷰어가 읽는 dev 증거는 이 역할의 라운드에서만 온다.
REVIEW_SUBJECT_ROLE = "developer"


def unharvested_developer_round(rounds: Sequence):
    """리뷰 라운드가 딛고 설 **직전 developer 라운드**가 산출 없음이면 그 라운드(아니면 None).

    시야는 그 티켓의 마지막 developer 라운드 하나다 — 리뷰 입력은 역할별 **마지막 산출**만
    싣고(`additional_reviewer._select_ticket_body_for_review`), 더 앞 라운드의 미회수는 이 준비가
    되돌릴 수 있는 상태가 아니다.

    developer 라운드가 **아예 없는** 티켓(코드만 보는 독립 검토)은 발판 자체가 없어 대상이
    아니다 — 없는 증거와 비어 있는 증거는 다른 상태이고, 앞의 것은 정상 경로다.

    "산출 없음" 은 라운드가 이미 실은 `pending` 을 그대로 읽는다 — 회수면
    (`harvest_ticket_copy`)이 board 를 바꿀지 정하는 그 시드 대조(`ticket_round_body_is_pending`)
    와 **같은 기준 하나**를 준비면도 소비해야 한쪽만 갱신돼 어긋나지 않는다. 속성 판독 표기는
    같은 규칙을 쓰는 `round_rules.latest_round_of_role` 과 맞춘다.
    """
    developer_rounds = [
        item for item in rounds if item.role == REVIEW_SUBJECT_ROLE
    ]
    if not developer_rounds:
        return None
    latest = max(developer_rounds, key=lambda item: item.ordinal)
    return latest if getattr(latest, "pending", False) else None


def _warn_unharvested_developer_round(rounds: Sequence) -> None:
    """리뷰 라운드 준비면의 loud 경고 — **거부가 아니다**(rc=0 · 판정 어휘의 `gap` 분류).

    거부하지 않는 근거는 실측이다. 시드 그대로인 라운드 예약을 지우거나 되돌릴 수단이 엔진에
    없고(`ticket prepare|harvest|copies` 뿐), kill 된 위임이 남긴 시드 developer 라운드를 이고
    가는 티켓이 실재한다 — 거부하면 그 티켓은 리뷰 라운드를 영영 못 연다. 리뷰어를 dev 없이
    돌리는 정당한 경우(코드만 보는 독립 검토)도 있어 판단은 PM 이 한다.

    준비 시점에 내는 이유는 하나다 — 리뷰가 실행되기 **전**이라야 회수하고 다시 걸 수 있다.
    """
    stale = unharvested_developer_round(rounds)
    if stale is None:
        return
    name = round_rules.round_filename(stale.ordinal, stale.role)
    # 단정문 대신 값 진술 — 앞선 라운드에 산출이 있으면(형상 B) 그것이 리뷰어 입력에 실린다.
    # "실리지 않습니다" 는 그 형상에서 거짓이라 쓰지 않는다. 스폰면
    # (`additional_reviewer._warn_seed_developer_round`)과 같은 계산 하나(
    # `latest_round_of_role`)를 써 두 표면이 같은 값을 말하게 한다.
    latest = round_rules.latest_round_of_role(rounds, REVIEW_SUBJECT_ROLE)
    latest_name = (
        round_rules.round_filename(latest.ordinal, latest.role)
        if latest is not None else "없음"
    )
    print(
        "경고: 리뷰 라운드를 산출 없는 developer 라운드 위에서 준비합니다 — "
        f"{name} 이 시드 골격 그대로입니다. 리뷰어 입력(`rounds/`)에 실리는 developer 산출 "
        f"라운드: {latest_name}. 결함 클래스 전수·검증 근거·빈틈 보고를 이번 라운드에도 실으려면 "
        "먼저 `ticket harvest` 로 회수한 뒤 다시 준비하세요.",
        file=sys.stderr,
    )


def render_ticket_growth_section_seed(
    role: str, ticket_text: str, *,
    previous_round: tuple[int, str] | None = None, rounds: Sequence = (),
) -> str:
    """라운드 예약이 넣을 역할별 본문 골격. review JSON은 parser 상수에서만 파생한다.

    `previous_round` 는 **같은 역할의 직전 라운드** `(순번, 본문)` 이다 — 확인 대상 finding ID
    프리필의 유일한 입력이다. 명세(`ticket_text`)는 PM 판정 블록(=`rejected`
    배제)의 출처이고 finding 선언은 라운드 파일에만 있으므로 두 입력이 함께 필요하다.

    `rounds` 는 이 티켓의 라운드 전체(이 예약 이전)다 — developer 골격이 확인 대상 분류기를
    돌려 verify 행을 프리필하는 유일한 입력이다. 미전달(`()`)이면 accepted 0 건과 동치로 다뤄
    verify fence 없는 기존 골격 그대로 렌더한다(호환 기본값).

    developer 골격은 판정을 세우지 못한 두 상태(PM 미판정·선행 결정 필요)를 **거부**한다.
    강등해서 시드하면 채울 자리가 없는 라운드가 나가고, 두 단계 뒤 그 책임이 dev 태만처럼
    표면화된다. 거부는 예약 전이라 잔여가 없다.
    """
    if role == "architect":
        return (
            "## 경계 실측\n- <관측한 경계와 근거>\n\n"
            "## 불변식\n- <보존할 불변식>\n\n"
            "## 표면 상한\n- <허용 인터페이스와 비목표>\n\n"
            "## 테스트 전략\n- <검증할 정상·실패 경로>\n\n"
            + review_form.render_architect_test_skeleton() + "\n"
            "검토 판정: <설계 통과|수정 후 통과|반려>\n"
        )
    if role == "developer":
        # "PM 초안 → architect 점검 → PM 비준" 3단 규율의 판정 seam. 예약 **전**에
        # 거부해 잔여를 남기지 않는다 — 호출자(`_render_round_seed_body`)가
        # `DelegateError` 를 `RoundsError` 로 번역하고, 그 위(`prepare_ticket_copy`)가 아직
        # board_lock/reserve_round 를 타기 전이라 라운드 파일도 장부 행도 남지 않는다.
        design_problem = design_rules.design_evidence_problem(
            "<T-NNNN>", ticket_text, rounds,
        )
        if design_problem is not None:
            raise delegate_errors.DelegateError(design_problem)
        verify_rows: Sequence[tuple[str, Mapping[str, str] | None]] = ()
        if rounds:
            try:
                delta = review_state.parse_pm_review_delta(ticket_text, rounds)
                missing_contracts = [
                    finding.id for finding, _disposition in delta.accepted
                    if finding.fix_contract is None
                ]
                if missing_contracts:
                    raise review_form.PMReviewError(
                        "malformed",
                        "fix 입력 reviewer 수정·테스트 계약 누락: "
                        + ", ".join(missing_contracts),
                    )
                # 쓰는 쪽과 읽는 쪽이 같은 분류기다 — 시드가 요구하는 ID 집합과 판정이
                # 요구하는 ID 집합은 정의상 같다.
                template = review_state.pm_review_verify_template(ticket_text, rounds)
            except review_form.PMReviewError as exc:
                if exc.code in review_form.PM_REVIEW_SEED_BLOCKING_CODES:
                    # 이 거부는 board 예약 **전**에 일어난다 — 라운드 파일도 장부 행도 남지
                    # 않는다(잔여 0).
                    raise review_form.PMReviewError(
                        exc.code,
                        f"판정을 세우지 못해 developer 라운드를 시드할 수 없습니다"
                        f"[{exc.code}]: {exc}\n"
                        f"  · {review_form._pm_review_prescription(exc.code, '<T-NNNN>', channels=exc.channels)}",
                    ) from exc
                # 최초 구현 라운드(리뷰 라운드 없음) 등은 정상 형상이다 — verify fence 없는
                # 골격으로 강등한다(리뷰 골격 prefill 강등과 동형).
                print(
                    f"경고: accepted delta 를 해소할 수 없어 verify 골격 없이 시드합니다: {exc}",
                    file=sys.stderr,
                )
            else:
                verify_rows = template.seed_prefill_rows()
        return _render_developer_round_seed_body(verify_rows)
    if role == "researcher":
        return (
            "## 조사 질문\n- <무엇을 확정하러 갔는가>\n\n"
            "## 실측\n- <명령/파일>: <관측값>\n\n"
            "## 판단\n- <결론>: <근거>\n\n"
            "## 미해소\n- <남은 질문 또는 없음>\n"
        )
    if role not in review_form.REVIEW_ROLES:
        raise delegate_errors.DelegateError(f"역할별 라운드 골격 미지원: {role}")

    # 리뷰어가 딛고 설 dev 산출이 비어 있는지는 **예약 전** 이 자리에서 낸다 — 리뷰 라운드를
    # 준비하는 세 진입점(`ticket prepare` · cross 위임의 자동 준비 · `board section-add`)이
    # 모두 이 시드 seam 을 지나므로, 여기 한 곳이 그 클래스 전부를 덮는다.
    _warn_unharvested_developer_round(rounds)

    # 두 리뷰 채널은 같은 골격을 쓰고 finding ID 접두만 다르다(판정 표면이 하나이므로).
    id_prefix = review_form._pm_review_finding_id_prefix(role)
    placeholder_id = f"{id_prefix}-NNN"
    confirmation_ids = [placeholder_id]
    if previous_round is not None:
        previous_ordinal, previous_text = previous_round
        previous = review_state._RoundView(role, previous_ordinal, previous_text)
        refused = review_state._pm_review_refused_rounds([previous])
        if (role, previous_ordinal) not in refused:
            try:
                confirmation_ids = review_state._pm_review_round_ids(previous)
                rejected_ids = review_state._pm_review_rejected_finding_ids(
                    ticket_text, reviewer_role=role,
                    reviewer_ordinal=previous_ordinal,
                )
                confirmation_ids = [
                    finding_id for finding_id in confirmation_ids
                    if finding_id not in rejected_ids
                ]
            except delegate_errors.DelegateError as exc:
                print(
                    f"경고: 직전 {role} 라운드의 finding ID prefill을 해소할 수 없어 "
                    f"{placeholder_id} 골격으로 강등합니다: ordinal={previous_ordinal} · {exc}",
                    file=sys.stderr,
                )
                confirmation_ids = [placeholder_id]

    # 다음 finding ID 실값은 **이 자리에서만** 계산한다 — 명세와 라운드 전체를 이미 쥐고 있는
    # 유일한 지점이라 신규 로드가 없고, 아래 렌더러는 문자열만 받는다(무편집 판정의 렌더러이기도
    # 해서 판정 시점에 다시 계산할 수 있는 입력을 그쪽에 두지 않는다).
    return _render_review_round_seed_body(
        role, confirmation_ids, review_state.next_review_finding_id(ticket_text, role, rounds),
    )


def _render_review_round_seed_body(
    role: str, confirmation_ids: Sequence[str], next_finding_id: str | None,
    *, scope_rule: str = review_form.CONFIRM_ROUND_SCOPE_RULE,
    contract_rule: str | None = review_form.PM_REVIEW_CONCRETE_FIX_CONTRACT_RULE,
) -> str:
    """리뷰 채널 라운드 시드 본문 — 확인 대상 ID·다음 finding ID 말고는 전부 골격 상수다.

    `next_finding_id` 는 이 라운드가 쓸 첫 finding ID 실값이다(계산은 호출부 소유 · 기본값 없음).
    `None`(그 값이 생기기 전에 예약된 라운드를 무편집 판정이 되읽은 결과)이거나 자리표시자면 그
    라운드와 **같은 bytes** 를 낸다 — 무편집 판정이 본문 자신에서 읽은 값을 그대로 되돌려주므로,
    옛 시드가 그 변경만으로 "산출 있음" 이 되지 않는다.

    `scope_rule` 은 확인 라운드 주석 문장이다. 예약은 항상 현재 문구(기본값)로 시드하고,
    무편집 판정만 옛 문구(`LEGACY_CONFIRM_ROUND_SCOPE_RULES`)를 넣어 다시 렌더한다 — 문구를
    바꾸기 전에 예약된 라운드도 같은 골격 대조로 "산출 없음" 을 유지한다.

    산문은 판정 요약과 must-fix ID 나열까지다 — 증거·권고·심각도는 블록이 단일 진실이라
    항목별 서술을 다시 적지 않는다(같은 finding 3중 기재 제거). must-fix 절은 0건 라운드의
    통과 선언 교차 확인 입력이라 그대로 남는다.

    렌더와 무편집 판정(`ticket_round_body_is_pending`)이 이 함수 하나를 본다 — 판정이 골격을
    따로 적으면 문구가 갈린 순간 손대지 않은 라운드가 "산출 있음"으로 뒤집힌다.
    """
    placeholder_id = f"{review_form._pm_review_finding_id_prefix(role)}-NNN"
    # F-001 fix — native 위임은 엔진이 프롬프트를 만들지 않으므로 시드가 유일한 보장 채널이다.
    # 프리필된 실 ID(=확인 라운드)일 때만 스코프 문구를 HTML 주석 1줄로 첫 줄 아래에 심는다 —
    # 최초 리뷰 라운드(자리표시자만 있는 골격)는 탐색 스코프를 제한할 대상이 없어 그대로 둔다.
    is_confirmation_round = list(confirmation_ids) != [placeholder_id]
    scope_notice = f"<!-- {scope_rule} -->\n\n" if is_confirmation_round else ""
    # 다음 ID 는 리뷰어가 추측할 값이 아니다 — 엔진이 실값을 싣는다. 안내는 스코프 문구와 같은
    # 표기(주석 1줄)를 쓴다: 엔진 지시가 산출 산문으로 굳지 않는다.
    prefilled_finding_id = (
        next_finding_id if next_finding_id and next_finding_id != placeholder_id else None
    )
    finding_id_notice = (
        f"<!-- {review_form.NEXT_FINDING_ID_RULE.format(next_id=prefilled_finding_id)} -->\n"
        if prefilled_finding_id is not None else ""
    )
    contract_notice = f"<!-- {contract_rule} -->\n" if contract_rule is not None else ""
    return (
        scope_notice
        + contract_notice
        + f"## must-fix\n{finding_id_notice}- <없음 또는 finding ID 나열({placeholder_id})·"
        "증거와 권고는 아래 블록>\n\n"
        "## 판정\n판정: <통과|반려> · finding <N>건(must-fix <N>건)\n\n"
        + review_form.render_pm_review_block_skeleton(
            role, confirmation_ids, prefilled_finding_id,
        )
    )


def _render_developer_round_seed_body(
    verify_rows: Sequence[tuple[str, Mapping[str, str] | None]],
) -> str:
    """developer 라운드 시드 본문 — verify 행 말고는 전부 골격 상수(리뷰 골격과 동형).

    `verify_rows` 는 `(finding ID, 프리필 값 또는 None)` 이다 — 프리필 값은 그 ID 의 최신 선언이고
    `machine_verifiable` 은 렌더가 항상 자리표시자로 낸다.

    렌더와 무편집 판정(`ticket_round_body_is_pending`)이 이 함수 하나를 본다 — 판정이 골격을
    따로 적으면 문구가 갈린 순간 손대지 않은 라운드가 "산출 있음"으로 뒤집힌다.
    """
    body = (
        "## 변경 파일\n- `<경로>`: <변경 내용과 이유>\n\n"
        "## 신규 테스트\n- 추가한 테스트: <N개 · 파일/케이스>\n\n"
        f"<!-- {DEVELOPER_REGRESSION_RULE} -->\n"
        "## 회귀\n- 커맨드: `<실행 커맨드>`\n"
        "- 결과: <rc=0 · A passed / 0 failed>\n\n"
        "## DoD evidence\n- <완료 조건>: <충족 근거>\n\n"
        "## 민감도\n- <상수/가드 임시 변경 → red, 복원 → green 실측>\n"
    )
    if verify_rows:
        body += "\n" + review_form.render_pm_review_verify_skeleton(verify_rows)
    return body


def _dev_round_seed_verify_rows(
    body: str,
) -> list[tuple[str, dict[str, str]]] | None:
    """본문의 `pm-review-verify-v1` 블록이 실은 행 목록(판정 불가면 None·부재면 []).

    행마다 `(finding ID, 값 4칸)` 을 그 파일 자신에서 되읽는다 — 시드가 실은 프리필 값은 예약
    당시 board 상태의 산물이라 판정 시점에 다시 계산하면 앞 라운드의 회수가 손대지 않은 뒤
    라운드의 판정을 뒤집는다(`_round_seed_prefill_ids` 와 같은 규율).

    `machine_verifiable` 자리는 **되읽지 않는다**. 되읽으면 dev 가 boolean 을 채운 실산출까지
    왕복이 성립해 모든 developer 라운드가 영구 `pending` 이 된다 — 그 한 자리가 선언 행위의
    유일한 신호라 렌더는 항상 자리표시자를 내고, 채워진 순간 대조가 깨진다.

    accepted 0 건이던 최초 시드는 verify fence 자체가 없다 — 그건 정상 pristine 형상이라
    빈 목록을 돌려준다. 블록이 있는데 파싱이 안 되거나(자리표시자 그대로 편집 중 등) 둘
    이상이면 시드 그대로인지 확신할 수 없어 None(=pending 아님·`_round_seed_prefill_ids`
    와 동형 규칙)이다.

    `machine_verifiable` 골격 자리는 따옴표 없는 raw placeholder 라 손대지 않은
    시드 그대로도 strict JSON 이 아니다 — 구조 스캔 전용 재-인용(`_pm_review_requote_verify_
    placeholder`)으로 fence 존재만 확인하고 행을 뽑는다. 실제 검증 파서
    (`_pm_review_parse_verify_row`)는 이 재-인용을 거치지 않는다(데이터 수용 관용이 아니다)."""
    requoted = review_form._pm_review_requote_verify_placeholder(body)
    try:
        blocks = [
            block for block in review_form._pm_review_json_blocks(requoted)
            if block.kind == review_form.PM_REVIEW_VERIFY_BLOCK
        ]
    except review_form.PMReviewError:
        return None
    if not blocks:
        return []
    if len(blocks) != 1:
        return None
    verifications = blocks[0].value.get("verifications")
    if not isinstance(verifications, list):
        return None
    rows: list[tuple[str, dict[str, str]]] = []
    for item in verifications:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            return None
        values = {key: item.get(key) for key in review_form._PM_REVIEW_VERIFY_SEED_VALUE_KEYS}
        if not all(isinstance(value, str) for value in values.values()):
            return None
        rows.append((item["id"], values))
    return rows


def _round_seed_prefill_ids(body: str) -> tuple[str, list[str]] | None:
    """본문의 유일한 `pm-review-v1` 블록이 실은 `(첫 finding ID, confirmation ID 목록)`.

    골격 대조의 가변 입력을 **그 파일 자신**에서 뽑는다 — 프리필된 ID 도 다음 finding ID 도
    예약 당시 board 상태의 산물이라, 판정 시점에 다시 계산하면 같은 역할 앞 라운드의 회수가
    손대지 않은 뒤 라운드의 판정을 뒤집는다. 판정 불가면 None(=산출 있음 쪽으로 기운다).
    """
    try:
        blocks = [
            block for block in review_form._pm_review_json_blocks(body)
            if block.kind == review_form.PM_REVIEW_BLOCK
        ]
    except review_form.PMReviewError:
        return None
    if len(blocks) != 1:
        return None
    findings = blocks[0].value.get("findings")
    if not isinstance(findings, list) or len(findings) != 1:
        return None
    first = findings[0]
    if not isinstance(first, dict) or not isinstance(first.get("id"), str):
        return None
    confirmations = blocks[0].value.get("confirmations")
    if not isinstance(confirmations, list):
        return None
    ids: list[str] = []
    for item in confirmations:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            return None
        ids.append(item["id"])
    return first["id"], ids


def ticket_round_body_is_pending(role: str, body: str) -> bool:
    """라운드 본문(첫 줄 헤더 제외)이 시드 골격 그대로인가 = 산출 없음.

    판정 입력은 **이 본문 하나**다 — 예약 날짜도, 같은 티켓의 다른 라운드도, 명세도 보지 않는다.
    역할 골격은 상수이고 예약 시점 board 에서 프리필되는 자리(리뷰 채널의 확인 대상·다음 finding
    ID · developer 의 verify 행)만 가변이므로, 그 값들은 본문 자신에서 읽어 같은 골격을 다시
    렌더해 대조한다: 프리필된 값이 있어도 선언 자리(리뷰의 status·evidence · verify 의
    machine_verifiable)가 자리표시자 그대로면 산출이 없는 것이다.

    개행 표기는 정규화해 비교한다 — CRLF 체크아웃(또는 CRLF 를 쓰는 슬롯)을 지난 같은 골격을
    "편집됨"으로 읽으면 산출 없는 라운드가 산출 있는 것으로 위장된다.
    """
    normalized = markdown_text._normalized_newlines(body)
    if role == "developer":
        verify_rows = _dev_round_seed_verify_rows(normalized)
        if verify_rows is None:
            return False
        return normalized == markdown_text._normalized_newlines(
            _render_developer_round_seed_body(verify_rows)
        )
    if role not in review_form.REVIEW_ROLES:
        return normalized == markdown_text._normalized_newlines(
            render_ticket_growth_section_seed(role, "")
        )
    prefill = _round_seed_prefill_ids(normalized)
    if prefill is None:
        return False
    next_finding_id, confirmation_ids = prefill
    # 후보는 현재/옛 스코프 문구 × 현재/옛/부재 계약 문구의 같은 골격들이다 — 어느 문구든
    # 바꾸기 전에 예약된 라운드가 그 변경만으로 "산출 있음" 이 되면 안 된다. 값이 채워진
    # 라운드는 어느 후보와도 같지 않으므로 이 후보 확장이 pending 판정을 느슨하게 하지 않는다.
    candidates = [
        (scope_rule, contract_rule)
        for scope_rule in (review_form.CONFIRM_ROUND_SCOPE_RULE, *review_form.LEGACY_CONFIRM_ROUND_SCOPE_RULES)
        for contract_rule in (
            review_form.PM_REVIEW_CONCRETE_FIX_CONTRACT_RULE,
            *review_form.LEGACY_PM_REVIEW_CONCRETE_FIX_CONTRACT_RULES,
            None,
        )
    ]
    return any(
        normalized == markdown_text._normalized_newlines(
            _render_review_round_seed_body(
                role, confirmation_ids, next_finding_id,
                scope_rule=scope_rule, contract_rule=contract_rule,
            )
        )
        for scope_rule, contract_rule in candidates
    )
