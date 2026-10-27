"""부트스트랩 수집 순서와 task 진입 조정."""
from __future__ import annotations
import datetime
import argparse
from pm_engine.domain import git_freshness
from pm_engine.infra import pm_paths
import subprocess
from pm_engine.infra import lease_store
from pm_engine.domain import dashboard
from pm_engine.infra import worktree_git
from pm_engine.app import task_lifecycle
from pm_engine.infra import console
from pm_engine.infra import pool_store
from pm_engine.infra import fs
from pm_engine.app import slot_resolution
from pathlib import Path
import sys
from typing import Callable
import json
from pm_engine.infra import delegate_ledger
from pm_engine.domain import worktree_records
from pm_engine.domain import identity_args
from pm_engine.domain import session_history
import re
from pm_engine.infra import task_store
from pm_engine.infra import python_env
from pm_engine.domain import bootstrap_rules
from pm_engine.app import bootstrap_history
from pm_engine.cli import bootstrap_report
from pm_engine.app import bootstrap_slots
from pm_engine.cli import bootstrap_card
from pm_engine.infra import clock

GIT_NETWORK_TIMEOUT = 20


class PmBootstrap:
    """PM 세션 부트스트랩 기계 측정 핵심 로직.

    subprocess 함수를 DI 해 테스트에서 실제 실행 없이 결정론적으로 검증한다.
    ticket_finish.py 의 TicketFinisher DI 패턴과 동일.
    """

    def __init__(
        self,
        home,
        *,
        run_board_fn: Callable[[list[str]], tuple[int, str]] | None = None,
        run_pytest_fn: Callable[[], tuple[int, str]] | None = None,
        run_git_fn: Callable[[list[str]], tuple[int, str]] | None = None,
        log_file: Path = None,
        board_py: Path = None,
        areas_file: Path | None = None,
        venv_python: str | Path = None,
        pm_state_file: Path | None = None,
        board_dir: Path | None = None,
    ) -> None:
        self._home = home
        log_file = (
            log_file if log_file is not None else pm_paths._resolve_log_file(home)
        )
        board_py = (
            board_py
            if board_py is not None
            else home.root / ".project_manager/tools/board.py"
        )
        venv_python = (
            venv_python if venv_python is not None else python_env._default_python(home)
        )
        self._log_file = log_file
        self._board_py = board_py
        self._board_dir = (
            board_dir
            if board_dir is not None
            else self._home.local_dir.parent / "board"
        )
        self._pm_state_file = pm_state_file
        self._areas_file = (
            areas_file if areas_file is not None else pm_paths.areas_file(self._home)
        )
        self._venv_python = venv_python
        self._run_board_fn = run_board_fn or self._default_run_board
        self._run_pytest_fn = run_pytest_fn or self._default_run_pytest
        self._run_git_fn = run_git_fn or self._default_run_git
        self._task_name: str | None = None
        self._task_pm_state_file: Path | None = None
        self._task_workspace_slots: tuple[str, ...] = ()
        self._task_collection_slot: str | None = None

    def _worktree_cwd(self, slot: str | None = None) -> str:
        """git/pytest 를 돌릴 작업 디렉토리를 해소한다 (분리된 PM 홈+worktree 모델).

        자기분리토폴로지: 코드/tests=worktree·board/wiki=PM 홈. 분리된 PM
        홈 루트에서 bootstrap 을 돌리면 그 자리엔 코드/tests 가 없으므로, git dump·pytest
        회귀는 활성 worktree 슬롯 cwd 에서 돌아야 한다. 이 함수가 그 경로를 해소한다.
        (board 러너는 홈 소유라 이 경로를 쓰지 않는다 — `_default_run_board` 는 REPO 고정.)

        해소 순서:
          - `slot`(순회 중인 task 보유 슬롯) 이 있으면 `REPO / slot`,
          - 없으면 현재 순회 슬롯 또는 task 보유 슬롯의 정렬 첫 항목,
          - task 보유 슬롯이 0개면 `REPO` (전역 슬롯 자동해소 **금지** — 남의 슬롯을 고를 수 있다).
        """
        if slot:
            return str(self._home.root / slot)
        task_slot = self._task_collection_slot
        if task_slot is None and self._task_workspace_slots:
            task_slot = self._task_workspace_slots[0]
        return str(self._home.root / task_slot) if task_slot else str(self._home.root)

    def _pm_state_display_path(self) -> str:
        """첫-turn 안내에 쓸 pm_state 경로 — 이 task 의 서술 pm_state.

        task 가 유일한 연속성 앵커이므로 task 바인딩이 해소한
        `.local/tasks/<task>/pm_state.md` 를 그대로 표시한다.
        """
        if self._task_pm_state_file is not None:
            return bootstrap_report._display_path_text(self._task_pm_state_file)
        return f".project_manager/.local/tasks/{self._task_name}/pm_state.md"

    def _default_run_board(self, args: list[str]) -> tuple[int, str]:
        result = subprocess.run(
            [str(self._venv_python), str(self._board_py)] + args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(self._home.root),
        )
        return (result.returncode, result.stdout + result.stderr)

    def _default_run_pytest(self) -> tuple[int, str]:
        result = subprocess.run(
            [str(self._venv_python), "-m", "pytest", "tests/", "-q"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=self._worktree_cwd(),
        )
        return (result.returncode, result.stdout + result.stderr)

    def _default_run_git(self, args: list[str]) -> tuple[int, str]:
        is_network = bool({"fetch", "pull"} & set(args))
        timeout = GIT_NETWORK_TIMEOUT if is_network else None
        try:
            result = subprocess.run(
                ["git"] + args,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=self._worktree_cwd(),
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return (
                1,
                f"[timeout] git {' '.join(args)} 가 {timeout}s 초과 — 원격 무응답(offline?)",
            )
        return (result.returncode, result.stdout + result.stderr)

    def _collect_board(self) -> dict:
        """board list(내 것 렌즈·+ done 별도 조회) + lint 결과를 수집한다. `list` 실패만 여전히 sys.exit(1).

        렌즈는 이 세션의 task 다 — `list --task <현재 task>`. user-wide `--mine` 은 같은 사용자의
        다른 task claim 까지 합치므로 task identity dump 의 근거로 쓰지 않는다.
        전체 보드(contention 가시·backlog 확인·타 세션 claim)는 `board list --all` 로 PM 이 명시
        조회한다(무인자 기본은 내 세션 스트림만·타 세션 티켓 정보는 기본 dump 에서 완전 제거).

        반환 `counts` 는 이 렌즈 스코프 값이다.
        라벨 명확화는 빌더(`_format_board_counts_line`)가 `counts_scope` 로 담당한다.

        **done 카운트는 default 뷰(무-`--status`) 비의존**:
        `board.py list`(무-status)가 done 을 접어활성 상태(open/claimed/blocked)만
        보여주므로, 위 default 뷰 호출 출력엔 done 행이 아예 없어 `counts["done"]` 이 항상
        그래서 done 전용으로
        `["list", "--status", "done", *lens]` 을 **별도 호출**해 그 출력만 파싱한 done 카운트로
        덮어쓴다 — open/claimed/blocked 는 첫 호출(default 뷰) 그대로(그 상태들은 default 뷰에
        이미 있으므로 재조회 불요). `list` 호출은 2회다(default·done).
        """
        lens = ["--task", self._task_name]
        counts_scope = f"task {self._task_name}"
        rc, output = self._run_board_fn(["list", *lens])
        if rc != 0:
            print(f"[중단] board.py list 실패 (rc={rc}):\n{output}", file=sys.stderr)
            sys.exit(1)
        counts = bootstrap_rules.parse_board_counts(output)
        open_tickets = bootstrap_rules.parse_open_tickets(output)
        done_rc, done_output = self._run_board_fn(["list", "--status", "done", *lens])
        if done_rc == 0:
            counts["done"] = bootstrap_rules.parse_board_counts(done_output).get(
                "done", 0
            )
        lint_rc, lint_output = self._run_board_fn(["lint", "--gate"])
        lint_counts = bootstrap_rules.parse_lint_counts(lint_output)
        lint_parse_failed = lint_counts is None
        hook_generation_blocked = (
            bootstrap_report._hook_generation_notice(lint_output) is not None
            if lint_parse_failed
            else False
        )
        lint_result = (
            bootstrap_rules._LINT_HOOK_GENERATION_RESULT
            if hook_generation_blocked
            else (
                "gate error (unparsed)"
                if lint_parse_failed
                else bootstrap_report.parse_lint_result(lint_output)
            )
        )
        return {
            "counts": counts,
            "counts_scope": counts_scope,
            "open_tickets": open_tickets,
            "lint": lint_result,
            "lint_blocking": lint_rc != 0 or lint_parse_failed,
            "lint_gate_rc": lint_rc,
            "lint_parse_failed": lint_parse_failed,
            "lint_gate_output": lint_output,
        }

    def _collect_pytest(self) -> dict | None:
        """pytest 회귀를 실행하고 결과를 반환한다.

        default 는 skip — 호출자가 with_pytest=True 일 때만 호출한다.
        실패 시 sys.exit(1).
        """
        rc, output = self._run_pytest_fn()
        parsed = bootstrap_rules.parse_pytest_counts(output)
        if parsed is None:
            print(f"[중단] pytest 출력 파싱 실패 (rc={rc}):\n{output}", file=sys.stderr)
            sys.exit(1)
        passed, total = parsed
        if rc != 0 or passed != total:
            scope = (
                f" (slot={self._task_collection_slot})"
                if self._task_collection_slot is not None
                else ""
            )
            print(
                f"[중단] pytest 회귀 실패{scope} — rc={rc}, passed={passed}, failed={total - passed}, total={total}:\n{output}",
                file=sys.stderr,
            )
            raise SystemExit(1)
        return {"passed": passed, "total": total, "output": output}

    def _collect_pytest_for_scope(self) -> dict | None:
        """현재 정체성의 pytest를 수집한다 — task 모드면 보유 작업공간 전수.

        task 보유 슬롯 각각에서 회귀를 실행해 합산하고, 0개면 PM 홈 tests 로 조용히 빠지지 않고
        fail-loud 한다. bootstrap 기본은 회귀 skip 이라 신규 task(작업공간 0개) 생성 자체는 정상이다.
        """
        slots = list(self._task_workspace_slots)
        if not slots:
            print(
                f"[중단] task {self._task_name!r} 보유 작업공간이 0개라 --with-pytest 대상을 해소할 수 없습니다 — `{bootstrap_card._runtime_skill_entry('pm-env')} alloc <repo> --task <task>`으로 작업공간을 대여한 뒤 다시 실행하세요. 전역 auto-slot으로 대체하지 않습니다.",
                file=sys.stderr,
            )
            raise SystemExit(1)
        results: list[dict] = []
        try:
            for task_slot in slots:
                self._task_collection_slot = task_slot
                self._require_task_slot_ownership(task_slot, "pytest 실행")
                result = self._collect_pytest()
                result["slot"] = task_slot
                results.append(result)
        finally:
            self._task_collection_slot = None
        return {
            "passed": sum((result["passed"] for result in results)),
            "total": sum((result["total"] for result in results)),
            "output": "\n".join(
                (f"[{result['slot']}]\n{result['output']}" for result in results)
            ),
            "scopes": [result["slot"] for result in results],
        }

    def _collect_git(self) -> dict:
        """git 브랜치·최근 3 commit·working tree 상태를 수집한다.

        빈 repo(커밋 0 — fresh clone `pm_import --new` 직후)는 **정상 케이스**로
        degrade 한다: branch 는 `symbolic-ref` 폴백, commits 는 빈 목록 +
        `no_commits=True`. **진짜 git repo 가 아닐 때만** (symbolic-ref·status 전부
        실패) sys.exit 로 중단한다. 빈-repo 판정은 commits==[] 기준 (로캘 독립 —
        git 메시지 텍스트를 파싱하지 않는다).
        """
        branch_rc, branch_out = self._run_git_fn(["rev-parse", "--abbrev-ref", "HEAD"])
        if branch_rc == 0:
            branch = bootstrap_rules.parse_git_branch(branch_out)
        else:
            sym_rc, sym_out = self._run_git_fn(["symbolic-ref", "--short", "HEAD"])
            if sym_rc != 0:
                print(
                    f"[중단] git repo 아님 — rev-parse(rc={branch_rc})·symbolic-ref(rc={sym_rc}) 모두 실패:\n{sym_out}",
                    file=sys.stderr,
                )
                sys.exit(1)
            branch = bootstrap_rules.parse_git_branch(sym_out)
        log_rc, log_out = self._run_git_fn(["log", "--oneline", "-5"])
        commits = bootstrap_rules.parse_git_log(log_out) if log_rc == 0 else []
        no_commits = commits == []
        status_rc, status_out = self._run_git_fn(["status", "--short"])
        if status_rc != 0:
            print(
                f"[중단] git repo 아님 — git status 실패 (rc={status_rc}):\n{status_out}",
                file=sys.stderr,
            )
            sys.exit(1)
        working_tree = bootstrap_rules.parse_git_status(status_out)
        return {
            "branch": branch,
            "commits": commits,
            "no_commits": no_commits,
            "working_tree": working_tree,
        }

    def _collect_board_git(self) -> dict | None:
        """board submodule(`.project_manager/board`) 의 HEAD·dirty·ahead/behind 를 수집한다.

        board 는 `ignore=all` git submodule 이라 부모 repo `git status` 가
        board 의 git 상태를 통째로 숨긴다 — multi-PM 에서 board = claim 즉시-sync 공유채널
        이라 board 가 stale 하면 남의 claim 을 놓칠 수 있다(freshness load-bearing).

        `self._board_dir`(`.project_manager/board`) 가 **실 디렉토리가 아니면**(board
        미분리) None 을 반환해 graceful skip 한다(빌더가 Git 섹션에 이 줄을 생략). 실
        디렉토리여도 `-C <board_dir>` git 호출이 전부 실패하면(예: `.git` 없음·손상) 마찬가지로
        None(fail-soft — board freshness 는 *추가 인지*이지 부트스트랩 본체를 막지 않는다).

        반환: {"head": "<sha7>", "dirty": bool, "ahead": int|None, "behind": int|None} 또는 None.
        ahead/behind 는 upstream 미설정/조회불가 시 둘 다 None(dirty·head 는 여전히 유효할 수
        있음 — 부분 degrade).
        """
        if not self._board_dir.is_dir():
            return None
        head_rc, head_out = self._run_git_fn(
            ["-C", str(self._board_dir), "rev-parse", "--short", "HEAD"]
        )
        if head_rc != 0:
            return None
        head = head_out.strip()
        status_rc, status_out = self._run_git_fn(
            ["-C", str(self._board_dir), "status", "-s"]
        )
        dirty = status_rc == 0 and bool(status_out.strip())
        ahead: int | None = None
        behind: int | None = None
        ab_rc, ab_out = self._run_git_fn(
            [
                "-C",
                str(self._board_dir),
                "rev-list",
                "--left-right",
                "--count",
                "HEAD...@{u}",
            ]
        )
        if ab_rc == 0:
            parsed = git_freshness.parse_git_ahead_behind(ab_out)
            if parsed is not None:
                ahead, behind = parsed
        return {"head": head, "dirty": dirty, "ahead": ahead, "behind": behind}

    def _probe_git_freshness(self, label: str, scope_dir: Path) -> dict:
        """한 git 컨텍스트를 fetch origin 후 branch/dirty/ahead·behind 로 측정한다 (pull 없음).

        모든 호출은 `-C <dir>` 로 명시(러너 cwd 무관·`_collect_board_git` 동형·DI 보존).
        fetch 실패는 fail-soft(fetched=False·이후 측정은 stale local 기준). `state` 는
        `freshness_decision` — 호출부가 `state == "동기"` 로 clean·ff 인지 판정해 pull 한다.
        """
        scope: dict = {
            "label": label,
            "dir": str(scope_dir),
            "fetched": None,
            "branch": None,
            "detached": False,
            "dirty": None,
            "ahead": None,
            "behind": None,
            "pulled": False,
            "note": None,
            "state": None,
        }
        d = str(scope_dir)
        f_rc, _f_out = self._run_git_fn(["-C", d, "fetch", "origin"])
        scope["fetched"] = f_rc == 0
        b_rc, b_out = self._run_git_fn(["-C", d, "symbolic-ref", "HEAD"])
        b_ref = b_out.strip()
        if b_rc == 0 and b_ref.startswith(bootstrap_rules._SYMREF_BRANCH_PREFIX):
            scope["branch"] = b_ref[len(bootstrap_rules._SYMREF_BRANCH_PREFIX) :]
        else:
            scope["detached"] = True
        s_rc, s_out = self._run_git_fn(["-C", d, "status", "-s"])
        scope["dirty"] = bool(s_out.strip()) if s_rc == 0 else None
        ab_rc, ab_out = self._run_git_fn(
            ["-C", d, "rev-list", "--left-right", "--count", "HEAD...@{u}"]
        )
        if ab_rc == 0:
            parsed = git_freshness.parse_git_ahead_behind(ab_out)
            if parsed is not None:
                scope["ahead"], scope["behind"] = parsed
        scope["state"], _do_pull = git_freshness.freshness_decision(
            fetched=scope["fetched"],
            detached=scope["detached"],
            dirty=scope["dirty"],
            ahead=scope["ahead"],
            behind=scope["behind"],
        )
        return scope

    def _sync_scope(
        self,
        label: str,
        scope_dir: Path,
        *,
        is_home: bool = False,
        task_slot: str | None = None,
    ) -> dict:
        """한 git 컨텍스트를 측정하고 clean·ff 면 `git pull --ff-only` 로 자동 동기한다.

        pull 실패는 fail-soft(경고 노트 실어 계속·abort 안 함). dirty·diverged·detached 는
        pull 하지 않고 `⚠ behind N — 수동 동기 필요` 경고만. (is_home)는 board 서브모듈
        rider(fetch + branch 유지 pull)를 함께 돌려 `scope["board_sync"]` 로 반환한다.
        """
        if task_slot is not None:
            self._require_task_slot_ownership(task_slot, "git fetch/pull 실행")
        scope = self._probe_git_freshness(label, scope_dir)
        d = str(scope_dir)
        if scope["state"] == "동기":
            if task_slot is not None:
                self._require_task_slot_ownership(task_slot, "git pull 실행")
            was_behind = scope["behind"]
            p_rc, _p_out = self._run_git_fn(["-C", d, "pull", "--ff-only"])
            if p_rc == 0:
                scope["pulled"] = True
                scope["behind"] = 0
                scope["note"] = f"ff-pull 동기 완료 (behind {was_behind}→0)"
            else:
                scope["note"] = f"⚠ pull --ff-only 실패 (rc={p_rc}) — 수동 확인"
        elif scope["behind"] and scope["behind"] > 0:
            scope["note"] = git_freshness._behind_warning(scope)
        if is_home:
            scope["board_sync"] = self._sync_board_submodule()
        return scope

    def _sync_board_submodule(self) -> dict | None:
        """board 서브모듈 fetch + branch 유지 ff-pull.

        board 미분리(`_board_dir` 비-디렉토리)면 None(skip). fetch 상시(fail-soft),
        clean·ff 면 `checkout <branch> && pull --ff-only` 로 branch 를 유지한 채 동기한다 —
        `git submodule update` 의 detached HEAD 를 피한다(그러면 이후 board mutation 기록이
        sentinel 로 스킵된다). dirty·diverged·detached 는 표면화만(pull 안 함).
        """
        board_dir = self._board_dir
        if not board_dir.is_dir():
            return None
        scope = self._probe_git_freshness("board 서브모듈", board_dir)
        d = str(board_dir)
        if scope["state"] == "동기" and scope["branch"]:
            was_behind = scope["behind"]
            co_rc, _co = self._run_git_fn(["-C", d, "checkout", scope["branch"]])
            p_rc, _p = self._run_git_fn(["-C", d, "pull", "--ff-only"])
            if co_rc == 0 and p_rc == 0:
                scope["pulled"] = True
                scope["behind"] = 0
                scope["note"] = (
                    f"board ff-pull 동기 완료 (branch 유지·behind {was_behind}→0)"
                )
            else:
                scope["note"] = (
                    f"⚠ board 동기 실패 (checkout rc={co_rc}·pull rc={p_rc}) — 수동 확인"
                )
        elif scope["behind"] and scope["behind"] > 0:
            scope["note"] = git_freshness._behind_warning(scope)
        return scope

    def _collect_freshness(self) -> list[dict]:
        """각 scope 를 fetch → behind/ahead → clean·ff 면 ff-pull 한다.

        미분리면 한 scope 로 접힌다. 반환 목록을 Git 절 freshness surface 에 쓴다.
        부트스트랩 *첫 단계*로 돌려(run() 순서) 이후 git/board 수집이 pull 반영 상태를
        dump 하게 한다. 실 fetch/pull 은 DI 러너(`_run_git_fn`) 경유 — 테스트는 mock.
        """
        freshness = [self._sync_scope("PM 홈", self._home.root, is_home=True)]
        for task_slot in self._task_workspace_slots:
            wt_dir = Path(self._worktree_cwd(task_slot))
            if wt_dir.resolve() != self._home.root.resolve():
                freshness.append(
                    self._sync_scope(
                        f"작업 폴더 {task_slot}",
                        wt_dir,
                        is_home=False,
                        task_slot=task_slot,
                    )
                )
        return freshness

    def _require_task_slot_ownership(self, slot: str, action: str) -> None:
        """task 슬롯 변경/검증 직전 strict 장부 소유권 gate (TOCTOU 차단)."""
        task = self._task_name
        try:
            owned = (
                bool(pool_store.lease_owned_by_task_strict(self._home, slot, task))
                if task is not None
                else False
            )
        except Exception as exc:
            print(
                f"[중단·소유권 재검증] task 슬롯 {slot!r} 장부 조회 실패 — {exc}. {action}을 실행하지 않습니다.",
                file=sys.stderr,
            )
            raise SystemExit(1) from exc
        if not owned:
            print(
                f"[중단·소유권 재검증] task 슬롯 {slot!r}은 더 이상 state=leased && session={task!r}가 아닙니다 — {action}을 중단합니다. 새 소유자의 작업공간을 변경하거나 검증하지 않습니다.",
                file=sys.stderr,
            )
            raise SystemExit(1)

    def _bound_session_name(self) -> str | None:
        """차수 유도/본문 dump 귀속용 bound 세션 키 — **log/pm_state 귀속 필터** 전용.

        **task 모드(`--task <task>`)면 `task:<task>` 태그**를 반환한다 — handoff write 측이
        task 세션 종료를 `(task:<task>)` sentinel 태그로 박고 task 서술 pm_state 를 쓰므로
        이 read 측이 같은 키로 자기 태그 entry(차수·본문)를 되읽어 resume 루프를
        닫는다(write↔read 대칭). task 진입은 task 앵커만 사용한다.

        정체성 확정 전(수집 앞단 호출)이면 None 이다.
        """
        if self._task_name is None:
            return None
        return f"{bootstrap_rules._TASK_TAG_PREFIX}{self._task_name}"

    def _collect_log_entry(self) -> dict | None:
        """**자기 슬롯**의 마지막 handoff entry 제목(date·type·title) + **본문 전체**를 수집한다.

        인계 컨텍스트 dump 를 전역 마지막 entry 가 아니라 *자기
        슬롯의 마지막 handoff*(태그 필터·무태그 폴백=slot-1)로 좁혀 자기 컨텍스트를 복원한다
        (타 슬롯 entry 본문 유입 차단). **MF-2**: bound 세션이 해소됐는데 자기 handoff 가 0개
        (fresh 슬롯)면 **전역 마지막으로 폴백하지 않는다** — 그러면 타 슬롯 handoff 본문·branch 가
        유입된다("타 슬롯 최소 유입" 정면 위반). fresh
        슬롯은 이 슬롯의 첫 세션이라 인계 dump 없음(None). 전역 폴백은 bound 가 **진짜 미해소**
        (식별 자체 없음)일 때만 — 그땐 현행 표시(전역 마지막 제목+본문)를 잃지 않는다.
        """
        if not self._log_file.exists():
            return None
        log_text = fs.read_text_shared(self._log_file, encoding="utf-8")
        bound_session = self._bound_session_name()
        if bound_session is None:
            entry = bootstrap_history.parse_log_last_entry(log_text)
            if entry is None:
                return None
            entry = dict(entry)
            entry["body"] = bootstrap_history.extract_last_log_entry_body(log_text)
            return entry
        slot_entry = bootstrap_history.extract_slot_handoff_entry(
            self._home, log_text, bound_session=bound_session
        )
        if slot_entry is not None:
            return slot_entry
        return None

    def _resolve_pm_state_file(self) -> Path | None:
        """이 task 의 서술 pm_state 경로 (read-only).

        명시 주입(`self._pm_state_file`·hermetic 테스트)이 있으면 그대로. 없으면 task 바인딩이
        해소한 `.local/tasks/<task>/pm_state.md`(handoff write 측 `_task_pm_state_file` 미러)다 —
        `bind_task` 가 파일 존재를 보장한다. 부트스트랩은 pm_state 를 *읽기만* 한다.
        """
        if self._pm_state_file is not None:
            return self._pm_state_file
        return self._task_pm_state_file

    def _open_todos(self) -> list[dict]:
        """이 task 의 열린 todo — task 별 파일(`.local/tasks/<task>/todo.json`) 단일 출처.

        옛 "남은 작업 전체 그림" 절을 대체한다. 그 절은 손으로 쓰는 서술이라 갱신이 밀리면
        부트스트랩이 조용히 낡은 사실을 dump 했다 — 이 목록은 기계가 쓰는 자리에서만 온다.
        판독 실패는 여기서 접지 않는다(같은 자리를 읽는 슬롯 검증과 같은 규율).
        """
        if not self._task_name:
            return []
        return list(task_store.read_task_todos(self._home, self._task_name))

    def _collect_handoff_context(self, log_text: str | None = None) -> dict | None:
        """bound slot pm_state 차수(+ log 교차검증) + 이 task 의 열린 todo 를 수집한다.

        반환: {"session_num": int|str|None, "session_stale": bool,
               "state_session_num": int|str|None, "todos": list[dict],
               "state_path": str} 또는 None(pm_state·log·todo 전부 미해소 — surface 생략·graceful).
        - `session_num`: pm_state(`session_history.infer_next_session_num`) 와 log(`parse_last_handoff_session_num`+1)
          을 `reconcile_session_num` 으로 교차검증한 *최종* 차수. pm_state 해소 경로가 우선이되
          (현행 무변경), pm_state 미해소(template/부재)면 log-derived N+1 로 **폴백**하고, pm_state
          가 해소돼도 log-derived 가 더 크면 **log 우선(max)** + stale 표시(머신 간 미동기).
        - `session_stale`: pm_state 가 log 보다 뒤처져 log 를 택했으면 True(경고 1줄).
        - `state_session_num`: pm_state-derived 원값(stale 경고 메시지의 진단용).
        - `todos`: 이 task 의 열린 todo 목록(`.local/tasks/<task>/todo.json` · 없으면 빈
          리스트 — 호출부가 등록 경로를 안내).
        pm_state·log·todo 전부 미해소면 None — 호출부가 현행(placeholder)로 폴백한다.
        """
        state_path = self._resolve_pm_state_file()
        state_text: str | None = None
        if state_path is not None and state_path.exists():
            state_text = fs.read_text_shared(state_path, encoding="utf-8")
        state_num = (
            session_history.infer_next_session_num(state_text)
            if state_text is not None
            else None
        )
        session_num = session_history.infer_next_task_session_num(
            state_text, log_text, self._task_name
        )
        session_stale = (
            isinstance(state_num, int)
            and isinstance(session_num, int)
            and (session_num > state_num)
        )
        todos = self._open_todos()
        if session_num is None and (not todos) and (state_text is None):
            return None
        return {
            "session_num": session_num,
            "session_stale": session_stale,
            "state_session_num": state_num,
            "todos": todos,
            "state_path": (
                bootstrap_report._display_path_text(state_path)
                if state_path is not None
                else "pm_state.md"
            ),
        }

    def _read_log_text(self) -> str | None:
        """log/current.md 원문을 읽는다 (차수 log-폴백/user 연속성 공용·부재면 None)."""
        if not self._log_file.exists():
            return None
        return fs.read_text_shared(self._log_file, encoding="utf-8")

    def _handoff_commit_author(self, handoff_header: str) -> str | None:
        """직전 handoff entry 헤더 줄을 담은 commit 의 author email (pickaxe).

        `git log -1 --format=%ae -S<header> -- <log_file>` — 그 헤더 줄을 추가한 commit 을
        pickaxe(-S)로 찾아 author email 을 얻는다. log/current.md 는 REPO가 소유하므로
        `-C <REPO>` 로 명시(러너 cwd=worktree 무관·`_collect_board_git` 동형). rc≠0·빈 출력·git
        실패 → None(fail-soft·줄 생략). DI 러너(`_run_git_fn`) 경유 — 테스트는 mock.
        """
        rc, out = self._run_git_fn(
            [
                "-C",
                str(self._home.root),
                "log",
                "-1",
                "--format=%ae",
                f"-S{handoff_header}",
                "--",
                str(self._log_file),
            ]
        )
        if rc != 0:
            return None
        stripped = out.strip()
        if not stripped:
            return None
        return stripped.splitlines()[0].strip() or None

    def _collect_user_continuity(self, log_text: str | None) -> str | None:
        """직전 handoff 작성자(commit author) vs 현재 user 비교 → 연속성 1줄.

        일치 → `사용자: <email> (직전 handoff 작성자와 동일 — 연속)`.
        불일치 → `⚠ 직전 handoff 는 다른 사용자(<email>) — pending intent 는 프로젝트 상태로 취급`.
        어느 쪽이든 미해소(handoff 부재·author 조회불가·user 미상) → None(줄 생략·fail-soft).
        """
        header = bootstrap_history.last_handoff_header_line(
            self._home, log_text, bound_session=self._bound_session_name()
        )
        if header is None:
            return None
        author = self._handoff_commit_author(header)
        if not author:
            return None
        current = lease_store.user_name(self._home)
        if not current:
            return None
        if current == author:
            return f"사용자: {current} (직전 handoff 작성자와 동일 — 연속)"
        return f"⚠ 직전 handoff 는 다른 사용자({author}) — pending intent 는 프로젝트 상태로 취급"

    def _collect_dashboard_others(self) -> dict | None:
        """타 슬롯 대시보드 섹션(자기 제외) light dump 데이터를 수집한다.

        - `wiki/log/dashboard.md` 존재 → `dashboard.parse_dashboard_sections`로
          파싱해 자기 task 제외 +
          **활성(leased) 슬롯과 교집합** 섹션들을
          `{"mode":"dashboard","others":[{"session","body"}]}` 로 반환. 자기 섹션은 표시 안 함
          (자기 컨텍스트는 pm_state/log 몫). `--done` 으로 release 된 idle 슬롯 섹션은
          활성 집합에 없어 배제된다(MF-1·codex·stale idle 노출 0). 활성 집합 판정불가(장부 부재)면
          필터 no-op(현행 표시 보존). 남는 타 슬롯 0개면 None(절 생략).
        - 대시보드 부재/읽기 실패(OSError) → `_lease_others` 폴백(`{"mode":"lease","others":[…]}`).
        - 어느 쪽도 타 PM 0개 → None(절 생략·graceful).
        """
        dash_file = pm_paths._dashboard_file(self._home)
        if dash_file.exists():
            try:
                text = fs.read_text_shared(dash_file, encoding="utf-8")
            except OSError:
                text = None
            if text is not None:
                sections = dashboard.parse_dashboard_sections(text)
                bound = self._task_name
                active = bootstrap_history._active_leased_sessions(self._home)
                others = [
                    {"session": key, "body": body}
                    for key, body in sections
                    if key != bound and (active is None or key in active)
                ]
                return {"mode": "dashboard", "others": others} if others else None
        lease_others = bootstrap_history._lease_others(self._home, self._task_name)
        return {"mode": "lease", "others": lease_others} if lease_others else None

    def run(
        self,
        *,
        task: str,
        output_json: bool = False,
        with_pytest: bool = False,
        force: bool = False,
    ) -> int:
        """부트스트랩 정보를 수집해 출력한다.

        task:        세션 정체성이다. 보유 작업 폴더 집합은 `pool_store.slots_for_task` 로
                     자동 수령하고, 슬롯 대여·편입은 pm-config alloc/worktree add 가 한다.
        with_pytest: True 면 pytest 회귀 실행, False (default) 면 skip.
                     default skip 인 이유는 모듈 docstring 참조.
        force:       열린 기록(종료 기록 없이 남은 task)을 이어받겠다는 **사용자 응답**. 기본
                     False — 자동 승낙 값(환경변수·설정 키·기본 True 인자)은 없다.

        반환: 0=성공, 1=실패 (sys.exit 로 중단할 수도 있음).
        """
        now = datetime.datetime.now(tz=clock.KST)
        timestamp = now.strftime("%Y-%m-%d %H:%M KST")
        self._command_environment = python_env._detect_command_environment(self._home)
        preflight_rc = self._phase0_preflight()
        if preflight_rc != 0:
            return preflight_rc
        task_slot_leases = self._resolve_task_slot_leases(task)
        if task_slot_leases is None:
            return 1
        task_info = self._bind_task_or_reject(task, force=force)
        if task_info is None:
            return 1
        self._task_name = task_info["name"]
        self._task_pm_state_file = Path(task_info["pm_state_path"])
        if (
            self._validate_task_slot_set(task_info["name"], leases=task_slot_leases)
            != 0
        ):
            return 1
        for repo_name in dict.fromkeys(
            (getattr(lease, "repo", None) for lease in task_slot_leases)
        ):
            if repo_name:
                bootstrap_slots._reconcile_protected_sidecar(self._home, repo_name)
        freshness = self._collect_freshness()
        board = self._collect_board()
        pytest_result = self._collect_pytest_for_scope() if with_pytest else None
        git = self._collect_git()
        if self._task_name is not None:
            git["task_cwd_slot"] = (
                self._task_workspace_slots[0] if self._task_workspace_slots else None
            )
            git["task_workspace_count"] = len(self._task_workspace_slots)
        git["board_git"] = self._collect_board_git()
        git["freshness"] = freshness
        log_text = self._read_log_text()
        log_entry = self._collect_log_entry()
        git["user_continuity"] = self._collect_user_continuity(log_text)
        handoff_ctx = self._collect_handoff_context(log_text)
        dashboard_others = self._collect_dashboard_others()
        if output_json:
            data = bootstrap_report._build_json(
                board,
                pytest_result,
                git,
                log_entry,
                timestamp,
                handoff_ctx,
                dashboard_others,
                environment=self._command_environment,
                task_name=self._task_name,
            )
            if task_info is not None:
                task_info["slot_set"] = self._task_slot_set_rows(
                    task_info["name"], leases=task_slot_leases
                )
                data["task"] = task_info
            console.write_machine_line(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            markdown = bootstrap_report._build_markdown(
                board,
                pytest_result,
                git,
                log_entry,
                timestamp,
                handoff_ctx,
                dashboard_others,
                environment=self._command_environment,
                task_name=self._task_name,
                state_path=self._pm_state_display_path(),
            )
            print(markdown)
            card = bootstrap_card._build_command_card_markdown(
                self._home,
                self._task_name,
                environment=getattr(self, "_command_environment", None),
            )
            print()
            print(card)
            if task_info is not None:
                task_info["slot_set"] = self._task_slot_set_rows(
                    task_info["name"], leases=task_slot_leases
                )
                print()
                print(bootstrap_report._build_task_identity_markdown(task_info))
        if board.get("lint_blocking"):
            print(
                bootstrap_report.format_blocking_lint_warning(
                    board["lint_gate_output"], lint_rc=board.get("lint_gate_rc")
                ),
                file=sys.stderr,
            )
            return 1
        return 0

    def _reject_worktree_copy_anchor(self) -> bool:
        """실행 Home의 등록 worktree 앵커를 검사한다. 판정 실패는 전파한다."""
        pm_home = pm_paths._pm_home_worktree_misanchor(self._home)
        if pm_home is None:
            return False
        print(
            f"[중단·0단계] 부트스트랩을 worktree(코드 전용) 트리에서 실행했습니다 — 세션 상태는 PM 홈이 소유합니다. 이대로면 이 worktree 를 세션 진실로 잘못 dump 합니다.\n  → PM 홈에서 실행하세요:  cd {pm_home}\n  (현재 엔진 앵커: {self._home.root})",
            file=sys.stderr,
        )
        return True

    def _phase0_preflight(self) -> int:
        """0단계 — dump 이전 엔진 앵커 검증. 0=통과·1=거부(FAIL-LOUD).

        엔진 파일 위치(REPO)가 PM 홈이 아니라 worktree 사본이면 거부한다
        (`pm_paths._pm_home_worktree_misanchor`와 명시 Home 사용).
        """
        if self._reject_worktree_copy_anchor():
            return 1
        return 0

    _HANDOVER_MAX_ITEMS = 5

    def _handover_claimed_tickets(self, task: str) -> list[str]:
        """그 task 명의로 claim 중인 티켓 id — board 조회(읽기만)."""
        lens = ["--task", task]
        try:
            rc, output = self._run_board_fn(["list", "--status", "claimed", *lens])
        except (OSError, subprocess.SubprocessError):
            return []
        if rc != 0:
            return []
        seen: list[str] = []
        for match in re.finditer("\\b(?:[a-z0-9_]+-)?T-\\d+\\b", output):
            if match.group(0) not in seen:
                seen.append(match.group(0))
        return seen

    def _handover_delegate_runs(self, slots: list[str]) -> list[str]:
        """미회수 위임 준비와 그 라운드 단계 — 준비 사본 디렉터리를 읽어 실값으로 낸다.

        준비 사본은 **그 세션이 잡고 있던 작업 폴더 안**에 생긴다(위임의 cwd 가 슬롯이다) —
        PM 홈 아래가 아니다. 그래서 뿌리는 `slots`(보유 작업 폴더 목록)이 준다. 상대 루트는
        준비 사본을 만드는 쪽(`delegate_ledger.TICKET_COPY_REL_ROOT`)이 소유한다.
        """
        rows: list[str] = []
        for slot in slots:
            root = (
                pm_paths.slot_path(self._home, slot)
                / delegate_ledger.TICKET_COPY_REL_ROOT
            )
            if not root.is_dir():
                continue
            for ticket_dir in sorted(root.glob("*/*/*")):
                if not ticket_dir.is_dir():
                    continue
                rounds = (
                    sorted((p.name for p in (ticket_dir / "rounds").glob("*.md")))
                    if (ticket_dir / "rounds").is_dir()
                    else []
                )
                stage = rounds[-1] if rounds else "(라운드 파일 없음 = 1라운드)"
                rows.append(
                    f"{bootstrap_report._display_path_text(ticket_dir)} · 라운드 단계: {stage}"
                )
        return rows

    def _handover_slots(self, task: str) -> list[tuple[str, str]]:
        """그 task 가 보유한 작업 폴더와 그 갈래 — 장부 행 + live 브랜치.

        반환은 `(슬롯, 표시 문자열)` 쌍이다 — 같은 목록을 위임 준비 뿌리로도 쓴다(조회 축을
        하나 더 열지 않는다).
        """
        if not task:
            return []
        try:
            leases = [
                l
                for l in pool_store.list_leases(self._home)
                if worktree_records.lease_is_open(l) and l.session == task
            ]
        except (OSError, KeyError, TypeError):
            return []
        rows: list[tuple[str, str]] = []
        for lease in leases:
            branch = worktree_git.current_branch(self._home, lease.slot)
            rows.append(
                (lease.slot, f"{lease.slot} (갈래: {branch or '(미상·detached)'})")
            )
        return rows

    def _handover_pm_state(self, task: str) -> str | None:
        """그 task 의 서술 파일 경로."""
        if not task:
            return None
        try:
            return bootstrap_report._display_path_text(
                pm_paths.task_pm_state_file(self._home, task)
            )
        except worktree_records.InvalidTaskName:
            return None

    def _handover_last_log(self) -> str | None:
        """기록(log/current.md)의 마지막 entry 제목."""
        if not self._log_file.exists():
            return None
        try:
            entry = bootstrap_history.parse_log_last_entry(
                fs.read_text_shared(self._log_file, encoding="utf-8")
            )
        except (OSError, UnicodeDecodeError):
            return None
        if entry is None:
            return None
        return f"[{entry.get('date', '')}] {entry.get('type', '')} | {entry.get('title', '')}"

    def _render_handover(self, *, task: str, indent: str = "  ") -> str:
        """이전 세션이 하던 것 — **읽기만** 한다(장부·log·board 를 쓰지 않는다).

        재료는 전부 이미 조회 가능한 것들이다(새 기록 파일 0): claim 중인 티켓 · 미회수 위임
        준비와 그 라운드 단계 · 서술 pm_state · 마지막 기록 항목 · 보유 작업 폴더와 갈래.
        거부(묻는 자리)와 강제 인계가 같은 renderer 를 쓴다 — 표시 자리는 하나다.
        """
        claimed = self._handover_claimed_tickets(task)
        slot_rows = self._handover_slots(task)
        runs = self._handover_delegate_runs([slot for slot, _text in slot_rows])
        slots = [text for _slot, text in slot_rows]
        pm_state = self._handover_pm_state(task)
        last_log = self._handover_last_log()

        def _join(values: list[str]) -> str:
            if not values:
                return "(없음)"
            head = values[: self._HANDOVER_MAX_ITEMS]
            more = (
                ""
                if len(values) <= self._HANDOVER_MAX_ITEMS
                else f" 외 {len(values) - self._HANDOVER_MAX_ITEMS}건"
            )
            return ", ".join(head) + more

        lines = [
            f"{indent}이전 세션이 하던 것(읽기만·복원 표시):",
            f"{indent}- claim 중인 티켓: {_join(claimed)}",
            f"{indent}- 미회수 위임 준비: {_join(runs)}",
            f"{indent}- 서술 pm_state: {pm_state or '(없음)'}",
            f"{indent}- 마지막 기록: {last_log or '(없음)'}",
            f"{indent}- 보유 작업 폴더: {_join(slots)}",
        ]
        return "\n".join(lines)

    def _bind_task_or_reject(self, task: str, *, force: bool = False) -> dict | None:
        """`--task` 신규/resume 바인딩. 열린 기록이 있고 `--force` 가 없으면 None(거부).

        `task_lifecycle.bind_task` 로 신규/resume 을 처리한다 — 열린 기록(종료 기록 없음)은
        `TaskActiveElsewhere` → 여기서 그 기록의 정체와 이전 세션이 하던 것을 찍고 강제 여부를
        **묻는다**(None → caller 가 dump 이전 중단). `force` 면 기록을 먼저 닫고
        (`close_task_session` — 기존 프리미티브) 같은 bind 경로를 그대로 탄다: 강제 전용 분기도,
        자동 승낙 값도 없다. 성공 시 surface 용 dict(name·prefix·action·서술 pm_state 경로/존재)를
        반환한다. task 는 슬롯 축과 직교라 이 바인딩은 `.local/slots/` 를 건드리지 않는다.
        """
        try:
            session = task_store.take_task_session_claim(self._home, task)
            if force:
                record = pool_store.find_task(self._home, task)
                if record is not None and record.open and (record.session != session):
                    print(
                        f"[강제 인계] task {task!r} 의 열린 기록(task 시작: {getattr(record, 'started', '') or '(미상)'})을 이어받습니다 — 이전 세션이 하던 것:\n{self._render_handover(task=task, indent='  ')}",
                        file=sys.stderr,
                    )
                    task_lifecycle.close_task_session(self._home, task)
            record, action = task_lifecycle.bind_task(
                self._home,
                task,
                session=session,
                registered_repos=slot_resolution._registered_repos(
                    self._home, self._areas_file
                ),
            )
        except task_store.NoTaskSessionClaim as exc:
            print(f"[중단] {exc}", file=sys.stderr)
            return None
        except worktree_records.TaskArchived as exc:
            archives = ", ".join((path.name for path in exc.archives))
            print(
                f"[중단] task {task!r} 은(는) 종료 archive로 보관되어 있어 새 task로 만들지 않습니다 ({archives}).\n  → `pm-config.py task reopen {task}` (복수 후보면 `--archive <basename>` 추가) 후 `{bootstrap_card._runtime_skill_entry('pm-bootstrap')} --task {task}` 로 재진입하세요.",
                file=sys.stderr,
            )
            return None
        except worktree_records.TaskActiveElsewhere as exc:
            print(
                f"[중단] task {task!r} 에 **열린 세션 기록**이 있습니다 (task 시작: {exc.started or '(미상)'}) — 종료 기록이 없습니다. 그 창이 아직 살아 있는지는 기계가 알 수 없습니다(프로세스 번호로는 판정하지 않습니다) — 사용자에게 확인하세요.\n{self._render_handover(task=task, indent='  ')}\n  → 택1 (사용자 응답 필요):\n     (a) 그 창을 쓰세요.\n     (b) 그 세션이 끝난 것이면 기록을 닫으세요: {bootstrap_rules._CARD_TOOL_INVOKE}/pm_config.py task close {task}\n     (c) 이어받으려면 같은 명령에 `--force` 를 붙여 다시 실행하세요.",
                file=sys.stderr,
            )
            return None
        except worktree_records.InvalidTaskName as exc:
            print(
                f"[중단] task 명 {task!r} 이(가) 부적합합니다 — {exc.reason}.\n  → task 명은 경로 문자(`/`·`\\`·`..`·선행 `.`) 없는 단일 이름이어야 하고, 슬롯 폴더 이름 패턴(<등록 repo>_<N>)은 쓸 수 없습니다.",
                file=sys.stderr,
            )
            return None
        except Exception as exc:
            print(
                f"[중단] task {task!r} 생성/재개 중 pm_state 보장에 실패했습니다 — {exc}. task 장부만 있는 반쪽 상태로 진행하지 않습니다.",
                file=sys.stderr,
            )
            raise
        pm_state = pm_paths.task_dir(self._home, task) / "pm_state.md"
        if not pm_state.exists():
            print(
                f"[중단] task {task!r} 바인딩 뒤 pm_state가 생성되지 않았습니다 — {pm_state}. task state 즉시 생성 계약을 지원하는 worktree_pool 엔진으로 동기화하세요.",
                file=sys.stderr,
            )
            return None
        return {
            "name": record.name,
            "prefix": record.prefix,
            "action": action,
            "started": getattr(record, "started", None),
            "pm_state_path": bootstrap_report._display_path_text(pm_state),
            "pm_state_exists": True,
        }

    def _resolve_task_slot_leases(self, task: str) -> list | None:
        """task 보유 슬롯 집합을 조회한다 — `None`=해소 실패, `[]`=정당한 0슬롯.

        task bind/state write 전에 호출해 실패가 반쪽 영속 기록을 남기지 않게 한다. 실제 빈 집합과
        worktree_pool/primitive 부재·None 반환·예외를 구분하는 단일 choke point다."""
        try:
            resolved = pool_store.slots_for_task_strict(self._home, task)
            if resolved is None:
                raise RuntimeError("slots_for_task가 None을 반환")
            return list(resolved)
        except (OSError, ValueError) as exc:
            print(
                f"[중단·진입검증] task {task!r} 보유 슬롯 장부 조회 실패 — {exc}. 실제 0슬롯으로 간주하지 않고 부분 dump 없이 중단합니다.",
                file=sys.stderr,
            )
            return None

    def _validate_task_slot_set(self, task: str, *, leases: list | None = None) -> int:
        """task 보유 슬롯 **전수** 0단계 검증 — fault 1+ = 진입 차단.

        `slots_for_task(task)` 보유 집합 각각에 기존 0단계 프리미티브(folder 실재·불완전 생성·
        보호브랜치·기록↔live compare — `_phase0_*`·`compare_slot_git` **재사용**·판정 재구현 0)를
        돌려 fault 를 모은다. 하나라도 있으면 **전 fault 를 한 번에** stderr 로 표시하고(각 슬롯·
        판정 근거·해소 커맨드 실값·순차 발견 금지) rc 1 로 진입을 차단한다 — 부분 dump 금지(0단계
        동형·"해소 전 진행 없음"·ⓐC). 감지=기계·해소=사용자(부트스트랩 밖 record/prune-stale 등
        실행 후 재진입). 0개 보유 = no-op(검증 대상 없음·진입). worktree_pool/slots_for_task 부재나
        조회 예외는 실제 빈 집합과 구분해 fail-loud 한다(집합을 모르면 검증을 통과시킬 수 없음).
        반환 0=통과·1=차단(FAIL-LOUD). `leases` 미주입 직접 호출도 같은 해소 choke를 탄다."""
        if leases is None:
            leases = self._resolve_task_slot_leases(task)
        if leases is None:
            self._task_workspace_slots = ()
            return 1
        self._task_workspace_slots = tuple(
            sorted(
                {
                    str(slot_id)
                    for lease in leases
                    if (slot_id := getattr(lease, "slot", None))
                }
            )
        )
        faults: list[tuple[str, tuple[str, str, str]]] = []
        for lease in sorted(leases, key=lambda x: getattr(x, "slot", "")):
            fault = bootstrap_slots._task_slot_fault(
                self._home, lease, task, run_git_fn=self._run_git_fn
            )
            if fault is not None:
                faults.append((getattr(lease, "slot", "(미상)"), fault))
        if not faults:
            return 0
        lines = [
            f"[중단·진입검증] task {task!r} 보유 슬롯 {len(faults)}개가 0단계 검증에 실패했습니다 — 진입 차단(부분 dump 금지). **아래 전부를** 해소한 뒤 다시 부트스트랩하세요 (해소 전 진행 없음·순차 발견 금지):"
        ]
        for slot_id, (label, reason, resolve) in faults:
            lines.append(f"  - {slot_id}: {label}")
            lines.append(f"      근거: {reason}")
            lines.append(f"      해소: {resolve}")
        print("\n".join(lines), file=sys.stderr)
        return 1

    def _task_slot_set_rows(
        self, task: str, *, leases: list | None = None
    ) -> list[dict]:
        """task 보유 슬롯 전수 열거 행렬 데이터 (부작용 0).

        `slots_for_task(task)` → 슬롯별 {slot·repo·branch·head·record_live·dirty} dict 리스트를
        돌려준다 — 진입 검증(_validate_task_slot_set) **통과 후** surface 렌더(markdown 행렬·JSON)가
        소비한다. run()은 최초 strict 조회·검증을 통과한 `leases` 스냅샷을 주입해 재조회 사이
        장부 변경/오류로 surface가 검증 결과와 어긋나는 일을 막는다. 직접 호출 호환 경로만
        slots_for_task를 조회한다(빈 결과는 "(없음)" 분기). 조회 실패는 전파한다."""
        if leases is None:
            leases = list(pool_store.slots_for_task(self._home, task) or [])
        else:
            leases = list(leases)
        rows: list[dict] = []
        for lease in sorted(leases, key=lambda x: getattr(x, "slot", "")):
            slot_id = getattr(lease, "slot", None)
            if not slot_id:
                continue
            rows.append(bootstrap_slots._task_slot_row(self._home, lease, slot_id))
        return rows


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pm_bootstrap.py",
        description="PM 세션 시작 부트스트랩 헬퍼 — 기계 측정 부분을 한 명령으로 dump 한다.",
    )
    parser.add_argument(
        "--task",
        metavar="이름",
        required=True,
        help="task 이름 — 세션 정체성이다. 부트스트랩의 유일한 진입이며 보유 작업 폴더는 장부에서 자동 수령한다(슬롯 대여·편입은 pm-config alloc/worktree add 소관).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="output_json",
        help="기계 파싱용 JSON 출력 (기본: markdown).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="열린 기록(종료 기록 없이 남은 task)을 이어받는다 — 사용자 응답 자리다. 기본은 거부이고 자동 승낙 값은 없다.",
    )
    parser.add_argument(
        "--with-pytest",
        action="store_true",
        help="pytest 회귀 측정 opt-in (default skip — handoff entry 가 숫자를 기록한다고 가정).",
    )
    return parser


def _main(home, argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        identity_args.validate_task_name(
            args.task, slot_resolution._registered_repos(home)
        )
    except identity_args.InvalidTaskName as exc:
        build_parser().error(str(exc))
    bootstrap = PmBootstrap(home)
    return bootstrap.run(
        output_json=args.output_json,
        with_pytest=args.with_pytest,
        task=args.task,
        force=args.force,
    )


def main(home, argv: list[str] | None = None) -> int:
    return _main(home, argv)
