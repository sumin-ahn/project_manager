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
        "delegate_enabled": "true",
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


def test_successful_delegation_prints_no_substitute_note(pd, monkeypatch, tmp_path, capsys):
    """정상 위임(rc=0·reply 있음)은 무음 대체 금지 안내를 내지 않는다(실패 경로 전용 문구)."""
    fake = _FakeRun(stdout=_codex_stdout("정상답변"))

    rc, _ledger = _run(pd, monkeypatch, tmp_path, fake)

    captured = capsys.readouterr()
    assert rc == 0
    assert "정상답변" in captured.out
    assert "재위임은 명시 재호출만" not in captured.err
