"""external_review REPO 앵커 — 상향 탐색(`.project_manager` 마커) 해소 가드 (T-0242·ADR-0033 ①).

finance_dev 제보 D2: 하드코딩 `REPO = Path(__file__).resolve().parents[2]` 는 tools 가
`<root>/.project_manager/tools/` 정확히 2단 깊이라고 가정한다 — 채택자 형상(PM 홈/worktree
구조 상이·다른 깊이)에선 어긋난다. `_find_repo_root()` 가 부모 체인을 상향 탐색해 `.project_manager`
를 품은 첫(최근접) 조상을 REPO 로 해소하고, 마커 부재 시 현행 `parents[2]` 로 폴백함을 hermetic
하게 단언한다 (board.py `board_root()` graceful 탐지 동형·존재할 때만 갈리고 없으면 현 위치 폴백).

hermetic seam: `_find_repo_root()` 는 모듈 전역 `__file__` 을 읽으므로, fresh 모듈 인스턴스의
`__file__` 을 tmp 합성 경로로 monkeypatch 해 실제 파일 이동 없이 임의 깊이를 모사한다
(test_board_root_external_tools 의 모듈-레벨 REPO monkeypatch 관용구와 동류).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"


def _load(name: str = "external_review"):
    """도구 모듈을 (패키지 아님) importlib 로 경로 로드 — test_board_root 동일 규약."""
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def external():
    return _load("external_review")


def test_find_repo_root_resolves_project_manager_ancestor(external, tmp_path, monkeypatch):
    """채택자 형상(tools 가 다른 깊이) → REPO == `.project_manager` 를 품은 최근접 조상.

    tools 를 `<root>/.project_manager/tools/nested/` 에 두면 하드코딩 parents[2] 는
    `<root>/.project_manager/tools`(오답)를 준다 — 상향 탐색은 마커로 <root> 를 해소해야 한다."""
    root = tmp_path / "adopter"
    nested = root / ".project_manager" / "tools" / "nested"
    nested.mkdir(parents=True)
    monkeypatch.setattr(external, "__file__", str(nested / "external_review.py"))
    assert external._find_repo_root() == root


def test_find_repo_root_returns_nearest_ancestor(external, tmp_path, monkeypatch):
    """중첩 `.project_manager`(PM 홈 안 worktree) → 최근접 조상을 반환한다(바깥 홈 아님).

    self-host 형상(② 홈 안 ① worktree·ADR-0027)에서 각 도구가 *자기* worktree 로 해소되게
    하는 핵심 — 바깥 홈의 `.project_manager` 를 먼저 만나지 않는다(최근접 우선)."""
    outer = tmp_path / "home"
    inner = outer / "work" / "wt1"
    (outer / ".project_manager").mkdir(parents=True)
    tools = inner / ".project_manager" / "tools"
    tools.mkdir(parents=True)
    monkeypatch.setattr(external, "__file__", str(tools / "external_review.py"))
    assert external._find_repo_root() == inner


def test_find_repo_root_falls_back_to_parents2_when_marker_absent(external, tmp_path, monkeypatch):
    """마커 부재 → 현행 `parents[2]` 폴백(회귀 0·board_root 동형 graceful 폴백)."""
    deep = tmp_path / "a" / "b" / "c" / "d"
    deep.mkdir(parents=True)
    monkeypatch.setattr(external, "__file__", str(deep / "external_review.py"))
    # parents of .../a/b/c/d/external_review.py: [d, c, b, ...] → parents[2] == .../a/b
    assert external._find_repo_root() == tmp_path / "a" / "b"


def test_find_repo_root_framework_shape_matches_parents2(external):
    """프레임워크 형상(현 repo) → 상향 탐색 == 하드코딩 parents[2](불변·additive).

    실 repo 는 `<root>/.project_manager/tools/external_review.py` 정확히 2단이라 마커 해소가
    parents[2] 와 동일해야 한다 — REPO 상수·하위 파생(TICKETS_DIR/LOCAL_CONF 등)이 안 바뀜을 박제."""
    expected = Path(external.__file__).resolve().parents[2]
    assert external._find_repo_root() == expected
    assert external.REPO == expected
