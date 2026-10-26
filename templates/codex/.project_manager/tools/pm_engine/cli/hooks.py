"""하네스 어댑터 진입과 git 훅 판정 CLI (층: cli).

어댑터는 `adapter_home` 으로 설치 루트의 Home 을 만들고 git 판정 세 함수를 부른다.
`board.py git-anchor`·`git-grant`·`git-tool-input` 명령 처리도 이 모듈이 소유한다.
판정 규칙은 app.hooks·app.engine_guard·domain.git_policy 가 소유하고 여기서는 입력을 모아 넘긴다.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from pm_engine.app import engine_guard
from pm_engine.app import hooks as app_hooks
from pm_engine.domain import git_policy
from pm_engine.domain import home
from pm_engine.infra import console, fs, pm_paths


def adapter_home(root: str | os.PathLike[str]) -> home.Home:
    """어댑터가 설치된 루트(문자열 또는 경로)의 Home 을 만든다."""
    return pm_paths.home_at(Path(root))


def judge_tool_input(
    home: home.Home, session: str, cwd: str, tool_name: str, tool_input: object,
) -> dict[str, str]:
    """셸이 아닌 도구(파일 쓰기·편집·위임 등) 입력 판정 — 허용 위조, 그리고 슬롯 쓰기 주인.

    허용 기록 폴더 이름이 어느 값에든 있으면(파일 경로·patch 본문) 기록 파일 쓰기로, 줄 머리에
    허용 표식이 있으면(파일 내용·위임 프롬프트) 중첩 세션 프롬프트로 허용을 만들려는 시도로 본다.
    파일 쓰기·편집 도구의 대상이 `home.root`(훅이 설치된 PM 홈) 슬롯 안이면 이 세션 task 가 잡은
    슬롯일 때만 통과한다. 그리고 대상이 어디든, 이 세션 task 에 **열린 todo 가 없으면** 경고를 붙인다
    (`_no_open_todo_judgment`·막지는 않는다) — 무엇을 하는 중인지 장부에 없다는 사실을 화면에 올린다.
    거부가 이기고 경고는 거부가 없을 때만 나간다(같은 호출에 두 말을 겹치지 않는다).
    """
    for text in git_policy._tool_input_strings(tool_input):
        if git_policy.GIT_GRANT_DIR_NAME in text.replace("\\", "/") or git_policy.GIT_GRANT_LINE.search(text):
            judgment = git_policy._git_grant_forgery_judgment(
                f"도구 입력에 허용 기록 폴더(`{git_policy.GIT_GRANT_DIR_NAME}`) 또는 줄 머리 허용 표식"
                "(`git 허용:`)이 있음"
            )
            judgment["reason"] = git_policy._git_guard_stop_reason(
                [judgment.pop("case")], judgment["reason"], grants_available=False,
            )
            return judgment
    targets = app_hooks._write_tool_targets(cwd, tool_name, tool_input) if isinstance(tool_input, dict) else []
    # 파일을 바꾸는 호출에만 본다 — 읽기 도구는 대상이 없어 여기 들어오지 않는다.
    todo_warning = engine_guard._no_open_todo_judgment(home, session) if targets else None
    denies = [
        judgment for judgment in (app_hooks._slot_write_judgment(home.root, session, target) for target in targets)
        if judgment is not None and judgment["verdict"] == "deny"
    ]
    if denies:
        return {
            "verdict": "deny", "cwd_identity": "slot", "code": git_policy.GIT_DESTRUCTIVE_DENY_CODE,
            "reason": git_policy._git_guard_stop_reason(
                [item["case"] for item in denies], " | ".join(item["reason"] for item in denies),
                grants_available=False,
            ),
        }
    if todo_warning is not None:
        return todo_warning
    return {"verdict": "ok", "cwd_identity": "non-repo", "reason": "허용 위조 표식·남의 슬롯 쓰기 없음"}


def record_git_grants(home: home.Home, session: str, cwd: str, prompt: str) -> list[str]:
    """사용자 프롬프트의 `git 허용: <명령 원문>` 줄마다 허용 기록 1건을 `home` 에 남긴다.

    사용자 프롬프트 훅 전용이다 — 도구 호출 경로에서 부르지 않는다. 한 줄이라도 형식이 틀리면
    아무것도 기록하지 않고 `GitGrantShapeError` 를 올린다. 반환은 기록한 명령(정규화 표기)이다.
    """
    if not session:
        raise ValueError("세션 id 가 없어 허용을 세션에 묶을 수 없다")
    records = [
        app_hooks._git_grant_record(session, cwd, app_hooks._git_grant_argv(match.group(1)))
        for match in git_policy.GIT_GRANT_LINE.finditer(prompt)
    ]
    for record in records:
        path = app_hooks._git_grant_path(home.root, record)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}-{time.monotonic_ns()}")
        temporary.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
        fs.atomic_replace(temporary, path)
    return [shlex.join(record["argv"]) for record in records]


def judge_git_anchor_command(
    home: home.Home, session: str, cwd: str, command: str, *, grants: bool = False,
    runner: Any = subprocess.run,
) -> dict[str, str]:
    """셸 command의 모든 git/엔진/파일 삭제 호출을 중앙 파싱하고 가장 강한 판정을 반환한다.

    멈춤 거부(손 git 파괴 형태·슬롯/공용 저장소 파일 삭제·허용 위조·남의 슬롯 git 쓰기)는 첫 줄 멈춤
    지시와 경우별 다음 행동을 붙인다. ``home`` 은 훅이 설치된 PM 홈, ``session`` 은 호출한 하네스 세션
    ID 다 — 슬롯 쓰기 주인 판정에 쓰고, 거부가 아니면 `pm_bootstrap.py --task <task>` 호출의
    세션 기록을 남긴다(`_record_bootstrap_session_claims`). ``grants`` 는 이 하네스가 사용자 1회 허용을
    쓸 수 있을 때 준다 — 그러면 1회 허용으로 풀 수 있는 거부에서 ``home`` 에 남은 이 세션·정확히 같은
    명령의 허용 기록을 소비하고 통과시킨다. 없으면 그 거부는 사용자가 터미널에서 직접 실행하도록 안내한다.
    """
    root = home.root
    parsed = app_hooks._git_invocations_from_shell(cwd, command)
    invocations = parsed.invocations
    judgments: list[dict[str, str]] = []
    for call in invocations:
        judgment = engine_guard.judge_git_anchor(home, session, call.cwd, list(call.argv), runner=runner, cwd_certain=call.anchor_certain)
        if (
            not call.certain and judgment["verdict"] != "warn"
            and judgment.get("code") != git_policy.GIT_DESTRUCTIVE_DENY_CODE
        ):
            judgment = {
                **judgment,
                "verdict": "warn",
                "reason": f"조건/cwd를 정적으로 단일 증명할 수 없음 — 차단하지 않음; {judgment['reason']}",
            }
        judgments.append(judgment)
    for call in parsed.engine_invocations:
        judgment = engine_guard.judge_engine_invocation(call.cwd, list(call.argv), runner=runner)
        if not call.certain and judgment["verdict"] != "warn":
            judgment = {
                **judgment,
                "verdict": "warn",
                "reason": f"조건/cwd를 정적으로 단일 증명할 수 없음 — 차단하지 않음; {judgment['reason']}",
            }
        judgments.append(judgment)
    for call in parsed.removal_invocations:
        removal = engine_guard.judge_file_removal(
            call.cwd, call.argv, cwd_certain=call.anchor_certain, runner=runner,
        )
        if removal is not None:
            judgments.append(removal)
    forgery = git_policy._git_grant_forgery_in_command(command) if isinstance(command, str) else None
    if forgery is not None:
        judgments.append(forgery)
    if parsed.persistent_cd:
        judgments.append({
            "verdict": "warn", "cwd_identity": "non-repo",
            "reason": (
                "cd는 셸 cwd를 호출 뒤에도 잔존시킬 수 있음 — 대상을 절대경로로 지정하라; "
                "cd가 꼭 필요하면 이 호출 안에서만 사용하고, 다음 호출은 cwd를 가정하지 마라"
            ),
        })
    if parsed.uncertain:
        judgments.append({
            "verdict": "warn", "cwd_identity": "non-repo",
            "reason": f"{parsed.reason} — 차단하지 않음; cwd를 직접 확인",
        })
    if isinstance(command, str):
        judgments.extend(app_hooks._shell_slot_warnings(root, session, cwd, command))
    if not judgments:
        return {
            "verdict": "ok", "cwd_identity": "non-repo",
            "reason": "git mutation/엔진 호출 없음",
        }
    result = app_hooks._finish_git_guard_judgment(
        git_policy._strongest_git_guard_judgment(judgments), judgments, cwd=cwd, command=command,
        grant_root=root if grants else None, grant_session=session if grants else None,
    )
    if result["verdict"] != "deny":
        engine_guard._record_bootstrap_session_claims(session, parsed)
    return result


def cmd_git_anchor(home: home.Home, args: argparse.Namespace) -> int:
    """하네스 훅용 JSON 판정 표면. 한 줄 JSON만 출력한다.

    ``--grants`` 를 주면 이 엔진 루트의 사용자 1회 허용을 쓸 수 있는 하네스로 판정한다
    (정확히 같은 명령·같은 세션의 허용 기록이 있으면 소비한다).
    """
    judgment = judge_git_anchor_command(
        home, args.session, args.cwd, args.command, grants=args.grants,
    )
    console.write_machine_line(json.dumps(judgment, ensure_ascii=False))
    return 0


def cmd_git_grant(home: home.Home, args: argparse.Namespace) -> int:
    """사용자 프롬프트 훅 전용 — stdin 프롬프트의 `git 허용:` 줄을 이 엔진 루트의 허용 기록으로 남긴다."""
    prompt = sys.stdin.buffer.read().decode("utf-8")
    try:
        granted = record_git_grants(home, args.session, args.cwd, prompt)
    except ValueError as exc:
        sys.exit(f"[중단] {exc}")
    console.write_machine_line(json.dumps({"granted": granted}, ensure_ascii=False))
    return 0


def cmd_git_tool_input(home: home.Home, args: argparse.Namespace) -> int:
    """하네스 훅용 — stdin JSON 도구 입력의 판정(허용 위조·슬롯 쓰기 주인)을 한 줄 JSON 으로 출력한다."""
    tool_input = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    judgment = judge_tool_input(home, args.session, args.cwd, args.tool, tool_input)
    console.write_machine_line(json.dumps(judgment, ensure_ascii=False))
    return 0
