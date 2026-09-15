"""묶음 종결의 슬롯 반납 단계 — 반납 대상 판정에 예외가 없다.

옛 엔진은 PM 홈 자신을 가리키던 행(`slot="."`)을 "정체성" 이라며 반납 무대상으로 접었다
(T-0919 로 그 등재 경로와 예외 분기를 함께 삭제). 지금 무대상은 셋뿐이다 — 슬롯 미해소·장부에
그 슬롯 없음·이미 반납됨. 그 밖의 leased 슬롯은 값과 무관하게 반납된다.
"""
from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest

from _slot_seed import SLOT, seed_slot

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

_CLUSTER = "C-wave"


def _load_ticket_finish(home: Path, monkeypatch):
    """tmp 홈에 엔진 사본을 깔고 그 홈을 REPO 로 보는 ticket_finish 모듈을 만든다."""
    shutil.copytree(TOOLS, home / ".project_manager" / "tools")
    (home / ".project_manager" / "wiki" / "log").mkdir(parents=True)
    (home / ".project_manager" / "wiki" / "log" / "current.md").write_text(
        "# log\n", encoding="utf-8")
    path = home / ".project_manager" / "tools" / "ticket_finish.py"
    spec = importlib.util.spec_from_file_location(f"_tf_{home.name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "REPO", home)
    monkeypatch.setattr(module, "TOOLS_DIR", home / ".project_manager" / "tools")
    monkeypatch.setattr(module, "BOARD_PY", home / ".project_manager" / "tools" / "board.py")
    return module


def _closer(module, home: Path, *, slot: str | None, released: list[str]):
    finisher = module.TicketFinisher(
        board_py=home / ".project_manager" / "tools" / "board.py",
        log_file=home / ".project_manager" / "wiki" / "log" / "current.md",
        run_pytest_fn=lambda: (0, "1 passed in 0.01s"),
    )

    def _release(value: str) -> tuple[bool, str]:
        released.append(value)
        return True, f"슬롯 {value} 반납됨"

    return module.ClusterCloser(
        _CLUSTER, finisher=finisher, board_py=home / ".project_manager" / "tools" / "board.py",
        slot=slot, release_fn=_release,
    )


@pytest.fixture
def home(tmp_path):
    return tmp_path / "home"


def test_leased_work_slot_is_released(home, monkeypatch, capsys):
    """leased 작업 슬롯은 반납된다 — 반납 seam 호출과 출력이 그 슬롯 값을 싣는다."""
    module = _load_ticket_finish(home, monkeypatch)
    seed_slot(home, slot=SLOT)
    released: list[str] = []

    block = _closer(module, home, slot=SLOT, released=released)._step_release()

    assert block is None
    assert released == [SLOT]
    assert f"슬롯 {SLOT} 반납됨" in capsys.readouterr().out


def test_repo_root_slot_value_gets_no_exemption(home, monkeypatch, capsys):
    """홈 자신을 가리키는 옛 값(`.`)도 예외가 아니다 — 그 판별이 되살아나면 red."""
    module = _load_ticket_finish(home, monkeypatch)
    seed_slot(home, slot=".")
    released: list[str] = []

    block = _closer(module, home, slot=".", released=released)._step_release()

    assert block is None
    assert released == ["."], "반납 무대상으로 접혔다 — 홈 예외가 되살아났다"
    assert "PM 홈 자신" not in capsys.readouterr().out


def test_unresolved_and_unknown_and_already_released_stay_no_target(home, monkeypatch, capsys):
    """무대상 셋 — 슬롯 미해소 · 장부에 없음 · 이미 반납(idle)."""
    module = _load_ticket_finish(home, monkeypatch)
    seed_slot(home, slot="work/proj_2")
    released: list[str] = []

    assert _closer(module, home, slot=None, released=released)._step_release() is None
    assert _closer(module, home, slot=SLOT, released=released)._step_release() is None

    ledger = seed_slot(home, slot="work/proj_2")
    ledger.write_text(
        ledger.read_text(encoding="utf-8").replace('"leased"', '"idle"'), encoding="utf-8")
    assert _closer(module, home, slot="work/proj_2",
                   released=released)._step_release() is None
    assert released == []
    out = capsys.readouterr().out
    assert "슬롯 미해소 — 무대상" in out
    assert f"슬롯 {SLOT} 리스 없음 — 무대상" in out
    assert "이미 반납됨(idle)" in out


class _Released:
    def __init__(self, repo: str):
        self.repo = repo


class _Held:
    def __init__(self, slot: str):
        self.slot = slot


@pytest.mark.parametrize("held, expected", [
    ([], "가진 슬롯: 없음. 이 task 로 코드 작업을 이어가려면 `pm-config alloc A --task main` 로 다시 잡는다"),
    (["work/A_2"], "가진 슬롯: work/A_2"),
])
def test_task_close_release_says_which_slots_the_task_still_holds(home, monkeypatch, held, expected):
    """종결이 슬롯을 반납하면 그 task 가 아직 가진 슬롯을 말한다 — 0개면 다시 잡는 명령까지."""
    module = _load_ticket_finish(home, monkeypatch)
    calls = []

    class _Pool:
        def release(self, slot, *, owner_task):
            calls.append((slot, owner_task))
            return _Released("A")

        def slots_for_task(self, name):
            return [_Held(value) for value in held]

    monkeypatch.setattr(module, "_load_tool_module", lambda path: _Pool())
    finisher = module.TicketFinisher(
        board_py=home / ".project_manager" / "tools" / "board.py",
        log_file=home / ".project_manager" / "wiki" / "log" / "current.md",
        run_pytest_fn=lambda: (0, "1 passed in 0.01s"),
    )
    closer = module.ClusterCloser(
        _CLUSTER, finisher=finisher, board_py=home / ".project_manager" / "tools" / "board.py",
        slot=SLOT, task="main",
    )

    ok, message = closer._default_release(SLOT)

    assert ok and calls == [(SLOT, "main")]
    assert message.startswith(f"슬롯 {SLOT} 반납됨 — task main ")
    assert expected in message
