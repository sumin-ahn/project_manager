"""`~`/`~user` 확장 규칙의 **사본 대조** — board 와 pm_config 가 같은 판정을 낸다 (T-0712 축 C).

없는 사용자(`~ghost/…`)의 확장은 플랫폼마다 다르게 끝난다: POSIX 는 `pwd` 조회 실패를
RuntimeError 로 올리고, Windows 는 실재 확인 없이 `C:\\Users\\<name>` 을 조립해 준다. 그래서
"해소 실패"에서 멈춰야 하는 경계가 Windows 에서만 열렸다 — 두 엔진 모듈이 각자 그 자리를 막았고
(`pm_config._expanded_user_path` = 보호 훅 설치 경로 · `board._expanded_user_path` = 소유 repo
freshness 해소·`core.hooksPath` 확장), **호출로 합칠 수는 없다**: board 는 pm_config 를 로드하지
않는다(의존 방향이 pm_config → board 다).

그래서 이 파일이 두 사본의 **행동 동일성**을 지킨다. 한쪽만 고치면 여기서 갈린다.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

# 실재하지 않아야 하는 사용자명 — 두 구현 모두 "해소 실패"로 끝내야 하는 입력.
GHOST_USER = "pm_user_that_must_not_exist_0712"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def modules():
    return _load("board"), _load("pm_config")


def _posix_expanduser(path: Path) -> Path:
    """POSIX `Path.expanduser()` 대역 — 없는 사용자는 RuntimeError."""
    first = path.parts[0] if path.parts else ""
    if first.startswith("~") and len(first) > 1:
        raise RuntimeError(f"Could not determine home directory for {first!r}")
    return Path("/home/pmuser") / Path(*path.parts[1:]) if first == "~" else path


def _windows_expanduser(path: Path) -> Path:
    """Windows `Path.expanduser()` 대역 — 없는 사용자도 `C:/Users/<name>` 으로 조립한다."""
    first = path.parts[0] if path.parts else ""
    if not first.startswith("~"):
        return path
    return Path("C:/Users") / (first[1:] or "pmuser") / Path(*path.parts[1:])


def _verdict(module, monkeypatch, expanduser, value: str) -> str:
    """그 구현의 판정을 비교 가능한 문자열로 — `실패` 또는 `ok:<확장 결과>`."""
    monkeypatch.setattr(module, "_expanduser_path", expanduser)
    try:
        return f"ok:{module._expanded_user_path(value).as_posix()}"
    except RuntimeError:
        return "실패"


CASES = [
    f"~{GHOST_USER}/owner",     # 없는 사용자 — 두 플랫폼 모두 해소 실패여야 한다.
    f"~{GHOST_USER}",           # 마디 하나짜리 같은 축
    "~/owner",                  # 현재 사용자 — 실재 검사 대상 아님(정상 확장)
    "~",
    "/abs/owner",               # `~` 무관 경로 — 그대로 통과
    "relative/owner",
]


@pytest.mark.parametrize("value", CASES)
@pytest.mark.parametrize("platform, expanduser",
                         [("posix", _posix_expanduser), ("windows", _windows_expanduser)])
def test_board_and_pm_config_expand_user_paths_identically(
        modules, monkeypatch, value, platform, expanduser):
    """같은 입력·같은 확장 동작이면 두 사본의 판정이 값까지 같다 (사본 분기 차단)."""
    board, pm_config = modules
    assert (_verdict(board, monkeypatch, expanduser, value)
            == _verdict(pm_config, monkeypatch, expanduser, value))


@pytest.mark.parametrize("platform, expanduser",
                         [("posix", _posix_expanduser), ("windows", _windows_expanduser)])
def test_a_missing_user_home_fails_on_both_platforms(modules, monkeypatch, platform,
                                                     expanduser):
    """규칙 자체의 값 단언 — 없는 `~user` 는 플랫폼과 무관하게 해소 실패다.

    동일성만 보면 두 사본이 **같이 틀려도** 통과한다([[guard-must-cover-its-own-surface]]).
    """
    for module in modules:
        assert _verdict(module, monkeypatch, expanduser, f"~{GHOST_USER}/owner") == "실패"


@pytest.mark.parametrize("platform, expanduser",
                         [("posix", _posix_expanduser), ("windows", _windows_expanduser)])
def test_the_current_user_home_is_not_existence_checked(modules, monkeypatch, platform,
                                                        expanduser):
    """`~`(현재 사용자)에는 실재 검사를 걸지 않는다 — 홈이 아직 없는 정상 형상 과차단 금지."""
    for module in modules:
        assert _verdict(module, monkeypatch, expanduser, "~/owner").startswith("ok:")
