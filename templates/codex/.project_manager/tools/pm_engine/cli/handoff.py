"""핸드오프 실행 순서와 CLI."""
from __future__ import annotations

from typing import Callable
from pathlib import Path
import argparse
from pm_engine.domain import dashboard
import datetime
from pm_engine.infra import fs
from pm_engine.cli import handoff_dirty
from pm_engine.infra import handoff_git
from pm_engine.app import handoff_log
from pm_engine.cli import handoff_prompt
from pm_engine.domain import handoff_text
from pm_engine.domain import identity_args
from pm_engine.infra import lease_store
from pm_engine.infra import log_store
from pm_engine.infra import pm_paths
from pm_engine.infra import pool_store
from pm_engine.infra import python_env
from pm_engine.infra import raw_output
from pm_engine.app import regression
from pm_engine.domain import session_history
from pm_engine.app import session_identity
from pm_engine.infra import session_store
from pm_engine.app import slot_resolution
from pm_engine.infra import slot_snapshot
import subprocess
import sys
from pm_engine.app import task_lifecycle
from pm_engine.infra import task_store
from pm_engine.domain import worktree_records
from pm_engine.app import worktree_status


class PmHandoff:
    """PM 핸드오프 7단계 자동화 핵심 로직.

    subprocess 함수를 DI 해 테스트에서 실제 실행 없이 결정론적으로 검증한다.
    ticket_finish.py 의 TicketFinisher DI 패턴과 동일.
    """

    def __init__(self, home, *, run_pytest_fn: Callable[[], tuple[int, str]] | None=None, run_git_fn: Callable[[list[str]], tuple[int, str]] | None=None, log_file: Path=None, pm_playbook_file: Path=None, pm_state_file: Path | None=None, dashboard_file: Path | None=None, venv_python: str | Path=None, raw_ledger_file: Path | None=None, peer_raw_ledger_files: tuple[Path, ...] | None=None) -> None:
        self._home = home
        log_file = log_file if log_file is not None else pm_paths.wiki_dir(home) / 'log/current.md'
        pm_playbook_file = pm_playbook_file if pm_playbook_file is not None else pm_paths.wiki_dir(home) / 'pm_playbook.md'
        venv_python = venv_python if venv_python is not None else python_env._default_python(home)
        self._log_file = log_file
        self._pm_playbook_file = pm_playbook_file
        # 미마감 raw sweep([6b/7]) 장부 seam — 명시(hermetic 테스트)면 그 경로, None(프로덕션)이면
        # sweep 시점에 raw_output 의 기본 위치(`.project_manager/.local/raw_outputs.json`)로 해소한다.
        self._raw_ledger_file = raw_ledger_file
        # 사본 장부 병기 seam — 명시(hermetic 테스트)면 그 목록(`()` = 사본 없음), None(프로덕션)
        # 이면 sweep 시점에 위임 조회면 판정(`raw_output._peer_engine_ledgers`)으로 해소한다.
        self._peer_raw_ledger_files = peer_raw_ledger_files
        # slot 대시보드 seam — 자기 섹션 overwrite 대상. 명시(hermetic 테스트)면
        # 그 경로, None(프로덕션)이면 write 시 `_dashboard_file()`(호출 시점 REPO 추종)로 해소한다.
        self._dashboard_file = dashboard_file
        # pm_state 는 *슬롯별* — 명시 주입(테스트/override)이 있으면
        # 그 경로 고정, 미지정(None·프로덕션)이면 run() 진입부에서 활성 슬롯을
        # 해소해 `_pm_state_path` 로 per-slot 경로를 세팅한다(초기값은 마이그레이션 원본 경로일
        # 뿐 해소 결과가 아니다). 명시 주입 여부를 기억해 per-slot 재해소가 hermetic 테스트의
        # 명시 경로를 덮지 않게 한다.
        # 명시 주입 여부를 기억해 run() 의 task pm_state 해소가 hermetic 테스트의 명시 경로를
        # 덮지 않게 한다.
        self._pm_state_file_explicit = pm_state_file is not None
        self._pm_state_file = pm_state_file
        self._venv_python = venv_python
        # worktree_pool seam — 테스트는 mock 모듈을 주입(hermetic). None 이면 소비 지점에서
        # 동적 로드한다.
        # 슬롯별 회귀가 도는 동안의 cwd 앵커 — `_run_regression_for_slot` 만 세팅하고 즉시
        # 원복한다. 세션 정체성이 아니다(정체성은 task 하나다).
        self._regression_slot: str | None = None
        # task handoff 시작 시 영속 변경 전에 한 번 해소한 보유 슬롯 스냅샷. 회귀/출하/재스냅이
        # 같은 집합을 재사용해 후반 재조회 실패가 이미 기록된 handoff를 false-green으로 끝내지 않게 한다.
        self._task_slots_snapshot: tuple[str, tuple[str, ...]] | None = None
        # membership gate가 읽은 task record 스냅샷. 정상 handoff가 종료 기록을 남긴 뒤
        # bootstrap 없이 같은 CLI를 재실행했는지 차수 추론에서 구분한다.
        self._task_record_snapshot: tuple[str, object] | None = None

        self._run_pytest_fn = run_pytest_fn or self._default_run_pytest
        self._run_git_fn = run_git_fn or self._default_run_git

    # ── 핸드오프 완료: 보유 슬롯 git 재스냅 ("여기 두고 간다") ───────────────

    def _record_slot_snapshot(self, slot: str, *, task: str | None = None) -> bool:
        """bound 슬롯의 live git 을 `lease.git` 에 재기록한다 — "여기 두고 간다".

        핸드오프 기록(log·pm_state) 완료 후, 슬롯의 현재 branch/HEAD 를 리스 장부에 재스냅해
        차기 부트스트랩 0단계 record-vs-live 정합(`compare_slot_git`)이 보는 *도착 스냅* 을
        갱신한다. 세션 중 브랜치/HEAD 가 바뀌면(예: 릴리즈 v1.3.2→v1.3.3) bind 의 옛 도착 스냅만
        남아 0단계가 `diverged` FAIL-LOUD 로 정당한 자기 진행을 외부-개입 오경보로 차단하기
        때문이다.

        write 프리미티브 `slot_snapshot.record_git_snapshot(slot)` 만 호출한다 — 브랜치 부모
        기록은 git 설정에 있어 건드리지 않는다·판정 로직 재구현 없음. 슬롯 미바인딩/장부 부재(record None)는
        무해 skip, 스냅 예외는 전파한다.

        **실갱신 vs 무변경 구분 출력(dual-gate suggestion)**: `record_git_snapshot` 은 슬롯
        live 스냅이 불가하면(worktree 경로 부재 등) 기존 `lease.git` 을 clobber 하지 않고 그대로
        둔다(`_apply_git_snapshot` no-op·silent 손실 방지). 그럴 때 옛 branch/head 를 "✓ 기록"으로
        내면 갱신 안 됐는데 성공처럼 읽힌다 — 재스냅 *전* lease.git(`read_lease`)과 *후* 값을 비교해
        실제 바뀌었을 때만 "재스냅 기록", 무변경이면 "스냅 불가·기존 유지"로 구분 출력한다."""

        if task is not None:
            try:
                owned = bool(pool_store.lease_owned_by_task_strict(self._home, slot, task))
            except (OSError, ValueError) as exc:
                print(f"  [중단·소유권 재검증] task 슬롯 {slot!r} 장부 조회 실패 — {exc}. "
                      "git 재스냅을 실행하지 않습니다.", file=sys.stderr)
                return False
            if not owned:
                print(f"  [중단·소유권 재검증] task 슬롯 {slot!r}은 더 이상 "
                      f"state=leased && session={task!r}가 아닙니다 — git 재스냅 중단. "
                      "새 소유자의 lease.git을 덮어쓰지 않습니다.", file=sys.stderr)
                return False
        before_git = self._lease_git_before(slot)
        lease = slot_snapshot.record_git_snapshot(self._home, slot)
        if lease is None:
            print(f"  ⚠ slot {slot!r} 리스 장부에 없음 — git 재스냅 skip(무해).", file=sys.stderr)
            return True
        after_git = lease.git if isinstance(lease.git, dict) else None
        if after_git is not None and after_git != before_git:
            print(
                f"  ✓ git 재스냅 기록: {slot} → "
                f"branch=`{after_git.get('branch')}` head=`{after_git.get('head')}` "
                "(실갱신·여기 두고 간다)"
            )
        else:
            # 무변경 — 슬롯 live 스냅 불가(worktree 경로 부재 등)로 기존 기록 유지(clobber 방지·무해).
            print(
                f"  · git 재스냅 무변경: {slot} — 슬롯 live 스냅 불가·기존 기록 유지"
                "(스냅 불가·차기 0단계는 기존 스냅 기준·무해)."
            )
        return True

    def _lease_git_before(self, slot: str) -> "dict | None":
        """재스냅 *전* 슬롯 lease.git 을 조회한다 — 실갱신/무변경 판별용.

        worktree_pool `read_lease`(순수 장부 read·`record_git_snapshot` 짝)로 현재 스냅을 읽는다.
        미기록은 None(→ 후 값이 dict 면 실갱신으로 취급). 조회 실패는 전파한다."""
        lease = pool_store.read_lease(self._home, slot)
        if lease is None:
            return None
        git = getattr(lease, "git", None)
        return git if isinstance(git, dict) else None

    # ── task 퇴장: 보유 슬롯 집합 열거·변경 판정·전 슬롯 재스냅 ──────

    def _task_held_slots(self, task: str) -> list[str]:
        """task(session==name) 명의로 leased 인 슬롯 식별자 리스트 — `slots_for_task` 재사용 (조회 전용).

        퇴장 의미론: task 세션은 보유 슬롯 **집합**을 두고 나간다. `worktree_pool.
        slots_for_task(task)`(tasks 장부 조인·session==task 이고 leased 인 슬롯)를 소비해
        보유 집합을 열거한다(재열거 로직 재구현 없음). run()이 영속 변경 전에 한 번 성공적으로
        해소한 스냅샷은 회귀/출하/재스냅 전 단계가 공유한다. worktree_pool/slots_for_task 부재,
        None 반환, 예외, 슬롯 식별자 없는 행은 **해소 실패**로 raise한다. 실제 `[]`만 정당한
        0슬롯이다."""
        if self._task_slots_snapshot is not None and self._task_slots_snapshot[0] == task:
            return list(self._task_slots_snapshot[1])

        # strict 장부 API로 손상·읽기 오류를 실제 0슬롯과 구분한다.
        try:
            resolved = pool_store.slots_for_task_strict(self._home, task)
            if resolved is None:
                raise RuntimeError("slots_for_task가 None을 반환")
            leases = list(resolved)
        except Exception as exc:
            raise RuntimeError(f"slots_for_task({task!r}) 조회 실패: {exc}") from exc
        slots: list[str] = []
        for lease in leases:
            slot = getattr(lease, "slot", None)
            if not slot:
                raise RuntimeError("slots_for_task 결과에 slot 식별자 없는 행 존재")
            slots.append(str(slot))
        return slots

    def _slot_has_changes(self, slot: str) -> tuple[bool, str]:
        """슬롯이 도착/직전 스냅(`lease.git`) 대비 변경 흔적(head 전진 또는 dirty)이 있는지.

        반환 `(changed, reason)` — reason 은 출력용 사유 문구. 판정("변경 흔적 있는
        보유 슬롯만 회귀"):
          - 스냅 미기록(`unrecorded`) → 보수적으로 changed(직전 green 근거가 없으니 회귀 포함).
          - branch 변경(`not branch_match`) 또는 head 가 match 아님(descendant/diverged/unknown) →
            changed(세션 중 커밋 전진/리셋).
          - dirty(미커밋 변경) → changed(compare 는 head 만 봐 커밋 안 한 작업을 못 잡는다).
          - head match + branch match + clean → **unchanged**(직전 green 불변·회귀 신호 0·skip).
        판정 프리미티브는 재구현하지 않고 worktree_pool `compare_slot_git`(도착 스냅 vs live)+
        `slot_git_status`(dirty)를 소비한다. 조회 예외는 전파한다."""

        cmp = slot_snapshot.compare_slot_git(self._home, slot)
        if getattr(cmp, "unrecorded", False):
            return True, "스냅 미기록·보수적 변경 취급"
        if not getattr(cmp, "branch_match", False):
            return True, "branch 변경"
        if getattr(cmp, "head_relation", None) != worktree_records.HEAD_MATCH:
            return True, f"head {getattr(cmp, 'head_relation', None)}(전진/재설정)"
        dirty = bool(worktree_status.slot_git_status(self._home, slot).get("dirty", False))
        if dirty:
            return True, "dirty(미커밋 변경)"
        return False, "head match·clean"

    def _record_task_slot_snapshots(self, task: str, dry_run: bool) -> bool:
        """task 보유 **전 슬롯**의 live git 을 `lease.git` 에 재스냅한다 — "집합 전체 두고 간다".

        현행 1슬롯 한정(`_record_slot_snapshot` 단일 호출)을 폐지하고, task 가 보유한 leased 슬롯
        전수(`_task_held_slots`·slots_for_task)를 루프로 재스냅한다. 각 슬롯은 프리미티브
        (`_record_slot_snapshot`)를 그대로 재사용(판정 재구현 없음). 보유 0개면
        명시 skip. dry_run 은 슬롯별 예고만."""
        slots = self._task_held_slots(task)
        print("\n[재스냅] task 보유 슬롯 git 재스냅 (집합 전체 두고 간다)...")
        if not slots:
            print(f"  · task {task!r} 보유 슬롯 0개 — 재스냅 대상 없음(무해 skip).")
            return True
        for slot in slots:
            if dry_run:
                print(f"  [dry-run] git 재스냅 예고: {slot} (실행 생략).")
            else:
                if not self._record_slot_snapshot(slot, task=task):
                    return False
        return True

    def _close_session_records(self, task: str) -> bool:
        """이 세션이 연 기록을 **한 자리에서** 전부 닫는다 — 성공 여부 반환.

        핸드오프 기록(log·pm_state) 전에 호출한다. 세션이 끝나는 자리에서 그 세션이 연 기록을
        닫지 않으면 같은 사실이 두 곳에서 다르게 읽힌다 — 다음 진입이 그 자리를 "열려 있음"으로
        보고 매번 강제 확인을 요구한다. 기존 write 프리미티브 하나만 부른다 —
        `task_lifecycle.close_task_session(task)` → 장부 task 행 `session` 비움.
        **대여는 그대로 둔다** — 슬롯 반납(`pm-config release`)과는 다른 축이다. 닫히는 것은
        "지금 세션이 열려 있다"는 표시뿐이고 작업 폴더는 두고 간다.

        그 task 가 보유한 슬롯 행의 session 은 **task 이름**(소유 축)이라 건드리지 않는다 —
        그 세션의 열림/닫힘은 task 행 하나가 말한다.

        같은-session 재실행 판정이 이 durable 신호에 의존하므로 더는 보조 fail-soft 기록이 아니다.
        step2 log write **전**에 성공해야 한다. record 부재는 [중단](False), 기록 예외는 [중단] 문구 뒤 전파한다.
        성공 뒤 후속 단계가 중단돼도 종료 기록이 남아 재실행이 같은 차수로 복구된다."""

        try:
            task_rec = task_lifecycle.close_task_session(self._home, task)
        except Exception as exc:  # noqa: BLE001 — load-bearing intent 기록 실패는 차단.
            print(f"  [중단] task 종료 기록 실패 — {exc}.", file=sys.stderr)
            raise
        if task_rec is None:
            print(f"  [중단] task {task!r} 장부에 없음 — 멱등성 신호를 기록하지 못했다.",
                  file=sys.stderr)
            return False
        print(
            f"  ✓ task 종료 기록: {task} → 닫힘 "
            "(재실행=같은 차수·다음 bootstrap=재개/N+1)"
        )
        return True

    # ── 기본 subprocess 구현 (실제 실행) ──────────────────────────────────────

    def _default_run_pytest(self) -> tuple[int, str]:
        """회귀를 실행해 (returncode, stdout+stderr) 반환.

        명령 해소(ticket_finish `_default_run_pytest` 와 동형):
          - **해소 성공** — `regression._resolve_gate_cmd()` 가 준 문자열을 shell 로 실행
            (board.py 회귀와 동형·비-Python repo 수용).
          - **해소 실패** — `[venv_python, -m, pytest, tests/, -q, -n, auto]` 병렬 기본 argv.

        cwd 는 _regression_cwd 가 해소한다— 분리된 PM 홈엔 tests/ 가 없으므로
        활성 worktree 슬롯에서 돌린다. 미해소면 REPO 폴백(현행 보존).
        """
        cwd = slot_resolution._regression_cwd(self._home, self._regression_slot)
        gate_cmd = regression._resolve_gate_cmd(self._home)
        if gate_cmd:
            # 해소된 게이트 — test_cmd 문자열을 shell 로(board.py regression run 과 동형).
            result = subprocess.run(
                gate_cmd,
                shell=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=cwd,
            )
        else:
            # 프레임워크 자기 회귀 — xdist 가용 환경의 병렬 기본 argv.
            result = subprocess.run(
                [str(self._venv_python), "-m", "pytest", "tests/", "-q", "-n", "auto"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=cwd,
            )
        output = result.stdout + result.stderr
        return result.returncode, output


    # ── task 퇴장: 변경 흔적 있는 보유 슬롯 각각에서 회귀 ──────

    def _run_regression_for_slot(self, slot: str) -> tuple[int, str]:
        """지정 슬롯 worktree 에서 회귀를 1회 돌린다 — task 다중슬롯 회귀.

        `self._regression_slot` 을 그 슬롯으로 잠시 세팅해 `_default_run_pytest` 의 `_regression_cwd`
        해소가 해당 worktree 를 cwd 로 보게 하고 원복한다(scope 밖 오염 방지). 주입 seam
        (`run_pytest_fn`)도 그대로 소비해 hermetic 테스트와 정합(injected runner 는 세팅된 슬롯을
        관찰 가능)."""
        prev = self._regression_slot
        self._regression_slot = slot
        try:
            return self._run_pytest_fn()
        finally:
            self._regression_slot = prev

    def _slot_worktree_missing(self, slot: str) -> bool:
        """슬롯 worktree 디렉터리가 stale(장부엔 있으나 dir 부재)인지 — task 슬롯별 회귀 vacuous-pass 가드.

        `_regression_cwd(slot)` 는 stale 슬롯(장부 조인 통과·dir 부재)을 **soft 하게 REPO 로 폴백**한다
        — **task 슬롯별 회귀**에선 그 REPO 에서 pytest 가 돌아 그
        슬롯이 green 처럼 보인다(vacuous-pass·엉뚱한 트리 green). 그래서 이 경로에선 REPO
        폴백을 금지하고 부재를 fail-loud(그 슬롯 red)로 올린다. 실재 판별은 worktree_pool `slot_path(slot).
        exists()`(엔진 소유 축·`_regression_cwd` 의 `REPO/slot` 과 동형). 판별 실패는 전파한다."""

        return not pm_paths.slot_path(self._home, slot).exists()

    def _classify_task_changed_slots(self, task: str) -> list[tuple[str, str]]:
        """task 보유 슬롯을 변경/무변경 분류해 **변경-슬롯 [(slot, reason)] 리스트**를 돌려준다.

        회귀([1/7])와 출하 변경 surface([1b])가 공유하는 '변경-슬롯 집합' 계산의 **단일 지점**. 회귀
        실행 여부(--no-pytest)와 무관하게 이 열거는 수행한다 — 비용은 슬롯당 git 조회 몇 회(회귀와
        무관·저렴)뿐이고, skip_pytest 여도 [1b] 가 REPO 폴백 단일 검사로 후퇴하지 않게 변경 집합을
        확보해야 하기 때문이다.
          - 보유 0개 → 명시 skip(대여 안내)·빈 리스트.
          - 변경 흔적(`_slot_has_changes`: lease 스냅 대비 head 전진/dirty·미기록=보수적 변경) 있는
            슬롯만 집합에 넣고 — 무변경 슬롯은 신호 0(직전 green 불변)이라 skip+사유 출력.
          - 변경 0개 → "변경 슬롯 없음" 명시(회귀·surface 대상 0)."""
        slots = self._task_held_slots(task)
        if not slots:
            print(
                f"  · task {task!r} 보유 슬롯 0개 — 대상 없음(skip). "
                f"회귀가 필요하면 `{lease_store._runtime_skill_entry('pm-env')} alloc <repo> --task <task>` "
                "을 먼저 시도하라. 풀이 소진됐다면 사용자에게 task-aware 슬롯 생성 승인을 "
                "요청하고, 승인한 사용자만 worktree add를 직접 실행한 뒤 task-only handoff를 "
                "다시 실행한다(세션 자동 생성 금지)."
            )
            return []
        changed: list[tuple[str, str]] = []
        for slot in slots:
            is_changed, reason = self._slot_has_changes(slot)
            if is_changed:
                changed.append((slot, reason))
            else:
                print(f"  · {slot} — 변경 흔적 없음({reason})·skip(직전 green 불변·신호 0).")
        if not changed:
            print(
                f"  · task {task!r} 보유 {len(slots)}슬롯 전부 무변경 — 변경 슬롯 없음"
                "(회귀·출하 surface 대상 0·신호 0·push 게이트=pre-push 훅 별도)."
            )
        return changed

    def _run_task_regressions(
        self, changed: list[tuple[str, str]], dry_run: bool
    ) -> "list[str] | None":
        """변경-슬롯 각각에서 회귀를 돌린다.

        `changed` = `_classify_task_changed_slots` 결과([(slot, reason)]·변경 슬롯만). 반환: 회귀가
        돈 **변경-슬롯 식별자 리스트**(green·비어있으면 회귀 실행 없음) 또는 **None**(한 슬롯이라도
        red → 호출부가 핸드오프 차단). 변경 슬롯 전부 green 이어야 통과. push 게이트는 pre-push 훅
        전체 회귀가 별도 재검증(이중 안전·무변경 슬롯 부담 제거)."""
        ran: list[str] = []
        # 게이트 종류를 루프 밖에서 한 번 해소한다 — green 판정 기준과 안내 문구가 여기서
        # 갈린다(`_gate_is_pytest`). 슬롯마다 같은 repo 게이트라 재해소는 낭비다.
        gate_cmd = regression._resolve_gate_cmd(self._home)
        for slot, reason in changed:
            # stale 슬롯(장부엔 있으나 worktree dir 부재) = fail-loud(REPO 폴백 vacuous-pass 금지·
            # 그 슬롯을 red 로 차단하고 해소 커맨드를 안내한다.
            if self._slot_worktree_missing(slot):
                print(
                    f"\n[중단] task 슬롯 {slot!r} 의 worktree 디렉터리 부재(stale — 장부엔 존재·"
                    f"{self._home.root / slot}) — REPO 폴백 회귀는 vacuous-pass(엉뚱한 트리 green)이므로 "
                    f"그 슬롯을 red 로 차단한다. `{lease_store._runtime_skill_entry('pm-worktree')} "
                    "prune-stale`(장부 정리) 또는 "
                    "사용자에게 슬롯 재생성 승인을 요청하라. 승인한 사용자만 "
                    "`worktree add <repo> --task <task> --user-ack <repo>`를 직접 실행한 뒤 "
                    "재시도한다(세션 자동 실행 금지). "
                    "log/current.md·pm_state.md 어떤 것도 건드리지 않는다.",
                    file=sys.stderr,
                )
                return None
            print(f"  ▷ {slot} 회귀 ({reason})...")
            if dry_run:
                print(f"    [dry-run] {handoff_text._gate_label(gate_cmd)} 실행 중 (파일 편집만 생략)...")
            returncode, output = self._run_regression_for_slot(slot)
            print(output.rstrip())
            if not handoff_text._regression_is_green(output, returncode, gate_cmd):
                print(
                    f"\n[중단] {slot} 회귀 red — 핸드오프 불가. "
                    "log/current.md·pm_state.md 어떤 것도 건드리지 않는다.",
                    file=sys.stderr,
                )
                if not handoff_text._gate_is_pytest(gate_cmd):
                    print(
                        f"원인: 회귀 명령이 실패했다 — `{gate_cmd}` (exit {returncode}).",
                        file=sys.stderr,
                    )
                return None
            summ = handoff_text.parse_pytest_summary(output)
            print(f"  ✓ green: {slot} — {summ}")
            ran.append(slot)
        return ran

    def _shipping_surface_for_slots(self, slots: list[str]) -> None:
        """회귀가 돈 **변경-슬롯 각각**의 worktree 에서 출하 변경을 surface 한다 (codex must-fix).

        task 다중슬롯 회귀는 변경-슬롯 집합을 돌므로, 출하 변경 surface([1b])도 **같은 집합**을 슬롯별
        로 돌려야 한다 — 단일 `_regression_cwd(None)` 자동해소가 엉뚱한 트리를 보거나 일부 변경-슬롯의
        SHIPPING_GLOBS 변경을 놓치는 것을 막는다(집합 1급화 일관·회귀·재스냅과 같은 슬롯 목록 공유).
        각 슬롯은 회귀와 동일하게 `_regression_cwd(slot)` 로 cwd 를 해소한다(stale 슬롯은 그쪽에서 REPO
        폴백·동형). 0개(변경 슬롯 없음)면 미push 변경도 없으니 surface 대상 없음."""
        if not slots:
            print("  · task 변경 슬롯 0개 — 출하 변경 surface 대상 없음(미push 변경 없음).")
            return
        for slot in slots:
            print(f"  ▷ {slot} 출하 변경:")
            self._shipping_surface_step(slot_resolution._regression_cwd(self._home, slot))

    def _default_run_git(self, args: list[str]) -> tuple[int, str]:
        """git 명령을 실행해 (returncode, stdout+stderr) 반환."""
        result = subprocess.run(
            ["git"] + args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(self._home.root),
        )
        output = result.stdout + result.stderr
        return result.returncode, output

    # ── 출하 변경 surface step ────────────────────

    def _shipping_surface_step(self, worktree: str) -> None:
        """[1b/7] 미검증 출하 변경을 **비차단 1줄로 surface** 한다.

        미push diff ∩ SHIPPING_GLOBS([[smoke-gate-by-output-change]])를 분류해 출하 변경
        (hits)·분류불명(unknown)이 있으면 "릴리즈 전 라이브 필요" 경고 1줄을 출력한다 —
        **핸드오프를 지연·차단하지 않는다**(rc 무영향). 라이브 LLM 검증(실 하네스 smoke)은
        릴리즈 단일 지점(release wave)으로 모았으므로, 고빈도 지점인
        핸드오프에서는 가시성만 보존한다. hits·unknown 모두 없으면(push 없음·명확한 비-출하)
        skip 사유만 출력.

        분류기(`_shipping_paths_in_pending_push`·`SHIPPING_GLOBS`)는 존치 — surface 의 기반이자
        향후 게이트 복원 가능성의 가역 지점.
        """
        shipping_hits, has_unknown = handoff_git._shipping_paths_in_pending_push(
            worktree, git_runner=self._run_git_fn
        )
        if shipping_hits:
            print(
                f"  ⚠ 미검증 출하 변경 {len(shipping_hits)}파일 — 릴리즈 전 "
                "라이브(release wave) 필요: `board.py livegate record`."
            )
            return
        if has_unknown:
            print(
                "  ⚠ 미검증 출하 변경 가능성 (미push diff 분류 불명·baseline 해소불가) — "
                "릴리즈 전 라이브(release wave) 확인 필요."
            )
            return
        print("  출하 변경 없음 (미push diff 가 비-출하·또는 push 없음) — release 라이브 불요.")

    # ── dirty-tree 게이트 step ([0/7]·조기 중단) ────────────────




    # ── 미마감 raw sweep step ────────────────

    def _resolve_raw_ledger(self) -> Path:
        """조회할 raw 장부 경로 — 명시 주입 우선, 없으면 `raw_output` 기본 위치. 해소 실패는 전파한다."""
        if self._raw_ledger_file is not None:
            return self._raw_ledger_file
        _raw_dir, ledger = raw_output.raw_storage_paths(self._home.local_dir.parent.parent, "delegate")
        return ledger

    def _unfinished_raw_step(self) -> None:
        """[6b/7] 미마감 raw 레코드를 **비차단 경고로 surface** 한다 (건수 + 최근 N건 식별자).

        미마감 = 실행 전 예약된 레코드가 마감되지 않은 것 = 그 위임/리뷰가 비정상 종료했다는 뜻.
        누적되면 장부 조회가 무력해지므로 세션 끝에서 건수를 보여주고 정리 판단을 PM 에게 남긴다.
        미마감 건수는 차단하지 않는다(rc 무영향). 장부 부재는 사유 1줄 후 skip, 위치 해소·장부 판독
        실패는 전파한다.

        판정 범위(어느 장부를 봤는지)를 항상 1줄로 밝힌다 — 위임은 소유 PM 홈
        (`raw_output`에 전달한 Home)이나 명시 `--output-dir` 기준으로 장부를 고르므로
        이 sweep 이 안 보는 사본 장부(예: worktree 쪽)가 있을 수 있다. 표기가 없으면 "0건"이
        "전 장부 0건"으로 읽힌다. 그 사본이 **실재하면** 경로와 건수도 병기한다(가시성만 —
        전 장부 통합 조회는 이 표면의 범위 밖).
        """
        ledger = self._resolve_raw_ledger()
        print(f"  이 장부 기준: {ledger}")
        self._report_unfinished_raw(ledger)
        self._report_peer_raw_ledgers(ledger)

    def _report_unfinished_raw(self, ledger: Path) -> None:
        """결정 공급 장부 1개의 미마감 판정 — 건수 + 최근 N건 (부재는 사유 1줄 후 skip)."""
        if not ledger.exists():
            print("  raw 장부 없음 — 미마감 raw 0건.")
            return
        rows = raw_output.unfinished_raw_records(ledger)
        if not rows:
            print("  미마감 raw 없음.")
            return
        print(
            f"  ⚠ 미마감 raw {len(rows)}건 — 비정상 종료 잔존. 전체 조회: "
            "`pm_delegate.py raw --unfinished`"
        )
        for row in rows[:handoff_text.UNFINISHED_RAW_DISPLAY_LIMIT]:
            print(
                f"    - {row.get('started_at')} · {row.get('surface')} · "
                f"{row.get('harness')} · role={row.get('role')} · id={row.get('id')}"
            )
        if len(rows) > handoff_text.UNFINISHED_RAW_DISPLAY_LIMIT:
            print(f"    … 외 {len(rows) - handoff_text.UNFINISHED_RAW_DISPLAY_LIMIT}건")

    def _peer_raw_ledgers(self, primary: Path) -> tuple[Path, ...]:
        """이 sweep 이 **안 보는** 사본 장부(worktree 등) — 판정은 위임 조회면 것을 재사용."""
        resolved_primary = primary.resolve()
        if self._peer_raw_ledger_files is not None:
            peers: tuple[Path, ...] = tuple(self._peer_raw_ledger_files)
        else:
            peers = tuple(raw_output._peer_engine_ledgers(self._home, None))
        return tuple(peer for peer in peers if peer != resolved_primary)

    def _report_peer_raw_ledgers(self, primary: Path) -> None:
        """사본 장부가 실재하면 **경로와 미마감 건수**를 1줄씩 병기한다 (가시성만·비차단).

        통합 조회는 범위 밖이다 — 이 sweep 은 결정 공급 장부 하나만 판정하고, 다른 사본이
        있다는 사실과 그 건수만 알린다. 사본이 없으면 아무 줄도 내지 않는다.
        """
        for peer in self._peer_raw_ledgers(primary):
            count = len(raw_output.unfinished_raw_records(peer))
            print(
                f"  {'⚠ ' if count else ''}사본 장부 {peer} — 미마감 {count}건 "
                "(이 sweep 밖·통합 조회는 범위 밖)."
            )

    # ── 구 task state 호환 backfill (정상 생성은 bind_task 시점) ─────────────────────

    def _task_registration_status(self, task: str) -> tuple[str, str | None]:
        """task 장부 membership을 읽기 전용으로 구분해 반환한다. 장부 판독 실패는 전파한다."""
        if self._task_record_snapshot is not None and self._task_record_snapshot[0] == task:
            return "registered", None

        # membership도 같은 strict 장부 파서를 우선 사용한다. 그렇지 않으면 손상 JSON을
        # "미등록 task"로 오인해 held-slot strict gate에 도달하기 전에 원인을 숨긴다.
        registered = pool_store.find_task_strict(self._home, task)
        if registered is None:
            archives = task_store.task_archive_candidates(self._home, task)
            if archives:
                return "archived", ", ".join(path.name for path in archives)
            return "unregistered", None
        self._task_record_snapshot = (task, registered)
        return "registered", None

    def _task_is_registered(self, task: str) -> bool:
        """task 장부 membership을 조회 전용으로 검증한다.

        task 생성 권한은 bootstrap(`bind_task`)에만 있다. handoff는 기존 task의 연속성만
        기록하므로, state backfill·회귀·log/dashboard write 전에 장부 membership을 확인해
        오타 task가 고아 state/log/dashboard를 만드는 것을 막는다.
        """
        status, detail = self._task_registration_status(task)
        if status == "registered":
            return True
        if status == "archived":
            print(
                f"\n[중단] task {task!r} 은(는) 종료 archive로 보관됨 ({detail}) — "
                "새 task로 bootstrap하지 않는다. "
                f"`pm-config.py task reopen {task}`"
                " (복수 후보면 `--archive <basename>` 추가) 후 bootstrap으로 재진입하라. "
                "(log/current.md·pm_state·dashboard 어떤 것도 건드리지 않는다.)",
                file=sys.stderr,
            )
            return False
        print(
            f"\n[중단] 미등록 task {task!r} — handoff는 task를 생성하지 않는다. "
            "bootstrap으로 task를 생성/재개한 뒤 다시 시도하라. "
            "(log/current.md·pm_state·dashboard 어떤 것도 건드리지 않는다.)",
            file=sys.stderr,
        )
        return False

    def _ensure_task_pm_state(self, task: str) -> bool:
        """task pm_state가 존재함을 보장한다.

        정상 경로에서는 bootstrap의 `task_lifecycle.bind_task`가 task 생성/재개 시 이미 state를
        만든다. 여기서는 구 엔진에서 state 없이 남은 task를 handoff 직전 호환 backfill한다.
        생성 로직을 복제하지 않고 worktree_pool의 단일 primitive만 소비한다.
        """
        if self._pm_state_file.exists():
            return True

        try:
            self._pm_state_file = Path(task_store.ensure_task_pm_state(self._home, task))
        except Exception as exc:  # noqa: BLE001 — state 생성 실패는 연속성 손실이므로 fail-loud.
            print(
                f"\n[중단] task pm_state 생성 실패 — {exc}. task state 없이 handoff하지 않는다.",
                file=sys.stderr,
            )
            raise
        print(f"  ✓ 구 task pm_state backfill ({self._pm_state_file})")
        return True

    # ── slot 대시보드 자기 섹션 overwrite step ───────────

    def _write_dashboard_section(
        self,
        session_identity: str,
        session_num: int | str,
        wave_summary: str,
        date_str: str,
    ) -> Path:
        """대시보드(`wiki/log/dashboard.md`)의 자기 섹션(`## <session_identity>`)을 overwrite 한다.

        차수·wave 요약·claimed 티켓을 3~5줄로 렌더(`render_dashboard_section`)해 자기 섹션만
        교체한다(`upsert_dashboard_section`·타 슬롯 byte 불변). 파일/섹션은 lazy 생성. claimed
        티켓은 board tickets 스캔(fail-soft·부재면 줄 생략). read→upsert→write 는 **파일락
        (`_dashboard_lock`) 안에서 직렬화**한다 — cross-slot 동시 핸드오프의 lost update 차단
        (MF-2·codex). 반환: 실제 write 한 대시보드 경로.
        """
        dash_file = (
            self._dashboard_file if self._dashboard_file is not None else pm_paths._dashboard_file(self._home)
        )
        section = dashboard.render_dashboard_section(
            session_identity,
            session_num=session_num,
            wave_summary=wave_summary,
            claimed_tickets=session_store._claimed_tickets_for_session(self._home, session_identity),
            date=date_str,
        )
        # read-modify-write 전체를 락 안에서 — 다른 슬롯이 사이에 write 해도 최신 파일을 다시
        # 읽어 그 슬롯 섹션을 보존한다(직렬화·lost update 0·타 슬롯 byte 불변). write 는
        # tmp 파일 → `fs.atomic_replace` 원자 교체 — crash/동시 read 시 빈·부분 파일 노출 차단
        # (worktree_pool `_write_ledger` 동형·codex suggestion).
        with session_store._dashboard_lock(self._home):
            existing = fs.read_text_shared(dash_file, encoding="utf-8") if dash_file.exists() else ""
            updated = dashboard.upsert_dashboard_section(existing, session_identity, section)
            dash_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = dash_file.with_suffix(dash_file.suffix + ".tmp")
            tmp.write_text(updated, encoding="utf-8", newline="\n")
            fs.atomic_replace(tmp, dash_file)
        return dash_file

    # ── 메인 흐름 ─────────────────────────────────────────────────────────────

    def run(
        self,
        task: str,
        *,
        dry_run: bool = False,
        skip_pytest: bool = False,
        ack_dirty: str | None = None,
        auto_trigger: bool = False,
        user_ack: str | None = None,
        force_handoff: bool = False,
    ) -> int:
        """PM 핸드오프 7단계 자동화 전체 흐름을 실행한다.

        task: 핸드오프 대상 task 이름 — 세션 정체성의 단일 축이다. pm_state 는
            `.local/tasks/<task>/pm_state.md`·dashboard 자기 섹션은 `## <task>`·log 헤더 태그는
            `(task:<task>)`. 보유 작업 폴더 집합은 장부에서 자동 수령하며 대여는 유지한다
            (세션 종료 ≠ task 종료). 보유 0개도 정상이다.
        ack_dirty: dirty-tree 게이트([0/7]) override 사유. 비어있지 않은 문자열이어야 하며
            (빈 사유는 부작용 0 로 중단) 사유는 단일행으로 평탄화돼 handoff entry 에 박제된다.
        auto_trigger: 사용자 명시 핸드오프 호출부의 호환 신호. dirty-tree 게이트를 차단 대신
            loud 경고 + 사유 자동 박제로 강등하지만, 독자 트리거나 승인으로 취급하지 않으며
            ``user_ack`` 값-결속 계약을 그대로 적용한다.
        user_ack: 사용자 발화에서 받은 핸드오프 대상 승인값. 세션이 만들거나 자동 부착하면
            안 되며, 실제 실행은 해소된 task 이름과 정확히 일치해야 한다.
        force_handoff: 사용자가 "강제 핸드오프" 를 고른 경우 — 보유 슬롯의 커밋 안 한 변경을
            두고 기록만 하며 entry 에 `커밋 안 한 변경 있음, 슬롯 유지: <slot>` 을 적는다.

        차수와 wave 요약은 인자가 아니다 — 회귀 게이트를 통과한 뒤 task pm_state 에서만
        추론한다. 따라서 어떤 호출 경로도 임의 차수를 log/state/dashboard 에 영속할 수 없다.

        반환: 0=성공, 1=실패 (중단).
        """
        # ack 사유는 **박제가 목적**이라 빈 문자열이면 override 를 받아들이지 않는다. CLI(argparse)
        # 뿐 아니라 run() 직접 호출도 같은 경계를 탄다 — 어떤 파일도 건드리기 전에 판정한다.
        if ack_dirty is not None and not ack_dirty.strip():
            print(
                "\n[중단] --ack-dirty 는 사유가 필수다 — 빈 사유로는 dirty 를 override 할 수 없다 "
                "(log/current.md·pm_state.md 어떤 것도 건드리지 않는다).",
                file=sys.stderr,
            )
            return 1

        # task 이름 검증 — main() CLI 뿐 아니라 run() 직접 호출도 우회 못 하게 엔진층 단일 choke
        # (validate_task_name_engine)를 *어떤 task 소비(pm_state 경로·log 태그·dashboard·트리거)
        # 이전에* 통과시킨다. traversal(`../evil`)·whitespace(`my task`)·슬롯 예약패턴·엔진부재는
        # 부작용 0 로 중단(1).
        # 검증기가 내는 것은 둘뿐이다 — 부적합 이름(`InvalidTaskName`·`.reason`)과 검증 실패(RuntimeError).

        try:
            session_identity.validate_task_name_engine(self._home, task)
        except (RuntimeError, worktree_records.InvalidTaskName) as exc:
            print(
                f"\n[중단] task 이름 {task!r} 이(가) 부적합 — {getattr(exc, 'reason', exc)} "
                "(log/current.md·pm_state 어떤 것도 건드리지 않는다.)",
                file=sys.stderr,
            )
            return 1
        # 이름이 유효해도 장부에 없는 task는 handoff가 만들 수 없다. 생성 권한은 bootstrap
        # (`bind_task`) 전용이며, 이 read-only membership gate는 회귀·state backfill 및 모든
        # 영속 write보다 앞선다(오타로 인한 고아 state/log/dashboard 부작용 0).
        if not self._task_is_registered(task):
            return 1
        # task 보유 슬롯은 log/pm_state/dashboard 및 재스냅 어떤 영속 변경보다 먼저 한 번
        # 해소한다. 실패를 실제 0슬롯으로 바꾸면 회귀·출하 검사를 전부 skip한 뒤 기록까지
        # 완료하는 false-green이 되므로 fail-loud. 성공 스냅샷(빈 tuple 포함)은 전 단계가 재사용한다.
        self._task_slots_snapshot = None
        try:
            held_slots = self._task_held_slots(task)
        except RuntimeError as exc:
            # `_task_held_slots` 는 모든 해소 실패를 RuntimeError 로 감싼다(해소 실패 ≠ 실제 빈 집합).
            print(
                f"\n[중단] task {task!r} 보유 슬롯 장부 해소 실패 — {exc}. "
                "실제 0슬롯으로 간주하지 않습니다. "
                "log/current.md·pm_state·dashboard·git 재스냅 어떤 것도 기록하지 않습니다.",
                file=sys.stderr,
            )
            return 1
        self._task_slots_snapshot = (task, tuple(held_slots))
        date_str = datetime.date.today().isoformat()
        # 차수는 회귀 게이트 통과 뒤 task pm_state 에서 추론한다(아래). wave 요약도 인자가 아니라
        # 여기서 만든다 — 사람이 숫자·문구를 대면 기록과 장부가 갈린다.
        session_num: int | None = None
        wave_summary = f"task {task} 세션 핸드오프"
        # 명시 pm_state 주입(hermetic 테스트)이 아니면 이 실행이 task 의 영속 기록(pm_state·장부
        # 종료 기록)을 소유한다. 주입 경로는 주입된 파일만 쓰고 장부는 건드리지 않는다.
        owns_task_state = not self._pm_state_file_explicit

        # 사용자 ack 는 CLI 가 아니라 핵심 엔진 경계에서 검증한다 — 실행 대상값(task)과 승인값을
        # 같은 자리에서 결속해 두 조회 사이에 대상이 갈리는 TOCTOU 를 만들지 않는다.
        if not require_handoff_user_ack(user_ack, task=task, dry_run=dry_run):
            return 1
        # 연속성 앵커 = task 서술 공간(`.local/tasks/<task>/pm_state.md`).
        if owns_task_state:
            self._pm_state_file = pm_paths.task_pm_state_file(self._home, task)
        print(
            f"[pm_handoff] task {task} 핸드오프 시작 "
            f"(dry_run={dry_run}, skip_pytest={skip_pytest}, "
            f"보유 작업 폴더 {len(held_slots)}개)"
        )

        # ── 0. dirty-tree 게이트 (조기 중단) ──────────────────────────────────
        # "핸드오프 시작 시점 tree 는 clean" 불변식의 기계 차단. 위치가 load-bearing 이다 —
        # 어떤 mutation(task state 보장·task 종료 기록·[2/7] log append)보다도 앞이라 차단돼도
        # 재실행이 중복 entry 를 만들지 않는다. 회귀([1/7]) **앞**에 두는 이유는 비용이다:
        # 판정은 git 조회 몇 번(초 단위)인데 회귀는 수 분이라, 뒤에 두면 커밋만 하면 될 PM 이
        # 회귀 한 판을 통째로 기다린 뒤에야 차단 사유를 본다.
        print("\n[0/7] dirty-tree 게이트 (미커밋 잔여 — 첫 mutation 전 판정)...")
        dirty_rc, dirty_ack_note = handoff_dirty._dirty_tree_gate(self._home, ack_dirty=ack_dirty, auto_trigger=auto_trigger, force_handoff=force_handoff, dry_run=dry_run, held_slots=self._task_slots_snapshot[1] if self._task_slots_snapshot is not None and self._task_slots_snapshot[0] == task else (), run_git_fn=self._run_git_fn)
        if dirty_rc != 0:
            return dirty_rc

        # ── 1. 회귀 측정 ───────────────────────────────────────────────────────
        # 변경 흔적(lease 스냅 대비 head 전진/dirty) 있는 **보유 슬롯 각각**에서 회귀한다
        # (보유 0개·변경 0개면 명시 skip).
        # 변경-슬롯 집합은 [1b] 출하 변경 surface 가 공유한다(codex must-fix) — **회귀
        # skip(--no-pytest) 여도** 열거는 수행한다(비용은 슬롯당 git 조회 몇 회뿐이고, [1b] 가
        # REPO 폴백 단일 검사로 후퇴하지 않게 집합을 확보해야 한다).
        task_shipping_slots: list[str] = []
        print("\n[1/7] 회귀 측정...")
        if dry_run:
            # ack 면제의 전제는 쓰기 0이다. pytest는 프로젝트 코드가 아니어도 cacheprovider 등
            # 플러그인이 파일을 만들 수 있으므로 dry-run에서는 runner 자체를 호출하지 않는다.
            print("  [dry-run] 순수 미리보기 — pytest 실행 생략(캐시 생성 없음).")
            pytest_summary = "(dry-run 미실행)"
            changed = self._classify_task_changed_slots(task)
            task_shipping_slots = [slot for slot, _reason in changed]
        elif skip_pytest:
            print("  [--no-pytest] 회귀 측정 skip.")
            pytest_summary = "(skip)"
            changed = self._classify_task_changed_slots(task)
            task_shipping_slots = [slot for slot, _reason in changed]
        else:
            changed = self._classify_task_changed_slots(task)
            ran_slots = self._run_task_regressions(changed, dry_run)
            if ran_slots is None:   # 변경 슬롯 중 하나라도 red — 핸드오프 차단.
                return 1
            task_shipping_slots = ran_slots
            pytest_summary = "(task 슬롯별 회귀·위 참조)"

        # ── 1b. 출하 변경 surface ───────────────
        # 기계회귀 green 직후 미push diff ∩ SHIPPING_GLOBS 를 분류해 미검증 출하 변경이
        # 있으면 "릴리즈 전 라이브 필요" 1줄을 surface 한다 — **비차단**(rc 무영향·핸드오프
        # 지연 0). 라이브 LLM 검증은 릴리즈 단일 지점(release wave)으로 모았다
        # dry_run 은 git 분류도 건너뛴다(미리보기). 회귀가 돈 **변경-슬롯 각각**에서 surface 한다 —
        # 단일 자동해소가 엉뚱한 트리를 보거나 일부 슬롯의 출하 변경을 놓치는 것을 막는다
        # (집합 1급화 일관·codex must-fix).
        print("\n[1b/7] 출하 변경 surface (비차단·릴리즈 라이브는 release wave)...")
        if dry_run:
            print("  [dry-run] 출하 변경 surface skip (미리보기).")
        else:
            self._shipping_surface_for_slots(task_shipping_slots)

        # task state 보장/차수 추론은 회귀·출하 게이트를 통과한 뒤, log/dashboard 등 첫 영속 변경
        # **전**에 끝낸다(MF-b). 구 task backfill 실패 시 외부 상태는 아직 무접촉이라 재시도해도
        # log 중복이 생기지 않는다. dry-run은 state를 만들지 않되 기존 state에서 같은 추론을 한다.
        if not dry_run and owns_task_state and not self._ensure_task_pm_state(task):
            return 1
        if not self._pm_state_file.exists():
            print(
                f"\n[중단] task pm_state가 없다: {self._pm_state_file} — "
                "차수를 임의 지정하지 않고 task state를 먼저 복구하라.",
                file=sys.stderr,
            )
            return 1
        inferred = session_history.infer_next_task_session_num(
            fs.read_text_shared(self._pm_state_file, encoding="utf-8"),
            log_store._read_text_exact(self._log_file) if self._log_file.exists() else "",
            task,
            task_released=(
                self._task_record_snapshot is not None
                and self._task_record_snapshot[0] == task
                and getattr(self._task_record_snapshot[1], "open", None) is False
            ),
        )
        if not isinstance(inferred, int):
            print(
                f"\n[중단] task {task!r} pm_state에서 현재 차수를 추론할 수 없다 — "
                "task state를 복구하라.",
                file=sys.stderr,
            )
            return 1
        session_num = inferred
        print(f"  ✓ task state+log 차수 자동 복구: {session_num}차")

        # 세션 종료 기록(닫힘)은 같은-session 재실행을 판별하는 load-bearing 신호이자, 다음
        # 진입이 이 자리를 "열려 있음"으로 오독하지 않게 하는 값이다. log/state/dashboard 어떤
        # handoff write보다 먼저 기록해, 이후 단계가 중단돼도 재실행이 같은 차수로 복구되게 한다.
        # 닫는 것은 task 행 하나다(대여는 유지). dry-run 은 예고만 하고 장부를 쓰지 않는다.
        # 명시 pm_state 주입(hermetic)은 장부를 소유하지 않으므로 기록하지 않는다.
        if owns_task_state:
            print("\n[세션] 종료 기록(닫힘) (log write 전)...")
            if dry_run:
                print(f"  [dry-run] 종료 기록 예고: {task} (실행 생략).")
            elif not self._close_session_records(task):
                return 1

        # ── 2. log/current.md handoff entry append / 같은-세션 갱신 ────────────────────
        print("\n[2/7] log/current.md handoff entry append/갱신...")
        # 연속성 앵커 = task. dashboard 자기 섹션 = `## <task>`(verbatim·interface 2),
        # log 헤더 태그 = `(task:<name>)`(sentinel·서술괄호와 기계 구분·interface 3). 두 표면의
        # 요구가 달라(사람 가독 vs 기계 파싱) 값을 분리한다.
        dashboard_key = task
        log_session_tag = f"{handoff_text._TASK_TAG_PREFIX}{task}"
        normalized_session_num = handoff_text._normalize_session_num(session_num)
        # ack 박제 자리를 못 찾으면(`DirtyAckStampError`) 통과시키지 않는다 — 사유를 잃은 채
        # 미커밋 세션이 넘어가는 것이 게이트가 막으려는 바로 그 상태다. 예외는 계획 단계에서
        # 나므로(write 앞) log 는 무접촉이다.
        try:
            if dry_run:
                log_text = (
                    log_store._read_text_exact(self._log_file)
                    if self._log_file.exists() else ""
                )
                same_session_rerun, _payload, preview, handoff_warning = (
                    handoff_text._prepare_handoff_log_change(
                        log_text,
                        session_num=normalized_session_num,
                        date=date_str,
                        session=log_session_tag,
                        task=task,
                        dirty_ack_note=dirty_ack_note,
                    )
                )
            else:
                # 판정+read-modify-write/append는 app.handoff_log가 infra.log_store 공유 락 한 임계구역에서 원자화한다.
                same_session_rerun, preview, handoff_warning = handoff_log._commit_handoff_log_change(
                    self._log_file,
                    session_num=normalized_session_num,
                    date=date_str,
                    session=log_session_tag,
                    task=task,
                    dirty_ack_note=dirty_ack_note,
                )
        except handoff_text.DirtyAckStampError as exc:
            print(
                f"\n[중단] dirty-tree ack 사유를 handoff entry 에 박제할 수 없다 — {exc}. "
                f"기존 entry 를 복구하거나 `{handoff_text.DIRTY_ACK_ENTRY_LABEL}: {dirty_ack_note}` 를 "
                "손으로 적은 뒤 재실행하라 (log/current.md 는 건드리지 않았다).",
                file=sys.stderr,
            )
            return 1

        if handoff_warning is not None:
            print(f"  ⚠ {handoff_warning}", file=sys.stderr)
        if same_session_rerun:
            print(
                f"[모드] 같은 차수({normalized_session_num}) 재실행 — "
                "entry 갱신·윈도 shift 생략"
            )
            if dirty_ack_note:
                print(f"  ✓ dirty-tree ack 사유 멱등 upsert: {dirty_ack_note}")
            if dry_run:
                print("  [dry-run] 기존 entry 본문 유지·기계 소유 구획 갱신 미리보기:")
                print("  " + preview.replace("\n", "\n  "))
            else:
                print(
                    f"  ✓ 기존 handoff entry 유지·박제 목록 갱신 "
                    f"(PM {normalized_session_num}차)"
                )
        elif dry_run:
            print("  [dry-run] log/current.md 에 append 할 skeleton:")
            print("  " + preview.replace("\n", "\n  "))
        else:
            print(
                f"  ✓ log/current.md handoff entry skeleton append "
                f"(PM {normalized_session_num}차)"
            )

        # log/current.md entry 누적 점검 — 임계 초과 시 archive 권장 (차단 아님).
        cur_log_text = log_store._read_text_exact(self._log_file) if self._log_file.exists() else ""
        entry_count = len(handoff_text._LOG_ENTRY_RE.findall(cur_log_text))
        if entry_count > handoff_text.LOG_ARCHIVE_SUGGEST_THRESHOLD:
            print(
                f"  ⚠ log/current.md entry {entry_count}개 > {handoff_text.LOG_ARCHIVE_SUGGEST_THRESHOLD} — "
                f"`pm_log.py archive --before <날짜>` 로 오래된 entry 봉인 권장 "
                f"(부트스트랩 읽기 비용 ↓).",
                file=sys.stdout,
            )

        # ── 2b. slot 대시보드 자기 섹션 overwrite ──────────
        # log entry append 와 같은 write 패스로 자기 섹션만 overwrite 한다(타 섹션 byte 불변·
        # append 아님). dry_run 은 write 안 함(미리보기). 명시 pm_state 주입(hermetic 테스트)도
        # 대시보드는 쓴다 — 파일이 별도 seam(`self._dashboard_file`)으로 격리돼 있다.
        print("\n[2b/7] 대시보드 자기 섹션 overwrite (수정형)...")
        if dry_run:
            print(f"  [dry-run] 대시보드 자기 섹션 overwrite 예고: ## {dashboard_key}")
        else:
            dash_file = self._write_dashboard_section(
                dashboard_key,
                handoff_text._normalize_session_num(session_num),
                wave_summary,
                date_str,
            )
            print(f"  ✓ 대시보드 자기 섹션 overwrite: {dash_file} (## {dashboard_key})")

        # ── 3·4. pm_state.md sliding window 정리 + 길이 검증 ───────────────────
        # pm_state.md 부재(board.py init 미실행 clone)는 치명 아님 — fail-soft.
        # 경고 후 3·4단계(세션 window 정리·길이 검증)를 skip 하고 나머지 진행.
        if not self._pm_state_file.exists():
            print(
                "\n[3-4/7] ⚠ pm_state.md 없음 — `board.py init` 미실행 clone. "
                "세션 식별 sliding window 갱신 skip. 핸드오프 계속.",
                file=sys.stderr,
            )
        else:
            # ── 3. pm_state.md sliding window 정리 ─────────────────────────────
            print("\n[3/7] pm_state.md 세션 식별 sliding window 정리...")
            state_text = fs.read_text_shared(self._pm_state_file, encoding="utf-8")

            try:
                # update_session_window 자체가 같은 차수면 byte no-op이다. 재실행에서도 이
                # primitive를 태워 step2(log) 뒤 step3(state) 전 중단된 반쪽 상태만 정확히
                # 복구하고, 이미 shift된 정상 재실행은 실제 write 없이 건너뛴다.
                new_state_text = handoff_text.update_session_window(
                    pm_state_text=state_text,
                    session_num=normalized_session_num,
                    date_str=date_str,
                    wave_summary=wave_summary,
                )
            except ValueError as exc:
                # fail-soft: 앵커/entry 불일치는 step3(sliding
                # window) *한정* 스킵하고 핸드오프는 완주한다. `return 1` 로 전체를 죽이면
                # 채택자 pm_state 의 미세 변형에 매 핸드오프가 무너진다(9세션 연속 수동 우회).
                # 추측 편집 금지 원칙은 유지 — window 를 지어내지 않되(원본 보존) 죽지도 않는다.
                print(
                    f"\n[3/7] ⚠ 세션 식별 sliding window 스킵 — {exc} "
                    "나머지 단계(4~7) 계속 진행.",
                    file=sys.stderr,
                )
                new_state_text = state_text  # window 미편집 — 원본 유지(step4 길이검증은 원본으로).
            else:
                if same_session_rerun:
                    if new_state_text == state_text:
                        print(
                            f"  ↷ 같은 차수({normalized_session_num}) 재실행 — "
                            "세션 window shift 생략(write 0·이미 반영)."
                        )
                    elif dry_run:
                        print(
                            f"  [dry-run] 같은 차수({normalized_session_num}) log 선행 반쪽 상태 — "
                            "세션 window 미반영분 복구 예고."
                        )
                    else:
                        self._pm_state_file.write_text(
                            new_state_text, encoding="utf-8", newline="\n")
                        print(
                            f"  ✓ 같은 차수({normalized_session_num}) log 선행 반쪽 상태 — "
                            "세션 window 미반영분 복구."
                        )
                elif dry_run:
                    # diff 미리보기: 변경된 줄 출력
                    old_lines = state_text.splitlines()
                    new_lines = new_state_text.splitlines()
                    added = [l for l in new_lines if l not in set(old_lines)]
                    removed = [l for l in old_lines if l not in set(new_lines)]
                    print("  [dry-run] pm_state.md 세션 식별 절 변경 예고:")
                    for line in removed[:5]:
                        print(f"  - {line}")
                    for line in added[:5]:
                        print(f"  + {line}")
                else:
                    self._pm_state_file.write_text(
                        new_state_text, encoding="utf-8", newline="\n")
                    print(
                        f"  ✓ pm_state.md 세션 식별 sliding window 정리 완료 "
                        f"(PM {session_num}차 추가·최근 최대 3개 유지)"
                    )

            # ── 4. pm_state.md 길이 검증 ────────────────────────────────────────
            print("\n[4/7] pm_state.md 길이 검증...")
            text_to_check = new_state_text if not dry_run else state_text
            line_count = len(text_to_check.splitlines())
            if line_count > handoff_text.PM_STATE_LINE_WARNING_THRESHOLD:
                print(
                    f"  ⚠ pm_state.md 길이 {line_count} 라인 > {handoff_text.PM_STATE_LINE_WARNING_THRESHOLD} 라인 임계값.",
                    file=sys.stdout,
                )
                print(
                    f"  ⚠ 과거 세션 정리 누락 신호 — §세션 식별 sliding window 정리를 점검하라.",
                    file=sys.stdout,
                )
            else:
                print(f"  ✓ pm_state.md {line_count} 라인 (임계값 {handoff_text.PM_STATE_LINE_WARNING_THRESHOLD} 이하).")

        # ── 5. 인계 프롬프트 stdout 출력 ───────────────────────────────────────
        # 템플릿(정적)은 pm_playbook.md 에서 추출한다 — sliding window 편집 대상(pm_state.md)과 분리.
        print("\n[5/7] 인계 프롬프트 출력...")
        playbook_text = fs.read_text_shared(self._pm_playbook_file, encoding="utf-8")
        prompt_output = handoff_prompt.build_handoff_prompt_output(self._home, pm_playbook_text=playbook_text, session_num=handoff_text._normalize_session_num(session_num), wave_summary=wave_summary, date_str=date_str, task=task)
        print(prompt_output)

        # ── 6. git status dump ─────────────────────────────────────────────────
        print("\n[6/7] git status dump...")
        git_rc, git_out = self._run_git_fn(["status", "-s"])
        if git_rc != 0:
            print(f"  ⚠ git status 실패 (rc={git_rc}): {git_out.rstrip()}")
        else:
            changed_files = [l for l in git_out.splitlines() if l.strip()]
            print(f"  변경 파일 수: {len(changed_files)}")
            if changed_files:
                print("  git status -s 출력:")
                for line in changed_files:
                    print(f"    {line}")
            else:
                print("  (변경 없음)")
        # 미push commit(ahead) — 커밋만 하고 push 를 안 한 세션이 그대로 넘어가면 다음 세션은
        # 원격에 없는 기록을 기준으로 판단한다(실측: PM 홈 2커밋 미push). 경고 1줄로만 세운다.
        ahead = handoff_git._ahead_commit_count(self._run_git_fn)
        if ahead is None:
            print("  (미push commit 수 미해소 — upstream 미설정·detached·원격 부재)")
        elif ahead[0] > 0:
            print(
                f"  ⚠ 미push commit {ahead[0]}개 (기준 {ahead[1]}) — 아래 [7/7] push 단계에서 "
                "올려라(미push 기록은 다음 세션에 안 보인다)."
            )
        else:
            print(f"  미push commit 없음 (기준 {ahead[1]}).")

        # ── 6b. 미마감 raw sweep ───────────────────────────────────────────────
        print("\n[6b/7] 미마감 raw sweep (비차단·경고)...")
        self._unfinished_raw_step()

        # ── 7. 잔여 PM 수동 작업 출력 ──────────────────────────────────────────
        print("\n[7/7] PM 이 손으로 할 잔여 작업:")
        print(
            "  [ ] log/current.md handoff entry 본문 채우기 — lean 3섹션"
            "(읽기범위·메타학습·다음intent)+회귀/incident(회귀 1줄 baseline). "
            "board/git/log 대량 재열거 금지("
            f"{lease_store._runtime_skill_entry('pm-bootstrap')} 라이브)."
        )
        print("  [ ] domain capture 검토 — `domain.py capture --tickets \"T-0001,T-0002\"`(이 세션 done ticket ID·콤마분리 또는 공백 나열) 출력 보고 ⚠/gap 페이지 갱신/신설(채록·surface-only).")
        print("        갱신 = 현재-진실 *교체* — 세션별 delta 를 페이지에 덧붙이지 마라"
              "('언제 왜 바뀌었나'는 log/ADR 몫·domain lint `history` 축).")
        print("  [ ] pm_state.md '진행 중인 의사결정' 표 갱신")
        print("  [ ] 열린 todo 정리 — 끝낸 것은 `pm-config task todo done <task> <ID>`. "
              "남은 것은 그대로 다음 세션이 승계한다(등록일이 묵은 정도를 보인다).")
        # 커밋 지시는 **경로 명시형** 이다 (공유 워킹트리 mutation 은 선언된 경로만).
        # bare `git commit` 은 다른 슬롯이 index 에 올려둔 남의 변경까지 함께 싣는다. 이 도구가
        # 실제로 쓰는 산출물은 `log/current.md` 하나다(pm_state 는 gitignored 라 대상 밖) —
        # PM 이 이번 세션에 손으로 고친 wiki 문서가 있으면 그 경로를 뒤에 덧붙인다.
        print("  [ ] git commit — **경로를 명시**하라: "
              "`git commit -m \"<메시지>\" -- .project_manager/wiki/log/current.md "
              "<이번 세션에 고친 wiki 문서 경로들>` (Co-Authored-By: Claude 트레일러 포함)")
        # commit 다음 단계가 빠져 있으면 기록은 로컬에만 남는다 — 위 [6/7] ahead 경고와 짝이다.
        print("  [ ] PM 홈 push(private 자율) — commit 후 `git push`. "
              "보호 브랜치(공개 제품 repo)는 사용자 승인 게이트라 여기서 밀지 마라.")

        # ── 핸드오프 완료: 보유 슬롯 git 재스냅 ────
        # 기록(log·pm_state) 완료 후 보유 슬롯 **전체**의 live git 을 lease.git 에 재기록한다 —
        # 세션 중 브랜치/HEAD 변경(예: 릴리즈 v1.3.2→v1.3.3)이 차기 부트스트랩 0단계
        # record-vs-live 정합(compare_slot_git)을 `diverged` FAIL-LOUD 로 오탐시켜 정당한 자기
        # 진행을 외부-개입 오경보로 차단하는 것을 막는다. base 미전달=기존 보존(arrival 동형)·
        # 판정 재구현 없이 write 프리미티브만 호출. dry_run 은 예고만. 장부 부재는
        # _record_slot_snapshot 내부에서 무해 skip.
        if not self._record_task_slot_snapshots(task, dry_run):
            return 1

        if dry_run:
            print("\n[dry-run] 완료 — 실제 파일 편집은 실행하지 않았다.")
        else:
            print(f"\n[완료] PM {session_num}차 핸드오프 자동화 완료.")

        return 0


def _handoff_user_ack_target(task: str) -> tuple[str, str]:
    """핸드오프 승인 토큰의 canonical 대상값과 사람이 읽는 대상 설명을 반환한다.

    대상은 **장부가 준 정체성**, 곧 task 이름 하나다. 해소는 run() 진입 전에 이미 끝나 있으므로
    여기서 미해소를 다시 다루지 않는다.
    """
    return task, f"task {task!r}"


def require_handoff_user_ack(
    user_ack: str | None,
    *,
    task: str,
    dry_run: bool,
) -> bool:
    """기존 값-결속 ack 관용구로 실제 핸드오프만 사용자 승인 게이트한다."""
    target, description = _handoff_user_ack_target(task)
    if dry_run:
        print(
            f"[dry-run] 쓰기 0 미리보기는 사용자 ack 없이 허용한다. 실제 {description} "
            "핸드오프를 실행하려면 사용자 명시 승인이 필요하다 — 먼저 사용자에게 핸드오프 "
            f"여부를 확인하라. 승인한 사용자만 `--user-ack {target}`를 대상값 그대로 "
            "붙여 실행하라(세션 자동 부착 금지)."
        )
        return True
    if user_ack == target:
        print(
            f"[승인 감사] pm_handoff: 사용자 승인 토큰이 {description} 대상값 {target!r}에 "
            "값-결속됨."
        )
        return True
    mismatch = (
        f" 제공된 값 {user_ack!r}은 대상값 {target!r}에 결속되지 않았다."
        if user_ack is not None
        else ""
    )
    print(
        f"[중단] pm_handoff {description}: 세션 핸드오프에는 사용자 명시 승인이 필요하다."
        f"{mismatch}\n"
        "  1순위: 사용자에게 핸드오프 여부를 확인하라.\n"
        f"  부차 수단: 승인한 사용자만 `--user-ack {target}`를 대상값 그대로 붙여 "
        "실행하라(세션 자동 부착 금지).",
        file=sys.stderr,
    )
    return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pm_handoff.py",
        description="PM 핸드오프 7단계 자동화 헬퍼.",
    )
    # 세션 정체성은 task 하나다. 차수와 wave 요약은 인자가 아니라 task pm_state 에서 엔진이
    # 추론한다 — 사람이 숫자를 대면 기록과 장부가 갈린다.
    parser.add_argument(
        "--task",
        metavar="이름",
        default=None,
        help=(
            "핸드오프 대상 task 이름. 생략하면 cwd 작업 폴더 → 단일 활성 task 순으로 스스로 "
            "해소하고, 그래도 정할 수 없으면 중단한다(`<등록 repo>_<N>` 예약 패턴은 거부)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="파일 편집 없이 변경 미리보기.",
    )
    parser.add_argument(
        "--user-ack",
        metavar="<task 이름>",
        default=None,
        help=(
            "사용자가 명시 승인한 핸드오프 대상값(task 이름)과 정확히 결속하는 토큰. "
            "--dry-run 미리보기는 생략 가능."
        ),
    )
    parser.add_argument(
        "--no-pytest",
        action="store_true",
        help="회귀 측정 skip (기본 측정·대화형 경로).",
    )
    parser.add_argument(
        "--ack-dirty",
        metavar="사유",
        default=None,
        help=(
            "dirty-tree 게이트([0/7]) override — 미커밋 잔여를 두고 나가는 **사유**가 필수다"
            "(bare 플래그·빈 사유 거부). 사유는 단일행으로 평탄화돼 handoff entry 에 박제된다."
        ),
    )
    parser.add_argument(
        "--force-handoff",
        action="store_true",
        help=(
            "보유 작업 폴더에 커밋 안 한 변경이 있을 때 사용자가 강제 핸드오프를 고른 경우 — "
            "커밋 없이 핸드오프 기록만 하고 변경은 제자리에 둔다. "
            "entry 에 '커밋 안 한 변경 있음, 슬롯 유지: <slot>' 을 적는다."
        ),
    )
    parser.add_argument(
        "--auto-trigger",
        action="store_true",
        help=(
            "사용자가 핸드오프를 명시 지시한 호출부를 위한 호환 신호. dirty-tree 게이트를 "
            "차단 대신 loud 경고 + 사유 자동 박제로 강등한다. 독자 트리거·승인이 아니며 "
            "--user-ack 값-결속 계약을 우회하지 않는다."
        ),
    )
    return parser


def _main(home, argv: list[str] | None=None, *, identity_resolver=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handoff = PmHandoff(home)

    # --ack-dirty 는 **사유 박제**가 목적이라 빈/공백 사유를 받지 않는다 (사유 없는 bare 플래그는
    # argparse 가 "expected one argument" 로 이미 usage error). 엔진 run() 도 같은 경계를 갖는다.
    # 개행이 살아 있으면 사유 하나로 가짜 entry 줄을 위조할 수 있어 여기서 단일행으로 평탄화한다
    # (엔진 `build_dirty_ack_note` 도 같은 choke 를 다시 태운다 — 직접 호출 경로 보호).
    if args.ack_dirty is not None:
        if not args.ack_dirty.strip():
            parser.error(
                "--ack-dirty 는 사유가 필수다 — 빈 사유로는 dirty-tree 게이트를 override 할 수 없다."
            )
        args.ack_dirty = handoff_text.flatten_ack_reason(args.ack_dirty)

    task = args.task
    if task is None:
        # 인자 검증 뒤, 실제 handoff 직전에만 생산자 identity 를 해소한다. seam 주입은 CLI 회귀가
        # 실행 머신의 REPO/cwd 장부를 읽지 않게 한다.
        task, identity_source = (identity_resolver() if identity_resolver is not None else _resolve_implicit_handoff_identity(home))
        # 생산자 층이 하나라도 미분류로 늘어나면 여기서 즉시 중단한다(계약 검문).
        session_identity._handoff_identity_source_contract()
        if task is None:
            # 쓸 자리를 지어내지 않는다 — 어느 task 의 연속성을 이어야 하는지 모르면 중단이 답이다.
            print(
                f"[중단] 핸드오프 대상 task 미해소 ({identity_source}) — 이 cwd 를 가진 작업 폴더가 "
                "없고 활성 task 도 하나가 아니다. `--task <task>` 으로 명시하라. "
                f"{identity_args.NO_WORK_FOLDER_REMEDY}",
                file=sys.stderr,
            )
            return 1
        print(
            f"[pm_handoff] 정체성 자동 해소: task={task} "
            f"({identity_source}; pm_log 공유 체인)"
        )

    # 명시·자동 해소 양쪽 모두 생산자와 같은 task 이름 validator 를 통과한다. 장부 membership
    # 검사는 run() 이 영속 변경 전에 한 번만 한다(중복 판정 금지).
    try:
        session_identity.validate_task_name_engine(home, task)
    except RuntimeError as exc:
        parser.error(str(exc))
    except worktree_records.InvalidTaskName as exc:
        parser.error(
            f"--task 이름 {task!r} 이(가) 부적합 — "
            f"{getattr(exc, 'reason', exc)}. 안전한 단일 이름(공백·괄호·path 문자·"
            "슬롯 예약패턴 `<repo>_<N>` 불가)이어야 한다."
        )

    return handoff.run(
        task,
        dry_run=args.dry_run,
        skip_pytest=args.no_pytest,
        ack_dirty=args.ack_dirty,
        auto_trigger=args.auto_trigger,
        user_ack=args.user_ack,
        force_handoff=args.force_handoff,
    )


def _resolve_implicit_handoff_identity(home, *, cwd: Path | None=None) -> tuple[str | None, str]:
    """pm_log 생산자와 같은 체인으로 무인자 handoff 의 task 를 해소한다.

    체인은 생산자가 소유한다 — `cwd→lease 행` → `단일 활성 task` → 미해소. 이 함수는 그 결과를
    그대로 돌려줄 뿐 자기 층을 더하지 않는다(층이 갈리면 checkpoint 와 handoff 가 서로 다른
    정체성을 쓴다).
    """
    resolved_cwd = Path(cwd or Path.cwd()).resolve(strict=False)
    return session_identity.resolve_snapshot_identity(home, resolved_cwd)


def main(home, argv: list[str] | None=None, *, identity_resolver=None) -> int:
    return _main(home, argv, identity_resolver=identity_resolver)
