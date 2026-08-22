"""T-0596 — 위임 채널 실패의 **무음 대체 금지**(fail-loud rc · 대체 시도 0 · 장부 실패 기록).

실사고: codex 세션에서 claude 위임이 실패하자 그 세션의 native GPT 가 위임 대상 작업을 조용히
대행했다 — 장부엔 대행 기록이 없어 사후에 누가 무엇을 했는지 재구성할 수 없었다. 실패 안내가
"네이티브/다른 하네스로 재시도를 검토하세요"였던 것이 그 입구였다.

이 파일이 고정하는 계약(전부 mock·run_fn DI — 실 하네스 스폰 0):
  ① 채널 실행 실패(스폰 실패·비정상 rc·타임아웃)는 **rc=1 fail-loud** 로 끝난다.
  ② 폴백 미설정이면 **대체 시도 0** — 그 호출의 run_fn 호출 수는 정확히 1이다.
  ③ 실패 안내는 다른 수신자를 권하지 않고(옛 문구 소멸) 명시 재호출만 지시한다.
  ④ 실패는 raw 장부의 그 레코드에 rc 와 함께 **마감 기록으로 잔존**한다(조회 가능).
  ⑤ 명시 fallback tuple 경로는 불변 — 인프라 실패 1회 loud 폴백은 그대로 동작한다
     (금지 대상은 "명시 설정 없는 자동 대체"이지 선언된 폴백이 아니다).
"""
from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PM_DELEGATE = REPO / ".project_manager" / "tools" / "pm_delegate.py"

# 제거된 옛 안내 문구 — 이 문자열이 stderr 에 다시 나오면 무음 대체의 입구가 되살아난 것이다.
_SUBSTITUTION_INVITATION = "네이티브/다른 하네스로 재시도"
# Codex sandbox 의 네트워크 차단 마커 — 스폰 전 차단 경로의 유일한 판정 입력.
_EGRESS_MARKER = "CODEX_SANDBOX_NETWORK_DISABLED"


@pytest.fixture()
def pd():
    spec = importlib.util.spec_from_file_location("pm_delegate_t0596", PM_DELEGATE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _neutral_codex_egress_marker(monkeypatch):
    """ambient Codex egress 마커 중화 — Codex 세션에서 돌려도 다른 케이스가 게이트에 안 걸린다."""
    monkeypatch.delenv(_EGRESS_MARKER, raising=False)


class _FakeRun:
    """run_fn seam stub — 호출을 기록하고 canned 결과를 낸다(호출 수 = 대체 시도 관측치)."""

    def __init__(self, *, stdout: str = "", returncode: int = 0,
                 stderr: str = "", timed_out: bool = False, raises: BaseException | None = None):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr
        self.timed_out = timed_out
        self.raises = raises
        self.calls: list[dict] = []

    def __call__(self, argv, *, stdin_text, cwd, env, timeout, harness):
        self.calls.append({"argv": argv, "harness": harness, "timeout": timeout})
        if self.raises is not None:
            raise self.raises
        return {"returncode": self.returncode, "stdout": self.stdout,
                "stderr": self.stderr, "timed_out": self.timed_out}


class _SequenceRun:
    """호출 순서대로 다른 결과를 내는 stub — primary 실패 → 폴백 성공 시나리오용."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls: list[dict] = []

    def __call__(self, argv, *, stdin_text, cwd, env, timeout, harness):
        self.calls.append({"argv": argv, "harness": harness})
        outcome = self.results.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _codex_stdout(reply: str = "DONE") -> str:
    return "\n".join([
        json.dumps({"type": "thread.started", "thread_id": "th1"}),
        json.dumps({"type": "item.completed",
                    "item": {"type": "agent_message", "text": reply}}),
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 3}}),
    ])


def _claude_stdout(reply: str = "DONE") -> str:
    return json.dumps({"type": "result", "result": reply, "session_id": "s-fb"})


def _conf(**extra) -> dict[str, str]:
    conf = {
        "delegate.enabled": "true",
        "delegate.developer.harness": "codex",
        "delegate.developer.model": "gpt-x",
    }
    conf.update(extra)
    return conf


def _write_prompt(tmp_path: Path) -> Path:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("티켓 본문: 구현하라.", encoding="utf-8")
    return prompt


def _run(pd, monkeypatch, tmp_path, fake, conf=None) -> tuple[int, Path]:
    """mock 위임 1회 실행 — (rc, 장부 경로). raw/장부는 tmp output-dir 로 격리한다."""
    monkeypatch.setattr(pd, "local_config", lambda: conf if conf is not None else _conf())
    monkeypatch.setattr(pd, "_cwd_in_git_repo", lambda *a, **k: True)
    outdir = tmp_path / "raw"
    rc = pd.main(
        ["--role", "developer", "--prompt-file", str(_write_prompt(tmp_path)),
         "--cwd", str(tmp_path), "--output-dir", str(outdir)],
        run_fn=fake,
    )
    return rc, outdir / "raw_outputs.json"


def _ledger_rows(ledger: Path) -> list[dict]:
    return json.loads(ledger.read_text(encoding="utf-8"))["records"]


# ════════════════════════════════════════════════════════════════════════
# ①~④ 폴백 미설정 — 실패는 fail-loud rc · 대체 시도 0 · 장부 잔존
# ════════════════════════════════════════════════════════════════════════

def test_spawn_failure_fails_loud_with_no_substitute_attempt(pd, monkeypatch, tmp_path, capsys):
    """스폰 실패(바이너리 부재) → rc=1 · run_fn 호출 1회 · 다른 하네스 시도 0."""
    fake = _FakeRun(raises=FileNotFoundError("codex: not found"))

    rc, ledger = _run(pd, monkeypatch, tmp_path, fake)

    err = capsys.readouterr().err
    assert rc == 1, "스폰 실패가 fail-loud rc 로 끝나지 않음"
    assert len(fake.calls) == 1, f"같은 호출 안에서 재시도/대체가 일어남: {fake.calls}"
    assert {call["harness"] for call in fake.calls} == {"codex"}, "다른 하네스로 대체 시도됨"
    assert "재위임은 명시 재호출만" in err, f"무음 대체 금지 안내 누락:\n{err}"
    assert _SUBSTITUTION_INVITATION not in err, f"대체 권유 문구 재발:\n{err}"
    # 실행 전 예약된 레코드가 마감되어 실패가 장부에 남는다(rc=127=launch 실패 정규화값).
    rows = _ledger_rows(ledger)
    assert len(rows) == 1 and rows[0]["finished_at"] is not None
    assert rows[0]["rc"] == 127, f"장부에 실패 rc 가 안 남음: {rows[0]}"


def test_nonzero_rc_fails_loud_and_keeps_failure_record(pd, monkeypatch, tmp_path, capsys):
    """비정상 rc → rc=1 · 대체 시도 0 · 장부에 rc≠0 마감 레코드 · 대체 권유 문구 0."""
    fake = _FakeRun(stdout="", returncode=2, stderr="boom")

    rc, ledger = _run(pd, monkeypatch, tmp_path, fake)

    err = capsys.readouterr().err
    assert rc == 1
    assert len(fake.calls) == 1, f"대체 시도 0 위반: {fake.calls}"
    assert "위임 하네스 실패(rc=2)" in err
    assert "재위임은 명시 재호출만" in err
    assert "native 모델이 대신 수행하지 마세요" in err
    assert _SUBSTITUTION_INVITATION not in err, (
        "실패 안내가 아직 다른 하네스/네이티브 재시도를 권한다 — 무음 대체의 입구")
    rows = _ledger_rows(ledger)
    assert [row["rc"] for row in rows] == [2]
    assert all(row["finished_at"] is not None for row in rows)


def test_timeout_fails_loud_with_no_substitute_attempt(pd, monkeypatch, tmp_path, capsys):
    """turn 타임아웃 → rc=1 · 대체 시도 0 · 무음 대체 금지 안내."""
    fake = _FakeRun(stdout="", timed_out=True)

    rc, _ledger = _run(pd, monkeypatch, tmp_path, fake)

    err = capsys.readouterr().err
    assert rc == 1
    assert len(fake.calls) == 1
    assert "타임아웃" in err
    assert "재위임은 명시 재호출만" in err


def test_empty_reply_fails_loud_with_no_substitute_attempt(pd, monkeypatch, tmp_path, capsys):
    """rc=0 이지만 reply 미추출(빈 출력) → rc=1 · 대체 시도 0 · 무음 대체 금지 안내."""
    fake = _FakeRun(stdout="", returncode=0)

    rc, _ledger = _run(pd, monkeypatch, tmp_path, fake)

    err = capsys.readouterr().err
    assert rc == 1
    assert len(fake.calls) == 1
    assert "reply 미추출" in err
    assert "재위임은 명시 재호출만" in err


# ════════════════════════════════════════════════════════════════════════
# ⑤ 명시 fallback tuple — 기존 loud 폴백 동작 불변(금지 대상이 아니다)
# ════════════════════════════════════════════════════════════════════════

def test_declared_fallback_still_runs_once_and_stays_loud(pd, monkeypatch, tmp_path, capsys):
    """설정된 폴백 tuple + 인프라 실패 → 폴백 1회 실행·rc=0·loud provenance(기존 동작 보존).

    무음 대체 금지는 **명시 설정 없는 자동 대체**를 막는 것이다 — 선언된 폴백은 사람이 미리
    승인한 수신자라 이 티켓의 금지 대상이 아니다(그 실행은 stderr·stdout 에 loud 하게 남는다).
    """
    conf = _conf(**{
        "delegate.developer.fallback.harness": "claude",
        "delegate.developer.fallback.model": "opus",
    })
    fake = _SequenceRun(
        FileNotFoundError("codex: not found"),                       # primary 스폰 실패
        {"returncode": 0, "stdout": _claude_stdout("폴백결과"),      # 폴백 성공
         "stderr": "", "timed_out": False},
    )

    rc, ledger = _run(pd, monkeypatch, tmp_path, fake, conf=conf)

    captured = capsys.readouterr()
    assert rc == 0
    assert [call["harness"] for call in fake.calls] == ["codex", "claude"], (
        f"선언된 1단 폴백이 실행되지 않음(또는 2단 폴백 발생): {fake.calls}")
    assert "폴백: codex→claude(opus)" in captured.err
    assert "폴백결과" in captured.out
    # 두 attempt 모두 장부에 마감 레코드로 남는다(실패한 primary 도 사라지지 않는다).
    rows = sorted(_ledger_rows(ledger), key=lambda row: row["started_at"])
    assert [row["harness"] for row in rows] == ["codex", "claude"]
    assert [row["rc"] for row in rows] == [127, 0]


def test_declared_fallback_failure_has_no_second_substitute(pd, monkeypatch, tmp_path, capsys):
    """폴백까지 실패 → rc=1 · 2차 폴백 없음(총 실행 2회) · 명시 재호출 안내."""
    conf = _conf(**{
        "delegate.developer.fallback.harness": "claude",
        "delegate.developer.fallback.model": "opus",
    })
    fake = _SequenceRun(
        FileNotFoundError("codex: not found"),
        {"returncode": 3, "stdout": "", "stderr": "fallback boom", "timed_out": False},
    )

    rc, _ledger = _run(pd, monkeypatch, tmp_path, fake, conf=conf)

    err = capsys.readouterr().err
    assert rc == 1
    assert len(fake.calls) == 2, f"2차 폴백이 발생함: {fake.calls}"
    assert "2차 폴백 없음" in err
    assert "재위임은 명시 재호출만" in err


# ════════════════════════════════════════════════════════════════════════
# 스폰 전 차단 경로 — 장부 안전망이 없는 자리라 안내가 더 중요하다
# ════════════════════════════════════════════════════════════════════════

def test_codex_egress_block_carries_no_substitute_note(pd, monkeypatch, tmp_path, capsys):
    """네트워크 차단 환경의 증명 없는 실행(스폰·raw 예약 전 종료)도 무음 대체 금지 안내를 낸다.

    이 경로는 raw 예약 **전**이라 장부에 아무것도 안 남는다 — 안내가 없으면 실패가 어디에도
    기록되지 않은 채 세션 native 모델이 대행하는 실사고 경로가 그대로 열린다.
    """
    monkeypatch.setenv(_EGRESS_MARKER, "1")
    fake = _FakeRun(stdout=_codex_stdout("가면 안 되는 답"))

    rc, ledger = _run(pd, monkeypatch, tmp_path, fake)

    err = capsys.readouterr().err
    assert rc == 1
    assert fake.calls == [], "차단 경로인데 타겟 CLI 가 스폰됨"
    assert not ledger.exists(), "스폰 전 차단인데 raw 장부 레코드가 예약됨"
    assert "Codex sandbox 네트워크 차단" in err, f"차단 사유가 사라짐:\n{err}"
    assert "재위임은 명시 재호출만" in err, f"스폰 전 차단 경로에 무음 대체 금지 안내 누락:\n{err}"


def test_no_substitute_note_has_single_consumer(pd):
    """`NO_SILENT_SUBSTITUTE_NOTE` 의 소비자는 `fail_loud` 하나뿐이다(문자열 수동 결합 금지).

    지점마다 안내를 손으로 이어붙이면 **새 실패 경로가 조용히 빠뜨린다** — 실제로 스폰 전 egress
    차단 경로가 그렇게 누락됐다. 소비자를 하나로 묶어 다음 인스턴스를 기계로 막는다.
    """
    tree = ast.parse(PM_DELEGATE.read_text(encoding="utf-8"))
    funnel = next(
        (node for node in tree.body
         if isinstance(node, ast.FunctionDef) and node.name == "fail_loud"), None)
    assert funnel is not None, "실패-종료 단일 깔때기 `fail_loud` 가 사라짐"
    inside = {id(node) for node in ast.walk(funnel)}

    leaked = [
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and node.id == "NO_SILENT_SUBSTITUTE_NOTE"
        and isinstance(node.ctx, ast.Load)
        and id(node) not in inside
    ]

    assert not leaked, (
        f"안내 문구를 깔때기 밖에서 직접 결합한 지점: pm_delegate.py:{leaked} — "
        "실패 종료는 `return fail_loud(<사유>)` 로만 끝내라.")
    assert pd.fail_loud is not None  # 로드된 모듈에도 같은 seam 이 존재


# ════════════════════════════════════════════════════════════════════════
# AST 가드 확장 (T-0601 ⑥) — "안내를 아예 안 쓰는 새 실패 종료" 클래스
# ════════════════════════════════════════════════════════════════════════
# 위의 단일-소비자 가드는 "안내를 깔때기 **밖에서** 결합했나"만 본다. 새 실패 종료가 안내를 아예
# 안 쓰면 그 가드는 조용히 통과한다 — T-0596 의 원 사고가 정확히 그 클래스였다. 그래서 채널 실행
# 구간의 **비영 종료 전수**가 `fail_loud` 경유임을 구조로 단언한다.

# 채널 실행 구간 = 하네스를 실제로 스폰하고 결과를 회수하는 함수. 이 앞의 게이트(설정 해소·시크릿
# 스캔·재앵커)는 전송 자체가 없어 다른 축이다.
_CHANNEL_FUNCTION = "_execute_and_collect"


def _own_returns(node: ast.AST) -> list[ast.Return]:
    """그 함수 **자신의** return 문 (중첩 함수/람다의 것은 제 소유가 아니라 제외)."""
    found: list[ast.Return] = []
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(child, ast.Return):
            found.append(child)
        found.extend(_own_returns(child))
    return found


def _is_success_exit(node: ast.Return) -> bool:
    """`return 0` — 성공 종료(안내 대상 아님)."""
    return isinstance(node.value, ast.Constant) and node.value.value == 0


def _is_funnel_exit(node: ast.Return) -> bool:
    """`return fail_loud(...)` — 단일 깔때기 경유 실패 종료."""
    return (isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "fail_loud")


def _leaking_exits(source: str, function: str) -> list[int]:
    """그 함수에서 `return 0` 도 `return fail_loud(...)` 도 아닌 종료의 줄 번호."""
    tree = ast.parse(source)
    target = next(
        (node for node in ast.walk(tree)
         if isinstance(node, ast.FunctionDef) and node.name == function), None)
    assert target is not None, f"채널 실행 구간 `{function}` 이 사라짐 — 가드 앵커 소실"
    return [node.lineno for node in _own_returns(target)
            if not (_is_success_exit(node) or _is_funnel_exit(node))]


def test_every_channel_exit_goes_through_the_funnel(pd):
    """채널 실행 구간의 종료는 `return 0` 아니면 `return fail_loud(...)` 뿐이다.

    이 단언이 막는 것은 "안내를 손으로 안 이어붙였다"가 아니라 **안내를 아예 안 내는 새 실패
    종료**다 — 그 경로는 raw 장부만 남고 사람에겐 아무 처방이 없어, 실패한 위임을 세션 native
    모델이 조용히 대행하는 입구가 된다."""
    leaked = _leaking_exits(PM_DELEGATE.read_text(encoding="utf-8"), _CHANNEL_FUNCTION)
    assert not leaked, (
        f"깔때기를 안 거치는 종료: pm_delegate.py:{leaked} — 실패 종료는 "
        "`return fail_loud(<사유>)`, 성공 종료는 `return 0` 으로만 끝내라.")
    assert callable(pd.fail_loud)


@pytest.mark.parametrize("statement, detected", [
    ("return 1", True),                       # 안내 없는 새 실패 종료(원 사고 클래스)
    ("return rc", True),                      # 변수 rc 도 안내를 안 낸다
    ("print('실패'); return 2", True),         # 손으로 낸 메시지는 깔때기가 아니다
    ("return _other_helper(msg)", True),      # 다른 헬퍼 경유도 안내를 보장 못 한다
    ("return fail_loud('오류: x')", False),
    ("return 0", False),
])
def test_guard_actually_detects_a_new_silent_exit(statement, detected):
    """가드 자신의 검출력 — 합성 종료를 실제로 잡는지 확인한다(가짜 게이트 방지).

    안 돌려보면 가짜 게이트다: 이 파라미터가 없으면 분류기가 무엇이든 통과시키도록 퇴화해도
    위 단언은 계속 green 이다."""
    source = (
        f"def {_CHANNEL_FUNCTION}(*, args):\n"
        "    if args:\n"
        f"        {statement}\n"
        "    return 0\n"
    )
    assert bool(_leaking_exits(source, _CHANNEL_FUNCTION)) is detected


def test_guard_ignores_nested_helper_returns():
    """중첩 함수의 종료는 채널 종료가 아니다 — 오탐으로 가드가 못 쓰이게 되는 걸 막는다."""
    source = (
        f"def {_CHANNEL_FUNCTION}(*, args):\n"
        "    def _tally(rows):\n"
        "        return len(rows)\n"
        "    _tally(args)\n"
        "    return 0\n"
    )
    assert _leaking_exits(source, _CHANNEL_FUNCTION) == []


def test_successful_delegation_prints_no_substitute_note(pd, monkeypatch, tmp_path, capsys):
    """정상 위임(rc=0·reply 있음)은 무음 대체 금지 안내를 내지 않는다(실패 경로 전용 문구)."""
    fake = _FakeRun(stdout=_codex_stdout("정상답변"))

    rc, _ledger = _run(pd, monkeypatch, tmp_path, fake)

    captured = capsys.readouterr()
    assert rc == 0
    assert "정상답변" in captured.out
    assert "재위임은 명시 재호출만" not in captured.err
