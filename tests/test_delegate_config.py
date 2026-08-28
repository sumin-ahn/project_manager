"""delegate 설정 표면 — board.py init 산출 + board lint advisory 배선 (T-0446·T-0767).

pm_delegate 판정 로직(lint_same_model)의 단위는 test_pm_delegate.py 에 있다. 이 파일은 board 쪽
표면을 hermetic 하게 검증한다:
  ① `board.py init` 이 쓰는 local.conf 에는 **실값만** 있다 — 위임 키 카탈로그·설명 블록은
     출하 문서가 소유하고, 재실행은 기존 사용자 값을 비파괴로 보존한다(T-0767).
  ② `board.py lint`(lint_delegate)가 pm_delegate 동일-모델 경고를 advisory 로 표면화하고 rc(gate)
     에 기여하지 않는다(never-block).

hermetic: board.py 모듈 전역(REPO·LOCAL_CONF·AREAS_FILE 등)이 import 시점에 실 repo 절대경로로
굳으므로 tmp 프로젝트로 monkeypatch 재지정한다(test_board_multipm 패턴 동류).
"""
from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"


def _load_board():
    spec = importlib.util.spec_from_file_location("board_delegate", TOOLS / "board.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def board(tmp_path, monkeypatch):
    """fresh board 모듈 + IO 전역을 tmp 로 재지정 + init 부작용(git/stdin) stub."""
    proj = tmp_path / "proj"
    pm = proj / ".project_manager"
    wiki = pm / "wiki"
    (wiki / "tickets").mkdir(parents=True, exist_ok=True)
    mod = _load_board()
    for name, val in {
        "REPO": proj,
        "LOCAL_CONF": pm / "local.conf",
        "AREAS_FILE": pm / "areas.md",
        "PM_STATE_FILE": wiki / "pm_state.md",
        "PM_STATE_TEMPLATE": wiki / "pm_state.template.md",
    }.items():
        monkeypatch.setattr(mod, name, val)
    # 실 git hook 설치·stdin opt-in 프롬프트·board submodule 조작을 무해 stub 으로 차단
    # (init 의 local.conf 효과만 검증).
    monkeypatch.setattr(mod, "install_pre_push_hook", lambda: False)
    monkeypatch.setattr(mod, "prompt_additional_reviewer_optin", lambda: None)
    monkeypatch.setattr(mod, "_configure_board_submodule", lambda: False)
    monkeypatch.setattr(mod, "_detect_py", lambda: "python3")
    monkeypatch.setattr(mod, "_is_noninteractive", lambda: True)
    monkeypatch.delenv("PM_SESSION_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_NAME", raising=False)
    return mod


class _FakeStdin:
    """isatty() 를 강제하는 최소 stdin 대역(pytest 캡처 stdin 은 isatty=False)."""

    def __init__(self, tty: bool):
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def _init_args(prefix=None, area=None, owner=None, repo=None, slot=None, user=None):
    return argparse.Namespace(prefix=prefix, area=area, owner=owner,
                              repo=repo, slot=slot, user=user)


# ── ② board lint advisory 배선 ───────────────────────────────────────────────

def test_lint_delegate_surfaces_advisory(board, monkeypatch):
    """dev/reviewer 동일-모델 시 board lint_delegate 가 advisory finding 을 낸다(kind 등재·never-block)."""
    same = {
        "delegate.developer.harness": "codex", "delegate.developer.model": "gpt-x",
        "delegate.code-reviewer.harness": "claude", "delegate.code-reviewer.model": "gpt-x",
    }
    monkeypatch.setattr(board, "local_config", lambda: same)
    findings = board.lint_delegate()
    assert len(findings) == 1
    label, kind, detail = findings[0]
    assert kind == "delegate-same-model"
    assert kind in board._ADVISORY_LINT_KINDS               # --gate 종료코드 비기여(never-block)
    assert "generate≠evaluate" in detail


def test_lint_delegate_clean_no_finding(board, monkeypatch):
    """모델 상이·미설정이면 lint_delegate 는 finding 0(솔로/미사용 무영향)."""
    monkeypatch.setattr(board, "local_config", lambda: {
        "delegate.developer.model": "gpt-x", "delegate.code-reviewer.model": "opus"})
    assert board.lint_delegate() == []
    monkeypatch.setattr(board, "local_config", lambda: {})
    assert board.lint_delegate() == []


def _same_model_conf() -> dict:
    return {
        "delegate.developer.harness": "codex", "delegate.developer.model": "gpt-x",
        "delegate.code-reviewer.harness": "claude", "delegate.code-reviewer.model": "gpt-x",
    }


def test_lint_tickets_wires_delegate(board, monkeypatch):
    """실제 lint_tickets() 가 delegate 경고를 포함한다(배선 직접 검증·blocking 필터 재구현 아님)."""
    monkeypatch.setattr(board, "local_config", lambda: _same_model_conf())
    monkeypatch.setattr(board, "_load_domain_module", lambda: None)  # 실 DOMAIN_DIR 스캔 격리.
    kinds = {kind for _label, kind, _detail in board.lint_tickets()}
    assert "delegate-same-model" in kinds


def test_cmd_lint_gate_delegate_is_advisory_exit0(board, monkeypatch, capsys):
    """실제 cmd_lint(--gate) 는 delegate 동일-모델 경고만 있을 때 종료코드 0(advisory·never-block)."""
    monkeypatch.setattr(board, "local_config", lambda: _same_model_conf())
    monkeypatch.setattr(board, "_load_domain_module", lambda: None)  # advisory 격리(gate 산술만).
    rc = board.cmd_lint(argparse.Namespace(gate=True))
    assert rc == 0                                          # block_count 0 → gate 통과
    out = capsys.readouterr().out
    assert "delegate-same-model" in out                    # 보고엔 표면화(가시성 유지)




# ── ① init 산출 = 실값만 (설명 블록 0 · T-0767) ───────────────────────────────

_EXPLANATION_MARKERS = (
    "cross-harness 역할 위임",     # 옛 위임 카탈로그 헤더
    "delegate.developer.harness",  # 옛 예시 키 라인
    "generate≠evaluate",           # 옛 권고 산문
)


def test_init_conf_carries_values_only(board):
    """fresh init 산출에 위임 카탈로그·설명 블록이 없다 — 실값만 남는 파일이다.

    설명이 conf 에 살면 값과 어긋난 채 굳는다(그 파일은 아무도 다시 읽지 않는다). 카탈로그의
    자리는 출하 문서이고, conf 는 이 clone 이 실제로 정한 값만 담는다.
    """
    rc = board.cmd_init(_init_args())
    assert rc == 0
    text = board.LOCAL_CONF.read_text(encoding="utf-8")
    for marker in _EXPLANATION_MARKERS:
        assert marker not in text, f"conf 에 설명 블록 잔존: {marker}"
    # 활성 키는 전부 실값이다 — 위임 축은 한 줄도 없다(기본 허용이라 기록할 값이 없다).
    parsed = board.local_config()
    assert not [key for key in parsed if key.startswith("delegate.")], parsed
    for key in ("runtime.py", "test.cmd", "project.name",
                "ctx.nudge_pct", "ctx.stop_pct", "ctx.window_tokens"):
        assert key in parsed, f"init 이 실값 키를 안 썼다: {key}"


def test_init_conf_has_no_commented_out_key_examples(board):
    """주석 처리된 `KEY=value` 예시가 0이다 — 예시는 값이 아니라 문서다.

    주석 예시는 "설정 0개인 설명"이라 실값과 모순돼도 아무 게이트가 못 본다. 카탈로그가
    문서로 갔으므로 conf 의 주석은 파일 정체를 밝히는 머리말뿐이어야 한다.
    """
    board.cmd_init(_init_args())
    for line in board.LOCAL_CONF.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("#"):
            continue
        body = stripped.lstrip("#").strip()
        assert "=" not in body, f"주석 처리된 키 예시 잔존: {line!r}"


def test_init_merges_without_appending_a_delegate_block(board):
    """기존 conf 로 재실행해도 위임 블록을 덧붙이지 않고 사용자 값을 그대로 둔다(비파괴)."""
    board.LOCAL_CONF.parent.mkdir(parents=True, exist_ok=True)
    board.LOCAL_CONF.write_text(
        "session=pm\nruntime.py=python3\ntest.cmd=pytest -q\nctx.window_tokens=500000\n"
        "additional_reviewer.enabled=true\n",
        encoding="utf-8")

    assert board.cmd_init(_init_args()) == 0

    text = board.LOCAL_CONF.read_text(encoding="utf-8")
    for marker in _EXPLANATION_MARKERS:
        assert marker not in text
    parsed = board.local_config()
    assert parsed["ctx.window_tokens"] == "500000"          # 커스텀 값 보존
    assert parsed["additional_reviewer.enabled"] == "true"  # 기존 결정 보존
    assert parsed["session"] == "pm"


def test_init_preserves_an_explicit_delegate_switch(board):
    """위임을 끈 채택자(`delegate.enabled=false`)의 명시 값은 재실행이 덮지 않는다.

    기본이 허용이라 값이 없는 것과 `false` 는 다른 상태다 — init 이 그 줄을 지우면 위임이
    조용히 다시 켜진다.
    """
    board.LOCAL_CONF.parent.mkdir(parents=True, exist_ok=True)
    board.LOCAL_CONF.write_text(
        "session=pm\nruntime.py=python3\ntest.cmd=pytest -q\ndelegate.enabled=false\n"
        "delegate.developer.harness=codex\ndelegate.developer.model=gpt-x\n",
        encoding="utf-8")

    board.cmd_init(_init_args())

    parsed = board.local_config()
    assert parsed["delegate.enabled"] == "false"
    assert parsed["delegate.developer.harness"] == "codex"
    active = [ln for ln in board.LOCAL_CONF.read_text(encoding="utf-8").splitlines()
              if ln.strip().startswith("delegate.enabled=")]
    assert active == ["delegate.enabled=false"]             # 재질문/중복 기록 없음
