"""T-0693: 기계 판독 출력의 UTF-8 무손실 보장 회귀 테스트.

T-0690 은 PowerShell 이 캡처하는 非tty stdout 을 콘솔 codepage(cp949 등)로 되돌리고, 그
코덱에 없는 문자를 `pm_translit` 대체표로 치환한다. 치환은 되돌릴 수 없으므로 **다른
프로세스가 파싱하는 출력**(하네스 훅 엔벨로프·`--json` 페이로드·ticket 사본 capability)이
그 텍스트 레이어를 지나면 데이터가 사라진다. 검증 축은 티켓 (a)~(d):

  (a) 콘솔 코덱이 cp949 로 전환된 상태에서 각 호출부의 출력이 UTF-8 bytes 로 나가고,
      cp949 미매핑 문자(`✅ ◦`)와 대체표 무음 치환 문자(`— ✓`)가 왕복 `json.loads` 로
      보존된다.
  (b) 같은 조건에서 **사람 출력**(`print` 경로)은 종전대로 콘솔 코덱으로 나간다 — 두 경로가
      스트림이 아니라 호출 seam 으로 갈린다는 것이 이 티켓의 결정이다.
  (c) `.buffer` 가 없는 스트림(테스트 캡처·`io.StringIO`)은 텍스트 write 로 폴백한다.
  (d) 정적 가드 — 엔진이 콘솔 텍스트 레이어로 기계 JSON 을 흘리는 세 형태
      (`print(json.dumps(...))` · 스트림 `json.dump(...)` · `sys.stdout.write(json.dumps(...))`)
      가 `.project_manager/tools/*.py` 전수에서 0 건. 파일 출력은 `json.dumps` + LF write 를
      쓰므로 이 금지는 파일 경로도 함께 덮는다(허용 예외는 상단 상수 + 사유).

cp949 스트림은 실 Windows 없이 T-0690 이 만드는 상태를 그대로 모델링한다 — `configure_console_utf8`
가 캡처에서 실행하는 `reconfigure(encoding="cp949", errors="pm_translit")` 와 같은 텍스트 레이어다.
"""
from __future__ import annotations

import ast
import importlib.util
import io
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

# cp949 대체표(`_TRANSLIT`)가 무음 치환하는 문자(`—`·`✓`)와 표에도 없어 `?` 로 사라지는 문자
# (`✅`·`◦`)를 한글과 함께 싣는다 — 손실이 나면 왕복이 깨진다.
LOSSY_SAMPLE = "판정 ✅ 완료 — 잔여 ✓ 항목 ◦ 없음"
CP949_UNMAPPABLE = ("✅", "◦")
TRANSLITERATED = {"—": "-", "✓": "v"}


def _load(name: str):
    """엔진 canonical 사본을 파일 경로로 직접 로드한다(도구는 패키지가 아니다)."""
    spec = importlib.util.spec_from_file_location(f"t0693_{name}", TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def console_encoding():
    return _load("console_encoding")


def _capture_console(console_encoding):
    """PowerShell 캡처에서 T-0690 이 만드는 텍스트 레이어(cp949 · pm_translit)를 모델링한다."""
    assert console_encoding is not None  # pm_translit 핸들러 등록은 모듈 로드가 소유한다.
    return io.TextIOWrapper(
        io.BytesIO(), encoding="cp949", errors="pm_translit", newline="\n",
        write_through=True,
    )


def _written_bytes(stream) -> bytes:
    stream.flush()
    return stream.buffer.getvalue()


def _machine_payload(stream) -> object:
    """스트림에 실제로 나간 bytes 를 UTF-8 로 읽어 왕복 파싱한다(손실이면 여기서 깨진다)."""
    raw = _written_bytes(stream)
    assert raw.endswith(b"\n"), raw[-20:]
    return json.loads(raw.decode("utf-8"))


# ── (a) 호출부별 UTF-8 무손실 ────────────────────────────────────────────────

def test_seam_writes_utf8_bytes_under_a_cp949_console(console_encoding):
    """seam 자체 — cp949 텍스트 레이어를 건너뛰고 UTF-8 bytes 한 줄을 쓴다."""
    stream = _capture_console(console_encoding)
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
            return 0, "\u2713 no lint issues\n"
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


def test_pm_bootstrap_json_payload_is_utf8(console_encoding, monkeypatch, tmp_path):
    """`--json` 부트스트랩 페이로드(로그 본문 포함)가 cp949 콘솔에서도 무손실이다."""
    bootstrap = _load("pm_bootstrap")
    _neutralize_engine_anchor_guard(bootstrap, monkeypatch)
    inst = _hermetic_bootstrap(bootstrap, tmp_path, _LOG_TEXT)
    stream = _capture_console(console_encoding)
    monkeypatch.setattr(sys, "stdout", stream)

    assert inst.run(output_json=True) == 0

    data = _machine_payload(stream)
    assert LOSSY_SAMPLE in data["log_last_entry"]["body"]


def test_pm_log_ctx_guidance_json_payload_is_utf8(console_encoding, monkeypatch):
    """ctx 가드 훅 엔벨로프(`--json`)가 UTF-8 로 나간다(호스트가 파싱하는 systemMessage)."""
    pm_log = _load("pm_log")
    monkeypatch.setattr(
        pm_log, "build_ctx_guard_guidance", lambda *args, **kwargs: LOSSY_SAMPLE
    )
    stream = _capture_console(console_encoding)
    monkeypatch.setattr(sys, "stdout", stream)

    args = SimpleNamespace(
        band="warn", used_pct=80.0, remaining_pct=20.0, stop_pct=95.0, json=True
    )
    assert pm_log.cmd_ctx_guidance(args) == 0

    assert _machine_payload(stream) == {
        "systemMessage": LOSSY_SAMPLE, "suppressOutput": False,
    }


def test_pm_log_snapshot_json_payload_is_utf8(console_encoding, monkeypatch, tmp_path):
    """snapshot 훅 페이로드는 ASCII 이스케이프지만 같은 seam 을 지나 UTF-8 bytes 로 나간다."""
    pm_log = _load("pm_log")
    monkeypatch.setattr(pm_log, "resolve_pm_home", lambda repo, cwd: cwd)
    monkeypatch.setattr(
        pm_log, "build_snapshot", lambda *args, **kwargs: (LOSSY_SAMPLE, "")
    )
    stream = _capture_console(console_encoding)
    monkeypatch.setattr(sys, "stdout", stream)

    args = SimpleNamespace(cwd=str(tmp_path), state_lines=40, json=True)
    assert pm_log.cmd_snapshot(args) == 0

    raw = _written_bytes(stream)
    assert raw.decode("ascii")  # ensure_ascii=True 계약 유지(형태 변경 없음).
    assert json.loads(raw.decode("utf-8")) == {
        "systemMessage": LOSSY_SAMPLE, "suppressOutput": False,
    }


def test_board_git_anchor_hook_json_is_utf8(console_encoding, monkeypatch):
    """git-anchor 하네스 훅 판정 JSON(한글 사유)이 cp949 콘솔에서도 무손실이다."""
    board = _load("board")
    verdict = {"verdict": "deny", "reason": LOSSY_SAMPLE}
    monkeypatch.setattr(
        board, "judge_git_anchor_command", lambda cwd, command: verdict
    )
    stream = _capture_console(console_encoding)
    monkeypatch.setattr(sys, "stdout", stream)

    args = SimpleNamespace(cwd="/repo", command="git commit")
    assert board.cmd_git_anchor(args) == 0

    assert _machine_payload(stream) == verdict


def test_pm_delegate_ticket_cli_json_is_utf8(console_encoding, monkeypatch, tmp_path):
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
    stream = _capture_console(console_encoding)
    monkeypatch.setattr(sys, "stdout", stream)

    assert pm_delegate._cmd_ticket([
        "prepare", "--ticket", "T-2000", "--role", "developer", "--cwd", str(slot),
    ]) == 0

    prepared = _machine_payload(stream)
    assert prepared == {"copy": str(plan.path), "capability": plan.capability.hex()}
    assert Path(prepared["copy"]).parent == slot  # 한글·em dash 경로가 그대로 왕복한다.

    harvest_stream = _capture_console(console_encoding)
    monkeypatch.setattr(sys, "stdout", harvest_stream)
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
    console_encoding, tmp_path, argv, stdin_payload
):
    """decide CLI · codex 훅 · Claude 훅 판정 페이로드가 모두 UTF-8 한 줄로 나간다."""
    guard = _load("delegate_channel_guard")
    stream = _capture_console(console_encoding)

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


def test_delegate_channel_guard_fallback_line_survives_a_missing_seam(
    console_encoding, monkeypatch, tmp_path
):
    """seam 로드 자체가 실패한 경계도 호스트에 완전한 엔벨로프 한 줄을 준다(fail-open 계약)."""
    guard = _load("delegate_channel_guard")
    real_loader = guard._load_module_from_path

    def _broken_loader(path, expected_filename, **kwargs):
        if expected_filename == "console_encoding.py":
            raise RuntimeError("simulated missing console_encoding")
        return real_loader(path, expected_filename, **kwargs)

    monkeypatch.setattr(guard, "_load_module_from_path", _broken_loader)
    stream = _capture_console(console_encoding)
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


# ── (b) 사람 출력은 종전대로 콘솔 코덱 ───────────────────────────────────────

def test_human_markdown_path_still_goes_through_the_console_codec(
    console_encoding, monkeypatch, tmp_path
):
    """같은 부트스트랩의 markdown(사람) 출력은 cp949 로 나간다 — 분리 축은 스트림이 아니라 seam."""
    bootstrap = _load("pm_bootstrap")
    _neutralize_engine_anchor_guard(bootstrap, monkeypatch)
    inst = _hermetic_bootstrap(bootstrap, tmp_path, _LOG_TEXT)
    stream = _capture_console(console_encoding)
    monkeypatch.setattr(sys, "stdout", stream)

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


def test_human_text_path_of_the_same_command_is_not_utf8(console_encoding, monkeypatch):
    """`--json` 없는 ctx 가드 안내(같은 커맨드의 사람 분기)도 콘솔 코덱을 그대로 탄다."""
    pm_log = _load("pm_log")
    monkeypatch.setattr(
        pm_log, "build_ctx_guard_guidance", lambda *args, **kwargs: LOSSY_SAMPLE
    )
    stream = _capture_console(console_encoding)
    monkeypatch.setattr(sys, "stdout", stream)

    args = SimpleNamespace(
        band="warn", used_pct=80.0, remaining_pct=20.0, stop_pct=95.0, json=False
    )
    assert pm_log.cmd_ctx_guidance(args) == 0

    raw = _written_bytes(stream)
    with pytest.raises(UnicodeDecodeError):
        raw.decode("utf-8")
    assert "-" in raw.decode("cp949")  # em dash 는 대체표를 탄다.


# ── (c) `.buffer` 없는 스트림 폴백 ───────────────────────────────────────────

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
STDOUT_WRITE_SHAPE = "sys.stdout.write(json.dumps(...))"

# 허용 예외 — (파일명, 형태) → 사유. 비어 있는 것이 정상 상태다. 새 항목은 사유와 함께만
# 등재한다(빈 사유는 아래 사유 가드가 red). 기계 출력은 `console_encoding.write_machine_line`,
# 파일 출력은 `json.dumps(...)` + LF write 가 엔진의 단일 관용구다.
ALLOWED_CONSOLE_JSON: dict[tuple[str, str], str] = {}


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
    if name == "sys.stdout.write" and any(_has_json_dumps(arg) for arg in call.args):
        return STDOUT_WRITE_SHAPE
    return None


def scan_console_json(tools: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(스캔한 파일명, 위반) — 주석·문자열은 세지 않는 AST 판정."""
    scanned: list[str] = []
    violations: list[str] = []
    for source in sorted(tools.glob("*.py")):
        scanned.append(source.name)
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            shape = _shape_of(node)
            if shape is None or ALLOWED_CONSOLE_JSON.get((source.name, shape), "").strip():
                continue
            violations.append(f"{source.name}:{node.lineno}: {shape}")
    return tuple(scanned), tuple(violations)


_TEXT_CANDIDATE = re.compile(r"print\(\s*json\.dumps|json\.dump\(|sys\.stdout\.write\(")


def text_scan_candidates(tools: Path) -> tuple[str, ...]:
    """AST 와 독립된 텍스트 전수 측정 — 같은 표면을 정규식으로 다시 센다.

    `sys.stdout.write(` 는 사람 텍스트 출력에도 쓰이므로 같은 줄의 `json.dump` 유무로 가른다.
    여러 줄에 걸친 호출은 이 측정이 못 보므로(AST 가 상위집합) 텍스트 결과는 하한이다.
    """
    hits: list[str] = []
    for source in sorted(tools.glob("*.py")):
        for number, line in enumerate(
            source.read_text(encoding="utf-8").splitlines(), start=1
        ):
            match = _TEXT_CANDIDATE.search(line)
            if match is None:
                continue
            if match.group().startswith("sys.stdout.write") and "json.dump" not in line:
                continue
            hits.append(f"{source.name}:{number}")
    return tuple(hits)


def test_no_machine_json_goes_through_the_console_text_layer():
    """엔진 전수에서 콘솔 텍스트 레이어로 나가는 기계 JSON 이 0 건이다."""
    _scanned, violations = scan_console_json(TOOLS)
    assert violations == (), "\n".join(violations)


def test_guard_view_equals_the_surface_and_matches_an_independent_text_scan():
    """가드 시야 == 표면(tools/*.py 전수) · 텍스트 전수 측정과 결과가 어긋나지 않는다."""
    scanned, violations = scan_console_json(TOOLS)
    surface = tuple(sorted(path.name for path in TOOLS.glob("*.py")))

    assert scanned == surface
    assert len(surface) > 1  # 표면 자체가 비면 가드가 공전한다.
    assert text_scan_candidates(TOOLS) == ()
    assert violations == ()


def test_allowed_exceptions_are_reasoned():
    """허용 예외는 사유 없이 등재할 수 없다(빈 사유 = 사실상 무제한 예외)."""
    for key, reason in ALLOWED_CONSOLE_JSON.items():
        assert isinstance(reason, str) and reason.strip(), key


@pytest.mark.parametrize(
    ("module", "current", "reverted", "shape"),
    (
        (
            "pm_log",
            "        _write_machine_line(\n"
            "            json.dumps(payload, ensure_ascii=True, separators=(\",\", \":\"))\n"
            "        )\n",
            "        print(json.dumps(payload, ensure_ascii=True, separators=(\",\", \":\")))\n",
            PRINT_SHAPE,
        ),
        (
            "delegate_channel_guard",
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
        ),
    ),
)
def test_guard_turns_red_when_one_call_site_is_reverted(
    tmp_path, module, current, reverted, shape
):
    """감도 실증 — 호출부 **한 곳**을 종전 형태로 되돌리면 AST·텍스트 두 측정이 함께 red."""
    tools = tmp_path / "tools"
    tools.mkdir()
    source = (TOOLS / f"{module}.py").read_text(encoding="utf-8")
    assert source.count(current) == 1, "변이 앵커 소실"
    (tools / f"{module}.py").write_text(
        source.replace(current, reverted, 1), encoding="utf-8", newline="\n"
    )

    _scanned, violations = scan_console_json(tools)

    assert [violation for violation in violations if violation.endswith(shape)]
    assert text_scan_candidates(tools)  # 독립 텍스트 측정도 같은 복원을 본다.


def test_guard_ignores_mentions_in_comments_and_strings(tmp_path):
    """주석·문자열의 형태 언급은 세지 않는다(AST 판정 · 문서화 자유)."""
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "prose.py").write_text(
        '"""print(json.dumps(payload)) 는 콘솔 코덱을 탄다."""\n'
        "# json.dump(payload, sys.stdout)\n"
        'DOC = "sys.stdout.write(json.dumps(x))"\n',
        encoding="utf-8",
        newline="\n",
    )

    _scanned, violations = scan_console_json(tools)

    assert violations == ()
