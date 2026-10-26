#!/usr/bin/env python3
"""claude relay driver — `claude -p` subprocess 세션 구동 (어댑터·얇음).

엔진 세션 연속성(Supervisor)의 SessionDriver 구현체. relay/respawn/marker 로직은 *엔진* 에 있고,
엔진 공개 진입 `pm_engine.cli.hooks.run_relay` 가 이 클래스에 실제 파서·실행기·회전 판정·marker
writer 를 공급해 구성한다. 이 파일은 **claude CLI 고유**한 부분만 —
`claude -p [--session-id <uuid>|--resume <uuid>] --output-format stream-json` 을
subprocess 로 호출하고 stream-json 을 파싱한다(PoC `run_turn` 골격 재사용).

CLI 진입점: `python3 pm_orch_claude.py [--cwd <PM repo root>] [--model opus]`.
사용자가 이 wrapper 를 띄우면 ctx 한계 도달 시 손 안 대고 새 PM 으로 자동 회전(연속 운영).

결정적 `--session-id`: 엔진이 uuid4 발급 → 첫 spawn 은 `--session-id <uuid>` 로 child 의 세션
id 를 *지정*. driver 가 turn 후 usage 판정으로 post-turn marker(`<uuid>.done`)를 박고
supervisor 가 같은 sid 로 stat 한다. resume 은 `--resume <uuid>`. (sid 예측 가능성은 통합 스모크에서 실측.)

nested claude 실행은 OAuth 상속(PoC 확증) — SDK 없이 CLI subprocess 만.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# 같은 디렉토리 어댑터 코어 — 설치 루트(repo_root)와 usage 입력 토큰 관측을 빌려 쓴다.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import ctx_guard  # noqa: E402  (같은 디렉토리 어댑터 코어)

# 엔진 공개 진입(`pm_engine.cli.hooks`)은 이 어댑터가 설치된 루트의 엔진에서 import 한다.
_engine_tools = str(Path(__file__).resolve().parents[1] / ".project_manager" / "tools")
if _engine_tools not in sys.path:
    sys.path.insert(0, _engine_tools)

CLAUDE_BIN = "claude"
DEFAULT_MODEL = "claude-opus-5"  # PM 세션 기본 = opus (품질 우선·사용자 결정). CLI `--model` 로 frugal override 가능.
TURN_TIMEOUT_SEC = 600  # subprocess 당 hard hang 가드(상한 — 한 turn 이 길 수 있음).


# 사용자 1회 허용 표식 — 사용자 프롬프트 훅이 허용 기록 진입 여부를 고르는 데만 쓴다.
GIT_GRANT_MARKER = r"git[ \t]+허용[ \t]*:"
# git-anchor 훅이 명령을 보는 셸 도구. 그 밖의 도구(파일 쓰기·편집·위임)는 엔진이 입력을 본다.
GIT_ANCHOR_SHELL_TOOL = "Bash"


def _hook_session_id(stdin: dict) -> str:
    session = stdin.get("session_id") or stdin.get("sessionId")
    return session if isinstance(session, str) else ""


def _pre_tool_use_output(judgment: dict) -> dict | None:
    verdict = judgment.get("verdict")
    reason = judgment.get("reason", "git cwd 정체 판정")
    if verdict == "deny":
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": f"[git-anchor/deny] {reason}",
            }
        }
    if verdict == "warn":
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": f"[git-anchor/warn] {reason}",
            }
        }
    return None


def git_grant_prompt_evaluate(stdin: dict, root: Path) -> dict | None:
    """UserPromptSubmit — 사용자 프롬프트의 `git 허용: <명령 원문>` 줄을 1회 허용 기록으로 남긴다.

    허용 기록은 이 경로(사용자 입력)에서만 만들어진다. 형식이 틀린 줄이 있으면 프롬프트를 막고
    사유를 사용자에게 보인다 — 허용 없이 대화가 진행돼 거부가 반복되지 않게 한다."""
    prompt = stdin.get("prompt")
    if not isinstance(prompt, str) or not re.search(GIT_GRANT_MARKER, prompt):
        return None
    cwd = stdin.get("cwd")
    anchor = cwd if isinstance(cwd, str) and cwd else str(root)
    from pm_engine.cli import hooks

    try:
        granted = hooks.record_git_grants(hooks.adapter_home(root), _hook_session_id(stdin), anchor, prompt)
    except ValueError as exc:
        return {
            "decision": "block",
            "reason": (
                f"[git-anchor/허용 거부] {exc} — `git 허용: <명령 원문>` 한 줄에 한 명령을 값 그대로 "
                "적어 다시 보낸다"
            ),
        }
    if not granted:
        return None
    listed = ", ".join(f"`{command}`" for command in granted)
    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": (
                f"[git-anchor/허용] 사용자가 폴더 {anchor} 에서 다음 명령의 1회 실행을 허용했다: "
                f"{listed} — 그 명령을 원문 그대로 한 번만 실행한다"
            ),
        }
    }


def git_anchor_hook_evaluate(stdin: dict, root: Path) -> dict | None:
    """Claude 훅 입력을 엔진 공개 판정(`pm_engine.cli.hooks`)에 배선한다.

    PreToolUse(Bash) 는 ``judge_git_anchor_command`` 로(세션이 있으면 1회 허용 소비 포함), 그 밖의
    PreToolUse 도구(settings matcher 가 연 쓰기·편집·위임 도구)는 ``judge_tool_input`` 으로(허용 위조·
    슬롯 쓰기 주인), UserPromptSubmit 은 허용 기록으로 보낸다. 세션 ID 는 훅 입력의 ``session_id`` 다."""
    event = stdin.get("hook_event_name") or stdin.get("hookEventName")
    if event == "UserPromptSubmit":
        return git_grant_prompt_evaluate(stdin, root)
    tool = stdin.get("tool_name") or stdin.get("toolName")
    tool_input = stdin.get("tool_input") or stdin.get("toolInput") or {}
    if event != "PreToolUse" or not isinstance(tool_input, dict):
        return None
    cwd = stdin.get("cwd")
    anchor = cwd if isinstance(cwd, str) and cwd else str(root)
    session = _hook_session_id(stdin)
    if tool != GIT_ANCHOR_SHELL_TOOL:
        from pm_engine.cli import hooks

        return _pre_tool_use_output(
            hooks.judge_tool_input(hooks.adapter_home(root), session, anchor, tool, tool_input)
        )
    command = tool_input.get("command")
    # 셸 명령 원문은 거르지 않고 전부 엔진 판정에 넘긴다 — 판정 대상인지는 엔진이 정한다.
    if not isinstance(command, str):
        return None
    from pm_engine.cli import hooks

    return _pre_tool_use_output(
        hooks.judge_git_anchor_command(
            hooks.adapter_home(root), session, anchor, command, grants=bool(session),
        )
    )


def _write_hook_json(output: dict) -> None:
    """Claude 훅 JSON 을 콘솔 코덱과 무관하게 UTF-8 bytes 로 쓴다.

    Windows 파이프 stdout 은 로케일 코덱(cp949)+``errors=strict`` 다. 판정 사유에는 한글과
    cp949 미매핑 문자(em dash·`✓`·`⚠`)가 실리므로 텍스트 write 는 ``UnicodeEncodeError`` 로
    죽고 훅 출력이 통째로 사라진다 — bytes 를 직접 써서 그 표면을 없앤다.

    **형태는 종전 그대로다**: 단일 JSON · 종결 개행 없음(Claude 훅 소비자 계약 §인터페이스).
    엔진 seam(`pm_engine.infra.console.write_machine_line`)은 한 줄 종결(LF)을 붙이므로
    이 자리에서 그대로 부르면 바이트 형태가 바뀐다. 그래서 seam 은 **인코딩 정책의 출처로만**
    참조하고(같은 규율: 텍스트 레이어 선-flush → UTF-8 bytes → flush), 쓰기는 표준 라이브러리로
    한다 — 어댑터가 엔진 사본에 묶이지 않는 기존 관례와도 같은 방향이다.
    """
    text = json.dumps(output, ensure_ascii=False)
    stream = sys.stdout
    if stream is None:  # pythonw/pyw 기동엔 표준 스트림이 없다 — 종전 write 처럼 무출력.
        return
    buffer = getattr(stream, "buffer", None)
    if buffer is None:  # `.buffer` 없는 캡처 스트림(테스트·래퍼) — 종전 텍스트 경로.
        stream.write(text)
        return
    try:
        # 텍스트 레이어에 남은 출력이 bytes 뒤로 밀리지 않게 먼저 비운다.
        stream.flush()
    except Exception:
        pass
    buffer.write(text.encode("utf-8"))
    buffer.flush()


def _git_anchor_advisory_text(output: dict) -> str | None:
    """중복 억제 대상인 **비차단** advisory 본문 (아니면 None).

    deny 는 `permissionDecision` 으로 도구 실행을 실제로 막는다 — 같은 문구가 반복돼도
    억제하면 두 번째 위험 명령이 그냥 통과한다(가드 약화). 억제 대상은 PreToolUse 의
    `additionalContext` 뿐이다(UserPromptSubmit 허용 기록 안내는 매번 알린다).
    """
    hook_output = output.get("hookSpecificOutput") if isinstance(output, dict) else None
    if (
        not isinstance(hook_output, dict) or hook_output.get("permissionDecision")
        or hook_output.get("hookEventName") != "PreToolUse"
    ):
        return None
    text = hook_output.get("additionalContext")
    return text if isinstance(text, str) and text else None


def _git_anchor_already_emitted(root: Path, payload: dict, output: dict | None) -> bool:
    """이 세션에서 **문자열이 완전히 같은** advisory 를 이미 냈으면 True (이번 호출은 stdout 0).

    한 번 실린 훅 출력은 그 세션 컨텍스트에 그대로 쌓이므로 같은 문구의 반복 발화는
    컨텍스트만 태운다(실측 — 발화 1,261회 중 52.7% 가 세션 내 완전일치 중복).
    판정 로직·차단 동작은 건드리지 않고 *같은 말을 두 번 하지 않을* 뿐이다.

    세션 1회 판정은 엔진 ``claim_notice`` 가 한다(세션키 파일명 안전화·문구 해시 키). ``seen`` 만
    억제하고 ``unavailable``(멤버십 파일 I/O 실패)·비-advisory 는 False — 발화를 유지한다(fail-open).
    """
    text = _git_anchor_advisory_text(output) if output is not None else None
    if text is None:
        return False
    from pm_engine.cli import hooks

    claimed = hooks.claim_notice(hooks.adapter_home(root), session=_hook_session_id(payload), text=text)
    return claimed["status"] == "seen"


def run_git_anchor_hook(root: Path) -> int:
    """stdin JSON을 읽어 Claude hook JSON을 쓰는 얇은 CLI 모드."""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (TypeError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    try:
        # 판정은 설치 루트 엔진의 공개 진입(`pm_engine.cli.hooks`)이 한다 — 그 tools 를 import 경로에 둔다.
        tools = str(root / ".project_manager" / "tools")
        if tools not in sys.path:
            sys.path.insert(0, tools)
        output = git_anchor_hook_evaluate(payload, root)
    except BaseException as exc:  # SystemExit 포함 판정 인프라 손상은 정상 Bash를 막지 않는다.
        event = payload.get("hook_event_name") or payload.get("hookEventName")
        output = {
            "hookSpecificOutput": {
                # 훅 출력의 이벤트 이름은 발화 이벤트와 같아야 한다(허용 기록 실패도 같은 경고 형태).
                "hookEventName": "UserPromptSubmit" if event == "UserPromptSubmit" else "PreToolUse",
                "additionalContext": f"[git-anchor/warn] 판정 불가({exc}) — cwd를 직접 확인",
            }
        }
    else:
        # 세션 내 완전일치 중복은 낼 것을 비운다 — stdout 0바이트(빈 JSON 도 쓰지 않는다).
        # 판정 인프라 손상 발화(위 except)는 이 게이트를 지나지 않는다(fail-open 무조건 발화).
        if _git_anchor_already_emitted(root, payload, output):
            output = None
    if output is not None:
        _write_hook_json(output)
    return 0


def _emit_git_anchor_boundary_warn(exc: BaseException) -> int:
    """hook-mode 최외곽 실패를 Claude advisory JSON + rc0으로 강등한다."""
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": f"[git-anchor/warn] 판정 불가({exc}) — cwd를 직접 확인",
        }
    }
    _write_hook_json(output)
    return 0


class ClaudeCliDriver:
    """`claude -p` subprocess 로 PM 세션을 구동하는 SessionDriver (claude 고유 어댑터).

    얇다 — 세션 생명주기/회전/marker 는 엔진 Supervisor 가 쥐고, 이 driver 는 한 turn 의
    claude CLI 호출 + stream-json 파싱만 한다. PoC run_turn 골격을 메서드로 재사용.
    """

    def __init__(self, parse_stream_json, *, ctx_budget: int | None = None,
                 precompact_at_pct: int | None = None, root: Path | None = None,
                 mark_stop=None, spawn_result=None, model: str = DEFAULT_MODEL,
                 claude_bin: str = CLAUDE_BIN, timeout: int = TURN_TIMEOUT_SEC,
                 runner=subprocess.run, budget_reader=None) -> None:
        # parse_stream_json 은 엔진 순수 헬퍼 주입(DI) — driver 가 파싱 로직을 중복 보유하지 않음.
        self._parse = parse_stream_json
        # ctx 회전 판정과 SpawnResult 생성은 엔진 소유 헬퍼/타입 주입(DI). 어느 ctx 신호든
        # 빠지면 가드는 no-op 이라 기존 단위·부트 경로에 영향을 주지 않는다.
        self._ctx_budget = ctx_budget
        # budget_reader(세션 모델) → (적용 한도, 압축 임박 경고 %, 모델 불일치 경고) — turn 마다 다시 읽는다.
        self._budget_reader = budget_reader
        self._session_model = None
        self._precompact_at_pct = precompact_at_pct
        self._root = Path(root) if root is not None else None
        self._mark_stop = mark_stop
        self._warned_missing_usage = False
        # main 은 엔진 SpawnResult 를 명시 주입한다. 주입이 없으면 구조 호환 tuple 을 쓴다.
        self._spawn_result = spawn_result or (lambda sid, reply: (sid, reply))
        self.model = model
        self.claude_bin = claude_bin
        self.timeout = timeout
        self.runner = runner  # subprocess.run seam(테스트 stub 가능).
        # claude 의 세션 저장은 **cwd-scoped** — `--resume` 는 spawn 과 같은 cwd 에서만 그 세션을
        # 찾는다(다른 cwd 면 "No conversation found"). 따라서 driver 가 세션별 cwd 를 기억해
        # resume 에 재사용한다. 이건 *어댑터*-국소 세션 메타(claude CLI 고유)지 relay
        # 대화 상태가 아니다 — 엔진 Supervisor 의 stateless 불변식은 그대로다.
        self._session_cwd: dict[str, str] = {}

    def spawn(self, cwd: str, session_id: str, bootstrap: str):
        """첫 세션 — `--session-id <uuid>` 로 세션 id 지정 + bootstrap 프롬프트 전송.

        반환 = SpawnResult(관측된 실제 session_id, bootstrap reply). session_id 는 보통 입력값과
        같으며 다르면 marker 환원 경로용으로 관측값을 따른다."""
        observed, reply, used_tokens = self._turn(cwd, bootstrap, session_id=session_id)
        sid = observed or session_id
        self._session_cwd[sid] = cwd  # resume 이 같은 cwd 에서 세션을 찾도록 기억.
        self._maybe_mark_ctx(sid, used_tokens)
        return self._spawn_result(sid, reply)

    def relay_turn(self, session_id: str, text: str) -> str:
        """기존 세션 resume — `--resume <uuid>` 로 한 turn 중계하고 reply 반환.

        claude 세션은 cwd-scoped 라 spawn 때의 cwd 에서 resume 해야 한다(없으면 현재 dir)."""
        cwd = self._session_cwd.get(session_id)
        _, result, used_tokens = self._turn(cwd=cwd, prompt=text, resume=session_id)
        self._maybe_mark_ctx(session_id, used_tokens)
        return result or ""

    def close(self, session_id: str) -> None:
        """`-p` 1회성 turn 은 자동 exit(PoC 확증) — 명시 kill 불요. 세션 cwd 메타만 정리."""
        self._session_cwd.pop(session_id, None)

    # ── claude CLI 한 turn (PoC run_turn 골격) ─────────────────────────────────

    def _turn(self, cwd, prompt, *, session_id=None, resume=None):
        """비대화 claude turn 1회. (observed_session_id, result_text, used_tokens) 반환.

        child cwd 격리 — subprocess cwd 를 PM repo root 로 명시(엔진 제약). resume 은
        같은 세션을 cwd 인자 없이 잇는다(claude 가 세션에 cwd 를 묶음).
        프롬프트는 표준 입력이다(cross 위임 `build_claude_argv` 와 같다 — 여러 줄 프롬프트를 실행 인자에
        실으면 Windows 에서 `claude.cmd` 를 거칠 때 cmd.exe 가 첫 줄 뒤를 버린다)."""
        cmd = [self.claude_bin, "-p",
               "--output-format", "stream-json", "--verbose",
               "--model", self.model]
        if session_id:
            cmd += ["--session-id", session_id]
        if resume:
            cmd += ["--resume", resume]

        run_kwargs = dict(
            capture_output=True, text=True, timeout=self.timeout,
            # 프롬프트 전문을 stdin 으로 쓰고 닫는다 — relay supervisor 의 파이프 stdin 은 child 에 가지 않는다.
            input=prompt,
        )
        if cwd is not None:
            run_kwargs["cwd"] = cwd  # child cwd 격리(PM repo root).
        try:
            completed = self.runner(cmd, **run_kwargs)
        except subprocess.TimeoutExpired:
            sys.stderr.write(f"[pm-orch] claude turn timeout ({self.timeout}s)\n")
            return None, None, None
        except OSError as exc:
            sys.stderr.write(f"[pm-orch] claude 실행 실패: {exc}\n")
            return None, None, None

        # 실패를 조용한 빈 응답으로 삼키지 않는다 — 최소 진단을 stderr 로(stdout=PM 대화 채널 보존).
        if getattr(completed, "returncode", 0):
            tail = (completed.stderr or "").strip().splitlines()[-1:] or [""]
            sys.stderr.write(f"[pm-orch] claude rc={completed.returncode}: {tail[0]}\n")
        elif not (completed.stdout or ""):
            sys.stderr.write("[pm-orch] claude turn 무출력(rc 0) — stdin/파싱 점검\n")

        self._session_model = None
        for raw in (completed.stdout or "").splitlines():
            try:
                event = json.loads(raw)
            except (ValueError, TypeError):
                continue
            message = event.get("message") if isinstance(event, dict) and event.get("type") == "assistant" else None
            # 사용량이 있는 응답의 모델만 쓴다 — 합성 응답(API 오류 등)은 사용량 0 이다(훅과 같은 규칙).
            if isinstance(message, dict) and (ctx_guard._usage_input_tokens(message.get("usage")) or 0) > 0:
                self._session_model = message.get("model")
        lines = (completed.stdout or "").splitlines()
        return self._parse(lines)

    def _maybe_mark_ctx(self, session_id: str, used_tokens: int | None) -> None:
        """주입된 usage·예산 신호가 모두 있으면 엔진의 post-turn 회전 헬퍼를 호출한다."""
        if self._budget_reader is not None:
            self._ctx_budget, self._precompact_at_pct, warning = self._budget_reader(self._session_model)
            if warning:
                sys.stderr.write(f"[pm-orch] {warning}\n")

        ctx_di_complete = all(value is not None for value in (
            self._ctx_budget, self._precompact_at_pct, self._root, self._mark_stop,
        ))
        if used_tokens is None:
            if ctx_di_complete and not self._warned_missing_usage:
                sys.stderr.write(
                    "[pm-orch] claude usage 신호 소실 — ctx post-turn 가드를 판정할 수 없음\n"
                )
                self._warned_missing_usage = True
            return
        if not ctx_di_complete:
            return
        # claude usage 는 요청 단위 절대 점유라 codex `_last_total` 누계 차분이 불요하다.
        # 파서와 ctx_guard._usage_input_tokens 모두 입력+캐시 계열을 현재 점유로 정의한다.
        self._mark_stop(
            self._root, session_id, used_tokens, self._ctx_budget, self._precompact_at_pct,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="claude PM relay — thin stateless supervisor 세션 자동-회전."
    )
    parser.add_argument(
        "--cwd", default=None,
        help="child PM 세션의 작업 디렉토리(기본 = 현재 dir). PM repo root 여야 한다.",
    )
    parser.add_argument(
        "--model", default=DEFAULT_MODEL,
        help=f"claude 모델(기본 {DEFAULT_MODEL}). opus 등으로 override.",
    )
    parser.add_argument(
        "--task", default=None, metavar="이름",
        help="task 정체성(필수). 회전된 새 PM 세션의 재진입 프롬프트에 `--task <task>` 실값을 "
             "박아 같은 task 를 이어가게 한다(명시 전달·cwd 추론 금지).",
    )
    parser.add_argument("--git-anchor-hook", action="store_true", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    actual_argv = list(sys.argv[1:] if argv is None else argv)
    hook_requested = "--git-anchor-hook" in actual_argv
    if hook_requested:
        try:
            args = build_parser().parse_args(actual_argv)
            root = ctx_guard.repo_root(Path(__file__).resolve().parent)
            return run_git_anchor_hook(root)
        except BaseException as exc:
            return _emit_git_anchor_boundary_warn(exc)
    parser = build_parser()
    args = parser.parse_args(actual_argv)
    if not args.task:
        # relay 는 회전된 세션을 `/pm-bootstrap --task <task>` 로 다시 연다 — 부트스트랩이 `--task`
        # 필수라 task 없는 relay 는 시작하지 않는다. 훅 진입(같은 파서)은 task 를 받지 않아 여기서 본다.
        parser.error("relay 에는 --task <task> 가 필요하다")
    root = ctx_guard.repo_root(Path(__file__).resolve().parent)
    from pm_engine.cli import hooks

    cwd = args.cwd or os.getcwd()

    def announced_driver(*driver_args, **driver_kwargs) -> ClaudeCliDriver:
        # 시작 안내는 엔진이 예산 기록을 확인하고 driver 를 구성한 뒤에 낸다(설정 오류가 먼저 보인다).
        driver = ClaudeCliDriver(*driver_args, **driver_kwargs)
        sys.stderr.write(
            f"[pm-orch] claude supervisor 시작 (cwd={cwd} model={args.model}). "
            "ctx 한계 도달 시 자동 회전. 종료 = /quit 또는 EOF.\n"
        )
        sys.stderr.flush()
        return driver

    # 압축 한도 기록이 없거나 손상·입력 불일치면 시작하지 않는다(CtxBudgetError).
    return hooks.run_relay(
        hooks.adapter_home(root), announced_driver, harness="claude", task=args.task, cwd=cwd,
        model=args.model, stdin=sys.stdin, stdout=sys.stdout,
    )


if __name__ == "__main__":
    raise SystemExit(main())
