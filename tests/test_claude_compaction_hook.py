"""출하 Claude ctx 훅의 압축 기록 smoke (T-0621 · T-0953).

압축 기록은 압축 이벤트(PreCompact·PostCompact)가 아니라 주 스레드 훅(PreToolUse·UserPromptSubmit,
`agent_id` 없음)이 한다. 그 훅이 주 대화 기록 끝부분의 마지막 압축 경계 uuid 를 보고 경계마다 한 번 PM 홈
log/current.md 에 breadcrumb 와 checkpoint 골격을 남기고 엔진 snapshot 을 주입한다. 기록 task 는
훅 입력 `session_id` 와 같은 `session` 을 가진 task 기록이다. 계약:
  ① 열린 task 가 여럿인 PM 홈 루트 세션도 그 세션 task 로 기록·주입한다(경계당 1회).
  ② 서브에이전트 압축(압축 이벤트·`agent_id` 입력)은 주 세션 상태를 바꾸지 않는다.
  ③ 판정·기록 실패는 사유 한 줄을 한 번 보인다(조용한 생략 없음).
  ④ 항상 exit 0 (도구·prompt 무차단), 자기 트리에만 기록.

전부 hermetic — 실 래퍼(`ctx_stop_hook.sh`)와 엔진 사본을 tmp 트리에 복사해 subprocess 로 실행한다.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CLAUDE = REPO / "templates" / "claude_code" / ".claude"
HOOK = CLAUDE / "ctx_stop_hook.sh"
TOOLS = REPO / ".project_manager" / "tools"

# breadcrumb 이 남기는 마커 문구(네이티브 압축 발생 신호).
BREADCRUMB_MARKER = "auto-compact 발생 — 압축은 자동이고 세션은 그대로 이어진다"
CHECKPOINT_HEADER = "checkpoint | (task:main) — compaction"
FAILURE_PREFIX = "[ctx-checkpoint] 압축 기록 실패:"

# PM 홈 실측(설계 3.5절) 형상의 task 기록 — 열린 task 3개, 세션 ID 는 각자 다르다.
_THREE_TASKS = (("main", "s-main"), ("hermes", "s-hermes"), ("guard-check", "ses_guard"))

# 래퍼가 sh 없으면 돌릴 수 없다 — 그런 환경(드묾)은 skip(hermetic·crash 금지).
_SH = shutil.which("sh")
pytestmark = pytest.mark.skipif(_SH is None, reason="sh 미설치 — POSIX 훅 smoke skip")


def _make_repo(root: Path, *, with_log: bool, tasks=_THREE_TASKS) -> Path:
    """tmp 에 실 래퍼·훅·엔진 사본과 task 기록을 가진 최소 트리를 만든다 — 래퍼 경로를 돌려준다.

    훅은 자기 위치(.claude/)에서 repo_root 를 자기해소하므로 이 tmp 루트가 곧 repo_root(실 repo 무오염).
    """
    claude_dir = root / ".claude"
    claude_dir.mkdir(parents=True)
    for name in ("ctx_stop_hook.sh", "ctx_stop_hook.py", "ctx_guard.py"):
        shutil.copy2(CLAUDE / name, claude_dir / name)
    (claude_dir / "ctx_stop_hook.sh").chmod(0o755)
    shutil.copytree(TOOLS, root / ".project_manager" / "tools")
    (root / ".project_manager" / "local.conf").write_text("# test root anchor\n", encoding="utf-8")
    local = root / ".project_manager" / ".local"
    for name, _session in tasks:
        state = local / "tasks" / name / "pm_state.md"
        state.parent.mkdir(parents=True)
        state.write_text(f"# {name}\n", encoding="utf-8")
    (local / "worktree-leases.json").write_text(json.dumps({
        "leases": [{"slot": "work/product_1", "state": "leased", "session": "main"}],
        "tasks": [{"name": name, "prefix": None, "session": session, "started": "t"}
                  for name, session in tasks],
    }), encoding="utf-8")
    if with_log:
        log_dir = root / ".project_manager" / "wiki" / "log"
        log_dir.mkdir(parents=True)
        (log_dir / "current.md").write_text(
            "## [2026-01-01] handoff | 기존 entry\n", encoding="utf-8"
        )
    from test_harness_ctx_limits import install_ctx_record
    install_ctx_record(root)
    return claude_dir / "ctx_stop_hook.sh"


def _declare_slot_git_pointer(pm_home: Path, worktree: Path, repo: str = "product") -> None:
    """슬롯이 소유 PM 홈을 선언하는 `.git` 포인터를 세운다(`worktree_pool` 실 형상)."""
    git_dir = pm_home / ".repos" / f"{repo}.git" / "worktrees" / worktree.name
    git_dir.mkdir(parents=True, exist_ok=True)
    (worktree / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
    (git_dir / "commondir").write_text("../..\n", encoding="utf-8")


_BOUNDARY_UUID = "00000000-0000-4000-8000-0000000000a1"


def _compact_boundary() -> dict:
    """주 대화 기록의 압축 경계 항목(설계 3.3절 실측 형상)."""
    return {
        "type": "system", "subtype": "compact_boundary", "isSidechain": False,
        "uuid": _BOUNDARY_UUID,
        "compactMetadata": {"trigger": "auto", "preTokens": 429_814, "postTokens": 11_387},
    }


def _usage(tokens: int, *, sidechain: bool = False) -> dict:
    return {
        "type": "assistant", "isSidechain": sidechain,
        "message": {"role": "assistant", "usage": {"input_tokens": tokens}},
    }


def _write_jsonl(path: Path, entries: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")
    return path


def _prompt(session_id: str, transcript: Path, cwd: Path) -> dict:
    """UserPromptSubmit 훅 입력(Claude Code 2.1.290·2.1.293 실측 필드)."""
    return {
        "session_id": session_id, "transcript_path": str(transcript), "cwd": str(cwd),
        "permission_mode": "default", "hook_event_name": "UserPromptSubmit", "prompt": "계속",
    }


def _run_hook(hook_path: Path, payload: dict | None = None, *, cwd: Path | None = None):
    return subprocess.run(
        [_SH, str(hook_path)], input=json.dumps(payload or {}),
        capture_output=True, text=True, encoding="utf-8", timeout=60,
        cwd=str(cwd) if cwd else None,
    )


def _log(root: Path) -> Path:
    return root / ".project_manager" / "wiki" / "log" / "current.md"


def _ctx_stop(root: Path) -> Path:
    return root / ".project_manager" / ".local" / "ctx-stop"


# ── ① 열린 task 3개 PM 홈 루트 세션 ───────────────────────────────────────────

def test_t0953_pm_home_root_session_with_three_tasks_records_and_injects(tmp_path):
    """PM 홈 루트·열린 task 3개에서 압축 뒤 첫 주 스레드 훅이 그 세션 task 로 기록·주입한다(AT-011)."""
    hook = _make_repo(tmp_path, with_log=True)
    transcript = _write_jsonl(tmp_path / "s-main.jsonl", [
        _usage(150_000), _compact_boundary(), _usage(1_000),
    ])

    first = _run_hook(hook, _prompt("s-main", transcript, tmp_path), cwd=tmp_path)

    assert first.returncode == 0, first.stderr
    context = json.loads(first.stdout)["hookSpecificOutput"]["additionalContext"]
    assert context.startswith("## PM 정체성 (compaction 복구)") and "- task: main" in context
    appended = _log(tmp_path).read_text(encoding="utf-8").split("기존 entry", 1)[1]
    assert appended.count("\n## ") == 1 and appended.count(CHECKPOINT_HEADER) == 1
    assert appended.count(BREADCRUMB_MARKER) == 1
    assert (_ctx_stop(tmp_path) / f"s-main.compact-{_BOUNDARY_UUID}").is_file()

    log_before = _log(tmp_path).read_bytes()
    second = _run_hook(hook, _prompt("s-main", transcript, tmp_path), cwd=tmp_path)
    assert second.returncode == 0 and "PM 정체성" not in second.stdout
    assert _log(tmp_path).read_bytes() == log_before


# ── ② 서브에이전트 압축 ───────────────────────────────────────────────────────

def test_t0953_subagent_compaction_leaves_main_state_untouched(tmp_path):
    """서브에이전트 압축의 압축 이벤트·`agent_id` 도구 호출은 주 세션 상태를 바꾸지 않는다(AT-012).

    2026-10-10 06:20Z 형상: 압축 이벤트 입력은 주 세션 ID·주 대화 기록으로 오고, 경계는
    서브에이전트 기록에만 남는다. 주 대화 기록은 최종 밴드(이미 `.final` 소비)라 마지막 주 스레드
    훅도 ok 재무장 없이 아무것도 바꾸지 않는다."""
    hook = _make_repo(tmp_path, with_log=True)
    slot = tmp_path / "work" / "product_1"
    slot.mkdir(parents=True)
    main_transcript = _write_jsonl(tmp_path / "s-main.jsonl", [_usage(190_000)])
    _write_jsonl(tmp_path / "s-main" / "subagents" / "agent-x.jsonl", [
        _usage(150_000, sidechain=True),
        {**_compact_boundary(), "isSidechain": True, "agentId": "x"},
        _usage(1_000, sidechain=True),
    ])
    marker_dir = _ctx_stop(tmp_path)
    marker_dir.mkdir(parents=True)
    for suffix in ("nudge", "final"):
        (marker_dir / f"s-main.{suffix}").write_text("fired\n", encoding="utf-8")
    log_before = _log(tmp_path).read_bytes()
    base = {"session_id": "s-main", "transcript_path": str(main_transcript), "cwd": str(slot),
            "permission_mode": "default"}
    payloads = [
        {**base, "hook_event_name": "PreCompact", "trigger": "auto", "custom_instructions": ""},
        {**base, "hook_event_name": "PostCompact", "trigger": "auto", "compact_summary": "요약"},
        {**base, "hook_event_name": "PreToolUse", "agent_id": "x", "agent_type": "Explore",
         "tool_name": "Bash", "tool_input": {"command": "ls"}, "tool_use_id": "toolu_x"},
        {**base, "hook_event_name": "PreToolUse",
         "tool_name": "Bash", "tool_input": {"command": "ls"}, "tool_use_id": "toolu_main"},
    ]

    for payload in payloads:
        result = _run_hook(hook, payload, cwd=slot)
        assert result.returncode == 0 and result.stdout == "", (payload["hook_event_name"], result)

    assert _log(tmp_path).read_bytes() == log_before
    assert list(marker_dir.glob("compact-checkpoint.*")) == []
    assert list(marker_dir.glob("*.compact-*")) == []
    assert sorted(path.name for path in marker_dir.iterdir()) == ["s-main.final", "s-main.nudge"]


def test_t0953_subagent_tool_call_does_not_take_main_boundary(tmp_path):
    """주 세션 압축 직후 서브에이전트 도구 호출이 먼저 와도 경계 처리권·log·snapshot 을 가져가지 않는다(E1-6).

    주 대화 기록에 새 경계가 있는 상태에서 `agent_id` 입력이 먼저 오고, 이어 오는 주 스레드
    도구 호출이 그 경계를 한 번 처리한다."""
    hook = _make_repo(tmp_path, with_log=True)
    transcript = _write_jsonl(tmp_path / "s-main.jsonl", [
        _usage(150_000), _compact_boundary(), _usage(1_000),
    ])
    marker_dir = _ctx_stop(tmp_path)
    log_before = _log(tmp_path).read_bytes()
    base = {"session_id": "s-main", "transcript_path": str(transcript), "cwd": str(tmp_path),
            "permission_mode": "default", "hook_event_name": "PreToolUse",
            "tool_name": "Bash", "tool_input": {"command": "ls"}}

    subagent = _run_hook(hook, {**base, "agent_id": "x", "agent_type": "Explore",
                                "tool_use_id": "toolu_x"}, cwd=tmp_path)

    assert subagent.returncode == 0 and subagent.stdout == "", subagent
    assert _log(tmp_path).read_bytes() == log_before
    assert not (marker_dir / f"s-main.compact-{_BOUNDARY_UUID}").exists()
    assert list(marker_dir.glob("compact-checkpoint.*")) == []

    main = _run_hook(hook, {**base, "tool_use_id": "toolu_main"}, cwd=tmp_path)

    assert main.returncode == 0, main.stderr
    context = json.loads(main.stdout)["hookSpecificOutput"]["additionalContext"]
    assert context.startswith("## PM 정체성 (compaction 복구)") and "- task: main" in context
    assert (marker_dir / f"s-main.compact-{_BOUNDARY_UUID}").is_file()
    appended = _log(tmp_path).read_text(encoding="utf-8").split("기존 entry", 1)[1]
    assert appended.count(CHECKPOINT_HEADER) == 1


# ── ③ 판정·기록 실패 신호 ─────────────────────────────────────────────────────

def test_t0953_unbootstrapped_session_shows_signal_once(tmp_path):
    """부트스트랩하지 않은 세션은 사유 한 줄을 한 번 보이고 log 를 바꾸지 않는다(AT-013)."""
    hook = _make_repo(tmp_path, with_log=True)
    transcript = _write_jsonl(tmp_path / "s-unbound.jsonl", [_compact_boundary(), _usage(1_000)])
    log_before = _log(tmp_path).read_bytes()

    first = _run_hook(hook, _prompt("s-unbound", transcript, tmp_path), cwd=tmp_path)

    assert first.returncode == 0, first.stderr
    output = json.loads(first.stdout)
    assert output["systemMessage"].startswith(FAILURE_PREFIX)
    assert "이 세션이 연 task 없음" in output["systemMessage"]
    assert output["systemMessage"] in output["hookSpecificOutput"]["additionalContext"]
    assert _log(tmp_path).read_bytes() == log_before

    second = _run_hook(hook, _prompt("s-unbound", transcript, tmp_path), cwd=tmp_path)
    assert second.returncode == 0 and "압축 기록 실패" not in second.stdout


def test_graceful_skip_when_log_absent(tmp_path):
    """log/current.md 가 없는 트리에서 훅은 exit 0·파일 미생성이고 실패 사유를 한 번 보인다."""
    hook = _make_repo(tmp_path, with_log=False)
    transcript = _write_jsonl(tmp_path / "s-main.jsonl", [_compact_boundary(), _usage(1_000)])

    first = _run_hook(hook, _prompt("s-main", transcript, tmp_path), cwd=tmp_path)
    second = _run_hook(hook, _prompt("s-main", transcript, tmp_path), cwd=tmp_path)

    assert first.returncode == second.returncode == 0, first.stderr
    assert not _log(tmp_path).exists()
    message = json.loads(first.stdout)["systemMessage"]
    assert message.startswith(FAILURE_PREFIX) and "current.md 없음" in message
    assert "압축 기록 실패" not in second.stdout


# ── ④ 자기 트리 기록·항상 exit 0 ─────────────────────────────────────────────

def test_hook_never_writes_to_an_enclosing_pm_home(tmp_path):
    """감싸는 PM 홈이 있어도 훅은 자기 트리에만 기록한다 — 위치는 판정 입력이 아니다.

    2026-08-31 실측에서 실 PM 홈 `wiki/log/current.md` 에 108줄이 들어간 경로와 같은 형상이다:
    등록 슬롯 안쪽의 마커 없는 트리에서 훅을 돌리면, 조상 훑기가 살아 있을 때 바깥 PM 홈이
    소유자로 뽑혀 그쪽 log 에 append 된다.
    """
    pm_home = tmp_path / "pm-home"
    slot = pm_home / "work" / "product_1"
    slot.mkdir(parents=True)
    _declare_slot_git_pointer(pm_home, slot)
    shutil.copytree(TOOLS, pm_home / ".project_manager" / "tools")
    ledger = pm_home / ".project_manager" / ".local" / "worktree-leases.json"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(json.dumps({
        "leases": [{"slot": "work/product_1", "state": "leased", "session": "main"}],
        "tasks": [{"name": "main", "session": "s-main"}],
    }), encoding="utf-8")
    home_log = _log(pm_home)
    home_log.parent.mkdir(parents=True)
    home_log.write_text("## [2026-01-01] handoff | PM 홈 기존 entry\n", encoding="utf-8")
    home_log_before = home_log.read_bytes()

    inner = slot / ".project_manager" / ".local" / "tmp" / "anchor"
    hook = _make_repo(inner, with_log=True)
    transcript = _write_jsonl(inner / "s-main.jsonl", [_compact_boundary(), _usage(1_000)])

    result = _run_hook(hook, _prompt("s-main", transcript, inner), cwd=inner)

    assert result.returncode == 0, result.stderr
    inner_text = _log(inner).read_text(encoding="utf-8")
    assert BREADCRUMB_MARKER in inner_text and CHECKPOINT_HEADER in inner_text
    assert home_log.read_bytes() == home_log_before


def test_exit_zero_always(tmp_path):
    """log 존재/부재 무관 훅은 exit 0 — prompt·도구를 절대 막지 않는다."""
    for index, with_log in enumerate((True, False)):
        root = tmp_path / f"r{index}"
        hook = _make_repo(root, with_log=with_log)
        transcript = _write_jsonl(root / "s-main.jsonl", [_compact_boundary(), _usage(1_000)])
        result = _run_hook(hook, _prompt("s-main", transcript, root), cwd=root)
        assert result.returncode == 0, (
            f"with_log={with_log} 시 fail-soft 위반 (exit {result.returncode}): {result.stderr}"
        )
