"""묶음 종결(close) — 고정 여섯 단계·상태 관측 기반 재개·이미 통합된 묶음 복구 (층: cli)."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Callable

from pm_engine.app import board_sync, cluster_binding, cluster_code_tree, ticket_access
from pm_engine.cli import board_ticket, finish_record
from pm_engine.domain import cluster_rules, finish_rules, ticket_rules
from pm_engine.infra import cluster_store, finish_git, fs, git, pm_paths, ticket_store


# ── close 파이프라인 (묶음 종결) ───────────────────────────────────────────
#
# 종결은 순서가 있는 여섯 단계다: 리뷰 게이트 처분(확인 생성) → 티켓별 완료 기록 → 슬롯 커밋 →
# 통합 브랜치로 재배치 → 머지 → board·포인터 커밋. 사람이 순서를 규칙으로
# 지키던 자리를 한 커맨드가 가져간다 — 순서가 판정 결과를 바꾸던 축(사설 참조·측정 폭)은 이미
# 판정 기준 교체로 닫혔으므로, 남은 것은 "빠뜨리지 않는" 축이다(포인터 커밋 누락이 실측된
# 잔여다).
#
# **종결은 슬롯을 반납하지 않는다.** 슬롯은 task 가 빌리는 것이라 반납은 task 종료
# (`pm-config task end`)나 사용자 반납(`pm-config release`) 몫이다. 종결이 도는 작업 트리는
# 묶음 기록의 `branch`(없으면 `base_branch`)를 체크아웃한 작업 트리 정확히 하나다
# (`app.cluster_code_tree.resolve_code_tree`) — task 가 슬롯을 몇 개 쥐었는지는 보지 않고, 대여 기록은 찾는 곳을
# 정하는 데만 읽는다.
#
# **막을 조건은 첫 부작용 전에 전부 모아 한 번에 낸다**(`preflight`). 각 단계가 자기 차례에
# 와서야 자기 조건을 보면 한 실행에 차단 사유가 하나씩만 나오고, 첫 완주까지 사람이 같은
# 명령을 여러 번 친다(실측). 그래서 단계마다 **읽기 전용 선검사**를 선언으로 달고, 실행 전에
# 그것만 전부 돌려 사유를 누적한다. 편입 기준은 "읽기 전용"이 아니라 **단조성**이다 — 앞
# 단계가 해소할 수 있는 조건(작업 트리 dirty·log entry 부재·게이트 미처분)을 선검사에 넣으면
# 정상 실행이 첫 줄에서 막힌다. 실행해야 아는 것(머지 충돌·회귀 red)은 선언에서 `None` 이다.
#
# **재실행이 곧 재개다.** 각 단계는 자기 부작용이 이미 있는지 **관측**해서 건너뛴다(티켓이
# done 인가 · 커밋할 변경이 남았나 · 통합 브랜치의 조상인가). 장부의
# 단계 기록은 그 관측을 대신하지 않는다 — 기록과 실제가 어긋나면(중단·외부 원복) 기록을 믿는
# 재개는 없는 커밋 위에서 진행한다. 기록은 어디까지 갔는지 사람이 읽는 값이다.
#
# 크기 1 묶음도 같은 경로를 탄다(특례 없음) — 티켓 하나짜리 종결이라고 다른 코드가 도는 일은
# 없다. 발행이 만드는 크기 1 장부는 기준 브랜치(`base_branch`)를 싣고 묶음 브랜치(`branch`)만
# 비우므로 머지가 **무대상**이다. 기준 브랜치 미선언은 그 축이 아니라 판정 기준의 부재라 정지다.

_CLOSE_STEP_KEY = "close_step"            # 장부에 남기는 단계 진행 기록(보조 기록)
_CLUSTER_STATUS_OPEN = "open"             # 열거의 소유자는 board — 그 열거 안에 있을 때만 쓴다
_CLUSTER_STATUS_CLOSING = "closing"
_CLUSTER_STATUS_CLOSED = "closed"
# 처분 종결(`ticket_rules.STATUS_DIRS` 안의 값 · 열거의 소유자는 ticket_rules). `done` 과 달리 구현이 끝나서
# 닫힌 것이 아니라 흡수·폐기로 닫힌 상태다 — 완료 기록 대상도 코드 산출도 없으므로 종결
# 파이프라인의 코드 단계는 이 멤버를 대상으로 삼지 않는다.
_TICKET_STATUS_DISCARDED = "discarded"
_CLOSE_BOARD_POINTER_PATH = ".project_manager/board"
_CLOSE_SUBMODULE_MODE = "160000"          # `ls-files --stage` 가 gitlink 에 쓰는 모드
_CLOSE_MERGE_MESSAGE = "{unit} merge — {title}"
_CLOSE_BOARD_MESSAGE = "{unit} board — {title}"


_CLOSE_INTEGRATION_UNDECLARED = (
    "{cluster} 장부가 통합 브랜치(base_branch)를 선언하지 않았다 — 재배치·머지의 대상이 "
    "그 선언이다. `board.py cluster show {cluster}` 로 장부를 확인하고 `base_branch` 를 채운 "
    "뒤 다시 실행하라."
)

_CLOSE_INTEGRATION_BRANCH_MISSING = (
    "통합 브랜치 {integration} 를 이 코드 트리에서 찾지 못했다 ({tree}) — 장부가 선언한 "
    "이름과 이 트리의 브랜치를 대조하라."
)

_CLOSE_SOURCE_BRANCH_MISSING = "묶음 브랜치 {source} 를 이 코드 트리에서 찾지 못했다 ({tree})."

_CLOSE_INTEGRATION_TREE_MISSING = (
    "{integration} 를 체크아웃한 작업 트리를 찾지 못했다 — 같은 브랜치를 두 트리가 동시에 "
    "체크아웃할 수 없으므로 머지는 그 트리에서만 된다. 통합 브랜치를 가진 트리를 열어 두고 "
    "다시 실행하라."
)

_CLOSE_INTEGRATION_TREE_DIRTY = (
    "{integration} 트리에 남은 변경이 있어 머지하지 않는다 ({count}건 · {tree})."
)

# 작업 트리 해소의 두 멈춤 — 브랜치를 쥔 트리가 없을 때와 둘 이상일 때다. 모호 문구는 통합
# 트리 해소(`_integration_worktree`)도 같이 쓴다(한 규칙 한 문구).


_CLOSE_TICKET_NOT_CLAIMED = (
    "{ticket} 가 claimed 가 아니다({status}) — 완료 기록은 claimed 티켓만 done 으로 옮긴다."
)

_CLOSE_OPEN_ALL_DONE = (
    "{cluster} 장부는 open 인데 멤버 {members} 가 이미 전부 done 이다 — 정상 종결은 이 "
    "반쪽 상태를 닫지 않는다(그 완료 기록은 이 파이프라인이 만든 것이 아니라 커밋·재배치·머지가 "
    "무엇을 근거로 도는지 알 수 없다). 이미 통합된 commit 을 명시하는 복구 문으로 닫는다:\n"
    "  python3 .project_manager/tools/ticket_finish.py --cluster {cluster} "
    "--reconcile-integrated \\\n"
    "    --integrated-rev T-NNNN=<commit> [--legacy-base-rev T-NNNN=<commit>] \\\n"
    "    --user-ack {cluster} --repo <repo> --slot <N>"
)




class ClusterCloser:
    """묶음 종결(close) — 고정 여섯 단계·상태 관측 기반 멱등 + 첫 부작용 앞 선검사.

    git·board·위임 호출은 전부 `TicketFinisher` 가 이미 들고 있는 seam 을 그대로 쓴다(사본 0).
    이 클래스가 새로 받는 seam 은 라운드 처분 CLI 하나뿐이다. 슬롯은 반납하지 않는다 — 대여
    기록은 작업 트리를 찾을 곳을 정하는 데만 읽는다(`app.cluster_code_tree.resolve_code_tree`).
    """

    # `(단계 키, 사람이 읽는 이름, 읽기 전용 선검사 메서드 이름 또는 None)`.
    # 세 번째 값이 **선언**이다 — `None` 은 "실행해야 아는 것"(머지 충돌·커밋 rc).
    # `run()`·`preflight()` 가 이 3-튜플을 언팩하므로 선언을 빠뜨린 단계를 추가하면 그 자리에서
    # `ValueError` 다(별도 가드 장치 없음).
    STEPS: tuple[tuple[str, str, str | None], ...] = (
        ("resolve", "기계 확인 생성·리뷰 게이트 처분", "_pre_resolve"),
        ("record", "티켓별 완료 기록", "_pre_record"),
        ("commit", "슬롯 커밋", None),
        ("rebase", "통합 브랜치로 재배치", "_pre_rebase"),
        ("merge", "통합 브랜치 머지", "_pre_merge"),
        ("board", "board·포인터 커밋", "_pre_board"),
    )

    # 멤버의 코드 산출을 전제하는 단계 — 완료 기록할 티켓·커밋할 경로·재배치할 브랜치·머지할
    # 브랜치가 전부 살아 있는 멤버에서 나온다. 멤버가 전부 종결(처분)이면 이 단계들은 대상이
    # 없다(머지할 코드가 없다).
    CODE_STEP_KEYS: frozenset[str] = frozenset(
        {"record", "commit", "rebase", "merge"}
    )

    def __init__(
        self,
        home,
        cluster: str,
        *,
        finisher: finish_record.TicketFinisher,
        board_py: Path | None = None,
        slot: str | None = None,
        task: str | None = None,
        board_identity_args: Sequence[str] = (),
        dry_run: bool = False,
        skip_pytest: bool = False,
        reconcile_integrated: bool = False,
        integrated_revisions: Sequence[str] = (),
        legacy_base_revisions: Sequence[str] = (),
        user_ack: str | None = None,
        explicit_slot: bool = False,
        run_delegate_fn: Callable[[list[str]], tuple[int, str]] | None = None,
    ) -> None:
        self._home = home
        self._cluster = cluster
        self._finisher = finisher
        # 하위 프로세스 실행 위치 — None 이면 생성 시 명시 Home 에서 해소한다(데이터 조회 입력 아님).
        self._board_py = (board_py if board_py is not None
                          else home.root / ".project_manager" / "tools" / "board.py")
        self._slot = slot
        self._task = task
        self._board_identity_args = list(board_identity_args)
        self._dry_run = dry_run
        self._skip_pytest = skip_pytest
        self._reconcile_integrated = reconcile_integrated
        self._integrated_revision_args = tuple(integrated_revisions)
        self._legacy_base_revision_args = tuple(legacy_base_revisions)
        self._user_ack = user_ack
        self._explicit_slot = explicit_slot
        # 리뷰 처분 실행 함수. 묶음 표면 rounds resolve --cluster 를 한 번 부른다.
        self._run_delegate_fn = run_delegate_fn or self._default_run_delegate
        # 장부가 선언한 멤버 전부와, 그중 코드 단계의 대상이 되는 **살아 있는** 멤버.
        # 기록 축(무엇을 board 에 남기나·커밋 문안의 단위)은 선언을, 실행 축(완료 기록·커밋·
        # 재배치·머지)은 살아 있는 멤버를 본다.
        self._declared_tickets: tuple[str, ...] = ()
        self._tickets: tuple[str, ...] = ()

    # ── 기본 seam 구현 ────────────────────────────────────────────────

    def _default_run_delegate(self, args: list[str]) -> tuple[int, str]:
        """pm_delegate.py 를 subprocess 로 호출해 (returncode, stdout+stderr) 반환."""
        delegate_py = Path(self._board_py).with_name("pm_delegate.py")
        result = subprocess.run(
            [str(self._finisher._venv_python), str(delegate_py)] + args,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(self._home.root),
        )
        return result.returncode, result.stdout + result.stderr

    # ── 공용 조회 ─────────────────────────────────────────────────────

    def _git(self, cwd: Path, args: list[str]) -> tuple[int, str]:
        """mutation git — 진단(stderr)을 합쳐 돌려주는 seam 쪽이다."""
        if cwd == self._home.root:
            return self._finisher._run_git_fn(args)
        return self._finisher._run_git_at_fn(cwd, args)

    def _git_stdout(self, cwd: Path, args: list[str]) -> tuple[int, str]:
        """기계 판독 git — stdout 만 보는 seam(경고 한 줄이 값으로 둔갑하지 않게)."""
        if cwd == self._home.root:
            return self._finisher._run_git_stdout_fn(args)
        return self._finisher._run_git_stdout_at_fn(cwd, args)

    def _ledger(self) -> dict | None:
        """묶음 장부 frontmatter (없으면 None — 크기 1 해석은 멤버 조회가 소유한다)."""
        return cluster_store.load_cluster(self._home, self._cluster)

    def _declared_branch(self, key: str) -> str | None:
        """장부가 선언한 브랜치 이름 (`branch`=묶음 브랜치 · `base_branch`=통합 브랜치)."""
        value = (self._ledger() or {}).get(key)
        return value.strip() or None if isinstance(value, str) else None

    def _ticket_status(self, tid: str) -> str | None:
        """그 티켓이 지금 놓인 board 상태 (board 에 없으면 None — 관측 성공).

        조회 실패는 판정 불능이라 정지다 — 완료 기록이 이미 끝났는지 모른 채
        진행하면 앞 단계 부작용을 반복한다."""
        try:
            found = ticket_store.find_ticket_exact(self._home, tid)
        except Exception as exc:  # noqa: BLE001 — 조회 실패는 판정 불능(정지).
            raise finish_rules._CloseObservationFailure(
                f"{tid} 상태를 관측하지 못했다 — {type(exc).__name__}: {exc}") from exc
        return found[0] if found is not None else None

    def _pending_gates(self) -> list[str]:
        """처분·확인이 필요한 멤버 — 리뷰 라운드 파일의 잔여가 아직 완료 판정을 막는 것."""
        return [tid for tid in self._tickets if board_ticket._review_completion_problem(self._home, tid) is not None]

    def _pending_paths(self, cwd: Path, pathspec: Sequence[str]) -> tuple[str, ...]:
        """그 pathspec 안에 아직 커밋되지 않은 경로 (관측 실패는 정지).

        커밋 단계가 자기 부작용의 유무를 묻는 관측 지점이다 — 남은 변경이 없으면 이미 커밋된
        것이고, 재실행은 빈 커밋을 만들지 않는다. 조회가 실패하면 **커밋했는지 모르는** 것이라
        빈 목록(=이미 커밋됨)으로 접지 않는다 — 그렇게 접으면 아무것도 커밋하지 않은 실행이
        커밋한 실행과 같은 모양으로 끝난다.

        **서브모듈 내부 dirty 는 변경으로 세지 않는다**(`--ignore-submodules=dirty`) — 상위
        repo 가 커밋할 수 있는 것은 포인터뿐이라, 내부 dirty 를 변경으로 읽으면 커밋할 것이 없는
        자리에서 커밋을 시도하고 git 이 거부한다(그 dirty 는 그 repo 가 자기 커밋으로 닫는다).
        """
        if not pathspec:
            return ()
        try:
            rc, out = self._git_stdout(
                cwd,
                ["status", "--porcelain", "-z", "--untracked-files=all",
                 "--ignore-submodules=dirty", "--", *pathspec],
            )
        except Exception as exc:  # noqa: BLE001 — 관측 실패는 정지(무대상 위장 금지).
            raise finish_rules._CloseObservationFailure(
                f"남은 변경을 관측하지 못했다 ({cwd}) — "
                f"{type(exc).__name__}: {exc}") from exc
        if rc != 0:
            raise finish_rules._CloseObservationFailure(
                f"남은 변경을 관측하지 못했다 ({cwd} · git status rc={rc}): {out.rstrip()}")
        entries = git.git_parse_porcelain_z(out)
        return tuple(path for _code, path in entries)

    def _is_ancestor(self, cwd: Path, ancestor: str, descendant: str) -> bool:
        """`ancestor` 가 `descendant` 의 조상인가 — 판정 불능은 정지.

        `git merge-base --is-ancestor` 는 조상이면 rc 0, 아니면 rc 1 이다. 그 밖의 rc(잘못된
        ref·비-repo)는 답이 아니라 판정 실패이므로 False(=아직 안 들어갔다)로 접지 않는다 —
        그렇게 접으면 이미 끝난 재배치·머지를 다시 시도한다."""
        try:
            rc, out = self._git_stdout(
                cwd, ["merge-base", "--is-ancestor", ancestor, descendant])
        except Exception as exc:  # noqa: BLE001 — 판정 실패는 정지.
            raise finish_rules._CloseObservationFailure(
                f"{ancestor} 가 {descendant} 의 조상인지 판정하지 못했다 ({cwd}) — "
                f"{type(exc).__name__}: {exc}") from exc
        if rc not in (0, 1):
            raise finish_rules._CloseObservationFailure(
                f"{ancestor} 가 {descendant} 의 조상인지 판정하지 못했다 "
                f"({cwd} · rc={rc}): {out.rstrip()}")
        return rc == 0

    def _dirty_paths(self, cwd: Path) -> tuple[str, ...]:
        """그 트리에 남은 변경 전체 (재배치·머지의 선행 조건 관측 · 실패는 정지).

        조회 실패를 빈 목록으로 접으면 **clean 으로 위장**돼 dirty 트리에서 재배치·머지가 돈다."""
        try:
            entries = self._finisher._status_entries_at_fn(cwd)
        except Exception as exc:  # noqa: BLE001 — 관측 실패는 정지(clean 위장 금지).
            raise finish_rules._CloseObservationFailure(
                f"작업 트리 상태를 관측하지 못했다 ({cwd}) — "
                f"{type(exc).__name__}: {exc}") from exc
        return tuple(path for _code, path in entries)


    def _integration_worktree(self, branch: str) -> Path | None:
        """그 브랜치를 체크아웃한 작업 트리 (없으면 None · 둘 이상이면 정지).

        머지는 통합 브랜치를 **가진 트리에서** 해야 한다 — 같은 브랜치를 두 트리가 동시에
        체크아웃할 수 없으므로(git 제약) 종결 슬롯에서 체크아웃해 오는 길은 없다. 그 제약은
        우회(`--ignore-other-worktrees`)가 있어 둘 이상도 관측된다 — 그때는 고르지 않는다.
        """
        target = f"refs/heads/{branch}"
        holders = [tree for tree, ref in finish_git._worktree_list(self._code_tree(), run_git_stdout_at=self._git_stdout)
                   if ref == target]
        if len(holders) > 1:
            raise finish_rules._CloseObservationFailure(finish_rules._CLOSE_TREE_AMBIGUOUS.format(
                branch=branch, count=len(holders), trees=", ".join(map(str, holders))))
        return holders[0] if holders else None


    def _code_tree(self) -> Path:
        return self._finisher._code_tree()

    def _wrong_branch_reason(self, tree: Path, source: str) -> str | None:
        """이 코드 트리가 묶음 브랜치를 들고 있는가 — 아니면 사유(다른 브랜치를 옮기지 않는다).

        재배치는 HEAD 를, 머지는 장부가 선언한 브랜치를 옮긴다. 둘이 같은 브랜치가 아니면 이
        슬롯의 작업이 아닌 것을 통합 브랜치에 얹게 되므로 첫 mutation 앞에서 거부한다.
        """
        rc, out = self._git_stdout(tree, ["symbolic-ref", "--quiet", "--short", "HEAD"])
        current = (out or "").strip() if rc == 0 else ""
        if current == source:
            return None
        return (f"이 코드 트리가 든 브랜치({current or 'detached HEAD'})가 장부의 묶음 브랜치 "
                f"{source} 와 다르다 ({tree}) — 종결은 묶음 브랜치를 체크아웃한 슬롯에서 "
                "돈다(다른 브랜치를 옮기지 않는다).")

    def _unit(self) -> tuple[str, str]:
        """커밋 문안이 쓰는 `(단위 표기, 제목)` — 크기 1 은 티켓 표기, 묶음은 묶음 표기.

        단위는 **선언 멤버**로 정한다 — 멤버가 처분됐다고 해서 그 묶음이 다른 단위가 되지 않는다.
        """
        if len(self._declared_tickets) == 1:
            tid = self._declared_tickets[0]
            return tid, (self._finisher._ticket_title_fn(tid) or tid)
        name = cluster_rules.cluster_name_of(self._cluster)
        return self._cluster, name

    def _plans(self, tid: str) -> tuple[finish_rules.RepoStagePlan | None, finish_rules.RepoStagePlan | None]:
        """그 티켓의 `(PM 홈 계획, 코드 트리 계획)` — 단일 트리 형상은 같은 계획을 둘 다 준다."""
        plans = self._finisher._stage_plans(tid)
        home = next((plan for plan in plans if plan.cwd == self._home.root), None)
        code = next((plan for plan in plans if plan.cwd != self._home.root), None)
        if code is None:
            code = home       # 임베디드 형상 — 코드와 홈이 같은 트리다.
        return home, code

    # ── 단계 ──────────────────────────────────────────────────────────

    def _pre_resolve(self) -> list[str]:
        """멤버마다 돌려받은 리뷰와 resolve 가 소비할 확인 입력을 read-only 검증한다.

        리뷰 라운드가 없거나 돌려받지 않았거나 전부 폐기된 멤버는 여기서 막힌다 — 종결은 리뷰를
        돌려받은 멤버만 진행한다. 예외는 tier: pm-direct 이고 리뷰 라운드가 0개인 멤버다(리뷰
        라운드가 있으면 pm-direct 도 같은 판정을 받는다). 처분 여부는 여기 없다(처분은 resolve
        자신이 하는 일이라 비단조다). 판정은 board_ticket 소유(`_review_resolution_input_problem`)다.
        """
        problems: list[str] = []
        for tid in self._tickets:
            problem = board_ticket._review_resolution_input_problem(self._home, tid)
            if problem is None:
                print(f"  ✓ {tid} 리뷰 판정 입력 complete")
            else:
                problems.append(f"리뷰 확인 입력 미충족 — {tid}: {problem}")
        return problems

    def _step_resolve(self) -> str | None:
        """리뷰 게이트 처분 — 묶음 표면 `rounds resolve --cluster` 를 한 번 부른다."""
        if not self._pending_gates():
            print("  처분할 리뷰 잔여 없음 — 건너뜀")
            return None
        argv = [
            "rounds", "resolve", "--cluster", self._cluster, "--pm-verified",
            *self._board_identity_args,
        ]
        if self._dry_run:
            print(f"  [dry-run] pm_delegate.py {' '.join(argv)}")
            return None
        rc, out = self._run_delegate_fn(argv)
        for line in out.rstrip().splitlines():
            print(f"  {line}")
        if rc != 0:
            return (f"리뷰 게이트 처분 실패 (rc={rc}) — `{' '.join(argv)}`. 위 사유를 "
                    "해소한 뒤 다시 실행하라(앞 단계는 반복하지 않는다).")
        return None

    def _pre_record(self) -> list[str]:
        """완료 기록의 읽기 전용 차단 사유 전건 — 이미 done 인 티켓은 세지 않는다.

        여기 **없는** 것과 그 이유: [1/5] 회귀 red 와 자기 축 회귀는 판정 입력이 pytest 실행
        결과라 읽기로 답이 나오지 않는다. [2/5] log entry 부재와 리뷰 잔여는 각각 그
        append 와 앞 단계(resolve)가 **해소하는** 조건이라 선검사에 넣으면 정상 실행이 첫 줄에서
        막힌다(비단조).

        판정은 전부 남의 함수다 — 완료 기록이 쓰는 DI 셋(`_private_ref_block_fn`·
        `_dod_block_fn`·`_residual_block_fn`)과 `board complete` 가 쓰는
        판정 셋을 그대로 부른다(사본 0).
        """
        reasons: list[str] = []
        for tid in self._tickets:
            status = self._ticket_status(tid)
            if status == "done":
                continue
            if status != "claimed":
                # 여기서 멈추지 않는다 — `board complete` 는 이 한 줄에서 return 하지만, 그
                # 자리가 곧 "한 번에 하나씩" 의 원인이다. 나머지 판정도 전부 읽기로 답이 난다.
                reasons.append(_CLOSE_TICKET_NOT_CLAIMED.format(
                    ticket=tid, status=status or "board 에 없음"))
            for block_fn in (self._finisher._private_ref_block_fn,
                             self._finisher._dod_block_fn,
                             self._finisher._residual_block_fn):
                block = block_fn(tid)
                if block:
                    reasons.append(block)
            reasons.extend(self._board_complete_problems(tid))
        return reasons

    def _board_complete_problems(self, tid: str) -> list[str]:
        """`board complete` 가 첫 거부에서 멈추는 판정 셋을 읽기로 **전건** 모은다(사본 0).

        `cmd_complete` 는 결속 → 소유 → 게이트 순서로 첫 거부에서 return 하므로, 그 셋을 여기서
        각각 불러 누적한다. 소유 판정에는 자기 CLI가 검증한 task와 명시 user=None을 넘긴다.
        """
        found = ticket_store.find_ticket_exact(self._home, tid)
        if found is None:
            raise finish_rules._CloseObservationFailure(
                f"{tid} 명세 파일을 board 에서 찾지 못했다 — 차단 사유를 관측할 수 없다.")
        _status, path = found
        frontmatter, _body = ticket_store.load_ticket(path)
        spec_text = fs.read_text_shared(path, encoding="utf-8")
        problems: list[str] = []
        binding = cluster_binding.cluster_complete_binding_problem(
            self._home, tid, frontmatter, self._cluster)
        if binding is not None:
            problems.append(f"{tid}: {binding}")
        verdict = ticket_access._ticket_ownership(
            self._home, frontmatter, task=self._task, user=None)
        if not verdict.ok:
            problems.append(ticket_rules._ownership_rejection("complete", tid, verdict))
        problems.extend(
            f"{tid}: {problem}"
            for problem in board_ticket._round_completion_problems(self._home, tid, spec_text))
        return problems

    def _has_pipeline_work(self) -> bool:
        """이 파이프라인이 아직 낼 부작용이 남아 있는가 — 커밋 대상이 하나라도 남았으면 참.

        "이미 끝난 단계는 차단 사유가 아니다"의 억제 조건이다. 커밋 전에는 묶음 브랜치가
        통합 브랜치의 조상이라 **이미 머지된 것처럼 보이는** 형상이 있는데, 그 관측을 그대로
        믿으면 선검사가 통과하고 커밋 뒤 머지 단계가 통합 트리 부재로 막힌다.
        """
        for tid in self._tickets:
            _home, code = self._plans(tid)
            pathspec = list(code.scope.pathspec) if code is not None else []
            if pathspec and self._pending_paths(code.cwd, pathspec):
                return True
        return False

    def _step_record(self) -> str | None:
        """티켓별 완료 기록 — 현행 완료 기록 그대로다(같은 코드 경로·같은 출력 줄)."""
        for tid in self._tickets:
            if self._ticket_status(tid) == "done":
                print(f"  {tid} 이미 done — 완료 기록 건너뜀")
                continue
            rc = self._finisher.run(
                ticket_id=tid,
                section=None,
                dry_run=self._dry_run,
                skip_pytest=self._skip_pytest,
                task=self._task,
                board_identity_args=self._board_identity_args,
                close=True,
                cluster_close=self._cluster,
            )
            if rc != 0:
                return (f"{tid} 완료 기록이 중단됐다 (rc={rc}) — 위 사유를 해소한 뒤 다시 "
                        "실행하라(이미 끝난 티켓은 다시 기록하지 않는다).")
        return None

    def _step_commit(self) -> str | None:
        """슬롯 커밋 — 티켓마다 그 티켓이 stage 한 경로를 제목 문안으로 커밋한다."""
        for tid in self._tickets:
            _home, code = self._plans(tid)
            pathspec = list(code.scope.pathspec) if code is not None else []
            if not pathspec:
                print(f"  {tid} 커밋할 선언 경로 없음")
                continue
            pending = self._pending_paths(code.cwd, pathspec)
            if not pending:
                print(f"  {tid} 커밋할 변경 없음 — 건너뜀")
                continue
            title = self._finisher._ticket_title_fn(tid) or tid
            if self._dry_run:
                print(f"  [dry-run] cwd={code.cwd} git commit -m \"{title}\" "
                      f"-- {' '.join(pathspec)}")
                continue
            rc, out = self._git(code.cwd, ["commit", "-m", title, "--", *pathspec])
            if rc != 0:
                return (f"{tid} 커밋 실패 (rc={rc}): {out.rstrip()}")
            print(f"  ✓ {tid} 커밋 — {len(pending)}개 경로 ({title})")
        return None

    def _pre_rebase(self) -> list[str]:
        """재배치의 읽기 전용 선행 조건 — 선언·존재·이 트리가 든 브랜치(사유 전건).

        작업 트리 dirty 와 재배치 충돌은 여기 없다 — 앞 단계(슬롯 커밋)가 해소하거나 실행해야
        아는 것이라 첫 부작용 앞에서 물으면 정상 실행이 첫 줄에서 막힌다.

        빈 기준선(`finish_git._unborn_baseline` — 커밋 0 · HEAD 가 그 브랜치)은 "브랜치가 없다" 가
        아니라 첫 커밋이 아직 없는 것이라 차단 사유가 아니다. detached·다른 브랜치의
        미해소는 종전대로 사유다.
        """
        integration = self._declared_branch("base_branch")
        if not integration:
            return [_CLOSE_INTEGRATION_UNDECLARED.format(cluster=self._cluster)]
        reasons: list[str] = []
        tree = self._code_tree()
        rc, _out = self._git_stdout(
            tree, ["rev-parse", "--verify", "--quiet", f"{integration}^{{commit}}"])
        if rc != 0 and not finish_git._unborn_baseline(self._git_stdout, tree, integration):
            reasons.append(_CLOSE_INTEGRATION_BRANCH_MISSING.format(
                integration=integration, tree=tree))
        source = self._declared_branch("branch")
        if source:
            wrong_branch = self._wrong_branch_reason(tree, source)
            if wrong_branch is not None:
                reasons.append(wrong_branch)
        return reasons

    def _step_rebase(self) -> str | None:
        """통합 브랜치로 재배치 — 충돌은 원상 복구(abort) 후 정지한다.

        기준 브랜치 선언이 없으면 무대상이 아니라 정지다 — 재배치를 건너뛴 종결은 이 작업을
        통합 브랜치 밖에 남긴 채 완료를 선언한다."""
        blocks = self._pre_rebase()
        if blocks:
            return "\n".join(blocks)
        integration = self._declared_branch("base_branch")
        tree = self._code_tree()
        if finish_git._unborn_baseline(self._git_stdout, tree, integration):
            # 선언 경로가 비어 앞 단계가 첫 커밋을 만들지 못한 형상 — 기준선도 HEAD 도 없어
            # 조상 판정 자체가 성립하지 않는다(그 상태로 물으면 관측 실패로 죽는다).
            print(f"  {integration} 에 커밋이 없다 — 재배치할 것 없음")
            return None
        if self._is_ancestor(tree, integration, "HEAD"):
            print(f"  이미 {integration} 위에 있다 — 건너뜀")
            return None
        if self._is_ancestor(tree, "HEAD", integration):
            # 이 작업이 이미 통합 브랜치 안에 있다(머지가 끝난 뒤의 재실행) — 재배치할 것이
            # 남아 있지 않다. 이 관측이 없으면 종결이 끝난 묶음을 다시 돌릴 때마다 브랜치를
            # 통합 tip 으로 끌어올리는 불필요한 mutation 이 반복된다.
            print(f"  이 작업은 이미 {integration} 안에 있다 — 건너뜀")
            return None
        dirty = self._dirty_paths(tree)
        if dirty:
            return (f"작업 트리에 남은 변경이 있어 재배치하지 않는다 ({len(dirty)}건 · "
                    f"{tree}) — 앞 단계가 커밋하지 못한 경로를 확인하라.")
        if self._dry_run:
            print(f"  [dry-run] cwd={tree} git rebase {integration}")
            return None
        rc, out = self._git(tree, ["rebase", integration])
        if rc != 0:
            abort_rc, abort_out = self._git(tree, ["rebase", "--abort"])
            recovered = ("원상 복구했다" if abort_rc == 0
                         else f"원상 복구 실패({abort_out.rstrip()})")
            return (f"재배치 충돌 — {recovered}. 충돌을 해소한 뒤 다시 실행하라: "
                    f"{out.rstrip()}")
        print(f"  ✓ {integration} 위로 재배치")
        return None

    def _pre_merge(self) -> list[str]:
        """머지의 읽기 전용 선행 조건 — 선언·존재·현재 브랜치·통합 트리(사유 전건).

        통합 트리는 이 파이프라인이 커밋하지 않으므로 그 트리의 dirty 도 단조다(앞 단계가
        해소하지 않는다) — 그래서 여기 있다. 머지 충돌만 실행해야 아는 것이다.

        **이미 머지됐다는 관측은 아직 낼 부작용이 없을 때만 믿는다**(`_has_pipeline_work`).
        커밋 전에는 묶음 브랜치가 통합 브랜치의 조상이라 끝난 것처럼 보이는 형상이 있다.
        """
        integration = self._declared_branch("base_branch")
        if not integration:
            return [_CLOSE_INTEGRATION_UNDECLARED.format(cluster=self._cluster)]
        source = self._declared_branch("branch")
        if not source:
            return []                       # 묶음 브랜치 미선언 = 머지 무대상
        tree = self._code_tree()
        rc, _out = self._git_stdout(
            tree, ["rev-parse", "--verify", "--quiet", f"{source}^{{commit}}"])
        if rc != 0:
            return [_CLOSE_SOURCE_BRANCH_MISSING.format(source=source, tree=tree)]
        if not self._has_pipeline_work() and self._is_ancestor(tree, source, integration):
            return []                       # 이미 머지됐다 — 끝난 단계는 차단 사유가 아니다
        reasons: list[str] = []
        wrong_branch = self._wrong_branch_reason(tree, source)
        if wrong_branch is not None:
            reasons.append(wrong_branch)
        target_tree = self._integration_worktree(integration)
        if target_tree is None:
            reasons.append(
                _CLOSE_INTEGRATION_TREE_MISSING.format(integration=integration))
            return reasons
        dirty = self._dirty_paths(target_tree)
        if dirty:
            reasons.append(_CLOSE_INTEGRATION_TREE_DIRTY.format(
                integration=integration, count=len(dirty), tree=target_tree))
        return reasons

    def _step_merge(self) -> str | None:
        """통합 브랜치 머지 — 통합 브랜치를 가진 트리에서 `--no-ff` 로 받는다.

        묶음 브랜치(`branch`)는 사람이 선언한 묶음만 갖는다(크기 1 자동 장부는 없다) — 그
        선언이 없으면 받을 브랜치가 없어 무대상이다. 기준 브랜치 미선언은 그 축이 아니라
        판정 기준의 부재라 정지다. 합쳐진 묶음 브랜치는 지우지 않는다 — 작업 트리가 그 브랜치를
        쥔 채 여기 닿으므로 삭제는 늘 거부된다. 브랜치 정리는 슬롯 제거
        (`worktree_pool.remove_slot`) 몫이다."""
        blocks = self._pre_merge()
        if blocks:
            return "\n".join(blocks)
        integration = self._declared_branch("base_branch")
        source = self._declared_branch("branch")
        if not source:
            print("  묶음 브랜치 미선언 — 무대상")
            return None
        tree = self._code_tree()
        if self._is_ancestor(tree, source, integration):
            print(f"  {source} 는 이미 {integration} 에 있다 — 건너뜀")
            return None
        target_tree = self._integration_worktree(integration)
        if target_tree is None:
            # 선검사가 같은 관측으로 이미 막았어야 하는 자리 — 그 사이에 트리가 사라졌다.
            return _CLOSE_INTEGRATION_TREE_MISSING.format(integration=integration)
        unit, title = self._unit()
        message = _CLOSE_MERGE_MESSAGE.format(unit=unit, title=title)
        if self._dry_run:
            print(f"  [dry-run] cwd={target_tree} git merge --no-ff -m \"{message}\" {source}")
            return None
        rc, out = self._git(target_tree, ["merge", "--no-ff", "-m", message, source])
        if rc != 0:
            self._git(target_tree, ["merge", "--abort"])
            return (f"머지 충돌 — 원상 복구했다(merge --abort). 충돌을 해소한 뒤 다시 "
                    f"실행하라: {out.rstrip()}")
        print(f"  ✓ {source} → {integration} ({message})")
        return None

    def _pre_board(self) -> list[str]:
        """기록 대상 경로가 해소되는가 — 해소 불가는 차단 사유가 아니라 관측 실패(정지)다.

        단계와 **같은 함수**(`_board_git_paths`)를 부른다. board-git 로컬 커밋 미생성과 PM 홈
        커밋 실패는 실행해야 아는 것이라 여기 없다.
        """
        self._board_git_paths()
        return []

    def _step_board(self) -> str | None:
        """board-git 기록 + PM 홈 포인터·산출물 커밋 — 두 git 을 한 단계가 닫는다.

        종결 표시(`status=closed`)는 커밋 **대상**이다 — 장부 자신이 board-git 커밋에 실리므로,
        표시를 커밋 뒤에 쓰면 board-git 이 그 값을 영영 못 담고 다음 실행마다 그 한 줄을
        커밋하려 든다(재실행 무부작용이 깨진다). 그래서 순서는 표시 → 두 커밋이고, **어느
        커밋이든 실패하면 표시를 되돌린다** — 디스크에 남는 `closed` 는 모든 단계가 전부
        관측된 뒤에만 존재한다.
        """
        paths = self._board_git_paths()
        previous = self._ledger() or {}
        prior = (previous.get(_CLOSE_STEP_KEY), previous.get("status"))
        self._record_progress(self.STEPS[-1][0])
        try:
            block = self._sync_board_git(paths)
            if block is None:
                block = self._commit_pm_home()
        except finish_rules._CloseObservationFailure:
            # 관측 실패도 종결 표시의 근거가 아니다 — 되돌리고 그대로 정지한다.
            self._restore_progress(*prior)
            raise
        if block is not None:
            self._restore_progress(*prior)
            return block
        return None

    def _board_git_paths(self) -> list[Path]:
        """board-git 에 기록할 경로 — 묶음 장부 + **선언** 멤버 티켓 명세 (해소 실패는 정지).

        기록 축은 선언이다 — 처분된 멤버도 이 묶음의 멤버라 그 명세가 기록 대상에서 빠지지
        않는다. 여기서 하나라도 못 찾으면 이번 종결이 무엇을 기록해야 하는지 모르는 것이다 —
        빈 목록으로 접으면 아무것도 기록하지 않은 실행이 성공으로 끝난다."""
        paths: list[Path] = []
        if self._ledger() is not None:
            paths.append(cluster_store.cluster_ledger_path(self._home, self._cluster))
        for tid in self._declared_tickets:
            try:
                found = ticket_store.find_ticket_exact(self._home, tid)
            except Exception as exc:  # noqa: BLE001 — 조회 실패는 정지.
                raise finish_rules._CloseObservationFailure(
                    f"{tid} 명세 경로를 관측하지 못했다 — "
                    f"{type(exc).__name__}: {exc}") from exc
            if found is None:
                raise finish_rules._CloseObservationFailure(
                    f"{tid} 명세 파일을 board 에서 찾지 못했다 — 기록 대상이 확정되지 않는다.")
            paths.append(found[1])
        if not paths:
            raise finish_rules._CloseObservationFailure(
                f"{self._cluster} 기록 대상 경로가 하나도 없다 — 멤버 해소를 확인하라.")
        return paths

    def _sync_board_git(self, paths: Sequence[Path]) -> str | None:
        """이번 종결이 만진 board 경로를 board-git 에 기록한다(장부·티켓 명세).

        seam 부재는 정지다(기록 채널이 없다는 사실을 성공으로 만들지 않는다). `False` 는
        **로컬 커밋이 안 생겼다**는 board 의 확정 보고라 그대로 차단이다 — 그 상태로 포인터를
        커밋하면 board 에 없는 내용을 가리키는 포인터가 남는다(원격 push 실패는 `True` 로
        보고되므로 offline 은 여기서 막히지 않는다).
        """
        unit, title = self._unit()
        message = _CLOSE_BOARD_MESSAGE.format(unit=unit, title=title)
        if self._dry_run:
            print(f"  [dry-run] board-git 기록: {message}")
            return None
        if not board_sync._board_git_sync_best_effort(self._home, message, list(paths)):
            return ("board-git 로컬 커밋이 생기지 않았다 — 위 board 진단의 미커밋 경로를 "
                    "해소한 뒤 다시 실행하라(포인터는 아직 커밋하지 않았다).")
        print(f"  ✓ board-git 기록 ({message})")
        return None

    def _board_pointer_path(self) -> str | None:
        """PM 홈이 board 를 서브모듈로 가리키는가 — 아니면 None(포인터 커밋 무대상).

        서브모듈 포인터는 상위 `git status` 가 보여주지 않는 형상(`ignore = all`)이라 사람이
        놓치는 자리다 — 그래서 종결이 명시적으로 add 한다. 서브모듈이 아니면 board 파일은
        홈 계획이 이미 덮는다.

        빈 출력은 **관측 성공**(그 경로가 인덱스에 없다 = 서브모듈 아님)이고, 조회 실패는
        판정 불능이라 정지다 — 포인터가 있는데 못 읽은 것을 '무대상' 으로 접으면 실측된 잔여
        (포인터 커밋 누락)가 그대로 재발한다.
        """
        try:
            rc, out = self._git_stdout(
                self._home.root, ["ls-files", "--stage", "--", _CLOSE_BOARD_POINTER_PATH])
        except Exception as exc:  # noqa: BLE001 — 판정 실패는 정지.
            raise finish_rules._CloseObservationFailure(
                f"board 포인터를 관측하지 못했다 ({self._home.root}) — "
                f"{type(exc).__name__}: {exc}") from exc
        if rc != 0:
            raise finish_rules._CloseObservationFailure(
                f"board 포인터를 관측하지 못했다 ({self._home.root} · rc={rc}): {out.rstrip()}")
        if not out.strip():
            return None
        return (_CLOSE_BOARD_POINTER_PATH
                if out.split(maxsplit=1)[0] == _CLOSE_SUBMODULE_MODE else None)

    def _commit_pm_home(self) -> str | None:
        """PM 홈 커밋 — 이 실행이 쓴 홈 산출물 + board 서브모듈 포인터."""
        pathspec: list[str] = []
        for tid in self._tickets:
            home, _code = self._plans(tid)
            if home is None:
                continue
            for path in home.scope.pathspec:
                if path not in pathspec:
                    pathspec.append(path)
        pointer = self._board_pointer_path()
        if pointer is not None and pointer not in pathspec:
            pathspec.append(pointer)
        if not pathspec:
            print("  PM 홈에 커밋할 경로 없음")
            return None
        unit, title = self._unit()
        message = _CLOSE_BOARD_MESSAGE.format(unit=unit, title=title)
        if self._dry_run:
            print(f"  [dry-run] cwd={self._home.root} git add -- {pointer or ''}")
            print(f"  [dry-run] cwd={self._home.root} git commit -m \"{message}\" "
                  f"-- {' '.join(pathspec)}")
            return None
        if pointer is not None:
            rc, out = self._git(self._home.root, ["add", "--", pointer])
            if rc != 0:
                return f"board 포인터 stage 실패 (rc={rc}): {out.rstrip()}"
        if not self._pending_paths(self._home.root, pathspec):
            print("  PM 홈에 커밋할 변경 없음 — 건너뜀")
            return None
        rc, out = self._git(self._home.root, ["commit", "-m", message, "--", *pathspec])
        if rc != 0:
            return f"PM 홈 커밋 실패 (rc={rc}): {out.rstrip()}"
        print(f"  ✓ PM 홈 커밋 ({message})")
        return None

    # ── 진행 기록 ─────────────────────────────────────────────────────

    def _record_progress(self, step_key: str) -> None:
        """장부에 단계 진행을 남긴다 — 관측을 대신하지 않는 보조 기록(기록 실패는 전파)."""
        status = (_CLUSTER_STATUS_CLOSED if step_key == self.STEPS[-1][0]
                  else _CLUSTER_STATUS_CLOSING)
        self._write_progress(step_key, status)

    def _restore_progress(self, step_key: str | None, status: str | None) -> None:
        """마지막 단계가 실패했을 때 진행 기록을 그 실행 **직전 값**으로 되돌린다.

        종결 표시는 커밋에 실어야 해서 커밋보다 먼저 쓰지만, 커밋이 실패하면 그 표시는 근거가
        없다 — 디스크에 남는 `closed` 는 모든 단계가 관측된 경우로 제한한다."""
        self._write_progress(step_key, status)

    def _write_progress(self, step_key: str | None, status: str | None) -> None:
        """장부의 진행 기록 두 값을 쓴다 (기록 실패는 전파한다)."""
        if self._dry_run:
            return
        with fs.board_lock(self._home):
            ledger = cluster_store.load_cluster(self._home, self._cluster)
            if ledger is None:
                return
            if step_key is None:
                ledger.pop(_CLOSE_STEP_KEY, None)
            else:
                ledger[_CLOSE_STEP_KEY] = step_key
            if status is None or status in cluster_rules.CLUSTER_STATUSES:
                # cluster_rules의 상태 열거에 포함될 때만 쓴다. None은 되돌리기
                # 전 값이 비어 있던 형상이라 그대로 되돌린다.
                ledger["status"] = status
            cluster_store.dump_cluster(self._home, ledger)

    # ── 실행 ──────────────────────────────────────────────────────────

    def _resolve_members(self) -> str | None:
        """멤버 티켓 해소 — 묶음을 못 찾으면 사유(첫 부작용 앞에서 거부).

        **선언 멤버 0**(묶음 부재)과 **살아 있는 멤버 0**(선언은 있고 전부 처분됨)은 다른
        상태다 — 전자는 대상 자체가 없어 거부이고, 후자는 코드 단계가 무대상인 정상 종결이다.
        """
        self._declared_tickets = tuple(cluster_store.cluster_members(self._home, self._cluster))
        if not self._declared_tickets:
            return (f"묶음을 찾지 못했다: {self._cluster} — 멤버 조회는 "
                    "`board.py cluster show <ticket-group>` 이다.")
        self._tickets = tuple(
            tid for tid in self._declared_tickets
            if self._ticket_status(tid) != _TICKET_STATUS_DISCARDED
        )
        return None

    def _open_all_done_block(self) -> str | None:
        """정상 종결의 첫 부작용 앞 read-only 상태 게이트 — open + 전원 done 은 복구 전용이다.

        `open` 장부의 멤버가 전부 done 이면 그 완료 기록은 이 파이프라인이 남긴 것이 아니다
        (1단계가 성공하는 순간 장부는 `closing` 이 된다). 그 상태로 정상 종결을 시작하면 무엇이
        이미 통합됐는지 모르는 채 커밋·재배치·머지로 들어간다 — 그래서 여기서 멈추고
        증거를 명시하는 복구 문으로 보낸다. `closing` 재개와 `closed` 멱등 관측은 그대로다.
        """
        ledger = self._ledger()
        if ledger is None:
            return None
        if str(ledger.get("status") or "").strip() != _CLUSTER_STATUS_OPEN:
            return None
        # 살아 있는 멤버가 없으면 "그 완료 기록을 누가 남겼나" 라는 질문 자체가 없다 — 기록할
        # 멤버가 0이라 복구 문이 요구하는 통합 commit 증거도 존재하지 않는다.
        if not self._tickets:
            return None
        if any(self._ticket_status(tid) != "done" for tid in self._tickets):
            return None
        return _CLOSE_OPEN_ALL_DONE.format(
            cluster=self._cluster, members=", ".join(self._tickets))

    # ── all-done recovery ───────────────────────────────────────────

    @staticmethod
    def _revision_map(values: Sequence[str], label: str) -> tuple[dict[str, str], str | None]:
        """``T-NNNN=<rev>`` 반복 인자를 중복 없이 읽는다."""
        mapped: dict[str, str] = {}
        for value in values:
            ticket, separator, revision = str(value).partition("=")
            ticket, revision = ticket.strip(), revision.strip()
            if not separator or not ticket or not revision:
                return {}, f"{label} 형식은 T-NNNN=<git-rev> 이다: {value!r}"
            if ticket in mapped:
                return {}, f"{label}에 {ticket} mapping이 중복됐다"
            mapped[ticket] = revision
        return mapped, None

    def _cluster_id(self, value: str | None) -> str:
        """이름/장부 id 어느 표기든 board 의 장부 id 하나로 정규화한다 (빈 값은 빈 값).

        정상 종결은 `load_cluster`·`cluster_members` 안에서 이 정규화를 이미 받는다. recovery
        만 원시 문자열을 비교하면 같은 표기로 부른 호출이 "장부 id 가 다르다"로 멈춘다.
        """
        text = str(value or "").strip()
        if not text:
            return ""
        return str(cluster_rules.cluster_id_for_name(text)).strip()

    def _commit_oid(self, tree: Path, revision: str, *, label: str) -> tuple[str | None, str | None]:
        """명시 hex revision을 선택 제품 git의 full commit OID로 정규화한다."""
        if re.fullmatch(r"[0-9A-Fa-f]{7,64}", revision) is None:
            return None, f"{label}는 ref/branch가 아닌 hex commit revision이어야 한다: {revision!r}"
        rc, out = self._git_stdout(
            tree, ["rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}"])
        oid = out.strip().splitlines()[0] if rc == 0 and out.strip() else ""
        if rc != 0 or re.fullmatch(r"[0-9a-fA-F]{40,64}", oid) is None:
            return None, f"{label}를 선택 제품 git에서 commit으로 해소하지 못했다: {revision}"
        return oid.lower(), None

    def _reconcile_dirty_paths(self, tree: Path) -> tuple[str, ...]:
        """Recovery용 read-only status — optional index refresh도 금지한다."""
        rc, out = self._git_stdout(
            tree,
            ["--no-optional-locks", "status", "--porcelain", "-z",
             "--untracked-files=all"],
        )
        if rc != 0:
            raise finish_rules._CloseObservationFailure(f"작업 트리 상태를 read-only 관측하지 못했다 ({tree})")
        entries = git.git_parse_porcelain_z(out)
        return tuple(path for _code, path in entries)

    def _reconcile_ticket_frontmatter(self, tid: str) -> tuple[dict | None, str | None]:
        found = ticket_store.find_ticket_exact(self._home, tid)
        if found is None:
            return None, f"{tid} 티켓을 찾지 못했다"
        status, path = found
        fm, _body = ticket_store.load_ticket(path)
        if status != "done" or str(fm.get("status") or "") != "done":
            return None, f"{tid}가 all-done 상태가 아니다 (directory={status}, frontmatter={fm.get('status')})"
        return fm, None

    def _validate_reconcile(self) -> tuple[dict | None, str | None, str | None]:
        """Recovery의 전 입력과 제품 graph를 읽는다 — write는 절대 하지 않는다.

        반환은 ``(canonical closure, ledger status, problem)``이다. 호출자가 이 스냅샷을
        bookend로 두 번 대조한 뒤에만 장부를 쓴다.
        """
        if not self._explicit_slot or not self._slot or self._task is not None:
            return None, None, "recovery는 명시 --repo와 --slot 둘 다 필요하고 --task를 받지 않는다"
        cluster_id = self._cluster_id(self._cluster)
        if self._cluster_id(self._user_ack) != cluster_id:
            return None, None, (f"--user-ack은 대상 cluster와 정확히 같아야 한다: "
                                f"expected {self._cluster!r}, got {self._user_ack!r}")
        # 복구할 제품 트리는 명시한 슬롯이다 — 정상 종결의 작업 트리 해소와 무관하다.
        tree = self._home.root / self._slot
        try:
            if tree.resolve() == Path(self._home.root).resolve():
                return None, None, f"제품 git이 PM 홈으로 강등됐다: {tree}"
        except OSError as exc:
            return None, None, f"선택 제품 경로를 해소하지 못했다: {exc}"
        if not (tree / ".git").is_file():
            return None, None, f"선택 경로가 linked product worktree가 아니다: {tree}"
        dirty = self._reconcile_dirty_paths(tree)
        if dirty:
            return None, None, (f"선택 슬롯이 dirty라 recovery를 거부한다 ({tree} · "
                                f"{len(dirty)}건: {', '.join(dirty[:5])})")

        ledger = self._ledger()
        if ledger is None:
            return None, None, f"묶음 장부를 찾지 못했다: {self._cluster}"
        ledger_id = self._cluster_id(ledger.get("id"))
        if ledger_id != cluster_id:
            return None, None, f"장부 id가 대상과 다르다: {ledger_id!r} != {cluster_id!r}"
        board_root = self._home.board_root
        if not (board_root / ".git").exists():
            return None, None, f"recovery는 분리 board-git이 필요하다: {board_root}"
        members = tuple(cluster_store.cluster_tickets(ledger))
        if (not members or len(members) != len(set(members))
                or members != self._declared_tickets):
            return None, None, f"cluster 멤버 집합이 비었거나 중복·drift했다: {members}"

        evidence, problem = self._revision_map(
            self._integrated_revision_args, "--integrated-rev")
        if problem:
            return None, None, problem
        legacy, problem = self._revision_map(
            self._legacy_base_revision_args, "--legacy-base-rev")
        if problem:
            return None, None, problem
        member_set = set(members)
        if set(evidence) != member_set:
            missing = sorted(member_set - set(evidence))
            extra = sorted(set(evidence) - member_set)
            return None, None, f"evidence mapping 전수성 위반 (누락={missing}, 여분={extra})"

        head, problem = self._commit_oid(tree, self._head_revision(tree), label="제품 exact HEAD")
        if problem:
            return None, None, problem
        assert head is not None
        normalized_revisions: dict[str, str] = {}
        member_anchors: dict[str, dict[str, str]] = {}
        # 귀속 유일성은 **전 catalog 를 읽었다는 증명** 위에서만 성립한다 — 판독 불능 장부가
        # 하나라도 있으면 그 파일이 중복 귀속일 가능성을 배제하지 못하므로 첫 write 전에 정지한다.
        ledgers, catalog_problem = cluster_store.all_clusters_strict(self._home)
        if catalog_problem is not None:
            return None, None, catalog_problem
        for tid in members:
            fm, problem = self._reconcile_ticket_frontmatter(tid)
            if problem:
                return None, None, problem
            assert fm is not None
            declared = self._cluster_id(fm.get("cluster"))
            owners = [
                self._cluster_id(row.get("id")) for row in ledgers
                if tid in cluster_store.cluster_tickets(row)
            ]
            if declared != cluster_id or owners != [cluster_id]:
                return None, None, (f"{tid} 양방향 cluster 귀속 불일치 "
                                    f"(ticket={declared or '없음'}, ledgers={owners})")
            revision, problem = self._commit_oid(
                tree, evidence[tid], label=f"{tid} evidence")
            if problem:
                return None, None, problem
            assert revision is not None
            claimed = str(fm.get("claimed_rev") or "").strip()
            if claimed:
                if tid in legacy:
                    return None, None, f"{tid}는 claimed_rev가 있어 legacy base를 받을 수 없다"
                anchor, problem = self._commit_oid(
                    tree, claimed, label=f"{tid} claimed_rev anchor")
                source = "claimed_rev"
            else:
                supplied = legacy.get(tid)
                if supplied is None:
                    return None, None, f"{tid}는 claimed_rev가 없어 --legacy-base-rev가 필수다"
                anchor, problem = self._commit_oid(
                    tree, supplied, label=f"{tid} operator legacy anchor")
                source = "operator-supplied"
            if problem:
                return None, None, problem
            assert anchor is not None
            if not self._is_ancestor(tree, anchor, revision):
                return None, None, f"{tid} evidence가 anchor 이후가 아니다: {anchor} !<= {revision}"
            if not self._is_ancestor(tree, revision, head):
                return None, None, f"{tid} evidence가 제품 exact HEAD의 조상이 아니다: {revision} !<= {head}"
            normalized_revisions[tid] = revision
            member_anchors[tid] = {"anchor": anchor, "anchor_source": source}

        expected_legacy = {
            tid for tid in members if member_anchors[tid]["anchor_source"] == "operator-supplied"
        }
        if set(legacy) != expected_legacy:
            return None, None, (f"legacy base mapping 전수성 위반 "
                                f"(필요={sorted(expected_legacy)}, 제공={sorted(legacy)})")
        closure = {
            "mode": "reconciled",
            "integration_head": head,
            "member_revisions": normalized_revisions,
            "member_anchors": member_anchors,
        }
        status = str(ledger.get("status") or "").strip()
        existing = ledger.get("closure")
        if status == "closed":
            if existing != closure:
                return None, status, "closed 장부의 closure evidence가 이번 명시 증거와 다르다"
        elif status == "open":
            if existing not in (None, closure):
                return None, status, "open 장부에 다른 closure evidence가 남아 있다"
        else:
            return None, status, f"recovery는 open all-done 전용이다 (현재 status={status!r})"
        return closure, status, None

    def _head_revision(self, tree: Path) -> str:
        rc, out = self._git_stdout(tree, ["rev-parse", "--verify", "HEAD"])
        if rc != 0 or not out.strip():
            raise finish_rules._CloseObservationFailure(f"제품 exact HEAD를 읽지 못했다 ({tree})")
        return out.strip().splitlines()[0]

    def _report_other_slots(self) -> None:
        """선택 밖 leased slot은 읽기만 하고 path·branch·dirty를 loud 보고한다."""
        try:
            data = json.loads(
                fs.read_text_shared(pm_paths.leases_file(self._home), encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            print(f"  ⚠ 다른 슬롯 상태를 읽지 못했다: {type(exc).__name__}: {exc}", file=sys.stderr)
            return
        rows = data.get("leases", []) if isinstance(data, dict) else []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or row.get("state") != "leased":
                continue
            slot = str(row.get("slot") or "").strip()
            if not slot or slot == self._slot:
                continue
            path = self._home.root / slot
            rc, out = self._git_stdout(path, ["symbolic-ref", "--quiet", "--short", "HEAD"])
            branch = out.strip() if rc == 0 and out.strip() else "unknown"
            try:
                dirty = self._reconcile_dirty_paths(path)
                dirty_label = f"dirty({len(dirty)})" if dirty else "clean"
            except finish_rules._CloseObservationFailure:
                dirty_label = "unknown"
            print(f"  · 비선택 슬롯 보존: path={path} branch={branch} state={dirty_label}")

    def _commit_reconcile_pointer(self) -> str | None:
        """Recovery가 허용하는 유일한 PM-home write: board submodule pointer commit."""
        pointer = self._board_pointer_path()
        if pointer is None:
            print("  PM 홈 board pointer 무대상")
            return None
        if not self._pending_paths(self._home.root, [pointer]):
            print("  PM 홈 board pointer 이미 최신 — 건너뜀")
            return None
        if self._dry_run:
            print(f"  [dry-run] cwd={self._home.root} git add/commit -- {pointer}")
            return None
        rc, out = self._git(self._home.root, ["add", "--", pointer])
        if rc != 0:
            return f"board pointer stage 실패 (rc={rc}): {out.rstrip()}"
        unit, title = self._unit()
        message = _CLOSE_BOARD_MESSAGE.format(unit=unit, title=title)
        rc, out = self._git(self._home.root, ["commit", "-m", message, "--", pointer])
        if rc != 0:
            return f"board pointer commit 실패 (rc={rc}): {out.rstrip()}"
        print(f"  ✓ PM 홈 board pointer 기록 ({message})")
        return None

    def _reconcile_ledger_pending(self) -> bool:
        """Crash 뒤 closed ledger가 board-git에 아직 미기록인지 관측한다."""
        board_root = self._home.board_root
        ledger_path = cluster_store.cluster_ledger_path(self._home, self._cluster)
        try:
            pathspec = ledger_path.relative_to(board_root).as_posix()
        except ValueError as exc:
            raise finish_rules._CloseObservationFailure(
                f"cluster ledger가 board-git 밖을 가리킨다: {ledger_path}") from exc
        return bool(self._pending_paths(board_root, [pathspec]))

    def _run_reconcile(self) -> int:
        """이미 통합된 all-done cluster를 git mutation 없이 감사 가능하게 닫는다."""
        try:
            first, first_status, problem = self._validate_reconcile()
        except finish_rules._CloseObservationFailure as failure:
            first, first_status, problem = None, None, f"관측 실패 — {failure}"
        if problem:
            print(f"\n[중단] reconcile preflight — {problem}", file=sys.stderr)
            return 1
        assert first is not None
        self._report_other_slots()
        try:
            second, second_status, problem = self._validate_reconcile()
        except finish_rules._CloseObservationFailure as failure:
            second, second_status, problem = None, None, f"관측 실패 — {failure}"
        if problem or second != first or second_status != first_status:
            detail = problem or "제품 HEAD 또는 board 상태가 bookend 사이에 drift했다"
            print(f"\n[중단] reconcile bookend — {detail}", file=sys.stderr)
            return 1
        if self._dry_run:
            print(f"[dry-run] {self._cluster} all-done reconcile: {first}")
            return 0

        prior: dict | None = None
        if first_status == "open":
            with fs.board_lock(self._home):
                third, third_status, problem = self._validate_reconcile()
                if problem or third != first or third_status != "open":
                    detail = problem or "최종 write 직전 board/HEAD가 drift했다"
                    print(f"\n[중단] reconcile final bookend — {detail}", file=sys.stderr)
                    return 1
                prior = dict(cluster_store.load_cluster(self._home, self._cluster) or {})
                updated = dict(prior)
                updated["closure"] = first
                updated["status"] = _CLUSTER_STATUS_CLOSED
                updated[_CLOSE_STEP_KEY] = self.STEPS[-1][0]
                cluster_store.dump_cluster(self._home, updated)
            needs_board_sync = True
        else:
            try:
                needs_board_sync = self._reconcile_ledger_pending()
            except finish_rules._CloseObservationFailure as failure:
                print(f"\n[중단] reconcile board persistence 관측 실패 — {failure}", file=sys.stderr)
                return 1
            if needs_board_sync:
                print("  동일 closure evidence의 미완 board-git 기록을 재개")
            else:
                print("  동일 closure evidence가 이미 board-git에 기록됨 — 장부 write 건너뜀")

        if needs_board_sync:
            ledger_path = cluster_store.cluster_ledger_path(self._home, self._cluster)
            try:
                block = self._sync_board_git([ledger_path])
            except finish_rules._CloseObservationFailure as failure:
                block = f"board-git 기록 관측 실패 — {failure}"
            if block is not None:
                if prior is not None:
                    with fs.board_lock(self._home):
                        cluster_store.dump_cluster(self._home, prior)
                print(f"\n[중단] {block}", file=sys.stderr)
                return 1

        block = self._commit_reconcile_pointer()
        if block is not None:
            print(f"\n[중단] {block} — 같은 증거로 재실행하면 pointer 단계만 재개한다.",
                  file=sys.stderr)
            return 1
        print(f"\n[완료] {self._cluster} all-done reconcile 완료 (제품/slot write 0).")
        return 0

    def _steps(self) -> tuple[tuple[str, str, str | None], ...]:
        """이번 종결이 밟는 단계 — 살아 있는 멤버가 없으면 코드 단계가 목록에서 빠진다.

        폐기를 아는 단계를 뒤에 더하는 대신 **무엇을 밟을지**를 앞에서 정한다. 그래야 그
        단계들의 선검사(통합 브랜치 실재·묶음 브랜치 실재·완료 기록 가능)도 함께 무대상이 된다
        — 머지할 코드가 없는 묶음에 브랜치를 요구하지 않는다.
        """
        if self._tickets:
            return self.STEPS
        return tuple(step for step in self.STEPS if step[0] not in self.CODE_STEP_KEYS)

    def preflight(self) -> list[str]:
        """첫 부작용 앞에서 읽기만으로 판정 가능한 차단 사유를 **전건** 모은다.

        단계 선언(`STEPS` 3번째 값)이 대상 여부의 단일 진실이다 — 여기서 단계 목록을 다시
        적지 않는다. 통과한 종결은 **읽기로 알 수 있는 사유로는** 중간에 멈추지 않는다:
        선검사가 통과시켰는데 뒤 단계가 같은 판정 함수로 멈추면 그것이 결함이다.

        `--dry-run` 여부와 무관하게 항상 먼저 돈다.
        """
        labelled: dict[str, str] = {}
        for _key, label, precheck in self._steps():
            if precheck is None:
                continue
            for reason in getattr(self, precheck)():
                # 두 단계가 같은 조건을 각자 판정하는 자리가 있다(기준 브랜치 미선언·현재
                # 브랜치 불일치는 재배치와 머지가 둘 다 본다). 그대로 누적하면 목록의 건수가
                # 부풀어 사람이 고칠 것을 두 개로 읽는다 — 사유 **본문**으로 접고 먼저 만난
                # 라벨만 남긴다. `_pre_*` 는 각자 독립 판정 그대로다(단계가 서로를 알지 않는다).
                labelled.setdefault(reason, f"[{label}] {reason}")
        return list(labelled.values())

    def run(self) -> int:
        """종결 파이프라인 실행 — 0=성공, 1=실패(그 단계에서 정지).

        단계는 두 가지로 멈춘다: **차단 사유**(관측은 됐고 조건이 안 맞는다)와 **관측 실패**
        (판정 자체가 안 된다). 후자를 통과시키면 그 단계가 끝났다는 증거가 없는 채로 종결이
        진행되므로, 같은 자리에서 같은 rc 로 멈춘다."""
        try:
            block = self._resolve_members()
            if not block and not self._reconcile_integrated:
                block = self._open_all_done_block()
        except finish_rules._CloseObservationFailure as failure:
            print(f"\n[중단] 관측 실패 — {failure}", file=sys.stderr)
            return 1
        if block:
            print(f"\n[중단] {block}", file=sys.stderr)
            return 1
        if self._reconcile_integrated:
            return self._run_reconcile()
        print(f"[close] {self._cluster} 종결 시작 "
              f"(dry_run={self._dry_run}, 멤버 {len(self._declared_tickets)}: "
              f"{', '.join(self._declared_tickets)})")
        discarded = [
            tid for tid in self._declared_tickets if tid not in self._tickets
        ]
        if discarded:
            print(f"  처분 종결 멤버 {len(discarded)}: {', '.join(discarded)} — 코드 산출이 "
                  f"없어 코드 단계는 대상이 아니다({', '.join(sorted(self.CODE_STEP_KEYS))})")
        # 작업 트리는 실행마다 git 에서 다시 찾는다(재실행이 곧 재개). 트리 없이는 트리를 보는
        # 선검사를 돌릴 수 없으므로 해소 실패는 선검사 앞에서 단독으로 멈춘다. 살아 있는 멤버가
        # 없으면 코드 단계가 없어 찾지 않는다.
        if self._tickets:
            try:
                tree, problem = cluster_code_tree.resolve_code_tree(self._home, self._cluster, run_git_stdout_at=self._git_stdout)
            except finish_rules._CloseObservationFailure as failure:
                tree, problem = None, f"관측 실패 — {failure}"
            if problem:
                print(f"\n[중단] 작업 트리 해소 — {problem}", file=sys.stderr)
                return 1
            assert tree is not None
            self._finisher.use_code_tree(tree)
        try:
            reasons = self.preflight()
        except finish_rules._CloseObservationFailure as failure:
            print(f"\n[중단] 관측 실패 — {failure}", file=sys.stderr)
            return 1
        if reasons:
            print(f"\n[중단] 종결 선검사 — 막는 조건 {len(reasons)}건. 아직 아무 부작용도 "
                  "내지 않았다. 전부 해소한 뒤 다시 실행하라:", file=sys.stderr)
            for index, reason in enumerate(reasons, start=1):
                print(f"  {index}. {reason}", file=sys.stderr)
            return 1
        steps = self._steps()
        total = len(steps)
        for index, (key, label, _precheck) in enumerate(steps, start=1):
            print(f"\n[{index}/{total}] {label}...")
            try:
                block = getattr(self, f"_step_{key}")()
            except finish_rules._CloseObservationFailure as failure:
                block = f"관측 실패 — {failure}"
            if block:
                print(f"\n[중단] {block}", file=sys.stderr)
                print(f"  · 종결은 [{index}/{total}] 에서 멈췄다 — 다시 실행하면 끝난 "
                      "단계는 건너뛰고 이 단계부터 이어간다.", file=sys.stderr)
                return 1
            self._record_progress(key)
        if self._dry_run:
            print(f"\n[dry-run] {self._cluster} 종결 계획만 출력했다.")
        else:
            print(f"\n[완료] {self._cluster} 종결 완료.")
        return 0
