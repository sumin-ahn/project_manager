"""어댑터 공개 ctx·원칙·relay API 의 패키지 소유 계약.

Python 어댑터가 쓰는 `pm_engine.cli.hooks` 의 ctx 판정(`context_status`·`judge_context`)·원칙 recall·
세션 1회 안내·relay 구성과 `board.py hook-context`·`hook-tool` 기계 JSON 진입을 실제 소유자
(domain.ctx_recovery·app.ctx_hooks·infra.compaction_store·principle_store·app.session.relay)로 검증한다.
적용 기록은 고정 모델 한도 fixture(`install_ctx_record`)로 만들고 하네스·네트워크는 부르지 않는다.
"""
from __future__ import annotations

import importlib.util
import io
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from _textio import utf8_child_env
from pm_engine.app.session import relay
from pm_engine.cli import hooks
from pm_engine.domain import conf_schema, ctx_recovery
from pm_engine.infra import compaction_store, principle_store, raw_output
from pm_engine.infra.harness import contract, watchdog
from test_harness_ctx_limits import install_ctx_record

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
TEMPLATES = REPO / "templates"
# 기본 입력 기록 — 사용 한도 200,000 · 중간 기록 권고 70% · 압축 임박 경고 80%.
E = 200_000
THRESHOLDS = {"checkpoint_at_pct": 70, "precompact_at_pct": 80}
# 디렉터리 자리를 파일로 열 때 올라오는 오류 타입 — Windows(CreateFileW)는 PermissionError, 그 밖은 IsADirectoryError.
DIRECTORY_OPEN_ERROR = PermissionError if sys.platform == "win32" else IsADirectoryError
STATUS_KEYS = {"version", "status", "measured", "used_pct", "level", "notice"}
DECISION_KEYS = STATUS_KEYS | {"guidance", "emit", "compaction_new", "compaction_guidance",
                               "compaction_boundary_id"}
RULES = (
    "#### 번들\n"
    "- `[shell: git\\s+push\\s+--force]` 강제 push 는 경고 신호다. 어기면 원격 히스토리가 덮인다.\n"
    "- `[edit: (^|/)tests?/]` 테스트 파일 편집이다. 어기면 회귀가 비활성화될 수 있다.\n"
)


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _root(tmp_path: Path, name: str = "home", **record) -> Path:
    root = tmp_path / name
    install_ctx_record(root, **record)
    return root


def _markers(root: Path) -> dict[str, bytes]:
    directory = root / ".project_manager" / ".local" / "ctx-stop"
    return {path.name: path.read_bytes() for path in sorted(directory.iterdir())} if directory.exists() else {}


# 이 시험의 데이터 Home 쓰기는 `.local` 아래 이 깊이 안에 있다(marker 폴더·기록 파일).
_LOCAL_SNAPSHOT_DEPTH = 4


def _local_files(root: Path) -> dict[str, bytes]:
    local = root / ".project_manager" / ".local"
    paths = (path for depth in range(1, _LOCAL_SNAPSHOT_DEPTH + 1) for path in local.glob("/".join(["*"] * depth)))
    return {str(path.relative_to(local)): path.read_bytes() for path in sorted(paths) if path.is_file()}


def _judge(root: Path, tokens: int, *, session: str = "s-1", harness: str = "claude", **extra) -> dict:
    return hooks.judge_context(hooks.adapter_home(root), harness=harness, session=session, tokens=tokens,
                               model=None, main_thread=extra.pop("main_thread", True), **extra)


def _install_board(root: Path) -> Path:
    board = root / ".project_manager" / "tools" / "board.py"
    shutil.copyfile(TOOLS / "board.py", board)
    return board


def _run_board(board: Path, argv: list[str], stdin: bytes) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(board), *argv], input=stdin, capture_output=True,
                          env=utf8_child_env(), timeout=120)


def test_band_rounding_table(tmp_path):
    """쓴 % 는 반올림(동률 짝수)·0~100 제한, 밴드 경계는 포함, post-turn 회전은 원시 정수 곱이다."""
    rounding = {(1, 200): 0, (3, 200): 2, (5, 200): 2, (139, 200): 70, (141, 200): 70, (143, 200): 72,
                (400_000, E): 100, (0, E): 0, (-5, E): 0, (15_328, 0): 0, (15_328, 20_000): 77}
    assert {pair: ctx_recovery.context_used_pct(*pair) for pair in rounding} == rounding
    table = {69: "ok", 70: "nudge", 76: "nudge", 77: "nudge2", 79: "nudge2", 80: "stop", 100: "stop"}
    assert {used: ctx_recovery.classify(used, THRESHOLDS) for used in table} == table
    assert ctx_recovery.CTX_NUDGE2_MARGIN_PCT == 3
    assert ctx_recovery.nudge2_threshold({"checkpoint_at_pct": 79, "precompact_at_pct": 80}) == 79
    # 반올림 백분율은 80% 최종 밴드여도 원시 정수 곱 795*100 < 1000*80 이라 회전하지 않는다.
    assert ctx_recovery.classify(ctx_recovery.context_used_pct(795, 1000), THRESHOLDS) == "stop"
    assert [ctx_recovery.rotation_due(tokens, 1000, 80) for tokens in (795, 799, 800, 0)] == [
        False, False, True, False]
    assert ctx_recovery.rotation_due(900, 0, 80) is False
    marker_root = tmp_path / "relay"
    assert relay.mark_ctx_post_turn_if_over(marker_root, "sid", 799, 1000, 80) is False
    assert relay.stop_marker_present(marker_root, "sid") is False
    assert relay.mark_ctx_post_turn_if_over(marker_root, "sid", 800, 1000, 80) is True
    # 공개 API 도 같은 표 — 기록 E=200,000 에서 69.5% 는 70%(nudge), 70.5% 도 70%.
    home = hooks.adapter_home(_root(tmp_path))
    for tokens, used, level in ((139_000, 70, "nudge"), (141_000, 70, "nudge"), (153_000, 76, "nudge"),
                                (154_000, 77, "nudge2"), (159_000, 80, "stop"), (500_000, 100, "stop")):
        status = hooks.context_status(home, harness="claude", tokens=tokens, model=None)
        assert (status["used_pct"], status["level"]) == (used, level), tokens


def test_statusline_is_read_only(tmp_path):
    """status 조회는 6키 값만 내고 marker·seen·기록 파일을 하나도 바꾸지 않는다."""
    root = _root(tmp_path)
    home = hooks.adapter_home(root)
    marker_dir = root / ".project_manager" / ".local" / "ctx-stop"
    marker_dir.mkdir(parents=True)
    for kind in ("nudge", "nudge2", "final"):
        (marker_dir / f"s-1.{kind}").write_bytes(f"fired-{kind}\n".encode())
    before = _local_files(root)
    expected = {0: (False, 0, "ok"), 100_000: (True, 50, "ok"), 150_000: (True, 75, "nudge"),
                190_000: (True, 95, "stop")}
    for tokens, (measured, used, level) in expected.items():
        status = hooks.context_status(home, harness="claude", tokens=tokens, model=None)
        assert status == {"version": 1, "status": "ok", "measured": measured, "used_pct": used,
                          "level": level, "notice": None}
    statusline = _load(TEMPLATES / "claude_code" / ".claude" / "ctx_statusline.py", "s10_statusline")
    line = statusline.build_statusline({"context_window": {"current_usage": {"input_tokens": 100_000}}},
                                       home=home)
    assert "ctx 50%" in line
    assert _local_files(root) == before
    (root / ".project_manager" / ".local" / "ctx-budget.json").unlink()
    unavailable = hooks.context_status(home, harness="claude", tokens=150_000, model=None)
    assert set(unavailable) == STATUS_KEYS
    assert (unavailable["status"], unavailable["measured"], unavailable["used_pct"], unavailable["level"]) == (
        "unavailable", True, None, None)
    assert "`pm_config.py apply`" in unavailable["notice"]
    assert _markers(root) == {f"s-1.{kind}": f"fired-{kind}\n".encode() for kind in ("nudge", "nudge2", "final")}


def test_nudge2_and_final_are_independent(tmp_path):
    """nudge·nudge2·final 은 사이클당 각자 한 번 — 앞 밴드 발화가 뒤 밴드를 소모하지 않고 건너뛴 밴드도 낸다."""
    root = _root(tmp_path)
    first = _judge(root, 156_000)
    assert set(first) == DECISION_KEYS
    assert (first["level"], first["emit"]) == ("nudge2", True)
    assert first["guidance"] == ctx_recovery.build_ctx_guard_guidance("nudge2", used_pct=78, precompact_at_pct=80)
    assert _judge(root, 156_000)["emit"] is False
    final = _judge(root, 190_000)
    assert (final["level"], final["emit"]) == ("stop", True)
    assert final["guidance"] == ctx_recovery.build_ctx_guard_guidance("final", used_pct=95, precompact_at_pct=80)
    assert _judge(root, 190_000) == {**final, "guidance": None, "emit": False}
    assert sorted(_markers(root)) == ["s-1.final", "s-1.nudge2"]
    skipped = _judge(root, 190_000, session="s-2")
    assert skipped["emit"] is True and sorted(_markers(root)) == ["s-1.final", "s-1.nudge2", "s-2.final"]


def test_measured_ok_and_zero_rearm(tmp_path):
    """양수 ok 실측만 밴드 marker 를 재무장하고, 미측정 0 은 보존한다 — 같은 코드의 두 Home 은 서로 섞이지 않는다."""
    first, second = _root(tmp_path, "first"), _root(tmp_path, "second")
    assert _judge(first, 150_000)["emit"] is True
    assert _judge(second, 150_000)["emit"] is True  # 다른 Home 의 marker 를 공유하지 않는다.
    unmeasured = _judge(first, 0)
    assert (unmeasured["measured"], unmeasured["used_pct"], unmeasured["level"], unmeasured["emit"]) == (
        False, 0, "ok", False)
    assert sorted(_markers(first)) == ["s-1.nudge"]
    assert _judge(first, 150_000)["emit"] is False
    assert _judge(first, 4_000)["level"] == "ok"
    assert _markers(first) == {}
    assert sorted(_markers(second)) == ["s-1.nudge"]
    assert _judge(first, 150_000)["emit"] is True and _judge(second, 150_000)["emit"] is False


_SUBPROCESS_JUDGE = """
import json, sys
sys.path.insert(0, sys.argv[1])
from pm_engine.cli import hooks
boundary = sys.argv[4] if sys.argv[4] != "-" else None
print(json.dumps(hooks.judge_context(hooks.adapter_home(sys.argv[2]), harness="claude", session="s-1",
                                     tokens=int(sys.argv[3]), model=None, main_thread=True,
                                     boundary=boundary)))
"""


def _judge_in_process(root: Path, tokens: int, boundary: str | None) -> dict:
    completed = subprocess.run(
        [sys.executable, "-c", _SUBPROCESS_JUDGE, str(root / ".project_manager" / "tools"), str(root),
         str(tokens), boundary or "-"],
        capture_output=True, text=True, encoding="utf-8", env=utf8_child_env(), timeout=120, check=True,
    )
    return json.loads(completed.stdout)


def test_durable_compaction_is_idempotent(tmp_path, monkeypatch):
    """다른 프로세스가 선점한 같은 세션 밴드·경계는 재발화하지 않고, 새 경계만 처리하며 예산 불능에서도 처리한다."""
    root = _root(tmp_path)
    home = hooks.adapter_home(root)
    assert _judge_in_process(root, 150_000, None)["emit"] is True
    assert _judge(root, 150_000)["emit"] is False
    principle_store.record_seen_marker(home, "s-1", ["shell:git"])
    child = _judge_in_process(root, 0, "1")
    assert (child["compaction_new"], child["compaction_boundary_id"]) == (True, "claude-s-1-1")
    assert child["compaction_guidance"] == ctx_recovery.build_ctx_guard_guidance("precompact")
    assert sorted(_markers(root)) == ["s-1.compact-1"]  # 밴드 marker 셋 재무장.
    assert principle_store.load_seen_marker(home, "s-1") == set()
    same = _judge(root, 0, boundary="1")
    assert (same["compaction_new"], same["compaction_guidance"], same["compaction_boundary_id"]) == (
        False, None, None)
    assert _judge(root, 150_000, boundary="1")["emit"] is True  # 재무장 뒤 같은 사이클의 다음 상승.

    def broken_rearm(_home, _session):
        raise PermissionError("seen marker 삭제 불가")

    with monkeypatch.context() as patch:
        patch.setattr(principle_store, "rearm_seen_marker", broken_rearm)
        with pytest.raises(PermissionError, match="seen marker 삭제 불가"):
            _judge(root, 0, boundary="2")
    assert "s-1.compact-2" not in _markers(root)  # 실패한 경계는 다음 호출이 다시 처리한다.
    (root / ".project_manager" / ".local" / "ctx-budget.json").unlink()
    retried = _judge(root, 150_000, boundary="2")
    assert (retried["status"], retried["emit"], retried["compaction_new"]) == ("unavailable", False, True)
    opaque = _judge(root, 0, harness="opencode", session="ses_1", boundary="20261027T000000Z-uuid/ses_1")
    assert opaque["compaction_boundary_id"] == "20261027T000000Z-uuid/ses_1"
    assert "ses_1.compact-20261027T000000Z-uuidses_1" in _markers(root)
    subagent = _judge(root, 190_000, main_thread=False, boundary="9")
    assert subagent == {"version": 1, "status": "ok", "measured": False, "used_pct": None, "level": None,
                        "notice": None, "guidance": None, "emit": False, "compaction_new": False,
                        "compaction_guidance": None, "compaction_boundary_id": None}
    assert "s-1.compact-9" not in _markers(root)


def _hook_context(board: Path, payload, *, session: str = "S") -> subprocess.CompletedProcess:
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
    return _run_board(board, ["hook-context", "--harness", "opencode", "--session", session], body)


def _hook_tool(board: Path, payload) -> subprocess.CompletedProcess:
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
    return _run_board(board, ["hook-tool", "--session", "git-root"], body)


def test_config_budget_and_wire_errors_are_visible(tmp_path, monkeypatch):
    """형식 오류는 부작용 전에 거부하고, 구표기·판독 불능·렌더 오류는 예외로, 기록 문제는 notice 로 드러난다."""
    root = _root(tmp_path, harness="opencode")
    home = hooks.adapter_home(root)
    for kwargs, error in (({"tokens": True}, TypeError), ({"tokens": -1}, ValueError),
                          ({"tokens": 1.0}, TypeError), ({"harness": "gemini"}, ValueError),
                          ({"model": 3}, TypeError), ({"main_thread": "yes"}, TypeError),
                          ({"boundary": ""}, ValueError), ({"session": None}, TypeError)):
        call = {"harness": "opencode", "session": "s", "tokens": 1, "model": None, "main_thread": True,
                **kwargs}
        with pytest.raises(error):
            hooks.judge_context(home, **call)
    assert _markers(root) == {}

    def broken_render(band, **_kwargs):
        raise RuntimeError(f"렌더 실패 {band}")

    with monkeypatch.context() as patch:
        patch.setattr(ctx_recovery, "build_ctx_guard_guidance", broken_render)
        with pytest.raises(RuntimeError, match="렌더 실패 nudge"):
            _judge(root, 150_000, harness="opencode")
    assert _markers(root) == {}  # 렌더 실패는 밴드 marker 를 소비하지 않는다.

    board = _install_board(root)
    good = _hook_context(board, {"version": 1, "tokens": 150_000, "model": None, "main_thread": True,
                                 "boundary": None})
    assert good.returncode == 0, good.stderr.decode("utf-8", "replace")
    assert good.stdout.endswith(b"\n") and good.stdout.count(b"\n") == 1
    decision = json.loads(good.stdout)
    assert set(decision) == DECISION_KEYS and (decision["level"], decision["emit"]) == ("nudge", True)
    for bad in ({"version": 1, "tokens": True, "model": None, "main_thread": True, "boundary": None},
                {"version": 1, "tokens": 1, "model": None, "main_thread": True, "boundary": None, "x": 1},
                {"version": 2, "tokens": 1, "model": None, "main_thread": True, "boundary": None},
                {"version": 1, "tokens": 1, "model": None, "main_thread": 1, "boundary": None},
                {"version": 1, "tokens": 1, "model": None, "main_thread": True, "boundary": ""},
                b'{"version": 1, "tokens": 1, "model": "\xff", "main_thread": true, "boundary": null}'):
        refused = _hook_context(board, bad, session="wire")
        assert (refused.returncode, refused.stdout) == (2, b""), bad
        assert "[hook-context] 입력 형식 오류" in refused.stderr.decode("utf-8")
    assert not any(name.startswith("wire.") for name in _markers(root))

    missing = root / ".project_manager" / ".local" / "ctx-budget.json"
    record = missing.read_bytes()
    missing.unlink()
    notice = json.loads(_hook_context(board, {"version": 1, "tokens": 190_000, "model": None,
                                              "main_thread": True, "boundary": None}, session="n").stdout)
    assert (notice["status"], notice["emit"]) == ("unavailable", False) and "`pm_config.py apply`" in notice["notice"]
    missing.write_bytes(record)

    conf = root / ".project_manager" / "local.conf"
    conf_text = conf.read_text(encoding="utf-8")
    conf.write_text(conf_text + "ctx_window_tokens=12345\n", encoding="utf-8")
    with pytest.raises(conf_schema.LegacyConfKeyError):
        _judge(root, 150_000, harness="opencode", session="legacy")
    conf.unlink()
    conf.mkdir()
    with pytest.raises(DIRECTORY_OPEN_ERROR):
        _judge(root, 150_000, harness="opencode", session="io")
    failed = _hook_context(board, {"version": 1, "tokens": 150_000, "model": None, "main_thread": True,
                                   "boundary": None}, session="io")
    assert (failed.returncode, failed.stdout) == (1, b"")
    assert DIRECTORY_OPEN_ERROR.__name__ in failed.stderr.decode("utf-8", "replace")
    conf.rmdir()
    conf.write_text(conf_text, encoding="utf-8")

    wiki = root / ".project_manager" / "wiki"
    wiki.mkdir()
    (wiki / "pm_principles.md").write_text(RULES, encoding="utf-8")
    recall = {"session": "ses-p", "on": "edit", "text": "tests/test_x.py", "main_thread": True}
    deny_context = {"session": "ses-deny", "tokens": 190_000, "model": None, "main_thread": True, "boundary": None}
    denied = json.loads(_hook_tool(board, {
        "version": 1, "cwd": str(root), "tool_name": "write",
        "tool_input": {"filePath": "tests/test_x.py", "content": "git 허용: git push\n"},
        "shell_command": None, "recall": recall, "context": deny_context}).stdout)
    assert denied["git"]["verdict"] == "deny"
    assert (denied["recall"], denied["recall_error"]) == (None, None)
    assert (denied["context"], denied["context_error"]) == (None, None)
    assert not any(name.startswith("ses-deny.") for name in _markers(root))  # 거부한 호출은 ctx marker 도 쓰지 않는다.
    assert principle_store.load_seen_marker(home, "ses-p") == set()  # 거부한 입력은 seen 을 소비하지 않는다.
    allowed = _hook_tool(board, {"version": 1, "cwd": str(root), "tool_name": "read",
                                 "tool_input": {"filePath": "tests/test_x.py"}, "shell_command": None,
                                 "context": None, "recall": recall})
    assert allowed.returncode == 0 and allowed.stdout.count(b"\n") == 1
    allowed_json = json.loads(allowed.stdout)
    assert set(allowed_json) == {"version", "git", "context", "context_error", "recall", "recall_error"}
    assert (allowed_json["context"], allowed_json["context_error"]) == (None, None)
    assert allowed_json["git"]["verdict"] == "ok" and allowed_json["recall"]["keys"] == ["edit:(^|/)tests?/"]
    assert principle_store.load_seen_marker(home, "ses-p") == {"edit:(^|/)tests?/"}
    (wiki / "pm_principles.local.md").mkdir()
    broken = json.loads(_hook_tool(board, {"version": 1, "cwd": str(root), "tool_name": "read",
                                           "tool_input": {}, "shell_command": None, "context": None,
                                           "recall": {**recall, "session": "ses-q"}}).stdout)
    assert broken["git"]["verdict"] == "ok" and broken["recall"] is None
    assert broken["recall_error"].startswith(f"{DIRECTORY_OPEN_ERROR.__name__}: ")
    mismatch = _hook_tool(board, {"version": 1, "cwd": str(root), "tool_name": "bash",
                                  "tool_input": {"command": "ls"}, "shell_command": "pwd", "context": None,
                                  "recall": None})
    assert (mismatch.returncode, mismatch.stdout) == (2, b"")
    assert "[hook-tool] 입력 형식 오류" in mismatch.stderr.decode("utf-8")

    # ctx 를 실은 요청은 같은 Home 의 공개 ctx API 와 같은 11키를 싣는다(첫 발화끼리 비교).
    observed = {"session": "ses-c", "tokens": 150_000, "model": None, "main_thread": True, "boundary": None}
    with_context = json.loads(_hook_tool(board, {"version": 1, "cwd": str(root), "tool_name": "read",
                                                 "tool_input": {}, "shell_command": None,
                                                 "context": observed, "recall": None}).stdout)
    assert with_context["context_error"] is None
    assert with_context["context"] == _judge(root, 150_000, harness="opencode", session="ses-c2")
    assert with_context["context"]["emit"] is True and {"ses-c.nudge", "ses-c2.nudge"} <= set(_markers(root))
    # ctx 설정 IO 실패는 git 판정을 보존한 채 context_error 로 드러나고, 형식 오류는 부작용 전에 rc 2 다.
    conf.unlink()
    conf.mkdir()
    io_failed = json.loads(_hook_tool(board, {"version": 1, "cwd": str(root), "tool_name": "read",
                                              "tool_input": {}, "shell_command": None,
                                              "context": {**observed, "session": "ses-io"}, "recall": None}).stdout)
    assert io_failed["git"]["verdict"] == "ok" and io_failed["context"] is None
    assert io_failed["context_error"].startswith(f"{DIRECTORY_OPEN_ERROR.__name__}: ")
    conf.rmdir()
    conf.write_text(conf_text, encoding="utf-8")
    for bad_context in ({**observed, "tokens": True}, {**observed, "extra": 1}, {**observed, "boundary": ""},
                        {key: observed[key] for key in ("session", "tokens", "model", "main_thread")}):
        refused = _hook_tool(board, {"version": 1, "cwd": str(root), "tool_name": "read", "tool_input": {},
                                     "shell_command": None, "context": bad_context, "recall": None})
        assert (refused.returncode, refused.stdout) == (2, b""), bad_context
    old_shape = _hook_tool(board, {"version": 1, "cwd": str(root), "tool_name": "read", "tool_input": {},
                                   "shell_command": None, "recall": None})
    assert (old_shape.returncode, old_shape.stdout) == (2, b"")  # context 키 없는 이전 형식은 받지 않는다.


def test_principle_markers_and_errors_are_visible(tmp_path, capsys):
    """원칙 count·keys·text·broken 과 seen·재무장·서브에이전트 면제, 실제 IO 불능, 안내 1회 3상태를 보인다."""
    root = tmp_path / "home"
    home = hooks.adapter_home(root)
    wiki = root / ".project_manager" / "wiki"
    wiki.mkdir(parents=True)
    (wiki / "pm_principles.md").write_text(RULES + "- `[shell: (unclosed]` 파손 항목.\n", encoding="utf-8")
    call = {"session": "ses-1", "on": "shell", "text": "git push --force origin main"}
    assert hooks.recall_principles(home, **call, main_thread=False) is None
    assert not (root / ".project_manager" / ".local").exists()
    first = hooks.recall_principles(home, **call, main_thread=True)
    assert (first["count"], first["keys"], first["broken"]) == (1, ["shell:git\\s+push\\s+--force"], 1)
    assert "강제 push" in first["text"] and "파손 항목 1건" in first["text"]
    again = hooks.recall_principles(home, **call, main_thread=True)
    assert (again["count"], again["keys"], again["broken"]) == (0, [], 1)
    hooks.rearm_principles(home, session="ses-1")
    assert hooks.recall_principles(home, **call, main_thread=True)["count"] == 1
    (wiki / "pm_principles.md").write_text(RULES, encoding="utf-8")
    assert hooks.recall_principles(home, session="ses-2", on="shell", text="ls", main_thread=True) is None
    with pytest.raises(ValueError):
        hooks.recall_principles(home, session="ses-2", on="read", text="x", main_thread=True)

    seen = root / ".project_manager" / ".local" / "principle-recall" / "ses-3.json"
    seen.mkdir(parents=True)
    with pytest.raises(DIRECTORY_OPEN_ERROR):
        hooks.recall_principles(home, session="ses-3", **{k: call[k] for k in ("on", "text")}, main_thread=True)
    (wiki / "pm_principles.local.md").mkdir()
    with pytest.raises(DIRECTORY_OPEN_ERROR):
        hooks.recall_principles(home, session="ses-4", **{k: call[k] for k in ("on", "text")}, main_thread=True)
    (wiki / "pm_principles.local.md").rmdir()

    # Claude 훅은 원칙 채널 실패를 원인 있는 경고로 합치고 이미 얻은 ctx 안내를 버리지 않는다.
    install_ctx_record(root)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(json.dumps({"type": "assistant", "message": {
        "role": "assistant", "usage": {"input_tokens": 190_000}}}) + "\n", encoding="utf-8")
    stop_hook = _load(TEMPLATES / "claude_code" / ".claude" / "ctx_stop_hook.py", "s10_stop_hook")
    rc, output = stop_hook.evaluate({"hook_event_name": "PreToolUse", "session_id": "ses-3",
                                     "tool_name": "Bash", "tool_input": {"command": "git push --force"},
                                     "transcript_path": str(transcript)}, home)
    context = output["hookSpecificOutput"]["additionalContext"]
    assert rc == 0 and "[ctx-nudge/최종]" in context and f"[principle-recall] 판정 실패({DIRECTORY_OPEN_ERROR.__name__}" in context

    assert hooks.claim_notice(home, session="ses-1", text="[git-anchor/warn] 같은 말") == {"status": "claimed"}
    assert hooks.claim_notice(home, session="ses-1", text="[git-anchor/warn] 같은 말") == {"status": "seen"}
    assert hooks.claim_notice(home, session="ses-2", text="[git-anchor/warn] 같은 말") == {"status": "claimed"}
    membership = root / ".project_manager" / ".local" / "ctx-stop" / "ses-9.git-anchor"
    membership.mkdir(parents=True)
    assert hooks.claim_notice(home, session="ses-9", text="x") == {"status": "unavailable"}


class _CaptureSupervisor:
    built: list = []

    def __init__(self, driver, *, root, task=None):
        self.built.append((driver, root, task))

    def run_loop(self, cwd, in_stream, out_stream):
        return 7


def _drivers():
    return {
        "claude": _load(TEMPLATES / "claude_code" / ".claude" / "pm_orch_claude.py", "s10_claude").ClaudeCliDriver,
        "codex": _load(TEMPLATES / "codex" / ".codex" / "pm_orch_codex.py", "s10_codex").CodexCliDriver,
        "opencode": _load(TEMPLATES / "opencode" / ".opencode" / "pm_orch_opencode.py",
                          "s10_opencode").OpencodeCliDriver,
    }


def test_relay_factories_receive_real_callables_and_values(tmp_path, monkeypatch, capsys):
    """세 native driver 클래스가 실제 파서·SpawnResult·예산·데이터 루트·실행기·워치독·writer 를 받는다."""
    monkeypatch.setattr(relay, "Supervisor", _CaptureSupervisor)
    _CaptureSupervisor.built = []
    drivers = _drivers()
    built = {}
    for harness, factory in drivers.items():
        root = _root(tmp_path, harness, harness=harness)
        home = hooks.adapter_home(root)
        rc = hooks.run_relay(home, factory, harness=harness, task=f"task-{harness}", cwd=str(tmp_path),
                             model="m-1" if harness == "claude" else None,
                             agent="pm" if harness == "opencode" else None, stdin=io.StringIO(""),
                             stdout=io.StringIO())
        driver, supervisor_root, task = _CaptureSupervisor.built[-1]
        assert (rc, supervisor_root, task) == (7, root, f"task-{harness}")
        assert isinstance(driver, factory)
        budget, pct, _warning = relay.ctx_budget(home, harness, None)
        assert (driver._ctx_budget, driver._precompact_at_pct, driver._root) == (budget, pct, root)
        assert driver._spawn_result is relay.SpawnResult
        built[harness] = driver
    claude, codex, opencode = built["claude"], built["codex"], built["opencode"]
    assert claude._parse is contract.parse_stream_json and claude.model == "m-1"
    assert claude.runner is hooks._resolving_runner and claude._mark_stop.func is relay.mark_ctx_post_turn_if_over
    assert claude._budget_reader(None) == relay.ctx_budget(hooks.adapter_home(tmp_path / "claude"), "claude", None)
    assert codex._parse is contract.parse_codex_json and codex.runner is hooks._resolving_runner
    assert codex._mark_ctx_if_over is relay.mark_ctx_post_turn_if_over
    assert opencode._parse is contract.parse_opencode_json and opencode.agent == "pm"
    assert opencode.runner is hooks._watchdog_runner
    assert opencode._stall_error_types == (watchdog.StallWatchdogError,)
    assert (opencode._temp_root, opencode._attached_message, opencode._assert_prompt_in_cwd) == (
        raw_output.temp_root, contract.OPENCODE_ATTACHED_MSG, contract.assert_opencode_prompt_in_cwd)

    # 원시 정수 회전 경계: 임계 - 1 은 marker 없음, 임계는 marker.
    threshold = claude._ctx_budget * claude._precompact_at_pct // 100
    claude._maybe_mark_ctx("sid-under", threshold - 1)
    claude._maybe_mark_ctx("sid-at", threshold)
    assert relay.stop_marker_present(tmp_path / "claude", "sid-under") is False
    assert relay.stop_marker_present(tmp_path / "claude", "sid-at") is True
    codex._maybe_mark_ctx("tid-at", {"input": codex._ctx_budget * codex._precompact_at_pct // 100, "output": 0})
    assert relay.stop_marker_present(tmp_path / "codex", "tid-at") is True
    capsys.readouterr()

    # 실제 writer 가 기록하지 못하면 하네스마다 원인을 알리고, 임계 미달은 조용하다.
    monkeypatch.setattr(relay, "write_post_turn_marker", lambda *_args: False)
    opencode._maybe_mark_ctx("ses-under", opencode._ctx_budget * opencode._precompact_at_pct // 100 - 1)
    assert capsys.readouterr().err == ""
    opencode._maybe_mark_ctx("ses-at", opencode._ctx_budget * opencode._precompact_at_pct // 100)
    codex._maybe_mark_ctx("tid-fail", {"input": codex._ctx_budget, "output": 0})
    assert capsys.readouterr().err == (
        "[pm-orch] opencode ctx marker 박제 실패\n[pm-orch] codex ctx marker 박제 실패\n")
    with pytest.raises(ValueError, match="지원하지 않는 하네스"):
        hooks.run_relay(hooks.adapter_home(tmp_path / "claude"), drivers["claude"], harness="gemini",
                        task="t", cwd=str(tmp_path), stdin=None, stdout=None)
