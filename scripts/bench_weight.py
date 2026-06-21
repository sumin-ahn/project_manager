#!/usr/bin/env python3
"""벤치마크 — full vs lite 진입 토큰·자족성 정량 (XDG sandbox).

2D 무게축의 **lite 구현 범위(A/B/C)를 데이터로 결정**하기 위해, full 진입 vs
lite 진입을 동일 시나리오로 돌려 **진입 토큰·완주 토큰·자족성**(lite 가 추가 read
없이 완주했나)을 정량 측정하는 도구다 (T-0008).

사용:
    scripts/bench_weight.py --harness {claude,opencode} --weight {full,lite}
                            --scenario <id> [--out <경로>] [--live]

기본(`--live` 없음) = **dry/plan 모드**: 측정을 실행하지 않고 해당 (harness,weight)
의 진입 파일셋·프롬프트·sandbox 계획을 출력만 한다 (토큰 0·외부 호출 0). `--live`
일 때만 실제 하니스를 subprocess 로 구동한다.

설계 (board T-0008):
  - 순수 함수(parse_*·count_entry_files·render_table)는 라이브 호출 없이 단위
    테스트 가능하게 분리한다 — tests/test_bench_weight.py 가 stub 으로 검증.
  - 라이브 러너는 XDG sandbox(tempfile.mkdtemp) 로 실 프로젝트 오염을 막는다.
  - 외부 의존 금지 — stdlib(argparse·json·subprocess·tempfile) 만.
  - 라이브 실패(timeout·non-zero·빈 JSON)는 fail-soft 금지 — 명확한 에러 + non-zero exit.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]

# ── 진입 파일셋 매니페스트 ────────────────────────────────────────────────────
# full = PM 세션 부트스트랩 진입셋 (CLAUDE.md + wiki 코어 4종).
FULL_FILESET: tuple[str, ...] = (
    "CLAUDE.md",
    ".project_manager/wiki/pm_role.md",
    ".project_manager/wiki/pm_playbook.md",
    ".project_manager/wiki/pm_state.md",
    ".project_manager/wiki/status.md",
)
# lite = full 에서 pm_role.md·pm_playbook.md 를 제외한 진입 (= lite-A 가설).
# NOTE: 이건 T-0010(lite 어댑터) 전의 *프록시*다 — 실제 lite 진입 파일이 아직
#       없으므로, 무거운 방법론 문서 2종을 lazy 로 뺀 가설을 수동 구성해 측정한다.
#       측정 결과로 T-0010 의 lite-A 범위를 확정한다.
_LITE_EXCLUDE: frozenset[str] = frozenset(
    {".project_manager/wiki/pm_role.md", ".project_manager/wiki/pm_playbook.md"}
)
LITE_FILESET: tuple[str, ...] = tuple(f for f in FULL_FILESET if f not in _LITE_EXCLUDE)

WEIGHT_FILESETS: dict[str, tuple[str, ...]] = {
    "full": FULL_FILESET,
    "lite": LITE_FILESET,
}

# 진입 측정 프롬프트 — 진입셋만 read 시키고 즉시 종료시켜 input_tokens 를 진입
# 비용 근사로 잡는다. 시나리오 완주 프롬프트는 --scenario 로 별도 구성.
ENTRY_PROBE_REPLY = "done"

# char/4 휴리스틱 토큰 추정 분모 — 로컬 토크나이저(tiktoken/anthropic) 부재 시
# 정적 프록시. 실제 토큰은 라이브 하니스 report 가 진실.
_CHAR4_DIVISOR = 4

# 라이브 subprocess 타임아웃 (초). 진입 probe 는 짧지만 시나리오 완주는 길 수 있다.
LIVE_TIMEOUT_SEC = 600


# ── 순수 함수: claude usage 파싱 ──────────────────────────────────────────────

def parse_claude_usage(json_str: str) -> dict[str, Any]:
    """claude `-p --output-format json` 출력을 정규화 dict 로 파싱한다.

    실제 스키마(캡처본): 최상위 `total_cost_usd`·`num_turns`·`result`,
    `usage.{input_tokens,output_tokens,cache_read_input_tokens,
    cache_creation_input_tokens}`, `modelUsage.<model_id>.{inputTokens,...,
    costUSD,contextWindow}`.

    반환: input/output/cache_read/cache_creation/cost 정규화 + model·
    context_window·num_turns·result. 누락 키에 KeyError 안 남(0/None fallback).
    """
    data = json.loads(json_str)
    usage = data.get("usage") or {}
    model_usage = data.get("modelUsage") or {}

    # modelUsage 는 첫 모델 항목을 대표로 취한다(보통 단일 모델). 비면 {}.
    model_id: str | None = None
    model_entry: dict[str, Any] = {}
    for key, val in model_usage.items():
        model_id = key
        model_entry = val or {}
        break

    return {
        "input": int(usage.get("input_tokens", 0) or 0),
        "output": int(usage.get("output_tokens", 0) or 0),
        "cache_read": int(usage.get("cache_read_input_tokens", 0) or 0),
        "cache_creation": int(usage.get("cache_creation_input_tokens", 0) or 0),
        "cost": float(data.get("total_cost_usd", 0.0) or 0.0),
        "num_turns": int(data.get("num_turns", 0) or 0),
        "result": data.get("result", ""),
        "model": model_id,
        "context_window": model_entry.get("contextWindow"),
    }


# ── 순수 함수: opencode usage 파싱 ───────────────────────────────────────────

def _opencode_events(json_str: str) -> list[dict[str, Any]]:
    """opencode `run --format json` 출력을 이벤트 리스트로 파싱한다.

    실제 출력(opencode 1.17.6, `opencode run --help` + 라이브 캡처로 확인)은
    **JSONL 이벤트 스트림**이다 — 한 줄당 한 JSON 객체. 단일 JSON 배열/객체로
    오는 변종도 관용적으로 흡수한다(스키마 불확실성 방어).
    """
    json_str = json_str.strip()
    if not json_str:
        return []
    # 변종 1: 단일 JSON 값 (배열 또는 객체).
    try:
        whole = json.loads(json_str)
    except json.JSONDecodeError:
        whole = None
    if isinstance(whole, list):
        return [e for e in whole if isinstance(e, dict)]
    if isinstance(whole, dict):
        return [whole]
    # 변종 2(기본): JSONL — 줄마다 파싱, 깨진 줄은 건너뛴다.
    events: list[dict[str, Any]] = []
    for line in json_str.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events


def _dig(obj: Any, *keys: str) -> Any:
    """중첩 dict 에서 keys 경로를 따라 값을 꺼낸다. 없으면 None (KeyError 안전)."""
    cur = obj
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _first_num(*candidates: Any) -> float:
    """후보 중 첫 숫자값을 반환(없으면 0). 여러 후보 키를 try 하는 방어 헬퍼."""
    for cand in candidates:
        if isinstance(cand, bool):
            continue
        if isinstance(cand, (int, float)):
            return cand
    return 0


def parse_opencode_usage(json_str: str) -> dict[str, Any]:
    """opencode `run --format json` 출력을 정규화 dict 로 파싱한다.

    실제 스키마(라이브 캡처): JSONL 이벤트 스트림. `step_finish` 이벤트의
    `part.tokens.{total,input,output,reasoning,cache.{write,read}}` 와
    `part.cost` 가 토큰·비용을 담고, `text` 이벤트의 `part.text` 가 결과다.
    멀티스텝(tool 사용) 시 step_finish 가 여러 개 — 토큰·비용을 합산하고 text
    를 이어붙인다.

    스키마 불확실 → 여러 후보 키를 관용적으로 탐색한다(`tokens` 가 part 밖/
    이벤트 최상위에 오거나, `cost` 가 다른 이름으로 와도 흡수). 누락 키에
    KeyError 안 남.

    반환: input/output/cache_read/cache_creation/cost/total 정규화 + result.
    """
    events = _opencode_events(json_str)

    total_input = 0.0
    total_output = 0.0
    total_cache_read = 0.0
    total_cache_write = 0.0
    total_tokens = 0.0
    total_cost = 0.0
    text_parts: list[str] = []

    for ev in events:
        part = ev.get("part") if isinstance(ev.get("part"), dict) else {}

        # 토큰: part.tokens 우선, 없으면 이벤트 최상위 tokens 후보.
        tokens = _dig(part, "tokens")
        if not isinstance(tokens, dict):
            tokens = ev.get("tokens") if isinstance(ev.get("tokens"), dict) else {}
        if tokens:
            total_input += _first_num(tokens.get("input"), tokens.get("input_tokens"))
            total_output += _first_num(tokens.get("output"), tokens.get("output_tokens"))
            total_tokens += _first_num(tokens.get("total"), tokens.get("total_tokens"))
            cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
            total_cache_read += _first_num(
                cache.get("read"), tokens.get("cache_read"), tokens.get("cache_read_input_tokens")
            )
            total_cache_write += _first_num(
                cache.get("write"), tokens.get("cache_write"), tokens.get("cache_creation_input_tokens")
            )

        # 비용: part.cost 우선, 없으면 이벤트 최상위 cost / costUSD 후보.
        cost = _first_num(part.get("cost"), ev.get("cost"), ev.get("costUSD"))
        total_cost += cost

        # 결과 텍스트: text 이벤트의 part.text 를 모은다.
        if ev.get("type") == "text":
            txt = part.get("text")
            if isinstance(txt, str):
                text_parts.append(txt)

    return {
        "input": int(total_input),
        "output": int(total_output),
        "cache_read": int(total_cache_read),
        "cache_creation": int(total_cache_write),
        "total": int(total_tokens),
        "cost": float(total_cost),
        "result": "".join(text_parts),
    }


# ── 순수 함수: 진입 파일셋 정적 계측 ──────────────────────────────────────────

def count_entry_files(fileset: tuple[str, ...] | list[str], root: Path) -> dict[str, Any]:
    """진입 파일셋의 byte 수·문자 수(정적 프록시)와 per-file breakdown·합계를 잰다.

    로컬 토크나이저가 없으므로 char/4 휴리스틱 토큰 추정치(`approx_tokens_char4`)
    도 같이 반환한다 — 필드명으로 *추정*임을 표시한다. 실제 토큰은 라이브 하니스
    report 가 진실.

    누락 파일은 exists=False·bytes=0·chars=0 으로 기록(에러 raise 안 함) — plan
    모드가 누락을 가시화할 수 있게.
    """
    files: list[dict[str, Any]] = []
    total_bytes = 0
    total_chars = 0
    for rel in fileset:
        path = Path(root) / rel
        if path.is_file():
            raw = path.read_bytes()
            n_bytes = len(raw)
            n_chars = len(raw.decode("utf-8", errors="replace"))
            exists = True
        else:
            n_bytes = 0
            n_chars = 0
            exists = False
        total_bytes += n_bytes
        total_chars += n_chars
        files.append(
            {
                "path": rel,
                "exists": exists,
                "bytes": n_bytes,
                "chars": n_chars,
                "approx_tokens_char4": n_chars // _CHAR4_DIVISOR,
            }
        )
    return {
        "files": files,
        "total_bytes": total_bytes,
        "total_chars": total_chars,
        "total_approx_tokens_char4": total_chars // _CHAR4_DIVISOR,
    }


# ── 순수 함수: 결과 표 렌더 ───────────────────────────────────────────────────

# 표 컬럼 — full vs lite × (진입토큰·완주토큰·자족성). 라이브 미측정 항목은 row
# 에서 누락 가능 → "—" placeholder.
_TABLE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("weight", "weight"),
    ("entry_tokens", "진입토큰"),
    ("scenario_tokens", "완주토큰"),
    ("self_sufficiency", "자족성"),
)


def _fmt_cell(val: Any) -> str:
    if val is None:
        return "—"
    if isinstance(val, float):
        return f"{val:g}"
    return str(val)


def render_table(rows: list[dict[str, Any]]) -> str:
    """full vs lite × (진입토큰·완주토큰·자족성) markdown 표를 생성한다.

    각 row 는 `weight`·`entry_tokens`·`scenario_tokens`·`self_sufficiency` 키를
    가질 수 있다(누락 시 "—"). 표는 헤더·구분선·데이터 행으로 구성된다.
    """
    headers = [label for _, label in _TABLE_COLUMNS]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        cells = [_fmt_cell(row.get(key)) for key, _ in _TABLE_COLUMNS]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


# ── plan(dry) 모드 ────────────────────────────────────────────────────────────

def build_entry_probe_prompt(fileset: tuple[str, ...] | list[str]) -> str:
    """진입 측정 프롬프트 — 진입셋만 read 시키고 즉시 종료(input_tokens=진입 근사)."""
    listing = ", ".join(fileset)
    return f"Read these files: {listing}. Then reply exactly: {ENTRY_PROBE_REPLY}."


def render_plan(harness: str, weight: str, scenario: str, root: Path) -> str:
    """dry/plan 모드 출력 — 측정 없이 진입 파일셋·프롬프트·sandbox 계획만 보여준다."""
    fileset = WEIGHT_FILESETS[weight]
    counts = count_entry_files(fileset, root)
    lines: list[str] = []
    lines.append(f"# bench_weight plan (dry — no measurement, 0 tokens)")
    lines.append("")
    lines.append(f"- harness: {harness}")
    lines.append(f"- weight: {weight}")
    lines.append(f"- scenario: {scenario}")
    lines.append(f"- root: {root}")
    lines.append("")
    lines.append("## 진입 파일셋 (정적 프록시)")
    lines.append("")
    lines.append("| 파일 | 존재 | bytes | chars | ~tok(char/4) |")
    lines.append("| --- | --- | --- | --- | --- |")
    for f in counts["files"]:
        lines.append(
            f"| {f['path']} | {'O' if f['exists'] else 'MISSING'} "
            f"| {f['bytes']} | {f['chars']} | {f['approx_tokens_char4']} |"
        )
    lines.append(
        f"| **합계** |  | {counts['total_bytes']} | {counts['total_chars']} "
        f"| {counts['total_approx_tokens_char4']} |"
    )
    lines.append("")
    lines.append("## 진입 측정 프롬프트")
    lines.append("")
    lines.append(f"    {build_entry_probe_prompt(fileset)}")
    lines.append("")
    lines.append("## sandbox 계획 (--live 시)")
    lines.append("")
    lines.append("- XDG_{CONFIG,DATA,STATE,CACHE}_HOME → tempfile.mkdtemp() 하위 격리")
    lines.append("- 실 프로젝트 config/data 오염 없음")
    lines.append("- NOTE: 토큰 0 — 실제 측정은 --live 로.")
    return "\n".join(lines)


# ── 라이브 러너 (--live · XDG sandbox) ────────────────────────────────────────

class LiveBenchError(RuntimeError):
    """라이브 측정 실패 — fail-soft 금지(명시적 non-zero exit 신호)."""


def _make_xdg_sandbox() -> tuple[dict[str, str], str]:
    """XDG_*_HOME 을 tempfile.mkdtemp() 하위로 격리한 env override 와 base dir 을 반환한다.

    실 프로젝트의 ~/.config·~/.local 오염을 막는다. caller 가 base dir 정리 책임.
    """
    base = tempfile.mkdtemp(prefix="bench_weight_xdg_")
    env = dict(os.environ)
    env["XDG_CONFIG_HOME"] = os.path.join(base, "config")
    env["XDG_DATA_HOME"] = os.path.join(base, "data")
    env["XDG_STATE_HOME"] = os.path.join(base, "state")
    env["XDG_CACHE_HOME"] = os.path.join(base, "cache")
    for sub in ("config", "data", "state", "cache"):
        os.makedirs(os.path.join(base, sub), exist_ok=True)
    return env, base


def _run_subprocess(cmd: list[str], env: dict[str, str]) -> str:
    """subprocess 구동 — stdout 반환. timeout·non-zero·빈 출력은 LiveBenchError raise."""
    try:
        proc = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=LIVE_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired as exc:
        raise LiveBenchError(f"라이브 측정 타임아웃 ({LIVE_TIMEOUT_SEC}s): {' '.join(cmd)}") from exc
    if proc.returncode != 0:
        raise LiveBenchError(
            f"라이브 하니스 non-zero exit ({proc.returncode}): {' '.join(cmd)}\n"
            f"--- stderr ---\n{proc.stderr.strip()}"
        )
    if not proc.stdout.strip():
        raise LiveBenchError(f"라이브 하니스 빈 출력: {' '.join(cmd)}")
    return proc.stdout


def run_live(harness: str, weight: str, scenario: str, root: Path) -> dict[str, Any]:
    """--live 측정 — XDG sandbox 격리 후 하니스를 구동해 진입 usage 를 파싱한다.

    진입 측정만 정확히 잡는다. 자족성(시나리오 완주 후 진입셋 밖 read 카운트)은
    하니스 transcript 접근이 불확실하므로 placeholder 로 둔다 — PM 이 라이브 시
    수동 판정한다(아래 self_sufficiency 필드). 토큰은 정확.

    실패(timeout·non-zero·빈 JSON)는 LiveBenchError 로 raise — fail-soft 금지.
    """
    fileset = WEIGHT_FILESETS[weight]
    prompt = build_entry_probe_prompt(fileset)
    env, base = _make_xdg_sandbox()
    try:
        if harness == "claude":
            cmd = ["claude", "-p", prompt, "--output-format", "json"]
            out = _run_subprocess(cmd, env)
            usage = parse_claude_usage(out)
        elif harness == "opencode":
            cmd = ["opencode", "run", prompt, "--format", "json"]
            out = _run_subprocess(cmd, env)
            usage = parse_opencode_usage(out)
        else:  # argparse choices 가 막지만 방어.
            raise LiveBenchError(f"지원하지 않는 harness: {harness}")
    finally:
        # sandbox 정리는 best-effort (디버그 위해 실패해도 측정 결과는 유지).
        pass

    return {
        "harness": harness,
        "weight": weight,
        "scenario": scenario,
        "sandbox": base,
        "entry_input_tokens": usage.get("input", 0),
        "entry_output_tokens": usage.get("output", 0),
        "cost": usage.get("cost", 0.0),
        "usage": usage,
        # 자족성: 라이브 transcript 접근 불확실 → PM 수동 판정 placeholder.
        # (시나리오 완주 후 진입셋 밖 파일 추가 read 수를 PM 이 라이브로 센다.)
        "self_sufficiency": "PM-manual-pending",
    }


# ── CLI ──────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bench_weight.py",
        description="벤치마크 — full vs lite 진입 토큰·자족성 정량 (XDG sandbox). T-0008.",
    )
    parser.add_argument(
        "--harness",
        required=True,
        choices=("claude", "opencode"),
        help="측정 하니스.",
    )
    parser.add_argument(
        "--weight",
        required=True,
        choices=("full", "lite"),
        help="진입 무게 (full = 부트스트랩 전체; lite = pm_role·pm_playbook 제외 프록시).",
    )
    parser.add_argument(
        "--scenario",
        required=True,
        metavar="ID",
        help="고정 마이크로워크플로 시나리오 ID (재현용 기록).",
    )
    parser.add_argument(
        "--out",
        metavar="경로",
        default=None,
        help="결과 기록 경로 (미지정 시 stdout).",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="실제 하니스를 구동해 측정한다 (기본은 dry/plan — 토큰 0).",
    )
    return parser


def _emit(text: str, out: str | None) -> None:
    """text 를 out 파일 또는 stdout 으로 내보낸다."""
    if out:
        Path(out).write_text(text + "\n", encoding="utf-8")
        print(f"wrote {out}")
    else:
        print(text)


def main(argv: list[str] | None = None) -> int:
    # 콘솔/파이프 출력을 UTF-8 로 재설정 — cp949 콘솔이나 리다이렉트된 stdout 에서
    # 이모지·em-dash(—) print 가 UnicodeEncodeError 로 죽는 것을 막는다 (T-0017).
    # reconfigure 미지원 스트림(테스트 캡처 등)은 hasattr 가드로 건너뛴다.
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.live:
        plan = render_plan(args.harness, args.weight, args.scenario, REPO)
        _emit(plan, args.out)
        return 0

    # --live: 실제 측정 — 실패는 명시적 non-zero exit (fail-soft 금지).
    try:
        result = run_live(args.harness, args.weight, args.scenario, REPO)
    except LiveBenchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    row = {
        "weight": result["weight"],
        "entry_tokens": result["entry_input_tokens"],
        "scenario_tokens": None,  # 시나리오 완주 측정은 PM 라이브 운용(자족성과 함께).
        "self_sufficiency": result["self_sufficiency"],
    }
    report = "\n".join(
        [
            f"# bench_weight live — {result['harness']} / {result['weight']} / {result['scenario']}",
            "",
            f"- sandbox: {result['sandbox']}",
            f"- cost_usd: {result['cost']}",
            "",
            render_table([row]),
        ]
    )
    _emit(report, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
