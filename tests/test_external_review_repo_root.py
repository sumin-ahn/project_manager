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


# ── 빈-diff fail-loud 가드 (T-0326 — adopter#0 false-green 원천 차단) ──────────
# adopter#0/worktree 형상에서 REPO 앵커가 PM 홈을 가리키면 실 변경은 worktree 에 있어
# `git diff` 가 비고, codex 가 "변경 없음"을 통과로 판정해 가짜 통과(false-green)가 난다
# (PM 65 실측). main() 이 diff 추출 직후·codex 호출 전에 빈/공백-only diff 를 무조건 fail
# (비-0 exit)하고 외부 리뷰어를 호출하지 않음을 hermetic 하게 단언한다. extract_diff·run_review
# 를 module-level 로 monkeypatch 해 실제 git/codex 없이 diff 를 주입한다(--force 로 활성화
# 게이트 우회 → 가드가 유일한 차단 지점임을 보장).


def _run_main_with_diff(external, monkeypatch, diff: str):
    """main() 을 실행하되 diff 는 주입하고 외부 리뷰어(run_review) 호출 여부를 기록한다.

    반환: (exit_code, reviewer_called). --paths 로 ticket 파싱을 건너뛰고 --force 로 활성화
    게이트를 우회한다 — 빈-diff 가드가 codex 호출 전에 유일하게 차단함을 격리한다."""
    # extract_diff 는 (diff, 제외 경로 목록) 튜플 반환 (T-0428) — 제외 없음(빈 목록)으로 주입.
    monkeypatch.setattr(external, "extract_diff", lambda *a, **k: (diff, []))
    # conf 도 주입한다 — 주입하지 않으면 이 실행이 **개발자 트리의 실 local.conf** 를 읽어,
    # 그 파일의 상태(구표기 잔존 등)가 이 절의 판정을 좌우한다(hermetic 아님).
    monkeypatch.setattr(external, "local_config", lambda repo=None: {
        "additional_reviewer.enabled": "true",
        "additional_reviewer.harness": "codex",
        "additional_reviewer.model": "gpt-5.6-sol",
    })
    called = {"reviewer": False}

    def _fake_run_review(*args, **kwargs):
        called["reviewer"] = True
        return {
            "reviewer": "x", "ok": True, "output": "판정: 통과",
            "verdict": {"has_must_fix": False, "has_pass": True},
            "file": None, "failed": False, "any_must_fix": False, "all_pass": True,
        }

    monkeypatch.setattr(external, "run_review", _fake_run_review)
    exit_code = external.main(["--paths", "foo.py", "--force", "--no-gate"])
    return exit_code, called["reviewer"]


def test_main_empty_diff_fails_loud_before_reviewer(external, monkeypatch, capsys):
    """빈 diff → 비-0 exit + 외부 리뷰어 미호출 + 원인/조치 안내(false-green 차단)."""
    exit_code, reviewer_called = _run_main_with_diff(external, monkeypatch, "")
    assert exit_code != 0
    assert reviewer_called is False  # codex subprocess 미호출 단언 (DoD)
    err = capsys.readouterr().err
    assert "리뷰할 diff 가 없습니다" in err
    assert "--paths" in err       # worktree canonical 형상 안내
    assert "git add" in err       # untracked-only 안내 (stage-before-external-review)


def test_main_whitespace_only_diff_fails_loud(external, monkeypatch, capsys):
    """공백-only diff → 빈 diff 와 동일하게 무조건 fail (strip() 후 비면 리뷰 무의미)."""
    exit_code, reviewer_called = _run_main_with_diff(external, monkeypatch, "  \n\t\n  ")
    assert exit_code != 0
    assert reviewer_called is False
    assert "리뷰할 diff 가 없습니다" in capsys.readouterr().err


def test_main_nonempty_diff_invokes_reviewer(external, monkeypatch, capsys):
    """비어있지 않은 diff → 가드 통과·외부 리뷰어 호출·기존 동작 불변(통과→exit 0)."""
    diff = "diff --git a/foo.py b/foo.py\n@@ -1 +1 @@\n-old\n+new\n"
    exit_code, reviewer_called = _run_main_with_diff(external, monkeypatch, diff)
    assert reviewer_called is True  # 가드가 정상 diff 를 막지 않음
    assert exit_code == 0           # 통과 판정 → 기존 exit 규약 불변
    assert "리뷰할 diff 가 없습니다" not in capsys.readouterr().err
