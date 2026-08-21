#!/usr/bin/env python3
"""claude relay driver — `claude -p` subprocess 세션 구동 (ADR-0009 · 어댑터·얇음).

엔진 core(`pm_relay.py`)의 SessionDriver Protocol 구현체. relay/respawn/marker 로직은
*엔진* 에 있고(루트 `.project_manager/tools/`·DI 로 테스트), 이 파일은 **claude CLI 고유**한
부분만 — `claude -p [--session-id <uuid>|--resume <uuid>] --output-format stream-json` 을
subprocess 로 호출하고 stream-json 을 파싱한다(PoC `run_turn` 골격 재사용).

CLI 진입점: `python3 pm_orch_claude.py [--cwd <PM repo root>] [--model opus]`.
사용자가 이 wrapper 를 띄우면 ctx 한계 도달 시 손 안 대고 새 PM 으로 자동 회전(연속 운영).

결정적 `--session-id`: 엔진이 uuid4 발급 → 첫 spawn 은 `--session-id <uuid>` 로 child 의 세션
id 를 *지정*. driver 가 turn 후 usage 판정으로 post-turn marker(`<uuid>.done`)를 박고
supervisor 가 같은 sid 로 stat 한다. resume 은 `--resume <uuid>`. (sid 예측 가능성은 통합 스모크에서 실측.)

nested claude 실행은 OAuth 상속(T-0044 PoC 확증) — SDK 없이 CLI subprocess 만.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# 엔진 core 를 import — 어댑터는 엔진 사본 경로에 묶이지 않게 repo_root 로 동적 해석한다
# (ctx_guard.repo_root 와 동일 관례). SessionDriver Protocol·new_session_id 등을 빌려 쓴다.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import ctx_guard  # noqa: E402  (repo_root 재사용 — 같은 디렉토리 어댑터 코어)

CLAUDE_BIN = "claude"
DEFAULT_MODEL = "claude-opus-5"  # PM 세션 기본 = opus (품질 우선·2026-08-06 사용자 결정). CLI `--model` 로 frugal override 가능.
TURN_TIMEOUT_SEC = 600  # subprocess 당 hard hang 가드(상한 — 한 turn 이 길 수 있음).
GIT_ANCHOR_ONCE_NAMESPACE = "git-anchor"  # 세션 1회 발화 멤버십 파일 접미사(엔진 claim_session_once).
GIT_ANCHOR_ONCE_KEY_CHARS = 16  # 멤버십 키 = 발화 문구 sha256 앞 16자(문구 전문을 파일에 남기지 않는다).


def _load_engine():
    """루트 `.project_manager/tools/pm_relay.py`(엔진 core)를 importlib 로 로드.

    어댑터는 엔진을 PYTHONPATH 에 의존하지 않고 repo_root 기준 경로로 직접 로드한다
    (ctx_guard 가 board.py 를 import 하지 않고 local.conf 를 직접 파싱하는 것과 같은 관례).
    """
    root = ctx_guard.repo_root(Path(__file__).resolve().parent)
    engine_path = root / ".project_manager" / "tools" / "pm_relay.py"
    spec = importlib.util.spec_from_file_location("pm_relay", engine_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, root


def _load_board(root: Path):
    """raw git 훅 판정의 단일 진실인 board.py를 root 기준으로 로드한다."""
    board_path = root / ".project_manager" / "tools" / "board.py"
    spec = importlib.util.spec_from_file_location("pm_git_anchor_board", board_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git_anchor_hook_evaluate(stdin: dict, root: Path) -> dict | None:
    """Claude PreToolUse(Bash) 입력을 중앙 ``judge_git_anchor_command`` seam에 배선한다."""
    event = stdin.get("hook_event_name") or stdin.get("hookEventName")
    tool = stdin.get("tool_name") or stdin.get("toolName")
    tool_input = stdin.get("tool_input") or stdin.get("toolInput") or {}
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    # 매 Bash 호출에 훅이 떠도 판정 대상 비포함은 board import조차 하지 않는 선필터(핫패스 비용 0).
    # 대상은 넷이다 — git 호출(앵커) · 엔진 도구 호출(사본 정체) · pytest(트리 정체) · cd 잔존.
    if event != "PreToolUse" or tool != "Bash" or not isinstance(command, str):
        return None
    prefilter = command.replace("\\\n", "").replace("'", "").replace('"', "")
    normalized = re.sub(r"/+", "/", prefilter.replace("\\", "/"))
    if not (
        re.search(r"(?<![A-Za-z0-9_.-])git(?=\s|$|[<>])", prefilter)
        or ".project_manager/tools/" in normalized
        or re.search(r"(?<![A-Za-z0-9_.-])pytest(?=\s|$)", prefilter)
        or re.search(r"(?:^|[;&|])\s*cd(?=\s)", prefilter)
    ):
        return None
    cwd = stdin.get("cwd")
    anchor = cwd if isinstance(cwd, str) and cwd else str(root)
    judgment = _load_board(root).judge_git_anchor_command(anchor, command)
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


def _write_hook_json(output: dict) -> None:
    """Claude 훅 JSON 을 콘솔 코덱과 무관하게 UTF-8 bytes 로 쓴다 (T-0736).

    Windows 파이프 stdout 은 로케일 코덱(cp949)+``errors=strict`` 다. 판정 사유에는 한글과
    cp949 미매핑 문자(em dash·`✓`·`⚠`)가 실리므로 텍스트 write 는 ``UnicodeEncodeError`` 로
    죽고 훅 출력이 통째로 사라진다 — bytes 를 직접 써서 그 표면을 없앤다.

    **형태는 종전 그대로다**: 단일 JSON · 종결 개행 없음(Claude 훅 소비자 계약·[[T-0736]]
    §인터페이스). 엔진 seam(`console_encoding.write_machine_line`)은 한 줄 종결(LF)을 붙이므로
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
    억제하면 두 번째 위험 명령이 그냥 통과한다(가드 약화). 억제 대상은 `additionalContext` 뿐이다.
    """
    hook_output = output.get("hookSpecificOutput") if isinstance(output, dict) else None
    if not isinstance(hook_output, dict) or hook_output.get("permissionDecision"):
        return None
    text = hook_output.get("additionalContext")
    return text if isinstance(text, str) and text else None


def _git_anchor_already_emitted(root: Path, payload: dict, output: dict | None) -> bool:
    """이 세션에서 **문자열이 완전히 같은** advisory 를 이미 냈으면 True (이번 호출은 stdout 0).

    한 번 실린 훅 출력은 그 세션 컨텍스트에 그대로 쌓이므로 같은 문구의 반복 발화는
    컨텍스트만 태운다([[T-0764]] 실측 — 발화 1,261회 중 52.7% 가 세션 내 완전일치 중복).
    판정 로직·차단 동작은 건드리지 않고 *같은 말을 두 번 하지 않을* 뿐이다.

    세션키는 훅 stdin 의 `session_id`(엔진 `_sanitize_session_id` 가 파일명 안전화·빈 값은
    `unknown`)이고 namespace 는 `git-anchor` 고정이라, 다른 세션·다른 경고 클래스의 멤버십과
    섞이지 않는다. 마커 I/O 실패(None)·비-advisory 는 False — 발화를 유지한다(fail-open).
    """
    text = _git_anchor_advisory_text(output) if output is not None else None
    if text is None:
        return False
    try:
        engine, _engine_root = _load_engine()
        key = hashlib.sha256(text.encode("utf-8")).hexdigest()[:GIT_ANCHOR_ONCE_KEY_CHARS]
        claimed = engine.claim_session_once(
            root,
            payload.get("session_id") or payload.get("sessionId") or "",
            GIT_ANCHOR_ONCE_NAMESPACE,
            key,
        )
    except BaseException:  # 멤버십 판정 손상은 훅 판정을 깨지 않는다 — 발화 유지(fail-open).
        return False
    return claimed is False


def run_git_anchor_hook(root: Path) -> int:
    """stdin JSON을 읽어 Claude hook JSON을 쓰는 얇은 CLI 모드."""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (TypeError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    try:
        output = git_anchor_hook_evaluate(payload, root)
    except BaseException as exc:  # SystemExit 포함 판정 인프라 손상은 정상 Bash를 막지 않는다.
        output = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
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
                 stop_pct: int | None = None, root: Path | None = None,
                 mark_stop=None, spawn_result=None, model: str = DEFAULT_MODEL,
                 claude_bin: str = CLAUDE_BIN, timeout: int = TURN_TIMEOUT_SEC,
                 runner=subprocess.run) -> None:
        # parse_stream_json 은 엔진 순수 헬퍼 주입(DI) — driver 가 파싱 로직을 중복 보유하지 않음.
        self._parse = parse_stream_json
        # ctx 회전 판정과 SpawnResult 생성은 엔진 소유 헬퍼/타입 주입(DI). 어느 ctx 신호든
        # 빠지면 가드는 no-op 이라 기존 단위·부트 경로에 영향을 주지 않는다.
        self._ctx_budget = ctx_budget
        self._stop_pct = stop_pct
        self._root = Path(root) if root is not None else None
        self._mark_stop = mark_stop
        self._warned_missing_usage = False
        # main 은 타입을 명시 주입한다. parser만 주입하는 기존/단위 경로도 parser 소유 엔진의
        # SpawnResult를 찾아 같은 선언 타입을 쓰며, 제3자 parser면 구조 호환 tuple로 폴백한다.
        parser_globals = getattr(parse_stream_json, "__globals__", {})
        self._spawn_result = (
            spawn_result or parser_globals.get("SpawnResult")
            or (lambda sid, reply: (sid, reply))
        )
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

        child cwd 격리 — subprocess cwd 를 PM repo root 로 명시(엔진 제약 ①). resume 은
        같은 세션을 cwd 인자 없이 잇는다(claude 가 세션에 cwd 를 묶음)."""
        cmd = [self.claude_bin, "-p", prompt,
               "--output-format", "stream-json", "--verbose",
               "--model", self.model]
        if session_id:
            cmd += ["--session-id", session_id]
        if resume:
            cmd += ["--resume", resume]

        run_kwargs = dict(
            capture_output=True, text=True, timeout=self.timeout,
            # relay supervisor 의 파이프 stdin 을 child 가 삼키지 않게 즉시 EOF 로 격리.
            stdin=subprocess.DEVNULL,
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

        lines = (completed.stdout or "").splitlines()
        return self._parse(lines)

    def _maybe_mark_ctx(self, session_id: str, used_tokens: int | None) -> None:
        """주입된 usage·예산 신호가 모두 있으면 엔진의 post-turn 회전 헬퍼를 호출한다."""
        ctx_di_complete = all(value is not None for value in (
            self._ctx_budget, self._stop_pct, self._root, self._mark_stop,
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
            self._root, session_id, used_tokens, self._ctx_budget, self._stop_pct,
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
        help="task 정체성(F7·T-0356) — 회전된 새 PM 세션의 재진입 프롬프트에 `--task <이름>` 실값을 "
             "박아 같은 task 를 resume 하게 한다((b) 명시 전달·cwd 추론 금지). 미지정이면 bare "
             "`/pm-bootstrap`(슬롯/솔로).",
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
    args = build_parser().parse_args(actual_argv)
    engine, root = _load_engine()

    cwd = args.cwd or os.getcwd()
    conf = ctx_guard.load_local_config(root)
    ctx_budget = ctx_guard.resolve_budget(conf, "claude")
    stop_pct = ctx_guard.ctx_thresholds(conf)["stop_pct"]
    engine.validate_relay_budget(ctx_budget, stop_pct)
    driver = ClaudeCliDriver(
        engine.parse_stream_json,
        ctx_budget=ctx_budget,
        stop_pct=stop_pct,
        root=root,
        mark_stop=engine.mark_ctx_post_turn_if_over,
        spawn_result=engine.SpawnResult,
        model=args.model,
    )
    supervisor = engine.Supervisor(driver, root=root, task=args.task)

    sys.stderr.write(
        f"[pm-orch] claude supervisor 시작 (cwd={cwd} model={args.model}). "
        "ctx 한계 도달 시 자동 회전. 종료 = /quit 또는 EOF.\n"
    )
    sys.stderr.flush()
    return supervisor.run_loop(cwd, sys.stdin, sys.stdout)


if __name__ == "__main__":
    raise SystemExit(main())
