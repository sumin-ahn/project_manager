"""회귀 병렬 실행(pytest-xdist) 가드 — 워커 경고가 컨트롤러에서 되살아나나 (T-0757).

워커에서 난 경고를 xdist 컨트롤러는 `importlib.import_module(<경고 클래스의 모듈 이름>)` 으로 되살린다.
파일 경로로 연 모듈의 이름은 컨트롤러에 없어 import 가 실패하고, xdist 가 그 실패를 잡지 않아 실행
전체가 INTERNALERROR 로 죽었다. `tests/conftest.py` 가 두 겹으로 닫는다 — 엔진 합성 이름
(`_project_manager_<name>:<abs path>`)은 meta_path finder 가 실제 파일로 해소하고, 테스트가 자기 사본에
붙이는 임의 이름(`spec_from_file_location` 호출부 192 파일이 제각각)은 어떤 규칙으로도 해소할 수 없으니
실패-연성 되살리기가 원 클래스 이름·메시지를 본문에 보존한 일반 Warning 으로 낮춰 받는다.
이 파일이 세 층위로 그것을 고정한다.

1. finder 단위 — 잎 이름(실 파일)·조상 조각·타 이름·사라진 사본.
2. xdist 되살리기 왕복 — `serialize_warning_message`/`unserialize_warning_message` 직접 호출.
   finder 를 걷어내면 같은 왕복이 ModuleNotFoundError 로 깨지는 것과, 해소 불가 이름이 실패-연성
   되살리기로 낮춰지는 것을 함께 고정한다(가드가 제 표면을 덮나).
3. `-n 2` 실행 — 워커가 두 종류(엔진 합성 이름·임의 사본 이름)의 경고를 내는 소형 스위트를 자식
   pytest 로 돌려 rc0 을 본다. 가드 없는 같은 스위트가 INTERNALERROR 로 죽는 것을 대조군으로 함께
   돌린다.
"""
from __future__ import annotations

import importlib
import shutil
import subprocess
import sys
import textwrap
import warnings
from pathlib import Path

import pytest
from xdist import workermanage
from xdist.remote import serialize_warning_message

import conftest as test_config

REPO = Path(__file__).resolve().parent.parent
TOOLS = REPO / ".project_manager" / "tools"

# 병렬 안전성이 걸린 엔진 경고 — 실제로 합성 모듈명을 `__module__` 로 갖는 클래스들.
_ENGINE_WARNING_SOURCES = {
    "RepoFilesFallbackWarning": TOOLS / "repo_owned_files.py",
    "LocklessFallbackWarning": TOOLS / "file_lock.py",
}

# 자식 pytest 실행 상한 — 프로브 스위트는 테스트 두 개라 정상이면 1초 안팎에 끝난다.
NESTED_RUN_TIMEOUT_SECONDS = 300


class _AdHocCopyWarning(RuntimeWarning):
    """테스트가 자기 사본에 붙인 임의 이름으로 되살릴 수 없는 경고를 흉내 낼 때 쓰는 클래스."""


def _synthetic_name(source: Path) -> str:
    """엔진이 그 파일에 주는 것과 같은 형식의 합성 모듈명."""
    return f"_project_manager_{source.stem}:{source}"


def _forget_synthetic_modules() -> dict[str, object]:
    """sys.modules 에서 합성 이름 항목을 걷어내고 원본을 돌려준다(복원은 호출부 책임)."""
    removed = {
        name: module for name, module in list(sys.modules.items())
        if test_config.synthetic_module_location(name) is not None
    }
    for name in removed:
        del sys.modules[name]
    return removed


# ── 1. finder 단위 ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("class_name", sorted(_ENGINE_WARNING_SOURCES))
def test_finder_loads_engine_module_from_synthetic_name(class_name):
    """잎 이름은 그 경로의 실 파일로 로드되고 sys.modules 에 재사용 가능하게 남는다."""
    source = _ENGINE_WARNING_SOURCES[class_name]
    name = _synthetic_name(source)

    module = importlib.import_module(name)

    warning_class = getattr(module, class_name)
    assert issubclass(warning_class, Warning)
    assert warning_class.__module__ == name
    assert Path(module.__file__) == source
    assert sys.modules[name] is module
    assert importlib.import_module(name) is module


def test_finder_makes_ancestor_fragment_a_package():
    """합성 이름의 점 표기 조상 조각은 빈 패키지로 선다 — 잎 import 가 부모 `__path__` 를 탄다."""
    fragment = f"_project_manager_repo_owned_files:{REPO}/"

    spec = test_config.EngineSyntheticModuleFinder().find_spec(fragment)

    assert spec is not None
    assert spec.submodule_search_locations == []
    module = importlib.import_module(fragment)
    try:
        assert module.__path__ == []
    finally:
        sys.modules.pop(fragment, None)


@pytest.mark.parametrize(
    "foreign_name",
    [
        "json",
        "_project_manager_console_encoding_state_v1",  # 합성 아님(경로 구분자 없음)
        "_project_manager_:/abs/path/board.py",  # 이름 부분이 비었다
    ],
)
def test_finder_ignores_foreign_names(foreign_name):
    """엔진 합성 형식이 아닌 이름은 건드리지 않는다(다른 finder 에게 넘긴다)."""
    assert test_config.EngineSyntheticModuleFinder().find_spec(foreign_name) is None


def test_finder_fabricates_warning_class_for_vanished_copy(tmp_path):
    """사본이 이미 지워진 잎 이름이면 같은 이름의 Warning 하위 클래스를 대신 준다.

    엔진 사본을 tmp 트리에 두고 in-process 로 로드한 테스트의 경고가 그 트리 삭제 뒤 컨트롤러에
    닿아도 실행 전체가 죽지 않아야 한다. 경고가 아닌 속성은 평소대로 AttributeError.
    """
    vanished = tmp_path / "tools" / "repo_owned_files.py"
    name = _synthetic_name(vanished)
    assert not vanished.exists()

    module = importlib.import_module(name)
    try:
        fabricated = module.RepoFilesFallbackWarning
        assert issubclass(fabricated, Warning)
        assert fabricated.__module__ == name
        assert module.RepoFilesFallbackWarning is fabricated
        with pytest.raises(AttributeError):
            module.load_module
    finally:
        sys.modules.pop(name, None)


# ── 2. xdist 되살리기 왕복 ──────────────────────────────────────────────────────


def _raw_unserializer():
    """감싸기 이전의 xdist 원본 되살리기 — 컨트롤러에선 감싸개가 원본을 들고 있다."""
    current = workermanage.unserialize_warning_message
    return getattr(current, "_pm_original", current)


def _warning_message(warning_class, source: Path) -> warnings.WarningMessage:
    return warnings.WarningMessage(
        message=warning_class("git 보장이 사라져 강등"),
        category=warning_class,
        filename=str(source),
        lineno=1,
    )


@pytest.mark.parametrize("class_name", sorted(_ENGINE_WARNING_SOURCES))
def test_xdist_round_trips_engine_warning(class_name):
    """워커 직렬화 → 컨트롤러 역직렬화가 엔진 경고 클래스를 그대로 되살린다."""
    source = _ENGINE_WARNING_SOURCES[class_name]
    warning_class = getattr(importlib.import_module(_synthetic_name(source)), class_name)

    restored = _raw_unserializer()(
        serialize_warning_message(_warning_message(warning_class, source)))

    assert restored.category is warning_class
    assert isinstance(restored.message, warning_class)
    assert str(restored.message) == "git 보장이 사라져 강등"


def test_xdist_round_trip_breaks_without_the_finder():
    """finder 와 캐시를 걷어내면 같은 왕복이 ModuleNotFoundError — 가드가 제 표면을 덮는다."""
    source = _ENGINE_WARNING_SOURCES["RepoFilesFallbackWarning"]
    name = _synthetic_name(source)
    warning_class = getattr(
        importlib.import_module(name), "RepoFilesFallbackWarning")
    data = serialize_warning_message(_warning_message(warning_class, source))

    installed = [
        finder for finder in sys.meta_path
        if isinstance(finder, test_config.EngineSyntheticModuleFinder)
    ]
    assert installed, "conftest 가 finder 를 meta_path 에 꽂지 않았다"
    forgotten = _forget_synthetic_modules()
    for finder in installed:
        sys.meta_path.remove(finder)
    try:
        with pytest.raises(ModuleNotFoundError) as raised:
            _raw_unserializer()(data)
    finally:
        for finder in reversed(installed):
            sys.meta_path.insert(0, finder)
        sys.modules.update(forgotten)

    assert "_project_manager_" in str(raised.value)


def _ad_hoc_name_warning_data() -> dict:
    """컨트롤러에서 해소되지 않는 사본 이름을 가진 경고 데이터 (테스트 사본 로드 재현)."""
    data = serialize_warning_message(
        _warning_message(_AdHocCopyWarning, REPO / "tests" / "test_regression_parallel.py"))
    return dict(
        data, message_module="file_lock_under_test", category_module="file_lock_under_test")


def test_unresolvable_warning_name_degrades_instead_of_killing_the_run():
    """컨트롤러가 못 푸는 이름은 원 정체를 본문에 담은 일반 Warning 으로 낮춰 받는다."""
    data = _ad_hoc_name_warning_data()

    restored = test_config.tolerant_warning_unserializer(_raw_unserializer())(data)

    # 되살리기가 성공했다면 category 는 원래 클래스다 — Warning 이라는 것이 낮춰 받았다는 증거다.
    assert restored.category is Warning
    assert isinstance(restored.message, Warning)
    assert "file_lock_under_test._AdHocCopyWarning" in str(restored.message)
    assert "git 보장이 사라져 강등" in str(restored.message)
    assert restored.filename == data["filename"]


def test_raw_xdist_dies_on_the_unresolvable_warning_name():
    """감싸지 않은 xdist 되살리기는 같은 데이터에서 ModuleNotFoundError — 감싸기의 대상 표면."""
    with pytest.raises(ModuleNotFoundError):
        _raw_unserializer()(_ad_hoc_name_warning_data())


def test_tolerant_unserializer_is_installed_on_the_controller_only(request):
    """되살리기를 실제로 하는 컨트롤러에만 감싸기가 서고, 워커는 원본 그대로 둔다."""
    installed = getattr(workermanage.unserialize_warning_message, "_pm_tolerant", False)

    if hasattr(request.config, "workerinput"):
        assert not installed
    else:
        assert installed


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

            REPO_FILES_SOURCE = Path({str(_ENGINE_WARNING_SOURCES["RepoFilesFallbackWarning"])!r})
            FILE_LOCK_SOURCE = Path({str(_ENGINE_WARNING_SOURCES["LocklessFallbackWarning"])!r})
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
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-n", "2", str(root)],
        cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace",
        stdin=subprocess.DEVNULL, timeout=NESTED_RUN_TIMEOUT_SECONDS)


def test_parallel_run_survives_worker_warnings(tmp_path):
    """conftest 가드가 있으면 `-n 2` 실행이 두 경고를 다 받고도 rc0 으로 끝난다."""
    root = tmp_path / "with-guards"
    _write_probe_suite(root, with_guards=True)

    result = _run_parallel_probe(root)

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "INTERNALERROR" not in output
    # 경고 자체는 숨기지 않는다 — 두 건 모두 컨트롤러 요약에 남는다.
    assert "합성 모듈명 프로브" in output
    assert "사본 이름 프로브" in output


def test_parallel_run_without_guards_dies_on_the_same_warnings(tmp_path):
    """대조군 — 가드 없는 같은 스위트는 컨트롤러 INTERNALERROR 로 죽는다(가드 표면 확인)."""
    root = tmp_path / "without-guards"
    _write_probe_suite(root, with_guards=False)

    result = _run_parallel_probe(root)

    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "INTERNALERROR" in output
    assert "ModuleNotFoundError" in output
