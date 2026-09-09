"""`pm_relay.session_pid` — 장부에 적을 **세션** pid 해소 (T-0915).

기록되는 pid 가 "명령을 실행한 엔진 프로세스"이면 명령이 끝나는 순간 죽어 `status --json` 의
`alive` 가 늘 false 다(모든 세션이 회수 대기로 보인다). `session_pid` 는 그 자리에 "그 명령을
부른 하네스 세션"(claude·codex·opencode)을, 하네스가 없으면 직계 부모 셸을 준다.

검증 축:
  - 실 프로세스 사슬 — 하네스 이름 심링크로 띄운 더미 조상의 pid 를 돌려준다. 그 조상이
    끝나면 `pid_is_alive` 가 False 다(alive 계약의 양 끝).
  - 표 주입 음성 — 하네스 조상 없음(=부모 pid) · `claude-desktop` 불인정 · 부모 없음(=예외) ·
    깊이 상한 · 사이클 · 빈 표/표 조회 실패.
  - Windows 분기 — `_platform_name="nt"` 주입으로 비-Windows 회귀에서 검증하고, 프로세스 표는
    `console_encoding._toolhelp_processes` 하나만 쓴다(두 번째 열거 0).
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

from _session_chain import live_harness_session, real_process_table_supported

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
RELAY = TOOLS / "pm_relay.py"

_real_chain = pytest.mark.skipif(
    not real_process_table_supported(),
    reason="실 프로세스 사슬 단언은 POSIX `ps` 표가 있는 환경에서만 돈다",
)

# 손자 프로세스가 자기 세션 pid 를 한 줄 JSON 으로 낸다 — 사슬 밖에서 계산하지 않는다.
_PROBE_SOURCE = '''\
import importlib.util, json, os, sys

spec = importlib.util.spec_from_file_location("pm_relay_session_probe", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
print(json.dumps({"session_pid": module.session_pid(),
                  "pid": os.getpid(), "ppid": os.getppid()}))
'''


@pytest.fixture(scope="module")
def relay():
    spec = importlib.util.spec_from_file_location("pm_relay_session_pid", RELAY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── ① 실 프로세스 사슬 ───────────────────────────────────────────────────────


@_real_chain
def test_session_pid_resolves_the_dummy_harness_ancestor(tmp_path):
    """하네스 이름으로 뜬 조상이 있으면 그 pid — 직계 부모(중간 홉)도 자기 pid 도 아니다."""
    probe = tmp_path / "probe.py"
    probe.write_text(_PROBE_SOURCE, encoding="utf-8")

    with live_harness_session(tmp_path, [sys.executable, str(probe), str(RELAY)]) as session:
        assert session.rc == 0, session.stderr
        observed = json.loads(session.stdout)

    assert observed["session_pid"] == session.pid
    assert observed["session_pid"] != observed["pid"], "자기 pid 를 돌려주면 명령과 함께 죽는다"
    assert observed["session_pid"] != observed["ppid"], (
        "직계 부모(중간 홉)를 돌려주면 하네스 조상 판정이 실효 없다"
    )


@_real_chain
@pytest.mark.parametrize("harness", ("claude", "codex", "opencode"))
def test_session_pid_accepts_every_harness_name(tmp_path, harness):
    """`HARNESS_CHOICES` 세 이름 모두 세션으로 인정한다(값 테이블 단일 소유)."""
    probe = tmp_path / "probe.py"
    probe.write_text(_PROBE_SOURCE, encoding="utf-8")

    with live_harness_session(
        tmp_path, [sys.executable, str(probe), str(RELAY)], harness=harness,
    ) as session:
        assert session.rc == 0, session.stderr
        assert json.loads(session.stdout)["session_pid"] == session.pid


@_real_chain
def test_recorded_session_pid_dies_with_the_session(tmp_path, relay):
    """세션이 사는 동안 `pid_is_alive` True · 끝나면 False — `alive` 계약의 양 끝."""
    probe = tmp_path / "probe.py"
    probe.write_text(_PROBE_SOURCE, encoding="utf-8")

    with live_harness_session(tmp_path, [sys.executable, str(probe), str(RELAY)]) as session:
        recorded = json.loads(session.stdout)["session_pid"]
        assert relay.pid_is_alive(recorded) is True

    assert relay.pid_is_alive(recorded) is False


@_real_chain
def test_posix_process_table_carries_this_process_and_its_parent(relay):
    """`ps -eo pid=,ppid=,comm=` 표에 이 프로세스 행이 실리고 ppid 가 실제 부모와 같다."""
    table = relay._posix_process_table()

    assert table, "빈 표는 '하네스 없음'이 아니라 해소 실패다"
    rows = {pid: (parent_pid, name) for pid, parent_pid, name in table}
    assert os.getpid() in rows
    parent_pid, name = rows[os.getpid()]
    assert parent_pid == os.getppid()
    assert name and "/" not in name, "이름은 basename 이어야 한다(macOS ps 는 전체 경로를 싣는다)"


# ── ② 표 주입 음성 ───────────────────────────────────────────────────────────


def test_no_harness_ancestor_falls_back_to_the_direct_parent(relay):
    """하네스 조상이 없으면 사람이 직접 부른 것 — 그 셸(직계 부모)이 세션이다."""
    table = [
        (os.getpid(), 4242, "python3"),
        (4242, 4241, "bash"),
        (4241, 1, "sshd"),
    ]

    assert relay.session_pid(_platform_name="posix", _process_table=table) == os.getppid()


def test_claude_desktop_ancestor_is_not_a_harness(relay):
    """접두 일치면 상주 `claude-desktop` 을 세션으로 오인한다 — 완전 일치만 인정한다."""
    table = [
        (os.getpid(), 4242, "python3"),
        (4242, 4241, "claude-desktop"),
        (4241, 1, "systemd"),
    ]

    assert relay.session_pid(_platform_name="posix", _process_table=table) == os.getppid()


def test_harness_ancestor_beats_the_direct_parent(relay):
    """중간에 셸이 끼어도 하네스 조상을 고른다(주입 표로 고정 — 주변 사슬 무관)."""
    table = [
        (os.getpid(), 4242, "bash"),
        (4242, 4241, "claude"),
        (4241, 1, "bash"),
    ]

    assert relay.session_pid(_platform_name="posix", _process_table=table) == 4242


def test_unresolvable_parent_fails_loud(relay, monkeypatch):
    """부모가 init(1)에 입양됐으면 귀속할 세션이 없다 — 1 을 적으면 영원히 작업 중으로 보인다."""
    monkeypatch.setattr(os, "getppid", lambda: 1)
    table = [(os.getpid(), 1, "python3"), (1, 0, "systemd")]

    with pytest.raises(relay.SessionPidUnresolved, match="세션 pid 를 해소하지 못했다"):
        relay.session_pid(_platform_name="posix", _process_table=table)


def test_empty_process_table_is_a_failure_not_an_absent_harness(relay):
    """빈 표는 '하네스 없음'이 아니라 '표를 못 읽음'이다 — 조용히 부모로 떨어지지 않는다."""
    with pytest.raises(relay.SessionPidUnresolved, match="프로세스 표를 읽지 못함"):
        relay.session_pid(_platform_name="posix", _process_table=[])


def test_process_table_read_failure_fails_loud(relay, monkeypatch):
    """`ps` 실행 실패(rc≠0·부재)도 같은 해소 실패다 — 빈 목록으로 축약하지 않는다."""
    def refuse(argv, **kwargs):
        raise OSError("ps 없음")

    monkeypatch.setattr(relay.subprocess, "run", refuse)
    with pytest.raises(relay.SessionPidUnresolved, match="프로세스 표를 읽지 못함"):
        relay._posix_process_table()


def test_ancestor_walk_stops_at_the_depth_limit(relay):
    """깊이 상한(=`console_encoding._MAX_ANCESTOR_DEPTH`) 밖의 하네스는 세지 않는다."""
    depth = relay._MAX_SESSION_ANCESTOR_DEPTH
    chain = [(os.getpid(), 5000, "python3")]
    chain += [(5000 + step, 5001 + step, "bash") for step in range(depth + 2)]
    chain.append((5000 + depth + 2, 1, "claude"))

    assert relay.session_pid(_platform_name="posix", _process_table=chain) == os.getppid()


def test_ancestor_walk_breaks_cycles(relay):
    """부모 사슬이 순환하는 표를 줘도 무한 순회하지 않는다."""
    table = [
        (os.getpid(), 6001, "python3"),
        (6001, 6002, "bash"),
        (6002, 6001, "bash"),
    ]

    assert relay.session_pid(_platform_name="posix", _process_table=table) == os.getppid()


# ── ③ Windows 분기 ───────────────────────────────────────────────────────────


def test_windows_branch_walks_the_same_chain(relay):
    """Windows 도 같은 순회·같은 판정이다(비-Windows 회귀에서 `_platform_name` 주입으로 검증)."""
    table = [
        (os.getpid(), 7001, "python.exe"),
        (7001, 7002, "claude"),
        (7002, 1, "powershell.exe"),
    ]

    assert relay.session_pid(_platform_name="nt", _process_table=table) == 7001


def test_windows_process_table_reuses_the_toolhelp_enumeration(relay, monkeypatch):
    """Windows 표는 `console_encoding._toolhelp_processes` 하나에서 온다(두 번째 열거 0)."""
    console_encoding = relay._load_module_from_path(
        TOOLS / "console_encoding.py", "console_encoding.py",
        verifier=relay._verify_engine_rev, cache=True,
    )
    monkeypatch.setattr(
        console_encoding, "_toolhelp_processes",
        lambda: [(11, 12, r"C:\\Users\\me\\AppData\\Local\\bin\\Claude.EXE"),
                 (12, 1, r"C:\\Windows\\System32\\cmd.exe")],
    )

    assert relay._windows_process_table() == [(11, 12, "claude.exe"), (12, 1, "cmd.exe")]


def test_relay_does_not_enumerate_processes_itself(relay):
    """열거 구현은 플랫폼당 하나다 — 이 모듈이 Toolhelp32 를 다시 열면 규칙이 둘로 갈린다."""
    source = RELAY.read_text(encoding="utf-8")

    for token in ("CreateToolhelp32Snapshot", "PROCESSENTRY32", "psutil"):
        assert token not in source, f"pm_relay 가 프로세스를 직접 열거한다: {token}"
