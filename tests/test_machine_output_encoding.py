"""T-0693 · T-0736: 기계 판독 출력의 UTF-8 무손실 보장 회귀 테스트.

T-0690 은 PowerShell 이 캡처하는 非tty stdout 을 콘솔 codepage(cp949 등)로 되돌리고, 그
코덱에 없는 문자를 `pm_translit` 대체표로 치환한다. 치환은 되돌릴 수 없으므로 **다른
프로세스가 파싱하는 출력**(하네스 훅 엔벨로프·`--json` 페이로드·ticket 사본 capability)이
그 텍스트 레이어를 지나면 데이터가 사라진다. 검증 축:

  (a) 콘솔 코덱이 cp949 로 전환된 상태에서 각 호출부의 출력이 UTF-8 bytes 로 나가고,
      cp949 미매핑 문자(`✅ ◦`)와 대체표 무음 치환 문자(`— ✓`)가 왕복 `json.loads` 로
      보존된다. 그 전환 상태는 **실 `configure_console_utf8`** 을 태워 만든다(픽스처가 실
      전환 함수와 기계로 묶인다 · 버퍼링도 실제와 같은 write_through=False).
  (b) 같은 조건에서 **사람 출력**(`print` 경로)은 종전대로 콘솔 코덱으로 나간다 — 두 경로가
      스트림이 아니라 호출 seam 으로 갈린다는 것이 이 티켓의 결정이다. 사람/기계 출력의
      **순서**도 지켜진다(seam 의 텍스트 레이어 선-flush).
  (c) `.buffer` 가 없는 스트림(테스트 캡처·`io.StringIO`)은 텍스트 write 로 폴백하고,
      `sys.stdout is None`(pythonw 기동)은 조용히 무출력이다.
  (d) 정적 가드 — 콘솔 텍스트 레이어로 기계 JSON 을 흘리는 세 형태
      (`print(json.dumps(...))` · 스트림 `json.dump(...)` · stdout 이름 수신자의
      `.write(json.dumps(...))`)가 가드 시야 전수에서 0 건. 시야 = 엔진 `tools/*.py` +
      기계 JSON 을 쓰는 어댑터 훅(`pm_orch_claude.py`). 파일 출력은 `json.dumps` + LF write
      관용구를 쓰므로 이 금지는 파일 경로와 충돌하지 않는다(허용 예외는 상단 상수 + 사유).
  (e) T-0736 — claude 어댑터 git-anchor 훅이 strict 콘솔 코덱(cp949) 아래서도 UTF-8 bytes 로
      쓴다(서브프로세스 실측).
"""
from __future__ import annotations

import ast
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
# 기계 JSON 을 쓰는 유일한 어댑터 훅(T-0736) — canonical 이 templates 안인 `@source` 파일이다.
ADAPTER_HOOK = REPO / "templates" / "claude_code" / ".claude" / "pm_orch_claude.py"

# cp949 대체표(`_TRANSLIT`)가 무음 치환하는 문자(`—`·`✓`)와 표에도 없어 `?` 로 사라지는 문자
# (`✅`·`◦`)를 한글과 함께 싣는다 — 손실이 나면 왕복이 깨진다.
LOSSY_SAMPLE = "판정 ✅ 완료 — 잔여 ✓ 항목 ◦ 없음"
CP949_UNMAPPABLE = ("✅", "◦")
TRANSLITERATED = {"—": "-", "✓": "v"}


def _load(name: str):
    """엔진 canonical 사본을 파일 경로로 직접 로드한다(도구는 패키지가 아니다)."""
    return _load_path(TOOLS / f"{name}.py", f"t0693_{name}")


def _load_path(path: Path, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def console_encoding():
    return _load("console_encoding")


def _install_capture_console(module, monkeypatch):
    """T-0690 의 실 전환 함수를 태워 PowerShell 캡처 상태(cp949·pm_translit)를 만든다.

    코덱/에러핸들러 문자열을 테스트가 다시 타이핑하지 않는다 — `_powershell_capture_encoding`
    하나만 대역하고 나머지는 실 `configure_console_utf8` 이 결정한다. 그래서 실제 캡처와 같은
    버퍼링(reconfigure 는 write_through 를 바꾸지 않아 기본 False)이 그대로 재현된다.
    """
    stdout = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", newline="\n")
    stderr = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", newline="\n")
    monkeypatch.setenv("PM_CONSOLE_HINT", "0")
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    monkeypatch.setattr(module, "_powershell_capture_encoding", lambda: "cp949")

    module.configure_console_utf8()

    state = module.console_state()["streams"]["stdout"]
    assert state["selected_encoding"] == "cp949" and state["applied"] is True
    assert stdout.write_through is False  # 실 캡처와 같은 버퍼링 상태여야 순서 축이 산다.
    return stdout


@pytest.fixture
def capture_console(console_encoding, monkeypatch):
    """실 전환을 태운 캡처 stdout 을 만든다(한 테스트에서 여러 번 호출 가능)."""
    def _make():
        return _install_capture_console(console_encoding, monkeypatch)
    return _make


def _written_bytes(stream) -> bytes:
    stream.flush()
    return stream.buffer.getvalue()


def _machine_payload(stream) -> object:
    """스트림에 실제로 나간 bytes 를 UTF-8 로 읽어 왕복 파싱한다(손실이면 여기서 깨진다)."""
    raw = _written_bytes(stream)
    assert raw.endswith(b"\n"), raw[-20:]
    return json.loads(raw.decode("utf-8"))


# ── (a) 호출부별 UTF-8 무손실 ────────────────────────────────────────────────

def test_seam_writes_utf8_bytes_under_a_cp949_console(console_encoding, capture_console):
    """seam 자체 — cp949 텍스트 레이어를 건너뛰고 UTF-8 bytes 한 줄을 쓴다."""
    stream = capture_console()
    payload = {"systemMessage": LOSSY_SAMPLE, "suppressOutput": False}

    console_encoding.write_machine_line(
        json.dumps(payload, ensure_ascii=False), stream=stream
    )

    raw = _written_bytes(stream)
    assert raw == json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"
    assert json.loads(raw.decode("utf-8")) == payload


def _neutralize_engine_anchor_guard(bootstrap, monkeypatch):
    """0단계 엔진 앵커 검사를 hermetic 무력화한다(`tests/test_pm_bootstrap.py` 의 autouse 픽스처와
    동형). 이 파일의 엔진은 등록 슬롯 worktree 에서 로드될 수 있어 실 REPO 가 PM 홈 등록 worktree
    사본으로 보이면 `[중단·0단계]` 로 dump 전에 끝난다 — 이 테스트의 축은 seam(bytes) 이지 앵커가
    아니다. 실 board 를 로드해 `_pm_home_worktree_misanchor`→None 만 패치하고 board=None 경로가
    그 패치본을 받게 한다."""
    real_board = bootstrap._load_board()
    if real_board is not None:
        monkeypatch.setattr(real_board, "_pm_home_worktree_misanchor",
                            lambda anchor, **_kw: None, raising=False)
    monkeypatch.setattr(bootstrap, "_load_board", lambda: real_board)


def _hermetic_bootstrap(bootstrap, tmp_path, log_text: str):
    """board/git/pytest 는 stub, log/pm_state 는 tmp 파일인 PmBootstrap (실 자산 미접촉)."""
    log_file = tmp_path / "current.md"
    log_file.write_text(log_text, encoding="utf-8", newline="\n")
    areas_file = tmp_path / "areas.md"  # 미생성 → 솔로 경로.

    def _board_fn(args):
        if args[:1] == ["lint"]:
            return 0, "✓ no lint issues\n"
        return 0, "  [open   ] T-0001  something  pm  tag\n"

    def _git_fn(args):
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return 0, "main\n"
        if args[:2] == ["log", "--oneline"]:
            return 0, "abc123 subject\n"
        return 0, ""

    return bootstrap.PmBootstrap(
        run_board_fn=_board_fn,
        run_pytest_fn=lambda: (_ for _ in ()).throw(AssertionError("pytest 미호출")),
        run_git_fn=_git_fn,
        log_file=log_file,
        areas_file=areas_file,
        pm_state_file=tmp_path / "absent_pm_state.md",
    )


_LOG_TEXT = (
    "# Project Log\n\n"
    "## [2026-08-17] handoff | PM 인계 — 다음 우선순위\n"
    f"- {LOSSY_SAMPLE}\n"
)


def test_pm_bootstrap_json_payload_is_utf8(capture_console, tmp_path, monkeypatch):
    """`--json` 부트스트랩 페이로드(로그 본문 포함)가 cp949 콘솔에서도 무손실이다."""
    bootstrap = _load("pm_bootstrap")
    _neutralize_engine_anchor_guard(bootstrap, monkeypatch)
    inst = _hermetic_bootstrap(bootstrap, tmp_path, _LOG_TEXT)
    stream = capture_console()

    assert inst.run(output_json=True) == 0

    data = _machine_payload(stream)
    assert LOSSY_SAMPLE in data["log_last_entry"]["body"]


def test_pm_log_ctx_guidance_json_payload_is_utf8(capture_console, monkeypatch):
    """ctx 가드 훅 엔벨로프(`--json`)가 UTF-8 로 나간다(호스트가 파싱하는 systemMessage)."""
    pm_log = _load("pm_log")
    monkeypatch.setattr(
        pm_log, "build_ctx_guard_guidance", lambda *args, **kwargs: LOSSY_SAMPLE
    )
    stream = capture_console()

    args = SimpleNamespace(
        band="warn", used_pct=80.0, remaining_pct=20.0, stop_pct=95.0, json=True
    )
    assert pm_log.cmd_ctx_guidance(args) == 0

    assert _machine_payload(stream) == {
        "systemMessage": LOSSY_SAMPLE, "suppressOutput": False,
    }


def test_pm_log_snapshot_json_payload_is_utf8(capture_console, monkeypatch, tmp_path):
    """snapshot 훅 페이로드는 ASCII 이스케이프지만 같은 seam 을 지나 UTF-8 bytes 로 나간다."""
    pm_log = _load("pm_log")
    monkeypatch.setattr(pm_log, "resolve_pm_home", lambda repo, cwd: cwd)
    monkeypatch.setattr(
        pm_log, "build_snapshot", lambda *args, **kwargs: (LOSSY_SAMPLE, "")
    )
    stream = capture_console()

    args = SimpleNamespace(cwd=str(tmp_path), state_lines=40, json=True)
    assert pm_log.cmd_snapshot(args) == 0

    raw = _written_bytes(stream)
    assert raw.decode("ascii")  # ensure_ascii=True 계약 유지(형태 변경 없음).
    assert json.loads(raw.decode("utf-8")) == {
        "systemMessage": LOSSY_SAMPLE, "suppressOutput": False,
    }


def test_board_git_anchor_hook_json_is_utf8(capture_console, monkeypatch):
    """git-anchor 하네스 훅 판정 JSON(한글 사유)이 cp949 콘솔에서도 무손실이다."""
    board = _load("board")
    verdict = {"verdict": "deny", "reason": LOSSY_SAMPLE}
    monkeypatch.setattr(
        board, "judge_git_anchor_command", lambda cwd, command: verdict
    )
    stream = capture_console()

    args = SimpleNamespace(cwd="/repo", command="git commit")
    assert board.cmd_git_anchor(args) == 0

    assert _machine_payload(stream) == verdict


def test_pm_delegate_ticket_cli_json_is_utf8(capture_console, monkeypatch, tmp_path):
    """ticket prepare/harvest 의 경로·capability JSON(다른 프로세스가 파싱)이 무손실이다."""
    pm_delegate = _load("pm_delegate")
    slot = tmp_path / "슬롯 — 사본"
    slot.mkdir()
    monkeypatch.setattr(pm_delegate, "_ticket_cli_owner", lambda _cwd: tmp_path / "pm")
    plan = pm_delegate.TicketCopyPlan(
        slot / "ticket-T-2000.md", slot / "baseline.md", slot / "metadata.json",
        slot, tmp_path / "pm", "T-2000", "developer", b"c" * 32,
    )
    monkeypatch.setattr(pm_delegate, "prepare_ticket_copy", lambda **_kwargs: plan)
    stream = capture_console()

    assert pm_delegate._cmd_ticket([
        "prepare", "--ticket", "T-2000", "--role", "developer", "--cwd", str(slot),
    ]) == 0

    prepared = _machine_payload(stream)
    assert prepared == {"copy": str(plan.path), "capability": plan.capability.hex()}
    assert Path(prepared["copy"]).parent == slot  # 한글·em dash 경로가 그대로 왕복한다.

    harvest_stream = capture_console()
    monkeypatch.setattr(
        pm_delegate, "harvest_ticket_copy",
        lambda **_kwargs: pm_delegate.TicketHarvestResult(False, True),
    )

    assert pm_delegate._cmd_ticket([
        "harvest", "--copy", str(plan.path), "--cwd", str(slot),
    ]) == 0

    assert _machine_payload(harvest_stream) == {
        "copy": str(plan.path.resolve()), "changed": False, "sync_ready": True,
    }


def _guard_conf() -> dict[str, str]:
    return {
        "delegate_enabled": "true",
        "delegate.developer.harness": "opencode",
        "delegate.developer.model": "qwen3-coder",
    }


def _guard_payload(role: str = "developer") -> dict[str, object]:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "Agent",
        "tool_input": {"subagent_type": role},
    }


@pytest.mark.parametrize(
    ("argv", "stdin_payload"),
    (
        (["decide", "--role", "developer", "--harness", "claude"], None),
        (["codex-hook"], {
            "hook_event_name": "PreToolUse",
            "tool_name": "collaborationspawn_agent",
            "tool_input": {"task_name": "developer"},
            "cwd": "/repo",
        }),
        ([], _guard_payload()),
    ),
)
def test_delegate_channel_guard_hook_payloads_are_utf8(
    capture_console, tmp_path, argv, stdin_payload
):
    """decide CLI · codex 훅 · Claude 훅 판정 페이로드가 모두 UTF-8 한 줄로 나간다."""
    guard = _load("delegate_channel_guard")
    stream = capture_console()

    assert guard.main(
        argv or None,
        stdin=io.StringIO(json.dumps(stdin_payload or {})),
        stdout=stream,
        stderr=io.StringIO(),
        config_loader=_guard_conf,
        state_dir=tmp_path,
    ) == 0

    result = _machine_payload(stream)
    # deny 사유는 한글 처방 문구다 — cp949 를 탔다면 대체표가 문자를 지웠을 것이다.
    assert "—" in json.dumps(result, ensure_ascii=False)


def _break_console_seam_loader(guard, monkeypatch):
    """가드 진입의 `console_encoding` 로드만 실패시킨다(사본 skew·형제 부재 모사)."""
    real_loader = guard._load_module_from_path

    def _broken_loader(path, expected_filename, **kwargs):
        if expected_filename == "console_encoding.py":
            raise RuntimeError("simulated missing console_encoding")
        return real_loader(path, expected_filename, **kwargs)

    monkeypatch.setattr(guard, "_load_module_from_path", _broken_loader)


def test_delegate_channel_guard_fallback_line_survives_a_missing_seam(
    capture_console, monkeypatch, tmp_path
):
    """seam 로드 자체가 실패한 경계도 호스트에 완전한 엔벨로프 한 줄을 준다(fail-open 계약)."""
    guard = _load("delegate_channel_guard")
    _break_console_seam_loader(guard, monkeypatch)
    stream = capture_console()
    stderr = io.StringIO()

    assert guard.main(
        ["supervise", "PreToolUse", sys.executable, "-c", "pass"],
        stdin=io.StringIO("{}"),
        stdout=stream,
        stderr=stderr,
        config_loader=_guard_conf,
        state_dir=tmp_path,
    ) == 0

    text = _written_bytes(stream).decode("utf-8")
    assert text.count("\n") == 1
    assert set(json.loads(text)) == set(guard.CODEX_WARNING_ENVELOPE_KEYS)
    # 조용한 강등이 아니다 — 원인(seam 로드 실패)이 fail-open 보고에 남는다.
    assert "simulated missing console_encoding" in stderr.getvalue()


def test_missing_seam_fallback_still_writes_utf8_bytes(
    capture_console, monkeypatch, tmp_path
):
    """seam 없는 폴백도 UTF-8 bytes 다 — 한글+em dash 사유가 strict cp949 로 유실되지 않는다."""
    guard = _load("delegate_channel_guard")
    _break_console_seam_loader(guard, monkeypatch)
    stream = capture_console()

    assert guard.main(
        ["decide", "--role", "developer", "--harness", "claude"],
        stdout=stream,
        stderr=io.StringIO(),
        config_loader=_guard_conf,
        state_dir=tmp_path,
    ) == 0

    payload = _machine_payload(stream)
    assert payload["verdict"] == "allow"
    assert "—" in payload["reason"] and "통과(fail-open)" in payload["reason"]


# ── (b) 사람 출력은 종전대로 콘솔 코덱 · 순서 보존 ───────────────────────────

def test_human_markdown_path_still_goes_through_the_console_codec(
    capture_console, tmp_path, monkeypatch
):
    """같은 부트스트랩의 markdown(사람) 출력은 cp949 로 나간다 — 분리 축은 스트림이 아니라 seam."""
    bootstrap = _load("pm_bootstrap")
    _neutralize_engine_anchor_guard(bootstrap, monkeypatch)
    inst = _hermetic_bootstrap(bootstrap, tmp_path, _LOG_TEXT)
    stream = capture_console()

    assert inst.run() == 0

    raw = _written_bytes(stream)
    with pytest.raises(UnicodeDecodeError):
        raw.decode("utf-8")  # UTF-8 이 아니다(=사람 경로는 전환된 코덱을 탄다).
    text = raw.decode("cp949")
    assert "판정" in text and "완료" in text  # cp949 에 있는 한글은 살아 있다.
    for lost in CP949_UNMAPPABLE:
        assert lost not in text
    for original, replacement in TRANSLITERATED.items():
        assert original not in text
        assert replacement in text


def test_human_text_path_of_the_same_command_is_not_utf8(capture_console, monkeypatch):
    """`--json` 없는 ctx 가드 안내(같은 커맨드의 사람 분기)도 콘솔 코덱을 그대로 탄다."""
    pm_log = _load("pm_log")
    monkeypatch.setattr(
        pm_log, "build_ctx_guard_guidance", lambda *args, **kwargs: LOSSY_SAMPLE
    )
    stream = capture_console()

    args = SimpleNamespace(
        band="warn", used_pct=80.0, remaining_pct=20.0, stop_pct=95.0, json=False
    )
    assert pm_log.cmd_ctx_guidance(args) == 0

    raw = _written_bytes(stream)
    with pytest.raises(UnicodeDecodeError):
        raw.decode("utf-8")
    assert "-" in raw.decode("cp949")  # em dash 는 대체표를 탄다.


_HUMAN_BEFORE = "사람 출력 1"
_HUMAN_AFTER = "사람 출력 2"
_MACHINE_LINE = '{"기계":"판정 —"}'


def _emit_human_machine_human(module, stream) -> bytes:
    """사람 → 기계 → 사람 순서로 같은 스트림에 쓴 뒤 실제 bytes 를 돌려준다."""
    print(_HUMAN_BEFORE, file=stream)
    module.write_machine_line(_MACHINE_LINE, stream=stream)
    print(_HUMAN_AFTER, file=stream)
    return _written_bytes(stream)


def test_machine_line_keeps_the_human_output_order(console_encoding, capture_console):
    """기계 bytes 를 쓰기 전에 텍스트 레이어를 비워 사람/기계 출력 순서가 뒤집히지 않는다."""
    stream = capture_console()

    raw = _emit_human_machine_human(console_encoding, stream)

    before = raw.index(_HUMAN_BEFORE.encode("cp949"))
    machine = raw.index(_MACHINE_LINE.encode("utf-8"))
    after = raw.index(_HUMAN_AFTER.encode("cp949"))
    assert before < machine < after


def test_order_is_lost_when_the_pre_flush_is_removed(tmp_path, monkeypatch):
    """감도 실증 — seam 의 선-flush 한 줄을 지우면 기계 줄이 사람 출력 앞으로 밀린다."""
    source = (TOOLS / "console_encoding.py").read_text(encoding="utf-8")
    anchor = '    _flush_quietly(target)\n    buffer.write(line.encode("utf-8"))\n'
    assert source.count(anchor) == 1, "변이 앵커 소실"
    mutated_path = tmp_path / "console_encoding.py"
    mutated_path.write_text(
        source.replace(anchor, '    buffer.write(line.encode("utf-8"))\n', 1),
        encoding="utf-8",
        newline="\n",
    )
    module = _load_path(mutated_path, "console_encoding_without_preflush")
    stream = _install_capture_console(module, monkeypatch)

    raw = _emit_human_machine_human(module, stream)

    machine = raw.index(_MACHINE_LINE.encode("utf-8"))
    assert machine < raw.index(_HUMAN_BEFORE.encode("cp949"))  # 순서 붕괴가 실제로 보인다.


# ── (c) `.buffer` 없는 스트림 · 표준 스트림 부재 ─────────────────────────────

def test_stream_without_buffer_falls_back_to_text_write(console_encoding):
    """`io.StringIO`(테스트 캡처)는 텍스트 write 로 폴백하고 한 줄 계약을 지킨다."""
    sink = io.StringIO()
    line = json.dumps({"reason": LOSSY_SAMPLE}, ensure_ascii=False)

    console_encoding.write_machine_line(line, stream=sink)

    assert sink.getvalue() == line + "\n"


def test_default_stream_without_buffer_is_taken_from_sys_stdout(
    console_encoding, monkeypatch
):
    """`stream` 미지정이면 호출 시점의 `sys.stdout` 을 쓴다(캡처 스트림 폴백 포함)."""
    sink = io.StringIO()
    monkeypatch.setattr(sys, "stdout", sink)

    console_encoding.write_machine_line("{}")

    assert sink.getvalue() == "{}\n"


def test_absent_standard_stream_is_a_silent_no_op(console_encoding, monkeypatch):
    """`sys.stdout is None`(pythonw/pyw 기동)은 종전 `print` 처럼 무출력이다(예외 금지)."""
    monkeypatch.setattr(sys, "stdout", None)

    console_encoding.write_machine_line("{}")  # AttributeError 로 죽으면 red.


def test_guard_hook_line_is_unchanged_on_a_capture_stream(tmp_path):
    """`.buffer` 없는 스트림을 넘기는 기존 소비자(테스트·래퍼)의 한 줄 계약이 그대로다."""
    guard = _load("delegate_channel_guard")
    sink = io.StringIO()

    assert guard.main(
        ["decide", "--role", "developer", "--harness", "opencode"],
        stdout=sink,
        config_loader=_guard_conf,
        state_dir=tmp_path,
    ) == 0

    assert sink.getvalue().count("\n") == 1
    assert json.loads(sink.getvalue())["verdict"] in {"allow", "deny"}


# ── (d) 정적 가드 — 콘솔 텍스트 레이어로 나가는 기계 JSON 0 건 ────────────────

PRINT_SHAPE = "print(json.dumps(...))"
STREAM_DUMP_SHAPE = "json.dump(..., stream)"
STDOUT_WRITE_SHAPE = "<stdout>.write(json.dumps(...))"

# stdout 을 담는 이름들. 엔진은 `sys.stdout` 을 `stdout`/`output_stream` 지역 이름으로 넘겨
# 쓰므로(delegate_channel_guard) 그 별칭 회귀도 가드 시야에 든다. 파일 handle 쓰기
# (`handle.write(json.dumps(...))` 실측 8건 — gate_snapshot·pm_import·pm_relay)는 이름이 달라
# 걸리지 않는다.
_STDOUT_WRITER_NAMES = frozenset({"sys.stdout", "stdout", "output_stream"})

# 허용 예외 — (파일 경로, 형태) → 사유. 비어 있는 것이 정상 상태다. 새 항목은 사유와 함께만
# 등재한다(빈 사유는 아래 사유 가드가 red). 기계 출력은 `console_encoding.write_machine_line`,
# 파일 출력은 `json.dumps(...)` + LF write 가 엔진의 단일 관용구다.
ALLOWED_CONSOLE_JSON: dict[tuple[str, str], str] = {}

# 가드 시야 밖 어댑터 스크립트 — `console_encoding` 을 로드하지 않아 T-0690 의 코덱 전환/대체표
# 손실 클래스에 닿지 않는다(제외 근거). 그 전제는 아래 테스트가 기계로 고정한다.
EXCLUDED_ADAPTER_SCRIPTS = (
    REPO / "templates" / "claude_code" / ".claude" / "ctx_guard.py",
    REPO / "templates" / "claude_code" / ".claude" / "ctx_statusline.py",
    REPO / "templates" / "claude_code" / ".claude" / "ctx_stop_hook.py",
    REPO / "templates" / "codex" / ".codex" / "pm_orch_codex.py",
    REPO / "templates" / "opencode" / ".opencode" / "pm_orch_opencode.py",
)


def guarded_sources() -> tuple[Path, ...]:
    """가드 시야 = 엔진 `tools/*.py` 전수 + 기계 JSON 을 쓰는 어댑터 훅."""
    return tuple(sorted(TOOLS.glob("*.py"))) + (ADAPTER_HOOK,)


def _dotted_name(call: ast.Call) -> str:
    """`print`·`json.dumps`·`sys.stdout.write` 처럼 호출 대상을 점 표기 이름으로 만든다."""
    parts: list[str] = []
    node: ast.AST = call.func
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return ""
    parts.append(node.id)
    return ".".join(reversed(parts))


def _has_json_dumps(node: ast.AST) -> bool:
    return any(
        isinstance(child, ast.Call) and _dotted_name(child) == "json.dumps"
        for child in ast.walk(node)
    )


def _shape_of(call: ast.Call) -> str | None:
    name = _dotted_name(call)
    if name == "print" and any(_has_json_dumps(arg) for arg in call.args):
        return PRINT_SHAPE
    if name == "json.dump":
        return STREAM_DUMP_SHAPE
    if name.endswith(".write") and name[: -len(".write")] in _STDOUT_WRITER_NAMES:
        if any(_has_json_dumps(arg) for arg in call.args):
            return STDOUT_WRITE_SHAPE
    return None


def scan_console_json(sources) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(스캔한 파일, 위반) — 주석·문자열은 세지 않는 AST 판정."""
    scanned: list[str] = []
    violations: list[str] = []
    for source in sources:
        scanned.append(_display(source))
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            shape = _shape_of(node)
            if shape is None:
                continue
            if ALLOWED_CONSOLE_JSON.get((_display(source), shape), "").strip():
                continue
            violations.append(f"{_display(source)}:{node.lineno}: {shape}")
    return tuple(scanned), tuple(violations)


def _display(path: Path) -> str:
    try:
        return path.relative_to(REPO).as_posix()
    except ValueError:
        return path.name


_TEXT_CANDIDATE = re.compile(
    r"print\(\s*json\.dumps|json\.dump\(|(?<![\w.])(?:sys\.stdout|stdout|output_stream)\.write\("
)


def text_scan_candidates(sources) -> tuple[str, ...]:
    """AST 와 독립된 텍스트 전수 측정 — 같은 표면을 정규식으로 다시 센다.

    stdout 계열 `.write(` 는 사람 텍스트 출력에도 쓰이므로 같은 줄의 `json.dump` 유무로 가른다.
    여러 줄에 걸친 호출은 이 측정이 못 보므로(AST 가 상위집합) 텍스트 결과는 하한이다.
    """
    hits: list[str] = []
    for source in sources:
        for number, line in enumerate(
            source.read_text(encoding="utf-8").splitlines(), start=1
        ):
            match = _TEXT_CANDIDATE.search(line)
            if match is None:
                continue
            if match.group().endswith(".write(") and "json.dump" not in line:
                continue
            hits.append(f"{_display(source)}:{number}")
    return tuple(hits)


def test_no_machine_json_goes_through_the_console_text_layer():
    """가드 시야 전수에서 콘솔 텍스트 레이어로 나가는 기계 JSON 이 0 건이다."""
    _scanned, violations = scan_console_json(guarded_sources())
    assert violations == (), "\n".join(violations)


def test_guard_view_equals_the_surface_and_matches_an_independent_text_scan():
    """가드 시야 == 표면(엔진 전수 + 어댑터 훅) · 텍스트 전수 측정과 어긋나지 않는다."""
    scanned, violations = scan_console_json(guarded_sources())
    surface = tuple(
        _display(path) for path in sorted(TOOLS.glob("*.py"))
    ) + (_display(ADAPTER_HOOK),)

    assert scanned == surface
    assert len(surface) > 1  # 표면 자체가 비면 가드가 공전한다.
    assert _display(ADAPTER_HOOK) in scanned  # T-0736 — 어댑터 훅이 시야에 있다.
    assert text_scan_candidates(guarded_sources()) == ()
    assert violations == ()


def test_excluded_adapter_scripts_do_not_load_the_console_seam():
    """가드 시야 밖 어댑터 스크립트는 `console_encoding` 을 로드하지 않는다(제외 근거 고정).

    이 단언이 red 가 되는 순간 = 그 어댑터가 코덱 전환을 태우기 시작한 순간이므로, 그때 그
    파일을 `guarded_sources()` 에 넣어야 한다(가드가 침묵한 채 손실 표면이 생기지 않게).
    """
    assert EXCLUDED_ADAPTER_SCRIPTS, "제외 목록이 비면 근거 고정이 공전한다"
    for path in EXCLUDED_ADAPTER_SCRIPTS:
        assert path.is_file(), path
        assert "console_encoding" not in path.read_text(encoding="utf-8"), _display(path)


def test_allowed_exceptions_are_reasoned():
    """허용 예외는 사유 없이 등재할 수 없다(빈 사유 = 사실상 무제한 예외)."""
    for key, reason in ALLOWED_CONSOLE_JSON.items():
        assert isinstance(reason, str) and reason.strip(), key


@pytest.mark.parametrize(
    ("relative", "current", "reverted", "shape", "text_visible"),
    (
        (
            ".project_manager/tools/pm_log.py",
            "        _write_machine_line(\n"
            "            json.dumps(payload, ensure_ascii=True, separators=(\",\", \":\"))\n"
            "        )\n",
            "        print(json.dumps(payload, ensure_ascii=True, separators=(\",\", \":\")))\n",
            PRINT_SHAPE,
            True,
        ),
        (
            ".project_manager/tools/delegate_channel_guard.py",
            "            _write_machine_line(\n"
            "                json.dumps(_validated_codex_envelope(result), ensure_ascii=False),\n"
            "                output_stream,\n"
            "                machine_seam,\n"
            "            )\n",
            "            json.dump(\n"
            "                _validated_codex_envelope(result), output_stream, ensure_ascii=False\n"
            "            )\n"
            "            output_stream.write(\"\\n\")\n",
            STREAM_DUMP_SHAPE,
            True,
        ),
        (
            ".project_manager/tools/delegate_channel_guard.py",
            "            _write_machine_line(\n"
            "                json.dumps(_validated_codex_envelope(result), ensure_ascii=False),\n"
            "                output_stream,\n"
            "                machine_seam,\n"
            "            )\n",
            "            output_stream.write(\n"
            "                json.dumps(_validated_codex_envelope(result), ensure_ascii=False)\n"
            "                + \"\\n\"\n"
            "            )\n",
            STDOUT_WRITE_SHAPE,
            False,  # 수신자와 `json.dumps` 가 다른 줄 — 텍스트 측정의 하한 경계.
        ),
        (
            "templates/claude_code/.claude/pm_orch_claude.py",
            "    if output is not None:\n        _write_hook_json(output)\n",
            "    if output is not None:\n"
            "        sys.stdout.write(json.dumps(output, ensure_ascii=False))\n",
            STDOUT_WRITE_SHAPE,
            True,
        ),
    ),
)
def test_guard_turns_red_when_one_call_site_is_reverted(
    tmp_path, relative, current, reverted, shape, text_visible
):
    """감도 실증 — 호출부 **한 곳**을 종전 형태로 되돌리면 AST 판정이 red.

    텍스트 전수 측정은 한 줄 형태만 보는 하한이라 결과를 `text_visible` 로 못박는다 —
    AST 가 상위집합이라는 성질 자체를 여기서 실증한다(수신자와 `json.dumps` 가 다른 줄인
    형태는 텍스트 0·AST 1).
    """
    source = (REPO / relative).read_text(encoding="utf-8")
    assert source.count(current) == 1, "변이 앵커 소실"
    mutated = tmp_path / Path(relative).name
    mutated.write_text(
        source.replace(current, reverted, 1), encoding="utf-8", newline="\n"
    )

    _scanned, violations = scan_console_json([mutated])

    assert [violation for violation in violations if violation.endswith(shape)]
    assert bool(text_scan_candidates([mutated])) is text_visible


def test_guard_ignores_mentions_in_comments_and_strings(tmp_path):
    """주석·문자열의 형태 언급은 세지 않는다(AST 판정 · 문서화 자유)."""
    prose = tmp_path / "prose.py"
    prose.write_text(
        '"""print(json.dumps(payload)) 는 콘솔 코덱을 탄다."""\n'
        "# json.dump(payload, sys.stdout)\n"
        'DOC = "output_stream.write(json.dumps(x))"\n',
        encoding="utf-8",
        newline="\n",
    )

    _scanned, violations = scan_console_json([prose])

    assert violations == ()


def test_guard_does_not_flag_file_handle_writes(tmp_path):
    """파일 handle 로 가는 `.write(json.dumps(...))` 는 콘솔 표면이 아니다(오탐 금지)."""
    handle_write = tmp_path / "handle.py"
    handle_write.write_text(
        "import json\n"
        "def dump(path, payload):\n"
        "    with open(path, 'w', encoding='utf-8', newline='\\n') as handle:\n"
        "        handle.write(json.dumps(payload, ensure_ascii=False) + '\\n')\n",
        encoding="utf-8",
        newline="\n",
    )

    _scanned, violations = scan_console_json([handle_write])

    assert violations == ()


# ── (e) T-0736 — claude 어댑터 git-anchor 훅의 strict 코덱 실측 ───────────────

# cp949 에 없는 기호 + 한글. em dash 는 실측 UnicodeEncodeError 라 텍스트 write 면 훅이 죽는다.
HOOK_ADVISORY = "판정 — ✓ 확인 ⚠ 주의"

_HOOK_DRIVER = """
import importlib.util
import sys
from pathlib import Path

adapter_path, mode, advisory, root = sys.argv[1:5]
spec = importlib.util.spec_from_file_location("pm_orch_claude_hook_mode", adapter_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
if mode == "evaluate":
    module.git_anchor_hook_evaluate = lambda payload, root: {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": advisory,
        }
    }
    sys.exit(module.run_git_anchor_hook(Path(root)))
sys.exit(module._emit_git_anchor_boundary_warn(RuntimeError(advisory)))
"""


def _strict_cp949_env() -> dict[str, str]:
    """Windows 파이프 stdout(로케일 코덱·strict)을 어느 플랫폼에서나 모사하는 자식 환경."""
    env = dict(os.environ)
    env.update({"PYTHONIOENCODING": "cp949:strict", "PYTHONUTF8": "0"})
    return env


@pytest.mark.parametrize("mode", ("evaluate", "boundary"))
def test_claude_adapter_git_anchor_hook_writes_utf8_under_strict_cp949(tmp_path, mode):
    """어댑터 훅의 두 emit 이 strict cp949 콘솔에서도 UTF-8 bytes 로 나간다 (T-0736)."""
    proc = subprocess.run(
        [
            sys.executable, "-c", _HOOK_DRIVER,
            str(ADAPTER_HOOK), mode, HOOK_ADVISORY, str(tmp_path),
        ],
        input=b"{}",
        capture_output=True,
        env=_strict_cp949_env(),
        check=False,
    )

    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    payload = json.loads(proc.stdout.decode("utf-8"))  # 손실이면 여기서 깨진다.
    context = payload["hookSpecificOutput"]["additionalContext"]
    assert HOOK_ADVISORY in context
    for symbol in ("—", "✓", "⚠", "판정"):
        assert symbol in context


def test_claude_adapter_hook_output_shape_is_unchanged(tmp_path):
    """훅 출력 형태는 종전 그대로다 — 단일 JSON · 종결 개행 없음(Claude 소비자 계약)."""
    proc = subprocess.run(
        [
            sys.executable, "-c", _HOOK_DRIVER,
            str(ADAPTER_HOOK), "evaluate", HOOK_ADVISORY, str(tmp_path),
        ],
        input=b"{}",
        capture_output=True,
        env=_strict_cp949_env(),
        check=False,
    )

    assert proc.returncode == 0
    assert not proc.stdout.endswith(b"\n")
    assert proc.stdout.count(b"\n") == 0
