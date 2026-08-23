"""local.conf 알려지지 않은 키 경고 (T-0761).

`local.conf` 의 known-key 판정은 `local_conf.py`(T-0767)가 소유한다(`KNOWN_KEYS`·`LEGACY_KEY_MAP`·
패턴 키군 전개). 이 파일은 그 판정을 **소비만** 하는 board.py 배선의 회귀다 — 사본을 만들지 않는다:

1. `board.lint_local_conf_keys()` 가 `local_conf.load(...).unknown` 을 그대로 advisory 로 감싸는지.
2. `lint_tickets()` 합류 · kind 등재(`_ADVISORY_LINT_KINDS`) · `lint --gate` 종료코드 비기여.
3. 유입 경로 전수(오타·폐기 키·섹션 오기·대소문자) — 한 규칙이 전부 잡는지.
4. 역방향 — 정상 키·주석·빈 줄이 새지 않는지.
5. `board.py init` 병합 경로가 같은 함수·같은 목록을 1줄로 재사용하는지.
6. 가드 민감도 — 레지스트리에서 키 1개를 빼면 그 키를 담은 conf 가 red.

hermetic: 실 `local.conf` 파일을 tmp 에 만들어 board 도구가 그 파일을 읽어 해소한 값을 단언한다.
외부 프로세스 스폰 0.
"""
from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
BOARD_PY = TOOLS / "board.py"
LOCAL_CONF_PY = TOOLS / "local_conf.py"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_board(alias: str = "board_local_conf_keys"):
    return _load_module(alias, BOARD_PY)


@pytest.fixture
def board():
    return _load_board()


def _write_conf(root: Path, text: str) -> Path:
    """tmp 트리에 실 `local.conf` 파일을 만든다 — 값 해소는 항상 이 파일에서 출발한다."""
    path = root / ".project_manager" / "local.conf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _wire_repo(board, monkeypatch, root: Path) -> Path:
    """`lint_tickets`/`cmd_lint` 구동에 필요한 최소 wiki 트리(빈 상태 디렉토리)를 깐다."""
    wiki = root / ".project_manager" / "wiki"
    tickets = wiki / "tickets"
    ideas = wiki / "ideas"
    decisions = wiki / "decisions"
    for status in board.STATUS_DIRS:
        (tickets / status).mkdir(parents=True, exist_ok=True)
    for status in board.IDEA_STATUS_DIRS:
        (ideas / status).mkdir(parents=True, exist_ok=True)
    decisions.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(board, "REPO", root)
    monkeypatch.setattr(board, "TICKETS_DIR", tickets)
    monkeypatch.setattr(board, "IDEAS_DIR", ideas)
    monkeypatch.setattr(board, "DECISIONS_DIR", decisions)
    return wiki


# ── ① lint_local_conf_keys() — load().unknown 소비 ─────────────────────────


def test_lint_local_conf_keys_wraps_the_registry_owned_unknown_property(board, monkeypatch, tmp_path):
    conf = _write_conf(tmp_path, "runtime.py=python3\nreview_ticket_body_max_bytes=65536\n")
    monkeypatch.setattr(board, "LOCAL_CONF", conf)

    findings = board.lint_local_conf_keys()

    assert len(findings) == 1, findings
    where, kind, detail = findings[0]
    assert kind == "local-conf-unknown-key"
    assert "review_ticket_body_max_bytes" in detail
    assert "runtime.py" not in detail  # known 키는 목록에 새지 않는다


def test_board_does_not_duplicate_the_known_key_registry():
    """레지스트리는 `local_conf.py` 하나 — board.py 에 사본(`KNOWN_KEYS`·`LEGACY_KEY_MAP`)이 0."""
    source = BOARD_PY.read_text(encoding="utf-8")
    assert "KNOWN_KEYS" not in source
    assert "LEGACY_KEY_MAP" not in source


def test_no_local_conf_file_is_quiet(board, monkeypatch, tmp_path):
    monkeypatch.setattr(board, "LOCAL_CONF", tmp_path / ".project_manager" / "local.conf")
    assert board.lint_local_conf_keys() == []


# ── ② lint_tickets 합류 · advisory 등재 · never-block ───────────────────────


def test_local_conf_unknown_key_kind_is_advisory(board):
    assert "local-conf-unknown-key" in board._ADVISORY_LINT_KINDS


_UNRELATED_LINT_FNS = (
    "lint_dependencies", "lint_bodies", "lint_ideas", "lint_status",
    "lint_wikilinks", "lint_unstable_refs", "lint_scopes",
    "lint_domain", "lint_adr_lifecycle", "lint_architecture_freshness",
    "lint_render_leak", "lint_unmigrated_overlay", "_run_lint_hooks",
)


def test_lint_tickets_includes_the_unknown_key_finding(board, monkeypatch, tmp_path):
    _wire_repo(board, monkeypatch, tmp_path)
    _write_conf(tmp_path, "runtime.py=python3\nadditional_reviewer.enalbed=true\n")
    monkeypatch.setattr(board, "LOCAL_CONF", tmp_path / ".project_manager" / "local.conf")
    for fn in _UNRELATED_LINT_FNS:
        monkeypatch.setattr(board, fn, lambda: [])

    issues = board.lint_tickets()

    assert any(kind == "local-conf-unknown-key" and "additional_reviewer.enalbed" in detail
               for _name, kind, detail in issues), issues


def test_gate_zero_on_unknown_key_only(board, monkeypatch, tmp_path):
    """오타 키가 있어도 `lint --gate` rc 0(never-block) · 무인자 `lint` 는 표면화(rc 1)."""
    _wire_repo(board, monkeypatch, tmp_path)
    _write_conf(tmp_path, "runtime.py=python3\nreview_ticket_body_max_bytes=65536\n")
    monkeypatch.setattr(board, "LOCAL_CONF", tmp_path / ".project_manager" / "local.conf")
    for fn in _UNRELATED_LINT_FNS:
        monkeypatch.setattr(board, fn, lambda: [])

    assert board.cmd_lint(SimpleNamespace(gate=True)) == 0
    assert board.cmd_lint(SimpleNamespace(gate=False)) == 1


# ── ③ 유입 경로 전수 — 오타·폐기 키·섹션 오기·대소문자 ──────────────────────


_UNKNOWN_KEY_CLASSES = (
    pytest.param("additional_reviewer.enalbed=true\n", "additional_reviewer.enalbed", id="typo"),
    pytest.param("review_ticket_body_max_bytes=65536\n", "review_ticket_body_max_bytes",
                 id="deprecated-key-v177-folder-cap"),
    pytest.param("status_total_style=compact\n", "status_total_style",
                 id="orphaned-key-no-reader-adr0023"),
    pytest.param("delegate.no_such_role.model=x\n", "delegate.no_such_role.model",
                 id="unknown-section-delegate-role"),
    pytest.param("harness.no_such_harness.idle_timeout=5\n", "harness.no_such_harness.idle_timeout",
                 id="unknown-section-harness-name"),
    pytest.param("diff_cap.huge=999999\n", "diff_cap.huge", id="unknown-section-diff-cap-estimate"),
    pytest.param("Runtime.Py=python3\n", "Runtime.Py", id="case-variant-of-known-key"),
    pytest.param("RUNTIME.PY=python3\n", "RUNTIME.PY", id="upper-case-variant-of-known-key"),
)


@pytest.mark.parametrize("conf_line,expected_key", _UNKNOWN_KEY_CLASSES)
def test_every_unknown_key_class_is_flagged(board, monkeypatch, tmp_path, conf_line, expected_key):
    conf = _write_conf(tmp_path, conf_line)
    monkeypatch.setattr(board, "LOCAL_CONF", conf)

    findings = board.lint_local_conf_keys()

    assert findings, f"{expected_key!r} 가 unknown 으로 걸리지 않는다"
    assert findings[0][1] == "local-conf-unknown-key"
    assert expected_key in findings[0][2]


def test_multiple_unknown_keys_surface_as_a_single_finding(board, monkeypatch, tmp_path):
    """여러 키가 동시에 있어도 finding 은 1개(1줄) — `cmd_init` 이 같은 형태를 재사용한다."""
    conf = _write_conf(
        tmp_path,
        "runtime.py=python3\n"
        "additional_reviewer.enalbed=true\n"
        "review_ticket_body_max_bytes=65536\n"
        "status_total_style=compact\n",
    )
    monkeypatch.setattr(board, "LOCAL_CONF", conf)

    findings = board.lint_local_conf_keys()

    assert len(findings) == 1, findings
    detail = findings[0][2]
    for key in ("additional_reviewer.enalbed", "review_ticket_body_max_bytes", "status_total_style"):
        assert key in detail, (key, detail)


# ── ④ 역방향 — 정상 키·주석·빈 줄이 새지 않는다 ─────────────────────────────


def test_known_keys_comments_and_blank_lines_produce_no_finding(board, monkeypatch, tmp_path):
    text = (
        "# per-clone 설정 — 주석은 무시된다\n"
        "\n"
        "runtime.py=python3\n"
        "test.cmd=pytest -q\n"
        "identity.user=a@b.example\n"
        "delegate.enabled=true\n"
        "delegate.code-reviewer.rounds_max=3\n"
        "delegate.developer.harness=claude\n"
        "delegate.developer.hard.fallback.model=gpt-x\n"
        "delegate.model_alias.mymodel=gpt-x\n"
        "additional_reviewer.enabled=false\n"
        "ctx.nudge_pct=30\n"
        "ctx.stop_pct=20\n"
        "ctx.window_tokens=200000\n"
        "harness.opencode.pro_model=foo\n"
        "harness.codex.idle_timeout=600\n"
        "diff_cap.small=4000\n"
        "\n"
        "# 빈 줄과 주석 사이에도 실키가 있을 수 있다\n"
        "upstream.path=/tmp/upstream\n"
    )
    conf = _write_conf(tmp_path, text)
    monkeypatch.setattr(board, "LOCAL_CONF", conf)

    assert board.lint_local_conf_keys() == []


def test_fresh_init_conf_has_zero_unknown_keys(board, monkeypatch, tmp_path):
    """`board.py init` 산출 fresh conf(주석 시드 포함)를 그대로 lint 하면 unknown 0."""
    pm = tmp_path / ".project_manager"
    pm.mkdir(parents=True)
    monkeypatch.setattr(board, "REPO", tmp_path)
    monkeypatch.setattr(board, "LOCAL_CONF", pm / "local.conf")
    monkeypatch.setattr(board, "_detect_py", lambda: "python3")

    board._write_init_local_conf()

    assert board.lint_local_conf_keys() == []


# ── ⑤ `board.py init` 병합 경로 — 같은 함수·같은 목록을 1줄로 표면화 ────────


def test_init_merge_path_surfaces_the_same_findings_pre_existing_unknown_key(board, monkeypatch, tmp_path):
    """병합이 손대지 않는 기존 unknown 키도 init 이 `lint_local_conf_keys()` 로 재확인 가능해진다.

    `_write_init_local_conf` 는 비파괴 병합이라 기존 오타 키를 지우지 않는다 — init 이 쓴 뒤에도
    `lint_local_conf_keys()` 를 다시 부르면 같은 finding 이 나온다는 것이 "같은 목록 재사용"의
    관측 가능한 계약이다(cmd_init 은 이 함수의 산출을 그대로 print 한다·본문 소스로 배선 확인).
    """
    pm = tmp_path / ".project_manager"
    pm.mkdir(parents=True)
    conf = pm / "local.conf"
    conf.write_text("runtime.py=python3\nreview_ticket_body_max_bytes=65536\n", encoding="utf-8")
    monkeypatch.setattr(board, "REPO", tmp_path)
    monkeypatch.setattr(board, "LOCAL_CONF", conf)
    monkeypatch.setattr(board, "_detect_py", lambda: "python3")

    board._write_init_local_conf()

    findings = board.lint_local_conf_keys()
    assert len(findings) == 1, findings
    assert "review_ticket_body_max_bytes" in findings[0][2]
    # 병합은 비파괴다 — 기존 오타 키가 지워지지 않았다(그래서 재확인이 뜻이 있다).
    assert "review_ticket_body_max_bytes" in conf.read_text(encoding="utf-8")


def test_cmd_init_source_reuses_lint_local_conf_keys_for_the_merge_path(board):
    """`cmd_init` 이 별도 목록/문구를 만들지 않고 `lint_local_conf_keys()` 를 그대로 재호출한다."""
    import inspect

    source = inspect.getsource(board.cmd_init)
    assert "lint_local_conf_keys()" in source


# ── ⑥ 가드 민감도 — 레지스트리에서 키 1개를 빼면 red ────────────────────────


def test_removing_a_known_key_from_the_registry_turns_its_conf_line_red(board, monkeypatch, tmp_path):
    """`local_conf.KNOWN_KEYS` 에서 `runtime.py` 를 빼면 그 키를 담은 conf 가 unknown 으로 뜬다.

    `board._load_local_conf()` 가 돌려주는(캐시된) 실 모듈 객체의 `KNOWN_KEYS` 를 직접 줄여
    board 가 그 축소된 레지스트리로 판정하게 만든다 — 사본이 아니라 board 가 실제로 소비하는
    그 객체를 흔든다. 대조군(줄이기 전)이 green 이라는 것도 같은 conf 로 먼저 확인한다.
    """
    conf = _write_conf(tmp_path, "runtime.py=python3\ntest.cmd=pytest -q\n")
    monkeypatch.setattr(board, "LOCAL_CONF", conf)

    assert board.lint_local_conf_keys() == [], "대조군(레지스트리 온전)부터 이미 red"

    conf_module = board._load_local_conf()
    reduced = tuple(key for key in conf_module.KNOWN_KEYS if key != "runtime.py")
    assert len(reduced) == len(conf_module.KNOWN_KEYS) - 1
    monkeypatch.setattr(conf_module, "KNOWN_KEYS", reduced)

    findings = board.lint_local_conf_keys()
    assert findings, "레지스트리에서 키를 빼도 advisory 가 뜨지 않는다 (가드 민감도 상실)"
    assert findings[0][1] == "local-conf-unknown-key"
    assert "runtime.py" in findings[0][2]


# ── ⑦ `cmd_init` 동적 구동 — 정적 소스 확인을 실 stdout 으로 보강 ───────────
# 픽스처는 `tests/test_local_conf_identity_keys_retired.py` 의 fresh-home 관용구와 동형이다.


def _wired_init_board(monkeypatch, tmp_path, alias: str):
    project = tmp_path / "proj"
    pm_dir = project / ".project_manager"
    tickets = pm_dir / "wiki" / "tickets"
    for status in ("open", "claimed", "blocked", "done"):
        (tickets / status).mkdir(parents=True, exist_ok=True)
    (pm_dir / ".local").mkdir(parents=True, exist_ok=True)
    module = _load_module(alias, BOARD_PY)
    for name, value in {
        "REPO": project,
        "TICKETS_DIR": tickets,
        "BOARD_FILE": pm_dir / "wiki" / "board.md",
        "LOG_FILE": pm_dir / "wiki" / "log" / "current.md",
        "STATUS_FILE": pm_dir / "wiki" / "status.md",
        "LOCAL_CONF": pm_dir / "local.conf",
        "AREAS_FILE": pm_dir / "areas.md",
        "LOCAL_DIR": pm_dir / ".local",
        "BOARD_LOCK": pm_dir / ".local" / "board.lock",
        "LEASES_FILE": pm_dir / ".local" / "worktree-leases.json",
        "PM_STATE_FILE": pm_dir / "wiki" / "pm_state.md",
        "PM_STATE_TEMPLATE": pm_dir / "wiki" / "pm_state.template.md",
    }.items():
        monkeypatch.setattr(module, name, value)
    monkeypatch.setattr(module, "install_pre_push_hook", lambda: False)
    monkeypatch.setattr(module, "prompt_external_review_optin", lambda: None)
    monkeypatch.setattr(module, "_configure_board_submodule", lambda: False)
    monkeypatch.setattr(module, "_git_config_email", lambda: None)
    monkeypatch.setattr(module, "_detect_py", lambda: "python3")
    monkeypatch.delenv("PM_SESSION_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_NAME", raising=False)
    return module


def _init_args(**kv):
    base = dict(prefix=None, area=None, owner=None, repo=None, slot=None,
                user=None, user_ack=None)
    base.update(kv)
    return argparse.Namespace(**base)


def test_cmd_init_prints_the_unknown_key_line_for_a_pre_existing_conf(monkeypatch, tmp_path, capsys):
    """`board.py init` 을 실제로 구동 — 병합 전부터 있던 오타 키가 stdout 에 1줄로 뜬다."""
    init_board = _wired_init_board(monkeypatch, tmp_path, "board_init_unknown_key")
    init_board.LOCAL_CONF.parent.mkdir(parents=True, exist_ok=True)
    init_board.LOCAL_CONF.write_text(
        "runtime.py=python3\nreview_ticket_body_max_bytes=65536\n", encoding="utf-8")

    rc = init_board.cmd_init(_init_args())

    assert rc == 0
    out = capsys.readouterr().out
    assert out.count("review_ticket_body_max_bytes") == 1, out  # 1회만(스팸 금지)
    assert "⚠" in out


def test_cmd_init_is_quiet_on_a_fresh_home(monkeypatch, tmp_path, capsys):
    """등록 0 인 fresh clone(conf 부재)은 unknown-key 줄을 안 낸다 — 오탐 0."""
    init_board = _wired_init_board(monkeypatch, tmp_path, "board_init_fresh_home")

    rc = init_board.cmd_init(_init_args())

    assert rc == 0
    out = capsys.readouterr().out
    assert "엔진이 모르는 local.conf 키" not in out
