"""`pm-config delegate models --harness <h> --json` — 모델 목록 읽기 전용 조회 (T-0916).

설정 화면과 사람이 "이 하네스에 무엇을 적나" 를 받는 자리다. 목록의 원천은 `pm_relay.MODEL_SOURCE`
한 표이고, 이 명령은 그 표의 `kind` 로만 갈린다(하네스 이름 조건 분기 0). 이 파일이 못박는 계약:

  - 응답은 `{"harness", "kind", "models"}` 한 줄이다. `free_text` 는 싣지 않는다 — 그 사실은 키
    레지스트리의 `choices.free_text` 가 소유하고, 두 자리에 두면 갈린다.
  - rc 는 0(성공) · 2(`--harness`/`--json` 미지정·미지원) · 3(조회 실패) 이다. **빈 목록 성공은
    없다** — 0개면 실패이고 사유가 stderr 로 나온다.
  - 조회는 읽기 전용이다. 실행 전후로 트리가 한 바이트도 바뀌지 않는다.

opencode(`kind="live"`)는 **실 subprocess 층**으로 친다: 조회 seam(`pm_import._real_models_runner`)
을 가짜로 갈아끼우지 않고, `opencode` 가 없는 격리 PATH 로 자식을 돌려 `shutil.which` 분기가 실제로
도는 것을 본다(라이브 CLI 의존 0 — 있는 환경 전용 단언은 두지 않는다). codex(`kind="config"`)는
`CODEX_HOME`/홈 아래 config 를 tmp 로 돌려 hermetic 하다.
"""
from __future__ import annotations

import importlib.util
import inspect
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from _manager_home import tree_snapshot
from _textio import utf8_child_env

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
PM_CONFIG_PY = TOOLS / "pm_config.py"

MODELS_ARGV = ("delegate", "models")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pc():
    return _load("pm_config_delegate_models", PM_CONFIG_PY)


@pytest.fixture(scope="module")
def relay():
    return _load("relay_for_delegate_models", TOOLS / "pm_relay.py")


def _run(*argv: str, env_extra: dict | None = None, drop: tuple[str, ...] = (),
         cwd: Path | None = None) -> subprocess.CompletedProcess:
    """실 자식 프로세스로 명령을 돌린다 — rc·stdout·stderr 가 전부 실측값이다."""
    env = utf8_child_env()
    for name in drop:
        env.pop(name, None)
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, str(PM_CONFIG_PY), *argv],
        capture_output=True, text=True, encoding="utf-8", env=env,
        cwd=str(cwd) if cwd is not None else None,
    )


def _payload(completed: subprocess.CompletedProcess) -> dict:
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def _codex_home(tmp_path: Path, body: str | None) -> dict:
    """tmp `CODEX_HOME` — 본문을 주면 `config.toml` 을 깐다(없으면 빈 홈)."""
    home = tmp_path / "codex-home"
    home.mkdir(exist_ok=True)
    if body is not None:
        (home / "config.toml").write_text(body, encoding="utf-8")
    return {"CODEX_HOME": str(home)}


def _empty_path(tmp_path: Path) -> dict:
    """실행 파일이 하나도 없는 PATH — `shutil.which` 가 실제로 부재를 만난다."""
    empty = tmp_path / "empty-bin"
    empty.mkdir(exist_ok=True)
    return {"PATH": str(empty)}


# ── ① 원천 표 — 지원 집합과 응답 모양 ───────────────────────────────────────


def test_every_supported_harness_is_declared_in_the_model_source_table(relay):
    """지원 하네스와 모델 원천 표가 같은 집합이다 — 원천 없는 하네스를 남기지 않는다."""
    assert tuple(relay.MODEL_SOURCE) == relay.HARNESS_CHOICES


def test_an_alias_harness_answers_from_the_declared_table(relay):
    """`kind="alias"` 는 CLI 가 계약한 별칭 목록 그대로다(하드코딩 사본 0)."""
    document = _payload(_run(*MODELS_ARGV, "--harness", "claude", "--json"))

    assert document == {"harness": "claude", "kind": "alias",
                        "models": list(relay.MODEL_SOURCE["claude"].values)}


def test_the_response_never_carries_the_free_text_axis(tmp_path):
    """응답 키는 셋뿐이다 — `free_text` 는 레지스트리 `choices` 가 소유한다(두 자리 금지)."""
    alias = _payload(_run(*MODELS_ARGV, "--harness", "claude", "--json"))
    config = _payload(_run(*MODELS_ARGV, "--harness", "codex", "--json",
                           env_extra=_codex_home(tmp_path, 'model = "gpt-tmp-1"\n')))

    for document in (alias, config):
        assert set(document) == {"harness", "kind", "models"}, document


# ── ② config 원천(codex) — 경로는 표가 정한다 ──────────────────────────────


def test_a_config_harness_reads_the_model_declared_under_the_env_home(tmp_path):
    """`CODEX_HOME` 이 있으면 그 아래 config 의 최상위 `model` 이 현재 값이다."""
    document = _payload(_run(*MODELS_ARGV, "--harness", "codex", "--json",
                             env_extra=_codex_home(tmp_path, 'model = "gpt-tmp-1"\n')))

    assert document == {"harness": "codex", "kind": "config", "models": ["gpt-tmp-1"]}


def test_a_config_harness_falls_back_to_the_home_path_without_the_env(tmp_path, relay):
    """env 가 없으면 홈 아래 선언 경로를 읽는다 — 두 자리 다 표에서 온다."""
    home = tmp_path / "home"
    config = home / relay.MODEL_SOURCE["codex"].config_home
    config.parent.mkdir(parents=True)
    config.write_text('model = "gpt-home-1"\n', encoding="utf-8")

    document = _payload(_run(
        *MODELS_ARGV, "--harness", "codex", "--json", drop=("CODEX_HOME",),
        env_extra={"HOME": str(home), "USERPROFILE": str(home)}))

    assert document == {"harness": "codex", "kind": "config", "models": ["gpt-home-1"]}


@pytest.mark.parametrize("body", [
    pytest.param(None, id="config-absent"),
    pytest.param("other = 1\n", id="key-absent"),
    pytest.param("model = 3\n", id="not-a-string"),
    pytest.param('model = "   "\n', id="blank"),
])
def test_a_config_that_declares_no_model_is_a_failure_not_an_empty_list(tmp_path, body):
    """읽지 못한 자리에 값을 지어내지 않는다 — 부재·키 없음·문자열 아님은 전부 rc 3 이다."""
    completed = _run(*MODELS_ARGV, "--harness", "codex", "--json",
                     env_extra=_codex_home(tmp_path, body))

    assert completed.returncode == 3, completed.stdout
    assert completed.stdout == ""
    assert "config.toml" in completed.stderr and "codex" in completed.stderr


# ── ③ live 원천(opencode) — 실 subprocess 층 ───────────────────────────────


def test_a_live_harness_without_its_binary_fails_loudly(tmp_path):
    """조회 수단이 없는 환경에서 rc 0 이나 빈 목록 성공을 내지 않는다(실 PATH 분기)."""
    completed = _run(*MODELS_ARGV, "--harness", "opencode", "--json",
                     env_extra=_empty_path(tmp_path))

    assert completed.returncode == 3, completed.stdout
    assert completed.stdout == ""
    assert "opencode" in completed.stderr
    assert "PATH 부재" in completed.stderr, "조회 seam 의 사유가 사라졌다"


def test_the_live_lookup_reuses_the_import_engine_query_seam(pc):
    """두 번째 opencode 조회 코드를 만들지 않는다 — 기존 seam 을 그대로 부른다."""
    source = inspect.getsource(pc._delegate_models)

    assert "_real_models_runner" in source
    assert "subprocess" not in source, "조회를 여기서 다시 구현했다"


def test_the_lookup_declares_no_new_timeout_knob():
    """timeout 축은 기존 `PM_OPENCODE_MODELS_TIMEOUT` 하나다 — 새 env 를 만들지 않는다."""
    assert "PM_OPENCODE_MODELS_TIMEOUT" not in PM_CONFIG_PY.read_text(encoding="utf-8")


def test_an_empty_live_list_is_never_a_success(pc, relay):
    """조회가 0개를 주면 성공이 아니다 — 빈 목록을 성공으로 위장하지 않는다."""
    args = SimpleNamespace(harness="opencode", json=True)

    rc = pc.cmd_delegate_models(args, models_runner=lambda: (True, []))

    assert rc == 3


def test_an_unknown_source_kind_is_fail_loud(pc):
    """표에 없는 `kind` 는 선언과 판정이 갈린 것이다 — 조용히 빈 목록으로 내려가지 않는다."""
    with pytest.raises(ValueError):
        pc._delegate_models(SimpleNamespace(
            kind="nope", values=(), config_env=None, config_home=None,
            config_key=None, value_format="", how_to_check=""))


# ── ④ 받는 모양 — rc 2 ─────────────────────────────────────────────────────


def test_an_unsupported_harness_is_rejected_with_the_usage_return_code(tmp_path):
    """표 밖의 하네스는 rc 2 다 — 지원 목록을 그 자리에서 말한다."""
    completed = _run(*MODELS_ARGV, "--harness", "nope", "--json")

    assert completed.returncode == 2, completed.stdout
    assert completed.stdout == ""
    assert "claude, codex, opencode" in completed.stderr


@pytest.mark.parametrize("argv", [
    pytest.param(("--json",), id="harness-missing"),
    pytest.param(("--harness", "claude"), id="json-missing"),
    pytest.param(("--harness", "claude", "--json", "--nope"), id="unknown-flag"),
])
def test_the_command_takes_only_the_declared_shape(argv):
    """받는 모양은 `--harness <h> --json` 하나다(그 밖은 사용법 rc 2)."""
    completed = _run(*MODELS_ARGV, *argv)

    assert completed.returncode == 2, completed.stdout
    assert completed.stdout == ""


# ── ⑤ 읽기 전용 — 조회는 아무것도 쓰지 않는다 ──────────────────────────────


def test_a_lookup_writes_nothing(tmp_path):
    """조회 전후로 프로젝트 트리와 config 홈이 한 바이트도 바뀌지 않는다."""
    project = tmp_path / "project"
    (project / ".project_manager").mkdir(parents=True)
    (project / ".project_manager" / "local.conf").write_text(
        "runtime.py=python3\n", encoding="utf-8")
    env_extra = _codex_home(tmp_path, 'model = "gpt-tmp-1"\n')
    env_extra.update(_empty_path(tmp_path))
    before = (tree_snapshot(project), tree_snapshot(tmp_path / "codex-home"))

    for harness in ("claude", "codex", "opencode"):
        _run(*MODELS_ARGV, "--harness", harness, "--json",
             env_extra=env_extra, cwd=project)

    assert (tree_snapshot(project), tree_snapshot(tmp_path / "codex-home")) == before
