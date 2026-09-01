#!/usr/bin/env python3
"""opencode relay driver — `opencode run` subprocess 세션 구동 (어댑터·얇음).

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
권위 — driver 가 그 sid 로 post-turn marker 를 쓰고 supervisor 가 stat).

opencode 어댑터는 claude 와 달리 옆에 Python `ctx_guard` 모듈이 없다(ctx-guard 는 JS plugin) —
그래서 엔진 루트 해소를 driver 자체에 둔다. 규칙은 JS 훅 코어의 `ENGINE_ROOT` 와 같다: 파일 자기
위치에서 고정 깊이로 받고 조상을 훑지 않는다(T-0889).
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

OPENCODE_BIN = "opencode"
DEFAULT_AGENT = "pm"          # PM primary spawn 타깃. build 폴백(`--agent build`).
TURN_TIMEOUT_SEC = 600        # subprocess 당 hard hang 가드(상한 — 한 turn 이 길 수 있음).
CTX_STOP_PCT_DEFAULT = 20     # 잔여 정지 임계(%).
CTX_WINDOW_TOKENS_DEFAULT = 200_000


def repo_root(start: Path) -> Path:
    """driver 위치(``<root>/.opencode/``)에서 엔진 루트를 낸다 — 그 부모다.

    어댑터 사본은 항상 ``<root>/.opencode/`` 에 설치되므로 루트는 그 자리의 함수다. 조상을
    훑어 ``.project_manager/tools/pm_handoff.py`` 를 찾으면 driver 가 자기 트리가 아니라 위에
    있는 남의 PM 홈에 착지한다(claude ctx_guard.repo_root·codex `repo_root` 동형).
    """
    return start.resolve().parent


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


# 차단 구키 목록은 엔진이 **생성**한다(어댑터가 매핑표를 복제하면 표와 파서가 갈린다):
#   python3 .project_manager/tools/local_conf.py --render-adapter-block python
# 생성 시작 — 차단 구키 (local_conf.render_adapter_block · 손편집 금지)
LEGACY_CONF_KEYS = (
    "additional_reviewer.enabled",
    "additional_reviewer_enabled",
    "additional_reviewer_incomplete_round_limit",
    "additional_reviewer_round_limit",
    "additional_reviewer_wave_budget",
    "ctx_nudge_pct",
    "ctx_stop_pct",
    "ctx_window_tokens",
    "date",
    "delegate_enabled",
    "delegate_idle_timeout",
    "delegate_timeout",
    "external_review_enabled",
    "external_review_idle_timeout",
    "external_review_incomplete_round_limit",
    "external_review_progress_signal",
    "external_review_round_limit",
    "external_review_timeout",
    "external_review_wave_budget",
    "opencode_pro_model",
    "project_name",
    "project_root",
    "project_tagline",
    "py",
    "regression_min_collected",
    "review_denylist_extra",
    "review_paths",
    "review_rounds_max",
    "reviewer_cmd",
    "reviewer_env_keep_extra",
    "reviewer_home_artifacts_extra",
    "test_cmd",
    "upstream",
    "upstream_rev",
    "upstream_seen_rev",
    "user",
)
LEGACY_CONF_KEY_PREFIX = "ctx_window_tokens_"
# 생성 끝 — 차단 구키


def _assert_no_legacy_conf(conf: dict[str, str], path: Path) -> None:
    """구표기 키가 남아 있으면 **값 해소 전에** 멈춘다 (조용한 기본값 강등 차단).

    어댑터는 엔진을 import 하지 않아 신표기 이름을 말하지 못한다 — 무엇이 걸렸는지만 말하고
    전수 지목은 엔진 도구(`board.py lint`·`pm_update.py` 안내)에 맡긴다. 여기서 강등하면 채택자는
    conf 를 고쳤는데 아무 일도 안 일어나는 상태(임계·예산이 전부 엔진 기본값)를 본다."""
    found = sorted(key for key in conf
                   if key in LEGACY_CONF_KEYS
                   or (key.startswith(LEGACY_CONF_KEY_PREFIX)
                       and len(key) > len(LEGACY_CONF_KEY_PREFIX)))
    if not found:
        return
    print(f"오류: local.conf 에 구표기 키가 남아 있습니다 ({path}) — "
          f"{', '.join(found)}. 값이 조용히 기본값으로 떨어지지 않도록 여기서 멈춥니다. "
          "전수 지목은 `board.py lint` 또는 `pm_update.py` 안내가 냅니다.", file=sys.stderr)
    raise SystemExit(1)


def load_local_config(root: Path) -> dict[str, str]:
    """`.project_manager/local.conf` 를 KEY=value dict 로 읽는다(없으면 {})."""
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
        key, _, value = line.partition("=")
        conf[key.strip()] = value.strip()
    _assert_no_legacy_conf(conf, path)
    return conf


def resolve_ctx_budget(conf: dict[str, str]) -> int:
    """`harness.opencode.ctx_window_tokens` > generic > 200000 순으로 예산을 해소한다."""
    for key in ("harness.opencode.ctx_window_tokens", "ctx.window_tokens"):
        raw = conf.get(key)
        if raw is None:
            continue
        try:
            budget = int(str(raw).strip())
        except (ValueError, AttributeError):
            continue
        if budget > 0:
            return budget
    return CTX_WINDOW_TOKENS_DEFAULT


def resolve_stop_pct(conf: dict[str, str]) -> int:
    """잔여 ctx 정지 %를 해소한다. 비정상 값은 기본 20으로 폴백한다."""
    raw = conf.get("ctx.stop_pct")
    if raw is None:
        return CTX_STOP_PCT_DEFAULT
    try:
        stop_pct = int(str(raw).strip())
    except (ValueError, AttributeError):
        return CTX_STOP_PCT_DEFAULT
    return stop_pct if 0 < stop_pct < 100 else CTX_STOP_PCT_DEFAULT


class OpencodeCliDriver:
    """`opencode run` subprocess 로 PM 세션을 구동하는 SessionDriver (opencode 고유 어댑터).

    얇다 — 세션 생명주기/회전/marker 는 엔진 Supervisor 가 쥐고, 이 driver 는 한 turn 의
    opencode CLI 호출 + json 파싱만 한다(claude driver 동형).
    """

    def __init__(self, parse_opencode_json, *, ctx_budget: int | None = None,
                 stop_pct: int | None = None, root: Path | None = None,
                 mark_stop=None, spawn_result=None, agent: str = DEFAULT_AGENT,
                 opencode_bin: str = OPENCODE_BIN, timeout: int = TURN_TIMEOUT_SEC,
                 runner=subprocess.run, stall_error=None) -> None:
        # parse_opencode_json 은 엔진 순수 헬퍼 주입(DI) — driver 가 파싱 로직을 중복 보유하지 않음.
        self._parse = parse_opencode_json
        self._ctx_budget = ctx_budget
        self._stop_pct = stop_pct
        self._root = Path(root) if root is not None else None
        # mark_stop = 엔진 mark_ctx_post_turn_if_over(root, sid, used, budget, stop_pct).
        self._mark_stop = mark_stop
        # main은 SpawnResult를 명시 주입한다. parser만 주입한 기존/단위 경로도 parser 소유
        # 엔진 타입을 찾아 쓰며, 제3자 parser면 구조 호환 tuple로 폴백한다.
        parser_globals = getattr(parse_opencode_json, "__globals__", {})
        self._spawn_result = (
            spawn_result or parser_globals.get("SpawnResult")
            or (lambda sid, reply: (sid, reply))
        )
        self.agent = agent
        self.opencode_bin = opencode_bin
        self.timeout = timeout
        self.runner = runner  # subprocess.run seam(테스트 stub 가능).
        # 프로덕션 runner(_make_watchdog_runner)가 첫-이벤트 stall 소진 시 던지는 엔진
        # StallWatchdogError 클래스(main 이 주입). None 이면 빈 튜플 → `except ()` 는 아무것도
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
        child cwd 격리 — `--dir <cwd>` 로 PM repo root 를 명시(엔진 제약)."""
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
            used_tokens, self._ctx_budget, self._stop_pct, self._root, self._mark_stop,
        )):
            return
        self._mark_stop(
            self._root, session_id, used_tokens, self._ctx_budget, self._stop_pct,
        )


def _make_watchdog_runner(engine):
    """프로덕션 기본 runner — 엔진 첫-이벤트 워치독으로 opencode 를 실행(startup stall→유한 재시도).

    driver 의 `runner` seam(테스트가 FakeRunner 주입)을 유지하면서, 프로덕션 main 만 이 runner 로
    현행 600s hard 가드(overall_timeout=self.timeout) *안쪽에* 첫-이벤트 감시를 더한다. capture_output/
    text kwargs 는 흡수(워치독이 항상 캡처·text). first_event/retries 는 env 노브(engine 해소기)로.
    StallWatchdogError 는 잡지 않고 올려보낸다 — driver `_turn` 이 loud stderr + fail-soft 로 수습."""
    def runner(cmd, *, capture_output=True, text=True, timeout=TURN_TIMEOUT_SEC, **_kwargs):
        return engine.run_with_first_event_watchdog(
            cmd,
            first_event_timeout=engine.first_event_timeout_default(),
            overall_timeout=timeout,
            retries=engine.stall_retries_default(),
        )
    return runner


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
        help="task 정체성 — 회전된 새 PM 세션의 재진입 프롬프트에 `--task <이름>` 실값을 "
             "박아 같은 task 를 resume 하게 한다((b) 명시 전달·cwd 추론 금지). 미지정이면 bare "
             "`/pm-bootstrap`(슬롯/솔로).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    engine, root = _load_engine()

    cwd = args.cwd or os.getcwd()
    conf = load_local_config(root)
    budget = resolve_ctx_budget(conf)
    stop_pct = resolve_stop_pct(conf)
    engine.validate_relay_budget(budget, stop_pct)
    # 프로덕션 driver 는 첫-이벤트 워치독 runner 로 opencode 를 구동(startup stall→유한 재시도).
    # 소진 시 StallWatchdogError → driver `_turn` 이 loud + fail-soft(stall_error 주입).
    driver = OpencodeCliDriver(
        engine.parse_opencode_json,
        ctx_budget=budget,
        stop_pct=stop_pct,
        root=root,
        mark_stop=engine.mark_ctx_post_turn_if_over,
        spawn_result=engine.SpawnResult,
        agent=args.agent,
        runner=_make_watchdog_runner(engine),
        stall_error=engine.StallWatchdogError,
    )
    supervisor = engine.Supervisor(driver, root=root, task=args.task)

    sys.stderr.write(
        f"[pm-orch] opencode supervisor 시작 (cwd={cwd} agent={args.agent}). "
        "ctx 한계 도달 시 자동 회전. 종료 = /quit 또는 EOF.\n"
    )
    sys.stderr.flush()
    return supervisor.run_loop(cwd, sys.stdin, sys.stdout)


if __name__ == "__main__":
    raise SystemExit(main())
