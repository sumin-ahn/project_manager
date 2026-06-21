#!/usr/bin/env python3
"""opencode relay driver — `opencode run` subprocess 세션 구동 (ADR-0009 · 어댑터·얇음).

엔진 core(루트 `.project_manager/tools/pm_relay.py`)의 SessionDriver Protocol
구현체. relay/respawn/marker 로직은 *엔진* Supervisor 에 있고(루트 `.project_manager/tools/`·
DI 로 테스트), 이 파일은 **opencode CLI 고유**한 부분만 — `opencode run --format json` 을
subprocess 로 호출하고 json 이벤트 스트림을 파싱한다(claude `pm_orch_claude.py` 와 동형).

CLI 진입점: `python3 pm_orch_opencode.py [--cwd <PM repo root>] [--agent pm]`.
사용자가 이 wrapper 를 띄우면 ctx 한계 도달 시 손 안 대고 새 PM 으로 자동 회전(연속 운영).

opencode sid 발급(claude 와 다른 핵심): claude 는 `--session-id <uuid>` 로 child 의 세션 id 를
*지정* 하지만, opencode 는 `opencode run -s <없는id>` → "Session not found"(실측) — sid 사전
지정 불가다. 대신 `--format json` 모든 이벤트에 `sessionID` 가 실리므로(실측) **출력에서 sid 를
파싱해 획득** 한다. 엔진이 발급한 uuid4 session_id 인자는 **무시** 한다(opencode 가 발급한 sid 가
권위 — 그 sid 로 ctx-guard.js plugin 이 marker 를 쓴다 → supervisor 가 그 marker 를 stat).

opencode 어댑터는 claude 와 달리 옆에 Python `ctx_guard` 모듈이 없다(ctx-guard 는 JS plugin) —
그래서 엔진 루트 탐색을 driver 자체에 둔다(JS `findEngineRoot` 와 동일 규칙·동형 어댑터).
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

OPENCODE_BIN = "opencode"
DEFAULT_AGENT = "pm"          # T-0045 PM primary spawn 타깃. build 폴백(`--agent build`).
TURN_TIMEOUT_SEC = 600        # subprocess 당 hard hang 가드(상한 — 한 turn 이 길 수 있음).


def repo_root(start: Path) -> Path:
    """driver 위치(.opencode/)에서 엔진 루트를 찾는다 — JS `findEngineRoot` 와 동일 규칙.

    `.project_manager/tools/pm_handoff.py` 가 있는 가장 가까운 조상을 루트로 본다(ctx-guard.js
    의 루트 탐색과 일치 — 같은 어댑터의 일관). 없으면 start 의 부모(.opencode/ → 루트)."""
    start = start.resolve()
    for cand in (start, *start.parents):
        if (cand / ".project_manager" / "tools" / "pm_handoff.py").exists():
            return cand
    return start.parents[0] if start.parents else start


def _load_engine():
    """루트 `.project_manager/tools/pm_relay.py`(엔진 core)를 importlib 로 로드.

    어댑터는 엔진을 PYTHONPATH 에 의존하지 않고 repo_root 기준 경로로 직접 로드한다
    (claude `pm_orch_claude._load_engine` 동형). Supervisor·parse_opencode_json 을 빌려 쓴다.
    """
    root = repo_root(Path(__file__).resolve().parent)
    engine_path = root / ".project_manager" / "tools" / "pm_relay.py"
    spec = importlib.util.spec_from_file_location("pm_relay", engine_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, root


class OpencodeCliDriver:
    """`opencode run` subprocess 로 PM 세션을 구동하는 SessionDriver (opencode 고유 어댑터).

    얇다 — 세션 생명주기/회전/marker 는 엔진 Supervisor 가 쥐고, 이 driver 는 한 turn 의
    opencode CLI 호출 + json 파싱만 한다(claude driver 동형).
    """

    def __init__(self, parse_opencode_json, *, agent: str = DEFAULT_AGENT,
                 opencode_bin: str = OPENCODE_BIN, timeout: int = TURN_TIMEOUT_SEC,
                 runner=subprocess.run) -> None:
        # parse_opencode_json 은 엔진 순수 헬퍼 주입(DI) — driver 가 파싱 로직을 중복 보유하지 않음.
        self._parse = parse_opencode_json
        self.agent = agent
        self.opencode_bin = opencode_bin
        self.timeout = timeout
        self.runner = runner  # subprocess.run seam(테스트 stub 가능).
        # opencode 세션은 `-s <sid>` 로 어디서든 resume 되나(claude 의 cwd-scope 제약 없음),
        # `--dir` 로 child cwd 를 격리하므로 세션별 cwd 를 기억해 relay 에 재사용한다. 이건
        # *어댑터*-국소 세션 메타(opencode CLI 고유)지 relay 대화 상태가 아니다 —
        # 엔진 Supervisor 의 stateless 불변식은 그대로다.
        self._session_cwd: dict[str, str] = {}

    def spawn(self, cwd: str, session_id: str, bootstrap: str) -> str:
        """첫 세션 — `opencode run --agent <pm|build> --dir <cwd>` 로 bootstrap 전송.

        session_id 인자(엔진 uuid4)는 **무시** — opencode 가 sid 사전지정 불가라 출력에서
        파싱한 sid 를 권위로 반환한다(ctx-guard.js plugin 도 그 sid 로 marker 를 쓴다)."""
        observed, _ = self._turn(cwd, bootstrap, new_session=True)
        if not observed:
            # sid 파싱 실패 = 치명 — opencode 는 sid 사전지정 불가라 uuid4 로 폴백하면 그 세션이
            # *존재하지 않아* 다음 relay_turn 의 `-s <uuid>` 가 "Session not found" → 연속성
            # 침묵 파손(codex T-0048 must-fix). 폴백 대신 명시 중단 — relay 는 유효
            # 세션 없이 못 돈다. (engine uuid4 인자는 opencode 경로에선 marker 예측에도 안 쓰인다.)
            raise RuntimeError(
                "[pm-orch] opencode 출력에서 sessionID 를 파싱하지 못했다 — 세션 구동 실패. "
                "(opencode 는 sid 사전지정 불가라 폴백 불가 · opencode/모델/agent 설정 확인.)"
            )
        self._session_cwd[observed] = cwd  # resume 이 같은 cwd(--dir)로 잇도록 기억.
        return observed

    def relay_turn(self, session_id: str, text: str) -> str:
        """기존 세션 resume — `opencode run -s <sid> --dir <cwd> --format json` 한 turn 중계."""
        cwd = self._session_cwd.get(session_id)
        _, reply = self._turn(cwd=cwd, prompt=text, session_id=session_id)
        return reply or ""

    def close(self, session_id: str) -> None:
        """`opencode run` 1회성 turn 은 자동 exit(실측) — 명시 kill 불요. 세션 cwd 메타만 정리."""
        self._session_cwd.pop(session_id, None)

    # ── opencode CLI 한 turn ───────────────────────────────────────────────────

    def _turn(self, cwd, prompt, *, new_session=False, session_id=None):
        """비대화 opencode turn 1회. (observed_session_id, reply_text) 반환.

        - new_session=True: `--agent <agent>` 로 fresh 세션(opencode 가 sid 발급).
        - session_id 주어지면: `-s <sid>` 로 그 세션 resume.
        child cwd 격리 — `--dir <cwd>` 로 PM repo root 를 명시(엔진 제약 ①)."""
        cmd = [self.opencode_bin, "run", "--format", "json"]
        if new_session:
            cmd += ["--agent", self.agent]
        if session_id:
            cmd += ["-s", session_id]
        if cwd is not None:
            cmd += ["--dir", cwd]  # child cwd 격리(PM repo root).
        cmd.append(prompt)  # message positional 은 맨 끝.

        try:
            completed = self.runner(
                cmd, capture_output=True, text=True, timeout=self.timeout
            )
        except subprocess.TimeoutExpired:
            sys.stderr.write(f"[pm-orch] opencode turn timeout ({self.timeout}s)\n")
            return None, None
        except OSError as exc:
            sys.stderr.write(f"[pm-orch] opencode 실행 실패: {exc}\n")
            return None, None

        # 실패를 조용한 빈 응답으로 삼키지 않는다 — 최소 진단을 stderr 로(stdout=PM 대화 채널 보존).
        if getattr(completed, "returncode", 0):
            tail = (completed.stderr or "").strip().splitlines()[-1:] or [""]
            sys.stderr.write(f"[pm-orch] opencode rc={completed.returncode}: {tail[0]}\n")

        lines = (completed.stdout or "").splitlines()
        return self._parse(lines)


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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    engine, root = _load_engine()

    cwd = args.cwd or os.getcwd()
    driver = OpencodeCliDriver(engine.parse_opencode_json, agent=args.agent)
    supervisor = engine.Supervisor(driver, root=root)

    sys.stderr.write(
        f"[pm-orch] opencode supervisor 시작 (cwd={cwd} agent={args.agent}). "
        "ctx 한계 도달 시 자동 회전. 종료 = /quit 또는 EOF.\n"
    )
    sys.stderr.flush()
    return supervisor.run_loop(cwd, sys.stdin, sys.stdout)


if __name__ == "__main__":
    raise SystemExit(main())
