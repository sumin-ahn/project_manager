"""설정 현황·갱신 위임 소비자가 package CLI 로 옮긴 뒤의 실행 경계.

두 함수는 parameter 없이 실제 package 를 부른다.

- 현황(`config_status.cmd_status`)은 호출자가 넘긴 Home 하나에서만 읽는다. 같은 프로세스에서 Home 을
  바꿔 불러도 앞 Home 의 areas·conf·ticket·기록이 섞이지 않고, 기계 JSON 은 문서를 다 만든 뒤 한 줄로만
  나온다(실패면 stdout 0바이트).
- 갱신 위임(`config_install.cmd_update`·평면 `pm_config.py update`·`update_cmd.main`)은 `update` 뒤 원본
  토큰을 다시 해석하지 않고 그대로 한 번 넘긴다. 사용법·rc·예외는 두 진입이 같고, 평면 `pm_update.py`
  는 package CLI 를 부르는 실행 진입만 남는다.
"""
from __future__ import annotations

import ast
import importlib.util
import itertools
import json
import subprocess
import sys
from pathlib import Path

import pytest

from _git_fixture import git, init_git_repo
from _textio import utf8_child_env
# 현황 JSON 계약 시험의 상태 트리 해시를 그대로 쓴다(전후 비교 기준 하나).
from test_pm_config_status_json import _tree_digest
from pm_engine.app import adapter_report
from pm_engine.cli import config_cmd
from pm_engine.app import board_dashboard
from pm_engine.cli import config_install
from pm_engine.cli import config_status
from pm_engine.cli import update_cmd
from pm_engine.domain import manifest_rules
from pm_engine.infra import adapter_baseline
from pm_engine.infra import pm_paths

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

_AREAS_HEADER = (
    "| repo | prefix | git | test_cmd | owner | base | protected | area_owner |\n"
    "|---|---|---|---|---|---|---|---|\n"
)
_SCOPE_FLAGS = ("--with-board", "--with-adapters", "--with-remote")
_UPDATE_FACADE_PROG = "pm-config update"


def _load_pm_config():
    spec = importlib.util.spec_from_file_location("pm_config_s9_consumers", TOOLS / "pm_config.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _ticket_text(ticket_id: str, title: str) -> str:
    front = {
        "id": ticket_id, "title": title, "status": "open", "created": "2026-10-26",
        "created_by": "me", "claimed_by": None, "claimed_at": None, "completed_at": None,
        "depends_on": [], "blocks": [], "touches": [], "estimate": "small",
    }
    lines = ["---", *(f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in front.items()),
             "---", "", f"# {ticket_id} {title}", "", "## 목표", "본문", ""]
    return "\n".join(lines)


def _status_home(root: Path, label: str, *, separate_board: bool, collected: int):
    """실제 areas·conf·ticket·기록·슬롯(로컬 origin 의 clone)을 가진 Home 하나."""
    init_git_repo(root, commit="init")
    if separate_board:
        (root / ".project_manager" / "board" / "tickets").mkdir(parents=True)
    home = pm_paths.home_at(root)
    origin = root.parent / f"{label}-origin.git"
    seed = init_git_repo(root.parent / f"{label}-seed")
    _write(seed / "README.md", f"{label}\n")
    git(seed, "add", "README.md")
    git(seed, "commit", "-qm", "seed")
    git(root.parent, "clone", "-q", "--bare", str(seed), str(origin))
    slot = f"work/{label}_1"
    git(root, "clone", "-q", str(origin), str(root / slot))
    _write(pm_paths.areas_file(home),
           _AREAS_HEADER + f"| {label} |  | git@example.invalid:{label}.git |  | me | main |  | me |\n")
    _write(home.local_conf, f"project.name={label}\nzz.{label}.probe=1\n")
    _write(pm_paths.tickets_dir(home) / "open" / f"T-{collected:04d}.md",
           _ticket_text(f"T-{collected:04d}", f"{label} 열린 것"))
    _write(pm_paths.regression_flag(home),
           json.dumps({"head": label, "status": "pass", "rc": 0, "collected": collected}))
    _write(pm_paths.leases_file(home), json.dumps({"leases": [{
        "slot": slot, "repo": label, "session": "", "started": "2026-10-26T00:00:00+00:00",
        "state": "idle", "test_cmd": None,
    }], "tasks": []}))
    return home, slot


def _state_digest(home) -> str:
    return _tree_digest(home.root / ".project_manager")


def test_status_sections_use_explicit_home_and_keep_json_atomic(tmp_path, monkeypatch, capsysbinary):
    pm_config = _load_pm_config()
    parser = config_cmd.build_parser()
    home_one, slot_one = _status_home(tmp_path / "one" / "home", "alpha", separate_board=False,
                                      collected=11)
    home_two, slot_two = _status_home(tmp_path / "two" / "home", "beta", separate_board=True,
                                      collected=22)
    assert home_one.board_root != home_two.board_root

    calls: dict[str, list] = {}

    def spy(owner, name):
        real = getattr(owner, name)

        def recorded(*args, **kwargs):
            calls.setdefault(name, []).append(args[0] if args else None)
            return real(*args, **kwargs)
        monkeypatch.setattr(owner, name, recorded)

    spy(board_dashboard, "status_snapshot")
    spy(config_status, "_status_json_adapters")
    spy(config_status, "_status_json_remote")
    spy(config_status, "_source_missing_required")

    def run(home, *argv, engine_rev):
        calls.clear()
        rc = config_status.cmd_status(home, parser.parse_args(["status", *argv]),
                                      engine_rev=engine_rev)
        captured = capsysbinary.readouterr()
        return rc, captured.out, captured.err.decode("utf-8")

    def document(home, *argv, engine_rev):
        rc, out, err = run(home, "--json", *argv, engine_rev=engine_rev)
        assert rc == 0, err
        lines = out.decode("utf-8").splitlines()
        assert len(lines) == 1 and out.endswith(b"\n"), out
        return json.loads(lines[0])

    def expect_home(doc, home, label, slot, collected, engine_rev):
        assert doc["schema"] == config_status.STATUS_JSON_SCHEMA == 1
        assert doc["engine_rev"] == engine_rev
        assert doc["pm_home"] == str(home.root.resolve())
        assert [row["repo"] for row in doc["repos"]] == [label]
        assert [row["slot"] for row in doc["slots"]] == [slot]
        assert f"zz.{label}.probe" in doc["conf"]["unknown"]
        if doc["board"] is not None:
            assert doc["board"]["open"] == [
                {"id": f"T-{collected:04d}", "title": f"{label} 열린 것", "claimed_by": None}]
            assert doc["board"]["regression"]["collected"] == collected

    # 기존 첫 판독이 만드는 잠금 파일 뒤로는 상태 bytes 가 바뀌지 않는다.
    document(home_one, engine_rev="rev-one")
    document(home_two, engine_rev="rev-two")
    before = {id(home): _state_digest(home) for home in (home_one, home_two)}

    # Home 을 one → two → one 으로 바꿔도 앞 Home 의 입력이 섞이지 않는다(기본 호출은 무거운 절 null).
    for home, label, slot, collected, rev in (
        (home_one, "alpha", slot_one, 11, "rev-one"),
        (home_two, "beta", slot_two, 22, "rev-two"),
        (home_one, "alpha", slot_one, 11, "rev-one"),
    ):
        doc = document(home, engine_rev=rev)
        expect_home(doc, home, label, slot, collected, rev)
        assert doc["board"] is None and doc["adapters"] is None
        assert doc["conf"]["missing_for_latest"] is None
        assert doc["slots"][0]["remote"] is None
        assert calls == {}

    # 세 범위 플래그 8조합: 고른 절만 정확히 한 번, 고르지 않은 절은 null·미호출.
    for chosen in itertools.product((False, True), repeat=len(_SCOPE_FLAGS)):
        flags = [flag for flag, on in zip(_SCOPE_FLAGS, chosen) if on]
        board_on, adapters_on, remote_on = chosen
        source = ["--from", str(REPO)] if adapters_on else []
        for home, label, slot, collected, rev in (
            (home_two, "beta", slot_two, 22, "rev-two"),
            (home_one, "alpha", slot_one, 11, "rev-one"),
        ):
            doc = document(home, *flags, *source, engine_rev=rev)
            expect_home(doc, home, label, slot, collected, rev)
            assert calls.get("status_snapshot", []) == ([home] if board_on else [])
            assert calls.get("_status_json_adapters", []) == ([home] if adapters_on else [])
            assert calls.get("_status_json_remote", []) == ([home] if remote_on else [])
            assert len(calls.get("_source_missing_required", [])) == (1 if adapters_on else 0)
            assert (doc["board"] is None) is not board_on
            assert (doc["adapters"] is None) is not adapters_on
            assert (doc["conf"]["missing_for_latest"] is None) is not adapters_on
            remote = doc["slots"][0]["remote"]
            if remote_on:
                assert remote["url"].endswith(f"{label}-origin.git")
                assert remote["upstream"]["name"] == "origin/main"
                assert (remote["upstream"]["ahead"], remote["upstream"]["behind"]) == (0, 0)
            else:
                assert remote is None

    # `--from` 단독은 최신 기준 누락만 채운다(어댑터 절은 null).
    doc = document(home_one, "--from", str(REPO), engine_rev="rev-one")
    assert doc["adapters"] is None and isinstance(doc["conf"]["missing_for_latest"], list)
    assert len(calls["_source_missing_required"]) == 1 and "_status_json_adapters" not in calls

    after = {id(home): _state_digest(home) for home in (home_one, home_two)}
    assert after == before

    # JSON 없이 준 범위 플래그 네 개는 사용법 오류 rc 2 이고 아무 절도 읽지 않는다.
    for flag in (*_SCOPE_FLAGS, "--from"):
        argv = [flag, str(REPO)] if flag == "--from" else [flag]
        rc, out, err = run(home_one, *argv, engine_rev="rev-one")
        assert (rc, out) == (2, b"") and flag in err and "--json" in err
        assert calls == {}

    # 소스를 못 잡거나 실제 IO 가 실패하면 rc 1 이고 stdout 은 0바이트다.
    rc, out, err = run(home_one, "--json", "--with-adapters", engine_rev="rev-one")
    assert (rc, out) == (1, b"") and "[중단]" in err

    def unavailable(*_args, **_kwargs):
        raise adapter_baseline.AdapterConfigChannelUnavailable("injected adapter channel failure")
    with monkeypatch.context() as patch:
        patch.setattr(adapter_report, "adapter_config_reports", unavailable)
        rc, out, err = run(home_two, "--json", "--with-adapters", "--from", str(REPO),
                           engine_rev="rev-two")
    assert (rc, out) == (1, b"") and "injected adapter channel failure" in err

    git(home_one.root / slot_one, "remote", "remove", "origin")
    rc, out, err = run(home_one, "--json", "--with-remote", engine_rev="rev-one")
    assert (rc, out) == (1, b"") and "원격 대조 실패" in err and slot_one in err

    # 소스 키 CLI 가 실패하면 빈 목록으로 접지 않고 그 예외가 그대로 올라간다.
    broken = tmp_path / "broken-source"
    _write(broken / ".project_manager" / "tools" / "local_conf.py", "import sys\nsys.exit(3)\n")
    with pytest.raises(RuntimeError, match="rc=3"):
        run(home_two, "--json", "--from", str(broken), engine_rev="rev-two")
    assert capsysbinary.readouterr().out == b""


def test_update_raw_dispatch_preserves_usage_rc_and_cli_entry(tmp_path, monkeypatch, capsys):
    pm_config = _load_pm_config()
    facade_home = pm_paths.home_at(pm_config.REPO)
    other_home = pm_paths.home_at(tmp_path / "other")
    tokens = ["--dry-run", "--from", "경로 with space,콤마", "--", "--target", "a,b", "한글 값"]

    recorded: list[tuple] = []
    outcome: dict = {"result": 7}

    def fake_main(home, argv=None, *, prog="pm_update.py"):
        recorded.append((home, argv, prog))
        result = outcome["result"]
        if isinstance(result, BaseException):
            raise result
        return result

    with monkeypatch.context() as patch:
        patch.setattr(update_cmd, "_main", fake_main)

        # 평면 raw 분기·위임 handler·package CLI 가 같은 토큰을 같은 순서로 정확히 한 번 받는다.
        for invoke, home, prog in (
            (lambda: pm_config.main(["update", *tokens]), facade_home, _UPDATE_FACADE_PROG),
            (lambda: config_install.cmd_update(other_home, list(tokens)), other_home,
             _UPDATE_FACADE_PROG),
            (lambda: update_cmd.main(other_home, list(tokens)), other_home, "pm_update.py"),
        ):
            recorded.clear()
            assert invoke() == 7
            assert recorded == [(home, tokens, prog)]

        # 분류된 출하 오류는 두 진입에서 같은 rc 1·같은 문구, 그 밖 예외는 같은 객체로 올라간다.
        empty = manifest_rules.EmptyShippingInventoryError(tmp_path / "source", "templates/codex")
        outcome["result"] = empty
        for invoke in (lambda: pm_config.main(["update"]),
                       lambda: config_install.cmd_update(other_home, [])):
            assert invoke() == 1
            assert capsys.readouterr().err == f"오류: {empty}\n"
        failure = RuntimeError("갱신 본체 실패")
        outcome["result"] = failure
        for invoke in (lambda: pm_config.main(["update", "--dry-run"]),
                       lambda: config_install.cmd_update(other_home, ["--dry-run"])):
            with pytest.raises(RuntimeError) as raised:
                invoke()
            assert raised.value is failure

    # 원래 진입의 실제 parser: 두 prog 의 도움말과 인자 오류.
    capsys.readouterr()
    for invoke, prog in (
        (lambda argv: pm_config.main(["update", *argv]), _UPDATE_FACADE_PROG),
        (lambda argv: config_install.cmd_update(other_home, argv), _UPDATE_FACADE_PROG),
        (lambda argv: update_cmd.main(other_home, argv), "pm_update.py"),
    ):
        with pytest.raises(SystemExit) as helped:
            invoke(["--help"])
        assert helped.value.code == 0
        assert capsys.readouterr().out.startswith(f"usage: {prog} ")
        with pytest.raises(SystemExit) as refused:
            invoke(["--no-such-flag"])
        assert refused.value.code == 2
        err = capsys.readouterr().err
        assert err.startswith(f"usage: {prog} ") and "--no-such-flag" in err

    # 평면 pm_update.py 는 실행 진입만 남았다(옛 main 재수출 없음) — 실제 자식으로 package CLI 에 닿는다.
    flat = TOOLS / "pm_update.py"
    flat_tree = ast.parse(flat.read_text(encoding="utf-8"))
    assert not [node.name for node in flat_tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    child = subprocess.run(
        [sys.executable, str(flat), "--help"], cwd=tmp_path, capture_output=True, text=True,
        encoding="utf-8", env=utf8_child_env(), timeout=60, check=False,
    )
    assert child.returncode == 0, child.stderr
    assert child.stdout.startswith("usage: pm_update.py ")
    assert update_cmd.__doc__.splitlines()[0] in child.stdout.splitlines()
