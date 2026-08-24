"""게이트 회계 자동 유도 — `--ticket` 실행의 조용한 무기록 폐쇄 (T-0626).

`external_review.py` 를 `--ticket`(+`--paths`)만으로 돌리면 리뷰는 정상 전송·과금되면서 라운드
예약·기록·상한 회계가 전부 생략됐다(`_reserve_round_budget` 첫 분기 = `--gate` 미지정 → 상한 대상
밖·무기록). 실측(2026-08-10): 하루 8+ 라운드가 장부에 0건이라, 반려 must-fix 가 릴리즈 차단
표면(`board.py livegate record`)에 도달하지 못했다. 이 파일은 그 함정이 기계로 닫혔음을 단언한다:

  ① 자동 유도 — `--gate` 미지정 `--ticket` 실행은 게이트를 그 티켓으로 유도해 라운드·wave 를
     기록한다(명시 `--gate` 가 항상 우선·유도 사실은 stderr 1줄). 조회(`--rounds-report`)·기록
     (`--resolve-gate`) 면은 유도 대상이 아니다 — 거기서 `--gate` 는 필터·무시 목록이다.
  ② opt-out — 회계 밖 자문 실행은 명시 `--no-gate` 로만 열리고, 무기록·비회계 사실을 loud 로
     표기한다. `--gate` 와 동시 지정은 부작용 0 지점에서 거부한다.
  ③ 회귀 무변경 — 명시 `--gate` 실행과 `--confirm-fix` 회계는 종전 그대로다.

hermetic: REPO 를 tmp 로 monkeypatch 해 장부를 격리하고(형제 `test_external_review.py`·
`test_review_convergence_gate.py` 동일 규약), PM 홈 해소·touches·extract_diff·run_review 를 주입해
실제 git/추가 리뷰어 없이(외부 전송 0·ADR-0004 opt-in) 분기를 단언한다.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

# 유도 고지의 식별 접두 — 무기록 안내가 처방으로 같은 낱말을 쓰므로 접두로 구분한다.
_DERIVED_PREFIX = "게이트 자동 유도: --gate"

TICKET = "T-" + "0626"
OTHER_GATE = "T-" + "0626-gate"
FOLLOW_UP_TICKET = "T-" + "0627"


# 해소 가능한 추가 리뷰어 대상 — 대상은 `harness`+`model` 구조화 키로만 서므로(엔진 기본 커맨드
# 없음) 이 파일의 모든 형상이 그 세트를 깔고 시작한다.
_REVIEWER_TARGET = {
    "additional_reviewer.enabled": "true",
    "additional_reviewer.harness": "codex",
    "additional_reviewer.model": "gpt-5.6-sol",
}


def _load(name: str = "external_review"):
    spec = importlib.util.spec_from_file_location(f"gate_accounting_{name}", TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def external():
    return _load("external_review")


# ── 배선 ─────────────────────────────────────────────────────────────────────


def _answer(must_fix: int) -> str:
    """must-fix N 건을 선언한 리뷰어 응답 (0 이면 '없음' 표기)."""
    items = "- 없음" if must_fix == 0 else "\n".join(
        f"- 결함 {index}" for index in range(1, must_fix + 1)
    )
    return (
        f"판정: {'통과' if must_fix == 0 else '반려'}\n\n"
        f"**must-fix** (반드시 수정):\n{items}\n\n"
        "**suggestion** (권장):\n- 없음\n"
    )


def _result(must_fix: int) -> dict:
    answer = _answer(must_fix)
    return {
        "reviewer": "x", "ok": True, "output": answer, "answer": answer,
        "verdict": {"has_must_fix": must_fix > 0, "has_pass": must_fix == 0},
        "file": None, "failed": False, "started": True,
        "any_must_fix": must_fix > 0, "all_pass": must_fix == 0,
    }


class _Reviewer:
    """run_review 대역 — 호출 순서대로 정해진 must_fix 수를 돌려주고 호출 수를 센다."""

    def __init__(self, series: list[int]):
        self._series = list(series)
        self.calls = 0

    def __call__(self, *args, **kwargs):
        must_fix = self._series[min(self.calls, len(self._series) - 1)]
        self.calls += 1
        return dict(_result(must_fix))


def _wire(external, monkeypatch, tmp_path, *, series: list[int] | None = None, conf=None):
    """main() 을 tmp REPO 로 격리 배선한다 — 반환값은 리뷰어 호출 counter.

    `--ticket` 실행이 타는 상류(소유 PM 홈 해소·touches)도 고정해, 이 파일이 보는 것이 회계 축
    하나가 되게 한다(앵커 해소 회귀는 `test_gate_anchor_resolution.py` 소유)."""
    monkeypatch.setattr(external, "REPO", tmp_path)
    monkeypatch.setattr(
        external, "local_config",
        lambda repo=None: dict(conf) if conf is not None
        else dict(_REVIEWER_TARGET))
    monkeypatch.setattr(
        external, "resolve_pm_home_for_repo", lambda anchor, **kwargs: tmp_path)
    monkeypatch.setattr(external, "parse_ticket_touches", lambda ticket, **kwargs: ["x.py"])
    monkeypatch.setattr(
        external, "extract_diff",
        lambda *a, **k: ("diff --git a/x b/x\n@@ -1 +1 @@\n-o\n+n\n", []))
    monkeypatch.setattr(
        external, "create_reviewer_workspace",
        lambda diff_root, *, base_dir=None, conf=None, source_home=None, denylist=():
        external.ReviewerWorkspace(
            root=tmp_path / "mirror", tree=tmp_path / "tree", home=tmp_path / "home",
            files=1, skipped_unsafe=0, git_repo=True,
        ))
    monkeypatch.setattr(external, "_diff_cap_refusal", lambda *a, **k: None)
    # 이 파일의 스코프는 회계 축 하나다. 리뷰 뒤 게이트 티켓에 external-reviewer 절을 기록하는
    # 회수(T-0696)는 실 보드 왕복이 필요한 별도 축이라 여기서는 격리한다 — 그 회귀는
    # tests/test_external_review_ticket_harvest.py 가 소유한다(run_review stub 과 같은 결).
    monkeypatch.setattr(
        external, "_harvest_external_review_section", lambda *_a, **_k: None,
    )
    reviewer = _Reviewer(series if series is not None else [0])
    monkeypatch.setattr(external, "run_review", reviewer)
    real_main = external.main

    def _isolated_main(argv=None):
        argv = list(argv or [])
        if "--output-dir" not in argv:
            argv += ["--output-dir", str(tmp_path / "raw")]
        return real_main(argv)

    monkeypatch.setattr(external, "main", _isolated_main)
    return reviewer


def _ledger(tmp_path) -> dict:
    path = tmp_path / ".project_manager" / ".local" / "review_rounds.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _write_ledger(project: Path, ledger: dict) -> None:
    path = project / ".project_manager" / ".local" / "review_rounds.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger, ensure_ascii=False), encoding="utf-8")


def _rejected_ledger() -> dict:
    """E2E 조회·처분이 공유할 잔여 must-fix 1건 장부."""
    return {
        TICKET: {
            "count": 1,
            "rounds": [{
                "sequence": 1, "verdict": 1, "must_fix": 1, "suggestions": 0,
                "started_at": "2026-08-10T00:00:00+00:00",
                "target_rev": "sha256:" + "a" * 64,
                "ts": "2026-08-10T00:01:00+00:00",
            }],
        },
    }


def _subprocess_project(tmp_path: Path) -> tuple[Path, Path]:
    """`external_review.py` 실 CLI 프로세스를 tmp PM 홈의 엔진 사본에서 돌린다.

    형제 모듈 목록을 테스트가 재선언하지 않고 tools 집합을 그대로 복사한다. 엔진 seam이 새로
    분리돼도 사본만 낡아 실 CLI 경로가 깨지는 형상을 만들지 않는다.
    """
    project = tmp_path / "subprocess-project"
    tools = project / ".project_manager" / "tools"
    shutil.copytree(
        TOOLS, tools,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    for status in ("open", "claimed", "blocked", "done"):
        (project / ".project_manager" / "wiki" / "tickets" / status).mkdir(
            parents=True, exist_ok=True,
        )
    ticket = (
        project / ".project_manager" / "wiki" / "tickets" / "open"
        / f"{FOLLOW_UP_TICKET}-fixture.md"
    )
    ticket.write_text(
        f"---\nid: {FOLLOW_UP_TICKET}\ntitle: 후속\ntouches:\n- x.py\n---\n",
        encoding="utf-8",
    )
    return project, tools / "external_review.py"


def _run_external_review(project: Path, script: Path, *argv: str) -> subprocess.CompletedProcess:
    """tmp PM 홈에서 실 인자 파싱→`_main`→조회/처분 경로를 실행한다."""
    return subprocess.run(
        [sys.executable, str(script), *argv], cwd=project,
        capture_output=True, text=True, encoding="utf-8", check=False,
    )


def _args(**overrides) -> argparse.Namespace:
    """유도 판정이 읽는 인자면만 담은 Namespace (순수 헬퍼 단언용)."""
    base = {
        "gate": None, "ticket": None, "no_gate": False,
        "rounds_report": False, "resolve_gate": None, "dry_run": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


# ══ ① 자동 유도 ══════════════════════════════════════════════════════════════


def test_ticket_only_run_is_ledgered_under_the_derived_gate(
        external, monkeypatch, tmp_path, capsys):
    """`--ticket` 단독 실행이 게이트 자동 유도로 라운드를 예약·기록한다 (DoD·발단 클래스).

    수정 전엔 같은 실행이 리뷰를 전송·과금하고도 장부에 0건을 남겼다."""
    reviewer = _wire(external, monkeypatch, tmp_path)

    assert external.main(["--ticket", TICKET]) == 0
    assert reviewer.calls == 1
    entry = _ledger(tmp_path)[TICKET]
    assert entry["count"] == 1
    assert [row["must_fix"] for row in entry["rounds"]] == [0]
    assert _DERIVED_PREFIX in capsys.readouterr().err


def test_derived_gate_spends_the_wave_budget_too(external, monkeypatch, tmp_path):
    """유도된 게이트의 라운드는 wave 총예산에서도 차감된다 (회계 축이 갈리지 않는다)."""
    _wire(external, monkeypatch, tmp_path)
    assert external.main(["--ticket", TICKET]) == 0
    assert _ledger(tmp_path)[external.WAVE_SECTION_KEY]["spent"] == 1


def test_derived_gate_hits_the_convergence_cap_like_an_explicit_one(
        external, monkeypatch, tmp_path, capsys):
    """유도된 게이트도 수렴 상한에 걸린다 — `--ticket` 반복이 무한 라운드 우회로가 아니다 (DoD).

    실측 함정의 결과(하루 8+ 라운드)가 기계로 불가능해졌음을 3라운드째 rc 4 로 못박는다."""
    reviewer = _wire(external, monkeypatch, tmp_path, series=[3, 2])
    argv = ["--ticket", TICKET]
    for _ in range(2):                      # 기본 상한 2 (additional_reviewer.rounds_max)
        assert external.main(argv) == 1
    capsys.readouterr()

    assert external.main(argv) == external.EXIT_ROUND_LIMIT_EXCEEDED
    assert reviewer.calls == 2              # 리뷰어 미호출 (외부 전송 0·과금 0)
    err = capsys.readouterr().err
    assert "수렴 게이트 차단" in err
    assert "3 → 2" in err


def test_explicit_gate_wins_over_the_ticket(external, monkeypatch, tmp_path, capsys):
    """명시 `--gate` 는 항상 우선 — 유도하지 않고 고지도 내지 않는다 (기존 호출 무변경)."""
    _wire(external, monkeypatch, tmp_path)

    assert external.main(["--ticket", TICKET, "--gate", OTHER_GATE]) == 0
    ledger = _ledger(tmp_path)
    assert OTHER_GATE in ledger and TICKET not in ledger
    assert _DERIVED_PREFIX not in capsys.readouterr().err


def test_paths_only_real_send_requires_gate_or_explicit_opt_out(
        external, monkeypatch, tmp_path, capsys):
    """`--paths` 단독 실 전송은 회계 선택 누락을 fail-loud 한다 (T-0631 E2E-1)."""
    reviewer = _wire(external, monkeypatch, tmp_path)

    assert external.main(["--paths", "x.py"]) == 1
    assert reviewer.calls == 0
    assert _ledger(tmp_path) == {}
    err = capsys.readouterr().err
    assert "실 전송에는 게이트 회계 지정 또는 명시 opt-out" in err
    assert "--ticket <T-NNNN>" in err and "--gate <게이트>" in err
    assert "--no-gate" in err


def test_real_send_help_examples_choose_gate_accounting(external):
    """도움말의 복사 가능한 실 전송 예시는 게이트 회계 선택 없이 rc=1 경로를 안내하지 않는다."""
    prefix = "python3 .project_manager/tools/external_review.py"
    commands = [
        shlex.split(line.strip())
        for line in external.build_arg_parser().format_help().splitlines()
        if line.strip().startswith(prefix)
    ]
    non_sending_modes = {"--dry-run", "--rounds-report", "--resolve-gate"}
    real_send_commands = [
        command for command in commands
        if not non_sending_modes.intersection(command)
    ]

    assert real_send_commands
    for command in real_send_commands:
        assert {"--ticket", "--gate", "--no-gate"}.intersection(command), command


def test_paths_only_with_no_gate_sends_and_keeps_the_unaccounted_notice(
        external, monkeypatch, tmp_path, capsys):
    """`--paths --no-gate` 실 전송은 허용하되 무기록 고지를 유지한다 (T-0631 E2E-2)."""
    reviewer = _wire(external, monkeypatch, tmp_path)

    assert external.main(["--paths", "x.py", "--no-gate"]) == 0
    assert reviewer.calls == 1
    assert _ledger(tmp_path) == {}
    err = capsys.readouterr().err
    assert "`--no-gate` 명시 opt-out" in err
    assert "이 실행이 전송되면" in err
    assert "라운드 장부에 기록되지 않고" in err


def test_paths_only_dry_run_needs_no_gate(external, monkeypatch, tmp_path, capsys):
    """`--dry-run` 은 미전송 미리보기라 게이트 선택 없이도 통과한다 (T-0631 E2E-3)."""
    reviewer = _wire(external, monkeypatch, tmp_path)

    assert external.main(["--paths", "x.py", "--dry-run"]) == 0
    assert reviewer.calls == 0
    assert _ledger(tmp_path) == {}
    captured = capsys.readouterr()
    assert "[dry-run] 외부 호출 생략" in captured.out
    assert external._GATE_ACCOUNTING_REQUIRED_GUIDANCE not in captured.err


def test_dry_run_with_a_derived_gate_records_nothing(external, monkeypatch, tmp_path, capsys):
    """미리보기는 유도된 게이트로도 부작용 0 이다 — 예약·기록 없음 (dry-run 계약 무변경)."""
    reviewer = _wire(external, monkeypatch, tmp_path)

    assert external.main(["--ticket", TICKET, "--dry-run"]) == 0
    assert reviewer.calls == 0
    assert _ledger(tmp_path) == {}
    assert _DERIVED_PREFIX in capsys.readouterr().err


def test_a_reserved_ledger_key_ticket_is_refused_before_sending(
        external, monkeypatch, tmp_path, capsys):
    """유도한 이름도 예약 키 검사를 지난다 — 장부 예약 키 티켓은 전송 전 rc 1 (크래시 아님)."""
    reviewer = _wire(external, monkeypatch, tmp_path)

    assert external.main(["--ticket", external.WAVE_SECTION_KEY]) == 1
    assert reviewer.calls == 0
    assert _ledger(tmp_path) == {}
    err = capsys.readouterr().err
    assert "예약 키" in err and "--ticket" in err


def test_report_surface_is_not_a_derivation_target(external):
    """조회면의 `--gate` 는 '한 게이트만 보기' 필터라 유도값이 사용자 선택으로 둔갑하면 안 된다."""
    args = _args(ticket=TICKET, rounds_report=True)
    assert external._derive_gate_from_ticket(args) == external.GateDerivation()
    assert args.gate is None


def test_resolve_gate_surface_is_not_a_derivation_target(external):
    """처분 선언면의 `--gate` 는 무시 목록 입력이다 — 유도하면 없던 무시 경고가 뜬다."""
    args = _args(ticket=TICKET, resolve_gate=OTHER_GATE, rounds_report=False)
    assert external._derive_gate_from_ticket(args) == external.GateDerivation()
    assert args.gate is None
    assert "--gate" not in external._resolve_gate_ignored_flags(
        argparse.Namespace(gate=args.gate, no_gate=False, rounds_report=False,
                           confirm_fix=False, ack_wave=False, force=False))


def test_derivation_helper_sets_the_gate_and_returns_one_line(external, capsys):
    """유도는 게이트를 티켓 값으로 세우고 고지 **문구를 돌려준다** — 출력 자리는 호출부가 정한다.

    헬퍼가 직접 찍으면 stderr 첫 줄이 config provenance 라는 계약을 이 고지가 밀어낸다."""
    args = _args(ticket=TICKET)
    derivation = external._derive_gate_from_ticket(args)
    assert args.gate == TICKET
    assert derivation.refusal is None
    assert derivation.notice.startswith(_DERIVED_PREFIX)
    assert "\n" not in derivation.notice                    # 정확히 1줄
    assert capsys.readouterr().err == ""                    # 헬퍼는 아무것도 찍지 않는다


def test_derivation_notice_follows_the_provenance_first_line(
        external, monkeypatch, tmp_path, capsys):
    """유도 고지는 stderr **첫 줄 provenance 다음**에 온다 (첫 줄 계약·SKILL 안내 보존)."""
    _wire(external, monkeypatch, tmp_path)

    assert external.main(["--ticket", TICKET]) == 0
    lines = [line for line in capsys.readouterr().err.splitlines() if line.strip()]
    assert lines[0].startswith("[external-review] config provenance:")
    assert lines[1].startswith(_DERIVED_PREFIX)


def test_empty_diff_does_not_claim_the_derived_gate_was_ledgered(
        external, monkeypatch, tmp_path, capsys):
    """빈 diff 조기 종료는 예약·기록 전이라 자동 유도 고지도 전송 조건형으로만 남는다."""
    reviewer = _wire(external, monkeypatch, tmp_path)
    monkeypatch.setattr(external, "extract_diff", lambda *a, **k: ("", []))

    assert external.main(["--ticket", TICKET]) == 1
    assert reviewer.calls == 0
    assert _ledger(tmp_path) == {}
    err = capsys.readouterr().err
    assert _DERIVED_PREFIX in err
    assert "이 실행이 전송되면" in err
    assert "라운드 회계가 이 게이트에 붙습니다" not in err


def test_dry_run_notice_does_not_claim_this_run_is_ledgered(
        external, monkeypatch, tmp_path, capsys):
    """미리보기 고지는 확정형이 아니다 — 예약도 기록도 없는 실행이 '기록된다'고 말하지 않는다."""
    _wire(external, monkeypatch, tmp_path)

    assert external.main(["--ticket", TICKET, "--dry-run"]) == 0
    err = capsys.readouterr().err
    assert "미리보기라 이번 실행은 기록·집계하지 않고" in err
    assert "라운드 회계가 이 게이트에 붙습니다). 회계 밖" not in err   # 실행판 확정 문구


# ══ ② opt-out (`--no-gate`) ═════════════════════════════════════════════════


def test_no_gate_opts_out_of_the_ledger_with_a_loud_notice(
        external, monkeypatch, tmp_path, capsys):
    """`--no-gate` 는 유도를 끄고 회계 밖으로 나간다 — 무기록·비회계를 loud 로 표기한다 (DoD)."""
    reviewer = _wire(external, monkeypatch, tmp_path)

    assert external.main(["--ticket", TICKET, "--no-gate"]) == 0
    assert reviewer.calls == 1                    # 리뷰 자체는 정상 실행
    assert _ledger(tmp_path) == {}                # 장부는 만들지 않는다
    err = capsys.readouterr().err
    assert "`--no-gate` 명시 opt-out" in err
    assert "라운드·wave 예산도 쓰지 않습니다" in err
    assert "livegate record" in err               # 릴리즈 차단이 못 본다는 사실까지
    assert _DERIVED_PREFIX not in err


def test_no_gate_with_an_explicit_gate_is_refused_before_sending(
        external, monkeypatch, tmp_path, capsys):
    """`--gate` + `--no-gate` 는 모순이라 부작용 0 지점에서 거부한다 (경고-만-실행 금지)."""
    reviewer = _wire(external, monkeypatch, tmp_path)

    assert external.main(["--gate", OTHER_GATE, "--no-gate", "--paths", "x.py"]) == 1
    assert reviewer.calls == 0
    assert _ledger(tmp_path) == {}
    err = capsys.readouterr().err
    assert "함께 쓸 수 없습니다" in err and OTHER_GATE in err


def test_no_gate_leaves_confirm_fix_without_a_gate(external, monkeypatch, tmp_path, capsys):
    """opt-out 실행의 `--confirm-fix` 는 게이트가 없어 거부된다 (1회 회계는 장부가 소유)."""
    reviewer = _wire(external, monkeypatch, tmp_path)

    assert external.main(["--ticket", TICKET, "--no-gate", "--confirm-fix"]) == 1
    assert reviewer.calls == 0
    assert _ledger(tmp_path) == {}
    assert "게이트당 1회" in capsys.readouterr().err


# ══ ③ 기존 동작 회귀 무변경 ═════════════════════════════════════════════════


def test_explicit_gate_run_is_unchanged(external, monkeypatch, tmp_path, capsys):
    """명시 `--gate` 실행은 종전 그대로 기록되고, 새 고지·경고를 얹지 않는다 (DoD)."""
    _wire(external, monkeypatch, tmp_path)

    assert external.main(["--gate", OTHER_GATE, "--paths", "x.py"]) == 0
    assert _ledger(tmp_path)[OTHER_GATE]["count"] == 1
    err = capsys.readouterr().err
    assert _DERIVED_PREFIX not in err
    assert "게이트 없는 실행" not in err


def test_confirm_fix_opens_on_a_derived_gate(external, monkeypatch, tmp_path, capsys):
    """확인 전용 라운드 회계도 유도된 게이트 위에서 종전과 동형으로 돈다 (게이트당 1회)."""
    reviewer = _wire(external, monkeypatch, tmp_path, series=[2, 2, 0])
    argv = ["--ticket", TICKET]
    for _ in range(2):
        assert external.main(argv) == 1
    assert external.main(argv) == external.EXIT_ROUND_LIMIT_EXCEEDED     # 수렴 상한
    capsys.readouterr()

    assert external.main(argv + ["--confirm-fix"]) == 0                  # 예외 1회
    assert reviewer.calls == 3
    assert _ledger(tmp_path)[TICKET]["confirm_fix"] == 1
    assert "확인 전용 라운드" in capsys.readouterr().err

    assert external.main(argv + ["--confirm-fix"]) == external.EXIT_ROUND_LIMIT_EXCEEDED
    assert reviewer.calls == 3                                           # 2회째는 전송 없음


def test_confirm_fix_without_any_selector_is_still_refused(
        external, monkeypatch, tmp_path, capsys):
    """티켓도 게이트도 없는 `--confirm-fix` 는 종전대로 전송 전 거부다 (회귀 무변경)."""
    reviewer = _wire(external, monkeypatch, tmp_path)

    assert external.main(["--confirm-fix", "--paths", "x.py"]) == 1
    assert reviewer.calls == 0
    assert _ledger(tmp_path) == {}
    assert "--gate" in capsys.readouterr().err


# ══ ④ 무기록 고지의 시점·확정성 ═════════════════════════════════════════════


def test_pre_spawn_notice_is_conditional_not_a_sent_claim(
        external, monkeypatch, tmp_path, capsys):
    """예약 자리 고지는 조건형이다 — 그 뒤 격리 실패로 중단돼도 이미 찍힌 문구와 모순되지 않는다.

    확정형("이번 전송은 회계 밖")으로 찍으면 스폰 전에 끊긴 실행이 '전송했다'고 말한 셈이 된다."""
    _wire(external, monkeypatch, tmp_path)

    def _fail(*a, **k):
        raise external.ReviewerWorkspaceError("거울 생성 실패")

    monkeypatch.setattr(external, "create_reviewer_workspace", _fail)
    assert external.main(["--paths", "x.py", "--no-gate"]) == 1
    err = capsys.readouterr().err
    assert "이 실행이 전송되면" in err                        # 조건형
    assert "이번 전송은" not in err                          # 확정형 아님
    assert _ledger(tmp_path) == {}


def test_summary_block_states_the_unaccounted_gate(external, monkeypatch, tmp_path, capsys):
    """판정 블록이 회계 밖 사실을 확정형 1줄로 병기한다 (stderr 만으로 두지 않는다)."""
    _wire(external, monkeypatch, tmp_path)

    assert external.main(["--paths", "x.py", "--no-gate"]) == 0
    assert f"게이트: {external._SUMMARY_UNACCOUNTED_GATE}" in capsys.readouterr().out


def test_summary_block_names_the_derived_gate(external, monkeypatch, tmp_path, capsys):
    """회계에 든 실행의 판정 블록은 유도된 게이트 이름을 그대로 보여 준다 (같은 자리·같은 줄)."""
    _wire(external, monkeypatch, tmp_path)

    assert external.main(["--ticket", TICKET]) == 0
    out = capsys.readouterr().out
    assert f"게이트: {TICKET}" in out
    assert external._SUMMARY_UNACCOUNTED_GATE not in out


# ══ ⑤ 조회·처분면의 무시 경고 ═══════════════════════════════════════════════


def test_report_surface_warns_that_no_gate_is_ignored(external, monkeypatch, tmp_path, capsys):
    """조회면은 회계가 없으므로 `--no-gate` 도 무시 경고 목록에 든다 (기존 목록 규율과 대칭)."""
    _wire(external, monkeypatch, tmp_path)

    assert external.main(["--rounds-report", "--no-gate"]) == 0
    err = capsys.readouterr().err
    assert "무시합니다" in err and "--no-gate" in err


def test_resolve_gate_surface_warns_that_no_gate_is_ignored(external):
    """처분 선언면의 무시 목록에도 `--no-gate` 가 든다 (선언은 전송·예약이 없다)."""
    ignored = external._resolve_gate_ignored_flags(
        argparse.Namespace(gate=None, no_gate=True, rounds_report=False,
                           confirm_fix=False, ack_wave=False, force=False))
    assert ignored == "--no-gate"


def test_report_gate_and_no_gate_reaches_report_with_ignore_warning_in_subprocess(tmp_path):
    """조회면의 `--gate`+병행 시 `--no-gate`는 충돌이 아니라 무시되고 표가 나온다."""
    project, script = _subprocess_project(tmp_path)
    _write_ledger(project, _rejected_ledger())

    proc = _run_external_review(
        project, script, "--rounds-report", "--gate", TICKET, "--no-gate",
    )

    assert proc.returncode == 0, proc.stderr
    assert "무시합니다" in proc.stderr and "--no-gate" in proc.stderr
    assert f"게이트 {TICKET}: count=1" in proc.stdout


def test_rounds_report_missing_companion_fails_loud_in_subprocess(tmp_path):
    """동반 seam이 빠진 불완전 엔진 사본은 파일명과 복구법을 명시해 진단한다."""
    project, script = _subprocess_project(tmp_path)
    (script.parent / "review_rounds.py").unlink()

    proc = _run_external_review(project, script, "--rounds-report")

    assert proc.returncode == 1
    assert "엔진 사본 불완전" in proc.stderr
    assert "review_rounds.py" in proc.stderr
    assert "pm-update" in proc.stderr


def test_removed_into_is_rejected_before_gate_no_gate_precedence_in_subprocess(
        tmp_path):
    """폐지된 into는 argparse에서 거부되고 기존 장부 bytes를 보존한다."""
    project, script = _subprocess_project(tmp_path)
    _write_ledger(project, _rejected_ledger())

    before = (project / ".project_manager" / ".local" / "review_rounds.json").read_bytes()
    proc = _run_external_review(
        project, script,
        "--resolve-gate", TICKET, "--into", FOLLOW_UP_TICKET,
        "--gate", OTHER_GATE, "--no-gate",
    )

    assert proc.returncode == 2
    assert "unrecognized arguments: --into" in proc.stderr
    assert (project / ".project_manager" / ".local" / "review_rounds.json").read_bytes() == before
