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


@pytest.fixture(autouse=True)
def _clear_detect_py_cache(board):
    board._detect_py.cache_clear()
    yield
    board._detect_py.cache_clear()


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


def test_dump_ticket_passes_utf8_encoding_and_untranslated_newlines(
        board, tmp_path, monkeypatch):
    """dump_ticket 의 쓰기 핸들이 encoding='utf-8' + newline='' 를 명시하는지 직접 검증.

    ambient PYTHONUTF8 가 cp949 버그를 가려도 이 단언은 통과하지 못한다 —
    수정 전 코드(encoding 누락)에서 captured['encoding'] 은 None.
    `newline=''`(줄끝 미번역)은 표기 보존 축이다 — 본문이 담은 개행이 그대로 bytes 가 돼야
    CRLF 티켓이 lifecycle 재작성에서 LF 로 뒤집히지 않는다(T-0709).
    """
    captured: dict = {}
    orig = Path.open

    def spy(self, mode="r", *args, **kwargs):
        if self.name.endswith(".md") and "T-9999" in self.name and "w" in mode:
            captured["encoding"] = kwargs.get("encoding")
            captured["newline"] = kwargs.get("newline")
        return orig(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", spy)
    board.dump_ticket(tmp_path / "T-9999-x.md", {"id": "T-9999"}, "body")
    assert captured.get("encoding") == "utf-8"
    assert captured.get("newline") == ""


def test_load_ticket_passes_utf8_encoding(board, tmp_path, monkeypatch):
    """load_ticket 이 판독에 encoding='utf-8' 를 명시하는지 직접 검증.

    판독은 공유 읽기 seam 을 지나므로([[T-0729]]) 관찰도 그 자리에서 한다 — `Path.read_text` 를
    보면 엔진이 그 호출을 더는 하지 않아 이 검증이 공허해진다.
    """
    path = tmp_path / "T-9999-y.md"
    path.write_text("---\nid: T-9999\n---\nbody\n", encoding="utf-8")

    captured: dict = {}
    orig = board.file_lock.read_text_shared

    def spy(target, *args, **kwargs):
        if Path(target).name == "T-9999-y.md":
            captured["encoding"] = kwargs.get("encoding")
        return orig(target, *args, **kwargs)

    monkeypatch.setattr(board.file_lock, "read_text_shared", spy)
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

    assert not conf.exists() or "additional_reviewer.enabled" not in conf.read_text(encoding="utf-8")


def test_prompt_optin_writes_nothing_on_eof_under_tty(board, monkeypatch, tmp_path):
    """isatty=True 인데 input() 이 EOFError (Windows-under-pytest 재현) → 아무것도 안 씀.

    수정 전 코드는 answer='' 로 떨어져 additional_reviewer.enabled=false 를 기록했다 —
    사용자의 기존 true 결정을 덮어 preservation 을 깨뜨림.
    """
    conf = _isolated_local_conf(board, monkeypatch, tmp_path)
    monkeypatch.setattr(board.sys.stdin, "isatty", lambda: True)

    def _raise_eof(prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", _raise_eof)

    board.prompt_external_review_optin()

    assert not conf.exists() or "additional_reviewer.enabled" not in conf.read_text(encoding="utf-8")


def test_prompt_optin_does_not_clobber_existing_true(board, monkeypatch, tmp_path):
    """이미 additional_reviewer.enabled 가 있으면(여기선 true) EOF 경로로도 건드리지 않음."""
    conf = _isolated_local_conf(board, monkeypatch, tmp_path)
    conf.write_text("additional_reviewer.enabled=true\n", encoding="utf-8")
    monkeypatch.setattr(board.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": (_ for _ in ()).throw(EOFError))

    board.prompt_external_review_optin()

    text = conf.read_text(encoding="utf-8")
    assert "additional_reviewer.enabled=true" in text
    assert "additional_reviewer.enabled=false" not in text


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

    assert not conf.exists() or "additional_reviewer.enabled" not in conf.read_text(
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

    assert "additional_reviewer.enabled=true" in conf.read_text(encoding="utf-8")


def test_prompt_optin_no_env_preserves_non_tty_skip(board, monkeypatch, tmp_path):
    """PM_NONINTERACTIVE 미설정 + 비-tty → 기존 isatty 폴백대로 skip(무기록)."""
    conf = _isolated_local_conf(board, monkeypatch, tmp_path)
    monkeypatch.delenv("PM_NONINTERACTIVE", raising=False)
    monkeypatch.setattr(board.sys.stdin, "isatty", lambda: False)

    board.prompt_external_review_optin()

    assert not conf.exists() or "additional_reviewer.enabled" not in conf.read_text(
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
    """훅 write_text 에 encoding='utf-8' 가 명시됐는지 직접 검증 (주석에 한글 포함).

    실제 쓰기는 **원자 교체**라 `pre-push.<pid>.tmp` 에 먼저 떨어진다(실행 중인 훅을 같은 inode
    에 truncate-rewrite 하지 않기 위해·T-0593) — spy 도 그 tmp 를 함께 본다.
    """
    hooks = tmp_path / "hooks"
    monkeypatch.setattr(board, "_hooks_dir", lambda: hooks)
    # 실행검증 seam 을 mock 해 detection 을 which mock 만으로 결정적이게 (실 인터프리터 비의존).
    monkeypatch.setattr(board, "_interp_runs", lambda cmd: True)
    monkeypatch.setattr(board.shutil, "which", lambda cmd: "/usr/bin/python3" if cmd == "python3" else None)

    captured: dict = {}
    orig = Path.write_text

    def spy(self, data, *args, **kwargs):
        if self.name.startswith("pre-push"):
            captured["encoding"] = kwargs.get("encoding")
        return orig(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", spy)
    board.install_pre_push_hook()
    assert captured.get("encoding") == "utf-8"
    assert (hooks / "pre-push").is_file()      # 교체가 실제로 최종 경로에 착지했다


# ── C8: cmd_init 가 local.conf 에 ctx_window_tokens 핸드오프 예산 surface (T-0128) ──

def _init_isolated(board, monkeypatch, tmp_path):
    """cmd_init 을 hermetic 으로: 경로 전역 tmp 재지정 + pm_state·훅·opt-in 부수효과 차단.

    `REPO` 까지 tmp 로 묶는다 — init 은 areas repo 행을 **항상** 등록하므로(T-0779) LOCAL_CONF
    만 격리하면 `areas_file()`·`board_lock()` 이 실 저장소 루트를 잡아 실 areas.md 를 만든다
    (hermetic 위반). 등록 repo 이름은 `REPO.name` 에서 유도되므로 tmp 이름이 곧 그 값이다.
    """
    proj = tmp_path / "proj"
    (proj / ".project_manager" / ".local").mkdir(parents=True, exist_ok=True)
    conf_path = tmp_path / "local.conf"
    monkeypatch.setattr(board, "REPO", proj)
    monkeypatch.setattr(board, "LOCAL_CONF", conf_path)
    monkeypatch.setattr(board, "AREAS_FILE", proj / ".project_manager" / "areas.md")
    monkeypatch.setattr(board, "LOCAL_DIR", proj / ".project_manager" / ".local")
    monkeypatch.setattr(board, "BOARD_LOCK",
                        proj / ".project_manager" / ".local" / "board.lock")
    monkeypatch.setattr(board, "LEASES_FILE",
                        proj / ".project_manager" / ".local" / "worktree-leases.json")
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
    assert f"ctx.window_tokens={board.CTX_WINDOW_TOKENS_DEFAULT}" in conf_text
    assert board.CTX_WINDOW_TOKENS_DEFAULT == 200000
    # nudge/stop pct 옆에 배치됐는지 (기존 ctx 임계와 한 블록).
    assert "ctx.nudge_pct=" in conf_text and "ctx.stop_pct=" in conf_text


def test_init_ctx_budget_is_a_value_not_an_explanation(board, monkeypatch, tmp_path):
    """예산 라인은 **실값 한 줄**이다 — 비용 의미·오버라이드 카탈로그는 출하 문서가 소유한다.

    설명을 conf 에 심으면 값과 어긋난 채 굳는다(T-0767). conf 는 이 clone 이 정한 값만 담고,
    "이 숫자가 무엇인가"는 README 키 카탈로그에서 읽는다.
    """
    conf_path = _init_isolated(board, monkeypatch, tmp_path)
    args = argparse.Namespace(prefix=None, area=None, owner=None, session="pm")

    assert board.cmd_init(args) == 0

    conf_text = conf_path.read_text(encoding="utf-8")
    assert f"ctx.window_tokens={board.CTX_WINDOW_TOKENS_DEFAULT}" in conf_text
    # 설명 산문·주석 예시가 conf 에 없다.
    for marker in ("핸드오프 토큰 예산", "물리 window 아님",
                   "harness.claude.ctx_window_tokens",
                   "harness.opencode.ctx_window_tokens"):
        assert marker not in conf_text, f"conf 에 설명 블록 잔존: {marker}"
    parsed = board.local_config()  # _init_isolated 가 LOCAL_CONF 를 conf_path 로 patch.
    assert parsed.get("ctx.window_tokens") == str(board.CTX_WINDOW_TOKENS_DEFAULT)
    assert not [key for key in parsed if key.startswith("harness.")]


def test_harness_ctx_override_is_documented_in_shipping_docs(board):
    """하네스별 오버라이드 키의 자리는 출하 문서다 — 카탈로그가 사라지지 않았음을 값으로 못박는다."""
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert "harness.<name>.ctx_window_tokens" in readme
    assert "ctx.window_tokens" in readme


# ── C9: cmd_init 재실행 비파괴 병합 — 사용자/operational 키 보존 (T-0184) ──────
# 🔴 데이터 손실 버그: cmd_init 이 local.conf 를 가드 없이 통째 덮어써 재실행 시 init 이
# 안 쓰는 사용자 키(additional_reviewer.enabled·upstream·upstream_rev·harness.opencode.pro_model 등)가
# 소멸하고 커스텀 ctx_window_tokens 가 default 로 리셋됐다. 존재 시 병합으로 수정.

# init 이 안 쓰는 사용자/operational 키 + 커스텀 init 기본키를 담은 기존 local.conf.
_CUSTOM_CONF = (
    "# per-clone 설정 (git-ignored). board.py init 생성. clone 마다 다름.\n"
    "session=my-pm\n"
    "runtime.py=python3\ntest.cmd=pytest -q\nproject.name=myproj\n"
    "ctx.nudge_pct=20\nctx.stop_pct=10\n"
    "ctx.window_tokens=5000\n"
    "# 외부 코드리뷰 (ADR-0004)\n"
    "additional_reviewer.enabled=false\n"
    "additional_reviewer.harness=codex\n"
    "additional_reviewer.model=gpt-5.6-sol\n"
    "upstream.path=/x\nupstream.rev=abc\n"
    "harness.opencode.pro_model=m\n"
    "status_total_style=fraction\n"
    "identity.user=me@example.com\n"
)


def test_init_rerun_preserves_custom_operational_keys(board, monkeypatch, tmp_path):
    """(a) 커스텀 키(additional_reviewer.enabled·upstream·upstream_rev·harness.opencode.pro_model 등)를
    담은 local.conf 에 cmd_init 재실행 → 모든 커스텀 키/값이 생존한다(통째 덮어쓰기 금지)."""
    conf_path = _init_isolated(board, monkeypatch, tmp_path)
    conf_path.write_text(_CUSTOM_CONF, encoding="utf-8")
    args = argparse.Namespace(prefix=None, area=None, owner=None, session=None)

    assert board.cmd_init(args) == 0

    conf_text = conf_path.read_text(encoding="utf-8")
    # init 이 안 쓰는 사용자/operational 키가 전부 원값 그대로 생존.
    assert "additional_reviewer.enabled=false" in conf_text
    assert "additional_reviewer.harness=codex" in conf_text
    assert "additional_reviewer.model=gpt-5.6-sol" in conf_text
    assert "upstream.path=/x" in conf_text
    assert "upstream.rev=abc" in conf_text
    assert "harness.opencode.pro_model=m" in conf_text
    assert "status_total_style=fraction" in conf_text
    assert "identity.user=me@example.com" in conf_text
    # T-0207: 기존 클론의 ctx 임계(20/10)는 디폴트 상향(30/20)이 있어도 불변 —
    # init 은 '없을 때만 추가'라 이미 기록된 값을 덮지 않는다(마이그레이션 영향 0).
    assert "ctx.nudge_pct=20" in conf_text and "ctx.stop_pct=10" in conf_text
    assert "ctx.nudge_pct=30" not in conf_text and "ctx.stop_pct=20" not in conf_text


def test_init_rerun_preserves_custom_ctx_window_tokens(board, monkeypatch, tmp_path):
    """(b) 커스텀 ctx_window_tokens=5000 이 default(200000)로 리셋되지 않는다(없을 때만 추가)."""
    conf_path = _init_isolated(board, monkeypatch, tmp_path)
    conf_path.write_text(_CUSTOM_CONF, encoding="utf-8")
    args = argparse.Namespace(prefix=None, area=None, owner=None, session=None)

    assert board.cmd_init(args) == 0

    conf_text = conf_path.read_text(encoding="utf-8")
    assert "ctx.window_tokens=5000" in conf_text
    assert f"ctx.window_tokens={board.CTX_WINDOW_TOKENS_DEFAULT}" not in conf_text
    # init 이 안 쓰는 키(구 `session=` 포함)는 비파괴 병합으로 byte 보존된다 — 엔진은 채택자
    # conf 를 대신 고쳐 쓰지 않는다(읽지 않을 뿐·T-0779).
    assert "session=my-pm" in conf_text


def test_init_absent_writes_full_default(board, monkeypatch, tmp_path):
    """(c) local.conf 부재 시 전체 default 생성(현행 회귀·기본키 존재)."""
    conf_path = _init_isolated(board, monkeypatch, tmp_path)
    assert not conf_path.exists()
    args = argparse.Namespace(prefix=None, area=None, owner=None, session="pm")

    assert board.cmd_init(args) == 0

    conf_text = conf_path.read_text(encoding="utf-8")
    # 세션·prefix 는 per-clone conf 의 키가 아니다(T-0779) — 정체성은 lease 장부·areas.md.
    assert "session=" not in conf_text and "prefix=" not in conf_text
    assert "runtime.py=" in conf_text
    assert f"test.cmd={board.default_pytest_cmd(board._detect_py())}" in conf_text
    assert f"ctx.window_tokens={board.CTX_WINDOW_TOKENS_DEFAULT}" in conf_text
    assert "ctx.nudge_pct=" in conf_text and "ctx.stop_pct=" in conf_text


def test_init_rerun_explicit_identity_registers_that_repo_and_preserves_conf(
    board, monkeypatch, tmp_path,
):
    """(d) `--repo`/`--slot`(ADR-0057) 명시 → 그 repo 이름으로 등록되고 conf 는 전부 보존된다.

    세션은 conf 키가 아니므로(T-0779) 명시 정체성은 **등록 repo 이름**으로만 나타나고,
    사용자 커스텀 키는 한 줄도 바뀌지 않는다(비파괴 병합).
    """
    conf_path = _init_isolated(board, monkeypatch, tmp_path)
    conf_path.write_text(_CUSTOM_CONF, encoding="utf-8")
    args = argparse.Namespace(prefix=None, area=None, owner=None, repo="newsess", slot=2)

    assert board.cmd_init(args) == 0

    assert board.registered_repos() == {"newsess"}
    conf_text = conf_path.read_text(encoding="utf-8")
    assert "session=my-pm" in conf_text          # init 이 안 쓰는 키는 byte 보존
    assert "session=newsess_2" not in conf_text  # 세션을 conf 에 쓰지 않는다
    assert "additional_reviewer.enabled=false" in conf_text
    assert "upstream.path=/x" in conf_text
    assert "ctx.window_tokens=5000" in conf_text


# default 키 전부 존재 + additional_reviewer.enabled *부재* + 마지막 줄 개행 없음.
# (updates 가 비어 `_set_conf_keys` 가 원문 verbatim 반환 → trailing newline 회귀 재현 조건.)
_NO_TRAILING_NL_CONF = (
    "# per-clone 설정 (git-ignored). board.py init 생성. clone 마다 다름.\n"
    "session=my-pm\n"
    "runtime.py=python3\ntest.cmd=pytest -q\nproject.name=myproj\n"
    "ctx.nudge_pct=20\nctx.stop_pct=10\n"
    "ctx.window_tokens=5000"  # ← 마지막 줄·개행 없음(intentional)
)


def test_init_rerun_no_trailing_newline_optin_append_preserves_last_key(
    board, monkeypatch, tmp_path
):
    """codex must-fix: 병합 경로가 개행 없는 local.conf 를 남기면 뒤이은 external_review
    opt-in append 가 마지막 키에 그대로 붙어 기존 키를 변질시킨다. cmd_init 이 write 전
    trailing newline 을 보장해 (a) 마지막 키(ctx_window_tokens=5000)가 온전하고 뒤에 `#` 이
    붙지 않으며 (b) opt-in 블록이 *새 줄*에서 시작함을 검증한다.

    `_init_isolated`(opt-in stub)를 안 쓰고 *실제* prompt_external_review_optin append 를
    태운다 — 대화형 'n' 경로(additional_reviewer.enabled=false 를 append)를 결정적으로 재현."""
    conf_path = tmp_path / "local.conf"
    monkeypatch.setattr(board, "LOCAL_CONF", conf_path)
    monkeypatch.setattr(board, "PM_STATE_FILE", tmp_path / "pm_state.md")
    monkeypatch.setattr(board, "PM_STATE_TEMPLATE", tmp_path / "missing-template.md")
    monkeypatch.setattr(board, "install_pre_push_hook", lambda: False)
    # init 은 areas repo 행을 **항상** 등록하므로(T-0779) REPO 도 tmp 로 묶어야 hermetic 하다 —
    # 안 묶으면 `areas_file()`·`board_lock()` 이 실 저장소 루트를 잡는다.
    _pm = tmp_path / "proj" / ".project_manager"
    (_pm / ".local").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(board, "REPO", tmp_path / "proj")
    monkeypatch.setattr(board, "AREAS_FILE", _pm / "areas.md")
    monkeypatch.setattr(board, "LOCAL_DIR", _pm / ".local")
    monkeypatch.setattr(board, "BOARD_LOCK", _pm / ".local" / "board.lock")
    monkeypatch.setattr(board, "LEASES_FILE", _pm / ".local" / "worktree-leases.json")
    # 실 opt-in append 를 태운다 — 대화형 'n'(OFF) 경로를 결정적으로:
    monkeypatch.setattr(board, "_is_noninteractive", lambda: False)
    monkeypatch.setattr(board.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a, **k: "n")

    conf_path.write_text(_NO_TRAILING_NL_CONF, encoding="utf-8")
    args = argparse.Namespace(prefix=None, area=None, owner=None, session=None)

    assert board.cmd_init(args) == 0

    conf_text = conf_path.read_text(encoding="utf-8")
    # (a) 마지막 키가 변질 안 됨 — 값 온전·뒤에 `#`(주석) 안 붙음.
    assert "ctx.window_tokens=5000\n" in conf_text
    assert "ctx.window_tokens=5000#" not in conf_text
    # (b) opt-in 블록이 새 줄에서 시작(additional_reviewer.enabled 라인이 온전).
    assert "additional_reviewer.enabled=false" in conf_text
    # 파싱 무결성: 값 파트에 `#` 이 섞여 들어가지 않았다.
    assert board.local_config().get("ctx.window_tokens") == "5000"
    assert board.local_config().get("additional_reviewer.enabled") == "false"
