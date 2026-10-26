"""additional_reviewer REPO 앵커 — 설치 깊이 고정 해소 가드.

도구는 언제나 `<root>/.project_manager/tools/` 에 설치된다 — 설치 경로를 만드는
`pm_import` 가 그 깊이를 못 박으므로 다른 깊이는 나올 수 없다. 그래서 REPO 는 상향 탐색이
아니라 `Path(__file__).resolve().parents[2]` 다. 상향 탐색은 합성 트리에서 *자기 위의 실
인스턴스*를 답으로 주는 부작용만 냈다(실측: 실 PM 홈 log 오염 · 등록 안 된 worktree 가
등록으로 판정).

진입 파일의 REPO 식은 R5 문법(tests/test_engine_structure.py)이 고정하고, CLI 본체는
`pm_engine.cli.additional_reviewer` 가 넘겨받은 Home 으로 돈다.
"""
from __future__ import annotations

from pm_engine.infra import review_diff
from pm_engine.app import reviewer_run

from pm_engine.infra import conf_store

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"


def _load(name: str = "additional_reviewer"):
    """도구 모듈을 (패키지 아님) importlib 로 경로 로드 — test_board_root 동일 규약."""
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def external():
    return _load("additional_reviewer")


def test_repo_constant_anchors_at_the_install_depth(external):
    """모듈 상수 REPO 는 도구 **자기 위치**에서 나온다 — 하위 파생 경로가 함께 따라온다.

    실 repo 는 `<root>/.project_manager/tools/additional_reviewer.py` 정확히 2단이다.
    REPO 상수·하위 파생(TICKETS_DIR/LOCAL_CONF 등)이 그 앵커를 쓰는지 박제한다."""
    expected = Path(external.__file__).resolve().parents[2]
    assert external.REPO == expected


# ── 빈-diff fail-loud 가드 (T-0326 — adopter#0 false-green 원천 차단) ──────────
# adopter#0/worktree 형상에서 REPO 앵커가 PM 홈을 가리키면 실 변경은 worktree 에 있어
# `git diff` 가 비고, codex 가 "변경 없음"을 통과로 판정해 가짜 통과(false-green)가 난다
# (PM 65 실측). main() 이 diff 추출 직후·codex 호출 전에 빈/공백-only diff 를 무조건 fail
# (비-0 exit)하고 추가 리뷰어를 호출하지 않음을 hermetic 하게 단언한다. extract_diff·run_review
# 를 module-level 로 monkeypatch 해 실제 git/codex 없이 diff 를 주입한다(--force 로 활성화
# 게이트 우회 → 가드가 유일한 차단 지점임을 보장).


def _run_main_with_diff(external, monkeypatch, diff: str):
    """main() 을 실행하되 diff 는 주입하고 추가 리뷰어(run_review) 호출 여부를 기록한다.

    반환: (exit_code, reviewer_called). --paths 로 ticket 파싱을 건너뛰고 --force 로 활성화
    게이트를 우회한다 — 빈-diff 가드가 codex 호출 전에 유일하게 차단함을 격리한다."""
    # 추출 대역은 본문과 필수 좌표를 함께 제공한다.
    monkeypatch.setattr(
        review_diff, "extract_diff",
        lambda *a, **k: review_diff.ReviewDiff(
            diff,
            'HEAD',
            'INDEX/WORKTREE (untracked 포함)',
            ('x.py',),
            (('git', '-C', str(external.REPO), 'diff', '--', 'x.py'),),
        ),
    )
    # conf 도 주입한다 — 주입하지 않으면 이 실행이 **개발자 트리의 실 local.conf** 를 읽어,
    # 그 파일의 상태(구표기 잔존 등)가 이 절의 판정을 좌우한다(hermetic 아님).
    monkeypatch.setattr(conf_store, 'local_config', lambda _home, repo=None: {
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

    monkeypatch.setattr(reviewer_run, "run_review", _fake_run_review)
    exit_code = external.main(["--paths", "foo.py", "--no-gate"])
    return exit_code, called["reviewer"]


def test_main_empty_diff_fails_loud_before_reviewer(external, monkeypatch, capsys):
    """빈 diff → 비-0 exit + 추가 리뷰어 미호출 + 원인/조치 안내(false-green 차단)."""
    exit_code, reviewer_called = _run_main_with_diff(external, monkeypatch, "")
    assert exit_code != 0
    assert reviewer_called is False  # codex subprocess 미호출 단언 (DoD)
    err = capsys.readouterr().err
    assert "리뷰할 diff 가 없습니다" in err
    assert "--paths" in err       # worktree canonical 형상 안내
    assert "git add" in err       # untracked-only 안내 (stage-before-additional-reviewer)


def test_main_whitespace_only_diff_fails_loud(external, monkeypatch, capsys):
    """공백-only diff → 빈 diff 와 동일하게 무조건 fail (strip() 후 비면 리뷰 무의미)."""
    exit_code, reviewer_called = _run_main_with_diff(external, monkeypatch, "  \n\t\n  ")
    assert exit_code != 0
    assert reviewer_called is False
    assert "리뷰할 diff 가 없습니다" in capsys.readouterr().err


def test_main_nonempty_diff_invokes_reviewer(external, monkeypatch, capsys):
    """비어있지 않은 diff → 가드 통과·추가 리뷰어 호출·기존 동작 불변(통과→exit 0)."""
    diff = "diff --git a/foo.py b/foo.py\n@@ -1 +1 @@\n-old\n+new\n"
    exit_code, reviewer_called = _run_main_with_diff(external, monkeypatch, diff)
    assert reviewer_called is True  # 가드가 정상 diff 를 막지 않음
    assert exit_code == 0           # 통과 판정 → 기존 exit 규약 불변
    assert "리뷰할 diff 가 없습니다" not in capsys.readouterr().err
