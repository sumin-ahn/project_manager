"""티켓 생성과 상태 전이 명령 (층: cli)."""
from __future__ import annotations

from pathlib import Path
from pm_engine.app import areas_registry
from pm_engine.app import board_claim
from pm_engine.app import board_dashboard
from pm_engine.app import board_sync
from pm_engine.app import body_lint
from pm_engine.app import cluster_binding
from pm_engine.app import completion_gate
from pm_engine.app import round_maintenance
from pm_engine.app import round_seed
from pm_engine.app import ticket_access
from pm_engine.cli import board_cluster
from pm_engine.cli import task_args
from pm_engine.domain import board_git_rules
from pm_engine.domain import cluster_rules
from pm_engine.domain import design_rules
from pm_engine.domain import identity_args
from pm_engine.domain import review_state
from pm_engine.domain import round_rules
from pm_engine.domain import ticket_rules
from pm_engine.infra import areas_store
from pm_engine.infra import board_git
from pm_engine.infra import clock
from pm_engine.infra import cluster_store
from pm_engine.infra import fs
from pm_engine.infra import lease_store
from pm_engine.infra import pm_paths
from pm_engine.infra import round_store
from pm_engine.infra import ticket_store
from typing import Any
import argparse
import datetime
import json
import re
import sys


def cmd_claim(home, args: argparse.Namespace) -> int:
    # claim = 귀속 쓰기 — `--task` 가 없으면 어떤 쓰기·git 호출보다 먼저 멈춘다. 함께 준
    # `--repo`/`--slot` 은 그 task 가 보유한 폴더(claim 앵커)를 고른다.
    override = task_args._execution_actor_session_override(home, args)
    if override is None:
        sys.exit(task_args.TASK_REQUIRED.format(action="claim", command=f"board.py claim {args.id}"))
    # claimed_by 는 `<user>/<task>` — user 미상이면 task 만(graceful).
    assignee = lease_store.identity_tag(home, session_override=override,
                            user_override=getattr(args, "user", None))
    # 코드 트리 HEAD 박제 — 락 밖(읽기 전용 git 조회)이라 board mutation 창을 넓히지 않는다.
    claimed_rev = task_args._claim_head_rev(home, args)

    # strict claim 전체(prefetch → local move/dump → commit/push → confirm/rollback)를 두 락으로
    # 감싼다. 순서는 **board_git_lock → board_lock** 고정. board_lock은 network 동안도 유지해
    # rollback이 동시 in-place write를 옛 bytes로 덮는 창을 닫는다(짧은 전역 coordination 비용 수용).
    # `_cmd_claim_locked`는 두 락 보유 전제라 내부에서 어느 락도 재진입하지 않는다.
    with board_git.board_git_lock(home):
        with fs.board_lock(home):
            return _cmd_claim_locked(home, args, assignee, claimed_rev)


def _cmd_claim_locked(home, args: argparse.Namespace, assignee: str, claimed_rev: str) -> int:
    """`cmd_claim` 본체 — board_git_lock + board_lock 보유 전제(둘 다 재진입 금지)."""
    # claim STRICT 1단계: 선점 감지는 **읽기 전용**(fetch + 원격 트리
    # 직접 조회)이고, `pull --rebase` 는 원격이 앞섰을 때만 시도한다. 차단은 "원격이 앞섰고
    # 통합 불가" 로만 남고, 사유(dirty/rebase 충돌/offline/upstream 없음)와 진단 재료(behind·
    # 더러운 파일)를 구조체로 받아 **한 곳에서** 안내를 렌더한다(오판·이중출력 0).
    prefetch = board_claim._board_git_claim_prefetch(home, args.id)
    if prefetch.block is not None:
        print(board_claim._claim_block_message(args.id, prefetch), file=sys.stderr)
        return 1
    orig_head = prefetch.anchor

    # 아래 local write뿐 아니라 이 함수 앞의 prefetch와 뒤의 confirm/rollback도 호출자가 잡은
    # board_lock 안이다. 따라서 strict claim 전체가 다른 ticket writer에 하나의 순서로 관측된다.
    # On POSIX the atomic rename(2) alone was exclusive; on Windows os.rename is
    # NOT exclusive across concurrent processes, so without serialization every
    # concurrent claimer could "win" and duplicate the claim
    # the "rename is the lock" premise is Windows-invalid.
    # Holding board_lock makes the complete ticket write exclusive everywhere: once
    # the winner moves the ticket out of open/, every serialized loser's
    # lookup/load/move hits the now-missing path → FileNotFoundError. Those windows
    # all mean the same thing — we lost the claim
    # race — so all surface as one clean "claim race lost" (rc=1), never an
    # unhandled traceback. board_lock is OS-flock
    # based, so a crash mid critical-section auto-releases the lock (no stale
    # lock). Note: a dependency's own FileNotFoundError is caught below and is a
    # *normal* rejection ("dependency not found"), distinct from a claim race.
    try:
        try:
            status, path = ticket_store.find_ticket_for_mutation(home, args.id)
        except FileNotFoundError as e:
            print(e, file=sys.stderr)
            return 2
        if status != "open":
            print(f"cannot claim {args.id}: currently in {status}/", file=sys.stderr)
            return 1
        fm, body = ticket_store.load_ticket(path)
        # Check dependencies
        for dep in fm.get("depends_on") or []:
            try:
                # 의존성 조회도 정확 일치만 — 이 판정이 claim 이동 여부를 정하므로, 폴백이
                # 답한 *다른* 티켓의 done 상태로 게이트가 열리면 안 된다(미해소 = 거부).
                dep_status, _ = ticket_store.find_ticket_for_mutation(home, dep)
            except FileNotFoundError:
                print(f"dependency {dep} not found", file=sys.stderr)
                return 1
            if dep_status != "done":
                print(f"dependency {dep} is {dep_status}/, not done",
                      file=sys.stderr)
                return 1

        # 설계 단계 게이트 — `design: required` 티켓은 설계 절을 완성하고
        # 필드를 `done` 으로 승격하기 전에는 집을 수 없다. 의존성 거부와 같은 자리
        # (이동 *전*·board_lock 안)라 티켓은 open/ 에 그대로 남는다.
        # `n/a`·필드 부재(구세대 티켓)는 무영향 — 판정은 `_design_issues` 단일
        # 깔때기(lint advisory·promote 게이트와 같은 규칙).
        design_verdict = design_rules._design_issues(args.id, body, fm.get("design"))
        if design_verdict:
            print(f"cannot claim {args.id}: 설계 단계 미완 — "
                  f"{design_verdict[0][2]}", file=sys.stderr)
            print("  → 설계 절(경계 실측·불변식·표면 상한·테스트 전략)을 채우고 설계 "
                  "검토를 마친 뒤 frontmatter 를 승격하라.", file=sys.stderr)
            return 1

        # 롤백 복원 기준 = 이동 *전* 원본 바이트
        # frontmatter 갱신(claimed_by/claimed_at)을 되돌릴 원본을 우리가 들고 있어야 한다.
        original_bytes = fs.read_bytes_shared(path)
        new_path = ticket_store.move_ticket(home, path, "claimed")
        fm["status"] = "claimed"
        fm["claimed_by"] = assignee
        fm["claimed_at"] = clock.now_utc()
        fm = task_args._frontmatter_with_claimed_rev(fm, claimed_rev)
        ticket_store.dump_ticket(new_path, fm, body)
    except FileNotFoundError:
        print(f"claim race lost on {args.id}", file=sys.stderr)
        return 1

    # claim STRICT 3·4단계: 로컬 claim 을 **그 티켓 경로만** commit 하고
    # push 가 성공해야 *비로소* 소유 확정. push 실패(non-FF/거부)면 `reset --soft <anchor>` +
    # 티켓 파일 역이동·원본 복원으로 **이 claim 이 만진 것만** 되돌리고(공유 트리의 무관한
    # 미커밋 작업은 불변) race-lost 로 명시 실패한다 — 거짓 소유를 남기지 않는다. board 가 별도
    # git 이 아니면 confirm 은 True(로컬 rename 만으로 확정·legacy 무변경).
    claim_files = board_git_rules._ClaimFiles(old=path, new=new_path, original=original_bytes)
    if not board_claim._board_git_claim_confirm(home, orig_head, claim_files):
        print(f"claim race lost on {args.id} (board push 충돌·소유 미확정·롤백됨)",
              file=sys.stderr)
        # board_lock 보유 중 public refresh_board()는 재진입이다. rollback까지 끝난 현재 상태를
        # locked 본체로 render하고 두 락을 함께 해제한 뒤 반환한다.
        board_dashboard._refresh_board_locked(home)
        return 1
    print(f"claimed {args.id} as {assignee}")
    board_dashboard._refresh_board_locked(home)
    return 0


def _review_problem(home, tid: str, checker_name: str, spec_text: str | None) -> str | None:
    """리뷰 잔여 판정 배선 — 명세와 리뷰 라운드 파일을 읽어 review_state 판정 함수에 넘긴다.

    판정 입력은 board 라운드 파일과 명세 PM 영역뿐이다. 판정 불능(티켓·명세·라운드 파일을
    읽지 못함)은 통과가 아니라 차단 사유다.
    """
    if spec_text is None:
        found = ticket_store.find_ticket_exact(home, tid)
        if found is None:
            return f"티켓을 찾지 못해 리뷰 잔여를 판정할 수 없습니다: {tid}"
        try:
            spec_text = fs.read_text_shared(found[1], encoding="utf-8", newline="")
        except (OSError, UnicodeError) as exc:
            return f"티켓 명세를 읽을 수 없어 리뷰 잔여를 판정할 수 없습니다: {exc}"
    checker = getattr(review_state, checker_name)
    try:
        rounds = round_seed.load_rounds(pm_paths.tickets_dir(home), tid, ticket_text=spec_text)
    except round_rules.RoundsError as exc:
        return f"리뷰 라운드를 읽지 못해 리뷰 잔여를 판정할 수 없습니다: {exc}"
    return checker(spec_text, rounds)


def _review_completion_problem(home, tid: str, spec_text: str | None = None) -> str | None:
    """완료 판정의 리뷰 잔여 — 처분·확인되지 않은 잔여가 있으면 사유(없으면 None).

    `spec_text` 를 주면 그 명세로 판정한다(완료 게이트는 lock 안에서 읽은 본문을 넘긴다).
    """
    return _review_problem(home, tid, "review_completion_problem", spec_text)


def _review_resolution_input_problem(home, tid: str) -> str | None:
    """종결 선검사의 리뷰 입력 — 돌려받은 리뷰가 없거나 확인 입력이 모자라면 사유."""
    return _review_problem(home, tid, "review_resolution_input_problem", None)


def _round_completion_problems(home, tid: str, spec_text: str) -> list[str]:
    """완료 게이트가 보는 라운드 판정 — **순번 유일성·연속성이 깨진 둘만 차단**한다.

    빈틈(`round-gap`)과 중복(`round-dup`)은 라운드가 지워졌거나 순서가 모호하다는 뜻이라
    완료 증거가 성립하지 않는다. 이름 문법 위반과 산출 없는 라운드(시드 그대로)는 정보로만
    낸다 — 미회수는 게이트가 아니라는 결정이고, 판정 코드의 단일 진실은 사이드카 seam 이다.
    """
    problems = round_seed.verify_rounds(pm_paths.tickets_dir(home), tid, ticket_text=spec_text)
    blocking_codes = (round_rules.PROBLEM_GAP, round_rules.PROBLEM_DUPLICATE)
    blocking: list[str] = []
    for item in problems:
        if item.code in blocking_codes:
            blocking.append(f"{item.code}: {item.detail}")
        else:
            print(f"ⓘ  {item.code}: {item.detail}", file=sys.stderr)
    return blocking


def _complete_gate(home, tid: str, args: argparse.Namespace,
                   body: str | None = None, *, spec_text: str | None = None,
                   ticket_path: Path | None = None) -> list[str]:
    """Verify completion housekeeping before a ticket may move to done/.

    Returns a list of *blocking* problems (empty = gate passes). Non-blocking
    concerns are printed to stderr as warnings from here.

    The regression check trusts the caller's `--tests-pass` assertion rather
    than re-running the (slow) suite.

    `body` = 완료 대상 티켓 본문(§3 DoD 체크 입력). None 이면 DoD·라운드·리뷰 잔여 검사를 건너뛴다 —
    본문 없이 기록 축만 묻는 호출자(단위 테스트 등)를 위한 것이고, 실 complete 경로
    (`cmd_complete`)는 항상 본문과 명세 전문을 넘긴다.

    `spec_text` = 명세 파일 전문(frontmatter 포함) — PM 판정 블록이 사는 자리다(없으면 본문으로
    대신한다). 라운드 산출 없음 판정의 입력은 아니다(그 판정은 라운드 파일 내용만 본다).
    """
    problems: list[str] = []
    id_re = re.compile(rf"\b{re.escape(tid)}\b")

    # 1. log/current.md must contain an entry for this ticket.
    if not args.allow_missing_log:
        log_text = fs.read_text_shared((pm_paths.wiki_dir(home) / "log/current.md"), encoding="utf-8") if (pm_paths.wiki_dir(home) / "log/current.md").exists() else ""
        if not id_re.search(log_text):
            problems.append(
                f"no log/current.md entry mentions {tid} — append one to "
                f"{pm_paths._rel_to_repo(home, (pm_paths.wiki_dir(home) / "log/current.md"))} (or pass --allow-missing-log)")

    # 2. regression must be confirmed by the implementing session.
    if not (args.tests_pass or args.allow_untested):
        problems.append(
            "regression not confirmed — run the project test_cmd, then pass "
            "--tests-pass (or --allow-untested for a regression-irrelevant "
            "ticket)")

    # 3. DoD 전항이 체크(`- [x]`)되었거나 사유와 함께 이월(`- [>] … (이월: …)`)되어야 한다.
    #    미체크 항목이 남은 채 done 이 되면 그 항목은 보드에서 증발한다.
    if body is not None:
        problems.extend(completion_gate._dod_open_items(body))

        problems.extend(_round_completion_problems(home,
            tid, body if spec_text is None else spec_text))

        # 5. 리뷰 잔여 — 마지막 판정 표면 리뷰 라운드의 must-fix 가 처분·확인돼야 한다(입력은
        #    리뷰 라운드 파일과 명세 PM 영역). 리뷰 라운드가 없는 티켓은 대상이 아니다.
        review_problem = _review_completion_problem(home,
            tid, body if spec_text is None else spec_text)
        if review_problem is not None:
            problems.append(review_problem)

    return problems


def cmd_complete(home, args: argparse.Namespace) -> int:
    with fs.board_lock(home):
        try:
            status, path = ticket_store.find_ticket_for_mutation(home, args.id)
        except FileNotFoundError as e:
            print(e, file=sys.stderr)
            return 2
        if status != "claimed":
            print(f"cannot complete {args.id}: in {status}/, must be claimed",
                  file=sys.stderr)
            return 1

        # 본문은 게이트 **전**에 읽는다 — §3 DoD 체크의 입력이다(이동 전 읽기라 차단 시
        # 티켓은 claimed/ 에 그대로 남는다). lookup부터 최종 dump까지 같은 lock이라 tier 가
        # 이 body snapshot 뒤에 끼어들어 유실되지 않는다.
        with fs.open_shared(path, binary=False, encoding="utf-8", newline="") as handle:
            spec_text = handle.read()
        fm, body = ticket_store.load_ticket(path)

        # 묶음 멤버 lifecycle mutation은 정상 종결 파이프라인에 결속한다. ownership, DoD,
        # move/dump와 board-git sync보다 앞이라 거부 시 ticket/ledger/git write가 0이다.
        binding = getattr(args, "cluster_close", None)
        binding_problem = cluster_binding.cluster_complete_binding_problem(home, args.id, fm, binding)
        if binding_problem is None and binding is not None:
            # 결속 완료는 종결 파이프라인이 연 장부(closing)에서만 받는다 — 파이프라인 밖의 직접
            # 호출은 티켓만 done 으로 옮기고 장부·머지를 남긴다(복구 전용 반쪽 상태).
            ledger = cluster_store.load_cluster(home, binding)
            if ledger is not None and str(ledger.get("status") or "").strip() != "closing":
                owner = cluster_rules.cluster_id_for_name(binding)
                binding_problem = (
                    f"묶음 멤버의 완료는 종결 파이프라인 안에서만 한다 — {owner} 장부가 종결 중"
                    f"(closing)이 아니다(현재 {ledger.get('status')}). "
                    f"`ticket_finish.py --cluster {owner}` 로 닫아라")
        if binding_problem is not None:
            print(f"cannot complete {args.id}: {binding_problem}", file=sys.stderr)
            return 1

        # 소유 대조 — claim 한 정체성만 완료할 수 있다. 이동·기록 **전**이라 거부 시 티켓은
        # claimed/ 에 한 바이트도 안 바뀐 채 남는다.
        ownership = ticket_access._ticket_ownership(home, fm, task=getattr(args, "task", None), user=getattr(args, "user", None))
        if not ownership.ok:
            print(ticket_rules._ownership_rejection("complete", args.id, ownership), file=sys.stderr)
            return 1

        # Sync gate — refuse to mark done until housekeeping is verified.
        problems = _complete_gate(home,
            args.id, args, body, spec_text=spec_text, ticket_path=path)
        if problems:
            print(f"cannot complete {args.id}: sync gate failed —", file=sys.stderr)
            for msg in problems:
                print(f"  ✗ {msg}", file=sys.stderr)
            return 1

        new_path = ticket_store.move_ticket(home, path, "done")
        fm["status"] = "done"
        fm["completed_at"] = clock.now_utc()
        ticket_store.dump_ticket(new_path, fm, body)
    board_dashboard.refresh_board(home)
    # 스코프 = 이 mutation 이 만진 두 경로만 — 공유 board 의 무관한 미커밋 작업이
    # 이 커밋에 실려 push 되지 않는다. best-effort sync는 기존 lifecycle 6 callsite
    # (new/promote/complete/block/unclaim/unblock) + 공용 helper 1곳(section-add/tier 공유)이
    # 모두 명시 path를 넘긴다(AST 메타가드가 7 callsite를 감사).
    ready = board_sync._board_git_sync_best_effort(home, f"complete {args.id}", (path, new_path))
    print(f"completed {args.id}{board_git._board_git_mutation_state_suffix(ready)}")
    return 0


def cmd_block(home, args: argparse.Namespace) -> int:
    with fs.board_lock(home):
        try:
            status, path = ticket_store.find_ticket_for_mutation(home, args.id)
        except FileNotFoundError as e:
            print(e, file=sys.stderr)
            return 2
        if status not in ("open", "claimed"):
            print(f"cannot block from {status}/", file=sys.stderr)
            return 1
        fm, body = ticket_store.load_ticket(path)
        # 소유 대조 — claimed 티켓(=소유자 있음)의 상태 변경은 그 소유 세션만. open 티켓은
        # claimed_by 가 없어 무대상이다(현행 무변경).
        ownership = ticket_access._ticket_ownership(home, fm, task=getattr(args, "task", None), user=getattr(args, "user", None))
        if not ownership.ok:
            print(ticket_rules._ownership_rejection("block", args.id, ownership), file=sys.stderr)
            return 1
        new_path = ticket_store.move_ticket(home, path, "blocked")
        fm["status"] = "blocked"
        # claimed_by/claimed_at 는 무접촉 — block 은 작업 중단이지 소유 포기가 아니다.
        # 소유 해제 문은 unclaim 하나뿐. `unblock` 이 이 값의 유무로 복귀 목적지를 정한다.
        note = f"\n## Blocked\n{args.reason} — {datetime.date.today().isoformat()}\n"
        ticket_store.dump_ticket(new_path, fm, body + note)
    board_dashboard.refresh_board(home)
    ready = board_sync._board_git_sync_best_effort(home, f"block {args.id}", (path, new_path))
    print(f"blocked {args.id}: {args.reason}{board_git._board_git_mutation_state_suffix(ready)}")
    return 0


_UNCLAIM_SOURCE_STATUSES: tuple[str, ...] = ("claimed", "blocked")


def cmd_unclaim(home, args: argparse.Namespace) -> int:
    takeover = bool(getattr(args, "takeover", False))
    reason = (getattr(args, "reason", None) or "").strip()
    # `--takeover` 는 귀속 쓰기다(본문 Takeover 절에 이전을 실행한 task 를 남긴다) — `--task` 가
    # 없으면 락·쓰기 전에 거부한다. takeover 없는 unclaim 은 소유 대조라 `--task` 가 선택이다.
    takeover_task = task_args._actor_session_override(home, args) if takeover else None
    if takeover and takeover_task is None:
        print(task_args.TASK_REQUIRED.format(
            action=f"unclaim {args.id} --takeover",
            command=f"board.py unclaim {args.id} --takeover --reason <사유>"), file=sys.stderr)
        return 1
    if takeover and not reason:
        # 사유 없는 강제 이전은 이력이 남지 않는다 — 락을 잡기 전에 거부한다(부작용 0).
        print(f"[중단] unclaim {args.id}: `--takeover` 는 `--reason <사유>` 가 필수다 "
              "— 소유자 부재 이전의 근거가 티켓 본문과 board-git 에 남아야 한다.",
              file=sys.stderr)
        return 1
    with fs.board_lock(home):
        try:
            status, path = ticket_store.find_ticket_for_mutation(home, args.id)
        except FileNotFoundError as e:
            print(e, file=sys.stderr)
            return 2
        if status not in _UNCLAIM_SOURCE_STATUSES:
            print(f"cannot unclaim {args.id}: in {status}/, must be "
                  f"{'/'.join(_UNCLAIM_SOURCE_STATUSES)}", file=sys.stderr)
            return 1
        fm, body = ticket_store.load_ticket(path)
        # 소유 대조 — 정상 경로는 소유자만. `--takeover` 는 **소유자 부재 이전의 유일한 문**
        # 이다(complete/block/unblock 엔 이 인수를 두지 않는다 — 문이 넷이면 소유 규칙이 네 벌이
        # 된다). 이전 사유는 본문 노트와 board-git 커밋 메시지 양쪽에 남긴다(board-git 없는
        # 채택자도 이력이 남아야 한다).
        ownership = ticket_access._ticket_ownership(home, fm, task=getattr(args, "task", None), user=getattr(args, "user", None))
        if not ownership.ok and not takeover:
            print(ticket_rules._ownership_rejection("unclaim", args.id, ownership), file=sys.stderr)
            return 1
        # blocked 는 상태를 유지한다 — unclaim 은 "소유 해제" 지 "차단 해제" 가 아니다.
        dest_status = "open" if status == "claimed" else status
        new_path = ticket_store.move_ticket(home, path, dest_status) if dest_status != status else path
        fm["status"] = dest_status
        previous_owner = fm.get("claimed_by")
        fm["claimed_by"] = None
        fm["claimed_at"] = None
        # claimed_rev 도 함께 비운다 — 소유가 풀린 티켓에 옛 claim 앵커를 남기지 않는다.
        fm.pop("claimed_rev", None)
        if takeover:
            body = body + (f"\n## Takeover\n{reason} — {clock.now_utc()} "
                           f"(소유 `{previous_owner}` 해제 · 실행 task "
                           f"`{takeover_task}`)\n")
        ticket_store.dump_ticket(new_path, fm, body)
    board_dashboard.refresh_board(home)
    message = (f"unclaim {args.id} (takeover: {reason})" if takeover
               else f"unclaim {args.id}")
    ready = board_sync._board_git_sync_best_effort(home, message, (path, new_path))
    detail = f" → {dest_status}/" if dest_status != "open" else ""
    takeover_note = f" (takeover: {reason})" if takeover else ""
    print(f"unclaimed {args.id}{detail}{takeover_note}"
          f"{board_git._board_git_mutation_state_suffix(ready)}")
    return 0


def cmd_unblock(home, args: argparse.Namespace) -> int:
    with fs.board_lock(home):
        try:
            status, path = ticket_store.find_ticket_for_mutation(home, args.id)
        except FileNotFoundError as e:
            print(e, file=sys.stderr)
            return 2
        if status != "blocked":
            print(f"cannot unblock {args.id}: in {status}/, must be blocked",
                  file=sys.stderr)
            return 1
        fm, body = ticket_store.load_ticket(path)
        # 소유 대조 — claimed_by 를 보유한 blocked(=claimed 기원)는 그 소유 세션만 되돌린다.
        # 소유자 없는 open-기원 blocked 는 무대상(현행 무변경).
        ownership = ticket_access._ticket_ownership(home, fm, task=getattr(args, "task", None), user=getattr(args, "user", None))
        if not ownership.ok:
            print(ticket_rules._ownership_rejection("unblock", args.id, ownership), file=sys.stderr)
            return 1
        # claimed_by 보유 티켓(claimed-origin blocked)은 claimed/ 로 복귀시켜 소유 표식을
        # 보존한다 — 무조건 open 으로 옮기면 "open + claimed_by 잔존" 모순이 생겼다.
        # open-origin blocked(claimed_by 없음)는 현행대로 open/ 으로 돌아간다(왕복 불변식).
        dest_status = "claimed" if fm.get("claimed_by") else "open"
        new_path = ticket_store.move_ticket(home, path, dest_status)
        fm["status"] = dest_status
        ticket_store.dump_ticket(new_path, fm, body)
    board_dashboard.refresh_board(home)
    ready = board_sync._board_git_sync_best_effort(home, f"unblock {args.id}", (path, new_path))
    print(f"unblocked {args.id} → {dest_status}/{board_git._board_git_mutation_state_suffix(ready)}")
    return 0


_DISCARD_SOURCE_STATUSES: tuple[str, ...] = (*ticket_rules.ACTIVE_STATUS_DIRS, "draft")


def cmd_discard(home, args: argparse.Namespace) -> int:
    """`discard <T-NNNN> <merged|dropped> --reason <사유>` — 처분 종결(비-완료 종결).

    병합(다른 티켓이 이 일을 흡수)·폐기(취소·대체)를 `done` 과 다른 디렉토리로 닫는다.
    병합 대상 티켓 ID 는 `--reason` 산문에 남긴다(기계 링크 필드는 두지 않는다 — 플래그가
    늘고 처분 종류를 인자로 또 갈라야 한다).
    """
    reason = (getattr(args, "reason", None) or "").strip()
    if not reason:
        print(f"[중단] discard {args.id}: `--reason <사유>` 가 필수다 — 처분 근거 없이 "
              "티켓을 닫지 않는다.", file=sys.stderr)
        return 1
    with fs.board_lock(home):
        try:
            status, path = ticket_store.find_ticket_for_mutation(home, args.id)
        except FileNotFoundError as e:
            print(e, file=sys.stderr)
            return 2
        if status not in _DISCARD_SOURCE_STATUSES:
            print(f"cannot discard {args.id}: in {status}/, must be "
                  f"{'/'.join(_DISCARD_SOURCE_STATUSES)} "
                  f"(이미 종결된 티켓은 `reopen` 후 다시 처분하라)", file=sys.stderr)
            return 1
        fm, body = ticket_store.load_ticket(path)
        # 소유 대조 — 남이 claim 해 진행 중인 티켓을 다른 세션이 처분으로 닫으면 그 작업이
        # 통째로 사라진다(complete 와 같은 축).
        ownership = ticket_access._ticket_ownership(home, fm, task=getattr(args, "task", None), user=getattr(args, "user", None))
        if not ownership.ok:
            print(ticket_rules._ownership_rejection("discard", args.id, ownership), file=sys.stderr)
            return 1
        round_maintenance._discard_round_pending_notice(home, args.id)
        new_path = ticket_store.move_ticket(home, path, "discarded")
        fm["status"] = "discarded"
        fm["disposition"] = args.disposition
        fm["disposition_reason"] = reason
        # 출처 status 를 모든 discard 에 기록한다 — draft 출처면 reopen 이 `.drafts/` 로
        # 되돌아갈 목적지를 이 값으로 판정한다(부재 = 비-draft 출처 · 기존 discarded 자산과
        # 하위호환).
        fm["discarded_from"] = status
        note = (f"\n## Discarded\n{args.disposition} — {reason} — {clock.now_utc()}\n")
        ticket_store.dump_ticket(new_path, fm, body + note)
    board_dashboard.refresh_board(home)
    ready = board_sync._board_git_sync_best_effort(
        home, f"discard {args.id} ({args.disposition}: {reason})", (path, new_path))
    print(f"discarded {args.id} as {args.disposition}: {reason}"
          f"{board_git._board_git_mutation_state_suffix(ready)}")
    return 0


def cmd_reopen(home, args: argparse.Namespace) -> int:
    """`reopen <T-NNNN> --reason <사유>` — 종결(done·discarded)을 open(또는 draft)으로 되돌린다.

    잘못 처리된 종결을 되돌리는 **유일한 문**이다(옛 안내는 "홈 git 으로 revert" 뿐이었다).
    완료·처분 표식(`completed_at`·`disposition*`)과 소유 표식(`claimed_by`·`claimed_at`·
    `claimed_rev`)을 전부 비워 정상 형상으로 되돌리고, 사유·시각을 본문과 board-git 커밋에
    남긴다. 소유 대조는 하지 않는다 — 되돌릴 대상은 이미 소유가 끝난 종결 티켓이고,
    오처리 복구는 그 종결을 낸 세션이 이미 없을 때가 정상이다.

    `discarded_from: draft` 로 기록된 티켓(=discard 직전이 draft 였다)은 `open/` 이 아니라
    `.drafts/` 로 되돌아간다 — draft 본문은 placeholder 게이트를 안 거쳤으므로 open 진입을
    허용하면 board-git 에 미충전 본문이 노출된다(`open/` 승격은 `promote` 전용 문).
    `discarded_from` 부재(기존 자산 전부)는 현행대로 `open/`.
    """
    reason = (getattr(args, "reason", None) or "").strip()
    if not reason:
        print(f"[중단] reopen {args.id}: `--reason <사유>` 가 필수다 — 종결을 되돌린 근거가 "
              "남아야 한다.", file=sys.stderr)
        return 1
    with fs.board_lock(home):
        try:
            status, path = ticket_store.find_ticket_for_mutation(home, args.id)
        except FileNotFoundError as e:
            print(e, file=sys.stderr)
            return 2
        if status not in ticket_rules.TERMINAL_STATUS_DIRS:
            print(f"cannot reopen {args.id}: in {status}/, must be "
                  f"{'/'.join(ticket_rules.TERMINAL_STATUS_DIRS)}", file=sys.stderr)
            return 1
        fm, body = ticket_store.load_ticket(path)
        to_draft = fm.get("discarded_from") == "draft"
        dest_status = "draft" if to_draft else "open"
        new_path = ticket_store.move_ticket(home, path, ".drafts" if to_draft else "open")
        fm["status"] = dest_status
        fm["completed_at"] = None
        fm["claimed_by"] = None
        fm["claimed_at"] = None
        for field in ("claimed_rev", "disposition", "disposition_reason", "discarded_from"):
            fm.pop(field, None)
        # open 진입은 `new`(승격)·`promote` 와 같은 자리에서 묶음에 귀속한다 — 판정 표면
        # (라운드 예산·완료 기록)은 장부 선언만 읽으므로 장부 없이 돌아온 티켓은 위임에서
        # 멈춘다. 필드가 이미 있으면 손대지 않는다: 그 장부가 이 티켓을 담고 있고, 덮으면
        # 그 장부의 선언(status·budget·tickets)이 지워지거나 중복 귀속이 남는다. `.drafts/`
        # 복귀는 귀속하지 않는다 — draft 명세는 공유 board 밖이다(`cmd_new` 와 같은 규칙).
        # 필드가 비었어도 **장부 파일이** 이 티켓을 담고 있으면(구세대 티켓·손으로 만든
        # 장부) 만들지 않고 역참조만 복구한다 — 같은 이름으로 새로 만들면 그 장부의
        # base_branch·status·close_step 이 기본값으로 덮인다(실측).
        cluster_ledger: Path | None = None
        if dest_status == "open" and not str(fm.get("cluster") or "").strip():
            of_record = cluster_store._cluster_of_record(home, args.id)
            if of_record is not None:
                fm["cluster"] = of_record
            else:
                fm["cluster"], cluster_ledger = board_cluster._cluster_auto_attach(home, args.id, args)
        note = f"\n## Reopened\n{reason} — {clock.now_utc()} (이전 상태 {status}/)\n"
        ticket_store.dump_ticket(new_path, fm, body + note)
    board_dashboard.refresh_board(home)
    touched = ((path, new_path) if cluster_ledger is None
               else (path, new_path, cluster_ledger))
    ready = board_sync._board_git_sync_best_effort(
        home, f"reopen {args.id} (from {status}: {reason})", touched)
    print(f"reopened {args.id} (was {status}/): {reason}"
          f"{board_git._board_git_mutation_state_suffix(ready)}")
    return 0


def cmd_new(home, args: argparse.Namespace) -> int:
    # 발행 = 귀속 쓰기(created_by = `<user>/<task>`). `--task` 가 없으면 prefix 판정·draft 생성·
    # ID 발행 어떤 부작용보다 먼저 멈춘다. `--task` 는 정체성 깔때기에서 task 명 검증을 받고,
    # 함께 준 `--repo`/`--slot` 은 그 task 가 보유한 폴더를 고르는 위치 인자일 뿐이다.
    actor_session = task_args._execution_actor_session_override(home, args)
    if actor_session is None:
        sys.exit(task_args.TASK_REQUIRED.format(action="new", command='board.py new "<제목>"'))
    # 명시 --prefix sanity (예약어 `none`·형식 [a-z0-9_]+) — 위반이면 ID 스캔·파일
    # 발행 전에 부작용 0 으로 즉시 거부. 유도/count-based 로 해소된 legacy prefix 는 검증하지
    # 않는다(기존 발행분 존중) — 사용자가 *명시*한 override 만 입력측 sanity 대상이다.
    requested_override = getattr(args, "prefix", None)
    override = requested_override
    if override:
        reason = ticket_rules._validate_prefix(override)
        if reason:
            print(f"[중단] {reason}", file=sys.stderr)
            return 1
    else:
        # 우선순위: --prefix 명시 > task 설정 prefix(`task prefix`·검증·기록) > count-based.
        task_pfx = lease_store.task_prefix(actor_session, (pm_paths.leases_file(home)))
        if task_pfx:
            override = task_pfx
    # 명시/task 값은 우선순위상 이미 최종 후보다. areas-only fold lookup(`id_prefix(override)`)으로
    # 먼저 canonical화하지 않고 아래 **공통 3소스 funnel**에 원 표기 그대로 넘긴다. 그래야
    # 오염된 areas `AAA`/`aaa`의 set 순회가 명시 경로 진단 입력을 다시 비결정적으로 고르지 않는다.
    prefix = override if override else areas_store.id_prefix(home, None)
    prefix_precheck: str | None = None
    prefix_surface = "board new --prefix" if requested_override else "board new derived prefix"
    if prefix is not None:
        # 명시/유도 분기 **뒤의 최종 발행 prefix**가 canonical 판정의 단일 funnel이다.
        # task·단일 areas 유도값도 명시 --prefix와 같은 3소스 snapshot을
        # 통과해야 한다. 여기서 복수 case면 같은 fail-loud 진단으로 막고, 단일 case면 그
        # canonical 표기를 발행값으로 고정한다. 신규 명시값만 user_ack가 실제 의미를 가지며,
        # 유도값은 자신을 제공한 3소스 중 하나에 이미 존재하므로 승인 없이 existing으로 통과한다.
        canonical = areas_registry.require_prefix_user_ack(
            home, prefix,
            getattr(args, "user_ack", None),
            surface=prefix_surface,
        )
        if canonical is None:
            return 1
        prefix = canonical
        prefix_precheck = canonical
    # multi-repo 네임스페이스 가드는 **레지스트리 *존재*가 아니라 등록 repo *개수*** 기준이다.
    # 등록 prefix 가 ≥2 면 진짜 ID 충돌 가능성이 있으니 prefix 필수(namespace 강제). 등록이
    # ≤1(0=레지스트리 부재/빈·1=단일 self-host) 이면 충돌이 없으므로 무prefix legacy `T-NNNN` 을
    # 허용한다(prefix optional) — 단일 self-host 가 areas.md 1행만으로 multi-PM 마찰을 떠안지
    # 않게(단일 등록 repo 케이스). 명시 prefix 가 *주어지면* 그건 그대로
    # 존중해 prefixed ID 를 발행한다 — ≤1 라도 사용자가 골랐으면 따른다.
    #
    # **명시 prefix 의 "등록돼 있어야 한다" 가드는 없다**. prefix 는 repo
    # 네임스페이스가 아니라 작업 카테고리이므로 미등록 라벨도 사용자가 그 대상값을
    # `--user-ack`으로 승인하면 즉석 신설할 수 있다. 입력측 sanity와 신설 승인은 위에서
    # `_validate_prefix` + `require_prefix_user_ack` 두 단계로 닫히며, 기존 fold-매치는 무마찰이다.
    # 아래 ≥2 가드는 별개 — 등록 repo 가 ≥2 인데 prefix 를 *안* 준 implicit 모호성 방지다
    # (유도 체인·미해소 fail-loud).
    registered = areas_store.registered_prefixes(home)
    if len(registered) >= 2:
        if not prefix:
            print("multi-repo 네임스페이스 모드(등록 repo ≥2) — prefix 필요(미해소). "
                  "`--prefix <PFX>` 로 명시하거나 task prefix 를 지정하라"
                  "(`pm-config task prefix <task> <PFX>`). 새 카테고리가 필요하면 1순위로 사용자에게 "
                  "그 prefix 승인을 요청하라. 승인한 사용자만 부차적으로 "
                  "`board.py init --prefix <PFX> --area <name> --user-ack <PFX>`를 "
                  "대상값에 결속해 실행한다(세션 자동 부착 금지).",
                  file=sys.stderr)
            return 1

    # 설계 단계 기본값 — 명시 `--design` 우선, 무명시는 estimate=large → required · 그 외 n/a
    # 값 형식은 발행 시점에 fail-loud 로 검증한다. 잘못된 값이 티켓에 박히면
    # claim 게이트가 나중에야 걸린다.
    design = design_rules._resolve_design(getattr(args, "design", None),
                             getattr(args, "estimate", None))
    design_reason = design_rules._validate_design(design)
    if design_reason:
        print(f"[중단] {design_reason}", file=sys.stderr)
        return 1

    tmpl_fm, tmpl_body = ticket_store.load_ticket(pm_paths.template_file(home))
    # ID 발행 전에는 템플릿의 ID로 본문 경로만 판정한다.
    draft_lint_id = str(tmpl_fm.get("id") or "")
    draft_body = tmpl_body.replace("T-NNNN", draft_lint_id).replace("<제목>", args.title)
    is_draft = board_git._board_git_enabled(home) and bool(
        body_lint._body_lint_issues(home, draft_lint_id, draft_body, strict_sections=True, design=design))
    # 코드 없는 task draft는 허용한다. 위치 인자(`--repo`/`--slot`)를 줬으면 draft에서도 검증한다.
    binding_required = (not is_draft
                        or getattr(args, "repo", None) or getattr(args, "slot", None))
    binding_message = ("[중단] 발행의 코드 트리와 기준 브랜치를 task 폴더에서 읽지 못했다 "
                       "(`--task <task>`, task 의 폴더가 2개 이상이면 `--repo <repo> --slot <N>` 추가). "
                       "코드 없는 초안은 draft로 작성한 뒤 기존 cluster new로 결속하라.")
    if binding_required:
        tree, base = board_cluster._cluster_publish_base_branch(home, args)
        if not tree or not base:
            print(binding_message, file=sys.stderr)
            return 1

    # 발행 규율 게이트: board-git 이 공유(별도 git·submodule) 상태일 때만
    # 의미가 있다 — 미충전 stub 이 board-git 에 커밋돼 다른 slot 의 handoff/bootstrap 을
    # 오염시키는 게 문제이므로, board 가 별도 git 이 아니면(legacy)
    # 게이트 없이 기존처럼 즉시 open/ 에 발행한다. 별도 git 이면 본문을 *쓰기 전에* 미리
    # 검사(`_body_lint_issues` — `lint_bodies` 와 동일 로직)해 placeholder/thin 이 남아있으면
    # `open/` 이 아니라 `drafts_dir()`(STATUS_DIRS 밖)에 쓴다 — draft 가 STATUS_DIRS
    # 순회 대상이 되는 창(open/ 에 잠깐이라도 존재)이 아예 없어야, 이후의 **어떤** mutation
    # (자기 자신의 board-git sync 뿐 아니라 무관한 claim/complete/promote 등)도 draft 를
    # board-git 에 잘못 쓸어담을 수 없다
    # board-git 에 아예 안 보이는 게 핵심).
    # `list`/`show`/`promote` 는 `find_ticket`/`drafts_dir()` 로 draft 를 계속 인지한다.
    #
    # ID 발행(`_next_id` = max+1·동시 발행 race)과 파일 생성을 단일 락으로 직렬화한다
    # 락 안에서 ID 를 *읽고* 곧바로 파일을 만들어, 다른 세션이 같은 ID 를
    # 발행할 틈을 없앤다. board.md 재생성은 락 밖(별도 트랜잭션 — 파생물).
    with fs.board_lock(home):
        if prefix is not None:
            # 모든 non-None 발행 prefix를 ID 발행 락 안에서 cache-free 3소스 snapshot으로
            # 다시 확인한다. 명시/유도 어느 경로든 그사이 다른 case가 생기면 `_next_id`와
            # 파일 write 전에 중단한다.
            fresh = areas_registry.revalidate_prefix_user_ack(
                home, prefix,
                getattr(args, "user_ack", None),
                prefix_precheck,
                surface=prefix_surface,
            )
            if fresh is None:
                return 1
            prefix = fresh
        tid = ticket_store._next_id(home, prefix)
        slug = ticket_rules._slugify(args.title)
        filename = f"{tid}-{slug}.md"

        # Replace placeholder tokens in body
        body = tmpl_body.replace("T-NNNN", tid).replace("<제목>", args.title)
        # lint 판정은 `tid` 치환과 무관(placeholder/section 검사가 `T-NNNN` 자체를 안 봄) —
        # 발행 전에 판정해 쓰기 경로(open/ vs drafts_dir())를 정한다(open/ 창 노출 0).
        is_draft = board_git._board_git_enabled(home) and bool(
            body_lint._body_lint_issues(home, tid, body, strict_sections=True, design=design))

        fm: dict[str, Any] = dict(tmpl_fm)
        fm["id"] = tid
        fm["title"] = args.title
        fm["status"] = "draft" if is_draft else "open"
        fm["created"] = datetime.date.today().isoformat()
        # created_by = `<user>/<task>` (provenance·불변·생성 시 set·세션 기본 뷰 스트림 판정 입력).
        # "누가 추가했나" = 중복-작업 방지의 출처 표식. user 미상이면 task 만(graceful).
        fm["created_by"] = lease_store.identity_tag(home,
            session_override=actor_session,
            user_override=getattr(args, "user", None))
        fm["claimed_by"] = None
        fm["claimed_at"] = None
        fm["completed_at"] = None
        fm["touches"] = (args.touches.split(",") if args.touches else [])
        fm["depends_on"] = (args.depends.split(",") if args.depends else [])
        fm["blocks"] = []
        fm["tags"] = (args.tag.split(",") if args.tag else [])
        fm["estimate"] = args.estimate
        # 설계 단계 상태 — 템플릿에 이미 `design:` 이 있으면 그 자리를, 없으면(구 템플릿) 끝에
        # 붙는다. 값 자체는 위에서 해소·검증했다.
        fm["design"] = design
        # 운영 단위 — 새 티켓은 자기 크기 1 장부를 갖는다(기준 브랜치·예산은 그 자리에서
        # 박는다). **draft 는 미룬다**: 장부는 board-git 공유인데 draft 명세는 격리(공유 밖)라,
        # 지금 멤버로 올리면 다른 클론에는 없는 티켓을 가리키는 장부가 공유된다. 승격
        # 전에 기존 cluster new로 제품 브랜치와 멤버를 명시 결속한다.
        cluster_ledger: Path | None = None
        # 마지막 제품 판정과 기록에 같은 결과를 사용한다. helper의 재조회로 바꾸지 않는다.
        final_binding_required = (not is_draft
                                  or getattr(args, "repo", None) or getattr(args, "slot", None))
        if not is_draft or final_binding_required:
            publish_basis = board_cluster._cluster_publish_base_branch(home, args)
            if final_binding_required and (not publish_basis[0] or not publish_basis[1]):
                print(binding_message, file=sys.stderr)
                return 1
        if not is_draft:
            fm["cluster"], cluster_ledger = board_cluster._cluster_auto_attach(home, tid, args, publish_basis=publish_basis)

        if is_draft:
            pm_paths.drafts_dir(home).mkdir(parents=True, exist_ok=True)
            path = pm_paths.drafts_dir(home) / filename
        else:
            path = pm_paths.tickets_dir(home) / "open" / filename
        ticket_store.dump_ticket(path, fm, body)
    areas_registry.invalidate_known_prefixes_cache()

    print(f"created {tid} ({pm_paths._rel_to_repo(home, path)})")
    print("  → fill in 목표 / 완료 조건 / 참고, then `board.py lint --gate` "
          "(placeholders left in the body fail lint)")
    if cluster_ledger is not None:
        # 기존 stdout 줄은 그대로 두고 생성 사실만 stderr 1줄로 고지한다(크기 1 = 현행 동작).
        print(f"  ⓘ 크기 1 묶음 장부 생성: {fm['cluster']}", file=sys.stderr)

    if is_draft:
        # draft 는 STATUS_DIRS 밖(drafts_dir())에 있어 board.md(STATUS_DIRS 스캔)에도, 다른
        # slot 의 board-git pull/handoff 에도 나타나지 않는다 — 로컬 `board.py show <id>` 로만
        # 조회 가능(`find_ticket` 이 drafts_dir() 폴백으로 찾는다). draft **파일**은 기존 금지
        # pathspec 으로 계속 커밋하지 않되, sync funnel 은 한 번 태운다. 그래야 `.gitignore` 없는
        # board 의 첫 mutation 이 바로 draft 인 경우에도 루트 backfill 이 commit/push 되어, 방금
        # 만든 draft 가 handoff dirty 관측에 untracked 로 남는 창이 없다.
        print(f"  ⚠ draft — board-git 미커밋(공유 board 오염 방지): "
              f"미충전(placeholder/thin) 본문. 본문을 채운 뒤 "
              f"`board.py promote {tid}` 로 승격(open/ 이동 + board-git 커밋)하라.",
              file=sys.stderr)
    else:
        board_dashboard.refresh_board(home)
    sync_needed = not is_draft or board_git._board_git_root_files_need_backfill(home)
    ready = True
    if sync_needed:
        sync_label = f"new {tid} draft isolation" if is_draft else f"new {tid}"
        sync_paths = (path,) if cluster_ledger is None else (path, cluster_ledger)
        ready = board_sync._board_git_sync_best_effort(home, sync_label, sync_paths)
    if not ready:
        label = "draft 격리 기록" if is_draft else "기록"
        print(f"  ⚠ board-git {label} 보류: local-only/uncommitted", file=sys.stderr)
    # 발행 시점 판단 재료 — 겹침 0 이면 전부 침묵. 차단 없음(rc 불변).
    # 슬롯 재료의 repo 는 이 발행의 위치 인자 `--repo` 다 — REPO.name(물리 폴더명)과 다른
    # 등록명을 공식 지원하므로 그 값을 재사용한다(첫 줄의 실행 명령 판정 함수가 이미 검증한 args).
    new_repo = identity_args.parse_identity(args).repo
    for line in cluster_binding._publish_overlap_material(home, tid, fm.get('touches'), new_repo or (home.root).name):
        print(line, file=sys.stderr)
    return 0


def cmd_promote(home, args: argparse.Namespace) -> int:
    """draft(drafts_dir() 격리·board-git 미커밋) 티켓을 승격 — 재검사 후 open/ 이동 + board-git sync.

    `board.py new` 가 생성 시점 게이트로 `drafts_dir()`(STATUS_DIRS 밖)에 남긴
    draft 를, 본문을 채운 뒤 이 명령으로 승격한다. 여전히 placeholder/thin 이 남아있으면
    거부(rc=1)하고 남은 이슈를 안내한다(파일은 drafts_dir() 에 그대로 — 재수정 후 재시도).
    형식(placeholder/thin/설계 절)에 더해 본문이 **주장하는 사실**도 이 자리에서 판정한다
    (`content_facts=True` — repo-relative `파일:줄` 인용의 부재·범위 초과와 `design:
    required|done` 티켓의 architect 점검 라운드 미회수는 차단, touches 미해소와 판정불능
    개수는 경고 1줄). 판정 대상은 승격되는 티켓 하나이며 기존 open 티켓을 소급하지 않는다.
    통과해야 `open/` 으로 옮겨져 STATUS_DIRS 스캔 대상이 되고 board-git 에 커밋돼 공유 board
    에 존재하게 된다(생성~claim 사이 handoff 창에 미충전 stub 이 인계되는 실패를 원천 차단).
    legacy board에서는 본문 게이트와 sync가 no-op이다. 제품 branch/base와 멤버 결속은
    공유 여부와 관계없이 검증한다.
    """
    moved = False
    with fs.board_lock(home):
        try:
            status, path = ticket_store.find_ticket_for_mutation(home, args.id)
        except FileNotFoundError as e:
            print(e, file=sys.stderr)
            return 2
        if status not in ("open", "draft"):
            print(f"cannot promote {args.id}: currently in {status}/ (promote 는 open/draft 만)",
                  file=sys.stderr)
            return 1
        fm, body = ticket_store.load_ticket(path)
        if board_git._board_git_enabled(home):
            issues = body_lint._body_lint_issues(home, args.id, body, strict_sections=True,
                                       design=fm.get("design"),
                                       touches=fm.get("touches"), content_facts=True)
            # 경고 축(`_PROMOTE_ADVISORY_KINDS`)은 승격을 막지 않는다 — 차단 여부와 무관하게
            # 먼저 표면화해, 거부된 경우에도 같은 1줄을 본다.
            advisories = [i for i in issues if i[1] in body_lint._PROMOTE_ADVISORY_KINDS]
            for tid, kind, detail in advisories:
                print(f"  ⚠ [{kind}] {detail}", file=sys.stderr)
            remaining = [i for i in issues if i[1] not in body_lint._PROMOTE_ADVISORY_KINDS]
            if remaining:
                print(f"promote 거부 — {args.id} 에 아직 미충전 {len(remaining)}건:",
                      file=sys.stderr)
                for tid, kind, detail in remaining:
                    print(f"  ✗ [{kind}] {detail}", file=sys.stderr)
                return 1
        # 스코프= 이 promote 가 만진 경로. draft 쪽 옛 경로는 `_board_git_scope_pathspec`
        # 이 걸러낸다.
        touched: list[Path] = [path]
        final_path = path
        # 공유 전에 기존 cluster new의 제품·멤버 결속을 읽기로 검증한다.
        # 선언된 장부와 티켓 cluster 값은 승격이 재작성하지 않는다.
        if board_git._board_git_enabled(home):
            problem = cluster_binding.cluster_binding_problem(home, cluster_rules.ticket_cluster(args.id, fm))
            if problem is None:
                problem = cluster_binding.cluster_complete_binding_problem(home, args.id, fm, str(fm.get('cluster') or ''))
            if problem:
                print(f"[중단] promote 결속 검증: {problem}", file=sys.stderr)
                return 1
        if status == "draft":
            # drafts_dir() → open/ 이동 — 이제서야 STATUS_DIRS 스캔·board-git 대상이 된다.
            # status dump와 rename도 같은 lock 안이라 claim/tier writer가 중간 형상을 못 본다.
            fm["status"] = "open"
            ticket_store.dump_ticket(path, fm, body)
            new_path = ticket_store.move_ticket(home, path, "open")
            touched.append(new_path)
            final_path = new_path
            moved = True
        # draft 의 라운드도 promote 가 출하한다 — draft 명세는 board-git ignore 지만
        # `tickets/<라운드 디렉터리>/<티켓>/` 은 ignore 밖이라, 승격 커밋에 함께 싣지 않으면
        # 준비된 라운드가 미커밋으로 눌러앉는다(스코프 전개는 디렉터리를 파일로 펼친다).
        round_dir = round_store.rounds_dir_for_ticket(args.id, pm_paths.tickets_dir(home))
        if round_dir.is_dir():
            touched.append(round_dir)
    if moved:
        board_dashboard.refresh_board(home)
    ready = board_sync._board_git_sync_best_effort(home, f"promote {args.id}", touched)
    if not board_git._board_git_enabled(home):
        # board 가 홈 git 안이면 board-git 커밋도 공유도 없다 — 실제로 한 일(이동 여부)만 말한다.
        placement = "draft → open/" if moved else "open/ 그대로"
        print(f"promoted {args.id} ({placement} · board 가 홈 git 안이라 커밋 없음)")
    elif ready:
        print(f"promoted {args.id} (board-git 승격 완료)")
    else:
        print(f"promoted {args.id} (board-git 기록 보류: local-only/uncommitted)")
    # 발행 시점 판단 재료 — 성공 경로 세 갈래(홈 git board/승격 완료/기록 보류) 공통 뒤,
    # `_board_git_enabled()` 게이트 분기 밖(솔로/legacy 형상에서도 나온다). 겹침 0 이면 침묵.
    # `promote` 는 `--repo`/`--slot` 인자가 없다 — 슬롯 재료의 repo 는 이 티켓의 provenance
    # (`created_by`)에서 세션 토큰을 뽑아(`_created_by_session` — 기존 세션-뷰 seam) 장부
    # 등록 repo 로 재해소한다(`lease_store.session_coordinates` — 행이 있으면 행의 repo,
    # 없으면 세션명 키 분해로 폴백). 둘 다 기존 seam 재사용 — 새 해소 축 아님.
    promote_repo, _promote_slot_number = lease_store.session_coordinates(
        ticket_rules._created_by_session(fm.get("created_by")), (pm_paths.leases_file(home)))
    for line in cluster_binding._publish_overlap_material(home, args.id, fm.get('touches'), promote_repo or (home.root).name):
        print(line, file=sys.stderr)
    return 0


_IDEA_BODY_TEMPLATE = """# Idea-{iid} — {title}

## 한 줄 요약

<무엇을 / 왜 끌리는가 1~2 문장>

## 동기

- <왜 이 idea 에 끌리는가>

## 가능한 구현 형태 (high-level)

- <high-level 구현 방향 — 어느 모듈/계층에, 어떤 형태로>

## 위험 / 고민거리

- <검토할 위험>

## 열린 질문

- [ ] <답이 필요한 질문>

## 다음 행동

- promote 기준 / kill 기준 / 어떻게 익힐지

## 관련 링크

- [[xxxxx]]
"""


def cmd_idea_list(home, args: argparse.Namespace) -> int:
    rows: list[tuple[str, dict]] = []
    for status in ticket_rules.IDEA_STATUS_DIRS:
        if args.status and args.status != status:
            continue
        for p in sorted(((pm_paths.ideas_dir(home)) / status).glob("[0-9]*.md")):
            loaded = ticket_store.load_ticket_soft(home, p)
            if loaded is None:
                continue
            fm, _ = loaded
            if args.tag and args.tag not in ticket_rules._tag_values(fm):
                continue
            rows.append((status, fm))
    if not rows:
        print("(no ideas)")
        return 0
    for status, fm in rows:
        tags = ",".join(ticket_rules._tag_values(fm))
        iid = str(fm.get("id") or "")
        title = (fm.get("title") or "")[:60]
        print(f"  [{status:8s}] {iid:6s} {title:60s}  {tags}")
    return 0


def cmd_idea_new(home, args: argparse.Namespace) -> int:
    iid = ticket_store._next_idea_id(home)
    slug = ticket_rules._slugify(args.title)
    filename = f"{iid}-{slug}.md"

    today = datetime.date.today().isoformat()
    fm: dict[str, Any] = {
        "id": iid,
        "title": args.title,
        "created": today,
        "updated": today,
        "type": "idea",
        "status": "open",
        "tags": (args.tag.split(",") if args.tag else []),
    }
    body = "\n" + _IDEA_BODY_TEMPLATE.format(iid=iid, title=args.title)

    path = (pm_paths.ideas_dir(home)) / "open" / filename
    ticket_store.dump_ticket(path, fm, body)
    print(f"created idea {iid} ({pm_paths._rel_to_repo(home, path)})")
    print("  → fill in 한 줄 요약 / 동기 / 위험 / 다음 행동")
    return 0


_IDEA_TRANSITION_VERB = {"promoted": "promote", "killed": "kill"}


def _transition_idea(home, iid: str, dst_status: str) -> int:
    """Atomic mv open/ → dst_status/ + frontmatter status sync.

    Shared by `idea promote` and `idea kill` — the only transitions ideas
    support. Both move out of `open/` only.
    """
    verb = _IDEA_TRANSITION_VERB[dst_status]
    try:
        status, path = ticket_store.find_idea(home, iid)
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        return 2
    if status != "open":
        print(f"cannot {verb} idea {iid}: currently in {status}/",
              file=sys.stderr)
        return 1
    fm, body = ticket_store.load_ticket(path)
    new_path = ticket_store.move_idea(home, path, dst_status)
    fm["status"] = dst_status
    fm["updated"] = datetime.date.today().isoformat()
    ticket_store.dump_ticket(new_path, fm, body)
    print(f"{dst_status} idea {iid} ({pm_paths._rel_to_repo(home, new_path)})")
    return 0


def cmd_idea_promote(home, args: argparse.Namespace) -> int:
    return _transition_idea(home, args.id, "promoted")


def cmd_idea_kill(home, args: argparse.Namespace) -> int:
    return _transition_idea(home, args.id, "killed")


def _sidecar_document(path: Path) -> dict | None:
    """기계 기록 sidecar 한 개를 읽는다 — 부재·손상·비-dict 는 None(판정 없음)."""
    try:
        data = json.loads(fs.read_text_shared(path, encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None
