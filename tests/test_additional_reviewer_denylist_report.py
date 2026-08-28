"""additional_reviewer 시크릿 denylist 제외 보고 — 게이트 false-confidence 차단 (T-0428).

시크릿 denylist 오탐이 경로를 diff 에서 제외하고도 판정에 전혀 안 남으면, 지정분이 조용히 빠진 채
'통과'가 나 게이트가 실제보다 넓게 검증한 것처럼 보인다(false-confidence). 발단(2026-07-21·T-0424
게이트): 신규 가드 테스트 `tests/test_adapter_token_substitution.py` 가 패턴 `*token*` 에 걸려 통째로
제외됐고 그 상태로 '종합 판정: 통과' 가 나왔다.

denylist 패턴 자체는 불변(오탐 허용·누락 금지·additional_reviewer.py:165) — 이 파일은 *판정 보고*만
고쳤음을 박제한다: (A) 실 filter 가 발단 파일을 제외함(real matching), (B) --paths 명시 지정분 제외
→ 차단(exit 1 + 왜), (C) --ticket/기본 암묵 수집분 제외 → 비차단·판정 라인 병기, (D) 제외 0건 →
종전 완전 동일.

hermetic: PM-home 해소·extract_diff·run_review 를 monkeypatch 해 외부 codex 실호출(ADR-0004
opt-in·과금) 없이 main() 분기를 격리한다. 발단 end-to-end 재현(F)만 실 subprocess.run 을 주입해
diff→실 filter→차단의 전 production 경로를 태운다(외부 git 바이너리 없이).
"""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

# 발단 파일 — 실제로 `*token*` 에 걸려 제외된 신규 가드 테스트 (T-0424 게이트).
TOKEN_FILE = "tests/test_adapter_token_substitution.py"
REAL_FILE = "tests/test_real.py"


# 해소 가능한 추가 리뷰어 대상 — 대상은 `harness`+`model` 구조화 키로만 서므로(엔진 기본 커맨드
# 없음) 이 파일의 모든 형상이 그 세트를 깔고 시작한다.
_REVIEWER_TARGET = {
    "additional_reviewer.enabled": "true",
    "additional_reviewer.harness": "codex",
    "additional_reviewer.model": "gpt-5.6-sol",
}


def _load(name: str = "additional_reviewer"):
    """도구 모듈을 (패키지 아님) importlib 로 경로 로드 — 형제 additional_reviewer 테스트 동일 규약."""
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def external():
    return _load("additional_reviewer")


@pytest.fixture(autouse=True)
def isolated_round_ledger(external, monkeypatch, tmp_path):
    """라운드 장부의 **읽기·쓰기 양쪽**을 tmp 로 격리한다 (REPO 앵커는 실 저장소 그대로).

    게이트 회계 자동 유도(T-0626) 이후 `--ticket` 실행은 라운드를 장부에 예약·기록한다 — 이 파일은
    실 저장소 앵커로 denylist 분기를 태우므로, 격리하지 않으면 denylist 단언이 실 장부에 라운드를
    쌓고 상한까지 흔들어 테스트가 실행 이력에 의존하게 된다.

    쓰기 경로(`_round_ledger_path`·락 파일은 여기서 파생)만 옮기면 절반이다 — 승계 입력인 legacy
    경로(`_legacy_round_ledger_path`)는 REPO 파생이라 **실 장부를 계속 읽는다**. 실 장부에 같은
    이름의 게이트가 있으면 그 상태가 tmp 장부로 승계돼 단언이 다시 실행 이력에 묶인다."""
    local = tmp_path / ".project_manager" / ".local"
    local.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(external, "_round_ledger_path", lambda: local / "review_rounds.json")
    monkeypatch.setattr(
        external, "_legacy_round_ledger_path", lambda: local / "legacy_review_rounds.json")


def test_round_ledger_isolation_covers_read_and_write(external, tmp_path):
    """격리 fixture 가 쓰기(장부)와 **읽기**(legacy 승계 입력) 양쪽을 tmp 로 옮겼는지 못박는다.

    쓰기만 옮기면 실 장부의 동명 게이트 상태가 legacy 승계로 흘러들어 단언이 실행 이력에 묶인다."""
    for resolve in (external._round_ledger_path, external._legacy_round_ledger_path):
        assert tmp_path in resolve().parents, resolve()


# ── (A) 실 matching — 발단 파일이 *token* 에 걸림 (패턴 불변 전제 박제) ───────────


def test_token_test_file_is_denylisted(external):
    """발단 파일 tests/test_adapter_token_substitution.py 가 `*token*` denylist 에 걸린다."""
    assert external._is_secret_path(TOKEN_FILE) is True


def test_matching_pattern_reports_token(external):
    """`_matching_denylist_pattern` 이 어느 패턴에 걸렸는지(=왜)를 돌려준다 — `*token*`."""
    assert external._matching_denylist_pattern(TOKEN_FILE) == "*token*"


def test_directory_name_with_token_is_denylisted(external):
    """디렉토리명에 token 이 들어도 걸린다(fnmatch 는 / 미특별취급·오탐 허용이 의도)."""
    assert external._matching_denylist_pattern("tests/token_utils/a.py") == "*token*"


def test_plain_file_not_denylisted(external):
    """무관 파일은 매칭 없음 → None (제외 0건 경로 — 종전 동작)."""
    assert external._matching_denylist_pattern(REAL_FILE) is None


def test_filter_excludes_token_keeps_real(external):
    """실 filter_secret_hunks: 발단 파일 hunk 제거 + real 파일 잔존 + 제외 목록 반환."""
    diff = (
        f"diff --git a/{TOKEN_FILE} b/{TOKEN_FILE}\n@@ -0,0 +1 @@\n+guard = 1\n"
        f"diff --git a/{REAL_FILE} b/{REAL_FILE}\n@@ -0,0 +1 @@\n+ok = 1\n"
    )
    filtered, excluded = external.filter_secret_hunks(diff, external._SECRET_DENYLIST_PATTERNS)
    assert excluded == [TOKEN_FILE]
    assert "token_substitution" not in filtered
    assert REAL_FILE in filtered


def test_extract_diff_returns_diff_and_excluded(external):
    """extract_diff 가 제외 목록을 삼키지 않고 (diff, excluded) 튜플로 반환한다(T-0428 근본)."""
    raw = "diff --git a/x.py b/x.py\n+ok\ndiff --git a/.env b/.env\n+SECRET=1\n"

    def _git(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, stdout=raw, stderr="")

    diff, excluded = external.extract_diff("main", ["x.py", ".env"], run_fn=_git)
    assert excluded == [".env"]
    assert "SECRET" not in diff and "x.py" in diff


# ── 헬퍼 unit ────────────────────────────────────────────────────────────────


def test_exclusion_suffix_empty_is_blank(external):
    """제외 0건/None → 빈 접미사(판정 라인 종전과 완전 동일)."""
    assert external._exclusion_suffix([]) == ""
    assert external._exclusion_suffix(None) == ""


def test_exclusion_suffix_counts_and_lists(external):
    """제외 N건 → '(검토 제외 N건 — p1, p2)' 병기 접미사."""
    assert external._exclusion_suffix([TOKEN_FILE]) == f" (검토 제외 1건 — {TOKEN_FILE})"
    assert external._exclusion_suffix(["a", "b"]) == " (검토 제외 2건 — a, b)"


def test_explicit_block_names_path_pattern_and_remedy(external):
    """차단 안내가 어느 경로·어느 패턴·우회법(경로 빼고 재실행)을 담는다."""
    block = external._format_explicit_exclusion_block(
        [TOKEN_FILE], external._SECRET_DENYLIST_PATTERNS)
    assert TOKEN_FILE in block
    assert "*token*" in block           # 왜(어느 패턴)
    assert "--paths" in block           # 우회 안내
    assert "denylist" in block


def test_explicit_block_uses_extra_pattern(external):
    """additional_reviewer.denylist_extra 사용자 패턴 매칭분도 그 패턴명으로 '왜'가 보고된다.

    파일명은 내장 패턴엔 안 걸리고 사용자 추가 `*apikey*` 에만 걸리게 골라, 병합 패턴(내장+추가)으로
    '왜'가 해소됨을 격리한다(내장 `*secret*` 등이 먼저 삼키지 않도록)."""
    patterns = external._denylist_patterns({"additional_reviewer.denylist_extra": "*apikey*"})
    block = external._format_explicit_exclusion_block(["data/company_apikey.dat"], patterns)
    assert "*apikey*" in block
    assert "data/company_apikey.dat" in block


# ── main() 격리 harness ──────────────────────────────────────────────────────


def _run_main(external, monkeypatch, *, argv, excluded, diff=None,
              conf=None, verdict="pass", touches=None):
    """main() 을 격리 실행 — extract_diff 를 (diff, excluded) 로 주입, 리뷰어 호출을 기록.

    selector 상류의 PM-home 해소도 고정해 checkout/lease 형상과 무관하게 denylist 분기만 격리한다.
    반환: (exit_code, reviewer_called)."""
    if diff is None:
        diff = f"diff --git a/{REAL_FILE} b/{REAL_FILE}\n@@ -0,0 +1 @@\n+ok = 1\n"
    conf = dict(_REVIEWER_TARGET) if conf is None else conf
    monkeypatch.setattr(external, "local_config", lambda repo=None: conf)
    monkeypatch.setattr(
        external, "resolve_pm_home_for_repo", lambda anchor, **kwargs: external.REPO,
    )
    monkeypatch.setattr(external, "extract_diff", lambda *a, **k: (diff, list(excluded)))
    if touches is not None:
        monkeypatch.setattr(
            external, "parse_ticket_touches", lambda t, **kwargs: list(touches),
        )
    called = {"reviewer": False}

    def _fake_run_review(*a, **k):
        called["reviewer"] = True
        if verdict == "must_fix":
            return {"reviewer": "x", "ok": True, "output": "판정: 반려",
                    "verdict": {"has_must_fix": True, "has_pass": False}, "file": None,
                    "failed": False, "any_must_fix": True, "all_pass": False}
        if verdict == "failed":
            return {"reviewer": "x", "ok": False, "output": "", "verdict": None, "file": None,
                    "failed": True, "any_must_fix": False, "all_pass": False}
        return {"reviewer": "x", "ok": True, "output": "판정: 통과",
                "verdict": {"has_must_fix": False, "has_pass": True}, "file": None,
                "failed": False, "any_must_fix": False, "all_pass": True}

    monkeypatch.setattr(external, "run_review", _fake_run_review)
    # 이 파일은 denylist 판정 보고 축을 소유한다. 산출 회수(T-0696)는 별도 축이라
    # tests/test_additional_reviewer_ticket_harvest.py 가 소유하고 여기서는 격리한다.
    monkeypatch.setattr(
        external, "_harvest_additional_reviewer_section", lambda *_a, **_k: None,
    )
    return external.main(argv), called["reviewer"]


# ── (B) --paths 명시 지정분 제외 → 차단 (exit 1 + 왜) ──────────────────────────


def test_paths_partial_exclusion_blocks_before_reviewer(external, monkeypatch, capsys):
    """발단 클래스: --paths 에 [real, token] → token 제외·real diff 잔존 → 차단(exit 1)·리뷰어 미호출.

    수정 전엔 real diff 가 남아 리뷰어가 '통과'(exit 0)를 냈다(false-green). 명시 지정분 제외는 차단."""
    exit_code, reviewer_called = _run_main(
        external, monkeypatch, argv=["--paths", REAL_FILE, TOKEN_FILE], excluded=[TOKEN_FILE])
    assert exit_code == 1
    assert reviewer_called is False    # codex 전송 전 차단 (false-confidence 원천)
    err = capsys.readouterr().err
    assert TOKEN_FILE in err
    assert "*token*" in err            # 어느 패턴에 걸렸는지(왜)
    assert "false-confidence" in err


def test_paths_all_excluded_empty_diff_blocks_with_denylist_reason(external, monkeypatch, capsys):
    """명시 경로 전부 제외 → diff 비어도 '변경 없음'(빈-diff 안내)이 아니라 denylist 차단이 우선.

    단일 token 파일만 --paths 로 주면 diff 가 빈다 — 빈-diff 가드보다 앞선 denylist 차단이 정확한
    원인을 알린다(수정 전엔 '리뷰할 diff 가 없습니다'로 오도)."""
    exit_code, reviewer_called = _run_main(
        external, monkeypatch, argv=["--paths", TOKEN_FILE], excluded=[TOKEN_FILE], diff="")
    assert exit_code == 1
    assert reviewer_called is False
    err = capsys.readouterr().err
    assert "denylist" in err
    assert "리뷰할 diff 가 없습니다" not in err   # 빈-diff 안내로 오도하지 않음


def test_paths_and_ticket_together_paths_dominates_blocks(external, monkeypatch, capsys):
    """--paths 와 --ticket 동시 → --paths 가 명시 지정으로 우선 → 제외분 차단(exit 1)."""
    exit_code, reviewer_called = _run_main(
        external, monkeypatch,
        argv=["--paths", REAL_FILE, TOKEN_FILE, "--ticket", "T-0001"],
        excluded=[TOKEN_FILE], touches=["ignored/"])
    assert exit_code == 1
    assert reviewer_called is False
    assert TOKEN_FILE in capsys.readouterr().err


def test_paths_extra_pattern_exclusion_blocks(external, monkeypatch, capsys):
    """additional_reviewer.denylist_extra 사용자 패턴 매칭분도 --paths 명시 지정이면 동일 차단·패턴명 보고."""
    conf = {**_REVIEWER_TARGET, "additional_reviewer.denylist_extra": "*apikey*"}
    exit_code, reviewer_called = _run_main(
        external, monkeypatch, argv=["--paths", "data/company_apikey.dat"],
        excluded=["data/company_apikey.dat"], diff="", conf=conf)
    assert exit_code == 1
    assert reviewer_called is False
    err = capsys.readouterr().err
    assert "*apikey*" in err            # 사용자 패턴명이 '왜'로 보고됨


# ── (C) --ticket/기본 암묵 수집분 제외 → 비차단·판정 병기 ──────────────────────


def test_ticket_exclusion_annotates_verdict_not_block(external, monkeypatch, capsys):
    """--ticket 수집분 제외 → 차단 없이 리뷰 진행 + 종합 판정 라인에 제외 병기(exit 0 통과)."""
    exit_code, reviewer_called = _run_main(
        external, monkeypatch, argv=["--ticket", "T-0001"],
        excluded=[TOKEN_FILE], touches=[REAL_FILE, TOKEN_FILE])
    assert exit_code == 0
    assert reviewer_called is True     # 암묵 수집분은 차단 안 함
    out = capsys.readouterr().out
    assert f"종합 판정: 통과 (검토 제외 1건 — {TOKEN_FILE})" in out


def test_ticket_multiple_exclusions_annotates_count(external, monkeypatch, capsys):
    """복수 제외 → '(검토 제외 2건 — a, b)' 로 건수·경로 병기."""
    _run_main(external, monkeypatch, argv=["--ticket", "T-0001"],
              excluded=[TOKEN_FILE, "config/secret.yaml"], touches=[REAL_FILE])
    out = capsys.readouterr().out
    assert f"(검토 제외 2건 — {TOKEN_FILE}, config/secret.yaml)" in out


def test_default_paths_exclusion_annotates_not_block(external, monkeypatch, capsys):
    """기본 경로는 암묵 지정이므로 제외을 병기하고, 명시 opt-out 실송은 비차단한다."""
    exit_code, reviewer_called = _run_main(
        external, monkeypatch, argv=["--no-gate"], excluded=[TOKEN_FILE])
    assert exit_code == 0
    assert reviewer_called is True
    assert f"(검토 제외 1건 — {TOKEN_FILE})" in capsys.readouterr().out


def test_ticket_exclusion_stderr_warns_with_pattern(external, monkeypatch, capsys):
    """암묵 수집분 제외 시 stderr 경고에 경로·패턴을 남긴다(판정 병기와 별개 보조 알림)."""
    _run_main(external, monkeypatch, argv=["--ticket", "T-0001"],
              excluded=[TOKEN_FILE], touches=[REAL_FILE])
    err = capsys.readouterr().err
    assert TOKEN_FILE in err and "*token*" in err


# ── (D) 제외 0건 → 출력·exit code 종전과 동일 ─────────────────────────────────


def test_zero_exclusion_paths_unchanged(external, monkeypatch, capsys):
    """--paths 제외 0건 → 리뷰 진행·exit 0·판정 라인에 '검토 제외' 접미사 없음(종전 무변경)."""
    exit_code, reviewer_called = _run_main(
        external, monkeypatch, argv=["--paths", REAL_FILE, "--no-gate"], excluded=[])
    assert exit_code == 0
    assert reviewer_called is True
    out = capsys.readouterr().out
    assert "종합 판정: 통과" in out
    assert "검토 제외" not in out


def test_zero_exclusion_ticket_unchanged(external, monkeypatch, capsys):
    """--ticket 제외 0건 → 병기 없음(종전 통과 경로 무변경)."""
    exit_code, _ = _run_main(
        external, monkeypatch, argv=["--ticket", "T-0001"], excluded=[], touches=[REAL_FILE])
    assert exit_code == 0
    assert "검토 제외" not in capsys.readouterr().out


def test_print_summary_zero_exclusion_byte_identical(external, capsys):
    """print_summary(excluded=[]) == 종전 호출(excluded 미지정) — 바이트 동일(0건 무변경)."""
    result = {"reviewer": "codex", "ok": True, "output": "판정: 통과",
              "verdict": {"has_must_fix": False, "has_pass": True}, "file": None,
              "failed": False, "any_must_fix": False, "all_pass": True}
    external.print_summary(result)                 # 종전 시그니처(excluded 미지정)
    baseline = capsys.readouterr().out
    external.print_summary(result, excluded=[])    # 신규 인자·0건
    assert capsys.readouterr().out == baseline
    assert "검토 제외" not in baseline


# ── (E) print_summary 병기 — 판정 분기별 ─────────────────────────────────────


def test_print_summary_annotates_pass(external, capsys):
    """통과 판정 라인에 제외 병기."""
    result = {"reviewer": "codex", "ok": True,
              "verdict": {"has_must_fix": False, "has_pass": True}, "file": None,
              "failed": False, "any_must_fix": False, "all_pass": True}
    external.print_summary(result, excluded=[TOKEN_FILE])
    assert f"종합 판정: 통과 (검토 제외 1건 — {TOKEN_FILE})" in capsys.readouterr().out


def test_print_summary_annotates_must_fix(external, capsys):
    """비-통과(must-fix) 판정 라인에도 제외 병기(coverage caveat 는 모든 판정에 유효)."""
    result = {"reviewer": "codex", "ok": True,
              "verdict": {"has_must_fix": True, "has_pass": False}, "file": None,
              "failed": False, "any_must_fix": True, "all_pass": False}
    external.print_summary(result, excluded=[TOKEN_FILE])
    out = capsys.readouterr().out
    assert "비-통과 (must-fix 감지 — PM 검토 필요)" in out
    assert f"(검토 제외 1건 — {TOKEN_FILE})" in out


# ── (F) 발단 end-to-end — 실 subprocess 주입→실 extract_diff/filter→차단 ────────


def test_reproduce_false_confidence_end_to_end(external, monkeypatch, capsys):
    """발단 end-to-end: 실 extract_diff/filter 가 token 파일을 `*token*` 로 제외 → --paths 명시라 차단.

    subprocess.run 을 주입해 raw diff 를 준다(외부 git 없이) — main→extract_diff→filter_secret_hunks
    실 경로를 태운다. 수정 전엔 real.py diff 가 남아 리뷰어가 '통과'(exit 0)를 냈다: 이 테스트가
    발단(T-0424 게이트)을 red 로 박제한다."""
    raw = (
        f"diff --git a/{TOKEN_FILE} b/{TOKEN_FILE}\n@@ -0,0 +1 @@\n+guard = 1\n"
        f"diff --git a/{REAL_FILE} b/{REAL_FILE}\n@@ -0,0 +1 @@\n+ok = 1\n"
    )

    def _fake_git(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, stdout=raw, stderr="")

    monkeypatch.setattr(external.subprocess, "run", _fake_git)
    monkeypatch.setattr(
        external, "local_config",
        lambda repo=None: dict(_REVIEWER_TARGET),
    )
    monkeypatch.setattr(
        external, "resolve_pm_home_for_repo", lambda anchor, **kwargs: external.REPO,
    )
    called = {"reviewer": False}

    def _rr(*a, **k):
        called["reviewer"] = True
        return {"reviewer": "x", "ok": True, "output": "판정: 통과",
                "verdict": {"has_must_fix": False, "has_pass": True}, "file": None,
                "failed": False, "any_must_fix": False, "all_pass": True}

    monkeypatch.setattr(external, "run_review", _rr)
    exit_code = external.main(["--base", "main", "--paths", TOKEN_FILE, REAL_FILE])
    assert exit_code == 1               # 수정 후: 명시 지정분 제외 → 차단
    assert called["reviewer"] is False   # 수정 전: 리뷰어 호출·exit 0 (false-green)
    err = capsys.readouterr().err
    assert TOKEN_FILE in err and "*token*" in err
