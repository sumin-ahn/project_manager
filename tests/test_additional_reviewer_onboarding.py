"""추가 리뷰어(additional reviewer) 온보딩·카드·활성문서 계약 (T-0590·T-0597·T-0598).

사람이 부르는 역할 이름은 **추가 리뷰어**이고, 게이트 키도 `additional_reviewer.enabled` 로 통일돼
있다(T-0597). `external_review*` 는 모듈 파일 이름·raw 파일 접두처럼 이미 기록된 산출물에 박힌
기계 식별자와 외부 전송·격리·과금 축의 이름으로만 남는다. 이 파일이 못박는 3축:

1. **첫 opt-in 계약** — `board.py init` 과 `pm_update` 는 결정이 없을 때만 **1회** 묻고, "예" 면
   `additional_reviewer.enabled=true` + `additional_reviewer.harness/model/reasoning` 4키를 원자적으로
   기록한다(`reviewer_cmd` 미생성). 이미 결정(true/false)이 있으면 묻지도, 기존 구조적 튜플·레거시
   `reviewer_cmd` 를 덮지도 않는다. 비대화형은 안전쪽(OFF) + 나중에 켜는 법 1줄.
2. **지속 동의** — `additional_reviewer.enabled=true` 는 설정된 외부 전송과 통상 과금에 대한 지속
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

from _repo_owned_inventory import OWNED, repo_owned_paths

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
TEMPLATE_DIRS = ("claude_code", "codex", "opencode")

# 첫 opt-in 이 심어야 하는 정확한 4키 (순서 포함). 엔진 상수를 읽지 않고 여기 리터럴로 둔다 —
# 상수와 함께 조용히 바뀌면 가드가 아니다.
EXPECTED_DEFAULTS = (
    ("additional_reviewer.enabled", "true"),
    ("additional_reviewer.harness", "codex"),
    ("additional_reviewer.model", "gpt-5.6-sol"),
    ("additional_reviewer.reasoning", "max"),
)

# 폐지된 라운드 연장 승인 플래그 (T-0593) — 출하 문서에서 0 이어야 한다(축 5 가드).
RETIRED_ROUND_ACK_FLAG = "--ack-rounds"
# 새 수렴 게이트(T-0593)의 카드 서술 계약 — 문장이 아니라 **규율 4요소**를 못박는다:
# 상한 2회 · 발산 조기 차단 · 확인 전용 라운드 1회(신규 발견은 재설계 신호) · 출구는 재설계/분할.
# 두 판정 경계는 엔진(`_convergence_refusal`)과 글자로 맞춘다 — 상한 도달은 must-fix 잔존과 무관한
# 차단이고(사유 라벨만 `cap-unresolved`/`cap-reached` 로 갈린다), 조기 차단은 **strict 증가**만이다
# (평탄 3→2→2 는 조기 차단이 아니라 상한에서 걸린다). 이 둘을 느슨하게 적으면 카드가 "must-fix 0
# 이면 4라운드째가 열린다"·"평탄도 조기 차단" 같은 없는 경로를 가르친다.
CONVERGENCE_GATE_CONTRACTS = (
    "additional_reviewer.rounds_max",
    "라운드 상한 2회",
    "must-fix 잔존과 무관하게 차단",
    "발산 조기 차단",
    "--confirm-fix",
    "신규 발견은 재설계 신호",
    "재설계·티켓 분할",
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


def _card_with_operational_details(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    details = path.parent / "references" / "operational-details.md"
    if details.is_file():
        text += "\n" + details.read_text(encoding="utf-8")
    return text


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
    """'n' → additional_reviewer.enabled=false 만. 하네스/모델/명령을 지어내지 않는다."""
    conf = _isolated_conf(board, monkeypatch, tmp_path)
    monkeypatch.setattr(board.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")

    board.prompt_external_review_optin()

    text = conf.read_text(encoding="utf-8")
    assert _parse_conf(text) == {"additional_reviewer.enabled": "false"}
    assert "reviewer_cmd" not in text
    # 거절은 대상(하네스·모델·추론)을 발명하지 않는다 — 게이트 키 한 줄뿐이다.
    for key in ("additional_reviewer.harness", "additional_reviewer.model",
                "additional_reviewer.reasoning"):
        assert key not in text


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
    assert "additional_reviewer.enabled=true" in out
    assert "additional_reviewer.harness/model/reasoning" in out


@pytest.mark.parametrize(
    "existing",
    [
        pytest.param(
            "additional_reviewer.enabled=true\n"
            "additional_reviewer.harness=opencode\n"
            "additional_reviewer.model=qwen3-coder-next\n"
            "additional_reviewer.reasoning=low\n",
            id="user-structured-tuple",
        ),
        pytest.param("additional_reviewer.enabled=false\n", id="declined"),
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
    assert _parse_conf(text)["additional_reviewer.enabled"] == "false"
    for key in ("additional_reviewer.harness", "additional_reviewer.model",
                "additional_reviewer.reasoning"):
        assert key not in text


def test_pm_update_optin_appends_safely_to_newlineless_conf(
    pm_update, monkeypatch, tmp_path
):
    """마지막 개행 없는 conf 에 append 해도 기존 키가 변질되지 않는다."""
    dest, conf = _dest_with_conf(tmp_path, "session=pm\nupstream.rev=abc123")
    monkeypatch.delenv("PM_NONINTERACTIVE", raising=False)
    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": lambda self: True})())
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")

    pm_update.maybe_prompt_external_review(dest)

    parsed = _parse_conf(conf.read_text(encoding="utf-8"))
    assert parsed["upstream.rev"] == "abc123"
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
            "additional_reviewer.enabled=true\nadditional_reviewer.harness=claude\n"
            "additional_reviewer.model=opus\n",
            id="user-structured-tuple",
        ),
        pytest.param("additional_reviewer.enabled=false\n", id="declined"),
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
# 판정을 conf **raw 텍스트 substring** 으로 하면 주석 한 줄(`# additional_reviewer.enabled=false`)
# 이나 무관한 값 안의 같은 문자열이 "이미 결정됨"으로 읽힌다. 그러면 켜려던 채택자는 질문도
# (대화형) 안내도(비대화형) 못 받고, 결정은 영영 기록되지 않는다. 두 진입 모두 local_config
# 파싱 의미로 판정해야 한다 — 주석/빈 줄/`=` 없는 줄 제외 · key·value strip · 중복 last-wins.

# 활성 키 부재 = **미결정**. 파싱 규칙의 각 조항이 하나씩 대응한다.
UNDECIDED_CONFS = (
    pytest.param(
        "# additional_reviewer.enabled=false\nsession=pm\n", id="commented-out-decision"
    ),
    pytest.param(
        "# 켜려면 additional_reviewer.enabled=true 로 바꾼다\nsession=pm\n",
        id="prose-comment-naming-the-key",
    ),
    pytest.param(
        "session=pm\nnot_additional_reviewer_enabled=true\n",
        id="other-key-ending-with-the-name",
    ),
    pytest.param(
        "test.cmd=pytest -k additional_reviewer_enabled\n",
        id="key-name-inside-another-value",
    ),
)

# 활성 키 존재 = **결정됨**(값 무관). 공백 패딩·중복 키도 local_config 와 같게 본다.
DECIDED_CONFS = (
    pytest.param("additional_reviewer.enabled=true\n", id="plain-true"),
    pytest.param("additional_reviewer.enabled=false\n", id="plain-false"),
    pytest.param(
        "  additional_reviewer.enabled=true  \nsession=pm\n", id="whitespace-padded-key"
    ),
    pytest.param(
        "additional_reviewer.enabled=false\nadditional_reviewer.enabled=true\n",
        id="duplicate-keys-last-wins",
    ),
)

# 비대화형 경로가 남겨야 하는 "나중에 켜는 법" 문장 (엔진 상수와 별도 리터럴 — 함께 조용히
# 바뀌면 가드가 아니다).
ENABLE_HINT_TEXT = (
    "local.conf 에 additional_reviewer.enabled=true + "
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
        assert ("additional_reviewer.enabled" in board.local_config(tree)) is decided, text
        assert ("additional_reviewer.enabled" in pm_update._read_local_conf(conf)) is decided, text
        assert ("additional_reviewer.enabled" in _parse_conf(text)) is decided, text


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
        f"upstream.path={source}\n"
        f"upstream.rev={CONVERGED_REV}\nupstream.seen_rev={CONVERGED_REV}\n"
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
    # 위임 opt-in 질문은 폐지됐다(마스터 스위치 기본 허용) — 그 키를 새로 쓰지 않는다.
    assert "delegate.enabled" not in parsed


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
    decided = "session=pm\nadditional_reviewer.enabled=false\ndelegate.enabled=false\n"
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
        "additional_reviewer.enabled=true\n"
        "additional_reviewer.harness=opencode\n"
        "additional_reviewer.model=qwen3-coder-next\n"
        "additional_reviewer.reasoning=low\n"
        "additional_reviewer.persona=security\n"
    )
    conf = _isolated_conf(board, monkeypatch, tmp_path, existing)
    monkeypatch.setenv("PM_NONINTERACTIVE", "1")
    monkeypatch.setattr(board, "REPO", tmp_path)
    # init 은 areas repo 행을 항상 등록하므로(T-0779) 레지스트리·락 경로도 tmp 로 묶는다.
    pm_dir = tmp_path / ".project_manager"
    (pm_dir / ".local").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(board, "AREAS_FILE", pm_dir / "areas.md")
    monkeypatch.setattr(board, "LOCAL_DIR", pm_dir / ".local")
    monkeypatch.setattr(board, "BOARD_LOCK", pm_dir / ".local" / "board.lock")
    monkeypatch.setattr(board, "LEASES_FILE", pm_dir / ".local" / "worktree-leases.json")
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
    assert parsed["additional_reviewer.enabled"] == "true"
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
    """지속 동의 + 수렴 게이트 규율(3R·발산(증가) 차단·confirm-fix 1회)이 codex 카드에 명시된다."""
    text = _card_with_operational_details(CODEX_PM_REVIEW)
    assert "additional_reviewer.enabled=true" in text
    assert "후속 호출마다 비용을 다시 묻지 않는다" in text
    assert "기계적 anti-loop 정지" in text
    assert "--rounds-report" in text
    for contract in CONVERGENCE_GATE_CONTRACTS:
        assert contract in text, f"수렴 게이트 서술 누락: {contract}"
    # 재개 ack 가 남은 축은 wave 예산 하나 — 라운드 축엔 연장 승인 경로가 없다.
    assert "--ack-wave" in text
    assert RETIRED_ROUND_ACK_FLAG not in text
    # 폐기된 규율: 재개 때마다 사용자 승인을 요구하던 문장.
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
    """공용 카드도 추가 리뷰어 역할·지속 동의·수렴 게이트 규율을 고정한다."""
    for path in SHARED_PM_REVIEW_CARDS:
        text = path.read_text(encoding="utf-8")
        details = path.parent / "references" / "operational-details.md"
        if details.is_file():
            text += "\n" + details.read_text(encoding="utf-8")
        assert "# /pm-review — 추가 리뷰어 교차검증 게이트" in text, path
        assert "additional reviewer" in text, path
        for key, value in EXPECTED_DEFAULTS:
            assert f"{key}={value}" in text, (path, key)
        assert "기계적 anti-loop 정지" in text, path
        for contract in CONVERGENCE_GATE_CONTRACTS:
            assert contract in text, (path, contract)
        assert "--ack-wave" in text, path
        assert RETIRED_ROUND_ACK_FLAG not in text, path
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
    assert "reviewer_cmd" in text          # 폐지된 통짜 커맨드 키를 이름으로 지목


def test_readme_documents_optin_tuple_and_no_per_review_reapproval():
    """README 는 외부 전송 opt-in 은 유지하되 리뷰마다 비용 승인은 요구하지 않는다."""
    text = (REPO / "README.md").read_text(encoding="utf-8")
    assert "외부로 전송" in text                      # 전송 사실은 계속 분명히
    assert "opt-in 을 결정한다" in text
    for key, value in EXPECTED_DEFAULTS:
        assert f"{key}={value}" in text
    assert "비용 승인을 다시 받지 않는다" in text
    # 폐지된 통짜 커맨드 키를 이름으로 지목한다(무엇이 더 이상 안 읽히는지).
    assert "`reviewer_cmd` 통짜 커맨드는 더 이상 읽히지 않는다" in text


def test_pm_role_makes_cap_ack_autonomous_not_a_cost_gate():
    """PM 매뉴얼: wave 예산 ack 는 자율 영역, 라운드 축은 연장 승인 자체가 없다."""
    text = (REPO / ".project_manager" / "wiki" / "pm_role.md").read_text(
        encoding="utf-8"
    )
    # 남은 ack 축(wave 예산)이 *자율* 절에 들어 있어야 한다 — 사용자 게이트 절이 아니라.
    autonomous = text.split("**자율+사후")[1].split("**사용자 게이트")[0]
    assert "--ack-wave" in autonomous
    assert "정상 수렴 ack" in autonomous
    # 폐지된 라운드 연장 승인은 자율 목록에서도 문서 전체에서도 사라져야 한다.
    assert RETIRED_ROUND_ACK_FLAG not in text
    assert "비용 동의는 **켤 때 한 번**이다" in text
    assert "기계적 anti-loop 정지" in text
    # 새 규율: 라운드 축의 출구는 재설계·티켓 분할이고 예외는 확인 전용 라운드 1회다.
    assert "리뷰 라운드 축은 연장 승인이 없다" in text
    assert "additional_reviewer.rounds_max" in text
    assert "--confirm-fix" in text
    assert "재설계·티켓 분할" in text


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
#   - 기계 식별자·전송 계약 — `external_review.py` · `additional_reviewer.enabled` · `reviewer_cmd`
#   - 히스토리 — ADR · CHANGELOG · done 티켓 · archive log · 과거 지적 인용("codex 게이트 must-fix")
#   - 실제로 *수신 하네스*(선택된 기본 codex)를 가리키는 codex 전용 문맥 — egress 승격 절차 등
# 인벤토리를 파일 열거로 만드는 이유: 새 카드/새 타깃이 목록을 우회해 폐기 이름을 다시 실어도
# 자동으로 red 가 되게 하기 위해서다(하드코딩 목록은 반드시 뒤처진다).

# 폐기된 역할 이름 — 사람이 부르는 이름은 **추가 리뷰어**다. "외부 리뷰어"는 T-0597 sweep 대상이고,
# 인벤토리에 agent 카드가 없어 opencode architect 카드 하나가 R1 까지 살아남았다(카드 사각).
RETIRED_ROLE_PHRASES = ("codex 외부 교차검증", "codex 교차검증", "외부 리뷰어")

# 폐기된 활동 명사 — 활동 이름은 **추가 리뷰**다(T-0599). 역할(누가)과 축이 달라 따로 둔다:
# 구키 제거 릴리즈에서 한쪽만 풀릴 수 있고, 활동 명사는 역할 이름이 없는 문장("ticket → dev →
# 외부리뷰")에도 박혀 있어 역할 스캔이 통째로 놓쳤다.
RETIRED_ACTIVITY_PHRASES = ("외부리뷰",)

# 활성 표면 스캔이 보는 폐기 표현 전체. 두 축이 같은 인벤토리를 쓰므로 스캔은 하나다 —
# 축마다 스캔을 복사하면 새 표면이 한쪽 목록에만 들어가는 절반 커버가 생긴다.
RETIRED_REVIEW_PHRASES = (*RETIRED_ROLE_PHRASES, *RETIRED_ACTIVITY_PHRASES)

SKILL_CARD_ROOTS = (
    REPO / ".claude" / "skills",
    REPO / "templates" / "claude_code" / ".claude" / "skills",
    REPO / "templates" / "opencode" / ".claude" / "skills",
    REPO / "templates" / "codex" / ".agents" / "skills",
)

# agent 카드는 4 네임스페이스가 서로 다른 경로·포맷(codex 는 TOML)이라 스킬 카드 인벤토리로는
# 잡히지 않는다 — 역할 이름을 싣는 표면이므로 따로 열거한다(glob 패턴까지 명시).
AGENT_CARD_ROOTS = (
    (REPO / ".claude" / "agents", "*.md"),
    (REPO / "templates" / "claude_code" / ".claude" / "agents", "*.md"),
    (REPO / "templates" / "opencode" / ".opencode" / "agents", "*.md"),
    (REPO / "templates" / "codex" / ".codex" / "agents", "*.toml"),
)


def _shipping_agent_cards() -> list[Path]:
    """출하되는 역할 정의 카드 전수 (4 네임스페이스 × 역할). 열거 자체가 판정의 본질."""
    cards: list[Path] = []
    for root, pattern in AGENT_CARD_ROOTS:
        assert root.is_dir(), f"agent 네임스페이스 없음: {root}"
        found = sorted(root.glob(pattern))
        assert found, f"agent 카드 0개 — 인벤토리 앵커가 깨졌다: {root}"
        cards.extend(found)
    return cards

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
    return [*_shipping_skill_cards(), *_shipping_agent_cards(),
            *METHODOLOGY_SURFACES, *BOOTSTRAP_SOURCES]


def test_active_pm_surface_inventory_covers_agent_cards():
    """인벤토리가 agent 카드 4 네임스페이스를 실제로 포함한다 — 카드 사각(R1 실측)의 재발 차단."""
    scanned = {path.relative_to(REPO).as_posix() for path in _active_pm_surfaces()}
    for rel in (
        ".claude/agents/architect.md",
        "templates/claude_code/.claude/agents/architect.md",
        "templates/opencode/.opencode/agents/architect.md",
        "templates/codex/.codex/agents/architect.toml",
    ):
        assert rel in scanned, f"agent 카드가 인벤토리 밖: {rel}"


def _surface_label(path: Path) -> str:
    """진단용 자리 이름 — repo 안이면 repo-상대 경로, 밖이면 파일 이름.

    sensitivity 가 합성 표면(tmp)으로 검출기를 태울 수 있어야 하므로 repo 결합을 여기서만 푼다.
    """
    try:
        return path.relative_to(REPO).as_posix()
    except ValueError:
        return path.name


def _phrase_residue(paths, phrases, allowed_lines=None) -> list[str]:
    """주어진 표면들에서 폐기 표현이 놓인 자리 — `<자리>:<lineno> — <표현>` 목록.

    잔존 가드 3축(역할·활동 명사·구키)이 같은 검출기를 쓰게 만드는 단일 seam 이다. 축마다 스캔을
    베끼면 sensitivity 를 축마다 다시 증명해야 하고, 대개 한 축만 증명한 채 남는다.

    `allowed_lines` = `{repo상대경로: (허용 줄 텍스트, …)}` — **줄 단위 예외**다. 파일 통째
    예외는 그 파일에 새로 들어온 사용까지 영영 가려주지만, 줄 단위는 적어 둔 그 줄만 뺀다.
    """
    hits: list[str] = []
    for path in paths:
        assert path.is_file(), f"인벤토리 대상 부재: {path}"
        allowed = frozenset((allowed_lines or {}).get(_surface_label(path), ()))
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), 1
        ):
            if line.strip() in allowed:
                continue
            hits += [
                f"{_surface_label(path)}:{lineno} — {phrase}"
                for phrase in phrases
                if phrase in line
            ]
    return hits


def test_active_pm_surfaces_drop_the_retired_reviewer_names():
    """활성 출하 PM 표면 전수에 폐기된 역할 이름·활동 명사가 없다."""
    residue = _phrase_residue(_active_pm_surfaces(), RETIRED_REVIEW_PHRASES)
    assert not residue, "폐기된 명칭 잔존(활성 표면):\n  " + "\n  ".join(residue)


def test_retired_phrase_scan_detects_an_injected_residue(tmp_path):
    """주입한 폐기 표현을 검출기가 실제로 잡고, 복원하면 다시 green (비공허 sensitivity).

    잔존 0 단언은 스캔이 죽어도(빈 인벤토리·오탈자 패턴) 통과한다 — 검출기 자체를 합성
    표면으로 태워 "잡을 수 있음"을 증명한다.
    """
    surface = tmp_path / "SKILL.md"
    clean = "위임(`pm_delegate.py`)과 추가 리뷰(`external_review.py`)는 raw 를 예약한다.\n"
    surface.write_text(clean, encoding="utf-8")
    assert _phrase_residue([surface], RETIRED_REVIEW_PHRASES) == []

    surface.write_text(clean + "금지(반드시 ticket → dev → 외부리뷰)\n", encoding="utf-8")
    hits = _phrase_residue([surface], RETIRED_REVIEW_PHRASES)
    assert hits == ["SKILL.md:2 — 외부리뷰"], hits

    surface.write_text(clean, encoding="utf-8")
    assert _phrase_residue([surface], RETIRED_REVIEW_PHRASES) == []


# 활동 명사 sweep 이 닿은 엔진 파일 — 위임/추가 리뷰 실행 축 4종(T-0600). **엔진 파일 전체를
# 잔존 스캔 인벤토리(`_active_pm_surfaces`)에 넣지는 않는다**: 거긴 과거 지적 인용·릴리즈 서술
# 같은 히스토리가 사는 자리라(`test_role_rename_keeps_transport_identifiers_and_history` 가 그
# 경계를 값으로 고정한다) 전면 스캔은 과거 기록 개서를 요구한다. 여기서 보는 건 **활동 명사
# 하나**뿐이고, 역할 이름·codex 인용은 대상이 아니다.
_ACTIVITY_SWEEP_ENGINE_FILES = (
    "pm_delegate.py", "external_review.py", "pm_relay.py", "pm_handoff.py",
    "pm_import.py",
)


@pytest.mark.parametrize("name", _ACTIVITY_SWEEP_ENGINE_FILES)
def test_swept_engine_files_drop_the_retired_activity_noun(name):
    """sweep 대상 엔진 파일에 폐기 활동 명사가 0건이다 (docstring·주석·CLI help 포함)."""
    residue = _phrase_residue([TOOLS / name], RETIRED_ACTIVITY_PHRASES)
    assert not residue, "폐기된 활동 명사 잔존(엔진):\n  " + "\n  ".join(residue)


def test_engine_cli_help_names_the_activity_as_additional_review(capsys):
    """**사용자에게 렌더되는** 엔진 CLI help 가 활동을 '추가 리뷰'로 부른다.

    파일 텍스트 스캔이 아니라 argparse 가 실제로 찍는 문자열을 태운다 — 사용자 노출 표면은
    소스 어디에 적혔는지가 아니라 출력이 진실이고, 히스토리 주석과 섞이지 않는다.
    """
    delegate = _load("pm_delegate")
    with pytest.raises(SystemExit):
        delegate._cmd_raw(["--help"])
    help_text = capsys.readouterr().out

    assert "추가 리뷰" in help_text
    for phrase in RETIRED_REVIEW_PHRASES:
        assert phrase not in help_text, f"CLI help 에 폐기 명칭 잔존 — {phrase}"


def test_retired_activity_noun_scan_covers_the_swept_surfaces():
    """활동 명사 sweep 대상 8파일이 스캔 인벤토리 안에 있다 — 좁은 스캔의 false-green 방지."""
    scanned = {path.relative_to(REPO).as_posix() for path in _active_pm_surfaces()}
    expected = {
        f"{prefix}.claude/skills/pm-dev-delegate/SKILL.md"
        for prefix in ("", "templates/claude_code/", "templates/opencode/")
    } | {
        "templates/codex/.agents/skills/pm-dev-delegate/SKILL.md",
        ".project_manager/wiki/pm_role.md",
        *(f"templates/{flavor}/.project_manager/wiki/pm_role.md"
          for flavor in TEMPLATE_DIRS),
    }
    assert len(expected) == 8
    assert expected <= scanned, f"sweep 대상이 스캔 밖: {sorted(expected - scanned)}"


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
    for phrase in RETIRED_REVIEW_PHRASES:
        assert phrase not in card, f"카드에 폐기 이름 잔존 — {phrase}"


def test_role_rename_keeps_transport_identifiers_and_history():
    """이름 변경은 사람 표면 한정 — 기계 식별자와 히스토리는 그대로 둔다.

    인벤토리 밖이어야 하는 것(엔진 히스토리 주석)을 값으로 못박아, 가드가 히스토리까지
    번지지 않게 경계를 고정한다.
    """
    # 전송/설정의 기계 이름은 활성 카드에서도 계속 쓰인다.
    card = CANONICAL_PM_REVIEW.read_text(encoding="utf-8")
    assert "external_review.py" in card
    assert "additional_reviewer.enabled=true" in card
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


# ── 축 5: 폐지된 라운드 연장 승인 플래그 잔재 0 (T-0598) ─────────────────────
#
# T-0593 이 라운드 연장 승인을 엔진에서 폐지했다(호출하면 rc=1 거부·아무것도 실행 안 함). 출하
# 문서가 그 플래그를 계속 가르치면 PM 이 존재하지 않는 출구를 시도하고, 문서-엔진 모순이 그대로
# 운영 지침이 된다. 그래서 출하 표면의 **잔존 0** 을 기계로 못박는다. 잔존이 정당한 자리는 셋뿐:
#   - `CHANGELOG.md` — 릴리즈 히스토리(그 시점의 동작 서술).
#   - 엔진 `external_review.py` — 폐지 거부 안내 문구(구 장부 필드는 제거됐다 · T-0772).
#   - **명시된 테스트 파일들** — 폐지 동작(거부)·구 장부 해석을 단언하는 테스트 자신.
# `tests/` 를 통째로 빼지 않는 이유: 그러면 테스트 docstring 에 남은 *옛 흐름 서술*(실제로 R1 이
# `test_external_review.py` 에서 잡았다)을 가드가 영영 못 본다. 파일을 이름으로 적고, 각 파일이
# 실제로 그 문자열을 갖고 있는지까지 단언해 목록이 썩지 않게 한다.
# 히스토리 디렉토리 제외는 두지 않는다 — dev-state(log·decisions·tickets 상태·sealed spike)는 PM
# 홈 repo 소유라 이 제품 repo 스캔에 애초에 없다(`.gitkeep` 뿐). 검증할 수 없는 공허한 예외는
# allowlist 를 헐겁게만 만든다.
# 잔존 스캔 3축(폐지 플래그·구 게이트 키·구 노브 키)이 공유하는 확장자 인벤토리. 산문·엔진만
# 보던 `.md`/`.py` 에 **실행/설정 표면**을 더한다(T-0600) — 진입 스크립트(`.sh`·`.cmd`)·opencode
# 설정(`.jsonc`)·codex agent 카드와 설정(`.toml`)도 폐기 키/플래그를 실을 수 있는 자리이고,
# 확장자 하나가 빠지면 그 표면의 잔존은 영영 안 보인다(현행 실잔재는 0 — 그 상태를 못박는다).
_RETIRED_ACK_SCAN_SUFFIXES = {".md", ".py", ".sh", ".cmd", ".jsonc", ".toml"}
# 엔진의 폐지 안내·구 장부 해석이 사는 파일 (canonical + 템플릿 미러 4벌 모두 같은 이름).
_RETIRED_ACK_ENGINE_FILE = "external_review.py"
# 폐지 동작을 단언하느라 플래그 리터럴이 정당하게 남는 테스트 (파일명 명시 — 디렉토리 통째 아님).
_RETIRED_ACK_TEST_FILES = (
    "tests/test_additional_reviewer_onboarding.py",   # 이 가드 자신(상수·부재 단언)
    "tests/test_external_review.py",                  # 어느 표면에서도 거부됨을 단언
    "tests/test_raw_output_ledger.py",                # 폐지 플래그 argv 의 장부 무변경
    "tests/test_review_convergence_gate.py",          # 수렴 축 서술(폐지 사실 인용)
)


def _retired_ack_scan_targets() -> list[Path]:
    """폐지 플래그 잔존을 검사할 출하 표면 (allowlist 제외 후)."""
    targets: list[Path] = []
    for path in repo_owned_paths(REPO, ".", mode=OWNED):
        if not path.is_file() or path.suffix.lower() not in _RETIRED_ACK_SCAN_SUFFIXES:
            continue
        rel = path.relative_to(REPO).as_posix()
        if rel == "CHANGELOG.md" or rel in _RETIRED_ACK_TEST_FILES:
            continue
        if path.name == _RETIRED_ACK_ENGINE_FILE:
            continue
        targets.append(path)
    return targets


def test_retired_round_ack_flag_has_no_residue_in_shipping_surfaces():
    """출하 문서·코드 전수에 폐지된 라운드 연장 승인 플래그가 0건이다."""
    residue = [
        f"{path.relative_to(REPO).as_posix()}:{lineno}"
        for path in _retired_ack_scan_targets()
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), 1)
        if RETIRED_ROUND_ACK_FLAG in line
    ]
    assert not residue, (
        f"폐지된 연장 승인 플래그 잔존({RETIRED_ROUND_ACK_FLAG}) — 새 흐름"
        "(3R 상한·발산(증가) 차단·confirm-fix 1회·출구=재설계/분할)으로 고쳐라:\n  "
        + "\n  ".join(residue)
    )


def test_retired_round_ack_scan_covers_the_swept_surfaces():
    """스캔 인벤토리가 sweep 대상을 실제로 포함한다 — 빈/좁은 스캔의 false-green 방지."""
    scanned = {path.relative_to(REPO).as_posix() for path in _retired_ack_scan_targets()}
    for rel in (
        ".claude/skills/pm-review/SKILL.md",
        "templates/claude_code/.claude/skills/pm-review/SKILL.md",
        "templates/opencode/.claude/skills/pm-review/SKILL.md",
        "templates/codex/.agents/skills/pm-review/SKILL.md",
        ".project_manager/wiki/pm_role.md",
        ".project_manager/wiki/pm_playbook.md",
        "templates/codex/.project_manager/wiki/pm_playbook.md",
        ".project_manager/tools/pm_bootstrap.py",
    ):
        assert rel in scanned, f"sweep 대상이 스캔 밖: {rel}"


@pytest.mark.parametrize("rel", [
    "pm-import.sh",                                     # 루트 진입 파사드(bash)
    "templates/claude_code/pm-update.cmd",              # Windows 진입 파사드
    "templates/opencode/.opencode/opencode.jsonc",      # opencode 어댑터 설정
    "templates/codex/.codex/agents/code-reviewer.toml",  # codex agent 카드
    "templates/codex/.codex/config.toml",               # codex 어댑터 설정
])
def test_residue_scan_covers_the_execution_and_config_surfaces(rel):
    """확장자 인벤토리가 실행/설정 표면까지 본다 (T-0600 — 좁은 스캔의 false-green 방지)."""
    scanned = {path.relative_to(REPO).as_posix() for path in _retired_ack_scan_targets()}
    assert rel in scanned, f"스캔 밖 표면: {rel}"


def test_retired_round_ack_allowlist_entries_are_load_bearing():
    """allowlist 는 실제로 그 문자열을 담은 자리만 뺀다 — 빈 예외는 가드를 헐겁게 만든다."""
    engine = (TOOLS / _RETIRED_ACK_ENGINE_FILE).read_text(encoding="utf-8")
    assert RETIRED_ROUND_ACK_FLAG in engine      # 거부 안내 + 구 장부 필드 해석 주석
    assert "폐지" in engine
    assert RETIRED_ROUND_ACK_FLAG in (REPO / "CHANGELOG.md").read_text(
        encoding="utf-8")                        # 릴리즈 히스토리
    for rel in _RETIRED_ACK_TEST_FILES:          # 목록이 썩으면(잔존 0 파일이 남으면) red
        path = REPO / rel
        assert path.is_file(), f"allowlist 대상 부재: {rel}"
        assert RETIRED_ROUND_ACK_FLAG in path.read_text(encoding="utf-8"), (
            f"{rel} 에 더는 폐지 플래그가 없다 — allowlist 에서 빼라(스캔 대상 복귀)."
        )


def test_retired_round_ack_allowlisted_tests_teach_the_new_flow():
    """allowlist 테스트의 *산문*(모듈 docstring)은 폐지된 재개 흐름을 가르치지 않는다.

    파일 단위 allowlist 의 값은 "리터럴은 허용, 옛 흐름 서술은 불허"다 — 거부를 단언하는 코드는
    플래그를 쓸 수밖에 없지만, 그 파일의 docstring 이 "승인 후 재개"를 계속 설명하면 읽는 사람이
    폐지 사실을 놓친다(R1 실측 지적).
    """
    retired_prose = ("승인 후 `--ack-rounds`", "승인 후에만 `--ack-rounds`",
                     "`--ack-rounds`로만 재개", "`--ack-rounds` 로만 재개")
    # 이 파일 자신은 제외 — 폐기 문구를 *열거* 하는 자리라 정당하다(test_terminology `_SELF` 동형).
    self_rel = Path(__file__).resolve().relative_to(REPO).as_posix()
    offenders = [
        f"{rel} — {phrase}"
        for rel in _RETIRED_ACK_TEST_FILES
        if rel != self_rel
        for phrase in retired_prose
        if phrase in (REPO / rel).read_text(encoding="utf-8")
    ]
    assert not offenders, "테스트 산문에 폐지된 재개 흐름 잔존:\n  " + "\n  ".join(offenders)


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
# 활성 플래그 하나만 없는 conf 는 "미결정"이지만 **대상은 이미 있을 수 있다** — 구조화 튜플만
# 손으로 적고 플래그를 안 켠 채택자다. 그 형상에 기본 4키를 그대로 append 하면 last-wins 로
# 사용자의 하네스/모델/추론 강도가 **조용히** 기본값이 된다.
# 그래서 "예" 는 대상 유무로 갈린다: 없으면 4키, 있으면 활성 플래그 한 줄.

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
)


def _external_review():
    """실행 코어 — **테스트만** 읽는다(온보딩 경로는 import 하지 않는다·키 대조용)."""
    return _load("external_review")


def test_onboarding_target_keys_mirror_the_execution_core(board, pm_update):
    """두 온보딩 사본의 키 이름이 실행 해소 코어의 선언과 글자 단위로 같다(드리프트 가드)."""
    core = _external_review()
    for module in (board, pm_update):
        assert module.ADDITIONAL_REVIEWER_KEYS == core.ADDITIONAL_REVIEWER_KEYS
        assert module.ADDITIONAL_REVIEWER_PREFIX == core.ADDITIONAL_REVIEWER_PREFIX


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("session=pm\n", "none", id="no-target"),
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
    assert parsed["additional_reviewer.enabled"] == "true"
    # 기존 대상의 값이 한 글자도 바뀌지 않는다.
    assert _parse_conf(after) | _parse_conf(text) == parsed
    assert parsed["additional_reviewer.harness"] == "opencode"
    assert parsed["additional_reviewer.model"] == "qwen3-coder-next"
    assert parsed["additional_reviewer.reasoning"] == "low"
    # 기본 프로필이 last-wins 로 사용자 선언을 덮지 않았음을 값으로 못박는다.
    for key, value in EXPECTED_DEFAULTS[1:]:
        assert parsed[key] != value, f"{key} 가 기본값으로 덮였다"
    assert "기존" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("text", "label"),
    [
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
    assert parsed["additional_reviewer.enabled"] == "true"
    assert parsed["additional_reviewer.harness"] == "opencode"
    assert parsed["additional_reviewer.model"] == "qwen3-coder-next"
    assert parsed["additional_reviewer.reasoning"] == "low"


def test_optin_yes_on_a_configured_target_appends_safely_without_trailing_newline(
    board, pm_update, monkeypatch, tmp_path
):
    """개행 없이 끝난 conf 의 마지막 대상 줄도 변질되지 않는다(두 진입 모두)."""
    newlineless = STRUCTURED_ONLY_CONF.rstrip("\n")
    conf = _isolated_conf(board, monkeypatch, tmp_path, newlineless)
    monkeypatch.setattr(board.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    board.prompt_external_review_optin()
    parsed = _parse_conf(conf.read_text(encoding="utf-8"))
    assert parsed["additional_reviewer.reasoning"] == "low"
    assert parsed["additional_reviewer.enabled"] == "true"

    dest, update_conf = _dest_with_conf(tmp_path / "u", newlineless)
    monkeypatch.delenv("PM_NONINTERACTIVE", raising=False)
    _tty(monkeypatch, "y")
    pm_update.maybe_prompt_external_review(dest)
    parsed = _parse_conf(update_conf.read_text(encoding="utf-8"))
    assert parsed["additional_reviewer.reasoning"] == "low"
    assert parsed["additional_reviewer.enabled"] == "true"


@pytest.mark.parametrize("text", [STRUCTURED_ONLY_CONF])
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
    assert _parse_conf(after)["additional_reviewer.enabled"] == "false"

    dest, update_conf = _dest_with_conf(tmp_path / "u", text)
    monkeypatch.delenv("PM_NONINTERACTIVE", raising=False)
    _tty(monkeypatch, "n")
    pm_update.maybe_prompt_external_review(dest)
    after = update_conf.read_text(encoding="utf-8")
    assert after.startswith(text)
    assert _parse_conf(after)["additional_reviewer.enabled"] == "false"


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
        pytest.param(STRUCTURED_ONLY_CONF, id="structured-target"),
        pytest.param("session=pm\nupstream.rev=abc123", id="newlineless"),
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
    assert "additional_reviewer.enabled" not in _parse_conf(conf.read_text(encoding="utf-8"))

    dest, update_conf = _dest_with_conf(tmp_path / "u", text)
    monkeypatch.delenv("PM_NONINTERACTIVE", raising=False)
    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": lambda self: True})())
    monkeypatch.setattr("builtins.input", _eof)
    pm_update.maybe_prompt_external_review(dest)
    assert update_conf.read_text(encoding="utf-8") == text
    assert "additional_reviewer.enabled" not in _parse_conf(
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
    original = f"session=pm\nupstream.path={source}\n"
    conf_path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(pm_update, "REPO", dest)

    pm_import = pm_update._load_pm_import()
    monkeypatch.setattr(pm_import, "read_upstream_rev", lambda *a, **k: None)
    monkeypatch.setattr(pm_update, "_load_pm_import", lambda: pm_import)
    monkeypatch.delenv("PM_NONINTERACTIVE", raising=False)
    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": lambda self: True})())

    def _eof(prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", _eof)

    assert pm_update._main([]) == 0
    assert conf_path.read_text(encoding="utf-8") == original, "EOF 인데 conf 가 바뀌었다"
    assert "additional_reviewer.enabled" not in _parse_conf(
        conf_path.read_text(encoding="utf-8")
    )


# ── 축 6: 판정은 **커밋 시점**의 conf 가 소유한다 (T-0590 R4) ────────────────
#
# 질문은 사람이 답할 때까지 열려 있다(수 초~수 분). 그 사이 다른 행위자가 같은 local.conf 를
# 바꿀 수 있다 — 다른 세션의 `board.py init`, 병렬 `pm_update`, 손으로 여는 편집기. 질문 **전**
# 판정으로 기록하면 그사이 생긴 대상 위에 기본 4키가 얹혀, 축 4 가 닫은 손상(레거시와의 이중
# 대상·구조화의 last-wins)이 창만 바꿔 그대로 재현된다. 그래서 기록 직전에 다시 읽고 다시
# 판정하며, 재읽기→판정→append 는 배타락 + 단일 O_APPEND 로 닫는다.

_ENABLE_ONLY_HINT_TEXT = "local.conf 에 additional_reviewer.enabled=true"


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
        pytest.param("session=pm\nadditional_reviewer.enabled=false\n", id="decided-false"),
        pytest.param(
            "session=pm\nadditional_reviewer.enabled=true\n"
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
        pytest.param(STRUCTURED_ONLY_CONF, "structured", id="structured-appears"),
        pytest.param(
            STRUCTURED_ONLY_CONF.rstrip("\n"), "structured", id="newlineless",
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
    assert parsed["additional_reviewer.enabled"] == "true"
    assert (parsed["additional_reviewer.reasoning"]
            == _parse_conf(appeared)["additional_reviewer.reasoning"])
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
        kind, board, pm_update, monkeypatch, tmp_path, STRUCTURED_ONLY_CONF)
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
        "builtins.input", _answer_after_writing(conf, STRUCTURED_ONLY_CONF, "n"))

    run()

    after = conf.read_text(encoding="utf-8")
    assert after.startswith(STRUCTURED_ONLY_CONF)
    assert _parse_conf(after)["additional_reviewer.enabled"] == "false"
    out = capsys.readouterr().out
    assert _ENABLE_ONLY_HINT_TEXT in out
    assert "additional_reviewer.harness/model/reasoning" not in out, (
        "이미 대상이 있는데 구조화 키를 더 적으라고 안내했다")


@pytest.mark.parametrize("kind", ENTRIES)
@pytest.mark.parametrize(
    "appeared",
    [
        pytest.param(STRUCTURED_ONLY_CONF, id="target-appeared"),
        pytest.param("session=pm\nadditional_reviewer.harness=codex\n", id="broken"),
        pytest.param("session=pm\nupstream.rev=abc123", id="newlineless"),
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
    assert "additional_reviewer.enabled" not in _parse_conf(appeared)


@pytest.mark.parametrize("kind", ENTRIES)
@pytest.mark.parametrize(
    "existing",
    [
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
        assert (module._additional_reviewer_enable_hint(module.REVIEWER_TARGET_STRUCTURED)
                == _ENABLE_ONLY_HINT_TEXT)
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


# ── 축 6: 게이트·노브 키 표기 (dot notation 단일 표기 · T-0767) ─────────────
#
# `local.conf` 표기가 dot notation 하나로 통일되면서 이 축의 키도 `additional_reviewer.<속성>`
# 이 됐다. 구표기(flat `additional_reviewer_*`·`external_review_*`)의 **1릴리즈 fallback·감지
# 상수·안내 깔때기는 전부 제거**됐다 — 잔존 구표기는 이 모듈이 아니라 공용 로더가 conf 를 읽는
# 지점에서 fail-loud 로 막고 키 단위 교체를 처방한다(`tests/test_local_conf_notation.py`).
# 여기서는 **현재 이름이 세 진입에서 같고, 그 이름만 값을 공급한다**는 것만 못박는다.
GATE_KEY = "additional_reviewer.enabled"

# 각 노브의 해소 함수와 엔진 기본값 — 어느 축이 어느 키를 읽는지까지 못박는다(세 키가 한 표에서
# 파생하므로 배선이 어긋나면 상한/예산이 서로의 값을 읽는다).
# 판정 라운드 상한은 이 표에 없다 — conf 노브 없이 엔진 고정값 하나다(대체 키 없음).
_KNOB_RESOLVERS = {
    "additional_reviewer.incomplete_rounds_max": ("_incomplete_round_limit", 2),
    "additional_reviewer.wave_budget": ("_wave_budget", 24),
}
KNOB_KEYS = tuple(_KNOB_RESOLVERS)


def test_gate_key_constant_is_shared_across_the_three_entries(board, pm_update):
    """세 진입(코어·board·pm_update)의 게이트 키 리터럴이 글자 단위로 같다."""
    core = _external_review()
    for module in (core, board, pm_update):
        assert module.ADDITIONAL_REVIEWER_ENABLED_KEY == GATE_KEY
    # 판정도 세 진입이 같다 — 결정을 공급하는 키는 하나뿐이다.
    for module, resolver in ((core, "enabled_decision_key"),
                             (board, "additional_reviewer_decision_key"),
                             (pm_update, "additional_reviewer_decision_key")):
        assert getattr(module, resolver)({GATE_KEY: "false"}) == GATE_KEY
        assert getattr(module, resolver)({}) is None


@pytest.mark.parametrize(
    ("conf", "enabled"),
    [
        pytest.param({GATE_KEY: "true"}, True, id="on"),
        pytest.param({GATE_KEY: "false"}, False, id="off"),
        pytest.param({}, False, id="undecided"),
    ],
)
def test_core_gate_reads_the_gate_key_only(conf, enabled):
    """코어 게이트는 이 키 하나만 읽는다 — 미결정은 OFF(기본 OFF 축)."""
    core = _external_review()
    assert core._is_enabled(conf) is enabled


@pytest.mark.parametrize(
    ("conf_shape", "expected"),
    [
        pytest.param("set", f"local.conf {GATE_KEY}=false", id="decided"),
        pytest.param("none", "local.conf 에 opt-in 결정 없음", id="undecided"),
    ],
)
def test_disabled_notice_names_the_key_that_supplied_the_decision(conf_shape, expected):
    """비활성 안내는 **결정을 공급한 키 실명**으로 현재 상태를 말한다 (T-0600).

    미결정 conf 에 "그 키=false" 라고 쓰면 채택자가 자기 파일에 없는 줄을 인용받는다 — 처방을
    적용할 자리를 못 찾는다. 그래서 미결정은 키 이름 대신 "결정 없음"을 말한다.
    """
    core = _external_review()
    conf = {"set": {GATE_KEY: "false"}, "none": {}}[conf_shape]

    notice = core.disabled_gate_notice(conf)

    assert expected in notice
    assert f"`{GATE_KEY}=true`" in notice                 # 처방은 언제나 이 키


def test_onboarding_blocks_write_the_gate_key_only(board, pm_update):
    """새로 기록하는 블록(수락·플래그만·거절)·안내가 모두 현재 키를 쓴다."""
    for module in (board, pm_update):
        for block in (module.ADDITIONAL_REVIEWER_OPTIN_BLOCK,
                      module.ADDITIONAL_REVIEWER_ENABLE_ONLY_BLOCK,
                      module.ADDITIONAL_REVIEWER_DECLINE_BLOCK):
            assert GATE_KEY in block, block
        for hint in (module.ADDITIONAL_REVIEWER_ENABLE_HINT,
                     module.ADDITIONAL_REVIEWER_ENABLE_ONLY_HINT):
            assert GATE_KEY in hint


def test_knob_key_constants_match_the_engine_table():
    """엔진의 노브 키 상수가 글자 단위로 이 표와 같다 (배선 드리프트 가드)."""
    core = _external_review()
    assert (core.ADDITIONAL_REVIEWER_INCOMPLETE_ROUND_LIMIT_KEY
            == "additional_reviewer.incomplete_rounds_max")
    assert core.ADDITIONAL_REVIEWER_WAVE_BUDGET_KEY == "additional_reviewer.wave_budget"
    assert set(KNOB_KEYS) == {
        core.ADDITIONAL_REVIEWER_INCOMPLETE_ROUND_LIMIT_KEY,
        core.ADDITIONAL_REVIEWER_WAVE_BUDGET_KEY,
    }
    # 판정 상한은 노브도 축도 아니다 — 키 상수·해소 함수·엔진 기본값이 모두 없다(축 제거).
    assert not hasattr(core, "ADDITIONAL_REVIEWER_ROUND_LIMIT_KEY")
    assert not hasattr(core, "_round_limit")
    assert not hasattr(core, "DEFAULT_ROUND_LIMIT")


@pytest.mark.parametrize("key", KNOB_KEYS, ids=list(KNOB_KEYS))
@pytest.mark.parametrize(
    ("conf_shape", "expected"),
    [
        pytest.param("set", 3, id="configured"),
        pytest.param("none", None, id="unset-default"),
    ],
)
def test_knob_resolution_reads_the_configured_key(key, conf_shape, expected):
    """노브 해소: 그 키가 값을 공급하고, 없으면 엔진 기본값이다(`expected=None`)."""
    core = _external_review()
    resolver_name, engine_default = _KNOB_RESOLVERS[key]
    conf = {"set": {key: "3"}, "none": {}}[conf_shape]

    resolved = getattr(core, resolver_name)(conf)
    assert resolved == (engine_default if expected is None else expected)


def test_empty_knob_value_is_unset():
    """공백만 있는 값은 "설정 안 함" 이라 엔진 기본값으로 간다(값 공급 판정의 의미 승계)."""
    core = _external_review()
    for key in KNOB_KEYS:
        resolver_name, engine_default = _KNOB_RESOLVERS[key]
        resolve = getattr(core, resolver_name)
        assert resolve({key: "   "}) == engine_default
        assert core.knob_value_key({key: "   "}, key) is None


@pytest.mark.parametrize("broken", ["abc", "-1", "3.5"])
def test_broken_knob_value_falls_to_the_engine_default(broken):
    """깨진 값은 엔진 기본값으로 간다 — 공급 판정은 값의 존재이지 형식이 아니다 (T-0600 엣지)."""
    core = _external_review()
    for key in KNOB_KEYS:
        resolver_name, engine_default = _KNOB_RESOLVERS[key]
        assert getattr(core, resolver_name)({key: broken}) == engine_default
        # 값을 공급한 키는 여전히 그 키다(형식 오류가 공급 사실을 뒤집지 않는다).
        assert core.knob_value_key({key: broken}, key) == key


def test_engine_guidance_names_the_knob_keys():
    """상한/예산 차단 안내가 현재 키를 가르친다 — 안내는 채택자가 값을 고치는 유일한 접점이다."""
    core = _external_review()
    round_guidance = core._ROUND_LIMIT_GUIDANCE
    assert core.ADDITIONAL_REVIEWER_INCOMPLETE_ROUND_LIMIT_KEY in round_guidance
    assert "additional_reviewer.round_limit" not in round_guidance
    assert core.ADDITIONAL_REVIEWER_WAVE_BUDGET_KEY in core._WAVE_BUDGET_GUIDANCE


def test_internal_round_guidance_names_its_own_knob_key():
    """내부 축 거부 안내도 같은 규율이다 — 설정값·조정 키를 문구가 스스로 말한다."""
    delegate = _load("pm_delegate")
    assert (delegate.INTERNAL_REVIEW_ROUNDS_MAX_KEY
            == f"delegate.{delegate.INTERNAL_REVIEW_ROLE}.rounds_max")
    guidance = delegate._INTERNAL_ROUND_REFUSAL
    assert "{knob}" in guidance and "{default}" in guidance
    assert "상한 3" not in guidance                  # 값 재타이핑 금지(설정값 주입)
    # 신키는 레지스트리에도 있다 — 채택자가 적으면 '모르는 키' 경고가 나면 안 된다.
    conf_module = _load("local_conf")
    assert delegate.INTERNAL_REVIEW_ROUNDS_MAX_KEY in conf_module.KNOWN_KEYS
    assert conf_module.unknown_keys(
        {delegate.INTERNAL_REVIEW_ROUNDS_MAX_KEY: "5"}) == ()
