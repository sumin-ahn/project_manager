"""회귀 병렬 실행(pytest-xdist) 가드 — 워커 경고가 컨트롤러를 죽이지 않나 (T-0757).

워커에서 난 경고를 xdist 컨트롤러는 `importlib.import_module(<경고 클래스의 모듈 이름>)` 으로 되살린다.
파일 경로로 연 모듈의 이름은 컨트롤러에 없어 import 가 실패하고, xdist 가 그 실패를 잡지 않아 경고 한
건이 실행 전체를 INTERNALERROR 로 죽였다(실측 두 형태: 엔진 합성 이름 `_project_manager_<name>:<path>` ·
테스트가 자기 사본에 붙인 이름 `file_lock_under_test`). `tests/conftest.py` ③절이 그 되살리기를
실패-연성으로 감싸, 되살릴 수 없는 경고는 원 모듈명·클래스명·메시지를 본문에 보존한 일반 Warning 으로
낮춰 받는다. 이 파일이 세 층위로 고정한다.

1. 감싸기 단위 — 해소 불가 두 이름이 낮춰지고, 해소되는 이름은 그대로 되살아난다.
2. 설치 — 컨트롤러에만 서고 워커엔 안 선다 · 감쌀 xdist 심볼이 없으면 조용히 넘어가지 않고 실패한다.
3. `-n 2` 실행 — 워커가 두 종류의 경고를 내는 소형 스위트를 자식 pytest 로 돌려 rc0 과 요약 보존을
   본다. 가드 없는 같은 스위트가 INTERNALERROR 로 죽는 것을 대조군으로 함께 돌린다.
"""
from __future__ import annotations

import contextlib
import shutil
import subprocess
import sys
import textwrap
import warnings
from pathlib import Path

import pytest
import xdist
from xdist import workermanage
from xdist.remote import serialize_warning_message

import conftest as test_config
from _textio import utf8_child_env

REPO = Path(__file__).resolve().parent.parent
TOOLS = REPO / ".project_manager" / "tools"

# 실측된 두 형태 — 엔진 로더의 합성 이름(사본 구분용 경로 포함)과 테스트가 붙인 사본 이름
# (`tests/test_file_lock.py` 가 file_lock.py 를 이 이름으로 연다). 어느 쪽도 컨트롤러에서 import 되지
# 않는다: `spec_from_file_location` 호출부 192 파일이 제각각 이름을 붙여 되짚을 규칙이 없다.
_UNRESOLVABLE_WARNING_NAMES = {
    "engine-synthetic": (
        f"_project_manager_repo_owned_files:{TOOLS / 'repo_owned_files.py'}",
        "RepoFilesFallbackWarning"),
    "ad-hoc-copy": ("file_lock_under_test", "LocklessFallbackWarning"),
}
_WARNING_TEXT = "git 보장이 사라져 강등"

# 자식 pytest 실행 상한 — 프로브 스위트는 테스트 두 개라 정상이면 1초 안팎에 끝난다.
NESTED_RUN_TIMEOUT_SECONDS = 300


class _ProbeWarning(RuntimeWarning):
    """직렬화 데이터를 만들 때 쓰는 경고 — 이름/모듈은 데이터 쪽에서 실측값으로 덮는다."""


def _raw_unserializer():
    """감싸기 이전의 xdist 원본 되살리기 — 컨트롤러에선 감싸개가 원본을 들고 있다."""
    current = workermanage.unserialize_warning_message
    return getattr(current, "_pm_original", current)


@contextlib.contextmanager
def _controller_module_shape(module_name: str):
    """그 이름이 `sys.modules` 에 없는 상태 — 경고를 되살리는 컨트롤러의 형상을 재현한다.

    워커는 같은 프로세스에서 엔진/사본 모듈을 이미 열어 그 이름을 캐시에 갖고 있을 수 있다(엔진
    부트스트랩이 합성 이름으로 캐시한다 · `-n 8` 실측에서 gw5 만 초록이 아니었다). 컨트롤러에는 그
    캐시가 없으므로 잠시 비우고 단언한 뒤 원상복구한다.
    """
    cached = sys.modules.pop(module_name, None)
    try:
        yield
    finally:
        if cached is not None:
            sys.modules[module_name] = cached


def _warning_data(module_name: str, class_name: str) -> dict:
    """워커가 보내는 것과 같은 모양의 경고 직렬화 데이터."""
    data = serialize_warning_message(warnings.WarningMessage(
        message=_ProbeWarning(_WARNING_TEXT),
        category=_ProbeWarning,
        filename=str(TOOLS / "repo_owned_files.py"),
        lineno=1,
    ))
    return dict(
        data,
        message_module=module_name, message_class_name=class_name,
        category_module=module_name, category_class_name=class_name)


# ── 1. 감싸기 단위 ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("shape", sorted(_UNRESOLVABLE_WARNING_NAMES))
def test_raw_xdist_dies_on_unresolvable_warning_name(shape):
    """감싸지 않은 xdist 되살리기는 두 형태 모두에서 ModuleNotFoundError — 감싸기의 대상 표면."""
    module_name, class_name = _UNRESOLVABLE_WARNING_NAMES[shape]

    with _controller_module_shape(module_name), pytest.raises(ModuleNotFoundError):
        _raw_unserializer()(_warning_data(module_name, class_name))


@pytest.mark.parametrize("shape", sorted(_UNRESOLVABLE_WARNING_NAMES))
def test_unresolvable_warning_degrades_with_identity_preserved(shape):
    """되살릴 수 없는 경고는 원 모듈명·클래스명·메시지를 본문에 담은 일반 Warning 으로 낮춰 받는다."""
    module_name, class_name = _UNRESOLVABLE_WARNING_NAMES[shape]
    data = _warning_data(module_name, class_name)

    with _controller_module_shape(module_name):
        restored = test_config.tolerant_warning_unserializer(_raw_unserializer())(data)

    # 되살리기가 성공했다면 category 는 원래 클래스다 — Warning 이라는 것이 낮춰 받았다는 증거다.
    assert restored.category is Warning
    assert isinstance(restored.message, Warning)
    assert f"{module_name}.{class_name}" in str(restored.message)
    assert _WARNING_TEXT in str(restored.message)
    assert restored.filename == data["filename"]
    assert restored.lineno == data["lineno"]


def test_resolvable_warning_is_not_degraded():
    """이름으로 되살아나는 경고는 감싸개를 지나도 클래스 정체 그대로다(무차별 강등 아님)."""
    data = _warning_data("builtins", "RuntimeWarning")

    restored = test_config.tolerant_warning_unserializer(_raw_unserializer())(data)

    assert restored.category is RuntimeWarning
    assert isinstance(restored.message, RuntimeWarning)
    assert str(restored.message) == _WARNING_TEXT


# ── 2. 설치 ────────────────────────────────────────────────────────────────────


def test_tolerant_unserializer_is_installed_on_the_controller_only(request):
    """되살리기를 실제로 하는 컨트롤러에만 감싸기가 서고, 워커는 원본 그대로 둔다."""
    installed = getattr(workermanage.unserialize_warning_message, "_pm_tolerant", False)

    if hasattr(request.config, "workerinput"):
        assert not installed
    else:
        assert installed


def test_install_fails_loud_when_the_xdist_symbol_is_gone(monkeypatch):
    """감쌀 xdist 심볼이 없으면 조용히 넘어가지 않는다 — 버전과 심볼 이름을 짚어 실패한다."""
    monkeypatch.delattr(workermanage, "unserialize_warning_message")

    with pytest.raises(RuntimeError) as raised:
        test_config.install_tolerant_warning_unserializer()

    message = str(raised.value)
    assert "unserialize_warning_message" in message
    assert xdist.__version__ in message
    assert "INTERNALERROR" in message


# ── 3. `-n 2` 실행 ─────────────────────────────────────────────────────────────


def _write_probe_suite(root: Path, *, with_guards: bool) -> None:
    """워커에서 두 종류의 경고를 내는 최소 스위트를 만든다 (conftest 가드 유무로 대조)."""
    root.mkdir(parents=True, exist_ok=True)
    if with_guards:
        shutil.copy2(REPO / "tests" / "conftest.py", root / "conftest.py")
    else:
        (root / "conftest.py").write_text("", encoding="utf-8")
    (root / "test_engine_warning_probe.py").write_text(
        textwrap.dedent(f'''\
            """워커에서 컨트롤러로 넘어가는 두 종류의 경고를 낸다 (합성 모듈명·임의 사본 이름)."""
            import importlib.util
            import sys
            import warnings
            from pathlib import Path

            REPO_FILES_SOURCE = Path({str(TOOLS / "repo_owned_files.py")!r})
            FILE_LOCK_SOURCE = Path({str(TOOLS / "file_lock.py")!r})
            SYNTHETIC_NAME = f"_project_manager_repo_owned_files:{{REPO_FILES_SOURCE}}"
            AD_HOC_NAME = "file_lock_under_test"


            def _load(source, name):
                spec = importlib.util.spec_from_file_location(name, source)
                module = importlib.util.module_from_spec(spec)
                sys.modules[name] = module
                spec.loader.exec_module(module)
                return module


            def test_engine_synthetic_name_warning_reaches_the_controller():
                module = _load(REPO_FILES_SOURCE, SYNTHETIC_NAME)
                warnings.warn("합성 모듈명 프로브", module.RepoFilesFallbackWarning)


            def test_ad_hoc_copy_name_warning_reaches_the_controller():
                module = _load(FILE_LOCK_SOURCE, AD_HOC_NAME)
                warnings.warn("사본 이름 프로브", module.LocklessFallbackWarning)
            '''),
        encoding="utf-8")


def _run_parallel_probe(root: Path) -> subprocess.CompletedProcess:
    """중첩 pytest 를 UTF-8 stdio 로 띄운다.

    부모가 `encoding="utf-8"` 로 읽어도, 자식 파이썬 자체가 로케일 기본 코덱(Windows
    cp949)으로 쓰면 그 바이트를 UTF-8 로 재해석하는 순간 한글 경고 본문이 mojibake 된다
    (T-0741). `utf8_child_env` 로 자식 stdio 코덱을 명시해 부모의 UTF-8 디코드 가정과
    맞춘다.
    """
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-n", "2", str(root)],
        cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=utf8_child_env(), stdin=subprocess.DEVNULL, timeout=NESTED_RUN_TIMEOUT_SECONDS)


def test_parallel_run_survives_worker_warnings(tmp_path):
    """conftest 가드가 있으면 `-n 2` 실행이 두 경고를 다 받고도 rc0 으로 끝난다.

    강등이 정보를 지우지 않는지도 같은 실행에서 본다 — 요약에 원 모듈명·클래스명·메시지가 남는다.
    """
    root = tmp_path / "with-guards"
    _write_probe_suite(root, with_guards=True)

    result = _run_parallel_probe(root)

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "INTERNALERROR" not in output
    assert "_project_manager_repo_owned_files:" in output
    assert "RepoFilesFallbackWarning: 합성 모듈명 프로브" in output
    assert "file_lock_under_test.LocklessFallbackWarning: 사본 이름 프로브" in output


def test_parallel_run_without_guards_dies_on_the_same_warnings(tmp_path):
    """대조군 — 가드 없는 같은 스위트는 컨트롤러 INTERNALERROR 로 죽는다(가드 표면 확인)."""
    root = tmp_path / "without-guards"
    _write_probe_suite(root, with_guards=False)

    result = _run_parallel_probe(root)

    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "INTERNALERROR" in output
    assert "ModuleNotFoundError" in output
