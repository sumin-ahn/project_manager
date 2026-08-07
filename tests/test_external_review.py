"""external_review 라운드 상한 게이트 — codex 게이트 무한 라운드 기계 차단 (T-0457).

외부 리뷰(codex)는 과금·외부 전송 게이트라 라운드가 무한정 이어지면 비용이 쌓인다(PM 10차 실측:
한 게이트 클러스터 25라운드). PM 자의 "수렴 판단"을 기계 판정으로 대체한다
([[mechanize-dont-instruct-llm]]): `--gate <T-NNNN>` 별 라운드 장부
(`.project_manager/.local/review_rounds.json`·per-clone·git-ignored)에 실 전송 count 를 쌓고,
승인 없이 limit(local.conf `external_review_round_limit`·기본 4)을 넘기면 실행 *전에* 거부(전용 rc
`EXIT_ROUND_LIMIT_EXCEEDED`)하고 "사용자 보고·대기" loud 안내를 낸다. 사용자 승인 후 `--ack-rounds`
로만 재개(+limit 창).

T-0583 이 같은 장부에 두 축을 더한다: 라운드별 **산출**(`rounds` — 판정 rc·must-fix 수) append 와
게이트별 상한과 **별개**인 wave 단위 총 예산(`wave` 절 · local.conf `external_review_wave_budget`·
기본 24 · 소진 시 같은 rc 4 · `--ack-wave` 로만 재개). 조회면은 `--rounds-report`. 기록은 무조건
(파싱 불가는 null·리뷰 비차단)이고 hard 거부는 예산 축뿐이다.

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
import tempfile
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
    """장부 경로 = `<앵커>/.project_manager/.local/review_rounds.json` (호출 시점 해소·per-clone).

    앵커는 raw 장부와 같은 규칙 — 해소된 소유 PM 홈(`_PM_HOME_OVERRIDE`) 우선, 미주입이면
    엔진 자기 앵커 REPO 폴백.
    """
    monkeypatch.setattr(external, "REPO", tmp_path)
    assert external._round_ledger_path() == \
        tmp_path / ".project_manager" / ".local" / "review_rounds.json"

    pm_home = tmp_path / "pm-home"
    monkeypatch.setattr(external, "_PM_HOME_OVERRIDE", pm_home)
    assert external._round_ledger_path() == \
        pm_home / ".project_manager" / ".local" / "review_rounds.json"


def test_ledger_lock_path_follows_the_ledger_anchor(external, tmp_path, monkeypatch):
    """락 파일은 장부 경로에서 파생된다 — 앵커가 갈리면 같은 장부를 두 실행이 동시에 고쳐 쓴다."""
    monkeypatch.setattr(external, "REPO", tmp_path)
    assert external._round_ledger_lock_path() == \
        external._round_ledger_path().with_name("review_rounds.lock")

    pm_home = tmp_path / "pm-home"
    monkeypatch.setattr(external, "_PM_HOME_OVERRIDE", pm_home)
    assert external._round_ledger_lock_path() == \
        pm_home / ".project_manager" / ".local" / "review_rounds.lock"


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
    empty = {"count": 0, "acked_through": 0, "sequence": 0, "records": [], "rounds": []}
    gate_one, gate_two, gate_three = ("T-" + suffix for suffix in ("0001", "0002", "0003"))
    ledger: dict = {gate_two: {"count": "bad", "acked_through": None}, gate_three: 7}
    assert external._gate_entry(ledger, gate_one) == empty
    assert external._gate_entry(ledger, gate_two) == empty
    assert external._gate_entry(ledger, gate_three) == empty
    # 정규화 결과가 ledger 에 심겨 후속 save 가 깨끗한 값을 기록한다.
    assert ledger[gate_one] == empty

    ledger["mixed-records"] = {
        "count": "bad",
        "acked_through": None,
        "records": ["junk", {"sequence": "3", "verdict": True}, 7],
        "rounds": ["junk", {"ts": "2026-08-07T00:00:00+00:00", "verdict": 1}],
    }
    assert external._gate_entry(ledger, "mixed-records") == {
        "count": 0,
        "acked_through": 0,
        "sequence": 3,
        "records": [{"sequence": "3", "verdict": True}],
        "rounds": [{"ts": "2026-08-07T00:00:00+00:00", "verdict": 1}],
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
    # 산출물은 두 채널 구조다(T-0563) — 진단 본문은 회신 채널에 실린다.
    assert ok is False and started is True and "타임아웃" in out.answer
    assert "--timeout <초>" in out.answer
    assert "external_review_timeout=<초>" in out.answer


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
    assert "runner seam 계약 오류" in out.answer
    assert "리뷰어 실행 오류" not in out.answer


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


def _stub_reviewer_isolation(external, monkeypatch) -> None:
    """리뷰어 가시 범위 거울 생성을 스텁한다 (T-0563 · run_review 스텁과 같은 취지).

    이 파일의 게이트 테스트는 라운드 장부/앵커 분기만 보며 diff 를 주입하므로 tmp REPO 가 실제 git
    저장소가 아니다. 실 거울(격리) 회귀는 `test_external_review_reviewer_isolation.py` 가 실 저장소로
    소유한다 — 여기서는 격리 성립을 가정하고 그 아래 분기만 격리한다."""
    def _fake_workspace(diff_root, *, base_dir=None, conf=None, source_home=None,
                        denylist=()):
        return external.ReviewerWorkspace(
            root=Path(tempfile.mkdtemp(prefix="stub_reviewer_mirror_")),
            tree=Path(tempfile.mkdtemp(prefix="stub_reviewer_tree_")),
            home=Path(tempfile.mkdtemp(prefix="stub_reviewer_home_")),
            files=1, skipped_unsafe=0, git_repo=True,
        )

    monkeypatch.setattr(external, "create_reviewer_workspace", _fake_workspace)


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
    _stub_reviewer_isolation(external, monkeypatch)
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
    assert "라운드 상한 도달" in err
    # 상한의 성격은 무한 루프 차단이지 비용 재승인 요구가 아니다 — 안내는 조회면 확인 뒤 PM 자율
    # 재개를 지시하고, 사람 호출은 수렴 실패 때로 한정한다.
    assert "--rounds-report" in err           # 먼저 볼 조회면
    assert "--ack-rounds" in err              # 재개 경로
    assert "자율" in err                       # PM 자율 재개 (사용자 비용 승인 대기 아님)
    assert "수렴" in err                       # 사람 호출 조건 = 수렴 실패
    assert "대기" not in err                   # '사용자 승인 대기' 규율은 삭제됐다


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


def _flaky_round_lock(external, monkeypatch, *, fail_on: int):
    """`fail_on` 번째 진입에서만 OSError 를 내는 라운드 락 대역 (락 경합 재현).

    공용 seam 은 프리미티브 획득 실패를 삼키지 않고 올린다(무락 진행 폐지) — 그 예외를 두 구간
    (예약·마감)이 각각 어떻게 번역하는지 본다. Windows `msvcrt.locking` 재시도 소진이 실 유입원.
    """
    import contextlib as _contextlib

    real_lock = external._round_ledger_lock
    entries = {"n": 0}

    @_contextlib.contextmanager
    def _flaky():
        entries["n"] += 1
        if entries["n"] == fail_on:
            raise OSError(11, "resource temporarily unavailable")
        with real_lock():
            yield

    monkeypatch.setattr(external, "_round_ledger_lock", _flaky)
    return entries


def test_lock_failure_before_send_is_translated_and_blocks_the_send(
    external, monkeypatch, tmp_path, capsys,
):
    """예약 구간 락 실패 = **전송 전 중단**(과금 0) + 조치 문구 — 상한 미확인 전송 금지."""
    calls = _wire(external, monkeypatch, tmp_path)
    _flaky_round_lock(external, monkeypatch, fail_on=1)

    rc = external.main(["--gate", "T-0565", "--paths", "x.py"])

    err = capsys.readouterr().err
    assert rc == 1
    assert calls["n"] == 0                       # 외부 전송 시도 0
    assert "다른 게이트 실행이 장부 락을 보유" in err
    assert "잠시 후 다시 실행" in err
    assert str(external._round_ledger_path()) in err


def test_lock_failure_at_finish_keeps_the_verdict_exit_code(
    external, monkeypatch, tmp_path, capsys,
):
    """마감 구간 락 실패는 판정 rc 를 보존한다 — 끝난 전송의 부기가 판정을 뒤집지 않는다."""
    calls = _wire(external, monkeypatch, tmp_path)
    _flaky_round_lock(external, monkeypatch, fail_on=2)

    rc = external.main(["--gate", "T-0565", "--paths", "x.py"])

    err = capsys.readouterr().err
    assert rc == 0                               # 통과 판정 그대로 (락 사정으로 안 뒤집힘)
    assert calls["n"] == 1                       # 전송은 정상 수행
    assert "라운드 장부 마감 실패" in err
    assert "미완으로 남아" in err
    # 마감 못 한 레코드는 finished_at 없이 남아 다음 실행이 보수적으로(미완) 센다.
    record = _ledger(external, tmp_path)["T-0565"]["records"][0]
    assert "finished_at" not in record


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


# ══ 라운드별 산출 장부 + wave 예산 (T-0583) ═════════════════════════════════
# 라운드 count 만으로는 "그 라운드가 실결함을 냈는가"를 기계로 확인할 수 없어 비용 적정성 판단이
# PM 자기보고에 의존했다. 게이트 항목에 산출 이력(`rounds`)을 append 하고, 게이트별 상한과 별개인
# wave 단위 총 예산(`wave` 절)을 둔다. 기록은 무조건이고 hard 거부는 예산 축뿐이다.

# 리뷰어 응답을 실은 결과 템플릿 — 산출 파싱은 **회신 채널**(answer)만 본다.
_PASS_ANSWER = (
    "판정: 통과\n\n"
    "**must-fix** (반드시 수정):\n- 없음\n\n"
    "**suggestion** (권장):\n- 없음\n"
)
_REJECT_ANSWER = (
    "판정: 반려\n\n"
    "**must-fix** (반드시 수정):\n- 장부를 락 없이 쓴다\n- 종료 코드가 판정과 어긋난다\n\n"
    "**suggestion** (권장):\n- 주석 보강\n"
)
_PASS_WITH_ANSWER = {
    "reviewer": "x", "ok": True, "output": _PASS_ANSWER, "answer": _PASS_ANSWER,
    "verdict": {"has_must_fix": False, "has_pass": True}, "file": None,
    "failed": False, "started": True, "any_must_fix": False, "all_pass": True,
}
_REJECT_WITH_ANSWER = {
    "reviewer": "x", "ok": True, "output": _REJECT_ANSWER, "answer": _REJECT_ANSWER,
    "verdict": {"has_must_fix": True, "has_pass": False}, "file": None,
    "failed": False, "started": True, "any_must_fix": True, "all_pass": False,
}
# 오염 진단이 붙은 출력 — 판정 표면에서 무효화되므로 결함 수도 세지 않아야 한다.
_CONTAMINATED_WITH_ANSWER = {
    **_REJECT_WITH_ANSWER,
    "contamination": ("external_review_codex_20260807_1.txt",),
    "any_must_fix": False, "all_pass": False,
}


def _wave(external, tmp_path) -> dict:
    """저장된 장부의 wave 절 (절 부재 = 빈 dict)."""
    return _ledger(external, tmp_path).get(external.WAVE_SECTION_KEY, {})


def _wave_budget_state(external, tmp_path) -> dict:
    """wave 절에서 예산 좌표만 — 세대 `id` 는 매번 새로 발급되므로 값 비교에서 뺀다."""
    state = dict(_wave(external, tmp_path))
    state.pop("id", None)
    return state


# ── 순수 헬퍼: wave 예산 노브 (_wave_budget) ────────────────────────────────


def test_wave_budget_default(external):
    """미설정 → DEFAULT_WAVE_BUDGET(24 = 게이트 상한 4 × 동시 진행 6티켓 어림)."""
    assert external._wave_budget({}) == external.DEFAULT_WAVE_BUDGET == 24


def test_wave_budget_knob_override(external):
    """local.conf external_review_wave_budget 노브가 예산을 바꾼다 (채택자 조정)."""
    assert external._wave_budget({"external_review_wave_budget": "6"}) == 6
    assert external._wave_budget({"external_review_wave_budget": "0"}) == 0


def test_wave_budget_garbage_and_negative_fall_back(external):
    """비정수·음수는 기본값으로 fail-soft (라운드 상한 노브와 같은 규칙)."""
    assert external._wave_budget({"external_review_wave_budget": "x"}) == 24
    assert external._wave_budget({"external_review_wave_budget": "-3"}) == 24


# ── 순수 헬퍼: 구세대 장부 하위호환 (rounds/wave 절 없음) ────────────────────


def test_legacy_gate_entry_without_rounds_normalizes_to_empty_history(external):
    """`rounds` 없는 구세대 항목도 정상 로드된다 — count 는 그대로, 이력만 빈 배열로 시작."""
    ledger = {"T-0301": {"count": 3, "acked_through": 1}}
    entry = external._gate_entry(ledger, "T-0301")
    assert (entry["count"], entry["acked_through"]) == (3, 1)
    assert entry["rounds"] == []


def test_legacy_ledger_without_wave_section_starts_a_fresh_wave(external):
    """`wave` 절이 없거나 손상이면 새 wave(미시작·spent 0)로 정규화하고 장부에 심는다.

    세대 id 가 없던 구세대 절에는 여기서 하나 발급해 심는다 — 이후 예약/환불이 같은 좌표를 쓴다."""
    ledger = {"T-0301": {"count": 3}}
    state = external._wave_state(ledger)
    assert (state["started"], state["spent"]) == (None, 0)
    assert isinstance(state["id"], str) and state["id"]
    assert ledger[external.WAVE_SECTION_KEY] is state

    corrupt = external._wave_state({"wave": {"spent": "bad", "started": 7, "id": 9}})
    assert (corrupt["started"], corrupt["spent"]) == (None, 0)
    assert isinstance(corrupt["id"], str) and corrupt["id"]


def test_wave_state_keeps_an_existing_generation_id(external):
    """이미 세대 id 가 있으면 그대로 둔다 — 정규화가 세대를 갈아치우면 환불 판정이 무의미해진다."""
    ledger = {"wave": {"id": "gen-1", "started": "2026-08-07T00:00:00+00:00", "spent": 2}}
    assert external._wave_state(ledger) == {
        "id": "gen-1", "started": "2026-08-07T00:00:00+00:00", "spent": 2,
    }


def test_gate_names_skip_the_reserved_wave_key(external):
    """wave 는 예약 키라 게이트 집계 순회에서 빠진다 (예산 절이 게이트로 보이면 안 된다)."""
    ledger = {"T-0302": {"count": 1}, external.WAVE_SECTION_KEY: {"spent": 1}}
    assert external._gate_names(ledger) == ["T-0302"]


# ── 예약 키 충돌 차단 (게이트 이름 = wave) ──────────────────────────────────
# 두 축이 한 dict 를 공유하므로 같은 이름을 게이트로 쓰면 `_gate_entry` 와 `_wave_state` 가 한
# 항목을 서로 덮어써 게이트 count 는 저장되지 않고 wave 예산은 매 실행 되살아난다. 게이트 이름
# 형식은 강제하지 않는다(장부 실측: `wave4-b1` 류 자유 문자열 실사용) — 예약 키만 거부한다.


def test_reserved_gate_name_is_refused_before_any_effect(
        external, monkeypatch, tmp_path, capsys):
    """`--gate wave` 는 리뷰어 호출·장부 접근 전에 거부된다 (전송 0·장부 미생성)."""
    calls = _wire(external, monkeypatch, tmp_path)
    rc = external.main(["--gate", external.WAVE_SECTION_KEY, "--paths", "x.py"])
    err = capsys.readouterr().err
    assert rc == 1
    assert calls["n"] == 0
    assert _ledger(external, tmp_path) == {}
    assert "예약 키" in err and "--gate wave" in err
    assert "형식 제약은 없습니다" in err     # 하위호환: 자유 이름은 계속 허용


def test_reserved_gate_name_is_refused_on_the_report_surface_too(
        external, monkeypatch, tmp_path, capsys):
    """조회면도 같은 이름을 게이트로 부르지 않는다 (한 곳에서 거르는 단일 판정)."""
    _wire(external, monkeypatch, tmp_path)
    assert external.main(["--rounds-report", "--gate", external.WAVE_SECTION_KEY]) == 1
    assert "예약 키" in capsys.readouterr().err


def test_free_form_gate_names_still_work(external, monkeypatch, tmp_path):
    """T-NNNN 이 아닌 실사용 게이트 이름(wave4-b1)은 그대로 동작한다 (형식 강제 없음)."""
    _wire(external, monkeypatch, tmp_path)
    assert external.main(["--gate", "wave4-b1", "--paths", "x.py"]) == 0
    assert _ledger(external, tmp_path)["wave4-b1"]["count"] == 1


def test_gate_entry_refuses_the_reserved_key_loudly(external):
    """예약 키를 게이트로 정규화하려는 호출은 fail-loud — 두 축의 무언의 상호 덮어쓰기 차단."""
    ledger: dict = {}
    with pytest.raises(ValueError, match="예약 키"):
        external._gate_entry(ledger, external.WAVE_SECTION_KEY)
    assert ledger == {}


def test_wave_state_replaces_a_legacy_gate_entry_loudly(external, capsys):
    """예약 키 자리에 옛 게이트 항목이 있으면 경고 후 wave 절로 대체한다 (조용한 삭제 금지)."""
    ledger = {external.WAVE_SECTION_KEY: {"count": 4, "acked_through": 0, "records": []}}
    state = external._wave_state(ledger)
    assert (state["started"], state["spent"]) == (None, 0)
    err = capsys.readouterr().err
    assert "예약 키" in err and "재계산" in err


@pytest.mark.parametrize(
    "corrupt",
    ("wave", ["spent"], {"spent": "bad"}, {"spent": [1]}, {"spent": True}, {"spent": -1}),
)
def test_corrupt_wave_spend_is_recomputed_loudly(external, capsys, corrupt):
    """손상 wave 값은 라운드 이력으로 재계산되고 그 사실을 고지한다 (무통보 리셋 아님).

    wave 는 유일한 예산 hard-block 축이라, 조용히 0 으로 접히면 상한이 무통보로 열린다 — 특히
    음수는 예전 `max(0, …)` 보정에서 정상값처럼 통과해 `spent: -1` 한 줄 편집이 승인을 대신했다."""
    state = external._wave_state({external.WAVE_SECTION_KEY: corrupt})
    assert (state["started"], state["spent"]) == (None, 0)   # 이력이 없으면 재계산도 0
    err = capsys.readouterr().err
    assert "재계산" in err and "--ack-wave" in err


def test_negative_wave_spent_is_recomputed_from_round_history(external, capsys):
    """음수 spent 는 0 이 아니라 **wave 시작 이후 산출 수**로 되살린다 (좌표는 그대로)."""
    ledger = {
        "T-0335": {"count": 2, "rounds": [
            {"ts": "2026-08-07T01:00:00+00:00"}, {"ts": "2026-08-07T02:00:00+00:00"},
        ]},
        "T-0336": {"count": 1, "rounds": [{"ts": "2026-08-06T23:00:00+00:00"}]},  # wave 이전
        external.WAVE_SECTION_KEY: {
            "id": "gen-1", "started": "2026-08-07T00:00:00+00:00", "spent": -5,
        },
    }
    state = external._wave_state(ledger)
    assert state["spent"] == 2                               # 시작 이후 산출만
    assert state["id"] == "gen-1"                            # 신뢰 가능한 좌표는 보존
    assert state["started"] == "2026-08-07T00:00:00+00:00"
    assert "음수" in capsys.readouterr().err


def test_unusable_wave_frame_recomputes_from_all_rounds(external, capsys):
    """절 자체가 못 쓸 형상이면 범위를 좁힐 근거가 없어 **전체** 산출을 센다 (보수적)."""
    ledger = {
        "T-0335": {"count": 2, "rounds": [
            {"ts": "2026-08-07T01:00:00+00:00"}, {"ts": "2026-08-06T23:00:00+00:00"},
        ]},
        external.WAVE_SECTION_KEY: "손상",
    }
    assert external._wave_state(ledger)["spent"] == 2
    assert "재계산" in capsys.readouterr().err


def test_missing_wave_section_stays_quiet(external, capsys):
    """절 부재(구세대 장부·첫 실행)는 정상이라 조용하다 — 고지는 손상에만."""
    external._wave_state({"T-0333": {"count": 1}})
    assert capsys.readouterr().err == ""


# ── 순수 헬퍼: 산출 파싱 (_must_fix_count) ──────────────────────────────────


def test_must_fix_count_reads_only_the_answer_channel(external):
    """결함 수는 회신 채널만 센다 — 진행 로그엔 diff 원문이 실려 리뷰 대상 문구가 결함이 된다."""
    result = {
        "ok": True, "verdict": {"has_pass": True, "has_must_fix": False},
        "answer": _PASS_ANSWER,
        "output": "…진행 로그…\n**must-fix**:\n- diff 안에 있던 문구\n",
    }
    assert external._must_fix_count(result) == 0


def test_must_fix_count_is_null_when_the_section_is_absent(external):
    """must-fix 섹션이 아예 없는 응답은 0 이 아니라 None — "없음" 표기와 부재는 다르다.

    항목 근거 없이 반려만 있는 응답을 '결함 0건 반려'로 박제하면 비용 판단이 거짓이 된다."""
    result = {
        "ok": True, "verdict": {"has_pass": False, "has_must_fix": True},
        "answer": "판정: 반려\n\n리뷰 본문: 락 순서가 위험합니다.\n",
    }
    assert external._must_fix_count(result) is None
    # 형식을 지켜 "없음"이라고 *말한* 응답은 0 (리뷰어의 명시 선언).
    assert external._must_fix_count(
        {"ok": True, "verdict": {"has_pass": True, "has_must_fix": False},
         "answer": _PASS_ANSWER}
    ) == 0


def test_must_fix_count_is_null_without_a_valid_verdict(external):
    """판정이 무효한 라운드(실패·오염)는 셀 근거가 없어 None — 판정 표면과 같은 규칙."""
    assert external._must_fix_count(
        {"ok": False, "verdict": {"has_pass": False, "has_must_fix": False},
         "answer": _REJECT_ANSWER}
    ) is None
    assert external._must_fix_count(
        {"ok": True, "verdict": {"has_pass": False, "has_must_fix": True},
         "contamination": ("raw.txt",), "answer": _REJECT_ANSWER}
    ) is None
    # 회신 채널이 없으면(실패 결과 dict) 파싱 불가 → None (fail-soft·기록 자체는 남는다).
    assert external._must_fix_count(_FAIL_STARTED) is None


# ── 라운드 산출 기록 (main 흐름·DoD) ────────────────────────────────────────


def test_pass_round_appends_outcome_with_verdict(external, monkeypatch, tmp_path):
    """전송된 라운드는 산출(`ts`·판정 rc·결함 수)이 장부에 append 된다 (DoD)."""
    _wire(external, monkeypatch, tmp_path, result=_PASS_WITH_ANSWER)
    assert external.main(["--gate", "T-0303", "--paths", "x.py"]) == 0
    rounds = _ledger(external, tmp_path)["T-0303"]["rounds"]
    assert len(rounds) == 1
    outcome = rounds[0]
    assert outcome["verdict"] == 0                 # 기존 rc 판정 (0=통과)
    assert outcome["must_fix"] == 0                # must-fix 섹션이 "없음"
    assert outcome["suggestions"] is None          # suggestion 판별기는 후속
    assert outcome["ts"].endswith("+00:00")        # 예약/마감과 같은 UTC ISO 표기


def test_reject_round_records_the_must_fix_count(external, monkeypatch, tmp_path):
    """반려 라운드는 rc 1 과 함께 실결함 수를 남긴다 — "그 라운드가 결함을 냈나"의 근거."""
    _wire(external, monkeypatch, tmp_path, result=_REJECT_WITH_ANSWER)
    assert external.main(["--gate", "T-0304", "--paths", "x.py"]) == 1
    outcome = _ledger(external, tmp_path)["T-0304"]["rounds"][0]
    assert (outcome["verdict"], outcome["must_fix"]) == (1, 2)


def test_reject_without_a_must_fix_section_records_null_count(
        external, monkeypatch, tmp_path):
    """섹션 없는 반려는 결함 수를 만들어내지 않는다 — 판정만 1, 수는 '미상'(None)."""
    answer = "판정: 반려\n\n락 순서가 위험합니다.\n"
    result = {**_REJECT_WITH_ANSWER, "answer": answer, "output": answer}
    _wire(external, monkeypatch, tmp_path, result=result)
    assert external.main(["--gate", "T-0327", "--paths", "x.py"]) == 1
    outcome = _ledger(external, tmp_path)["T-0327"]["rounds"][0]
    assert (outcome["verdict"], outcome["must_fix"]) == (1, None)


def test_round_outcome_carries_the_reservation_identity(external, monkeypatch, tmp_path):
    """산출 레코드가 예약 identity(id·sequence)를 실어 라운드↔결과 연결이 확정된다."""
    _wire(external, monkeypatch, tmp_path, result=_PASS_WITH_ANSWER)
    assert external.main(["--gate", "T-0328", "--paths", "x.py"]) == 0
    entry = _ledger(external, tmp_path)["T-0328"]
    outcome, record = entry["rounds"][0], entry["records"][0]
    assert outcome["id"] == record["id"]
    assert outcome["sequence"] == record["sequence"] == 1


def test_unparsable_outcome_is_null_and_does_not_block_the_review(
        external, monkeypatch, tmp_path):
    """산출 파싱 불가(판정 없는 전송)는 null 기록 — 리뷰 판정/rc 는 그대로다 (DoD·fail-soft)."""
    calls = _wire(external, monkeypatch, tmp_path, result=_FAIL_STARTED)
    assert external.main(["--gate", "T-0305", "--paths", "x.py"]) == 1  # 판정 rc 보존
    assert calls["n"] == 1
    outcome = _ledger(external, tmp_path)["T-0305"]["rounds"][0]
    assert outcome["verdict"] == 1
    assert outcome["must_fix"] is None and outcome["suggestions"] is None


def test_contaminated_round_records_no_defect_count(external, monkeypatch, tmp_path):
    """오염으로 무효화한 판정은 결함 수도 세지 않는다 (판정 표면과 장부가 갈리지 않게)."""
    _wire(external, monkeypatch, tmp_path, result=_CONTAMINATED_WITH_ANSWER)
    assert external.main(["--gate", "T-0306", "--paths", "x.py"]) == 1
    outcome = _ledger(external, tmp_path)["T-0306"]["rounds"][0]
    assert outcome["verdict"] == 1 and outcome["must_fix"] is None


def test_spawn_failure_leaves_no_outcome(external, monkeypatch, tmp_path):
    """전송이 확실히 없던 라운드는 산출도 남기지 않는다 (리뷰어가 아무 말도 하지 않았다)."""
    _wire(external, monkeypatch, tmp_path, result=_FAIL_UNSTARTED)
    assert external.main(["--gate", "T-0307", "--paths", "x.py"]) == 1
    entry = _ledger(external, tmp_path)["T-0307"]
    assert entry["count"] == 0 and entry["rounds"] == []


def test_ack_rounds_keeps_the_outcome_history(external, monkeypatch, tmp_path, capsys):
    """승인은 집계 창(records)만 비운다 — 산출 이력은 비용 판단 근거라 남는다."""
    _wire(external, monkeypatch, tmp_path, result=_PASS_WITH_ANSWER)
    argv = ["--gate", "T-0308", "--paths", "x.py"]
    for _ in range(4):
        assert external.main(argv) == 0
    assert external.main(argv) == external.EXIT_ROUND_LIMIT_EXCEEDED
    capsys.readouterr()

    assert external.main(argv + ["--ack-rounds"]) == 0
    entry = _ledger(external, tmp_path)["T-0308"]
    assert len(entry["rounds"]) == 5          # 승인 전 4 + 승인 재개 1 (이력은 append-only)
    assert len(entry["records"]) == 1         # 집계 창은 비워지고 이번 라운드만 남는다


def test_old_schema_ledger_keeps_counting_and_gains_outcomes(
        external, monkeypatch, tmp_path, capsys):
    """구세대 장부(rounds/wave 절 없음)로도 상한 판정이 이어지고 새 산출만 뒤에 쌓인다 (DoD)."""
    calls = _wire(external, monkeypatch, tmp_path, result=_PASS_WITH_ANSWER)
    ledger_path = tmp_path / ".project_manager" / ".local" / "review_rounds.json"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(
        json.dumps({"T-0309": {"count": 3, "acked_through": 0}}), encoding="utf-8",
    )

    assert external.main(["--gate", "T-0309", "--paths", "x.py"]) == 0
    entry = _ledger(external, tmp_path)["T-0309"]
    assert entry["count"] == 4                # 옛 count 를 이어 센다 (리셋 없음)
    assert len(entry["rounds"]) == 1          # 산출은 이번 라운드부터
    assert _wave(external, tmp_path)["spent"] == 1

    capsys.readouterr()
    assert external.main(
        ["--gate", "T-0309", "--paths", "x.py"]
    ) == external.EXIT_ROUND_LIMIT_EXCEEDED   # 승계된 count 로 상한이 계속 산다
    assert calls["n"] == 1


# ── wave 예산 게이트 (DoD) ──────────────────────────────────────────────────


def test_wave_budget_blocks_across_gates_then_ack_wave_resumes(
        external, monkeypatch, tmp_path, capsys):
    """wave 예산은 게이트를 가로질러 합계로 센다 — 소진 시 rc 4·`--ack-wave` 로만 재개 (DoD)."""
    conf = {"external_review_enabled": "true", "external_review_wave_budget": "2"}
    calls = _wire(external, monkeypatch, tmp_path, conf=conf)
    for gate in ("T-0310", "T-0311"):         # 서로 다른 게이트 = 각자 라운드 상한은 여유
        assert external.main(["--gate", gate, "--paths", "x.py"]) == 0
    assert _wave(external, tmp_path)["spent"] == 2
    capsys.readouterr()

    rc = external.main(["--gate", "T-0312", "--paths", "x.py"])
    assert rc == external.EXIT_ROUND_LIMIT_EXCEEDED   # 라운드 상한과 같은 rc
    assert calls["n"] == 2                            # 리뷰어 미호출 (전송 전 거부)
    err = capsys.readouterr().err
    assert "wave 예산 소진" in err
    assert "--ack-wave" in err and "예산 리셋" in err
    assert "--rounds-report" in err                   # 보고 근거 조회면 안내
    assert "T-0312" not in _ledger(external, tmp_path)  # 거부는 아무것도 커밋하지 않는다

    rc = external.main(["--gate", "T-0312", "--paths", "x.py", "--ack-wave"])
    assert rc == 0 and calls["n"] == 3
    assert "wave 예산 승인 재개" in capsys.readouterr().err
    assert _wave(external, tmp_path)["spent"] == 1     # 리셋 후 이번 전송 1


def test_wave_started_marks_the_first_send_and_accumulates(external, monkeypatch, tmp_path):
    """wave.started 는 첫 전송 시각이고 명시 리셋 전까지 누적된다 (세션 자동 감지 없음)."""
    _wire(external, monkeypatch, tmp_path)
    argv = ["--gate", "T-0313", "--paths", "x.py"]
    assert external.main(argv) == 0
    started = _wave(external, tmp_path)["started"]
    assert started and started.endswith("+00:00")

    assert external.main(argv) == 0
    assert _wave_budget_state(external, tmp_path) == {"started": started, "spent": 2}


def test_spawn_failure_refunds_the_wave_budget(external, monkeypatch, tmp_path):
    """전송 0(스폰 전 실패)은 라운드 count 와 wave spent 를 같은 조건으로 되돌린다.

    첫 전송이 아예 없었으므로 시작 시각도 남지 않는다 — 그러지 않으면 조회 표가 있지도 않은
    wave 를 진행 중으로 보여주고, 다음 첫 전송이 자기 시각을 못 찍는다."""
    calls = _wire(external, monkeypatch, tmp_path, result=_FAIL_UNSTARTED)
    for _ in range(6):
        assert external.main(["--gate", "T-0314", "--paths", "x.py"]) == 1
    assert calls["n"] == 6
    assert _ledger(external, tmp_path)["T-0314"]["count"] == 0
    # 설치/PATH 문제로 예산이 새지 않는다 + 시작 시각도 복원된다.
    assert _wave_budget_state(external, tmp_path) == {"started": None, "spent": 0}


def test_refund_keeps_the_wave_start_when_earlier_sends_exist(
        external, monkeypatch, tmp_path):
    """실 전송이 있던 wave 는 환불 후에도 첫 전송 시각을 유지한다 (0 으로 돌아갈 때만 지운다)."""
    _wire(external, monkeypatch, tmp_path)
    assert external.main(["--gate", "T-0314", "--paths", "x.py"]) == 0
    started = _wave(external, tmp_path)["started"]

    _wire(external, monkeypatch, tmp_path, result=_FAIL_UNSTARTED)
    assert external.main(["--gate", "T-0314", "--paths", "x.py"]) == 1
    assert _wave_budget_state(external, tmp_path) == {"started": started, "spent": 1}


def test_refund_wave_round_clears_started_only_when_the_wave_empties(external):
    """순수 헬퍼 — 같은 세대일 때만, spent 가 0 이 되는 순간에만 started 를 지운다."""
    state = {"id": "gen-1", "started": "2026-08-07T00:00:00+00:00", "spent": 2}
    assert external._refund_wave_round(state, "gen-1") is True
    assert state == {"id": "gen-1", "started": "2026-08-07T00:00:00+00:00", "spent": 1}
    assert external._refund_wave_round(state, "gen-1") is True
    assert state == {"id": "gen-1", "started": None, "spent": 0}
    assert external._refund_wave_round(state, "gen-1") is True   # 이미 0 이면 그대로(음수 없음)
    assert state == {"id": "gen-1", "started": None, "spent": 0}


def test_refund_wave_round_ignores_another_generation(external):
    """세대가 다르면 아무것도 하지 않는다 — 리셋된 새 wave 의 예산을 옛 실패가 깎으면 우회다."""
    state = {"id": "gen-2", "started": "2026-08-07T01:00:00+00:00", "spent": 3}
    assert external._refund_wave_round(state, "gen-1") is False
    assert external._refund_wave_round(state, None) is False
    assert state == {"id": "gen-2", "started": "2026-08-07T01:00:00+00:00", "spent": 3}


def test_refund_skips_a_wave_that_was_reset_mid_flight(
        external, monkeypatch, tmp_path, capsys):
    """전송 중 `--ack-wave` 로 새 wave 가 열리면 이 실패는 그 예산을 깎지 않는다 (세대 확인).

    다른 실행이 리뷰 도중 승인·리셋한 형상을 리뷰어 스텁 안에서 재현한다 — 예약 구간 락은 이미
    풀린 뒤라 실제 동시 실행과 같은 순서다."""
    _wire(external, monkeypatch, tmp_path, result=_FAIL_UNSTARTED)
    stub_run_review = external.run_review

    def _reset_wave_then_fail(*args, **kwargs):
        with external._round_ledger_lock():             # 다른 실행의 `--ack-wave` 대역
            ledger = external._load_round_ledger()
            external._spend_wave_round(external._reset_wave(ledger))
            external._save_round_ledger(ledger)
        return stub_run_review(*args, **kwargs)

    monkeypatch.setattr(external, "run_review", _reset_wave_then_fail)
    assert external.main(["--gate", "T-0326", "--paths", "x.py"]) == 1

    assert _wave(external, tmp_path)["spent"] == 1       # 새 wave 의 소비는 그대로
    assert _ledger(external, tmp_path)["T-0326"]["count"] == 0   # 라운드 count 는 환불됨
    assert "wave 가 이미 리셋" in capsys.readouterr().err


def test_refund_is_skipped_when_the_reservation_record_vanishes(
        external, monkeypatch, tmp_path, capsys):
    """마감 직전 예약 레코드가 사라지면(동시 `--ack-rounds`) 어느 축도 환불하지 않는다.

    승인이 집계 창을 비우면 그 라운드는 이미 acked_through 로 접힌 것이라, count 를 되돌리면
    이중 환불이고 wave 만 깎으면 두 축의 소비가 갈린다."""
    _wire(external, monkeypatch, tmp_path, result=_FAIL_UNSTARTED)
    stub_run_review = external.run_review

    def _ack_rounds_then_fail(*args, **kwargs):
        with external._round_ledger_lock():             # 다른 실행의 `--ack-rounds` 대역
            ledger = external._load_round_ledger()
            entry = external._gate_entry(ledger, "T-0332")
            entry["acked_through"] = entry["count"]
            entry["records"] = []
            external._save_round_ledger(ledger)
        return stub_run_review(*args, **kwargs)

    monkeypatch.setattr(external, "run_review", _ack_rounds_then_fail)
    assert external.main(["--gate", "T-0332", "--paths", "x.py"]) == 1

    entry = _ledger(external, tmp_path)["T-0332"]
    assert (entry["count"], entry["records"]) == (1, [])   # 환불 대상 없음 → 이중 환불 없음
    assert _wave(external, tmp_path)["spent"] == 1         # wave 도 갈리지 않는다
    assert "wave 가 이미 리셋" not in capsys.readouterr().err   # 세대 오보 경고도 없다


def test_outcome_keeps_its_sequence_when_records_are_acked_away(
        external, monkeypatch, tmp_path):
    """동시 `--ack-rounds` 로 예약 레코드가 사라져도 산출은 예약 순번을 잃지 않는다.

    순번을 마감 시점 레코드에서 되찾아 읽으면 이 형상에서 None 이 되고, 조회 표의 라운드 번호가
    사라진다 — 예약 시점 값을 그대로 실어 둬야 한다."""
    _wire(external, monkeypatch, tmp_path, result=_PASS_WITH_ANSWER)
    stub_run_review = external.run_review

    def _ack_rounds_then_pass(*args, **kwargs):
        with external._round_ledger_lock():             # 다른 실행의 `--ack-rounds` 대역
            ledger = external._load_round_ledger()
            entry = external._gate_entry(ledger, "T-0341")
            entry["acked_through"] = entry["count"]
            entry["records"] = []
            external._save_round_ledger(ledger)
        return stub_run_review(*args, **kwargs)

    monkeypatch.setattr(external, "run_review", _ack_rounds_then_pass)
    assert external.main(["--gate", "T-0341", "--paths", "x.py"]) == 0

    entry = _ledger(external, tmp_path)["T-0341"]
    assert entry["records"] == []                       # 승인이 집계 창을 비운 형상
    outcome = entry["rounds"][0]
    assert outcome["sequence"] == 1                     # 예약 순번은 산출에 남는다
    assert isinstance(outcome["id"], str) and outcome["id"]


def test_negative_wave_spent_does_not_reopen_the_budget(
        external, monkeypatch, tmp_path, capsys):
    """장부를 `spent: -1` 로 고쳐도 예산은 열리지 않는다 — 손상은 승인을 대신하지 않는다."""
    conf = {"external_review_enabled": "true", "external_review_wave_budget": "2"}
    calls = _wire(external, monkeypatch, tmp_path, conf=conf, result=_PASS_WITH_ANSWER)
    for gate in ("T-0337", "T-0338"):
        assert external.main(["--gate", gate, "--paths", "x.py"]) == 0
    path = tmp_path / ".project_manager" / ".local" / "review_rounds.json"
    ledger = json.loads(path.read_text(encoding="utf-8"))
    ledger[external.WAVE_SECTION_KEY]["spent"] = -1          # 예산 되돌리기 시도
    path.write_text(json.dumps(ledger), encoding="utf-8")
    capsys.readouterr()

    rc = external.main(["--gate", "T-0339", "--paths", "x.py"])

    err = capsys.readouterr().err
    assert rc == external.EXIT_ROUND_LIMIT_EXCEEDED          # 여전히 막힌다
    assert calls["n"] == 2                                   # 전송 없음
    assert "음수" in err and "재계산" in err
    assert "wave 예산 소진" in err
    assert _wave(external, tmp_path)["spent"] == 2           # 이력 기반 복원값이 저장된다


def test_ack_wave_does_not_open_the_gate_round_limit(
        external, monkeypatch, tmp_path, capsys):
    """두 예산은 독립 축 — wave 승인이 게이트 라운드 상한을 열지 않는다."""
    calls = _wire(external, monkeypatch, tmp_path)
    argv = ["--gate", "T-0315", "--paths", "x.py"]
    for _ in range(4):
        assert external.main(argv) == 0
    capsys.readouterr()

    assert external.main(argv + ["--ack-wave"]) == external.EXIT_ROUND_LIMIT_EXCEEDED
    assert "라운드 상한 도달" in capsys.readouterr().err
    assert calls["n"] == 4


def test_ack_rounds_does_not_reset_the_wave_budget(
        external, monkeypatch, tmp_path, capsys):
    """반대 방향도 같다 — 게이트 승인이 wave 예산을 열지 않는다 (재개는 각 축의 승인으로)."""
    conf = {"external_review_enabled": "true", "external_review_wave_budget": "2"}
    calls = _wire(external, monkeypatch, tmp_path, conf=conf)
    for gate in ("T-0316", "T-0317"):
        assert external.main(["--gate", gate, "--paths", "x.py"]) == 0
    capsys.readouterr()

    rc = external.main(["--gate", "T-0318", "--paths", "x.py", "--ack-rounds"])
    assert rc == external.EXIT_ROUND_LIMIT_EXCEEDED
    assert "wave 예산 소진" in capsys.readouterr().err
    assert calls["n"] == 2


# ── 두 상한 동시 소진: 승인 유실 금지 ───────────────────────────────────────
# 한 축의 승인을 적용해 놓고 다른 축에서 저장 없이 되돌아가면 사용자가 준 승인이 조용히 사라진다
# (같은 승인을 다시 받아야 한다). 승인은 거부되는 실행에서도 저장하고, 두 플래그 동시 지정은
# 둘 다 적용해 그대로 재개한다.


def _exhaust_both_budgets(external, monkeypatch, tmp_path):
    """게이트 라운드 상한(1)과 wave 예산(2)을 동시에 소진한 장부를 만든다."""
    conf = {
        "external_review_enabled": "true",
        "external_review_round_limit": "1",
        "external_review_wave_budget": "2",
    }
    calls = _wire(external, monkeypatch, tmp_path, conf=conf)
    assert external.main(["--gate", "T-0330", "--paths", "x.py"]) == 0   # 게이트 상한 소진
    assert external.main(["--gate", "T-0331", "--paths", "x.py"]) == 0   # wave 예산 소진
    assert calls["n"] == 2
    return calls


def test_round_approval_survives_a_wave_refusal(
        external, monkeypatch, tmp_path, capsys):
    """wave 가 막아도 `--ack-rounds` 승인은 저장된다 — 다음 실행이 그 승인을 이어받는다."""
    calls = _exhaust_both_budgets(external, monkeypatch, tmp_path)
    capsys.readouterr()

    argv = ["--gate", "T-0330", "--paths", "x.py"]
    assert external.main(argv + ["--ack-rounds"]) == external.EXIT_ROUND_LIMIT_EXCEEDED
    err = capsys.readouterr().err
    assert "wave 예산 소진" in err                       # 남은 축이 막는다
    assert calls["n"] == 2
    assert _ledger(external, tmp_path)["T-0330"]["acked_through"] == 1   # 승인은 남았다
    # rc 4 인 실행이 "재개"를 말하면 종료 코드와 어긋난 loud 오보다 — 기록됐다고만 말한다.
    assert "라운드 상한 승인 재개" not in err
    assert "라운드 상한 승인 기록" in err and "거부" in err

    # wave 만 승인하면 재개된다 — 라운드 승인을 다시 받을 필요가 없다.
    assert external.main(argv + ["--ack-wave"]) == 0
    assert calls["n"] == 3


def test_wave_approval_survives_a_round_limit_refusal(
        external, monkeypatch, tmp_path, capsys):
    """반대 방향도 동형 — 게이트가 막아도 `--ack-wave` 리셋은 저장된다."""
    calls = _exhaust_both_budgets(external, monkeypatch, tmp_path)
    capsys.readouterr()

    argv = ["--gate", "T-0330", "--paths", "x.py"]
    assert external.main(argv + ["--ack-wave"]) == external.EXIT_ROUND_LIMIT_EXCEEDED
    err = capsys.readouterr().err
    assert "라운드 상한 도달" in err
    assert calls["n"] == 2
    assert _wave(external, tmp_path)["spent"] == 0        # 리셋은 남았다
    assert "wave 예산 승인 재개" not in err                # 거부된 실행은 '재개'라 말하지 않는다
    assert "wave 예산 승인 기록" in err and "거부" in err

    assert external.main(argv + ["--ack-rounds"]) == 0
    assert calls["n"] == 3


def test_both_ack_flags_together_resume_in_one_run(
        external, monkeypatch, tmp_path, capsys):
    """두 플래그 동시 지정은 둘 다 적용돼 그 호출이 바로 재개된다 (승인 왕복 불필요)."""
    calls = _exhaust_both_budgets(external, monkeypatch, tmp_path)
    capsys.readouterr()

    rc = external.main(
        ["--gate", "T-0330", "--paths", "x.py", "--ack-rounds", "--ack-wave"]
    )
    err = capsys.readouterr().err
    assert rc == 0 and calls["n"] == 3
    assert "라운드 상한 승인 재개" in err and "wave 예산 승인 재개" in err
    assert _ledger(external, tmp_path)["T-0330"]["acked_through"] == 1
    assert _wave(external, tmp_path)["spent"] == 1        # 리셋 후 이번 전송 1


def test_dry_run_and_empty_diff_do_not_spend_the_wave_budget(
        external, monkeypatch, tmp_path):
    """전송 없는 실행(dry-run·빈 diff)은 wave 예산도 쓰지 않는다 (라운드 count 규칙과 동형)."""
    calls = _wire(external, monkeypatch, tmp_path)
    for _ in range(3):
        assert external.main(["--gate", "T-0319", "--paths", "x.py", "--dry-run"]) == 0
    assert calls["n"] == 0
    assert _ledger(external, tmp_path) == {}

    _wire(external, monkeypatch, tmp_path, diff="")
    assert external.main(["--gate", "T-0319", "--paths", "x.py", "--force"]) == 1
    assert _ledger(external, tmp_path) == {}


def test_ack_wave_without_gate_warns_and_proceeds(external, monkeypatch, tmp_path, capsys):
    """`--ack-wave` 를 --gate 없이 쓰면 경고 후 정상 진행 (장부 대상 아님·--ack-rounds 동형)."""
    _wire(external, monkeypatch, tmp_path)
    assert external.main(["--ack-wave", "--paths", "x.py"]) == 0
    err = capsys.readouterr().err
    assert "--ack-wave" in err and "--gate 와 함께" in err
    assert _ledger(external, tmp_path) == {}


# ── 조회면 (--rounds-report) ────────────────────────────────────────────────


def test_rounds_report_dumps_gate_rounds_and_wave(external, monkeypatch, tmp_path, capsys):
    """조회면이 게이트별 라운드 수·라운드별 판정/결함 수·wave spent 를 표로 낸다 (외부 전송 0)."""
    calls = _wire(external, monkeypatch, tmp_path, result=_REJECT_WITH_ANSWER)
    assert external.main(["--gate", "T-0320", "--paths", "x.py"]) == 1
    capsys.readouterr()

    assert external.main(["--rounds-report"]) == 0
    out = capsys.readouterr().out
    assert calls["n"] == 1                                  # 조회는 리뷰어를 부르지 않는다
    assert str(external._round_ledger_path()) in out        # 어느 장부를 읽었는지
    assert "wave: spent=1 / 예산 24" in out
    assert "게이트 T-0320: count=1" in out
    assert "판정=1(비통과) must_fix=2 suggestions=미상" in out


def test_rounds_report_filters_by_gate(external, monkeypatch, tmp_path, capsys):
    """`--gate` 를 주면 그 게이트만 본다 (다른 게이트는 표에 없다)."""
    _wire(external, monkeypatch, tmp_path, result=_PASS_WITH_ANSWER)
    for gate in ("T-0321", "T-0322"):
        assert external.main(["--gate", gate, "--paths", "x.py"]) == 0
    capsys.readouterr()

    assert external.main(["--rounds-report", "--gate", "T-0321"]) == 0
    out = capsys.readouterr().out
    assert "게이트 T-0321" in out and "T-0322" not in out


def test_rounds_report_on_empty_ledger_is_rc_zero(external, monkeypatch, tmp_path, capsys):
    """장부가 없어도 조회는 답을 낸다 — diff·검토 경로 없이 rc 0 (전송 게이트 뒤가 아니다)."""
    calls = _wire(external, monkeypatch, tmp_path)
    assert external.main(["--rounds-report"]) == 0
    out = capsys.readouterr().out
    assert calls["n"] == 0
    assert "wave: spent=0 / 예산 24 · 시작 미시작" in out
    assert "기록된 게이트 없음" in out

    assert external.main(["--rounds-report", "--gate", "T-0323"]) == 0
    assert "게이트 T-0323: 장부에 기록 없음" in capsys.readouterr().out


def test_rounds_report_with_a_selector_reads_the_recording_anchor(
        external, monkeypatch, tmp_path, capsys):
    """`--paths` 를 준 조회는 기록면과 **같은 해소**를 타 같은 장부를 읽는다 (앵커 갈림 방지)."""
    calls = _wire(external, monkeypatch, tmp_path, result=_PASS_WITH_ANSWER)
    assert external.main(["--gate", "T-0334", "--paths", "x.py"]) == 0
    capsys.readouterr()

    assert external.main(["--rounds-report", "--paths", "x.py"]) == 0
    out = capsys.readouterr().out
    assert calls["n"] == 1                                  # selector 조회도 전송 없음
    assert str(external._round_ledger_path()) in out
    assert "게이트 T-0334: count=1" in out


def test_rounds_report_warns_about_ignored_action_flags(
        external, monkeypatch, tmp_path, capsys):
    """조회 전용면이 행동 플래그를 조용히 무시하지 않는다 (승인이 삼켜졌다고 오인 금지)."""
    calls = _wire(external, monkeypatch, tmp_path)
    assert external.main(["--rounds-report", "--ack-wave", "--force"]) == 0
    err = capsys.readouterr().err
    assert "조회 전용" in err and "--ack-wave" in err and "--force" in err
    assert calls["n"] == 0
    assert _ledger(external, tmp_path) == {}                # 조회는 장부를 고치지 않는다


def test_rounds_report_reflects_the_wave_budget_knob(external, monkeypatch, tmp_path, capsys):
    """예산 표기는 해소된 conf 노브를 따른다 (조회와 게이트가 같은 값을 본다)."""
    conf = {"external_review_enabled": "true", "external_review_wave_budget": "6"}
    _wire(external, monkeypatch, tmp_path, conf=conf)
    assert external.main(["--rounds-report"]) == 0
    assert "예산 6" in capsys.readouterr().out


def test_render_rounds_report_marks_unknown_fields_and_is_read_only(external):
    """산출이 없거나 손상인 필드는 '미상'으로 구분되고, 렌더는 장부를 고치지 않는다."""
    ledger = {
        "T-0324": {
            "count": 2, "acked_through": 0,
            "rounds": [
                {"ts": "2026-08-07T00:00:00+00:00", "sequence": 1, "verdict": 0,
                 "must_fix": 0, "suggestions": None},
                {"verdict": None, "must_fix": None, "suggestions": None},
            ],
        },
        "T-0325": {"count": 1, "acked_through": 0},
        external.WAVE_SECTION_KEY: {"started": "2026-08-07T00:00:00+00:00", "spent": 3},
    }
    snapshot = json.loads(json.dumps(ledger))

    report = external.render_rounds_report(
        ledger, ledger_path="/tmp/review_rounds.json", wave_budget=24,
    )

    assert "wave: spent=3 / 예산 24 · 시작 2026-08-07T00:00:00+00:00" in report
    assert "#1 2026-08-07T00:00:00+00:00 판정=0(통과) must_fix=0 suggestions=미상" in report
    assert "#미상 시각 미상 판정=미상 must_fix=미상 suggestions=미상" in report
    assert "(라운드 산출 기록 없음" in report            # 구세대 게이트 표기
    assert ledger == snapshot                            # 조회가 장부를 고치지 않는다


def test_rounds_report_numbers_rounds_by_reservation_sequence(external):
    """표시 번호·나열 순서는 **예약 순번**이다 — append(=완료) 순서로 읽으면 라운드가 뒤바뀐다."""
    ledger = {
        "T-0340": {"count": 2, "acked_through": 0, "rounds": [
            {"ts": "2026-08-07T02:00:00+00:00", "sequence": 2, "verdict": 0},
            {"ts": "2026-08-07T03:00:00+00:00", "sequence": 1, "verdict": 1},  # 늦게 끝난 1라운드
        ]},
    }
    body = [line for line in external.render_rounds_report(ledger).splitlines()
            if line.startswith("  #")]
    assert body[0].startswith("  #1 2026-08-07T03:00:00+00:00 판정=1")
    assert body[1].startswith("  #2 2026-08-07T02:00:00+00:00 판정=0")
