#!/usr/bin/env python3
"""codex relay driver — `codex exec` subprocess 세션 구동 (ADR-0009 · ADR-0070 · 어댑터·얇음).

엔진 core(루트 `.project_manager/tools/pm_relay.py`)의 SessionDriver Protocol 구현체. relay/
respawn/marker 로직은 *엔진* Supervisor 에 있고(루트 `.project_manager/tools/`·DI 로 테스트),
이 파일은 **codex CLI 고유**한 부분만 — `codex exec --json` 을 subprocess 로 호출하고 JSONL
이벤트 스트림을 파싱한다(opencode `pm_orch_opencode.py` 와 동형·claude 와는 세션 id 획득 방식만 다름).

CLI 진입점: `python3 pm_orch_codex.py [--cwd <PM repo root>] [--task <이름>]`.
사용자가 이 wrapper 를 띄우면 ctx 한계 도달 시 손 안 대고 새 PM 으로 자동 회전(연속 운영).

codex thread_id 발급(claude 와 다른 핵심·opencode 동형): claude 는 `--session-id <uuid>` 로 child
세션 id 를 *지정* 하지만, codex 는 사전지정 불가 — `codex exec --json` 첫 이벤트 `thread.started`
의 `thread_id` 를 **출력 파싱으로 획득** 한다. 엔진이 발급한 uuid4 session_id 인자는 **무시**
(codex 가 발급한 thread_id 가 권위 — 그 tid 로 driver 가 ctx marker 를 쓰고 supervisor 가 stat).
이어가기 = `codex exec resume <thread_id>`.

⚠ stdin close 필수: `codex exec` 는 stdin 이 안 닫히면 "Reading additional input from stdin..."
로 **무기한 대기** 한다(라이브 실측·spike §D3·3m timeout 재현) — 매 turn subprocess 에
`stdin=subprocess.DEVNULL` 을 준다. 미준수 시 relay 가 첫 turn 에서 영원히 멈춘다.

ctx 기계 가드(ADR-0070 D4 ①·ADR-0041): opencode 는 JS plugin 이 ctx-STOP marker 를 쓰지만 codex
relay 경로엔 그 채널이 없다 — driver 가 `turn.completed.usage` 로 예산 초과를 직접 판정해 **post-turn**
stop marker 를 박제한다(엔진 `write_post_turn_marker` DI·Supervisor 무수정 회전). ⚠ 이 marker 는 turn
*실행 후* 박제라 opencode/claude 의 pre-turn(입력 차단) marker 와 의미가 다르다 — Supervisor 는
post-turn marker 에선 그 입력을 **재전송하지 않는다**(이미 실행된 turn 의 이중 실행 방지·codex R2·
엔진 `stop_marker_is_post_turn` 이 payload sentinel 로 구분). 예산 = local.conf
`ctx_window_tokens_codex` > generic `ctx_window_tokens` > 200000(ADR-0041 per-harness precedence).

codex 어댑터는 claude 와 달리 옆에 Python `ctx_guard` 모듈이 없다(claude=`.claude/ctx_guard.py`·
opencode=JS core) — 그래서 엔진 루트 탐색·local.conf 파싱을 driver 자체에 둔다(opencode driver 동형).
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

CODEX_BIN = "codex"
TURN_TIMEOUT_SEC = 600  # subprocess 당 hard hang 가드(상한 — 한 turn 이 길 수 있음·codex reasoning).

# ── ctx 기계 가드 상수 (ADR-0070 D4 ①·ADR-0041 — 엔진 ctx_guard 미러) ──────────
# 엔진 ctx-guard 는 "잔여 <= ctx_stop_pct" 에서 정지한다 — relay 경로엔 plugin 채널이 없어 driver 가
# usage 로 그 정지점을 직접 미러한다. 임계는 local.conf `ctx_stop_pct` override 해소(기본 20·아래
# resolve_stop_pct·claude ctx_guard.ctx_thresholds 대칭). 잔여 20% 정지 ⟺ 사용률 80%.
CTX_STOP_PCT_DEFAULT = 20  # 잔여 정지 임계(%) — claude ctx_guard.CTX_STOP_PCT_DEFAULT 미러.
# ctx 예산(분모) 최종 폴백 — local.conf ctx_window_tokens_<codex|generic> 미설정 시(ADR-0041).
CTX_WINDOW_TOKENS_DEFAULT = 200_000


def repo_root(start: Path) -> Path:
    """driver 위치(.codex/)에서 엔진 루트를 찾는다 — opencode `repo_root` 동형.

    `.project_manager/tools/pm_handoff.py` 가 있는 가장 가까운 조상을 루트로 본다(같은 어댑터의
    일관). 없으면 start 의 부모(.codex/ → 루트)."""
    start = start.resolve()
    for cand in (start, *start.parents):
        if (cand / ".project_manager" / "tools" / "pm_handoff.py").exists():
            return cand
    return start.parents[0] if start.parents else start


def _load_engine():
    """루트 `.project_manager/tools/pm_relay.py`(엔진 core)를 importlib 로 로드 (opencode 동형).

    어댑터는 엔진을 PYTHONPATH 에 의존하지 않고 repo_root 기준 경로로 직접 로드한다.
    Supervisor·parse_codex_json·_marker_path 를 빌려 쓴다."""
    root = repo_root(Path(__file__).resolve().parent)
    engine_path = root / ".project_manager" / "tools" / "pm_relay.py"
    spec = importlib.util.spec_from_file_location("pm_relay", engine_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, root


# ── local.conf 직접 파싱 + ctx 예산 해소 (claude ctx_guard 미러·codex 는 옆에 ctx_guard 없음) ──

def load_local_config(root: Path) -> dict[str, str]:
    """`.project_manager/local.conf` 를 KEY=value dict 로 (없으면 {}).

    claude `ctx_guard.load_local_config` 미러 — codex driver 는 엔진/claude 어댑터를 import 하지
    않으므로 같은 파싱을 작게 재현한다(`#` 주석·빈 줄·`=` 없는 줄 skip)."""
    conf: dict[str, str] = {}
    path = root / ".project_manager" / "local.conf"
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return conf
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        conf[key.strip()] = val.strip()
    return conf


def resolve_ctx_budget(conf: dict[str, str]) -> int:
    """ctx 예산(분모)을 per-harness precedence 로 해소 (ADR-0041 Decision 1).

    `ctx_window_tokens_codex` > generic `ctx_window_tokens` > CTX_WINDOW_TOKENS_DEFAULT(200000).
    각 층 >0 정수 sanity — ≤0·비정수·미설정이면 다음 층 폴백(0/음수 특수의미 없음). claude
    ctx_guard.resolve_budget(conf,"codex")·opencode resolveBudget 동형(하네스별 키 완전 독립)."""
    for key in ("ctx_window_tokens_codex", "ctx_window_tokens"):
        raw = conf.get(key)
        if raw is None:
            continue
        try:
            size = int(str(raw).strip())
        except (ValueError, AttributeError):
            continue
        if size > 0:
            return size
    return CTX_WINDOW_TOKENS_DEFAULT


def resolve_stop_pct(conf: dict[str, str]) -> int:
    """ctx 정지 임계(잔여 %)를 conf `ctx_stop_pct` 로 해소 — 없으면/비정상이면 기본 20.

    claude `ctx_guard.ctx_thresholds` 의 stop 축과 대칭(sanity: 0 < stop < 100·위반 시 기본 폴백).
    relay 기계 가드는 정지만 판정하므로 nudge 축은 불요 — driver 는 이 하나로 예산 정지점을 잡는다."""
    raw = conf.get("ctx_stop_pct")
    if raw is None:
        return CTX_STOP_PCT_DEFAULT
    try:
        stop = int(str(raw).strip())
    except (ValueError, AttributeError):
        return CTX_STOP_PCT_DEFAULT
    return stop if 0 < stop < 100 else CTX_STOP_PCT_DEFAULT


class CodexCliDriver:
    """`codex exec` subprocess 로 PM 세션을 구동하는 SessionDriver (codex 고유 어댑터).

    얇다 — 세션 생명주기/회전/marker 는 엔진 Supervisor 가 쥐고, 이 driver 는 한 turn 의 codex
    CLI 호출 + JSONL 파싱 + (relay 경로 전용) usage 기계 ctx 가드만 한다(opencode driver 동형에
    driver-side ctx marker 를 더한 형태 — codex 엔 plugin marker 채널이 없어서다·ADR-0070 D4 ①).
    """

    def __init__(self, parse_codex_json, *, ctx_budget: int | None = None,
                 stop_pct: int = CTX_STOP_PCT_DEFAULT, mark_stop=None,
                 codex_bin: str = CODEX_BIN, timeout: int = TURN_TIMEOUT_SEC,
                 runner=subprocess.run, root: Path | None = None) -> None:
        # parse_codex_json 은 엔진 순수 헬퍼 주입(DI) — driver 가 파싱 로직을 중복 보유하지 않음.
        self._parse = parse_codex_json
        # mark_stop 은 엔진 `write_post_turn_marker`(root, sid)->bool 주입(DI) — marker payload/경로
        # 계약을 엔진이 소유하고 driver 는 예산 판정 후 트리거만 한다. post-turn 표식이라 Supervisor 가
        # 재전송 없이 회전한다(이미 실행된 turn 의 이중 실행 방지·codex R2). None 이면 가드 no-op.
        self._mark_stop = mark_stop
        self._ctx_budget = ctx_budget
        self._stop_pct = stop_pct  # 잔여 정지 임계(%) — main 이 local.conf ctx_stop_pct 로 해소해 주입.
        self._root = Path(root) if root is not None else None
        self.codex_bin = codex_bin
        self.timeout = timeout
        self.runner = runner  # subprocess.run seam(테스트 stub 가능).
        # codex 세션은 `resume <tid>` 로 이어가되 `-C <cwd>` 로 child cwd 를 격리하므로 세션별 cwd 를
        # 기억해 relay 에 재사용한다. 어댑터-국소 세션 메타(codex CLI 고유)지 relay 대화 상태가
        # 아니다 — 엔진 Supervisor 의 stateless 불변식은 그대로다(opencode _session_cwd 대칭).
        self._session_cwd: dict[str, str] = {}

    def spawn(self, cwd: str, session_id: str, bootstrap: str) -> str:
        """첫 세션 — `codex exec --json` 으로 bootstrap 전송, `thread.started.thread_id` 반환.

        session_id 인자(엔진 uuid4)는 **무시** — codex 는 thread_id 사전지정 불가라 출력에서
        파싱한 thread_id 를 권위로 반환한다(그 tid 로 driver 가 ctx marker 를 쓰고 supervisor 가
        stat)."""
        tid, _reply, usage = self._turn(cwd, bootstrap)
        if not tid:
            # thread_id 파싱 실패 = 치명 — codex 는 tid 사전지정 불가라 uuid4 폴백 시 `resume <uuid>`
            # 가 존재하지 않는 세션을 가리켜 연속성 침묵 파손(opencode sid-fail 동형·resume 불가).
            # 폴백 대신 명시 중단 — relay 는 유효 세션 없이 못 돈다.
            raise RuntimeError(
                "[pm-orch] codex 출력에서 thread.started.thread_id 를 파싱하지 못했다 — "
                "세션 구동 실패. (codex 는 thread_id 사전지정 불가라 폴백 불가 · codex/모델 설정 확인.)"
            )
        self._session_cwd[tid] = cwd  # resume 이 같은 cwd(-C)로 잇도록 기억.
        self._maybe_mark_ctx(tid, usage)  # ADR-0070 D4 ① driver-side 기계 ctx 가드.
        return tid

    def relay_turn(self, session_id: str, text: str) -> str:
        """기존 세션 resume — `codex exec resume <tid> -C <cwd> --json` 한 turn 중계."""
        cwd = self._session_cwd.get(session_id)
        _tid, reply, usage = self._turn(cwd, text, resume_id=session_id)
        self._maybe_mark_ctx(session_id, usage)  # ADR-0070 D4 ① driver-side 기계 ctx 가드.
        return reply or ""

    def close(self, session_id: str) -> None:
        """`codex exec` 1회성 turn 은 자동 exit — 명시 kill 불요. 세션 cwd 메타만 정리."""
        self._session_cwd.pop(session_id, None)

    # ── codex CLI 한 turn ───────────────────────────────────────────────────────

    def _turn(self, cwd, prompt, *, resume_id=None):
        """비대화 codex turn 1회. (thread_id, reply, usage) 반환.

        - resume_id 없으면: fresh `codex exec`(codex 가 thread_id 발급).
        - resume_id 주어지면: `resume <tid>` 로 그 세션 이어감.
        child cwd 격리 — `-C <cwd>` 로 PM repo root 를 명시(opencode `--dir` 대칭·엔진 제약 ①).
        커맨드 형 = `codex exec --json -s workspace-write --skip-git-repo-check [-C <cwd>]
        [resume <tid>] <prompt>` (티켓 명세 순). `-C` 는 exec-레벨 플래그라 `resume` 서브커맨드
        *앞*에 둔다 — resume 뒤에 두면 resume 이 -C 를 거부할 때 cwd 격리가 파손된다.
        (resume+-C 실효는 T-0407 라이브 확인 전제.)
        sandbox 는 `-s workspace-write` 로 **명시 핀**(codex R4) — PM relay 세션은 파일 수정/테스트
        실행이 핵심이라, 사용자 전역 config 가 read-only 면 실작업이 막히고 더 느슨하면 안전 경계가
        흔들린다. fill 경로(pm_import)와 동일 핀·spawn/resume 양쪽 공통."""
        cmd = [self.codex_bin, "exec", "--json", "-s", "workspace-write", "--skip-git-repo-check"]
        if cwd is not None:
            cmd += ["-C", cwd]  # exec-레벨 workdir 핀(resume 앞·child cwd 격리·PM repo root).
        if resume_id:
            cmd += ["resume", resume_id]  # codex exec resume <thread_id>.
        cmd.append(prompt)  # PROMPT positional 은 맨 끝.

        try:
            completed = self.runner(
                cmd, capture_output=True, text=True, timeout=self.timeout,
                # ⚠⚠ stdin close 필수 — 미닫힘 시 codex 가 "Reading additional input from
                # stdin..." 로 무기한 대기(라이브 실측·spike §D3·3m timeout 재현).
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            sys.stderr.write(f"[pm-orch] codex turn timeout ({self.timeout}s)\n")
            return None, None, None
        except OSError as exc:
            sys.stderr.write(f"[pm-orch] codex 실행 실패: {exc}\n")
            return None, None, None

        # 실패를 조용한 빈 응답으로 삼키지 않는다 — 최소 진단을 stderr 로(stdout=PM 대화 채널 보존).
        if getattr(completed, "returncode", 0):
            tail = (completed.stderr or "").strip().splitlines()[-1:] or [""]
            sys.stderr.write(f"[pm-orch] codex rc={completed.returncode}: {tail[0]}\n")

        return self._parse((completed.stdout or "").splitlines())

    # ── driver-side ctx 기계 가드 (ADR-0070 D4 ①·relay 경로 전용) ─────────────────

    def _maybe_mark_ctx(self, session_id: str, usage) -> None:
        """turn usage 가 ctx 예산 정지점(잔여 <= stop_pct)에 도달하면 **post-turn** stop marker 를 박제한다.

        opencode 는 plugin 이 (입력 처리 전) marker 를 쓰지만 codex relay 경로엔 그 채널이 없어 driver 가
        turn.completed 후 예산 초과를 판정한다(spike §3.4). turn 이 *이미 실행·응답됐으므로* 엔진
        `write_post_turn_marker` 로 post-turn 표식을 박제 → Supervisor 는 재전송 없이 회전한다(이중 실행
        방지·codex R2·엔진 `stop_marker_is_post_turn` 판정). 예산·usage·root·mark_stop 중 하나라도 없으면
        no-op(가드 비활성·부트/테스트 경로 무영향). 정지점 = 예산 × (100 - stop_pct)/100.

        사용 토큰 = input + output + reasoning_output. **cached_input 은 가산하지 않는다** — 실측
        (codex 0.144.6·PM 프로브 5회) input_tokens 가 cached_input_tokens 를 포함하는 상위집합
        (예 input_tokens=12481 ⊃ cached_input_tokens=9600)이라 cached 를 더하면 이중 계상이 된다.
        usage 는 parse_codex_json 이 wire(`*_tokens`)→contract 로 정규화한 dict(접미사 없는 키).
        ⚠ T-0407 watch-item: input_tokens 가 *누적* 컨텍스트를 반영하는지 per-turn 인지 라이브 확인 —
        엔진 예산 판정은 누적 점유 가정(per-turn 이면 누적 환산이 필요). marker 박제 실패는 fail-soft."""
        if not (self._ctx_budget and usage and self._root and self._mark_stop):
            return
        used = ((usage.get("input") or 0)
                + (usage.get("output") or 0)
                + (usage.get("reasoning_output") or 0))
        stop_threshold = self._ctx_budget * (100 - self._stop_pct) / 100
        if used >= stop_threshold:
            # post-turn 표식으로 박제 — Supervisor 가 재전송 없이 회전(이 turn 은 이미 실행·응답됨).
            if not self._mark_stop(self._root, session_id):  # 박제 실패는 fail-soft(relay 무중단).
                sys.stderr.write("[pm-orch] codex ctx marker 박제 실패\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="codex PM relay — thin stateless supervisor 세션 자동-회전."
    )
    parser.add_argument(
        "--cwd", default=None,
        help="child PM 세션의 작업 디렉토리(기본 = 현재 dir). PM repo root 여야 한다.",
    )
    parser.add_argument(
        "--task", default=None, metavar="이름",
        help="task 정체성(F7·T-0356) — 회전된 새 PM 세션의 재진입 프롬프트에 `--task <이름>` 실값을 "
             "박아 같은 task 를 resume 하게 한다((b) 명시 전달·cwd 추론 금지). 미지정이면 bare "
             "`$pm-bootstrap`(슬롯/솔로).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    engine, root = _load_engine()

    cwd = args.cwd or os.getcwd()
    conf = load_local_config(root)
    # driver-side ctx 가드 원천 — local.conf 예산·정지임계 해소 + 엔진 post-turn marker writer 주입.
    driver = CodexCliDriver(
        engine.parse_codex_json,
        ctx_budget=resolve_ctx_budget(conf),
        stop_pct=resolve_stop_pct(conf),
        mark_stop=engine.write_post_turn_marker,
        root=root,
    )
    supervisor = engine.Supervisor(driver, root=root, task=args.task)

    sys.stderr.write(
        f"[pm-orch] codex supervisor 시작 (cwd={cwd}). "
        "ctx 한계 도달 시 자동 회전. 종료 = /quit 또는 EOF.\n"
    )
    sys.stderr.flush()
    return supervisor.run_loop(cwd, sys.stdin, sys.stdout)


if __name__ == "__main__":
    raise SystemExit(main())
