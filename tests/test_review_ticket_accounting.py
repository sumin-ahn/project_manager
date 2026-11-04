"""리뷰 집계 티켓 자동 유도 — `--ticket` 실행의 조용한 무기록 폐쇄 (T-0626).

`additional_reviewer.py` 를 `--ticket`(+`--paths`)만으로 돌리면 리뷰는 정상 호출·과금되면서 라운드
예약·기록·상한 집계가 전부 생략됐다(`_reserve_round_budget` 첫 분기 = `--review-ticket` 미지정 → 상한 대상
밖·무기록). 실측(2026-08-10): 하루 8+ 라운드가 기록 파일에 0건이라, 반려 must-fix 가 릴리즈 차단
명령(`board.py live-test record`)에 도달하지 못했다. 이 파일은 그 함정이 기계로 닫혔음을 단언한다:

  ① 자동 유도 — `--review-ticket` 미지정 `--ticket` 실행은 리뷰 집계 티켓을 그 티켓으로 유도해 라운드·세션 호출 예산을
     기록한다(명시 `--review-ticket` 이 항상 우선·유도 사실은 stderr 1줄). 조회(`--rounds-report`)·기록
     (`--resolve-review`) 면은 유도 대상이 아니다 — 거기서 `--review-ticket` 은 필터·무시 목록이다.
  ② opt-out — 집계 밖 자문 실행은 명시 `--no-review-ticket` 으로만 열리고, 무기록·집계 제외 사실을 loud 로
     표기한다. `--review-ticket` 과 동시 지정은 부작용 0 지점에서 거부한다.
  ③ 회귀 무변경 — 명시 `--review-ticket` 실행은 종전 그대로고 폐지 플래그는 파서에서 거부된다.

hermetic: REPO 를 tmp 로 monkeypatch 해 기록 파일을 격리하고(형제 `test_additional_reviewer.py`·
`test_review_convergence_check.py` 동일 규약), PM 홈 해소·touches·extract_diff·run_review 를 주입해
실제 git/추가 리뷰어 없이(호출 0·ADR-0004 opt-in) 분기를 단언한다.
"""
from __future__ import annotations

from pm_engine.infra import review_diff
from pm_engine.infra import review_rounds
from pm_engine.domain import reviewer_budget
from pm_engine.app import reviewer_collect
from pm_engine.app import reviewer_reservation
from pm_engine.cli import reviewer_rounds
from pm_engine.app import reviewer_run
from pm_engine.infra import round_store

from pm_engine.infra.harness import delegate_env

from pm_engine.infra import conf_store

import argparse
import importlib.util
import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from pm_engine.infra import pm_paths
from pm_engine.cli import additional_reviewer as cli_additional_reviewer

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

# 유도 고지의 식별 접두 — 무기록 안내가 해결 방법으로 같은 낱말을 쓰므로 접두로 구분한다.
_DERIVED_PREFIX = "리뷰 집계 티켓 자동 유도: --review-ticket"

TICKET = "T-" + "0626"
OTHER_REVIEW_TICKET = "T-" + "0626-review"
FOLLOW_UP_TICKET = "T-" + "0627"


# 해소 가능한 추가 리뷰어 대상 — 대상은 `harness`+`model` 구조화 키로만 서므로(엔진 기본 커맨드
# 없음) 이 파일의 모든 경우가 그 세트를 깔고 시작한다.
_REVIEWER_TARGET = {
    "additional_reviewer.harness": "codex",
    "additional_reviewer.model": "gpt-5.6-sol",
}


def _load(name: str = "additional_reviewer"):
    spec = importlib.util.spec_from_file_location(f"review_ticket_accounting_{name}", TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def external():
    return _load("additional_reviewer")


# ── 연결 ─────────────────────────────────────────────────────────────────────


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
    """main() 을 tmp REPO 로 격리 연결한다 — 반환값은 리뷰어 호출 counter.

    `--ticket` 실행이 타는 상류(소유 PM 홈 해소·touches)도 고정해, 이 파일이 보는 것이 집계 하나가
    되게 한다(대상 저장소 해소 회귀는 `test_target_repo_resolution.py` 소유)."""
    monkeypatch.setattr(external, "REPO", tmp_path)
    monkeypatch.setattr(
        conf_store, 'local_config',
        lambda _home, repo=None: dict(conf) if conf is not None
        else dict(_REVIEWER_TARGET))
    monkeypatch.setattr(
        pm_paths, "resolve_pm_home_for_repo", lambda target_repo_root, **kwargs: tmp_path)
    monkeypatch.setattr(round_store, "parse_ticket_touches", lambda _home, ticket, **kwargs: ["x.py"])
    monkeypatch.setattr(
        review_diff, "extract_diff",
        lambda *a, **k: review_diff.ReviewDiff(
            'diff --git a/x b/x\n@@ -1 +1 @@\n-o\n+n\n',
            'HEAD',
            'INDEX/WORKTREE (untracked 포함)',
            ('x.py',),
            (('git', '-C', str(external.REPO), 'diff', '--', 'x.py'),),
        ),
    )
    # 이 파일의 스코프는 집계 하나다. 리뷰 뒤 리뷰 집계 티켓에 additional-reviewer 절을 기록하는
    # 돌려받기(T-0696)는 실 보드 왕복이 필요한 별도 기준이라 여기서는 격리한다 — 그 회귀는
    # tests/test_additional_reviewer_ticket_collect.py 가 소유한다(run_review stub 과 같은 결).
    monkeypatch.setattr(
        reviewer_collect, "_collect_additional_reviewer_section", lambda *_a, **_k: None,
    )
    reviewer = _Reviewer(series if series is not None else [0])
    monkeypatch.setattr(reviewer_run, "run_review", reviewer)
    real_main = external.main

    def _isolated_main(argv=None):
        argv = list(argv or [])
        if "--output-dir" not in argv:
            argv += ["--output-dir", str(tmp_path / "raw")]
        return real_main(argv)

    monkeypatch.setattr(external, "main", _isolated_main)
    return reviewer


def _read_records(tmp_path) -> dict:
    path = tmp_path / ".project_manager" / ".local" / "review_rounds.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _write_round_records(project: Path, round_records: dict) -> None:
    path = project / ".project_manager" / ".local" / "review_rounds.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(round_records, ensure_ascii=False), encoding="utf-8")


def _rejected_round_records() -> dict:
    """E2E 조회·처분이 공유할 남은 must-fix 1건 기록."""
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
    """`additional_reviewer.py` 실 CLI 프로세스를 tmp PM 홈의 엔진 사본에서 돌린다.

    형제 모듈 목록을 테스트가 재선언하지 않고 tools 집합을 그대로 복사한다. 엔진 모듈이 새로
    분리돼도 사본만 낡아 실 CLI 경로가 깨지는 경우를 만들지 않는다.
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
    return project, tools / "additional_reviewer.py"


def _run_additional_reviewer(project: Path, script: Path, *argv: str) -> subprocess.CompletedProcess:
    """tmp PM 홈에서 실 인자 파싱→`_main`→조회/처분 경로를 실행한다."""
    return subprocess.run(
        [sys.executable, str(script), *argv], cwd=project,
        capture_output=True, text=True, encoding="utf-8", check=False,
    )


def _args(**overrides) -> argparse.Namespace:
    """유도 판정이 읽는 인자면만 담은 Namespace (순수 헬퍼 단언용)."""
    base = {
        "review_ticket": None, "ticket": None, "no_review_ticket": False,
        "rounds_report": False, "resolve_review": None, "dry_run": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


# ══ ① 자동 유도 ══════════════════════════════════════════════════════════════


def test_ticket_only_run_is_recorded_under_the_derived_review_ticket(
        external, monkeypatch, tmp_path, capsys):
    """`--ticket` 단독 실행이 리뷰 집계 티켓 자동 유도로 라운드를 예약·기록한다 (DoD·발단 클래스).

    수정 전엔 같은 실행이 리뷰를 호출·과금하고도 기록 파일에 0건을 남겼다."""
    reviewer = _wire(external, monkeypatch, tmp_path)

    assert external.main(["--ticket", TICKET]) == 0
    assert reviewer.calls == 1
    entry = _read_records(tmp_path)[TICKET]
    assert entry["count"] == 1
    assert [row["must_fix"] for row in entry["rounds"]] == [0]
    assert _DERIVED_PREFIX in capsys.readouterr().err


def test_derived_review_ticket_spends_the_session_budget_too(external, monkeypatch, tmp_path):
    """유도된 리뷰 집계 티켓의 라운드는 세션 호출 예산에서도 차감된다 (집계 기준이 갈리지 않는다)."""
    _wire(external, monkeypatch, tmp_path)
    assert external.main(["--ticket", TICKET]) == 0
    assert _read_records(tmp_path)[reviewer_budget.SESSION_BUDGET_SECTION_KEY]["spent"] == 1


def test_derived_review_ticket_hits_the_convergence_cap_like_an_explicit_one(
        external, monkeypatch, tmp_path, capsys):
    """유도된 리뷰 집계 티켓도 마무리 상한에 걸린다 — `--ticket` 반복이 무한 라운드 우회로가 아니다 (DoD).

    실측 함정의 결과(하루 8+ 라운드)가 기계로 불가능해졌음을 3라운드째 rc 4 로 못박는다."""
    reviewer = _wire(external, monkeypatch, tmp_path, series=[3, 2])
    argv = ["--ticket", TICKET]
    for _ in range(2):                      # 기본 상한 2 (additional_reviewer.rounds_max)
        assert external.main(argv) == 1
    capsys.readouterr()

    assert external.main(argv) == reviewer_budget.EXIT_ROUND_LIMIT_EXCEEDED
    assert reviewer.calls == 2              # 리뷰어 미호출 (호출 0·과금 0)
    err = capsys.readouterr().err
    assert "마무리 통과 조건에서 막힘" in err
    assert "3 → 2" in err


def test_explicit_review_ticket_wins_over_the_ticket(external, monkeypatch, tmp_path, capsys):
    """명시 `--review-ticket` 은 항상 우선 — 유도하지 않고 고지도 내지 않는다 (기존 호출 무변경)."""
    _wire(external, monkeypatch, tmp_path)

    assert external.main(["--ticket", TICKET, "--review-ticket", OTHER_REVIEW_TICKET]) == 0
    round_records = _read_records(tmp_path)
    assert OTHER_REVIEW_TICKET in round_records and TICKET not in round_records
    assert _DERIVED_PREFIX not in capsys.readouterr().err


def test_paths_only_real_send_requires_review_ticket_or_explicit_opt_out(
        external, monkeypatch, tmp_path, capsys):
    """`--paths` 단독 실 호출은 집계 선택 누락을 fail-loud 한다 (T-0631 E2E-1)."""
    reviewer = _wire(external, monkeypatch, tmp_path)

    assert external.main(["--paths", "x.py"]) == 1
    assert reviewer.calls == 0
    assert _read_records(tmp_path) == {}
    err = capsys.readouterr().err
    assert "실 호출에는 리뷰 집계 티켓 지정 또는 명시 opt-out" in err
    assert "--ticket <T-NNNN>" in err and "--review-ticket <T-NNNN>" in err
    assert "--no-review-ticket" in err


def test_real_send_help_examples_choose_review_ticket_accounting(external):
    """도움말의 복사 가능한 실 호출 예시는 리뷰 집계 티켓 선택 없이 rc=1 경로를 안내하지 않는다."""
    prefix = "python3 .project_manager/tools/additional_reviewer.py"
    commands = [
        shlex.split(line.strip())
        for line in cli_additional_reviewer.build_arg_parser().format_help().splitlines()
        if line.strip().startswith(prefix)
    ]
    non_sending_modes = {"--dry-run", "--rounds-report", "--resolve-review"}
    real_send_commands = [
        command for command in commands
        if not non_sending_modes.intersection(command)
    ]

    assert real_send_commands
    for command in real_send_commands:
        assert {"--ticket", "--review-ticket", "--no-review-ticket"}.intersection(command), command


def test_paths_only_with_no_review_ticket_sends_and_keeps_the_unaccounted_notice(
        external, monkeypatch, tmp_path, capsys):
    """`--paths --no-review-ticket` 실 호출은 허용하되 무기록 고지를 유지한다 (T-0631 E2E-2)."""
    reviewer = _wire(external, monkeypatch, tmp_path)

    assert external.main(["--paths", "x.py", "--no-review-ticket"]) == 0
    assert reviewer.calls == 1
    assert _read_records(tmp_path) == {}
    err = capsys.readouterr().err
    assert "`--no-review-ticket` 명시 opt-out" in err
    assert "리뷰어를 부르면 라운드 기록에" in err
    assert "라운드 기록에 남지 않고" in err


def test_paths_only_dry_run_needs_no_review_ticket(external, monkeypatch, tmp_path, capsys):
    """`--dry-run` 은 미호출 미리보기라 리뷰 집계 티켓 선택 없이도 통과한다 (T-0631 E2E-3)."""
    reviewer = _wire(external, monkeypatch, tmp_path)

    assert external.main(["--paths", "x.py", "--dry-run"]) == 0
    assert reviewer.calls == 0
    assert _read_records(tmp_path) == {}
    captured = capsys.readouterr()
    assert "[dry-run] 리뷰어 호출 생략" in captured.out
    assert reviewer_budget._REVIEW_TICKET_ACCOUNTING_REQUIRED_GUIDANCE not in captured.err


def test_dry_run_with_a_derived_review_ticket_records_nothing(external, monkeypatch, tmp_path, capsys):
    """미리보기는 유도된 리뷰 집계 티켓으로도 부작용 0 이다 — 예약·기록 없음 (dry-run 계약 무변경)."""
    reviewer = _wire(external, monkeypatch, tmp_path)

    assert external.main(["--ticket", TICKET, "--dry-run"]) == 0
    assert reviewer.calls == 0
    assert _read_records(tmp_path) == {}
    assert _DERIVED_PREFIX in capsys.readouterr().err


def test_a_reserved_record_key_ticket_is_refused_before_sending(
        external, monkeypatch, tmp_path, capsys):
    """유도한 이름도 예약 키 검사를 지난다 — 기록 예약 키 티켓은 호출 전 rc 1 (크래시 아님)."""
    reviewer = _wire(external, monkeypatch, tmp_path)

    assert external.main(["--ticket", reviewer_budget.SESSION_BUDGET_SECTION_KEY]) == 1
    assert reviewer.calls == 0
    assert _read_records(tmp_path) == {}
    err = capsys.readouterr().err
    assert "예약 키" in err and "--ticket" in err


def test_report_surface_is_not_a_derivation_target(external):
    """조회면의 `--review-ticket` 은 '한 리뷰 집계 티켓만 보기' 필터라 유도값이 사용자 선택으로 둔갑하면 안 된다."""
    args = _args(ticket=TICKET, rounds_report=True)
    assert reviewer_reservation._derive_review_ticket_from_ticket(args) == reviewer_budget.ReviewTicketDerivation()
    assert args.review_ticket is None


def test_resolve_review_surface_is_not_a_derivation_target(external):
    """처분 선언면의 `--review-ticket` 은 무시 목록 입력이다 — 유도하면 없던 무시 경고가 뜬다."""
    args = _args(ticket=TICKET, resolve_review=OTHER_REVIEW_TICKET, rounds_report=False)
    assert reviewer_reservation._derive_review_ticket_from_ticket(args) == reviewer_budget.ReviewTicketDerivation()
    assert args.review_ticket is None
    assert "--review-ticket" not in reviewer_rounds._resolve_review_ignored_flags(
        argparse.Namespace(review_ticket=args.review_ticket, no_review_ticket=False, rounds_report=False,
                           reset_session_budget=False, force=False))


def test_derivation_helper_sets_the_review_ticket_and_returns_one_line(external, capsys):
    """유도는 리뷰 집계 티켓을 티켓 값으로 세우고 고지 **문구를 돌려준다** — 출력 자리는 호출부가 정한다.

    헬퍼가 직접 찍으면 stderr 첫 줄이 config provenance 라는 계약을 이 고지가 밀어낸다."""
    args = _args(ticket=TICKET)
    derivation = reviewer_reservation._derive_review_ticket_from_ticket(args)
    assert args.review_ticket == TICKET
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
    assert lines[0].startswith("[additional-reviewer] config provenance:")
    assert lines[1].startswith(_DERIVED_PREFIX)


def test_empty_diff_does_not_claim_the_derived_review_ticket_was_recorded(
        external, monkeypatch, tmp_path, capsys):
    """빈 diff 조기 종료는 예약·기록 전이라 자동 유도 고지도 호출 조건형으로만 남는다."""
    reviewer = _wire(external, monkeypatch, tmp_path)
    monkeypatch.setattr(
        review_diff, "extract_diff",
        lambda *a, **k: review_diff.ReviewDiff(
            '',
            'HEAD',
            'INDEX/WORKTREE (untracked 포함)',
            ('x.py',),
            (('git', '-C', str(external.REPO), 'diff', '--', 'x.py'),),
        ),
    )

    assert external.main(["--ticket", TICKET]) == 1
    assert reviewer.calls == 0
    assert _read_records(tmp_path) == {}
    err = capsys.readouterr().err
    assert _DERIVED_PREFIX in err
    assert "이 실행이 리뷰어를 부르면 라운드 집계가" in err
    assert "라운드 집계가 이 리뷰 집계 티켓에 붙습니다" not in err


def test_dry_run_notice_does_not_claim_this_run_is_recorded(
        external, monkeypatch, tmp_path, capsys):
    """미리보기 고지는 확정형이 아니다 — 예약도 기록도 없는 실행이 '기록된다'고 말하지 않는다."""
    _wire(external, monkeypatch, tmp_path)

    assert external.main(["--ticket", TICKET, "--dry-run"]) == 0
    err = capsys.readouterr().err
    assert "미리보기라 이번 실행은 기록·집계하지 않고" in err
    assert "에 붙습니다). 집계 밖" not in err   # 실행판 확정 문구


# ══ ② opt-out (`--no-review-ticket`) ═════════════════════════════════════════════════


def test_no_review_ticket_opts_out_of_the_record_with_a_loud_notice(
        external, monkeypatch, tmp_path, capsys):
    """`--no-review-ticket` 은 유도를 끄고 집계 밖으로 나간다 — 무기록·집계 제외를 loud 로 표기한다 (DoD)."""
    reviewer = _wire(external, monkeypatch, tmp_path)

    assert external.main(["--ticket", TICKET, "--no-review-ticket"]) == 0
    assert reviewer.calls == 1                    # 리뷰 자체는 정상 실행
    assert _read_records(tmp_path) == {}                # 기록 파일은 만들지 않는다
    err = capsys.readouterr().err
    assert "`--no-review-ticket` 명시 opt-out" in err
    assert "라운드·세션 호출 예산도 쓰지 않습니다" in err
    assert "live-test record" in err               # 릴리즈 차단이 못 본다는 사실까지
    assert _DERIVED_PREFIX not in err


def test_no_review_ticket_with_an_explicit_review_ticket_is_refused_before_sending(
        external, monkeypatch, tmp_path, capsys):
    """`--review-ticket` + `--no-review-ticket` 은 모순이라 부작용 0 지점에서 거부한다 (경고-만-실행 금지)."""
    reviewer = _wire(external, monkeypatch, tmp_path)

    assert external.main(["--review-ticket", OTHER_REVIEW_TICKET, "--no-review-ticket", "--paths", "x.py"]) == 1
    assert reviewer.calls == 0
    assert _read_records(tmp_path) == {}
    err = capsys.readouterr().err
    assert "함께 쓸 수 없습니다" in err and OTHER_REVIEW_TICKET in err


def test_no_review_ticket_rejects_removed_confirm_fix_before_accounting(external, monkeypatch, tmp_path, capsys):
    """opt-out 조합에서도 폐지 플래그는 argparse 단계에서 거부된다."""
    reviewer = _wire(external, monkeypatch, tmp_path)

    with pytest.raises(SystemExit):
        external.main(["--ticket", TICKET, "--no-review-ticket", "--confirm-fix"])
    assert reviewer.calls == 0
    assert _read_records(tmp_path) == {}
    assert "unrecognized arguments: --confirm-fix" in capsys.readouterr().err


# ══ ③ 기존 동작 회귀 무변경 ═════════════════════════════════════════════════


def test_explicit_review_ticket_run_is_unchanged(external, monkeypatch, tmp_path, capsys):
    """명시 `--review-ticket` 실행은 종전 그대로 기록되고, 새 고지·경고를 얹지 않는다 (DoD)."""
    _wire(external, monkeypatch, tmp_path)

    assert external.main(["--review-ticket", OTHER_REVIEW_TICKET, "--paths", "x.py"]) == 0
    assert _read_records(tmp_path)[OTHER_REVIEW_TICKET]["count"] == 1
    err = capsys.readouterr().err
    assert _DERIVED_PREFIX not in err


def test_removed_confirm_fix_does_not_open_a_derived_review_ticket(external, monkeypatch, tmp_path, capsys):
    """티켓에서 리뷰 집계 티켓을 유도할 수 있어도 폐지 플래그는 새 라운드를 열지 않는다."""
    reviewer = _wire(external, monkeypatch, tmp_path, series=[2])
    argv = ["--ticket", TICKET]
    assert external.main(argv) == 1
    before = review_rounds._round_record_path(pm_paths.home_at(external.REPO)).read_bytes()
    capsys.readouterr()

    with pytest.raises(SystemExit):
        external.main(argv + ["--confirm-fix"])
    assert reviewer.calls == 1
    assert review_rounds._round_record_path(pm_paths.home_at(external.REPO)).read_bytes() == before
    assert "unrecognized arguments: --confirm-fix" in capsys.readouterr().err


def test_removed_confirm_fix_without_any_selector_is_rejected(
        external, monkeypatch, tmp_path, capsys):
    """티켓도 리뷰 집계 티켓도 없는 폐지 플래그 역시 argparse에서 거부된다."""
    reviewer = _wire(external, monkeypatch, tmp_path)

    with pytest.raises(SystemExit):
        external.main(["--confirm-fix", "--paths", "x.py"])
    assert reviewer.calls == 0
    assert _read_records(tmp_path) == {}
    assert "unrecognized arguments: --confirm-fix" in capsys.readouterr().err


# ══ ④ 무기록 고지의 시점·확정성 ═════════════════════════════════════════════


def test_pre_spawn_notice_is_conditional_not_a_sent_claim(
        external, monkeypatch, tmp_path, capsys):
    """예약 자리 고지는 조건형이다 — 그 뒤 스폰 전 실패로 중단돼도 이미 찍힌 문구와 모순되지 않는다.

    확정형("이번 호출은 집계 밖")으로 찍으면 스폰 전에 끊긴 실행이 '호출했다'고 말한 셈이 된다."""
    _wire(external, monkeypatch, tmp_path)

    def _fail(*a, **k):
        raise RuntimeError("리뷰어 환경 구성 실패(주입)")

    monkeypatch.setattr(delegate_env, "build_env", _fail)
    with pytest.raises(RuntimeError):
        external.main(["--paths", "x.py", "--no-review-ticket"])
    err = capsys.readouterr().err
    assert "리뷰어를 부르면 라운드 기록에" in err                        # 조건형
    assert "이번 호출은" not in err                          # 확정형 아님
    assert _read_records(tmp_path) == {}


def test_summary_block_states_the_unaccounted_review_ticket(external, monkeypatch, tmp_path, capsys):
    """판정 블록이 집계 밖 사실을 확정형 1줄로 병기한다 (stderr 만으로 두지 않는다)."""
    _wire(external, monkeypatch, tmp_path)

    assert external.main(["--paths", "x.py", "--no-review-ticket"]) == 0
    assert f"리뷰 집계 티켓: {reviewer_budget._SUMMARY_UNACCOUNTED_REVIEW_TICKET}" in capsys.readouterr().out


def test_summary_block_names_the_derived_review_ticket(external, monkeypatch, tmp_path, capsys):
    """집계에 든 실행의 판정 블록은 유도된 리뷰 집계 티켓 이름을 그대로 보여 준다 (같은 자리·같은 줄)."""
    _wire(external, monkeypatch, tmp_path)

    assert external.main(["--ticket", TICKET]) == 0
    out = capsys.readouterr().out
    assert f"리뷰 집계 티켓: {TICKET}" in out
    assert reviewer_budget._SUMMARY_UNACCOUNTED_REVIEW_TICKET not in out


# ══ ⑤ 조회·처분면의 무시 경고 ═══════════════════════════════════════════════


def test_report_surface_warns_that_no_review_ticket_is_ignored(external, monkeypatch, tmp_path, capsys):
    """조회면은 집계가 없으므로 `--no-review-ticket` 도 무시 경고 목록에 든다 (기존 목록 규율과 대칭)."""
    _wire(external, monkeypatch, tmp_path)

    assert external.main(["--rounds-report", "--no-review-ticket"]) == 0
    err = capsys.readouterr().err
    assert "무시합니다" in err and "--no-review-ticket" in err


def test_resolve_review_surface_warns_that_no_review_ticket_is_ignored(external):
    """처분 선언면의 무시 목록에도 `--no-review-ticket` 이 든다 (선언은 호출·예약이 없다)."""
    ignored = reviewer_rounds._resolve_review_ignored_flags(
        argparse.Namespace(review_ticket=None, no_review_ticket=True, rounds_report=False,
                           reset_session_budget=False, force=False))
    assert ignored == "--no-review-ticket"


def test_report_review_ticket_and_no_review_ticket_reaches_report_with_ignore_warning_in_subprocess(tmp_path):
    """조회면의 `--review-ticket`+병행 시 `--no-review-ticket`은 충돌이 아니라 무시되고 표가 나온다."""
    project, script = _subprocess_project(tmp_path)
    _write_round_records(project, _rejected_round_records())

    proc = _run_additional_reviewer(
        project, script, "--rounds-report", "--review-ticket", TICKET, "--no-review-ticket",
    )

    assert proc.returncode == 0, proc.stderr
    assert "무시합니다" in proc.stderr and "--no-review-ticket" in proc.stderr
    assert f"리뷰 집계 티켓 {TICKET}: count=1" in proc.stdout


def test_removed_into_is_rejected_before_review_ticket_no_review_ticket_precedence_in_subprocess(
        tmp_path):
    """폐지된 into는 argparse에서 거부되고 기존 기록 파일 bytes를 보존한다."""
    project, script = _subprocess_project(tmp_path)
    _write_round_records(project, _rejected_round_records())

    before = (project / ".project_manager" / ".local" / "review_rounds.json").read_bytes()
    proc = _run_additional_reviewer(
        project, script,
        "--resolve-review", TICKET, "--into", FOLLOW_UP_TICKET,
        "--review-ticket", OTHER_REVIEW_TICKET, "--no-review-ticket",
    )

    assert proc.returncode == 2
    assert "unrecognized arguments: --into" in proc.stderr
    assert (project / ".project_manager" / ".local" / "review_rounds.json").read_bytes() == before
