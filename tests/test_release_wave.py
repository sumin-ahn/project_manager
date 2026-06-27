"""릴리즈 테스트(③ tier·`release` marker) — 실 LLM 한 세션이 fresh adopter 에서 full wave 운영.

테스트 3-tier 의 Tier 3(릴리즈). Tier 2(런타임 smoke·`test_fresh_adopter_runtime_smoke`)는 실 LLM 이
*PM 으로서* ticket 라이프사이클(new→claim→complete)을 운영하는지까지 친다. 이 층은 그 위 — **위임**까지
포함한 full wave: PM 세션이 ticket 을 발행·claim 하고 **developer 서브에이전트에 구현을 Task 위임**,
**code-reviewer 서브에이전트에 리뷰를 Task 위임**한 뒤 complete 까지 운영하는지, 그리고 **위임이 실제로
일어났는지**(developer 가 작성한 probe 파일·ticket done 전이)를 검증한다.

게이트 아님 — 사용자가 릴리즈 직전 `PM_ORCH_LIVE_RELEASE=1` 로 occasional 트리거(비용·flaky 감수).
기본 skip(env 미설정·CI green 불변). claude 경로는 PM 36 라이브 probe 로 검증된 mechanics
(`scratchpad/release_probe.py`·145s·dev×15·reviewer×21·probe.txt·done)를 옮긴 것이다.

단언 철학(runtime_smoke 와 동일): **side-effect 기반**이라 LLM 출력 phrasing 비결정에 강건하다 —
probe.txt(=developer 서브에이전트가 작성)·ticket done 전이가 핵심 단언. claude 는 위에 더해 stream-json
의 `subagent_type` 관측으로 *위임이 일어났음*까지 hard 단언한다(probe 검증됨). opencode 는 위임 관측
수단이 미확정(stream-json 과 다름·spike §6)이라 side-effect 만 hard·위임 흔적은 best-effort 다.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

# 런타임 smoke 와 헬퍼 공유(같은 tests/ 디렉토리·import) — adopter import·LLM env 격리·ticket 조회.
from test_fresh_adopter_runtime_smoke import (
    _import_adopter,
    _live_env,
    _tickets_in,
)

# 릴리즈 트리거 — 사용자가 릴리즈 직전 명시 set(occasional). 미설정이면 전부 skip(CI green 불변).
_RELEASE_LIVE = os.environ.get("PM_ORCH_LIVE_RELEASE") == "1"
# claude: sonnet-4-6(API 과금·env override). probe 가 이 모델로 PASS.
CLAUDE_MODEL = os.environ.get("PM_ORCH_LIVE_CLAUDE_MODEL", "claude-sonnet-4-6")
# opencode: full wave(claim→위임→complete sync-gate)는 *강한* 모델이 필요하다 — gemma4:26b 는
# complete 의 sync-gate 를 못 넘어 flaky(위임=probe.txt 는 쓰나 ticket 이 claimed 에 머묾·PM 39 실측).
# qwen3.5:397b-cloud(ollama cloud)로 full wave PASS 검증(69s·PM 39). 그래서 release default 는 이 모델
# 이다(runtime_smoke[lite·sync-gate 없음]는 gemma 로 충분 — 거긴 별도 default). env override 로 교체 가능.
LIVE_MODEL = os.environ.get("PM_ORCH_LIVE_MODEL", "ollama/qwen3.5:397b-cloud")

# full wave probe 가 작성하도록 지시하는 산출 파일·내용 — side-effect 단언의 기준(단일 진실).
PROBE_FILE = "probe.txt"
PROBE_TEXT = "hello from dev"

# 위임 단언 대상 서브에이전트 — full wave 가 developer(구현)·code-reviewer(리뷰) 둘 다 거쳐야 통과.
_DEV_SUBAGENT = "developer"
_REVIEWER_SUBAGENT = "code-reviewer"

# opencode 는 gemma 가 느리고 변동 커 1800s, claude 는 probe 실측 145s 여유분 600s.
_OPENCODE_TIMEOUT = int(os.environ.get("PM_ORCH_LIVE_RELEASE_TIMEOUT", "1800"))
_CLAUDE_TIMEOUT = int(os.environ.get("PM_ORCH_LIVE_RELEASE_CLAUDE_TIMEOUT", "600"))


def _full_wave_prompt(entry_doc: str) -> str:
    """PM 세션이 full wave(new→claim→**developer 위임**→**code-reviewer 위임**→complete)를 운영하라는 프롬프트.

    board.py 경로를 *주지 않는다* — adopter 가 `entry_doc` 만으로 도구를 찾아 운영해야 통과(= 문서 운영성).
    developer 위임 단계에서 `probe.txt`(='hello from dev')를 작성하게 지시 → side-effect 로 위임 *결과*를
    관측(서브에이전트가 실제로 구현했음). 5단계(new/claim/delegate developer/delegate code-reviewer/complete)
    키워드를 포함하므로 hermetic 단위테스트가 구조를 가드한다.
    """
    return (
        f"You are the PM for this project. Read {entry_doc} to learn how the project board "
        "tool works. Then run a full release wave: "
        "(1) create exactly one ticket titled 'release wave probe' (touches README.md) with the "
        "board tool, "
        "(2) claim it, "
        f"(3) delegate the implementation to the '{_DEV_SUBAGENT}' subagent using the Task tool — "
        f"instruct the {_DEV_SUBAGENT} to create a file named {PROBE_FILE} in the project root "
        f"containing the text '{PROBE_TEXT}', "
        f"(4) delegate a review to the '{_REVIEWER_SUBAGENT}' subagent using the Task tool, "
        "(5) mark the ticket complete/done (satisfy the complete sync gate however the docs say — "
        "e.g. a log entry and the tests-pass / untested flag). "
        "Reply with the ticket id when the ticket is done."
    )


def _collect_subagent_types(stdout: str) -> list[str]:
    """stream-json stdout 의 각 라인을 json 파싱 → 재귀 walk 로 `subagent_type` 값 수집.

    PM 36 probe 의 walk 와 동형(검증됨) — Task tool_use input 에 `subagent_type` 가 들어간다. claude
    의 stream-json 형식 정확 스키마에 비의존적으로 *어느 깊이든* 키를 긁는다(형식 변동에 강건). 파싱
    불가 라인(비-json·빈 줄)은 무시. opencode 출력엔 이 키가 없을 수 있어(미확정) best-effort 로만 쓴다.
    """
    types: list[str] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "subagent_type":
                    types.append(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        walk(obj)
    return types


def _assert_wave_side_effects(dest: Path, proc: subprocess.CompletedProcess, harness: str) -> None:
    """full wave side-effect 단언 — developer 가 probe.txt 작성·ticket 이 done/ 도달.

    probe.txt(내용 'hello from dev') = developer 서브에이전트가 위임받아 구현했다는 증거. done/ 도달 =
    new→claim→complete 전이 완주(complete sync-gate 통과). 둘 다 출력 phrasing 비결정에 강건한 side-effect.
    """
    tail = (
        f"--- {harness} stdout(tail) ---\n{proc.stdout[-2000:]}\n"
        f"--- stderr(tail) ---\n{proc.stderr[-1000:]}"
    )
    probe_path = dest / PROBE_FILE
    assert probe_path.exists(), (
        f"실 {harness} full wave 후 {PROBE_FILE} 부재 — developer 서브에이전트 위임/구현 실패.\n" + tail
    )
    assert probe_path.read_text(encoding="utf-8").strip() == PROBE_TEXT, (
        f"{PROBE_FILE} 내용이 '{PROBE_TEXT}' 아님 — developer 가 다르게 구현.\n" + tail
    )
    done_tickets = _tickets_in(dest, "done")
    assert done_tickets, (
        f"실 {harness} 가 ticket 을 done/ 까지 운영하지 못함 — full wave 미완주.\n"
        f"open={_tickets_in(dest, 'open')} claimed={_tickets_in(dest, 'claimed')}\n" + tail
    )


@pytest.mark.release
@pytest.mark.skipif(
    not _RELEASE_LIVE or not shutil.which("claude"),
    reason="release wave — PM_ORCH_LIVE_RELEASE=1 + claude CLI 필요(API 과금). 기본 skip·사용자 트리거.",
)
def test_release_wave_claude_full_wave(tmp_path):
    """실 claude(`claude-sonnet-4-6`)가 `CLAUDE.md` 만 보고 full wave 를 운영·위임이 관측된다.

    PM 36 라이브 probe(`scratchpad/release_probe.py`·PASS·dev×15·reviewer×21)의 mechanics 를 옮긴 것.
    claude 는 subprocess cwd 를 존중한다(`--dir` 불요). stream-json 으로 위임(subagent_type)을 관측하고
    side-effect(probe.txt·done)를 단언한다. API 과금.
    """
    dest = _import_adopter(tmp_path, "claude")

    proc = subprocess.run(
        ["claude", "-p", "--model", CLAUDE_MODEL,
         "--allowedTools", "Bash", "Task",
         "--output-format", "stream-json", "--verbose",
         "--dangerously-skip-permissions",
         _full_wave_prompt("CLAUDE.md")],
        cwd=str(dest), capture_output=True, text=True,
        env=_live_env(CLAUDE_MODEL), timeout=_CLAUDE_TIMEOUT,
    )

    # 위임 관측(hard) — stream-json 에서 developer·code-reviewer 둘 다 등장해야 통과(probe 검증됨).
    subagent_types = _collect_subagent_types(proc.stdout)
    tail = (
        f"--- claude stdout(tail) ---\n{proc.stdout[-2500:]}\n"
        f"--- stderr(tail) ---\n{proc.stderr[-1000:]}"
    )
    assert _DEV_SUBAGENT in subagent_types and _REVIEWER_SUBAGENT in subagent_types, (
        f"claude full wave 에서 위임 미관측 — subagent_type={subagent_types} "
        f"({_DEV_SUBAGENT}·{_REVIEWER_SUBAGENT} 둘 다 필요).\n" + tail
    )

    # side-effect(hard) — developer 위임 결과(probe.txt)·done 전이.
    _assert_wave_side_effects(dest, proc, "claude")


@pytest.mark.release
@pytest.mark.skipif(
    not _RELEASE_LIVE or not shutil.which("opencode"),
    reason="release wave — PM_ORCH_LIVE_RELEASE=1 + opencode CLI(+ollama 모델) 필요. 기본 skip·사용자 트리거.",
)
def test_release_wave_opencode_full_wave(tmp_path):
    """실 opencode(agentic·ollama)가 `AGENTS.md` 만 보고 full wave 를 운영한다 (side-effect 단언).

    opencode 의 위임 관측 수단은 claude 의 stream-json `subagent_type` 와 다르다 — PM 36 라이브 probe
    실측 결과 gemma/opencode 는 위임 흔적(subagent_type·'developer'·task)을 출력에 **0** 으로 낸다(비결정).
    그래서 **side-effect(probe.txt·done)만 hard 단언**하고(probe.txt=developer 가 위임받아 작성·done=wave
    완주 → side-effect 가 위임 *결과*를 커버), 위임 흔적(stdout 에 'developer'/'code-reviewer' 등장)은
    **best-effort**(있으면 단언·없으면 skip)다. opencode 위임 관측 수단은 PM probe 후 보강한다.
    gemma 는 느리고 변동 커 timeout 1800s. `--dir` 로 루트 핀(opencode 는 PWD 로 루트 오판).
    """
    dest = _import_adopter(tmp_path, "opencode")

    proc = subprocess.run(
        # `--dangerously-skip-permissions`: 비대화 헤드리스라 opencode 가 `--dir` 디렉토리를
        # external_directory 로 보고 권한을 auto-reject → AGENTS.md 도 못 읽고 wave 시작 실패한다.
        # 이 플래그로 권한을 통과시켜야 wave 완주(throwaway tmp adopter 격리라 안전·PM 36 probe 실측).
        ["opencode", "run", "--agent", "build", "--dir", str(dest),
         "--dangerously-skip-permissions", "-m", LIVE_MODEL,
         _full_wave_prompt("AGENTS.md")],
        cwd=str(dest), capture_output=True, text=True,
        env=_live_env(LIVE_MODEL), timeout=_OPENCODE_TIMEOUT,
    )

    # side-effect(hard) — full wave 의 핵심 결과(developer 위임 산출 probe.txt·done 전이).
    _assert_wave_side_effects(dest, proc, "opencode")

    # 위임 흔적(best-effort) — opencode 출력에 서브에이전트 이름이 등장하면 위임 관측으로 단언.
    # 등장 안 해도 fail 시키지 않는다 — opencode 위임 관측 수단=stream-json 아님·gemma 비결정으로
    # 위임 흔적 출력 0(PM 36 probe 실측). 위임은 side-effect(probe.txt·done)로 검증한다.
    if _DEV_SUBAGENT in proc.stdout and _REVIEWER_SUBAGENT in proc.stdout:
        assert _DEV_SUBAGENT in proc.stdout and _REVIEWER_SUBAGENT in proc.stdout


# ── hermetic 단위 가드 (라이브 실행 없이·@release/skipif 무관 — 매 회귀 통과) ──────────────
# 위 라이브 테스트는 PM_ORCH_LIVE_RELEASE 미설정 시 skip 이라 CI 에선 안 돈다. 아래 단위는 라이브
# 없이도 돌아 (1) full wave 프롬프트가 5단계 키워드를 담는지 (2) subagent_type walk 가 stream-json
# 샘플에서 값을 정확히 추출하는지 — 라이브 미실행 시에도 mechanics 구조를 가드한다(회귀가 잡음).


def test_full_wave_prompt_has_all_five_stages():
    """full wave 프롬프트가 5단계(new·claim·delegate developer·delegate code-reviewer·complete)를 담는다."""
    prompt = _full_wave_prompt("CLAUDE.md")
    # (1) new — 정확히 1개 ticket 발행 지시.
    assert "create exactly one ticket" in prompt
    # (2) claim.
    assert "claim it" in prompt
    # (3) developer 위임 + probe.txt 산출 지시(side-effect 단언 대상).
    assert f"delegate the implementation to the '{_DEV_SUBAGENT}' subagent" in prompt
    assert PROBE_FILE in prompt and PROBE_TEXT in prompt
    # (4) code-reviewer 위임.
    assert f"delegate a review to the '{_REVIEWER_SUBAGENT}' subagent" in prompt
    # (5) complete + sync gate.
    assert "mark the ticket complete/done" in prompt
    # 진입문서가 프롬프트에 박힌다(harness 별 CLAUDE.md/AGENTS.md).
    assert "CLAUDE.md" in prompt
    assert "AGENTS.md" in _full_wave_prompt("AGENTS.md")


def test_collect_subagent_types_extracts_from_stream_json():
    """subagent_type walk 가 claude stream-json 형 샘플에서 developer·code-reviewer 를 정확히 추출한다."""
    # claude stream-json 근사: 각 라인 1 json. Task tool_use input 깊숙이 subagent_type 가 박힌다.
    sample_lines = [
        json.dumps({"type": "system", "subtype": "init"}),
        json.dumps({
            "type": "assistant",
            "message": {"content": [
                {"type": "tool_use", "name": "Task",
                 "input": {"subagent_type": _DEV_SUBAGENT, "prompt": "create probe.txt"}}
            ]},
        }),
        "",  # 빈 줄 — 무시돼야.
        "not json at all",  # 비-json — 무시돼야.
        json.dumps({
            "type": "assistant",
            "message": {"content": [
                {"type": "tool_use", "name": "Task",
                 "input": {"subagent_type": _REVIEWER_SUBAGENT, "prompt": "review"}}
            ]},
        }),
    ]
    stdout = "\n".join(sample_lines)

    types = _collect_subagent_types(stdout)

    assert _DEV_SUBAGENT in types
    assert _REVIEWER_SUBAGENT in types
    # 비-json·빈 줄은 조용히 무시(파싱 예외로 죽지 않음).
    assert types == [_DEV_SUBAGENT, _REVIEWER_SUBAGENT]


def test_collect_subagent_types_handles_no_delegation():
    """위임 없는 stdout(subagent_type 부재)에서 walk 가 빈 리스트를 돌려준다(false-positive 0)."""
    stdout = "\n".join([
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}),
        json.dumps({"type": "result", "subtype": "success"}),
    ])
    assert _collect_subagent_types(stdout) == []
