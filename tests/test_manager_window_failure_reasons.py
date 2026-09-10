"""창 열기 실패 사유 셋 — 표준 출력으로 나오는 사람 말 안내 (T-0922).

사유는 셋으로 유한하다: 화면을 붙일 곳이 없음(rc 1) · 화면 산출물 없음(rc 1) · 렌더러 없음
(rc 3). 라이브러리 경고만 남기고 조용히 끝나던 것은 첫째뿐이었다 — 뒤 둘은 이미 문장이 있었고
자리(표준 오류)만 틀렸다. `webview.start()` 는 화면을 붙일 곳이 없으면 예외 없이 프로세스를
끝내므로(실측), 이 사유는 띄우기 전 점검(`deps.missing_renderer_message`)에서만 잡을 수 있다.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _run_manager(*argv: str, drop_env: tuple[str, ...] = (),
                 extra_path: Path | None = None) -> subprocess.CompletedProcess:
    search_path = str(REPO) if extra_path is None else os.pathsep.join(
        [str(extra_path), str(REPO)])
    env = {**os.environ, "PYTHONPATH": search_path}
    for key in drop_env:
        env.pop(key, None)
    return subprocess.run(
        [sys.executable, "-m", "manager", *argv],
        cwd=str(REPO), capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, timeout=120)


@pytest.mark.skipif(sys.platform != "linux", reason="붙일 화면 판정은 Linux 분기(Gtk.init_check)")
def test_no_display_to_attach_to_prints_a_stdout_message_and_stops(tmp_path):
    """실 서브프로세스에서 DISPLAY·WAYLAND_DISPLAY 를 지운다 — 지금은 stdout 이 0 byte 다."""
    completed = _run_manager(
        "--smoke", "--registry", str(tmp_path / "projects.json"),
        drop_env=("DISPLAY", "WAYLAND_DISPLAY"))

    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert completed.stdout.strip() != "", "붙일 곳이 없을 때 앱이 낸 문장이 0줄이다"
    lines = completed.stdout.strip().splitlines()
    assert len(lines) >= 2, lines  # 사유 1줄 + 다음에 할 일 1줄 이상
    assert "xvfb-run" in completed.stdout or "X 서버" in completed.stdout
    assert not (tmp_path / "projects.json").exists(), "멈춘 실행이 등록부를 만들었다"


def test_missing_screen_bundle_prints_to_stdout_and_stops_with_rc_1(monkeypatch, capsys):
    from manager import window

    monkeypatch.setattr(window, "dist_index", lambda: Path("/no/such/index.html"))

    assert window.open_window() == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    lines = captured.out.strip().splitlines()
    assert len(lines) == 2, lines
    assert "npm run build" in captured.out


@pytest.mark.skipif(sys.platform != "linux", reason="GTK 웹킷 안내는 Linux 분기")
def test_missing_renderer_prints_to_stdout_and_stops_with_rc_3(tmp_path):
    stub = tmp_path / "stub"
    stub.mkdir()
    (stub / "gi.py").write_text('raise ImportError("gi typelib 없음")\n', encoding="utf-8")

    completed = _run_manager("--smoke", "--registry", str(tmp_path / "projects.json"),
                             extra_path=stub)

    assert completed.returncode == 3, completed.stdout + completed.stderr
    assert completed.stdout.strip() != ""
    lines = completed.stdout.strip().splitlines()
    assert "sudo apt install" in lines[1]
    assert "sudo dnf install" in lines[2]


def test_the_three_window_open_reasons_do_not_print_to_stderr():
    """창 열기 실패 안내가 `file=sys.stderr` 로 나가는 자리가 남아 있으면 red."""
    manager_dir = REPO / "manager"
    for name in ("window.py", "__main__.py"):
        source = (manager_dir / name).read_text(encoding="utf-8")
        assert "file=sys.stderr" not in source, f"{name} 에 창 열기 실패를 stderr 로 내는 자리가 있다"

    smoke_source = (manager_dir / "smoke.py").read_text(encoding="utf-8")
    screen_missing_block = smoke_source[:smoke_source.index("manager_window = window.ManagerWindow")]
    assert "file=sys.stderr" not in screen_missing_block

# 정상 경로(창이 실제로 뜨는 `--smoke`)의 출력 불변은 `tests/test_manager_deps.py::
# test_smoke_opens_the_window_and_answers` 가 이미 잰다 — 같은 서버를 잇달아 띄우면 포트 경합으로
# 흔들리므로(환경 문제 · 이 티켓 범위 밖) 여기서 다시 띄우지 않는다.
