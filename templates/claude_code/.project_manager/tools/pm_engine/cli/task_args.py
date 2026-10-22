"""실행 명령의 `--task`·`--repo`·`--slot` 정체성 검증과 task 작업공간 해소 (층: cli).

잘못된 입력은 `[중단]` 문구로 종료한다.
"""
from __future__ import annotations

import argparse
import sys

from pm_engine.domain import identity_args
from pm_engine.infra import areas_store, conf_store, git, lease_store, pm_paths, python_env

from typing import Any

from pm_engine.app import ticket_access


# 실행 소비자(claim·new·cluster new·regression·livegate record·rounds resolve)에 `--task` 없이
# `--repo`/`--slot` 이 왔을 때의 중단 문구. {retry} 는 입력값을 채운 다시 칠 인자다.
SLOT_SELECTOR_WITHOUT_TASK = (
    "[중단] `--repo`/`--slot` 은 task 가 보유한 폴더를 고르는 위치 인자다(정체성 아님). "
    "`{retry}` 로 다시 실행하라."
)


def _slot_selector_retry(identity: "identity_args.Identity") -> str:
    """위치 인자만 준 호출의 다시 칠 인자 — 입력한 repo·slot 을 채운다."""
    retry = f"--task <task> --repo {identity.repo}"
    if identity.slot is not None:
        retry += f" --slot {identity.slot}"
    return retry


def _execution_actor_session_override(home, args: argparse.Namespace) -> str | None:
    """실행 소비자의 정체성 — 검증한 `--task` 값, 없으면 None.

    `--repo`/`--slot` 은 task 가 보유한 폴더를 고르는 위치 인자라 `--task` 와 함께일 때만 받는다.
    `--task` 없이 오면 어떤 쓰기·git 호출보다 먼저 `SLOT_SELECTOR_WITHOUT_TASK` 로 멈춘다.
    """
    try:
        identity = identity_args.parse_identity(args)
    except ValueError as exc:
        sys.exit(f"[중단] {exc}")
    if identity.task:
        ticket_access._validate_actor_task_or_exit(home, identity.task)
        return identity.task
    if identity.kind != "none":
        sys.exit(SLOT_SELECTOR_WITHOUT_TASK.format(retry=_slot_selector_retry(identity)))
    return None


def _resolve_task_workspace_cwd(
    home, args: argparse.Namespace, *, require_test_cmd: bool = False,
) -> tuple[str, str | None] | None:
    """허용된 task 실행 소비의 leased work 트리와 해당 행의 test_cmd를 해소한다."""
    if not getattr(args, "task", None):
        return None
    if getattr(args, "cwd", None) is not None:
        sys.exit("[중단] --task 모드에서는 --cwd를 명시할 수 없다, 외부 경로 override를 허용하지 않는다. 기존 repo/slot 좌표로 선택하라.")
    _execution_actor_session_override(home, args)
    try:
        ws = lease_store.resolve_task_workspace(identity_args.parse_identity(args), pm_paths.leases_file(home))
    except (ValueError, identity_args.WorkspaceResolutionError) as exc:
        sys.exit(f"[중단] {exc}")
    if ws.readonly:
        sys.exit("[중단] readonly 슬롯은 task 실행 쓰기 대상이 아니다, 참조는 기존 --cwd 핀을 사용하라.")
    tree = home.root / ws.slot
    if not tree.is_dir() or not (tree / ".git").exists():
        sys.exit(f"[중단] task 코드 트리의 경로 또는 git 트리가 없다: {tree}")
    if not git._git_head_at(str(tree)):
        sys.exit(f"[중단] task 코드 트리의 git HEAD를 읽지 못했다: {tree}")
    if not git._cluster_current_branch(str(tree)):
        sys.exit(f"[중단] task 코드 트리의 브랜치를 읽지 못했다: {tree}")
    command = ws.test_cmd
    if require_test_cmd:
        command = getattr(args, "cmd", None)
        if command is None:
            command = ws.test_cmd
            if not command:
                _header, rows = areas_store._parse_areas(home)
                matching = [row for row in rows if row.get("repo") == ws.repo]
                if len(matching) > 1:
                    sys.exit(f"[중단] 선택 repo {ws.repo!r}의 test_cmd 장부가 모호하다")
                command = (matching[0].get("test_cmd") if matching else None)
                command = command or conf_store.local_config(home).get("test.cmd") or python_env.default_pytest_cmd()
        if not isinstance(command, str) or not command.strip():
            sys.exit(f"[중단] task 선택 작업공간의 test_cmd를 해소할 수 없다: {tree}")
    return str(tree), command


# 귀속 쓰기(new·claim·migrate-identity·unclaim --takeover)에 `--task` 가 없을 때의 중단 문구.
# 정체성은 task 하나라 다른 값(환경변수·단일 대여)으로 대신하지 않는다.
TASK_REQUIRED = (
    "[중단] {action}: 귀속 쓰기는 `--task <task>` 가 필요하다 — 정체성은 task 하나다. "
    "`{command} --task <task>` 로 다시 실행하라(task 의 폴더가 2개 이상이면 "
    "`--repo <repo> --slot <N>` 을 함께 준다). task 폴더는 "
    "`pm-config alloc <repo> --task <task>` 로 빌린다."
)


def _actor_session_override(home, args: argparse.Namespace) -> str | None:
    """비실행 명령(소유 대조·귀속 쓰기)의 정체성 — 검증한 `--task` 값, 없으면 None.

    이 명령들의 parser 에는 `--repo`/`--slot` 이 없다. 무검증 task 명이 created_by/claimed_by/
    lease-session 으로 영속되지 않도록 여기서 한 번 검증한다(실행 소비자는
    `_execution_actor_session_override` 가 같은 검증을 부른다).
    """
    task = getattr(args, "task", None)
    if not task:
        return None
    ticket_access._validate_actor_task_or_exit(home, task)
    return task


def _frontmatter_with_claimed_rev(
    fm: dict[str, Any], claimed_rev: str,
) -> dict[str, Any]:
    """`claimed_at` 바로 뒤에 `claimed_rev` 를 둔 frontmatter (앵커 키가 없으면 끝에 붙인다).

    frontmatter 는 삽입 순서대로 직렬화되므로(`sort_keys=False`) 자리를 정하려면 재조립해야
    한다. claim 3종(주체·시각·rev)이 붙어 있어야 티켓을 연 사람이 한 자리에서 읽는다. 기존
    값이 있으면 그 자리를 옮겨 덮어써 중복 키가 생기지 않는다."""
    if "claimed_at" not in fm:
        return {**fm, "claimed_rev": claimed_rev}
    rebuilt: dict[str, Any] = {}
    for key, value in fm.items():
        if key == "claimed_rev":
            continue
        rebuilt[key] = value
        if key == "claimed_at":
            rebuilt["claimed_rev"] = claimed_rev
    return rebuilt


def _claim_code_tree(home, args: argparse.Namespace) -> str | None:
    """claim·발행·묶음 선언·확인 실행이 읽을 코드 트리 — task 가 보유한 작업공간.

    실행 명령 판정 함수를 먼저 거친다 — `--task` 없는 `--repo`/`--slot` 은 여기서 멈추고, 인자가
    전무하면 None 이다(호출부가 각자의 문구로 멈춘다). task 입력은 leased work·경로·HEAD·브랜치를
    선검사하고 실패하면 중단한다.
    """
    if _execution_actor_session_override(home, args) is None:
        return None
    return _resolve_task_workspace_cwd(home, args)[0]


def _claim_head_rev(home, args: argparse.Namespace) -> str:
    """claim 시점 HEAD — task 작업공간의 HEAD(읽지 못하면 중단)."""
    tree = _claim_code_tree(home, args)
    rev = git._git_head_at(tree) if tree else ""
    if not rev:
        sys.exit(f"[중단] task claim 앵커 HEAD를 읽지 못했다: {tree}")
    return rev
