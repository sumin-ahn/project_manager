"""실행 명령의 `--task`·`--repo`·`--slot` 정체성 검증과 task 작업공간 해소 (층: cli).

잘못된 입력은 `[중단]` 문구로 종료한다.
"""
from __future__ import annotations

import argparse
import sys

from pm_engine.domain import identity_args
from pm_engine.infra import areas_store, conf_store, git, lease_store, pm_paths, python_env


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
        _validate_actor_task_or_exit(home, identity.task)
        return identity.task
    if identity.kind != "none":
        sys.exit(SLOT_SELECTOR_WITHOUT_TASK.format(retry=_slot_selector_retry(identity)))
    return None


def _validate_actor_task_or_exit(home, task: str) -> None:
    """task 명 공유 validator 소비 — 불법이면 fail-loud (깔때기 단일 지점).

    정체성 깔때기(`_actor_session_override`·귀속)와 실행-위치 해소(`_resolve_task_workspace_cwd`)가
    **같은 한 지점**을 소비해, 무검증 task 명이 created_by/claimed_by/lease-session 으로 영속되거나
    실행-위치로 쓰이기 **이전** 부작용 0 로 거부한다. 공유 validator = `identity_args.validate_task_name`
    (worktree_pool 엔진 validator 와 동형·로직 중복 0). 예약패턴(`<repo>_<N>`) 판정용 registered_repos
    는 areas 에서 fail-soft 해소(파싱 실패 시 char/traversal 검증만).
    """
    try:
        registered = areas_store.registered_repos(home)
    except Exception:  # noqa: BLE001 — areas 파싱 실패는 예약패턴 검증만 완화(char/traversal 유지).
        registered = None
    try:
        identity_args.validate_task_name(task, registered)
    except identity_args.InvalidTaskName as e:
        sys.exit(
            f"[중단] {e} — `--task` 는 안전한 단일 이름이어야 하고 슬롯 예약패턴"
            f"(`<repo>_<N>`)은 쓸 수 없다."
        )


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
