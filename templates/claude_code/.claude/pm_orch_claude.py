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
import importlib.util
import os
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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
