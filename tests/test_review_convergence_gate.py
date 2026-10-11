"""리뷰 수렴 게이트 · 폐지 플래그 차단 · 변경량 상한 없음 (T-0593 · T-0950).

라운드 상한(T-0457)과 wave 예산(T-0583)은 "몇 번 호출했나"만 셌고 "닫히고 있나"는 묻지 않았다.
실측 두 형상이 그 공백이다 — 리뷰 12라운드(연장 승인 반복)와 must_fix 3→2→2 평탄. 이 파일은 세
축을 단언한다:

  ① 수렴-형상 게이트 — 라운드 장부의 `rounds[].must_fix` 추이로 판정한다(LLM 판단 0).
     상한 도달(local.conf `additional_reviewer.rounds_max`·기본 2)·직전 대비 증가(조기 차단)면 rc 4 로 막고,
     감소 수렴은 그대로 통과시킨다. 상한에서는 현재 티켓을 정지해 사용자에게 보고한다.
  ② 폐지 플래그 — fix 후 재리뷰 예약 인자는 장부 상태와 무관하게 파서에서 거부한다.
  ③ 변경량 상한 없음 — 추가 리뷰어 호출은 변경 줄 수로 거부되지 않는다(사용자 결정 2026-10-10).
     남는 공유 측정(기계 mirror 제외 · 측정 폭의 단계 표 · untracked 포함)은 그 소비자
     `measured_diff_text` 로 단언한다.

hermetic: REPO 를 tmp 로 monkeypatch 해 장부를 격리하고(`test_additional_reviewer.py` 동형),
extract_diff·run_review·local_config 를 주입해 실제 git/추가 리뷰어 없이(호출 0) 분기를
단언한다. diff 측정만 실 git 을 쓰는 테스트는 tmp 저장소를 직접 만든다.
"""
from __future__ import annotations

import datetime
import importlib.util
import json
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"


# 해소 가능한 추가 리뷰어 대상 — 대상은 `harness`+`model` 구조화 키로만 서므로(엔진 기본 커맨드
# 없음) 이 파일의 모든 형상이 그 세트를 깔고 시작한다.
_REVIEWER_TARGET = {
    "additional_reviewer.harness": "codex",
    "additional_reviewer.model": "gpt-5.6-sol",
}


def _load(name: str = "additional_reviewer"):
    spec = importlib.util.spec_from_file_location(f"convergence_{name}", TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def external():
    return _load("additional_reviewer")


# ── 응답 대역 — 산출 파싱은 **회신 채널**(answer)만 본다 ─────────────────────


def _answer(must_fix: int) -> str:
    """must-fix N 건을 선언한 리뷰어 응답 (0 이면 '없음' 표기)."""
    items = "- 없음" if must_fix == 0 else "\n".join(
        f"- 결함 {index}" for index in range(1, must_fix + 1)
    )
    verdict = "통과" if must_fix == 0 else "반려"
    return (
        f"판정: {verdict}\n\n"
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


def _wire(external, monkeypatch, tmp_path, *, series: list[int], conf=None):
    """main() 을 tmp REPO 로 격리 배선하고 라운드별 must_fix 를 주입한다."""
    monkeypatch.setattr(external, "REPO", tmp_path)
    monkeypatch.setattr(
        external, "local_config",
        lambda repo=None: dict(conf) if conf is not None
        else dict(_REVIEWER_TARGET))
    monkeypatch.setattr(
        external, "extract_diff",
        lambda *a, **k: "diff --git a/x b/x\n@@ -1 +1 @@\n-o\n+n\n")
    reviewer = _Reviewer(series)
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


# ══ ① 순수 헬퍼: 수렴 판정 ═══════════════════════════════════════════════════


def test_rounds_max_default_and_knob(external):
    """미설정 → 2(출하 기본값) · local.conf `additional_reviewer.rounds_max` 가 상한을 바꾼다."""
    assert external._review_rounds_max({}) == 2
    assert external._review_rounds_max({"additional_reviewer.rounds_max": "5"}) == 5


def test_rounds_max_garbage_and_negative_fall_back(external):
    """비정수·음수는 기본값으로 fail-soft (다른 예산 노브와 같은 규칙)."""
    assert external._review_rounds_max({"additional_reviewer.rounds_max": "x"}) == 2
    assert external._review_rounds_max({"additional_reviewer.rounds_max": "-2"}) == 2


def _entry(external, must_fix_series):
    """must_fix 추이만 심은 게이트 항목 (예약 순번은 나열 순서 그대로)."""
    ledger: dict = {"g": {"rounds": [
        {"sequence": index + 1, "verdict": 0 if value == 0 else 1, "must_fix": value}
        for index, value in enumerate(must_fix_series)
    ]}}
    return external._gate_entry(ledger, "g")


def test_series_reads_reservation_order_not_append_order(external):
    """추이는 **예약 순번** 순이다 — append 순서(완료 순서)로 읽으면 발산이 뒤집혀 보인다."""
    ledger = {"g": {"rounds": [
        {"sequence": 2, "must_fix": 5}, {"sequence": 1, "must_fix": 1},
    ]}}
    entry = external._gate_entry(ledger, "g")
    assert external._recorded_must_fix_series(entry) == (1, 5)


def test_unknown_must_fix_is_not_folded_into_zero(external):
    """셀 근거가 없던 라운드는 '미상'(None)이고 0 으로 접지 않는다 (보수 방향)."""
    entry = _entry(external, [None])
    assert external._recorded_must_fix_series(entry) == (None,)
    assert external._format_must_fix_series((3, None, 0)) == "3 → 미상 → 0"


def test_convergence_passes_below_the_cap(external):
    """상한 미만이고 발산이 아니면 통과 (정상 경로 무영향)."""
    assert external._convergence_refusal(_entry(external, []), 3) is None
    assert external._convergence_refusal(_entry(external, [3]), 3) is None
    assert external._convergence_refusal(_entry(external, [3, 1]), 3) is None


def test_convergence_blocks_on_increase_before_the_cap(external):
    """직전 대비 must_fix 증가는 상한 도달을 기다리지 않는다 (조기 차단·DoD)."""
    refusal = external._convergence_refusal(_entry(external, [3, 5]), 3)
    assert refusal == external.CONVERGENCE_DIVERGING


def test_convergence_blocks_at_the_cap_with_unresolved_defects(external):
    """상한 도달 + must_fix 미해소 → 차단 (비감소 3R 형상·DoD)."""
    refusal = external._convergence_refusal(_entry(external, [3, 2, 2]), 3)
    assert refusal == external.CONVERGENCE_CAP_UNRESOLVED


def test_convergence_blocks_at_the_cap_even_when_clean(external):
    """상한 도달은 must_fix 0 이어도 라운드를 더 주지 않는다 (사유 라벨만 갈린다)."""
    refusal = external._convergence_refusal(_entry(external, [3, 1, 0]), 3)
    assert refusal == external.CONVERGENCE_CAP_REACHED


def test_convergence_treats_unknown_last_round_as_unresolved(external):
    """마지막 라운드가 '미상'이면 해소로 접지 않는다 — 미상을 0 으로 읽으면 발산이 통과한다."""
    refusal = external._convergence_refusal(_entry(external, [1, 1, None]), 3)
    assert refusal == external.CONVERGENCE_CAP_UNRESOLVED


# ══ ① main 흐름: 차단·통과 ══════════════════════════════════════════════════


def test_default_cap_blocks_the_third_round(external, monkeypatch, tmp_path, capsys):
    """출하 기본 2에서 must_fix 3→2 뒤 3라운드째 실행 전에 막힌다 (DoD)."""
    reviewer = _wire(external, monkeypatch, tmp_path, series=[3, 2])
    argv = ["--gate", "T-0593", "--paths", "x.py"]
    for _ in range(2):
        assert external.main(argv) == 1          # 반려 rc (라운드 자체는 정상 진행)
    assert reviewer.calls == 2
    capsys.readouterr()

    assert external.main(argv) == external.EXIT_ROUND_LIMIT_EXCEEDED
    assert reviewer.calls == 2                    # 리뷰어 미호출 (호출 0)
    err = capsys.readouterr().err
    assert "수렴 게이트 차단" in err
    assert "3 → 2" in err                         # 판정 근거를 그대로 보여준다
    assert "현재 티켓을 정지" in err and "사용자에게 보고" in err
    assert "--confirm-fix" not in err
    assert _ledger(tmp_path)["T-0593"]["count"] == 2   # 거부는 장부를 늘리지 않는다


def test_increasing_series_is_blocked_early(external, monkeypatch, tmp_path, capsys):
    """override 3에서도 must_fix 3→5 는 3라운드째에 조기 차단된다 (DoD)."""
    conf = {**_REVIEWER_TARGET, "additional_reviewer.rounds_max": "3"}
    reviewer = _wire(external, monkeypatch, tmp_path, series=[3, 5], conf=conf)
    argv = ["--gate", "T-0594", "--paths", "x.py"]
    for _ in range(2):
        assert external.main(argv) == 1
    capsys.readouterr()

    assert external.main(argv) == external.EXIT_ROUND_LIMIT_EXCEEDED
    assert reviewer.calls == 2
    err = capsys.readouterr().err
    assert "발산" in err and "조기 차단" in err


def test_converging_series_runs_all_rounds(external, monkeypatch, tmp_path):
    """local.conf 3 상향 시 must_fix 3→1→0 세 라운드를 모두 허용한다 (DoD)."""
    conf = {**_REVIEWER_TARGET, "additional_reviewer.rounds_max": "3"}
    reviewer = _wire(external, monkeypatch, tmp_path, series=[3, 1, 0], conf=conf)
    argv = ["--gate", "T-0595", "--paths", "x.py"]
    assert [external.main(argv) for _ in range(3)] == [1, 1, 0]
    assert reviewer.calls == 3
    entry = _ledger(tmp_path)["T-0595"]
    assert [row["must_fix"] for row in entry["rounds"]] == [3, 1, 0]


def test_rounds_max_knob_can_raise_the_cap(external, monkeypatch, tmp_path):
    """`additional_reviewer.rounds_max=3` 상향 시 기본값과 달리 3라운드째를 허용한다."""
    conf = {**_REVIEWER_TARGET, "additional_reviewer.rounds_max": "3"}
    reviewer = _wire(external, monkeypatch, tmp_path, series=[2, 1, 0], conf=conf)
    argv = ["--gate", "T-0596", "--paths", "x.py"]
    assert [external.main(argv) for _ in range(3)] == [1, 1, 0]
    assert reviewer.calls == 3


def test_rounds_max_knob_can_lower_the_cap(external, monkeypatch, tmp_path):
    """`additional_reviewer.rounds_max=1` 하향 시 첫 라운드 뒤 2라운드째를 차단한다."""
    conf = {**_REVIEWER_TARGET, "additional_reviewer.rounds_max": "1"}
    reviewer = _wire(external, monkeypatch, tmp_path, series=[1], conf=conf)
    argv = ["--gate", "T-0596-lower", "--paths", "x.py"]

    assert external.main(argv) == 1
    assert external.main(argv) == external.EXIT_ROUND_LIMIT_EXCEEDED
    assert reviewer.calls == 1
    assert _ledger(tmp_path)["T-0596-lower"]["count"] == 1


def test_gate_without_a_ledger_is_untouched(external, monkeypatch, tmp_path):
    """라운드 장부가 없는 티켓의 첫 라운드는 그대로 진행된다 (정상 경로 무영향·DoD)."""
    reviewer = _wire(external, monkeypatch, tmp_path, series=[0])
    assert _ledger(tmp_path) == {}
    assert external.main(["--gate", "T-0597", "--paths", "x.py"]) == 0
    assert reviewer.calls == 1


# ══ ② 폐지한 fix 후 재리뷰 표면 ═══════════════════════════════════════════════


@pytest.mark.parametrize("gate_state", ("empty", "rejected", "passed"))
def test_removed_confirm_fix_flag_is_rejected_before_any_send(
        external, monkeypatch, tmp_path, capsys, gate_state):
    """게이트 이력과 무관하게 폐지 플래그는 argparse 에서 거부되고 장부·호출은 불변이다."""
    reviewer = _wire(
        external, monkeypatch, tmp_path,
        series=[0] if gate_state == "passed" else [1],
    )
    argv = ["--gate", "T-0598", "--paths", "x.py"]
    if gate_state == "rejected":
        assert external.main(argv) == 1
    elif gate_state == "passed":
        assert external.main(argv) == 0
    before_calls = reviewer.calls
    ledger_path = tmp_path / ".project_manager" / ".local" / "review_rounds.json"
    before = ledger_path.read_bytes() if ledger_path.exists() else None
    capsys.readouterr()

    with pytest.raises(SystemExit):
        external.main(argv + ["--confirm-fix"])

    assert "unrecognized arguments: --confirm-fix" in capsys.readouterr().err
    assert reviewer.calls == before_calls
    assert (ledger_path.read_bytes() if ledger_path.exists() else None) == before


def test_round_records_keep_must_fix_texts_for_audit(
        external, monkeypatch, tmp_path):
    """폐지 경로와 무관하게 라운드 기록은 결함 건수와 항목 텍스트를 함께 보존한다."""
    _wire(external, monkeypatch, tmp_path, series=[2])
    assert external.main(["--gate", "T-0602d", "--paths", "x.py"]) == 1
    entry = _ledger(tmp_path)["T-0602d"]
    assert entry["rounds"][0]["must_fix"] == 2
    assert entry["records"][0]["must_fix_items"] == ["결함 1", "결함 2"]


# ══ ②-b 수렴 상한의 **진행 중 예약** (T-0602 ②) ══════════════════════════════
# codex R2 지적: 상한 판정이 완료 `rounds` 만 세고 예약된 실행을 무시한다 — 2완료 상태에서 두
# 실행이 연속 예약되면 둘 다 통과해 상한 3 인데 4라운드가 호출된다. 미마감 예약을 상한에 더해
# 그 창을 닫는다(미완 재시도 상한과는 역할이 다르다).


def _ago(seconds: float) -> str:
    """지금으로부터 `seconds` 초 전의 UTC ISO 시각 (장부 표기와 같은 형식)."""
    return (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(seconds=seconds)).isoformat()


def _seed_rounds(tmp_path, gate: str, *, completed: int, inflight: int,
                 inflight_age_sec: float = 5) -> None:
    """완료 산출 `completed` 건 + **미마감 예약** `inflight` 건인 장부를 심는다.

    미마감 예약의 나이(`inflight_age_sec`)가 회수 판정 입력이다 — 하네스 벽시계 백스톱을 넘긴
    예약은 실행 중일 수 없으므로 상한 합산에서 빠진다."""
    records = [
        {"id": f"done{index}", "sequence": index, "started_at": "2026-08-09T00:00:00+00:00",
         "finished_at": "2026-08-09T00:00:01+00:00", "verdict": True}
        for index in range(1, completed + 1)
    ] + [
        {"id": f"live{index}", "sequence": completed + index,
         "started_at": _ago(inflight_age_sec)}               # 마감 기록 없음 = 진행 중
        for index in range(1, inflight + 1)
    ]
    rounds = [
        {"ts": "2026-08-09T00:00:01+00:00", "id": f"done{index}", "sequence": index,
         "verdict": 1, "must_fix": 2, "suggestions": None}
        for index in range(1, completed + 1)
    ]
    path = tmp_path / ".project_manager" / ".local" / "review_rounds.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({gate: {
        "count": completed + inflight,
        "sequence": completed + inflight, "records": records, "rounds": rounds,
    }}), encoding="utf-8")


def test_inflight_reservation_counts_toward_the_convergence_cap(external, tmp_path):
    """2완료 + 1예약 = 상한 3 도달 — 완료분만 세면 이 형상이 통과해 4호출이 난다 (순수 판정)."""
    _seed_rounds(tmp_path, "g", completed=2, inflight=1)
    ledger = json.loads(
        (tmp_path / ".project_manager" / ".local" / "review_rounds.json").read_text(
            encoding="utf-8"))
    entry = external._gate_entry(ledger, "g")
    assert external._load_review_rounds().inflight_reservations(entry) == 1
    assert external._convergence_round_usage(entry) == (2, 1)
    assert external._convergence_refusal(entry, 3) == external.CONVERGENCE_CAP_UNRESOLVED


def test_second_concurrent_reservation_is_refused_before_sending(
        external, monkeypatch, tmp_path, capsys):
    """재현: 2완료 + 1예약(동시 실행 A) 상태에서 실행 B 는 호출 전에 막힌다 (상한 3·4호출 창)."""
    conf = {**_REVIEWER_TARGET, "additional_reviewer.rounds_max": "3"}
    reviewer = _wire(external, monkeypatch, tmp_path, series=[2], conf=conf)
    _seed_rounds(tmp_path, "T-0602f", completed=2, inflight=1)

    rc = external.main(["--gate", "T-0602f", "--paths", "x.py"])

    assert rc == external.EXIT_ROUND_LIMIT_EXCEEDED
    assert reviewer.calls == 0                       # 호출 0
    err = capsys.readouterr().err
    assert "수렴 게이트 차단" in err
    assert "진행 중 예약 1" in err                    # 판정 근거를 그대로 보여준다
    assert _ledger(tmp_path)["T-0602f"]["count"] == 3   # 거부는 예약을 늘리지 않는다


def test_completed_rounds_below_the_cap_still_run(external, monkeypatch, tmp_path):
    """정상 경로 무변경 — 진행 중 예약이 없으면 2완료 상태의 3라운드째는 그대로 나간다."""
    conf = {**_REVIEWER_TARGET, "additional_reviewer.rounds_max": "3"}
    reviewer = _wire(external, monkeypatch, tmp_path, series=[1], conf=conf)
    _seed_rounds(tmp_path, "T-0602g", completed=2, inflight=0)

    assert external.main(["--gate", "T-0602g", "--paths", "x.py"]) == 1
    assert reviewer.calls == 1


def test_fresh_reservation_counts_but_a_timed_out_one_does_not(external, tmp_path):
    """회수 기준은 **하네스 벽시계 백스톱**이다 — 신선 예약은 세고, 초과 예약은 세지 않는다.

    kill·전원차단으로 마감하지 못한 레코드는 영원히 미마감이라, 세면 연장 승인이 없는 수렴 축을
    영구 잠식한다(중단 2회 = 상한 3 이 라운드 1회). 백스톱을 넘긴 예약은 그 시점에 실행 중일 수
    없으므로 회수한다."""
    def _entry(age_sec: float) -> dict:
        _seed_rounds(tmp_path, "g", completed=0, inflight=1, inflight_age_sec=age_sec)
        ledger = json.loads(
            (tmp_path / ".project_manager" / ".local" / "review_rounds.json").read_text(
                encoding="utf-8"))
        return external._gate_entry(ledger, "g")

    fresh = _entry(5)
    assert external._load_review_rounds().inflight_reservations(fresh, wall_timeout_sec=3600) == 1
    stale = _entry(3600 + 60)
    assert external._load_review_rounds().inflight_reservations(stale, wall_timeout_sec=3600) == 0
    # 백스톱을 안 넘기면 종전대로 전부 센다(회수는 시간 조건 하나뿐).
    assert external._load_review_rounds().inflight_reservations(stale) == 1


def test_timed_out_reservation_does_not_lock_the_convergence_axis(
        external, monkeypatch, tmp_path):
    """같은 2완료+1예약 형상이라도 그 예약이 **백스톱을 넘겼으면** 라운드는 나간다 (회수 경로).

    신선 예약이면 같은 장부가 차단된다(`test_second_concurrent_reservation_is_refused_before_sending`)
    — 두 테스트가 회수 기준이 *시간* 하나임을 함께 못박는다. 회수가 없으면 kill 한 번이 상한을
    영구 잠식한다(연장 승인이 폐지된 축이라 되돌릴 손잡이가 없다)."""
    conf = {**_REVIEWER_TARGET, "additional_reviewer.rounds_max": "3"}
    reviewer = _wire(external, monkeypatch, tmp_path, series=[1], conf=conf)
    _seed_rounds(tmp_path, "T-0602j", completed=2, inflight=1,
                 inflight_age_sec=3600 * 24)             # 하루 전 예약 = 실행 중일 수 없다

    assert external.main(["--gate", "T-0602j", "--paths", "x.py"]) == 1
    assert reviewer.calls == 1, "회수되지 않은 잔재가 라운드를 잠갔다"


def test_refunded_reservation_does_not_hold_the_cap(external, monkeypatch, tmp_path):
    """호출이 없던 예약은 환불로 레코드째 사라져 상한을 잡아 두지 않는다 (fail-closed 오차단 방지)."""
    conf = {**_REVIEWER_TARGET, "additional_reviewer.rounds_max": "3"}
    _wire(external, monkeypatch, tmp_path, series=[1], conf=conf)
    unstarted = {
        "reviewer": "x", "ok": False, "output": "[리뷰어 명령 없음]",
        "answer": "[리뷰어 명령 없음]",
        "verdict": {"has_must_fix": False, "has_pass": False}, "file": None,
        "failed": True, "started": False, "any_must_fix": False, "all_pass": False,
    }
    monkeypatch.setattr(external, "run_review", lambda *a, **k: dict(unstarted))
    _seed_rounds(tmp_path, "T-0602h", completed=2, inflight=0)
    assert external.main(["--gate", "T-0602h", "--paths", "x.py"]) == 1

    entry = _ledger(tmp_path)["T-0602h"]
    assert external._load_review_rounds().inflight_reservations(external._gate_entry({"g": entry}, "g")) == 0
    reviewer = _Reviewer([1])
    monkeypatch.setattr(external, "run_review", reviewer)
    assert external.main(["--gate", "T-0602h", "--paths", "x.py"]) == 1   # 여전히 열려 있다
    assert reviewer.calls == 1


# ══ ②-c 만료 기준은 **레코드가 소유한다** (T-0604 ④) ═════════════════════════
# 회수 기준을 "지금 장부를 읽는 호출자의 백스톱"으로 재면, 짧은 timeout 의 후속 호출이 긴
# timeout 으로 실제 돌고 있는 라운드를 stale 로 접어 상한을 넘겨 예약한다. 예약 시점에 그 실행의
# 만료 시각을 레코드에 새기고, 판정은 그 값을 본다. 값 없는 구레코드만 종전 규칙으로 합산한다.


def _ahead(seconds: float) -> str:
    """지금으로부터 `seconds` 초 뒤의 UTC ISO 시각 (장부 표기와 같은 형식)."""
    return (datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(seconds=seconds)).isoformat()


def _inflight_entry(*, deadline: str | None, started_at: str) -> dict:
    record = {"id": "live1", "sequence": 1, "started_at": started_at}
    if deadline is not None:
        record["deadline"] = deadline
    return {"records": [record], "rounds": []}


def test_reservation_stamps_its_own_deadline(external):
    """예약이 **이 실행의** 백스톱을 만료 시각으로 새긴다 (백스톱 없으면 null)."""
    entry: dict = {}
    record = external._reserve_round(entry, "r1", wall_timeout_sec=3600)
    assert record["deadline"] > _ahead(3500) and record["deadline"] < _ahead(3700)
    assert external._reserve_round(entry, "r2")["deadline"] is None
    assert external._reserve_round(entry, "r3", wall_timeout_sec=0)["deadline"] is None


def test_a_short_caller_timeout_does_not_expire_a_long_running_reservation(external):
    """긴 timeout 으로 도는 라운드를 짧은 timeout 의 후속 호출이 stale 로 접지 않는다.

    이 오판이 곧 상한 초과 예약이다 — 실제로 나가 있는 호출을 0으로 세고 슬롯을 다시 내준다."""
    entry = _inflight_entry(deadline=_ahead(3600), started_at=_ago(600))

    assert external._load_review_rounds().inflight_reservations(entry, wall_timeout_sec=60) == 1
    assert external._convergence_refusal(entry, 1, wall_timeout_sec=60) is not None


def test_an_expired_deadline_is_reclaimed_even_with_a_long_caller_timeout(external):
    """반대 방향도 레코드가 이긴다 — 만료 시각을 지난 예약은 호출자 백스톱과 무관하게 회수된다."""
    entry = _inflight_entry(deadline=_ago(1), started_at=_ago(30))

    assert external._load_review_rounds().inflight_reservations(entry, wall_timeout_sec=3600 * 24) == 0


def test_a_legacy_record_without_a_deadline_keeps_the_previous_rule(external):
    """만료 시각이 없는 구레코드는 종전 규칙(호출자 백스톱 대 `started_at`)으로 보수 합산한다."""
    fresh = _inflight_entry(deadline=None, started_at=_ago(5))
    stale = _inflight_entry(deadline=None, started_at=_ago(3600 + 60))

    assert external._load_review_rounds().inflight_reservations(fresh, wall_timeout_sec=3600) == 1
    assert external._load_review_rounds().inflight_reservations(stale, wall_timeout_sec=3600) == 0
    assert external._load_review_rounds().inflight_reservations(stale) == 1        # 백스톱 없으면 전부 센다


def test_an_unreadable_deadline_is_counted_conservatively(external):
    """만료 시각을 읽을 수 없는 레코드는 계속 센다 — 판정 불능은 세는 쪽으로 틀린다."""
    entry = _inflight_entry(deadline=None, started_at=_ago(5))
    entry["records"][0]["deadline"] = 12345                   # 문자열 아님 = 판정 불가

    assert external._load_review_rounds().inflight_reservations(entry, wall_timeout_sec=3600) == 1


def test_a_real_reservation_records_the_deadline_in_the_ledger(
        external, monkeypatch, tmp_path):
    """실 실행이 만든 예약도 장부에 만료 시각을 남긴다 (`--timeout` 이 그 값의 출처)."""
    _wire(external, monkeypatch, tmp_path, series=[0])

    assert external.main(
        ["--gate", "T-0604a", "--paths", "x.py", "--timeout", "3600"]) == 0

    record = _ledger(tmp_path)["T-0604a"]["records"][-1]
    assert record["deadline"] > _ahead(3500) and record["deadline"] < _ahead(3700)


# ══ ③ 변경량 상한 없음 · 공유 측정 ══════════════════════════════════════════


def _git_init(root: Path) -> None:
    for args in (["init", "-q"], ["config", "user.email", "t@e"], ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def test_reviewer_call_has_no_change_size_cap(external, monkeypatch, tmp_path, capsys):
    """estimate small 티켓 게이트의 400줄 변경도 리뷰어를 부른다 — 변경 줄 수 거부가 없다."""
    real_extract_diff = external.extract_diff
    _wire(external, monkeypatch, tmp_path, series=[0])
    monkeypatch.setattr(external, "extract_diff", real_extract_diff)
    # 실 티켓 게이트라 라운드 파일 회수가 구조화 블록을 요구한다 — 통과 회신에 빈 블록을 싣는다.
    block = json.dumps({"version": _load("pm_delegate").PM_REVIEW_VERSION, "findings": [],
                        "confirmations": []}, separators=(",", ":"))
    reviewer = _Reviewer([0])
    answer = _answer(0) + f"\n```pm-review-v1\n{block}\n```\n"

    def _structured_review(*args, **kwargs):
        result = reviewer(*args, **kwargs)
        result.update(output=answer, answer=answer)
        return result

    monkeypatch.setattr(external, "run_review", _structured_review)
    _write_ticket(tmp_path, "T-0950", "estimate: small")
    _git_init(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "src"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "seed"], check=True,
                   capture_output=True)
    (tmp_path / "src" / "app.py").write_text(
        "seed\n" + "".join(f"line{index}\n" for index in range(400)), encoding="utf-8")

    assert external.main(["--gate", "T-0950", "--paths", "src/app.py"]) == 0
    assert reviewer.calls == 1
    out, err = capsys.readouterr()
    assert "서킷브레이커" not in out + err
    for name in ("_diff_cap_refusal", "diff_cap_block", "_diff_cap", "DIFF_CAP_KEY_PREFIX",
                 "diff_line_total", "measured_numstat_text", "cluster_integration_tip",
                 "parse_ticket_estimate"):
        assert not hasattr(external, name), name


# ── 기계 mirror 제외 (T-0601 ①) ────────────────────────────────────────────
# `templates/<타깃>/.project_manager/` 는 pm_update 가 기계로 내보내는 엔진 사본이다. 손작업과
# 같은 대상으로 보면 같은 줄이 출하 타깃 수만큼 다시 잡힌다. mirror 정합은 drift-0 가드가 따로
# 지킨다.


@pytest.mark.parametrize("path, mirrored", [
    ("templates/claude_code/.project_manager/tools/board.py", True),
    ("templates/codex/.project_manager/tools/pm_delegate.py", True),
    ("./templates/opencode/.project_manager/local.conf", True),          # `./` 표기
    (r"templates\opencode\.project_manager\tools\board.py", True),       # Windows 표기
    (".opencode/node_modules/pkg/index.js", True),                       # 루트 npm 산출물
    (".project_manager/tools/board.py", False),                          # 엔진 원본(손작업)
    (".opencode/package.json", False),                                   # 별도 소형 untrack
    ("templates/claude_code/CLAUDE.md", False),                          # manifest 미등재(손작업)
    ("templates/claude_code/.claude/agents/architect.md", True),         # bare @render(기계 mirror·T-0832)
    ("tests/test_board_lint.py", False),
])
def test_machine_mirror_predicate_is_the_single_exclusion_rule(external, path, mirrored):
    """제외 판정은 경로 문자열 하나다 — 두 소비처(리뷰·완료)가 같은 술어를 쓴다(사본 0)."""
    assert external.is_machine_mirror_path(path) is mirrored


# ── 어댑터층 기계 mirror (T-0832) ───────────────────────────────────────────
# `_MACHINE_MIRROR_RE` 는 `.project_manager/` 만 봤다 — 그 밖의 어댑터층(`.claude/`·`.codex/`·
# `.opencode/`)은 pm_update --all-targets 산출인데도 손작업으로 계상됐다(689줄 실측). 아래는 그
# 판정이 manifest `@source=` 방향에서 파생됨을 실제 세 출하 manifest 로 고정한다 — 경로 패턴
# 하드코딩이 아니라는 것과, flavor 가 손으로 관리하는 canonical 원본은 여전히 계상된다는 것.


@pytest.mark.parametrize("path, mirrored", [
    # bare(마커 없음) manifest 항목 — 소스는 레포 루트의 같은 상대경로(교차 위치) → 기계 mirror.
    ("templates/opencode/.claude/skills/pm-env/SKILL.md", True),
    ("templates/opencode/.claude/skills/pm-dev-delegate/SKILL.md", True),
    # `@source=` 가 flavor 자기 자신을 가리키는(self-referential) override — pm_update 가 재생성
    # 하지 않는 canonical 원본이라 flavor 가 손으로 고친다(결정문 예시와 동형).
    ("templates/codex/.agents/skills/pm-dev-delegate/SKILL.md", False),
    ("templates/claude_code/.claude/ctx_guard.py", False),
    # `@source=` 가 다른 flavor 물리 경로(레포 루트 `.claude/skills`)를 가리키는 진짜 교차 위치.
    ("templates/codex/.agents/skills/pm-adr/SKILL.md", True),
    # manifest 미등재 — instance/flavor 전용 root doc·config 는 애초에 판정에 들어오지 않는다.
    ("templates/codex/.codex/config.toml", False),
    ("templates/opencode/AGENTS.md", False),
    ("templates/opencode/.opencode/opencode.jsonc", False),
])
def test_adapter_layer_mirror_derives_from_manifest_source_direction(external, path, mirrored):
    """어댑터층 판정 = 그 타깃 engine.manifest 의 `@source=` 방향(자기참조=손작업·교차=mirror)."""
    assert external.is_machine_mirror_path(path) is mirrored


def test_comment_example_of_a_self_referential_original_is_still_self_referential(external):
    """판정 근거 주석이 예로 든 경로는 지금도 자기참조 원본이어야 한다 (설명↔거동 drift 0).

    주석은 `_TEMPLATES_FLAVOR_RE` 위에서 판정 규칙을 서술하며 "flavor 가 손으로 관리하는 canonical
    원본" 의 실례를 하나 든다. manifest 에서 그 override 행이 사라지면 경로 성격이 기계 mirror 로
    뒤집히는데 주석은 옛 성격을 예로 든 채 남는다(T-0895 실측). 예시 경로를 주석에서 뽑아 술어로
    되재 그 drift 를 값으로 막는다 — `<타깃>` 류 자리표시자는 실 경로가 아니라 제외한다."""
    lines = (TOOLS / "additional_reviewer.py").read_text(encoding="utf-8").splitlines()
    anchor = next(index for index, line in enumerate(lines)
                  if line.startswith("_TEMPLATES_FLAVOR_RE"))
    start = anchor
    while start > 0 and lines[start - 1].startswith("#"):
        start -= 1
    quoted = [path for path in re.findall(r"`(templates/[^`\s]+)`", "\n".join(lines[start:anchor]))
              if "<" not in path]
    assert quoted, "판정 근거 주석이 자기참조 원본의 실 경로 예시를 잃었다"
    for path in quoted:
        assert external.is_machine_mirror_path(path) is False, (
            f"주석이 자기참조 원본의 예로 든 {path} 가 지금은 기계 mirror 다")


def test_adapter_layer_mirror_extends_to_a_new_target_without_code_changes(
    external, tmp_path, monkeypatch,
):
    """새 타깃이 engine.manifest 를 갖추면 패턴 수정 없이 판정에 편입된다 (표면 상한 고정).

    `templates/<타깃>/` 매칭이 flavor 이름을 하드코딩하지 않는다는 것을 실측한다 — REPO 를
    가짜 flavor(`newtarget`) 하나짜리 tmp 트리로 monkeypatch 하고 bare/자기참조/`@target-owned`
    세 항목을 선언해, 코드 변경 없이 같은 결과 형태가 나오는지 본다.

    `@target-owned` 항목은 F-001(T-0832 라운드3 리뷰) 회귀다 — bare 부모 디렉토리(`.guest`, 그
    자체로는 mirror) 아래에 더 구체적인 `@target-owned` override(`.guest/manual.md`)를 두어,
    그 override 가 부모의 mirror 판정을 덮고 손작업으로 남는지(측정에서 안 빠지는지) 확인한다."""
    fake_repo = tmp_path / "repo"
    manifest = fake_repo / "templates" / "newtarget" / ".project_manager" / "engine.manifest"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        "# fake newtarget manifest — AGENTS.md 는 의도적으로 미등재(instance-owned 대칭)\n"
        ".newtarget/agents\n"                                  # bare → 레포 루트 교차 → mirror
        ".newtarget/hooks/guard.py  @source=templates/newtarget/.newtarget/hooks/guard.py\n"
        ".guest\n"                                              # bare 부모 → 그 자체는 mirror
        ".guest/manual.md  @target-owned\n"                     # 더 구체적인 target-owned override
        ,
        encoding="utf-8",
    )
    monkeypatch.setattr(external, "REPO", fake_repo)

    assert external.is_machine_mirror_path("templates/newtarget/.newtarget/agents/x.md") is True
    assert external.is_machine_mirror_path(
        "templates/newtarget/.newtarget/hooks/guard.py") is False
    assert external.is_machine_mirror_path("templates/newtarget/AGENTS.md") is False
    # bare 부모 아래 다른 파일은 여전히 mirror(교차 위치) — override 의 영향이 그 파일 자신으로 좁다.
    assert external.is_machine_mirror_path("templates/newtarget/.guest/other.md") is True
    # `@target-owned` override 파일은 부모가 mirror 라도 손작업으로 남는다(F-001).
    assert external.is_machine_mirror_path("templates/newtarget/.guest/manual.md") is False


def test_exclusion_is_a_subtree_including_hand_edited_files(external):
    """제외는 **subtree 단위**다 — 그 안의 manifest 밖 손편집 파일도 함께 빠진다(알고 고른 경계).

    이 경계를 문서와 술어가 같은 말로 해야 다음 사람이 "mirror 만 빠진다"로 오독하지 않는다.
    오차 방향은 측정 축소뿐이라 정당한 작업을 오차단하지 않는다."""
    hand_edited = "templates/codex/.project_manager/engine.manifest"
    assert external.is_machine_mirror_path(hand_edited) is True
    doc = external.is_machine_mirror_path.__doc__
    assert "전부는 아니다" in doc and "측정 축소" in doc


def _git_repo(tmp_path: Path) -> Path:
    root = tmp_path / "code"
    root.mkdir()
    for args in (["init"], ["config", "user.email", "t@e"], ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(root), *args], check=True,
                       capture_output=True, text=True)
    (root / "seed.py").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-m", "seed"], check=True,
                   capture_output=True)
    return root


def _added_lines(diff_text: str) -> list[str]:
    """unified diff 의 추가 줄 — 측정 폭에 실린 내용을 줄 단위로 본다."""
    return [line for line in diff_text.splitlines()
            if line.startswith("+") and not line.startswith("+++")]


def test_measured_diff_reuses_the_review_stage_table(external, tmp_path):
    """측정 폭은 리뷰가 보는 폭과 같다 — 'HEAD' 단계는 스테이징+언스테이징 둘 다 싣는다."""
    root = _git_repo(tmp_path)
    (root / "seed.py").write_text("seed\n" + "".join(f"a{i}\n" for i in range(9)),
                                  encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "seed.py"], check=True,
                   capture_output=True)
    (root / "seed.py").write_text("seed\n" + "".join(f"a{i}\n" for i in range(12)),
                                  encoding="utf-8")
    diff_text = external.measured_diff_text(root, "HEAD", ["seed.py"])
    assert _added_lines(diff_text) == [f"+a{i}" for i in range(12)]
    assert external.measured_diff_text(root, "HEAD", ["없는경로.py"]) == ""


# ── untracked 신규 파일 (T-0604 ③) ──────────────────────────────────────────
# `git diff` 는 index/커밋에 있는 것만 본다 — 아직 add 되지 않은 신규 파일은 리뷰 diff 에서도,
# 완료 기록 측정에서도 통째로 빠졌다. 측정한 뒤 그대로 stage 되는 순서(완료 기록)가 그 공백의
# 직접 대가다. 포함은 **index 를 건드리지 않는** `--no-index` 로 한다.


def _untracked(root: Path, relpath: str, lines: int) -> Path:
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"n{i}\n" for i in range(lines)), encoding="utf-8")
    return path


def test_untracked_new_files_ride_the_measured_width(external, tmp_path):
    """add 안 된 신규 파일도 측정 폭에 실린다 — 종전엔 통째로 빠졌다."""
    root = _git_repo(tmp_path)
    _untracked(root, "new_module.py", 40)

    for scope in (["new_module.py"], ["."]):
        diff_text = external.measured_diff_text(root, "HEAD", scope)
        assert "new_module.py" in diff_text and len(_added_lines(diff_text)) == 40, scope


def test_measuring_untracked_files_does_not_touch_the_index(external, tmp_path):
    """측정은 read-only 다 — `git add -N` 류로 index 를 바꾸면 위임 스코프 지문이 흔들린다."""
    root = _git_repo(tmp_path)
    _untracked(root, "new_module.py", 12)
    before = subprocess.run(["git", "-C", str(root), "status", "--porcelain"],
                            capture_output=True, text=True, check=True).stdout

    assert "new_module.py" in external.measured_diff_text(root, "HEAD", ["."])

    after = subprocess.run(["git", "-C", str(root), "status", "--porcelain"],
                           capture_output=True, text=True, check=True).stdout
    assert after == before == "?? new_module.py\n"


def test_untracked_files_outside_the_declared_scope_are_not_measured(external, tmp_path):
    """폭은 선언 경로가 정한다 — 스코프 밖 신규 파일은 싣지 않는다(diff 와 같은 pathspec)."""
    root = _git_repo(tmp_path)
    _untracked(root, "src/inside.py", 7)
    _untracked(root, "elsewhere/outside.py", 900)

    diff_text = external.measured_diff_text(root, "HEAD", ["src/"])
    assert "src/inside.py" in diff_text and "outside.py" not in diff_text


def test_ignored_files_are_not_measured(external, tmp_path):
    """`.gitignore` 대상은 신규 파일이어도 측정 밖이다 (산출물이 스코프를 부풀리지 않게)."""
    root = _git_repo(tmp_path)
    (root / ".gitignore").write_text("build/\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", ".gitignore"], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-m", "ignore"], check=True,
                   capture_output=True)
    (root / "seed.py").write_text("seed\nedit\n", encoding="utf-8")   # 손작업 1줄 추가
    _untracked(root, "build/artifact.py", 300)

    diff_text = external.measured_diff_text(root, "HEAD", ["."])
    assert "artifact.py" not in diff_text and _added_lines(diff_text) == ["+edit"]


def test_a_commit_range_stage_does_not_pull_in_untracked_files(external, tmp_path):
    """커밋 사이 폭(명시 base)에는 붙이지 않는다 — 아직 커밋되지 않은 파일은 그 폭의 구성원이 아니다."""
    root = _git_repo(tmp_path)
    (root / "seed.py").write_text("seed\nmore\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-m", "second"], check=True,
                   capture_output=True)
    _untracked(root, "new_module.py", 40)

    diff_text = external.measured_diff_text(root, "HEAD~1..HEAD", ["."])
    assert "new_module.py" not in diff_text and _added_lines(diff_text) == ["+more"]


def test_untracked_new_files_appear_in_the_review_diff(external, monkeypatch, tmp_path):
    """리뷰가 보는 diff 에도 신규 파일이 실린다 — 잰 것과 보낸 것이 같은 폭이다."""
    root = _git_repo(tmp_path)
    _untracked(root, "new_module.py", 3)
    monkeypatch.setattr(external, "REPO", root)

    diff = external.extract_diff("HEAD", ["new_module.py"])

    assert "new file mode" in diff and "new_module.py" in diff and "+n0" in diff


# ══ ③-b estimate 해석 단일화 (T-0602 ⑥) ═════════════════════════════════════
# codex R2 지적: 리뷰쪽은 정규식으로 읽어 `estimate: small # reviewed` 를 `small # reviewed` 로
# 해석했고, board 는 같은 값을 YAML 로 `small` 로 읽는다. 리뷰 입력 머리의 `estimate=` 표시가
# board 와 다른 값을 보이지 않게 해석을 board 의 frontmatter 로더 하나로 모은다.


def _write_ticket(tmp_path, tid: str, frontmatter: str) -> Path:
    """tmp board 에 티켓 한 건 — frontmatter 본문만 케이스별로 바꾼다."""
    tickets = tmp_path / ".project_manager" / "board" / "tickets" / "claimed"
    tickets.mkdir(parents=True, exist_ok=True)
    path = tickets / f"{tid}-x.md"
    path.write_text(f"---\nid: {tid}\n{frontmatter}\ntouches:\n- src/\n---\n\n# 본문\n",
                    encoding="utf-8")
    return path


@pytest.mark.parametrize("declared, expected", [
    ("estimate: small # reviewed", "small"),        # 재현 형상 — YAML 주석 꼬리
    ("estimate: small   # 2026-08-09 PM 재판정", "small"),
    ("estimate: 'small'", "small"),                 # 인용 형상
    ('estimate: "medium"  # quoted', "medium"),
    ("estimate:   large  ", "large"),               # 공백 여백
    ("estimate: [small]", None),                    # 비-문자열 — 값을 지어내지 않는다
    ("estimate:", None),                            # 빈 값
])
def test_estimate_is_read_as_yaml_not_as_a_regex_tail(
        external, tmp_path, monkeypatch, declared, expected):
    """주석·인용·여백 형상을 YAML 해석으로 읽는다 — 꼬리를 값에 붙이면 board 와 다른 값이 된다."""
    path = _write_ticket(tmp_path, "T-0910", declared)
    monkeypatch.setattr(external, "REPO", tmp_path)
    assert external._parse_estimate_from_file(path) == expected


# ══ ③-c 티켓 조회는 canonical ID 정확 일치 (T-0603 ④) ═══════════════════════
# codex R3 지적: touches 조회(추가 리뷰어)와 티켓 로드(ticket_finish)가 `{id}-*.md` prefix glob
# **첫 매칭**을 믿는다 — `T-0036` 과 `T-0036-001` 이 공존하면 다른 티켓의 touches 로 검토 범위가
# 정해지거나(허용/차단이 뒤바뀐다) 완료 게이트가 남의 touches 로 판정한다. 판정을 board 공용
# seam(`find_ticket_exact`) 하나로 모아 닫았고, 아래는 공존 픽스처에서 **세 소비처**(회귀 강등·
# 리뷰 범위·완료 로드)가 전부 오인 0 임을 한 자리에서 단언한다.


def _coexisting_tickets(tmp_path) -> Path:
    """legacy `T-0036` 과 prefixed `T-0036-001` 이 함께 사는 tmp 보드 (claimed/)."""
    tickets = tmp_path / ".project_manager" / "board" / "tickets" / "claimed"
    tickets.mkdir(parents=True, exist_ok=True)
    (tickets / "T-0036-본래-티켓.md").write_text(
        "---\nid: T-0036\nestimate: small\ntouches:\n- src/pay.py\n---\n\n# 본문\n",
        encoding="utf-8")
    (tickets / "T-0036-001-다른-티켓.md").write_text(
        "---\nid: T-0036-001\nestimate: large\ntouches:\n- src/unrelated.py\n---\n\n# 본문\n",
        encoding="utf-8")
    return tickets


def _real_board_shim(tmp_path, finish) -> Path:
    """실 board 엔진을 tmp 보드에 앵커한 shim — 조회 판정은 **실 seam** 그대로다."""
    shim = tmp_path / "board_shim_exact.py"
    shim.write_text(
        f"ENGINE_REV = {finish.ENGINE_REV!r}\n"
        "import importlib.util, pathlib\n"
        "_spec = importlib.util.spec_from_file_location("
        f"'shim_board_exact', {str(TOOLS / 'board.py')!r})\n"
        "_real = importlib.util.module_from_spec(_spec)\n"
        "_spec.loader.exec_module(_real)\n"
        f"_real.REPO = pathlib.Path({str(tmp_path)!r})\n"
        "find_ticket_exact = _real.find_ticket_exact\n"
        "load_ticket = _real.load_ticket\n",
        encoding="utf-8")
    return shim


def test_prefix_glob_first_match_is_the_reproduced_hazard(tmp_path):
    """재현 — 공존 픽스처의 `T-0036-*.md` 첫 매칭은 **다른 티켓**이다(차단이 어디서 서는지 못박음)."""
    tickets = _coexisting_tickets(tmp_path)
    assert sorted(tickets.glob("T-0036-*.md"))[0].name.startswith("T-0036-001")


def test_three_gate_consumers_never_answer_with_a_prefixed_ticket(
        external, tmp_path, monkeypatch):
    """강등 스코프·리뷰 범위·완료 로드가 모두 canonical `T-0036` 을 본다 (오인 0·DoD)."""
    _coexisting_tickets(tmp_path)
    finish = _load("ticket_finish")
    board = _load("board")
    monkeypatch.setattr(board, "REPO", tmp_path)
    monkeypatch.setattr(external, "REPO", tmp_path)

    # ① 회귀 강등 스코프 (board)
    assert board._ticket_touches("T-0036") == ["src/pay.py"]
    assert board._gate_ticket("T-0036")[1].name.startswith("T-0036-본래")
    # ② 리뷰 범위 (추가 리뷰어)
    assert external.parse_ticket_touches("T-0036", pm_home=tmp_path) == ["src/pay.py"]
    # ③ 완료 게이트 티켓 로드 (ticket_finish)
    shim = _real_board_shim(tmp_path, finish)
    assert finish.get_ticket_touches(shim, "T-0036") == ["src/pay.py"]

    # prefixed 티켓 자신의 조회는 그대로 자기 값을 본다 (좁힘이 정상 조회를 삼키지 않는다).
    assert external.parse_ticket_touches("T-0036-001", pm_home=tmp_path) == ["src/unrelated.py"]
    assert finish.get_ticket_touches(shim, "T-0036-001") == ["src/unrelated.py"]
    assert board._ticket_touches("T-0036-001") == ["src/unrelated.py"]


def test_missing_ticket_keeps_each_consumer_on_its_own_fail_soft(
        external, tmp_path, monkeypatch):
    """정확 일치가 없으면 각 소비처는 종전 처리 그대로다 (FULL·빈 값·fail-loud)."""
    _coexisting_tickets(tmp_path)
    finish = _load("ticket_finish")
    board = _load("board")
    monkeypatch.setattr(board, "REPO", tmp_path)
    monkeypatch.setattr(external, "REPO", tmp_path)
    shim = _real_board_shim(tmp_path, finish)

    assert board._ticket_touches("T-0036-002") == []              # 스코프 없음 = FULL
    assert finish.get_ticket_touches(shim, "T-0036-002") == []
    with pytest.raises(external.AnchorResolutionError):           # touches 는 fail-loud 유지
        external.parse_ticket_touches("T-0036-002", pm_home=tmp_path)


def test_slugless_ticket_filename_still_resolves(external, tmp_path, monkeypatch):
    """슬러그 없는 `T-0040.md` 폴백은 유지된다 — prefix 충돌이 불가능한 정확 이름이다."""
    tickets = tmp_path / ".project_manager" / "board" / "tickets" / "claimed"
    tickets.mkdir(parents=True, exist_ok=True)
    (tickets / "T-0040.md").write_text(
        "---\nid: T-0040\nestimate: medium\ntouches:\n- src/a.py\n---\n\n# 본문\n",
        encoding="utf-8")
    monkeypatch.setattr(external, "REPO", tmp_path)
    assert external.parse_ticket_touches("T-0040", pm_home=tmp_path) == ["src/a.py"]


# ══ ④ 비-매핑 frontmatter 는 조회 경로를 죽이지 않는다 (T-0605 ④) ═══════════
# codex R5 지적: YAML 이 비어 있지 않은 **스칼라·리스트**면 파싱은 성공하고 `fm.get()` 이
# AttributeError 로 터진다. 그 예외가 정확-일치 조회(`find_ticket_exact`) 안에서 나면 활성 리뷰
# 판정·강등 판정보다 먼저 실행이 죽어, "손상 티켓은 skip 하고 FULL 로 남긴다"는 규약이 깨진다.


def _write_raw_ticket(tmp_path, name: str, text: str):
    """티켓 파일을 **원문 그대로** 심는다 (frontmatter 형상 자체가 검증 대상이라 조립하지 않는다)."""
    tickets = tmp_path / ".project_manager" / "board" / "tickets" / "claimed"
    tickets.mkdir(parents=True, exist_ok=True)
    path = tickets / name
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize("frontmatter", [
    "그냥 문자열",                      # 스칼라
    "- 목록\n- 원소",                   # 리스트
])
def test_non_mapping_frontmatter_never_crashes_the_lookup(tmp_path, monkeypatch, frontmatter):
    """비-매핑 frontmatter 는 **파일명 폴백**으로 접힌다 — 조회가 예외로 죽지 않는다."""
    board = _load("board")
    monkeypatch.setattr(board, "REPO", tmp_path)
    path = _write_raw_ticket(tmp_path, "T-0050-x.md", f"---\n{frontmatter}\n---\n\n# 본문\n")

    assert board._canonical_ticket_id(path) == "T-0050"      # AttributeError 없이 폴백
    assert board.find_ticket_exact("T-0050") == ("claimed", path)


def test_a_broken_ticket_does_not_hide_the_healthy_one(tmp_path, monkeypatch):
    """같은 번호 공간에 손상 티켓이 섞여도 정확-일치 조회는 성한 티켓을 답한다."""
    board = _load("board")
    monkeypatch.setattr(board, "REPO", tmp_path)
    _write_raw_ticket(tmp_path, "T-0051-001-broken.md", "---\n스칼라\n---\n\n# 본문\n")
    healthy = _write_raw_ticket(
        tmp_path, "T-0051-ok.md",
        "---\nid: T-0051\ntouches:\n- src/pay.py\n---\n\n# 본문\n")

    assert board.find_ticket_exact("T-0051") == ("claimed", healthy)
    assert board._ticket_touches("T-0051") == ["src/pay.py"]


def test_a_broken_ticket_scope_is_unresolved_not_a_crash(tmp_path, monkeypatch):
    """손상 티켓의 스코프 조회는 None(확정 실패) — 호출부가 FULL 로 남긴다."""
    board = _load("board")
    monkeypatch.setattr(board, "REPO", tmp_path)
    _write_raw_ticket(tmp_path, "T-0052-x.md", "---\n스칼라\n---\n\n# 본문\n")

    assert board._ticket_touches("T-0052") is None


# ══ ⑤ touches 형식 불명 = 강등 취소 (T-0605 ③) ══════════════════════════════
# 스칼라 문자열 touches 를 `list(...)` 로 펴면 문자 목록이 되어 `-k "s or r or c ..."` 같은
# 오염된 스코프가 선다. 실 frontmatter 로 그 경로를 재현하고 확정 실패(None)를 단언한다.


@pytest.mark.parametrize("declaration, expected", [
    ("touches: src/pay.py", None),                   # 스칼라 문자열 — 문자 스코프 재현
    ("touches:\n- src/pay.py\n- 3", None),           # 비문자열 원소
    ("touches:\n- src/pay.py", ["src/pay.py"]),      # 정상(무변경)
])
def test_ticket_touches_forces_a_list_of_strings(tmp_path, monkeypatch, declaration, expected):
    """실 frontmatter 기준 — 리스트[str] 만 스코프가 되고 그 밖은 확정 실패(None)다."""
    board = _load("board")
    monkeypatch.setattr(board, "REPO", tmp_path)
    _write_raw_ticket(tmp_path, "T-0053-x.md", f"---\nid: T-0053\n{declaration}\n---\n\n# 본문\n")

    assert board._ticket_touches("T-0053") == expected


# ══ ⑥ 구세대 outcome 은 신기록보다 앞 (T-0605 ⑥) ════════════════════════════
# codex R5 지적: `sequence` 없는 구기록을 뒤에 정렬하면 업그레이드 후 새 통과가 기록돼도 옛
# 반려가 영원히 '최신'이 된다. 정렬 판정은 board 공용 seam 하나가 소유한다(사본 금지).


def test_legacy_outcomes_sort_before_the_sequenced_ones(external):
    """순번 없는 구기록이 앞(오래된 쪽)·순번 있는 기록이 순번 순으로 뒤."""
    rounds = [{"sequence": 2, "verdict": 0}, {"verdict": 1}, {"sequence": 1, "verdict": 1}]
    ordered = external._ordered_round_outcomes(rounds)
    assert [row.get("sequence") for row in ordered] == [None, 1, 2]


def test_an_upgrade_pass_becomes_the_latest_over_a_legacy_rejection(external):
    """구기록 반려 뒤에 신기록 통과가 쌓이면 **최신은 통과**다 — 자격도 닫힌다."""
    entry = external._gate_entry({"g": {"rounds": [
        {"verdict": 1, "must_fix": 2},                       # 구세대 반려(순번 없음)
        {"sequence": 1, "verdict": 0, "must_fix": 0},        # 업그레이드 후 통과
    ]}}, "g")

    assert external._ordered_round_outcomes(entry["rounds"])[-1]["verdict"] == 0


def test_the_ordering_rule_is_the_shared_board_seam(external):
    """정렬 규칙의 소유자는 board 공용 seam 이다 — 사본이 생기면 두 표면이 갈린다."""
    board = _load("board")
    rounds = [{"sequence": 2}, {"verdict": 1}, {"sequence": 1}]
    assert external._ordered_round_outcomes(rounds) == sorted(
        rounds, key=board.round_outcome_order_key)
