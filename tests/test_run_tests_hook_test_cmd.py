r"""run_tests_hook.sh 러너 소유권 — `local.conf test_cmd` 해소 + 부재 시 엔진 스위트 폴백 (T-0579).

이 훅은 `pytest tests/` 를 하드코딩했다. 테스트 루트가 `tests/` 가 아닌 채택자(실측:
`test_cmd=<venv>/python -m pytest .project_manager/checks/tests -q`)에선 수집 0 이거나 엉뚱한 스위트가
돌아, 편집마다 뜨는 systemMessage 가 회귀 신호가 아니게 된다. 러너 명령의 소유권을 채택자
(`.project_manager/local.conf` 의 `test_cmd` — 엔진이 이미 소비하는 seam)로 단일화한 뒤 그 계약을 잠근다.

계약:
  · `local.conf` 에 `test_cmd` 가 있으면 **그 명령을 그대로**(플래그 미추가) repo 루트 cwd 에서 실행
  · 미지정(파일 부재·키 부재·빈 값·주석 처리) 이면 현행 `pytest tests/` 폴백
  · 같은 키 중복은 last-wins (`board.local_config()` 동형 — 마지막이 빈 값이면 해제로 보고 폴백)
  · 지정된 test_cmd 실행이 실패해도 폴백하지 않고 실패를 그대로 표기 (설정 오류 조용히 삼키기 금지)
  · 러너 종료코드 보존 — 비정상 종료면 rc 를 메시지에 싣고, 출력 없는 실패도 성공과 구분된다
  · '미지정'(파일 부재)과 '읽기 실패'(디렉토리·권한) 구분 — 후자는 폴백 대신 오류 표기
  · 출하 훅에 stale 참조(`precompact_capture_hook.sh` — `56b0162` 에서 출하 트리에서 삭제) 부재

경로 판정(형식정규화·containment·.py 게이트) 계약은 `test_run_tests_hook.py` 가 소유한다 — 여기선 그
파일의 hermetic 실행 하네스(`_make_repo`·`_hook_env`·`_run`)를 재사용해 러너 해소 축만 다룬다. 전부
tmp repo 안에서만 돌아 실 repo 를 오염시키지 않는다.
"""
from __future__ import annotations

import importlib.util
import json
import shlex
import subprocess
from pathlib import Path

import pytest

from test_run_tests_hook import BASH, IS_WINDOWS, _hook_env, _make_repo, _run, requires_bash

REPO = Path(__file__).resolve().parents[1]
ROOT_HOOK = REPO / ".claude" / "run_tests_hook.sh"
SHIPPED_HOOK = REPO / "templates" / "claude_code" / ".claude" / "run_tests_hook.sh"
BOARD_PY = REPO / ".project_manager" / "tools" / "board.py"

FALLBACK_MARKER = "fallback_ran.marker"   # 엔진 폴백 스위트(`pytest tests/`)가 실제로 수집됐다는 증거
STUB_MARKER = "stub_runner.marker"        # local.conf 의 test_cmd 가 실제로 실행됐다는 증거
STUB_TAG = "STUB-RUNNER-OK"


def _write_stub_runner(root: Path, name: str, marker: str, tag: str) -> None:
    """test_cmd 로 지정할 스텁 러너 — cwd 에 마커를 남기고 한 줄을 찍는다.

    마커를 `__file__` 이 아닌 **cwd 기준**으로 쓰고 스크립트도 상대경로로 호출한다 — 훅이 repo 루트에서
    실행하지 않으면 스크립트를 못 찾거나 마커가 딴 데 떨어져 단언이 깨진다(채택자 실값의
    `.project_manager/checks/tests` 같은 repo-상대 경로가 성립하는 전제를 같이 잠근다).
    """
    (root / name).write_text(
        "from pathlib import Path\n"
        f"Path({marker!r}).write_text('ran', encoding='utf-8')\n"
        f"print({tag!r} + ' 1 passed')\n",
        encoding="utf-8",
    )


def _make_hook_repo(tmp_path: Path) -> tuple[Path, Path]:
    """훅 사본 + 폴백 증거를 남기는 `tests/` 더미 + 기본 스텁 러너를 갖춘 tmp repo."""
    repo = tmp_path / "proj"
    hook = _make_repo(repo)
    # 폴백이 돌면 pytest 가 이 모듈을 수집(import)하면서 마커를 남긴다 — "하드코딩 미사용"의 직접 증거.
    (repo / "tests" / "test_dummy.py").write_text(
        "from pathlib import Path\n"
        f"Path(__file__).resolve().parents[1].joinpath({FALLBACK_MARKER!r}).write_text("
        "'ran', encoding='utf-8')\n"
        "\n"
        "def test_ok():\n"
        "    assert True\n",
        encoding="utf-8",
    )
    _write_stub_runner(repo, "stub_runner.py", STUB_MARKER, STUB_TAG)
    return repo, hook


def _write_local_conf(repo: Path, text: str) -> None:
    conf_dir = repo / ".project_manager"
    conf_dir.mkdir(parents=True, exist_ok=True)
    (conf_dir / "local.conf").write_text(text, encoding="utf-8")


def _fire(hook: Path, repo: Path, env: dict):
    """프로젝트 안 .py 편집 payload 로 훅을 발화시킨다(러너 축만 보게 경로 판정은 통과 조건 고정)."""
    proc = _run(hook, {"tool_input": {"file_path": str(repo / "somemodule.py")}}, env)
    assert proc.returncode == 0, f"rc={proc.returncode}\n{proc.stderr}"
    return proc


def _system_message(proc) -> str:
    """훅 stdout(JSON)의 systemMessage — 러너 출력 마지막 줄이 그대로 실린다."""
    assert proc.stdout.strip(), f"systemMessage 없음(훅이 발화하지 않음): {proc.stderr!r}"
    return json.loads(proc.stdout)["systemMessage"]


# ── test_cmd 지정 시: 그 명령이 돌고 하드코딩 스위트는 안 돈다 ──────────────────

@requires_bash
@pytest.mark.parametrize("conf", [
    pytest.param("test_cmd=python stub_runner.py\n", id="plain"),
    pytest.param("  test_cmd = python stub_runner.py  \n", id="whitespace-padded"),
    pytest.param(
        "# per-clone 설정\npy=python3\ntest_cmd=python stub_runner.py\nproject_name=x\n",
        id="among-other-keys",
    ),
])
def test_local_conf_test_cmd_replaces_hardcoded_suite(tmp_path, conf):
    """`local.conf test_cmd` 가 있으면 그 명령이 실행되고 `pytest tests/` 는 실행되지 않는다.

    채택자 제보의 핵심 — 테스트 루트가 다른 프로젝트에서 훅이 엉뚱한(또는 빈) 스위트를 돌던 클래스.
    키/값 주변 공백 허용은 `board.local_config()`(줄 strip + 키/값 strip) 와 같은 시맨틱이다.
    """
    repo, hook = _make_hook_repo(tmp_path)
    _write_local_conf(repo, conf)
    env = _hook_env(tmp_path)

    proc = _fire(hook, repo, env)

    assert (repo / STUB_MARKER).is_file(), (
        f"local.conf 의 test_cmd 가 실행되지 않음 — stdout={proc.stdout!r} stderr={proc.stderr!r}")
    assert not (repo / FALLBACK_MARKER).exists(), (
        "test_cmd 가 지정됐는데 하드코딩 스위트(pytest tests/)가 돌았다")
    message = _system_message(proc)
    assert STUB_TAG in message, message
    # 정상 종료(rc 0) 메시지는 현행 그대로 — rc 주석이 붙지 않는다(성공 신호 오염 방지).
    assert "(rc " not in message, f"정상 종료인데 rc 주석이 붙었다: {message!r}"


@requires_bash
def test_test_cmd_runs_verbatim_without_extra_flags(tmp_path):
    """test_cmd 는 **그대로** 실행된다 — 훅이 `-q --no-header` 등을 덧붙이지 않는다.

    채택자 실값이 이미 자기 플래그(`-q`)를 담고 있어, 덧붙이면 중복 인자가 되거나 러너가 pytest 가
    아닌 스택(go test 등)에서 인자 오류로 죽는다. 스텁이 받은 argv 를 그대로 찍어 검증한다.
    """
    repo, hook = _make_hook_repo(tmp_path)
    (repo / "argv_runner.py").write_text(
        "import sys\nprint('ARGV=' + repr(sys.argv[1:]))\n", encoding="utf-8")
    _write_local_conf(repo, "test_cmd=python argv_runner.py --self-flag\n")
    env = _hook_env(tmp_path)

    message = _system_message(_fire(hook, repo, env))

    assert "ARGV=['--self-flag']" in message, f"인자가 verbatim 이 아니다: {message!r}"


@requires_bash
def test_test_cmd_referencing_unset_variable_still_runs(tmp_path):
    """미설정 변수를 참조하는 정상 test_cmd 가 죽지 않는다 — 훅의 `set -u` 를 물려주지 않는다.

    `PYTHONPATH="$PYTHONPATH:src" pytest` 는 흔한 실값인데, 명령을 `eval` 로 돌리면 **이 훅의 셸**
    에서 평가돼 `set -u` 가 unbound variable 로 죽인다(엔진 `subprocess.run(shell=True)` 에선 정상).
    신선한 자식 셸(`sh -c`)로 넘겨 의미를 맞춘다(codex R3 must-fix).
    """
    repo, hook = _make_hook_repo(tmp_path)
    _write_local_conf(
        repo,
        'test_cmd=PM_T0579_UNSET="$PM_T0579_UNSET:src" python stub_runner.py\n',
    )
    env = _hook_env(tmp_path)
    env.pop("PM_T0579_UNSET", None)

    message = _system_message(_fire(hook, repo, env))

    assert (repo / STUB_MARKER).is_file(), (
        f"미설정 변수 참조만으로 러너가 실행되지 못했다(set -u 상속): {message!r}")
    assert "(rc " not in message, f"정상 명령인데 실패로 보고됐다: {message!r}"


@requires_bash
def test_duplicate_test_cmd_last_wins(tmp_path):
    """같은 키가 여러 번이면 마지막 값 — `board.local_config()` 의 last-wins 와 동일 시맨틱."""
    repo, hook = _make_hook_repo(tmp_path)
    _write_stub_runner(repo, "stub_first.py", "first.marker", "FIRST")
    _write_stub_runner(repo, "stub_last.py", "last.marker", "LAST")
    _write_local_conf(repo, "test_cmd=python stub_first.py\ntest_cmd=python stub_last.py\n")
    env = _hook_env(tmp_path)

    message = _system_message(_fire(hook, repo, env))

    assert (repo / "last.marker").is_file(), f"마지막 test_cmd 미실행: {message!r}"
    assert not (repo / "first.marker").exists(), "앞선 test_cmd 가 이겼다(last-wins 위반)"


# ── test_cmd 미지정: 현행 폴백 유지 ─────────────────────────────────────────────

@requires_bash
@pytest.mark.parametrize("conf", [
    pytest.param(None, id="no-local-conf"),
    pytest.param("py=python3\nproject_name=x\n", id="no-test_cmd-key"),
    pytest.param("test_cmd=\n", id="empty-value"),
    pytest.param("#test_cmd=python stub_runner.py\n", id="commented-out"),
    pytest.param("test_cmd=python stub_runner.py\ntest_cmd=\n", id="last-value-empty-unsets"),
])
def test_falls_back_to_engine_suite_when_test_cmd_unset(tmp_path, conf):
    """test_cmd 미지정이면 현행대로 엔진 스위트(`pytest tests/`)를 돌린다 (100% 하위호환).

    주석 처리된 키를 값으로 오독하지 않는 것까지 같이 잠근다(파싱이 줄 앵커를 지킨다는 증거).
    """
    repo, hook = _make_hook_repo(tmp_path)
    if conf is not None:
        _write_local_conf(repo, conf)
    env = _hook_env(tmp_path)

    proc = _fire(hook, repo, env)

    assert (repo / FALLBACK_MARKER).is_file(), (
        f"폴백 스위트 미실행 — stdout={proc.stdout!r} stderr={proc.stderr!r}")
    assert not (repo / STUB_MARKER).exists(), "미지정인데 스텁 러너가 실행됐다(오독)"


@requires_bash
def test_worktree_shape_local_conf_without_test_cmd_uses_engine_fallback(tmp_path):
    """PM 홈 + 코드 worktree 분리 형상 — worktree local.conf 에 test_cmd 가 없으면 폴백이 정상 경로.

    훅은 자기가 사는 체크아웃 루트의 local.conf **하나만** 본다(PM 홈 쪽 값을 끌어오지 않는다). 그
    형상에서 worktree conf 는 py/project_name/upstream_rev 만 담을 수 있고, 그때 엔진 스위트 폴백이
    오동작이 아니라 설계된 경로임을 실제 conf 모양으로 고정한다.
    """
    repo, hook = _make_hook_repo(tmp_path)
    _write_local_conf(
        repo,
        "# per-clone 설정 (git-ignored)\n"
        "py=python3\n"
        "project_name=demo\n"
        "external_review_enabled=true\n"
        "upstream_rev=0123456789abcdef0123456789abcdef01234567\n",
    )
    env = _hook_env(tmp_path)

    proc = _fire(hook, repo, env)

    assert (repo / FALLBACK_MARKER).is_file(), (
        f"worktree 형상 폴백 미발화 — stdout={proc.stdout!r} stderr={proc.stderr!r}")
    assert not (repo / STUB_MARKER).exists(), "test_cmd 부재인데 스텁이 실행됐다"


# ── 조용한 실패 금지: 설정된 러너가 깨져도 폴백으로 감추지 않는다 ───────────────

@requires_bash
def test_broken_test_cmd_reports_failure_instead_of_silent_fallback(tmp_path):
    """test_cmd 가 실행 불가(명령 부재)여도 폴백하지 않고 실패를 그대로 표기한다.

    폴백하면 설정 오류가 green 으로 위장돼(채택자는 자기 스위트가 도는 줄 안다) 훅이 회귀 신호를
    잃는다 — 훅의 조용한 실패 금지 규율.
    """
    repo, hook = _make_hook_repo(tmp_path)
    _write_local_conf(repo, "test_cmd=pm-no-such-runner-t0579 --run\n")
    env = _hook_env(tmp_path)

    proc = _fire(hook, repo, env)

    assert not (repo / FALLBACK_MARKER).exists(), (
        "test_cmd 실패를 폴백이 삼켰다 — 설정 오류가 조용히 green 으로 위장된다")
    assert _system_message(proc).strip() != "tests:", (
        f"실패 메시지가 비었다(무엇이 잘못됐는지 안 보인다): {proc.stdout!r}")


@requires_bash
def test_unreadable_local_conf_reports_error_instead_of_falling_back(tmp_path):
    """local.conf 가 **있는데 못 읽히면**(디렉토리·권한·I/O) 폴백하지 않고 오류로 표기한다.

    '미지정'(파일 부재)과 '읽기 실패'는 다른 사건이다 — 후자에서 하드코딩 스위트로 넘어가면 채택자는
    자기 러너가 돈 줄 알고 회귀 신호를 잘못 읽는다. sed 를 파이프에 물리면 rc 가 tail 것으로 덮여
    이 구분 자체가 불가능하다(codex R2 must-fix).
    """
    repo, hook = _make_hook_repo(tmp_path)
    (repo / ".project_manager" / "local.conf").mkdir(parents=True)  # 파일 자리에 디렉토리
    env = _hook_env(tmp_path)

    proc = _fire(hook, repo, env)
    message = _system_message(proc)

    assert not (repo / FALLBACK_MARKER).exists(), (
        "local.conf 읽기 실패인데 하드코딩 스위트로 조용히 폴백했다")
    assert not (repo / STUB_MARKER).exists(), "읽기 실패인데 스텁 러너가 실행됐다"
    assert "local.conf" in message, f"오류 메시지에 원인(local.conf)이 없다: {message!r}"


@requires_bash
def test_silent_failing_test_cmd_reports_rc(tmp_path):
    """출력 없이 rc≠0 로 죽는 러너도 실패로 보인다 — rc 를 메시지에 싣는다 (codex must-fix).

    러너를 `러너 | tail -1` 로 받으면 파이프라인 rc 가 tail 의 것(항상 0)이라 종료코드가 버려진다.
    `test_cmd=false` 처럼 출력까지 없으면 메시지가 빈 문자열이 돼 성공과 구분되지 않는다 — '조용한
    실패 금지' 결정을 구현이 스스로 깨는 지점. rc 를 따로 캡처해 메시지에 실어야 한다.
    """
    repo, hook = _make_hook_repo(tmp_path)
    (repo / "silent_fail.py").write_text("import sys\nsys.exit(3)\n", encoding="utf-8")
    _write_local_conf(repo, "test_cmd=python silent_fail.py\n")
    env = _hook_env(tmp_path)

    message = _system_message(_fire(hook, repo, env))

    assert message.strip() != "tests:", (
        f"무출력 실패가 빈 메시지로 성공과 구분 불가: {message!r}")
    assert "3" in message, f"러너 종료코드(3)가 메시지에 없다: {message!r}"
    assert not (repo / FALLBACK_MARKER).exists(), "무출력 실패를 폴백이 삼켰다"


@requires_bash
def test_failing_test_cmd_keeps_last_line_and_appends_rc(tmp_path):
    """출력이 있는 실패는 마지막 줄을 유지하면서 rc 를 덧붙인다(요약 정보 손실 없이 실패 명시)."""
    repo, hook = _make_hook_repo(tmp_path)
    (repo / "noisy_fail.py").write_text(
        "import sys\nprint('FAILED stub::case')\nsys.exit(2)\n", encoding="utf-8")
    _write_local_conf(repo, "test_cmd=python noisy_fail.py\n")
    env = _hook_env(tmp_path)

    message = _system_message(_fire(hook, repo, env))

    assert "FAILED stub::case" in message, f"러너 출력 마지막 줄이 사라졌다: {message!r}"
    assert "2" in message, f"러너 종료코드(2)가 메시지에 없다: {message!r}"


@requires_bash
def test_fallback_failure_also_reports_rc(tmp_path):
    """폴백(엔진 스위트) 경로도 같은 rc 보존 규칙을 따른다 — 두 경로의 신호가 갈리지 않게."""
    repo, hook = _make_hook_repo(tmp_path)
    (repo / "tests" / "test_dummy.py").write_text(
        "from pathlib import Path\n"
        f"Path(__file__).resolve().parents[1].joinpath({FALLBACK_MARKER!r}).write_text("
        "'ran', encoding='utf-8')\n"
        "\n"
        "def test_fails():\n"
        "    assert False\n",
        encoding="utf-8",
    )
    env = _hook_env(tmp_path)

    message = _system_message(_fire(hook, repo, env))

    assert (repo / FALLBACK_MARKER).is_file(), f"폴백 스위트 미실행: {message!r}"
    assert "rc 1" in message, f"폴백 실패에 rc 가 없다: {message!r}"


# ── 파싱 방언 동형성: 훅 sed ↔ 엔진 board.local_config() ────────────────────────

_RESOLVER_BEGIN = "--8<-- test_cmd 해소 시작"
_RESOLVER_END = "--8<-- test_cmd 해소 끝"


def _hook_resolver_snippet() -> str:
    """출하 훅에서 test_cmd 해소 구간을 *원문 그대로* 떼어온다 (사본 재작성 금지 — 실 코드를 돌린다).

    구간은 훅 안 sentinel 주석으로 경계가 표시돼 있다. 마커가 사라지면 여기서 fail-loud 로 멈춘다 —
    동형성 가드가 옛 사본을 돌리며 조용히 green 되는 것보다 낫다(추출 실패 = 가드 갱신 신호).
    """
    lines = SHIPPED_HOOK.read_text(encoding="utf-8").splitlines()
    begin = next((i for i, line in enumerate(lines) if _RESOLVER_BEGIN in line), None)
    end = next((i for i, line in enumerate(lines) if _RESOLVER_END in line), None)
    assert begin is not None, f"훅에서 해소 구간 시작 마커(`{_RESOLVER_BEGIN}`)를 못 찾음"
    assert end is not None and end > begin, f"훅에서 해소 구간 끝 마커(`{_RESOLVER_END}`)를 못 찾음"
    return "\n".join(lines[begin + 1:end])


def _resolve_via_hook(repo_root: Path) -> str:
    """훅의 해소 구문만 떼어 bash 로 실행한 결과(빈 문자열 = 미해소)."""
    script = (
        "set -u\n"
        f"repo_root={shlex.quote(repo_root.as_posix())}\n"
        f"{_hook_resolver_snippet()}\n"
        'printf "%s" "$test_cmd"\n'
    )
    proc = subprocess.run(
        [BASH, "-c", script], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60,
    )
    assert proc.returncode == 0, f"해소 구문 실행 실패 rc={proc.returncode}\n{proc.stderr}"
    return proc.stdout


def _load_board():
    spec = importlib.util.spec_from_file_location("board_t0579", BOARD_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def board():
    return _load_board()


# local.conf 방언 배터리 — 훅 sed 와 엔진 파서가 *같은 값*을 뽑아야 하는 입력들.
# None = 파일 자체 부재. 각 항목은 리뷰에서 지적된 갈림길(공백/개행/주석/BOM/오매칭/중복)을 하나씩 짚는다.
CONF_DIALECTS = [
    ("absent-file", None),
    ("empty-file", ""),
    ("blank-and-whitespace-lines", "\n   \n\t\n"),
    ("plain", "test_cmd=pytest -q\n"),
    ("no-trailing-newline-at-eof", "test_cmd=pytest -q"),
    ("leading-spaces", "   test_cmd=pytest -q\n"),
    ("leading-tab", "\ttest_cmd=pytest -q\n"),
    ("spaces-around-equals", "test_cmd = pytest -q\n"),
    ("tabs-around-equals", "test_cmd\t=\tpytest -q\n"),
    ("trailing-spaces-in-value", "test_cmd=pytest -q   \n"),
    ("trailing-tab-in-value", "test_cmd=pytest -q\t\n"),
    ("crlf-line-endings", "py=python3\r\ntest_cmd=pytest -q\r\n"),
    ("crlf-no-trailing-newline", "test_cmd=pytest -q\r"),
    ("bom-on-key-line", "\ufefftest_cmd=pytest -q\n"),
    ("bom-on-earlier-line", "\ufeffpy=python3\ntest_cmd=pytest -q\n"),
    ("empty-value", "test_cmd=\n"),
    ("whitespace-only-value", "test_cmd=   \n"),
    ("duplicate-last-wins", "test_cmd=first -q\ntest_cmd=second -q\n"),
    ("duplicate-last-empty-unsets", "test_cmd=first -q\ntest_cmd=\n"),
    ("equals-inside-value", "test_cmd=make test ARG=1\n"),
    ("double-equals", "test_cmd==pytest -q\n"),
    ("hash-inside-value", 'test_cmd=pytest -k "a#b"\n'),
    ("quotes-inside-value", 'test_cmd=python -c "import sys; sys.exit(0)"\n'),
    ("comment-line-tight", "#test_cmd=pytest -q\n"),
    ("comment-line-spaced", "  # test_cmd=pytest -q\n"),
    ("key-prefix-mismatch", "xtest_cmd=pytest -q\n"),
    ("key-suffix-mismatch", "test_cmdx=pytest -q\n"),
    ("key-underscore-neighbor", "slot_test_cmd=pytest -q\n"),
    ("key-case-mismatch", "TEST_CMD=pytest -q\n"),
    ("key-without-equals", "test_cmd\n"),
    ("non-ascii-value", "test_cmd=pytest -k 한글\n"),
    ("windows-path-value", "test_cmd=C:\\py\\python.exe -m pytest tests -q\n"),
    (
        "realistic-multi-key",
        "# per-clone 설정\npy=python3\ntest_cmd=python3 -m pytest .checks/tests -q\n"
        "project_name=demo\nupstream_rev=abc123\n",
    ),
]


@requires_bash
@pytest.mark.parametrize("content", [c for _, c in CONF_DIALECTS],
                         ids=[i for i, _ in CONF_DIALECTS])
def test_hook_parser_matches_engine_local_config(tmp_path, monkeypatch, board, content):
    """훅의 sed 해소가 엔진 `board.local_config()['test_cmd']` 와 같은 값을 낸다 (방언 전수).

    훅이 board 를 import 할 수 없으니(셸) 파싱이 필연적으로 두 벌이다 — 두 벌이 갈리면 채택자는
    `board.py regression` 과 편집 훅이 *서로 다른 스위트*를 도는 것을 알 수 없다. 두 파서를 실제로
    돌려 값을 대조해 그 갈림을 기계로 막는다. 훅은 미해소를 빈 문자열로, board 는 키 부재를 None 으로
    표현하므로 `None → ""` 만 정규화하고 나머지는 정확 일치를 요구한다.
    """
    conf_dir = tmp_path / ".project_manager"
    conf_dir.mkdir(parents=True)
    conf_path = conf_dir / "local.conf"
    if content is not None:
        conf_path.write_bytes(content.encode("utf-8"))

    monkeypatch.setattr(board, "LOCAL_CONF", conf_path)
    engine_value = board.local_config().get("test_cmd")
    hook_value = _resolve_via_hook(tmp_path)

    assert hook_value == (engine_value or ""), (
        f"파서 갈림 — 훅={hook_value!r} 엔진={engine_value!r} (입력={content!r})")


def test_dialect_battery_covers_the_reviewed_edges():
    """배터리가 리뷰 지적 축(공백·개행·주석·BOM·오매칭·중복·빈 값)을 실제로 담고 있는지 자기점검."""
    ids = {name for name, _ in CONF_DIALECTS}
    for axis in ("crlf-line-endings", "bom-on-key-line", "leading-tab", "comment-line-tight",
                 "key-prefix-mismatch", "key-suffix-mismatch", "key-case-mismatch",
                 "key-without-equals", "duplicate-last-wins", "duplicate-last-empty-unsets",
                 "empty-value", "no-trailing-newline-at-eof", "equals-inside-value"):
        assert axis in ids, f"배터리에서 축 누락: {axis}"


# ── 실행 방언: 훅 실행 ↔ 엔진 subprocess.run(shell=True) ────────────────────────

ARGV_RUNNER = "import json, sys\nprint('ARGV=' + json.dumps(sys.argv[1:]))\n"

# 파싱이 같아도 *실행* 의미가 갈리면(셸 컨텍스트 상속·재이스케이프) 같은 test_cmd 가 훅과 엔진에서
# 다르게 돈다. 파싱 배터리와 별개 축으로, 셸이 명령줄을 어떻게 쪼개는지 실제 argv 로 대조한다.
EXEC_DIALECTS = [
    ("plain-flags", "python argv_runner.py --self-flag"),
    ("multiple-args", "python argv_runner.py -k not slow --maxfail=1"),
    ("double-quoted-arg-with-space", 'python argv_runner.py "a b" c'),
    ("single-quoted-arg", "python argv_runner.py 'x  y'"),
    ("backslash-path-literal", "python argv_runner.py C:\\py\\python.exe"),
    ("quoted-backslash-path", "python argv_runner.py 'C:\\py\\python.exe'"),
    ("env-prefix-with-unset-var",
     'PM_T0579_UNSET="$PM_T0579_UNSET:src" python argv_runner.py --after'),
]

skip_on_windows_shell_dialect = pytest.mark.skipif(
    IS_WINDOWS,
    reason="엔진 shell=True 는 Windows 에서 cmd.exe, 훅은 Git Bash — 셸 방언 자체가 달라 등가 비교 불가",
)


def _argv_payload(text: str) -> str:
    """러너가 찍은 argv JSON 을 뽑는다(훅 메시지·엔진 stdout 공통 형식)."""
    marker = "ARGV="
    assert marker in text, f"러너 출력에 argv 표지가 없다: {text!r}"
    return text[text.index(marker) + len(marker):].strip()


def _prepare_exec_repo(tmp_path: Path, command: str):
    repo, hook = _make_hook_repo(tmp_path)
    (repo / "argv_runner.py").write_text(ARGV_RUNNER, encoding="utf-8")
    _write_local_conf(repo, f"test_cmd={command}\n")
    env = _hook_env(tmp_path)
    env.pop("PM_T0579_UNSET", None)   # 미설정 변수 축이 환경 오염으로 무력화되지 않게
    return repo, hook, env


@requires_bash
@skip_on_windows_shell_dialect
@pytest.mark.parametrize("command", [c for _, c in EXEC_DIALECTS],
                         ids=[i for i, _ in EXEC_DIALECTS])
def test_hook_execution_matches_engine_shell_semantics(tmp_path, command):
    """훅이 test_cmd 로 실제 띄우는 argv 가 엔진 `subprocess.run(shell=True)` 과 같다.

    같은 문자열이 두 실행 경로에서 다르게 쪼개지면 채택자는 `board.py regression` 과 편집 훅이
    *다른 명령*을 돌리는 것을 알 수 없다. 훅이 `eval`(현재 셸 컨텍스트·`set -u` 상속)이 아니라
    신선한 자식 셸을 쓰는지를 이 등가성이 잠근다 — `set -u` 축(미설정 변수 참조)이 그 차이를 실제로
    가른다(codex R3 must-fix).
    """
    repo, hook, env = _prepare_exec_repo(tmp_path, command)

    hook_argv = _argv_payload(_system_message(_fire(hook, repo, env)))

    engine = subprocess.run(
        command, shell=True, cwd=str(repo), env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    assert engine.returncode == 0, f"엔진 기준 실행 실패 rc={engine.returncode}\n{engine.stderr}"
    engine_argv = _argv_payload(engine.stdout)

    assert hook_argv == engine_argv, (
        f"실행 의미 갈림 — 훅={hook_argv} 엔진={engine_argv} (test_cmd={command!r})")


@requires_bash
@skip_on_windows_shell_dialect
def test_backslash_literal_follows_posix_shell_unescaping(tmp_path):
    r"""따옴표 없는 `C:\py\python.exe` 는 POSIX 셸 파싱에서 backslash 가 사라진다 (실측 고정).

    훅이 값을 망가뜨리는 게 아니라 셸 명령줄 문자열의 정의된 동작이고, 엔진 러너(`shell=True`
    = `/bin/sh -c`)도 똑같이 그렇게 한다 — `eval` 을 `sh -c` 로 바꿔도 동일(실측). Windows 경로를
    쓰려면 따옴표로 감싸야 하며 그 회피법이 실제로 통한다는 것까지 같이 박아 둔다.
    """
    repo, hook, env = _prepare_exec_repo(tmp_path, "python argv_runner.py C:\\py\\python.exe")
    bare_argv = _argv_payload(_system_message(_fire(hook, repo, env)))
    assert json.loads(bare_argv) == ["C:pypython.exe"], bare_argv

    repo2, hook2, env2 = _prepare_exec_repo(tmp_path / "quoted",
                                            "python argv_runner.py 'C:\\py\\python.exe'")
    quoted_argv = _argv_payload(_system_message(_fire(hook2, repo2, env2)))
    assert json.loads(quoted_argv) == ["C:\\py\\python.exe"], quoted_argv


# ── stale 참조 + 소유권 안내 (정적 가드·양 트리) ────────────────────────────────

def test_hook_has_no_stale_precompact_reference():
    """훅 주석의 `precompact_capture_hook.sh` 참조 부재 — 출하 트리엔 그 파일이 없다(`56b0162` 삭제).

    루트 `.claude/` 에만 남은 instance-owned 파일이라, 출하 사본을 받은 채택자에겐 dangling 참조였다.
    """
    assert not (SHIPPED_HOOK.parent / "precompact_capture_hook.sh").exists(), (
        "출하 템플릿에 precompact_capture_hook.sh 가 생겼다면 이 가드의 전제를 재검토할 것")
    for path in (ROOT_HOOK, SHIPPED_HOOK):
        assert "precompact_capture_hook" not in path.read_text(encoding="utf-8"), (
            f"{path} 에 stale 참조 재유입")


def test_hook_header_documents_local_conf_ownership():
    """훅 머리주석이 러너 소유권을 `local.conf test_cmd` 로 안내한다(이 파일을 고치라는 옛 안내 대체).

    훅 파일은 manifest 등재(엔진 소유)라 채택자가 러너 줄을 고쳐도 다음 동기에 덮인다 — 안내와
    소유권이 모순이던 것을 교정한 뒤, 그 안내가 사라지지 않게 잠근다.
    """
    for path in (ROOT_HOOK, SHIPPED_HOOK):
        header = path.read_text(encoding="utf-8").split("set -u", 1)[0]
        assert "local.conf" in header and "test_cmd" in header, (
            f"{path} 머리주석에 러너 소유권(local.conf test_cmd) 안내 부재")
