"""명령 분류 및 앵커 안내 (층: cli)."""
from __future__ import annotations

from pathlib import Path
import argparse
from pm_engine.domain import board_command_rules
from pm_engine.infra import console
from pm_engine.infra import pm_paths
import sys
from pm_engine.app import board_dashboard
from pm_engine.app import ticket_lint


def _resolved_subcommand(args: argparse.Namespace) -> str:
    """argparse Namespace 에서 실행된 subcommand 를 점표기로 해소한다(서브그룹 포함).

    top-level dest=`cmd`, 서브그룹 dest 는 아래 표 — dispatch 게이트가 mutation 분류를 조회할
    키를 만든다. (미지정/불명 → "".) 새 서브그룹은 표에만 등재하면 되고, 등재를 잊으면 그
    그룹의 leaf 가 분류에서 통째로 빠진다 — 메타 가드 테스트가 그 클래스를 잡는다.
    """
    cmd = getattr(args, '_board_subcommand', None) or getattr(args, 'cmd', None) or ''
    dest = board_command_rules._SUBCOMMAND_GROUP_DESTS.get(cmd)
    if dest is None:
        return cmd
    return f'{cmd} {getattr(args, dest, '') or ''}'.strip()

def _print_read_anchor(home, *, subcommand: str, pm_home: Path | None=None) -> None:
    """읽기 조회가 실제로 측정하는 repo 앵커와 역할을 stdout 첫 줄에 표시한다.

    앵커는 도구가 이미 사용하는 ``REPO`` 그대로다. 역할은 소유 PM 홈 유도가 다른 홈의
    worktree라고 확인한 경우만 ``worktree``로, 실 board 소유가 확인된 경우만 ``PM 홈``
    으로 표기한다. 어느 양성 증거도 없으면 역할을 단언하지 않는다. PM 홈 폴백이면 같은
    첫 줄에 실제 PM 입력 앵커와 read leaf별 입력 목록을 함께 표시한다.

    해소 실패는 이 줄의 입력이 아니다 — 실패한 실행은 dispatch 에 들어오지 못하고
    ``main()`` 이 `[중단] …` 으로 번역한다. 실패 사실로 ``worktree`` 라는 양성 역할을
    단언하지 않는다.
    """
    anchor = home.root
    if pm_home is not None:
        role = 'worktree'
    elif pm_paths._has_real_board(anchor / '.project_manager'):
        role = 'PM 홈'
    else:
        role = '역할 미상'
    line = f'repo 앵커: {anchor} ({role})'
    if pm_home is not None:
        inputs = board_command_rules._READ_PM_INPUTS_BY_SUBCOMMAND[subcommand]
        line += f' → PM 입력 앵커: {pm_home} (PM 홈 폴백: {inputs})'
    print(line, flush=True)

def _guard_worktree_misanchor(home, action: str) -> bool:
    """명시 Home이 PM 홈의 등록 worktree이면 안내 후 True를 반환한다.

    실행 시점의 Home을 사용하므로 코드 좌표를 import 시점에 고정하지 않는다."""
    anchor = home.root
    pm_home = pm_paths._pm_home_worktree_misanchor(home)
    if pm_home is None:
        return False
    print(f'[중단] `{action}` 을(를) worktree(코드 전용) 트리에서 실행했습니다 — board 상태는 PM 홈이 소유합니다. 이대로면 이 worktree 에 stray 티켓/log 를 잘못 만듭니다.\n  → PM 홈에서 실행하세요:  cd {pm_home}\n  (현재 앵커: {anchor})', file=sys.stderr)
    return True

def _write_machine_line(text: str) -> None:
    """기계 판독 한 줄(JSON 페이로드)을 콘솔 코덱 전환과 무관하게 UTF-8 로 내보낸다.

    사람 출력(``print``)은 PowerShell 캡처에서 콘솔 codepage(cp949 등)로 강등될 수 있고 그
    치환은 되돌릴 수 없다 — 다른 프로세스가 파싱하는 출력은 공용 seam 으로 UTF-8 bytes 를
    직접 쓴다(콘솔 코덱 전환과 독립).
    """
    console.write_machine_line(text)


def _report_engine_rev_skew_at_terminal(exc) -> int:
    """명시된 CLI 종료 경계에서 marked skew를 진단하고 실패 rc로 바꾼다."""
    print(
        f"[중단] 엔진 사본 불일치: {exc} — 먼저 pm-update로 엔진 전체를 "
        "동기화한 뒤 다시 실행하세요.",
        file=sys.stderr,
    )
    return 1


def cmd_refresh(home, _args: argparse.Namespace) -> int:
    board_dashboard.refresh_board(home)
    print(f"board refreshed: {pm_paths._rel_to_repo(home, pm_paths.wiki_dir(home) / "board.md")}")
    issues = ticket_lint.lint_tickets(home)
    if issues:
        print(f"⚠️  {len(issues)} lint issue(s) — run `board.py lint` for detail",
              file=sys.stderr)
    return 0
