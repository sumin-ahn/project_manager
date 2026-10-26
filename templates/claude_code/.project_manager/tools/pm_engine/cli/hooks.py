"""하네스 어댑터의 엔진 공개 진입과 훅 판정 CLI (층: cli).

Python 어댑터가 엔진에서 import 하는 모듈은 이것 하나다. 어댑터는 `adapter_home` 으로 설치 루트의
Home 을 만들고 git 판정·ctx 판정·원칙 recall·세션 1회 안내·relay 구동 함수를 부른다.
`board.py git-anchor`·`git-grant`·`git-tool-input`·`hook-context`·`hook-tool` 명령 처리도 이 모듈이
소유한다. 판정 규칙은 app.hooks·app.engine_guard·domain.git_policy(git), app.ctx_hooks·
domain.ctx_recovery(ctx), app.principle_recall(원칙), app.session.relay(세션 회전)가 소유하고, 여기서는
입력을 검증해 넘기고 relay 협력자를 실제 소유자에서 구성한다.
"""
from __future__ import annotations

import argparse
import functools
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from pm_engine.app import ctx_hooks, engine_guard, principle_recall
from pm_engine.app import hooks as app_hooks
from pm_engine.app.session import relay
from pm_engine.domain import git_policy
from pm_engine.domain import home
from pm_engine.domain import principle_rules
from pm_engine.infra import console, fs, pm_paths, principle_store, raw_output
from pm_engine.infra.harness import contract, watchdog


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


# ── ctx 판정·원칙 recall·세션 1회 안내 (어댑터 공개 API) ─────────────────────
CTX_HARNESSES = ("claude", "codex", "opencode")
# 세션 1회 안내 멤버십 — namespace 와 문구 sha256 앞 자리 수(문구 전문을 파일에 남기지 않는다).
NOTICE_NAMESPACE = "git-anchor"
NOTICE_KEY_CHARS = 16
_NOTICE_STATUS = {True: "claimed", False: "seen", None: "unavailable"}
# 기계 JSON 입력 세대와 키 집합 — 누락·추가 키는 업무 실행 전에 거부한다.
HOOK_WIRE_VERSION = 1
HOOK_CONTEXT_KEYS = frozenset({"version", "tokens", "model", "main_thread", "boundary"})
HOOK_TOOL_KEYS = frozenset({"version", "cwd", "tool_name", "tool_input", "shell_command", "recall"})
HOOK_RECALL_KEYS = frozenset({"session", "on", "text", "main_thread"})
# 기계 입력 형식 오류의 종료 코드(argparse 사용 오류와 같은 값).
HOOK_WIRE_ERROR_RC = 2


def _check_type(name: str, value: object, expected: tuple[type, ...]) -> None:
    if not isinstance(value, expected) or (isinstance(value, bool) and bool not in expected):
        names = "/".join("None" if kind is type(None) else kind.__name__ for kind in expected)
        raise TypeError(f"{name} 은 {names} 여야 한다: {value!r}")


def _check_context_input(*, harness: object, session: object, tokens: object, model: object,
                         main_thread: object, boundary: object) -> None:
    """ctx API 입력 형식 — 하네스 3종·문자열 세션·bool 아닌 음이 아닌 정수 토큰·문자열 또는 None 모델."""
    _check_type("harness", harness, (str,))
    if harness not in CTX_HARNESSES:
        raise ValueError(f"지원하지 않는 하네스: {harness!r}")
    _check_type("session", session, (str,))
    _check_type("tokens", tokens, (int,))
    if tokens < 0:
        raise ValueError(f"tokens 는 음수일 수 없다: {tokens}")
    _check_type("model", model, (str, type(None)))
    _check_type("main_thread", main_thread, (bool,))
    _check_type("boundary", boundary, (str, type(None)))
    if boundary == "":
        raise ValueError("boundary 는 빈 문자열일 수 없다")


def context_status(home: home.Home, *, harness: str, tokens: int, model: str | None) -> dict:
    """읽기 전용 ctx 상태 6키(version·status·measured·used_pct·level·notice). marker 를 쓰지 않는다."""
    _check_context_input(harness=harness, session="", tokens=tokens, model=model,
                         main_thread=True, boundary=None)
    return ctx_hooks.context_status(home, harness=harness, tokens=tokens, model=model)


def judge_context(home: home.Home, *, harness: str, session: str, tokens: int, model: str | None,
                  main_thread: bool, boundary: str | None = None) -> dict:
    """ctx 판정 11키 — 밴드 안내 marker 선점과 압축 경계 처리를 포함한다(`app.ctx_hooks.judge_context`)."""
    _check_context_input(harness=harness, session=session, tokens=tokens, model=model,
                         main_thread=main_thread, boundary=boundary)
    return ctx_hooks.judge_context(home, harness=harness, session=session, tokens=tokens, model=model,
                                   main_thread=main_thread, boundary=boundary)


def _check_recall_input(*, session: object, on: object, text: object, main_thread: object) -> None:
    _check_type("session", session, (str,))
    _check_type("on", on, (str,))
    if on not in principle_rules.ON_VALUES:
        raise ValueError(f"지원하지 않는 recall 축: {on!r}")
    _check_type("text", text, (str,))
    _check_type("main_thread", main_thread, (bool,))


def recall_principles(home: home.Home, *, session: str, on: str, text: str,
                      main_thread: bool) -> dict | None:
    """이 호출에 일치한 미주입 원칙(count·keys·text, 파손이 있으면 broken). 정상 무매칭은 None.

    서브에이전트는 seen marker 를 읽지도 쓰지도 않는다. 레지스트리 판독·marker 쓰기 실패는 올린다."""
    _check_recall_input(session=session, on=on, text=text, main_thread=main_thread)
    if not main_thread:
        return None
    seen = principle_store.load_seen_marker(home, session)
    result = principle_recall.judge_recall(home, on=on, text=text, seen=seen)
    if result is not None and result["keys"]:
        principle_store.record_seen_marker(home, session, result["keys"])
    return result


def rearm_principles(home: home.Home, *, session: str) -> None:
    """세션의 원칙 recall marker 를 지워 다음 사이클에 다시 주입 가능하게 한다."""
    _check_type("session", session, (str,))
    principle_store.rearm_seen_marker(home, session)


def claim_notice(home: home.Home, *, session: str, text: str) -> dict[str, str]:
    """세션에서 같은 안내 문구를 처음 내는지 선점한다 — ``{"status": claimed|seen|unavailable}``.

    unavailable 은 멤버십 파일을 쓸 수 없다는 뜻이며 호출부는 안내를 그대로 낸다(중복 억제 없음)."""
    _check_type("session", session, (str,))
    _check_type("text", text, (str,))
    key = hashlib.sha256(text.encode("utf-8")).hexdigest()[:NOTICE_KEY_CHARS]
    claimed = relay.claim_session_once(home.local_dir.parent.parent, session, NOTICE_NAMESPACE, key)
    return {"status": _NOTICE_STATUS[claimed]}


# ── relay 구동 ─────────────────────────────────────────────────────────────
# OpenCode 한 turn 의 전체 시간 상한(초) — 첫 이벤트 감시는 이 상한 안쪽에서 돈다.
OPENCODE_TURN_TIMEOUT_SEC = 600


def _resolving_runner(cmd, **kwargs):
    """Claude·Codex turn 실행기 — argv 첫 토큰을 실행 파일로 해소한 뒤 나머지를 그대로 넘긴다."""
    return subprocess.run([contract.resolve_executable(cmd[0], kwargs.get("env")), *cmd[1:]], **kwargs)


def _watchdog_runner(cmd, *, capture_output=True, text=True, timeout=OPENCODE_TURN_TIMEOUT_SEC, **_kwargs):
    """OpenCode turn 실행기 — 첫 이벤트 워치독(기본 대기·재시도 횟수)을 전체 시간 상한 안에서 쓴다.

    캡처·text 는 워치독이 항상 한다. 재시도 소진(`watchdog.StallWatchdogError`)은 driver 가 처리한다."""
    return watchdog.run_with_first_event_watchdog(
        cmd,
        first_event_timeout=watchdog.first_event_timeout_default(),
        overall_timeout=timeout,
        retries=watchdog.stall_retries_default(),
    )


def _marker_writer(harness: str):
    """post-turn 회전 marker writer — 기록하지 못하면 원인을 stderr 에 알리고 False 를 돌려준다."""
    def write(root: Path, session_id: str) -> bool:
        if relay.write_post_turn_marker(root, session_id):
            return True
        sys.stderr.write(f"[pm-orch] {harness} ctx marker 박제 실패\n")
        return False
    return write


def run_relay(home: home.Home, driver_factory, *, harness: str, task: str, cwd: str,
              model: str | None = None, agent: str | None = None, stdin, stdout) -> int:
    """하네스 driver 를 실제 협력자로 구성하고 relay Supervisor 를 돌려 그 종료 코드를 돌려준다.

    ``driver_factory`` 는 native driver 클래스다. 예산·회전 임계는 Home 의 압축 한도 기록에서 읽고
    (`relay.ctx_budget`), 기록 문제·낮은 회전 임계는 driver 를 만들기 전에 올린다. marker·relay 상태는
    Home 데이터 루트에 두고 child 의 cwd 는 ``cwd`` 다."""
    _check_type("harness", harness, (str,))
    if harness not in CTX_HARNESSES:
        raise ValueError(f"지원하지 않는 하네스: {harness!r}")
    root = home.local_dir.parent.parent
    ctx_budget, precompact_at_pct, _warning = relay.ctx_budget(home, harness, None)
    writer = _marker_writer(harness)
    common = {"ctx_budget": ctx_budget, "precompact_at_pct": precompact_at_pct, "root": root,
              "spawn_result": relay.SpawnResult}
    reader = functools.partial(relay.ctx_budget, home, harness)
    post_turn = functools.partial(relay.mark_ctx_post_turn_if_over, mark=writer)
    if harness == "claude":
        driver = driver_factory(contract.parse_stream_json, **common, mark_stop=post_turn,
                                runner=_resolving_runner, model=model, budget_reader=reader)
    elif harness == "codex":
        driver = driver_factory(contract.parse_codex_json, **common, mark_stop=writer,
                                runner=_resolving_runner, budget_reader=reader,
                                mark_ctx_if_over=relay.mark_ctx_post_turn_if_over)
    else:
        driver = driver_factory(contract.parse_opencode_json, **common, mark_stop=post_turn,
                                runner=_watchdog_runner, agent=agent,
                                stall_error=watchdog.StallWatchdogError, temp_root=raw_output.temp_root,
                                attached_message=contract.OPENCODE_ATTACHED_MSG,
                                assert_prompt_in_cwd=contract.assert_opencode_prompt_in_cwd)
    return relay.Supervisor(driver, root=root, task=task).run_loop(cwd, stdin, stdout)


# ── 기계 JSON 진입 (`board.py hook-context`·`hook-tool`) ──────────────────────
class HookWireError(ValueError):
    """기계 JSON 입력이 계약 형식이 아니다(업무 실행 전 거부)."""


def _read_wire_object(keys: frozenset[str]) -> dict:
    """stdin bytes 를 UTF-8 JSON 객체 하나로 읽고 키 집합·세대를 확인한다."""
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise HookWireError(f"UTF-8 JSON 이 아님: {exc}") from None
    if not isinstance(payload, dict):
        raise HookWireError(f"JSON 객체가 아님: {type(payload).__name__}")
    if set(payload) != keys:
        raise HookWireError(f"키 집합이 다름: {sorted(payload)} (기대 {sorted(keys)})")
    if payload["version"] != HOOK_WIRE_VERSION or isinstance(payload["version"], bool):
        raise HookWireError(f"지원하지 않는 version: {payload['version']!r}")
    return payload


def _wire_failure(command: str, exc: Exception) -> int:
    print(f"[{command}] 입력 형식 오류: {exc}", file=sys.stderr)
    return HOOK_WIRE_ERROR_RC


def cmd_hook_context(home: home.Home, args: argparse.Namespace) -> int:
    """stdin ctx 관측(version·tokens·model·main_thread·boundary)을 판정해 11키 JSON 한 줄을 낸다.

    입력 형식 오류는 rc 2·stdout 0바이트, 설정·IO·렌더 오류는 예외로 올린다(rc 1·stdout 0바이트)."""
    try:
        request = _read_wire_object(HOOK_CONTEXT_KEYS)
        observation = {key: request[key] for key in ("tokens", "model", "main_thread", "boundary")}
        _check_context_input(harness=args.harness, session=args.session, **observation)
    except (HookWireError, TypeError, ValueError) as exc:
        return _wire_failure("hook-context", exc)
    decision = judge_context(home, harness=args.harness, session=args.session, **observation)
    console.write_machine_line(json.dumps(decision, ensure_ascii=False))
    return 0


def _hook_tool_request(request: dict) -> None:
    """hook-tool 입력 형식 — 셸 호출은 tool_input.command 와 shell_command 가 같아야 한다."""
    _check_type("cwd", request["cwd"], (str,))
    _check_type("tool_name", request["tool_name"], (str,))
    _check_type("shell_command", request["shell_command"], (str, type(None)))
    if request["shell_command"] is not None:
        tool_input = request["tool_input"]
        _check_type("tool_input", tool_input, (dict,))
        if tool_input.get("command") != request["shell_command"]:
            raise ValueError("shell_command 가 tool_input.command 와 다르다")
    recall = request["recall"]
    if recall is not None:
        _check_type("recall", recall, (dict,))
        if set(recall) != HOOK_RECALL_KEYS:
            raise HookWireError(f"recall 키 집합이 다름: {sorted(recall)} (기대 {sorted(HOOK_RECALL_KEYS)})")
        _check_recall_input(**recall)


def cmd_hook_tool(home: home.Home, args: argparse.Namespace) -> int:
    """일반 도구 호출 1회의 git 판정과 원칙 recall 을 한 JSON 줄(version·git·recall·recall_error)로 낸다.

    git 거부면 recall 을 돌리지 않는다(seen 미소비). recall 의 판독·기록 실패는 git 판정을 보존한 채
    ``recall_error`` 로 싣는다. 입력 형식 오류는 rc 2, git 판정 오류는 예외로 올린다(rc 1)."""
    try:
        request = _read_wire_object(HOOK_TOOL_KEYS)
        _hook_tool_request(request)
    except (HookWireError, TypeError, ValueError) as exc:
        return _wire_failure("hook-tool", exc)
    if request["shell_command"] is not None:
        git = judge_git_anchor_command(home, args.session, request["cwd"], request["shell_command"], grants=True)
    else:
        git = judge_tool_input(home, args.session, request["cwd"], request["tool_name"], request["tool_input"])
    recall, recall_error = None, None
    if request["recall"] is not None and git["verdict"] != "deny":
        try:
            recall = recall_principles(home, **request["recall"])
        except (OSError, UnicodeError) as exc:
            recall_error = f"{type(exc).__name__}: {exc}"
    console.write_machine_line(json.dumps(
        {"version": HOOK_WIRE_VERSION, "git": git, "recall": recall, "recall_error": recall_error},
        ensure_ascii=False,
    ))
    return 0
