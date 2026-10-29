"""위임 산출 검증과 회수 조정 (층: app)."""
from __future__ import annotations

from typing import NamedTuple
from pathlib import Path
from pathlib import PurePosixPath
from collections.abc import Sequence
import datetime
from pm_engine.infra import delegate_copy_git
from pm_engine.infra import delegate_copy_store
from pm_engine.domain import delegate_errors
from pm_engine.infra import delegate_ledger
from pm_engine.domain import delegate_rounds
from pm_engine.infra import fs
from pm_engine.domain import markdown_text
from pm_engine.infra import pm_paths
from pm_engine.app import regression
from pm_engine.infra import review_command
from pm_engine.domain import review_contract
from pm_engine.app import review_evidence
from pm_engine.domain import review_form
from pm_engine.domain import review_state
from pm_engine.app import round_maintenance
from pm_engine.domain import round_rules
from pm_engine.app import round_seed
from pm_engine.infra import round_store
import stat
import sys
from pm_engine.infra import ticket_store



def harvest_ticket_copy(
    home, *, copy_path: Path, cwd: Path, ) -> delegate_rounds.TicketHarvestResult:
    """슬롯 라운드 파일로 board 라운드 파일을 원자 교체하고 그 티켓의 자리를 닫는다.

    회수 성공 = 그 티켓 자리 삭제 = 그 자리 닫힘(빈 run-dir 은 함께 걷는다 — 크기 1 이면 곧
    run 닫힘). 재회수 개념이 없고, 닫힌 자리에 다시 부르면 파일이 없어 자연 실패한다. 슬롯
    파일이 시드 그대로면 board 를 바꾸지 않고 경고만 낸다 — 자리는 남겨 같은 세션을 이어 시킬
    수 있게 한다(게이트가 아니다).

    "시드 그대로"는 **board 라운드 파일의 현재 bytes**와의 직접 대조로 판정한다(개행 표기만
    정규화). 그 파일이 이 run 의 예약이 쓴 시드 자신이라 판정 입력이 시점에 의존하지 않는다 —
    시드를 다시 렌더해 비교하면 그 사이 같은 역할의 다른 라운드가 회수될 때 프리필 입력이 바뀌어
    손대지 않은 산출이 "산출 있음"으로 뒤집힌다.
    """
    pm_home_root = home.local_dir.parent.parent.resolve()
    cwd = Path(cwd).resolve()
    row = delegate_copy_store._delegate_round_record(home, copy_path)
    # 한 copy 는 회수 또는 포기 중 **하나로만** 종결된다. 포기된 행의 회수를 열어 두면, 그 순번을
    # 다시 쓴 새 라운드를 옛 run 의 회수가 덮어쓴다(잘못된 포기의 유일한 조용한 손상 경로).
    if "abandoned_at" in row:
        raise delegate_errors.DelegateError(f"이미 포기된 준비는 회수할 수 없습니다: {copy_path}")
    # 장부 행이 인가하는 경로와 **전량 일치**해야 한다 — containment·깊이·순번/역할 결속이
    # 이 단언 하나로 닫힌다.
    expected = delegate_copy_store._expected_slot_round_path(cwd, row)
    given = Path(copy_path)
    if not given.is_absolute() or not delegate_copy_store._same_lexical_path(given, expected):
        raise delegate_errors.DelegateError(
            "delegate-rounds 장부가 인가한 라운드 경로와 불일치 — 회수하지 않습니다: "
            f"요청={given} · 장부 인가={expected}"
        )
    delegate_copy_store._assert_slot_run_dir_chain_is_plain(cwd, delegate_ledger._row_ticket_relative_dir(row))
    text = delegate_copy_store._read_slot_round_text(expected)
    run_dir = expected.parent
    board_path = pm_home_root / Path(*PurePosixPath(row["board_rel"]).parts)
    if board_path.name != expected.name:
        raise delegate_errors.DelegateError(
            "delegate-rounds 장부의 board 경로와 라운드 이름 불일치: "
            f"board={board_path.name} · 기대={expected.name}"
        )
    if row["role"] == review_form.INTERNAL_REVIEW_ROLE:
        # 돌려받기 트리 조건 — 시드 대조보다 앞이다. 산출 없음 판정의 조기 반환이 바뀐 슬롯을 가리지 않는다.
        delegate_copy_git._assert_review_tree_unchanged(cwd, row, copy=expected)

    try:
        reserved = fs.read_text_shared(
            board_path, encoding="utf-8", newline="",
        )
    except FileNotFoundError as exc:
        raise delegate_errors.DelegateError(
            f"board 라운드 파일이 없습니다(예약 소실): {board_path}"
        ) from exc
    except (OSError, UnicodeError) as exc:
        raise delegate_errors.DelegateError(
            f"board 라운드 파일 읽기 실패: {board_path}: {exc}"
        ) from exc
    if markdown_text._normalized_newlines(text) == markdown_text._normalized_newlines(reserved):
        print(
            "경고: ticket harvest 산출 없음 — 준비가 시드한 라운드 골격이 그대로입니다: "
            f"ticket={row['ticket']} {board_path.name} · run-dir 을 유지합니다: {run_dir}",
            file=sys.stderr,
        )
        return delegate_rounds.TicketHarvestResult(False, True)
    if row["role"] == "architect":
        try:
            review_contract.parse_architect_tests(text)
        except delegate_errors.DelegateError as exc:
            raise delegate_errors.DelegateError(
                f"architect 라운드 회수 거부 — {exc}. board 라운드 파일과 slot run-dir 은 "
                f"그대로 보존했습니다 — 사본을 고쳐 같은 경로로 다시 회수하세요: "
                f"round={board_path.name} · copy={expected}"
            ) from exc
    if row["role"] in review_form.REVIEW_ROLES:
        # 판정 표면에 올릴 수 없는 리뷰 산출은 board 라운드가 되지 못한다 — 들어가고 나면
        # 티켓 전역 delta 가 막히고 라운드 파일을 되돌릴 정식 수단이 없다(회수면에서 끊는다).
        problem = _review_round_harvest_problem(
            text, ticket=row["ticket"], reviewer_role=row["role"],
            ordinal=int(row["ordinal"]), home=home,
        )
        if problem is not None:
            raise delegate_errors.DelegateError(
                f"리뷰 라운드 회수 거부 — {problem}. board 라운드 파일과 slot run-dir 은 "
                f"그대로 보존했습니다 — 사본을 고쳐 같은 경로로 다시 회수하세요: "
                f"round={board_path.name} · copy={expected}"
            )
    if row["role"] == "developer":
        # 커밋 대상 트리 판정도 같은 자리다 — 이 회수가 만들 티켓 제목 커밋의 트리를 board
        # 교체 앞에서 확정한다.
        delegate_copy_git._assert_developer_commit_tree(cwd)
        # 검증 게이트는 리뷰 게이트와 **같은 자리**(교체 앞)다 — 교체 뒤로 밀면 낡은 기대값이
        # 이미 board 에 착지한 뒤라 되돌릴 정식 수단이 없다(라운드 파일은 회수 후 불변).
        terminal_fix = _terminal_developer_round(
            ticket=row["ticket"], ordinal=int(row["ordinal"]),
            home=home,
        )
        problem = _developer_round_harvest_problem(
            text, reserved, ticket=row["ticket"], ordinal=int(row["ordinal"]),
            home=home, cwd=cwd,
            base_rev=str(row.get("base_rev") or "HEAD"),
        )
        if problem is not None:
            if terminal_fix:
                raise delegate_rounds.TerminalFixHarvestError(
                    f"최종 fix 회수 거부 — {problem}. terminal stop: board 라운드 "
                    f"파일과 slot run-dir 증거를 그대로 보존했습니다. 현재 티켓 "
                    f"상태와 실패 근거를 사용자에게 보고하세요: "
                    f"round={board_path.name} · copy={expected}"
                )
            raise delegate_errors.DelegateError(
                f"developer 라운드 회수 거부 — {problem}. board 라운드 파일과 slot run-dir 은 "
                f"그대로 보존했습니다 — 사본을 고쳐 같은 경로로 다시 회수하세요: "
                f"round={board_path.name} · copy={expected}"
            )
    # promote와 같은 락에서 공개 상태 확인·교체를 마친다. draft 회수는 로컬 산출이며,
    # promote가 명세와 충전된 라운드를 함께 기록한다.
    with fs.board_lock(home):
        found = ticket_store.find_ticket_exact(home, row["ticket"])
        if found is None:
            raise delegate_errors.DelegateError(f"회수할 티켓 명세가 없습니다: {row['ticket']}")
        is_draft = found[0] == "draft"
        round_store.replace_round(board_path, text)
    message = f"ticket-harvest {row['ticket']} {row['role']}"
    # board 부분 커밋 seam 은 직접 부른다 — 이름을 더듬어 찾으면 부분 동기된 사본에서 커밋만
    # 조용히 빠진 rc0(라운드는 바뀌었는데 board git 은 그대로)이 된다. 이름이 갈리면
    # AttributeError 로 loud 하게 죽는 편이 그 침묵보다 낫다.
    sync_ready = True
    if not is_draft:
        sync_ready = bool(round_maintenance._rounds_mutation_sync_paths(home, message, (board_path,)))
    harvested = dict(row)
    harvested["harvested_at"] = datetime.datetime.now(
        datetime.timezone.utc
    ).isoformat()
    delegate_copy_store._append_delegate_rounds_ledger(home, harvested)

    # run-dir 은 엔진 소유 임시 산출물이다(사용자 파일 아님) — 삭제가 곧 run 닫힘이다.
    fs.force_rmtree(run_dir)
    delegate_copy_store._prune_empty_run_dir(run_dir)
    if row["role"] == "developer":
        # 회수가 끝난 **뒤** 커밋한다 — run-dir 이 이미 걷혀 임시 라운드 사본이 커밋에 실리지
        # 않는다. 커밋 문안은 티켓 제목이고, 종결의 커밋 단계는 이 커밋 뒤 "커밋할 변경 없음"
        # 을 관측해 건너뛴다(커밋 자리는 하나다).
        message = delegate_copy_git._commit_developer_round_output(
            cwd, ticket=row["ticket"], home=home,
        )
        if message is not None:
            print(f"슬롯 커밋: {row['ticket']} — {message} ({cwd})")
    return delegate_rounds.TicketHarvestResult(True, sync_ready)


class ClusterHarvestOutcome(NamedTuple):
    """묶음 회수 1건의 결과 — 티켓별로 독립이다(교차 원자성 없음).

    `result` 가 있으면 그 티켓의 회수 판정이 끝난 것이고(`changed=False` 는 시드 그대로라
    board 를 안 바꾼 정상 산출), `refusal` 이 있으면 그 티켓만 거부다 — 다른 티켓의 교체를
    되돌리지 않는다(실패한 자리는 장부 행이 복구 좌표를 소유한다).
    """

    ticket: str
    copy: Path
    result: delegate_rounds.TicketHarvestResult | None
    refusal: str | None
    terminal: bool = False


def harvest_cluster_copy(
    home, *, run_dir: Path, cwd: Path, ) -> tuple[ClusterHarvestOutcome, ...]:
    """run-dir 하나에 깔린 라운드 파일들을 **티켓별로** 회수한다.

    회수는 티켓별 독립이다 — 파일마다 교체·경고·거부를 따로 내고 호출부가 요약 1줄로 집계한다.
    N 파일을 한꺼번에 되돌리는 교차 원자성은 없다(있는 척하면 실패한 자리의 산출을 잃는다).
    각 행은 단일 경로 회수(`harvest_ticket_copy`)를 그대로 통과하므로 판정식이 한 벌뿐이고,
    크기 1 묶음이면 이 함수는 그 한 번의 회수와 같다.
    """
    cwd = Path(cwd).resolve()
    pm_home_root = home.local_dir.parent.parent.resolve()
    outcomes: list[ClusterHarvestOutcome] = []
    for row in delegate_copy_store._cluster_run_rows(home, run_dir):
        expected = delegate_copy_store._expected_slot_round_path(cwd, row)
        try:
            result = harvest_ticket_copy(
                home, copy_path=expected, cwd=cwd, )
        except delegate_errors.DelegateError as exc:
            outcomes.append(
                ClusterHarvestOutcome(
                    row["ticket"], expected, None, str(exc),
                    isinstance(exc, delegate_rounds.TerminalFixHarvestError),
                ))
            continue
        outcomes.append(ClusterHarvestOutcome(row["ticket"], expected, result, None))
    return tuple(outcomes)


def _review_round_harvest_problem(
    text: str, *, ticket: str, reviewer_role: str, ordinal: int, home,
) -> str | None:
    """내부 채널 회수 직전 리뷰 라운드 내용 판정 — 위반 사유 또는 None.

    판정 자체는 두 채널 공용(`review_harvest_problem`)이고 이 함수가 하는 일은 그 판정의 입력
    (명세·라운드 목록)을 PM 홈 board 좌표에서 읽어 오는 것뿐이다. 추가 리뷰어 채널은 같은 입력을
    회수 직전에 읽는다 — 두 채널이 같은 스냅샷 규칙을 쓴다.

    입력에서 **회수 대상 라운드 자신은 뺀다**. 그 자리는 장부 행의 `(역할, 순번)` 좌표가 이미
    가리키고 있다. 새로 예약한 라운드는 시드 그대로라 `pending` 배제에 걸리지만, 재개방
    (`--reopen-ordinal`)한 라운드는 직전 회수 산출로 채워져 있어 `pending` 도 엔진 표식도
    아니다 — 좌표로 빼지 않으면 그 라운드가 앞서 선언한 ID 가 자기 자신과 충돌해 이어 시키는
    회수가 통째로 막힌다. 다른 라운드가 이미 쓴 ID 의 재선언은 종전대로 거부다.

    입력을 읽지 못하면 통과가 아니라 거부다. 판정 불능인 채로 회수하면 그 라운드가 판정 표면에
    올라 티켓 전체를 막을 수 있고, 거부는 산출을 파괴하지 않으므로(run-dir 유지) 되돌릴 수 있다.
    """
    try:
        found = ticket_store.find_ticket_exact(home, ticket)
        if found is None:
            return f"티켓 명세를 찾지 못해 회수 판정을 낼 수 없습니다: {ticket}"
        _status, spec_path = found
        spec_text = fs.read_text_shared(
            spec_path, encoding="utf-8", newline="",
        )
        rounds = round_seed.load_rounds(
            pm_paths.tickets_dir(home), ticket, ticket_text=spec_text,
        )
    except (delegate_errors.DelegateError, round_rules.RoundsError, OSError, UnicodeError) as exc:
        return f"회수 판정 입력을 읽지 못했습니다: {ticket}: {exc}"
    corpus = tuple(
        item for item in rounds
        if not (item.role == reviewer_role and item.ordinal == ordinal)
    )
    return review_contract.review_harvest_problem(
        text, ticket_text=spec_text, rounds=corpus, reviewer_role=reviewer_role,
    )


def _full_regression_command(cwd: Path) -> str:
    """developer 단계 종료의 프로젝트 test_cmd(areas/slot/local/default 해소 전부)."""
    repo = Path(cwd).resolve()
    config_root = Path(pm_paths.resolve_pm_home_for_repo(repo)).resolve()
    command = str(regression._test_cmd(pm_paths.home_at(config_root), None, session=None)).strip()
    if not command:
        raise delegate_errors.DelegateError("developer 단계 종료 전체 회귀 test_cmd를 해소하지 못했습니다")
    return command


def _fix_round(rounds: Sequence, ordinal: int) -> bool:
    """현재 developer 자리가 reviewer 뒤의 마지막 fix 자리인가."""
    return any(
        item.role == review_form.INTERNAL_REVIEW_ROLE and item.ordinal < ordinal
        and not getattr(item, "pending", False)
        for item in rounds
    )


def _terminal_developer_round(
    *, ticket: str, ordinal: int, home,
) -> bool:
    """회수 거부 문구가 사람을 다시 투입해도 되는지 판정하는 단일 seam."""
    try:
        found = ticket_store.find_ticket_exact(home, ticket)
        if found is None:
            return False
        _status, spec_path = found
        spec_text = fs.read_text_shared(
            spec_path, encoding="utf-8", newline="",
        )
        rounds = round_seed.load_rounds(
            pm_paths.tickets_dir(home), ticket, ticket_text=spec_text,
        )
    except (round_rules.RoundsError, OSError, UnicodeError):
        return False
    return _fix_round(rounds, ordinal)


def _developer_round_harvest_problem(
    text: str, reserved: str, *, ticket: str, ordinal: int, home,
    cwd: Path, base_rev: str = "HEAD",
) -> str | None:
    """developer 라운드 회수 직전 verify 판정 — 위반 사유 또는 None(리뷰 게이트와 같은 자리).

    세 가지를 본다.

    (1) **지운 행**: 시드가 실은 verify 행이 산출에 그대로 있는가. 기준선은 판정 시점에 다시
    계산한 분류기 산출이 아니라 **그 run 의 예약 bytes** 다 — 그 사이 같은 티켓의 다른 라운드가
    회수되면 프리필 입력이 바뀌어, 손대지 않은 행이 "지워졌다"로 뒤집힌다(무편집 판정과 같은 규율).
    지운 행을 통과시키면 시드가 열린 accepted 전건을 싣는 의미가 사라진다 — 개발자가 이번 라운드에
    보지 않은 finding 을 산출에서 빼는 것으로 범위를 스스로 좁히게 된다.

    (2) **선언 검증**: 이번 라운드가 선언한 `machine_verifiable=true` 행의 커맨드를 실제로 돌려
    기대값이 관측되는가. 실행 대상은 이번 라운드의 선언뿐이라(티켓 전역 최신 행이 아니다) 실행
    시간이 티켓 수명에 비례하지 않는다. 설계 축 finding 은 기계 확인 대상이 아니므로 여기서도
    돌리지 않는다(확인 파서와 같은 규칙). 관측이 기대와 다르면 거부다 — 낡은 기대값을 board
    라운드에 착지시키면 PM 의 기계 확인이 그 값으로 막히고, 라운드 파일은 회수 뒤 불변이라
    되돌릴 정식 수단이 없다.

    (3) **stage-exit 전체 회귀 기록**: developer가 local.conf `test.cmd`를 직접 실행한 정확한
    커맨드와 ``rc=0`` 결과가 `## 회귀` 절에 있는가. harvest가 full을 다시 실행하면 inner-loop
    탐색에 전체 회귀를 반복하는 경로가 되므로 최초 developer와 final fix 모두 기록을 엄격 검증하고
    이 경계에서는 targeted architect/reviewer 계약만 재실행한다. final fix developer가 terminal
    전체 회귀를 직접 실행하며, 기록 누락·명령 불일치·nonzero 또는 계약 red는 terminal stop이다.

    이 관측은 **확인 증거가 아니다**. 회수는 슬롯 트리(개발자가 고친 브랜치)에서 재고, PM 의 기계
    확인은 묶음 통합 브랜치를 강제한다 — 측정 트리가 다르므로 두 관측을 섞지 않는다.

    입력을 읽지 못하면 통과가 아니라 거부다(리뷰 게이트와 같은 이유 · run-dir 보존이라 되돌릴 수
    있다).
    """
    try:
        found = ticket_store.find_ticket_exact(home, ticket)
        if found is None:
            return f"티켓 명세를 찾지 못해 회수 판정을 낼 수 없습니다: {ticket}"
        _status, spec_path = found
        spec_text = fs.read_text_shared(
            spec_path, encoding="utf-8", newline="",
        )
        rounds = round_seed.load_rounds(
            pm_paths.tickets_dir(home), ticket, ticket_text=spec_text,
        )
    except (round_rules.RoundsError, OSError, UnicodeError) as exc:
        return f"회수 판정 입력을 읽지 못했습니다: {ticket}: {exc}"

    repo_root = pm_paths._repo_root_for_cwd(cwd)
    try:
        architect_tests = review_evidence.architect_tests_from_rounds(rounds)
    except delegate_errors.DelegateError as exc:
        return str(exc)
    is_fix = _fix_round(rounds, ordinal)
    try:
        changed_paths = delegate_copy_git._developer_round_changed_paths(repo_root, base_rev=base_rev)
    except delegate_errors.DelegateError as exc:
        return f"developer 단계 diff를 읽지 못했습니다: {exc}"

    # accepted reviewer 계약과 결속하기 전에 **이번 산출의 선언**을 먼저 확정한다. PM-owned
    # 예외는 disposition 산문만으로 열리지 않고 같은 ID의 채워진 false verify 행까지 요구한다.
    seed_rows = round_seed._dev_round_seed_verify_rows(markdown_text._normalized_newlines(reserved))
    if seed_rows is None:
        return (
            f"예약 골격의 {review_form.PM_REVIEW_VERIFY_BLOCK} 블록을 읽지 못해 회수 판정을 낼 수 "
            f"없습니다: ordinal={ordinal}"
        )
    filled: dict[str, review_form.PMReviewVerifyRow] = {}
    unfilled: tuple[str, ...] = ()
    if seed_rows:
        try:
            filled, unfilled = review_state._pm_review_verify_round_declarations(
                review_state._RoundView("developer", ordinal, text),
            )
        except review_form.PMReviewError as exc:
            return f"{review_form.PM_REVIEW_VERIFY_BLOCK} 블록을 읽지 못했습니다[{exc.code}]: {exc}"
        declared = set(filled) | set(unfilled)
        deleted = [
            finding_id for finding_id, _values in seed_rows
            if finding_id not in declared
        ]
        if deleted:
            return (
                f"시드가 실은 verify 행을 지웠습니다: {', '.join(deleted)} — 행은 지우지 말고 값만 "
                "갱신하세요(이번 라운드에 구현하지 않은 finding 은 machine_verifiable=false · "
                f"reason={review_form.PM_REVIEW_VERIFY_GAP_REASON} 로 선언합니다)"
            )
    if not is_fix:
        for case in architect_tests:
            try:
                targets = review_contract._contract_test_targets(
                    case.target, f"architect 필수 테스트 {case.id}.target",
                )
            except delegate_errors.DelegateError as exc:
                return str(exc)
            missing = [target for target in targets if target not in changed_paths]
            if missing:
                return (
                    f"architect 필수 테스트 {case.id} 대상이 이 developer diff에 "
                    f"추가·수정되지 않았습니다: {', '.join(missing)}"
                )
    for case in architect_tests:
        problem = review_command._run_required_test(case.command, case.expected, cwd=repo_root)
        if problem is not None:
            return f"architect 필수 테스트 {case.id} {problem}"

    delta: review_form.PMReviewDelta | None = None
    if is_fix:
        try:
            delta = review_state.parse_pm_review_delta(spec_text, rounds)
        except review_form.PMReviewError as exc:
            return f"fix 입력 리뷰 delta를 읽지 못했습니다[{exc.code}]: {exc}"
        try:
            pm_owned_ids = review_form._pm_review_pm_owned_contract_ids(delta, filled)
        except delegate_errors.DelegateError as exc:
            return str(exc)
        for finding, _disposition in delta.accepted:
            if finding.fix_contract is None:
                return (
                    f"fix 입력 {finding.id}에 reviewer 수정·테스트 계약이 없습니다 — "
                    "코드 위치·오류 거동·수정 설계·회귀 테스트·명령·기대값이 모두 필요합니다"
                )
            if finding.id in pm_owned_ids:
                continue
            try:
                test_kind, targets = review_contract._reviewer_fix_test_requirement(
                    finding.fix_contract["test"],
                    f"reviewer 추가 회귀 {finding.id}.test",
                )
            except delegate_errors.DelegateError as exc:
                return str(exc)
            # 시험 변경 계약은 시험 파일의 변경을 요구한다. 기존 시험 재실행과 시험
            # 대상이 없는 지적은 location 이 적은 파일 중 하나가 이번 diff에 있어야 한다.
            if test_kind == "changed":
                missing = [target for target in targets if target not in changed_paths]
                if missing:
                    return (
                        f"reviewer 추가 회귀 {finding.id} 대상이 이 fix diff에 "
                        f"추가·수정되지 않았습니다: {', '.join(missing)}"
                    )
            elif not review_contract._contract_changed_paths(
                finding.fix_contract["location"], changed_paths,
            ):
                return (
                    f"reviewer 수정 계약 {finding.id}의 location 에 적힌 파일 중 "
                    f"이 fix diff에 바뀐 것이 없습니다: {finding.fix_contract['location']}"
                )
            if test_kind == "existing":
                for target in targets:
                    try:
                        observed = (repo_root / target).stat()
                    except FileNotFoundError:
                        return f"reviewer 기존 시험 {finding.id} 파일이 없습니다: {target}"
                    if not stat.S_ISREG(observed.st_mode):
                        return f"reviewer 기존 시험 {finding.id} 정규 파일이 아닙니다: {target}"
            problem = review_command._run_required_test(
                finding.fix_contract["command"], finding.fix_contract["expected"],
                cwd=repo_root,
            )
            if problem is not None:
                return f"reviewer 추가 회귀 {finding.id} {problem}"
    full_command = _full_regression_command(repo_root)
    try:
        regression = delegate_rounds.parse_developer_regression_record(text)
    except delegate_errors.DelegateError as exc:
        return str(exc)
    if regression.command != full_command:
        return (
            "developer 전체 회귀 커맨드가 stage-exit 명령과 다릅니다: "
            f"기록={regression.command!r} · 기대={full_command!r}"
        )
    if not seed_rows:
        return None
    declared_rows = [row for row in filled.values() if row.machine_verifiable]
    if not declared_rows:
        return None
    try:
        delta = delta or review_state.parse_pm_review_delta(spec_text, rounds)
    except (
        delegate_errors.DelegateError, review_form.PMReviewError, round_rules.RoundsError, OSError, UnicodeError,
    ) as exc:
        return f"회수 판정 입력을 읽지 못했습니다: {ticket}: {exc}"
    machine_axis_ids = {
        finding.id for finding, _disposition in delta.accepted
    }
    for row in declared_rows:
        if row.id not in machine_axis_ids:
            continue
        try:
            status, observed = review_command.run_pm_review_confirmation_command(
                row.command, cwd=repo_root, expected=row.expected,
            )
        except (review_form.PMReviewError, ValueError) as exc:
            return f"verify {row.id} 재현 커맨드를 실행하지 못했습니다: {exc}"
        if status != "resolved":
            return (
                f"verify {row.id} 재현 커맨드의 관측이 기대와 다릅니다: `{row.command}` "
                f"(cwd={repo_root}) · expected={row.expected!r} · 관측: {observed}"
            )
    return None
