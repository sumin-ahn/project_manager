"""board.py CP949/Windows 하드닝 단위 테스트 (T-0017).

한국어 Windows(기본 로케일 cp949) + Python 3.12 에서 board.py 가 추가 환경변수 없이
동작해야 한다. 이 테스트들은 *수정 전 코드에서 실패* 하도록 설계됐다 — ambient
PYTHONUTF8 가 버그를 가리지 않게, 파일 I/O 단언은 (locale 에 의존하지 않고) write/read
호출에 `encoding="utf-8"` 가 명시됐는지를 직접 검사한다.

도구가 패키지가 아니므로 importlib 로 경로 로드한다(test_portability 와 동일).
"""
from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

# em-dash(U+2014) + 이모지 + 한글 — cp949 로는 인코딩 불가. 실 ticket 본문의 재현.
HARD_CONTENT = "결정 — 외부 전송 발생 ✓ ✅ 🟡 🔴 — 끝"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def board():
    return _load("board")


# ── C1: load_ticket / dump_ticket round-trip (em-dash + 이모지) ──────────────

def test_dump_load_round_trip_em_dash_and_emoji(board, tmp_path):
    """`—`(U+2014)+이모지+한글 ticket 을 dump→load 했을 때 내용이 보존되고, 디스크에
    실제 UTF-8 바이트로 기록되는지. (cp949 기본이면 dump 가 UnicodeEncodeError 로 죽는다.)
    """
    path = tmp_path / "T-9999-hard.md"
    fm = {"id": "T-9999", "title": HARD_CONTENT, "status": "open"}
    body = f"# 본문\n{HARD_CONTENT}\n"

    board.dump_ticket(path, fm, body)

    # 디스크 바이트가 UTF-8 인지 — cp949 로 적혔다면 utf-8 decode 가 깨진다.
    raw = path.read_bytes()
    assert HARD_CONTENT.encode("utf-8") in raw

    fm2, body2 = board.load_ticket(path)
    assert fm2["title"] == HARD_CONTENT
    assert body2 == body


def test_dump_ticket_passes_utf8_encoding(board, tmp_path, monkeypatch):
    """dump_ticket 이 write_text 에 encoding='utf-8' 를 명시하는지 직접 검증.

    ambient PYTHONUTF8 가 cp949 버그를 가려도 이 단언은 통과하지 못한다 —
    수정 전 코드(encoding 누락)에서 captured['encoding'] 은 None.
    """
    captured: dict = {}
    orig = Path.write_text

    def spy(self, data, *args, **kwargs):
        if self.name.endswith(".md") and "T-9999" in self.name:
            captured["encoding"] = kwargs.get("encoding")
        return orig(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", spy)
    board.dump_ticket(tmp_path / "T-9999-x.md", {"id": "T-9999"}, "body")
    assert captured.get("encoding") == "utf-8"


def test_load_ticket_passes_utf8_encoding(board, tmp_path, monkeypatch):
    """load_ticket 이 read_text 에 encoding='utf-8' 를 명시하는지 직접 검증."""
    path = tmp_path / "T-9999-y.md"
    path.write_text("---\nid: T-9999\n---\nbody\n", encoding="utf-8")

    captured: dict = {}
    orig = Path.read_text

    def spy(self, *args, **kwargs):
        if self.name == "T-9999-y.md":
            captured["encoding"] = kwargs.get("encoding")
        return orig(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", spy)
    board.load_ticket(path)
    assert captured.get("encoding") == "utf-8"


# ── C6: prompt_external_review_optin — 비대화/EOF stdin 에서 아무것도 안 씀 ──

def _isolated_local_conf(board, monkeypatch, tmp_path) -> Path:
    """LOCAL_CONF 를 tmp 로 격리하고 빈 상태(미결정)로 둔다."""
    conf = tmp_path / "local.conf"
    monkeypatch.setattr(board, "LOCAL_CONF", conf)
    return conf


def test_prompt_optin_writes_nothing_when_non_tty(board, monkeypatch, tmp_path):
    """비대화형(isatty False) → 묻지 않고 반환, local.conf 에 아무것도 안 씀."""
    conf = _isolated_local_conf(board, monkeypatch, tmp_path)
    monkeypatch.setattr(board.sys.stdin, "isatty", lambda: False)

    board.prompt_external_review_optin()

    assert not conf.exists() or "external_review_enabled" not in conf.read_text(encoding="utf-8")


def test_prompt_optin_writes_nothing_on_eof_under_tty(board, monkeypatch, tmp_path):
    """isatty=True 인데 input() 이 EOFError (Windows-under-pytest 재현) → 아무것도 안 씀.

    수정 전 코드는 answer='' 로 떨어져 external_review_enabled=false 를 기록했다 —
    사용자의 기존 true 결정을 덮어 preservation 을 깨뜨림.
    """
    conf = _isolated_local_conf(board, monkeypatch, tmp_path)
    monkeypatch.setattr(board.sys.stdin, "isatty", lambda: True)

    def _raise_eof(prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", _raise_eof)

    board.prompt_external_review_optin()

    assert not conf.exists() or "external_review_enabled" not in conf.read_text(encoding="utf-8")


def test_prompt_optin_does_not_clobber_existing_true(board, monkeypatch, tmp_path):
    """이미 external_review_enabled 가 있으면(여기선 true) EOF 경로로도 건드리지 않음."""
    conf = _isolated_local_conf(board, monkeypatch, tmp_path)
    conf.write_text("external_review_enabled=true\n", encoding="utf-8")
    monkeypatch.setattr(board.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": (_ for _ in ()).throw(EOFError))

    board.prompt_external_review_optin()

    text = conf.read_text(encoding="utf-8")
    assert "external_review_enabled=true" in text
    assert "external_review_enabled=false" not in text


# ── T-0071: PM_NONINTERACTIVE 명시 신호 우선 (isatty 신뢰불가 함정 회피) ──

@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on"])
def test_prompt_optin_skips_when_pm_noninteractive_truthy(
    board, monkeypatch, tmp_path, val
):
    """PM_NONINTERACTIVE truthy → isatty=True(신뢰불가 DEVNULL 흉내)여도 묻지 않고 skip.

    Windows DEVNULL 의 isatty() 가 True 로 거짓-보고하는 함정을 흉내낸다 — env 신호가
    그걸 이겨 input() 을 절대 안 부르고 local.conf 에 아무것도 안 쓴다.
    """
    conf = _isolated_local_conf(board, monkeypatch, tmp_path)
    monkeypatch.setattr(board.sys.stdin, "isatty", lambda: True)  # 거짓 tty 보고
    monkeypatch.setenv("PM_NONINTERACTIVE", val)
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt="": pytest.fail("PM_NONINTERACTIVE 인데 input() 호출됨 — skip 위반."),
    )

    board.prompt_external_review_optin()

    assert not conf.exists() or "external_review_enabled" not in conf.read_text(
        encoding="utf-8"
    )


@pytest.mark.parametrize("val", ["", "0", "false", "no"])
def test_prompt_optin_falsy_pm_noninteractive_preserves_isatty_path(
    board, monkeypatch, tmp_path, val
):
    """PM_NONINTERACTIVE 빈/falsy → 기존 isatty 폴백 보존(설정 안 한 것과 동일).

    여기선 isatty=True + input 이 정상 'y' → 기록까지 진행해 isatty 경로가 살아있음을 친다.
    """
    conf = _isolated_local_conf(board, monkeypatch, tmp_path)
    monkeypatch.setattr(board.sys.stdin, "isatty", lambda: True)
    monkeypatch.setenv("PM_NONINTERACTIVE", val)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")

    board.prompt_external_review_optin()

    assert "external_review_enabled=true" in conf.read_text(encoding="utf-8")


def test_prompt_optin_no_env_preserves_non_tty_skip(board, monkeypatch, tmp_path):
    """PM_NONINTERACTIVE 미설정 + 비-tty → 기존 isatty 폴백대로 skip(무기록)."""
    conf = _isolated_local_conf(board, monkeypatch, tmp_path)
    monkeypatch.delenv("PM_NONINTERACTIVE", raising=False)
    monkeypatch.setattr(board.sys.stdin, "isatty", lambda: False)

    board.prompt_external_review_optin()

    assert not conf.exists() or "external_review_enabled" not in conf.read_text(
        encoding="utf-8"
    )


# ── C5: pre-push 훅 본문이 탐지 인터프리터를 쓰는지 (bare 'python3' 하드코딩 아님) ──

def test_hook_body_uses_detected_interpreter(board, monkeypatch, tmp_path):
    """훅 본문이 _detect_py() 결과를 주입하는지 — Windows 흔한 'python' 시나리오로 검증.

    수정 전 코드는 'python3' 를 하드코딩해 Windows 에서 깨진다. python 만 PATH 에 있는
    환경을 흉내내면, 고친 코드는 'python' 을 쓰고 bare 'python3' 토큰은 안 나온다.
    """
    import re

    hooks = tmp_path / "hooks"
    monkeypatch.setattr(board, "_hooks_dir", lambda: hooks)
    # 실행검증 seam 을 mock 해 detection 을 which mock 만으로 결정적이게 (실 인터프리터 비의존).
    monkeypatch.setattr(board, "_interp_runs", lambda cmd: True)
    # python3 부재·python 존재 → _detect_py() == 'python'.
    monkeypatch.setattr(
        board.shutil, "which",
        lambda cmd: r"C:\Python\python.exe" if cmd == "python" else None,
    )

    assert board.install_pre_push_hook() is True

    body = (hooks / "pre-push").read_text(encoding="utf-8")
    assert "python .project_manager/tools/board.py regression" in body
    # 명령 토큰으로서의 bare 'python3' 는 없어야 한다 (주석 문구의 .py 경로는 무관).
    assert not re.search(r"(?<![\w.])python3\s+\.project_manager", body)


def test_hook_write_passes_utf8_encoding(board, monkeypatch, tmp_path):
    """hook.write_text 에 encoding='utf-8' 가 명시됐는지 직접 검증 (주석에 한글 포함)."""
    hooks = tmp_path / "hooks"
    monkeypatch.setattr(board, "_hooks_dir", lambda: hooks)
    # 실행검증 seam 을 mock 해 detection 을 which mock 만으로 결정적이게 (실 인터프리터 비의존).
    monkeypatch.setattr(board, "_interp_runs", lambda cmd: True)
    monkeypatch.setattr(board.shutil, "which", lambda cmd: "/usr/bin/python3" if cmd == "python3" else None)

    captured: dict = {}
    orig = Path.write_text

    def spy(self, data, *args, **kwargs):
        if self.name == "pre-push":
            captured["encoding"] = kwargs.get("encoding")
        return orig(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", spy)
    board.install_pre_push_hook()
    assert captured.get("encoding") == "utf-8"


# ── C8: cmd_init 가 local.conf 에 ctx_window_tokens 핸드오프 예산 surface (T-0128) ──

def _init_isolated(board, monkeypatch, tmp_path):
    """cmd_init 을 hermetic 으로: LOCAL_CONF 만 tmp, pm_state·훅·opt-in 부수효과 차단."""
    conf_path = tmp_path / "local.conf"
    monkeypatch.setattr(board, "LOCAL_CONF", conf_path)
    monkeypatch.setattr(board, "PM_STATE_FILE", tmp_path / "pm_state.md")
    monkeypatch.setattr(board, "PM_STATE_TEMPLATE", tmp_path / "missing-template.md")
    monkeypatch.setattr(board, "install_pre_push_hook", lambda: False)
    monkeypatch.setattr(board, "prompt_external_review_optin", lambda: None)
    return conf_path


def test_init_writes_ctx_window_tokens_budget(board, monkeypatch, tmp_path):
    """init 이 local.conf 에 ctx_window_tokens=<기본> 라인을 nudge/stop pct 옆에 기록한다.

    회사 실사용 계기(T-0128): 핸드오프 토큰 예산을 사용자가 발견·조정할 수 있게 surface.
    기본은 어댑터 ctx_guard 와 동기된 200K (board 자체 상수 — touches 격리).
    """
    conf_path = _init_isolated(board, monkeypatch, tmp_path)
    args = argparse.Namespace(prefix=None, area=None, owner=None, session="pm")

    assert board.cmd_init(args) == 0

    conf_text = conf_path.read_text(encoding="utf-8")
    assert f"ctx_window_tokens={board.CTX_WINDOW_TOKENS_DEFAULT}" in conf_text
    assert board.CTX_WINDOW_TOKENS_DEFAULT == 200000
    # nudge/stop pct 옆에 배치됐는지 (기존 ctx 임계와 한 블록).
    assert "ctx_nudge_pct=" in conf_text and "ctx_stop_pct=" in conf_text


def test_init_ctx_window_tokens_has_cost_meaning_comment(board, monkeypatch, tmp_path):
    """ctx_window_tokens 라인 위에 비용 의미 주석(이른 핸드오프=토큰 경제·물리 window 아님)이 박힌다."""
    conf_path = _init_isolated(board, monkeypatch, tmp_path)
    args = argparse.Namespace(prefix=None, area=None, owner=None, session="pm")

    assert board.cmd_init(args) == 0

    conf_text = conf_path.read_text(encoding="utf-8")
    assert "# ctx_window_tokens:" in conf_text
    assert "핸드오프 토큰 예산" in conf_text
    # 핵심 의미: 물리 window 가 아니라 사용자가 정하는 비용/맥락 선택.
    assert "물리 window 아님" in conf_text


# ── C9: cmd_init 재실행 비파괴 병합 — 사용자/operational 키 보존 (T-0184) ──────
# 🔴 데이터 손실 버그: cmd_init 이 local.conf 를 가드 없이 통째 덮어써 재실행 시 init 이
# 안 쓰는 사용자 키(external_review_enabled·upstream·upstream_rev·opencode_pro_model 등)가
# 소멸하고 커스텀 ctx_window_tokens 가 default 로 리셋됐다. 존재 시 병합으로 수정.

# init 이 안 쓰는 사용자/operational 키 + 커스텀 init 기본키를 담은 기존 local.conf.
_CUSTOM_CONF = (
    "# per-clone 설정 (git-ignored). board.py init 생성. clone 마다 다름.\n"
    "session=my-pm\n"
    "py=python3\ntest_cmd=pytest -q\nproject_name=myproj\n"
    "ctx_nudge_pct=20\nctx_stop_pct=10\n"
    "ctx_window_tokens=5000\n"
    "# 외부 코드리뷰 (ADR-0004)\n"
    "external_review_enabled=false\n"
    "reviewer_cmd=codex exec\n"
    "upstream=/x\nupstream_rev=abc\n"
    "opencode_pro_model=m\n"
    "status_total_style=fraction\n"
    "user=me@example.com\n"
)


def test_init_rerun_preserves_custom_operational_keys(board, monkeypatch, tmp_path):
    """(a) 커스텀 키(external_review_enabled·upstream·upstream_rev·opencode_pro_model 등)를
    담은 local.conf 에 cmd_init 재실행 → 모든 커스텀 키/값이 생존한다(통째 덮어쓰기 금지)."""
    conf_path = _init_isolated(board, monkeypatch, tmp_path)
    conf_path.write_text(_CUSTOM_CONF, encoding="utf-8")
    args = argparse.Namespace(prefix=None, area=None, owner=None, session=None)

    assert board.cmd_init(args) == 0

    conf_text = conf_path.read_text(encoding="utf-8")
    # init 이 안 쓰는 사용자/operational 키가 전부 원값 그대로 생존.
    assert "external_review_enabled=false" in conf_text
    assert "reviewer_cmd=codex exec" in conf_text
    assert "upstream=/x" in conf_text
    assert "upstream_rev=abc" in conf_text
    assert "opencode_pro_model=m" in conf_text
    assert "status_total_style=fraction" in conf_text
    assert "user=me@example.com" in conf_text


def test_init_rerun_preserves_custom_ctx_window_tokens(board, monkeypatch, tmp_path):
    """(b) 커스텀 ctx_window_tokens=5000 이 default(200000)로 리셋되지 않는다(없을 때만 추가)."""
    conf_path = _init_isolated(board, monkeypatch, tmp_path)
    conf_path.write_text(_CUSTOM_CONF, encoding="utf-8")
    args = argparse.Namespace(prefix=None, area=None, owner=None, session=None)

    assert board.cmd_init(args) == 0

    conf_text = conf_path.read_text(encoding="utf-8")
    assert "ctx_window_tokens=5000" in conf_text
    assert f"ctx_window_tokens={board.CTX_WINDOW_TOKENS_DEFAULT}" not in conf_text
    # 인자 없는 재실행이므로 기존 session 도 보존.
    assert "session=my-pm" in conf_text


def test_init_absent_writes_full_default(board, monkeypatch, tmp_path):
    """(c) local.conf 부재 시 전체 default 생성(현행 회귀·기본키 존재)."""
    conf_path = _init_isolated(board, monkeypatch, tmp_path)
    assert not conf_path.exists()
    args = argparse.Namespace(prefix=None, area=None, owner=None, session="pm")

    assert board.cmd_init(args) == 0

    conf_text = conf_path.read_text(encoding="utf-8")
    assert "session=pm" in conf_text
    assert "py=" in conf_text and "test_cmd=pytest -q" in conf_text
    assert f"ctx_window_tokens={board.CTX_WINDOW_TOKENS_DEFAULT}" in conf_text
    assert "ctx_nudge_pct=" in conf_text and "ctx_stop_pct=" in conf_text


def test_init_rerun_explicit_session_updates_and_preserves(board, monkeypatch, tmp_path):
    """(d) --session 명시 시 session 만 갱신, 나머지 커스텀 키는 보존(set-or-replace)."""
    conf_path = _init_isolated(board, monkeypatch, tmp_path)
    conf_path.write_text(_CUSTOM_CONF, encoding="utf-8")
    args = argparse.Namespace(prefix=None, area=None, owner=None, session="newsess")

    assert board.cmd_init(args) == 0

    conf_text = conf_path.read_text(encoding="utf-8")
    assert "session=newsess" in conf_text
    assert "session=my-pm" not in conf_text
    # session 갱신은 나머지 커스텀 키를 건드리지 않는다.
    assert "external_review_enabled=false" in conf_text
    assert "upstream=/x" in conf_text
    assert "ctx_window_tokens=5000" in conf_text


# default 키 전부 존재 + external_review_enabled *부재* + 마지막 줄 개행 없음.
# (updates 가 비어 `_set_conf_keys` 가 원문 verbatim 반환 → trailing newline 회귀 재현 조건.)
_NO_TRAILING_NL_CONF = (
    "# per-clone 설정 (git-ignored). board.py init 생성. clone 마다 다름.\n"
    "session=my-pm\n"
    "py=python3\ntest_cmd=pytest -q\nproject_name=myproj\n"
    "ctx_nudge_pct=20\nctx_stop_pct=10\n"
    "ctx_window_tokens=5000"  # ← 마지막 줄·개행 없음(intentional)
)


def test_init_rerun_no_trailing_newline_optin_append_preserves_last_key(
    board, monkeypatch, tmp_path
):
    """codex must-fix: 병합 경로가 개행 없는 local.conf 를 남기면 뒤이은 external_review
    opt-in append 가 마지막 키에 그대로 붙어 기존 키를 변질시킨다. cmd_init 이 write 전
    trailing newline 을 보장해 (a) 마지막 키(ctx_window_tokens=5000)가 온전하고 뒤에 `#` 이
    붙지 않으며 (b) opt-in 블록이 *새 줄*에서 시작함을 검증한다.

    `_init_isolated`(opt-in stub)를 안 쓰고 *실제* prompt_external_review_optin append 를
    태운다 — 대화형 'n' 경로(external_review_enabled=false 를 append)를 결정적으로 재현."""
    conf_path = tmp_path / "local.conf"
    monkeypatch.setattr(board, "LOCAL_CONF", conf_path)
    monkeypatch.setattr(board, "PM_STATE_FILE", tmp_path / "pm_state.md")
    monkeypatch.setattr(board, "PM_STATE_TEMPLATE", tmp_path / "missing-template.md")
    monkeypatch.setattr(board, "install_pre_push_hook", lambda: False)
    # 실 opt-in append 를 태운다 — 대화형 'n'(OFF) 경로를 결정적으로:
    monkeypatch.setattr(board, "_is_noninteractive", lambda: False)
    monkeypatch.setattr(board.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a, **k: "n")

    conf_path.write_text(_NO_TRAILING_NL_CONF, encoding="utf-8")
    args = argparse.Namespace(prefix=None, area=None, owner=None, session=None)

    assert board.cmd_init(args) == 0

    conf_text = conf_path.read_text(encoding="utf-8")
    # (a) 마지막 키가 변질 안 됨 — 값 온전·뒤에 `#`(주석) 안 붙음.
    assert "ctx_window_tokens=5000\n" in conf_text
    assert "ctx_window_tokens=5000#" not in conf_text
    # (b) opt-in 블록이 새 줄에서 시작(external_review_enabled 라인이 온전).
    assert "external_review_enabled=false" in conf_text
    # 파싱 무결성: 값 파트에 `#` 이 섞여 들어가지 않았다.
    assert board.local_config().get("ctx_window_tokens") == "5000"
    assert board.local_config().get("external_review_enabled") == "false"
