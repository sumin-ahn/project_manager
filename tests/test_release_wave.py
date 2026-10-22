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

from pm_engine.infra import pm_paths
import pm_engine.app.session_identity

import ast
import importlib.util
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from pm_engine.domain import conf_schema
from pm_engine.infra import conf_store

from conftest import codex_auth_available, drop_codex_auth, make_codex_home, run_codex_exec

# 런타임 smoke 와 헬퍼 공유(같은 tests/ 디렉토리·import) — adopter import·LLM env 격리·ticket 조회.
# `_load_pm_import`(pm_import 모듈 로드)·`_real_models_runner` 스텁은 multi-repo 셋업 헬퍼에서도 재사용.
from test_fresh_adopter_runtime_smoke import (
    _import_adopter,
    _live_env,
    _load_pm_import,
    _tickets_in,
)
from pm_engine.infra.harness import watchdog
from pm_engine.app import regression
from pm_engine.domain import review_form

# 릴리즈 트리거 — 사용자가 릴리즈 직전 명시 set(occasional). 미설정이면 전부 skip(CI green 불변).
_RELEASE_LIVE = os.environ.get("PM_ORCH_LIVE_RELEASE") == "1"
# claude: sonnet-4-6(API 과금·env override). probe 가 이 모델로 PASS.
CLAUDE_MODEL = os.environ.get("PM_ORCH_LIVE_CLAUDE_MODEL", "claude-sonnet-4-6")
# opencode: full wave(claim→위임→complete sync-gate)는 *강한* 모델이 필요하다 — gemma4:26b 는
# complete 의 sync-gate 를 못 넘어 flaky(위임=probe.txt 는 쓰나 ticket 이 claimed 에 머묾·PM 39 실측).
# glm-5.2:cloud(ollama cloud) 강한 모델을 release default 로 쓴다(2026-07-07 채택·라이브 실측은
# 릴리즈 wave). runtime_smoke[lite·sync-gate 없음]는 gemma 로 충분 — 거긴 별도 default.
# env override 로 교체 가능.
LIVE_MODEL = os.environ.get("PM_ORCH_LIVE_MODEL", "ollama/glm-5.2:cloud")
CODEX_MODEL = os.environ.get("PM_ORCH_LIVE_CODEX_MODEL")

# full wave probe 가 작성하도록 지시하는 산출 파일·내용 — side-effect 단언의 기준(단일 진실).
PROBE_FILE = "probe.txt"
PROBE_TEXT = "hello from dev"

# 위임 단언 대상 서브에이전트 — developer가 구현(02)과 terminal fix(04)에 두 번 등장한다.
_ARCH_SUBAGENT = "architect"
_DEV_SUBAGENT = "developer"
_REVIEWER_SUBAGENT = "code-reviewer"
_GROWTH_PIPELINE = (
    (_ARCH_SUBAGENT, "LIVE_TICKET_ARCHITECT_PERSISTED"),
    (_DEV_SUBAGENT, "LIVE_TICKET_DEVELOPER_PERSISTED"),
    (_REVIEWER_SUBAGENT, "LIVE_TICKET_REVIEWER_PERSISTED"),
    (_DEV_SUBAGENT, "LIVE_TICKET_FINAL_FIX_PERSISTED"),
)
_WAVE_TEST_FILE = "tests/test_release_wave_probe.py"
# full wave 의 작업 폴더 형상([[T-0956]]) — task `wave` 가 작업 폴더 2개를 보유한다. 1번은 base_branch 를
# 계속 드는 통합 폴더, 2번은 묶음 브랜치 `task/wave` 로 전환한 작업 폴더다(커맨드 카드의 통합 폴더 형상).
_WAVE_TASK = "wave"
_WAVE_CLUSTER = "wave"
_WAVE_INTEGRATION_SLOT = 1
_WAVE_WORK_SLOT = 2
_WAVE_TARGETED_COMMAND = f"python3 -m pytest {_WAVE_TEST_FILE} -q -n auto"
_WAVE_FULL_COMMAND = "python3 -m pytest tests/ -q -n auto"


# opencode 는 gemma 가 느리고 변동 커 1800s. Claude full-wave는 고정 4단계+PM disposition
# 실측이 600s를 넘겨 기본 900s이고, 환경변수 override는 그대로 유지한다.
_OPENCODE_TIMEOUT = int(os.environ.get("PM_ORCH_LIVE_RELEASE_TIMEOUT", "1800"))
_CLAUDE_TIMEOUT_DEFAULT = 900
_CLAUDE_TIMEOUT = int(os.environ.get(
    "PM_ORCH_LIVE_RELEASE_CLAUDE_TIMEOUT", str(_CLAUDE_TIMEOUT_DEFAULT),
))
_CODEX_TIMEOUT = int(os.environ.get("PM_ORCH_LIVE_RELEASE_CODEX_TIMEOUT", "900"))

# Claude Code 2.1.241의 non-interactive `-p`는 prompt 문자열 `/compact`를 slash command로
# dispatch하지 않는다(PreCompact/PostCompact hook 0건 실측). 업무 중 compaction은 억제하고
# full-wave 완료 뒤 같은 세션을 낮은 native 경계로 resume해 compaction event를 유도한다.
_CLAUDE_WAVE_AUTOCOMPACT_THRESHOLD = "1m"
_CLAUDE_AUTOCOMPACT_THRESHOLD = "100k"

# T-0621 compaction boundary probe. 신규 @release 함수를 더하지 않고 기존 harness full-wave
# 항목에 결합해 전역 livegate 수 pin/board 소유 표면은 그대로 둔다.
_COMPACTION_RECOVERY_SENTINEL = "RECOVERED_AFTER_COMPACTION"
_CLAUDE_COMPACTION_PROBE_PROMPT = (
    "Use Bash exactly once to run `pwd`, then reply exactly "
    f"{_COMPACTION_RECOVERY_SENTINEL}"
)
_OPENCODE_COMPACTION_CONTEXT = 32768
_OPENCODE_COMPACTION_OUTPUT = 4096
# 현재 user prompt는 native compaction 대상이 아니므로 각 turn 자체가 context-output 입력
# 상한보다 충분히 작아야 한다. 12k ASCII chars는 최악의 1 char/token이어도 28,672-token
# 입력 상한보다 작고, 4 chars/token 기준 상한을 넘길 만큼 여러 turn을 누적한다.
_OPENCODE_COMPACTION_INPUT_LIMIT = (
    _OPENCODE_COMPACTION_CONTEXT - _OPENCODE_COMPACTION_OUTPUT
)
_OPENCODE_COMPACTION_TURN_CHARS = 12_000
_OPENCODE_COMPACTION_TARGET_HISTORY_CHARS = _OPENCODE_COMPACTION_INPUT_LIMIT * 4
_OPENCODE_COMPACTION_TURNS = (
    _OPENCODE_COMPACTION_TARGET_HISTORY_CHARS
    + _OPENCODE_COMPACTION_TURN_CHARS
    - 1
) // _OPENCODE_COMPACTION_TURN_CHARS

_TOOLS = Path(__file__).resolve().parents[1] / ".project_manager" / "tools"


def _pm_delegate_contract() -> tuple[str, int, str, int]:
    """라이브 prompt가 쓰는 review/architect schema 값을 엔진 상수에서 읽는다."""
    spec = importlib.util.spec_from_file_location(
        "_release_wave_pm_delegate", _TOOLS / "pm_delegate.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return (
        review_form.PM_REVIEW_BLOCK, review_form.PM_REVIEW_VERSION,
        review_form.ARCHITECT_TEST_BLOCK, review_form.ARCHITECT_TEST_VERSION,
    )


# 회수된 라운드가 담아야 하는 구조화 계약(엔진 파생).
(_PM_REVIEW_BLOCK, _PM_REVIEW_VERSION,
 _ARCHITECT_TEST_BLOCK, _ARCHITECT_TEST_VERSION) = _pm_delegate_contract()


def _compaction_checkpoint_count(dest: Path) -> int:
    """경계 훅의 durable side-effect만 센다(모델 출력 phrasing과 독립)."""
    current = dest / ".project_manager" / "wiki" / "log" / "current.md"
    try:
        text = current.read_text(encoding="utf-8")
    except OSError:
        return 0
    return sum(
        1 for line in text.splitlines()
        if line.startswith("## [") and "checkpoint |" in line and "— compaction" in line
    )


_OPENCODE_SNAPSHOT_RECEIPT_RE = re.compile(
    r"^compact-snapshot-receipt\.([A-Za-z0-9_-]+)\.([A-Za-z0-9_-]+)$"
)


def _opencode_snapshot_receipts(marker_dir: Path) -> frozenset[tuple[str, str]]:
    """실제 system[] 전달 뒤 남은 receipt를 (safeKey, generation)으로 읽는다."""
    generations = set()
    for receipt in marker_dir.glob("compact-snapshot-receipt.*"):
        match = _OPENCODE_SNAPSHOT_RECEIPT_RE.fullmatch(receipt.name)
        if match and receipt.is_file():
            generations.add((match.group(1), match.group(2)))
    return frozenset(generations)


def _compaction_checkpoint_markers(marker_dir: Path) -> frozenset[str]:
    """압축 checkpoint 가 남긴 durable marker 이름 집합(격리 adopter의 전후 delta용)."""
    return frozenset(
        marker.name
        for marker in marker_dir.glob("compact-checkpoint.*")
        if marker.is_file()
    )


def _claude_session_transcript(session_id: str) -> Path:
    """UUID로 현 live Claude main-session transcript를 유일 해소한다."""
    candidates = list(
        (Path.home() / ".claude" / "projects").glob(f"*/{session_id}.jsonl")
    )
    assert len(candidates) == 1, (
        f"Claude session transcript 유일 해소 실패: {session_id} -> {candidates}"
    )
    return candidates[0]


# 압축 기록·snapshot 의 task 는 세션 ID 로 찾는다(T-0953) — 라이브 웨이브는 그 세션을 이 task 로 기록한다.
_RELEASE_WAVE_TASK = "release-wave"


def _record_wave_task_session(dest: Path, session_id: str) -> None:
    """격리 채택자 task 기록(`worktree-leases.json` 최상위 `tasks`)에 웨이브 세션을 연 task 로 남긴다.

    웨이브 세션이 부트스트랩으로 이미 연 task(같은 `session` 행)가 있으면 기록을 바꾸지 않는다.
    같은 세션 행이 둘이면 세션 판정이 미해소가 된다."""
    assert isinstance(session_id, str) and session_id.strip(), session_id
    ledger = dest / ".project_manager" / ".local" / "worktree-leases.json"
    data = json.loads(ledger.read_text(encoding="utf-8")) if ledger.exists() else {}
    wanted = session_id.strip()
    if any(str(row.get("session") or "").strip() == wanted for row in data.get("tasks", [])):
        return
    tasks = [row for row in data.get("tasks", []) if row.get("name") != _RELEASE_WAVE_TASK]
    data["tasks"] = [*tasks, {"name": _RELEASE_WAVE_TASK, "session": session_id}]
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _opencode_session_id(stdout: str) -> str:
    """`opencode run --format json` 이벤트 스트림의 첫 `sessionID`(루트 세션)."""
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and isinstance(event.get("sessionID"), str):
            return event["sessionID"]
    raise AssertionError(f"opencode 이벤트 스트림에 sessionID 가 없음: {stdout[-1600:]!r}")


def _claude_recovery_deliveries(transcript: Path) -> int:
    """압축 뒤 첫 주 스레드 훅의 snapshot이 hook additional context로 실제 전달된 횟수."""
    count = 0
    for line in transcript.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        attachment = event.get("attachment")
        if not isinstance(attachment, dict):
            continue
        if attachment.get("type") != "hook_additional_context":
            continue
        content = attachment.get("content")
        chunks = content if isinstance(content, list) else [content]
        if any(
            isinstance(chunk, str) and "## PM 정체성 (compaction 복구)" in chunk
            for chunk in chunks
        ):
            count += 1
    return count


def _override_ctx_record(dest: Path, harness: str, window: int) -> None:
    """설치가 쓴 압축 한도 기록(schema 5)의 그 도구 사용 한도를 local.conf 에 덧붙인 `window` 로 옮긴다.

    훅·플러그인은 기록의 입력과 local.conf 가 다르면 판정 대신 기록 오류를 낸다(`read_ctx_budget`).
    `pm-config apply` 는 돌리지 않는다 — 기록의 inputs 사용 한도·W·E·P·경고 지점만 다시 쓰고 모델 행·C·settings 와
    하네스 설정 파일은 그대로다. P·경고 지점은 기록 inputs 의 비율로 계산한다."""
    path = dest / ".project_manager" / ".local" / "ctx-budget.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    inputs, entry = record["inputs"], record["harnesses"][harness]
    inputs[f"harness.{harness}.ctx_window_tokens"] = str(window)
    effective = min(window, entry["C"])
    entry.update(W=window, E=effective, P=effective * int(inputs["ctx.compact_at_pct"]) // 100,
                 checkpoint=effective * int(inputs["ctx.checkpoint_at_pct"]) // 100,
                 precompact=effective * int(inputs["ctx.precompact_at_pct"]) // 100)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _force_opencode_compaction_threshold(dest: Path, model: str) -> None:
    """격리 adopter의 per-harness 예산+선택 모델 limit만 낮춰 native compaction을 강제한다.

    `harness.opencode.ctx_window_tokens`는 PM 59의 격리/per-harness 방식으로 plugin 판정만 옮기고
    (압축 한도 기록의 opencode 항목도 같은 값으로 옮긴다 · `_override_ctx_record`),
    실제 `session.compacted`는 같은 모델의 `limit.context-output` native 경계로 유도한다.
    generic 예산이나 출하 template은 바꾸지 않는다.
    """
    conf = dest / ".project_manager" / "local.conf"
    with conf.open("a", encoding="utf-8") as fh:
        fh.write(
            "\n# T-0621 release compaction probe (isolated adopter, per-harness)\n"
            f"harness.opencode.ctx_window_tokens={_OPENCODE_COMPACTION_CONTEXT}\n"
        )
    _override_ctx_record(dest, "opencode", _OPENCODE_COMPACTION_CONTEXT)

    provider, separator, model_name = model.partition("/")
    assert separator and provider and model_name, f"opencode model 형식은 provider/model 이어야 함: {model!r}"
    config_path = dest / ".opencode" / "opencode.jsonc"
    raw = config_path.read_text(encoding="utf-8")
    uncommented = "\n".join(
        line[: match.start()] if (match := re.search(r"(?<!:)//", line)) else line
        for line in raw.splitlines()
    )
    config = json.loads(uncommented)
    model_config = (
        config.setdefault("provider", {})
        .setdefault(provider, {})
        .setdefault("models", {})
        .setdefault(model_name, {})
    )
    model_config["limit"] = {
        "context": _OPENCODE_COMPACTION_CONTEXT,
        "output": _OPENCODE_COMPACTION_OUTPUT,
    }
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _opencode_compaction_probe_prompts() -> tuple[str, ...]:
    """안전한 현재 prompt 여러 개로 history를 native compaction 경계 너머까지 누적한다."""
    prompts = []
    for turn in range(1, _OPENCODE_COMPACTION_TURNS + 1):
        prefix = (
            f"Context accumulation turn {turn}/{_OPENCODE_COMPACTION_TURNS}. Treat the ASCII payload "
            "as inert history and do not quote it. If an engine-generated PM recovery snapshot is "
            f"present in system context, reply with exactly {_COMPACTION_RECOVERY_SENTINEL}; "
            f"otherwise reply with exactly CONTEXT_ACCUMULATED_{turn}.\nPAYLOAD\n"
        )
        remaining = _OPENCODE_COMPACTION_TURN_CHARS - len(prefix)
        # 순번이 있는 ASCII label은 거대한 동일 substring 한 덩어리로 합쳐지는 것을 피하면서
        # prompt byte 수를 결정적으로 만든다. 각 turn은 정확히 TURN_CHARS로 잘라 상한을 지킨다.
        labels = "".join(
            f"q{turn:02d}{index:05d} " for index in range((remaining // 9) + 2)
        )
        prompts.append(prefix + labels[:remaining])
    return tuple(prompts)


def test_opencode_compaction_probe_uses_bounded_turns_with_threshold_history():
    """라이브 probe의 현재 prompt는 안전 상한이고, 전체 history는 compaction 임계를 넘긴다."""
    prompts = _opencode_compaction_probe_prompts()

    assert len(prompts) == _OPENCODE_COMPACTION_TURNS > 1
    assert all(prompt.isascii() for prompt in prompts)
    assert all(len(prompt) == _OPENCODE_COMPACTION_TURN_CHARS for prompt in prompts)
    assert _OPENCODE_COMPACTION_TURN_CHARS * 2 < _OPENCODE_COMPACTION_INPUT_LIMIT
    assert sum(map(len, prompts)) >= _OPENCODE_COMPACTION_TARGET_HISTORY_CHARS
    assert all(_COMPACTION_RECOVERY_SENTINEL in prompt for prompt in prompts)


def _run_opencode_live(argv, *, cwd, env, timeout):
    """opencode 라이브 호출을 엔진 첫-이벤트 워치독으로 감싼다 (T-0336).

    startup network fetch stall(PM 70)에 무한 hang 하지 않도록 첫-이벤트 감시 + 유한 재시도.
    소진 시 StallWatchdogError → 테스트 **fail-loud**(라이브 환경 문제 가시화). overall_timeout 은
    기존 turn 상한(1800s) 유지 — mid-turn(정상 긴 생성) 침묵은 그 백스톱이 담당. subprocess.run 과
    동일한 CompletedProcess(returncode·stdout·stderr)를 반환해 side-effect 단언은 무변경."""
    return watchdog.run_with_first_event_watchdog(
        argv,
        first_event_timeout=watchdog.first_event_timeout_default(),
        overall_timeout=timeout,
        retries=watchdog.stall_retries_default(),
        cwd=str(cwd),
        env=env,
    )


def _full_wave_prompt(entry_doc: str, harness: str) -> str:
    """PM 세션이 고정 4회 위임과 native 라운드 파일 왕복을 운영하라는 프롬프트.

    board.py 경로를 *주지 않는다* — adopter 가 `entry_doc` 만으로 도구를 찾아 운영해야 통과(= 문서 운영성).
    같은 claimed ticket에서 01 architect→02 developer→03 reviewer→04 developer 순서로
    prepare→native subagent→harvest를 수행한다. 준비가 예약한 라운드
    파일(`tickets/rounds/<T-NNNN>/NN-<역할>.md`)의 고유 sentinel과 developer의 probe.txt를 side-effect로
    관측한다. `section-add`는 여기서 쓰지 않는다 — 슬롯 없는 준비라 위임 경로와 겹치면 빈 라운드가
    하나 더 예약된다. 기존 Claude/OpenCode full-wave 호출을 재사용해 별도 native 중복 테스트를 만들지 않는다.

    작업 폴더 형상은 `_guided_adopter(folders=2, task=wave)` 다([[T-0956]]·커맨드 카드의 통합 폴더 형상):
    1번 폴더가 base_branch 를 계속 드는 통합 폴더, 2번 폴더가 묶음 브랜치 `task/wave` 로 전환한 작업
    폴더다. 2개를 빌린 task 는 `--task` 만으로 폴더를 고를 수 없어 발행·claim 은 `--slot 2`, 묶음 선언은
    `--slot 1` 로 지목한다. 생성 회귀 테스트는 작업 폴더에서 홈 보드를 보지 못하므로 자기 파일 기준
    저장소 루트의 probe.txt 만 확인한다 — 정본 라운드 확인은 `_assert_wave_side_effects` 가 홈 보드에서 한다.
    """
    repo = f"adopter-{harness}"
    work_folder = f"work/{repo}_{_WAVE_WORK_SLOT}"
    work_branch = f"task/{_WAVE_CLUSTER}"
    architect_payload = json.dumps({
        "version": _ARCHITECT_TEST_VERSION,
        "tests": [{
            "id": "AT-001",
            "target": _WAVE_TEST_FILE,
            "command": _WAVE_TARGETED_COMMAND,
            "expected": "passed",
            "negative": "probe 내용 또는 canonical architect round가 틀리면 실패해야 한다",
        }],
    }, ensure_ascii=False, separators=(",", ":"))
    zero_review_payload = json.dumps(
        {"version": _PM_REVIEW_VERSION, "findings": [], "confirmations": []},
        ensure_ascii=False, separators=(",", ":"),
    )
    architect_body = (
        "## 경계 실측\n- isolated release-wave adopter and canonical ticket rounds\n\n"
        "## 불변식\n- architect then developer then code-reviewer then final developer\n\n"
        "## 표면 상한\n- probe.txt and one stable regression test\n\n"
        "## 테스트 전략\n- positive canonical round/probe check and negative mismatch check\n\n"
        f"```{_ARCHITECT_TEST_BLOCK}\n{architect_payload}\n```\n\n"
        "검토 판정: 설계 통과\n"
        f"{_GROWTH_PIPELINE[0][1]}\n"
    )
    reviewer_zero_body = (
        "## must-fix\n- 없음\n\n"
        "## 판정\n판정: 통과 · finding 0건(must-fix 0건)\n\n"
        f"```{_PM_REVIEW_BLOCK}\n{zero_review_payload}\n```\n\n"
        f"{_GROWTH_PIPELINE[2][1]}\n"
    )
    delegation_tool = "spawn_agent" if harness == "codex" else "Task"
    return (
        f"You are the PM for this project. Read {entry_doc} to learn how the project board "
        "tool works. HARD DELEGATION GATE: the PM main may run board, prepare, and harvest commands but "
        f"must not directly write, edit, cp, or sed any round file body. Call native {delegation_tool} exactly four "
        "times in this order: architect once, developer once, code-reviewer once, developer once. After "
        "each prepare, pass that absolute round path and its complete ROUND contract below to the matching "
        f"{delegation_tool}; do not start the next {delegation_tool} until the current {delegation_tool} "
        "succeeds and its harvest returns rc=0. "
        f"If a {delegation_tool} call is missing or fails, stop without directly substituting for it. "
        f"The code-reviewer {delegation_tool} "
        "prompt must include the nested BEGIN EXACT REVIEWER BODY through END EXACT REVIEWER BODY content "
        "below verbatim, without paraphrasing or changing its schema. Before completion, self-check spawned "
        "role counts exactly architect=1, developer=2, code-reviewer=1 and stop on any mismatch. "
        f"The board task `{_WAVE_TASK}` already holds two work folders of repo `{repo}`: slot "
        f"{_WAVE_INTEGRATION_SLOT} is the integration folder (it keeps the merge target branch) and slot "
        f"{_WAVE_WORK_SLOT} is the work folder. Because the task holds two folders, pass "
        f"`--repo {repo} --slot <N>` wherever a board command names the folder. "
        "Then run a full release wave: "
        "(1) create exactly one ticket titled 'release wave probe' with the board tool, using "
        f"`--task {_WAVE_TASK} --repo {repo} --slot {_WAVE_WORK_SLOT} "
        f"--touches {PROBE_FILE},{_WAVE_TEST_FILE}`, "
        f"(2) claim it with `--task {_WAVE_TASK} --repo {repo} --slot {_WAVE_WORK_SLOT}`, then before "
        "ROUND 01 bind the merge target and move the work folder: run "
        f"`python3 .project_manager/tools/board.py cluster new {_WAVE_CLUSTER} --tickets <ticket> "
        f"--task {_WAVE_TASK} --repo {repo} --slot {_WAVE_INTEGRATION_SLOT}` and then "
        f"`python3 .project_manager/tools/worktree_pool.py switch {repo}_{_WAVE_WORK_SLOT} {work_branch}`, "
        "(3) execute exactly these four human rounds in order. For every round use pm_delegate.py ticket "
        "prepare with the stated role, pass the returned absolute round file to the matching native "
        "subagent, then always ticket harvest before advancing. Every ticket prepare and ticket harvest "
        f"takes `--cwd` set to the absolute path of the work folder `{work_folder}` under this project "
        "root, and the subagents write code only in that folder:\n"
        "ROUND 01 architect: use replacement/truncation, never append. Preserve the seeded first header "
        "line and make every byte from line 2 through EOF equal the body between BEGIN/END here:\n"
        f"BEGIN EXACT ARCHITECT BODY\n{architect_body}END EXACT ARCHITECT BODY\n"
        "Before replying, reopen the file and verify exact body equality, exactly one architect test "
        "contract block, exactly one architect sentinel, and zero `<...>` placeholder tokens. If the old "
        "skeleton follows the sentinel or any check fails, rewrite line 2 through EOF and reread it.\n"
        "ROUND 02 developer: implement that contract in the work folder. Create probe.txt with exactly "
        f"'{PROBE_TEXT}' and create {_WAVE_TEST_FILE}. The test checks only the content of probe.txt at "
        "the repository root derived from its own file, `Path(__file__).resolve().parents[1]`; the work "
        "folder does not see the project board, so the test must not read tickets or rounds. "
        "It must never refer "
        "to .local/delegate-ticket-copies, an absolute temp path, a random run id, UUID, or hash. "
        "After writing the test, reopen its source and verify the literal `.local/delegate-ticket-copies` "
        "occurs zero times in the entire file bytes, including comments and docstrings; describe this ban "
        "only in the round evidence, never inside the generated test. Also verify no 32-hex run hash occurs. "
        f"Run `{_WAVE_TARGETED_COMMAND}` and then `{_WAVE_FULL_COMMAND}` yourself. Only actual rc=0 may be "
        "recorded. The `## 회귀` section must contain exactly two nonblank rows: "
        f"`- 커맨드: `{_WAVE_FULL_COMMAND}`` and `- 결과: rc=0 · <the one observed full pytest "
        "summary>`. Put targeted evidence under `## DoD evidence`, not under `## 회귀`. Before replying, "
        "reopen the developer file and extract `## 회귀` through the next `## ` heading; rewrite and "
        f"reread unless it has exactly those two rows, contains `{_WAVE_FULL_COMMAND}` once, contains "
        f"neither `{_WAVE_TARGETED_COMMAND}` nor a placeholder, and the whole body contains "
        f"{_GROWTH_PIPELINE[1][1]} exactly once. Never fabricate a count.\n"
        f"ROUND 03 code-reviewer: the review unit is the cluster `{_WAVE_CLUSTER}`, so prepare it with "
        f"`python3 .project_manager/tools/pm_delegate.py ticket prepare --cluster C-{_WAVE_CLUSTER} --role "
        "code-reviewer --cwd <work folder absolute path>` and pass the returned `prompt` file path together "
        "with the absolute round file to the code-reviewer subagent. "
        "Review probe.txt and its regression test. If the known stable fixture is "
        "correct, use replacement/truncation, preserve the first header, and make line 2 through EOF equal "
        "the body between BEGIN/END here:\n"
        f"BEGIN EXACT REVIEWER BODY\n{reviewer_zero_body}END EXACT REVIEWER BODY\n"
        "Reopen the file and verify exact body equality, one review block, one sentinel, and no old skeleton "
        "after the sentinel before replying. "
        "If there is a real defect instead, fill the seeded v3 finding without changing its keys; include "
        "evidence and all fix_contract fields, "
        f"with test={_WAVE_TEST_FILE}, command=`{_WAVE_TARGETED_COMMAND}`, expected=passed. PM must harvest "
        "ROUND 03 and observe rc=0 plus the canonical reviewer content before running ROUND 04 prepare; never "
        "reserve or pre-create ROUND 04 earlier.\n"
        "PM DISPOSITION: after reviewer harvest, run review disposition-template for the actual reviewer "
        "ordinal. Accept only real current-ticket findings. For zero findings, keep dispositions rows at "
        "zero: append only the exact finding-zero block emitted by the template under `## PM 기계 확인` "
        "in the claimed ticket file, never in the canonical ROUND 03 reviewer file or any round file, and "
        "never invent a finding row. Do not search source or help for the block location. Reopen both files "
        "and verify the disposition fence occurs zero times in canonical ROUND 03 and exactly once under "
        "the claimed ticket's `## PM 기계 확인`; only after these counts are true may ROUND 04 prepare run. "
        "Confirm review delta is empty. Then pass the exact accepted-only delta output to "
        "the final developer. Do not create another ticket or reviewer round.\n"
        "ROUND 04 developer (terminal final fix): always prepare and delegate this second developer round, "
        "even when review delta is empty. Apply every accepted finding, add or modify its required regression "
        "test, and fill every preseeded pm-review-verify-v1 row. Run the architect/reviewer targeted commands "
        f"and `{_WAVE_FULL_COMMAND}` yourself. The final body must use the completed sections in exact order "
        "`## 변경 파일`, `## 신규 테스트`, `## 회귀`, `## DoD evidence`, `## 민감도`, then any "
        "seeded verify block and the final sentinel. The `## 회귀` section must contain exactly two "
        f"nonblank rows: `- 커맨드: `{_WAVE_FULL_COMMAND}`` and `- 결과: rc=0 · <the one observed "
        "full pytest summary>`. Put targeted evidence under `## DoD evidence`, not under `## 회귀`. "
        "Before replying, reopen the final file and extract `## 회귀` through the next `## ` heading; "
        f"rewrite and reread unless it has exactly those two rows, contains `{_WAVE_FULL_COMMAND}` once, "
        f"contains neither `{_WAVE_TARGETED_COMMAND}` nor a placeholder, and the whole body contains "
        f"{_GROWTH_PIPELINE[3][1]} exactly once. Harvest once and open no further human round.\n"
        "(4) after all four harvests, start a fresh board.py show process and verify the exact role order "
        "architect, developer, code-reviewer, developer and all four sentinels, "
        "(5) do not run a separate resolve command (the closer resolves the review gate). "
        "Do not search source or help for another gate command. Check all ticket DoD "
        "boxes, update the status row, append one ticket entry to log/current.md, then run exactly "
        f"`python3 .project_manager/tools/ticket_finish.py <ticket> --task {_WAVE_TASK} --no-pytest` "
        f"(the ticket belongs to cluster `{_WAVE_CLUSTER}`, so a direct `board.py complete` is rejected; the "
        "closer merges the work folder branch into the integration folder; --no-pytest reuses the full "
        "regression this wave already recorded). "
        "Do not do further discovery after that command succeeds. "
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


def _wave_base_branch(dest: Path) -> str:
    """묶음 `wave` 의 base_branch(통합 브랜치) — `board.py cluster show` 의 `base_branch:` 줄."""
    shown = subprocess.run(
        [sys.executable, str(dest / ".project_manager" / "tools" / "board.py"),
         "cluster", "show", _WAVE_CLUSTER],
        cwd=dest, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    assert shown.returncode == 0, f"cluster show {_WAVE_CLUSTER} rc={shown.returncode}\n{shown.stderr}"
    values = [line.split(":", 1)[1].strip() for line in shown.stdout.splitlines()
              if line.strip().startswith("base_branch:")]
    assert len(values) == 1, shown.stdout
    return values[0]


def _integration_file(dest: Path, base: str, relpath: str, repo: str | None = None) -> str | None:
    """브랜치 `base` 의 파일 내용 — 저장소(`repo`·기본은 홈 이름) bare 에서 `git show` (없으면 None)."""
    shown = subprocess.run(
        ["git", "--git-dir", str(dest / ".repos" / f"{repo or dest.name}.git"), "show", f"{base}:{relpath}"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    return shown.stdout if shown.returncode == 0 else None


def _assert_wave_side_effects(dest: Path, proc: subprocess.CompletedProcess, harness: str) -> None:
    """full wave side-effect 단언 — developer 산출이 통합 브랜치에 머지·ticket 이 done/ 도달.

    probe.txt(내용 'hello from dev') = developer 서브에이전트가 위임받아 구현했다는 증거이고, 그것이
    묶음 `wave` 의 base_branch(통합 브랜치)에 있어야 종결의 머지까지 끝난 것이다([[T-0956]]·통합 폴더
    형상). 지정 테스트 재실행은 그 브랜치를 든 통합 폴더에서 한다. done/ 도달·라운드 순서·sentinel 은
    홈 보드를 본다. 모두 출력 phrasing 비결정에 강건한 side-effect.
    """
    tail = (
        f"--- {harness} stdout(tail) ---\n{proc.stdout[-2000:]}\n"
        f"--- stderr(tail) ---\n{proc.stderr[-1000:]}"
    )
    base = _wave_base_branch(dest)
    assert base == f"{dest.name}_{_WAVE_INTEGRATION_SLOT}", (
        f"묶음 {_WAVE_CLUSTER} 의 base_branch {base!r} 가 통합 폴더 브랜치가 아니다.\n" + tail
    )
    probe = _integration_file(dest, base, PROBE_FILE)
    assert probe is not None, (
        f"실 {harness} full wave 후 통합 브랜치 {base} 에 {PROBE_FILE} 부재 — developer 위임/구현 "
        "또는 종결 머지 실패.\n" + tail
    )
    assert probe.strip() == PROBE_TEXT, (
        f"{PROBE_FILE} 내용이 '{PROBE_TEXT}' 아님 — developer 가 다르게 구현.\n" + tail
    )
    wave_test_text = _integration_file(dest, base, _WAVE_TEST_FILE)
    assert wave_test_text is not None, (
        f"architect 지정 회귀 {_WAVE_TEST_FILE} 가 통합 브랜치 {base} 에 없다.\n" + tail
    )
    assert PROBE_FILE in wave_test_text, "release probe가 probe.txt 를 확인하지 않음.\n" + tail
    assert ".local/delegate-ticket-copies" not in wave_test_text, (
        "release probe가 일회성 delegate copy 경로에 결속됨.\n" + tail
    )
    assert re.search(r"\b[0-9a-f]{32}\b", wave_test_text, re.IGNORECASE) is None, (
        "release probe가 랜덤 run hash를 hardcode함.\n" + tail
    )
    integration_folder = dest / "work" / base
    assert integration_folder.is_dir(), f"통합 폴더 {integration_folder} 부재.\n" + tail
    targeted = subprocess.run(
        _WAVE_TARGETED_COMMAND.split(), cwd=integration_folder, capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
    )
    assert targeted.returncode == 0, (
        f"live side-effect에서 architect 지정 테스트 재실행 red: {_WAVE_TARGETED_COMMAND}\n"
        f"stdout={targeted.stdout[-1200:]}\nstderr={targeted.stderr[-800:]}\n" + tail
    )
    done_tickets = _tickets_in(dest, "done")
    assert done_tickets, (
        f"실 {harness} 가 ticket 을 done/ 까지 운영하지 못함 — full wave 미완주.\n"
        f"open={_tickets_in(dest, 'open')} claimed={_tickets_in(dest, 'claimed')}\n" + tail
    )
    ticket_name = sorted(done_tickets)[-1]
    ticket_id_match = re.match(r"(T-\d+)-", ticket_name)
    assert ticket_id_match, f"done ticket 파일명에서 ID 를 못 읽음: {ticket_name}\n" + tail
    ticket_id = ticket_id_match.group(1)
    rounds_dir = (
        dest / ".project_manager" / "wiki" / "tickets" / "rounds" / ticket_id
    )
    assert rounds_dir.is_dir(), (
        f"실 {harness} done ticket에 라운드 디렉터리 부재 — prepare 예약이 board 에 남지 않았다: "
        f"{rounds_dir}\n" + tail
    )
    round_files = sorted(rounds_dir.glob("*.md"))
    actual_roles = [path.stem.split("-", 1)[1] for path in round_files]
    expected_roles = [role for role, _sentinel in _GROWTH_PIPELINE]
    assert actual_roles == expected_roles, (
        f"실 {harness} 고정 라운드 수열 불일치: {actual_roles} != {expected_roles}\n" + tail
    )
    for path, (role, sentinel) in zip(round_files, _GROWTH_PIPELINE):
        text = path.read_text(encoding="utf-8")
        assert sentinel in text, (
            f"실 {harness} {path.name}({role})에 harvest sentinel 부재.\n" + tail
        )
        # 첫 줄 헤더(라벨·역할·날짜)는 엔진이 시드하지만 사람 참고용이라 엔진이 재작성을
        # 강제하지 않는다(`round_rules` 단일 진실은 파일명의 순번·역할) — 라이브 tier 판정 축은
        # 엔진이 실제로 강제하는 성질(sentinel·아래 pm-review-v1 블록)로 좁힌다.
        if role == _REVIEWER_SUBAGENT:
            # 리뷰 라운드는 엔진이 시드한 판정 블록을 담은 채 회수돼야 delta 단계가 선다.
            assert f"```{_PM_REVIEW_BLOCK}" in text, (
                f"실 {harness} 리뷰 라운드에 {_PM_REVIEW_BLOCK} 블록 부재 — 시드 골격이 지워졌다: "
                f"{path}\n" + tail
            )


def _baseline_adopter(dest: Path) -> None:
    """설치한 채택자의 첫 커밋을 host 가 만든다 — 작업 폴더(bare clone)와 Codex 의 이후 커밋의 전제.

    커밋 0개 원본으로는 작업 폴더를 만들 수 없다(`repo add` 가 빈 bare 를 거부·[[T-0956]]). throwaway
    adopter에는 remote가 없고, 테스트 전용 identity도 repo config에 남기지 않는다.
    """
    commands = (
        ["git", "-C", str(dest), "add", "--all"],
        [
            "git", "-C", str(dest),
            "-c", "user.name=PM release fixture",
            "-c", "user.email=pm-release-fixture@example.invalid",
            "commit", "--no-gpg-sign", "-m", "release fixture baseline",
        ],
    )
    for command in commands:
        proc = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace",
            check=False,
        )
        assert proc.returncode == 0, (
            f"adopter baseline 실패: {' '.join(command)}\n"
            f"stdout={proc.stdout[-1200:]}\nstderr={proc.stderr[-1200:]}"
        )
    assert (dest / ".git" / "index").is_file()
    head = subprocess.run(
        ["git", "-C", str(dest), "rev-parse", "--verify", "HEAD"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    assert head.returncode == 0 and head.stdout.strip()


def _guided_adopter(tmp_path: Path, harness: str, *, task: str, folders: int) -> Path:
    """엔진 안내 경로 그대로 첫 작업 폴더를 만든 채택자([[T-0956]]).

    설치 → 기준 커밋 → 안내 명령 `pm-config worktree add <repo> --user-ack <repo>` 를 `folders` 번 →
    `pm-config task new <task>` → `pm-config alloc <repo> --task <task>` 를 `folders` 번. 그 task 가
    작업 폴더 `folders` 개를 보유한다. 카드 시험(folders=1)과 full wave(folders=2)가 함께 쓴다.
    """
    dest = _import_adopter(tmp_path, harness)
    _baseline_adopter(dest)
    pm_config = str(dest / ".project_manager" / "tools" / "pm_config.py")
    repo = dest.name
    steps = [("worktree", "add", repo, "--user-ack", repo)] * folders
    steps += [("task", "new", task)]
    steps += [("alloc", repo, "--task", task)] * folders
    # 작업 폴더 등록 owner 는 사람 신원(`git config user.email`)에서 온다 — 사용자 git 설정이 없는
    # 환경에서도 같게 돌도록 그 값을 이 자식 프로세스 환경으로만 준다(repo config 에 남기지 않는다).
    environment = {**os.environ, "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "user.email",
                   "GIT_CONFIG_VALUE_0": "pm-release-fixture@example.invalid"}
    for step in steps:
        proc = subprocess.run(
            [sys.executable, pm_config, *step], cwd=dest, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
            env=environment,
        )
        assert proc.returncode == 0, (
            f"안내 경로 {' '.join(step)} rc={proc.returncode}\n"
            f"stdout={proc.stdout[-1200:]}\nstderr={proc.stderr[-1200:]}"
        )
    return dest


@pytest.mark.release
@pytest.mark.skipif(
    not _RELEASE_LIVE or not shutil.which("claude"),
    reason="release wave — PM_ORCH_LIVE_RELEASE=1 + claude CLI 필요(API 과금). 기본 skip·사용자 트리거.",
)
def test_release_wave_claude_full_wave(tmp_path):
    """실 claude full wave + native auto-compaction이 snapshot을 재주입한다.

    PM 36 라이브 probe(`scratchpad/release_probe.py`·PASS·dev×15·reviewer×21)의 mechanics 를 옮긴 것.
    claude 는 subprocess cwd 를 존중한다(`--dir` 불요). stream-json 으로 위임(subagent_type)을 관측하고
    side-effect(probe.txt·done)를 먼저 단언한다. 그 세션을 웨이브 task 를 연 세션으로 task 기록에 남긴
    뒤 같은 세션을 낮은 native `--autocompact` 경계로 resume한다. 압축 뒤 첫 주 스레드 훅의
    checkpoint 1건 이상과 snapshot 전달, 모델 sentinel 응답을 함께 확인한다. API 과금.
    """
    dest = _guided_adopter(tmp_path, "claude", task=_WAVE_TASK, folders=2)
    session_id = str(uuid.uuid4())
    checkpoints_before = _compaction_checkpoint_count(dest)
    marker_dir = dest / ".project_manager" / ".local" / "ctx-stop"
    checkpoint_markers_before = _compaction_checkpoint_markers(marker_dir)

    proc = subprocess.run(
        ["claude", "-p", "--model", CLAUDE_MODEL,
         "--session-id", session_id,
         "--autocompact", _CLAUDE_WAVE_AUTOCOMPACT_THRESHOLD,
         "--allowedTools", "Bash", "Task",
         "--output-format", "stream-json", "--verbose",
         "--dangerously-skip-permissions",
         _full_wave_prompt("CLAUDE.md", "claude")],
        cwd=str(dest), capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=_live_env(CLAUDE_MODEL), timeout=_CLAUDE_TIMEOUT,
    )

    # 위임 관측(hard) — fixed pipeline의 developer가 구현·fix 두 번 등장해야 한다.
    subagent_types = _collect_subagent_types(proc.stdout)
    tail = (
        f"--- claude stdout(tail) ---\n{proc.stdout[-2500:]}\n"
        f"--- stderr(tail) ---\n{proc.stderr[-1000:]}"
    )
    assert (
        _ARCH_SUBAGENT in subagent_types
        and _REVIEWER_SUBAGENT in subagent_types
        and subagent_types.count(_DEV_SUBAGENT) >= 2
    ), (
        f"claude full wave 에서 위임 미관측 — subagent_type={subagent_types} "
        f"(architect 1·developer 2·code-reviewer 1 필요).\n" + tail
    )

    # side-effect(hard) — developer 위임 결과(probe.txt)·done 전이.
    _assert_wave_side_effects(dest, proc, "claude")

    assert proc.returncode == 0
    _record_wave_task_session(dest, session_id)
    resume_proc = subprocess.run(
        ["claude", "-p", "--model", CLAUDE_MODEL,
         "--resume", session_id,
         "--autocompact", _CLAUDE_AUTOCOMPACT_THRESHOLD,
         "--allowedTools", "Bash",
         "--output-format", "stream-json", "--verbose",
         "--dangerously-skip-permissions",
         _CLAUDE_COMPACTION_PROBE_PROMPT],
        cwd=str(dest), capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=_live_env(CLAUDE_MODEL), timeout=_CLAUDE_TIMEOUT,
    )
    assert resume_proc.returncode == 0, (
        "claude same-session native compaction probe 실패\n"
        f"--- stdout(tail) ---\n{resume_proc.stdout[-2500:]}\n"
        f"--- stderr(tail) ---\n{resume_proc.stderr[-1000:]}"
    )
    assert _COMPACTION_RECOVERY_SENTINEL in resume_proc.stdout
    assert _compaction_checkpoint_count(dest) >= checkpoints_before + 1, (
        "claude compaction 경계 checkpoint 골격이 log에 생성되지 않음"
    )
    assert _compaction_checkpoint_markers(marker_dir) - checkpoint_markers_before, (
        "claude 압축 checkpoint durable marker 증가분이 없음"
    )
    transcript = _claude_session_transcript(session_id)
    assert _claude_recovery_deliveries(transcript) >= 1, (
        "claude 압축 뒤 snapshot이 주 스레드 훅 additional context에 도달한 증거가 없음"
    )


@pytest.mark.release
@pytest.mark.skipif(
    not _RELEASE_LIVE or not shutil.which("codex") or not codex_auth_available(),
    reason="release wave native codex — PM_ORCH_LIVE_RELEASE=1 + codex auth 필요.",
)
def test_release_wave_codex_native_ticket_growth(tmp_path):
    """실 Codex main이 spawn_agent 3역할로 같은 ticket copy를 성장·harvest하고 done까지 완주한다."""
    dest = _guided_adopter(tmp_path, "codex", task=_WAVE_TASK, folders=2)
    home = make_codex_home(tmp_path)
    try:
        proc = run_codex_exec(
            _full_wave_prompt("AGENTS.md", "codex"), dest, home,
            model=CODEX_MODEL, timeout=_CODEX_TIMEOUT, sandbox="danger-full-access",
        )
    finally:
        drop_codex_auth(home)
    _assert_wave_side_effects(dest, proc, "codex")


@pytest.mark.release
@pytest.mark.skipif(
    not _RELEASE_LIVE or not shutil.which("opencode"),
    reason="release wave — PM_ORCH_LIVE_RELEASE=1 + opencode CLI(+ollama 모델) 필요. 기본 skip·사용자 트리거.",
)
def test_release_wave_opencode_full_wave(tmp_path):
    """실 opencode full wave + 강제 native 임계가 snapshot 재주입·checkpoint를 남긴다.

    opencode 의 위임 관측 수단은 claude 의 stream-json `subagent_type` 와 다르다 — PM 36 라이브 probe
    실측 결과 gemma/opencode 는 위임 흔적(subagent_type·'developer'·task)을 출력에 **0** 으로 낸다(비결정).
    그래서 **side-effect(probe.txt·done)만 hard 단언**하고(probe.txt=developer 가 위임받아 작성·done=wave
    완주 → side-effect 가 위임 *결과*를 커버), 위임 흔적(stdout 에 'developer'/'code-reviewer' 등장)은
    **best-effort**(있으면 단언·없으면 skip)다. opencode 위임 관측 수단은 PM probe 후 보강한다.
    gemma 는 느리고 변동 커 timeout 1800s. `--dir` 로 루트 핀(opencode 는 PWD 로 루트 오판).
    compaction 경계는 격리 adopter의 `harness.opencode.ctx_window_tokens`(하네스별 키) + 선택
    모델 `limit.context-output`만 낮추고 반복 입력으로 넘긴다. 출하 config/generic 예산은 무변경.
    두 단계다(T-0953): 첫 실행(`--format json`)에서 루트 세션 ID 를 얻어 task 기록에 남기고, 같은
    세션(`--continue`)으로 압축을 일으킨다 — 압축 기록의 task 는 세션 ID 로 찾는다.
    """
    dest = _guided_adopter(tmp_path, "opencode", task=_WAVE_TASK, folders=2)
    marker_dir = dest / ".project_manager" / ".local" / "ctx-stop"
    checkpoints_before = _compaction_checkpoint_count(dest)
    checkpoint_markers_before = _compaction_checkpoint_markers(marker_dir)
    receipts_before_turn = _opencode_snapshot_receipts(marker_dir)

    proc = _run_opencode_live(
        # `--dangerously-skip-permissions`: 비대화 헤드리스라 opencode 가 `--dir` 디렉토리를
        # external_directory 로 보고 권한을 auto-reject → AGENTS.md 도 못 읽고 wave 시작 실패한다.
        # 이 플래그로 권한을 통과시켜야 wave 완주(throwaway tmp adopter 격리라 안전·PM 36 probe 실측).
        ["opencode", "run", "--format", "json", "--agent", "build", "--dir", str(dest),
         "--dangerously-skip-permissions", "-m", LIVE_MODEL,
         _full_wave_prompt("AGENTS.md", "opencode")],
        cwd=str(dest), env=_live_env(LIVE_MODEL), timeout=_OPENCODE_TIMEOUT,
    )

    # side-effect(hard) — full wave 의 핵심 결과(developer 위임 산출 probe.txt·done 전이).
    _assert_wave_side_effects(dest, proc, "opencode")
    _record_wave_task_session(dest, _opencode_session_id(proc.stdout))

    # full wave를 정상 context로 완주한 뒤에만 같은 completed session의 native
    # compaction 경계를 낮춰, 아래 bounded safe turns로 checkpoint+receipt를 관측한다.
    _force_opencode_compaction_threshold(dest, LIVE_MODEL)

    # 위임 흔적(best-effort) — opencode 출력에 서브에이전트 이름이 등장하면 위임 관측으로 단언.
    # 등장 안 해도 fail 시키지 않는다 — opencode 위임 관측 수단=stream-json 아님·gemma 비결정으로
    # 위임 흔적 출력 0(PM 36 probe 실측). 위임은 side-effect(probe.txt·done)로 검증한다.
    if _DEV_SUBAGENT in proc.stdout and _REVIEWER_SUBAGENT in proc.stdout:
        assert _DEV_SUBAGENT in proc.stdout and _REVIEWER_SUBAGENT in proc.stdout

    # T-0627 release boundary: 현 full-wave 세션을 이어 안전 크기 turn들을 누적한다. 과거
    # turn은 native compaction으로 줄일 수 있지만 현재 prompt는 줄일 수 없으므로 단일 초대형
    # prompt를 쓰지 않는다. 새 checkpoint 증가 뒤에는 snapshot payload가 system[]에 실제 push된
    # generation receipt를 모델 phrasing·in-process marker 수명과 독립적으로 관측한다.
    probe_results = []
    receipt_observations = []
    receipt_appearances = []
    checkpoint_increased = (
        _compaction_checkpoint_count(dest) >= checkpoints_before + 1
        or bool(_compaction_checkpoint_markers(marker_dir) - checkpoint_markers_before)
    )
    receipts = _opencode_snapshot_receipts(marker_dir)
    delivered_receipts = set(receipts - receipts_before_turn)
    receipts_before_turn = receipts
    for turn, prompt in enumerate(
        () if checkpoint_increased and delivered_receipts
        else _opencode_compaction_probe_prompts(),
        start=1,
    ):
        compacted = _run_opencode_live(
            ["opencode", "run", "--continue", "--agent", "build", "--dir", str(dest),
             "--dangerously-skip-permissions", "-m", LIVE_MODEL, prompt],
            cwd=str(dest), env=_live_env(LIVE_MODEL), timeout=_OPENCODE_TIMEOUT,
        )
        probe_results.append(compacted)
        assert compacted.returncode == 0, (
            f"opencode compaction history 누적 turn {turn} 실패.\n"
            f"stdout={compacted.stdout[-1600:]!r}\nstderr={compacted.stderr[-1000:]!r}"
        )
        checkpoint_increased = (
            _compaction_checkpoint_count(dest) >= checkpoints_before + 1
            or bool(_compaction_checkpoint_markers(marker_dir) - checkpoint_markers_before)
        )
        receipts = _opencode_snapshot_receipts(marker_dir)
        appeared = receipts - receipts_before_turn
        receipt_observations.append(receipts)
        receipt_appearances.append(appeared)
        if checkpoint_increased:
            delivered_receipts.update(appeared)
        if checkpoint_increased and delivered_receipts:
            break
        receipts_before_turn = receipts

    # 마지막 누적 turn에서 process-boundary marker만 stage됐을 수 있으므로 작은 후속 turn에서
    # fallback push/receipt까지 한 번 더 관측한다. in-memory 소비면 이미 receipt가 있어 생략된다.
    if checkpoint_increased and not delivered_receipts:
        recovered = _run_opencode_live(
            ["opencode", "run", "--continue", "--agent", "build", "--dir", str(dest),
             "--dangerously-skip-permissions", "-m", LIVE_MODEL,
             "If a PM recovery snapshot is present in system context, reply with exactly "
             f"{_COMPACTION_RECOVERY_SENTINEL}; otherwise reply with exactly NO_COMPACTION_RECOVERY."],
            cwd=str(dest), env=_live_env(LIVE_MODEL), timeout=_OPENCODE_TIMEOUT,
        )
        probe_results.append(recovered)
        assert recovered.returncode == 0, (
            "opencode durable snapshot 소비 후속 turn 실패.\n"
            f"stdout={recovered.stdout[-1600:]!r}\nstderr={recovered.stderr[-1000:]!r}"
        )
        receipts = _opencode_snapshot_receipts(marker_dir)
        appeared = receipts - receipts_before_turn
        receipt_observations.append(receipts)
        receipt_appearances.append(appeared)
        delivered_receipts.update(appeared)

    trace = "\n".join(
        f"turn={index} receipts={sorted(receipts)!r} new={sorted(appeared)!r} "
        f"stdout={result.stdout[-600:]!r} "
        f"stderr={result.stderr[-400:]!r}"
        for index, (result, receipts, appeared) in enumerate(
            zip(probe_results, receipt_observations, receipt_appearances), start=1
        )
    )
    assert checkpoint_increased, (
        "opencode 강제 임계에서 session.compacted→checkpoint log/marker 증가분이 "
        "발화하지 않음.\n" + trace
    )
    assert delivered_receipts, (
        "opencode checkpoint 증가 뒤 staged snapshot이 system[]에 전달됐다는 새 "
        "(safeKey, generation) receipt가 관측되지 않음.\n" + trace
    )

    # PM 36 실측처럼 glm은 exact-reply 지시 자체를 무시할 수 있어 echo는 best-effort다.
    # sentinel 미등장은 receipt hard 단언을 실패시키지 않으며, 보인 echo는 trace에 보존된다.


# ── multi-repo 경로 (multi-PM 셋업 full wave · T-0158) ───────────────────────────────────
# 위 단일-adopter 테스트는 *한* repo 위 full wave 다. 아래는 그 multi-repo 확장 — multi-PM 셋업
# (`pm_config repo add` 2 repo + worktree slot)에서 한 LLM 세션이 공유 보드 위 *여러 repo* 의
# wave 를 운영하는지 검증한다. PM 라이브 probe(opencode·ollama cloud 모델·실측 PASS)로 viable 확인
# 후 그 mechanics 를 옮긴 것이다.

# multi-repo 셋업의 repo 이름 = prefix = worktree 슬롯 네임스페이스(단일 진실). 2 repo 로 충분 —
# 새 위험축(per-repo prefix·per-slot 식별)은 1→2 에서 이미 드러난다(대N 은 spike §6 후속).
# 이름은 **소문자**여야 한다 — prefix sanity(`_validate_prefix`·[a-z0-9_]+·ADR-0042/T-0237)가
# `--prefix <repo>` 를 검증하므로(대문자면 rc1). 프롬프트의 발행 줄은 실 repo 이름(소문자)을 채운
# `--prefix repoa` 다 → sanity 통과.
_MULTIREPO_REPOS = ("repoa", "repob")
# multi-repo wave 가 각 repo 작업 폴더에 쓰도록 지시하는 산출 파일·내용 — 단언은 이 파일이 그 저장소의
# 묶음 브랜치와 통합 브랜치에 있는지 본다(단일 wave 의 PROBE_FILE='probe.txt' 와 별개).
_WAVE_FILE = "wave-done.txt"


def _seed_git_repo(path: Path) -> None:
    """seed git repo(main·1 commit) 생성 — repo add 의 bare-clone 원(ADR-0011)."""
    path.mkdir(parents=True, exist_ok=True)
    _git = lambda *a: subprocess.run(["git", "-C", str(path), *a], check=True,
                                     capture_output=True, text=True, encoding="utf-8", errors="replace")
    _git("init", "-q")
    _git("config", "user.email", "probe@local")
    _git("config", "user.name", "probe")
    (path / "README.md").write_text(f"# {path.name}\n", encoding="utf-8")
    _git("add", "-A")
    _git("commit", "-q", "-m", "init")
    _git("branch", "-M", "main")


def _pm_config(home: Path, *args: str) -> subprocess.CompletedProcess:
    """home 의 pm_config.py 호출(엔진 도구·LLM 아님 → 부모 env 상속 OK·모델 무관)."""
    return subprocess.run(
        [sys.executable, str(home / ".project_manager" / "tools" / "pm_config.py"), *args],
        cwd=str(home), capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, "PM_NONINTERACTIVE": "1"},
    )


def _import_multipm_home(tmp_path: Path, harness: str,
                         repos: tuple[str, ...] = _MULTIREPO_REPOS,
                         *, area_owners: dict[str, str] | None = None) -> Path:
    """multi-PM 홈 import (hermetic) — fresh adopter 위에 `repo add`·`worktree add` 로 multi-repo 셋업.

    단일 `_import_adopter`(test_fresh_adopter_runtime_smoke) 와 *다른* 셋업이다 — 그건 import 만,
    이건 그 위에 repo 마다 [seed git repo → `pm_config repo add` → `pm_config worktree add`] 를
    얹어 공유 보드 + 슬롯(`work/<repo>_1`)을 만든다. `_load_pm_import`·`_real_models_runner` 스텁을
    재사용해 라이브 models 조회를 차단(hermetic). home 디렉토리를 반환한다.

    `area_owners`(선택·`{repo: user}`): 그 repo 의 areas.md `area_owner`(그 area 의 user 소유)를
    `pm_config repo add --user <user>` 로 distinct 2 user 로 스탬프한다 — multi-USER composite
    (`_import_multiuser_home`·T-0309). 미지정(기본·None)이면 현행 동작(빈 area_owner·단일 user).
    """
    pm_import = _load_pm_import()
    pm_import._real_models_runner = lambda source: (False, [])
    home = tmp_path / f"mpm-home-{harness}"
    origins = tmp_path / f"origins-{harness}"
    rc = pm_import.main(
        ["--new", str(home), "--harness", harness, "--name", "MPM", "--fill", "manual"]
    )
    assert rc == 0, f"{harness} multi-PM home import 실패 (rc={rc})"

    for repo in repos:
        _seed_git_repo(origins / repo)
        # 등록 owner(registrant) 는 T-0779 부터 세션 해소 체인(명시 > $PM_SESSION_NAME > leased 1개
        # > None)으로만 정해진다 — fresh 홈은 아직 worktree(lease)가 0개이므로(repo add 가 worktree
        # add 보다 먼저) local.conf session= 폴백(폐지)에 의존하던 이전 암묵 해소가 사라졌다. 이
        # 헬퍼가 재현하는 "한 사람이 여러 repo 를 attach 하는 부트스트랩" 시나리오의 registrant 를
        # `--owner`로 명시(area_owner 축의 `--user` 와는 별개 — registrant=등록 행위자·area_owner=
        # 그 area 의 user 소유).
        add_args = ["repo", "add", repo, "--git", str(origins / repo), "--owner", "bootstrap"]
        # area_owners 지정 시 그 repo 의 area_owner(=그 area 의 user 소유)를 `--user` 로 스탬프한다
        # (multi-USER composite). areas.md `area_owner` 칼럼은 `_ticket_owner`(open 소유)의 소유 유도
        # 소스다 — distinct 2 user 여야 세션 뷰가 strict-exclude(섞임 격리)로 돈다. querying identity 는
        # user-first(ADR-0056)로 현재 사용자다(area_owner-derived 폐기).
        if area_owners and repo in area_owners:
            add_args += ["--user", area_owners[repo]]
        added = _pm_config(home, *add_args)
        assert added.returncode == 0, (
            f"repo add {repo} 실패 (rc={added.returncode})\n"
            f"stdout={added.stdout[-600:]}\nstderr={added.stderr[-600:]}"
        )
        # 이 helper는 사용자가 승인한 multi-PM 홈 셋업을 실제 CLI로 재현한다. 신규 슬롯
        # 거부 테스트가 아니므로 대상 repo에 값-결속된 ack를 명시한다(T-0636 픽스처 정책).
        slotted = _pm_config(
            home, "worktree", "add", repo, "--user-ack", repo
        )
        assert slotted.returncode == 0, (
            f"worktree add {repo} 실패 (rc={slotted.returncode})\n"
            f"stdout={slotted.stdout[-600:]}\nstderr={slotted.stderr[-600:]}"
        )
    return home


def _multirepo_wave_home(tmp_path: Path, harness: str) -> Path:
    """다중 저장소 웨이브 홈 — 저장소마다 작업 폴더 2개(통합 1번·작업 2번)를 웨이브 task 가 보유한다([[T-0958]]).

    `_import_multipm_home` 이 저장소마다 만든 첫 폴더에 `worktree add` 로 둘째 폴더를 더하고, task 를 만든 뒤
    `alloc` 으로 네 폴더를 모두 빌린다(`_guided_adopter` 와 같은 안내 경로). 코드 변경의 머지는 base_branch 를
    든 폴더에서만 되므로 저장소마다 폴더 2개가 카드 흐름의 전제다.
    """
    home = _import_multipm_home(tmp_path, harness)
    steps = [("worktree", "add", repo, "--user-ack", repo) for repo in _MULTIREPO_REPOS]
    steps.append(("task", "new", _RELEASE_WAVE_TASK))
    steps += [("alloc", repo, "--task", _RELEASE_WAVE_TASK)
              for repo in _MULTIREPO_REPOS for _slot in (_WAVE_INTEGRATION_SLOT, _WAVE_WORK_SLOT)]
    for step in steps:
        proc = _pm_config(home, *step)
        assert proc.returncode == 0, (
            f"다중 저장소 웨이브 홈 {' '.join(step)} rc={proc.returncode}\n"
            f"stdout={proc.stdout[-600:]}\nstderr={proc.stderr[-600:]}"
        )
    return home


# 프롬프트의 티켓 ID 자리 — 모델(과 비라이브 재현)이 그 저장소 발행 출력의 ID 로 바꾼다.
_MULTIREPO_TICKET = "<TICKET_ID>"


def _multirepo_wave_commands(repo: str) -> tuple[str, ...]:
    """저장소 하나의 웨이브 실행 줄 7개 — 카드 줄에 그 저장소 값과 폴더 지목을 채운 것, 카드 순서([[T-0958]]).

    카드 순서는 new → cluster new → promote → tier → claim → switch → 종결이다([[T-0960]] · board 형상과
    무관한 한 순서). 발행·claim 은 작업 폴더(2번), 묶음 선언은 통합 폴더(1번)를 지목하고 switch 는 작업
    폴더를 묶음 브랜치로 옮긴다. 발행에는 새 prefix 의 값-결속 승인과 바꿀 파일 선언을 붙인다. 파일 쓰기와 완료 조건 체크는
    switch(6번째)와 종결(7번째) 사이의 사람 몫이다(프롬프트가 지시한다).
    """
    task, tid, cluster = _RELEASE_WAVE_TASK, _MULTIREPO_TICKET, f"wave-{repo}"
    work = f"--repo {repo} --slot {_WAVE_WORK_SLOT}"
    integration = f"--repo {repo} --slot {_WAVE_INTEGRATION_SLOT}"
    tools = "python3 .project_manager/tools"
    return (
        f'{tools}/board.py new "wave probe {repo}" --task {task} {work} '
        f"--prefix {repo} --user-ack {repo} --touches {_WAVE_FILE}",
        f"{tools}/board.py cluster new {cluster} --tickets {tid} --task {task} {integration}",
        f"{tools}/board.py promote {tid}",
        f"{tools}/board.py tier {tid} pm-direct",
        f"{tools}/board.py claim {tid} --task {task} {work}",
        f"{tools}/worktree_pool.py switch work/{repo}_{_WAVE_WORK_SLOT} task/{cluster}",
        f"{tools}/ticket_finish.py {tid} --task {task} --no-pytest",
    )


def _multirepo_wave_prompt(repos: tuple[str, ...] = _MULTIREPO_REPOS) -> str:
    """한 세션이 공유 보드 위 저장소마다 코드 변경 하나를 카드 흐름대로 종결하라는 프롬프트([[T-0958]]).

    새 위험축은 그대로다 — 한 세션이 저장소별 prefix(`--prefix <repo>` → `T-<repo>-NNN`)와 저장소별 폴더
    지목(`--repo <repo> --slot <N>`)을 바르게 다루는가. 실행 줄은 `_multirepo_wave_commands` 가 카드 줄에
    값을 채운 것이고 순서도 카드 순서다(비라이브 재현이 같은 줄을 실행한다). 맨 앞의 부트스트랩은 가드 훅이
    이 세션을 task 에 묶게 해 작업 폴더 쓰기를 연다. 신규 prefix 승인은 이 프롬프트가 주체이므로 저장소마다
    발행 줄 한 곳에만 값-결속 `--user-ack` 를 둔다(T-0744).
    """
    blocks = []
    for index, repo in enumerate(repos, start=1):
        commands = _multirepo_wave_commands(repo)
        lines = [f"Wave {index} (repo {repo}):"]
        lines += [f"  {n}. {command}" for n, command in enumerate(commands[:6], start=1)]
        lines.append(
            f"  7. Write the file work/{repo}_{_WAVE_WORK_SLOT}/{_WAVE_FILE} containing exactly "
            f"\"done by {repo}\" — only after step 6 and only in that folder. The finish step commits it."
        )
        lines.append(
            "  8. In the claimed ticket file (.project_manager/wiki/tickets/claimed/<TICKET_ID>-*.md) change "
            "every completion-condition checkbox `- [ ]` to `- [x]`."
        )
        lines.append(f"  9. {commands[6]}")
        blocks.append("\n".join(lines))
    folders = ", ".join(
        f"work/{repo}_{_WAVE_INTEGRATION_SLOT} (integration) and work/{repo}_{_WAVE_WORK_SLOT} (work)"
        for repo in repos
    )
    return (
        "You operate a multi-PM project-manager home that shares ONE board across "
        f"{len(repos)} code repos: {' and '.join(repos)}. The board task `{_RELEASE_WAVE_TASK}` already "
        f"holds two folders of each repo: {folders}. The integration folder keeps the merge target "
        "branch; you change files only in the work folder.\n\n"
        "This release-test instruction explicitly approves each repo's value-bound --user-ack for its "
        "new test prefix; do not invent or alter acknowledgements.\n\n"
        "First open the task session (the write guard allows edits inside the work folders only after "
        f"this):\n  python3 .project_manager/tools/pm_bootstrap.py --task {_RELEASE_WAVE_TASK}\n\n"
        "Then run one wave per repo, finishing one repo completely before starting the next. Run every "
        "command exactly as written and in this order; replace <TICKET_ID> with the ticket id printed by "
        "that repo's step 1. This project ships no test suite, so the finish command carries --no-pytest.\n\n"
        + "\n\n".join(blocks)
        + "\n\nReply 'done' when both tickets are done."
    )


def _ticket_field(path: Path, key: str) -> str | None:
    """티켓·묶음 장부 frontmatter 의 `key: 값` 한 줄 값."""
    match = re.search(rf"^{key}:\s*(\S+)\s*$", path.read_text(encoding="utf-8"), flags=re.M)
    return match.group(1) if match else None


def _assert_multirepo_wave_side_effects(home: Path, proc: subprocess.CompletedProcess,
                                        harness: str,
                                        repos: tuple[str, ...] = _MULTIREPO_REPOS) -> None:
    """저장소마다 결과의 실체를 본다 — 티켓 done·묶음 closed·바꾼 파일이 묶음 브랜치와 통합 브랜치에 있다.

    (1) `tickets/done/T-<repo>-*.md` 가 정확히 하나다(그 저장소 prefix 로 발행·종결 완주).
    (2) 그 티켓 `cluster` 장부가 `status: closed` 이고 `base_branch` 가 통합 폴더 브랜치(`<repo>_1`)다.
    (3) 그 저장소 bare 에서 묶음 `branch` 와 `base_branch` 의 `wave-done.txt` 내용이 `done by <repo>` 다.
        묶음 브랜치에 있어야 작업 폴더에서 고친 변경이 종결의 머지로 들어간 것이다. 폴더 안 파일 존재만 보면
        커밋되지 않은 파일과 통합 브랜치 직접 커밋을 가리지 못한다([[T-0958]] · 6d1c39c9 라이브 두 하네스).
    """
    root = home / ".project_manager" / "wiki" / "tickets"
    tail = (
        f"--- {harness} stdout(tail) ---\n{proc.stdout[-2500:]}\n"
        f"--- stderr(tail) ---\n{proc.stderr[-1000:]}"
    )
    for repo in repos:
        done = sorted((root / "done").glob(f"T-{repo}-*.md"))
        assert len(done) == 1, (
            f"실 {harness} multi-repo wave: repo '{repo}' 의 done ticket(T-{repo}-*) 이 {len(done)}개.\n"
            f"all done={_tickets_in(home, 'done')}\n"
            f"open={_tickets_in(home, 'open')} claimed={_tickets_in(home, 'claimed')}\n" + tail
        )
        cluster = _ticket_field(done[0], "cluster")
        ledger = root / "clusters" / f"{cluster}.md"
        assert cluster and ledger.is_file(), f"repo '{repo}' done ticket 의 묶음 장부 부재: {cluster}\n" + tail
        status, base, branch = (_ticket_field(ledger, key) for key in ("status", "base_branch", "branch"))
        assert status == "closed", f"repo '{repo}' 묶음 {cluster} status={status} (closed 아님)\n" + tail
        assert base == f"{repo}_{_WAVE_INTEGRATION_SLOT}", (
            f"repo '{repo}' 묶음 {cluster} 의 base_branch {base!r} 가 통합 폴더 브랜치가 아니다.\n" + tail
        )
        expected = f"done by {repo}"
        for label, ref in (("묶음 브랜치", branch), ("통합 브랜치", base)):
            content = _integration_file(home, ref, _WAVE_FILE, repo=repo) if ref else None
            assert content is not None and content.strip() == expected, (
                f"실 {harness} multi-repo wave: repo '{repo}' {label} {ref} 의 {_WAVE_FILE} = {content!r} "
                f"(기대 {expected!r}) — 작업 폴더에서 고쳐 종결로 머지하지 않았다.\n" + tail
            )


@pytest.mark.release
@pytest.mark.skipif(
    not _RELEASE_LIVE or not shutil.which("opencode"),
    reason="release wave multi-repo — PM_ORCH_LIVE_RELEASE=1 + opencode CLI(+ollama 모델) 필요. "
           "기본 skip·사용자 트리거.",
)
def test_release_wave_multirepo_opencode_full_wave(tmp_path):
    """실 opencode(agentic·ollama)가 다중 저장소 홈(2 repo·공유 보드)에서 저장소마다 카드 흐름 웨이브를 닫는다.

    홈은 `_multirepo_wave_home`(저장소마다 통합 폴더 1번·작업 폴더 2번을 task 가 보유)이고, 프롬프트
    (`_multirepo_wave_prompt`)는 부트스트랩 줄 다음에 저장소마다 카드 줄 7개(new·cluster new·promote·tier·
    claim·switch·종결)에 값을 채운 실행 줄과 그 사이 파일 쓰기·완료 조건 체크를 싣는다. direct complete 는
    프롬프트에 없다([[T-0958]]). 단일 full wave 와 다른 검증축이다: 한 세션이 공유 보드 위 저장소별
    prefix(`--prefix <repo>`)와 폴더 지목(`--repo <repo> --slot <N>`)을 바르게 다루는가.
    단언은 결과의 실체만 본다(`_assert_multirepo_wave_side_effects`): 저장소마다 done 티켓 1개, 그 묶음
    closed 와 base_branch `<repo>_1`, 묶음 브랜치와 통합 브랜치의 wave-done.txt 내용. 출력 문구 비결정에
    강건하다(T-0157 동형). `--dir` 로 루트 핀(opencode 는 PWD 로 루트 오판). API 과금 0(로컬/cloud ollama).
    """
    home = _multirepo_wave_home(tmp_path, "opencode")

    proc = _run_opencode_live(
        # `--dangerously-skip-permissions`: 비대화 헤드리스 격리(throwaway tmp home)라 안전 —
        # 단일 wave 테스트와 동일 근거(opencode 가 --dir 디렉토리를 external 로 보고 auto-reject).
        ["opencode", "run", "--agent", "build", "--dir", str(home),
         "--dangerously-skip-permissions", "-m", LIVE_MODEL,
         _multirepo_wave_prompt()],
        cwd=str(home), env=_live_env(LIVE_MODEL), timeout=_OPENCODE_TIMEOUT,
    )

    # 결과 실체(hard) — 저장소마다 done 티켓 1개·묶음 closed·base_branch·묶음 브랜치와 통합 브랜치의 파일 내용.
    _assert_multirepo_wave_side_effects(home, proc, "opencode")


@pytest.mark.release
@pytest.mark.skipif(
    not _RELEASE_LIVE or not shutil.which("claude"),
    reason="release wave multi-repo — PM_ORCH_LIVE_RELEASE=1 + claude CLI 필요(API 과금). "
           "기본 skip·사용자 트리거.",
)
def test_release_wave_multirepo_claude_full_wave(tmp_path):
    """실 claude(`claude-sonnet-4-6`)가 다중 저장소 홈(2 repo·공유 보드)에서 저장소마다 카드 흐름 웨이브를 닫는다.

    opencode 짝(`test_release_wave_multirepo_opencode_full_wave`)과 같은 홈·프롬프트·단언에 단일 claude
    실행 방식(`--allowedTools Bash`·subprocess cwd 존중이라 `--dir` 불요)을 얹은 것이다. 프롬프트는
    부트스트랩 줄과 저장소마다 카드 줄 7개(new·cluster new·promote·tier·claim·switch·종결)이고 direct
    complete 는 없다([[T-0958]]). 단언은 저장소마다 done 티켓 1개, 그 묶음 closed 와 base_branch `<repo>_1`,
    묶음 브랜치와 통합 브랜치의 wave-done.txt 내용이다(`_assert_multirepo_wave_side_effects`). 웨이브에
    developer·reviewer 위임이 없어 stream-json `subagent_type` 단언은 하지 않는다. API 과금.
    """
    home = _multirepo_wave_home(tmp_path, "claude")

    proc = subprocess.run(
        # `--allowedTools Bash`: 웨이브는 부트스트랩·카드 줄 실행(board.py·worktree_pool.py·ticket_finish.py)과
        # 파일 쓰기뿐이고 developer·reviewer 위임이 없어 Task 불요(단일 full wave 와 다른 점). claude 는
        # subprocess cwd 를 존중하므로 `--dir` 불요(opencode 와 다른 점). 결과 실체만 단언하므로 stream-json 도 불요.
        ["claude", "-p", "--model", CLAUDE_MODEL,
         "--allowedTools", "Bash",
         "--dangerously-skip-permissions",
         _multirepo_wave_prompt()],
        cwd=str(home), capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=_live_env(CLAUDE_MODEL), timeout=_CLAUDE_TIMEOUT,
    )

    # 결과 실체(hard) — 저장소마다 done 티켓 1개·묶음 closed·base_branch·묶음 브랜치와 통합 브랜치의 파일 내용.
    _assert_multirepo_wave_side_effects(home, proc, "claude")


# ── multi-USER composite 경로 (2 user 공유보드 뷰 섞임 격리 · release-marked 라이브 half · T-0309) ──
# 위 multi-repo 테스트는 *한* user(단일 정체성)가 여러 repo 슬롯을 운영하는 축이다. 아래는 그 직교
# 축 — **2 distinct user(alice/bob)가 공유 보드에서 각자 티켓을 만들고 세션 뷰가 서로 섞이지 않는가**
# (ADR-0053 세션격리 불변식). T-0304(`test_board_scoping_isolation`)가 이 불변식을 *무-LLM* durable
# 로 못박고(실 board API create→view), 이 라이브 half 는 실 opencode 세션이 각 identity 로 티켓을
# 생성한 뒤 뷰 섞임을 side-effect 로 검증한다(라이브 opencode end-to-end·release tier).

# 각 identity = (repo, prefix, task, user). prefix(al/be)는 repo(alpha/beta)와 별개 축(ID
# 네임스페이스·소문자 sanity `_validate_prefix`)이고, task 는 그 user 의 귀속 정체성이다(T-0962 —
# 귀속 쓰기는 `--task`). task 마다 그 repo 폴더 1개를 빌린다. `test_board_scoping_isolation._seed_composite` 와
# 동일 매핑을 라이브 홈에 재현한다. areas.md `area_owner` 칼럼이 open 티켓 소유(`_ticket_owner`·
# alpha→alice·beta→bob)를 정의한다(user-first·ADR-0056: querying identity 는 현재 사용자).
_MULTIUSER_REPOS = ("alpha", "beta")
_MULTIUSER_AREA_OWNERS = {"alpha": "alice", "beta": "bob"}
_MULTIUSER_IDENTITIES = (
    # (repo,   prefix, task,         user)
    ("alpha", "al", "alice-wave", "alice"),
    ("beta",  "be", "bob-wave",   "bob"),
)


def _import_multiuser_home(tmp_path: Path, harness: str) -> Path:
    """multi-USER 홈 — 2 repo(alpha/beta)에 distinct area_owner(alice/bob)를 등록한 multi-PM 셋업.

    `_import_multipm_home` 을 재사용하되 `area_owners` 로 repo add 에 `--user <owner>` 를 실어
    areas.md `area_owner` 칼럼을 alpha→alice·beta→bob 로 distinct 2 user 로 세팅한다(=
    `test_board_scoping_isolation._seed_composite` 의 areas 매핑을 라이브 홈에 재현). 그 위에서
    identity 마다 task 를 만들고 `pm-config alloc <repo> --task <task>` 로 그 repo 폴더를 빌린다(귀속
    쓰기의 정체성은 `--task`·T-0962). LLM 없이 도는 hermetic 셋업(`_real_models_runner` 스텁 상속).
    home 을 반환한다.
    """
    home = _import_multipm_home(tmp_path, harness, repos=_MULTIUSER_REPOS,
                                area_owners=_MULTIUSER_AREA_OWNERS)
    for repo, _prefix, task, _user in _MULTIUSER_IDENTITIES:
        for step in (("task", "new", task), ("alloc", repo, "--task", task)):
            proc = _pm_config(home, *step)
            assert proc.returncode == 0, (
                f"multiuser 홈 {' '.join(step)} rc={proc.returncode}\n"
                f"stdout={proc.stdout[-600:]}\nstderr={proc.stderr[-600:]}"
            )
    return home


def _multiuser_wave_prompt(identities: tuple = _MULTIUSER_IDENTITIES) -> str:
    """각 identity(alice/bob)가 공유 보드에서 자기 정체성으로 [미claim open + claim] 티켓을 만들라는 프롬프트.

    섞임 격리를 실증하려면 각 user 가 (i) 미claim open (다른 user 뷰가 절대 유출하면 안 되는 대상) +
    (ii) claim 티켓 (자기 뷰엔 열람) 을 남겨야 한다. 각 board 조작에 `--user <user>` 를 실어 티켓
    귀속(created_by/claimed_by user)을 distinct 2 user 로 스탬프한다 — 이게 `_distinct_ticket_users`
    다중사용자 신호(≥2)를 세워 세션 뷰가 strict-exclude(degrade 아님)로 돌게 한다. `--prefix` 로 ID
    네임스페이스(T-al-*/T-be-*)를 가르고, 이 release-test 프롬프트 자체가 신규 테스트
    prefix 승인 주체이므로 각 prefix의 **첫** `new` 원 명령에만 값-결속된
    `--user-ack <prefix>`를 명시한다. 에이전트가 ack를 자동 추가하는 형상은 테스트하지
    않으며(기계 가드가 금지), 두 번째 `new`는 이미 등록된 prefix를 쓴다. new 두 줄과 claim 줄은
    그 identity 의 `--task <task>` 를 싣는다(귀속 쓰기의 정체성·T-0962 — task 폴더가 1개라 위치 인자 없음).
    board.py 경로는 준다 — 새 위험축은 identity 귀속·뷰 격리이지 문서 운영성이 아니다(단일 full wave 커버).
    """
    blocks = []
    for _repo, prefix, task, user in identities:
        blocks.append(
            f"Person {user} (prefix {prefix}, task {task}) — do these 3 commands:\n"
            f'  1. python3 .project_manager/tools/board.py new "open probe {user}" '
            f"--prefix {prefix} --user-ack {prefix} --user {user} --task {task}\n"
            f"     (prints a ticket id like T-{prefix}-001 — this is {user}'s UNCLAIMED open; "
            f"do NOT claim it)\n"
            f'  2. python3 .project_manager/tools/board.py new "wip probe {user}" '
            f"--prefix {prefix} --user {user} --task {task}\n"
            f"     (prints a SECOND id, e.g. T-{prefix}-002 — note it)\n"
            f"  3. python3 .project_manager/tools/board.py claim <SECOND_ID> "
            f"--user {user} --task {task}\n"
        )
    body = "\n".join(blocks)
    people = " and ".join(u for _r, _p, _s, u in identities)
    return (
        f"You operate ONE shared project-manager board used by {len(identities)} different "
        f"people: {people}. The board engine is .project_manager/tools/board.py. Act as EACH "
        "person in turn and create their tickets with THEIR identity flags EXACTLY as written — "
        "this release-test instruction explicitly approves each first command's value-bound "
        "--user-ack for its new test prefix; do not invent or alter acknowledgements. The --user, "
        "--task and --prefix flags decide who owns each ticket, so never omit them. Finish "
        "one person completely before starting the next.\n\n"
        f"{body}\n"
        "Substitute the EXACT ticket id printed by each `new` command into the matching `claim` "
        f"command. Reply 'done' when all {2 * len(identities)} tickets exist."
    )


def _board_list(home: Path, *args: str) -> subprocess.CompletedProcess:
    """home 의 board.py list 를 subprocess 로 호출(엔진 도구·LLM 아님 → 부모 env OK).

    격리 판정은 **테스트가 직접** board.py 를 돌려 실 산출(뷰)을 파싱한다 — LLM 출력 phrasing
    비결정에 강건(side-effect 단언·T-0157 동형). `--task <task>` 는 아무것도 안 바꾸는 뷰 렌즈.
    """
    return subprocess.run(
        [sys.executable, str(home / ".project_manager" / "tools" / "board.py"), "list", *args],
        cwd=str(home), capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, "PM_NONINTERACTIVE": "1"},
    )


def _set_home_user(home: Path, user: str) -> None:
    """home local.conf 에 `identity.user=` 를 append(last-wins) — 필터 뷰 querying identity 를 그 user 로 스탬프.

    user-first(ADR-0056·T-0312): `list --task <task>` 는 **현재 사용자 ∩ task**라, 각 identity 의
    task 뷰는 *그 user 로* 조회해야 자기 claim 이 보인다(옛 area_owner-derivation 폐기 — `--task` 가
    area_owner 로 user 를 유도하지 않는다). `load_local_config` 는 KEY 마지막 값 채택이라 append 가
    이긴다(`_append_tiny_ctx_window` 동형). machine composite(`test_board_scoping_isolation`
    `_write_conf(user=…)`)의 라이브 짝.
    """
    conf = home / ".project_manager" / "local.conf"
    conf.write_text(conf.read_text(encoding="utf-8") + f"\nidentity.user={user}\n", encoding="utf-8")


def _parse_list_rows(stdout: str) -> list[tuple[str, str]]:
    """board.py list 출력에서 (status, ticket_id) 목록을 파싱 (`  [status ] T-...  title …`).

    `test_board_scoping_isolation._view` 동형 파싱을 subprocess stdout 에 적용한다 — `[` 로 시작하는
    행만, `]` 뒤 첫 토큰이 ticket id. 비-행(헤더·`(no tickets)`·경고)은 조용히 무시.
    """
    rows: list[tuple[str, str]] = []
    for line in stdout.splitlines():
        s = line.strip()
        if s.startswith("[") and "]" in s:
            status = s[1:s.index("]")].strip()
            rest = s.split("]", 1)[1].split()
            if rest:
                rows.append((status, rest[0]))
    return rows


def _assert_multiuser_view_isolation(home: Path, proc: subprocess.CompletedProcess,
                                     harness: str,
                                     identities: tuple = _MULTIUSER_IDENTITIES) -> None:
    """multi-USER 뷰 섞임 격리 단언 — 각 user 세션 뷰가 타 user 티켓을 미열람·자기 것만 열람.

    테스트가 직접 board.py list 를 돌려(side-effect·LLM phrasing 비결정 강건) 실 뷰를 파싱한다:
      (전제) 두 user 가 실제로 티켓을 만들었고(wave 완주) 각자 미claim open 을 남겼다 — 미claim open
             이 없으면 섞임 assert 가 공허해진다(degrade 가 유출할 대상 open 이 실재해야 catch).
      (a) alice task(alice-wave) 뷰 = T-al-* 만·bob(T-be-*) **미열람** · bob task(bob-wave) 뷰 = 역.
      (c) 각자 자기(T-<own>-*) 열람 (섞임 격리가 자기 것까지 지우는 over-exclude 도 잡음).
    ID prefix(T-al-*/T-be-*)가 소유 user 와 1:1 대응(각 user 가 자기 --prefix 로 발행)이라, "alice 뷰에
    T-be-* 0" = alice 세션이 bob 소유 티켓을 유출 안 함 = ADR-0053 세션격리. degrade(전체 open=mine)면
    alice 뷰에 bob 미claim open(T-be-*)이 섞여 이 단언이 red 로 잡는다(실 격리 검증·verify-real-output).
    """
    tail = (f"--- {harness} stdout(tail) ---\n{proc.stdout[-2500:]}\n"
            f"--- stderr(tail) ---\n{proc.stderr[-1000:]}")

    # (전제) 전체 보드 — 두 user 가 티켓을 만들었고 각자 미claim open 을 남겼나.
    # 전제 확인 = 전체 보드 상세 — ADR-0066 이후 무인자 기본 뷰는 내 스트림 스코프(미귀속 open 접힘)
    # 이라 `--all` 로 전체를 본다(v1.3.2 livegate red 근본원인).
    full = _board_list(home, "--all", "--status", "all")
    assert full.returncode == 0, (
        f"board.py list --status all rc={full.returncode}\n{full.stderr[-800:]}\n" + tail)
    all_rows = _parse_list_rows(full.stdout)
    for _repo, prefix, _task, user in identities:
        owned = [tid for _st, tid in all_rows if tid.startswith(f"T-{prefix}-")]
        assert owned, (
            f"실 {harness} multiuser wave: user '{user}'(prefix {prefix}) 티켓 미생성 — wave 미완주.\n"
            f"all={all_rows}\n" + tail)
        owned_open = [tid for st, tid in all_rows
                      if tid.startswith(f"T-{prefix}-") and st == "open"]
        assert owned_open, (
            f"실 {harness} multiuser wave: user '{user}' 미claim open(T-{prefix}-*) 부재 — 섞임 "
            f"격리 assert 가 공허해진다(유출 대상 open 이 실재해야 catch).\nall={all_rows}\n" + tail)

    # (a)·(c) 각 identity task 뷰 — 타 user 미열람 · 자기 열람.
    prefixes = [p for _r, p, _t, _u in identities]
    for _repo, prefix, task, user in identities:
        # user-first(ADR-0056·T-0312): task 뷰 querying identity = 현재 사용자. 각 identity 의
        # `--task <task>` 뷰는 *그 user 로* 조회해야 자기 task claim 이 보인다(area_owner-
        # derivation 폐기). 그 user 로 스탬프 후 조회 — 아니면 over-exclude(자기 claim 도 안 보임).
        _set_home_user(home, user)
        view = _board_list(home, "--task", task)
        assert view.returncode == 0, (
            f"board.py list --task {task} rc={view.returncode}\n{view.stderr[-800:]}\n" + tail)
        ids = {tid for _st, tid in _parse_list_rows(view.stdout)}
        others = [op for op in prefixes if op != prefix]
        leaked = {tid for tid in ids if any(tid.startswith(f"T-{op}-") for op in others)}
        assert not leaked, (
            f"실 {harness}: {user} task({task}) 뷰에 타 user 티켓 유출 {sorted(leaked)} — 세션 뷰 "
            f"섞임(ADR-0053 위반·degrade 재현).\n뷰={sorted(ids)}\n" + tail)
        assert any(tid.startswith(f"T-{prefix}-") for tid in ids), (
            f"실 {harness}: {user} task({task}) 뷰가 자기 티켓(T-{prefix}-*)을 미열람 — 섞임 격리가 "
            f"자기 것까지 지움(over-exclude).\n뷰={sorted(ids)}\n" + tail)

    # task 렌즈(`--task <이름>`·T-0365·[[ADR-0059]] Decision 10) 라이브 커버 — 기계 composite
    # (`test_board_scoping_isolation` 의 task 축 surface)와 **짝**(decision: 둘 중 하나만 갱신하면
    # 라이브/기계 뷰가 어긋난다). 라이브 wave 는 identity task 의 claim(claimed_by=<user>/<task>)만 남기므로
    # 그 위에서 다른 task 이름의 렌즈가 (i) 타 user 무유출 (ii) 그 claim 을 다른 task 이름으로 안 끌어옴
    # (claimed_by 재사용) 을 실증한다. task 바인딩 claim 이 없는 fresh task 명은 (claim 0 + 내 소유 open
    # backlog) 로 좁혀져야 한다 — 무필터 전체 보드로 새지 않음(핸들러가 `--task` 를 실 소비).
    owned_open_by_prefix = {p: {tid for st, tid in all_rows
                                if tid.startswith(f"T-{p}-") and st == "open"}
                            for _r, p, _t, _u in identities}
    owned_claimed_by_prefix = {p: {tid for st, tid in all_rows
                                   if tid.startswith(f"T-{p}-") and st == "claimed"}
                               for _r, p, _t, _u in identities}
    for _repo, prefix, _task, user in identities:
        _set_home_user(home, user)
        # identity task(`<user>-wave`)와 다른 자유 task 명 — 같은 이름이면 그 task 의 claim 이 정당하게
        # 보여 판별 검증이 공허해진다.
        fresh_task = f"{prefix}-probe-task"
        tview = _board_list(home, "--task", fresh_task)
        assert tview.returncode == 0, (
            f"board.py list --task {fresh_task} rc={tview.returncode}\n{tview.stderr[-800:]}\n" + tail)
        tids = {tid for _st, tid in _parse_list_rows(tview.stdout)}
        others = [op for op in prefixes if op != prefix]
        leaked = {tid for tid in tids if any(tid.startswith(f"T-{op}-") for op in others)}
        assert not leaked, (
            f"실 {harness}: {user} task 렌즈(--task {fresh_task})에 타 user 티켓 유출 {sorted(leaked)} — "
            f"task-aware 세션 격리 위반(ADR-0059).\n뷰={sorted(tids)}\n" + tail)
        # identity task 의 claim(<user>/<task>·T-{prefix}-* claimed)은 fresh task 이름과 안 겹쳐 그
        # 렌즈에서 걸러진다 — 남는 건 claim 0 + 내 소유 open backlog. 미claim open 은 실재(전제 assert)라 뷰가 안 빈다.
        assert not (owned_claimed_by_prefix[prefix] & tids), (
            f"실 {harness}: {user} task 렌즈(--task {fresh_task})에 다른 task 의 claim "
            f"{sorted(owned_claimed_by_prefix[prefix] & tids)} 유입 — task 렌즈가 claimed_by task 를 무시.\n"
            f"뷰={sorted(tids)}\n" + tail)
        assert tids == owned_open_by_prefix[prefix], (
            f"실 {harness}: {user} task 렌즈(--task {fresh_task}) != 내 소유 open backlog "
            f"{sorted(owned_open_by_prefix[prefix])} — 필터 미소비(silent no-op·전체 보드 유출) 또는 "
            f"over-exclude.\n뷰={sorted(tids)}\n" + tail)


@pytest.mark.release
@pytest.mark.skipif(
    not _RELEASE_LIVE or not shutil.which("opencode"),
    reason="release wave multiuser composite — PM_ORCH_LIVE_RELEASE=1 + opencode CLI(+ollama 모델) 필요. "
           "기본 skip·사용자 트리거.",
)
def test_release_wave_multiuser_composite_opencode(tmp_path):
    """실 opencode 세션이 공유 보드에서 2 distinct user(alice/bob)로 티켓을 만들고 세션 뷰가 섞이지 않음을 라이브 실증.

    T-0304(`test_board_scoping_isolation`)의 무-LLM composite 게이트의 **라이브 opencode 짝**(ADR-0053).
    셋업(`_import_multiuser_home`): 공유 보드 홈에 repo alpha(area_owner alice)·beta(area_owner bob)를
    distinct user 로 등록. opencode(agentic·ollama glm-5.2)가 `_multiuser_wave_prompt` 로 각 identity 의
    [미claim open + claim] 티켓을 자기 `--user`/`--prefix`/`--task` 로 만든다.

    격리 판정은 **테스트가 직접** board.py list 를 돌려(side-effect·LLM phrasing 비결정 강건) —
    alice task(alice-wave) 뷰는 bob 티켓(T-be-*) 미열람·자기(T-al-*) 열람, bob task(bob-wave) 뷰는 역 —
    즉 **뷰가 섞이지 않음**을 실 산출로 assert(`_assert_multiuser_view_isolation`). `--dir` 로 루트 핀
    (opencode 는 PWD 로 루트 오판). API 과금 0(로컬/cloud ollama). cross-slot 축은 T-0304 기계 게이트 커버.
    """
    home = _import_multiuser_home(tmp_path, "opencode")

    proc = _run_opencode_live(
        # `--dangerously-skip-permissions`: 비대화 헤드리스 격리(throwaway tmp home)라 안전 —
        # multi-repo 라이브 테스트와 동일 근거(opencode 가 --dir 디렉토리를 external 로 보고 auto-reject).
        ["opencode", "run", "--agent", "build", "--dir", str(home),
         "--dangerously-skip-permissions", "-m", LIVE_MODEL,
         _multiuser_wave_prompt()],
        cwd=str(home), env=_live_env(LIVE_MODEL), timeout=_OPENCODE_TIMEOUT,
    )

    # side-effect(hard) — 각 user 세션 뷰가 타 user 티켓을 미열람·자기만 열람(뷰 섞임 0).
    _assert_multiuser_view_isolation(home, proc, "opencode")


# ── compaction-native 최종 넛지 라이브 단언 (ADR-0038 D4/T-D · T-0190) ─────────────────────────
# 위 wave 테스트는 정상 컨텍스트에서 도는 full wave 다. 아래는 그 *경계* — 최종 넛지가
# 실 claude transcript 위에서 비차단으로 발화하는지를 라이브로 못박는다. 기계 단위 테스트
# (test_claude_ctx_guard)가 로직을 결정적으로 커버하지만,
# transcript-slug 탐색·실 transcript 의 100% 판정·래퍼(.sh) exec 발화는 실 하니스 형상에서만
# 드러나는 갭이다([[verify-real-output-not-just-review]]·설계검증 allow-list 렌즈).

# ctx 예산 극소 설정 — 실 transcript 의 첫 턴이 곧장 stop 밴드(쓴 100%)에 들도록. local.conf
# 에 이 값을 *append* 해 마지막-줄이 이긴다(last-wins·load_local_config 규칙·PM 47 실측).
_TINY_CTX_WINDOW = 2000
# 최종 넛지 훅 stdin 세션 id — marker 파일명(`<sid>.final`)의 단일 진실.
_FINAL_NUDGE_SID = "release-final-nudge-probe"


def _append_tiny_ctx_window(dest: Path) -> None:
    """adopter local.conf 에 극소 claude 사용 한도를 append(last-wins) — 즉발 stop 밴드.

    사용 한도는 하네스별 키(`harness.claude.ctx_window_tokens`)다 — 공통 한도 키는 없다(T-0923).
    append 한 극소값이 *마지막 줄* 로 이겨(load_local_config 는 KEY 마지막 값 채택) 첫 실 턴이
    쓴 100% = stop 밴드. 이 값은 훅 판정만 옮긴다 — 압축 한도 기록의 claude 항목도 같은 값으로
    옮기고(`_override_ctx_record`), 설정 파일 반영(`pm-config apply`)은 돌리지 않으므로 Claude
    Code 의 압축 지점은 출하 값 그대로다.
    """
    conf_path = dest / ".project_manager" / "local.conf"
    conf_path.write_text(
        conf_path.read_text(encoding="utf-8")
        + f"\nharness.claude.ctx_window_tokens={_TINY_CTX_WINDOW}\n",
        encoding="utf-8",
    )
    _override_ctx_record(dest, "claude", _TINY_CTX_WINDOW)


def _claude_project_slug(cwd: Path) -> str:
    """claude Code transcript 디렉토리 slug — cwd 절대경로의 비영숫자를 '-' 로 치환.

    실측: `/home/u/.../project_manager` → `-home-u-...-project-manager`(`/`·`_`·`.` 모두 `-`).
    """
    return re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def _find_claude_transcript(dest: Path, *, not_before: float = 0.0) -> Path | None:
    """turn1 이 남긴 실 transcript(`~/.claude/projects/<cwd-slug>/*.jsonl`) 최신본을 찾는다.

    1차: cwd(=dest) slug 디렉토리 직접 glob. 2차 폴백(resolve/치환 엣지 대비): dest.name slug 로
    끝나는 프로젝트 디렉토리 안을 훑는다. 못 찾으면 None(호출부가 명확 assert).

    `not_before`(test 시작 시각): 폴백이 dest.name('adopter-claude')만으로 매칭하면 **과거 run 의
    잔재 transcript** 를 집어 primary-miss 를 가릴 수 있다(reviewer should-fix) — 이번 run 생성분
    (mtime >= not_before)만 후보로 스코프해 stale false-green 을 차단한다.

    참고(비정리·누적): turn1 transcript 는 사용자 홈(`~/.claude/projects/<tmp-slug>/`)에 남고 이
    테스트는 정리하지 않는다 — tmp-unique slug 라 세션 간 간섭 없음·순수 축적만(release-only 수용).
    """
    projects = Path.home() / ".claude" / "projects"
    candidates = list((projects / _claude_project_slug(dest)).glob("*.jsonl"))
    if not candidates and projects.is_dir():
        tail = _claude_project_slug(Path(dest.name))  # 예: 'adopter-claude'
        for pdir in projects.iterdir():
            if pdir.is_dir() and pdir.name.endswith(tail):
                candidates.extend(pdir.glob("*.jsonl"))
    candidates = [p for p in candidates if p.stat().st_mtime >= not_before]
    return max(candidates, key=lambda p: p.stat().st_mtime) if candidates else None


def _load_adopter_ctx_guard(dest: Path):
    """adopter 가 실제로 쓰는 `.claude/ctx_guard.py` 를 로드 — 같은 machinery 로 % 판정 재현."""
    path = dest / ".claude" / "ctx_guard.py"
    spec = importlib.util.spec_from_file_location("adopter_ctx_guard", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fire_stop_hook(dest: Path, stdin_payload: dict) -> subprocess.CompletedProcess:
    """adopter 의 `.claude/ctx_stop_hook.sh` 래퍼를 하니스처럼 발화(stdin JSON·rc/stdout 그대로).

    claude Code 가 훅을 부르는 방식(래퍼 exec·stdin 에 hook JSON)을 그대로 재현한다 — 래퍼가
    인터프리터 self-resolve 후 ctx_stop_hook.py 를 exec. 엔진-측 스크립트라 LLM 아님·부모 env OK.
    bash 절대경로 경유 스폰 — Windows CreateProcess 는 shebang 스크립트를 직접 실행 못 한다
    (WinError 193·PM 48차 tier3 실측). POSIX 는 shebang 이 bash 라 동치.
    """
    bash = shutil.which("bash") or "bash"
    return subprocess.run(
        [bash, str(dest / ".claude" / "ctx_stop_hook.sh")],
        input=json.dumps(stdin_payload),
        cwd=str(dest), capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, "PM_NONINTERACTIVE": "1"},
    )


@pytest.mark.release
@pytest.mark.skipif(
    not _RELEASE_LIVE or not shutil.which("claude"),
    reason="release wave final nudge — PM_ORCH_LIVE_RELEASE=1 + claude CLI 필요(API 과금). "
           "기본 skip·사용자 트리거.",
)
def test_release_wave_claude_final_nudge_driver_marker_contract(tmp_path):
    """실 claude transcript 로 PreToolUse 최종 넛지·driver marker 소유권을 라이브 단언.

    fresh claude adopter 에 극소 컨텍스트 예산을 적용하고, 1회의 실 Claude 턴으로 만든
    transcript 가 stop 밴드인지 같은 ctx_guard machinery 로 확인한다. 그 transcript 위에서
    adopter 래퍼를 하니스 형상으로 발화해 다음 계약을 핀한다.

      1. PreToolUse + Bash 호출은 ``additionalContext`` 최종 넛지를 내며
         ``permissionDecision``을 포함하지 않는다.
      2. 같은 사이클의 UserPromptSubmit 은 먼저 발화한 PreToolUse 가 공유 marker 를
         소비했으므로 무출력이다.
      3. 훅 소유 ``.final`` marker 는 생성되지만 ``.done``은 생성되지 않는다.
         ``.done`` 생산자는 relay driver 계약이며 T-0553에서 연결한다.

    turn2 LLM 콜은 추가 과금·비결정을 낳으므로 래퍼 발화로 주입/멱등/marker 소유
    계약을 결정적으로 검증한다. claude 는 subprocess cwd 를 존중한다(``--dir`` 불요).
    API 과금은 transcript 생성용 turn1 1콜만 발생한다.
    """
    # 설치가 기록하는 모델을 이 테스트의 실제 세션 모델로 둔다 — 고정 한도 픽스처 기본 이름이면
    # UserPromptSubmit 에 모델 불일치 경고(T-0943)가 실려 중복 넛지 단언과 섞인다.
    dest = _import_adopter(tmp_path, "claude", ctx_model=CLAUDE_MODEL)
    _append_tiny_ctx_window(dest)
    test_start = time.time()  # transcript 탐색 스코프(과거 run 잔재 배제·reviewer should-fix).

    # turn1 — 실 claude 1콜로 transcript 인플레이션(요약 지시). Read 도구로 진입문서를 읽게 허용.
    turn1_prompt = (
        "Read CLAUDE.md and the key docs it references, then write a detailed multi-paragraph "
        "summary of how this project's board tool and PM workflow operate."
    )
    turn1 = subprocess.run(
        ["claude", "-p", "--model", CLAUDE_MODEL,
         "--allowedTools", "Bash", "Read",
         "--dangerously-skip-permissions", turn1_prompt],
        cwd=str(dest), capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=_live_env(CLAUDE_MODEL), timeout=_CLAUDE_TIMEOUT,
    )

    transcript = _find_claude_transcript(dest, not_before=test_start)
    assert transcript is not None, (
        "turn1 후 실 claude transcript 를 못 찾음 — 최종 넛지 판정 근거 부재.\n"
        f"찾은 slug={_claude_project_slug(dest)}  projects={Path.home() / '.claude' / 'projects'}\n"
        f"--- claude stdout(tail) ---\n{turn1.stdout[-1500:]}\n"
        f"--- stderr(tail) ---\n{turn1.stderr[-800:]}"
    )

    # 실 transcript 가 극소 window 대비 used=100%/stop 으로 판정되는지(같은 machinery 로 실증).
    ctx_guard = _load_adopter_ctx_guard(dest)
    used = ctx_guard.context_used_pct_from_transcript(str(transcript), _TINY_CTX_WINDOW)
    assert used == 100, (
        f"실 transcript 가 used=100% 로 판정되지 않음(used={used}·window={_TINY_CTX_WINDOW}) — "
        f"stop 밴드 진입 실패.\ntranscript={transcript}"
    )

    base_stdin = {"transcript_path": str(transcript), "session_id": _FINAL_NUDGE_SID}

    # (1) PreToolUse + 새 작업(Bash ls) → additionalContext 비차단 주입.
    pretool = _fire_stop_hook(dest, {
        **base_stdin, "hook_event_name": "PreToolUse",
        "tool_name": "Bash", "tool_input": {"command": "ls -la"},
    })
    assert pretool.returncode == 0 and pretool.stdout.strip(), (
        f"PreToolUse 최종 넛지 미발화.\nstdout={pretool.stdout!r} stderr={pretool.stderr!r}"
    )
    pretool_out = json.loads(pretool.stdout)
    hso = pretool_out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert "ctx-nudge/최종" in hso["additionalContext"]
    assert "permissionDecision" not in hso and "decision" not in pretool_out

    # (2) 같은 사이클 UserPromptSubmit → 채널 공유 `.final` marker 로 중복 없이 통과.
    duplicate = _fire_stop_hook(dest, {
        **base_stdin, "hook_event_name": "UserPromptSubmit", "prompt": "다음 작업",
    })
    assert duplicate.returncode == 0 and duplicate.stdout.strip() == "", (
        f"같은 사이클에서 최종 넛지가 중복 주입됨.\n"
        f"stdout={duplicate.stdout!r} stderr={duplicate.stderr!r}"
    )

    # 훅은 `.final`만 생산. relay driver 소유 `.done`은 T-0553 연결 전제를 핀한다.
    marker_dir = dest / ".project_manager" / ".local" / "ctx-stop"
    assert (marker_dir / f"{_FINAL_NUDGE_SID}.final").exists()
    assert not (marker_dir / f"{_FINAL_NUDGE_SID}.done").exists(), (
        "ctx 훅이 driver 소유 `.done` marker 를 생산함 — T-0553 계약 위반"
    )


# ── hermetic 단위 가드 (라이브 실행 없이·@release/skipif 무관 — 매 회귀 통과) ──────────────
# 위 라이브 테스트는 PM_ORCH_LIVE_RELEASE 미설정 시 skip 이라 CI 에선 안 돈다. 아래 단위는 라이브
# 없이도 돌아 (1) full wave 프롬프트가 5단계 키워드를 담는지 (2) subagent_type walk 가 stream-json
# 샘플에서 값을 정확히 추출하는지 — 라이브 미실행 시에도 mechanics 구조를 가드한다(회귀가 잡음).


def test_full_wave_prompt_has_ticket_growth_stages():
    """full wave 프롬프트가 고정 01→02→03→04와 실제 테스트 계약을 담는다."""
    prompt = _full_wave_prompt("CLAUDE.md", "claude")
    # (1) new — 정확히 1개 ticket 발행 지시.
    assert "create exactly one ticket" in prompt
    # (2) claim.
    assert "claim it" in prompt
    # (3) 성장 역할 4자리 + prepare/harvest + developer probe.
    for role, sentinel in _GROWTH_PIPELINE:
        assert role in prompt and sentinel in prompt
    assert [role for role, _sentinel in _GROWTH_PIPELINE] == [
        "architect", "developer", "code-reviewer", "developer",
    ]
    assert all(f"ROUND {ordinal:02d}" in prompt for ordinal in range(1, 5))
    assert "ticket prepare" in prompt and "ticket harvest" in prompt
    assert PROBE_FILE in prompt and PROBE_TEXT in prompt
    assert _ARCHITECT_TEST_BLOCK in prompt
    assert _WAVE_TEST_FILE in prompt and _WAVE_TARGETED_COMMAND in prompt
    assert _WAVE_FULL_COMMAND in prompt and "Only actual rc=0" in prompt
    assert "review disposition-template" in prompt and "review delta" in prompt
    assert "even when review delta is empty" in prompt
    assert "BEGIN EXACT ARCHITECT BODY" in prompt
    assert "exact body equality" in prompt
    assert "old skeleton follows the sentinel" in prompt
    assert "extract `## 회귀`" in prompt
    assert "exactly two" in prompt and "contains neither" in prompt
    assert "rounds resolve --gate" not in prompt
    assert "ticket prepare --cluster C-wave --role code-reviewer" in re.sub(r"\s+", " ", prompt)
    assert "pass the returned `prompt` file path" in prompt
    assert "Do not search source or help" in prompt
    assert "ticket_finish.py <ticket> --task wave --no-pytest" in prompt
    # 프롬프트는 "직접 board.py complete 는 거부된다" 를 **설명으로** 담는다. 금지 대상은
    # 그 설명이 아니라 옛 처방 커맨드 자체다.
    assert "board.py complete <ticket> --tests-pass" not in prompt
    # 통합 폴더 형상([[T-0956]]) — 발행·claim 은 작업 폴더(2번), 묶음 선언은 통합 폴더(1번)를 지목하고
    # 작업 폴더를 묶음 브랜치로 옮긴 뒤 라운드를 연다.
    flat = re.sub(r"\s+", " ", prompt)
    assert "--task wave --repo adopter-claude --slot 2 --touches probe.txt," in flat
    assert ("board.py cluster new wave --tickets <ticket> --task wave --repo adopter-claude "
            "--slot 1") in flat
    assert "worktree_pool.py switch adopter-claude_2 task/wave" in flat
    assert flat.index("(2) claim it") < flat.index("cluster new wave") < flat.index(
        "ROUND 01 architect:")
    assert "absolute path of the work folder `work/adopter-claude_2`" in flat
    # 생성 회귀 테스트는 작업 폴더에서 홈 보드를 보지 못하므로 probe.txt 만 확인한다(E11).
    assert "`Path(__file__).resolve().parents[1]`" in prompt
    assert "must not read tickets or rounds" in prompt
    assert ".local/delegate-ticket-copies" in prompt and "random run id" in prompt
    assert "including comments and docstrings" in prompt
    assert "occurs zero times in the entire file bytes" in prompt
    assert "never inside the generated test" in prompt
    round02 = prompt[prompt.index("ROUND 02 developer"):prompt.index("ROUND 03 code-reviewer")]
    assert "The `## 회귀` section must contain exactly two nonblank rows" in round02
    assert f"`- 커맨드: `{_WAVE_FULL_COMMAND}``" in round02
    assert "`- 결과: rc=0 · <the one observed full pytest summary>`" in round02
    assert "Put targeted evidence under `## DoD evidence`, not under `## 회귀`" in round02
    assert f"contains neither `{_WAVE_TARGETED_COMMAND}` nor a placeholder" in round02
    assert f"{_GROWTH_PIPELINE[1][1]} exactly once" in round02
    assert "BEGIN EXACT REVIEWER BODY" in prompt
    assert "exact body equality, one review block" in prompt
    assert "harvest ROUND 03 and observe rc=0" in prompt
    assert "never reserve or pre-create ROUND 04 earlier" in prompt
    # (4) 새 프로세스 canonical 재조회.
    assert "fresh board.py show process" in prompt
    # (5) complete + sync gate.
    assert "ticket_finish.py <ticket> --task wave --no-pytest" in prompt
    # 프롬프트는 "직접 board.py complete 는 거부된다" 를 **설명으로** 담는다. 금지 대상은
    # 그 설명이 아니라 옛 처방 커맨드 자체다.
    assert "board.py complete <ticket> --tests-pass" not in prompt
    # 진입문서가 프롬프트에 박힌다(harness 별 CLAUDE.md/AGENTS.md).
    assert "CLAUDE.md" in prompt
    assert "AGENTS.md" in _full_wave_prompt("AGENTS.md", "codex")
    assert _CLAUDE_TIMEOUT_DEFAULT == 900


def test_full_wave_prompt_requires_exact_native_task_sequence():
    """PM main의 round 직접 대체와 Task 생략을 round 시작 전에 차단한다."""
    prompt = _full_wave_prompt("CLAUDE.md", "claude")
    gate = "HARD DELEGATION GATE"
    contracts = (
        "must not directly write, edit, cp, or sed any round file body",
        "architect once, developer once, code-reviewer once, developer once",
        "do not start the next Task until the current Task succeeds and its harvest returns rc=0",
        "If a Task call is missing or fails, stop without directly substituting for it",
        "BEGIN EXACT REVIEWER BODY through END EXACT REVIEWER BODY content below verbatim",
        "role counts exactly architect=1, developer=2, code-reviewer=1",
    )

    assert prompt.index(gate) < prompt.index("(1) create exactly one ticket")
    assert prompt.index(gate) < prompt.index("ROUND 01 architect")
    for contract in contracts:
        assert contract in prompt
    assert prompt.index(contracts[4]) < prompt.index("BEGIN EXACT REVIEWER BODY\n")


def test_full_wave_prompt_uses_each_harness_native_delegation_tool():
    """Claude/OpenCode는 Task, Codex는 존재하는 spawn_agent만 지시한다."""
    claude = _full_wave_prompt("CLAUDE.md", "claude")
    opencode = _full_wave_prompt("AGENTS.md", "opencode")
    codex = _full_wave_prompt("AGENTS.md", "codex")

    for prompt in (claude, opencode):
        assert "Call native Task exactly four times" in prompt
        assert "spawn_agent" not in prompt
    assert "Call native spawn_agent exactly four times" in codex
    assert "next spawn_agent" in codex
    assert "code-reviewer spawn_agent" in codex
    assert "Task" not in codex


def test_codex_native_fixture_baselines_git_before_isolated_writable_call(
    tmp_path, monkeypatch,
):
    """unborn adopter를 host가 추적하고 Codex native wave 한 호출만 git 쓰기를 연다."""
    dest = tmp_path / "adopter-codex"
    (dest / ".project_manager").mkdir(parents=True)
    (dest / ".project_manager" / ".gitignore").write_text(".local/\n", encoding="utf-8")
    (dest / "README.md").write_text("fixture\n", encoding="utf-8")
    init = subprocess.run(
        ["git", "init", str(dest)], capture_output=True, text=True, check=False,
    )
    assert init.returncode == 0

    _baseline_adopter(dest)

    tracked = subprocess.run(
        ["git", "-C", str(dest), "ls-files"], capture_output=True, text=True, check=False,
    )
    assert tracked.returncode == 0
    assert ".project_manager/.gitignore" in tracked.stdout.splitlines()
    assert (dest / ".git" / "index").is_file()
    assert subprocess.run(
        ["git", "-C", str(dest), "rev-parse", "--verify", "HEAD"],
        capture_output=True, text=True, check=False,
    ).returncode == 0

    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    home = tmp_path / "codex-home"
    home.mkdir()
    run_codex_exec("default", dest, home)
    run_codex_exec("native wave", dest, home, sandbox="danger-full-access")
    assert calls[0][calls[0].index("-s") + 1] == "workspace-write"
    assert calls[1][calls[1].index("-s") + 1] == "danger-full-access"

    live_source = inspect.getsource(test_release_wave_codex_native_ticket_growth)
    assert live_source.index('_guided_adopter(tmp_path, "codex"') < live_source.index(
        "run_codex_exec("
    )
    assert 'sandbox="danger-full-access"' in live_source


def test_zero_finding_disposition_is_ticket_owned_before_round04_prepare():
    """zero-finding PM 판정은 reviewer round를 오염시키지 않고 04보다 먼저 닫힌다."""
    prompt = _full_wave_prompt("CLAUDE.md", "claude")
    disposition = "append only the exact finding-zero block emitted by the template"
    ticket_owner = "under `## PM 기계 확인` in the claimed ticket file"
    round_negative = "never in the canonical ROUND 03 reviewer file or any round file"
    count_guard = (
        "zero times in canonical ROUND 03 and exactly once under the claimed ticket's "
        "`## PM 기계 확인`"
    )
    prepare_barrier = "only after these counts are true may ROUND 04 prepare run"

    for contract in (
        disposition,
        ticket_owner,
        round_negative,
        "Do not search source or help for the block location",
        count_guard,
        prepare_barrier,
    ):
        assert contract in prompt
    assert prompt.index(disposition) < prompt.index(ticket_owner)
    assert prompt.index(ticket_owner) < prompt.index(round_negative)
    assert prompt.index(round_negative) < prompt.index(count_guard)
    assert prompt.index(count_guard) < prompt.index(prepare_barrier)
    assert prompt.index(prepare_barrier) < prompt.index("ROUND 04 developer")


def test_claude_full_wave_uses_two_phase_native_autocompact():
    """wave 완료 뒤 같은 세션에서만 낮은 native compaction 경계를 적용한다."""
    source = inspect.getsource(test_release_wave_claude_full_wave)

    side_effect = source.index('_assert_wave_side_effects(dest, proc, "claude")')
    resume = source.index('"--resume", session_id')
    low_threshold = source.index('"--autocompact", _CLAUDE_AUTOCOMPACT_THRESHOLD')
    evidence = source.index("_claude_recovery_deliveries(transcript)")

    assert '"--autocompact", _CLAUDE_WAVE_AUTOCOMPACT_THRESHOLD' in source
    assert source.count('"--resume", session_id') == 1
    assert side_effect < resume < low_threshold < evidence
    assert '"--allowedTools", "Bash"' in source[resume:evidence]
    assert '"/compact"' not in source
    assert _CLAUDE_WAVE_AUTOCOMPACT_THRESHOLD == "1m"
    assert _CLAUDE_AUTOCOMPACT_THRESHOLD == "100k"
    assert _CLAUDE_COMPACTION_PROBE_PROMPT == (
        "Use Bash exactly once to run `pwd`, then reply exactly "
        f"{_COMPACTION_RECOVERY_SENTINEL}"
    )


def test_claude_recovery_delivery_reads_durable_transcript_attachment(tmp_path):
    """marker가 후속 tool에 소비된 후에도 transcript가 실제 전달을 증명한다."""
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        "not-json\n"
        + json.dumps({
            "type": "attachment",
            "attachment": {
                "type": "hook_additional_context",
                "hookName": "PreToolUse:Bash",
                "content": ["## PM 정체성 (compaction 복구)\n- task: live"],
            },
        }, ensure_ascii=False)
        + "\n"
        + json.dumps({
            "type": "attachment",
            "attachment": {
                "type": "hook_success",
                "content": ["## PM 정체성 (compaction 복구)"],
            },
        }, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )

    assert _claude_recovery_deliveries(transcript) == 1


@pytest.mark.parametrize("harness", ["claude", "opencode"])
def test_ctx_record_override_matches_local_conf(tmp_path, harness):
    """라이브 두 도우미가 local.conf 와 함께 압축 한도 기록을 옮겨 훅·플러그인의 읽기가 통과한다(T-0943 F-002).

    설치가 쓴 기록(사용 한도 200,000)에 극소 사용 한도를 덧붙인 뒤 `read_ctx_budget` 은 그 값을 E 로 내고
    세션 모델 경고가 없다. 기록의 모델 행·C·settings 는 그대로이고 Claude 설정 파일은 바이트가 같다."""
    from test_harness_ctx_limits import install_ctx_record

    local_conf = conf_store
    session_model = install_ctx_record(tmp_path, {f"harness.{harness}.ctx_window_tokens": "200000"}, harness)
    record_path = tmp_path / local_conf.CTX_BUDGET_RELPATH
    before = json.loads(record_path.read_text(encoding="utf-8"))["harnesses"][harness]
    settings = tmp_path / ".claude" / "settings.json"
    settings_before = settings.read_bytes() if harness == "claude" else None
    if harness == "claude":
        _append_tiny_ctx_window(tmp_path)
        window = _TINY_CTX_WINDOW
    else:
        _force_opencode_compaction_threshold(tmp_path, session_model)
        window = _OPENCODE_COMPACTION_CONTEXT

    values = local_conf.load(tmp_path / ".project_manager" / "local.conf").values
    budget = local_conf.read_ctx_budget(tmp_path, values, harness, session_model)
    compact_at = int(conf_schema.ctx_input_values(values)["ctx.compact_at_pct"])
    assert (budget["E"], budget["P"], budget["warning"]) == (window, window * compact_at // 100, None)
    after = json.loads(record_path.read_text(encoding="utf-8"))["harnesses"][harness]
    assert {key: after[key] for key in ("models", "C", "settings")} == {
        key: before[key] for key in ("models", "C", "settings")}
    if harness == "claude":
        assert settings.read_bytes() == settings_before


def test_opencode_full_wave_uses_compaction_evidence_from_initial_run(
    tmp_path, monkeypatch,
):
    """full-wave 안의 checkpoint+receipt delta면 불필요한 추가 외부 turn을 생략한다."""
    calls = []

    monkeypatch.setitem(
        test_release_wave_opencode_full_wave.__globals__,
        "_guided_adopter",
        lambda _tmp_path, _harness, *, task, folders: tmp_path,
    )
    monkeypatch.setitem(
        test_release_wave_opencode_full_wave.__globals__,
        "_force_opencode_compaction_threshold",
        lambda _dest, _model: None,
    )
    monkeypatch.setitem(
        test_release_wave_opencode_full_wave.__globals__,
        "_assert_wave_side_effects",
        lambda _dest, _proc, _harness: None,
    )

    def fake_run(argv, *, cwd, env, timeout):
        calls.append(argv)
        assert len(calls) == 1, "pre-wave 증거가 있는데 compaction probe를 추가 호출함"
        marker_dir = tmp_path / ".project_manager" / ".local" / "ctx-stop"
        marker_dir.mkdir(parents=True, exist_ok=True)
        (marker_dir / "compact-checkpoint.live.boundary1").write_text(
            "durable precompact evidence\n", encoding="utf-8",
        )
        (marker_dir / "compact-snapshot-receipt.live.generation1").write_text(
            "delivered\n", encoding="utf-8",
        )
        stdout = json.dumps({"type": "step_start", "sessionID": "ses_wave"}) + "\n"
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    monkeypatch.setitem(
        test_release_wave_opencode_full_wave.__globals__,
        "_run_opencode_live",
        fake_run,
    )

    test_release_wave_opencode_full_wave(tmp_path)

    assert len(calls) == 1
    assert "--continue" not in calls[0]
    ledger = json.loads((tmp_path / ".project_manager" / ".local" / "worktree-leases.json")
                        .read_text(encoding="utf-8"))
    assert ledger["tasks"] == [{"name": _RELEASE_WAVE_TASK, "session": "ses_wave"}]
    source = inspect.getsource(test_release_wave_opencode_full_wave)
    assert source.index('_assert_wave_side_effects(dest, proc, "opencode")') < source.index(
        "_record_wave_task_session(dest, _opencode_session_id(proc.stdout))"
    ) < source.index(
        "_force_opencode_compaction_threshold(dest, LIVE_MODEL)"
    ) < source.index("_opencode_compaction_probe_prompts()")


def test_record_wave_task_session_keeps_the_bootstrapped_task(tmp_path):
    """웨이브 세션이 부트스트랩으로 연 task 가 있으면 헬퍼가 기록을 바꾸지 않아 세션 판정이 그 task 하나다(T-0953 F-001).

    빈 기록이면 헬퍼가 release-wave 행 하나를 더해 그 task 로 해소된다. 판정은 실 pm_log 의
    `resolve_session_task` 로 한다."""
    spec = importlib.util.spec_from_file_location(
        "pm_log_release_wave",
        Path(__file__).resolve().parents[1] / ".project_manager" / "tools" / "pm_log.py",
    )
    pm_log = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pm_log)

    bootstrapped = tmp_path / "bootstrapped"
    ledger = bootstrapped / ".project_manager" / ".local" / "worktree-leases.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(json.dumps({
        "leases": [],
        "tasks": [
            {"name": "main", "prefix": None, "session": "ses_wave"},
            {"name": "other", "prefix": None, "session": "ses_other"},
        ],
    }), encoding="utf-8")
    before = ledger.read_bytes()

    _record_wave_task_session(bootstrapped, "ses_wave")

    assert ledger.read_bytes() == before
    rows = json.loads(ledger.read_text(encoding="utf-8"))["tasks"]
    assert [row["name"] for row in rows if row["session"] == "ses_wave"] == ["main"]
    assert pm_engine.app.session_identity.resolve_session_task(pm_paths.home_at(bootstrapped), "ses_wave") == ("main", None)

    fresh = tmp_path / "fresh"
    _record_wave_task_session(fresh, "ses_wave")

    assert pm_engine.app.session_identity.resolve_session_task(pm_paths.home_at(fresh), "ses_wave") == (_RELEASE_WAVE_TASK, None)


def test_wave_side_effect_guard_rejects_ephemeral_run_hash(tmp_path, monkeypatch):
    """canonical 4라운드는 통과하고 delegate-copy run hash 결속은 역방향으로 거부한다.

    통합 브랜치 판독(`_wave_base_branch`·`_integration_file`)은 대역으로 바꾼다 — 이 시험이 보는
    것은 판정 규칙이고, 실제 머지 판독은 라이브 full wave 가 친다.
    """
    dest = tmp_path / "adopter-hermetic"
    base = f"{dest.name}_{_WAVE_INTEGRATION_SLOT}"
    (dest / "work" / base).mkdir(parents=True)
    merged = {
        PROBE_FILE: PROBE_TEXT + "\n",
        _WAVE_TEST_FILE: "# reads probe.txt from Path(__file__).resolve().parents[1]\n",
    }
    globals_ = _assert_wave_side_effects.__globals__
    monkeypatch.setitem(globals_, "_wave_base_branch", lambda _dest: base)
    monkeypatch.setitem(globals_, "_integration_file",
                        lambda _dest, branch, relpath: merged.get(relpath) if branch == base else None)
    done = dest / ".project_manager" / "wiki" / "tickets" / "done"
    done.mkdir(parents=True)
    (done / "T-0001-release-wave-probe.md").write_text("done\n", encoding="utf-8")
    rounds = done.parent / "rounds" / "T-0001"
    rounds.mkdir(parents=True)
    for ordinal, (role, sentinel) in enumerate(_GROWTH_PIPELINE, start=1):
        body = sentinel + "\n"
        if role == _REVIEWER_SUBAGENT:
            body += f"```{_PM_REVIEW_BLOCK}\n{{}}\n```\n"
        (rounds / f"{ordinal:02d}-{role}.md").write_text(body, encoding="utf-8")
    monkeypatch.setattr(
        subprocess, "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "1 passed\n", ""),
    )
    proc = subprocess.CompletedProcess([], 0, "", "")

    _assert_wave_side_effects(dest, proc, "hermetic")

    merged[_WAVE_TEST_FILE] += "# .local/delegate-ticket-copies/T-0001/0123456789abcdef0123456789abcdef\n"
    with pytest.raises(AssertionError, match="delegate copy"):
        _assert_wave_side_effects(dest, proc, "hermetic")
    merged[PROBE_FILE] = None
    with pytest.raises(AssertionError, match="통합 브랜치"):
        _assert_wave_side_effects(dest, proc, "hermetic")


def test_t0956_wave_adopter_fixture_creates_guided_work_folders(tmp_path):
    """[[T-0956]] AT-009 — 공용 픽스처가 안내 경로로 작업 폴더 2개를 만들고 task 가 둘 다 보유한다.

    기준 커밋 → 안내 명령 2회 → task new → alloc 2회. 자기 행 git·base 는 설치가 채웠고 공유 bare 의
    HEAD 가 해소된다(빈 bare 아님).
    """
    dest = _guided_adopter(tmp_path, "claude", task=_WAVE_TASK, folders=2)
    repo = dest.name
    slots = {f"work/{repo}_{n}" for n in (_WAVE_INTEGRATION_SLOT, _WAVE_WORK_SLOT)}
    for slot in slots:
        assert (dest / slot).is_dir(), slot
    bare_head = subprocess.run(
        ["git", "--git-dir", str(dest / ".repos" / f"{repo}.git"), "rev-parse", "--verify", "HEAD"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    assert bare_head.returncode == 0, bare_head.stderr
    ledger = json.loads((dest / ".project_manager" / ".local" / "worktree-leases.json")
                        .read_text(encoding="utf-8"))
    held = {row["slot"] for row in ledger["leases"]
            if row.get("session") == _WAVE_TASK and row.get("state") == "leased"}
    assert held == slots
    areas = (dest / ".project_manager" / "areas.md").read_text(encoding="utf-8")
    header = next(line for line in areas.splitlines() if line.startswith("| repo "))
    columns = [cell.strip() for cell in header.strip().strip("|").split("|")]
    (row,) = [dict(zip(columns, (cell.strip() for cell in line.strip().strip("|").split("|"))))
              for line in areas.splitlines() if line.startswith(f"| {repo} ")]
    assert row["git"] and row["base"], row


def test_collect_subagent_types_extracts_fixed_pipeline_from_stream_json():
    """stream-json에서 architect→developer→reviewer→developer를 순서·중복까지 보존한다."""
    # claude stream-json 근사: 각 라인 1 json. Task tool_use input 깊숙이 subagent_type 가 박힌다.
    sample_lines = [
        json.dumps({"type": "system", "subtype": "init"}),
        json.dumps({
            "type": "assistant",
            "message": {"content": [
                {"type": "tool_use", "name": "Task",
                 "input": {"subagent_type": _ARCH_SUBAGENT, "prompt": "design tests"}}
            ]},
        }),
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
        json.dumps({
            "type": "assistant",
            "message": {"content": [
                {"type": "tool_use", "name": "Task",
                 "input": {"subagent_type": _DEV_SUBAGENT, "prompt": "terminal final fix"}}
            ]},
        }),
    ]
    stdout = "\n".join(sample_lines)

    types = _collect_subagent_types(stdout)

    # 비-json·빈 줄은 조용히 무시(파싱 예외로 죽지 않음).
    assert types == [role for role, _sentinel in _GROWTH_PIPELINE]


def test_collect_subagent_types_handles_no_delegation():
    """위임 없는 stdout(subagent_type 부재)에서 walk 가 빈 리스트를 돌려준다(false-positive 0)."""
    stdout = "\n".join([
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}),
        json.dumps({"type": "result", "subtype": "success"}),
    ])
    assert _collect_subagent_types(stdout) == []


# ── multi-repo hermetic 가드 (라이브 실행 없이·@release/skipif 무관 — 매 회귀 통과 · T-0158) ──────
# multi-repo 라이브 테스트는 PM_ORCH_LIVE_RELEASE 미설정 시 skip 이라 CI 에선 안 돈다. 아래 단위는
# 라이브 없이도 돌아 (1) 셋업 헬퍼(`_import_multipm_home`)가 LLM 없이 home + 2 repo areas + 2 슬롯을
# 만드는지 (= 셋업 자체 검증·라이브 테스트의 전제) (2) multi-repo wave 프롬프트가 부트스트랩 줄 다음에
# 저장소마다 카드 줄 7개(new·cluster new·promote·tier·claim·switch·종결)를 순서대로 싣고 direct complete·
# 구 actor 플래그가 없으며 저장소별 prefix 승인이 발행 줄 한 곳에만 있는지 — 라이브 미실행 시에도 구조를
# 가드한다([[T-0958]] · 줄의 실행 가능성은 카드 사용성 파일의 비라이브 재현이 본다).


def test_import_multipm_home_sets_up_two_repos_and_slots(tmp_path):
    """`_import_multipm_home` 가 LLM 없이 multi-PM 홈 + 2 repo areas 등록 + 2 worktree 슬롯을 만든다.

    라이브 테스트의 전제(셋업)를 hermetic 하게 검증 — 셋업이 깨지면 라이브가 가짜 PASS/skip 으로
    숨지 않고 여기서 잡힌다(단일 hermetic 가드 패턴 동형). models 조회는 `_real_models_runner`
    스텁으로 차단되므로 LLM·네트워크 없이 돈다.
    """
    home = _import_multipm_home(tmp_path, "opencode")

    # (1) home 이 fresh adopter 로 import 됐다(공유 보드 + 엔진).
    assert (home / ".project_manager" / "tools" / "board.py").exists()
    assert (home / ".project_manager" / "wiki" / "tickets" / "open").is_dir()

    # (2) 2 repo 가 areas.md(per-repo 레지스트리·ADR-0014)에 prefix 로 등록됐다 — per-repo ID
    #     네임스페이스의 단일 진실(legacy 셋업에선 .project_manager/areas.md·wiki 밖·committed).
    areas_path = home / ".project_manager" / "areas.md"
    assert areas_path.exists(), "repo add 후 areas.md 부재"
    areas_text = areas_path.read_text(encoding="utf-8")
    for repo in _MULTIREPO_REPOS:
        assert f"| {repo} |" in areas_text, f"areas.md 에 repo '{repo}' 등록 행 부재"

    # (3) repo 마다 worktree 슬롯(work/<repo>_1)이 생성됐다 — per-slot 식별의 물리 자원.
    for repo in _MULTIREPO_REPOS:
        slot = home / "work" / f"{repo}_1"
        assert slot.is_dir(), f"worktree 슬롯 work/{repo}_1 미생성"


def test_import_multipm_home_claude_sets_up_two_repos_and_slots(tmp_path):
    """`_import_multipm_home` 가 claude 하니스에서도 multi-PM 홈 + 2 repo areas + 2 슬롯을 만든다.

    claude multi-repo 라이브 테스트(`test_release_wave_multirepo_claude_full_wave`)의 전제(셋업)를
    hermetic 하게 검증 — opencode 동형 짝(`test_import_multipm_home_sets_up_two_repos_and_slots`)이다.
    `_import_multipm_home` 은 harness 파라미터화돼 있어 셋업은 harness 무관해야 한다(어댑터층만 다름).
    셋업이 깨지면 라이브가 가짜 PASS/skip 으로 숨지 않고 여기서 잡힌다.
    """
    home = _import_multipm_home(tmp_path, "claude")

    # (1) home 이 fresh adopter 로 import 됐다(공유 보드 + 엔진).
    assert (home / ".project_manager" / "tools" / "board.py").exists()
    assert (home / ".project_manager" / "wiki" / "tickets" / "open").is_dir()

    # (2) 2 repo 가 areas.md(per-repo 레지스트리·ADR-0014)에 prefix 로 등록됐다.
    areas_path = home / ".project_manager" / "areas.md"
    assert areas_path.exists(), "repo add 후 areas.md 부재"
    areas_text = areas_path.read_text(encoding="utf-8")
    for repo in _MULTIREPO_REPOS:
        assert f"| {repo} |" in areas_text, f"areas.md 에 repo '{repo}' 등록 행 부재"

    # (3) repo 마다 worktree 슬롯(work/<repo>_1)이 생성됐다.
    for repo in _MULTIREPO_REPOS:
        slot = home / "work" / f"{repo}_1"
        assert slot.is_dir(), f"worktree 슬롯 work/{repo}_1 미생성"


def test_multirepo_wave_prompt_has_per_repo_mechanics():
    """multi-repo wave 프롬프트가 부트스트랩 다음에 저장소마다 카드 흐름 실행 줄을 순서대로 싣는다([[T-0958]]).

    실행 줄은 `_multirepo_wave_commands` 그대로이고 저장소 순서·줄 순서도 같다. 카드가 처방하지 않는 direct
    complete 와 구 actor 플래그는 없다(줄의 실행 가능성은 카드 사용성 파일의 비라이브 재현이 본다).
    """
    prompt = _multirepo_wave_prompt()
    cursor = prompt.index(f"python3 .project_manager/tools/pm_bootstrap.py --task {_RELEASE_WAVE_TASK}")
    for repo in _MULTIREPO_REPOS:
        for command in _multirepo_wave_commands(repo):
            cursor = prompt.index(command, cursor)
    assert "board.py complete" not in prompt
    assert "--session" not in prompt and "--worktree-slot" not in prompt


# ── multi-USER hermetic 가드 (라이브 실행 없이·@release/skipif 무관 — 매 회귀 통과 · T-0309) ─────
# multi-USER 라이브 테스트는 PM_ORCH_LIVE_RELEASE 미설정 시 skip 이라 CI 에선 안 돈다. 아래 단위는
# 라이브 없이도 돌아 (1) 셋업 헬퍼(`_import_multiuser_home`)가 2 repo 를 distinct area_owner
# (alpha→alice·beta→bob)로 등록하는지 (= 섞임 격리의 전제·라이브가 가짜 PASS 로 숨지 않게)
# (2) wave 프롬프트가 identity 귀속 mechanics 를 담는지 (3) 뷰 파서가 (status,id)를 정확히 뽑는지 —
# 라이브 미실행 시에도 구조·셋업을 가드한다(회귀가 잡음).


def test_import_multiuser_home_sets_up_two_distinct_area_owners(tmp_path):
    """`_import_multiuser_home` 가 LLM 없이 2 repo 를 distinct area_owner(alpha→alice·beta→bob)로 등록한다.

    라이브 multiuser composite 테스트의 전제(셋업)를 hermetic 하게 검증 — area_owner 가 distinct 2
    user 로 안 서면 세션 뷰가 solo degrade 로 돌아 섞임 격리가 무의미해지고 라이브가 가짜 PASS 로
    숨는다(여기서 잡힌다). areas.md 의 repo→area_owner 매핑(alpha→alice·beta→bob·open 소유
    `_ticket_owner`)을 못박는다. models 조회는 `_real_models_runner` 스텁 차단.
    """
    home = _import_multiuser_home(tmp_path, "opencode")

    # (1) multi-PM 홈 + 2 슬롯(work/alpha_1·work/beta_1) — per-slot 식별의 물리 자원.
    assert (home / ".project_manager" / "areas.md").exists(), "repo add 후 areas.md 부재"
    for repo in _MULTIUSER_REPOS:
        assert (home / "work" / f"{repo}_1").is_dir(), f"worktree 슬롯 work/{repo}_1 미생성"

    # (2) areas.md 가 repo→area_owner 를 distinct 2 user 로 매핑(alpha→alice·beta→bob·행 스코프 단언).
    areas_lines = (home / ".project_manager" / "areas.md").read_text(encoding="utf-8").splitlines()
    for repo, owner in _MULTIUSER_AREA_OWNERS.items():
        row = next((l for l in areas_lines if f"| {repo} |" in l), None)
        assert row is not None, f"areas.md 에 repo '{repo}' 행 부재"
        assert f"| {owner} |" in row, (
            f"areas.md repo '{repo}' 행의 area_owner 가 '{owner}' 아님 — distinct 2 user 미설정.\n{row}")

    # (3) 두 area_owner 가 서로 다름 = multi_user 신호의 전제(≥2 distinct → strict-exclude).
    assert len(set(_MULTIUSER_AREA_OWNERS.values())) == 2


def test_multiuser_wave_prompt_has_per_identity_mechanics():
    """multiuser wave 프롬프트가 귀속 mechanics와 사용자-명시 prefix ack를 담는다(T-0962 AT-012).

    라이브 미실행 시에도 프롬프트 구조를 가드 — identity 마다 new 두 줄과 claim 한 줄이 `--task <task>`
    (귀속 정체성)를 싣고 어느 줄에도 `--slot` 이 없으며, `--user-ack` 는 첫 new 줄에만 있다. `--user`
    (귀속 user·multi_user 신호)·`--prefix`(ID 네임스페이스)·미claim open(유출 대상)이 빠져도 잡힌다.
    """
    prompt = _multiuser_wave_prompt()
    lines = prompt.splitlines()
    for _repo, prefix, task, user in _MULTIUSER_IDENTITIES:
        assert user in prompt, f"프롬프트에 user '{user}' 미언급"
        assert f"--prefix {prefix}" in prompt, f"프롬프트에 --prefix {prefix} 누락"
        assert f"--user {user}" in prompt, f"프롬프트에 --user {user} 누락(귀속 user 미스탬프)"
        news = [line for line in lines if "board.py new" in line and f"--user {user}" in line]
        claims = [line for line in lines if "board.py claim" in line and f"--user {user}" in line]
        assert len(news) == 2 and len(claims) == 1, (news, claims)
        for line in (*news, *claims):
            assert f"--task {task}" in line, f"귀속 줄에 --task {task} 누락: {line!r}"
        assert f"--user-ack {prefix}" in news[0] and "--user-ack" not in news[1] + claims[0], (
            f"프롬프트의 신규 prefix {prefix} 승인은 첫 new 줄에만 정확히 1회여야 함"
        )
        assert prompt.count(f"--user-ack {prefix}") == 1
    assert "--slot" not in prompt, "프롬프트에 --slot 잔존(슬롯 세션 정체성)"
    assert "board.py new" in prompt and "board.py claim" in prompt
    # 미claim open(유출 대상) + claim 둘 다 지시 — 섞임 격리의 catch 대상 open 필요.
    assert "open probe" in prompt and "do NOT claim" in prompt
    # negative backstop — 구 actor 플래그 재유입 시 라이브 없이 red(T-0324 재발 방지).
    assert "--session" not in prompt and "--worktree-slot" not in prompt


def test_multirepo_wave_prompt_has_user_ack_per_prefix():
    """multirepo wave 프롬프트가 신규 prefix 의 값-결속 사용자 ack 를 저장소마다 `new` 줄 한 곳에만 담는다 (T-0744).

    `--prefix <repo>` 는 신규 prefix 사용자-승인 게이트(`require_prefix_user_ack`)를 지나므로 프롬프트가
    `--user-ack <repo>` 를 주지 않으면 라이브 판정이 모델 행동에 좌우된다(livegate d0b5890 실측). 승인
    주체 문장이 있고, ack 는 발행 줄 밖에 다시 붙지 않는다.
    """
    prompt = _multirepo_wave_prompt()
    for repo in _MULTIREPO_REPOS:
        assert prompt.count(f"--prefix {repo} --user-ack {repo}") == 1, repo
        for line in prompt.splitlines():
            if f"--user-ack {repo}" in line:
                assert "board.py new" in line, f"ack 는 new 줄에만 — 재부착 금지: {line!r}"
    assert "explicitly approves" in prompt
    assert "--session" not in prompt and "--worktree-slot" not in prompt


def test_parse_list_rows_extracts_status_and_id():
    """`_parse_list_rows` 가 board.py list 출력에서 (status, id)를 정확히 파싱한다(비-행 무시)."""
    # cmd_list 출력 근사(`  [{status:7s}] {id}  {title}  {claimed}  {tags}`·board.py:cmd_list).
    sample = (
        "open tickets:\n"
        "  [open   ] T-al-001  open probe alice           alice/alpha_1     \n"
        "  [claimed] T-be-002  wip probe bob               bob/beta_1        \n"
        "(no tickets)\n"
        "random noise line without bracket\n"
    )

    rows = _parse_list_rows(sample)

    assert ("open", "T-al-001") in rows
    assert ("claimed", "T-be-002") in rows
    # 비-행(`(no tickets)`·헤더·노이즈)은 조용히 무시(파싱 예외 0).
    assert rows == [("open", "T-al-001"), ("claimed", "T-be-002")]


# ── marker-수집 가드 (기계·항상 실행·@release/skipif 무관 — 매 회귀 통과 · T-0190) ────────────
# 릴리즈 게이트는 `pytest -m release` 로 라이브 서브셋을 선택한다. 마커가 소실(데코레이터 삭제)·
# 개명(다른 이름)되면 그 테스트는 selection 에서 조용히 빠지고, 게이트는 "0개 수집·exit5" 를
# false-green 으로 삼킨다 — pytest.ini strict-marker 는 *오타* 만 잡지 *소실* 은 못 잡는다. 그래서
# 마커 달린 테스트 함수 수를 pin 해, 마커가 사라지거나 이름이 바뀌면 이 기계 가드가 즉시 red 로
# 잡는다(T-0159 보완). 기대값은 테스트가 늘 때 의도적으로 함께 갱신한다.
# release tier 는 ADR-0039 D1 로 라이브 tier 를 하나로 통합한 것이라, 마커가 여러 파일(이 파일·
# test_fresh_adopter_runtime_smoke·test_command_card_usability·test_pm_worktree_live)에 걸쳐 있다 —
# AST 수집은 `_RELEASE_TEST_FILES` 의 모든 파일을 스캔한다.

_RELEASE_TEST_FILES = (
    Path(__file__),
    Path(__file__).parent / "test_fresh_adopter_runtime_smoke.py",
    Path(__file__).parent / "test_command_card_usability.py",
    Path(__file__).parent / "test_pm_worktree_live.py",
    Path(__file__).parent / "test_pm_release_live.py",
    Path(__file__).parent / "test_task_cycle_e2e.py",
    Path(__file__).parent / "test_engine_rev_release.py",
    Path(__file__).parent / "test_pm_relay_codex.py",
    Path(__file__).parent / "test_pm_delegate_live.py",
)
# 마커 소실/개명을 잡는 안전망 — 라이브 테스트를 의도적으로 추가할 때만 함께 올린다.
# 7(이 파일: full/multirepo × claude/opencode + Codex native ticket growth + final-nudge +
#     multiuser-composite opencode·T-0309)
# + 2(runtime_smoke: pm_update opencode/claude)
# + 2(command_card_usability: claude/opencode 카드 사용성·ADR-0046·T-0255)
# + 2(pm_worktree_live: claude/opencode 스킬 라이브 하네스·ADR-0050·T-0278)
# + 2(pm_release_live: claude hard/opencode best-effort 릴리즈 스킬 라이브·ADR-0049·T-0349)
# + 1(test_task_cycle_e2e: task 사이클 완주 기계 e2e·ADR-0068 사이클 게이트·T-0400 — 라이브 LLM
#     불요·순수 기계지만 release 티어에 편입해 릴리즈마다 사이클 완주를 강제).
# + 1(test_engine_rev_release: engine_rev.ENGINE_REV ↔ CHANGELOG 최신 릴리스 버전 정합·T-0397 —
#     기계·릴리즈마다 rev bump 를 강제·bump 누락을 릴리즈 게이트에서 red).
# + 1(test_pm_relay_codex: codex relay 라이브 smoke·spawn→resume·tid==marker·usage 누적·ADR-0070 D7·
#     T-0407 — codex 라이브 green 을 릴리즈 pin 에 편입·codex green 없이 v1.4.0 push 차단).
# + 4(test_pm_delegate_live: 기존 Codex smoke 1 + 선택 cross Claude→Codex·Claude→OpenCode·
#     Codex→Claude ticket growth 3·T-0685).
# ⚠ 커플드-pin: 이 값을 올리면 touches 밖의 전역 pin 도 함께 정합돼야 `livegate record`(수집
#   N==pin)가 통과한다(orchestrator 가 갱신·test_command_card_usability.py 주석 참조):
#   regression.LIVEGATE_RELEASE_PIN · tests/test_board_livegate.py(하드코딩 fake/assert) ·
#   tests/test_worktree_pool.py(_LIVEGATE_RELEASE_PIN 미러) · templates/*/pm_engine/app/regression.py(pm_update 전파).
_EXPECTED_RELEASE_TESTS = 22


def _pytest_marker_name(decorator) -> str | None:
    """데코레이터 AST 노드 → `pytest.mark.<name>` 의 <name> (그 형태가 아니면 None).

    `@pytest.mark.release`(bare Attribute)·`@pytest.mark.skipif(...)`(Call)·
    `@pytest.mark.parametrize(...)` 모두 처리 — Call 이면 `.func` 를 본다.
    """
    node = decorator.func if isinstance(decorator, ast.Call) else decorator
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "mark"
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "pytest"
    ):
        return node.attr
    return None


def _count_marked_tests(path: Path, marker: str) -> int:
    """`path` 의 모듈-레벨 테스트 함수 중 `@pytest.mark.<marker>` 가 달린 개수 (AST 파싱)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return sum(
        1
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(_pytest_marker_name(d) == marker for d in node.decorator_list)
    )


def test_release_pin_matches_board_livegate_pin():
    """pin 단일진실 교차 단언 (T-0221/T-0222 접점·PM 배선) — release 케이스 수가 바뀌면
    regression.LIVEGATE_RELEASE_PIN(livegate record 의 수집 게이트)도 함께 바뀌어야 한다.
    한쪽만 갱신하면 여기서 red — livegate 가 구 pin 으로 신규/삭제 케이스를 위장 통과시키는
    드리프트를 차단한다."""
    assert regression.LIVEGATE_RELEASE_PIN == _EXPECTED_RELEASE_TESTS


def test_release_marker_count_is_pinned():
    """`release` 마커 테스트 수(`_RELEASE_TEST_FILES` 합)가 고정값과 일치 — 마커 소실/개명 시 게이트 false-green 방어.

    근거(2026-07-02 실측): 릴리즈 게이트가 wrong-cwd + 잔재 tests/ 로 0개 수집·exit5 를 조용히
    내는 false-green 이 실제 발생. `-m release` selection 에서 마커가 빠진 테스트는 조용히 안 돌고,
    그 부재를 게이트가 못 본다. 이 수집-수 pin 이 마커 소실/개명 클래스를 red 로 세운다(T-0159 보완).
    ADR-0039 로 라이브 tier 가 하나(release)라, `_RELEASE_TEST_FILES` 파일들의 마커를 합산해 pin 한다.
    """
    actual = sum(_count_marked_tests(f, "release") for f in _RELEASE_TEST_FILES)
    assert actual == _EXPECTED_RELEASE_TESTS, (
        f"`release` 마커 테스트 수 {actual} != 기대 {_EXPECTED_RELEASE_TESTS} — 마커 소실/개명 "
        f"의심(게이트 selection 에서 조용히 누락될 위험). 라이브 테스트를 의도적으로 늘렸다면 "
        f"_EXPECTED_RELEASE_TESTS 를 함께 갱신하라."
    )
