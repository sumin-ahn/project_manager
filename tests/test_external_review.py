"""external_review 라운드 상한 게이트 — codex 게이트 무한 라운드 기계 차단 (T-0457).

외부 리뷰(codex)는 과금·외부 전송 게이트라 라운드가 무한정 이어지면 비용이 쌓인다(PM 10차 실측:
한 게이트 클러스터 25라운드). PM 자의 "수렴 판단"을 기계 판정으로 대체한다
([[mechanize-dont-instruct-llm]]): `--gate <T-NNNN>` 별 라운드 장부
(`.project_manager/.local/review_rounds.json`·per-clone·git-ignored)에 실 전송 count 를 쌓고,
승인 없이 limit(local.conf `external_review_round_limit`·기본 4)을 넘기면 실행 *전에* 거부(전용 rc
`EXIT_ROUND_LIMIT_EXCEEDED`)하고 "사용자 보고·대기" loud 안내를 낸다. 사용자 승인 후 `--ack-rounds`
로만 재개(+limit 창).

hermetic: REPO 를 tmp 로 monkeypatch 해 장부가 tmp `.local/` 에 격리되게 하고(`_round_ledger_path`
가 호출 시점 REPO 파생·`_tickets_dir` 동형), extract_diff·run_review·local_config 를 module-level
로 주입해 실제 git/codex 없이(외부 전송 0·ADR-0004 opt-in) 게이트 분기를 단언한다. 카운트 규칙
경계(dry-run·빈-diff·리뷰어 실패 제외)는 리뷰어 호출 여부·장부 count 로 격리한다.
"""
from __future__ import annotations

import importlib.util
import json
import multiprocessing
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"


def _load(name: str = "external_review"):
    """도구 모듈을 (패키지 아님) importlib 로 경로 로드 — sibling 테스트 동일 규약."""
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def external():
    return _load("external_review")


# ── PM 하네스 세션 마커 / 호출층 상한 진단 ──────────────────────────────────


def test_harness_session_marker_table_matches_relay_source(external):
    """세 표면의 상한 판정 입력 표는 relay 단일 선언을 그대로 참조한다."""
    delegate = _load("pm_delegate")
    relay = _load("pm_relay")
    external_relay = external._load_relay()
    delegate_relay = delegate._load_relay()
    assert relay.HARNESS_SESSION_MARKERS == external_relay.HARNESS_SESSION_MARKERS == \
        delegate_relay.HARNESS_SESSION_MARKERS
    assert relay.HARNESS_CAP_ENV == external_relay.HARNESS_CAP_ENV == \
        delegate_relay.HARNESS_CAP_ENV
    assert set(external_relay.HARNESS_CAP_ENV) == set(external_relay.HARNESS_SESSION_MARKERS)
    assert set(delegate_relay.HARNESS_CAP_ENV) == set(delegate_relay.HARNESS_SESSION_MARKERS)
    assert external_relay.HARNESS_CAP_ENV == delegate_relay.HARNESS_CAP_ENV
    def assert_prefilter_coverage(marker_table):
        declared_markers = {
            marker
            for markers in marker_table.values()
            for marker in markers
        }
        assert all(external._is_possible_harness_session_key(marker)
                   for marker in declared_markers)

    assert_prefilter_coverage(relay.HARNESS_SESSION_MARKERS)
    with pytest.raises(AssertionError):
        assert_prefilter_coverage({
            **relay.HARNESS_SESSION_MARKERS,
            "future": ("GEMINI_SESSION",),
        })


@pytest.mark.parametrize("key", ("OPENCODE_CONFIG", "CLAUDE_CONFIG_DIR",
                                  "OPENCODE_CONFIG_DIR"))
def test_harness_cap_advisory_ignores_config_and_unmeasured_keys(external, key):
    """설정 경로와 세션 근거 없는 키만으로는 호출층 상한 안내를 내지 않는다."""
    assert external.harness_cap_advisory(
        {key: "configured"}, execution_budget=10,
    ) is None


def test_harness_cap_advisory_does_not_load_relay_without_session_marker(monkeypatch):
    """비하네스 셸은 advisory 계산/로더 실패 표면을 만들지 않고 즉시 반환한다."""
    spec = importlib.util.spec_from_file_location(
        "external_review_no_relay", TOOLS / "external_review.py",
    )
    module = importlib.util.module_from_spec(spec)
    real_spec_from_file_location = importlib.util.spec_from_file_location

    def reject_relay(name, location, *args, **kwargs):
        if Path(location).name == "pm_relay.py":
            raise AssertionError("relay must not load")
        return real_spec_from_file_location(name, location, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "spec_from_file_location", reject_relay)
    spec.loader.exec_module(module)
    monkeypatch.setattr(
        module, "_load_relay",
        lambda: (_ for _ in ()).throw(AssertionError("relay must not load")),
    )
    assert module.harness_cap_advisory(
        {"OPENCODE_CONFIG_DIR": "/config"}, execution_budget=10,
    ) is None


def test_harness_cap_advisory_warns_for_all_nested_session_axes(external, monkeypatch):
    """중첩 세션의 공개 호출층 상한을 모두 검사해 경고를 합친다."""
    warning = external.harness_cap_advisory(
        {"OPENCODE": "child", "CLAUDECODE": "parent"},
        execution_budget=10,
    )
    assert warning is not None
    assert warning.count("[external-review] 경고:") == 2
    assert "BASH_MAX_TIMEOUT_MS" in warning
    assert "OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS" in warning


def test_harness_cap_advisory_keeps_claude_axis_with_opencode_config(
        external, monkeypatch):
    """OpenCode 설정 경로가 섞여도 Claude 세션의 호출층 상한을 진단한다."""
    warning = external.harness_cap_advisory(
        {"CLAUDECODE": "session", "OPENCODE_CONFIG_DIR": "/config/opencode"},
        execution_budget=10,
    )
    assert warning is not None
    assert "claude" in warning and "BASH_MAX_TIMEOUT_MS" in warning


def test_harness_cap_advisory_accepts_secondary_opencode_session_marker(
        external, monkeypatch):
    """보조 OpenCode 세션 마커도 공용 표 계약에 따라 OpenCode 상한을 안내한다."""
    warning = external.harness_cap_advisory(
        {"OPENCODE_PID": "123"}, execution_budget=10,
    )
    assert warning is not None
    assert "OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS" in warning


def test_timeout_output_routes_through_shared_partial_formatter(external, monkeypatch):
    """리뷰 소비처를 옛 로컬 결합기로 되돌리면 이 seam 감지가 실패한다."""
    class Relay:
        @staticmethod
        def format_partial_output(head, exc):
            return f"shared:{head}:{exc.output}"

    monkeypatch.setattr(external, "_load_relay", lambda: Relay())
    exc = subprocess.TimeoutExpired(["reviewer"], 1, output="partial")
    assert external._timeout_output(1, exc).endswith(":partial")


@pytest.mark.parametrize("failure", (RuntimeError("loader"), ValueError("formatter")))
def test_timeout_output_keeps_head_when_partial_formatter_fails(
        external, monkeypatch, failure):
    """이미 timeout인 리뷰는 relay 로더·포맷터 실패로 CLI 밖으로 튀지 않는다."""
    if isinstance(failure, RuntimeError):
        monkeypatch.setattr(external, "_load_relay", lambda: (_ for _ in ()).throw(failure))
    else:
        class BrokenRelay:
            @staticmethod
            def format_partial_output(head, exc):
                raise failure
        monkeypatch.setattr(external, "_load_relay", lambda: BrokenRelay())
    exc = subprocess.TimeoutExpired(["reviewer"], 1, output="partial")
    assert external._timeout_output(1, exc).startswith("[리뷰어 타임아웃")


@pytest.mark.parametrize("failure_site", ("loader", "formatter"))
def test_timeout_output_reraises_engine_rev_skew(external, monkeypatch, failure_site):
    """rev skew는 기존 loud 계약대로 timeout 포맷 경로에서도 숨기지 않는다."""
    skew = RuntimeError("skew")
    skew._engine_rev_skew = True
    if failure_site == "loader":
        monkeypatch.setattr(
            external, "_load_relay",
            lambda: (_ for _ in ()).throw(skew),
        )
    else:
        class BrokenRelay:
            @staticmethod
            def format_partial_output(head, exc):
                raise skew

        monkeypatch.setattr(external, "_load_relay", lambda: BrokenRelay())
    with pytest.raises(RuntimeError, match="skew"):
        external._timeout_output(1, subprocess.TimeoutExpired(["reviewer"], 1))


@pytest.mark.parametrize("output, expected", [
    ("", "[리뷰어 타임아웃"),
    ("x" * 10000, "x" * 10000),
    (b"ok\xff", "ok�"),
])
def test_timeout_output_shared_formatter_policy(external, output, expected):
    """변경된 external-review 소비처에도 무절단·bytes 정책을 고정한다."""
    rendered = external._timeout_output(1, subprocess.TimeoutExpired(["reviewer"], 1, output=output))
    assert rendered.startswith("[리뷰어 타임아웃")
    assert expected in rendered


# ── 순수 헬퍼: 상한 파싱 (_round_limit) ─────────────────────────────────────


def test_round_limit_default(external):
    """미설정 → DEFAULT_ROUND_LIMIT(4·사용자 전역 규율 ">3~4" 기계화)."""
    assert external._round_limit({}) == external.DEFAULT_ROUND_LIMIT == 4


def test_round_limit_knob_override(external):
    """local.conf external_review_round_limit 노브가 상한을 바꾼다."""
    assert external._round_limit({"external_review_round_limit": "2"}) == 2
    assert external._round_limit({"external_review_round_limit": "10"}) == 10


def test_round_limit_garbage_and_negative_fall_back(external):
    """비정수·음수는 기본값으로 fail-soft (깨진 노브가 게이트를 벽돌로 만들지 않음)."""
    assert external._round_limit({"external_review_round_limit": "x"}) == 4
    assert external._round_limit({"external_review_round_limit": "-3"}) == 4


# ── 순수 헬퍼: 외부 리뷰 timeout 해소 (T-0467) ───────────────────────────────


def test_timeout_cli_override_beats_local_conf(external):
    """명시 CLI 양수값은 local.conf 설정보다 우선한다."""
    args = external.argparse.Namespace(timeout=37)
    assert external._resolve_timeout(args, {"external_review_timeout": "71"}) == 37


def test_timeout_uses_local_conf_when_cli_unspecified(external):
    """CLI 미지정(None) 시 clone별 external_review_timeout 을 사용한다."""
    args = external.argparse.Namespace(timeout=None)
    assert external._resolve_timeout(args, {"external_review_timeout": "71"}) == 71


def _declared_wall(external) -> int:
    """리뷰어 기본 커맨드(codex 축)의 선언 벽시계 백스톱 — 별도 상수가 아니라 프로필에서 온다."""
    return int(external.reviewer_profile(external.DEFAULT_REVIEWER_CMD, {}).wall_timeout)


def test_timeout_uses_default_when_not_configured(external):
    """CLI/conf 모두 없으면 리뷰어 하네스 프로필의 유한 백스톱을 쓴다(T-0489 — 상수 폐지)."""
    args = external.argparse.Namespace(timeout=None)
    assert external._resolve_timeout(args, {}) == _declared_wall(external)


@pytest.mark.parametrize("raw", ["abc", "0", "-1"])
def test_timeout_invalid_conf_warns_and_falls_back(external, capsys, raw):
    """비수치/0/음수 conf 는 경고 뒤 선언 기본값으로 fail-soft 한다."""
    args = external.argparse.Namespace(timeout=None)
    assert external._resolve_timeout(args, {"external_review_timeout": raw}) == \
        _declared_wall(external)
    warning = capsys.readouterr().err
    assert "external_review_timeout" in warning
    assert "기본" in warning


def test_timeout_default_floor_pins_measurement_basis(external):
    """기본값 하한 — 실측(평상 153~294s) 기반 상향이 180 회귀로 되돌지 않게 고정."""
    assert _declared_wall(external) >= 600


def test_timeout_cli_nonpositive_usage_error(external, capsys):
    """CLI `--timeout` 0/음수는 usage error(rc=2) — pm_delegate 선례 파리티(T-0467)."""
    for raw in ("0", "-5"):
        with pytest.raises(SystemExit) as exc:
            external.main(["--timeout", raw, "--dry-run"])
        assert exc.value.code == 2
        assert "--timeout" in capsys.readouterr().err


def test_print_summary_failure_shows_reason_line(external, capsys):
    """실패 요약의 판정 라인에 실패 사유 1줄(타임아웃 안내 포함)이 병기된다 (T-0467)."""
    external.print_summary({
        "reviewer": "codex", "ok": False, "failed": True, "any_must_fix": False,
        "all_pass": False, "verdict": None, "file": None,
        "output": "[리뷰어 타임아웃 — 900초 초과] 재시도: `--timeout <초>` 또는 "
                  "local.conf `external_review_timeout=<초>` (양의 정수).\n[stderr]\nx",
    })
    out = capsys.readouterr().out
    assert "사유: [리뷰어 타임아웃" in out
    assert "--timeout" in out
    assert "FALLBACK_INTERNAL" in out


# ── 순수 헬퍼: 장부 (_round_ledger_path / load / save / _gate_entry) ─────────


def test_ledger_path_derives_from_repo(external, tmp_path, monkeypatch):
    """장부 경로 = `<REPO>/.project_manager/.local/review_rounds.json` (호출 시점 REPO·per-clone)."""
    monkeypatch.setattr(external, "REPO", tmp_path)
    assert external._round_ledger_path() == \
        tmp_path / ".project_manager" / ".local" / "review_rounds.json"


def test_ledger_load_missing_is_empty(external, tmp_path, monkeypatch):
    """장부 파일 부재 → 빈 dict (fail-soft)."""
    monkeypatch.setattr(external, "REPO", tmp_path)
    assert external._load_round_ledger() == {}


def test_ledger_save_load_roundtrip(external, tmp_path, monkeypatch):
    """save → load 왕복 정합 · `.local/` 아래 실 파일 생성(디렉토리 자동 생성)."""
    monkeypatch.setattr(external, "REPO", tmp_path)
    external._save_round_ledger({"T-0001": {"count": 3, "acked_through": 1}})
    assert external._round_ledger_path().is_file()
    assert external._load_round_ledger() == {"T-0001": {"count": 3, "acked_through": 1}}


def test_ledger_corrupt_falls_back_to_empty(external, tmp_path, monkeypatch):
    """손상 JSON → 빈 dict (regression flag 동형 fail-soft — 장부 손상이 게이트를 깨지 않음)."""
    monkeypatch.setattr(external, "REPO", tmp_path)
    path = external._round_ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")
    assert external._load_round_ledger() == {}


def test_gate_entry_normalizes_missing_and_corrupt(external):
    """`_gate_entry` — 부재/손상 항목을 0/0 으로 정규화하고 ledger 에 심는다."""
    gate_one, gate_two, gate_three = ("T-" + suffix for suffix in ("0001", "0002", "0003"))
    ledger: dict = {gate_two: {"count": "bad", "acked_through": None}, gate_three: 7}
    assert external._gate_entry(ledger, gate_one) == {
        "count": 0, "acked_through": 0, "sequence": 0, "records": [],
    }
    assert external._gate_entry(ledger, gate_two) == {
        "count": 0, "acked_through": 0, "sequence": 0, "records": [],
    }
    assert external._gate_entry(ledger, gate_three) == {
        "count": 0, "acked_through": 0, "sequence": 0, "records": [],
    }
    # 정규화 결과가 ledger 에 심겨 후속 save 가 깨끗한 값을 기록한다.
    assert ledger[gate_one] == {
        "count": 0, "acked_through": 0, "sequence": 0, "records": [],
    }

    ledger["mixed-records"] = {
        "count": "bad",
        "acked_through": None,
        "records": ["junk", {"sequence": "3", "verdict": True}, 7],
    }
    assert external._gate_entry(ledger, "mixed-records") == {
        "count": 0,
        "acked_through": 0,
        "sequence": 3,
        "records": [{"sequence": "3", "verdict": True}],
    }


def test_refund_keeps_concurrent_reservation_accounting_consistent(external):
    ledger = {}
    entry = external._gate_entry(ledger, "gate")
    external._reserve_round(entry, "first")
    external._reserve_round(entry, "second")
    external._refund_round(entry, "first")
    entry["records"][0]["verdict"] = True

    assert entry["count"] == 1
    assert entry["sequence"] == 2
    assert entry["records"][0]["number"] == 2
    assert external._unacked_round_counts(entry) == (1, 1, 0)

    external._reserve_round(entry, "third")
    assert [row["number"] for row in entry["records"]] == [2, 3]


def test_failed_result_with_partial_verdict_text_is_still_incomplete(external):
    result = {
        "ok": False,
        "verdict": {"has_pass": True, "has_must_fix": False},
    }
    assert external._round_has_verdict(result) is False


# ── started 신호: 스폰 여부 판정 (_run_reviewer_ex·MF-A 근원) ────────────────
# main() 게이트 테스트는 run_review 를 mock 하므로 started 매핑을 직접 태우지 않는다 — 여기서
# 실제 스폰-여부 판정(환불 대상 = 확실히 전송 전 실패만)을 run_fn 주입으로 단언한다.


def _completed(rc, out="판정: 통과"):
    import subprocess
    return subprocess.CompletedProcess(args=["codex"], returncode=rc, stdout=out, stderr="")


def test_started_true_on_success_and_nonzero_rc(external):
    """정상 종료(성공/비-0 rc)는 프로세스가 실행됐으므로 started=True (전송·과금 가능)."""
    ok, _o, started = external._run_reviewer_ex("p", "codex", 5, lambda *a, **k: _completed(0))
    assert (ok, started) == (True, True)
    ok, _o, started = external._run_reviewer_ex("p", "codex", 5, lambda *a, **k: _completed(1))
    assert (ok, started) == (False, True)  # 실패지만 스폰됨 → 환불 대상 아님


def test_started_true_on_timeout(external):
    """타임아웃은 프로세스가 시작돼 전송됐을 수 있으므로 started=True (카운트 유지·MF-A 핵심)."""
    def _raise(*a, **k):
        import subprocess
        raise subprocess.TimeoutExpired(cmd="codex", timeout=5)
    ok, out, started = external._run_reviewer_ex("p", "codex", 5, _raise)
    assert ok is False and started is True and "타임아웃" in out
    assert "--timeout <초>" in out
    assert "external_review_timeout=<초>" in out


def test_started_false_on_spawn_failures(external):
    """스폰-전 실패(빈 cmd·실행 파일 부재)는 전송 0 → started=False (환불 대상)."""
    ok, _o, started = external._run_reviewer_ex("p", "", 5, None)  # 빈 argv
    assert (ok, started) == (False, False)

    def _fnf(*a, **k):
        raise FileNotFoundError("codex")
    ok, _o, started = external._run_reviewer_ex("p", "codex", 5, _fnf)
    assert (ok, started) == (False, False)


def test_started_true_on_generic_error_conservative(external):
    """기타 실행 오류는 시작 여부 불확실 → 보수적으로 started=True (상한 우회 방지 > 과잉 카운트)."""
    def _boom(*a, **k):
        raise RuntimeError("weird")
    ok, _o, started = external._run_reviewer_ex("p", "codex", 5, _boom)
    assert (ok, started) == (False, True)


def test_strict_legacy_runner_does_not_receive_idle_timeout(external):
    """엄격한 기존 subprocess.run 호환 seam 에 새 키를 밀어 넣지 않는다."""
    seen = {}

    def strict(argv, *, input, capture_output, text, encoding, errors, timeout):
        seen.update(locals())
        return _completed(0)

    ok, _out, started = external._run_reviewer_ex("p", "codex", 5, strict)
    assert (ok, started) == (True, True)
    assert "idle_timeout" not in seen


def test_real_subprocess_run_remains_a_valid_injected_runner(external):
    """대표 strict 기존 러너 subprocess.run 을 실제로 태워 호출 전 TypeError 회귀를 막는다."""
    ok, _out, started = external._run_reviewer_ex(
        "prompt", f"{sys.executable} -c pass", 5, subprocess.run)
    assert (ok, started) == (True, True)


def test_explicit_idle_runner_receives_new_keyword(external):
    seen = {}

    def idle_aware(argv, *, input, capture_output, text, encoding, errors, timeout,
                   idle_timeout):
        seen["idle_timeout"] = idle_timeout
        return _completed(0)

    ok, _out, _started = external._run_reviewer_ex("p", "codex", 5, idle_aware, 37)
    assert ok is True and seen["idle_timeout"] == 37


def test_runner_seam_skew_is_loud_and_distinct(external):
    """호출 전 seam skew 는 일반 리뷰어 실행 오류/라운드 소비로 silent degrade 하지 않는다."""
    def incompatible(argv, *, required_new_contract):
        raise AssertionError("bind 전에 호출되면 안 됨")

    ok, out, started = external._run_reviewer_ex("p", "codex", 5, incompatible)
    assert (ok, started) == (False, False)
    assert "runner seam 계약 오류" in out
    assert "리뷰어 실행 오류" not in out


def test_run_reviewer_remains_two_tuple_facade(external):
    """run_reviewer 공개 API 는 여전히 2-튜플(성공, 출력) — 내부 split 이 계약을 깨지 않는다."""
    result = external.run_reviewer("p", reviewer_cmd="codex", run_fn=lambda *a, **k: _completed(0))
    assert isinstance(result, tuple) and len(result) == 2
    assert result[0] is True


def test_run_review_surfaces_started(external, tmp_path):
    """run_review 결과 dict 가 started 를 성공·실패(타임아웃) 양쪽에서 실어 나른다 (환불 판정 입력).

    main() 게이트 테스트는 run_review 를 mock 하므로, 여기서 실제 run_review 가 _run_reviewer_ex 의
    started 를 dict 로 전달함을 성공(True)과 타임아웃(실패지만 True)에서 단언해 seam 을 닫는다."""
    res = external.run_review("p", reviewer_cmd="codex", output_dir=tmp_path,
                              run_fn=lambda *a, **k: _completed(0))
    assert res["started"] is True and res["failed"] is False

    def _timeout(*a, **k):
        import subprocess
        raise subprocess.TimeoutExpired(cmd="codex", timeout=5)
    res = external.run_review("p", reviewer_cmd="codex", output_dir=tmp_path, run_fn=_timeout)
    assert res["failed"] is True and res["started"] is True  # 타임아웃=전송 가능 → 카운트 유지


# ── main() 게이트 harness ───────────────────────────────────────────────────


def _wire(external, monkeypatch, tmp_path, *, conf=None,
          diff="diff --git a/x b/x\n@@ -1 +1 @@\n-o\n+n\n", result=None):
    """main() 을 tmp REPO 로 격리 배선 — 외부 리뷰어 호출 횟수를 세는 counter 반환.

    REPO=tmp(장부 격리)·local_config(활성)·extract_diff(diff 주입·제외 없음)·run_review 를
    module-level 로 monkeypatch 한다. `result` 미지정이면 통과(started=True) 결과를, 지정하면 그
    dict 를 돌려준다(타임아웃=started True·스폰실패=started False 시나리오 주입). 반환 calls['n'] =
    run_review 호출 수(=외부 전송 시도)."""
    monkeypatch.setattr(external, "REPO", tmp_path)
    monkeypatch.setattr(
        external, "local_config",
        lambda repo=None: dict(conf) if conf is not None else {"external_review_enabled": "true"})
    monkeypatch.setattr(external, "extract_diff", lambda *a, **k: (diff, []))
    real_main = external.main

    def _isolated_main(argv=None):
        isolated_argv = list(argv or [])
        if "--output-dir" not in isolated_argv:
            isolated_argv += ["--output-dir", str(tmp_path / "raw")]
        return real_main(isolated_argv)

    monkeypatch.setattr(external, "main", _isolated_main)
    calls = {"n": 0}
    ok = {"reviewer": "x", "ok": True, "output": "판정: 통과",
          "verdict": {"has_must_fix": False, "has_pass": True}, "file": None,
          "failed": False, "started": True, "any_must_fix": False, "all_pass": True}

    def _run_review(*a, **k):
        calls["n"] += 1
        return dict(result) if result is not None else dict(ok)

    monkeypatch.setattr(external, "run_review", _run_review)
    return calls


# run_review 실패 결과 템플릿 (started 로 전송 여부를 구분 — MF-A).
_FAIL_STARTED = {"reviewer": "x", "ok": False, "output": "[리뷰어 타임아웃 — 180초 초과]",
                 "verdict": {"has_must_fix": False, "has_pass": False}, "file": None,
                 "failed": True, "started": True, "any_must_fix": False, "all_pass": False}
_FAIL_UNSTARTED = {"reviewer": "x", "ok": False, "output": "[리뷰어 명령 없음]",
                   "verdict": {"has_must_fix": False, "has_pass": False}, "file": None,
                   "failed": True, "started": False, "any_must_fix": False, "all_pass": False}


def _ledger(external, tmp_path):
    path = tmp_path / ".project_manager" / ".local" / "review_rounds.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def test_valid_json_with_corrupt_records_still_runs_gate(
    external, monkeypatch, tmp_path,
):
    calls = _wire(external, monkeypatch, tmp_path)
    ledger_path = tmp_path / ".project_manager" / ".local" / "review_rounds.json"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(
        json.dumps({"corrupt-gate": {
            "count": "bad",
            "acked_through": None,
            "records": ["junk"],
        }}),
        encoding="utf-8",
    )

    assert external.main(["--gate", "corrupt-gate", "--paths", "x.py"]) == 0
    assert calls["n"] == 1
    entry = _ledger(external, tmp_path)["corrupt-gate"]
    assert entry["count"] == 1
    assert len(entry["records"]) == 1
    assert entry["records"][0]["verdict"] is True


# ── red-first: 4회 정상 → 5회째 거부 (DoD) ──────────────────────────────────


def test_fifth_round_refused_before_reviewer(external, monkeypatch, tmp_path, capsys):
    """같은 --gate 5회째(기본 limit=4 초과) 실 실행이 거부되고 리뷰어는 호출되지 않는다 (red-첫).

    rounds 1..4 는 정상(rc=0·리뷰어 호출·count 누적), round 5 는 실행 전 전용 rc 로 거부되고 외부
    전송(리뷰어)이 일어나지 않음을 단언한다 — 과금 초과분을 기계가 멈춘다."""
    calls = _wire(external, monkeypatch, tmp_path)
    argv = ["--gate", "T-0100", "--paths", "x.py"]

    for i in range(1, 5):  # 1..4 정상
        assert external.main(argv) == 0, f"round {i} 는 정상이어야 한다"
    assert calls["n"] == 4
    entry = _ledger(external, tmp_path)[argv[1]]
    assert (entry["count"], entry["acked_through"]) == (4, 0)
    assert len(entry["records"]) == 4 and all(row["verdict"] for row in entry["records"])

    rc = external.main(argv)  # 5회째 거부
    assert rc == external.EXIT_ROUND_LIMIT_EXCEEDED
    assert rc not in (0, 1)  # 기존 0/1 과 구분되는 전용 rc
    assert calls["n"] == 4   # 리뷰어 미호출 (외부 전송 없음·과금 차단)
    # count 는 거부된 라운드로 늘지 않는다 (실 전송만 count).
    entry = _ledger(external, tmp_path)[argv[1]]
    assert (entry["count"], entry["acked_through"]) == (4, 0)
    err = capsys.readouterr().err
    assert "라운드 상한 초과" in err
    assert "사용자" in err and "대기" in err  # 보고·대기 안내
    assert "--ack-rounds" in err              # 재개 경로


# ── --ack-rounds 승인 재개 + 재차단 (DoD·멱등·장부 정합) ─────────────────────


def test_ack_rounds_resumes_then_reblocks(external, monkeypatch, tmp_path, capsys):
    """`--ack-rounds` 승인 재개 → +limit 창 소진 시 재차단 (장부 정합).

    거부 상태에서 ack 호출은 acked_through 를 현 count 로 올리고 그 호출 자체는 정상 진행(=재개 창의
    첫 라운드). 이후 limit 을 다시 소진하면 재차단된다."""
    calls = _wire(external, monkeypatch, tmp_path)
    argv = ["--gate", "T-0101", "--paths", "x.py"]
    for _ in range(4):
        external.main(argv)
    assert external.main(argv) == external.EXIT_ROUND_LIMIT_EXCEEDED  # 차단 확인

    # ack 재개 — acked_through=count(4), 그 호출은 정상 진행(count→5).
    rc = external.main(argv + ["--ack-rounds"])
    assert rc == 0
    assert calls["n"] == 5  # ack 호출이 실 전송으로 진행됨
    entry = _ledger(external, tmp_path)[argv[1]]
    assert (entry["count"], entry["acked_through"]) == (5, 4)
    assert "승인 재개" in capsys.readouterr().err

    # 재개 창에서 +limit(=4) 소진: ack 라운드(#5) 뒤 3라운드(#6,7,8) 더 정상 → #9 재차단.
    for i in range(3):
        assert external.main(argv) == 0, f"재개 후 round {i} 는 정상이어야 한다"
    entry = _ledger(external, tmp_path)[argv[1]]
    assert (entry["count"], entry["acked_through"]) == (8, 4)
    assert external.main(argv) == external.EXIT_ROUND_LIMIT_EXCEEDED  # 재차단
    assert calls["n"] == 8  # 재차단 라운드는 리뷰어 미호출


# ── 카운트 제외 3분기 (dry-run·빈-diff·리뷰어 실패) ─────────────────────────


def test_dry_run_excluded_from_count(external, monkeypatch, tmp_path):
    """dry-run 은 외부 전송이 없어 라운드가 아니다 — 리뷰어 미호출·장부 미기록(무한 반복 무영향)."""
    calls = _wire(external, monkeypatch, tmp_path)
    for _ in range(6):
        assert external.main(["--gate", "T-0102", "--paths", "x.py", "--dry-run"]) == 0
    assert calls["n"] == 0
    assert _ledger(external, tmp_path) == {}  # 장부 미생성


def test_empty_diff_excluded_from_count(external, monkeypatch, tmp_path):
    """빈-diff 거부(T-0326)는 전송 전 fail 이라 카운트 제외 — 리뷰어 미호출·장부 미기록."""
    calls = _wire(external, monkeypatch, tmp_path, diff="")
    for _ in range(6):
        assert external.main(["--gate", "T-0103", "--paths", "x.py", "--force"]) == 1
    assert calls["n"] == 0
    assert _ledger(external, tmp_path) == {}


def test_timeout_uses_separate_incomplete_retry_limit(external, monkeypatch, tmp_path, capsys):
    """판정 없는 종료는 별도 재시도 예산 2회를 쓰고 판정 상한은 소진하지 않는다.

    프롬프트가 이미 전송·과금됐을 수 있으므로 실패여도 예약을 환불하지 않는다. 두 번 모두 종료
    마감되지만 verdict=false이고, 세 번째 호출은 reviewer 전에 차단된다."""
    calls = _wire(external, monkeypatch, tmp_path, result=_FAIL_STARTED)
    argv = ["--gate", "T-0104", "--paths", "x.py"]
    for i in range(1, 3):
        assert external.main(argv) == 1, f"round {i} 는 FALLBACK(rc=1)"
    assert calls["n"] == 2
    entry = _ledger(external, tmp_path)[argv[1]]
    assert (entry["count"], entry["acked_through"]) == (2, 0)
    assert all(row["finished_at"] and not row["verdict"] for row in entry["records"])

    rc = external.main(argv)
    assert rc == external.EXIT_ROUND_LIMIT_EXCEEDED
    assert calls["n"] == 2
    err = capsys.readouterr().err
    assert "count=2(판정 0 · 미완 2)" in err


def test_spawn_failure_refunds_reservation(external, monkeypatch, tmp_path):
    """스폰-전 실패(started=False·전송 0·과금 0)는 예약을 환불한다 — 상한을 소진하지 않는다.

    MF-A: 외부 프로세스가 확실히 시작되지 않았으면(실행 파일 부재 등) 아무것도 전송되지 않았으므로
    예약한 라운드를 되돌린다. 6회 스폰 실패해도 count 는 0 으로 남아 정상 리뷰가 조기 차단되지 않음을
    단언한다(설치/PATH 문제로 게이트가 막히면 안 된다)."""
    calls = _wire(external, monkeypatch, tmp_path, result=_FAIL_UNSTARTED)
    for _ in range(6):
        assert external.main(["--gate", "T-0114", "--paths", "x.py"]) == 1
    assert calls["n"] == 6                       # 전송은 시도됨(리뷰어 호출)
    assert _ledger(external, tmp_path)["T-0114"]["count"] == 0  # 예약 환불 → never blocked


def test_killed_main_leaves_machine_counted_unfinished_record(
    external, monkeypatch, tmp_path,
):
    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("process kill ledger test requires fork")
    _wire(external, monkeypatch, tmp_path)

    def _block(*args, **kwargs):
        time.sleep(30)

    monkeypatch.setattr(external, "run_review", _block)
    gate = "kill-gate"
    process = multiprocessing.get_context("fork").Process(
        target=external.main,
        args=(["--gate", gate, "--paths", "x.py"],),
    )
    process.start()
    deadline = time.monotonic() + 5
    entry = None
    while time.monotonic() < deadline:
        ledger = _ledger(external, tmp_path)
        entry = ledger.get(gate)
        if entry and entry.get("records"):
            break
        time.sleep(0.02)
    assert entry is not None and entry["records"]
    process.kill()
    process.join(timeout=5)
    assert not process.is_alive()

    entry = _ledger(external, tmp_path)[gate]
    assert "finished_at" not in entry["records"][0]
    assert external._unacked_round_counts(entry) == (1, 0, 1)


# ── local.conf 노브 · --gate 미지정 · ack-without-gate ──────────────────────


def test_round_limit_knob_changes_threshold(external, monkeypatch, tmp_path):
    """local.conf external_review_round_limit=2 → 3회째 거부 (노브 변경 반영·DoD)."""
    conf = {"external_review_enabled": "true", "external_review_round_limit": "2"}
    calls = _wire(external, monkeypatch, tmp_path, conf=conf)
    argv = ["--gate", "T-0105", "--paths", "x.py"]
    assert external.main(argv) == 0
    assert external.main(argv) == 0
    assert external.main(argv) == external.EXIT_ROUND_LIMIT_EXCEEDED  # 3회째 거부
    assert calls["n"] == 2


def test_no_gate_is_not_ledgered(external, monkeypatch, tmp_path):
    """`--gate` 미지정 실행은 상한 대상 밖 — 무제한 진행·장부 미생성 (gate 단위 장부)."""
    calls = _wire(external, monkeypatch, tmp_path)
    for _ in range(8):
        assert external.main(["--paths", "x.py"]) == 0
    assert calls["n"] == 8
    assert _ledger(external, tmp_path) == {}


def test_ack_rounds_without_gate_warns_and_proceeds(external, monkeypatch, tmp_path, capsys):
    """`--ack-rounds` 를 --gate 없이 쓰면 경고 후 정상 진행 (장부 대상 아님·무해)."""
    _wire(external, monkeypatch, tmp_path)
    assert external.main(["--ack-rounds", "--paths", "x.py"]) == 0
    assert "--gate 와 함께" in capsys.readouterr().err
    assert _ledger(external, tmp_path) == {}


# ── 장부 위치: per-clone `.local/` (git-ignored) ────────────────────────────


def test_ledger_written_to_local_scratch(external, monkeypatch, tmp_path):
    """장부는 per-clone `.project_manager/.local/review_rounds.json` 에 기록된다 (board 오염 없음)."""
    _wire(external, monkeypatch, tmp_path)
    external.main(["--gate", "T-0106", "--paths", "x.py"])
    ledger_file = tmp_path / ".project_manager" / ".local" / "review_rounds.json"
    assert ledger_file.is_file()
    assert json.loads(ledger_file.read_text(encoding="utf-8"))["T-0106"]["count"] == 1


# ── 원자 write · 임계 구역 (MF-B) ───────────────────────────────────────────


def test_save_ledger_uses_unique_tmp_names(external, tmp_path, monkeypatch):
    """저장 tmp 는 pid+uuid 로 unique — 고정 `.tmp` 충돌(카운트 유실·write 예외) 없음 (MF-B).

    os.replace 를 가로채 tmp 경로를 캡처한다. 두 번 저장하면 서로 다른 tmp 이름(pid 포함)이라
    동시 실행이 같은 임시 파일을 밟지 않는다 — 순차 호출로 충돌 부재를 단언(실 동시성 스트레스 불요)."""
    monkeypatch.setattr(external, "REPO", tmp_path)
    seen: list[str] = []
    monkeypatch.setattr(external.os, "replace", lambda src, dst: seen.append(str(src)))
    external._save_round_ledger({"a": 1})
    external._save_round_ledger({"b": 2})
    assert len(seen) == 2 and seen[0] != seen[1]                 # unique (고정 .tmp 아님)
    assert all(f".{external.os.getpid()}." in s for s in seen)   # pid 포함
    assert all(s.endswith(".tmp") for s in seen)


def test_ledger_lock_acquires_and_releases(external, tmp_path, monkeypatch):
    """`_round_ledger_lock()` 이 락 파일을 만들고 획득·해제한다 — 순차(비중첩) 재획득 가능 (MF-B).

    확인→예약→저장을 이 컨텍스트로 감싸 동시 실행을 직렬화한다. 재진입 금지(board_lock 관례)이므로
    예약/환불은 각자 독립 구간 — 여기선 순차 재획득이 데드락 없이 됨을 단언한다."""
    monkeypatch.setattr(external, "REPO", tmp_path)
    with external._round_ledger_lock():
        assert external._round_ledger_lock_path().exists()  # 락 파일 생성
    with external._round_ledger_lock():                     # 해제 후 재획득(순차)
        pass


def test_reserve_check_save_under_single_lock(external, monkeypatch, tmp_path):
    """게이트 예약 경로가 단일 임계 구역(확인·예약·저장)을 통과함을 장부 정합으로 확인 (MF-B).

    lock 컨텍스트를 계측해 게이트 통과당 정확히 한 번 잡히고(중첩 아님) 예약이 원자적으로 기록됨을
    단언한다 — 잔여 슬롯 이중 통과(무락 RMW)로 인한 상한 우회를 닫는다."""
    _wire(external, monkeypatch, tmp_path)
    depth = {"cur": 0, "max": 0, "enters": 0}
    real_lock = external._round_ledger_lock

    import contextlib

    @contextlib.contextmanager
    def _instrumented():
        depth["cur"] += 1
        depth["enters"] += 1
        depth["max"] = max(depth["max"], depth["cur"])
        try:
            with real_lock():
                yield
        finally:
            depth["cur"] -= 1

    monkeypatch.setattr(external, "_round_ledger_lock", _instrumented)
    assert external.main(["--gate", "T-0116", "--paths", "x.py"]) == 0
    assert depth["max"] == 1          # 재진입/중첩 없음 (예약 구간 원자)
    assert depth["enters"] == 2       # 실행 전 등재 + 종료 시 마감
    assert _ledger(external, tmp_path)["T-0116"]["count"] == 1


def test_save_output_tempdir_fallback_with_injected_destination(
        external, monkeypatch, tmp_path):
    """PM 홈 미해소 폴백은 유지하되 테스트에서는 pytest 관리 목적지를 주입한다."""
    monkeypatch.setattr(external, "REPO", tmp_path / "unresolved-adopter")
    monkeypatch.setattr(external.tempfile, "gettempdir", lambda: str(tmp_path))
    dest = external.save_output("x", "fallback content")
    assert dest.parent == tmp_path
    assert dest.read_text(encoding="utf-8") == "fallback content"
