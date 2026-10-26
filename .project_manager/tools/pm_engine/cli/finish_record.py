"""티켓 완료 기록 — 진입 게이트·회귀·log 스켈레톤·board complete·선언 경로 stage (층: cli).

`TicketFinisher` 는 subprocess·판정 seam 을 주입받아 기록 순서를 실행한다. 경로 기본값은
생성 시 명시 Home 에서 해소하고, 판정·조회·측정은 finish_* 소유 모듈을 직접 부른다.
"""
from __future__ import annotations

import datetime
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Callable

from pm_engine.app import completion_gate, finish_context, finish_private_refs, tier_signals
from pm_engine.domain import finish_rules, handoff_text
from pm_engine.infra import (
    finish_baseline, finish_git, finish_store, fs, lease_store, log_store, pm_paths,
    python_env, repo_coordinates, review_diff, ticket_store,
)


# ── 로그 스켈레톤 ───────────────────────────────────────────────────────

# 회귀 baseline 은 *실측* new_total 1줄만 남긴다
# 합계는 status.md 에 박제하지 않으므로 delta 는 PM 이 서술로 채운다·history 단일 진실=log).
# task 태그 sentinel은 pm_log.py·pm_handoff.py의 동명 상수와 미러한다.
# 모듈 격리를 유지하려고 각 생산자가 상수를 소유한다.
_TASK_TAG_PREFIX = "task:"


LOG_SKELETON_TEMPLATE = """\
## [{date}] {entry_type} | [[{ticket_id}]] — {title}{task_tag}

- <!-- PM: 무엇을·왜 서술 -->
- 테스트: 회귀 {new_total} / {new_total} (실측 · 직전 대비 delta 는 PM 서술).
- board: done {board_before}→{board_after}.
"""

# 스켈레톤 헤딩 줄에서 그 티켓의 기록을 식별하는 표기 — 새 canonical wikilink와 이미
# 발행된 legacy raw-ID를 함께 읽어 재실행·선작성 모두 중복 append를 만들지 않는다.
_LOG_ENTRY_TICKET_MARKERS = ("| [[{ticket}]] — ", "| {ticket} — ")


def build_log_skeleton(
    ticket_id: str,
    title: str,
    new_total: int | str,
    board_before: int,
    board_after: int,
    entry_type: str = "<!-- feat/fix/verify/… -->",
    date: str | None = None,
    task: str | None = None,
) -> str:
    if date is None:
        date = datetime.date.today().isoformat()
    return LOG_SKELETON_TEMPLATE.format(
        date=date,
        entry_type=entry_type,
        ticket_id=ticket_id,
        title=title,
        task_tag=(f" ({_TASK_TAG_PREFIX}{task})" if task else ""),
        new_total=new_total,
        board_before=board_before,
        board_after=board_after,
    )


# ── 핵심 흐름 ──────────────────────────────────────────────────────────


class TicketFinisher:
    """PM 기록 자동화 핵심 로직.

    subprocess 함수를 DI 해 테스트에서 실제 실행 없이 결정론적으로 검증한다.
    broker/dispatch.py 의 clock_fn/sleep_fn DI 패턴과 동일.
    """

    def __init__(
        self,
        home,
        *,
        run_pytest_fn: Callable[[], tuple[int, str]] | None = None,
        run_board_fn: Callable[[list[str]], tuple[int, str]] | None = None,
        run_git_fn: Callable[[list[str]], tuple[int, str]] | None = None,
        board_count_fn: Callable[[], int] | None = None,
        ticket_title_fn: Callable[[str], str] | None = None,
        affected_domain_fn: Callable[[str], list[tuple[str, bool | None]] | None] | None = None,
        run_git_stdout_fn: Callable[[list[str]], tuple[int, str]] | None = None,
        stage_scope_fn: Callable[[str], finish_rules.StageScope] | None = None,
        status_entries_fn: Callable[[], tuple[tuple[str, str], ...]] | None = None,
        run_git_at_fn: Callable[[Path, list[str]], tuple[int, str]] | None = None,
        run_git_stdout_at_fn: Callable[[Path, list[str]], tuple[int, str]] | None = None,
        status_entries_at_fn: Callable[[Path], tuple[tuple[str, str], ...]] | None = None,
        private_ref_block_fn: Callable[[str], str | None] | None = None,
        dod_block_fn: Callable[[str], str | None] | None = None,
        residual_block_fn: Callable[[str], str | None] | None = None,
        self_axis_block_fn: Callable[[str], str | None] | None = None,
        log_file: Path | None = None,
        board_py: Path | None = None,
        venv_python: str | Path | None = None,
        regression_cwd: str | Path | None = None,
        task_workspace: str | Path | None = None,
    ) -> None:
        self._home = home
        # 경로 기본값은 import 시 캡처하지 않고 생성 시 명시 Home 에서 해소한다(명시 값은 그대로).
        self._log_file = (log_file if log_file is not None
                          else pm_paths.wiki_dir(home) / "log" / "current.md")
        self._board_py = (board_py if board_py is not None
                          else home.root / ".project_manager" / "tools" / "board.py")
        self._venv_python = (venv_python if venv_python is not None
                             else python_env._default_python(home))
        # 회귀 cwd seam — 분리된 PM 홈엔 tests/ 가 없으므로 회귀는 활성
        # worktree 슬롯(tests/)에서 돌아야 한다. **즉시 고정하지 않는다** — `regression_cwd`
        # 명시 주입은 그대로 보존(테스트/명시 override)하되, 미지정이면 `__init__` 시점 박제
        # 대신 _default_run_pytest 가 런타임에 `finish_context._regression_cwd` 로 self-host 슬롯을
        # 자동해소한다(`slot_resolution._regression_cwd` 재사용·판정 불능은 Home 루트 폴백).
        self._regression_cwd = str(regression_cwd) if regression_cwd else None
        self._task_workspace = Path(task_workspace) if task_workspace else None

        # subprocess DI — 기본값은 실제 subprocess 호출
        self._run_pytest_fn = run_pytest_fn or self._default_run_pytest
        self._run_board_fn = run_board_fn or self._default_run_board
        self._run_git_fn = run_git_fn or self._default_run_git

        # board 조회 DI — 기본값은 실 board.py import 구현
        self._board_count_fn = board_count_fn or self._default_board_count
        self._ticket_title_fn = ticket_title_fn or self._default_ticket_title

        # domain soft 알림 DI: 기본값은 패키지의 페이지 적재·매칭·stale 판정 구현.
        # None 반환은 페이지 적재·매칭 실패를 뜻하며 완료를 막지 않는다(soft).
        self._affected_domain_fn = affected_domain_fn or self._default_affected_domain

        # git 파싱 전용 러너 — **stdout 만** 돌려준다. `_run_git_fn` 은 진단 메시지를 살리려고
        # stdout+stderr 를 합치는데, 그 값을 `status --porcelain`·`ls-files -z` 파서에 먹이면
        # git 경고 한 줄이 **가짜 잔여 항목**으로 둔갑한다(reviewer). 호출부가 git seam 을 이미
        # 주입했으면(테스트) 그걸 그대로 써 hermetic 을 깨지 않는다.
        self._run_git_stdout_fn = (run_git_stdout_fn or run_git_fn
                                   or self._default_run_git_stdout)

        # git stage 스코프 DI — 기본값은 `touches ∪ 이 실행이 쓴 산출물` 실 해소.
        # 상태 조회도 같은 seam 으로 둬, 테스트가 stage 판정과 보고를 따로 격리한다.
        self._stage_scope_fn = stage_scope_fn or self._default_stage_scope
        self._status_entries_fn = status_entries_fn or self._default_status_entries
        self._run_git_at_fn = run_git_at_fn or self._default_run_git_at
        # task worktree도 홈과 같은 규칙: 기계 판독(`ls-files -z`/`status --porcelain -z`)에는
        # stdout만 쓴다. mutation seam은 stderr 진단을 계속 합쳐 호출자에게 보인다.
        self._run_git_stdout_at_fn = run_git_stdout_at_fn or finish_git._default_run_git_stdout_at
        self._status_entries_at_fn = status_entries_at_fn or self._default_status_entries_at
        # 사설 참조 완료 기록 preflight seam — 차단 사유 문자열 또는 None(통과·경고·판정
        # 불가). 진입 게이트의 첫 자리다 — 측정 폭(touches ∩ 출하 표면)의 내용(사설 참조)이
        # 확정 차단감이면 그 뒤의 DoD·잔여 판정을 물을 이유가 없다. 판정식은 domain.private_refs
        # 소유(사본 0) — 여기서는 측정 폭 재사용 + 호출만 한다.
        self._private_ref_block_fn = private_ref_block_fn or self._default_private_ref_block
        # DoD 기록 게이트 preflight seam — 차단 사유 문자열 또는 None(통과·판정 불가).
        # 규칙 소유자는 board(`_dod_open_items`)이고 여기서는 **더 앞에서 한 번 더** 물을 뿐이다.
        self._dod_block_fn = dod_block_fn or self._default_dod_block
        # 잔여(코드 트리 dirty ⊄ 선언 스코프) preflight seam — 차단 사유 문자열 또는 None(통과).
        # 판정 인구는 코드 트리뿐이다(PM 홈 dev-state 는 `_home_state_prefixes` 로 구조적 제외).
        self._residual_block_fn = residual_block_fn or self._default_residual_block
        # 완료 대상 작업 트리 자기 축 회귀 preflight seam — 차단 문자열 또는 None(통과·경고·
        # 판정불가). 판정 트리는 이 완료 대상 작업 트리 자체다(합성 병합 트리를 만들지 않는다).
        # 튜플의 **마지막** 원소로만 붙는다(비용순 — 사설 참조·DoD 는 서브프로세스 0~2회, 이건
        # base 가 red 일 때만 baseline 회귀를 한 번 더 돈다).
        self._self_axis_block_fn = self_axis_block_fn or self._default_self_axis_block
        # (스코프 밖 staged, 미스테이지 잔여) 건수 — `[완료]` 줄 재고지용(loud 강화).
        self._dirty_summary: tuple[int, int] = (0, 0)
        # 이 실행에서 이미 낸 loud 사유 키 — 같은 해소를 여러 판정 표면이 공유하므로 중복
        # 출력을 막되, 사유가 다르면 각각 낸다(`_warn_once`).
        self._loud_notes: set[str] = set()

    # ── 기본 subprocess 구현 (실제 실행) ─────────────────────────────

    def _default_run_pytest(self) -> tuple[int, str]:
        """회귀를 실행해 (returncode, stdout+stderr) 반환.

        명령 해소:
          - **해소 성공** — `finish_context._resolve_per_repo_test_cmd`(areas prefix 행 > areas repo 행 >
            local.conf)가 준 문자열을 shell 로 실행(board.py 회귀와 동형·비-Python repo 수용).
          - **프레임워크 자기 회귀** — 해소 실패면 현행 그대로
            `[venv_python, -m, pytest, tests/, -q, -n, auto]` venv argv.

        cwd 는 런타임 해소— 명시 주입(`regression_cwd` 인자)이 있으면 그 경로,
        없으면 `finish_context._regression_cwd` 가 self-host 단일 슬롯을 자동해소(홈 cwd 에서도 활성
        worktree 의 tests/ 에서 돌게). 판정 불능은 Home 루트 폴백(현행 보존·additive).
        """
        cwd = (self._regression_cwd if self._regression_cwd is not None
               else finish_context._regression_cwd(self._home))
        per_repo_cmd = finish_context._resolve_per_repo_test_cmd(self._home)
        if per_repo_cmd:
            # multi-PM — per-repo test_cmd 문자열을 shell 로(board.py regression run 과 동형).
            result = subprocess.run(
                per_repo_cmd,
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

    def _log_has_entry(self, ticket_id: str) -> bool:
        """이 티켓의 완료 entry가 log 에 이미 있는가 (읽기 실패·파일 부재는 False).

        판정은 완료 헤딩 줄의 티켓 표기 하나다 — PM이 선작성했거나 스켈레톤의 서술 불릿을
        채워도 그 표기는 남으므로, 한 번 남은 기록을 재실행이 다시 쌓지 않는다.

        읽기 실패는 '없음'으로 접는다 — 이 관측은 차단 판정이 아니라 중복 방지이고, 못 읽었다고
        기록을 막으면 완료 기록이 벽돌이 된다(중복은 사람이 지우면 되고, 누락은 안 그렇다)."""
        try:
            text = fs.read_text_shared(self._log_file, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 — 읽기 실패는 중복 방지 판정 불능.
            return False
        markers = tuple(
            marker.format(ticket=ticket_id) for marker in _LOG_ENTRY_TICKET_MARKERS
        )
        return any(
            line.startswith("## ") and any(marker in line for marker in markers)
            for line in text.splitlines()
        )

    def _code_tree(self) -> Path:
        """코드가 있는 트리 — 회귀 cwd 해소와 **같은 규칙**(task 작업공간 > 명시 > 자동 슬롯).

        분리된 PM 홈엔 코드가 없으므로 diff 측정도 회귀와 같은 트리를 봐야 한다.

        **회귀 skip 여부와 독립**이다 — `--no-pytest` 는 [1/5] 회귀 실행만 건너뛴다. 이 트리는
        사설 참조 측정 root·touches 좌표 정규화·PM-direct 재검이 함께 쓰므로, 회귀를
        안 돌린다고 해소를 건너뛰면 그 소비자들이 PM 홈(분리 형상에선 엔진 import 사본)을 잰다."""
        if self._task_workspace is not None:
            return self._task_workspace
        if self._regression_cwd is not None:
            return Path(self._regression_cwd)
        return Path(finish_context._regression_cwd(self._home))

    def use_code_tree(self, tree: Path) -> None:
        """이 트리를 코드 트리로 쓴다 — 회귀 cwd 와 `_code_tree()` 가 이 값을 본다.

        종결(`ClusterCloser.run`)이 묶음 기록의 브랜치로 찾은 작업 트리를 넘기는 자리다. task
        작업공간 값은 지운다 — 코드 트리는 이 트리 하나다.
        """
        self._regression_cwd = str(tree)
        self._task_workspace = None

    def _measured_ticket_touches(self, ticket_id: str) -> list[str]:
        """현재 티켓 touches — 티켓 읽기·파싱 실패는 전파한다."""
        return finish_store.get_ticket_touches(self._home, ticket_id)

    def _warn_once(self, key: str, message: str) -> None:
        """같은 사유를 한 실행에서 **한 번만** 낸다 (조용한 강등 금지 · 중복 출력 금지).

        판정 표면 여럿(측정 폭·사설 참조)이 같은 해소를 공유하므로, 해소 실패를 부를 때마다
        알리면 같은 줄이 표면 수만큼 반복된다."""
        if key in self._loud_notes:
            return
        self._loud_notes.add(key)
        print(f"  ⚠ {message}", file=sys.stderr)

    def _measurement_anchor(self, ticket_id: str) -> str | None:
        """측정 폭의 기준점 — 통합 브랜치와의 merge-base. 해소 실패는 정지다.

        사설 참조 판정이 이 한 자리에서 값을 받는다 — 리뷰가 본 표면과 완료 기록이 보는
        표면이 갈리지 않는다. integration_anchor의 기준점 판정을 직접 사용한다.

        빈 기준선(커밋 0)에서는 기준선도 HEAD 도 없어 **잴 표면 자체가 없다** — `None` 을
        돌려주고, 호출부가 기존 "측정 불가 = 가드 off + loud 한 줄" 등급으로 접는다(빈 트리
        sentinel 같은 새 rev 를 만들지 않는다)."""
        code_tree = self._code_tree()
        tip = self._integration_tip(ticket_id, code_tree)
        if tip is None:
            return None
        anchor, reason = review_diff.integration_anchor(code_tree, tip)
        if reason is not None:
            raise finish_rules._CloseObservationFailure(f"{ticket_id} {reason}")
        return anchor

    def _integration_tip(self, ticket_id: str, code_tree: Path) -> str | None:
        """판정 기준으로 쓸 통합 브랜치 — 장부 선언 ∩ 이 코드 트리 실재.

        선언이 없거나 이 트리에서 해소되지 않으면 **멈춘다**. 이 값은 측정 폭·사설 참조 신규
        판정·잔여 인구 세 판정의 공통 기준이라, 없는 채로 진행하면 세 판정이 각자 다른 것을
        보고 갈린다(선언과 실재가 갈린 상태를 통과로 위장하지도 않는다).

        **빈 기준선**(`finish_git._unborn_baseline`)은 그 미해소가 아니라 값이 없는 상태라 `None` 이다 —
        커밋이 0이고 HEAD 가 그 브랜치를 가리키면 잴 기준선이 아직 만들어지지 않은 것이고,
        이 종결의 커밋 단계가 그 첫 커밋을 얹는다. 소비자는 각자 그 상태의 참값을 쓴다
        (커밋된 인구 = 공집합 · 측정 폭 = 가드 off)."""
        branch = finish_store._cluster_integration_branch(self._home, ticket_id)
        if not branch:
            raise finish_rules._CloseObservationFailure(
                finish_rules._INTEGRATION_TIP_UNDECLARED.format(ticket=ticket_id))
        rc, _out = self._run_git_stdout_at_fn(
            code_tree,
            ["rev-parse", "--verify", "--quiet", f"{branch}^{{commit}}"],
        )
        if rc != 0:
            if finish_git._unborn_baseline(self._run_git_stdout_at_fn, code_tree, branch):
                return None
            raise finish_rules._CloseObservationFailure(finish_rules._INTEGRATION_TIP_UNRESOLVED.format(
                ticket=ticket_id, branch=branch, tree=code_tree))
        return branch

    def _warn_unborn_baseline(self, ticket_id: str) -> None:
        """빈 기준선에서 가드를 끈 사실의 단일 loud 표면 — 조용한 통과가 아니다."""
        branch = finish_store._cluster_integration_branch(self._home, ticket_id) or "(미선언)"
        self._warn_once(
            f"unborn-baseline:{ticket_id}",
            finish_rules._UNBORN_BASELINE_GUARD_OFF.format(ticket=ticket_id, branch=branch),
        )

    def _normalize_measured_touches(self, touches: Sequence[str]) -> list[str] | None:
        """한 티켓의 touches 를 측정 트리 좌표로 옮긴다(정규화 불능이면 None).

        PM 홈 좌표(`work/<repo>_<N>/…`)를 그대로 쓰면 측정 트리(코드 worktree)에 그 경로가 없다.
        정규화 규칙은 stage 경로가 쓰는 공용 `repo_coordinates` seam 그대로다. None 의 처리는
        호출부가 소유한다(사설 참조는 가드 off, 잔여 판정은 제외 근거 없음, PM 직접 수정 재검은
        skip 경고).
        """
        if not touches:
            return None
        if not any(finish_rules._has_worktree_touch_prefix(touch) for touch in touches):
            return list(touches)
        # 측정 트리 해소는 try **밖**이다 — 그쪽 실패가 여기 좌표 예외 처리에 섞이면
        # "정규화 실패" 한 줄로 위장된다.
        workspace = self._code_tree()
        try:
            return list(repo_coordinates.normalize_repo_paths(
                touches, pm_root=self._home.root, workspace=workspace,
            ))
        except repo_coordinates.RepoCoordinateError:
            return None

    def _default_private_ref_block(self, ticket_id: str) -> str | None:
        """사설 참조 preflight 판정 — 차단감이 있으면 차단 문자열, 아니면 None(경고는
        stderr 로 별도 · rc 는 안 바꾼다).

        차단감은 두 갈래의 합집합이다 — (a) actionable(안전 삭제 단위가 잡히는 참조)은 통합
        브랜치 도달 여부와 무관하게, (b) 통합 브랜치에 아직 없는 줄의 raw 참조는 이 작업이
        새로 들인 유입이라 함께 차단한다. 통합 브랜치가 이미 가진 줄의 raw 는 blame sha 를
        실어 알린다(차단감이 따로 있으면 차단 문구 안에, 없으면 stderr 경고로).

        측정 불가(touches 부재·표면 교집합 없음)는 **가드 off** 다 — 이 축의 실패로
        완료 기록을 막지 않는다(hard 차단은 확정 사실에만 건다). 판정
        기준(통합 브랜치)이 없는 것은 그 축에 들지 않는다 — 기준 없는 판정은 통과가 아니라
        정지다."""
        touches = self._normalize_measured_touches(
            self._measured_ticket_touches(ticket_id))
        if not touches:
            return None
        code_tree = self._code_tree()
        surface_paths = finish_private_refs._private_ref_surface_paths(
            self._home, code_tree, touches,
        )
        if not surface_paths:
            return None
        integration_tip = self._integration_tip(ticket_id, code_tree)
        if integration_tip is None:
            self._warn_unborn_baseline(ticket_id)
            return None
        anchor = self._measurement_anchor(ticket_id)
        relative_paths = [path.relative_to(code_tree).as_posix() for path in surface_paths]
        try:
            diff_text = review_diff.measured_diff_text(
                code_tree, "HEAD", relative_paths, claimed_rev=anchor,
            )
        except OSError:
            return None
        if not diff_text.strip():
            return None
        actionable, raw = finish_private_refs._private_ref_diff_offenders(
            surface_paths, code_tree, diff_text,
        )
        if not actionable and not raw:
            return None
        # actionable 은 raw 의 부분집합이다(같은 정규식·같은 스팬) — 같은 좌표를 두 번 세지
        # 않도록 raw 에서 빼고 남은 것만 커밋 여부로 가른다.
        actionable_keys = set(actionable)
        novel_raw, absorbed_raw = finish_private_refs._private_ref_raw_by_novelty(
            code_tree, [entry for entry in raw if entry not in actionable_keys],
            integration_tip, run_git_stdout_at=self._run_git_stdout_at_fn,
        )
        if not actionable and not novel_raw:
            # 통합 브랜치가 이미 가진 줄의 raw 만 남음 — 경고만(흡수분·허용분 재작성 실측상
            # 이 갈래). blame sha 를 실어 1회 읽기로 유입 커밋을 식별할 수 있게 한다.
            for path, line, token, sha in absorbed_raw:
                print(
                    f"  ⚠ 사설 참조 raw 감지(경고만) — {ticket_id} {path}:{line} {token}"
                    f" (git blame {sha})",
                    file=sys.stderr,
                )
            return None
        offenders = [(entry, "") for entry in actionable]
        offenders += [
            ((path, line, token), note) for path, line, token, note in novel_raw
        ]
        offenders.sort()
        lines = [
            f"사설 참조 유입 — {ticket_id} 이번 폭에서 추가한 줄에 채택자가 조회할 수 없는 "
            f"참조가 있다 ({len(offenders)}건):",
        ]
        lines += [
            f"  ✗ {path}:{line} {token}{note}" for (path, line, token), note in offenders
        ]
        if absorbed_raw:
            lines.append(
                f"  같은 폭 흡수분 raw 목록({len(absorbed_raw)}건 · 한 라운드로 정리):"
            )
            lines += [
                f"    · {path}:{line} {token} (git blame {sha})"
                for path, line, token, sha in absorbed_raw
            ]
        return "\n".join(lines)

    def _default_dod_block(self, ticket_id: str) -> str | None:
        """DoD 기록 게이트 preflight 판정 — 차단 사유 문자열, 통과·판정 불가면 None.

        규칙 소유자는 completion_gate(`_dod_open_items`)다 — 판정 사본을 두지 않고 그 함수를 부른다.
        권위 있는 차단은 여전히 [3/5] `board.py complete` 가 하고, 여기서는 **더 앞에서 한 번 더**
        묻는다. 이 preflight 가 없으면 [2/5] 가 log 스켈레톤을 append 한 *뒤에* [3/5] 가 막아,
        차단될 때마다 stray 스켈레톤이 남고 재실행마다 중복 append 된다(실측).

        판정 불가(티켓 부재/손상)는 None 이다 — preflight 는 조기 안내일 뿐이고
        진짜 게이트가 뒤에 있으므로, 여기서 fail-soft 해도 미체크 DoD 가 done 으로 새지 않는다."""
        try:
            _status, path = ticket_store.find_ticket(self._home, ticket_id)
            _fm, body = ticket_store.load_ticket(path)
        except Exception as exc:  # noqa: BLE001 — 조회 실패는 뒤 게이트가 소유(preflight fail-soft).
            return None
        problems = completion_gate._dod_open_items(body)
        if not problems:
            return None
        forms = completion_gate._DOD_VALUE_FORMS
        lines = [f"완료 조건(DoD) 미마감 — {ticket_id} ({len(problems)}건)"]
        lines += [f"  ✗ {problem}" for problem in problems]
        lines.append(f"  · 각 항목을 {forms} 로 마감하라." if forms else "")
        lines.append("  · board.py complete 의 게이트와 **같은 판정**이다 — "
                     "log/current.md 스켈레톤을 남기기 전에 미리 묻는다.")
        return "\n".join(line for line in lines if line)

    def _home_state_prefixes(self, tree: Path) -> tuple[str, ...]:
        '`tree` 안에 PM 홈 dev-state(board 루트·wiki 루트)가 있으면 그 트리-상대 접두를 낸다.\n\n        새 경로를 여기서 하드코딩하지 않는다 — Home 의 board 루트·`Home wiki` 은 엔진이\n        이미 소유한 PM 홈 값이다. 분리 형상에서는 두 루트가 PM 홈(`self._home`)에 있고 `tree` 는 다른\n        코드 worktree라 결과가 빈 튜플이다(제외 대상 자체가 없다 — 규칙이 약해지지 않는다).\n        임베디드 형상에서는 `tree == self._home.root` 라 둘 다 그 안에 있어, 다른 PM 세션의 wiki WIP 로 내\n        완료가 막히지 않는다. 해소 실패는 빈 튜플(fail-soft·제외 없음 = 판정 인구에 남겨 더\n        엄격한 쪽으로 접는다).'
        try:
            tree_resolved = tree.resolve()
        except OSError:
            return ()
        candidates: list[Path] = [self._home.board_root]
        candidates.append(pm_paths.wiki_dir(self._home))
        prefixes: list[str] = []
        for candidate in candidates:
            try:
                relative = candidate.resolve().relative_to(tree_resolved)
            except (OSError, ValueError):
                continue
            prefixes.append(relative.as_posix())
        return tuple(prefixes)

    def _default_residual_block(self, ticket_id: str) -> str | None:
        """코드 트리 변경 중 누구도 선언하지 않은 것이 있으면 차단 사유 문자열, 통과면 None.

        판정 인구는 `merge-base(<통합 tip>, HEAD)..작업 트리` 다(`_residual_split`) — 커밋
        여부가 판정을 바꾸지 않는다. 인구를 세는 트리는 **코드 트리**(`plan.cwd ==
        self._code_tree()`)뿐이다 — 분리 형상에서 PM 홈 산출물 계획(cwd=Home 루트)은 다른 실행·
        다른 PM 세션의 wiki WIP 가 상주하는 공유 표면이라 구조적으로 제외된다. 임베디드
        형상(코드 트리 자신이 PM 홈)에서는 `_home_state_prefixes` 가 board 루트·wiki 루트만
        같은 방식으로 제외한다.

        방향은 묻지 않는다 — 미스테이지 잔여·스코프 밖 staged·통합 미반영 커밋 어느 쪽이든
        비어 있지 않으면 차단이다(`git add` 나 `git commit` 한 번이 우회로가 되면 안 된다).
        `scope_error`(스코프 산출 실패)도
        차단이다 — 이 실행이 코드 트리에서 아무것도 stage 하지 못한다는 확정 사실이라, 여기서
        '판정 불가'로 접으면 전량 미머지가 조용히 통과한다. 이때 스코프는 **빈 스코프로
        읽는다**(아무 경로도 못 덮으므로) — 코드 트리 dirty 전량이 잔여가 되고, PM 홈
        제외 규칙은 정상 갈래와 동일하게 적용해 잔여 목록·표준 처방을 원인 문장에 동봉한다.

        잔여가 없으면 이어서 선언 경로의 변경이 같은 인구에 있는지 본다(`_declared_change_problem`)."""
        code_tree = self._code_tree()
        home_prefixes = self._home_state_prefixes(code_tree)
        for plan in self._stage_plans(ticket_id):
            if plan.cwd != code_tree:
                continue
            scope, scope_error = plan.scope
            if scope_error:
                cause = (
                    f"완료 기록 거부 — {ticket_id} stage 스코프를 산출하지 못했다 "
                    f"({scope_error}). 이 실행은 코드 트리에서 아무것도 stage 하지 못하므로 "
                    "변경 전량이 미머지로 남는다 — 먼저 원인(board 사본·PyYAML 등)을 해소하라."
                )
                staged_out, unstaged, committed = self._residual_split(
                    plan.cwd, (), home_prefixes, ticket_id=ticket_id)
                if not staged_out and not unstaged and not committed:
                    return cause
                return cause + "\n" + self._residual_lines(
                    staged_out, unstaged, committed)
            staged_out, unstaged, committed = self._residual_split(
                plan.cwd, scope, home_prefixes, ticket_id=ticket_id)
            if staged_out or unstaged or committed:
                return self._residual_block_message(
                    ticket_id, staged_out, unstaged, committed)
            problem = self._declared_change_problem(ticket_id, plan.cwd)
            if problem is not None:
                return problem
        return None

    def _declared_change_problem(self, ticket_id: str, cwd: Path) -> str | None:
        """선언한 코드 경로의 변경이 이번 종결이 머지할 변경에 하나도 없으면 차단 사유.

        머지할 변경 = `merge-base(<통합 tip>, HEAD)..작업 트리`(커밋분 ∪ 미커밋) — 잔여 판정과 같은
        인구다. 잔여 판정은 선언 **밖** 변경을 막고, 이 판정은 선언 **안** 변경이 그 인구에 있는지
        묻는다(switch 전 커밋·다른 폴더 작성·통합 브랜치 직접 커밋은 인구 밖이라 머지 없이 닫혔다).

        선언 경로는 분리 코드 트리면 코드 몫, 임베디드면 touches 전부다. 비면(코드 없는 pm-direct·홈에만
        있는 경로) 대상이 아니다. 단 분리 코드 트리에서 홈에만 있는 경로가 홈 git 미추적 새 경로이고 홈 상태
        루트(board·wiki) 밖이면 코드 몫으로 센다(`_home_new_paths` — 작업 폴더 대신 홈에 쓴 새 파일은 홈
        커밋에만 실려 머지되지 않는다). 판정 단위는 멤버이고 선언 경로 하나라도 인구에 있으면 통과다. 어느 트리에도
        없는 선언(미정 경로)은 홈 변경으로도 충족된다(홈 문서 삭제). 티켓 판독 실패는 전파한다 —
        빈 touches 로 접으면 "판정 대상 아님" 으로 통과한다."""
        frontmatter = finish_store._ticket_frontmatter_snapshot(self._home, ticket_id)
        touches = finish_rules._clean_touches(frontmatter.get("touches"))
        home_share: list[str] = []
        if touches and self._is_separated_code_tree(self._home, cwd):
            touches, home_share = self._split_touches_by_tree(ticket_id, cwd)
            home_new = self._home_new_paths([touch for touch in home_share if touch not in touches])
            touches += home_new
            home_share = [touch for touch in home_share if touch not in home_new]
        if not touches:
            return None
        declared = self._normalize_measured_touches(touches)
        if declared is None:
            raise finish_rules._CloseObservationFailure(
                f"{ticket_id} 선언 경로를 코드 트리 좌표로 옮기지 못했다 — 선언 변경의 머지 여부를 판정할 수 없다")
        # 어느 트리에도 실재하지 않는 선언(신규 파일 또는 삭제)은 두 계획에 모두 실린다 — PM 홈 쪽
        #   변경(홈 문서 삭제)도 이 종결이 커밋하는 변경이다.
        unresolved_home = [touch for touch in touches if touch in home_share]
        if unresolved_home:
            home_paths = self._observed_status_paths(self._home.root)
            if any(finish_rules._touch_claims_path(touch, path)
                   for touch in unresolved_home for path in home_paths):
                return None
        population = self._observed_status_paths(cwd)
        tip = self._integration_tip(ticket_id, cwd)
        if tip is not None:
            rc, out = self._run_git_stdout_at_fn(cwd, ["diff", "--name-only", "-z", f"{tip}...HEAD"])
            if rc != 0:
                raise finish_rules._CloseObservationFailure(finish_rules._RESIDUAL_POPULATION_UNRESOLVED.format(
                    ticket=ticket_id, tip=tip))
            population.update(path for path in out.split("\0") if path)
        if any(finish_rules._touch_claims_path(touch, path) for touch in declared for path in population):
            return None
        return (f"완료 기록 거부 — {ticket_id} 가 선언한 코드 경로({', '.join(touches)})의 변경이 이번 "
                f"종결이 머지할 변경(통합 브랜치 {tip or '(첫 커밋 전)'} 이후 {cwd} 의 커밋·미커밋)에 하나도 "
                "없다. 다른 폴더·브랜치에서 고쳤다면 그 변경을 이 작업 트리로 가져오고(커밋했다면 "
                "`git cherry-pick`), 고칠 코드가 없는 티켓이면 touches 에서 그 경로를 뺀다.")

    def _home_new_paths(self, home_only: Sequence[str]) -> list[str]:
        """홈에만 있는 선언 중 홈 git 이 하나도 추적하지 않는 새 경로이고 홈 상태 루트(board·wiki) 밖인 것.

        조회 실패는 관측 실패로 멈춘다(추적 중으로 접으면 그 새 파일이 다시 홈 몫으로 빠진다)."""
        state_roots = self._home_state_prefixes(self._home.root)
        candidates = [touch for touch in home_only if not finish_rules.scope_covers(state_roots, touch)]
        if not candidates:
            return []
        rc, out = self._run_git_stdout_at_fn(self._home.root, ["ls-files", "-z", "--", *candidates])
        if rc != 0:
            raise finish_rules._CloseObservationFailure(
                f"{self._home.root} 에서 선언 경로의 추적 여부를 관측하지 못했다 (git ls-files rc={rc})")
        tracked = [path for path in out.split("\0") if path]
        return [touch for touch in candidates
                if not any(finish_rules._touch_claims_path(touch, path) for path in tracked)]

    def _observed_status_entries(self, cwd: Path) -> tuple[tuple[str, str], ...]:
        """그 트리의 워킹트리 상태 — 조회 실패는 관측 실패로 멈춘다(clean 으로 접지 않는다)."""
        try:
            return self._status_entries_fn() if cwd == self._home.root else self._status_entries_at_fn(cwd)
        except finish_rules.StatusObservationError as exc:
            raise finish_rules._CloseObservationFailure(f"작업 트리 상태를 관측하지 못했다 ({cwd}) — {exc}") from exc

    def _observed_status_paths(self, cwd: Path) -> set[str]:
        """선언 변경 판정이 보는 그 트리의 미커밋 경로."""
        return {path for _code, path in self._observed_status_entries(cwd)}

    def _residual_split(
        self, cwd: Path, scope: Sequence[str], home_prefixes: Sequence[str], *,
        ticket_id: str,
    ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        """잔여 판정 인구를 (스코프 밖 staged, 미스테이지 잔여, 통합 미반영 커밋)로 가른다.

        인구는 `merge-base(<통합 tip>, HEAD)..작업 트리` = 커밋분 ∪ dirty 다. 산출을 개발자
        라운드가 커밋하는 모델에서 dirty 만 세면 **같은 선언 밖 변경이 커밋 전에는 차단, 커밋
        뒤에는 통과**가 된다 — 조작 순서가 판정의 입력으로 남는다. 세 갈래를 같은 인구에서 보고
        같은 선언으로 대조한다.

        인구에서 빼는 것은 둘이다 — PM 홈 dev-state 접두(다른 세션의 wiki WIP)와 **같은 묶음
        다른 멤버가 선언한 경로**(`_sibling_member_scopes`). 묶음은 브랜치 하나를 공유하므로
        멤버 B 의 커밋이 멤버 A 의 완료 기록을 막으면 크기 N 묶음은 종결 자체가 불가능하다.
        어느 멤버도 선언하지 않은 변경만 차단감이다.

        정상·`scope_error` 두 갈래가 이 함수를 공유한다(후자는 빈 스코프로 부른다).
        """
        staged_out, unstaged = self._dirty_split(scope, cwd=cwd)
        if self._integration_tip(ticket_id, cwd) is None:
            # 빈 기준선 — 아직 한 번도 커밋되지 않은 저장소다. 이 게이트가 세는 것은 **이
            # 작업이 만든** 선언 밖 변경인데, 커밋이 0이면 트리의 모든 파일이 미추적이라
            # 뼈대 전부가 인구가 된다(어떤 첫 티켓도 닫히지 않는다). 인구는 이 종결이 커밋할
            # 선언 경로뿐이고, 그 밖은 차단이 아니라 값으로 보고한다.
            self._warn_once(
                f"unborn-skeleton:{ticket_id}",
                finish_rules._UNBORN_SKELETON_NOTE.format(
                    ticket=ticket_id, count=len(staged_out) + len(unstaged)),
            )
            return (), (), ()
        excluded = tuple(home_prefixes) + self._sibling_member_scopes(ticket_id)
        if excluded:
            staged_out = tuple(line for line in staged_out
                               if not finish_rules.scope_covers(excluded, finish_rules._dirty_entry_path(line)))
            unstaged = tuple(line for line in unstaged
                             if not finish_rules.scope_covers(excluded, finish_rules._dirty_entry_path(line)))
        dirty_paths = {finish_rules._dirty_entry_path(line) for line in staged_out + unstaged}
        committed = tuple(
            f"{finish_rules._COMMITTED_ENTRY_CODE} {path}"
            for path in self._committed_out_of_scope(cwd, ticket_id, scope, excluded)
            if path not in dirty_paths      # 같은 경로를 두 갈래로 두 번 세지 않는다
        )
        return staged_out, unstaged, committed

    def _sibling_member_scopes(self, ticket_id: str) -> tuple[str, ...]:
        """같은 묶음 **다른 멤버**가 선언한 경로 — 잔여 판정 인구에서 뺀다.

        귀속 입력은 귀속 창 스냅샷 하나다
        (`get_in_window_ticket_touches` · 사본 0) — 멤버 목록은 묶음 장부가, 경로는 그 티켓
        frontmatter `touches` 가 소유한다. 창 밖 멤버(아직 claim 하지 않은 티켓)는 제외 근거가
        없어 인구에 남는다(과다 차단 쪽 — 선언 누락을 숨기지 않는다).

        경로 비교는 `scope_covers` 다(pathspec magic 은 덮지 않는다) — 해소 불능 선언 하나가
        임의의 커밋을 면제하면 이 게이트가 통째로 무력해진다.

        스냅샷 읽기 실패는 전파한다.
        """
        members = [member for member in finish_store._cluster_member_ids(self._home, ticket_id)
                   if member != ticket_id]
        if not members:
            return ()
        in_window = finish_store.get_in_window_ticket_touches(
            self._home, since=finish_store.get_ticket_claimed_at(self._home, ticket_id),
        )
        scopes: list[str] = []
        for member in members:
            normalized = self._normalize_measured_touches(
                in_window.get(member, []))
            scopes.extend(normalized or [])
        return tuple(dict.fromkeys(scopes))

    def _committed_out_of_scope(
        self, cwd: Path, ticket_id: str, scope: Sequence[str],
        excluded: Sequence[str],
    ) -> tuple[str, ...]:
        """`merge-base(<통합 tip>, HEAD)..HEAD` 중 누구도 선언하지 않은 경로.

        커밋된 인구의 대조 기준은 stage pathspec 이 아니라 **선언**(`touches` ∪ 그 pathspec)
        이다: 선언한 파일을 지우고 커밋하면 그 경로는 stage 가능 목록에서 빠지지만
        (`git_scope_stageable` — 미존재·미추적 제거) 선언은 그대로다. pathspec 만 보면 선언한
        삭제가 잔여로 둔갑한다.

        통합 tip 은 판정 인구의 기준이라 해소하지 못하면 멈춘다(`_integration_tip`) — dirty
        인구로 접으면 커밋된 선언 밖 변경이 보이지 않는 채로 통과한다.
        """
        tip = self._integration_tip(ticket_id, cwd)
        if tip is None:
            return ()       # 빈 기준선 — 커밋이 0이면 커밋된 인구는 공집합이다(참값).
        rc, out = self._run_git_stdout_at_fn(
            cwd, ["diff", "--name-only", "-z", f"{tip}...HEAD"])
        if rc != 0:
            raise finish_rules._CloseObservationFailure(finish_rules._RESIDUAL_POPULATION_UNRESOLVED.format(
                ticket=ticket_id, tip=tip))
        declared = tuple(scope) + tuple(
            self._normalize_measured_touches(
                finish_store.get_ticket_touches(self._home, ticket_id)) or ())
        return tuple(
            path for path in out.split("\0")
            if path and not finish_rules._is_local_runtime_path(path)
            and not finish_rules.scope_covers(declared, path)
            and not finish_rules.scope_covers(excluded, path)
        )

    def _residual_block_message(self, ticket_id: str, staged_out: Sequence[str],
                                unstaged: Sequence[str],
                                committed: Sequence[str] = ()) -> str:
        """잔여 차단 안내 — 원인 헤더 + 잔여 목록·처방 본문(`_residual_lines`)."""
        detail = (f"미스테이지 {len(unstaged)}건 · 스코프 밖 staged {len(staged_out)}건")
        if committed:
            detail += f" · 통합 미반영 커밋 {len(committed)}건"
        header = (
            f"완료 기록 거부 — {ticket_id} 코드 트리에 선언 스코프 밖 변경이 남아 있다 "
            f"({detail})."
        )
        return header + "\n" + self._residual_lines(staged_out, unstaged, committed)

    def _residual_lines(self, staged_out: Sequence[str], unstaged: Sequence[str],
                        committed: Sequence[str] = ()) -> str:
        """잔여 목록(기존 20줄 접기 규약 재사용) + 처방 2가지 — 헤더 없는 본문만.

        `_residual_block_message`(정상 갈래)와 `_default_residual_block` 의 `scope_error`
        갈래(원인 문장이 이미 별도 헤더)가 이 본문을 공유한다."""
        lines: list[str] = []
        if unstaged:
            lines.append("  미스테이지 잔여 (이 커밋에 안 실린다):")
            lines += self._fold_residual_lines(unstaged)
        if staged_out:
            lines.append("  스코프 밖 staged (내 변경이 아닌데 커밋에 실린다):")
            lines += self._fold_residual_lines(staged_out)
        if committed:
            lines.append("  통합 미반영 커밋 (커밋됐지만 어느 티켓도 선언하지 않았다):")
            lines += self._fold_residual_lines(committed)
        lines.append(
            "  처방: (1) 내 작업 누락이면 ticket `touches` 를 보강해 재실행하고, (2) 남의 "
            "변경이면 그 변경을 소유한 티켓의 `touches` 에 선언하라 — 커밋은 회피 수단이 "
            "아니다(커밋된 변경도 같은 인구로 판정한다)."
        )
        return "\n".join(lines)

    def _fold_residual_lines(self, lines: Sequence[str]) -> list[str]:
        """차단 안내용 잔여 목록 — `_RESIDUAL_DIRTY_PREVIEW_LINES` 접기 규약을 그대로 쓴다."""
        folded = [f"    {line}" for line in lines[:self._RESIDUAL_DIRTY_PREVIEW_LINES]]
        hidden = len(lines) - self._RESIDUAL_DIRTY_PREVIEW_LINES
        if hidden > 0:
            folded.append(f"    … 외 {hidden}건")
        return folded

    # ── 완료 대상 작업 트리 자기 축 회귀 판정 ────────────────────────────
    # 판정 트리는 완료 대상 작업 트리 자체다. 대상 집합은 이 트리의 변경분(추적+미추적)과
    # 전-트리 ratchet 가드 목록의 합집합이고, 그 대상을 작업 트리와 claim 시점 baseline
    # 양쪽에서 돌려 함수 단위 노드 ID 로 신규 실패만 골라낸다. 존재-판정("돌았다") 이 아니라
    # 값-대조("base 대비 늘었다")로 차단한다.

    def _default_self_axis_block(self, ticket_id: str) -> str | None:
        """완료 대상 작업 트리에서 base 대비 신규 red 를 판정한다 — 차단 문자열, 통과·판정불가면
        None.

        claim 앵커(board `claimed_rev`)가 없으면(채택자 형상·구 티켓) 비교 기준이 없으므로
        완전히 무발화다 — 작업 트리 회귀조차 돌리지 않는다. 앵커가 있어도 작업 트리가 green
        이면 baseline 은 돌리지 않는다(비용은 red 일 때만 낸다). red 면 baseline 을 claim
        시점으로 materialize 해 같은 대상 집합을 돌리고, 함수 단위 노드 ID 차집합(작업 트리 −
        baseline)이 있으면 차단, 없으면(전부 상속 red) loud 경고만 하고 통과시킨다.
        """
        code_tree = self._code_tree()
        target_files = finish_baseline._self_axis_target_files(
            code_tree, run_git_stdout_at=self._run_git_stdout_at_fn)
        if not target_files:
            return None

        claimed_rev = finish_store.get_ticket_claimed_rev(self._home, ticket_id)
        if not claimed_rev:
            return None

        work_rc, work_out = finish_baseline._run_pytest_subset(
            code_tree, sorted(target_files), venv_python=self._venv_python)
        if work_rc == 0:
            return None  # green — baseline 은 말할 것이 없으므로 돌리지 않는다.
        work_failed = finish_rules._self_axis_failed_node_ids(work_out)

        try:
            baseline_tree = finish_baseline._materialize_tree(
                self._home, code_tree, claimed_rev,
                run_git_stdout_at=self._run_git_stdout_at_fn)
        except (OSError, RuntimeError) as exc:
            print(
                f"  ⚠ 자기 축 회귀 판정 skip — {ticket_id}: baseline 판정 실패({exc})",
                file=sys.stderr,
            )
            return None
        try:
            base_rc, base_out = finish_baseline._run_pytest_subset(
                baseline_tree, sorted(target_files), venv_python=self._venv_python)
        finally:
            # `finally` 안이라 정리 실패를 raise 하면 try 블록의 예외(있다면)를 덮는다 — 형제
            # 규약(pm_import._force_rmtree 호출부)과 같은 형태: loud 경고만 남기고 삼키지 않는다.
            try:
                fs.force_rmtree(baseline_tree)
            except OSError as cleanup_exc:
                print(
                    f"경고: baseline 트리 정리 실패 — 직접 지우세요: {baseline_tree} "
                    f"({type(cleanup_exc).__name__}: {cleanup_exc})",
                    file=sys.stderr,
                )
        base_failed = finish_rules._self_axis_failed_node_ids(base_out) if base_rc != 0 else set()

        new_failed = sorted(work_failed - base_failed)
        if new_failed:
            return finish_rules._self_axis_block(ticket_id, new_failed, work_out)

        inherited = sorted(work_failed & base_failed)
        if inherited:
            print(
                f"  ⚠ 자기 축 회귀 경고 — {ticket_id}: base 에서 이미 red 인 실패뿐이다"
                f"({', '.join(inherited)}) — 차단하지 않는다.",
                file=sys.stderr,
            )
        return None

    def _default_run_board(self, args: list[str]) -> tuple[int, str]:
        """board.py 를 subprocess 로 호출해 (returncode, stdout+stderr) 반환."""
        result = subprocess.run(
            [str(self._venv_python), str(self._board_py)] + args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(self._home.root),
        )
        output = result.stdout + result.stderr
        return result.returncode, output

    def _default_run_git(self, args: list[str]) -> tuple[int, str]:
        """git 명령을 실행해 (returncode, stdout+stderr) 반환 — 실패 진단 메시지 보존용."""
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

    def _default_run_git_stdout(self, args: list[str]) -> tuple[int, str]:
        """git 명령을 실행해 (returncode, **stdout 만**) 반환 — 기계 파싱용.

        `status --porcelain`·`ls-files -z` 출력을 파싱하는 경로는 stderr 가 섞이면 안 된다:
        git 경고(예: CRLF·advice) 한 줄이 그대로 '잔여 항목'·'추적 경로'로 둔갑한다.
        """
        result = subprocess.run(
            ["git"] + args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(self._home.root),
        )
        return result.returncode, result.stdout

    @staticmethod
    def _default_run_git_at(cwd: Path, args: list[str]) -> tuple[int, str]:
        """명시 cwd git 실행 — task worktree 를 PM 홈과 절대 섞지 않는다."""
        result = subprocess.run(
            ["git"] + args, capture_output=True, text=True, encoding="utf-8",
            errors="replace", cwd=str(cwd),
        )
        return result.returncode, result.stdout + result.stderr


    def _default_board_count(self) -> int:
        """done 티켓 수를 반환한다 (명시 Home 의 티켓 디렉터리).

        실패 시 -1 반환.
        """
        return finish_store.count_board_done(self._home)

    def _default_ticket_title(self, ticket_id: str) -> str:
        """ticket_id 의 title 을 명시 Home 의 티켓에서 읽어온다.

        실패 시 빈 문자열 반환.
        """
        return finish_store.get_ticket_title(self._home, ticket_id)

    def _default_affected_domain(self, ticket_id: str) -> list[tuple[str, bool | None]] | None:
        """ticket touches ∩ domain covers 로 영향받는 페이지 (title, stale) 목록 (soft 알림).

        페이지 적재·매칭 실패 → None (조용히 skip). `doc_affected.pages_for_touches` 재사용.
        """
        return finish_context.affected_domain_titles(self._home, ticket_id)

    def _default_stage_scope(self, ticket_id: str) -> finish_rules.StageScope:
        """stage 할 선언 경로 pathspec (`touches ∪ 이 실행이 쓴 산출물`) — 실 해소."""
        return finish_context.stage_scope(self._home, ticket_id, self._log_file,
                                          self._run_git_stdout_fn)

    def _code_stage_scope(self, ticket_id: str, code_tree: Path,
                          touches: Sequence[str] | None = None) -> finish_rules.StageScope:
        """코드 트리에는 ticket touches만 계획한다 (홈 산출물 유입 금지).

        트리를 인자로 받는다 — 코드 트리는 task 작업공간일 수도, 해소된 슬롯 worktree 일 수도
        있고 둘은 stage 규칙이 같다(`_stage_plans` 가 어느 트리인지 판정한다). `touches` 는
        이 트리 몫으로 갈린 부분집합이다(미지정이면 티켓 touches 전체).
        """
        def run_git(args: list[str]) -> tuple[int, str]:
            return self._run_git_stdout_at_fn(code_tree, args)

        return finish_context.stage_scope(self._home, ticket_id, self._log_file, run_git,
                                          repo=code_tree, include_engine_outputs=False,
                                          touches_workspace=code_tree, touches=touches)

    def _split_touches_by_tree(self, ticket_id: str,
                               code_tree: Path) -> tuple[list[str], list[str]]:
        """티켓 touches 를 (코드 트리 몫, PM 홈 몫)으로 가른다.

        판정은 **어느 트리에 실재하는가** 다: 코드 트리에 있으면 코드 트리, 없고 PM 홈에 있으면
        PM 홈(wiki·결정 기록·domain 같은 홈-상주 산출물), 어디에도 없으면 두 계획 모두(신규
        파일이거나 추적 중인 삭제 — 어느 쪽인지는 각 트리의 stage 필터가 판정한다). 두-repo 계획이 코드 몫만 계획하면 홈-상주 touches 가 **어느 repo 에서도 stage 되지
        않고** 잔여 보고에만 남는다 — 그 경로들은 이미 선언돼 있어 "touches 를 보강하라"는 처방도
        듣지 않는다. 정규화 불능이면 전부 코드 몫으로 두어 `stage_scope` 가 기존과 같은 fail-loud
        사유를 내게 한다(여기서 삼키지 않는다).
        """
        touches = list(finish_store.get_ticket_touches(self._home, ticket_id))
        if not touches:
            return [], []
        # 코드 트리 좌표 해소는 측정이 쓰는 공용 seam 을 그대로 쓴다(사본 0·같은 규칙).
        # 그 seam 의 workspace 가 곧 `_code_tree()` 이므로 여기 코드 트리와 같은 트리다.
        in_code_tree = self._normalize_measured_touches(touches)
        if in_code_tree is None or len(in_code_tree) != len(touches):
            return touches, []
        code_touches: list[str] = []
        home_touches: list[str] = []
        for declared, normalized in zip(touches, in_code_tree):
            in_code = (finish_git._path_exists_in(code_tree, normalized)
                       or self._tracked_in(code_tree, normalized))
            in_home = finish_git._path_exists_in(self._home.root, declared)
            if in_code:
                code_touches.append(declared)
            elif in_home:
                home_touches.append(declared)
            else:
                # 어디에도 실재하지 않는 선언 — 신규 파일일 수도, **추적 중인 삭제**일 수도 있다.
                # 한쪽으로만 접으면 삭제가 그 트리에서 추적되지 않아 stage 필터에 떨어지고 어느
                # 계획에도 안 실린다. 둘 다에 넣어도 각 계획의 필터가 자기 트리에서 추적되는 것만
                # 남기므로 이중 stage 는 생기지 않는다.
                code_touches.append(declared)
                home_touches.append(declared)
        return code_touches, home_touches

    def _tracked_in(self, tree: Path, relative: str) -> bool:
        """`tree` 가 작업 트리에 없는 이 선언 경로(파일·디렉터리)를 추적하는가 — 지운 추적 경로의 몫 판정.

        작업 트리에 없는 경로를 index 나 HEAD 가 가지면 status 가 그 삭제(` D`·`D `)를 낸다. 미리
        `git rm` 한 경로(index 에도 없다)까지 한 조회로 본다. 조회 실패는 관측 실패로 멈춘다(미추적으로
        접으면 삭제가 다른 계획으로 간다)."""
        candidate = relative.strip()
        if not candidate or candidate.startswith(":(") or any(ch in candidate for ch in "*?["):
            return False
        rc, out = self._run_git_stdout_at_fn(
            tree, ["status", "--porcelain", "-z", "--untracked-files=no", "--", candidate])
        if rc != 0:
            raise finish_rules._CloseObservationFailure(
                f"{tree} 에서 선언 경로 {candidate} 의 추적 여부를 관측하지 못했다 (git status rc={rc})")
        return bool(out.strip("\0").strip())

    @staticmethod
    def _is_separated_code_tree(home, code_tree: Path) -> bool:
        """이 코드 트리가 PM 홈과 분리된 트리인가 — 판정은 `트리 != PM 홈` 하나다.

        "PM 홈 하위인가" 같은 위치 제약은 두지 않는다: 슬롯 worktree 가 심링크거나 다른
        마운트에 있으면 그 제약이 참이 아니고, 그러면 diff 측정은 코드 트리를 보는데 stage 만
        PM 홈으로 내려앉아 **한 값의 두 소비자가 갈린다**. 해소 자체가 이미 명시 Home 을
        앵커로 하므로(`_regression_cwd` 가 `repo_root` 를 forward) 트리가 PM 홈과 다르다는 것은
        곧 분리 형상이라는 뜻이다. 경로 해소 실패는 False(fail-soft·기존 단일 계획 유지)."""
        try:
            return code_tree.resolve() != Path(home.root).resolve()
        except OSError:
            return False

    def _default_status_entries(self) -> tuple[tuple[str, str], ...]:
        """워킹트리 상태 `((XY, 경로), …)` (loud 보고 입력) — 실 해소.

        NUL 파서는 `finish_git.status_entries` 가 infra.git 의 공용 파서로 공유한다(비-ASCII 경로 실경로화).

        조회 실패는 `strict` 로 올린다 — 잔여 판정·보고(`_dirty_split`)와 종결의 선행 조건
        관측(`_dirty_paths`)은 그 실패를 clean 과 구별해 정지한다.
        """
        return finish_git.status_entries(self._run_git_stdout_fn,
                                         strict=True)

    def _default_status_entries_at(self, cwd: Path) -> tuple[tuple[str, str], ...]:
        """`cwd` 트리의 워킹트리 상태 — 실패 처리는 `_default_status_entries` 와 같다."""
        return finish_git.status_entries(lambda args: self._run_git_stdout_at_fn(cwd, args),
                                         strict=True)

    def _stage_plans(self, ticket_id: str) -> tuple[finish_rules.RepoStagePlan, ...]:
        """실제 stage 와 dry-run 이 공유하는 repo별 계획 단일 진실.

        분기 축은 task 여부가 아니라 **해소된 코드 트리가 PM 홈인가**다 — 코드 트리 소비자
        (diff 측정·[4/5] stage·PM-direct 재검)는 전부 `_code_tree()` 하나를 본다. 트리가 PM 홈
        자신이면(임베디드) 기존 단일 계획이고, 분리 형상(task 작업공간 또는 해소된 슬롯
        worktree)이면 PM 홈에 log/board 산출물, 코드 트리에 코드 touches 를 각각 둔다. 축을
        task 유무로 두면 슬롯 해소로 온 코드 트리에서 PM 홈의 동명 사본이 stage 된다.

        touches 는 **repo 별 몫으로 갈라** 각 계획에 넘긴다(`_split_touches_by_tree`) — 홈에만
        사는 touches(wiki·결정 기록·domain)를 코드 계획에만 두면 어느 repo 에서도 stage 되지
        않는다. [5/5] 커밋 안내도 이 계획을 그대로 따라 repo 별로 나온다.
        """
        code_tree = self._code_tree()
        if not self._is_separated_code_tree(self._home, code_tree):
            return (finish_rules.RepoStagePlan("PM 홈", self._home.root, self._stage_scope_fn(ticket_id)),)
        code_touches, home_touches = self._split_touches_by_tree(ticket_id, code_tree)
        home_scope = finish_context.stage_scope(
            self._home, ticket_id, self._log_file, self._run_git_stdout_fn,
            include_touches=bool(home_touches), touches=home_touches,
        )
        label = ("task worktree touches" if self._task_workspace is not None
                 else "slot worktree touches")
        return (
            finish_rules.RepoStagePlan("PM 홈 산출물", self._home.root, home_scope),
            finish_rules.RepoStagePlan(label, code_tree,
                          self._code_stage_scope(ticket_id, code_tree, code_touches)),
        )

    # ── 메인 흐름 ────────────────────────────────────────────────────

    def _warn_pm_direct_conditions(self, ticket_id: str) -> None:
        """PM-direct a·b를 재검하되 finish 흐름/반환 코드는 막지 않는다."""
        frontmatter = finish_store._ticket_frontmatter_snapshot(self._home, ticket_id)
        if frontmatter.get("tier") != "pm-direct":
            return
        touches = finish_rules._clean_touches(frontmatter.get("touches"))
        measured_touches = ([] if not touches else
                            self._normalize_measured_touches(touches))
        if measured_touches is None:
            print(
                "  ⚠ PM-direct 재검 skip — touches 좌표를 컨텐츠 worktree로 "
                "정규화하지 못했습니다. raw PM-home 경로로 판정하지 않습니다.",
                file=sys.stderr,
            )
            return
        try:
            code_tree = self._code_tree()
            expanded = tier_signals.expand_owned_touch_files(code_tree, measured_touches)
            entries = (self._status_entries_at_fn(code_tree)
                       if self._is_separated_code_tree(self._home, code_tree)
                       else self._status_entries_fn())
            changed_paths = tuple(
                path for _xy, path in entries
                if any(finish_rules._touch_claims_path(touch, path) for touch in measured_touches)
            )
        except Exception as exc:  # noqa: BLE001 — advisory 재검은 never-block.
            detail = " ".join(str(exc).split()) or type(exc).__name__
            print(f"  ⚠ PM-direct 재검 skip — git 변경 경로를 읽지 못했습니다 ({detail}).",
                  file=sys.stderr)
            return
        for warning in finish_rules.pm_direct_finish_warnings(
                frontmatter.get("tier"), touches, changed_paths,
                touched_file_count=len(expanded.paths),
                unresolved_directories=expanded.unresolved_directories):
            print(f"  ⚠ {warning}", file=sys.stderr)

    def run(
        self,
        ticket_id: str,
        section: str | None,
        dry_run: bool,
        skip_pytest: bool = False,
        task: str | None = None,
        board_identity_args: Sequence[str] = (),
        close: bool = False,
        cluster_close: str | None = None,
    ) -> int:
        """ticket_id 완료 기록 전체 흐름을 실행한다.

        반환: 0=성공, 1=실패 (중단).

        `section` 은 후방호환용으로 받기만 하고 무시한다 — status.md 합계표 섹션 행은
        제거됐다(judgment-only·테스트 수는 박제 안 함).

        `board_identity_args` = 이 실행의 정체성(`--task <task>`)을 board 하위 호출에 그대로
        넘기는 인자다. board 의 complete 가 `claimed_by` 와 실행 task 를 대조하므로, 이 forward 가
        없으면 ticket_finish 경유 완료가 "남의 티켓" 으로 거부된다.

        `skip_pytest`(--no-pytest) 는 [1/5] 회귀 단계를 건너뛴다 — 측정은 PM 이 /pm-qa
        등으로 별도. board complete 는 `--tests-pass` 를 유지한다(pm_handoff `--no-pytest` 동형·
        회귀 red 아님·skip 로 진행). **회귀 실행만** 건너뛴다 — 코드 트리 해소·diff 측정·
        stage 는 그대로다(`_code_tree`).

        `close` 는 이 실행이 종결 파이프라인 안에 있다는 뜻이다 — [1/5]~[4/5]는 그대로이고
        [5/5]의 손작업 안내만 바뀐다(커밋·재배치·머지·board 기록을 종결이 실행하므로 그
        안내가 남으면 이미 기계가 한 일을 사람에게 시킨다). 판정·부작용은 같다.

        `cluster_close`는 ``ClusterCloser``가 넘기는 exact ledger path-binding이다. 인증
        capability가 아니며 public ``board complete``가 묶음 멤버를 반쪽 완료하지 못하게 한다.
        """
        del section  # status 합계표 제거로 더 이상 쓰지 않음(후방호환 수용만).
        print(
            f"[ticket_finish] {ticket_id} 완료 기록 시작 "
            f"(dry_run={dry_run}, skip_pytest={skip_pytest})"
        )

        # PM 판정은 권위를 유지한다. 이 재검은 경고만 보이고 이후 rc를 바꾸지 않는다.
        self._warn_pm_direct_conditions(ticket_id)

        # ── 0. 진입 게이트(preflight) — 사설 참조 · DoD 기록 · 코드 트리 잔여 ·
        # 자기 축 회귀 ────────────────────────────────────
        # 넷 다 회귀보다 **앞**이고, 무엇보다 [2/5] log 스켈레톤 append 보다 앞이다: 여기서
        # 막힐 실행은 어떤 부작용(회귀 실행·log append·board·git)도 내지 않아야 한다. 사설
        # 참조가 첫 자리다 — 측정 폭(touches ∩ 출하 표면)을 diff 축에서 재는 판정이고, 그
        # 결과가 확정 차단이면 뒤의 DoD·잔여 판정을 물을 이유가 없다. DoD 판정이 [3/5]
        # `board.py complete` 안에만 있던 동안에는 차단마다 stray 스켈레톤이 남고 재실행이
        # 그것을 중복 append 했다(실측) — 순서가 곧 결함이었다. 잔여 판정(코드 트리 dirty ⊄
        # 선언 스코프)도 같은 이유로 여기 있다 — [4/5] stage 뒤라면 board complete 가 이미
        # 기록된 뒤에야 막혀 되돌릴 수 없다. 자기 축 회귀는 **비용순으로 마지막**이다 — 앞
        # 셋은 서브프로세스 0~2회, 이건 작업 트리가 red 일 때만 pytest 를 최대 2번 돈다
        # (green 이면 0회).
        for preflight in (self._private_ref_block_fn, self._dod_block_fn,
                          self._residual_block_fn, self._self_axis_block_fn):
            block = preflight(ticket_id)
            if block:
                print(f"\n[중단] {block}", file=sys.stderr)
                return 1

        # ── 1. 회귀 실행 ──────────────────────────────────────────────
        # dry-run 도 pytest 를 실제 실행한다 — "부작용 없음"이지 "빠름"이 아니다.
        # 파일·board·git 편집만 생략하므로 pytest 실행은 항상 수행. (--no-pytest 는 예외 — 측정 skip.)
        print("\n[1/5] 회귀 실행 중...")
        if skip_pytest:
            # --no-pytest — 회귀 측정은 PM 이 별도(/pm-qa 등). board complete 는 --tests-pass 유지
            # (pm_handoff --no-pytest 동형·red 아님·skip 로 진행). 측정 total 은 log 스켈레톤에서 "?".
            print(
                "  [--no-pytest] 회귀 측정 skip — 측정은 별도("
                f"{lease_store._runtime_skill_entry('pm-qa')} 등). board complete 는 --tests-pass 유지."
            )
            new_total: int | str = "?"
        else:
            # 게이트 종류(pytest 스위트 / 해소된 임의 명령)를 먼저 해소한다 — green 판정
            # 기준과 안내 문구가 여기서 갈린다(`handoff_text._gate_is_pytest`).
            gate_cmd = finish_context._resolve_per_repo_test_cmd(self._home)
            if dry_run:
                print(
                    f"  [dry-run] {handoff_text._gate_label(gate_cmd)} 실행 중 "
                    "(파일·board·git 편집만 생략)..."
                )
            returncode, output = self._run_pytest_fn()
            print(output.rstrip())

            if not handoff_text._regression_is_green(output, returncode, gate_cmd):
                print(
                    "\n[중단] 회귀 red — log/current.md·board·git 어떤 것도 건드리지 않는다.",
                    file=sys.stderr,
                )
                if handoff_text._gate_is_pytest(gate_cmd):
                    print(
                        "원인: pytest 가 실패를 보고했거나 출력 파싱 실패.",
                        file=sys.stderr,
                    )
                else:
                    print(
                        f"원인: 회귀 명령이 실패했다 — `{gate_cmd}` (exit {returncode}).",
                        file=sys.stderr,
                    )
                return 1

            if handoff_text._gate_is_pytest(gate_cmd):
                parsed = finish_rules.parse_pytest_output(output)
                if parsed is None:
                    print(
                        "\n[중단] pytest 출력 파싱 실패 — passed 수를 읽지 못했다.",
                        file=sys.stderr,
                    )
                    return 1

                new_total, deselected = parsed
                print(f"\n  ✓ green: passed={new_total}, deselected={deselected}")
            else:
                # 비-pytest 게이트 — 테스트 수 개념이 없으므로 측정하지 않는다("?" ·
                # `--no-pytest` 경로와 동형). green 근거는 exit 0 이다.
                new_total = "?"
                print(f"\n  ✓ green: `{gate_cmd}` (exit 0) — 테스트 수 미측정")

        # status.md 는 더 이상 갱신하지 않는다.
        # 테스트 수는 위 pytest 실측이 단일 진실·history 는 아래 log skeleton 으로 남는다.

        # ── 2. log/current.md 스켈레톤 append ────────────────────────────────
        print("\n[2/5] log/current.md 스켈레톤 append...")
        board_before = self._board_count_fn()
        board_after = board_before + 1  # board complete 후 +1

        title = self._ticket_title_fn(ticket_id)
        if not title:
            title = f"<{ticket_id} 제목을 읽지 못했습니다>"

        skeleton = build_log_skeleton(
            ticket_id=ticket_id,
            title=title,
            new_total=new_total,
            board_before=board_before,
            board_after=board_after,
            task=task,
        )

        log_has_entry = self._log_has_entry(ticket_id)
        if log_has_entry:
            # 재실행이 곧 재개다 — 뒤 단계가 실패해 이 자리를 다시 지날 때 관측 없이 append
            # 하면 같은 스켈레톤이 중복으로 쌓인다.
            prefix = "[dry-run] " if dry_run else ""
            print(f"  {prefix}{ticket_id} 스켈레톤 이미 있음 — append 건너뜀")
        elif dry_run:
            print("  [dry-run] log/current.md 에 append 할 스켈레톤:")
            print("  " + skeleton.replace("\n", "\n  "))
        else:
            log_store.append_log(self._log_file, "\n" + skeleton)
            print(f"  ✓ log/current.md 스켈레톤 append ({ticket_id})")

        # ── 3. board.py complete ──────────────────────────────────────
        print("\n[3/5] board.py complete...")
        board_complete_argv = ["complete", ticket_id, "--tests-pass"]
        if cluster_close is not None:
            board_complete_argv.extend(["--cluster-close", cluster_close])
        board_complete_argv.extend(board_identity_args)
        if dry_run:
            print(f"  [dry-run] board.py {' '.join(board_complete_argv)}")
        else:
            board_rc, board_out = self._run_board_fn(board_complete_argv)
            print(f"  {board_out.rstrip()}")
            if board_rc != 0:
                print(
                    f"\n[중단] board.py complete 실패 (rc={board_rc}). "
                    "log/current.md 는 이미 편집됐다.",
                    file=sys.stderr,
                )
                return 1
            print(f"  ✓ board: {ticket_id} → done")

        # ── 4. git stage (선언 경로 스코프) ────────────────────
        # blanket `add -A` 가 아니다 — 이 티켓이 선언한 경로(`touches` ∪ *이 실행이 쓴* 산출물)만
        # stage 한다. 코드 트리의 스코프 밖 잔여는 preflight `_residual_block_fn` 이 이미 차단했다
        # (여기 도달했다는 것 자체가 코드 트리는 깨끗하다는 뜻). PM 홈 dev-state(판정 인구 밖) 등
        # 남는 잔여는 아래 loud 보고로 계속 가시화한다(비차단 — 내가 정리할 수 없는 남의 파일).
        print("\n[4/5] git stage (선언 경로 스코프)...")
        plans = self._stage_plans(ticket_id)
        multi_repo = len(plans) > 1
        for plan in plans:
            scope, scope_error = plan.scope
            if multi_repo:
                print(f"  [{plan.label}] cwd={plan.cwd}")
            if scope_error:
                # 판정기가 죽은 경우 — 조용한 no-op 금지(stage 0 이 '정상 완료'로 보인다).
                label = f"[{plan.label}] " if multi_repo else ""
                print(f"\n  ⚠ {label}stage 스코프를 산출하지 못했다 — {scope_error}. "
                      "이번 repo에서는 아무것도 stage 하지 않는다. 아래 잔여 목록을 보고 "
                      "PM 이 직접 `git add <경로>` 하라.", file=sys.stderr)
            # add 는 스코프 중 작업 트리에 있는 파일만 받는다(신규·수정). 선언한 삭제는 add 하지 않고
            # 커밋이 경로로 기록한다 — 미리 `git rm` 한 경로는 add 가 rc 128 로 거부하고, add 로 지운
            # 삭제는 index 에서 빠져 커밋 단계가 다시 계산한 pathspec 에서 사라진다.
            additions = [rel for rel in scope if os.path.lexists(plan.cwd / rel)]
            deletions = [rel for rel in scope if rel not in additions]
            label = f"[{plan.label}] " if multi_repo else ""
            if dry_run:
                if additions or not deletions:
                    preview = " ".join(additions) if additions else "(선언 경로 없음)"
                    cwd = f"cwd={plan.cwd} " if multi_repo else ""
                    print(f"  [dry-run] {cwd}git add -A -- {preview} (실제 실행 생략)")
            elif additions:
                if plan.cwd == self._home.root:
                    git_rc, git_out = self._run_git_fn(["add", "-A", "--", *additions])
                else:
                    git_rc, git_out = self._run_git_at_fn(plan.cwd, ["add", "-A", "--", *additions])
                if git_rc != 0:
                    print(
                        f"\n[중단] {label}git add 실패 (rc={git_rc}): {git_out.rstrip()}",
                        file=sys.stderr,
                    )
                    return 1
                print(f"  ✓ {label}git add — 선언 경로 {len(additions)}개만 stage (commit 은 아직 안 했다):")
                for rel in additions:
                    print(f"      {rel}")
            elif not scope_error and not deletions:
                print(f"  {label}stage 할 선언 경로 없음")
            if deletions:
                print(f"  {label}선언 삭제 {len(deletions)}개 — stage 하지 않고 커밋이 경로로 기록한다:")
                for rel in deletions:
                    print(f"      {rel}")
            if not dry_run:
                self._report_dirty_after_stage(scope, cwd=plan.cwd,
                                               label=plan.label if multi_repo else "")

        # ── 5. 잔여 PM 작업 출력 ─────────────────────────────────────
        print("\n[5/5] PM 이 손으로 할 잔여 작업:")
        print("  log/current.md 서술 불릿 채우기 — <!-- PM: 무엇을·왜 서술 --> 를 실제 내용으로 교체")
        print("  status.md 모듈 행(상태 + 비고) — 변경된 모듈 행 판정을 architect/PM 이 직접 갱신 (테스트 수는 박제 안 함)")
        # 단일 repo 출력은 기존 커밋 안내 문구를 byte-compatible하게 보존한다. 두 repo 계획일 때만
        # cwd별 안내로 확장해 PM 홈 성공이 task worktree 누락을 가리지 못하게 한다.
        if close:
            print("  git commit — 종결 파이프라인이 실행한다(커밋·재배치·머지·"
                  "board 기록까지 · 손 git 없음)")
        elif not multi_repo:
            print("  git commit — **경로를 명시**하라: "
                  "`git commit -m \"<메시지>\" -- <위 [4/5] 가 stage 한 경로들>` "
                  "(메시지는 PM 이 작성 · Co-Authored-By: Claude 트레일러 포함)")
        else:
            print("  git commit — **repo별 cwd와 경로를 명시**하라 "
                  "(메시지는 PM 이 작성 · Co-Authored-By: Claude 트레일러 포함):")
            for plan in plans:
                scope, scope_error = plan.scope
                if scope_error or not scope:
                    reason = scope_error or "stage 할 선언 경로 없음"
                    print(f"      [{plan.label}] cwd={plan.cwd}: commit 하지 말 것 ({reason})")
                    continue
                pathspec = " ".join(scope)
                print(f"      [{plan.label}] cwd={plan.cwd}: "
                      f"`git commit -m \"<메시지>\" -- {pathspec}`")

        # ── soft 알림: 영향받는 domain 페이지 (비차단) ──────
        # 정보일 뿐 게이트가 아니다 — 완료 흐름·rc 를 막지 않는다(예외도 삼킨다).
        # 페이지 적재·매칭 실패로 None을 반환하면 알림을 건너뛴다.
        self._notify_affected_domain(ticket_id)

        if dry_run:
            print("\n[dry-run] 완료 — 실제 편집·board·git 는 실행하지 않았다.")
        else:
            # 잔여 건수를 마지막 줄에 한 번 더 — [4/5] 보고가 이후 출력에 묻히지 않게(loud).
            staged_out, unstaged = self._dirty_summary
            tail = ""
            if unstaged or staged_out:
                tail = (f" ⚠ 미스테이지 잔여 {unstaged}건 · 스코프 밖 staged {staged_out}건 "
                        "— 위 [4/5] 목록 확인.")
            print(f"\n[완료] {ticket_id} 기록 완료.{tail}")

        return 0

    # 잔여 보고 최대 줄 수 — 넘으면 "… 외 N건" 으로 접는다(수백 줄 홍수 방지).
    _RESIDUAL_DIRTY_PREVIEW_LINES = 20

    def _print_dirty_block(self, header: str, lines: Sequence[str]) -> None:
        """보고 블록 1개 출력 (헤더 + 최대 N줄 + 접기) — 두 방향 보고가 같은 모양을 쓴다."""
        print(header)
        for line in lines[:self._RESIDUAL_DIRTY_PREVIEW_LINES]:
            print(f"      {line}")
        hidden = len(lines) - self._RESIDUAL_DIRTY_PREVIEW_LINES
        if hidden > 0:
            print(f"      … 외 {hidden}건")

    def _dirty_split(self, scope: Sequence[str], *, cwd: Path | None = None) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """현재 상태를 (스코프 밖 staged, 미스테이지 잔여)로 가른다 — 조회 실패는 관측 실패로 멈춘다.

        빈 목록으로 접으면 잔여 판정은 잔여 없음으로 통과하고 stage 뒤 보고는 "잔여 변경 없음" 을 낸다."""
        if cwd is None:
            cwd = self._home.root
        return finish_git.split_dirty(self._observed_status_entries(cwd), scope)

    # stage **전** 보고는 두지 않는다 — 이 도구의 `add` 는 스코프 경로만 올리므로 사전에 staged
    # 돼 있던 스코프 밖 변경은 stage 후에도 그대로 남고, 아래 사후 보고가 같은 목록을 낸다.
    # 별도 사전 보고는 같은 경고를 두 번 내면서 고유 판정이 없었다(teeth 0·reviewer 실측).

    def _report_dirty_after_stage(self, scope: Sequence[str], *, cwd: Path | None = None,
                                  label: str = "") -> None:
        """stage 후 상태를 양방향으로 **눈에 띄게** 보고한다 (loud·잔여는 비차단·관측 실패는 정지).

        두 실패 방향을 함께 본다 — 1) **미스테이지 잔여**(under-stage: 스코프가 못 덮은 변경 —
        내 작업 누락이면 `touches` 를 보강해 다시 stage, 남의 WIP 면 그대로 둔다) 2)
        **스코프 밖 staged**(누출: PM 커밋에 실린다). 좁히기가 만드는 실패를 *조용한 유출* 과
        바꾸지 않는 것이 요점이라, 어느 쪽도 침묵하지 않는다. 결과 건수는 `[완료]` 줄에서
        한 번 더 재고지한다(보고가 뒤 출력에 묻히지 않게).
        """
        staged_out, unstaged = self._dirty_split(scope, cwd=cwd)
        old_staged, old_unstaged = self._dirty_summary
        self._dirty_summary = (old_staged + len(staged_out), old_unstaged + len(unstaged))
        prefix = f"[{label}] " if label else ""
        if not staged_out and not unstaged:
            print(f"  ✓ {prefix}스코프 밖 잔여 변경 없음 (staged·미스테이지 모두)")
            return
        if unstaged:
            self._print_dirty_block(
                f"\n  ⚠ {prefix}미스테이지 잔여 {len(unstaged)}건 — **이 커밋에 안 실린다**. 내 작업 "
                "누락이면 ticket `touches` 를 보강해 다시 stage 하라(남의 WIP 면 그대로 둔다):",
                unstaged)
        if staged_out:
            self._print_dirty_block(
                f"\n  ⚠ {prefix}스코프 밖 staged {len(staged_out)}건 — 내 변경이 아닌데 **PM 커밋에 "
                "실린다**. 빼려면 `git restore --staged <경로>`:",
                staged_out)

    def _notify_affected_domain(self, ticket_id: str) -> None:
        """이 ticket 이 건드린 영역의 domain 페이지를 soft 알림으로 출력한다 (비차단).

        영향 페이지가 stale(covers 코드가 page updated 후 커밋)이면 그 줄 앞에
        `⚠` 를 단다 — fresh(False)/unknown(None)은 무표시. 도그푸딩/multi-PM 어디서든 일반 domain
        부재·예외는 조용히 삼켜 완료를 막지 않는다. 영향 0 이면 한 줄
        안내만 낸다. dry-run/실행 동일(정보 출력만). 일반 stale 예외/unknown 은 비차단.
        """
        print("\n[domain] 영향받는 domain 페이지 (soft·비차단):")
        try:
            affected = self._affected_domain_fn(ticket_id)
        except Exception:  # noqa: BLE001 — 일반 soft 알림 실패만 완료를 막지 않는다.
            affected = None
        if affected is None:
            print("  (domain 레이어 없음 — skip)")
        elif not affected:
            print("  (영향 domain 페이지 없음)")
        else:
            # 각 영향 페이지: stale(True) 줄 앞에 ⚠ — fresh/unknown 은 무표시.
            labels = [f"⚠ {title}" if stale is True else title for title, stale in affected]
            joined = ", ".join(labels)
            print(f"  📝 이 ticket 이 건드린 영역 domain 페이지: [{joined}] — 갱신 확인(soft)")
