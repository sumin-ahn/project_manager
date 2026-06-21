#!/usr/bin/env python3
"""claude relay driver — `claude -p` subprocess 세션 구동 (ADR-0009 · 어댑터·얇음).

엔진 core(`pm_relay.py`)의 SessionDriver Protocol 구현체. relay/respawn/marker 로직은
*엔진* 에 있고(루트 `.project_manager/tools/`·DI 로 테스트), 이 파일은 **claude CLI 고유**한
부분만 — `claude -p [--session-id <uuid>|--resume <uuid>] --output-format stream-json` 을
subprocess 로 호출하고 stream-json 을 파싱한다(PoC `run_turn` 골격 재사용).

CLI 진입점: `python3 pm_orch_claude.py [--cwd <PM repo root>] [--model opus]`.
사용자가 이 wrapper 를 띄우면 ctx 한계 도달 시 손 안 대고 새 PM 으로 자동 회전(연속 운영).

결정적 `--session-id`: 엔진이 uuid4 발급 → 첫 spawn 은 `--session-id <uuid>` 로 child 의 세션
id 를 *지정* → ctx_stop_hook 이 그 uuid 로 marker(`<uuid>.done`)를 쓰므로 supervisor 가 marker
파일명을 예측한다. resume 은 `--resume <uuid>`. (sid 예측 가능성은 통합 스모크에서 실측.)

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
DEFAULT_MODEL = "claude-haiku-4-5"  # frugal 기본. CLI `--model` 로 override(opus 등).
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

    def __init__(self, parse_stream_json, *, model: str = DEFAULT_MODEL,
                 claude_bin: str = CLAUDE_BIN, timeout: int = TURN_TIMEOUT_SEC,
                 runner=subprocess.run) -> None:
        # parse_stream_json 은 엔진 순수 헬퍼 주입(DI) — driver 가 파싱 로직을 중복 보유하지 않음.
        self._parse = parse_stream_json
        self.model = model
        self.claude_bin = claude_bin
        self.timeout = timeout
        self.runner = runner  # subprocess.run seam(테스트 stub 가능).
        # claude 의 세션 저장은 **cwd-scoped** — `--resume` 는 spawn 과 같은 cwd 에서만 그 세션을
        # 찾는다(다른 cwd 면 "No conversation found"). 따라서 driver 가 세션별 cwd 를 기억해
        # resume 에 재사용한다. 이건 *어댑터*-국소 세션 메타(claude CLI 고유)지 relay
        # 대화 상태가 아니다 — 엔진 Supervisor 의 stateless 불변식은 그대로다.
        self._session_cwd: dict[str, str] = {}

    def spawn(self, cwd: str, session_id: str, bootstrap: str) -> str:
        """첫 세션 — `--session-id <uuid>` 로 세션 id 지정 + bootstrap 프롬프트 전송.

        반환 = stream-json 에서 관측된 실제 session_id(보통 입력 session_id 와 같음 —
        marker 예측의 핵심 가정. 다르면 hook 환원 경로용으로 관측값을 따른다)."""
        observed, _ = self._turn(cwd, bootstrap, session_id=session_id)
        sid = observed or session_id
        self._session_cwd[sid] = cwd  # resume 이 같은 cwd 에서 세션을 찾도록 기억.
        return sid

    def relay_turn(self, session_id: str, text: str) -> str:
        """기존 세션 resume — `--resume <uuid>` 로 한 turn 중계하고 reply 반환.

        claude 세션은 cwd-scoped 라 spawn 때의 cwd 에서 resume 해야 한다(없으면 현재 dir)."""
        cwd = self._session_cwd.get(session_id)
        _, result = self._turn(cwd=cwd, prompt=text, resume=session_id)
        return result or ""

    def close(self, session_id: str) -> None:
        """`-p` 1회성 turn 은 자동 exit(PoC 확증) — 명시 kill 불요. 세션 cwd 메타만 정리."""
        self._session_cwd.pop(session_id, None)

    # ── claude CLI 한 turn (PoC run_turn 골격) ─────────────────────────────────

    def _turn(self, cwd, prompt, *, session_id=None, resume=None):
        """비대화 claude turn 1회. (observed_session_id, result_text) 반환.

        child cwd 격리 — subprocess cwd 를 PM repo root 로 명시(엔진 제약 ①). resume 은
        같은 세션을 cwd 인자 없이 잇는다(claude 가 세션에 cwd 를 묶음)."""
        cmd = [self.claude_bin, "-p", prompt,
               "--output-format", "stream-json", "--verbose",
               "--model", self.model]
        if session_id:
            cmd += ["--session-id", session_id]
        if resume:
            cmd += ["--resume", resume]

        run_kwargs = dict(capture_output=True, text=True, timeout=self.timeout)
        if cwd is not None:
            run_kwargs["cwd"] = cwd  # child cwd 격리(PM repo root).
        try:
            completed = self.runner(cmd, **run_kwargs)
        except subprocess.TimeoutExpired:
            sys.stderr.write(f"[pm-orch] claude turn timeout ({self.timeout}s)\n")
            return None, None
        except OSError as exc:
            sys.stderr.write(f"[pm-orch] claude 실행 실패: {exc}\n")
            return None, None

        # 실패를 조용한 빈 응답으로 삼키지 않는다 — 최소 진단을 stderr 로(stdout=PM 대화 채널 보존).
        if getattr(completed, "returncode", 0):
            tail = (completed.stderr or "").strip().splitlines()[-1:] or [""]
            sys.stderr.write(f"[pm-orch] claude rc={completed.returncode}: {tail[0]}\n")

        lines = (completed.stdout or "").splitlines()
        return self._parse(lines)


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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    engine, root = _load_engine()

    cwd = args.cwd or os.getcwd()
    driver = ClaudeCliDriver(engine.parse_stream_json, model=args.model)
    supervisor = engine.Supervisor(driver, root=root)

    sys.stderr.write(
        f"[pm-orch] claude supervisor 시작 (cwd={cwd} model={args.model}). "
        "ctx 한계 도달 시 자동 회전. 종료 = /quit 또는 EOF.\n"
    )
    sys.stderr.flush()
    return supervisor.run_loop(cwd, sys.stdin, sys.stdout)


if __name__ == "__main__":
    raise SystemExit(main())
