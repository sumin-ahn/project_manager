#!/usr/bin/env python3
"""opencode relay driver — `opencode run` subprocess 세션 구동 (어댑터·얇음).

엔진 세션 연속성(Supervisor)의 SessionDriver 구현체. relay/respawn/marker 로직은 *엔진* 에 있고,
엔진 공개 진입 `pm_engine.cli.hooks.run_relay` 가 이 클래스에 실제 파서·첫 이벤트 워치독 실행기·
회전 판정·프롬프트 첨부 협력자를 공급해 구성한다. 이 파일은 **opencode CLI 고유**한 부분만 —
`opencode run --format json` 을 subprocess 로 호출하고 json 이벤트 스트림을 파싱한다(claude
`pm_orch_claude.py` 와 동형).

CLI 진입점: `python3 pm_orch_opencode.py [--cwd <PM repo root>] [--agent pm]`.
사용자가 이 wrapper 를 띄우면 ctx 한계 도달 시 손 안 대고 새 PM 으로 자동 회전(연속 운영).

opencode sid 발급(claude 와 다른 핵심): claude 는 `--session-id <uuid>` 로 child 의 세션 id 를
*지정* 하지만, opencode 는 `opencode run -s <없는id>` → "Session not found"(실측) — sid 사전
지정 불가다. 대신 `--format json` 모든 이벤트에 `sessionID` 가 실리므로(실측) **출력에서 sid 를
파싱해 획득** 한다. 엔진이 발급한 uuid4 session_id 인자는 **무시** 한다(opencode 가 발급한 sid 가
권위 — driver 가 그 sid 로 post-turn marker 를 쓰고 supervisor 가 stat).

opencode 어댑터는 claude 와 달리 옆에 Python `ctx_guard` 모듈이 없다(ctx-guard 는 JS plugin) —
그래서 엔진 루트 해소를 driver 자체에 둔다. 규칙은 JS 훅 코어의 `ENGINE_ROOT` 와 같다: 파일 자기
위치에서 고정 깊이로 받고 조상을 훑지 않는다.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

OPENCODE_BIN = "opencode"
DEFAULT_AGENT = "pm"          # PM primary spawn 타깃. build 폴백(`--agent build`).
TURN_TIMEOUT_SEC = 600        # subprocess 당 hard hang 가드(상한 — 한 turn 이 길 수 있음).


def repo_root(start: Path) -> Path:
    """driver 위치(``<root>/.opencode/``)에서 엔진 루트를 낸다 — 그 부모다.

    어댑터 사본은 항상 ``<root>/.opencode/`` 에 설치되므로 루트는 그 자리의 함수다. 조상을
    훑어 ``.project_manager/tools/pm_handoff.py`` 를 찾으면 driver 가 자기 트리가 아니라 위에
    있는 남의 PM 홈에 착지한다(claude ctx_guard.repo_root·codex `repo_root` 동형).
    """
    return start.resolve().parent


class OpencodeCliDriver:
    """`opencode run` subprocess 로 PM 세션을 구동하는 SessionDriver (opencode 고유 어댑터).

    얇다 — 세션 생명주기/회전/marker 는 엔진 Supervisor 가 쥐고, 이 driver 는 한 turn 의
    opencode CLI 호출 + json 파싱만 한다(claude driver 동형).
    """

    def __init__(self, parse_opencode_json, *, ctx_budget: int | None = None,
                 precompact_at_pct: int | None = None, root: Path | None = None,
                 mark_stop=None, spawn_result=None, agent: str = DEFAULT_AGENT,
                 opencode_bin: str = OPENCODE_BIN, timeout: int = TURN_TIMEOUT_SEC,
                 runner=subprocess.run, stall_error=None, temp_root, attached_message,
                 assert_prompt_in_cwd) -> None:
        # parse_opencode_json 은 엔진 순수 헬퍼 주입(DI) — driver 가 파싱 로직을 중복 보유하지 않음.
        self._parse = parse_opencode_json
        self._ctx_budget = ctx_budget
        self._precompact_at_pct = precompact_at_pct
        self._root = Path(root) if root is not None else None
        # mark_stop = 엔진 mark_ctx_post_turn_if_over(root, sid, used, budget, precompact_at_pct).
        self._mark_stop = mark_stop
        # main 은 엔진 SpawnResult 를 명시 주입한다. 주입이 없으면 구조 호환 tuple 을 쓴다.
        self._spawn_result = spawn_result or (lambda sid, reply: (sid, reply))
        # 프롬프트는 cross 위임과 같이 `--dir` 안 파일로 `--file` 전달한다 — 자리(`raw_output.temp_root`)·
        # 첨부 안내 문구·containment 검사(`contract`)는 엔진 것을 쓴다(사본 없음). 세 값은 필수 주입이다.
        self._temp_root = temp_root
        self._attached_message = attached_message
        self._assert_prompt_in_cwd = assert_prompt_in_cwd
        self.agent = agent
        self.opencode_bin = opencode_bin
        self.timeout = timeout
        self.runner = runner  # subprocess.run seam(테스트 stub 가능).
        # 프로덕션 runner(엔진 run_relay 가 공급하는 첫 이벤트 워치독 실행기)가 stall 소진 시 던지는 엔진
        # StallWatchdogError 클래스(run_relay 가 주입). None 이면 빈 튜플 → `except ()` 는 아무것도
        # 안 잡는다(FakeRunner 주입 테스트 경로 무영향). fail-loud→turn-level fail-soft 로 수습.
        self._stall_error_types = (stall_error,) if stall_error is not None else ()
        # opencode 세션은 `-s <sid>` 로 어디서든 resume 되나(claude 의 cwd-scope 제약 없음),
        # `--dir` 로 child cwd 를 격리하므로 세션별 cwd 를 기억해 relay 에 재사용한다. 이건
        # *어댑터*-국소 세션 메타(opencode CLI 고유)지 relay 대화 상태가 아니다 —
        # 엔진 Supervisor 의 stateless 불변식은 그대로다.
        self._session_cwd: dict[str, str] = {}

    def spawn(self, cwd: str, session_id: str, bootstrap: str):
        """첫 세션 — `opencode run --agent <pm|build> --dir <cwd>` 로 bootstrap 전송.

        session_id 인자(엔진 uuid4)는 **무시** — opencode 가 sid 사전지정 불가라 출력에서
        파싱한 sid 를 권위로 반환한다. bootstrap reply 도 SpawnResult 로 보존한다."""
        observed, reply, used_tokens = self._turn(cwd, bootstrap, new_session=True)
        if not observed:
            # sid 파싱 실패 = 치명 — opencode 는 sid 사전지정 불가라 uuid4 로 폴백하면 그 세션이
            # *존재하지 않아* 다음 relay_turn 의 `-s <uuid>` 가 "Session not found" → 연속성
            # 침묵 파손(codex must-fix). 폴백 대신 명시 중단 — relay 는 유효
            # 세션 없이 못 돈다. (engine uuid4 인자는 opencode 경로에선 marker 예측에도 안 쓰인다.)
            raise RuntimeError(
                "[pm-orch] opencode 출력에서 sessionID 를 파싱하지 못했다 — 세션 구동 실패. "
                "(opencode 는 sid 사전지정 불가라 폴백 불가 · opencode/모델/agent 설정 확인.)"
            )
        self._session_cwd[observed] = cwd  # resume 이 같은 cwd(--dir)로 잇도록 기억.
        self._maybe_mark_ctx(observed, used_tokens)
        return self._spawn_result(observed, reply)

    def relay_turn(self, session_id: str, text: str) -> str:
        """기존 세션 resume — `opencode run -s <sid> --dir <cwd> --format json` 한 turn 중계."""
        cwd = self._session_cwd.get(session_id)
        _, reply, used_tokens = self._turn(cwd=cwd, prompt=text, session_id=session_id)
        self._maybe_mark_ctx(session_id, used_tokens)
        return reply or ""

    def close(self, session_id: str) -> None:
        """`opencode run` 1회성 turn 은 자동 exit(실측) — 명시 kill 불요. 세션 cwd 메타만 정리."""
        self._session_cwd.pop(session_id, None)

    # ── opencode CLI 한 turn ───────────────────────────────────────────────────

    def _turn(self, cwd, prompt, *, new_session=False, session_id=None):
        """비대화 opencode turn 1회. (observed_session_id, reply_text, used_tokens) 반환.

        - new_session=True: `--agent <agent>` 로 fresh 세션(opencode 가 sid 발급).
        - session_id 주어지면: `-s <sid>` 로 그 세션 resume.
        child cwd 격리 — `--dir <cwd>` 로 PM repo root 를 명시(엔진 제약).
        프롬프트는 실행 인자가 아니라 `--dir` 안 프롬프트 파일(`--file`)이다(cross 위임과 같다). 여러 줄
        프롬프트를 실행 인자에 실으면 Windows 에서 `opencode.cmd` 를 거칠 때 cmd.exe 가 첫 줄 뒤를 버린다."""
        sandbox = Path(cwd if cwd is not None else os.getcwd())
        prompt_dir = Path(tempfile.mkdtemp(prefix="pm-orch-opencode-", dir=self._temp_root(sandbox)))
        try:
            prompt_file = prompt_dir / "prompt.md"
            prompt_file.write_text(prompt, encoding="utf-8", newline="\n")
            self._assert_prompt_in_cwd(sandbox, prompt_file)
            cmd = [self.opencode_bin, "run", self._attached_message, "--file", str(prompt_file),
                   "--format", "json"]
            if new_session:
                cmd += ["--agent", self.agent]
            if session_id:
                cmd += ["-s", session_id]
            if cwd is not None:
                cmd += ["--dir", cwd]  # child cwd 격리(PM repo root).
            return self._run_turn(cmd)
        finally:
            shutil.rmtree(prompt_dir)

    def _run_turn(self, cmd):
        """한 turn 실행·진단·파싱 — (observed_session_id, reply_text, used_tokens)."""
        try:
            completed = self.runner(
                cmd, capture_output=True, text=True, timeout=self.timeout,
                # relay supervisor 의 파이프 stdin 을 child 가 삼키지 않게 즉시 EOF 로 격리.
                stdin=subprocess.DEVNULL,
            )
        except self._stall_error_types as exc:
            # 첫-이벤트 stall 재시도 소진 = fail-loud. 무한 hang(startup network fetch
            # stall) 대신 유한 재시도 후 여기 도달 → loud stderr + turn-level fail-soft
            # (relay 루프는 살아 다음 입력을 받는다·기존 timeout/OSError 처리와 동일 결).
            sys.stderr.write(f"[pm-orch] {exc}\n")
            return None, None, None
        except subprocess.TimeoutExpired:
            sys.stderr.write(f"[pm-orch] opencode turn timeout ({self.timeout}s)\n")
            return None, None, None
        except OSError as exc:
            sys.stderr.write(f"[pm-orch] opencode 실행 실패: {exc}\n")
            return None, None, None

        # 실패를 조용한 빈 응답으로 삼키지 않는다 — 최소 진단을 stderr 로(stdout=PM 대화 채널 보존).
        if getattr(completed, "returncode", 0):
            tail = (completed.stderr or "").strip().splitlines()[-1:] or [""]
            sys.stderr.write(f"[pm-orch] opencode rc={completed.returncode}: {tail[0]}\n")
        elif not (completed.stdout or ""):
            sys.stderr.write("[pm-orch] opencode turn 무출력(rc 0) — stdin/파싱 점검\n")

        lines = (completed.stdout or "").splitlines()
        return self._parse(lines)

    def _maybe_mark_ctx(self, session_id: str, used_tokens: int | None) -> None:
        """usage 신호와 DI가 모두 있으면 엔진 post-turn 예산 판정을 호출한다."""
        if any(value is None for value in (
            used_tokens, self._ctx_budget, self._precompact_at_pct, self._root, self._mark_stop,
        )):
            return
        self._mark_stop(
            self._root, session_id, used_tokens, self._ctx_budget, self._precompact_at_pct,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="opencode PM relay — thin stateless supervisor 세션 자동-회전."
    )
    parser.add_argument(
        "--cwd", default=None,
        help="child PM 세션의 작업 디렉토리(기본 = 현재 dir). PM repo root 여야 한다.",
    )
    parser.add_argument(
        "--agent", default=DEFAULT_AGENT,
        help=f"opencode agent(기본 {DEFAULT_AGENT}=PM primary). custom primary 부재 시 build 폴백.",
    )
    parser.add_argument(
        "--task", default=None, metavar="이름",
        help="task 정체성(필수). 회전된 새 PM 세션의 재진입 프롬프트에 `--task <task>` 실값을 "
             "박아 같은 task 를 이어가게 한다(명시 전달·cwd 추론 금지).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.task:
        # relay 는 회전된 세션을 `/pm-bootstrap --task <task>` 로 다시 연다 — 부트스트랩이 `--task`
        # 필수라 task 없는 relay 는 시작하지 않는다(claude·codex 런처와 같은 자리·같은 문구).
        parser.error("relay 에는 --task <task> 가 필요하다")
    root = repo_root(Path(__file__).resolve().parent)
    tools = str(root / ".project_manager" / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    from pm_engine.cli import hooks

    cwd = args.cwd or os.getcwd()

    def announced_driver(*driver_args, **driver_kwargs) -> OpencodeCliDriver:
        # 시작 안내는 엔진이 예산 기록을 확인하고 driver 를 구성한 뒤에 낸다(설정 오류가 먼저 보인다).
        driver = OpencodeCliDriver(*driver_args, **driver_kwargs)
        sys.stderr.write(
            f"[pm-orch] opencode supervisor 시작 (cwd={cwd} agent={args.agent}). "
            "ctx 한계 도달 시 자동 회전. 종료 = /quit 또는 EOF.\n"
        )
        sys.stderr.flush()
        return driver

    # 압축 한도 기록이 없거나 손상·입력 불일치·구표기 local.conf 면 시작하지 않는다. `opencode run --format
    # json` 은 모델을 내지 않아 세션 모델은 확인하지 않는다 — 기록 E 는 대상 모델 전체의 최솟값이다.
    return hooks.run_relay(
        hooks.adapter_home(root), announced_driver, harness="opencode", task=args.task, cwd=cwd,
        agent=args.agent, stdin=sys.stdin, stdout=sys.stdout,
    )


if __name__ == "__main__":
    raise SystemExit(main())
