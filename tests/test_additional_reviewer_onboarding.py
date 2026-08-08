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


# ── 축 4: 첫 opt-in 이 **이미 있는 대상**을 덮지 않는다 (T-0590 R3) ──────────
#
# 활성 플래그 하나만 없는 conf 는 "미결정"이지만 **대상은 이미 있을 수 있다** — 레거시
# `reviewer_cmd` 를 쓰던 채택자, 또는 구조화 튜플만 손으로 적고 플래그를 안 켠 채택자다. 그 형상에
# 기본 4키를 그대로 append 하면 두 가지로 망가진다:
#   · 레거시 + 구조화 = 엔진이 fail-loud 로 거부하는 **이중 대상**(리뷰가 아예 안 돈다).
#   · 구조화 + 구조화 = last-wins 로 사용자의 하네스/모델/추론 강도가 **조용히** 기본값이 된다.
# 그래서 "예" 는 대상 유무로 갈린다: 없으면 4키, 있으면 활성 플래그 한 줄.

LEGACY_ONLY_CONF = "session=pm\nreviewer_cmd=my-reviewer --flag --model my-model\n"
STRUCTURED_ONLY_CONF = (
    "session=pm\n"
    "additional_reviewer.harness=opencode\n"
    "additional_reviewer.model=qwen3-coder-next\n"
    "additional_reviewer.reasoning=low\n"
)
# 대상이 그 자체로 깨진 형상 — 어느 쪽이 이기는지 추측하지 않고 **쓰기 전에** 멈춘다.
BROKEN_TARGET_CONFS = (
    pytest.param(
        "session=pm\nadditional_reviewer.harness=codex\n", id="partial-harness-only"
    ),
    pytest.param(
        "session=pm\nadditional_reviewer.model=gpt-5.6-sol\n", id="partial-model-only"
    ),
    pytest.param(
        "session=pm\nadditional_reviewer.harness=\nadditional_reviewer.model=\n",
        id="blank-structured-declaration",
    ),
    pytest.param(
        "session=pm\nadditional_reviewer.reasoning=max\n", id="reasoning-only",
    ),
    pytest.param(
        "session=pm\nreviewer_cmd=my-reviewer\n"
        "additional_reviewer.harness=codex\nadditional_reviewer.model=gpt-5.6-sol\n",
        id="structured-plus-legacy-conflict",
    ),
)


def _external_review():
    """실행 코어 — **테스트만** 읽는다(온보딩 경로는 import 하지 않는다·키 대조용)."""
    return _load("external_review")


def test_onboarding_target_keys_mirror_the_execution_core(board, pm_update):
    """두 온보딩 사본의 키 이름이 실행 해소 코어의 선언과 글자 단위로 같다(드리프트 가드)."""
    core = _external_review()
    for module in (board, pm_update):
        assert module.ADDITIONAL_REVIEWER_KEYS == core.ADDITIONAL_REVIEWER_KEYS
        assert module.LEGACY_REVIEWER_CMD_KEY == core.LEGACY_REVIEWER_CMD_KEY
        assert module.ADDITIONAL_REVIEWER_PREFIX == core.ADDITIONAL_REVIEWER_PREFIX


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("session=pm\n", "none", id="no-target"),
        pytest.param("session=pm\nreviewer_cmd=\n", "none", id="blank-legacy-only"),
        pytest.param(LEGACY_ONLY_CONF, "legacy", id="legacy-only"),
        pytest.param(STRUCTURED_ONLY_CONF, "structured", id="structured-only"),
        pytest.param(
            "additional_reviewer.harness=codex\nadditional_reviewer.model=gpt-5.6-sol\n",
            "structured", id="structured-without-reasoning",
        ),
        pytest.param(
            "additional_reviewer.harness=codex\nadditional_reviewer.model=gpt-5.6-sol\n"
            "additional_reviewer.reasoning=\n",
            "structured", id="structured-with-blank-optional-reasoning",
        ),
    ],
)
def test_two_onboarding_entries_classify_the_target_identically(
    board, pm_update, text, expected
):
    """board·pm_update 가 같은 conf 를 같은 대상으로 읽는다(값 검증은 하지 않는다)."""
    conf = _parse_conf(text)
    assert board.classify_additional_reviewer_target(conf) == expected
    assert pm_update.classify_additional_reviewer_target(conf) == expected


@pytest.mark.parametrize("text", BROKEN_TARGET_CONFS)
def test_broken_target_is_refused_by_both_entries_and_the_core(board, pm_update, text):
    """부분 튜플·이중 대상은 두 온보딩과 실행 코어가 **모두** 거부한다(같은 판정면)."""
    conf = _parse_conf(text)
    with pytest.raises(board.AdditionalReviewerTargetError):
        board.classify_additional_reviewer_target(conf)
    with pytest.raises(pm_update.AdditionalReviewerTargetError):
        pm_update.classify_additional_reviewer_target(conf)
    core = _external_review()
    with pytest.raises(core.ReviewerTargetError):
        core.resolve_reviewer_target(conf)


@pytest.mark.parametrize(
    ("text", "label"),
    [
        pytest.param(LEGACY_ONLY_CONF, "legacy", id="legacy-only"),
        pytest.param(STRUCTURED_ONLY_CONF, "structured", id="structured-only"),
    ],
)
def test_board_optin_yes_on_a_configured_target_writes_only_the_flag(
    board, monkeypatch, tmp_path, capsys, text, label
):
    """이미 대상이 있으면 'y' 는 활성 플래그 한 줄만 append 하고 대상은 byte 그대로 둔다."""
    conf = _isolated_conf(board, monkeypatch, tmp_path, text)
    monkeypatch.setattr(board.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")

    board.prompt_external_review_optin()

    after = conf.read_text(encoding="utf-8")
    assert after.startswith(text), "기존 대상 줄이 변형됐다"
    parsed = _parse_conf(after)
    assert parsed["external_review_enabled"] == "true"
    # 기존 대상의 값이 한 글자도 바뀌지 않는다.
    assert _parse_conf(after) | _parse_conf(text) == parsed
    if label == "legacy":
        assert parsed["reviewer_cmd"] == "my-reviewer --flag --model my-model"
        assert "additional_reviewer." not in after, "레거시 위에 구조화 대상을 겹쳤다(이중 대상)"
    else:
        assert parsed["additional_reviewer.harness"] == "opencode"
        assert parsed["additional_reviewer.model"] == "qwen3-coder-next"
        assert parsed["additional_reviewer.reasoning"] == "low"
        assert "reviewer_cmd" not in after
        # 기본 프로필이 last-wins 로 사용자 선언을 덮지 않았음을 값으로 못박는다.
        for key, value in EXPECTED_DEFAULTS[1:]:
            assert parsed[key] != value, f"{key} 가 기본값으로 덮였다"
    assert "기존" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("text", "label"),
    [
        pytest.param(LEGACY_ONLY_CONF, "legacy", id="legacy-only"),
        pytest.param(STRUCTURED_ONLY_CONF, "structured", id="structured-only"),
    ],
)
def test_pm_update_optin_yes_on_a_configured_target_writes_only_the_flag(
    pm_update, monkeypatch, tmp_path, text, label
):
    """pm_update 도 같은 계약 — 대상이 있으면 활성 플래그만(board 동형)."""
    dest, conf = _dest_with_conf(tmp_path, text)
    monkeypatch.delenv("PM_NONINTERACTIVE", raising=False)
    _tty(monkeypatch, "y")

    pm_update.maybe_prompt_external_review(dest)

    after = conf.read_text(encoding="utf-8")
    assert after.startswith(text)
    parsed = _parse_conf(after)
    assert parsed["external_review_enabled"] == "true"
    if label == "legacy":
        assert parsed["reviewer_cmd"] == "my-reviewer --flag --model my-model"
        assert "additional_reviewer." not in after
    else:
        assert parsed["additional_reviewer.harness"] == "opencode"
        assert parsed["additional_reviewer.model"] == "qwen3-coder-next"
        assert parsed["additional_reviewer.reasoning"] == "low"
        assert "reviewer_cmd" not in after


def test_optin_yes_on_a_configured_target_appends_safely_without_trailing_newline(
    board, pm_update, monkeypatch, tmp_path
):
    """개행 없이 끝난 conf 의 마지막 대상 줄도 변질되지 않는다(두 진입 모두)."""
    newlineless = "session=pm\nreviewer_cmd=my-reviewer --flag"
    conf = _isolated_conf(board, monkeypatch, tmp_path, newlineless)
    monkeypatch.setattr(board.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    board.prompt_external_review_optin()
    parsed = _parse_conf(conf.read_text(encoding="utf-8"))
    assert parsed["reviewer_cmd"] == "my-reviewer --flag"
    assert parsed["external_review_enabled"] == "true"

    dest, update_conf = _dest_with_conf(tmp_path / "u", newlineless)
    monkeypatch.delenv("PM_NONINTERACTIVE", raising=False)
    _tty(monkeypatch, "y")
    pm_update.maybe_prompt_external_review(dest)
    parsed = _parse_conf(update_conf.read_text(encoding="utf-8"))
    assert parsed["reviewer_cmd"] == "my-reviewer --flag"
    assert parsed["external_review_enabled"] == "true"


@pytest.mark.parametrize("text", [LEGACY_ONLY_CONF, STRUCTURED_ONLY_CONF])
def test_optin_decline_records_only_the_flag_and_keeps_the_target(
    board, pm_update, monkeypatch, tmp_path, text
):
    """'n' 은 false 실키만 기록하고 기존 대상을 다시 쓰지 않는다(두 진입 모두)."""
    conf = _isolated_conf(board, monkeypatch, tmp_path, text)
    monkeypatch.setattr(board.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    board.prompt_external_review_optin()
    after = conf.read_text(encoding="utf-8")
    assert after.startswith(text)
    assert _parse_conf(after)["external_review_enabled"] == "false"

    dest, update_conf = _dest_with_conf(tmp_path / "u", text)
    monkeypatch.delenv("PM_NONINTERACTIVE", raising=False)
    _tty(monkeypatch, "n")
    pm_update.maybe_prompt_external_review(dest)
    after = update_conf.read_text(encoding="utf-8")
    assert after.startswith(text)
    assert _parse_conf(after)["external_review_enabled"] == "false"


@pytest.mark.parametrize("text", BROKEN_TARGET_CONFS)
@pytest.mark.parametrize("answer", ["y", "n"])
def test_board_optin_refuses_to_write_over_a_broken_target(
    board, monkeypatch, tmp_path, capsys, text, answer
):
    """깨진 대상이면 질문도 기록도 없다 — 어떤 답이 와도 write 0 (loud 진단만)."""
    conf = _isolated_conf(board, monkeypatch, tmp_path, text)
    monkeypatch.setattr(board.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(
        "builtins.input", lambda prompt="": pytest.fail("깨진 대상인데 질문함")
    )

    board.prompt_external_review_optin()

    assert conf.read_text(encoding="utf-8") == text, "깨진 대상 위에 무언가를 썼다"
    out = capsys.readouterr().out
    assert "추가 리뷰어 설정이 이미 깨져" in out
    assert "기록하지 않았습니다" in out


@pytest.mark.parametrize("text", BROKEN_TARGET_CONFS)
def test_pm_update_optin_refuses_to_write_over_a_broken_target(
    pm_update, monkeypatch, tmp_path, capsys, text
):
    """pm_update 도 깨진 대상에는 쓰지 않는다(board 동형·같은 진단)."""
    dest, conf = _dest_with_conf(tmp_path, text)
    monkeypatch.delenv("PM_NONINTERACTIVE", raising=False)
    _tty(monkeypatch, "깨진 대상인데 질문함")

    pm_update.maybe_prompt_external_review(dest)

    assert conf.read_text(encoding="utf-8") == text
    out = capsys.readouterr().out
    assert "깨져" in out and "기록하지 않았습니다" in out


# ── 축 5: EOF 는 거절이 아니다 (T-0590 R3) ──────────────────────────────────
#
# `input()` 의 EOFError 는 "안 켜겠다"가 아니라 **질문할 표면이 아니었다**는 신호다(TTY 오판정·
# 파이프 종료). 그걸 false 로 박제하면 결정이 durable 하게 남아 다음 init/update 가 다시 묻지
# 않고, 켜려던 채택자는 영영 질문을 못 받는다. 두 진입 모두 write 0 으로 돌아간다.

@pytest.mark.parametrize(
    "text",
    [
        pytest.param("session=pm\n", id="no-target"),
        pytest.param(LEGACY_ONLY_CONF, id="legacy-target"),
        pytest.param(STRUCTURED_ONLY_CONF, id="structured-target"),
        pytest.param("session=pm\nupstream_rev=abc123", id="newlineless"),
    ],
)
def test_eof_answer_writes_nothing_in_both_entries(
    board, pm_update, monkeypatch, tmp_path, text
):
    """EOF → byte 보존 + false 키 미기록(다음 실행이 제대로 된 표면에서 다시 묻는다)."""
    def _eof(prompt=""):
        raise EOFError

    conf = _isolated_conf(board, monkeypatch, tmp_path, text)
    monkeypatch.setattr(board.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", _eof)
    board.prompt_external_review_optin()
    assert conf.read_text(encoding="utf-8") == text
    assert "external_review_enabled" not in _parse_conf(conf.read_text(encoding="utf-8"))

    dest, update_conf = _dest_with_conf(tmp_path / "u", text)
    monkeypatch.delenv("PM_NONINTERACTIVE", raising=False)
    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": lambda self: True})())
    monkeypatch.setattr("builtins.input", _eof)
    pm_update.maybe_prompt_external_review(dest)
    assert update_conf.read_text(encoding="utf-8") == text
    assert "external_review_enabled" not in _parse_conf(
        update_conf.read_text(encoding="utf-8")
    )


def test_pm_update_main_eof_leaves_the_conf_untouched(pm_update, monkeypatch, tmp_path):
    """`_main` 전 구간에서도 EOF 는 결정을 박제하지 않는다(실 진입 회귀·byte 보존).

    직접 helper 호출만 보면 진입점이 다른 경로로 false 를 쓰는 변종을 놓친다. 여기서는 실
    `_main` 을 태우고 external_review 축의 write 가 0 임을 conf byte 로 단언한다(delegate 축은
    자체 EOF 계약이 따로라 이 테스트의 관심 밖 — 그 seam 만 no-op 으로 세운다).
    """
    source = tmp_path / "upstream"
    sentinel = source / ".project_manager" / "tools" / "__eof_sentinel__.py"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("# sentinel\n", encoding="utf-8")
    (source / ".project_manager" / "engine.manifest").write_text(
        ".project_manager/tools/__eof_sentinel__.py\n", encoding="utf-8"
    )
    subprocess.run(["git", "-C", str(source), "init", "-q"], check=True,
                   capture_output=True, text=True)
    subprocess.run(["git", "-C", str(source), "add", "-f", "-A"], check=True,
                   capture_output=True, text=True)

    dest = tmp_path / "dest"
    conf_path = dest / ".project_manager" / "local.conf"
    conf_path.parent.mkdir(parents=True)
    original = f"session=pm\nupstream={source}\n"
    conf_path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(pm_update, "REPO", dest)

    pm_import = pm_update._load_pm_import()
    monkeypatch.setattr(pm_import, "read_upstream_rev", lambda *a, **k: None)
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)
    monkeypatch.setattr(pm_update, "maybe_prompt_delegate_optin", lambda dest_root: None)
    monkeypatch.delenv("PM_NONINTERACTIVE", raising=False)
    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": lambda self: True})())

    def _eof(prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", _eof)

    assert pm_update._main([]) == 0
    assert conf_path.read_text(encoding="utf-8") == original, "EOF 인데 conf 가 바뀌었다"
    assert "external_review_enabled" not in _parse_conf(
        conf_path.read_text(encoding="utf-8")
    )


# ── 축 6: 판정은 **커밋 시점**의 conf 가 소유한다 (T-0590 R4) ────────────────
#
# 질문은 사람이 답할 때까지 열려 있다(수 초~수 분). 그 사이 다른 행위자가 같은 local.conf 를
# 바꿀 수 있다 — 다른 세션의 `board.py init`, 병렬 `pm_update`, 손으로 여는 편집기. 질문 **전**
# 판정으로 기록하면 그사이 생긴 대상 위에 기본 4키가 얹혀, 축 4 가 닫은 손상(레거시와의 이중
# 대상·구조화의 last-wins)이 창만 바꿔 그대로 재현된다. 그래서 기록 직전에 다시 읽고 다시
# 판정하며, 재읽기→판정→append 는 배타락 + 단일 O_APPEND 로 닫는다.

_ENABLE_ONLY_HINT_TEXT = "local.conf 에 external_review_enabled=true"


def _optin_entry(kind, board, pm_update, monkeypatch, tmp_path, text: str):
    """두 온보딩 진입을 같은 모양으로 세운다 → (conf 경로, 무인자 호출)."""
    monkeypatch.delenv("PM_NONINTERACTIVE", raising=False)
    if kind == "board":
        conf = _isolated_conf(board, monkeypatch, tmp_path, text)
        monkeypatch.setattr(board.sys.stdin, "isatty", lambda: True)
        return conf, board.prompt_external_review_optin
    dest, conf = _dest_with_conf(tmp_path / "u", text)
    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": lambda self: True})())
    return conf, lambda: pm_update.maybe_prompt_external_review(dest)


def _answer_after_writing(conf: Path, new_text: str, answer: str):
    """질문 도중(=`input()` 안) conf 를 통째로 바꾸고 답을 돌려주는 stdin 대역."""
    def _input(prompt=""):
        conf.write_text(new_text, encoding="utf-8")
        return answer
    return _input


ENTRIES = ["board", "pm_update"]


@pytest.mark.parametrize("kind", ENTRIES)
@pytest.mark.parametrize(
    "appeared",
    [
        pytest.param("session=pm\nexternal_review_enabled=false\n", id="decided-false"),
        pytest.param(
            "session=pm\nexternal_review_enabled=true\n"
            "additional_reviewer.harness=opencode\n"
            "additional_reviewer.model=qwen3-coder-next\n",
            id="decided-true-with-target",
        ),
    ],
)
def test_a_decision_that_appears_during_the_question_wins_byte_for_byte(
    board, pm_update, monkeypatch, tmp_path, capsys, kind, appeared
):
    """질문 도중 활성 키가 생기면 그 결정이 이긴다 — 이 응답은 한 바이트도 쓰지 않는다."""
    conf, run = _optin_entry(kind, board, pm_update, monkeypatch, tmp_path, "session=pm\n")
    monkeypatch.setattr("builtins.input", _answer_after_writing(conf, appeared, "y"))

    run()

    assert conf.read_text(encoding="utf-8") == appeared, "그사이 생긴 결정 위에 덧썼다"
    assert "기록하지 않았습니다" in capsys.readouterr().out


@pytest.mark.parametrize("kind", ENTRIES)
@pytest.mark.parametrize(
    ("appeared", "label"),
    [
        pytest.param(LEGACY_ONLY_CONF, "legacy", id="legacy-appears"),
        pytest.param(STRUCTURED_ONLY_CONF, "structured", id="structured-appears"),
        pytest.param(
            "session=pm\nreviewer_cmd=my-reviewer --flag", "legacy", id="newlineless",
        ),
    ],
)
def test_a_target_that_appears_during_the_question_gets_only_the_flag(
    board, pm_update, monkeypatch, tmp_path, kind, appeared, label
):
    """질문 시점엔 대상이 없었어도, 커밋 시점에 있으면 활성 플래그 한 줄만 붙는다.

    옛 판정으로 기록하면 레거시 위에는 이중 대상이, 구조화 위에는 last-wins 덮어쓰기가 생긴다.
    개행 없이 끝난 바이트열도 마지막 대상 줄이 변질되지 않아야 한다."""
    conf, run = _optin_entry(kind, board, pm_update, monkeypatch, tmp_path, "session=pm\n")
    monkeypatch.setattr("builtins.input", _answer_after_writing(conf, appeared, "y"))

    run()

    after = conf.read_text(encoding="utf-8")
    assert after.startswith(appeared), "그사이 생긴 대상의 바이트가 변형됐다"
    parsed = _parse_conf(after)
    assert parsed["external_review_enabled"] == "true"
    if label == "legacy":
        assert "additional_reviewer." not in after, "레거시 위에 구조화 대상을 겹쳤다(이중 대상)"
        assert parsed["reviewer_cmd"] == _parse_conf(appeared)["reviewer_cmd"]
    else:
        assert "reviewer_cmd" not in after
        for key, value in EXPECTED_DEFAULTS[1:]:
            assert parsed[key] != value, f"{key} 가 기본값으로 덮였다(last-wins)"


@pytest.mark.parametrize("kind", ENTRIES)
@pytest.mark.parametrize("appeared", BROKEN_TARGET_CONFS)
@pytest.mark.parametrize("answer", ["y", "n"])
def test_a_target_that_breaks_during_the_question_is_a_loud_no_write(
    board, pm_update, monkeypatch, tmp_path, capsys, kind, appeared, answer
):
    """커밋 시점에 대상이 깨져 있으면 어떤 답이 와도 write 0 (loud 진단만)."""
    conf, run = _optin_entry(kind, board, pm_update, monkeypatch, tmp_path, "session=pm\n")
    monkeypatch.setattr("builtins.input", _answer_after_writing(conf, appeared, answer))

    run()

    assert conf.read_text(encoding="utf-8") == appeared, "깨진 대상 위에 무언가를 썼다"
    out = capsys.readouterr().out
    assert "깨져" in out and "기록하지 않았습니다" in out


@pytest.mark.parametrize("kind", ENTRIES)
def test_a_target_that_disappears_during_the_question_gets_the_defaults(
    board, pm_update, monkeypatch, tmp_path, kind
):
    """반대 방향도 커밋 시점이 이긴다 — 질문 땐 대상이 있었어도 사라졌으면 기본 4키를 심는다.

    옛 판정을 쓰면 활성 플래그만 붙어 **대상 없는 ON** 이 된다(실행이 기본 커맨드로 흘러가거나
    해소에 실패한다). 질문 문구와 기록이 갈라지는 것은 감수한다 — 정직한 기록이 먼저다."""
    conf, run = _optin_entry(
        kind, board, pm_update, monkeypatch, tmp_path, LEGACY_ONLY_CONF)
    monkeypatch.setattr(
        "builtins.input", _answer_after_writing(conf, "session=pm\n", "y"))

    run()

    after = conf.read_text(encoding="utf-8")
    parsed = _parse_conf(after)
    for key, value in EXPECTED_DEFAULTS:
        assert parsed[key] == value, f"{key} 미기록/오값: {parsed.get(key)!r}"
    assert "reviewer_cmd" not in after


@pytest.mark.parametrize("kind", ENTRIES)
def test_declining_after_a_target_appears_records_only_the_flag_and_guides_to_it(
    board, pm_update, monkeypatch, tmp_path, capsys, kind
):
    """거절 계약은 그대로(false 실키 1개) + 안내는 활성 플래그만 말한다.

    이미 대상이 있는 conf 에 "구조화 3키를 더 적으라"고 안내하면, 그 말을 따른 사람이 손으로
    이중 대상(레거시 위)·last-wins(구조화 위)를 만든다 — 엔진이 write 경로에서 막아 둔 손상이다."""
    conf, run = _optin_entry(kind, board, pm_update, monkeypatch, tmp_path, "session=pm\n")
    monkeypatch.setattr(
        "builtins.input", _answer_after_writing(conf, LEGACY_ONLY_CONF, "n"))

    run()

    after = conf.read_text(encoding="utf-8")
    assert after.startswith(LEGACY_ONLY_CONF)
    assert _parse_conf(after)["external_review_enabled"] == "false"
    assert "additional_reviewer." not in after
    out = capsys.readouterr().out
    assert _ENABLE_ONLY_HINT_TEXT in out
    assert "additional_reviewer.harness/model/reasoning" not in out, (
        "이미 대상이 있는데 구조화 키를 더 적으라고 안내했다")


@pytest.mark.parametrize("kind", ENTRIES)
@pytest.mark.parametrize(
    "appeared",
    [
        pytest.param(LEGACY_ONLY_CONF, id="target-appeared"),
        pytest.param("session=pm\nadditional_reviewer.harness=codex\n", id="broken"),
        pytest.param("session=pm\nupstream_rev=abc123", id="newlineless"),
    ],
)
def test_eof_after_a_conf_change_still_writes_nothing(
    board, pm_update, monkeypatch, tmp_path, kind, appeared
):
    """EOF 계약 불변 — 질문 도중 conf 가 바뀌어도 아무것도 쓰지 않는다(다음 실행이 다시 묻는다)."""
    conf, run = _optin_entry(kind, board, pm_update, monkeypatch, tmp_path, "session=pm\n")

    def _eof(prompt=""):
        conf.write_text(appeared, encoding="utf-8")
        raise EOFError

    monkeypatch.setattr("builtins.input", _eof)

    run()

    assert conf.read_text(encoding="utf-8") == appeared
    assert "external_review_enabled" not in _parse_conf(appeared)


@pytest.mark.parametrize("kind", ENTRIES)
@pytest.mark.parametrize(
    "existing",
    [
        pytest.param(LEGACY_ONLY_CONF, id="legacy-target"),
        pytest.param(STRUCTURED_ONLY_CONF, id="structured-target"),
    ],
)
def test_noninteractive_guidance_on_an_existing_target_names_only_the_flag(
    board, pm_update, monkeypatch, tmp_path, capsys, kind, existing
):
    """비대화형 안내도 대상이 있으면 활성 플래그만 말한다(대상 없을 때는 종전 문장 그대로)."""
    conf, run = _optin_entry(kind, board, pm_update, monkeypatch, tmp_path, existing)
    monkeypatch.setenv("PM_NONINTERACTIVE", "1")
    monkeypatch.setattr(
        "builtins.input", lambda prompt="": pytest.fail("비대화형인데 질문함"))

    run()

    assert conf.read_text(encoding="utf-8") == existing
    out = capsys.readouterr().out
    assert _ENABLE_ONLY_HINT_TEXT in out
    assert "additional_reviewer.harness/model/reasoning" not in out


def test_the_two_entries_share_the_enable_hint_wording(board, pm_update):
    """두 사본의 안내 문구·판정이 글자 단위로 같다(드리프트 가드·축 4 의 키 미러와 같은 이유)."""
    for module in (board, pm_update):
        assert module.ADDITIONAL_REVIEWER_ENABLE_ONLY_HINT == _ENABLE_ONLY_HINT_TEXT
        assert module.ADDITIONAL_REVIEWER_ENABLE_HINT == ENABLE_HINT_TEXT
        assert module._additional_reviewer_enable_hint(
            module.REVIEWER_TARGET_NONE) == ENABLE_HINT_TEXT
        for target in (module.REVIEWER_TARGET_LEGACY, module.REVIEWER_TARGET_STRUCTURED):
            assert module._additional_reviewer_enable_hint(target) == _ENABLE_ONLY_HINT_TEXT
    assert board.ADDITIONAL_REVIEWER_DECLINE_BLOCK == pm_update.ADDITIONAL_REVIEWER_DECLINE_BLOCK


@pytest.mark.parametrize("kind", ENTRIES)
def test_the_commit_point_append_is_one_write_under_the_shared_lock(
    board, pm_update, monkeypatch, tmp_path, kind
):
    """재읽기→판정→append 가 배타락 안에서 **단일 추가**로 닫힌다(그 사이에 새 창을 안 만든다).

    두 진입이 같은 conf 에 대해 같은 락 파일에 도달해야 직렬화가 성립하므로 경로 규약도 함께
    못박는다(상수가 아니라 대상 conf 에서 유도)."""
    conf, run = _optin_entry(kind, board, pm_update, monkeypatch, tmp_path, "session=pm\n")
    module = board if kind == "board" else pm_update
    assert module._local_conf_lock_path(conf) == conf.parent / ".local" / "local-conf.lock"

    file_lock = _load("file_lock")
    appends: list[tuple[str, str]] = []
    real_append = file_lock.append_atomic
    locked: list[str] = []
    real_lock = file_lock.exclusive_file_lock

    def _spy_append(path, text, **kwargs):
        appends.append((str(path), text))
        assert locked, "락 밖에서 conf 를 건드렸다"
        return real_append(path, text, **kwargs)

    def _spy_lock(path, **kwargs):
        locked.append(str(path))
        return real_lock(path, **kwargs)

    if kind == "board":
        monkeypatch.setattr(board.file_lock, "append_atomic", _spy_append)
        monkeypatch.setattr(board.file_lock, "exclusive_file_lock", _spy_lock)
    else:
        loaded = pm_update._load_file_lock()
        monkeypatch.setattr(loaded, "append_atomic", _spy_append)
        monkeypatch.setattr(loaded, "exclusive_file_lock", _spy_lock)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")

    run()

    assert locked == [str(conf.parent / ".local" / "local-conf.lock")]
    assert [path for path, _text in appends] == [str(conf)]
    assert appends[0][1] == module.ADDITIONAL_REVIEWER_OPTIN_BLOCK
