"""`pm-config delegate models --harness <h> --json` — 모델 목록 읽기 전용 조회 (T-0916).

설정 화면과 사람이 "이 하네스에 무엇을 적나" 를 받는 자리다. 목록의 원천은 `pm_relay.MODEL_SOURCE`
한 표이고 조회는 `pm_relay.model_listing` 하나다(하네스 이름 조건 분기 0). 이 파일이 못박는 계약:

  - 응답은 `{"harness", "kind", "models", "variants"}` 한 줄이다. `variants` 는 모델별 추론 강도이고
    그것을 내는 원천(opencode)만 채운다(나머지 null). `free_text` 는 싣지 않는다 — 그 사실은 키
    레지스트리의 `choices.free_text` 가 소유하고, 두 자리에 두면 갈린다.
  - rc 는 0(성공) · 1(엔진 사본 부재 — 원천 표를 못 읽음) · 2(`--harness`/`--json` 미지정·미지원) ·
    3(조회 실패) 이다. **빈 목록 성공은 없다** — 0개면 실패이고 사유가 stderr 로 나온다.
  - 조회는 읽기 전용이다. 실행 전후로 트리가 한 바이트도 바뀌지 않는다.

live 원천(codex·opencode)은 **실 subprocess 층**으로 친다: 조회 함수를 갈아끼우지 않고, 실행 파일이
없는 격리 PATH 나 실제 출력 형식을 내는 가짜 실행 파일(`_fake_opencode`)을 PATH 앞에 둔 자식을 돌린다
(라이브 CLI 의존 0). 두 하네스가 다른 것은 원천 표의 `command`·`parse` 값뿐이다.
"""
from __future__ import annotations

import importlib.util
import inspect
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from _fake_opencode import OLLAMA_LISTING, install_fake_opencode, path_with
from _manager_home import tree_snapshot
from _textio import utf8_child_env
from _win_skip import posix_bash_supported

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
                        "models": list(relay.MODEL_SOURCE["claude"].values), "variants": None}


def test_the_response_never_carries_the_free_text_axis(pc, relay, capsys):
    """응답 키는 넷뿐이다 — `free_text` 는 레지스트리 `choices` 가 소유한다(두 자리 금지).

    두 kind 를 다 본다: alias 는 실 서브프로세스로, live 는 조회 함수 주입으로(라이브 CLI 를
    테스트 통과 조건으로 삼지 않는다).
    """
    alias = _payload(_run(*MODELS_ARGV, "--harness", "claude", "--json"))

    rc = pc.cmd_delegate_models(
        SimpleNamespace(harness="opencode", json=True),
        listing=lambda source: relay.ModelListing(("provider/model",), {"provider/model": ()}))
    live = json.loads(capsys.readouterr().out.strip())

    assert rc == 0
    assert live["variants"] == {"provider/model": []}
    for document in (alias, live):
        assert set(document) == {"harness", "kind", "models", "variants"}, document


@pytest.mark.skipif(not posix_bash_supported(), reason="가짜 opencode 는 POSIX sh 스크립트다")
def test_live_response_carries_variants_only_for_per_model_sources(tmp_path):
    """opencode 응답은 모델과 모델별 variants 를 같은 한 번의 조회에서 싣고, 별칭 원천은 null 이다."""
    fakebin = install_fake_opencode(tmp_path / "fakebin", OLLAMA_LISTING)

    document = _payload(_run(*MODELS_ARGV, "--harness", "opencode", "--json",
                             env_extra={"PATH": path_with(fakebin)}))
    claude = _payload(_run(*MODELS_ARGV, "--harness", "claude", "--json"))

    assert document == {
        "harness": "opencode", "kind": "live",
        "models": ["ollama/glm-5.2:cloud", "ollama/ornith1.5:35b-a3b"],
        "variants": {"ollama/glm-5.2:cloud": [], "ollama/ornith1.5:35b-a3b": ["low", "medium", "high"]},
    }
    assert claude["variants"] is None
    assert (fakebin / "opencode-calls.txt").read_text(encoding="utf-8").count("\n") == 1


# ── ② live 원천(codex·opencode) — 실 subprocess 층 ─────────────────────────


def test_the_codex_source_is_a_catalog_query_not_a_config_read(relay):
    """codex 는 현재 값 하나가 아니라 카탈로그를 준다 — `codex debug models` 실측(0.02~0.34초).

    옛 선언은 `~/.codex/config.toml` 의 `model` 한 줄을 읽어 **설정된 값 하나**만 냈고, 그래서
    설정 화면 모델 칸에 고를 것이 하나뿐이었다. 열거 수단이 없다는 전제가 틀렸다.
    """
    source = relay.MODEL_SOURCE["codex"]

    assert source.kind == "live"
    assert source.command == ("codex", "debug", "models")
    assert source.parse == "json-slug"


def test_a_live_harness_without_its_binary_fails_loudly(tmp_path):
    """조회 수단이 없는 환경에서 rc 0 이나 빈 목록 성공을 내지 않는다(실 PATH 분기)."""
    completed = _run(*MODELS_ARGV, "--harness", "opencode", "--json",
                     env_extra=_empty_path(tmp_path))

    assert completed.returncode == 3, completed.stdout
    assert completed.stdout == ""
    assert "opencode" in completed.stderr
    assert "PATH 부재" in completed.stderr, "조회 seam 의 사유가 사라졌다"


def test_the_live_lookup_reuses_the_relay_model_listing(pc):
    """두 번째 조회 코드를 만들지 않는다 — pm_config 와 설치 래퍼가 같은 `model_listing` 을 부른다."""
    source = inspect.getsource(pc.cmd_delegate_models)
    wrapper = inspect.getsource(_load("pm_import_for_delegate_models", TOOLS / "pm_import.py")
                                ._real_models_runner)

    assert "model_listing" in source and "model_listing" in wrapper
    assert "subprocess" not in source and "subprocess" not in wrapper, "조회를 여기서 다시 구현했다"


def test_the_lookup_declares_no_new_timeout_knob():
    """timeout 축은 기존 `PM_OPENCODE_MODELS_TIMEOUT` 하나다 — 새 env 를 만들지 않는다."""
    assert "PM_OPENCODE_MODELS_TIMEOUT" not in PM_CONFIG_PY.read_text(encoding="utf-8")


@pytest.mark.skipif(not posix_bash_supported(), reason="가짜 opencode 는 POSIX sh 스크립트다")
def test_an_empty_live_list_is_never_a_success(tmp_path):
    """조회가 0개를 주면 성공이 아니다 — 빈 목록을 성공으로 위장하지 않는다."""
    fakebin = install_fake_opencode(tmp_path / "fakebin", "")

    completed = _run(*MODELS_ARGV, "--harness", "opencode", "--json",
                     env_extra={"PATH": path_with(fakebin)})

    assert completed.returncode == 3, completed.stdout
    assert completed.stdout == ""
    assert "모델 0개" in completed.stderr


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


# ── ④\' 원천 표 자체를 못 읽는 사본 — rc 1 ─────────────────────────────────


def test_a_copy_without_the_relay_engine_fails_with_its_own_return_code(tmp_path):
    """`pm_relay.py` 가 없는 사본은 rc 1 이다 — 조회 실패(3)와 다른 사실이라 값도 다르다.

    표를 물어볼 자리 자체가 없는 상태다(부분 사본·중단된 갱신). 이 자리를 3 으로 접으면
    "표대로 물었는데 답을 못 얻었다" 와 구별되지 않는다.
    """
    tools = tmp_path / "copy" / ".project_manager" / "tools"
    shutil.copytree(TOOLS, tools)
    (tools / "pm_relay.py").unlink()

    completed = subprocess.run(
        [sys.executable, str(tools / "pm_config.py"), *MODELS_ARGV,
         "--harness", "claude", "--json"],
        capture_output=True, text=True, encoding="utf-8", env=utf8_child_env())

    assert completed.returncode == 1, completed.stdout
    assert completed.stdout == ""
    assert "pm_relay.py" in completed.stderr


# ── ⑤ 읽기 전용 — 조회는 아무것도 쓰지 않는다 ──────────────────────────────


def test_a_lookup_writes_nothing(tmp_path):
    """조회 전후로 프로젝트 트리와 config 홈이 한 바이트도 바뀌지 않는다."""
    project = tmp_path / "project"
    (project / ".project_manager").mkdir(parents=True)
    (project / ".project_manager" / "local.conf").write_text(
        "runtime.py=python3\n", encoding="utf-8")
    env_extra = _empty_path(tmp_path)
    before = tree_snapshot(project)

    for harness in ("claude", "codex", "opencode"):
        _run(*MODELS_ARGV, "--harness", harness, "--json",
             env_extra=env_extra, cwd=project)

    assert tree_snapshot(project) == before
