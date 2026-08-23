#!/usr/bin/env python3
"""codex relay driver — `codex exec` subprocess 세션 구동 (어댑터·얇음).

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
로 **무기한 대기** 한다(라이브 실측·3m timeout 재현) — 매 turn subprocess 에
`stdin=subprocess.DEVNULL` 을 준다. 미준수 시 relay 가 첫 turn 에서 영원히 멈춘다.

ctx 기계 가드: 세 하네스 모두 driver 가 usage 로 예산 초과를 판정해
**post-turn** 회전 marker 를 박제한다(엔진 `write_post_turn_marker` DI·Supervisor 무수정 회전).
marker 는 turn *실행 후* 박제 단일 의미론 — Supervisor 는 그 입력을 다시 보내지 않고 다음 입력 전
회전한다(세션 안 가드는 marker 를 만들지 않는다·비차단 안내 전용). 예산 = local.conf
`harness.codex.ctx_window_tokens` > generic `ctx.window_tokens` > 200000(per-harness precedence).

codex 어댑터는 claude 와 달리 옆에 Python `ctx_guard` 모듈이 없다(claude=`.claude/ctx_guard.py`·
opencode=JS core) — 그래서 엔진 루트 탐색·local.conf 파싱을 driver 자체에 둔다(opencode driver 동형).

세션 안 ctx 넛지: 위 relay 축과 별개로, 훅 진입점(`PreToolUse`·
`UserPromptSubmit`)에서 밴드에 들어가면 비차단 checkpoint 안내를 주입한다(회전 marker 미생산).
점유는 훅 stdin 의 `transcript_path` rollout JSONL 에서 읽고, 밴드·임계·문구는 claude 와 같은 값을
쓴다 — 압축이 시작된 뒤(PreCompact)에야 알려 주면 checkpoint 를 남길 여유가 없다는 것이 사유다.
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
import time
from pathlib import Path
from typing import NamedTuple

CODEX_BIN = "codex"
TURN_TIMEOUT_SEC = 600  # subprocess 당 hard hang 가드(상한 — 한 turn 이 길 수 있음·codex reasoning).

# ── ctx 기계 가드 상수 (엔진 ctx_guard 미러) ──────────
# 엔진 밴드 "잔여 <= ctx.stop_pct" 는 세션 안에서 최종 checkpoint 넛지(비차단)로 소비된다 — relay
# 경로는 그 같은 경계에서 driver 가 usage 로 **회전** 한다(post-turn marker → Supervisor 가 새 세션
# 으로 교체·실보호는 회전이 한다). 임계는 local.conf `ctx.stop_pct` override 해소(기본 20·아래
# resolve_stop_pct·claude ctx_guard.ctx_thresholds 대칭). 잔여 20% 회전 ⟺ 사용률 80%.
CTX_STOP_PCT_DEFAULT = 20  # 잔여 회전 임계(%) — claude ctx_guard.CTX_STOP_PCT_DEFAULT 미러.
# 세션 안 1단 넛지 임계(잔여 %) — claude ctx_guard.CTX_NUDGE_PCT_DEFAULT·opencode
# NUDGE_PCT_DEFAULT 미러(codex 는 네 번째 미러 사이트·tests/test_ctx_default_mirror.py).
# relay 회전 축은 stop 하나만 쓰고, 이 값은 아래 훅 축(`ctx_thresholds`)이 소비한다.
CTX_NUDGE_PCT_DEFAULT = 30
# 2단(strong) 넛지 마진(%p·파생값) — claude ctx_guard.CTX_NUDGE2_MARGIN_PCT·opencode
# NUDGE2_MARGIN_PCT 미러. nudge2 밴드 = stop_pct < 잔여 <= min(stop_pct + 이 마진, nudge_pct).
CTX_NUDGE2_MARGIN_PCT = 3
# ctx 예산(분모) 최종 폴백 — local.conf harness.codex.ctx_window_tokens/ctx.window_tokens 미설정 시.
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

# 차단 구키 목록은 엔진이 **생성**한다(어댑터가 매핑표를 복제하면 표와 파서가 갈린다):
#   python3 .project_manager/tools/local_conf.py --render-adapter-block python
# 생성 시작 — 차단 구키 (local_conf.render_adapter_block · 손편집 금지)
LEGACY_CONF_KEYS = (
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
    _assert_no_legacy_conf(conf, path)
    return conf


def resolve_ctx_budget(conf: dict[str, str]) -> int:
    """ctx 예산(분모)을 per-harness precedence 로 해소.

    `harness.codex.ctx_window_tokens` > generic `ctx.window_tokens` > CTX_WINDOW_TOKENS_DEFAULT(200000).
    각 층 >0 정수 sanity — ≤0·비정수·미설정이면 다음 층 폴백(0/음수 특수의미 없음). claude
    ctx_guard.resolve_budget(conf,"codex")·opencode resolveBudget 동형(하네스별 키 완전 독립)."""
    for key in ("harness.codex.ctx_window_tokens", "ctx.window_tokens"):
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
    """회전 임계(잔여 %)를 conf `ctx.stop_pct` 로 해소 — 없으면/비정상이면 기본 20.

    claude `ctx_guard.ctx_thresholds` 의 stop 축과 대칭(sanity: 0 < stop < 100·위반 시 기본 폴백).
    relay 기계 가드는 회전 시점만 판정하므로 nudge 축은 불요 — driver 는 이 하나로 회전 경계를 잡는다."""
    raw = conf.get("ctx.stop_pct")
    if raw is None:
        return CTX_STOP_PCT_DEFAULT
    try:
        stop = int(str(raw).strip())
    except (ValueError, AttributeError):
        return CTX_STOP_PCT_DEFAULT
    return stop if 0 < stop < 100 else CTX_STOP_PCT_DEFAULT


def _resolve_rollout_file(session_id: str) -> Path | None:
    """CODEX_HOME 아래에서 thread id가 든 최신 rollout 파일을 찾는다(fail-soft)."""
    if not session_id:
        return None
    codex_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    sessions = codex_home / "sessions"
    try:
        candidates = [
            path for path in sessions.rglob("rollout-*.jsonl")
            if session_id in path.name
        ]
        return max(candidates, key=lambda path: path.stat().st_mtime_ns, default=None)
    except (OSError, ValueError):
        return None


def _last_rollout_turn_tokens(path: Path, expected_input: int | None) -> int | None:
    """신선한 rollout token_count의 마지막 요청 단위 total_tokens를 뒤에서부터 읽는다.

    codex 내부 JSONL 형식은 공개 wire 계약이 아니므로 예상 구조만 좁게 읽고, 파일/디코딩/JSON/
    필드 이상은 모두 조용히 ``None``으로 돌려 누계 차분 폴백을 사용하게 한다. 해당 이벤트의
    ``total_token_usage.input_tokens``가 방금 받은 ``turn.completed.usage.input_tokens`` 누계와
    일치해야만 같은 turn의 신호로 인정한다. 불일치는 stale rollout로 보고 폴백한다.
    """
    valid_expected = (
        isinstance(expected_input, int)
        and not isinstance(expected_input, bool)
        and expected_input >= 0
    )
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            position = stream.tell()
            partial = b""
            while position > 0:
                size = min(64 * 1024, position)
                position -= size
                stream.seek(position)
                chunk = stream.read(size) + partial
                lines = chunk.split(b"\n")
                partial = lines[0]
                for raw_line in reversed(lines[1:]):
                    found, total, anchor = _parse_token_count_total(raw_line)
                    if found:
                        return total if valid_expected and anchor == expected_input else None
            found, total, anchor = _parse_token_count_total(partial)
            return (
                total
                if found and valid_expected and anchor == expected_input
                else None
            )
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def _parse_token_count_total(
    raw_line: bytes,
) -> tuple[bool, int | None, int | None]:
    """rollout 한 줄의 token_count 여부, 요청 단위 total, 누계 input anchor를 반환한다."""
    if not raw_line.strip():
        return False, None, None
    event = json.loads(raw_line)
    if not isinstance(event, dict):
        return False, None, None
    payload = event.get("payload")
    token_event = payload if isinstance(payload, dict) else event
    if token_event.get("type") != "token_count":
        return False, None, None
    info = token_event.get("info")
    last_usage = info.get("last_token_usage") if isinstance(info, dict) else None
    total = last_usage.get("total_tokens") if isinstance(last_usage, dict) else None
    total_usage = info.get("total_token_usage") if isinstance(info, dict) else None
    anchor = total_usage.get("input_tokens") if isinstance(total_usage, dict) else None
    valid_total = isinstance(total, int) and not isinstance(total, bool) and total >= 0
    valid_anchor = isinstance(anchor, int) and not isinstance(anchor, bool) and anchor >= 0
    return (
        True,
        total if valid_total else None,
        anchor if valid_anchor else None,
    )


class CodexCliDriver:
    """`codex exec` subprocess 로 PM 세션을 구동하는 SessionDriver (codex 고유 어댑터).

    얇다 — 세션 생명주기/회전/marker 는 엔진 Supervisor 가 쥐고, 이 driver 는 한 turn 의 codex
    CLI 호출 + JSONL 파싱 + (relay 경로 전용) usage 기계 ctx 가드만 한다(opencode driver 동형에
    driver-side ctx marker 를 더한 형태 — codex 엔 plugin marker 채널이 없어서다).
    """

    def __init__(self, parse_codex_json, *, ctx_budget: int | None = None,
                 stop_pct: int = CTX_STOP_PCT_DEFAULT, mark_stop=None,
                 mark_ctx_if_over=None, spawn_result=None,
                 codex_bin: str = CODEX_BIN, timeout: int = TURN_TIMEOUT_SEC,
                 runner=subprocess.run, root: Path | None = None) -> None:
        # parse_codex_json 은 엔진 순수 헬퍼 주입(DI) — driver 가 파싱 로직을 중복 보유하지 않음.
        self._parse = parse_codex_json
        # mark_stop 은 엔진 `write_post_turn_marker`(root, sid)->bool 주입(DI) — marker payload/경로
        # 계약을 엔진이 소유하고 driver 는 예산 판정 후 트리거만 한다. post-turn 표식이라 Supervisor 가
        # 그 입력을 다시 보내지 않고 회전한다(이미 실행된 turn 의 이중 실행 방지). None 이면 가드 no-op.
        self._mark_stop = mark_stop
        parser_globals = getattr(parse_codex_json, "__globals__", {})
        self._mark_ctx_if_over = (
            mark_ctx_if_over or parser_globals.get("mark_ctx_post_turn_if_over")
        )
        self._spawn_result = (
            spawn_result or parser_globals.get("SpawnResult")
            or (lambda sid, reply: (sid, reply))
        )
        self._ctx_budget = ctx_budget
        self._stop_pct = stop_pct  # 잔여 정지 임계(%) — main 이 local.conf ctx.stop_pct 로 해소해 주입.
        self._root = Path(root) if root is not None else None
        self.codex_bin = codex_bin
        self.timeout = timeout
        self.runner = runner  # subprocess.run seam(테스트 stub 가능).
        # codex 세션은 `resume <tid>` 로 이어가되 `-C <cwd>` 로 child cwd 를 격리하므로 세션별 cwd 를
        # 기억해 relay 에 재사용한다. 어댑터-국소 세션 메타(codex CLI 고유)지 relay 대화 상태가
        # 아니다 — 엔진 Supervisor 의 stateless 불변식은 그대로다(opencode _session_cwd 대칭).
        self._session_cwd: dict[str, str] = {}
        # turn.completed.usage 는 turn 단독값이 아니라 thread billing 누계다.
        # rollout 정밀 신호를 못 읽을 때 직전 누계와의 차분을 보수적 폴백으로 쓴다.
        self._last_total: dict[str, int] = {}
        self._rollout_files: dict[str, Path] = {}

    def spawn(self, cwd: str, session_id: str, bootstrap: str):
        """첫 세션 — codex 권위 thread id와 bootstrap reply를 ``SpawnResult`` 로 반환.

        session_id 인자(엔진 uuid4)는 **무시** — codex 는 thread_id 사전지정 불가라 출력에서
        파싱한 thread_id 를 권위로 반환한다(그 tid 로 driver 가 ctx marker 를 쓰고 supervisor 가
        stat)."""
        tid, reply, usage = self._turn(cwd, bootstrap)
        if not tid:
            # thread_id 파싱 실패 = 치명 — codex 는 tid 사전지정 불가라 uuid4 폴백 시 `resume <uuid>`
            # 가 존재하지 않는 세션을 가리켜 연속성 침묵 파손(opencode sid-fail 동형·resume 불가).
            # 폴백 대신 명시 중단 — relay 는 유효 세션 없이 못 돈다.
            raise RuntimeError(
                "[pm-orch] codex 출력에서 thread.started.thread_id 를 파싱하지 못했다 — "
                "세션 구동 실패. (codex 는 thread_id 사전지정 불가라 폴백 불가 · codex/모델 설정 확인.)"
            )
        self._session_cwd[tid] = cwd  # resume 이 같은 cwd(-C)로 잇도록 기억.
        self._maybe_mark_ctx(tid, usage)  # driver-side 기계 ctx 가드.
        return self._spawn_result(tid, reply)

    def relay_turn(self, session_id: str, text: str) -> str:
        """기존 세션 resume — `codex exec resume <tid> -C <cwd> --json` 한 turn 중계."""
        cwd = self._session_cwd.get(session_id)
        _tid, reply, usage = self._turn(cwd, text, resume_id=session_id)
        self._maybe_mark_ctx(session_id, usage)  # driver-side 기계 ctx 가드.
        return reply or ""

    def close(self, session_id: str) -> None:
        """`codex exec` 1회성 turn 은 자동 exit — 세션의 어댑터-국소 메타만 정리."""
        self._session_cwd.pop(session_id, None)
        self._last_total.pop(session_id, None)
        self._rollout_files.pop(session_id, None)

    # ── codex CLI 한 turn ───────────────────────────────────────────────────────

    def _turn(self, cwd, prompt, *, resume_id=None):
        """비대화 codex turn 1회. (thread_id, reply, usage) 반환.

        - resume_id 없으면: fresh `codex exec`(codex 가 thread_id 발급).
        - resume_id 주어지면: `resume <tid>` 로 그 세션 이어감.
        child cwd 격리 — `-C <cwd>` 로 PM repo root 를 명시(opencode `--dir` 대칭).
        커맨드 형 = `codex exec --json -s workspace-write --skip-git-repo-check [-C <cwd>]
        [resume <tid>] <prompt>` (티켓 명세 순). `-C` 는 exec-레벨 플래그라 `resume` 서브커맨드
        *앞*에 둔다 — resume 뒤에 두면 resume 이 -C 를 거부할 때 cwd 격리가 파손된다.
        (resume+-C 실효는 라이브 확인 전제.)
        sandbox 는 `-s workspace-write` 로 **명시 핀** — PM relay 세션은 파일 수정/테스트
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
                # stdin..." 로 무기한 대기(라이브 실측·3m timeout 재현).
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
        elif not (completed.stdout or ""):
            sys.stderr.write("[pm-orch] codex turn 무출력(rc 0) — stdin/파싱 점검\n")

        return self._parse((completed.stdout or "").splitlines())

    # ── driver-side ctx 기계 가드 (relay 경로 전용) ─────────────────

    def _maybe_mark_ctx(self, session_id: str, usage) -> None:
        """turn usage 가 ctx 예산 정지점(잔여 <= stop_pct)에 도달하면 **post-turn** 회전 marker 를 박제한다.

        세 하네스 공통으로 driver 가 회전 판정 주체다(세션 안 가드는 비차단 안내
        전용·marker 미생산). codex 는 turn.completed 후 판정한다. turn 이 *이미 실행·응답됐으므로* 엔진
        `write_post_turn_marker` 로 post-turn 표식을 박제 → Supervisor 는 그 입력을 다시 보내지 않고 회전한다(이중 실행
        방지). 예산·usage·root·mark_stop·엔진 판정 헬퍼 중 하나라도 없으면
        no-op(가드 비활성·부트/테스트 경로 무영향 — usage None turn 은 rollout 도 안 읽는다:
        신선도 anchor 가 wire 누계 대조를 요구하므로 검증 불가 rollout 채택은 fail-open 재도입.
        그 turn 의 소모는 다음 turn 차분이 보수 흡수한다·PM override). 정지점 = 예산 × (100 - stop_pct)/100.

        사용 토큰 = input + output. **reasoning_output·cached_input 은 가산하지 않는다** — codex
        upstream usage fixture상 output_tokens는 reasoning 포함(100+10(내 reasoning 5)=110)이라
        reasoning을 재가산하지 않는다. 실측(codex 0.144.6·PM 프로브 5회) input_tokens는
        cached_input_tokens를 포함하는 상위집합(예 input_tokens=12481 ⊃ cached_input_tokens=9600)이라
        각각을 더하면 이중 계상이 된다.
        usage 는 parse_codex_json 이 wire(`*_tokens`)→contract 로 정규화한 dict(접미사 없는 키).
        1순위 점유는 rollout의 마지막 token_count.last_token_usage.total_tokens(요청 단위)지만,
        같은 이벤트의 total_token_usage.input_tokens가 방금 wire로 받은 누계 input과 일치할 때만
        신선한 값으로 채택한다. 불일치·부재·파싱 실패는 2순위인 turn.completed 누계(input+output)
        차분으로 폴백한다. 이 차분은 turn 내 다중 모델 호출 합이라 실제 마지막 요청 점유의 보수적
        상한 근사다.
        marker 박제 실패는 fail-soft."""
        if not (self._ctx_budget and usage and self._root and self._mark_stop
                and self._mark_ctx_if_over):
            return
        # codex upstream usage fixture 상 output_tokens 는 reasoning 포함:
        # 100+10(내 reasoning 5)=110. 따라서 reasoning_output 재가산 금지.
        total = (usage.get("input") or 0) + (usage.get("output") or 0)
        delta = total - self._last_total.get(session_id, 0)
        self._last_total[session_id] = total

        rollout = self._rollout_files.get(session_id)
        if rollout is None or not rollout.is_file():
            rollout = _resolve_rollout_file(session_id)
            if rollout is not None:
                self._rollout_files[session_id] = rollout
        precise = (
            _last_rollout_turn_tokens(rollout, usage.get("input"))
            if rollout is not None else None
        )
        used = precise if precise is not None else delta
        if not self._mark_ctx_if_over(
            self._root, session_id, used, self._ctx_budget, self._stop_pct,
            self._mark_stop,
        ):
            # 임계 미달과 writer 실패가 같은 False 계약이지만, 여기까지 온 양수 사용량은
            # helper가 판정한다. 실패도 relay를 막지는 않고 운영 가시성만 복원한다.
            stop_threshold = self._ctx_budget * (100 - self._stop_pct) / 100
            if used >= stop_threshold:
                sys.stderr.write("[pm-orch] codex ctx marker 박제 실패\n")


# ── 세션 안 ctx 넛지 판정 (claude ctx_guard/ctx_stop_hook 미러) ────────────────
# 훅 stdin 에는 토큰 정보가 없다 — codex-cli 0.147.0 격리 CODEX_HOME 라이브 프로브 실측 키는
# session_id·turn_id·transcript_path·cwd·hook_event_name·model·permission_mode + 이벤트별
# (prompt | tool_name·tool_input·tool_use_id) + 서브에이전트 발화에서만 agent_id·agent_type 이다.
# 그래서 점유는 `transcript_path` 가 가리키는 rollout JSONL 의 마지막 `token_count` 이벤트에서
# 읽는다(claude 훅이 transcript JSONL 의 마지막 assistant usage 를 읽는 것과 같은 축).
#
# 이 축은 위 relay 기계 가드(CodexCliDriver·post-turn 회전 marker)와 **다른 소비자**다 — 여기서는
# 회전 marker 를 만들지 않고 비차단 안내만 주입한다. 밴드 경계·임계 키·안내 문구는
# claude 와 같은 값을 쓰고 codex 전용 임계·문구를 새로 만들지 않는다.

# rollout JSONL 은 세션이 길수록 커진다(라이브 실측 — 2 turn 46KB). 전량을 읽지 않고 꼬리만 본다:
#   token_count 는 turn 마다 기록되므로 최신 값은 꼬리에 있고, 꼬리에서 못 찾으면 측정 불가로
#   보고 침묵한다(fail-open — 가드 고장이 도구 호출을 막지 않는다).
CTX_ROLLOUT_TAIL_BYTES = 256 * 1024
# pm_log ctx-guidance 자식 상한(초) — claude ctx_stop_hook._SNAPSHOT_TIMEOUT_SECONDS 미러.
CTX_GUIDANCE_TIMEOUT_SEC = 3.0
# 사이클별 멱등 marker 디렉토리 — claude ctx_stop_hook._MARKER_DIR 와 같은 경로·규약
# (`.project_manager/.local/` 는 이미 git-ignored 라 채택자가 `git add -A` 해도 안 실린다).
CTX_MARKER_DIR = Path(".project_manager") / ".local" / "ctx-stop"
# 밴드 → pm_log `ctx-guidance --band` 인자. 마지막 밴드 이름(stop)은 회전이 아니라 **최종 넛지**로
# 소비된다(claude 와 같은 소비) — 그래서 인자는 `final` 이다.
CTX_BAND_GUIDANCE_ARG = {"nudge": "nudge", "nudge2": "nudge2", "stop": "final"}
# 밴드별 marker 파일 접미사 — claude `.nudge`/`.nudge2`/`.final` 와 같은 이름.
CTX_BAND_MARKER_SUFFIX = {"nudge": "nudge", "nudge2": "nudge2", "stop": "final"}
# 넛지를 주입할 수 있는 훅 이벤트 — 두 이벤트 모두 `additionalContext` 를 갖는다(스키마 실측).
CTX_NUDGE_EVENTS = ("PreToolUse", "UserPromptSubmit")


def _int_conf(conf: dict[str, str], key: str, default: int) -> int:
    """local.conf 정수 값 — 부재·비정수면 default (claude ctx_guard._int_conf 미러)."""
    raw = conf.get(key)
    if raw is None:
        return default
    try:
        return int(str(raw).strip())
    except (ValueError, AttributeError):
        return default


def ctx_thresholds(conf: dict[str, str]) -> dict[str, int]:
    """넛지 밴드 임계(잔여 %) — `ctx.nudge_pct`/`ctx.stop_pct` (claude ctx_thresholds 미러).

    sanity 0 < stop <= nudge < 100 위반이면 **둘 다** 엔진 기본으로 폴백한다(오타·역전에 robust).
    relay 회전 축의 `resolve_stop_pct` 와 키는 같고, 이쪽은 nudge 축까지 있어 claude 와 같은
    교차 sanity 를 쓴다."""
    nudge = _int_conf(conf, "ctx.nudge_pct", CTX_NUDGE_PCT_DEFAULT)
    stop = _int_conf(conf, "ctx.stop_pct", CTX_STOP_PCT_DEFAULT)
    if not (0 < stop <= nudge < 100):
        nudge, stop = CTX_NUDGE_PCT_DEFAULT, CTX_STOP_PCT_DEFAULT
    return {"nudge_pct": nudge, "stop_pct": stop}


def nudge2_threshold(thresholds: dict[str, int]) -> int:
    """2단 넛지 임계(잔여 %) — stop_pct + 마진, nudge_pct 로 캡 (claude nudge2_threshold 미러)."""
    return min(thresholds["stop_pct"] + CTX_NUDGE2_MARGIN_PCT, thresholds["nudge_pct"])


def remaining_pct(used_pct: int) -> int:
    return max(0, 100 - used_pct)


def context_used_pct_from_tokens(tokens: int, budget: int) -> int:
    """점유 토큰 / 예산 → 사용 % (claude context_used_pct_from_tokens 미러).

    측정 없음(토큰 <= 0)·예산 비정상은 0 — 정보 없음을 밴드로 승격하지 않는다."""
    if budget <= 0 or tokens <= 0:
        return 0
    value = tokens / float(budget) * 100
    if value != value or value in (float("inf"), float("-inf")):  # NaN/inf 가드
        return 0
    return max(0, min(100, round(value)))


def classify(used_pct: int, thresholds: dict[str, int]) -> str:
    """사용 % → 'ok' | 'nudge' | 'nudge2' | 'stop' (잔여 기준·claude classify 미러)."""
    remaining = remaining_pct(used_pct)
    if remaining <= thresholds["stop_pct"]:
        return "stop"
    if remaining <= nudge2_threshold(thresholds):
        return "nudge2"
    if remaining <= thresholds["nudge_pct"]:
        return "nudge"
    return "ok"


def _rollout_input_tokens(raw_line: bytes) -> int | None:
    """rollout 한 줄이 token_count 면 그 시점 점유 토큰, 아니면 None.

    점유 = `info.last_token_usage.input_tokens`(그 요청이 모델에 보낸 입력 = 그 시점 컨텍스트).
    `total_token_usage` 는 thread 누계라 점유가 아니다(라이브 실측에서 두 값이
    15328 vs 30516 으로 갈렸다). 형식은 codex 내부 JSONL 이라 공개 계약이 아니므로 예상 구조만
    좁게 읽고 어긋나면 None(다음 후보로 계속)."""
    if not raw_line.strip():
        return None
    try:
        event = json.loads(raw_line)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(event, dict):
        return None
    payload = event.get("payload")
    token_event = payload if isinstance(payload, dict) else event
    if token_event.get("type") != "token_count":
        return None
    info = token_event.get("info")
    last_usage = info.get("last_token_usage") if isinstance(info, dict) else None
    value = last_usage.get("input_tokens") if isinstance(last_usage, dict) else None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def rollout_context_tokens(transcript_path) -> int:
    """rollout JSONL 의 **마지막** token_count 점유 토큰 (측정 불가면 0·fail-open).

    claude `context_tokens_from_transcript` 대칭 — 파일 끝에서부터 첫 usable 값을 쓴다. 부재·
    null·읽기 실패·token_count 0건은 전부 0(측정 없음)이라 밴드 판정으로 올라가지 않는다.

    **구조적 한계(라이브 실측 · codex-cli 0.147.0)**: 점유의 첫 기록은 첫 모델 **응답 뒤**
    `event_msg/token_count` 다 — 새 thread 의 첫 `UserPromptSubmit`/`PreToolUse` 시점 rollout 에는
    그 레코드가 아직 없어 이 함수는 0을 돌려준다. 그 0은 "사용률 0%"가 아니라 "아직 측정 안 됨"
    sentinel 이고, codex 훅 payload 11종 전부(`additionalProperties:false`)에 점유·윈도 신호가
    없어 대체 채널도 없다(호출부 `ctx_nudge_envelope` 가 이 sentinel 을 밴드 판정에 올리지 않는
    이유). `codex exec resume`·compaction 은 같은 rollout 파일에 이어 쓰므로(같은 `session_id`)
    이 구간에 해당하지 않는다 — 무방비는 새 thread 의 첫 모델 요청 1회뿐이다. 이 실측 범위는
    `codex exec`/`exec resume`/auto-compaction 뿐이다 — direct TUI 는 미실측이다. 코어
    rollout writer 공유라 같은 양상일 것으로 예상하나 확인된 사실이 아니다."""
    if not isinstance(transcript_path, str) or not transcript_path.strip():
        return 0
    try:
        with Path(transcript_path).open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            start = max(0, size - CTX_ROLLOUT_TAIL_BYTES)
            stream.seek(start)
            tail = stream.read()
    except (OSError, ValueError):
        return 0
    lines = tail.split(b"\n")
    if start > 0:
        lines = lines[1:]  # 꼬리 첫 줄은 잘려 있어 JSON 이 아니다.
    for raw_line in reversed(lines):
        tokens = _rollout_input_tokens(raw_line)
        if tokens is not None:
            return tokens
    return 0


def _hook_session_id(payload: dict) -> str:
    """훅 payload 의 세션 식별자 → 파일명 안전 문자열 (claude `_session_id` 미러)."""
    sid = payload.get("session_id") or payload.get("sessionId")
    if isinstance(sid, str) and sid.strip():
        return "".join(c for c in sid.strip() if c.isalnum() or c in "-_")[:64] or "unknown"
    return "unknown"


def _ctx_marker_path(root: Path, session_id: str, band: str) -> Path:
    return Path(root) / CTX_MARKER_DIR / f"{session_id}.{CTX_BAND_MARKER_SUFFIX[band]}"


def _claim_ctx_marker(path: Path) -> bool:
    """marker 를 배타 생성해 이번 호출이 사이클별 주입권을 얻었는지 (claude `_claim_marker` 미러).

    `O_EXCL` 이라 두 채널(PreToolUse·UserPromptSubmit)이 동시에 와도 하나만 주입한다. 이미
    선점됐거나 marker 를 못 쓰면 주입하지 않는다(중복 주입보다 침묵이 안전)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        return False
    try:
        os.write(handle, b"codex ctx nudge injected\n")
    except OSError:
        pass  # 생성 성공 자체가 선점의 정본 — 본문 쓰기 실패는 선점을 무효화하지 않는다.
    finally:
        try:
            os.close(handle)
        except OSError:
            pass
    return True


def _rearm_ctx_cycle(root: Path, session_id: str) -> None:
    """ok 실측 복귀 시 밴드 marker 를 지워 다음 상승 사이클을 재무장 (claude `_rearm_cycle` 미러).

    PostCompact 재무장은 비목표다 — 그 이벤트 배선을 고치면 채택자 hook trust 핀이 깨진다.
    claude 도 ok 실측 경로 재무장을 병행하므로 압축 뒤 사이클은 이 경로가 연다."""
    for band in CTX_BAND_MARKER_SUFFIX:
        try:
            _ctx_marker_path(root, session_id, band).unlink(missing_ok=True)
        except OSError:
            pass  # 정리 실패는 best-effort — 남은 marker 는 중복 주입만 막는다.


def hook_is_subagent(payload: dict) -> bool:
    """이 훅 발화가 서브에이전트의 것인가 (checkpoint 서사는 메인 PM 세션 전용).

    라이브 실측(codex-cli 0.147.0): 메인 세션 발화엔 `agent_id`/`agent_type` 키가 **없고**,
    서브에이전트 발화엔 둘 다 있다(`agent_type="default"`). 서브에이전트의 `session_id` 는
    부모와 같아서 이 면제가 없으면 부모 사이클 marker 를 서브에이전트가 소비한다."""
    for key in ("agent_type", "agent_id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return True
    return False


def _ctx_guidance_text(root: Path, *, band: str, used_pct: int,
                       thresholds: dict[str, int], timeout: float,
                       runner=subprocess.run) -> str:
    """안내 문구는 엔진 `pm_log.py ctx-guidance` stdout **그대로** (문구 복제 금지·claude 대칭).

    엔진 부재·실패·timeout 은 빈 문자열 — 그 호출은 침묵한다(문구를 어댑터가 재작성하지 않는다).
    `--json` 봉투는 `systemMessage` 형이라 모델-facing 주입엔 쓰지 않는다."""
    engine = Path(root) / ".project_manager" / "tools" / "pm_log.py"
    if not engine.is_file():
        return ""
    command = [
        sys.executable, str(engine), "ctx-guidance",
        "--band", CTX_BAND_GUIDANCE_ARG[band],
        "--used-pct", str(used_pct),
        "--remaining-pct", str(remaining_pct(used_pct)),
        "--stop-pct", str(thresholds["stop_pct"]),
    ]
    try:
        completed = runner(command, cwd=str(root), stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, timeout=timeout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""
    if getattr(completed, "returncode", 1) != 0:
        return ""
    stdout = getattr(completed, "stdout", b"") or b""
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", "replace")
    return stdout.strip()


def ctx_nudge_envelope(payload: dict, root: Path, *,
                       timeout: float = CTX_GUIDANCE_TIMEOUT_SEC,
                       runner=subprocess.run) -> dict:
    """세션 안 ctx 밴드 판정 → 비차단 안내 엔벨로프. 밴드 밖·측정 실패는 **빈 엔벨로프**.

    빈 엔벨로프는 디스패처 합본에서 기여 0이라 이 기능이 침묵한 호출의 stdout 은 이 가드가
    없던 때와 **바이트 동일**하다(측정된 통과 형태). 차단·회전 판정은 내지 않는다.

    claude `ctx_stop_hook.evaluate` 와 같은 순서다 — 서브에이전트 면제 → 점유 측정 → 밴드 판정
    → ok 실측이면 재무장 → 안내 문구 → 사이클 marker 선점 → 주입.

    **첫 turn 은 보호하지 못한다**: `rollout_context_tokens` 가 아직 측정 없는 새
    thread 첫 요청에서 0(sentinel)을 돌려주면 사용률도 0%로 계산되고 밴드는 항상 `ok` 다 —
    거짓으로 "안전"을 알리는 게 아니라 **판정 자체를 안 하는 침묵**이다(안내 문구를 만들지
    않고 marker 도 건드리지 않는다). codex 훅에 이 구간을 메울 신호·비차단 채널이 없다는 결론은
    라이브 실측(codex-cli 0.147.0)이고, claude·opencode 도 같은 구조적 한계를 갖는다(세 하네스
    공통 규칙 — 하네스 특례 아님)."""
    event = payload.get("hook_event_name") or payload.get("hookEventName")
    if event not in CTX_NUDGE_EVENTS:
        return {}
    if hook_is_subagent(payload):
        return {}
    root = Path(root)
    session_id = _hook_session_id(payload)
    tokens = rollout_context_tokens(payload.get("transcript_path"))
    conf = load_local_config(root)
    used_pct = context_used_pct_from_tokens(tokens, resolve_ctx_budget(conf))
    thresholds = ctx_thresholds(conf)
    band = classify(used_pct, thresholds)
    if band == "ok":
        # 정수 used=0 은 큰 예산에서 작은 양의 측정값일 수도 있다 — 재무장은 **실측된 ok**
        # (점유 토큰 > 0)에서만 한다. 측정 실패(0)는 marker 를 보존해 중복 주입을 막는다.
        if tokens > 0:
            _rearm_ctx_cycle(root, session_id)
        return {}
    guidance = _ctx_guidance_text(
        root, band=band, used_pct=used_pct, thresholds=thresholds,
        timeout=max(0.1, min(CTX_GUIDANCE_TIMEOUT_SEC, timeout)), runner=runner)
    if not guidance:
        return {}  # 문구를 못 읽으면 침묵 — marker 도 안 쓴다(다음 호출이 다시 시도).
    if not _claim_ctx_marker(_ctx_marker_path(root, session_id, band)):
        return {}  # 이 사이클엔 이미 주입됐다(멱등).
    return {"hookSpecificOutput": {"hookEventName": event,
                                   "additionalContext": guidance}}


# ── codex 훅 범용 진입점 + 기능 디스패처 ──────────────────────────────
# `.codex/hooks.json` 은 채택자 소유(manifest 밖)라 가드 기능을 하나 더할 때마다 채택자 config
# 수정 + `/hooks` 재승인을 다시 요구했다. 그 마찰을 1회로 끝내려고 이벤트당 진입점을 **하나만**
# 열고(`matcher .*` → 이 파일), "이 payload 에 어떤 가드를 돌릴지" 의 판단을 **manifest 등재
# 코드**인 여기로 옮긴다 — 이후 기능 추가는 아래 registry 한 줄이고 config 는 다시 안 건드린다.
# opencode `plugins/` 디렉토리 스캔이 같은 문제를 구조적으로 없앤 참고 모델이다.
#
# 진입점 집합은 **릴리즈 간 불변**이다. 늘리려면 채택자 config 변경 + 재승인이 다시 필요하므로
# 같은 1회 마이그레이션을 거친다(선언은 엔진 `pm_import.ADAPTER_HOOK_SET.entrypoints`
# 가 역방향으로 대조한다 — 진입점이 빠진 채택자는 조용히 통과하지 않는다).
CODEX_HOOK_DISPATCH_FLAG = "--hook-dispatch"
CODEX_HOOK_FEATURES_FLAG = "--hook-features"
# git-anchor 기능의 자기참조 진입 플래그 — claude `pm_orch_claude.py --git-anchor-hook`
#   대칭. delegate-channel 처럼 부를 별도 엔진 파일이 없어 이 디스패처가 `{self}` 로 자기
#   자신을 다시 부른다(아래 CODEX_HOOK_FEATURES git-anchor 항목).
GIT_ANCHOR_HOOK_FLAG = "--git-anchor-hook"
# 이 파일이 진입점을 받는 이벤트 전수. hooks.json 의 (이벤트 × matcher) 진입점과 1:1 이며,
#   엔진 역방향 가드가 같은 이름으로 config 를 대조한다. 출하 hooks.json 이 선언하는 이벤트
#   전부가 여기 있다 — 진입점 밖에 남은 이벤트가 하나라도 있으면 그 이벤트의 두 번째 기능이
#   다시 채택자 config 변경 + `/hooks` 재승인을 요구한다.
CODEX_HOOK_ENTRYPOINT_EVENTS = ("PreToolUse", "UserPromptSubmit", "PostToolUse",
                                "SubagentStart", "PreCompact", "PostCompact")
# 한 훅 발화에서 기능 자식들에게 나눠 주는 총 예산(초). hooks.json 의 바깥 timeout 보다 작고
#   엔진 감독자(delegate_channel_guard.CODEX_SUPERVISOR_TIMEOUT_SECONDS=8)보다 커야 한다 —
#   감독자보다 짧으면 감독자가 완전한 엔벨로프를 내기 전에 이쪽이 먼저 죽여 사유가 사라진다.
CODEX_HOOK_DISPATCH_BUDGET_SEC = 12
# 어느 층이 답했는지 출력으로 구별된다 — 이 마커는 엔진 상수
#   `delegate_channel_guard.CODEX_ADAPTER_FALLBACK_MARKER` 와 같은 값이어야 하고 테스트가 결속한다
#   (훅 실행 시점에 엔진 모듈을 적재하지 않으려고 리터럴을 둔다·shell 폴백과 같은 근거).
CODEX_HOOK_ADAPTER_FALLBACK_MARKER = "adapter-fallback"
# `merge_hook_envelopes` 가 두 개 이상의 `hookSpecificOutput` 을 만나면, 그중 이벤트별 output
#   스키마 허용키라도 합본 규칙이 없는 키(예: `updatedInput`·`updatedMCPToolOutput`·기준이 아닌
#   응답의 `permissionDecision` 류)는 **조용히 버리지 않고** 이 마커로 남긴다.
CODEX_HOOK_MERGE_UNHANDLED_KEY_MARKER = "merge-unhandled-key"


class CodexHookFeature(NamedTuple):
    """진입점 뒤에 등록된 가드 기능 하나 — **배선의 단일 진실**.

    feature_id   기계 노출용 식별자(`--hook-features`). 기능 파리티 가드가 이 목록을 소비한다.
    event        발화 이벤트(`CODEX_HOOK_ENTRYPOINT_EVENTS` 중 하나).
    tool_pattern `match_field` 값의 정규식(fullmatch). None 이면 판별 없음(그 이벤트 전량).
                 옛 형상에서 hooks.json matcher 가 하던 판별이 그대로 여기로 옮겨 왔다.
    argv         기능을 실행하는 커맨드. `{py}` = 이 인터프리터, `{tools}` = 엔진 도구 디렉토리,
                 `{self}` = 이 디스패처 파일 자신. 자식은 payload 를 stdin 으로 받고 엔벨로프
                 한 줄을 stdout 으로 낸다.
    handler      이 파일 안에서 **in-process** 로 도는 기능이면 그 함수(`(payload, root, *,
                 timeout) -> 엔벨로프`). 주면 `argv` 는 쓰이지 않는다. 도구 무관 기능(모든 도구
                 호출마다 발화)을 자식 프로세스로 돌리면 매 호출에 인터프리터가 하나 더 뜨므로,
                 판정이 이 파일 안에 있는 기능은 자식을 띄우지 않는다(ctx 넛지).
    match_field  `tool_pattern` 을 대조할 payload 필드 이름. **판별 축은 이벤트마다 다르다** —
                 codex 0.147.0 훅 input 스키마 실측으로 PreToolUse/PostToolUse 만 `tool_name` 을
                 싣고, SubagentStart 는 `agent_type`, PreCompact/PostCompact 는 `trigger` 다.
                 축을 `tool_name` 에 고정하면 그 필드가 없는 이벤트에서는 판별식을 아예 쓸 수
                 없어(=`tool_pattern` 이 None 으로만 가능) 판정 불능 구분이 그 자리에서 사라진다.
    side_effect_only
                 자식을 **부작용만** 위해 돌린다 — rc·stdout 을 판정에 쓰지 않고 합본에 `{}` 를
                 기여한다. 옛 압축 커맨드의 `>/dev/null 2>&1 || true` 단계가 값 그대로 여기다.
                 기본(False) 자식 계약은 "rc0 + JSON dict stdout" 이고 위반은 폴백 경고인데,
                 사람글을 stdout 에 내는 장부 CLI 를 그 계약으로 옮기면 압축마다 경고가 나간다.
    """
    feature_id: str
    event: str
    tool_pattern: str | None
    argv: tuple[str, ...]
    handler: object | None = None
    match_field: str = "tool_name"
    side_effect_only: bool = False


# 등록된 기능 전수. **여기 한 줄이 곧 배선**이다 — 새 가드는 이 튜플에 항목을 더하면 되고
#   채택자 `.codex/hooks.json` 은 그대로다(이 구조가 닫은 마찰).
CODEX_HOOK_FEATURES: tuple[CodexHookFeature, ...] = (
    CodexHookFeature(
        # 옛 배선: hooks.json `PreToolUse` matcher `^collaborationspawn_agent$` 가 직접 감독자를
        #   불렀다. 판별식과 커맨드가 값 그대로 여기로 옮겨 왔다(진입점 뒤 분기·동작 보존).
        feature_id="delegate-channel",
        event="PreToolUse",
        tool_pattern="^collaborationspawn_agent$",
        argv=("{py}", "{tools}/delegate_channel_guard.py", "supervise", "PreToolUse",
              "{py}", "{tools}/delegate_channel_guard.py", "codex-hook"),
    ),
    # 세션 안 ctx 넛지 — claude 가 같은 두 이벤트에 거는 것과 같은 채널이다. 두 항목은
    #   사이클 marker 를 공유하므로 먼저 발화한 채널 하나만 주입한다(멱등).
    CodexHookFeature(
        feature_id="ctx-nudge-pretooluse",
        event="PreToolUse",
        tool_pattern=None,  # 도구 무관 — 긴 turn 안에서도 밴드 진입을 그 자리에서 알린다.
        argv=(),
        handler=ctx_nudge_envelope,
    ),
    CodexHookFeature(
        feature_id="ctx-nudge-userpromptsubmit",
        event="UserPromptSubmit",
        tool_pattern=None,
        argv=(),
        handler=ctx_nudge_envelope,
    ),
    CodexHookFeature(
        # 옛 배선: hooks.json `SubagentStart` matcher `.*` 가 직접 감독자를 불렀다. 그 matcher 는
        #   `agent_type` 값 공간을 전수 덮으므로(=판별 없음) 판별식 자리는 None 이다 — 여기에
        #   `agent_type` 정규식을 새로 세우면 옛 배선이 발화하던 입력(빈 값·읽을 수 없는 payload)
        #   에서 가드가 안 돌아 판정이 바뀐다.
        feature_id="delegate-channel-subagent",
        event="SubagentStart",
        tool_pattern=None,
        argv=("{py}", "{tools}/delegate_channel_guard.py", "supervise", "SubagentStart",
              "{py}", "{tools}/delegate_channel_guard.py", "codex-subagent-observe"),
    ),
    # 압축 두 이벤트의 옛 커맨드는 **2단**이었다 — 장부 checkpoint(출력 폐기) 다음 엔벨로프 생성.
    #   그 2단을 등재 2행으로 옮긴다(순서 = 옛 셸 순서). 옛 matcher 2개(`^auto$`·`^manual$`)는
    #   `trigger` enum 전수(`{manual, auto}`)라 판별 없음과 값이 같다 — 그래서 판별식이 None 이다.
    CodexHookFeature(
        feature_id="compaction-checkpoint-pre",
        event="PreCompact",
        tool_pattern=None,
        argv=("{py}", "{tools}/pm_log.py", "checkpoint", "--trigger", "compaction",
              "--phase", "pre", "--cwd", "."),
        side_effect_only=True,
    ),
    CodexHookFeature(
        feature_id="compaction-guidance",
        event="PreCompact",
        tool_pattern=None,
        argv=("{py}", "{tools}/pm_log.py", "ctx-guidance", "--band", "precompact",
              "--json"),
    ),
    CodexHookFeature(
        feature_id="compaction-checkpoint-post",
        event="PostCompact",
        tool_pattern=None,
        argv=("{py}", "{tools}/pm_log.py", "checkpoint", "--trigger", "compaction",
              "--phase", "post", "--cwd", "."),
        side_effect_only=True,
    ),
    CodexHookFeature(
        feature_id="compaction-snapshot",
        event="PostCompact",
        tool_pattern=None,
        argv=("{py}", "{tools}/pm_log.py", "snapshot", "--cwd", ".", "--json"),
    ),
    CodexHookFeature(
        # raw git cwd-anchor 가드가 claude_code·opencode 두 타깃엔 배선돼 있었는데 codex 엔
        #   없었다(도그푸딩 사각). 판정은 새로 만들지 않고 세 타깃이 공유하는
        #   `board.judge_git_anchor_command` 를 그대로 부른다(claude `pm_orch_claude.py
        #   git_anchor_hook_evaluate` 와 동형). git-anchor 엔 delegate-channel 처럼 부를 별도
        #   엔진 파일이 없어 `{self}` 로 이 디스패처가 자기 자신을 다시 부른다.
        feature_id="git-anchor",
        event="PreToolUse",
        tool_pattern="^Bash$",
        argv=("{py}", "{self}", GIT_ANCHOR_HOOK_FLAG),
    ),
)


def _registered_features(features):
    """판정이 쓸 기능 목록 — 미지정이면 **호출 시점**의 registry.

    기본값을 시그니처에 박으면 def 시점 튜플이 굳어, 기능을 더한 상류 사본을 로드해도 옛 목록이
    돈다(등록이 코드 변경으로 전파된다는 이 배선의 전제가 그 자리에서 거짓이 된다)."""
    return CODEX_HOOK_FEATURES if features is None else features


def hook_feature_registry(features=None) -> dict:
    """등록 진입점·기능을 **기계 판독 형태**로 노출 (`--hook-features`).

    배선이 디스패처 뒤로 들어가면 config 파싱으로는 기능을 열거할 수 없다 — 기능 파리티 가드가
    소비할 목록을 여기서 낸다(하네스 간 기능 집합 대조의 codex 쪽 입력)."""
    features = _registered_features(features)
    return {
        "entrypoint_events": list(CODEX_HOOK_ENTRYPOINT_EVENTS),
        "features": [
            {"feature_id": feature.feature_id, "event": feature.event,
             "tool_pattern": feature.tool_pattern}
            for feature in features
        ],
    }


def hook_fallback_envelope(subject: str, detail: str) -> dict:
    """가드를 못 돌렸다는 사실이 남는 2필드 경고 엔벨로프 (fail-open·차단 0).

    통과와 구별되지 않는 침묵이 이 클래스가 숨는 방식이라, 폴백은 항상 마커를 단다."""
    return {
        "systemMessage": (f"[hook-dispatch/warn] {CODEX_HOOK_ADAPTER_FALLBACK_MARKER}: "
                          f"{subject} 판정 불가({detail}) — 가드 없이 통과(fail-open)"),
        "suppressOutput": False,
    }


# 판별 결과는 셋이다 — **정상 미매칭**(조용한 통과)과 **판정 불능**을 같은 값으로 접지 않는다.
#   옛 배선에서는 hooks.json matcher 가 호스트 쪽에서 판별했고, 판별 근거가 없는 입력(빈 stdin·
#   `{}`·파손 JSON)은 가드 자식이 경고 엔벨로프로 냈다. 판별이 진입점 뒤로 옮겨 온 뒤에도 그
#   경고 의미가 남아야 한다 — 판정 불능이 통과와 같은 출력이면 가드가 꺼진 것과 구별되지
#   않는다(불변식 — 조용한 실패 금지).
CODEX_HOOK_ROUTE_MATCH = "match"
CODEX_HOOK_ROUTE_SKIP = "skip"
CODEX_HOOK_ROUTE_UNDECIDABLE = "undecidable"


class CodexHookRoute(NamedTuple):
    """한 기능에 대한 판별 결과 — 판정과 (판정 불능일 때의) 사유."""
    decision: str
    detail: str = ""


def _parse_hook_payload(payload_bytes) -> tuple[dict, str]:
    """훅 payload → (dict, 못 읽은 사유). 사유가 빈 문자열이면 정상 파싱이다.

    사유를 값으로 돌려주는 이유는 호출부가 "빈 payload" 와 "못 읽은 payload" 를 구분해야 하기
    때문이다 — 둘을 같은 빈 dict 로 접으면 판정 불능이 정상 미매칭으로 위장한다."""
    raw = bytes(payload_bytes)
    if not raw.strip():
        return {}, "빈 stdin(payload 없음)"
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, TypeError) as exc:
        detail = " ".join(str(exc).splitlines()).strip() or type(exc).__name__
        return {}, f"{type(exc).__name__}: {detail}"
    if not isinstance(payload, dict):
        return {}, f"payload 가 JSON 객체가 아님({type(payload).__name__})"
    return payload, ""


def _payload_field_alias(field: str) -> str:
    """snake_case 필드의 camelCase 표기 — 호스트 표기 흔들림을 같은 값으로 읽는다.

    `tool_name`/`toolName` 을 둘 다 보던 판정을 축이 늘어난 뒤에도 이름 하나에서 파생한다
    (필드마다 별칭 표를 손으로 유지하면 새 축이 조용히 별칭 없이 등록된다)."""
    head, _, tail = field.partition("_")
    return head + "".join(part[:1].upper() + part[1:] for part in tail.split("_") if part)


def _feature_route(feature: CodexHookFeature, payload: dict, *,
                   payload_error: str = "") -> CodexHookRoute:
    """그 payload 가 이 기능의 판별식에 걸리는가 — 걸림/안 걸림/**판정 불능**.

    판별식(`tool_pattern`)은 옛 hooks.json matcher 와 같은 fullmatch 이고, 대조 대상은
    `match_field` 가 지목한 payload 필드다. 판별에 쓸 값이 없거나(payload 파싱 실패·그 필드
    부재) 판별식 자체가 깨졌으면(정규식 오류) 안 걸린 것이 아니라 **판정을 못 한 것**이다."""
    if feature.tool_pattern is None:
        return CodexHookRoute(CODEX_HOOK_ROUTE_MATCH)
    field = feature.match_field
    if payload_error:
        return CodexHookRoute(CODEX_HOOK_ROUTE_UNDECIDABLE,
                              f"payload 를 못 읽어 {field} 판별 불가({payload_error})")
    name = payload.get(field)
    if not isinstance(name, str) or not name:
        name = payload.get(_payload_field_alias(field))
    if not isinstance(name, str) or not name:
        return CodexHookRoute(CODEX_HOOK_ROUTE_UNDECIDABLE,
                              f"payload 에 {field} 이 없어 판별 불가")
    try:
        matched = re.fullmatch(feature.tool_pattern, name) is not None
    except re.error as exc:
        return CodexHookRoute(
            CODEX_HOOK_ROUTE_UNDECIDABLE,
            f"registry tool_pattern {feature.tool_pattern!r} 정규식 오류({exc})")
    return CodexHookRoute(
        CODEX_HOOK_ROUTE_MATCH if matched else CODEX_HOOK_ROUTE_SKIP)


def _feature_matches(feature: CodexHookFeature, payload: dict) -> bool:
    """판별식에 **걸리는가** 하나만 보는 형태(판정 불능은 걸리지 않은 것으로 본다)."""
    return _feature_route(feature, payload).decision == CODEX_HOOK_ROUTE_MATCH


def _expand_hook_argv(argv, root: Path) -> list[str]:
    """registry argv 의 자리표시자를 실값으로 — `{py}`=이 인터프리터·`{tools}`=엔진 도구 디렉토리·
    `{self}`=이 디스패처 파일 자신(자기참조형 기능이 부르는 대상 — git-anchor 는 delegate-channel
    과 달리 부를 별도 엔진 파일이 없다).

    `{py}`·`{tools}`는 cwd 가 아니라 **엔진 루트**에서 해소한다(훅이 어느 디렉토리에서 발화해도
    같은 자식). `{self}`는 root 파생이 **아니다** — 실행 중인 파일은 자기 위치(`__file__`)를 이미
    알고, root 에서 다시 조합한 좌표는 같은 사실의 두 번째 사본이라 레이아웃이 바뀌면 어긋난다
    (flat 레이아웃에서 `root/.codex/pm_orch_codex.py` 재구성이 실재하지 않는 경로를
    가리켜 git-anchor 자식 spawn 이 rc=2 로 죽었다)."""
    tools = str(Path(root) / ".project_manager" / "tools")
    this_file = str(Path(__file__).resolve())
    return [str(token).replace("{py}", sys.executable)
                       .replace("{tools}", tools)
                       .replace("{self}", this_file)
            for token in argv]


def _run_hook_feature(feature: CodexHookFeature, payload_bytes: bytes, root: Path, *,
                      runner=subprocess.run, timeout: float) -> dict:
    """기능 자식을 시간 상한 안에서 돌리고 그 엔벨로프를 그대로 돌려준다(실패는 폴백 경고)."""
    if feature.side_effect_only:
        # 부작용 단계는 **답하지 않는다** — rc·stdout 을 보지 않고 `{}` 를 기여한다. 옛 압축
        #   커맨드가 이 자식의 출력·rc 를 `>/dev/null 2>&1 || true` 로 버리던 동작을 값 그대로
        #   옮긴 것이라, 여기서 경고를 내면 **모든 압축마다** 없던 소음이 새로 생긴다.
        #   이 침묵은 판정 불능의 조용한 통과와 다르다: 이 기능은 애초에 판정을 하지 않는다.
        try:
            runner(_expand_hook_argv(feature.argv, root), input=bytes(payload_bytes),
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        except BaseException:  # noqa: BLE001 — 장부 기록 실패가 훅 답을 바꾸지 않는다.
            pass
        return {}
    try:
        completed = runner(
            _expand_hook_argv(feature.argv, root), input=bytes(payload_bytes),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
        )
        returncode = getattr(completed, "returncode", None)
        if returncode != 0:
            raise ValueError(f"기능 rc={returncode}")
        envelope = json.loads((getattr(completed, "stdout", b"") or b"").decode("utf-8"))
        if not isinstance(envelope, dict):
            raise ValueError("엔벨로프가 JSON 객체가 아님")
        return envelope
    except BaseException as exc:  # noqa: BLE001 — 기능 고장이 도구 호출을 막지 않는다.
        detail = " ".join(str(exc).splitlines()).strip() or type(exc).__name__
        return hook_fallback_envelope(feature.feature_id, f"{type(exc).__name__}: {detail}")


def _run_hook_feature_inprocess(feature: CodexHookFeature, payload: dict, root: Path, *,
                                timeout: float) -> dict:
    """in-process 기능을 돌리고 그 엔벨로프를 그대로 돌려준다(실패는 폴백 경고).

    자식 프로세스 경로(`_run_hook_feature`)와 **같은 실패 계약**이다 — 기능 고장이 도구 호출을
    막지 않고, 폴백은 마커를 달아 통과와 구별된다."""
    try:
        envelope = feature.handler(payload, root, timeout=timeout)
        if not isinstance(envelope, dict):
            raise ValueError("엔벨로프가 dict 가 아님")
        return envelope
    except BaseException as exc:  # noqa: BLE001 — 기능 고장이 도구 호출을 막지 않는다.
        detail = " ".join(str(exc).splitlines()).strip() or type(exc).__name__
        return hook_fallback_envelope(feature.feature_id, f"{type(exc).__name__}: {detail}")


def _is_blocking_envelope(envelope: dict) -> bool:
    """그 엔벨로프가 차단 판정인가 (`decision`/`permissionDecision` 보유)."""
    if envelope.get("decision") == "block":
        return True
    hook_output = envelope.get("hookSpecificOutput")
    return (isinstance(hook_output, dict)
            and hook_output.get("permissionDecision") == "deny")


CODEX_HOOK_MERGE_HANDLED_HOOK_SPECIFIC_KEYS = frozenset({"hookEventName", "additionalContext"})

# 최상위 키 중 이미 병합 규칙이 있는 키(F-0824-CR01) — 이 밖의 최상위 키(예: `continue`·
#   `stopReason`·`decision`·`reason`)를 base 가 아닌 응답이 실으면 조용히 버리지 않고
#   `CODEX_HOOK_MERGE_UNHANDLED_KEY_MARKER` 로 남긴다 — `hookSpecificOutput` 서브키와 같은 축.
CODEX_HOOK_MERGE_HANDLED_TOP_LEVEL_KEYS = frozenset(
    {"systemMessage", "suppressOutput", "hookSpecificOutput"})


def merge_hook_envelopes(envelopes) -> dict:
    """여러 기능의 응답을 호스트가 읽는 **한 줄**로 합친다.

    호스트는 훅 하나당 엔벨로프 하나만 읽는다. 합본 규칙은 키마다 다르다:
      1. 빈 엔벨로프(`{}` = 통과)는 기여하지 않는다 — 아무도 답하지 않으면 `{}`(측정된 allow 형태).
      2. 응답이 하나면 **그대로** 돌려준다 — 진입점 도입이 기존 판정 값을 바꾸지 않는다.
      3. 둘 이상이면 차단 판정이 있는 응답이 기준(base)이 되고(없으면 첫 응답), 그 base 를 얕은
         복사해 합본을 시작한다 — base 자신의 키는 전량 그대로 실린다(유실 없음).
      4. `systemMessage` — **문자열 누적**: 전 응답의 값을 줄바꿈으로 이어 붙인다(중복 제외).
         차단이면 `reason`·`hookSpecificOutput.permissionDecisionReason` 도 같은 합본으로 맞춰
         사유가 잘리지 않게 한다.
      5. `hookSpecificOutput.additionalContext` — **문자열 누적**: base 가 아닌 응답도
         이 키만은 base 의 `hookSpecificOutput` 에 **중첩 dict 병합**으로 실린다. deny 등 base
         차단 판정과 **동시에** 성립한다(codex 0.147.0 라이브 실측 — 차단 집행과 안내 주입이
         한 응답에 함께 실리는 것을 확인했다). 값이 문자열이
         아니면(비-str) 침묵하지 않고 6번 마커로 남는다.
      6. `suppressOutput` — **논리 결합**: 하나라도 `False` 면 결과도 `False`(비차단 안내를 억누르지
         않는 쪽이 이긴다).
      7. base 가 아닌 응답이 4·5·6번이 다루지 않는 키를 최상위(예: `continue`·`stopReason`·
         `decision`·`reason`) 또는 `hookSpecificOutput` 서브키(예: `updatedInput`·
         `updatedMCPToolOutput`·기준이 아닌 응답의 `permissionDecision`류)로 실으면, **값을
         합성하지 않고** 합본 규칙이 없다는 사실만 `systemMessage` 에 `[hook-dispatch/warn]
         {CODEX_HOOK_MERGE_UNHANDLED_KEY_MARKER}` 마커로 남긴다(F-0824-CR01) — 조용히 버리지
         않는다. base 자신의 키는 `dict(base)` 로 이미 전량 실려 유실이 아니다.
    """
    answered = [item for item in envelopes if isinstance(item, dict) and item]
    if not answered:
        return {}
    if len(answered) == 1:
        return answered[0]
    blocking = [item for item in answered if _is_blocking_envelope(item)]
    base = blocking[0] if blocking else answered[0]
    merged = dict(base)
    messages: list[str] = []
    contexts: list[str] = []
    unhandled_keys: list[str] = []
    event_name = None
    for item in answered:
        text = item.get("systemMessage")
        if isinstance(text, str) and text.strip() and text not in messages:
            messages.append(text)
        if item is not base:
            # 최상위 허용키 중 4·6번 규칙이 없는 것(`continue`·`stopReason`·`decision`·
            #   `reason`)은 값을 합성하지 않고 유실 사실만 경고로 남긴다(F-0824-CR01).
            for key in item:
                if key not in CODEX_HOOK_MERGE_HANDLED_TOP_LEVEL_KEYS:
                    unhandled_keys.append(key)
        hook_output = item.get("hookSpecificOutput")
        if not isinstance(hook_output, dict):
            continue
        if event_name is None and isinstance(hook_output.get("hookEventName"), str):
            event_name = hook_output["hookEventName"]
        if "additionalContext" in hook_output:
            context_value = hook_output["additionalContext"]
            if isinstance(context_value, str):
                if context_value.strip() and context_value not in contexts:
                    contexts.append(context_value)
            else:
                unhandled_keys.append(f"additionalContext(비-str:{type(context_value).__name__})")
        if item is base:
            continue  # base 는 dict(base) 로 이미 전량 실렸다 — 나머지 키는 유실이 아니다.
        for key in hook_output:
            if key not in CODEX_HOOK_MERGE_HANDLED_HOOK_SPECIFIC_KEYS:
                unhandled_keys.append(key)
    if contexts:
        combined_context = "\n".join(contexts)
        hook_output = merged.get("hookSpecificOutput")
        hook_output = dict(hook_output) if isinstance(hook_output, dict) else {}
        hook_output.setdefault("hookEventName", event_name)
        hook_output["additionalContext"] = combined_context
        merged["hookSpecificOutput"] = hook_output
    if unhandled_keys:
        unique_unhandled = sorted(set(unhandled_keys))
        messages.append(
            f"[hook-dispatch/warn] {CODEX_HOOK_MERGE_UNHANDLED_KEY_MARKER}: "
            f"{', '.join(unique_unhandled)} 합본 규칙 없음 — 값 유실")
    if messages:
        combined = "\n".join(messages)
        merged["systemMessage"] = combined
        if _is_blocking_envelope(merged):
            merged["reason"] = combined
            hook_output = merged.get("hookSpecificOutput")
            if isinstance(hook_output, dict) and "permissionDecisionReason" in hook_output:
                merged["hookSpecificOutput"] = {
                    **hook_output, "permissionDecisionReason": combined}
    if any(item.get("suppressOutput") is False for item in answered):
        merged["suppressOutput"] = False
    return merged


def dispatch_hook(event: str, payload_bytes: bytes, root: Path, *,
                  features=None, runner=subprocess.run,
                  budget: float = CODEX_HOOK_DISPATCH_BUDGET_SEC) -> dict:
    """그 이벤트에 등록된 기능 중 payload 에 걸리는 것을 돌려 합본 엔벨로프를 만든다.

    걸리는 기능이 없으면 자식을 **하나도 띄우지 않는다** — 진입점이 전 도구 호출로 넓어졌으므로
    in-process 판별이 없으면 매 호출이 프로세스 하나를 더 만든다. 예산은 기능들이 나눠 쓴다.

    판별을 **못 한** 기능은 조용히 건너뛰지 않고 경고 엔벨로프로 남긴다(rc 는 종전대로 0·차단
    0) — 옛 배선에서 같은 입력에 가드 자식이 내던 경고가 진입점 뒤에서 사라지면, 가드가 꺼진
    형상과 출력이 같아진다."""
    if event not in CODEX_HOOK_ENTRYPOINT_EVENTS:
        # config 가 이 디스패처 세대가 모르는 진입점을 열었다 — 조용히 통과하지 않는다.
        return hook_fallback_envelope(
            f"dispatch:{event}", "이 디스패처 세대에 없는 진입점 이벤트")
    payload, payload_error = _parse_hook_payload(payload_bytes)
    deadline = time.monotonic() + budget
    answers: list[dict] = []
    for feature in _registered_features(features):
        if feature.event != event:
            continue
        route = _feature_route(feature, payload, payload_error=payload_error)
        if route.decision == CODEX_HOOK_ROUTE_SKIP:
            continue  # 정상 미매칭 — 옛 형상에서 호스트 matcher 가 조용히 걸러 주던 값이다.
        if route.decision == CODEX_HOOK_ROUTE_UNDECIDABLE:
            answers.append(hook_fallback_envelope(feature.feature_id, route.detail))
            continue
        timeout = max(0.1, deadline - time.monotonic())
        if feature.handler is not None:
            answers.append(_run_hook_feature_inprocess(
                feature, payload, root, timeout=timeout))
            continue
        answers.append(_run_hook_feature(
            feature, payload_bytes, root, runner=runner, timeout=timeout))
    return merge_hook_envelopes(answers)


def run_hook_dispatch(event: str, root: Path, *, stdin=None, stdout=None) -> int:
    """stdin 훅 payload → 합본 엔벨로프 한 줄. **어떤 실패도 rc0 + 유효 엔벨로프**.

    비-ASCII 는 `\\uXXXX` 로 이스케이프해 낸다 — 이 줄은 PowerShell/bash 캡처를 거쳐 호스트로
    가므로 cp949 콘솔에서도 JSON 이 깨지지 않아야 한다(엔진 감독자 출력과 같은 규약)."""
    stream = sys.stdin.buffer if stdin is None else stdin
    sink = sys.stdout if stdout is None else stdout
    try:
        payload_bytes = stream.read()
        if isinstance(payload_bytes, str):
            payload_bytes = payload_bytes.encode("utf-8")
        envelope = dispatch_hook(event, payload_bytes, root)
    except BaseException as exc:  # noqa: BLE001 — 디스패처 고장이 도구 호출을 막지 않는다.
        detail = " ".join(str(exc).splitlines()).strip() or type(exc).__name__
        envelope = hook_fallback_envelope(
            f"dispatch:{event}", f"{type(exc).__name__}: {detail}")
    sink.write(json.dumps(envelope, separators=(",", ":")) + "\n")
    sink.flush()
    return 0


# ── git-anchor 훅 축 (claude `pm_orch_claude.py --git-anchor-hook` 대칭) ──────────────────────
# raw git cwd-anchor 판정은 세 하네스가 공유하는 단일 진실 `board.judge_git_anchor_command` 다
#   (새 판정 로직 없음). 이 절은 codex PreToolUse(Bash) payload 를 그 판정에 배선하고 verdict 를
#   codex 엔벨로프로 옮기는 자리다 — `.codex/hooks.json` 은 여전히 `--hook-dispatch PreToolUse`
#   만 알고, 이 기능은 위 `CODEX_HOOK_FEATURES` registry 에 자기 자신을 다시 부르는 항목으로만
#   등록된다.
GIT_ANCHOR_ONCE_NAMESPACE = "git-anchor"  # 세션 1회 발화 멤버십 namespace(엔진 claim_session_once).
GIT_ANCHOR_ONCE_KEY_CHARS = 16  # 멤버십 키 = 발화 문구 sha256 앞 16자(문구 전문을 파일에 남기지 않는다).


def _load_board(root: Path):
    """raw git 훅 판정의 단일 진실인 board.py 를 root 기준으로 로드한다(claude 어댑터 동형)."""
    board_path = root / ".project_manager" / "tools" / "board.py"
    spec = importlib.util.spec_from_file_location("pm_git_anchor_board", board_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git_anchor_hook_evaluate(payload: dict, root: Path) -> dict:
    """codex PreToolUse(Bash) payload 를 중앙 `judge_git_anchor_command` seam 에 배선한다.

    반환은 **항상 유효한 dict** 다 — 이 기능은 자식 프로세스로 돌고, 디스패처
    `_run_hook_feature` 는 빈 stdout 을 실패로 읽어 adapter-fallback 경고로 강등한다. 그래서
    codex 축의 "침묵"은 claude 처럼 0바이트가 아니라 **측정된 allow 형태 `{}`** 다(`merge_hook_
    envelopes` 가 이미 그 값을 "기여 없음"으로 다룬다). ok·정상 미매칭·세션 내 완전일치 중복
    warn 이 이 값을 낸다. 판정불능(uncertain)은 board 가 이미 warn 으로 접어 넣으므로 여기서
    별도로 다루지 않는다(중앙 판정 결정 승계 — 판정불능은 침묵시키지 않는다)."""
    event = payload.get("hook_event_name") or payload.get("hookEventName")
    tool = payload.get("tool_name") or payload.get("toolName")
    tool_input = payload.get("tool_input") or payload.get("toolInput") or {}
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if event not in (None, "PreToolUse") or tool != "Bash" or not isinstance(command, str):
        return {}
    # claude `pm_orch_claude.git_anchor_hook_evaluate` 와 같은 선필터 — 대상은 넷(git 호출·엔진
    #   도구 호출·pytest·cd 잔존). 판정 대상 비포함은 board import 조차 하지 않는다(핫패스
    #   비용 0 — 이 함수 자체는 이미 자식 프로세스라 프로세스 spawn 비용은 등록 축(tool_pattern
    #   ^Bash$)이 진다).
    prefilter = command.replace("\\\n", "").replace("'", "").replace('"', "")
    normalized = re.sub(r"/+", "/", prefilter.replace("\\", "/"))
    if not (
        re.search(r"(?<![A-Za-z0-9_.-])git(?=\s|$|[<>])", prefilter)
        or ".project_manager/tools/" in normalized
        or re.search(r"(?<![A-Za-z0-9_.-])pytest(?=\s|$)", prefilter)
        or re.search(r"(?:^|[;&|])\s*cd(?=\s)", prefilter)
    ):
        return {}
    cwd = payload.get("cwd")
    anchor = cwd if isinstance(cwd, str) and cwd else str(root)
    judgment = _load_board(root).judge_git_anchor_command(anchor, command)
    verdict = judgment.get("verdict")
    reason = judgment.get("reason", "git cwd 정체 판정")
    if verdict == "deny":
        text = f"[git-anchor/deny] {reason}"
        return {
            "decision": "block",
            "reason": text,
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": text,
            },
            "systemMessage": text,
            "suppressOutput": False,
        }
    if verdict == "warn":
        text = f"[git-anchor/warn] {reason}"
        if _git_anchor_duplicate(root, payload, text):
            return {}  # 세션 내 완전일치 중복 — 측정된 allow 형태로 침묵.
        return {"systemMessage": text, "suppressOutput": False}
    return {}


def _git_anchor_duplicate(root: Path, payload: dict, text: str) -> bool:
    """이 세션에서 **문자열이 완전히 같은** advisory 를 이미 냈으면 True(claude 헬퍼 재사용).

    마커 I/O 실패(None)는 False — 발화를 유지한다(fail-open). 엔진 `pm_relay.claim_session_
    once` 를 그대로 부른다 — claude 와 같은 멤버십 디렉토리 `.project_manager/.local/ctx-stop/`
    를 공유하되 세션키가 달라 서로 섞이지 않는다(새 상태 파일·새 디렉토리 0)."""
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


def run_git_anchor_hook(root: Path, *, stdin=None, stdout=None) -> int:
    """stdin JSON 훅 payload → codex 엔벨로프 한 줄(항상 유효한 JSON). rc 는 항상 0.

    예외(판정 인프라 손상)는 중복 억제 게이트를 지나지 않고 무조건 발화한다(fail-open ·
    claude `run_git_anchor_hook` 동형)."""
    stream = sys.stdin if stdin is None else stdin
    sink = sys.stdout if stdout is None else stdout
    try:
        raw = stream.read()
        payload = json.loads(raw) if raw else {}
        if not isinstance(payload, dict):
            payload = {}
        envelope = git_anchor_hook_evaluate(payload, root)
    except BaseException as exc:  # SystemExit 포함 판정 인프라 손상은 정상 Bash 를 막지 않는다.
        detail = " ".join(str(exc).splitlines()).strip() or type(exc).__name__
        text = f"[git-anchor/warn] 판정 불가({type(exc).__name__}: {detail}) — cwd를 직접 확인"
        envelope = {"systemMessage": text, "suppressOutput": False}
    sink.write(json.dumps(envelope, separators=(",", ":")) + "\n")
    sink.flush()
    return 0


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
        help="task 정체성 — 회전된 새 PM 세션의 재진입 프롬프트에 `--task <이름>` 실값을 "
             "박아 같은 task 를 resume 하게 한다((b) 명시 전달·cwd 추론 금지). 미지정이면 bare "
             "`$pm-bootstrap`(슬롯/솔로).",
    )
    # 훅 축(relay 와 같은 파일·claude `pm_orch_claude.py --git-anchor-hook` 대칭) — 하네스가
    #   부르는 기계 진입점이라 사람 대상 help 에서는 감춘다.
    parser.add_argument(CODEX_HOOK_DISPATCH_FLAG, default=None, metavar="이벤트",
                        help=argparse.SUPPRESS)
    parser.add_argument(CODEX_HOOK_FEATURES_FLAG, action="store_true",
                        help=argparse.SUPPRESS)
    parser.add_argument(GIT_ANCHOR_HOOK_FLAG, action="store_true",
                        help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    actual_argv = sys.argv[1:] if argv is None else argv
    # 훅 축은 relay 부팅(엔진 적재·local.conf·supervisor)을 타지 않는다 — 훅은 매 도구 호출마다
    #   발화하므로 진입점이 얇아야 하고, 엔진 적재 실패가 도구 호출을 막으면 그게 곧 락아웃이다.
    if CODEX_HOOK_FEATURES_FLAG in actual_argv:
        sys.stdout.write(json.dumps(hook_feature_registry(), ensure_ascii=False,
                                    indent=2, sort_keys=True) + "\n")
        return 0
    if CODEX_HOOK_DISPATCH_FLAG in actual_argv:
        args = build_parser().parse_args(actual_argv)
        return run_hook_dispatch(
            args.hook_dispatch, repo_root(Path(__file__).resolve().parent))
    if GIT_ANCHOR_HOOK_FLAG in actual_argv:
        return run_git_anchor_hook(repo_root(Path(__file__).resolve().parent))

    args = build_parser().parse_args(argv)
    engine, root = _load_engine()

    cwd = args.cwd or os.getcwd()
    conf = load_local_config(root)
    ctx_budget = resolve_ctx_budget(conf)
    stop_pct = resolve_stop_pct(conf)
    engine.validate_relay_budget(ctx_budget, stop_pct)
    # driver-side ctx 가드 원천 — local.conf 예산·정지임계 해소 + 엔진 post-turn marker writer 주입.
    driver = CodexCliDriver(
        engine.parse_codex_json,
        ctx_budget=ctx_budget,
        stop_pct=stop_pct,
        mark_stop=engine.write_post_turn_marker,
        mark_ctx_if_over=engine.mark_ctx_post_turn_if_over,
        spawn_result=engine.SpawnResult,
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
