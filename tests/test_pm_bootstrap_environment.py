"""T-0679 finite OS/config/card-mode environment matrix."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
BOOTSTRAP = REPO / ".project_manager" / "tools" / "pm_bootstrap.py"


def _load():
    spec = importlib.util.spec_from_file_location("pm_bootstrap_environment", BOOTSTRAP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


OS_CASES = {
    "windows": ("Windows", "posix", "windows", ("py", "-3"), "separate-lines"),
    "linux": ("Linux", "posix", "linux", ("python3",), "and-if"),
    "darwin": ("Darwin", "posix", "macos", ("python3",), "and-if"),
    "unknown": ("FreeBSD", "posix", "other", ("python3",), "separate-lines"),
}
CONFIG_CASES = ("valid", "absent", "empty", "read-error")
MODES = ("slot", "solo", "task", "readonly")


def _configure(tmp_path: Path, monkeypatch, state: str, mod=None) -> None:
    if state == "absent":
        return
    conf_dir = tmp_path / ".project_manager"
    conf_dir.mkdir()
    conf = conf_dir / "local.conf"
    if state == "valid":
        conf.write_text("py=ignored\npy=custom-python\n", encoding="utf-8")
    elif state == "empty":
        conf.write_text("py=ignored\npy=\n", encoding="utf-8")
    elif state == "read-error":
        conf.write_text("py=ignored\n", encoding="utf-8")
        # conf 판독은 공유 읽기 seam 을 지난다([[T-0729]]) — 주입도 그 자리에 건다.
        # `Path.read_text` 에 걸면 엔진이 그 호출을 더는 하지 않아 이 케이스가 공허해진다.
        seam = mod._load_file_lock()
        original = seam.read_text_shared

        def _read_text(path, *args, **kwargs):
            if Path(path) == conf:
                raise OSError("unreadable local.conf")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(seam, "read_text_shared", _read_text)
    else:  # pragma: no cover - parametrization is closed above.
        raise AssertionError(state)


def _card(mod, environment, mode: str) -> str:
    inst = mod.PmBootstrap.__new__(mod.PmBootstrap)
    inst._command_environment = environment
    identity = None
    if mode == "slot":
        identity = {
            "repo": "project_manager", "session": "project_manager_1",
            "slot": "work/project_manager_1",
        }
    elif mode == "task":
        inst._task_name = "env-task"
    elif mode == "readonly":
        identity = {"role": "readonly", "slot": "work/project_manager_2"}
    elif mode != "solo":  # pragma: no cover - parametrization is closed above.
        raise AssertionError(mode)
    return inst._build_command_card_markdown(identity)


def _command_suffixes(card: str) -> list[str]:
    marker = ".project_manager/tools/"
    return [line.split(marker, 1)[1] for line in card.splitlines() if marker in line]


@pytest.mark.parametrize("os_case", OS_CASES, ids=OS_CASES)
@pytest.mark.parametrize("config_state", CONFIG_CASES)
@pytest.mark.parametrize("mode", MODES)
def test_environment_matrix_64(
    tmp_path, monkeypatch, os_case: str, config_state: str, mode: str
):
    """4 OS × 4 config states × 4 card modes = the complete finite surface."""
    mod = _load()
    _configure(tmp_path, monkeypatch, config_state, mod)
    system, os_name, label, default_argv, policy = OS_CASES[os_case]
    environment = mod._detect_command_environment(
        tmp_path, system=system, os_name=os_name
    )
    expected_argv = ("custom-python",) if config_state == "valid" else default_argv

    assert environment.os_label == label
    assert environment.python_argv == expected_argv
    assert environment.python_argv
    assert environment.python_source == (
        "local-conf" if config_state == "valid" else "os-default"
    )
    assert environment.chain_policy == policy

    card = _card(mod, environment, mode)
    prefix = f"{' '.join(expected_argv)} .project_manager/tools/"
    # Execution rows begin with the resolved launcher after Markdown indentation
    # is stripped.  Prose pointers may mention the same path mid-line and are not
    # shell rows.
    command_lines = [
        line.strip() for line in card.splitlines() if line.strip().startswith(prefix)
    ]
    assert command_lines
    if label == "windows":
        assert all("&&" not in line for line in command_lines)

    baseline = mod._CommandEnvironment(label, ("baseline-python",), "local-conf", policy)
    assert _command_suffixes(card) == _command_suffixes(_card(mod, baseline, mode))

    inst = mod.PmBootstrap(run_git_fn=lambda _args: (0, ""))
    inst._command_environment = environment
    board = {
        "counts": {"done": 0, "open": 0, "claimed": 0, "blocked": 0},
        "open_tickets": [], "lint": "clean",
    }
    git = {"branch": "main", "commits": [], "working_tree": "clean"}
    markdown = inst._build_markdown(board, None, git, None, "ts")
    line = mod._render_environment_line(environment)
    assert markdown.count("현재 환경:") == 1
    assert markdown.count(line) == 1
    data = inst._build_json(board, None, git, None, "ts")
    assert data["environment"] == {
        "os": label,
        "python": " ".join(expected_argv),
        "python_argv": list(expected_argv),
        "python_source": environment.python_source,
        "chain_policy": policy,
        "policy_basis": "os-safe-rendering-not-shell-detection",
    }


def test_final_nonempty_py_wins_and_final_empty_preserves_fallback(tmp_path):
    mod = _load()
    conf_dir = tmp_path / ".project_manager"
    conf_dir.mkdir()
    conf = conf_dir / "local.conf"
    conf.write_text("py=first\npy=second\n", encoding="utf-8")
    assert mod._resolve_python_argv(tmp_path, "windows") == (("second",), "local-conf")
    conf.write_text("py=first\npy=\n", encoding="utf-8")
    assert mod._resolve_python_argv(tmp_path, "windows") == (("py", "-3"), "os-default")


def test_configured_launcher_is_one_argv_token_not_shell_parsed(tmp_path):
    mod = _load()
    conf_dir = tmp_path / ".project_manager"
    conf_dir.mkdir()
    (conf_dir / "local.conf").write_text("py=launcher --not-a-shell-flag\n", encoding="utf-8")
    argv, source = mod._resolve_python_argv(tmp_path, "linux")
    assert argv == ("launcher --not-a-shell-flag",)
    assert source == "local-conf"


def test_platform_probe_exception_fails_soft_to_other(monkeypatch, tmp_path):
    mod = _load()

    def _boom():
        raise RuntimeError("probe failed")

    monkeypatch.setattr(mod.platform, "system", _boom)
    monkeypatch.setattr(mod.os, "name", "posix")
    environment = mod._detect_command_environment(tmp_path)
    assert environment == mod._CommandEnvironment(
        "other", ("python3",), "os-default", "separate-lines"
    )


@pytest.mark.parametrize(
    ("system", "expected"),
    [
        ("Windows", ("windows", ("py", "-3"), "separate-lines")),
        ("Linux", ("linux", ("python3",), "and-if")),
        ("Darwin", ("macos", ("python3",), "and-if")),
    ],
)
def test_real_platform_seam_is_mockable(monkeypatch, tmp_path, system, expected):
    """The production no-argument probe consumes stdlib platform, without subprocess."""
    mod = _load()
    monkeypatch.setattr(mod.platform, "system", lambda: system)
    monkeypatch.setattr(mod.os, "name", "posix")
    environment = mod._detect_command_environment(tmp_path)
    assert (
        environment.os_label,
        environment.python_argv,
        environment.chain_policy,
    ) == expected
