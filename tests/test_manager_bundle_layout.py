"""번들 배치 — 번들 안 상대 경로가 소스 상대 경로와 같다 (T-0914 AT-002).

관리자는 번들에서도 자기 위치(`bundle_root()`)를 기준으로 `.project_manager/tools/`·
`templates/`·`manager/ui/dist` 를 소스 트리와 **똑같이** 찾는다. 갈리는 것은 그 기준 한 자리
뿐이고 경로 문자열은 한 벌이다. 그 계약을 `manager/manager.spec` 이 읽는 순수 함수
(`manager.build.bundle_data_entries`)와 실행 시 소비 지점(`bundle_root`·`import_argv`·
`BUNDLED_TOOLS`) 양쪽에서 잰다.

빌드 자체(`npm ci`·`pyinstaller`)는 여기서 돌리지 않는다 — 네트워크·수 분 소요라 CI job 의
성공이 그 축이고, 이 회귀는 `manager/ui/dist` 나 `dist/` 없이 돈다(불변식 4).
"""
from __future__ import annotations

from pm_engine.domain import install_rules

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from manager import BUNDLED_TOOLS, PACKAGE_ROOT, REPO_ROOT, build, runner
from manager.bridge_update import BUILD_INFO_MODULE, bundle_root, import_argv
from pm_engine.infra import upstream_git

REPO = Path(__file__).resolve().parents[1]
SPEC_FILE = REPO / "manager" / "manager.spec"
# 빌드가 만드는 트리 — 번들에는 실리지만 소스 checkout 에는 없을 수 있다(git-ignored).
BUILD_PRODUCT_TREES = ("manager/ui/dist",)
# PyInstaller onefile 이 소스를 푸는 자리를 가리키는 이름. frozen 여부의 단일 신호다.
UNPACK_ATTRIBUTE = "_MEIPASS"
# 관리자가 in-process 로 여는 엔진 사본 — 숨은 import 산출을 재는 트리다(templates 는 그 사본).
ENGINE_TREE = ".project_manager/tools"

git_required = pytest.mark.skipif(shutil.which("git") is None, reason="git 바이너리 없음")


@pytest.fixture
def unpacked(tmp_path, monkeypatch) -> Path:
    """frozen 형상 — 번들이 풀린 트리 하나를 세우고 두 표식을 건다."""
    root = tmp_path / "_MEI654321"
    for _source, relative in build.bundle_data_entries(REPO):
        (root / relative).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, UNPACK_ATTRIBUTE, str(root), raising=False)
    return root


def _destinations() -> list[str]:
    return [destination for _source, destination in build.bundle_data_entries(REPO)]


# ── ① 매핑 자체 ──────────────────────────────────────────────────────────────


def test_every_bundled_tree_keeps_its_source_relative_path():
    for source, destination in build.bundle_data_entries(REPO):
        assert Path(source).relative_to(REPO).as_posix() == destination


def test_the_mapping_is_the_declared_list_and_nothing_else():
    assert _destinations() == list(build.BUNDLED_TREES)
    assert len(set(_destinations())) == len(_destinations()), "같은 자리를 두 번 싣는다"


def test_the_source_trees_exist_and_only_the_screen_is_a_build_product():
    missing = [destination for destination in _destinations()
               if destination not in BUILD_PRODUCT_TREES and not (REPO / destination).is_dir()]

    assert missing == []
    assert "manager/ui/dist" in _destinations(), "창이 여는 화면이 번들에 없다"


def test_every_registered_harness_template_is_bundled():
    """하네스가 넷이 되면 여기서 red 다 — 번들은 `pm_import --new` 가 복사할 트리를 다 실어야 한다."""
    template_dirs = install_rules.HARNESS_TEMPLATE_DIRS

    expected = {f"templates/{name}" for names in template_dirs.values() for name in names}
    assert expected <= set(_destinations()), f"{expected - set(_destinations())} 가 번들에 없다"


def test_the_development_leftovers_are_not_bundled():
    """번들은 git 저장소가 아니라 엔진 열거가 filesystem 순회로 강등된다(실측 경고).

    그래서 번들 내용 자체가 곧 출하 인벤토리다 — 개발 잔재는 실을 자리에서 뺀다.
    """
    assert set(build.BUNDLE_EXCLUDES) >= {"__pycache__", "*.pyc", "node_modules"}


# ── ② 번들에서의 소비 지점 ───────────────────────────────────────────────────


def test_the_manager_reads_the_engine_at_the_bundled_relative_path():
    assert BUNDLED_TOOLS.relative_to(REPO_ROOT).as_posix() in _destinations()
    assert (PACKAGE_ROOT / "ui" / "dist").relative_to(REPO_ROOT).as_posix() in _destinations()


def test_bundle_root_is_the_repo_tree_when_not_frozen():
    assert not hasattr(sys, UNPACK_ATTRIBUTE), "이 회귀는 frozen 이 아닌 인터프리터에서 돈다"

    assert bundle_root() == REPO_ROOT == REPO


def test_the_frozen_source_paths_are_the_same_relative_paths(unpacked, tmp_path):
    """frozen 에서도 경로 문자열은 한 벌이다 — 기준만 `_MEIPASS` 로 갈린다."""
    argv = import_argv(tmp_path / "new-project", "claude", "demo",
                       "https://example.invalid/x.git")
    prefix = runner.interpreter(tmp_path / "new-project")
    assert tuple(argv[:len(prefix)]) == prefix
    tool = Path(argv[len(prefix)])
    assert argv[len(prefix) + 1] == "--new"
    source_root = Path(argv[argv.index("--from") + 1])

    assert source_root == bundle_root() == unpacked
    assert tool.relative_to(source_root).as_posix() == ".project_manager/tools/pm_import.py"
    assert tool.parent.relative_to(source_root).as_posix() in _destinations()
    for destination in _destinations():
        assert (source_root / destination).is_dir(), f"번들에 {destination} 자리가 없다"


# ── ③ spec 이 그 매핑을 다시 적지 않는다 ─────────────────────────────────────


def test_the_spec_reads_the_mapping_instead_of_repeating_it():
    text = SPEC_FILE.read_text(encoding="utf-8")

    assert "bundle_data_entries" in text and "executable_stem" in text
    repeated = [tree for tree in build.BUNDLED_TREES if tree in text]
    assert repeated == [], f"spec 이 번들 경로를 다시 적는다: {repeated}"


def test_the_spec_makes_one_file_from_the_package_entry_script():
    text = SPEC_FILE.read_text(encoding="utf-8")

    assert "ENTRY_SCRIPT" in text
    assert build.ENTRY_SCRIPT.is_file()
    assert "COLLECT" not in text, "onefile 이 아니다"


def test_the_imprint_module_is_a_hidden_import_because_nothing_imports_it_statically():
    """`_build_info` 는 이름 문자열로만 열린다 — 정적 분석이 못 보므로 spec 이 이름으로 세운다."""
    assert BUILD_INFO_MODULE in build.hidden_imports(REPO)

    static_importers = [module.name for module in sorted((REPO / "manager").glob("*.py"))
                        if "import _build_info" in module.read_text(encoding="utf-8")]
    assert static_importers == []


def test_the_hidden_imports_come_from_the_bundled_engine_files():
    """엔진 트리는 **데이터**로 실려 정적 분석 밖이다 — 그 최상위 import 를 파일에서 뽑는다.

    빠지면 실패 시점이 갈린다: `console_encoding.py`(ctypes·ctypes.wintypes)는 매 기동에
    로드돼 실행 파일이 기동에서 죽고, `pm_import.py`(difflib·secrets·shlex·tomllib)는 첫
    프로젝트 조회에서 로드돼 사용자가 프로젝트를 등록한 뒤에야 드러난다.
    """
    derived = build.bundled_engine_imports(REPO, (ENGINE_TREE,))

    assert {"difflib", "secrets", "shlex", "ctypes", "ctypes.wintypes", "tomllib"} <= set(derived)
    # 이름 후보 중 모듈이 아닌 것(`pathlib.Path`)과 서드파티(`yaml`)는 산출이 아니다 —
    # 실행 파일에 엔진 런타임 의존을 넣지 않는다는 인터프리터 합의의 다른 면이다.
    assert "pathlib.Path" not in derived and "yaml" not in derived
    assert list(derived) == sorted(set(derived))


def test_the_hidden_imports_are_not_typed_by_hand():
    """수기 목록은 반드시 뒤처진다 — build.py 에 stdlib 이름 문자열이 하나도 없다."""
    text = (REPO / "manager" / "build.py").read_text(encoding="utf-8")

    typed = [name for name in build.bundled_engine_imports(REPO, (ENGINE_TREE,))
             if f'"{name}"' in text or f"'{name}'" in text]

    assert typed == [], f"build.py 가 stdlib 이름을 손으로 적는다: {typed}"


def test_the_imprint_module_is_the_only_name_the_derivation_cannot_see():
    imprinted = build.hidden_imports(REPO)

    assert imprinted[-1] == build.IMPRINT_MODULE == BUILD_INFO_MODULE
    assert set(imprinted) - {build.IMPRINT_MODULE} == set(build.bundled_engine_imports(REPO))


def test_the_spec_reads_the_hidden_imports_instead_of_naming_modules():
    text = SPEC_FILE.read_text(encoding="utf-8")

    assert "build.hidden_imports(" in text
    assert "tomllib" not in text and build.IMPRINT_MODULE not in text


# ── ④ 실행 파일 이름 ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(("system", "machine", "expected"), [
    ("Linux", "x86_64", "pm-manager-linux-x86_64"),
    ("Windows", "AMD64", "pm-manager-windows-x86_64.exe"),
    ("Darwin", "arm64", "pm-manager-macos-arm64"),
])
def test_the_three_release_targets_are_named(system, machine, expected):
    assert build.executable_filename(system=system, machine=machine) == expected


def test_the_windows_name_is_the_stem_plus_the_extension_pyinstaller_adds():
    stem = build.executable_stem(system="Windows", machine="AMD64")

    assert stem == "pm-manager-windows-x86_64"
    assert build.executable_filename(system="Windows", machine="AMD64") == stem + ".exe"


@pytest.mark.parametrize(("system", "machine"), [
    ("Plan9", "x86_64"),
    ("Linux", "riscv64"),
])
def test_an_unknown_target_is_not_given_a_made_up_name(system, machine):
    with pytest.raises(ValueError):
        build.executable_stem(system=system, machine=machine)


# ── ⑤ 각인 모듈과 빌드 산출의 자리 ───────────────────────────────────────────


_IMPRINT_FIXED_URL = "https://fixed.example.invalid/x/y.git"

# 단독 실행된 `build.py`(관리자 package 가 import 경로에 없는 상태)의 각인 — origin 조회 자리(git)만
#   고정 URL 대역으로 바꾸고 나머지는 실물이다. 마지막 줄에 엔진 모듈이 적재된 파일을 낸다.
_STANDALONE_IMPRINT = """
import json, shutil, subprocess, sys, types, importlib.util
from pathlib import Path
spec = importlib.util.spec_from_file_location("build_standalone", sys.argv[1])
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)
before = sorted(name for name in ("manager", "pm_engine") if name in sys.modules)
build.BUILD_INFO_FILE = Path(sys.argv[2])
def fixed_origin(argv, **kwargs):
    assert list(argv[-3:]) == ["remote", "get-url", "origin"], argv
    return types.SimpleNamespace(returncode=0, stdout=sys.argv[3] + "\\n", stderr="")
shutil.which = lambda _name: "fixture-git"
subprocess.run = fixed_origin
build.imprint_build_info()
from pm_engine.infra import upstream_git
print(json.dumps({"before": before, "engine": upstream_git.__file__}))
"""


def test_the_imprint_module_carries_the_build_checkout_upstream_and_nothing_else(
        tmp_path, monkeypatch):
    """각인은 읽는 곳이 있는 값 하나다 — 관리자 버전 표기는 번들 엔진 rev 가 낸다."""
    namespace: dict = {}
    exec(build.build_info_source("git@example.invalid:x/y.git"), namespace)

    assert namespace["UPSTREAM_URL"] == "git@example.invalid:x/y.git"
    assert [name for name in namespace if not name.startswith("__")] == ["UPSTREAM_URL"]

    # 실제 각인 함수 — origin 은 엔진 seam(upstream_git)으로 읽고 임시 각인 파일에 그 값 하나를 쓴다.
    imprint = tmp_path / "in_process_build_info.py"
    monkeypatch.setattr(build, "BUILD_INFO_FILE", imprint)
    monkeypatch.setattr(upstream_git, "_real_upstream_git_runner",
                        lambda: lambda argv: (0, _IMPRINT_FIXED_URL + "\n"))
    assert build.imprint_build_info() == imprint
    written: dict = {}
    exec(imprint.read_text(encoding="utf-8"), written)
    assert written["UPSTREAM_URL"] == _IMPRINT_FIXED_URL

    # 단독 실행 — 관리자 package 초기화가 번들 엔진 tools 를 import 경로에 올린 뒤 엔진을 읽는다.
    standalone = tmp_path / "standalone_build_info.py"
    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-c", _STANDALONE_IMPRINT,
         str(PACKAGE_ROOT / "build.py"), str(standalone), _IMPRINT_FIXED_URL],
        capture_output=True, text=True, encoding="utf-8", check=False)
    assert completed.returncode == 0, completed.stderr
    observed = json.loads(completed.stdout.strip().splitlines()[-1])
    assert observed["before"] == [], "단독 실행 전제 붕괴 — 관리자/엔진 package 가 이미 적재됐다"
    assert Path(observed["engine"]) == BUNDLED_TOOLS / "pm_engine" / "infra" / "upstream_git.py"
    written = {}
    exec(standalone.read_text(encoding="utf-8"), written)
    assert written["UPSTREAM_URL"] == _IMPRINT_FIXED_URL


@git_required
@pytest.mark.parametrize("path", [
    "manager/_build_info.py",
    "manager/ui/dist/index.html",
    "dist/pm-manager-linux-x86_64",
])
def test_build_products_stay_out_of_git(path):
    """빌드 산출은 릴리즈 절차가 만든다 — git 판정으로 잰다(문자열 대조 아님)."""
    ignored = subprocess.run(["git", "check-ignore", "-q", path], cwd=str(REPO),
                             capture_output=True, text=True, encoding="utf-8")
    tracked = subprocess.run(["git", "ls-files", "--error-unmatch", path], cwd=str(REPO),
                             capture_output=True, text=True, encoding="utf-8")

    assert ignored.returncode == 0, f"{path} 가 ignore 대상이 아니다"
    assert tracked.returncode != 0, f"{path} 가 git 에 추적되고 있다"


def test_the_mapping_is_a_pure_function_of_the_root():
    """매핑은 디스크를 보지 않는다 — 없는 루트에도 같은 다섯 쌍을 낸다(빌드 산출 비의존)."""
    entries = build.bundle_data_entries(Path("/없는-루트"))

    assert [destination for _source, destination in entries] == list(build.BUNDLED_TREES)
    assert entries[0][0].startswith(str(Path("/없는-루트")))


# ── ⑥ 창이 여는 것 (T-0921 AT-004) ──────────────────────────────────────────


def test_the_window_opens_the_local_server_address_instead_of_a_file():
    """창도 서버의 손님이다 — 파일 경로·`file://`·주입 인자가 남으면 데이터 경로가 둘이 된다."""
    source = (REPO / "manager" / "window.py").read_text(encoding="utf-8")
    call = source[source.index("webview.create_window("):]
    call = call[:call.index(")")]

    assert "js_api" not in call, "창에 파이썬 묶음을 주입하는 인자가 남아 있다"
    assert "http://127.0.0.1" in source or "self.server.url" in call
    assert "dist_index()" not in call and "file://" not in source
    assert "self.server.start()" in source, "창을 열기 전에 서버를 띄우는 자리가 없다"


def test_the_screen_bundle_is_what_the_server_serves():
    from manager.server import ManagerServer

    server = ManagerServer(object(), port=0)
    try:
        assert server.dist == PACKAGE_ROOT / "ui" / "dist"
        assert server.dist.relative_to(REPO_ROOT).as_posix() in _destinations()
        assert server.url.startswith("http://127.0.0.1:")
    finally:
        server.stop()


def test_the_smoke_run_goes_through_the_same_server_as_the_window():
    """`--smoke` 가 파일을 직접 열면 경로가 둘로 갈린다 — 창 하나를 그대로 쓴다."""
    source = (REPO / "manager" / "smoke.py").read_text(encoding="utf-8")

    assert "window.ManagerWindow(" in source
    assert "create_window" not in source and "dist_index()" not in source


def test_the_python_regression_does_not_need_the_build_toolchain():
    """이 파일은 Node·PyInstaller 없이 돈다(불변식 4) — 규칙만 읽고 빌드는 부르지 않는다."""
    assert "PyInstaller" not in sys.modules
