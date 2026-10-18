"""local.conf 키 레지스트리와 그 lint 배선 (T-0761·T-0906).

`local.conf` 의 키 판정은 `local_conf.py`(T-0767)가 소유하고, T-0906 이 그 자리를 **키마다
설명·기본값·필수 여부를 든 레지스트리**(`KEY_REGISTRY`·`PATTERN_REGISTRY`)로 넓혔다. 이 파일은
그 레지스트리의 완전성과, 판정을 **소비만** 하는 board.py 배선의 회귀다 — 사본을 만들지 않는다:

1. `board.lint_local_conf_keys()` 가 `local_conf.load(...).unknown` 을 그대로 advisory 로 감싸는지.
2. `lint_tickets()` 합류 · kind 등재(`_ADVISORY_LINT_KINDS`) · `lint --gate` 종료코드 비기여.
3. 유입 경로 전수(오타·폐기 키·섹션 오기·대소문자) — 한 규칙이 전부 잡는지.
4. 역방향 — 정상 키·주석·빈 줄이 새지 않는지.
5. `board.py init` 병합 경로가 같은 함수·같은 목록을 1줄로 재사용하는지.
6. 가드 민감도 — 레지스트리에서 키 1개를 빼면 그 키를 담은 conf 가 red.
7. 레지스트리 자체 — 설명·기본값·필수 완전성, 옮겨 온 기본값 9개의 소비자 값 동일성,
   `missing_required`/`nonstandard` 경계, `--keys` 표, 필수 누락 lint, 문서 카탈로그 퇴역.

hermetic: 실 `local.conf` 파일을 tmp 에 만들어 board 도구가 그 파일을 읽어 해소한 값을 단언한다.
자식 프로세스 스폰 0.
"""
from __future__ import annotations

import argparse
import ast
import importlib.util
import inspect
import json
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from _textio import utf8_child_env
from pm_engine.cli import local_conf as local_conf_cli
from pm_engine.domain import conf_schema
from pm_engine.infra import conf_store

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
BOARD_PY = TOOLS / "board.py"
LOCAL_CONF_PY = TOOLS / "local_conf.py"
LOCAL_CONF_CLI_PY = TOOLS / "pm_engine" / "cli" / "local_conf.py"
README = REPO / "README.md"


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


def test_board_platform_resolution_does_not_duplicate_the_local_conf_parser():
    source = BOARD_PY.read_text(encoding="utf-8")
    start = source.index("def _platform_test_commands(")
    end = source.index("\ndef _platform_marker(", start)
    resolver = source[start:end]
    assert "qa.platforms" not in resolver
    assert ".partition(" not in resolver
    assert "platform_test_commands" in resolver


def test_readme_legacy_flat_key_example_names_both_reviewer_generations():
    """신·구 flat key를 한 번씩 적어 migration 방향을 뒤집지 않는다."""
    text = README.read_text(encoding="utf-8")
    legacy_example = text.split("옛 flat 표기", 1)[1].split("등)는", 1)[0]
    assert legacy_example.count("`external_review_enabled`") == 1
    assert legacy_example.count("`additional_reviewer_enabled`") == 1


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
    "lint_render_leak", "_run_lint_hooks",
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
        "delegate.developer.harness=claude\n"
        "delegate.developer.hard.fallback.model=gpt-x\n"
        "delegate.model_alias.mymodel=gpt-x\n"
        "additional_reviewer.enabled=false\n"
        "ctx.checkpoint_at_pct=70\n"
        "ctx.precompact_at_pct=80\n"
        "ctx.compact_at_pct=90\n"
        "harness.claude.ctx_window_tokens=200000\n"
        "harness.opencode.pro_model=foo\n"
        "harness.codex.idle_timeout=600\n"
        "\n"
        "# 빈 줄과 주석 사이에도 실키가 있을 수 있다\n"
        "upstream.path=/tmp/upstream\n"
    )
    conf = _write_conf(tmp_path, text)
    monkeypatch.setattr(board, "LOCAL_CONF", conf)

    assert board.lint_local_conf_keys() == []


# ── project-local QA platform declaration ───────────────────────────────────


def test_platform_commands_preserve_declared_order_and_are_known():
    conf = conf_schema
    values = conf.parse(
        "qa.platforms=linux-arm,windows\n"
        "test.linux-arm.cmd=run-arm\n"
        "test.windows.cmd=run-windows\n"
    )
    assert conf.platform_test_commands(values) == (
        ("linux-arm", "run-arm"), ("windows", "run-windows"),
    )
    assert conf.unknown_keys(values) == ()


@pytest.mark.parametrize("declaration", (
    "windows,windows", "core", "Windows", "win.dows", "win/dows", "win\\dows",
    "win dows", "", "a" * 33,
))
def test_platform_declaration_rejects_invalid_or_duplicate_names(declaration):
    conf = conf_schema
    values = {"qa.platforms": declaration, f"test.{declaration}.cmd": "wrapper"}
    if declaration == "":
        assert conf.platform_test_commands(values) == ()
    else:
        with pytest.raises(ValueError):
            conf.platform_test_commands(values)


@pytest.mark.parametrize("values", (
    {"qa.platforms": "windows"},
    {"qa.platforms": "windows", "test.windows.cmd": ""},
    {"qa.platforms": "windows", "test.windows.cmd": "wrapper", "test.linux.cmd": "other"},
))
def test_platform_declaration_rejects_missing_blank_and_orphan_commands(values):
    conf = conf_schema
    with pytest.raises(ValueError):
        conf.platform_test_commands(values)


def test_no_platform_declaration_keeps_legacy_semantics_but_orphan_is_advisory_unknown():
    conf = conf_schema
    assert conf.platform_test_commands({}) == ()
    assert conf.platform_test_commands({"qa.platforms": ""}) == ()
    assert conf.unknown_keys({"test.windows.cmd": "wrapper"}) == ("test.windows.cmd",)
    malformed = {
        "test.Windows.cmd": "x", "test.windows.extra": "x", "test.win/dows.cmd": "x",
    }
    assert conf.unknown_keys(malformed) == tuple(malformed)


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

    conf_module = conf_schema
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


# ── ⑦ 레지스트리 — 설명·기본값·필수의 정본 (T-0906) ─────────────────────────
# 이 절은 "키가 무엇이고 기본이 얼마인가" 의 자리가 코드 한 곳임을 값으로 못박는다. 흩어져 있던
# 기본값 상수 9개(board ctx 3 · additional_reviewer 5 · pm_delegate 1)는 삭제됐고, 소비자가 읽는
# 값이 이동 전과 같은지를 소비 지점에서 확인한다(alias 0 · 옛 경로 0).


@pytest.fixture(scope="module")
def conf_module():
    return conf_schema


@pytest.fixture(scope="module")
def consumers():
    """옮겨 온 기본값을 읽는 세 소비자 모듈 — 상수가 있던 바로 그 자리들."""
    return SimpleNamespace(
        board=_load_module("board_moved_defaults", BOARD_PY),
        reviewer=_load_module("reviewer_moved_defaults", TOOLS / "additional_reviewer.py"),
        delegate=_load_module("delegate_moved_defaults", TOOLS / "pm_delegate.py"),
    )


# T-0906 직전(레지스트리 도입 전) `KNOWN_KEYS` 원소 전수 — 파생 튜플이 이 집합을 **그대로**
# 유지하는지가 이관의 불변식이다(키를 늘리거나 줄이는 티켓이 아니다).
_KEYS_BEFORE_THE_REGISTRY = frozenset({
    "delegate.enabled", "delegate.timeout", "delegate.idle_timeout",
    "additional_reviewer.harness", "additional_reviewer.model",
    "additional_reviewer.reasoning", "additional_reviewer.incomplete_rounds_max",
    "additional_reviewer.wave_budget", "additional_reviewer.rounds_max",
    "additional_reviewer.paths", "additional_reviewer.timeout",
    "additional_reviewer.idle_timeout", "additional_reviewer.progress_signal",
    "ctx.checkpoint_at_pct", "ctx.precompact_at_pct", "ctx.compact_at_pct",
    "qa.platforms",
    "upstream.path", "upstream.rev", "upstream.seen_rev",
    "project.name", "project.tagline", "project.root", "project.date",
    "runtime.py", "test.cmd", "identity.user",
})


def test_known_keys_is_derived_from_the_registry_and_keeps_its_members(conf_module):
    """`KNOWN_KEYS` 는 레지스트리 파생이고 원소 집합이 이관 전과 같다 (사본 0·표면 불변)."""
    assert conf_module.KNOWN_KEYS == tuple(conf_module.KEY_REGISTRY)
    assert set(conf_module.KNOWN_KEYS) == set(_KEYS_BEFORE_THE_REGISTRY)


def test_every_registered_key_declares_description_default_and_required(conf_module):
    """실명 27키 전부가 설명·기본값 표현·필수 bool·표시 묶음을 갖는다 (완전성)."""
    for key, spec in conf_module.KEY_REGISTRY.items():
        assert spec.description.strip(), key
        assert spec.default is None or isinstance(spec.default, str), key
        assert isinstance(spec.required, bool), key
        assert spec.group in conf_module.KEY_GROUPS, (key, spec.group)


def test_declared_groups_is_a_single_key_groups_order_filter(conf_module):
    """`_declared_groups()` 는 `KEY_GROUPS` 순서 필터 **하나**다 — 뒤에 덧붙는 자리가 없다.

    레지스트리의 모든 `spec.group` 이 `KEY_GROUPS` 원소라(위 완전성 가드가 강제) "KEY_GROUPS
    밖 묶음을 뒤에 붙인다" 는 확장은 어떤 입력으로도 발화하지 않는다. 그 발화 불가 분기를 지운
    뒤의 계약을 세 축으로 못박는다 — 함수 본문이 단일 `return`(누적-확장 경로 재유입 차단) ·
    산출이 `KEY_GROUPS` 순서 필터와 같음 · `--keys` 절 이름/순서와 각 절의 키 목록 불변.
    새 묶음을 쓰는 유일한 경로는 `KEY_GROUPS` 에 순서를 선언하는 것이다."""
    used = {spec.group for spec in conf_module.KEY_REGISTRY.values()}
    assert used <= set(conf_module.KEY_GROUPS), used - set(conf_module.KEY_GROUPS)

    tree = ast.parse(LOCAL_CONF_CLI_PY.read_text(encoding="utf-8"))
    function = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == "_declared_groups")
    statements = [node for node in function.body
                  if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))]
    assert len(statements) == 1 and isinstance(statements[0], ast.Return), \
        "본문이 단일 return 이 아니다 — 순서 필터 뒤에 다른 경로가 붙었다"

    declared = local_conf_cli._declared_groups()
    assert declared == [group for group in conf_module.KEY_GROUPS if group in used]

    # `--keys` 표: 절 이름·순서가 그 목록 그대로이고, 각 절의 키 목록이 레지스트리 순서 그대로다.
    sections: dict[str, list[str]] = {}
    current = None
    for line in local_conf_cli.render_key_table().splitlines():
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            sections[current] = []
        elif current and line.split(" ", 1)[0] in conf_module.KEY_REGISTRY:
            sections[current].append(line.split(" ", 1)[0])
    assert list(sections) == declared
    for group, keys in sections.items():
        assert keys == [key for key, spec in conf_module.KEY_REGISTRY.items()
                        if spec.group == group], group


_PATTERN_SAMPLES = (
    ("delegate.developer.harness", "delegate"),
    ("delegate.developer.hard.model", "delegate"),
    ("delegate.developer.hard.fallback.reasoning", "delegate"),
    ("delegate.architect.fallback.reasoning", "delegate"),
    ("delegate.model_alias.mymodel", "delegate"),
    ("harness.opencode.pro_model", "harness"),
    ("harness.codex.idle_timeout", "harness"),
    ("test.windows.cmd", "runtime"),
)


def test_every_pattern_family_declares_a_spec_and_resolves_its_members(conf_module):
    """패턴 군 전부가 설명을 갖고, 실키가 그 군으로 해소된다 (실명·패턴 양쪽 해소)."""
    for pattern, spec in conf_module.PATTERN_REGISTRY.items():
        assert spec.description.strip(), pattern
        assert isinstance(spec.required, bool), pattern
        assert spec.group in conf_module.KEY_GROUPS, (pattern, spec.group)
    for key, group in _PATTERN_SAMPLES:
        spec = conf_module.key_spec(key)
        assert spec is not None and spec.description.strip(), key
        assert spec.group == group, key
        assert conf_module.pattern_for(key) in conf_module.PATTERN_REGISTRY, key
    # 군에 속하지 않는 이름은 해소되지 않는다 (판정이 헐거워지지 않았다).
    for miss in ("delegate.no_such_role.model", "harness.no_such.idle_timeout",
                 "diff_cap.huge", "test.windows.exe", "nope.nope"):
        assert conf_module.key_spec(miss) is None, miss


def test_no_row_exists_that_nothing_reads(conf_module):
    """어디서도 안 읽히는 칸은 레지스트리에 없다 — 화면에 서면 채워도 아무 일이 없다.

    hard 티어는 `TIERED_ROLES` 역할만 읽고(나머지 역할의 `--tier` 는 usage error),
    안내 문서 모델 칸은 렌더 표(`OPERATIONAL_CONF_KEYS`)에 토큰이 있는 도구만 읽는다.
    """
    tiered = conf_module.TIERED_ROLES
    for role in conf_module.ROLE_CHOICES:
        for key in (f"delegate.{role}.hard.model", f"delegate.{role}.hard.fallback.harness"):
            expected = conf_module.DELEGATE_ROLE_PATTERN if role in tiered else None
            assert conf_module.pattern_for(key) == expected, key
    rendered = conf_schema.OPERATIONAL_CONF_KEYS.values()
    for name in conf_module.HARNESS_CHOICES:
        key = f"harness.{name}.pro_model"
        assert (conf_module.pattern_for(key) is not None) == (key in rendered), key


def test_required_is_only_what_the_core_commands_cannot_run_without(conf_module):
    """필수는 두 키뿐이다 — 위임 역할 키를 필수로 두면 새 import 마다 누락 배너가 뜬다."""
    assert conf_module.required_keys() == ("runtime.py", "test.cmd")
    for optional in ("delegate.developer.model", "delegate.developer.harness",
                     "additional_reviewer.harness", "project.name"):
        spec = conf_module.key_spec(optional)
        assert spec is not None and spec.required is False, optional


_MOVED_DEFAULTS = (
    pytest.param("ctx.checkpoint_at_pct", "70",
                 lambda c: c.board._conf_default_int("ctx.checkpoint_at_pct"), 70,
                 id="board-ctx-checkpoint-at-pct"),
    pytest.param("ctx.precompact_at_pct", "80",
                 lambda c: c.board._conf_default_int("ctx.precompact_at_pct"), 80,
                 id="board-ctx-precompact-at-pct"),
    pytest.param("ctx.compact_at_pct", "90",
                 lambda c: c.board._conf_default_int("ctx.compact_at_pct"), 90,
                 id="board-ctx-compact-at-pct"),
    pytest.param("additional_reviewer.paths", "src/,tests/,scripts/,.project_manager/tools/",
                 lambda c: c.reviewer._configured_paths({}),
                 ["src/", "tests/", "scripts/", ".project_manager/tools/"],
                 id="reviewer-paths"),
    pytest.param("additional_reviewer.incomplete_rounds_max", "2",
                 lambda c: c.reviewer._incomplete_round_limit({}), 2,
                 id="reviewer-incomplete-rounds-max"),
    pytest.param("additional_reviewer.rounds_max", "2",
                 lambda c: c.reviewer._review_rounds_max({}), 2,
                 id="reviewer-rounds-max"),
    pytest.param("additional_reviewer.wave_budget", "24",
                 lambda c: c.reviewer._wave_budget({}), 24,
                 id="reviewer-wave-budget"),
)


@pytest.mark.parametrize("key,declared,probe,expected", _MOVED_DEFAULTS)
def test_moved_default_is_declared_once_and_read_by_its_consumer(
        conf_module, consumers, monkeypatch, key, declared, probe, expected):
    """레지스트리 선언값과 소비자가 읽는 값이 같다 — ctx 비율 셋은 board init 이 conf 에 쓰는 값이다."""
    monkeypatch.setattr(consumers.board, "local_config", lambda *a, **k: {})
    assert conf_module.default_for(key) == declared
    assert probe(consumers) == expected


def test_the_deleted_constants_left_no_alias_behind(consumers):
    """상수는 삭제됐다 — 같은 이름의 alias 가 남아 있으면 값이 두 곳에서 공급된다."""
    for name in ("CTX_NUDGE_PCT_DEFAULT", "CTX_STOP_PCT_DEFAULT",
                 "CTX_WINDOW_TOKENS_DEFAULT"):
        assert not hasattr(consumers.board, name), name
    for name in ("DEFAULT_PATHS", "DEFAULT_INCOMPLETE_ROUND_LIMIT",
                 "DEFAULT_REVIEW_ROUNDS_MAX", "DEFAULT_DIFF_CAPS", "DEFAULT_WAVE_BUDGET"):
        assert not hasattr(consumers.reviewer, name), name
    assert not hasattr(consumers.delegate, "DEFAULT_INTERNAL_REVIEW_ROUNDS_MAX")


def test_timeout_keys_declare_no_scalar_default_and_name_the_profile_table(conf_module):
    """하네스별 표가 정본인 키는 기본값을 지어내지 않는다 (한 칸에 담기지 않는 값)."""
    for key in ("delegate.timeout", "delegate.idle_timeout",
                "additional_reviewer.timeout", "additional_reviewer.idle_timeout"):
        spec = conf_module.key_spec(key)
        assert spec.default is None, key
        # 문구는 화면에 그대로 나가는 사람 말이라 코드 식별자를 담지 않는다 — 대신 값을 지어내지
        # 않았다는 사실(기본값 부재)과 안 채웠을 때 "도구마다 정해진 기본 시간을 씀"을 잰다.
        # (`하네스` 는 내부 용어라 화면 문구에서 `도구` 로 바뀌었다 — 재는 사실은 그대로다.)
        assert "도구마다" in spec.unset, key


@pytest.mark.parametrize("values,missing", (
    pytest.param({}, ("runtime.py", "test.cmd"), id="empty-conf"),
    pytest.param({"runtime.py": "python3"}, ("test.cmd",), id="one-declared"),
    pytest.param({"runtime.py": "", "test.cmd": ""}, (), id="declared-but-empty"),
    pytest.param({"runtime.py": "python3", "test.cmd": "pytest -q"}, (), id="all-declared"),
))
def test_missing_required_reports_only_undeclared_keys(conf_module, values, missing):
    """빈 문자열은 "있음" 이다 — 선언 유무만 보는 축이라 빈값 허용 키를 오탐하지 않는다."""
    assert conf_module.missing_required(values) == missing


def test_nonstandard_compares_only_keys_that_declare_a_default(conf_module):
    """편차 판정은 기본값이 선언된 키만 본다 (기준 없는 자리에 기준을 지어내지 않는다)."""
    deviation = conf_module.nonstandard({
        "ctx.compact_at_pct": "85",          # 선언된 기본과 다름 → 편차
        "harness.claude.ctx_window_tokens": "1000000",  # 패턴 키도 멤버 기본(200000)과 비교한다
        "ctx.precompact_at_pct": "80",       # 기본과 같음 → 편차 아님
        "runtime.py": "python3",             # 기본 선언 없음 → 비교 대상 아님
        "additional_reviewer.timeout": "900",  # 하네스 표가 정본 → 비교 대상 아님
        "nope.nope": "1",                    # 모르는 키 → 비교 대상 아님
    })
    assert deviation == {
        "ctx.compact_at_pct": ("85", "90"),
        "harness.claude.ctx_window_tokens": ("1000000", "200000"),
    }


def test_keys_cli_lists_all_keys(conf_module):
    """`local_conf.py --keys` 가 실명 27키·패턴 군·기본값을 사람 표면 하나로 낸다."""
    proc = subprocess.run(
        [sys.executable, str(LOCAL_CONF_PY), "--keys"],
        capture_output=True, text=True, encoding="utf-8", env=utf8_child_env(),
    )
    assert proc.returncode == 0, proc.stderr
    for key in conf_module.KNOWN_KEYS:
        assert key in proc.stdout, key
    for pattern in conf_module.PATTERN_REGISTRY:
        assert pattern in proc.stdout, pattern
    for value in ("70", "80", "90", "24"):
        assert value in proc.stdout, value
    assert "필수" in proc.stdout                       # 필수 칸이 표에 있다


# ── ⑧ `--keys --json` — 화면이 읽는 기계 표면 (T-0911) ──────────────────────
# 관리자 설정 화면의 항목 목록은 **그 프로젝트 사본의** 이 산출이다. 사람 표와 같은 레지스트리를
# 읽되 표시용 합성 표기는 싣지 않고, 패턴 키군은 전개된 실명(`members`)을 함께 낸다.


def _keys_json(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(LOCAL_CONF_PY), "--keys", *args],
        capture_output=True, text=True, encoding="utf-8", env=utf8_child_env(),
    )


@pytest.fixture(scope="module")
def keys_document() -> dict:
    proc = _keys_json("--json")
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_keys_json_carries_both_registries_split_by_kind(conf_module, keys_document):
    """실명 레지스트리와 패턴 레지스트리가 `kind` 로 갈려 **선언 순서 그대로** 실린다."""
    assert keys_document["schema"] == local_conf_cli.KEY_JSON_SCHEMA == 6
    assert keys_document["groups"] == list(conf_module.KEY_GROUPS)

    named = [row for row in keys_document["keys"] if row["kind"] == "key"]
    patterns = [row for row in keys_document["keys"] if row["kind"] == "pattern"]
    assert [row["key"] for row in named] == list(conf_module.KEY_REGISTRY)
    assert [row["key"] for row in patterns] == list(conf_module.PATTERN_REGISTRY)
    for row in named:
        spec = conf_module.KEY_REGISTRY[row["key"]]
        assert row["description"] == spec.description
        assert row["default"] == spec.default
        assert row["required"] == spec.required
        assert row["group"] == spec.group
        assert row["members"] is None, row["key"]     # 실명 키에는 전개할 축이 없다
    for row in patterns:
        assert row["choices"] is None, row["key"]     # 군 자체는 고를 값을 갖지 않는다
        assert row["recommended"] is None, row["key"]
    assert all(row["group"] in keys_document["groups"] for row in keys_document["keys"])


def test_pattern_members_take_the_registry_default_not_the_table_string(
        conf_module, keys_document):
    """멤버의 기본값은 `default_for` 그대로다."""
    families = {row["key"]: row for row in keys_document["keys"] if row["kind"] == "pattern"}
    for family in families.values():
        for member in family["members"]:
            assert member["default"] == conf_module.default_for(member["key"]), member


def test_every_expanded_member_resolves_back_to_its_own_family(conf_module, keys_document):
    """전개 축(`_DELEGATE_TIERS`·하네스·역할)이 파서(`pattern_for`)와 갈리면 red.

    전개는 선언에서 만들고 판정은 `_delegate_pattern_for` 가 한다 — 둘이 어긋나면 화면이 그
    사본이 모르는 키를 목록에 싣는다. 왕복이 그 어긋남의 유일한 관측점이다.
    """
    families = {row["key"]: row for row in keys_document["keys"] if row["kind"] == "pattern"}
    expanded = [(family, member["key"])
                for family, row in families.items() for member in row["members"]]
    # 하네스 3×3 + opencode 모델 1 · 역할 (구현 4티어 + 나머지 3역할×2티어)×3
    assert len(expanded) == 10 + 30, expanded
    for family, key in expanded:
        assert conf_module.pattern_for(key) == family, key
        assert key not in conf_module.KEY_REGISTRY, key


def test_every_row_carries_unit_switch_and_readonly(conf_module, keys_document):
    """schema 6 축 셋이 모든 행에 있고, 선언과 소비자가 말하는 것과 같다."""
    rows = _rows(keys_document)
    assert rows and all({"unit", "governed_by", "readonly"} <= set(row) for row in rows)

    # 세트 — 역할 묶음과 교차 검토 묶음만, 이름은 문서의 `units` 에 있다.
    units = {row["unit"] for row in rows if row["unit"] is not None}
    assert units == set(keys_document["units"])
    reviewer = [row["key"] for row in rows if row["unit"] == conf_module._REVIEWER_UNIT]
    assert reviewer == [f"additional_reviewer.{suffix}" for suffix in conf_module._ROLE_SUFFIXES]
    assert keys_document["unit_fields"] == local_conf_cli._ROLE_FIELD_LABELS

    # 스위치 — 가리키는 키는 레지스트리에 있고 그 자신은 스위치에 묶이지 않는다.
    switches = {row["governed_by"]["key"] for row in rows if row["governed_by"]}
    assert switches == set(conf_module._MASTER_SWITCHES)
    for key in switches:
        assert key in conf_module.KEY_REGISTRY and conf_module.key_spec(key).governed_by is None
    # 교차 검토 세트 멤버는 스위치가 아니라 세트 체크박스 하나로 켠다.
    assert all(conf_module.key_spec(key).governed_by is None for key in reviewer)

    assert [row["key"] for row in rows if row["readonly"]] == [
        "upstream.rev", "upstream.seen_rev"]


def test_the_delegate_switch_on_values_are_the_engine_verdict(conf_module, keys_document):
    """화면이 쓰는 켜짐 목록이 `pm_delegate._is_enabled` 판정과 같다 (목록 복제 0)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("pm_delegate_for_switch", TOOLS / "pm_delegate.py")
    delegate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(delegate)
    governor = next(row["governed_by"] for row in _rows(keys_document)
                    if row["governed_by"] and row["governed_by"]["on"] is not None)

    assert governor["key"] == delegate.DELEGATE_ENABLED_KEY
    assert governor["on"] == list(conf_schema.DELEGATE_ENABLED_VALUES)
    for value in governor["on"]:
        assert delegate._is_enabled({governor["key"]: value.upper()})
    for value in ("false", "0", "no", "off"):
        assert not delegate._is_enabled({governor["key"]: value})
        assert value not in governor["on"]


def test_unset_says_what_happens_as_a_sentence(conf_module, keys_document):
    """안 채웠을 때의 문구는 낱말이 아니라 일어나는 일이다 — 세트 멤버는 티어마다 다르다."""
    rows = {row["key"]: row for row in _rows(keys_document)}
    for key, row in rows.items():
        assert (row["default"] is None) == (row["unset"] is not None), key
    assert rows["delegate.developer.model"]["unset"] != rows["delegate.developer.hard.model"]["unset"]
    assert rows["delegate.developer.fallback.model"]["unset"] == \
        rows["delegate.developer.hard.fallback.model"]["unset"]
    cells = [row for row in rows.values() if row["unit"] == "delegate.developer.hard"]
    assert len({row["unset"] for row in cells}) == 1, "한 세트 안에서 문구가 갈린다"


def test_families_without_a_code_owned_axis_expand_to_nothing(conf_module, keys_document):
    """축이 코드에 없는 군은 빈 목록이다 — 이름을 지어내지 않는다.

    `test.<platform>.cmd` 의 축은 그 프로젝트의 `qa.platforms` 값이고,
    `delegate.model_alias.<name>` 의 축은 채택자가 정하는 이름이다. 화면은 이 두 군의 실키를
    conf 에 이미 있는 값에서 읽는다(자유 입력을 여는 자리는 platform 명령 한 군뿐).
    """
    families = {row["key"]: row["members"]
                for row in keys_document["keys"] if row["kind"] == "pattern"}
    empty = {family for family, members in families.items() if members == []}

    assert empty == {conf_module.PLATFORM_TEST_PATTERN, conf_module.DELEGATE_ALIAS_PATTERN}


def test_the_human_table_is_byte_identical_with_or_without_the_json_flag(conf_module):
    """`--json` 을 붙인 것은 표를 바꾸지 않는다 — 사람 표면은 이전 세대와 같은 바이트다."""
    table = _keys_json()

    assert table.returncode == 0, table.stderr
    assert table.stdout == local_conf_cli.render_key_table() + "\n"


@pytest.mark.parametrize("args", [["--json", "--keys"], ["--keys", "--json", "--json"],
                                  ["--keys", "--nope"]])
def test_the_keys_cli_takes_only_the_declared_two_forms(args):
    """받는 모양은 `--keys` 와 `--keys --json` 둘뿐이다(그 밖은 사용법 rc 2)."""
    proc = subprocess.run(
        [sys.executable, str(LOCAL_CONF_PY), *args],
        capture_output=True, text=True, encoding="utf-8", env=utf8_child_env())

    assert proc.returncode == 2, proc.stdout
    assert "--keys [--json]" in proc.stderr


def test_readme_catalog_retired(board, monkeypatch, tmp_path):
    """키 표의 자리는 코드다 — 출하 문서에 표가 남아 있거나 안내가 문서를 가리키면 red."""
    readme = README.read_text(encoding="utf-8")
    assert "| 키 | 기본값 | 뜻 |" not in readme
    assert "키 카탈로그" not in readme
    assert "local_conf.py --keys" in readme
    # 모르는-키 안내도 사라진 문서 절이 아니라 조회 명령을 가리킨다(dangling 참조 0).
    conf = _write_conf(tmp_path, "runtime.py=python3\ntest.cmd=pytest -q\nnope.nope=1\n")
    monkeypatch.setattr(board, "LOCAL_CONF", conf)
    detail = board.lint_local_conf_keys()[0][2]
    assert "local_conf.py --keys" in detail and "README" not in detail
# ── ⑨ 선택지·권장값 (schema 2 · T-0916) ─────────────────────────────────────
# 설정 화면이 "고를 수 있는 값" 과 "권장값" 을 엔진에서 받는다. 목록의 정본은 형제 선언
# (`pm_relay.HARNESS_CHOICES`·`REASONING_ALLOWED`·`MODEL_SOURCE`)이고 레지스트리는 그것을
# **파생**한다 — 복제본이 생기면 두 자리가 갈리므로 아래 단언이 그 파생을 유일한 관측점으로 삼는다.
# 선택지는 **검증이 아니다**: harness·reasoning 은 닫힌 집합, model 은 자유 문자열 그대로다.

RELAY_PY = TOOLS / "pm_relay.py"


@pytest.fixture(scope="module")
def relay_module():
    return _load_module("relay_for_local_conf_keys", RELAY_PY)


def _rows(document: dict) -> list[dict]:
    """`keys[]` + 전개된 `members[]` — 화면이 실제로 그리는 행 전부."""
    rows = list(document["keys"])
    rows.extend(member for row in document["keys"] for member in (row["members"] or []))
    return rows


def _choice_rows(document: dict, axis: str) -> list[dict]:
    """마지막 세그먼트가 `axis` 인 행 중 선택지를 든 것 (harness·model·reasoning)."""
    return [row for row in _rows(document)
            if row["choices"] is not None and row["key"].rsplit(".", 1)[-1] == axis]


def test_every_row_carries_the_choices_and_recommended_keys(keys_document):
    """모든 행의 모양이 같다 — 화면이 행마다 분기하지 않게 두 키를 항상 싣는다(없으면 null)."""
    rows = _rows(keys_document)

    assert len(rows) == 27 + 4 + 10 + 30      # 실명 27 · 군 4 · 하네스 10 · 위임 30
    for row in rows:
        assert "choices" in row and "recommended" in row, row["key"]


def test_harness_choices_are_the_relay_declaration_in_declaration_order(
        keys_document, relay_module):
    """하네스 선택지는 `pm_relay.HARNESS_CHOICES` 그대로다 — 순서까지 같다(복제 0)."""
    rows = _choice_rows(keys_document, "harness")

    assert len(rows) == 1 + 10, [row["key"] for row in rows]   # 추가 리뷰어 1 · 역할×티어 10
    for row in rows:
        assert row["choices"]["depends_on"] is None            # 하네스는 무엇에도 종속하지 않는다
        assert row["choices"]["values"] == {"": list(conf_schema.HARNESS_CHOICES)}
        assert row["choices"]["fetch"] is None
        assert row["choices"]["free_text"] is False


def test_reasoning_choices_are_the_relay_allowed_sets_bucketed_by_harness(
        keys_document, relay_module):
    """추론 강도 선택지는 `pm_relay.REASONING_ALLOWED` 와 하네스마다 **집합으로 같다**."""
    rows = _choice_rows(keys_document, "reasoning")
    expected = {harness: set(values)
                for harness, values in conf_schema.REASONING_ALLOWED.items()}

    assert expected and all(expected.values()), "허용집합이 비었다 — 파생이 조용히 강등됐다"
    assert len(rows) == 1 + 10, [row["key"] for row in rows]
    for row in rows:
        values = row["choices"]["values"]
        assert {harness: set(listed) for harness, listed in values.items()} == expected
        assert row["choices"]["free_text"] is False


def test_reasoning_rows_fetch_the_sibling_model_variants(conf_module, keys_document):
    """추론 행은 같은 세트 모델의 variant 를 모델 목록 조회 응답에서 받는다(정적 버킷은 claude·codex 뿐).

    모델마다 추론 강도가 다른 하네스(opencode)는 버킷이 없고, 화면은 `fetch.variants_of` 가 가리키는
    모델 키의 현재 값으로 응답의 `variants` 를 고른다. 직접 입력은 없다.
    """
    rows = _choice_rows(keys_document, "reasoning")

    assert len(rows) == 1 + 10, [row["key"] for row in rows]
    for row in rows:
        sibling = row["key"].rsplit(".", 1)[0] + ".model"
        assert row["choices"]["fetch"] == {
            "command": "delegate models", "arg": "harness", "variants_of": sibling}, row["key"]
        assert set(row["choices"]["values"]) == {"claude", "codex"}, row["key"]
        assert row["choices"]["free_text"] is False
        assert conf_module.key_spec(sibling) is not None, sibling
    for row in _choice_rows(keys_document, "model"):
        assert "variants_of" not in (row["choices"]["fetch"] or {}), row["key"]


def test_reasoning_descriptions_point_at_the_owner_instead_of_copying_the_ladder(
        conf_module, keys_document, relay_module):
    """추론 강도 키의 **설명**은 허용집합을 복제하지 않고 소유자를 가리킨다(T-0918·X-4).

    복제한 목록은 반드시 갈린다 — 설명이 `codex 는 low/medium/high/xhigh` 라고 적어 둔 동안
    선언은 이미 `max` 를 갖고 있었다(설명만 보고 고른 값이 소비 지점에서 거부된다).
    """
    ladder = {value for values in conf_schema.REASONING_ALLOWED.values()
              for value in values}
    assert ladder, "허용집합이 비었다(공허 가드)"
    pattern = re.compile(r"\b(" + "|".join(sorted(ladder, key=len, reverse=True)) + r")\b")
    keys = [row["key"] for row in _rows(keys_document)
            if row["key"].rsplit(".", 1)[-1] == "reasoning"]
    assert len(keys) == 1 + 10, keys

    for key in keys:
        description = conf_module.key_spec(key).description
        copied = pattern.findall(description)
        assert copied == [], f"{key}: 설명이 허용값을 복제한다 {copied}"
    # 값을 복제하지 않는다는 것이 이 가드의 목적이고, 그것은 위 루프가 이미 전수로 잰다.
    # 정본의 자리(코드 식별자)는 화면 문구에 싣지 않는다 — 설명은 사람이 읽는 자리다.


def test_model_rows_open_free_text_and_point_at_the_query_command(keys_document):
    """모델은 닫힌 목록이 없다 — 자유 입력 + 조회 명령(`delegate models`)을 함께 연다."""
    rows = _choice_rows(keys_document, "model")

    assert len(rows) == 1 + 10, [row["key"] for row in rows]
    for row in rows:
        assert row["choices"]["values"] == {}                  # 박아 둔 목록 0
        assert row["choices"]["fetch"] == {"command": "delegate models", "arg": "harness"}
        assert row["choices"]["free_text"] is True


def test_every_dependent_choice_names_a_declared_sibling_key(conf_module, keys_document):
    """종속 키는 **같은 세트의 harness 키**이고 그 키는 레지스트리가 아는 이름이다.

    화면은 `depends_on` 의 현재 값을 버킷으로 읽는다 — 그 이름이 이 사본에 없는 키면 버킷이 항상
    비어 목록이 사라진다(조용한 빈 목록). 왕복이 그 어긋남의 유일한 관측점이다.
    """
    dependents = [row for row in _rows(keys_document)
                  if row["choices"] is not None and row["choices"]["depends_on"]]

    assert len(dependents) == 2 * (1 + 10), [row["key"] for row in dependents]
    for row in dependents:
        depends_on = row["choices"]["depends_on"]
        assert depends_on == row["key"].rsplit(".", 1)[0] + ".harness"
        assert conf_module.key_spec(depends_on) is not None, depends_on


def test_the_listed_choices_all_pass_the_unchanged_validators(keys_document, relay_module):
    """선택지는 고르기 쉽게 하는 값이지 통과를 막는 값이 아니다 — 검증 규칙은 그대로다."""
    harness_row = _choice_rows(keys_document, "harness")[0]
    for harness in harness_row["choices"]["values"][""]:
        assert relay_module.validate_harness(harness) == harness

    reasoning_row = _choice_rows(keys_document, "reasoning")[0]
    for harness, values in reasoning_row["choices"]["values"].items():
        for value in values:
            assert relay_module.validate_reasoning(
                harness, value, model="m", cwd=REPO, env={}) == value
    with pytest.raises(relay_module.HarnessContractError):
        relay_module.validate_reasoning(                      # 닫힌 집합은 여전히 닫혀 있다
            "codex", "nope", model="m", cwd=REPO, env={})


def test_recommended_is_declared_only_where_there_is_evidence(conf_module, keys_document):
    """권장값은 근거(adopter 실측 별칭)가 있는 자리에만 있다 — 없는 자리는 지어내지 않는다.

    `.fallback` 티어는 "인프라 실패 1회 대체" 에 무엇을 권할지가 결정이라 없고,
    `additional_reviewer.*` 는 실측 model 이 버전 붙은 라이브 ID 라 엔진에 박으면 drift 한다.
    """
    carried = {row["key"] for row in _rows(keys_document) if row["recommended"] is not None}
    expected = {f"delegate.{role_tier}.{suffix}"
                for role_tier, values in conf_module.DELEGATE_RECOMMENDED.items()
                for suffix in values}

    assert carried == expected
    assert len(carried) == 5 * 3                              # 역할·티어 5 × 접미 3
    assert not [key for key in carried if ".fallback." in key]
    assert not [key for key in carried if key.startswith("additional_reviewer.")]


def test_recommended_values_are_inside_their_own_choices(conf_module, keys_document):
    """권장 harness·reasoning 은 그 키의 선택지 안이고, model 은 자유 문자열이다."""
    rows = {row["key"]: row for row in _rows(keys_document) if row["recommended"] is not None}

    for key, row in rows.items():
        suffix = key.rsplit(".", 1)[-1]
        recommended_harness = rows[f"{key.rsplit('.', 1)[0]}.harness"]["recommended"]
        if suffix == "harness":
            assert row["recommended"] in row["choices"]["values"][""]
        elif suffix == "reasoning":
            assert row["recommended"] in row["choices"]["values"][recommended_harness]
        else:
            assert row["choices"]["free_text"] and row["recommended"].strip()


def test_recommended_keys_are_real_role_and_tier_combinations(conf_module):
    """권장 표의 키는 `<role><tier>` 실조합이다 — 없는 역할·티어에 권장을 달지 않는다."""
    combinations = {f"{role}{tier}" for role in conf_module.ROLE_CHOICES
                    for tier in conf_module._role_tiers(role)}

    assert set(conf_module.DELEGATE_RECOMMENDED) <= combinations
    for values in conf_module.DELEGATE_RECOMMENDED.values():
        assert set(values) == set(conf_module._ROLE_SUFFIXES)   # 세트를 통째로 쓴다


def test_recommended_for_is_not_a_default(conf_module):
    """권장값은 기본값이 아니다 — `default_for` 는 계속 None 이고 저장 전엔 효과가 없다."""
    assert conf_module.recommended_for("delegate.developer.model") == "sonnet"
    assert conf_module.default_for("delegate.developer.model") is None
    assert conf_module.recommended_for("delegate.developer.fallback.model") is None
    assert conf_module.recommended_for("additional_reviewer.model") is None
    assert conf_module.recommended_for("nope.nope") is None


def test_the_human_table_does_not_read_the_new_json_fields(conf_module):
    """사람 표는 새 필드를 소비하지 않는다 — 바이트 불변의 구조적 근거.

    바이트 동일성 테스트는 같은 함수끼리 비교라 새 필드 누출을 못 잡는다. 표를 만드는 두 함수의
    소스에 그 이름이 아예 없음을 못박아 "JSON 표면에만 산다" 를 직접 확인한다.
    """
    rendered = inspect.getsource(local_conf_cli.render_key_table)
    displayed = inspect.getsource(local_conf_cli._default_display)

    for token in ("choices", "recommended"):
        assert token not in rendered, token
        assert token not in displayed, token


# ── 하네스 사용 한도 구성원 (T-0923 AT-024) ──────────────────────────────────


def test_harness_window_member_label_default_and_rule(conf_module, keys_document):
    """`harness.<이름>.ctx_window_tokens` — 기본값 200000·줄 이름 "사용 한도 (토큰)"·규칙 문장.

    규칙 문장은 구성원별 설명 자리가 없어 패턴 설명에 싣는다(화면이 구성원에 패턴 설명을 붙인다).
    옛 문구("컨텍스트 크기")가 남으면 화면이 공통 분모 시절의 뜻을 가르친다.
    """
    rule = "설정한 희망 기준을 보존하며, 실제 적용 기준은 독립 근거로 확인한 모델의 가용 한도 이하로 제한합니다."
    pattern_row = next(row for row in keys_document["keys"]
                       if row["key"] == conf_module.HARNESS_PATTERN)
    assert rule in pattern_row["description"]
    assert "컨텍스트 크기" not in pattern_row["description"]
    members = {member["key"]: member for member in pattern_row["members"]}
    for harness in ("claude", "codex", "opencode"):
        key = f"harness.{harness}.ctx_window_tokens"
        assert conf_module.default_for(key) == "200000", key
        assert conf_module.unset_for(key) is None, key
        assert members[key]["default"] == "200000", key
        assert pattern_row["unit_labels"][key] == f"{harness} 사용 한도 (토큰)", key
        spec = conf_module.key_spec(key)
        assert rule in spec.description and "컨텍스트 크기" not in spec.description, key


def _assert_blocking_removed_key(conf_module, key: str) -> None:
    """대체 없는 차단 구키 — 표에 None 으로 있고 비차단 집합 밖이며 어댑터 블록 두 벌에 실린다."""
    assert key in conf_module.LEGACY_KEY_MAP and conf_module.LEGACY_KEY_MAP[key] is None, key
    assert key not in conf_module.NON_BLOCKING_LEGACY_KEYS, key
    assert key not in conf_module.KEY_REGISTRY, key
    assert conf_module.key_spec(key) is None, key
    for style in ("python", "js"):
        assert f'"{key}",' in local_conf_cli.render_adapter_block(style), (style, key)


def _assert_conf_line_is_blocked(conf_module, tmp_path: Path, line: str, key: str) -> None:
    path = _write_conf(tmp_path, f"test.cmd=pytest -q\n{line}\n")
    with pytest.raises(conf_module.LegacyConfKeyError) as caught:
        conf_store.load_checked_readable(path)
    assert f"`{key}` → 제거(대체 키 없음)" in str(caught.value)


def test_code_reviewer_rounds_max_is_a_removed_key(conf_module, keys_document, tmp_path):
    """리뷰 횟수 상한 키는 대체 없이 제거된 차단 구키다 — 관리자 화면에 그 행이 없다."""
    key = "delegate.code-reviewer.rounds_max"
    _assert_blocking_removed_key(conf_module, key)
    assert key not in {row["key"] for row in _rows(keys_document)}
    assert all(row.get("label") != "검토 최대 횟수" for row in _rows(keys_document))
    _assert_conf_line_is_blocked(conf_module, tmp_path, f"{key}=3", key)


def test_diff_cap_keys_are_removed_keys(conf_module, keys_document, tmp_path):
    """작업 크기별 변경 줄 수 상한 키 셋은 대체 없이 제거된 차단 구키다 — 군도 레지스트리에 없다."""
    for estimate in ("small", "medium", "large"):
        _assert_blocking_removed_key(conf_module, f"diff_cap.{estimate}")
    assert not any(key.startswith("diff_cap") for key in conf_module.KEY_REGISTRY)
    assert not any(key.startswith("diff_cap") for key in conf_module.PATTERN_REGISTRY)
    assert "diff_cap" not in json.dumps(keys_document, ensure_ascii=False)
    _assert_conf_line_is_blocked(conf_module, tmp_path, "diff_cap.large=20000", "diff_cap.large")
