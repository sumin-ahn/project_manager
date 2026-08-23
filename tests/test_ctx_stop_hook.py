"""claude `ctx_stop_hook.py` — 판단 원칙 recall 채널 합본 테스트.

ctx 밴드(넛지/최종 안내)는 `tests/test_claude_ctx_guard.py` 가 이미 전량 덮는다. 이 파일은 이
티켓이 새로 얹은 축 하나만 본다 — recall 문안이 ctx 밴드 안내와 **동시에** additionalContext 에
실리는가(문자열 누적)와, 레지스트리 부재가 기존 ctx 밴드 출력을 바꾸지 않는가(비차단 계약).
"""
from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CLAUDE = REPO / "templates" / "claude_code" / ".claude"
TOOLS = REPO / ".project_manager" / "tools"
PM_PRINCIPLES_PY = TOOLS / "pm_principles.py"
# 로더가 실행 중 지연 로드하는 형제 seam(중앙 로더·공용 읽기·기계 출력) — 채택자 트리엔 항상
# 함께 있으므로 사본 fixture 도 같은 집합을 깔아야 실제 훅 경로와 같아진다.
ENGINE_SIBLING_PY = ("repo_owned_files.py", "file_lock.py", "console_encoding.py")


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"claude_adapter_{name}", CLAUDE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_transcript(tmp_path: Path, input_tokens: int) -> Path:
    path = tmp_path / "transcript.jsonl"
    entry = {"type": "assistant", "message": {"role": "assistant", "usage": {"input_tokens": input_tokens}}}
    path.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    return path


def _write_registry(root: Path, text: str, *, with_loader: bool = True) -> None:
    """레지스트리 wiki 파일 + (기본) 로더 사본을 tmp root 에 배치한다.

    `_load_principles(root)` 는 `<root>/.project_manager/tools/pm_principles.py` 를 경로 로드하므로,
    실제 훅 경로를 태우려면 tmp root 에도 로더 사본이 있어야 한다(with_loader=False 는 로더 부재
    시나리오 전용)."""
    wiki = root / ".project_manager" / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    (wiki / "pm_principles.md").write_text(text, encoding="utf-8")
    if with_loader:
        tools = root / ".project_manager" / "tools"
        tools.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(PM_PRINCIPLES_PY, tools / "pm_principles.py")
        for name in ENGINE_SIBLING_PY:
            shutil.copyfile(TOOLS / name, tools / name)


def test_recall_and_ctx_band_additional_context_coexist(tmp_path):
    """밴드(stop) 안내 + recall 문안이 같은 additionalContext 에 둘 다 실린다(합본)."""
    stop_hook = _load("ctx_stop_hook")
    _write_registry(tmp_path, (
        "#### 번들\n"
        "- `[shell: git\\s+push\\s+--force]` 강제 push 는 경고 신호다. 어기면 원격 히스토리가 덮인다.\n"
    ))
    stdin = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "git push --force origin main"},
        # input_tokens 190_000 / window 200_000 = 95% used → stop 밴드.
        "transcript_path": str(_write_transcript(tmp_path, 190_000)),
        "session_id": "sess-recall-merge",
    }
    rc, output = stop_hook.evaluate(stdin, tmp_path, {})
    assert rc == 0
    hso = output["hookSpecificOutput"]
    guidance = hso["additionalContext"]
    assert "ctx-nudge/최종" in guidance
    assert "[principle-recall]" in guidance
    assert "강제 push" in guidance
    # 두 문안이 줄바꿈으로 이어붙었다(누적 — 어느 한쪽만 남지 않는다).
    assert guidance.index("ctx-nudge/최종") < guidance.index("[principle-recall]")


def test_recall_alone_when_ctx_band_is_ok(tmp_path):
    """ctx 밴드가 'ok'(출력 없음)여도 recall 매칭만으로 additionalContext 가 생긴다."""
    stop_hook = _load("ctx_stop_hook")
    _write_registry(tmp_path, (
        "#### 번들\n"
        "- `[edit: (^|/)tests?/]` 테스트 파일 편집이다. 어기면 회귀가 비활성화될 수 있다.\n"
    ))
    stdin = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Edit",
        "tool_input": {"file_path": "tests/test_something.py"},
        "session_id": "sess-recall-only",
    }
    rc, output = stop_hook.evaluate(stdin, tmp_path, {})
    assert rc == 0
    assert output is not None
    assert "[principle-recall]" in output["hookSpecificOutput"]["additionalContext"]


def test_missing_registry_leaves_ctx_band_output_unchanged(tmp_path):
    """레지스트리 파일 부재에서도 훅은 도구 실행을 막지 않고 기존 ctx 밴드 출력이 그대로 실린다."""
    stop_hook = _load("ctx_stop_hook")
    assert not (tmp_path / ".project_manager" / "wiki" / "pm_principles.md").exists()
    stdin = {
        "hook_event_name": "UserPromptSubmit",
        "prompt": "계속 진행해줘",
        "transcript_path": str(_write_transcript(tmp_path, 190_000)),
        "session_id": "sess-no-registry",
    }
    rc, output = stop_hook.evaluate(stdin, tmp_path, {})
    assert rc == 0
    guidance = output["hookSpecificOutput"]["additionalContext"]
    assert "ctx-nudge/최종" in guidance
    assert "[principle-recall]" not in guidance


def test_no_match_and_no_band_returns_none(tmp_path):
    """recall 도 ctx 밴드도 발화하지 않으면 기존 계약대로 rc0·output None."""
    stop_hook = _load("ctx_stop_hook")
    _write_registry(tmp_path, (
        "#### 번들\n"
        "- `[shell: never-matches-anything-xyz]` 문구. 어기면 깨진다.\n"
    ))
    stdin = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "ls -la"},
        "transcript_path": str(_write_transcript(tmp_path, 1_000)),
        "session_id": "sess-quiet",
    }
    rc, output = stop_hook.evaluate(stdin, tmp_path, {})
    assert rc == 0
    assert output is None


def test_postcompact_rearm_does_not_raise_without_registry(tmp_path):
    """레지스트리·로더 부재에서도 PostCompact 재무장 경로가 예외 없이 통과한다(비차단)."""
    stop_hook = _load("ctx_stop_hook")
    stdin = {"hook_event_name": "PostCompact", "session_id": "sess-rearm"}
    rc, output = stop_hook.evaluate(stdin, tmp_path, {})
    assert rc == 0
    assert output is None


def test_broken_registry_surfaces_a_non_blocking_warning_in_additional_context(tmp_path):
    """파손 태그 항목은 매칭 0 이어도 조용히 사라지지 않고 rc0 을 유지한 채 경고 문안으로
    additionalContext 에 표면화된다(어댑터가 `result.text` 하나만 보고 주입하기 때문)."""
    stop_hook = _load("ctx_stop_hook")
    _write_registry(tmp_path, "#### 번들\n- `[shell: (unclosed]` 파손 항목.\n")
    stdin = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "ls -la"},
        "session_id": "sess-broken",
    }
    rc, output = stop_hook.evaluate(stdin, tmp_path, {})
    assert rc == 0
    assert output is not None
    guidance = output["hookSpecificOutput"]["additionalContext"]
    assert "[principle-recall]" in guidance
    assert "파손" in guidance


def test_cap_merged_context_at_exact_cap_is_untouched():
    """실제 `_MAX_ADDITIONAL_CONTEXT_CHARS`(10,000) 경계 — 정확히 상한이면 원문 그대로다."""
    stop_hook = _load("ctx_stop_hook")
    existing = "ctx-nudge/최종 안내"
    cap = stop_hook._MAX_ADDITIONAL_CONTEXT_CHARS
    extra = "x" * (cap - len(existing) - 1)  # "\n" 구분자 1자를 뺀 정확한 상한.
    combined = stop_hook._cap_merged_context(existing, extra)
    assert len(combined) == cap
    assert combined == f"{existing}\n{extra}"


def test_cap_merged_context_over_by_one_char_is_summarized_not_truncated():
    """상한을 딱 1자 넘기면(10,001) ctx 밴드 안내는 보존하고 recall 쪽만 생략 표시로 접는다
    (원문을 자르지 않는다 — F-006 리뷰 실측 10,091 재현)."""
    stop_hook = _load("ctx_stop_hook")
    existing = "ctx-nudge/최종 안내"
    cap = stop_hook._MAX_ADDITIONAL_CONTEXT_CHARS
    extra = "x" * (cap - len(existing))  # 위 exact-cap 테스트보다 정확히 1자 더 김.
    combined = stop_hook._cap_merged_context(existing, extra)
    assert len(combined) <= cap
    assert existing in combined  # ctx 밴드 안내는 그대로 보존된다.
    assert "x" * 50 not in combined  # recall 원문은 절단된 채로 남지 않는다(생략 표시로 대체).
    assert "[principle-recall]" in combined
