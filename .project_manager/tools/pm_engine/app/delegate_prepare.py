"""위임 사본 준비와 포기 조정 (층: app)."""
from __future__ import annotations

from typing import Any
from pathlib import Path
from pathlib import PurePosixPath
from collections.abc import Sequence
from pm_engine.infra import board_git
from pm_engine.infra import cluster_store
import contextlib
import datetime
from pm_engine.infra import delegate_copy_git
from pm_engine.infra import delegate_copy_store
from pm_engine.domain import delegate_errors
from pm_engine.infra import delegate_ledger
from pm_engine.domain import delegate_rounds
from pm_engine.infra import fs
from pm_engine.domain import markdown_text
import os
from pm_engine.infra import pm_paths
from pm_engine.domain import review_contract
from pm_engine.app import review_evidence
from pm_engine.domain import review_form
from pm_engine.domain import review_state
from pm_engine.app import round_maintenance
from pm_engine.domain import round_rules
from pm_engine.app import round_seed
from pm_engine.infra import round_store
import sys
from pm_engine.infra import ticket_store
import uuid



def _ticket_round_seed(
    role: str, ticket_text: str, previous_round, *,
    today: str, rounds: Sequence = (),
) -> str:
    """이 라운드 파일의 시드 bytes — 렌더 규약은 공용 seam 이 소유한다."""
    return round_seed.render_round_seed(
        role, ticket_text, today=today, previous_round=previous_round, rounds=rounds,
    )


def prepare_ticket_copy(
    home, *, ticket: str, role: str, cwd: Path, owner_pid: int | None = None,
    reopen_ordinal: int | None = None,
) -> delegate_rounds.TicketCopyPlan:
    """티켓 하나의 라운드를 준비한다 — **크기 1 묶음 호출**의 얇은 래퍼.

    티켓당 경로를 따로 두지 않는다(특례 없음). 이 티켓이 속한 묶음은 명세 frontmatter 가
    말한다 — 필드가 없으면 그 티켓 이름으로 읽히고, 그 이름의 장부가 없으면 준비가 멈춘다
    (장부는 open 진입 세 경로가 만든다 · 부재는 판정 입력이 아니라 정지 사유다).
    반환은 종전대로 라운드 파일 하나의 좌표다.
    """
    found = ticket_store.find_ticket_exact(home, ticket)
    if found is None:
        raise delegate_errors.DelegateError(f"ticket not found: {ticket}")
    _status, source = found
    try:
        spec_text = fs.read_bytes_shared(source).decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise delegate_errors.DelegateError(f"PM 홈 티켓 읽기 실패: {source}: {exc}") from exc
    cluster = cluster_store.ticket_cluster_from_text(ticket, spec_text)
    plan = prepare_cluster_copy(
        home, cluster=cluster, tickets=(ticket,), role=role, cwd=cwd, owner_pid=owner_pid, reopen_ordinal=reopen_ordinal,
    )
    return plan.rounds[0]


def prepare_cluster_copy(
    home, *, cluster: str, tickets: Sequence[str] | None = None, role: str, cwd: Path,
    owner_pid: int | None = None, reopen_ordinal: int | None = None,
) -> delegate_rounds.ClusterCopyPlan:
    """board 에 티켓별 라운드 순번을 시드로 예약하고, 같은 파일들을 run-dir 하나에 깐다.

    **run 이 단위다** — 묶음의 티켓 N 개가 run-dir 하나를 공유하고, 그 안에서 티켓마다
    `<티켓>/{NN-<역할>.md, spec.md, rounds/}` 를 갖는다. 쓸 수 있는 자리는 run-dir 전체이고
    (하네스가 여는 범위가 원래 run-dir 통째다) 티켓별 명세·이전 라운드는 읽기 전용 입력이다.
    크기 1 묶음이면 티켓 하나짜리 run-dir 이라 종전 준비와 동작이 같다(별도 코드 경로 없음).

    **거부 판정은 전부 예약 앞에 둔다** — 역할·티켓 조회·상태·ignore 규칙·run-dir·시드 렌더
    (설계 근거 게이트 포함)가 **N 티켓 전부** board 를 건드리기 전에 끝난다.
    뒤에 두면 거부된 준비가 board 에 회수 불가능한 고아 라운드를 남기고 순번을 영구 소모한다.

    순서는 여섯이다. (1) 역할·멤버 해소·티켓별 상태 판정 (2) ignore 검증과
    run-dir 확보(슬롯 전용 부작용) (3) 티켓별 시드 렌더 (4) **상태 재확인 + 역할 count 재확인 +
    채번 + O_EXCL 예약을 하나의 board_lock 임계구역에서 최신 스냅샷으로 수행** (5) 슬롯 파일
    laydown + PM 홈 장부 기록(회수의 신뢰 뿌리 · 티켓당 한 행) (6) 공개된 신규 시드의
    경로 한정 보드 기록. (6)은 모든 사본과 장부가 완성된 뒤 수행한다.

    명세 스냅샷(1)과 최종 예약(4)은 board_lock 안에서 처리한다. 재확인·채번·생성이 같은
    구간에서 끝나므로 두 준비의 동일 순번 예약을 막는다. (6)의 기록 seam은 락 밖에서 부른다. 순번은
    **티켓별**이다 — 묶음 하나의 라운드가 티켓마다 다른 번호를 받을 수 있다.

    `owner_pid` 는 **호출자가 이 run 의 소유자일 때만** 준다 — 준비·실행·회수가 한 프로세스의
    try/finally 인 cross 위임이 그 경우다. 그 값이 장부에 실리면 포기(abandon) 판정이 "이 run 이
    아직 살아 있는가"를 기계 관측할 수 있다. 준비 프로세스가 곧바로 끝나고 실제 writer 가 다른
    세션인 native 위임은 이 값을 **주지 않는다**: 즉사한 준비 pid 를 표식으로 실으면 살아 있는
    run 이 죽은 것으로 읽힌다. 키 부재는 "죽음"이 아니라 "증거 없음"이다.

    묶음 고정 예산은 **모든 준비 표면**에서 같은 판정을 받는다 — 표면마다 켜고 끄는 인자가
    없으므로 티켓 하나짜리 준비가 단계 게이트를 비켜 가는 길도 없다. 판정은 사전판정과
    board_lock 재확인 **두 지점**에 함께 건다 — 한쪽에만 걸면 동시 준비 둘이 같은 예산을
    함께 통과한다.
    장부가 없거나 예산을 선언하지 않았으면 그 요청은 멈춘다(무제한·순서 무검사로 접는 갈래가
    없다).

    `reopen_ordinal` 은 **이어 시키는 단계**의 명시 요청이다 — 그 순번을 채번하지 않고 다시 열어
    (`reopen_round`) 슬롯 사본을 **board 라운드 파일의 현재 내용**으로 시드한다(시드 골격이
    아니다). 재개방은 라운드를 만들지 않으므로 수를 세는 판정(묶음 고정 예산 수열)의 대상이
    아니고, 대신 "그 순번이 있고 역할이 같다"를 예약 seam 이 판정한다.
    요청이 없으면 종전대로 새 순번을 채번한다(암묵 재개방 없음).
    """
    if role not in delegate_ledger.TICKET_COPY_ROLES - {review_form.ADDITIONAL_REVIEWER_ROLE, "researcher"}:
        raise delegate_errors.DelegateError(f"티켓 라운드 준비 미지원 역할: {role}")
    pm_home_root = home.local_dir.parent.parent.resolve()
    cwd = Path(cwd).resolve()
    # 슬롯 HEAD 를 장부 행에 남기는 역할 — developer(변경 경로 판정의 기준)와 code-reviewer
    # (돌려받기 트리 조건의 기준 · 준비 뒤 HEAD 가 바뀌면 거부한다).
    base_rev = (
        delegate_copy_git._cluster_git(cwd, "rev-parse", "HEAD").strip()
        if role in delegate_rounds._BASE_REV_ROLES else ""
    )
    cluster = str(cluster or "").strip()
    if not delegate_ledger._is_valid_cluster_id(cluster):
        raise delegate_errors.DelegateError(f"클러스터 id 형식이 아닙니다: {cluster!r}")
    members = tuple(tickets) if tickets else cluster_store.cluster_live_members(home, cluster)
    if not members:
        raise delegate_errors.DelegateError(
            f"클러스터 멤버가 없습니다: {cluster} — `board.py cluster show {cluster}` 로 "
            "장부를 확인하세요"
        )

    # ── (1) 티켓별 상태 판정 + 명세 판독 (board 무영향) ─────────────────────
    specs: dict[str, str] = {}
    with fs.board_lock(home):
        for ticket in members:
            found = ticket_store.find_ticket_exact(home, ticket)
            if found is None:
                raise delegate_errors.DelegateError(f"ticket not found: {ticket}")
            status, source = found
            if status not in delegate_rounds._TICKET_PREPARE_STATUSES and not (
                status == "draft" and role == "architect"
            ):
                raise delegate_errors.DelegateError(
                    "티켓 라운드 준비는 open/claimed 또는 draft×architect만 허용: "
                    f"{ticket} role={role} currently in {status}/"
                )
            try:
                specs[ticket] = fs.read_bytes_shared(source).decode("utf-8")
            except (OSError, UnicodeError) as exc:
                raise delegate_errors.DelegateError(f"PM 홈 티켓 읽기 실패: {source}: {exc}") from exc

    tickets_dir = pm_paths.tickets_dir(home)
    existing_rounds = {
        ticket: round_seed.load_rounds(tickets_dir, ticket, ticket_text=text)
        for ticket, text in specs.items()
    }
    # 묶음 예산 입력 — 장부가 선언한 고정 수열.
    def _budget_inputs() -> tuple[str, ...]:
        """장부에서 고정 수열을 **읽는 자리에서** 해소한다(캐시 없음).

        사전판정과 board_lock 재확인이 각자 읽는다. 장부 판독은 잠금 없는 파일 읽기라
        board_lock 안에서 불러도 재진입이 아니다.

        장부가 없으면 예약하지 않고 멈춘다 — 판정 입력이 없는 요청을 통과시키면 그 묶음만
        무제한·순서 무검사가 되고, 장부 삭제가 곧 예산 우회 수단이 된다.
        """
        ledger = cluster_store.load_cluster(home, cluster)
        if ledger is None:
            raise delegate_errors.DelegateError(delegate_rounds._CLUSTER_LEDGER_ABSENT.format(cluster=cluster))
        return delegate_rounds.cluster_round_sequence(ledger.get("budget"), cluster=cluster)

    budget_sequence: tuple[str, ...] = ()
    budget_sequence = _budget_inputs()

    # ── (1.4) 묶음 예산 사전판정 — 여기도 신뢰 뿌리가 아니다. 빈 수열은 판정을
    # 건너뛰는 값이 아니라 모든 요청을 거부하는 값이다.
    for ticket in members:
        if reopen_ordinal is not None:
            # 재개방은 라운드를 만들지 않는다 — 수열은 예약 수를 세는 판정이라 대상이
            # 아니다(불변식 4). 대신 대상 실재·역할 일치를 여기서 미리 본다: 신뢰 뿌리는 아래
            # (4) 의 board_lock 안 같은 호출이고, 이 자리는 run-dir 을 만들기 전에 흔한 실패를
            # 끝내는 자리다.
            delegate_copy_store._reopen_round_path(
                tickets_dir, ticket, role,
                ordinal=reopen_ordinal, lock=fs.board_lock(home),
            )
            continue
        refusal = delegate_rounds._cluster_budget_refusal(
            cluster=cluster, ticket=ticket, role=role,
            existing=existing_rounds[ticket], sequence=budget_sequence,
        )
        if refusal is not None:
            raise delegate_errors.ClusterRoundBudgetExceeded(refusal)

    # developer 준비의 입력 계약은 실제 prepare 경계에서 검증한다. 순수 시드 renderer는
    # 리뷰/verify 골격 단위 테스트에서도 쓰이므로 architect 라운드에 결합하지 않는다. 고정
    # 수열 판정을 먼저 낸 뒤, 아직 run-dir/board 예약 전인 여기서 누락·미완성
    # architect 계약을 잔여 없이 거부한다.
    if role == "developer":
        for ticket in members:
            try:
                review_evidence.architect_tests_from_rounds(existing_rounds[ticket])
            except delegate_errors.DelegateError as exc:
                raise delegate_errors.DelegateError(f"{ticket}: {exc}") from exc

    # ── (2) 예약 전 거부 판정 (board 무영향) ──────────────────────────────
    run_id = uuid.uuid4().hex
    delegate_copy_store._assert_ticket_copy_root_ignored(
        cwd, cluster=cluster, run_id=run_id, ticket=members[0],
    )
    run_dir, run_dir_fd = delegate_copy_store._secure_cluster_run_dir(cwd, cluster, run_id)
    ticket_dirs: dict[str, tuple[Path, int | None, Path, int | None]] = {}
    seeds: dict[str, str] = {}
    next_finding_ids: dict[str, str] = {}
    # 예약 성공분의 복구 좌표 — 티켓마다 `ticket·board_path·ordinal·board_rel`. 예약 직후
    # fallback 값으로 먼저 채우고 더 나은 값으로 교체한다(아래 (4) 주석).
    reserved: list[dict[str, Any]] = []
    plans: list[delegate_rounds.TicketCopyPlan] = []
    seed_sync_paths: list[Path] = []
    # 예약(reserve_round) 성공 전에 이 블록을 벗어나면(동시 prepare 경합 패자 포함) 이 run_dir
    # 은 이 호출 하나만의 소유라 안전하게 지운다. 성공 후 실패(장부 append 등)는 board 라운드가
    # 이미 살아 있어 `_reserved_round_residue` 진단으로 수동 복구 좌표를 남긴다(지우지 않는다).
    try:
        try:
            for ticket in members:
                ticket_dir, ticket_fd = delegate_copy_store._secure_ticket_copy_dir(
                    cwd, cluster, run_id, ticket,
                )
                rounds_dir, rounds_fd = delegate_copy_store._secure_ticket_copy_dir(
                    cwd, cluster, run_id, ticket,
                    extra_parts=(delegate_rounds.TICKET_COPY_ROUNDS_DIRNAME,),
                )
                ticket_dirs[ticket] = (ticket_dir, ticket_fd, rounds_dir, rounds_fd)

            # ── (3) 티켓별 시드 렌더 — 설계 근거 게이트가 여기서 거부한다(예약 전·잔여 0).
            for ticket in members:
                try:
                    # 재개방의 시드는 골격이 아니라 board 라운드 파일의 현재 내용이라 여기서
                    # 렌더하지 않는다 — 최신 bytes 를 (4) 의 board_lock 안에서 읽어 채운다.
                    if reopen_ordinal is None:
                        seeds[ticket] = _ticket_round_seed(
                            role, specs[ticket],
                            # 프리필 공급원 규칙(같은 역할 직전 라운드 · 산출 없는 라운드
                            # 배제)은 사이드카 seam 하나가 소유한다 — 여기서 다시 구현하면
                            # 예약측·판정측 규칙이 갈린다.
                            round_rules.previous_round_of_role(
                                existing_rounds[ticket], role,
                            ),
                            today=datetime.date.today().isoformat(),
                            # developer 골격의 verify 프리필 입력 — 이미 손에 든 라운드를
                            # 그대로 넘긴다(신규 로드 0).
                            rounds=existing_rounds[ticket],
                        )
                    # 사본 프리앰블이 실을 값 — 시드가 방금 쓴 것과 같은 함수·같은 입력이다
                    # (리뷰 채널이 아닌 역할은 실을 값 자체가 없다).
                    next_finding_ids[ticket] = (
                        review_state.next_review_finding_id(
                            specs[ticket], role, existing_rounds[ticket],
                        )
                        if role in review_form.REVIEW_ROLES else ""
                    )
                except round_rules.RoundsError as exc:
                    # 시드 seam 은 자기 오류형으로 거부한다(board `section-add` 가 잡는 형).
                    # 여기서 되받아 이 CLI 의 오류형으로 옮긴다 — 두 진입점이 같은 규칙·같은
                    # rc 를 낸다. 시드 seam 은 실 ticket id 를 모르므로(ticket_text 만 받는다)
                    # `<T-NNNN>` placeholder 로 처방을 낸다(리뷰 결정) — 이 층은 실값을 쥐고
                    # 있으므로 여기서만 치환해 커맨드가 그대로 실행 가능하게 한다.
                    raise delegate_errors.DelegateError(str(exc).replace("<T-NNNN>", ticket)) from exc

            # ── (4) 여기부터 board 를 건드린다: 상태 재확인·역할 count 재확인·최종 reserve 를
            # 하나의 board_lock 임계구역에서 최신 스냅샷으로 재수행한다 — 위 사전판정과 이
            # 지점 사이의 gap 이 TOCTOU 였다(두 준비가 사전판정을 함께 통과해도 여기서
            # 재확인하는 최신 스캔이 그중 하나만 통과시킨다). 재확인은 **N 티켓 전부**를 먼저
            # 통과시키고 그다음에야 첫 예약을 쓴다(부분 예약 금지).
            board_paths: dict[str, Path] = {}
            with fs.board_lock(home):
                # 재확인은 최신 스냅샷으로 낸다(스냅샷 1회 · 멤버 전부가 같은 장부 판독을 공유한다).
                # 그사이 장부가 사라졌으면 같은 판독이 그 자리에서 멈춘다(예약 없음).
                budget_sequence = _budget_inputs()
                formal_tickets: set[str] = set()
                for ticket in members:
                    refreshed = ticket_store.find_ticket_exact(home, ticket)
                    if refreshed is None:
                        raise delegate_errors.DelegateError(f"ticket not found: {ticket}")
                    fresh_status, _fresh_source = refreshed
                    if fresh_status not in delegate_rounds._TICKET_PREPARE_STATUSES and not (
                        fresh_status == "draft" and role == "architect"
                    ):
                        raise delegate_errors.DelegateError(
                            "티켓 라운드 준비는 open/claimed 또는 draft×architect만 허용: "
                            f"{ticket} role={role} currently in {fresh_status}/"
                        )
                    if fresh_status != "draft":
                        formal_tickets.add(ticket)
                    if reopen_ordinal is not None:
                        # 재개방은 순번을 늘리지 않아 수열이 볼 것이 없다 — 이 티켓의
                        # 재확인은 아래 예약 루프의 `reopen_round` 가 최신 스캔으로 낸다.
                        continue
                    fresh_existing = round_seed.load_rounds(
                        tickets_dir, ticket, ticket_text=specs[ticket],
                    )
                    refusal = delegate_rounds._cluster_budget_refusal(
                        cluster=cluster, ticket=ticket, role=role,
                        existing=fresh_existing, sequence=budget_sequence,
                    )
                    if refusal is not None:
                        raise delegate_errors.ClusterRoundBudgetExceeded(refusal)
                for ticket in members:
                    # 이 함수가 이미 board_lock 을 쥐고 있다(재진입 금지) — 두 seam 자신의
                    # `with lock:` 은 null 컨텍스트로 채워 판정+생성이 같은 임계구역 안에서 돈다.
                    if reopen_ordinal is None:
                        board_path = round_store.reserve_round(
                            tickets_dir, ticket, role, content=seeds[ticket],
                            lock=contextlib.nullcontext(),
                        )
                    else:
                        board_path = delegate_copy_store._reopen_round_path(
                            tickets_dir, ticket, role,
                            ordinal=reopen_ordinal, lock=contextlib.nullcontext(),
                        )
                        # 슬롯 시드 = 이 임계구역에서 읽은 board bytes. 회수의 "산출 없음"
                        # 판정이 슬롯과 board 를 직접 대조하므로, 낡은 판독을 깔면 손대지 않은
                        # 사본이 산출로 읽혀 옛 내용을 board 에 다시 쓴다.
                        seeds[ticket] = delegate_copy_store._read_board_round_text(board_path)
                    board_paths[ticket] = board_path
                    # 반환 즉시, **락 이탈 전** 같은 락 본문 안에서 예약 성공 상태와 복구 좌표를
                    # 기록한다. `exclusive_file_lock` 은 unlock/close 실패를 올리는 계약이라,
                    # 이 기록이 `with` 밖에 있으면 락 이탈 실패가 board 만 남기고 run-dir·좌표를
                    # 지우는 복구 불능 형상을 만든다. 아래 두 계산은 각각 실패 가능한 seam 이라
                    # (`parse_round_filename` 은 None 반환 · `_board_relative_path` 는 PM 홈 밖
                    # 해소를 계약형 오류로 던진다) **던지지 않는 fallback 좌표를 먼저 확정한 뒤**
                    # 더 나은 값으로 교체를 시도한다 — 그래야 복구 진단이 미초기화 값을 참조해
                    # 원 예외를 덮지 않는다.
                    record: dict[str, Any] = {
                        "ticket": ticket, "board_path": board_path,
                        "ordinal": 0, "board_rel": str(board_path),
                    }
                    reserved.append(record)
                    parsed_round = round_rules.parse_round_filename(board_path.name)
                    if parsed_round is not None:
                        record["ordinal"] = parsed_round[0]
                    record["board_rel"] = delegate_copy_store._board_relative_path(home, board_path, )
                    if reopen_ordinal is None and ticket in formal_tickets:
                        seed_sync_paths.append(board_path)

            # ── (5) 슬롯 파일 laydown ─────────────────────────────────────
            for record in reserved:
                ticket = record["ticket"]
                board_rel = record["board_rel"]
                ordinal = record["ordinal"]
                ticket_dir, ticket_fd, rounds_dir, rounds_fd = ticket_dirs[ticket]
                board_path = record["board_path"]
                copy_path = ticket_dir / board_path.name
                try:
                    delegate_copy_store._write_exclusive_file(
                        copy_path, seeds[ticket].encode("utf-8"), 0o600,
                        parent_fd=ticket_fd,
                    )
                    delegate_copy_store._write_exclusive_file(
                        ticket_dir / delegate_rounds.TICKET_COPY_SPEC_NAME,
                        specs[ticket].encode("utf-8"),
                        0o400,
                        parent_fd=ticket_fd,
                    )
                except OSError as exc:
                    raise delegate_errors.DelegateError(
                        f"티켓 라운드 사본 생성 실패: {ticket_dir}: {exc}"
                        + delegate_copy_store._reserved_round_residue(board_rel, ordinal)
                    ) from exc
                # 이전 라운드는 읽기 전용 입력이다 — 시드 그대로인 미회수 라운드도 함께 깐다
                # (진행 중 다른 역할의 자리를 숨기지 않는다). 재개방한 순번은 **쓰기 사본**이
                # 그 자리라 여기서 빠진다 — 같은 파일을 읽기 전용으로 한 벌 더 깔면 어느 쪽에
                # 쓰는지가 모호해진다.
                try:
                    for item in existing_rounds[ticket]:
                        if item.path.name == board_path.name:
                            continue
                        delegate_copy_store._write_exclusive_file(
                            rounds_dir / item.path.name,
                            item.text.encode("utf-8"),
                            0o400,
                            parent_fd=rounds_fd,
                        )
                except OSError as exc:
                    raise delegate_errors.DelegateError(
                        f"이전 라운드 사본 생성 실패: {rounds_dir}: {exc}"
                        + delegate_copy_store._reserved_round_residue(board_rel, ordinal)
                    ) from exc
                plans.append(delegate_rounds.TicketCopyPlan(
                    copy_path.resolve(), ticket_dir, ticket, role, ordinal, board_path,
                    run_id, next_finding_ids[ticket], cluster, run_dir,
                ))
        finally:
            for _ticket_dir, ticket_fd, _rounds_dir, rounds_fd in ticket_dirs.values():
                if ticket_fd is not None:
                    os.close(ticket_fd)
                if rounds_fd is not None:
                    os.close(rounds_fd)
            if run_dir_fd is not None:
                os.close(run_dir_fd)
    except BaseException as exc:
        # 정리(force_rmtree)가 `KeyboardInterrupt`·`SystemExit`·비-OSError 로 죽어도 이미
        # pending 인 **원** 예외(`exc`)를 대체하지 않는다: 아래는 어떤 예외형이든 잡아 loud
        # 경고만 남기고 마지막 `raise`(bare)는 항상 `exc` 를 그대로 재전파한다.
        if not reserved and os.path.lexists(run_dir):
            try:
                fs.force_rmtree(run_dir)
            except BaseException as cleanup_exc:
                cause = f"{type(cleanup_exc).__name__}: {cleanup_exc}"
                print(
                    "경고: 라운드 준비 실패(예약 전) 후 run-dir 정리 실패 — 잔류할 수 "
                    f"있습니다: {run_dir}: {cause}",
                    file=sys.stderr,
                )
        elif reserved:
            # board 예약은 이미 성공했지만(예: 락 이탈 실패) 이후 단계가 실패했다. run_dir 은
            # 지우지 않는다(위 분기가 스킵) — 다음 조작자가 board 좌표로 찾을 수 있게 예약된
            # 라운드 전부의 복구 좌표를 loud 하게 남긴다(복구 불능 — board 만 남고 run-dir·
            # 좌표 모두 없음 — 을 만들지 않는다).
            residue = " · ".join(
                f"{record['ticket']}"
                f"{delegate_copy_store._reserved_round_residue(record['board_rel'], record['ordinal'])}"
                for record in reserved
            )
            print(
                f"경고: board 라운드 예약 후 준비가 실패했습니다({type(exc).__name__}: {exc})"
                f" · run-dir={run_dir} · {residue}",
                file=sys.stderr,
            )
        raise

    for plan, record in zip(plans, reserved):
        ledger_row = {
            "ticket": plan.ticket,
            "role": role,
            "ordinal": plan.ordinal,
            "run_id": run_id,
            "cluster": cluster,
            "copy": str(plan.path),
            "board_rel": record["board_rel"],
            "prepared_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "harvested_at": None,
        }
        if owner_pid is not None:
            ledger_row["owner_pid"] = owner_pid
        if base_rev:
            ledger_row["base_rev"] = base_rev
        try:
            delegate_copy_store._append_delegate_rounds_ledger(home, ledger_row)
        except delegate_errors.DelegateError as exc:
            # 장부 행이 없으면 이 라운드는 회수 자체가 막힌다 — 남은 좌표를 진단에 싣는다.
            raise delegate_errors.DelegateError(
                f"{exc}; run-dir={run_dir}"
                f"{delegate_copy_store._reserved_round_residue(ledger_row['board_rel'], plan.ordinal)}"
            ) from exc
    # 모든 사본과 장부 행이 완성된 뒤, board_lock 밖에서 이번 공개 시드만 기록한다.
    # 재개방은 새 시드가 없고 draft는 promote가 공개하므로 이 집합에 들어오지 않는다.
    if seed_sync_paths:
        try:
            ready = round_maintenance._rounds_mutation_sync_paths(home,
                f"ticket-prepare {cluster} {role}", tuple(seed_sync_paths))
        except OSError as exc:
            residue = " · ".join(
                delegate_copy_store._reserved_round_residue(record["board_rel"], record["ordinal"])
                for record in reserved)
            raise delegate_errors.DelegateError(
                f"준비 완료 후 보드 기록 실패: {exc}; run-dir={run_dir}{residue}"
            ) from exc
        if not board_git._board_git_enabled(home):
            print("ticket prepare: 별도 board-git 없음 (기존 no-op 기록)", file=sys.stderr)
        elif ready:
            print("ticket prepare: 신규 시드 로컬 기록 완료 "
                  "(원격 동기 보류는 board sync 경고로 안내)", file=sys.stderr)
        else:
            print("ticket prepare: 신규 시드 로컬 기록 보류 (local-only/uncommitted)", file=sys.stderr)
    return delegate_rounds.ClusterCopyPlan(run_dir, cluster, run_id, role, tuple(plans))


def abandon_ticket_copy(
    home, *, copy_path: Path, cwd: Path, assume_dead: bool = False,
    superseded_by_ordinal: int | None = None, discard_reason: str | None = None,
) -> delegate_rounds.TicketAbandonResult:
    """kill 로 죽어 이어 갈 수 없는 준비를 명시적으로 정리한다 — 장부 행·board 라운드·run-dir.

    인가 경로는 회수(harvest)와 같다(같은 신뢰 뿌리 · 같은 경로 전량 일치 · 같은 run-dir chain
    plain 판정). 그 위에 이 처분만의 규칙 셋이 있다.

    1. **종료 증거**(`_abandon_liveness_problem`) — 살아 있는 run 을 지우지 않는다.
    2. **시드 그대로** — 슬롯 사본 bytes 와 board 라운드 파일의 현재 bytes 직접 대조(개행 표기만
       정규화). 산출이 있으면 harvest 로 회수할 대상이지 지울 대상이 아니다. **예외는 운영자의
       선언 둘**이고 서로 배타다: `superseded_by_ordinal` 은 "이 라운드는 재실행된 그 라운드로
       대체됐다"(finding ID 재선언 등으로 harvest 도 거부하는 재실행 대체본 한정),
       `discard_reason` 은 "이 라운드는 폐기한다"다. 선언이 하나도 없으면 여전히 거부다 —
       선언 없는 처분은 조용한 파괴다. 값 형식(대체본: 자기 자신 아님·1 이상 · 폐기: 한 줄
       비지 않은 사유)과 실재(그 ordinal 의 라운드가 이 티켓에 있음) 검증은 인자를 받는
       즉시(시드 대조와 무관하게) 끝낸다.
    3. **최대 순번일 때만 board 파일 삭제** — 중간 순번을 지우면 `round-gap`(완료 게이트 red)을
       만든다. 중간 순번이면 board 파일을 **보존**하고 장부 행과 run-dir 만 종결한다. 거부가
       아니라 분기라서 프로토콜이 항상 수렴한다. 보존한 파일에는 엔진 표식 줄을 발행한다 —
       그 라운드는 영원히 시드 그대로라 표식이 없으면 `round-pending` 과 판정 표면에 자리표시
       골격째로 남는다(발행은 파괴 판정 기준선을 읽은 **뒤**다). **폐기 선언은 순번과 무관하게
       보존**이고, 보존한 파일에 관측한 슬롯 산출을 그대로 옮긴 뒤 같은 표식을 붙인다 — 슬롯
       사본이 그 산출의 유일본이라 지우기 전에 board 로 옮기지 않으면 산출이 사라진다.
    4. **지울 자산이 둘 다 없는 행**(슬롯 사본도 board 라운드 파일도 없다) — 폐기 선언을 받아
       장부 행만 닫는다. 자산 부재는 "판정 불능"이 아니라 그 단계가 끝난 상태다. 사본만 없고
       board 가 살아 있으면 지울 대상이 있는데 기준선을 못 읽는 상태라 종전대로 거부다.

    순서는 되돌릴 수 없는 삭제를 마지막에 두고 그 앞을 전부 멱등 기록으로 만든다 — journal 없이
    재개가 성립한다. (1) 인가 + 선언 배타·값·실재 검증 (2) 종료 판단·시드 대조 → 선언 분기가
    실제로 발화했을 때만(값이 그냥 주어졌다는 사실만으로는 아님) 생존 게이트 통과 뒤 loud +
    `abandoned_at` 마감 행 append 에 `superseded_by_ordinal`·`discard_reason` 동반 (3)
    `board_lock` 안에서 최대 순번 재확인 + 최대면 unlink(폐기는 보존) (4) 락 밖 산출 이관·표식
    발행·sync (5) 슬롯 파일 재판독 후 시드(또는 **장부에 기록된** 선언이 있으면 이번 호출이
    관측한 산출) 그대로일 때만 run-dir 삭제 — 재호출이 인자를 다시 줘도 장부에 기록되지 않은
    값은 이 판정에 쓰지 않는다 (6) 세 자산 재판독 수렴 단언. **재호출은 rollback 이 아니라 남은
    작업의 완료**다 — 마감 행이 이미 있는 행은 거부하지 않고 2 를 건너뛰고, 선언 여부는 장부
    행에서 다시 읽는다(재호출은 선언 인자를 다시 주지 않아도 된다).

    `board_lock` 안에는 순번 판정과 unlink 만 둔다. `_rounds_mutation_sync_paths` 는
    `refresh_board` → `board_lock` 재진입이고 전역 락 순서(board_git_lock → board_lock) 역전이라
    락 안에서 부르면 교착한다.
    """
    pm_home_root = home.local_dir.parent.parent.resolve()
    cwd = Path(cwd).resolve()
    # 선언이 둘이면 장부에 어느 것이 실릴지 모호하다 — 상태를 읽기 전에 거부한다.
    if superseded_by_ordinal is not None and discard_reason is not None:
        raise delegate_errors.DelegateError(
            f"처분 선언은 하나여야 합니다 — {delegate_rounds.ABANDON_SUPERSEDED_BY_FLAG}(재실행 대체)와 "
            f"{delegate_rounds.ABANDON_DISCARD_REASON_FLAG}(폐기)를 함께 줄 수 없습니다"
        )
    if discard_reason is not None and not delegate_ledger._is_valid_discard_reason(discard_reason):
        raise delegate_errors.DelegateError(
            f"폐기 사유는 비어 있지 않은 한 줄이어야 합니다: "
            f"{delegate_rounds.ABANDON_DISCARD_REASON_FLAG}={discard_reason!r}"
        )
    # ── 1단계: 인가 ────────────────────────────────────────────────────────
    row = delegate_copy_store._delegate_round_record(home, copy_path)
    if row["harvested_at"] is not None:
        raise delegate_errors.DelegateError(f"이미 회수된 준비는 포기할 수 없습니다: {copy_path}")
    expected = delegate_copy_store._expected_slot_round_path(cwd, row)
    given = Path(copy_path)
    if not given.is_absolute() or not delegate_copy_store._same_lexical_path(given, expected):
        raise delegate_errors.DelegateError(
            "delegate-rounds 장부가 인가한 라운드 경로와 불일치 — 포기하지 않습니다: "
            f"요청={given} · 장부 인가={expected}"
        )
    run_dir = expected.parent
    # chain 검사는 **지울 것이 남아 있을 때만** 한다 — 이미 지운 run-dir 을 다시 요구하면
    # 수렴한 처분의 재호출이 "경로 검사 실패"로 막혀 전진 수렴이 깨진다.
    if os.path.lexists(run_dir):
        delegate_copy_store._assert_slot_run_dir_chain_is_plain(cwd, delegate_ledger._row_ticket_relative_dir(row))
    board_path = pm_home_root / Path(*PurePosixPath(row["board_rel"]).parts)
    if board_path.name != expected.name:
        raise delegate_errors.DelegateError(
            "delegate-rounds 장부의 board 경로와 라운드 이름 불일치: "
            f"board={board_path.name} · 기대={expected.name}"
        )
    if superseded_by_ordinal is not None:
        # 값·실재 검증은 시드 대조(mismatch) 분기와 무관하게 인자를 받는 즉시 끝낸다 — 시드
        # 그대로인 호출(플래그가 no-op 인 호출)에도 잘못된 값을 걸러 장부에 남을 값을 항상
        # 유효하게 유지한다.
        if superseded_by_ordinal < 1 or superseded_by_ordinal == row["ordinal"]:
            raise delegate_errors.DelegateError(
                f"대체 라운드 ordinal 이 올바르지 않습니다(자기 자신이거나 1 미만): "
                f"{delegate_rounds.ABANDON_SUPERSEDED_BY_FLAG}={superseded_by_ordinal} · "
                f"이 라운드 ordinal={row['ordinal']}"
            )
        existing_ordinals = {
            item.ordinal
            for item in round_seed.load_rounds(pm_paths.tickets_dir(home), row["ticket"])
        }
        if superseded_by_ordinal not in existing_ordinals:
            raise delegate_errors.DelegateError(
                "대체 라운드가 존재하지 않습니다 — 없는 ordinal 을 대체본으로 대지 않습니다: "
                f"{delegate_rounds.ABANDON_SUPERSEDED_BY_FLAG}={superseded_by_ordinal} · ticket={row['ticket']} · "
                f"준비된 순번={sorted(existing_ordinals)}"
            )
    changed = False
    sync_ready = False
    # 이번 호출이 관측한 슬롯 bytes. 5단계가 삭제 직전에 다시 읽어 이 값과 대조한다 — 사전 read
    # 이후 살아 있는 agent 가 쓴 산출을 지우지 않기 위해서다.
    observed_slot_text = (
        delegate_copy_store._read_slot_round_text(expected) if os.path.lexists(expected) else None
    )

    # ── 2단계: 종료 판단 + 시드 대조 → 마감 행 append(내구성 있는 intent) ──
    # 이번 호출이 실제로 선언 분기를 발화해 대체-확인·폐기를 확정했는지 — 장부 기록·loud 는 이
    # 값이 True 일 때만 한다(값이 그냥 주어졌다는 사실만으로는 기록하지 않는다).
    superseded_confirmed_this_call = False
    discard_confirmed_this_call = False
    if "abandoned_at" not in row:
        reserved = (
            delegate_copy_store._read_board_round_text(board_path) if os.path.lexists(board_path) else None
        )
        if observed_slot_text is None and reserved is None:
            # 지울 자산이 하나도 없다 — 남은 것은 장부 행뿐이고, 그 행을 닫는 조건은 "무엇을
            # 지우나" 가 아니라 "무엇을 밝혔나" 다.
            if discard_reason is None:
                raise delegate_errors.DelegateError(
                    "슬롯 사본도 board 라운드 파일도 없어 지울 자산이 없습니다 — 이 예약을 왜 "
                    f"폐기하는지 {delegate_rounds.ABANDON_DISCARD_REASON_FLAG} 로 밝히고 다시 부르세요: "
                    f"ticket={row['ticket']} · role={row['role']} · "
                    f"ordinal={row['ordinal']} · copy={expected}"
                )
            discard_confirmed_this_call = True
        elif observed_slot_text is None:
            raise delegate_errors.DelegateError(
                f"슬롯 라운드 파일이 없습니다(포기 판정 불능): {expected}"
            )
        elif reserved is None:
            raise delegate_errors.DelegateError(
                f"board 라운드 파일이 없습니다(예약 소실): {board_path}"
            )
        elif markdown_text._normalized_newlines(observed_slot_text) != markdown_text._normalized_newlines(reserved):
            # 값·실재 검증은 1단계에서 이미 끝났다 — 여기서는 이번 호출이 그 검증을 거쳐
            # 선언 분기를 실제로 발화했다는 사실만 표시한다(장부 기록·loud 를 여는 유일한
            # 조건).
            if discard_reason is not None:
                discard_confirmed_this_call = True
            elif superseded_by_ordinal is not None:
                superseded_confirmed_this_call = True
            else:
                raise delegate_errors.DelegateError(
                    "산출이 있어 포기할 수 없습니다(시드 그대로가 아님) — `ticket harvest` 로 "
                    "회수하거나, 재실행으로 대체된 라운드라면 그 대체본 ordinal 을 "
                    f"{delegate_rounds.ABANDON_SUPERSEDED_BY_FLAG} 로, 폐기하는 라운드라면 그 사유를 "
                    f"{delegate_rounds.ABANDON_DISCARD_REASON_FLAG} 로 밝히고 다시 부르세요: "
                    f"ticket={row['ticket']} {board_path.name}"
                )
        if not assume_dead:
            problem = delegate_copy_store._abandon_liveness_problem(
                home, row, copy_path=expected, )
            if problem is not None:
                raise delegate_errors.DelegateError(problem)
        if superseded_confirmed_this_call:
            # loud 는 생존 게이트를 통과한 뒤에만 — 거부된 호출에 "포기합니다" 를 남기지 않는다.
            print(
                "[pm-delegate] 대체-확인: 산출이 시드와 달라도 포기합니다 — "
                f"ticket={row['ticket']} role={row['role']} ordinal={row['ordinal']} 을 "
                f"ordinal={superseded_by_ordinal} 대체본으로 간주합니다(산출은 board 판정 표면에 "
                f"오르지 않습니다): copy={copy_path}",
                file=sys.stderr,
            )
        if discard_confirmed_this_call:
            print(
                "[pm-delegate] 폐기-확인: 이 라운드를 폐기합니다 — "
                f"ticket={row['ticket']} role={row['role']} ordinal={row['ordinal']} · "
                f"사유={discard_reason}(산출이 있으면 board 라운드 파일로 옮기고 판정 표면에는 "
                f"오르지 않습니다): copy={copy_path}",
                file=sys.stderr,
            )
        abandoned = dict(row)
        abandoned["abandoned_at"] = datetime.datetime.now(
            datetime.timezone.utc
        ).isoformat()
        if superseded_confirmed_this_call:
            abandoned["superseded_by_ordinal"] = superseded_by_ordinal
        if discard_confirmed_this_call:
            abandoned["discard_reason"] = discard_reason
        delegate_copy_store._append_delegate_rounds_ledger(home, abandoned)
        changed = True

    # 5단계의 파괴 효력은 **장부에 기록된** 선언만 인정한다 — 재호출 인자는(다시 줬어도)
    # 무시한다. 이번 호출이 2단계를 거쳤으면 방금 기록한 값을, 건너뛰었으면(행이 이미 닫혀
    # 있었으면) 그 닫힌 행에 이미 기록됐던 값을 그대로 읽는다 — 검증을 거치지 않은 이번
    # 호출의 인자가 파괴 판정에 섞이지 않는다.
    superseded_marker_recorded: int | None
    if superseded_confirmed_this_call:
        superseded_marker_recorded = superseded_by_ordinal
    else:
        recorded = row.get("superseded_by_ordinal")
        superseded_marker_recorded = (
            recorded if isinstance(recorded, int) and not isinstance(recorded, bool) else None
        )
    discarded_recorded: bool = (
        discard_confirmed_this_call or delegate_ledger._is_valid_discard_reason(row.get("discard_reason"))
    )

    # ── 3단계: board_lock 안 — 최대 순번 재확인 + 최대면 unlink (락 안은 여기까지) ──
    # 폐기는 순번과 무관하게 보존이다 — 그 자리가 산출을 옮겨 받는 자리라 지울 수 없다.
    board_kept_reason: str | None = None
    reserved_now: str | None = None
    unlinked = False
    with fs.board_lock(home):
        if os.path.lexists(board_path):
            existing = round_seed.load_rounds(pm_paths.tickets_dir(home), row["ticket"])
            max_ordinal = max((item.ordinal for item in existing), default=row["ordinal"])
            if not discarded_recorded and row["ordinal"] >= max_ordinal:
                try:
                    board_path.unlink()
                except OSError as exc:
                    raise delegate_errors.DelegateError(
                        f"board 라운드 파일 삭제 실패: {board_path}: {exc}"
                    ) from exc
                unlinked = True
                changed = True
            else:
                board_kept_reason = (
                    "폐기 처분(산출 이관 대상)" if discarded_recorded
                    else f"중간 순번(현재 최대 순번={max_ordinal})"
                )
                reserved_now = delegate_copy_store._read_board_round_text(board_path)

    # ── 4단계: 락 밖 — 보존 분기 산출 이관·표식 발행 + sync ────────────────
    # 발행은 `reserved_now` 재판독 **뒤**다: 5단계 파괴 판정이 그 bytes 를 기준선으로 쓰므로,
    # 앞서 붙이면 표식 자체가 "산출이 생겼다" 로 읽혀 정상 포기가 거부로 뒤집힌다. 락 안에도
    # 두지 않는다 — 그 임계구역은 순번 판정과 unlink 만 담는다(재진입 교착 주석 참조).
    # 폐기 분기는 표식만 붙이지 않고 **관측한 슬롯 산출로 본문을 교체**한다 — 그 사본이 유일본
    # 이라, 옮기지 않고 run-dir 을 지우면 산출이 사라진다. 표식 유무가 이관 완료의 관측이라
    # 재호출은 두 번 쓰지 않는다.
    marker_issued = False
    if reserved_now is not None:
        baseline = review_contract._round_text_without_refused_marker(reserved_now, row["role"])
        if baseline == reserved_now:
            moved = (
                observed_slot_text
                if discarded_recorded and observed_slot_text is not None
                else reserved_now
            )
            try:
                round_store.replace_round(
                    board_path, review_contract._round_text_with_refused_marker(moved, row["role"]),
                )
            except (OSError, UnicodeError) as exc:
                raise delegate_errors.DelegateError(
                    f"board 라운드 표식 발행 실패: {board_path}: {exc}"
                ) from exc
            marker_issued = True
            changed = True
        # 기준선은 언제나 표식 이전 bytes 다 — 재호출도 같은 값으로 5단계를 판정한다.
        reserved_now = baseline
    # 이번에 지웠든(첫 통과) 앞선 시도가 지웠든(재개), board 라운드가 사라졌는데 아직 정리할 것이
    # 남아 있으면 부른다 — 앞선 시도의 sync 실패가 영구 미커밋으로 굳지 않게 한다(`git add -A`
    # + commit 이라 재실행은 no-op). 보존 분기는 표식이 그 파일을 바꾸므로 같은 이유로 함께
    # 커밋한다.
    if (
        (not os.path.lexists(board_path) and (unlinked or os.path.lexists(run_dir)))
        or (reserved_now is not None and (marker_issued or os.path.lexists(run_dir)))
    ):
        message = f"ticket-abandon {row['ticket']} {row['role']}"
        # board 부분 커밋 seam 은 이름으로 직접 부른다 — harvest 와 같은 이유다(부분 동기 사본에서
        # 커밋만 조용히 빠지는 rc0 을 피한다).
        sync_ready = bool(round_maintenance._rounds_mutation_sync_paths(home, message, (board_path,)))

    # ── 5단계: 슬롯 파일 재판독 → 시드 그대로일 때만 run-dir 삭제 ──────────
    if os.path.lexists(run_dir):
        if os.path.lexists(expected):
            current = delegate_copy_store._read_slot_round_text(expected)
            references = [observed_slot_text]
            # board 파일이 남아 있으면(중간 순번 보존) 그 bytes 가 시드의 단일 진실이다 — 호출
            # 사이에 착지한 쓰기까지 이 대조가 잡는다. **선언이 기록된 행은 예외다**: 그 board
            # 파일은 이 run 이 산출을 낸 뒤로도 시드 그대로 남아 있거나(대체-확인) 이관으로
            # 산출을 받았으므로(폐기) 시드와 영구히 어긋난다 — 이 축에서는 관측된
            # 산출(`observed_slot_text`)이 유일한 기준선이다.
            if (
                reserved_now is not None
                and superseded_marker_recorded is None
                and not discarded_recorded
            ):
                references.append(reserved_now)
            if any(
                reference is None
                or markdown_text._normalized_newlines(current) != markdown_text._normalized_newlines(reference)
                for reference in references
            ):
                raise delegate_errors.DelegateError(
                    "슬롯 라운드 파일이 시드와 달라져 run-dir 을 지우지 않았습니다(산출 보존) — "
                    f"산출을 직접 꺼낸 뒤 정리하세요: copy={expected} · run-dir={run_dir}"
                )
        try:
            fs.force_rmtree(run_dir)
        except OSError as exc:
            raise delegate_errors.DelegateError(
                f"run-dir 삭제 실패: {run_dir}: {exc} — 같은 명령을 다시 실행하면 남은 정리를 "
                "이어 갑니다"
            ) from exc
        delegate_copy_store._prune_empty_run_dir(run_dir)
        changed = True

    # ── 6단계: 세 자산 재판독 수렴 단언 ───────────────────────────────────
    board_present = os.path.lexists(board_path)
    run_dir_removed = not os.path.lexists(run_dir)
    ledger_closed = "abandoned_at" in delegate_copy_store._delegate_round_record(home, copy_path)
    board_converged = board_present == (board_kept_reason is not None)
    return delegate_rounds.TicketAbandonResult(
        changed=changed,
        sync_ready=sync_ready,
        board_removed=not board_present,
        run_dir_removed=run_dir_removed,
        converged=bool(ledger_closed and run_dir_removed and board_converged),
    )


def _refund_gate_rejected_ticket_copy(
    home, ticket_copy: delegate_rounds.TicketCopyPlan, *, cwd: Path, ) -> None:
    """예약(`prepare_ticket_copy`) 이후 ~ 실행 인계(`_execute_and_collect`) 이전 구간의
    **단일 정리 경계**. `main()` 의 finally 하나가 이 함수를 부른다 — 그 구간을 벗어나는 모든
    경로(명시 return · 전파 예외)가 지점 삽입 없이 여기로 수렴한다(리뷰 라운드가 꽂았던 6개
    지점 삽입을 대체). board 라운드 파일·delegate-rounds 장부 행·run-dir 세 축을 같은 프로세스가
    즉시 종결한다. 새 경로를 만들지 않고 기존 `abandon_ticket_copy` 를 그대로 재사용한다.
    `assume_dead=True` — 이 run 의 `owner_pid` 는 **이 살아 있는 프로세스 자신**이라(핸드오프 전
    이탈) 생존 판정이 자기 자신을 "진행 중"으로 오판해 막는다; 정상 스폰 후 죽은 run 의
    교차-프로세스 정리와는 다른 축이다.
    원 종료 사유(rc·메시지)는 이 함수 호출 **전**에 이미 stderr 로 loud 하게 남거나(명시 거부) 그대로
    전파 중이다(예외 이탈). 여기서는 환불 결과만 덧붙이고, 환불 자체가 어떤 예외형으로 실패해도
    (`DelegateError` 뿐 아니라) 원 결과를 대체하지 않는다 — `return`/`raise` 없이 경고만 추가해,
    이 함수를 부른 `finally` 통과 뒤 원래 return 값·전파 예외가 그대로 유지된다.
    """
    try:
        result = abandon_ticket_copy(
            home, copy_path=ticket_copy.path, cwd=cwd, assume_dead=True,
        )
    except BaseException as exc:
        cause = f"{type(exc).__name__}: {exc}"
        print(
            "경고: 위임 이탈(거부/예외) 후 ticket 예약 환불 실패 — board 라운드·run-dir 이 잔류할 수 "
            f"있습니다. 같은 정리를 `ticket abandon --copy {ticket_copy.path} --cwd {cwd} "
            f"{delegate_rounds.ABANDON_ASSUME_DEAD_FLAG}` 로 다시 시도하세요: {cause}",
            file=sys.stderr,
        )
        return
    print(
        "[pm-delegate] 위임 이탈(거부/예외) 후 ticket 예약 환불: "
        f"board_removed={str(result.board_removed).lower()} · "
        f"run_dir_removed={str(result.run_dir_removed).lower()} · "
        f"converged={str(result.converged).lower()} · copy={ticket_copy.path}",
        file=sys.stderr,
    )
