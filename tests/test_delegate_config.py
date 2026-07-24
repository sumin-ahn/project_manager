"""delegate 설정 표면 — board.py init 시드 + board lint advisory 배선 (T-0446).

pm_delegate 판정 로직(lint_same_model)의 단위는 test_pm_delegate.py 에 있다. 이 파일은 board 쪽
표면을 hermetic 하게 검증한다:
  ① `board.py init` 이 fresh local.conf 에 delegate 주석 시드 블록을 쓴다(delegate_enabled 안내·
     3키 예시·외부 전송 경고). + 재실행 멱등(중복 시드 없음).
  ② 시드 예시 key 라인은 주석 해제 시 그대로 유효한 KEY=value 다(값에 inline `#`/화살표 없음).
  ③ `board.py lint`(lint_delegate)가 pm_delegate 동일-모델 경고를 advisory 로 표면화하고 rc(gate)
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
    monkeypatch.setattr(mod, "prompt_external_review_optin", lambda: None)
    monkeypatch.setattr(mod, "_configure_board_submodule", lambda: False)
    monkeypatch.setattr(mod, "_detect_py", lambda: "python3")
    # delegate opt-in 질문은 기본 비대화형으로 고정(seed/멱등 테스트 결정성) — TTY 질문 분기는
    # prompt_delegate_optin 직접 호출 테스트가 _is_noninteractive 를 False 로 덮어 검증한다.
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


# ── ① init 시드 내용 ─────────────────────────────────────────────────────────

def test_init_seeds_delegate_full_schema(board):
    """fresh init 이 delegate 스키마 시드를 쓴다 — 4역할 전부·3키 예시·false 기본·경고(must-fix 2)."""
    rc = board.cmd_init(_init_args())
    assert rc == 0
    conf = board.LOCAL_CONF.read_text(encoding="utf-8")
    assert "cross-harness 역할 위임" in conf                    # 섹션 헤더(마커)
    assert "# delegate_enabled=false" in conf                  # 기본 OFF 를 명시한 예시 라인
    assert "외부 하네스" in conf and "과금" in conf              # 외부 전송·과금 경고
    # 4역할 전부(developer·developer.hard·researcher·architect·code-reviewer) 예시 노출.
    for key in ("delegate.developer.harness=codex",
                "delegate.developer.model=",
                "delegate.developer.reasoning=",
                "delegate.developer.hard.harness=",
                "delegate.researcher.harness=",
                "delegate.researcher.reasoning=",
                "delegate.architect.harness=",
                "delegate.architect.reasoning=",
                "delegate.code-reviewer.harness=",
                "delegate.code-reviewer.model="):
        assert key in conf, f"시드 스키마에 예시 누락: {key}"
    assert "generate≠evaluate" in conf                         # reviewer 별-모델 권장
    # 예시 모델명은 실존 모델(sol·terra·luna)만 — 허구 모델명 금지(must-fix 3R).
    assert "gpt-5.6-luna" in conf and "gpt-5.6-nova" not in conf


def test_init_delegate_default_off_not_active_key(board):
    """비대화형 init 은 delegate_enabled 실키를 기록하지 않는다 — 기본 OFF(주석 스키마만)."""
    board.cmd_init(_init_args())
    conf = board.local_config()
    assert "delegate_enabled" not in conf                      # 실키 미기록(비대화형)
    text = board.LOCAL_CONF.read_text(encoding="utf-8")
    assert "# delegate_enabled=false" in text                  # 기본 OFF 스키마 예시(주석)


def test_init_delegate_example_lines_have_no_inline_comment(board):
    """시드 예시 key 라인은 주석 해제 시 그대로 유효 — 값 뒤 inline `#`/화살표 없음(파서 계약·§3.2)."""
    board.cmd_init(_init_args())
    text = board.LOCAL_CONF.read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.lstrip("#").strip()
        if not stripped.startswith("delegate."):
            continue
        # 주석 마커를 벗긴 예시 key 라인. `delegate.<key>=<value>` 이고 값에 inline 주석/화살표 없음.
        assert "=" in stripped, f"예시 delegate 라인에 = 없음: {line!r}"
        value = stripped.split("=", 1)[1]
        assert "#" not in value, f"예시 값에 inline # 혼입: {line!r}"
        assert "←" not in value and "→" not in value, f"예시 값에 화살표 주석 혼입: {line!r}"


# ── ① init 멱등 ──────────────────────────────────────────────────────────────

def test_init_delegate_seed_idempotent(board):
    """재실행해도 delegate 시드 블록이 중복되지 않는다(기존 init 멱등 패턴 상속)."""
    board.cmd_init(_init_args())
    first = board.LOCAL_CONF.read_text(encoding="utf-8")
    board.cmd_init(_init_args())
    second = board.LOCAL_CONF.read_text(encoding="utf-8")
    # 섹션 헤더가 두 번 나타나지 않는다(중복 시드 없음).
    assert second.count("cross-harness 역할 위임") == 1
    assert second.count("delegate.developer.harness=codex") == 1
    # 예시 key 라인 총 개수도 재실행 후 불변.
    assert first.count("delegate.developer") == second.count("delegate.developer")


# ── ③ board lint advisory 배선 ───────────────────────────────────────────────

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


# ── ① must-fix: 기존 adopter local.conf 재실행 append (멱등·비파괴) ──────────────

def test_init_existing_conf_without_delegate_gets_append(board):
    """this-change 이전 local.conf(기존 adopter)를 가진 채 init 재실행 → delegate 블록 append."""
    # 사용자 커스텀 키 + delegate 흔적 없는 기존 conf 를 심는다.
    board.LOCAL_CONF.parent.mkdir(parents=True, exist_ok=True)
    board.LOCAL_CONF.write_text(
        "session=pm\npy=python3\ntest_cmd=pytest -q\nctx_window_tokens=500000\n"
        "external_review_enabled=true\n",
        encoding="utf-8")
    rc = board.cmd_init(_init_args())
    assert rc == 0
    conf = board.LOCAL_CONF.read_text(encoding="utf-8")
    assert "cross-harness 역할 위임" in conf                    # delegate 시드 append 됨
    assert "delegate.developer.harness=codex" in conf
    # 기존 사용자 키/값 보존(비파괴 병합·커스텀 ctx_window·external_review 결정).
    assert "ctx_window_tokens=500000" in conf
    assert "external_review_enabled=true" in conf
    assert "session=pm" in conf


def test_init_existing_conf_append_idempotent(board):
    """append 경로도 멱등 — delegate 블록이 있는 conf 로 재실행 시 중복 append 0."""
    board.LOCAL_CONF.parent.mkdir(parents=True, exist_ok=True)
    board.LOCAL_CONF.write_text("session=pm\npy=python3\ntest_cmd=pytest -q\n", encoding="utf-8")
    board.cmd_init(_init_args())                              # 1회: append
    once = board.LOCAL_CONF.read_text(encoding="utf-8")
    board.cmd_init(_init_args())                              # 2회: no-op
    twice = board.LOCAL_CONF.read_text(encoding="utf-8")
    assert once.count("cross-harness 역할 위임") == 1
    assert twice.count("cross-harness 역할 위임") == 1          # 중복 append 없음
    assert once.count("delegate.developer") == twice.count("delegate.developer")


def test_init_existing_conf_with_active_delegate_key_preserves(board):
    """이미 실키 delegate_enabled 를 켠 adopter → 재실행에 실키 보존·재질문/덮어쓰기 없음(멱등)."""
    board.LOCAL_CONF.parent.mkdir(parents=True, exist_ok=True)
    board.LOCAL_CONF.write_text(
        "session=pm\npy=python3\ntest_cmd=pytest -q\ndelegate_enabled=true\n"
        "delegate.developer.harness=codex\ndelegate.developer.model=gpt-x\n",
        encoding="utf-8")
    board.cmd_init(_init_args())
    # 활성 실키는 true 로 보존(스키마 주석의 `# delegate_enabled=false` 예시는 파싱 제외).
    assert board.local_config().get("delegate_enabled") == "true"
    text = board.LOCAL_CONF.read_text(encoding="utf-8")
    # 활성(uncommented) delegate_enabled 라인은 정확히 1개(true)·false 활성 라인 없음.
    active = [ln for ln in text.splitlines()
              if ln.strip().startswith("delegate_enabled=")]
    assert active == ["delegate_enabled=true"]                # 재질문/덮어쓰기 없음(멱등)


# ── ① must-fix: TTY opt-in 질문 3분기 (y / N / 비-TTY) + 실키 멱등 ────────────────

def _tty(board_or_mod, monkeypatch, tty: bool, answer: str | None = None):
    """대상 모듈의 _is_noninteractive/isatty/input 을 TTY 분기용으로 고정한다."""
    monkeypatch.setattr(board_or_mod, "_is_noninteractive", lambda: False)
    monkeypatch.setattr(board_or_mod.sys, "stdin", _FakeStdin(tty))
    if answer is not None:
        monkeypatch.setattr("builtins.input", lambda *_a, **_k: answer)


def test_prompt_delegate_optin_tty_yes_records_true(board, monkeypatch):
    """TTY 에서 y → delegate_enabled=true 실키 기록."""
    board.LOCAL_CONF.parent.mkdir(parents=True, exist_ok=True)
    board.LOCAL_CONF.write_text("session=pm\n", encoding="utf-8")
    _tty(board, monkeypatch, tty=True, answer="y")
    board.prompt_delegate_optin()
    assert board.local_config().get("delegate_enabled") == "true"


def test_prompt_delegate_optin_tty_no_records_false(board, monkeypatch):
    """TTY 에서 무입력/기타 → delegate_enabled=false 실키 기록(질문 반복 방지)."""
    board.LOCAL_CONF.parent.mkdir(parents=True, exist_ok=True)
    board.LOCAL_CONF.write_text("session=pm\n", encoding="utf-8")
    _tty(board, monkeypatch, tty=True, answer="")
    board.prompt_delegate_optin()
    assert board.local_config().get("delegate_enabled") == "false"


def test_prompt_delegate_optin_nontty_no_key(board, monkeypatch):
    """비-TTY → 질문·실키 기록 없음(기본 OFF 유지)."""
    board.LOCAL_CONF.parent.mkdir(parents=True, exist_ok=True)
    board.LOCAL_CONF.write_text("session=pm\n", encoding="utf-8")
    _tty(board, monkeypatch, tty=False)
    board.prompt_delegate_optin()
    assert "delegate_enabled" not in board.local_config()


def test_prompt_delegate_optin_idempotent_after_record(board, monkeypatch):
    """실키가 이미 있으면 재호출 시 무질문(input 호출되면 실패)·no-op 멱등."""
    board.LOCAL_CONF.parent.mkdir(parents=True, exist_ok=True)
    board.LOCAL_CONF.write_text("session=pm\ndelegate_enabled=false\n", encoding="utf-8")

    def _boom(*_a, **_k):
        raise AssertionError("실키 기록 후엔 다시 묻지 않아야 한다")

    monkeypatch.setattr(board, "_is_noninteractive", lambda: False)
    monkeypatch.setattr(board.sys, "stdin", _FakeStdin(True))
    monkeypatch.setattr("builtins.input", _boom)
    board.prompt_delegate_optin()  # no-op(실키 present) — input 미호출
    assert board.local_config().get("delegate_enabled") == "false"


def test_prompt_delegate_optin_tty_eof_records_false(board, monkeypatch):
    """TTY 에서 EOF(Ctrl-D) → 기본 거절로 delegate_enabled=false 실키 기록(재질문 방지·must-fix 3R)."""
    board.LOCAL_CONF.parent.mkdir(parents=True, exist_ok=True)
    board.LOCAL_CONF.write_text("session=pm\n", encoding="utf-8")
    monkeypatch.setattr(board, "_is_noninteractive", lambda: False)
    monkeypatch.setattr(board.sys, "stdin", _FakeStdin(True))

    def _eof(*_a, **_k):
        raise EOFError

    monkeypatch.setattr("builtins.input", _eof)
    board.prompt_delegate_optin()
    assert board.local_config().get("delegate_enabled") == "false"


def test_prompt_delegate_optin_append_preserves_no_trailing_newline(board, monkeypatch):
    """마지막 개행 없는 conf 에 append 해도 기존 마지막 키가 손상되지 않는다(개행 보장·must-fix 3R)."""
    board.LOCAL_CONF.parent.mkdir(parents=True, exist_ok=True)
    board.LOCAL_CONF.write_text("session=pm\nupstream_rev=abc123", encoding="utf-8")  # 개행 없음
    _tty(board, monkeypatch, tty=True, answer="y")
    board.prompt_delegate_optin()
    conf = board.local_config()
    assert conf.get("upstream_rev") == "abc123"                # 기존 키 온전(손상 없음)
    assert conf.get("delegate_enabled") == "true"              # 새 실키 정상 기록


# ── ② must-fix: pm_update epilog delegate 도입 advisory ──────────────────────────

@pytest.fixture
def pm_update():
    spec = importlib.util.spec_from_file_location("pm_update_delegate", TOOLS / "pm_update.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_conf(tmp_path, text: str):
    pm = tmp_path / ".project_manager"
    pm.mkdir(parents=True, exist_ok=True)
    conf = pm / "local.conf"
    conf.write_text(text, encoding="utf-8")
    return conf


def test_pm_update_nontty_advises_when_realkey_absent(pm_update, tmp_path, capsys, monkeypatch):
    """비-TTY + 실키 부재 → 도입 advisory 1줄 표면화·conf 미수정(질문 write 는 TTY 경로 한정)."""
    conf = _write_conf(tmp_path, "session=pm\n# delegate_enabled=true\nupstream=/x\n")  # 주석만=미결정
    monkeypatch.setattr(pm_update, "_is_noninteractive", lambda: True)
    pm_update.maybe_prompt_delegate_optin(tmp_path)
    out = capsys.readouterr().out
    assert "pm_delegate" in out and "기본 OFF" in out
    assert conf.read_text(encoding="utf-8") == "session=pm\n# delegate_enabled=true\nupstream=/x\n"


def test_pm_update_tty_yes_records_true(pm_update, tmp_path, monkeypatch):
    """TTY + y → delegate_enabled=true 실키를 대상 conf 에 기록(질문 응답 = 유일 write 예외)."""
    conf = _write_conf(tmp_path, "session=pm\n")
    _tty(pm_update, monkeypatch, tty=True, answer="y")
    pm_update.maybe_prompt_delegate_optin(tmp_path)
    assert pm_update._read_local_conf(conf).get("delegate_enabled") == "true"


def test_pm_update_tty_no_records_false(pm_update, tmp_path, monkeypatch):
    """TTY + 무입력 → delegate_enabled=false 실키 기록(질문 반복 방지)."""
    conf = _write_conf(tmp_path, "session=pm\n")
    _tty(pm_update, monkeypatch, tty=True, answer="")
    pm_update.maybe_prompt_delegate_optin(tmp_path)
    assert pm_update._read_local_conf(conf).get("delegate_enabled") == "false"


def test_pm_update_realkey_present_noop(pm_update, tmp_path, capsys, monkeypatch):
    """실키가 이미 있으면 질문·안내 없음(무질문·무발화 멱등)."""
    _write_conf(tmp_path, "session=pm\ndelegate_enabled=false\n")

    def _boom(*_a, **_k):
        raise AssertionError("실키 present 면 다시 묻지 않아야 한다")

    monkeypatch.setattr(pm_update, "_is_noninteractive", lambda: False)
    monkeypatch.setattr(pm_update.sys, "stdin", _FakeStdin(True))
    monkeypatch.setattr("builtins.input", _boom)
    pm_update.maybe_prompt_delegate_optin(tmp_path)
    assert capsys.readouterr().out == ""


def test_pm_update_tty_eof_records_false(pm_update, tmp_path, monkeypatch):
    """TTY EOF(Ctrl-D) → 기본 거절로 delegate_enabled=false 실키 기록(재질문 방지·must-fix 3R)."""
    conf = _write_conf(tmp_path, "session=pm\n")
    monkeypatch.setattr(pm_update, "_is_noninteractive", lambda: False)
    monkeypatch.setattr(pm_update.sys, "stdin", _FakeStdin(True))

    def _eof(*_a, **_k):
        raise EOFError

    monkeypatch.setattr("builtins.input", _eof)
    pm_update.maybe_prompt_delegate_optin(tmp_path)
    assert pm_update._read_local_conf(conf).get("delegate_enabled") == "false"


def test_pm_update_append_preserves_no_trailing_newline(pm_update, tmp_path, monkeypatch):
    """개행 없는 conf 에 실키 append 해도 기존 마지막 키 손상 없음(개행 보장·must-fix 3R)."""
    conf = _write_conf(tmp_path, "session=pm\nupstream_rev=abc123")  # 개행 없음
    _tty(pm_update, monkeypatch, tty=True, answer="y")
    pm_update.maybe_prompt_delegate_optin(tmp_path)
    parsed = pm_update._read_local_conf(conf)
    assert parsed.get("upstream_rev") == "abc123"              # 기존 키 온전
    assert parsed.get("delegate_enabled") == "true"           # 새 실키 정상 기록


def test_pm_update_no_advice_when_conf_absent(pm_update, tmp_path, capsys, monkeypatch):
    """local.conf 부재(init 전)면 무발화 — board.py init 이 시드/질문한다."""
    (tmp_path / ".project_manager").mkdir(parents=True)
    monkeypatch.setattr(pm_update, "_is_noninteractive", lambda: True)
    pm_update.maybe_prompt_delegate_optin(tmp_path)
    assert capsys.readouterr().out == ""
