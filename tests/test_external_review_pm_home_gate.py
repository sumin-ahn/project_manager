"""external_review PM 홈 앵커 재지정 게이트 — adopter#0 false-green 능동 차단 (T-0367·ADR-0049).

빈-diff fail-loud 가드(T-0326·`test_external_review_repo_root.py`)는 diff 가 *실제로 빈 뒤* 사후
차단한다. 이 게이트는 그 안내(external_review.py:166)를 **능동 게이트로 승격**한다 — REPO 앵커가
adopter#0 PM 홈(import 사본)을 가리키고 `--paths` override 가 없으면, diff 추출 전에 canonical 코드
worktree 재지정을 안내하며 fail-loud 한다([[adopter0-gates-use-worktree-canonical]]·PM 65).

규율 경로(cwd·paths) 단언은 순수 filesystem 판정이라 hermetic — 외부 codex 실호출(과금·ADR-0004
opt-in) 없이 REPO 앵커를 tmp PM 홈/worktree 형상으로 monkeypatch 해 게이트 분기를 단언한다. stage
선행(git add)·빈-diff 백스톱은 `test_external_review_repo_root.py` 가 이미 커버(직교).

**오탐 0 (codex/reviewer 이중 게이트 수렴 must-fix)**: 재지정 대상은 `work/` 슬롯 스캔으로만 잡고
local.conf `upstream` 은 참조하지 않는다 — upstream 은 URL/무관 로컬 checkout(`pm_import --from
<로컬>` 정규 채택자·T-0053)일 수 있어, 실 board 를 소유한 정규 채택자에서 정상 리뷰를 hard-block
하던 오탐 클래스다(reviewer 실측 재현). 아래 두 축으로 못박는다: (1) foreign/로컬 upstream 채택자
→ 미발화(fail-soft), (2) adopter#0(`work/` 슬롯 실존) → 정확 재지정.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"


def _load(name: str = "external_review"):
    """도구 모듈을 (패키지 아님) importlib 로 경로 로드 — test_external_review_repo_root 동일 규약."""
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def external():
    return _load("external_review")


# ── 형상 fixtures ──────────────────────────────────────────────────────────


def _make_pm_home(tmp_path: Path, *, with_ticket: bool = True,
                  with_worktree: bool = True) -> tuple[Path, Path]:
    """adopter#0 PM 홈 형상을 합성한다 — 실 board(T-*.md) + 중첩 canonical worktree(`work/*`).

    반환: (pm_home, worktree). with_ticket/with_worktree 로 conjunction 개별 결핍을 모사."""
    home = tmp_path / "pm_home"
    (home / ".project_manager" / "board" / "tickets" / "open").mkdir(parents=True)
    if with_ticket:
        (home / ".project_manager" / "board" / "tickets" / "open" / "T-0001-x.md").write_text(
            "---\nid: T-0001\n---\n", encoding="utf-8")
    worktree = home / "work" / "project_manager_1"
    if with_worktree:
        (worktree / ".project_manager" / "tools").mkdir(parents=True)
        (worktree / ".project_manager" / "tools" / "external_review.py").write_text(
            "# engine copy\n", encoding="utf-8")
    return home, worktree


# ── _owns_real_board ──────────────────────────────────────────────────────


def test_owns_real_board_true_with_ticket(external, tmp_path):
    """실 티켓(T-*.md)이 있는 board → True (PM 홈)."""
    home, _ = _make_pm_home(tmp_path)
    assert external._owns_real_board(home / ".project_manager") is True


def test_owns_real_board_false_empty_scaffold(external, tmp_path):
    """빈 scaffold(status 디렉토리만·T-*.md 없음) → False (worktree 출하 형상·오탐 방지)."""
    home, _ = _make_pm_home(tmp_path, with_ticket=False)
    assert external._owns_real_board(home / ".project_manager") is False


def test_owns_real_board_legacy_wiki_tickets(external, tmp_path):
    """board/ 미분리 legacy(`wiki/tickets`)에서도 실 티켓을 인식한다."""
    pm = tmp_path / "legacy" / ".project_manager"
    (pm / "wiki" / "tickets" / "claimed").mkdir(parents=True)
    (pm / "wiki" / "tickets" / "claimed" / "T-0009-y.md").write_text("---\n---\n", encoding="utf-8")
    assert external._owns_real_board(pm) is True


# ── _canonical_worktree (work/ 스캔만 · upstream 미참조) ─────────────────────


def test_canonical_worktree_scans_work_dir(external, tmp_path):
    """`<anchor>/work/*` 스캔 중 엔진 사본 보유 디렉토리를 반환한다 (adopter#0 등록 관례)."""
    home, worktree = _make_pm_home(tmp_path)
    assert external._canonical_worktree(home) == worktree


def test_canonical_worktree_none_when_no_work_slot(external, tmp_path):
    """work/ 슬롯 부재 → None (재지정 대상 없음)."""
    home, _ = _make_pm_home(tmp_path, with_worktree=False)
    assert external._canonical_worktree(home) is None


def test_canonical_worktree_ignores_upstream_foreign_checkout(external, tmp_path):
    """무관한 로컬 checkout(엔진 보유·local.conf upstream 대상)을 재지정에 쓰지 않는다 (오탐 0).

    reviewer 실측 재현: `pm_import --from <로컬>` 정규 채택자의 upstream 은 URL/무관 로컬 checkout
    일 수 있다 — 이를 재지정 대상으로 삼으면 정상 리뷰를 stale/무관 checkout 으로 오안내한다.
    `work/` 슬롯이 없으면(정규 채택자·adopter#0 아님) upstream 유무와 무관하게 None."""
    home, _ = _make_pm_home(tmp_path, with_worktree=False)
    foreign = tmp_path / "foreign_checkout"
    (foreign / ".project_manager" / "tools").mkdir(parents=True)
    (foreign / ".project_manager" / "tools" / "external_review.py").write_text(
        "# unrelated checkout\n", encoding="utf-8")
    # foreign 이 존재하고 upstream 이 이를 가리켜도 work/ 슬롯 부재라 canonical=None (upstream 미참조).
    assert external._canonical_worktree(home) is None


# ── _pm_home_reanchor (2중 conjunction) ────────────────────────────────────


def test_reanchor_detects_pm_home(external, tmp_path):
    """실 board + canonical worktree(work/ 슬롯) 둘 다 → 재지정 대상 worktree 반환 (게이트 발화)."""
    home, worktree = _make_pm_home(tmp_path)
    assert external._pm_home_reanchor(home) == worktree


def test_reanchor_none_for_worktree_shape(external, tmp_path):
    """worktree(코드 전용·board 미소유)에서 실행 → None (정상·fail-soft·오탐 0).

    게이트가 실행 위치(worktree)를 PM 홈으로 오인해 정당 실행을 막지 않음을 못박는다."""
    home, worktree = _make_pm_home(tmp_path)
    # worktree 앵커 자신은 실 board 미소유 → conjunction (1) 탈락.
    assert external._pm_home_reanchor(worktree) is None


def test_reanchor_none_when_no_worktree(external, tmp_path):
    """실 board 는 있으나 canonical worktree(work/ 슬롯) 부재(로컬 upstream 정규 채택자) → None.

    conjunction (2) 탈락 — 정규 채택자를 adopter#0 으로 오인해 hard-block 하지 않는다."""
    home, _ = _make_pm_home(tmp_path, with_worktree=False)
    assert external._pm_home_reanchor(home) is None


# ── main() 게이트 (sensitivity: PM 홈 앵커 + paths 미지정 → 차단) ────────────


def _run_main(external, monkeypatch, anchor: Path, conf: dict, argv: list[str]):
    """REPO/local_config 를 monkeypatch 해 main() 을 격리 실행 — 외부 리뷰어는 호출되면 기록.

    반환: (exit_code, reviewer_called). 게이트가 codex 전송 전에 차단함을 격리한다."""
    monkeypatch.setattr(external, "REPO", anchor)
    monkeypatch.setattr(external, "local_config", lambda: conf)
    # extract_diff 는 (diff, 제외 경로 목록) 튜플 반환 (T-0428) — 제외 없음(빈 목록)으로 주입.
    monkeypatch.setattr(external, "extract_diff", lambda *a, **k: ("diff --git a/x b/x\n+y\n", []))
    called = {"reviewer": False}

    def _fake_run_review(*a, **k):
        called["reviewer"] = True
        return {"reviewer": "x", "ok": True, "output": "판정: 통과",
                "verdict": {"has_must_fix": False, "has_pass": True},
                "file": None, "failed": False, "any_must_fix": False, "all_pass": True}

    monkeypatch.setattr(external, "run_review", _fake_run_review)
    return external.main(argv), called["reviewer"]


def test_main_pm_home_no_paths_blocks_loud(external, monkeypatch, tmp_path, capsys):
    """PM 홈 앵커 + `--paths` 미지정 → 비-0 exit + codex 미호출 + worktree 재지정 안내 (게이트 승격).

    non-empty diff 를 주입해도 게이트가 diff 추출 전에 차단함을 단언한다 — 빈-diff 사후 차단(T-0326)이
    아니라 PM 홈 앵커의 *능동* 사전 차단(false-green 원천)."""
    home, worktree = _make_pm_home(tmp_path)
    conf = {"external_review_enabled": "true"}
    exit_code, reviewer_called = _run_main(external, monkeypatch, home, conf, [])
    assert exit_code != 0
    assert reviewer_called is False  # codex subprocess 미호출 (DoD — false-green 전송 차단)
    err = capsys.readouterr().err
    assert "PM 홈" in err
    assert str(worktree) in err       # canonical worktree(work/ 스캔) 재지정 경로 안내
    assert "--paths" in err           # override 안내


def test_main_pm_home_ticket_also_blocks(external, monkeypatch, tmp_path, capsys):
    """`--ticket` 도 `--paths` override 가 아니므로 PM 홈 앵커에서 동일 차단된다.

    --ticket touches 는 PM 홈 git 기준 상대경로라 여전히 빈 diff → false-green. --paths 만이
    deliberate override."""
    home, worktree = _make_pm_home(tmp_path)
    exit_code, reviewer_called = _run_main(external, monkeypatch, home, {}, ["--ticket", "T-0001"])
    assert exit_code != 0
    assert reviewer_called is False
    assert "PM 홈" in capsys.readouterr().err


def test_main_pm_home_with_paths_passes_gate(external, monkeypatch, tmp_path, capsys):
    """PM 홈 앵커라도 `--paths` 명시 → 게이트 통과(deliberate override)·리뷰어까지 진행.

    override 는 escape hatch — 명시 시 게이트가 막지 않음을 못박는다(빈 diff 면 빈-diff 가드가 백스톱)."""
    home, worktree = _make_pm_home(tmp_path)
    conf = {"external_review_enabled": "true"}
    exit_code, reviewer_called = _run_main(
        external, monkeypatch, home, conf, ["--paths", ".project_manager/tools/"])
    assert reviewer_called is True    # 게이트 미차단 → 리뷰어 진행
    assert exit_code == 0
    assert "PM 홈" not in capsys.readouterr().err


def test_main_worktree_shape_no_block(external, monkeypatch, tmp_path, capsys):
    """worktree(코드 전용) 앵커 + paths 미지정 → 게이트 미발화·정상 진행 (fail-soft·오탐 0).

    canonical worktree 에서 실행하는 것이 *정답*이므로 게이트가 이를 막으면 안 된다."""
    home, worktree = _make_pm_home(tmp_path)
    conf = {"external_review_enabled": "true"}
    # worktree 앵커: 실 board 미소유 → 게이트 미발화.
    exit_code, reviewer_called = _run_main(external, monkeypatch, worktree, conf, [])
    assert reviewer_called is True
    assert exit_code == 0
    assert "PM 홈" not in capsys.readouterr().err


def test_main_local_upstream_adopter_ticket_not_blocked(external, monkeypatch, tmp_path, capsys):
    """정규 로컬-upstream 채택자(실 board·work/ 슬롯 없음) + `--ticket` + non-empty diff → 미차단.

    reviewer 실증 재현(오탐 클래스): upstream 을 재지정에 쓰면 이 정상 리뷰를 hard-block 했다(DoD
    오탐 0 위반·빈-diff 백스톱도 실 diff non-empty 라 무력). work/ 스캔만 쓰므로 canonical=None →
    fail-soft 로 리뷰어까지 정상 진행한다."""
    home, _ = _make_pm_home(tmp_path, with_worktree=False)  # 실 board 소유·work/ 슬롯 없음
    # ticket touches 해소를 결정론화(실 board 의 T-0001 은 touches 부재라 기본경로 폴백해도 무방).
    monkeypatch.setattr(external, "parse_ticket_touches", lambda t: [".project_manager/tools/"])
    conf = {"upstream": str(tmp_path / "foreign"), "external_review_enabled": "true"}
    exit_code, reviewer_called = _run_main(external, monkeypatch, home, conf, ["--ticket", "T-0001"])
    assert reviewer_called is True   # 게이트 미차단 → 리뷰어 진행 (오탐 0)
    assert exit_code == 0
    assert "PM 홈" not in capsys.readouterr().err
