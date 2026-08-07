"""추가 리뷰어(additional reviewer) 온보딩·카드·활성문서 계약 (T-0590).

사람이 부르는 역할 이름은 **추가 리뷰어**이고, `external_review*`·`external_review_enabled` 는
기계 식별자와 외부 전송·격리·과금 축의 이름으로 남는다. 이 파일이 못박는 3축:

1. **첫 opt-in 계약** — `board.py init` 과 `pm_update` 는 결정이 없을 때만 **1회** 묻고, "예" 면
   `external_review_enabled=true` + `additional_reviewer.harness/model/reasoning` 4키를 원자적으로
   기록한다(`reviewer_cmd` 미생성). 이미 결정(true/false)이 있으면 묻지도, 기존 구조적 튜플·레거시
   `reviewer_cmd` 를 덮지도 않는다. 비대화형은 안전쪽(OFF) + 나중에 켜는 법 1줄.
2. **지속 동의** — `external_review_enabled=true` 는 설정된 외부 전송과 통상 과금에 대한 지속
   의사표시다. 카드·매뉴얼이 리뷰마다·라운드 재개마다 비용을 다시 묻게 하면 안 된다. 라운드/wave
   상한은 기계적 anti-loop 정지이고, 정상 수렴 ack 는 PM 자율이다.
3. **Codex 카드 자족성 / 비누출** — codex 전역 `network_access=false` 아래서 실 전송을 하려면
   `exec_command` 건별 승격이 필요하다. 그 절차는 codex 판 카드에만 있고, claude/opencode 가
   byte-공유하는 카드에는 Codex tool metadata 가 새면 안 된다.

값 드리프트를 테스트로 막는 이유: 실행 해소(하네스→실 명령)는 external_review 코어가 하고
board/pm_update 는 값만 시드한다(무거운 코어를 온보딩 경로로 끌어오지 않는다). 그래서 두 진입의
기본 프로필이 서로 어긋나도 런타임이 즉시 알려주지 않는다 — 여기서 잡는다.
"""
from __future__ import annotations

import argparse
import importlib.util
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
TEMPLATE_DIRS = ("claude_code", "codex", "opencode")

# 첫 opt-in 이 심어야 하는 정확한 4키 (순서 포함). 엔진 상수를 읽지 않고 여기 리터럴로 둔다 —
# 상수와 함께 조용히 바뀌면 가드가 아니다.
EXPECTED_DEFAULTS = (
    ("external_review_enabled", "true"),
    ("additional_reviewer.harness", "codex"),
    ("additional_reviewer.model", "gpt-5.6-sol"),
    ("additional_reviewer.reasoning", "max"),
)

CANONICAL_PM_REVIEW = REPO / ".claude" / "skills" / "pm-review" / "SKILL.md"
CODEX_PM_REVIEW = (
    REPO / "templates" / "codex" / ".agents" / "skills" / "pm-review" / "SKILL.md"
)
SHARED_PM_REVIEW_CARDS = (
    CANONICAL_PM_REVIEW,
    REPO / "templates" / "claude_code" / ".claude" / "skills" / "pm-review" / "SKILL.md",
    REPO / "templates" / "opencode" / ".claude" / "skills" / "pm-review" / "SKILL.md",
)


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def board():
    return _load("board")


@pytest.fixture(scope="module")
def pm_update():
    return _load("pm_update")


def _parse_conf(text: str) -> dict[str, str]:
    """local.conf 활성 키만 파싱(주석 제외·last-wins) — 엔진 reader 와 동치."""
    conf: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        conf[key.strip()] = value.strip()
    return conf


# ── 축 1: 두 온보딩 진입의 기본 프로필 동일성 ────────────────────────────────

def test_default_profile_is_the_documented_tuple(board, pm_update):
    """board·pm_update 가 같은 4키를 같은 순서로 심는다 + 값이 문서와 일치."""
    assert board.ADDITIONAL_REVIEWER_DEFAULTS == EXPECTED_DEFAULTS
    assert pm_update.ADDITIONAL_REVIEWER_DEFAULTS == EXPECTED_DEFAULTS


def test_optin_block_writes_four_keys_and_no_reviewer_cmd(board, pm_update):
    """opt-in 블록의 활성 키 = 정확히 그 4키. 레거시 reviewer_cmd 는 만들지 않는다."""
    for module in (board, pm_update):
        conf = _parse_conf(module.ADDITIONAL_REVIEWER_OPTIN_BLOCK)
        assert list(conf.items()) == list(EXPECTED_DEFAULTS)
        assert "reviewer_cmd" not in module.ADDITIONAL_REVIEWER_OPTIN_BLOCK
        # 지속 동의를 블록 주석에 박아 둔다 — conf 를 읽는 사람이 재승인 규율을 오해하지 않게.
        assert "지속 동의" in module.ADDITIONAL_REVIEWER_OPTIN_BLOCK


# ── 축 1: board.py init 첫 opt-in (yes/no/비대화/이미결정) ───────────────────

def _isolated_conf(board, monkeypatch, tmp_path, text: str | None = None) -> Path:
    conf = tmp_path / "local.conf"
    if text is not None:
        conf.write_text(text, encoding="utf-8")
    monkeypatch.setattr(board, "LOCAL_CONF", conf)
    monkeypatch.delenv("PM_NONINTERACTIVE", raising=False)
    return conf


def test_board_optin_yes_seeds_exact_tuple(board, monkeypatch, tmp_path, capsys):
    """'y' → 4키 원자 기록·reviewer_cmd 부재·프로필을 사용자에게 표면화."""
    conf = _isolated_conf(board, monkeypatch, tmp_path)
    monkeypatch.setattr(board.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")

    board.prompt_external_review_optin()

    parsed = _parse_conf(conf.read_text(encoding="utf-8"))
    assert list(parsed.items()) == list(EXPECTED_DEFAULTS)
    assert "reviewer_cmd" not in conf.read_text(encoding="utf-8")
    out = capsys.readouterr().out
    assert "추가 리뷰어 ON" in out and "gpt-5.6-sol" in out


def test_board_optin_no_records_false_without_inventing_target(
    board, monkeypatch, tmp_path
):
    """'n' → external_review_enabled=false 만. 하네스/모델/명령을 지어내지 않는다."""
    conf = _isolated_conf(board, monkeypatch, tmp_path)
    monkeypatch.setattr(board.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")

    board.prompt_external_review_optin()

    text = conf.read_text(encoding="utf-8")
    assert _parse_conf(text) == {"external_review_enabled": "false"}
    assert "reviewer_cmd" not in text
    assert "additional_reviewer." not in text


def test_board_optin_noninteractive_is_off_with_enable_hint(
    board, monkeypatch, tmp_path, capsys
):
    """비대화형 → 무기록(안전쪽 OFF) + 나중에 켜는 법 안내 1줄."""
    conf = _isolated_conf(board, monkeypatch, tmp_path)
    monkeypatch.setenv("PM_NONINTERACTIVE", "1")
    monkeypatch.setattr(board.sys.stdin, "isatty", lambda: True)  # 거짓 tty 보고
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt="": pytest.fail("비대화형인데 질문함 — 첫 opt-in 계약 위반"),
    )

    board.prompt_external_review_optin()

    assert not conf.exists()
    out = capsys.readouterr().out
    assert "external_review_enabled=true" in out
    assert "additional_reviewer.harness/model/reasoning" in out


@pytest.mark.parametrize(
    "existing",
    [
        pytest.param(
            "external_review_enabled=true\nreviewer_cmd=my-reviewer --flag\n",
            id="legacy-reviewer-cmd",
        ),
        pytest.param(
            "external_review_enabled=true\n"
            "additional_reviewer.harness=opencode\n"
            "additional_reviewer.model=qwen3-coder-next\n"
            "additional_reviewer.reasoning=low\n",
            id="user-structured-tuple",
        ),
        pytest.param("external_review_enabled=false\n", id="declined"),
    ],
)
def test_board_optin_never_reasks_or_overwrites_a_decision(
    board, monkeypatch, tmp_path, existing
):
    """이미 결정됐으면(true/false 무관) 묻지 않고 byte 단위로 그대로 둔다."""
    conf = _isolated_conf(board, monkeypatch, tmp_path, existing)
    monkeypatch.setattr(board.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(
        "builtins.input", lambda prompt="": pytest.fail("이미 결정됐는데 재질문함")
    )

    board.prompt_external_review_optin()

    assert conf.read_text(encoding="utf-8") == existing


# ── 축 1: pm_update 온보딩 (같은 계약·같은 튜플) ─────────────────────────────

def _dest_with_conf(tmp_path, text: str) -> tuple[Path, Path]:
    dest = tmp_path / "dest"
    conf = dest / ".project_manager" / "local.conf"
    conf.parent.mkdir(parents=True)
    conf.write_text(text, encoding="utf-8")
    return dest, conf


def test_pm_update_optin_yes_seeds_exact_tuple(pm_update, monkeypatch, tmp_path):
    """pm_update 'y' → board 와 동일한 4키·reviewer_cmd 부재."""
    dest, conf = _dest_with_conf(tmp_path, "session=pm\n")
    monkeypatch.delenv("PM_NONINTERACTIVE", raising=False)
    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": lambda self: True})())
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")

    pm_update.maybe_prompt_external_review(dest)

    parsed = _parse_conf(conf.read_text(encoding="utf-8"))
    for key, value in EXPECTED_DEFAULTS:
        assert parsed[key] == value
    assert parsed["session"] == "pm"                       # 기존 키 불변
    assert "reviewer_cmd" not in conf.read_text(encoding="utf-8")


def test_pm_update_optin_no_records_false_only(pm_update, monkeypatch, tmp_path):
    """pm_update 'n' → false 만 기록(대상 지어내지 않음)."""
    dest, conf = _dest_with_conf(tmp_path, "session=pm\n")
    monkeypatch.delenv("PM_NONINTERACTIVE", raising=False)
    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": lambda self: True})())
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")

    pm_update.maybe_prompt_external_review(dest)

    text = conf.read_text(encoding="utf-8")
    assert _parse_conf(text)["external_review_enabled"] == "false"
    assert "additional_reviewer." not in text


def test_pm_update_optin_appends_safely_to_newlineless_conf(
    pm_update, monkeypatch, tmp_path
):
    """마지막 개행 없는 conf 에 append 해도 기존 키가 변질되지 않는다."""
    dest, conf = _dest_with_conf(tmp_path, "session=pm\nupstream_rev=abc123")
    monkeypatch.delenv("PM_NONINTERACTIVE", raising=False)
    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": lambda self: True})())
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")

    pm_update.maybe_prompt_external_review(dest)

    parsed = _parse_conf(conf.read_text(encoding="utf-8"))
    assert parsed["upstream_rev"] == "abc123"
    assert parsed["additional_reviewer.harness"] == "codex"


def test_pm_update_optin_noninteractive_is_off_with_enable_hint(
    pm_update, monkeypatch, tmp_path, capsys
):
    """pm_update 비대화형 → 무기록 + 켜는 법 안내."""
    dest, conf = _dest_with_conf(tmp_path, "session=pm\n")
    before = conf.read_text(encoding="utf-8")
    monkeypatch.setenv("PM_NONINTERACTIVE", "1")
    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": lambda self: True})())
    monkeypatch.setattr(
        "builtins.input", lambda prompt="": pytest.fail("비대화형인데 질문함")
    )

    pm_update.maybe_prompt_external_review(dest)

    assert conf.read_text(encoding="utf-8") == before
    assert "additional_reviewer.harness/model/reasoning" in capsys.readouterr().out


@pytest.mark.parametrize(
    "existing",
    [
        pytest.param(
            "external_review_enabled=true\nreviewer_cmd=my-reviewer --flag\n",
            id="legacy-reviewer-cmd",
        ),
        pytest.param(
            "external_review_enabled=true\nadditional_reviewer.harness=claude\n"
            "additional_reviewer.model=opus\n",
            id="user-structured-tuple",
        ),
        pytest.param("external_review_enabled=false\n", id="declined"),
    ],
)
def test_pm_update_optin_never_reasks_or_overwrites(
    pm_update, monkeypatch, tmp_path, existing
):
    """pm_update 도 결정된 conf 를 재질문·수정하지 않는다(byte 보존)."""
    dest, conf = _dest_with_conf(tmp_path, existing)
    monkeypatch.delenv("PM_NONINTERACTIVE", raising=False)
    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": lambda self: True})())
    monkeypatch.setattr(
        "builtins.input", lambda prompt="": pytest.fail("이미 결정됐는데 재질문함")
    )

    pm_update.maybe_prompt_external_review(dest)

    assert conf.read_text(encoding="utf-8") == existing


# ── 축 1: "이미 결정됨" 판정면 = 파싱된 활성 키 (주석·문장·유사 키 아님) ──────
#
# 판정을 conf **raw 텍스트 substring** 으로 하면 주석 한 줄(`# external_review_enabled=false`)
# 이나 무관한 값 안의 같은 문자열이 "이미 결정됨"으로 읽힌다. 그러면 켜려던 채택자는 질문도
# (대화형) 안내도(비대화형) 못 받고, 결정은 영영 기록되지 않는다. 두 진입 모두 local_config
# 파싱 의미로 판정해야 한다 — 주석/빈 줄/`=` 없는 줄 제외 · key·value strip · 중복 last-wins.

# 활성 키 부재 = **미결정**. 파싱 규칙의 각 조항이 하나씩 대응한다.
UNDECIDED_CONFS = (
    pytest.param(
        "# external_review_enabled=false\nsession=pm\n", id="commented-out-decision"
    ),
    pytest.param(
        "# 켜려면 external_review_enabled=true 로 바꾼다\nsession=pm\n",
        id="prose-comment-naming-the-key",
    ),
    pytest.param(
        "session=pm\nnot_external_review_enabled=true\n",
        id="other-key-ending-with-the-name",
    ),
    pytest.param(
        "test_cmd=pytest -k external_review_enabled\n",
        id="key-name-inside-another-value",
    ),
)

# 활성 키 존재 = **결정됨**(값 무관). 공백 패딩·중복 키도 local_config 와 같게 본다.
DECIDED_CONFS = (
    pytest.param("external_review_enabled=true\n", id="plain-true"),
    pytest.param("external_review_enabled=false\n", id="plain-false"),
    pytest.param(
        "  external_review_enabled=true  \nsession=pm\n", id="whitespace-padded-key"
    ),
    pytest.param(
        "external_review_enabled=false\nexternal_review_enabled=true\n",
        id="duplicate-keys-last-wins",
    ),
)

# 비대화형 경로가 남겨야 하는 "나중에 켜는 법" 문장 (엔진 상수와 별도 리터럴 — 함께 조용히
# 바뀌면 가드가 아니다).
ENABLE_HINT_TEXT = (
    "local.conf 에 external_review_enabled=true + "
    "additional_reviewer.harness/model/reasoning"
)


def _tty(monkeypatch, answer_or_fail):
    """대화형 stdin + input 을 주입한다. answer_or_fail 이 str 이면 그 응답, 아니면 호출 즉시 실패."""
    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": lambda self: True})())
    if isinstance(answer_or_fail, str):
        asked: list[str] = []
        monkeypatch.setattr(
            "builtins.input", lambda prompt="": (asked.append(prompt) or answer_or_fail)
        )
        return asked
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail(answer_or_fail))
    return []


@pytest.mark.parametrize("original", UNDECIDED_CONFS)
def test_pm_update_optin_undecided_conf_prompts_and_seeds_exact_tuple(
    pm_update, monkeypatch, tmp_path, original
):
    """활성 키가 없으면 pm_update 는 묻고, 'y' 는 정확히 4키를 append 한다(기존 줄 불변)."""
    dest, conf = _dest_with_conf(tmp_path, original)
    monkeypatch.delenv("PM_NONINTERACTIVE", raising=False)
    asked = _tty(monkeypatch, "y")

    pm_update.maybe_prompt_external_review(dest)

    assert asked, "활성 키가 없는데 질문하지 않음 — 주석/문장이 결정을 가로챘다"
    text = conf.read_text(encoding="utf-8")
    assert text.startswith(original), "기존 conf 를 덮어씀(append 계약 위반)"
    parsed = _parse_conf(text)
    for key, value in EXPECTED_DEFAULTS:
        assert parsed[key] == value, f"{key} 미기록/오값: {parsed.get(key)!r}"
    assert "reviewer_cmd" not in text, "신규 온보딩이 레거시 키를 만들었다"


@pytest.mark.parametrize("original", UNDECIDED_CONFS)
def test_pm_update_optin_undecided_conf_noninteractive_hints_without_write(
    pm_update, monkeypatch, tmp_path, capsys, original
):
    """활성 키가 없고 비대화형이면 write 0 + 나중에 켜는 법 1줄(안내마저 삼키면 안 된다)."""
    dest, conf = _dest_with_conf(tmp_path, original)
    monkeypatch.setenv("PM_NONINTERACTIVE", "1")
    _tty(monkeypatch, "비대화형인데 질문함")

    pm_update.maybe_prompt_external_review(dest)

    assert conf.read_text(encoding="utf-8") == original, "비대화형인데 conf 를 건드렸다"
    out = capsys.readouterr().out
    assert "추가 리뷰어" in out
    assert ENABLE_HINT_TEXT in out, f"켜는 법 안내 누락: {out!r}"


@pytest.mark.parametrize("original", DECIDED_CONFS)
def test_pm_update_optin_active_key_is_decided_whatever_the_spacing(
    pm_update, monkeypatch, tmp_path, capsys, original
):
    """활성 키가 있으면(공백 패딩·중복 포함) 무질문·무발화·byte 보존."""
    dest, conf = _dest_with_conf(tmp_path, original)
    monkeypatch.delenv("PM_NONINTERACTIVE", raising=False)
    _tty(monkeypatch, "이미 결정됐는데 재질문함")

    pm_update.maybe_prompt_external_review(dest)

    assert conf.read_text(encoding="utf-8") == original
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("original", UNDECIDED_CONFS)
def test_board_optin_undecided_conf_prompts_and_seeds_exact_tuple(
    board, monkeypatch, tmp_path, original
):
    """board 진입도 같은 판정면 — 주석/문장은 결정이 아니다(pm_update 와 동일 계약)."""
    conf = _isolated_conf(board, monkeypatch, tmp_path, original)
    monkeypatch.setattr(board.sys.stdin, "isatty", lambda: True)
    asked: list[str] = []
    monkeypatch.setattr("builtins.input", lambda prompt="": (asked.append(prompt) or "y"))

    board.prompt_external_review_optin()

    assert asked, "활성 키가 없는데 질문하지 않음"
    text = conf.read_text(encoding="utf-8")
    assert text.startswith(original)
    parsed = _parse_conf(text)
    for key, value in EXPECTED_DEFAULTS:
        assert parsed[key] == value
    assert "reviewer_cmd" not in text


@pytest.mark.parametrize("original", UNDECIDED_CONFS)
def test_board_optin_undecided_conf_noninteractive_hints_without_write(
    board, monkeypatch, tmp_path, capsys, original
):
    """board 비대화형도 write 0 + 켜는 법 안내."""
    conf = _isolated_conf(board, monkeypatch, tmp_path, original)
    monkeypatch.setenv("PM_NONINTERACTIVE", "1")
    monkeypatch.setattr(board.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(
        "builtins.input", lambda prompt="": pytest.fail("비대화형인데 질문함")
    )

    board.prompt_external_review_optin()

    assert conf.read_text(encoding="utf-8") == original
    assert ENABLE_HINT_TEXT in capsys.readouterr().out


@pytest.mark.parametrize("original", DECIDED_CONFS)
def test_board_optin_active_key_is_decided_whatever_the_spacing(
    board, monkeypatch, tmp_path, capsys, original
):
    """board 도 공백 패딩·중복 키를 local_config 와 같게 '결정됨'으로 본다."""
    conf = _isolated_conf(board, monkeypatch, tmp_path, original)
    monkeypatch.setattr(board.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(
        "builtins.input", lambda prompt="": pytest.fail("이미 결정됐는데 재질문함")
    )

    board.prompt_external_review_optin()

    assert conf.read_text(encoding="utf-8") == original
    assert capsys.readouterr().out == ""


def test_optin_decision_matches_local_config_key_presence(board, pm_update, tmp_path):
    """위 두 표의 분류가 **엔진 reader 의 키 인식**과 정확히 같은지 대조한다.

    온보딩 판정면은 `board.local_config` / `pm_update._read_local_conf` 의 키 존재여야 한다.
    표가 계약을 잘못 그렸거나 두 reader 가 서로 갈라지면 여기서 먼저 깨진다.
    """
    cases = [(param.values[0], False) for param in UNDECIDED_CONFS]
    cases += [(param.values[0], True) for param in DECIDED_CONFS]
    for index, (text, decided) in enumerate(cases):
        tree = tmp_path / f"case{index}"
        conf = tree / ".project_manager" / "local.conf"
        conf.parent.mkdir(parents=True)
        conf.write_text(text, encoding="utf-8")
        assert ("external_review_enabled" in board.local_config(tree)) is decided, text
        assert ("external_review_enabled" in pm_update._read_local_conf(conf)) is decided, text
        assert ("external_review_enabled" in _parse_conf(text)) is decided, text


# ── 축 1: 변경 0 수렴 실행(RUN2)도 첫 opt-in 을 배달한다 (진입점 계약) ────────
#
# 위 단위들은 helper 를 직접 부른다 — helper 가 옳아도 `_main` 이 그걸 has-changes 경로에서만
# 부르면 계약은 배달되지 않는다. 추가 리뷰어를 실은 엔진을 이미 흡수한 채택자는 그 다음 실행부터
# 영구히 `changes == 0` 이라, 미결정이면 질문도 안내도 한 번도 못 받는다(훅 재설치·진입 doc
# 전환이 changes 와 독립인 것과 같은 논거). 그래서 진입점 자체를 여기서 못박는다 —
# 발화 경계는 has-changes 경로와 같다: 비-dry-run · `--paths` 아님 · 어댑터 config red 아님.

SENTINEL_REL = ".project_manager/tools/__pm_update_upstream_sentinel__.py"
SENTINEL_BODY = "# upstream sentinel\n"
CONVERGED_REV = "converged-rev-1"


def _zero_change_tree(tmp_path, conf_text: str) -> tuple[Path, Path, Path]:
    """엔진 변경 0 인 채택자 트리(dest) + 그 상류(source) → (dest, source, local.conf).

    dest 가 상류 등재 파일을 이미 같은 바이트로 갖고 있어 `_main` 은 `not changes` 경로로 간다.
    revision 두 키도 상류 HEAD(스텁값)와 맞춰 둬 conf 변화는 opt-in 응답분만 남는다(수렴 write
    와 섞이지 않게).
    """
    source = tmp_path / "upstream"
    sentinel = source / SENTINEL_REL
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text(SENTINEL_BODY, encoding="utf-8")
    (source / ".project_manager" / "engine.manifest").write_text(
        SENTINEL_REL + "\n", encoding="utf-8")
    # tracked checkout 으로 만들어 directory-manifest fallback 경고를 없앤다(다른 진입점
    # 테스트와 같은 fixture 규약).
    subprocess.run(["git", "-C", str(source), "init", "-q"],
                   capture_output=True, text=True, check=True)
    subprocess.run(["git", "-C", str(source), "add", "-f", "-A"],
                   capture_output=True, text=True, check=True)

    dest = tmp_path / "adopter"
    dest_sentinel = dest / SENTINEL_REL
    dest_sentinel.parent.mkdir(parents=True, exist_ok=True)
    dest_sentinel.write_text(SENTINEL_BODY, encoding="utf-8")
    conf = dest / ".project_manager" / "local.conf"
    conf.write_text(
        f"upstream={source}\n"
        f"upstream_rev={CONVERGED_REV}\nupstream_seen_rev={CONVERGED_REV}\n"
        + conf_text,
        encoding="utf-8",
    )
    return dest, source, conf


def _run_zero_change(pm_update, monkeypatch, dest, argv=()) -> int:
    """`_main` 을 self-location 모드로 실행 — REPO 와 upstream rev 읽기를 결정적으로 고정."""
    monkeypatch.setattr(pm_update, "REPO", dest)
    pm_import = pm_update._load_pm_import()
    monkeypatch.setattr(pm_import, "read_upstream_rev", lambda *a, **k: CONVERGED_REV)
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)
    return pm_update.main(list(argv))


def _spy_optin(pm_update, monkeypatch) -> list[Path]:
    """maybe_prompt_external_review 호출 인자를 기록(실 helper 는 그대로 실행)."""
    real = pm_update.maybe_prompt_external_review
    calls: list[Path] = []

    def spy(dest_root):
        calls.append(Path(dest_root))
        return real(dest_root)

    monkeypatch.setattr(pm_update, "maybe_prompt_external_review", spy)
    return calls


def test_main_zero_change_prompts_undecided_adopter_exactly_once(
    pm_update, monkeypatch, tmp_path, capsys
):
    """변경 0 · 대화형 · 미결정 → 첫 질문이 정확히 1회 오고 'y' 가 4키를 심는다."""
    dest, _source, conf = _zero_change_tree(tmp_path, "session=pm\n")
    monkeypatch.delenv("PM_NONINTERACTIVE", raising=False)
    _tty(monkeypatch, "y")
    calls = _spy_optin(pm_update, monkeypatch)

    assert _run_zero_change(pm_update, monkeypatch, dest) == 0

    assert "최신 — 변경 없음." in capsys.readouterr().out, "변경 0 경로가 아니다(fixture 오류)"
    assert calls == [dest], f"변경 0 실행의 추가 리뷰어 opt-in 호출: {calls!r} (기대 1회)"
    parsed = _parse_conf(conf.read_text(encoding="utf-8"))
    for key, value in EXPECTED_DEFAULTS:
        assert parsed[key] == value, f"{key} 미기록/오값: {parsed.get(key)!r}"
    assert parsed["session"] == "pm"                       # 기존 키 불변
    # 짝인 delegate opt-in 도 같은 자리에서 계속 발화한다(둘 중 하나만 남기지 않는다).
    assert parsed["delegate_enabled"] == "true"


def test_main_zero_change_noninteractive_hints_without_write(
    pm_update, monkeypatch, tmp_path, capsys
):
    """변경 0 · 비대화형 · 미결정 → conf write 0 + 나중에 켜는 법 1줄."""
    dest, _source, conf = _zero_change_tree(tmp_path, "session=pm\n")
    before = conf.read_text(encoding="utf-8")
    monkeypatch.setenv("PM_NONINTERACTIVE", "1")
    _tty(monkeypatch, "비대화형인데 질문함")
    calls = _spy_optin(pm_update, monkeypatch)

    assert _run_zero_change(pm_update, monkeypatch, dest) == 0

    assert calls == [dest]
    assert conf.read_text(encoding="utf-8") == before, "비대화형인데 conf 를 건드렸다"
    assert ENABLE_HINT_TEXT in capsys.readouterr().out


def test_main_zero_change_does_not_reask_decided_adopter(
    pm_update, monkeypatch, tmp_path
):
    """이미 결정된 채택자는 변경 0 실행이 반복돼도 무질문·byte 보존(재질문 없음)."""
    decided = "session=pm\nexternal_review_enabled=false\ndelegate_enabled=false\n"
    dest, _source, conf = _zero_change_tree(tmp_path, decided)
    before = conf.read_text(encoding="utf-8")
    monkeypatch.delenv("PM_NONINTERACTIVE", raising=False)
    _tty(monkeypatch, "이미 결정됐는데 재질문함")

    assert _run_zero_change(pm_update, monkeypatch, dest) == 0
    assert _run_zero_change(pm_update, monkeypatch, dest) == 0

    assert conf.read_text(encoding="utf-8") == before


def test_main_zero_change_dry_run_does_not_prompt(pm_update, monkeypatch, tmp_path):
    """--dry-run 은 판정 실행이다 — 미결정이어도 묻지 않고 conf 를 쓰지 않는다."""
    dest, _source, conf = _zero_change_tree(tmp_path, "session=pm\n")
    before = conf.read_text(encoding="utf-8")
    monkeypatch.delenv("PM_NONINTERACTIVE", raising=False)
    _tty(monkeypatch, "dry-run 인데 질문함")
    calls = _spy_optin(pm_update, monkeypatch)

    assert _run_zero_change(pm_update, monkeypatch, dest, ["--dry-run"]) == 0

    assert calls == [], "dry-run 에서 opt-in 이 발화했다(무write 계약 위반)"
    assert conf.read_text(encoding="utf-8") == before


def test_main_zero_change_scoped_paths_does_not_prompt(pm_update, monkeypatch, tmp_path):
    """--paths(부분 전파)는 요청 밖 write 를 하지 않는다 — 온보딩 질문도 그 밖이다."""
    dest, _source, conf = _zero_change_tree(tmp_path, "session=pm\n")
    before = conf.read_text(encoding="utf-8")
    monkeypatch.delenv("PM_NONINTERACTIVE", raising=False)
    _tty(monkeypatch, "경로 스코프인데 질문함")
    calls = _spy_optin(pm_update, monkeypatch)

    assert _run_zero_change(
        pm_update, monkeypatch, dest, ["--paths", SENTINEL_REL]) == 0

    assert calls == [], "경로 스코프 실행에서 opt-in 이 발화했다"
    assert conf.read_text(encoding="utf-8") == before


def test_main_zero_change_adapter_config_red_does_not_prompt(
    pm_update, monkeypatch, tmp_path, capsys
):
    """어댑터 config red(rc1)면 질문 전에 중단한다 — 미수렴을 성공 프롬프트로 덮지 않는다."""
    dest, _source, conf = _zero_change_tree(tmp_path, "session=pm\n")
    before = conf.read_text(encoding="utf-8")
    monkeypatch.delenv("PM_NONINTERACTIVE", raising=False)
    _tty(monkeypatch, "어댑터 config red 인데 질문함")
    monkeypatch.setattr(pm_update, "_has_adapter_config_candidate", lambda *a, **k: True)
    monkeypatch.setattr(
        pm_update, "sync_adapter_configs",
        lambda *a, **k: {"status": "ok", "managed_converged": False,
                         "updated": [], "preserved": [], "drift": [],
                         "backfilled": [], "degraded": []},
    )
    calls = _spy_optin(pm_update, monkeypatch)

    assert _run_zero_change(pm_update, monkeypatch, dest) == 1

    assert calls == [], "adapter config red 인데 opt-in 이 발화했다"
    assert conf.read_text(encoding="utf-8") == before
    assert "미수렴" in capsys.readouterr().err


# ── 축 1: fresh init 이 온보딩 결정을 비파괴로 흡수 ──────────────────────────

def test_board_init_merge_preserves_existing_profile(board, monkeypatch, tmp_path):
    """기존 conf 가 있는 홈에 init 을 다시 돌려도 추가 리뷰어 프로필은 그대로다."""
    existing = (
        "session=mine\n"
        "external_review_enabled=true\n"
        "additional_reviewer.harness=opencode\n"
        "additional_reviewer.model=qwen3-coder-next\n"
        "additional_reviewer.reasoning=low\n"
        "additional_reviewer.persona=security\n"
    )
    conf = _isolated_conf(board, monkeypatch, tmp_path, existing)
    monkeypatch.setenv("PM_NONINTERACTIVE", "1")
    monkeypatch.setattr(board, "REPO", tmp_path)
    monkeypatch.setattr(board, "PM_STATE_FILE", tmp_path / "pm_state.md")
    monkeypatch.setattr(board, "PM_STATE_TEMPLATE", tmp_path / "pm_state.template.md")
    monkeypatch.setattr(board, "install_pre_push_hook", lambda: False)
    monkeypatch.setattr(board, "_configure_board_submodule", lambda: False)

    rc = board.cmd_init(
        argparse.Namespace(
            prefix=None, area=None, owner=None, repo=None, slot=None, user=None
        )
    )
    assert rc == 0

    parsed = _parse_conf(conf.read_text(encoding="utf-8"))
    assert parsed["external_review_enabled"] == "true"
    assert parsed["additional_reviewer.harness"] == "opencode"
    assert parsed["additional_reviewer.model"] == "qwen3-coder-next"
    assert parsed["additional_reviewer.reasoning"] == "low"
    assert parsed["additional_reviewer.persona"] == "security"


# ── 축 3: Codex 카드 자족성 (network_access=false 아래 건별 승격) ────────────

def test_codex_pm_review_card_is_self_contained_for_egress():
    """codex 판 카드가 dry-run → 도구 승격 → 좁은 prefix 순서를 자족적으로 싣는다."""
    text = CODEX_PM_REVIEW.read_text(encoding="utf-8")
    for contract in (
        "Codex egress 건별 승격 (load-bearing)",
        "network_access=false",
        "Codex egress: escalation required",
        'sandbox_permissions="require_escalated"',
        "--codex-egress-escalated",
        "호출층 attestation",
        'prefix_rule=["python3", ".project_manager/tools/external_review.py"]',
        'prefix_rule=["py", ".project_manager/tools/external_review.py"]',
        "sandbox_workspace_write.network_access=true",
    ):
        assert contract in text, f"Codex egress 계약 누락: {contract}"

    # 순서: sandbox dry-run 선행 → 도구 승격 메타데이터 → argv attestation.
    dry_run_i = text.index("`--dry-run`을 실행")
    permission_i = text.index('sandbox_permissions="require_escalated"')
    attestation_i = text.index("--codex-egress-escalated", permission_i)
    assert dry_run_i < permission_i < attestation_i
    # 승인 prefix 는 정확히 2 token — Python 전체/인자 전체 승인 금지.
    assert 'prefix_rule=["python3"]' not in text
    assert 'prefix_rule=["python3", ".project_manager/tools/' in text


def test_codex_pm_review_card_states_durable_consent_and_mechanical_caps():
    """지속 동의 + 라운드 상한의 PM 자율 ack 가 codex 카드에 명시된다."""
    text = CODEX_PM_REVIEW.read_text(encoding="utf-8")
    assert "external_review_enabled=true" in text
    assert "후속 호출마다 비용을 다시 묻지 않는다" in text
    assert "기계적 anti-loop 정지" in text
    assert "--rounds-report" in text
    assert "PM이 자율로 `--ack-rounds`" in text
    # 폐기된 규율: 재개 때마다 사용자 승인을 요구하던 문장.
    assert "승인 없이 `--ack-rounds` 금지" not in text
    assert "사용자가 계속을 승인한 경우에만" not in text


def test_codex_pm_review_card_uses_codex_skill_entry_and_active_role_name():
    """codex 판은 `$pm-review` 진입 표기 + 활성 역할 이름(추가 리뷰어)을 쓴다."""
    text = CODEX_PM_REVIEW.read_text(encoding="utf-8")
    assert "# $pm-review — 추가 리뷰어 교차검증 게이트" in text
    assert "/pm-review —" not in text
    assert "additional reviewer" in text
    for key, value in EXPECTED_DEFAULTS:
        assert f"{key}={value}" in text, f"카드가 프로필 튜플을 안 싣는다: {key}"


def test_codex_egress_metadata_does_not_leak_into_shared_cards():
    """Codex tool metadata 는 claude/opencode 가 byte-공유하는 카드의 계약이 아니다."""
    for path in SHARED_PM_REVIEW_CARDS:
        text = path.read_text(encoding="utf-8")
        assert 'sandbox_permissions="require_escalated"' not in text, path
        assert "--codex-egress-escalated" not in text, path
        assert "exec_command(" not in text, path
        assert "$pm-review" not in text, path


def test_shared_pm_review_cards_use_active_role_and_durable_consent():
    """공용 카드도 추가 리뷰어 역할·지속 동의·PM 자율 ack 규율을 고정한다."""
    for path in SHARED_PM_REVIEW_CARDS:
        text = path.read_text(encoding="utf-8")
        assert "# /pm-review — 추가 리뷰어 교차검증 게이트" in text, path
        assert "additional reviewer" in text, path
        for key, value in EXPECTED_DEFAULTS:
            assert f"{key}={value}" in text, (path, key)
        assert "기계적 anti-loop 정지" in text, path
        assert "PM이 자율로 `--ack-rounds`" in text, path
        assert "승인 없이 `--ack-rounds` 금지" not in text, path
        assert "사용자가 계속을 승인한 경우에만" not in text, path
        assert "후속 호출마다 비용을 다시 묻지 않는다" not in text, path
        assert "리뷰마다·라운드 상한 재개마다 사용자에게 비용을 다시 묻지 않는다" in text, path


def test_shared_pm_review_cards_stay_byte_identical():
    """canonical ↔ claude/opencode 템플릿 미러는 byte-identical(전파 무드리프트)."""
    canonical = CANONICAL_PM_REVIEW.read_bytes()
    for path in SHARED_PM_REVIEW_CARDS[1:]:
        assert path.read_bytes() == canonical, f"pm-review 카드 드리프트: {path}"


def test_codex_pm_review_override_is_registered_in_flavor_manifest():
    """codex flavor manifest 의 file override — 없으면 공유 카드 렌더가 이 판을 덮는다.

    상위 `.agents/skills @render @source=.claude/skills` 디렉토리 항목보다 구체적인 file
    remap 이 이겨야 codex 전용 egress 절이 살아남는다(pm-dev-delegate 와 같은 기전).
    """
    manifest = (
        REPO / "templates" / "codex" / ".project_manager" / "engine.manifest"
    ).read_text(encoding="utf-8")
    assert (
        ".agents/skills/pm-review/SKILL.md    @render "
        "@source=templates/codex/.agents/skills/pm-review/SKILL.md"
    ) in manifest


# ── 축 2: 활성 문서의 역할 이름·비용 규율 ────────────────────────────────────

ACTIVE_DOCS = (
    REPO / ".project_manager" / "wiki" / "pm_role.md",
    REPO / ".project_manager" / "wiki" / "pm_playbook.md",
    REPO / "README.md",
    REPO / "docs" / "portability.md",
)


def test_active_docs_use_the_additional_reviewer_role_name():
    """활성 매뉴얼/플레이북/README/이식성 문서가 역할을 '추가 리뷰어'로 부른다."""
    for path in ACTIVE_DOCS:
        assert "추가 리뷰어" in path.read_text(encoding="utf-8"), path


def test_playbook_states_atomic_tuple_and_first_time_only_optin():
    """사용자가 설정하는 자리에 원자적 튜플과 '1회만' 의미가 함께 적혀 있다."""
    text = (REPO / ".project_manager" / "wiki" / "pm_playbook.md").read_text(
        encoding="utf-8"
    )
    for key, value in EXPECTED_DEFAULTS:
        assert f"{key}={value}" in text
    assert "1회만" in text
    assert "지속 동의" in text
    assert "reviewer_cmd" in text          # 레거시 채택자 후방호환도 명시


def test_readme_documents_optin_tuple_and_no_per_review_reapproval():
    """README 는 외부 전송 opt-in 은 유지하되 리뷰마다 비용 승인은 요구하지 않는다."""
    text = (REPO / "README.md").read_text(encoding="utf-8")
    assert "외부로 전송" in text                      # 전송 사실은 계속 분명히
    assert "opt-in 을 결정한다" in text
    for key, value in EXPECTED_DEFAULTS:
        assert f"{key}={value}" in text
    assert "비용 승인을 다시 받지 않는다" in text
    assert "자동 마이그레이션 대상이 아니다" in text   # 레거시 reviewer_cmd 채택자 후방호환


def test_pm_role_makes_cap_ack_autonomous_not_a_cost_gate():
    """PM 매뉴얼: 정상 수렴 ack 는 자율 영역, 사용자 게이트는 비용이 아니라 판단."""
    text = (REPO / ".project_manager" / "wiki" / "pm_role.md").read_text(
        encoding="utf-8"
    )
    # ack 두 축이 *자율* 절에 들어 있어야 한다 — 사용자 게이트 절이 아니라.
    autonomous = text.split("**자율+사후")[1].split("**사용자 게이트")[0]
    assert "--ack-rounds" in autonomous and "--ack-wave" in autonomous
    assert "정상 수렴 ack" in autonomous
    assert "비용 동의는 **켤 때 한 번**이다" in text
    assert "기계적 anti-loop 정지" in text


def test_active_docs_have_no_per_round_user_cost_approval_rule():
    """폐기 규율(라운드 재개마다 사용자 비용 승인)이 활성 문서에 남아 있지 않다."""
    retired = (
        "사용자 승인 후에만 `--ack-wave`",
        "승인 없이 `--ack-rounds` 금지",
        "사용자가 계속을 승인한 경우에만",
    )
    surfaces = (*ACTIVE_DOCS, CODEX_PM_REVIEW)
    for path in surfaces:
        text = path.read_text(encoding="utf-8")
        for phrase in retired:
            assert phrase not in text, f"{path}: 폐기된 비용 재승인 규율 잔존 — {phrase}"


# ── 축 2: 활성 출하 PM 표면 전수의 역할 이름 ─────────────────────────────────
#
# 이름 변경은 **사람이 부르는 역할**에만 적용된다. 활성 표면(출하 스킬 카드 · 방법론 wiki ·
# README/이식성 문서 · 부트스트랩 첫 턴 카드)에서 폐기 이름을 몰아내되, 다음은 건드리지 않는다:
#   - 기계 식별자·전송 계약 — `external_review.py` · `external_review_enabled` · `reviewer_cmd`
#   - 히스토리 — ADR · CHANGELOG · done 티켓 · archive log · 과거 지적 인용("codex 게이트 must-fix")
#   - 실제로 *수신 하네스*(선택된 기본 codex)를 가리키는 codex 전용 문맥 — egress 승격 절차 등
# 인벤토리를 파일 열거로 만드는 이유: 새 카드/새 타깃이 목록을 우회해 폐기 이름을 다시 실어도
# 자동으로 red 가 되게 하기 위해서다(하드코딩 목록은 반드시 뒤처진다).

RETIRED_ROLE_PHRASES = ("codex 외부 교차검증", "codex 교차검증")

SKILL_CARD_ROOTS = (
    REPO / ".claude" / "skills",
    REPO / "templates" / "claude_code" / ".claude" / "skills",
    REPO / "templates" / "opencode" / ".claude" / "skills",
    REPO / "templates" / "codex" / ".agents" / "skills",
)

METHODOLOGY_SURFACES = (
    REPO / ".project_manager" / "wiki" / "pm_role.md",
    REPO / ".project_manager" / "wiki" / "pm_playbook.md",
    *(
        REPO / "templates" / flavor / ".project_manager" / "wiki" / name
        for flavor in TEMPLATE_DIRS
        for name in ("pm_role.md", "pm_playbook.md")
    ),
    REPO / "README.md",
    REPO / "docs" / "portability.md",
)

# 부트스트랩 카드는 코드가 문자열로 만든다 — 렌더 결과와 4 사본 소스를 함께 본다.
BOOTSTRAP_SOURCES = (
    TOOLS / "pm_bootstrap.py",
    *(
        REPO / "templates" / flavor / ".project_manager" / "tools" / "pm_bootstrap.py"
        for flavor in TEMPLATE_DIRS
    ),
)

CARD_IDENTITY = {
    "repo": "project_manager",
    "session": "project_manager_1",
    "slot": "work/project_manager_1",
    "slot_path": "/home/x/work/project_manager_1",
    "branch": "release/v1.0.6",
    "others": [],
    "protected_branch": None,
}


@pytest.fixture(scope="module")
def pm_bootstrap():
    return _load("pm_bootstrap")


def _shipping_skill_cards() -> list[Path]:
    """출하되는 PM 스킬 카드 전수 (4 네임스페이스 × 카드). 열거 자체가 판정의 본질."""
    cards: list[Path] = []
    for root in SKILL_CARD_ROOTS:
        assert root.is_dir(), f"스킬 네임스페이스 없음: {root}"
        found = sorted(root.glob("*/SKILL.md"))
        assert found, f"스킬 카드 0개 — 인벤토리 앵커가 깨졌다: {root}"
        cards.extend(found)
    return cards


def _active_pm_surfaces() -> list[Path]:
    return [*_shipping_skill_cards(), *METHODOLOGY_SURFACES, *BOOTSTRAP_SOURCES]


def test_active_pm_surfaces_drop_the_retired_reviewer_role_name():
    """활성 출하 PM 표면 전수에 폐기된 역할 이름이 없다."""
    residue = []
    for path in _active_pm_surfaces():
        assert path.is_file(), f"인벤토리 대상 부재: {path}"
        text = path.read_text(encoding="utf-8")
        residue += [
            f"{path.relative_to(REPO)} — {phrase}"
            for phrase in RETIRED_ROLE_PHRASES
            if phrase in text
        ]
    assert not residue, "폐기된 역할 이름 잔존(활성 표면):\n  " + "\n  ".join(residue)


def test_bootstrap_first_turn_card_names_the_additional_reviewer(
    pm_bootstrap, monkeypatch
):
    """부트스트랩 첫 턴 카드의 external_review 줄이 역할을 '추가 리뷰어'로 부른다.

    수신 하네스는 채택자 `local.conf` 설정값이라 카드 문구가 고정하지 않는다 — 반면 실행
    backbone `external_review.py` 는 기계 식별자로 그대로 남는다.
    """
    for marker in ("CODEX_THREAD_ID", "CODEX_CI"):
        monkeypatch.delenv(marker, raising=False)  # 하네스 감지 절 append 제거(결정론)
    inst = pm_bootstrap.PmBootstrap.__new__(pm_bootstrap.PmBootstrap)
    card = inst._build_command_card_markdown(CARD_IDENTITY)

    review_lines = [ln for ln in card.splitlines() if "external_review.py" in ln]
    assert len(review_lines) == 1, f"external_review 줄이 1개가 아님: {review_lines}"
    assert "추가 리뷰어" in review_lines[0], review_lines[0]
    for phrase in RETIRED_ROLE_PHRASES:
        assert phrase not in card, f"카드에 폐기 이름 잔존 — {phrase}"


def test_role_rename_keeps_transport_identifiers_and_history():
    """이름 변경은 사람 표면 한정 — 기계 식별자와 히스토리는 그대로 둔다.

    인벤토리 밖이어야 하는 것(엔진 히스토리 주석)을 값으로 못박아, 가드가 히스토리까지
    번지지 않게 경계를 고정한다.
    """
    # 전송/설정의 기계 이름은 활성 카드에서도 계속 쓰인다.
    card = CANONICAL_PM_REVIEW.read_text(encoding="utf-8")
    assert "external_review.py" in card
    assert "external_review_enabled=true" in card
    playbook = (REPO / ".project_manager" / "wiki" / "pm_playbook.md").read_text(
        encoding="utf-8"
    )
    assert "reviewer_cmd" in playbook       # 레거시 채택자 키도 이름이 바뀌지 않는다

    # 과거 codex 게이트가 낸 지적 인용은 엔진 주석의 히스토리다 — 인벤토리 대상이 아니다.
    history = TOOLS / "pm_handoff.py"
    assert "codex 교차검증 must-fix" in history.read_text(encoding="utf-8")
    assert history not in set(_active_pm_surfaces())


def test_codex_cards_keep_dollar_skill_entry_notation():
    """codex 네임스페이스 카드는 `$<스킬>` 진입 표기를 유지한다(claude/opencode 는 `/`)."""
    codex_root = REPO / "templates" / "codex" / ".agents" / "skills"
    for card in sorted(codex_root.glob("*/SKILL.md")):
        text = card.read_text(encoding="utf-8")
        heading = next(ln for ln in text.splitlines() if ln.startswith("# "))
        assert heading.startswith(f"# ${card.parent.name}"), (card, heading)
        assert not heading.startswith(f"# /{card.parent.name}"), card


# ── canonical ↔ 3 템플릿 parity (온보딩을 싣는 엔진·방법론) ──────────────────

@pytest.mark.parametrize(
    "relpath",
    [
        ".project_manager/tools/board.py",
        ".project_manager/tools/pm_import.py",
        ".project_manager/tools/pm_update.py",
        ".project_manager/wiki/pm_role.md",
        ".project_manager/wiki/pm_playbook.md",
    ],
)
def test_canonical_to_template_parity(relpath):
    """온보딩 계약을 담은 엔진/방법론 사본이 세 타깃에서 byte-identical."""
    canonical = (REPO / relpath).read_bytes()
    for flavor in TEMPLATE_DIRS:
        path = REPO / "templates" / flavor / relpath
        assert path.is_file(), f"템플릿 사본 없음: {path}"
        assert path.read_bytes() == canonical, (
            f"{flavor} 사본 드리프트 — pm_update 전파 필요: {relpath}"
        )
