"""리뷰 수렴 게이트 · 확인 전용 라운드 · diff 서킷브레이커 (T-0593).

라운드 상한(T-0457)과 wave 예산(T-0583)은 "몇 번 전송했나"만 셌고 "닫히고 있나"는 묻지 않았다.
실측 두 형상이 그 공백이다 — 리뷰 12라운드(연장 승인 반복)와 must_fix 3→2→2 평탄. 이 파일은 세
축을 단언한다:

  ① 수렴-형상 게이트 — 라운드 장부의 `rounds[].must_fix` 추이로 판정한다(LLM 판단 0).
     상한 도달(local.conf `review_rounds_max`·기본 3)·직전 대비 증가(조기 차단)면 rc 4 로 막고,
     감소 수렴은 그대로 통과시킨다. 출구는 재설계·티켓 분할뿐이다(`--ack-rounds` 폐지).
  ② 확인 전용 라운드(`--confirm-fix`) — 수렴 축의 유일한 예외. 게이트당 1회이고 장부가 소유하며,
     프롬프트에 "신규 발견은 재설계 신호" 헌장을 싣는다.
  ③ diff 서킷브레이커 — 티켓 estimate 별 diff 총량(추가+삭제) 상한. 측정 폭은 리뷰가 보는 폭과
     같은 단계 표를 쓰고(`_diff_bases`), 초과면 리뷰어 호출 전에 rc 1 로 막는다.

hermetic: REPO 를 tmp 로 monkeypatch 해 장부를 격리하고(`test_external_review.py` 동형),
extract_diff·run_review·local_config 를 주입해 실제 git/추가 리뷰어 없이(외부 전송 0) 분기를
단언한다. diff 측정만 실 git 을 쓰는 테스트는 tmp 저장소를 직접 만든다.
"""
from __future__ import annotations

import datetime
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"


def _load(name: str = "external_review"):
    spec = importlib.util.spec_from_file_location(f"convergence_{name}", TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def external():
    return _load("external_review")


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


def _wire(external, monkeypatch, tmp_path, *, series: list[int], conf=None,
          stub_diff_cap: bool = True):
    """main() 을 tmp REPO 로 격리 배선하고 라운드별 must_fix 를 주입한다.

    `stub_diff_cap` 은 서킷브레이커 축을 중립화한다 — 수렴 축 테스트가 그 축만 태우게. 서킷
    전용 절은 False 로 배선해 실제 진입 검사를 태운다."""
    monkeypatch.setattr(external, "REPO", tmp_path)
    monkeypatch.setattr(
        external, "local_config",
        lambda repo=None: dict(conf) if conf is not None
        else {"additional_reviewer_enabled": "true"})
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
    if stub_diff_cap:
        monkeypatch.setattr(external, "_diff_cap_refusal", lambda *a, **k: None)
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
    """미설정 → 3(사용자 확정 값) · local.conf `review_rounds_max` 가 상한을 바꾼다."""
    assert external._review_rounds_max({}) == external.DEFAULT_REVIEW_ROUNDS_MAX == 3
    assert external._review_rounds_max({"review_rounds_max": "5"}) == 5


def test_rounds_max_garbage_and_negative_fall_back(external):
    """비정수·음수는 기본값으로 fail-soft (다른 예산 노브와 같은 규칙)."""
    assert external._review_rounds_max({"review_rounds_max": "x"}) == 3
    assert external._review_rounds_max({"review_rounds_max": "-2"}) == 3


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


def test_flat_series_is_blocked_at_the_fourth_round(external, monkeypatch, tmp_path, capsys):
    """must_fix 3→2→2 (비감소)는 4라운드째 실행 전에 막힌다 — 실측 형상 (DoD)."""
    reviewer = _wire(external, monkeypatch, tmp_path, series=[3, 2, 2])
    argv = ["--gate", "T-0593", "--paths", "x.py"]
    for _ in range(3):
        assert external.main(argv) == 1          # 반려 rc (라운드 자체는 정상 진행)
    assert reviewer.calls == 3
    capsys.readouterr()

    assert external.main(argv) == external.EXIT_ROUND_LIMIT_EXCEEDED
    assert reviewer.calls == 3                    # 리뷰어 미호출 (외부 전송 0)
    err = capsys.readouterr().err
    assert "수렴 게이트 차단" in err
    assert "3 → 2 → 2" in err                     # 판정 근거를 그대로 보여준다
    assert "재설계" in err and "분할" in err        # 유일한 출구
    assert "--confirm-fix" in err                 # 해소 확인 전용 예외
    assert _ledger(tmp_path)["T-0593"]["count"] == 3   # 거부는 장부를 늘리지 않는다


def test_increasing_series_is_blocked_early(external, monkeypatch, tmp_path, capsys):
    """must_fix 3→5 는 상한(3) 전인 3라운드째에 막힌다 (발산 조기 차단·DoD)."""
    reviewer = _wire(external, monkeypatch, tmp_path, series=[3, 5])
    argv = ["--gate", "T-0594", "--paths", "x.py"]
    for _ in range(2):
        assert external.main(argv) == 1
    capsys.readouterr()

    assert external.main(argv) == external.EXIT_ROUND_LIMIT_EXCEEDED
    assert reviewer.calls == 2
    err = capsys.readouterr().err
    assert "발산" in err and "조기 차단" in err


def test_converging_series_runs_all_rounds(external, monkeypatch, tmp_path):
    """must_fix 3→1→0 은 세 라운드 모두 통과한다 (감소 수렴 무영향·DoD)."""
    reviewer = _wire(external, monkeypatch, tmp_path, series=[3, 1, 0])
    argv = ["--gate", "T-0595", "--paths", "x.py"]
    assert [external.main(argv) for _ in range(3)] == [1, 1, 0]
    assert reviewer.calls == 3
    entry = _ledger(tmp_path)["T-0595"]
    assert [row["must_fix"] for row in entry["rounds"]] == [3, 1, 0]


def test_rounds_max_knob_changes_the_cap(external, monkeypatch, tmp_path):
    """`review_rounds_max=2` → 3라운드째 차단 (노브 반영)."""
    conf = {"additional_reviewer_enabled": "true", "review_rounds_max": "2"}
    reviewer = _wire(external, monkeypatch, tmp_path, series=[2, 2], conf=conf)
    argv = ["--gate", "T-0596", "--paths", "x.py"]
    for _ in range(2):
        assert external.main(argv) == 1
    assert external.main(argv) == external.EXIT_ROUND_LIMIT_EXCEEDED
    assert reviewer.calls == 2


def test_gate_without_a_ledger_is_untouched(external, monkeypatch, tmp_path):
    """라운드 장부가 없는 티켓의 첫 라운드는 그대로 진행된다 (정상 경로 무영향·DoD)."""
    reviewer = _wire(external, monkeypatch, tmp_path, series=[0])
    assert _ledger(tmp_path) == {}
    assert external.main(["--gate", "T-0597", "--paths", "x.py"]) == 0
    assert reviewer.calls == 1


# ══ ② 확인 전용 라운드 (`--confirm-fix`) ════════════════════════════════════


def test_confirm_fix_opens_exactly_one_round_per_gate(
        external, monkeypatch, tmp_path, capsys):
    """차단된 게이트를 1회만 연다 — 장부에 기록되고 2회째는 거부된다 (DoD)."""
    reviewer = _wire(external, monkeypatch, tmp_path, series=[2, 2, 2, 0, 0])
    argv = ["--gate", "T-0598", "--paths", "x.py"]
    for _ in range(3):
        assert external.main(argv) == 1
    assert external.main(argv) == external.EXIT_ROUND_LIMIT_EXCEEDED
    capsys.readouterr()

    assert external.main(argv + ["--confirm-fix"]) == 0       # 예외 1회
    assert reviewer.calls == 4
    assert "확인 전용 라운드" in capsys.readouterr().err
    assert _ledger(tmp_path)["T-0598"]["confirm_fix"] == 1

    assert external.main(argv + ["--confirm-fix"]) == external.EXIT_ROUND_LIMIT_EXCEEDED
    assert reviewer.calls == 4                                # 2회째는 전송 없음
    err = capsys.readouterr().err
    assert "게이트당 1회" in err and "사용 1회" in err
    assert "재설계" in err and "분할" in err
    assert _ledger(tmp_path)["T-0598"]["confirm_fix"] == 1     # 거부는 quota 를 더 쓰지 않는다


def test_confirm_fix_quota_is_refunded_when_nothing_was_sent(
        external, monkeypatch, tmp_path):
    """스폰 전 실패(전송 0·과금 0)는 quota 도 되돌린다 — 라운드 count·wave 와 같은 조건.

    안 되돌리면 설치/PATH 문제 한 번으로 게이트당 1회뿐인 유일한 처방이 소멸한다("전송 0 실행은
    예산을 먹지 않는다" 불변식의 세 번째 축)."""
    _wire(external, monkeypatch, tmp_path, series=[2, 2, 2])
    argv = ["--gate", "T-0603", "--paths", "x.py"]
    for _ in range(3):
        assert external.main(argv) == 1
    assert external.main(argv) == external.EXIT_ROUND_LIMIT_EXCEEDED

    unstarted = {
        "reviewer": "x", "ok": False, "output": "[리뷰어 명령 없음]",
        "answer": "[리뷰어 명령 없음]",
        "verdict": {"has_must_fix": False, "has_pass": False}, "file": None,
        "failed": True, "started": False, "any_must_fix": False, "all_pass": False,
    }
    monkeypatch.setattr(external, "run_review", lambda *a, **k: dict(unstarted))
    assert external.main(argv + ["--confirm-fix"]) == 1
    entry = _ledger(tmp_path)["T-0603"]
    assert entry["count"] == 3            # 라운드 count 환불
    assert entry["confirm_fix"] == 0      # quota 도 함께 환불

    # 처방이 살아 있다 — 다음 시도가 실제로 확인 전용 라운드를 쓴다.
    reviewer = _Reviewer([0])
    monkeypatch.setattr(external, "run_review", reviewer)
    assert external.main(argv + ["--confirm-fix"]) == 0
    assert reviewer.calls == 1
    assert _ledger(tmp_path)["T-0603"]["confirm_fix"] == 1


def test_confirm_fix_quota_is_refunded_when_isolation_fails(
        external, monkeypatch, tmp_path):
    """예약 뒤·스폰 전 중단(격리 실패)도 같은 환불 규칙을 탄다 (환불 소유가 한 seam)."""
    _wire(external, monkeypatch, tmp_path, series=[2, 2, 2])
    argv = ["--gate", "T-0604", "--paths", "x.py"]
    for _ in range(3):
        assert external.main(argv) == 1

    def _fail(*a, **k):
        raise external.ReviewerWorkspaceError("거울 생성 실패")

    monkeypatch.setattr(external, "create_reviewer_workspace", _fail)
    assert external.main(argv + ["--confirm-fix"]) == 1
    entry = _ledger(tmp_path)["T-0604"]
    assert entry["count"] == 3 and entry["confirm_fix"] == 0


def test_confirm_fix_quota_is_per_gate(external, monkeypatch, tmp_path):
    """quota 는 게이트별이다 — 한 티켓이 썼다고 다른 티켓이 잠기지 않는다.

    두 게이트 모두 **반려 라운드를 먼저 쓴다** — 확인 전용 라운드는 확인할 지적이 있는 게이트
    에서만 열리므로(T-0602 ①), 자격을 갖춘 상태에서 quota 의 게이트별 독립성을 본다."""
    _wire(external, monkeypatch, tmp_path, series=[1, 0, 1, 0])
    for gate in ("T-0599", "T-0600"):
        assert external.main(["--gate", gate, "--paths", "x.py"]) == 1     # 반려 1라운드
        assert external.main(["--gate", gate, "--paths", "x.py", "--confirm-fix"]) == 0
    ledger = _ledger(tmp_path)
    assert ledger["T-0599"]["confirm_fix"] == ledger["T-0600"]["confirm_fix"] == 1


def test_confirm_fix_prompt_carries_the_charter(external, monkeypatch):
    """확인 전용 라운드 프롬프트는 임무를 좁힌다 — 신규 발견은 '재설계 신호'로 보고."""
    monkeypatch.setattr(external, "_load_review_context", lambda: "맥락")
    prompt = external.build_prompt(diff="x", gate="T-0593", confirm_fix=True)
    assert "확인 전용" in prompt
    assert "재설계 신호" in prompt
    assert "must-fix 로 올리지 말고" in prompt
    # 일반 라운드에는 헌장이 없다 (예외가 기본이 되지 않게).
    assert "확인 전용" not in external.build_prompt(diff="x", gate="T-0593")


# ══ ②-a 확인 전용 라운드의 **자격·근거** (T-0602 ①) ══════════════════════════
# codex R2 지적: 리뷰어는 매 라운드 fresh 세션이고 장부에는 must_fix **개수**만 남았다 — 그래서
# ① 첫 라운드에서도 `--confirm-fix` 를 쓸 수 있고(확인할 지적이 없는데 통과 판정만 나온다)
# ② 열리더라도 프롬프트가 직전 지적을 들고 가지 않아 무엇을 확인하는지 모른다.
# 아래 절은 두 우회를 **그 형상 그대로** 재현하고 차단을 단언한다.


class _PromptCapture:
    """run_review 대역 — 프롬프트를 보관하면서 지정한 must_fix 수를 돌려준다."""

    def __init__(self, series: list[int]):
        self._series = list(series)
        self.prompts: list[str] = []

    def __call__(self, *args, **kwargs):
        must_fix = self._series[min(len(self.prompts), len(self._series) - 1)]
        self.prompts.append(kwargs.get("prompt") or (args[0] if args else ""))
        return dict(_result(must_fix))


def test_confirm_fix_on_a_first_round_gate_is_refused(
        external, monkeypatch, tmp_path, capsys):
    """장부가 없는 **첫 라운드**의 `--confirm-fix` 는 전송 전에 거부된다 (근거 없는 통과 차단).

    재현: 반려 라운드가 하나도 없는 게이트에 확인 전용 라운드를 요청한다(codex R2 지적 형상)."""
    reviewer = _wire(external, monkeypatch, tmp_path, series=[0])

    rc = external.main(["--gate", "T-0602", "--paths", "x.py", "--confirm-fix"])

    assert rc == external.EXIT_ROUND_LIMIT_EXCEEDED
    assert reviewer.calls == 0                      # 외부 전송 0
    err = capsys.readouterr().err
    assert "최신 완료 라운드가 반려인 게이트" in err
    assert "라운드 기록 없음" in err                  # 무엇을 보고 막았는지 그대로
    assert "첫 리뷰는" in err                        # 처방 1줄
    assert _ledger(tmp_path) == {}                  # 거부는 장부를 만들지 않는다


def test_confirm_fix_on_an_all_pass_gate_is_refused(
        external, monkeypatch, tmp_path, capsys):
    """라운드는 있으나 **전건 통과**인 게이트도 자격 미달이다 — 확인할 지적이 없다."""
    reviewer = _wire(external, monkeypatch, tmp_path, series=[0, 0])
    argv = ["--gate", "T-0602b", "--paths", "x.py"]
    assert external.main(argv) == 0
    capsys.readouterr()

    assert external.main(argv + ["--confirm-fix"]) == external.EXIT_ROUND_LIMIT_EXCEEDED
    assert reviewer.calls == 1                      # 확인 전용 라운드는 나가지 않았다
    assert "최신 판정은 0(통과)" in capsys.readouterr().err
    assert _ledger(tmp_path)["T-0602b"]["confirm_fix"] == 0   # quota 도 안 쓴다


# ── 자격은 **최신** 완료 판정이다 (T-0603 ②) ────────────────────────────────
# codex R3 지적: 자격 판정이 '과거의 마지막 반려'를 찾는다 — 반려 → 통과로 이미 닫힌 게이트도
# 옛 지적을 근거로 영원히 자격을 갖는다(수렴 축의 1회 예외가 상시 예외가 되고 과금이 따라온다).


def test_confirm_fix_on_a_gate_closed_by_a_pass_is_refused(
        external, monkeypatch, tmp_path, capsys):
    """반려 → 통과로 **이미 닫힌** 게이트의 `--confirm-fix` 는 전송 전에 거부된다 (DoD).

    재현: 1라운드 반려 · 2라운드 통과로 수렴을 마친 게이트에 확인 전용 라운드를 요청한다."""
    reviewer = _wire(external, monkeypatch, tmp_path, series=[1, 0])
    argv = ["--gate", "T-0603a", "--paths", "x.py"]
    assert external.main(argv) == 1                 # 1R 반려
    assert external.main(argv) == 0                 # 2R 통과 — 사이클 마감
    capsys.readouterr()

    assert external.main(argv + ["--confirm-fix"]) == external.EXIT_ROUND_LIMIT_EXCEEDED
    assert reviewer.calls == 2                      # 외부 전송 0 (과금 라운드가 열리지 않는다)
    err = capsys.readouterr().err
    assert "최신 완료 라운드가 반려인 게이트" in err
    assert "최신 판정은 0(통과)" in err
    assert _ledger(tmp_path)["T-0603a"]["confirm_fix"] == 0   # quota 미소비


def test_confirm_fix_opens_for_the_latest_rejection_after_a_pass(
        external, monkeypatch, tmp_path):
    """정상 경로 무변경 — 통과 뒤 다시 반려로 끝났으면 열리고, 근거는 **그 최신 반려**다."""
    capture = _PromptCapture([3, 0, 2, 0])
    _wire(external, monkeypatch, tmp_path, series=[3])
    monkeypatch.setattr(external, "run_review", capture)
    argv = ["--gate", "T-0603b", "--paths", "x.py"]
    assert external.main(argv) == 1                 # 1R 반려 3건
    assert external.main(argv) == 0                 # 2R 통과
    assert external.main(argv) == 1                 # 3R 반려 2건 — 최신 반려

    assert external.main(argv + ["--confirm-fix"]) == 0
    prompt = capture.prompts[-1]
    assert "결함 1" in prompt and "결함 2" in prompt
    assert "결함 3" not in prompt, "1라운드(3건)가 아니라 최신 반려(2건)가 근거여야 한다"


def test_latest_round_helpers_read_reservation_order(external):
    """자격 판정 입력은 **예약 순번**의 마지막이다 — append 순서로 읽으면 최신이 뒤바뀐다."""
    entry = external._gate_entry({"g": {"rounds": [
        {"sequence": 2, "verdict": 0, "must_fix": 0},     # 나중 순번이 먼저 append 됨
        {"sequence": 1, "verdict": 1, "must_fix": 2},
    ]}}, "g")
    assert external._latest_round_outcome(entry)["sequence"] == 2
    assert external._latest_verdict_label(entry) == "0(통과)"
    assert external._confirm_fix_evidence(entry) is None       # 최신이 통과 = 자격 없음


def test_unknown_latest_verdict_does_not_open_the_exception(external):
    """최신 라운드 판정이 미상(기록 없음·손상)이면 자격을 세우지 않는다 (과금 축 보수 방향)."""
    entry = external._gate_entry({"g": {"rounds": [
        {"sequence": 1, "verdict": 1, "must_fix": 2},
        {"sequence": 2, "verdict": None, "must_fix": None},
    ]}}, "g")
    assert external._confirm_fix_evidence(entry) is None
    assert external._latest_verdict_label(entry) == "미상"


def test_confirm_fix_prompt_carries_the_previous_must_fix_items(
        external, monkeypatch, tmp_path):
    """확인 전용 라운드 프롬프트에 **직전 반려 항목 텍스트**가 실린다 (fresh 세션 근거 주입)."""
    capture = _PromptCapture([2, 0])
    _wire(external, monkeypatch, tmp_path, series=[2])
    monkeypatch.setattr(external, "run_review", capture)
    argv = ["--gate", "T-0602c", "--paths", "x.py"]
    assert external.main(argv) == 1                 # 반려 1라운드 (근거 생성)

    assert external.main(argv + ["--confirm-fix"]) == 0
    prompt = capture.prompts[-1]
    assert "직전 라운드 must-fix" in prompt
    assert "결함 1" in prompt and "결함 2" in prompt  # 항목 **텍스트** 그대로
    assert "확인 전용" in prompt                     # 헌장과 같은 프롬프트에 붙는다
    # 일반 라운드 프롬프트에는 근거 블록이 없다 (예외 전용).
    assert "직전 라운드 must-fix" not in capture.prompts[0]


def test_recorded_must_fix_texts_survive_in_the_reservation_record(
        external, monkeypatch, tmp_path):
    """항목 텍스트의 저장 지점은 예약 레코드다 — 정규화(`_gate_entry`)에서 살아남는다."""
    _wire(external, monkeypatch, tmp_path, series=[2])
    assert external.main(["--gate", "T-0602d", "--paths", "x.py"]) == 1

    entry = _ledger(tmp_path)["T-0602d"]
    assert [row.get("must_fix_items") for row in entry["records"]] == [["결함 1", "결함 2"]]
    # 개수 축(rounds[].must_fix)과 텍스트 축이 같은 파서를 쓴다 — 갈리면 근거가 거짓이 된다.
    assert entry["rounds"][0]["must_fix"] == 2
    assert external._gate_entry({"g": entry}, "g")["records"][0]["must_fix_items"] == [
        "결함 1", "결함 2"]


def test_confirm_fix_evidence_comes_from_the_eligibility_snapshot(
        external, monkeypatch, tmp_path):
    """자격 판정과 프롬프트 근거는 **같은 스냅샷**에서 나온다 (두 번 읽으면 갈린다).

    재현: 프롬프트 조립 시점의 read 는 반려 *이전* 장부를, 예약 임계 구역의 read 는 반려가 실린
    장부를 본다(동시 라운드가 그 사이에 마감한 형상). 두 read 를 그대로 두면 "자격 통과 · 근거
    블록 없음" 라운드가 난다 — 근거 없는 통과 판정 채널이 다시 열리는 것과 같다."""
    capture = _PromptCapture([0])
    _wire(external, monkeypatch, tmp_path, series=[0])
    monkeypatch.setattr(external, "run_review", capture)
    rejected = {"T-0602i": {
        "count": 1, "acked_through": 0, "sequence": 1,
        "records": [{"id": "r1", "sequence": 1, "started_at": "2026-08-09T00:00:00+00:00",
                     "finished_at": "2026-08-09T00:00:01+00:00", "verdict": True,
                     "must_fix_items": ["동시 라운드가 남긴 지적"]}],
        "rounds": [{"ts": "2026-08-09T00:00:01+00:00", "id": "r1", "sequence": 1,
                    "verdict": 1, "must_fix": 1, "suggestions": None}],
    }}
    before_rejection = {"T-0602i": {"count": 0, "acked_through": 0, "sequence": 0,
                                    "records": [], "rounds": []}}
    reads = {"n": 0}

    def _load_round_ledger():
        reads["n"] += 1
        # 첫 read(프롬프트 조립)만 반려 이전 상태 — 이후 read(자격·마감)는 반려가 실린 장부.
        import copy
        return copy.deepcopy(before_rejection if reads["n"] == 1 else rejected)

    monkeypatch.setattr(external, "_load_round_ledger", _load_round_ledger)

    assert external.main(["--gate", "T-0602i", "--paths", "x.py", "--confirm-fix"]) == 0
    assert reads["n"] >= 2, "스냅샷이 하나뿐이면 이 재현이 성립하지 않는다"
    prompt = capture.prompts[-1]
    assert "직전 라운드 must-fix" in prompt
    assert "동시 라운드가 남긴 지적" in prompt, (
        "자격은 통과했는데 프롬프트에 근거가 없다 — 두 read 가 갈렸다")


def test_legacy_round_without_texts_falls_back_to_the_count(
        external, monkeypatch, tmp_path):
    """텍스트 보관분이 없는 구세대 반려 라운드는 **건수 + 재구성 지시**로 떨어진다.

    자격은 그대로 열린다 — '근거가 빈 통과'보다 '무엇을 확인할지 재구성하라'가 낫다."""
    capture = _PromptCapture([0])
    _wire(external, monkeypatch, tmp_path, series=[0])
    monkeypatch.setattr(external, "run_review", capture)
    ledger = tmp_path / ".project_manager" / ".local" / "review_rounds.json"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(json.dumps({"T-0602e": {
        "count": 1, "acked_through": 0, "sequence": 1,
        "records": [{"id": "r1", "sequence": 1, "finished_at": "2026-08-09T00:00:00+00:00",
                     "verdict": True}],
        "rounds": [{"ts": "2026-08-09T00:00:00+00:00", "id": "r1", "sequence": 1,
                    "verdict": 1, "must_fix": 3, "suggestions": None}],
    }}), encoding="utf-8")

    assert external.main(["--gate", "T-0602e", "--paths", "x.py", "--confirm-fix"]) == 0
    prompt = capture.prompts[-1]
    assert "직전 라운드 must-fix" in prompt
    assert "must-fix 건수: 3" in prompt and "재구성" in prompt


# ══ ②-b 수렴 상한의 **진행 중 예약** (T-0602 ②) ══════════════════════════════
# codex R2 지적: 상한 판정이 완료 `rounds` 만 세고 예약된 실행을 무시한다 — 2완료 상태에서 두
# 실행이 연속 예약되면 둘 다 통과해 상한 3 인데 4라운드가 전송된다. 미마감 예약을 상한에 더해
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
        "count": completed + inflight, "acked_through": 0,
        "sequence": completed + inflight, "records": records, "rounds": rounds,
    }}), encoding="utf-8")


def test_inflight_reservation_counts_toward_the_convergence_cap(external, tmp_path):
    """2완료 + 1예약 = 상한 3 도달 — 완료분만 세면 이 형상이 통과해 4전송이 난다 (순수 판정)."""
    _seed_rounds(tmp_path, "g", completed=2, inflight=1)
    ledger = json.loads(
        (tmp_path / ".project_manager" / ".local" / "review_rounds.json").read_text(
            encoding="utf-8"))
    entry = external._gate_entry(ledger, "g")
    assert external._inflight_reservations(entry) == 1
    assert external._convergence_round_usage(entry) == (2, 1)
    assert external._convergence_refusal(entry, 3) == external.CONVERGENCE_CAP_UNRESOLVED


def test_second_concurrent_reservation_is_refused_before_sending(
        external, monkeypatch, tmp_path, capsys):
    """재현: 2완료 + 1예약(동시 실행 A) 상태에서 실행 B 는 전송 전에 막힌다 (상한 3·4전송 창)."""
    reviewer = _wire(external, monkeypatch, tmp_path, series=[2])
    _seed_rounds(tmp_path, "T-0602f", completed=2, inflight=1)

    rc = external.main(["--gate", "T-0602f", "--paths", "x.py"])

    assert rc == external.EXIT_ROUND_LIMIT_EXCEEDED
    assert reviewer.calls == 0                       # 외부 전송 0
    err = capsys.readouterr().err
    assert "수렴 게이트 차단" in err
    assert "진행 중 예약 1" in err                    # 판정 근거를 그대로 보여준다
    assert _ledger(tmp_path)["T-0602f"]["count"] == 3   # 거부는 예약을 늘리지 않는다


def test_completed_rounds_below_the_cap_still_run(external, monkeypatch, tmp_path):
    """정상 경로 무변경 — 진행 중 예약이 없으면 2완료 상태의 3라운드째는 그대로 나간다."""
    reviewer = _wire(external, monkeypatch, tmp_path, series=[1])
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
    assert external._inflight_reservations(fresh, wall_timeout_sec=3600) == 1
    stale = _entry(3600 + 60)
    assert external._inflight_reservations(stale, wall_timeout_sec=3600) == 0
    # 백스톱을 안 넘기면 종전대로 전부 센다(회수는 시간 조건 하나뿐).
    assert external._inflight_reservations(stale) == 1


def test_timed_out_reservation_does_not_lock_the_convergence_axis(
        external, monkeypatch, tmp_path):
    """같은 2완료+1예약 형상이라도 그 예약이 **백스톱을 넘겼으면** 라운드는 나간다 (회수 경로).

    신선 예약이면 같은 장부가 차단된다(`test_second_concurrent_reservation_is_refused_before_sending`)
    — 두 테스트가 회수 기준이 *시간* 하나임을 함께 못박는다. 회수가 없으면 kill 한 번이 상한을
    영구 잠식한다(연장 승인이 폐지된 축이라 되돌릴 손잡이가 없다)."""
    reviewer = _wire(external, monkeypatch, tmp_path, series=[1])
    _seed_rounds(tmp_path, "T-0602j", completed=2, inflight=1,
                 inflight_age_sec=3600 * 24)             # 하루 전 예약 = 실행 중일 수 없다

    assert external.main(["--gate", "T-0602j", "--paths", "x.py"]) == 1
    assert reviewer.calls == 1, "회수되지 않은 잔재가 라운드를 잠갔다"


def test_refunded_reservation_does_not_hold_the_cap(external, monkeypatch, tmp_path):
    """전송이 없던 예약은 환불로 레코드째 사라져 상한을 잡아 두지 않는다 (fail-closed 오차단 방지)."""
    _wire(external, monkeypatch, tmp_path, series=[1])
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
    assert external._inflight_reservations(external._gate_entry({"g": entry}, "g")) == 0
    reviewer = _Reviewer([1])
    monkeypatch.setattr(external, "run_review", reviewer)
    assert external.main(["--gate", "T-0602h", "--paths", "x.py"]) == 1   # 여전히 열려 있다
    assert reviewer.calls == 1


# ══ ③ diff 서킷브레이커 ═════════════════════════════════════════════════════


@pytest.mark.parametrize("estimate, cap", [
    ("small", 300), ("medium", 1000), ("large", 2500),
])
def test_diff_cap_defaults_per_estimate(external, estimate, cap):
    """3구간 기본 상한 — 사용자 확정 값(2026-08-08)."""
    assert external._diff_cap({}, estimate) == cap
    assert external.DEFAULT_DIFF_CAPS[estimate] == cap


def test_diff_cap_unknown_estimate_is_guard_off(external):
    """모르는/빈 estimate 는 상한을 지어내지 않는다 (가드 off)."""
    assert external._diff_cap({}, None) is None
    assert external._diff_cap({}, "") is None
    assert external._diff_cap({}, "epic") is None


def test_diff_cap_conf_override_and_failsoft(external):
    """`diff_cap.<estimate>` 로 덮어쓰고, 비정수·음수는 엔진 기본값으로 fail-soft."""
    assert external._diff_cap({"diff_cap.small": "50"}, "small") == 50
    assert external._diff_cap({"diff_cap.medium": "x"}, "medium") == 1000
    assert external._diff_cap({"diff_cap.large": "-1"}, "large") == 2500


@pytest.mark.parametrize("estimate, total, blocked", [
    ("small", 300, False), ("small", 301, True),
    ("medium", 1000, False), ("medium", 1001, True),
    ("large", 2500, False), ("large", 2501, True),
])
def test_diff_cap_boundaries(external, estimate, total, blocked):
    """경계는 '상한 초과'다 — 상한과 같은 값은 통과한다 (off-by-one 고정)."""
    cap = external._diff_cap({}, estimate)
    block = external.diff_cap_block(
        total, cap, ticket="T-0001", estimate=estimate, scope=["src/"],
    )
    assert (block is not None) is blocked
    if blocked:
        assert "분할" in block and "재설계" in block
        assert f"{total}줄" in block and f"{cap}줄" in block


def test_numstat_sum_ignores_binary_and_junk(external):
    """`--numstat` 합산은 추가+삭제만 센다 — 바이너리(`-`)·깨진 줄은 제외."""
    text = "10\t5\tsrc/a.py\n-\t-\tassets/logo.png\ngarbage\n3\t0\tsrc/b.py\n"
    assert external._sum_numstat(text) == 18


# ── 기계 mirror 제외 (T-0601 ①) ────────────────────────────────────────────
# `templates/<타깃>/.project_manager/` 는 pm_update 가 기계로 내보내는 엔진 사본이다. 손작업과
# 같은 가중으로 합산하면 구현 스코프가 출하 타깃 수만큼 부풀어(실측 estimate 교정 2회) 분할이
# 필요 없는 티켓을 서킷이 막는다. mirror 정합은 drift-0 가드가 따로 지킨다.


@pytest.mark.parametrize("path, mirrored", [
    ("templates/claude_code/.project_manager/tools/board.py", True),
    ("templates/codex/.project_manager/tools/pm_delegate.py", True),
    ("./templates/opencode/.project_manager/local.conf", True),          # `./` 표기
    (r"templates\opencode\.project_manager\tools\board.py", True),       # Windows 표기
    (".project_manager/tools/board.py", False),                          # 엔진 원본(손작업)
    ("templates/claude_code/CLAUDE.md", False),                          # 어댑터층(손작업)
    ("templates/claude_code/.claude/agents/architect.md", False),        # 어댑터층(손작업)
    ("tests/test_board_lint.py", False),
])
def test_machine_mirror_predicate_is_the_single_exclusion_rule(external, path, mirrored):
    """제외 판정은 경로 문자열 하나다 — 두 소비처(리뷰·완료)가 같은 술어를 쓴다(사본 0)."""
    assert external.is_machine_mirror_path(path) is mirrored


def test_exclusion_is_a_subtree_including_hand_edited_files(external):
    """제외는 **subtree 단위**다 — 그 안의 manifest 밖 손편집 파일도 함께 빠진다(알고 고른 경계).

    이 경계를 문서와 술어가 같은 말로 해야 다음 사람이 "mirror 만 빠진다"로 오독하지 않는다.
    오차 방향은 측정 축소(=가드 약화)뿐이라 정당한 작업을 오차단하지 않는다."""
    hand_edited = "templates/codex/.project_manager/engine.manifest"
    assert external.is_machine_mirror_path(hand_edited) is True
    doc = external.is_machine_mirror_path.__doc__
    assert "전부는 아니다" in doc and "측정 축소" in doc


def test_mirror_rows_do_not_inflate_the_measured_total(external):
    """mirror 행은 합산에서 빠지고 손작업 행만 남는다 (rename 표기도 목적지로 판정)."""
    text = (
        "40\t10\t.project_manager/tools/board.py\n"                       # 손작업 50
        "40\t10\ttemplates/claude_code/.project_manager/tools/board.py\n"  # 기계 mirror
        "40\t10\ttemplates/codex/.project_manager/tools/board.py\n"        # 기계 mirror
        "40\t10\ttemplates/opencode/.project_manager/tools/board.py\n"     # 기계 mirror
        "3\t0\ttemplates/claude_code/CLAUDE.md\n"                          # 어댑터층 손작업 3
        "2\t1\t{templates/codex => templates/codex}/.project_manager/tools/x.py\n"
    )
    assert external._sum_numstat(text) == 53


def test_broad_template_declaration_still_excludes_the_mirror(external, tmp_path):
    """선언이 넓은 접두(`templates/`)여도 제외가 성립한다 — 판정이 선언 형태에 안 좌우된다.

    이게 경로 목록 필터가 아니라 **측정 출력 필터**여야 하는 이유다: 이 wave 의 실 티켓들이
    `templates/` 한 줄로 선언한다."""
    root = _git_repo(tmp_path)
    hand = root / "engine.py"
    hand.write_text("".join(f"h{i}\n" for i in range(7)), encoding="utf-8")
    mirror = root / "templates" / "codex" / ".project_manager" / "tools" / "engine.py"
    mirror.parent.mkdir(parents=True)
    mirror.write_text("".join(f"m{i}\n" for i in range(500)), encoding="utf-8")
    adapter = root / "templates" / "codex" / "AGENTS.md"
    adapter.write_text("a\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, capture_output=True)

    total = external.diff_line_total(root, "HEAD", ["engine.py", "templates/"])
    assert total == 7 + 1, "기계 mirror 500줄이 손작업 스코프에 합산됐다"


def test_cap_block_states_the_measured_scope_meaning(external):
    """차단 안내가 측정 의미를 스스로 말한다 — "왜 내 diff 보다 적나"를 문구가 답한다."""
    block = external.diff_cap_block(
        301, 300, ticket="T-0601", estimate="small", scope=["templates/"],
    )
    assert external.MEASURED_SCOPE_NOTE in block
    assert "기계 mirror 제외" in block


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


def test_diff_line_total_reuses_the_review_stage_table(external, tmp_path):
    """측정 폭은 리뷰가 보는 폭과 같다 — 'HEAD' 단계는 스테이징+언스테이징 둘 다 센다."""
    root = _git_repo(tmp_path)
    (root / "seed.py").write_text("seed\n" + "".join(f"a{i}\n" for i in range(9)),
                                  encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "seed.py"], check=True,
                   capture_output=True)
    (root / "seed.py").write_text("seed\n" + "".join(f"a{i}\n" for i in range(12)),
                                  encoding="utf-8")
    assert external.diff_line_total(root, "HEAD", ["seed.py"]) == 12
    assert external.diff_line_total(root, "HEAD", ["없는경로.py"]) == 0


def test_main_blocks_an_oversized_ticket_before_the_reviewer(
        external, monkeypatch, tmp_path, capsys):
    """상한 초과 티켓은 추가 리뷰어 호출 전에 rc 1 로 막힌다 (진입 검사·DoD)."""
    reviewer = _wire(external, monkeypatch, tmp_path, series=[0], stub_diff_cap=False)
    monkeypatch.setattr(external, "parse_ticket_estimate", lambda *a, **k: "small")
    monkeypatch.setattr(external, "diff_line_total", lambda *a, **k: 301)

    assert external.main(["--gate", "T-0601", "--paths", "x.py"]) == 1
    assert reviewer.calls == 0                       # 외부 전송 0
    assert _ledger(tmp_path) == {}                   # 라운드도 쓰지 않는다
    err = capsys.readouterr().err
    assert "서킷브레이커 차단" in err and "301줄" in err


def test_main_lets_a_ticket_within_the_cap_through(external, monkeypatch, tmp_path):
    """상한 이내면 종전대로 진행한다 (정상 경로 무영향·DoD)."""
    reviewer = _wire(external, monkeypatch, tmp_path, series=[0], stub_diff_cap=False)
    monkeypatch.setattr(external, "parse_ticket_estimate", lambda *a, **k: "small")
    monkeypatch.setattr(external, "diff_line_total", lambda *a, **k: 300)

    assert external.main(["--gate", "T-0602", "--paths", "x.py"]) == 0
    assert reviewer.calls == 1


def test_dry_run_is_not_blocked_by_the_circuit_breaker(
        external, monkeypatch, tmp_path, capsys):
    """미리보기는 전송·과금 0 이라 서킷을 지나지 않는다 — 분할 판단에 필요한 diff 확인 채널.

    막으면 "왜 막혔는지 보려고 dry-run 하는" 동선이 함께 닫힌다(목적 밖 차단)."""
    reviewer = _wire(external, monkeypatch, tmp_path, series=[0], stub_diff_cap=False)
    monkeypatch.setattr(external, "parse_ticket_estimate", lambda *a, **k: "small")
    monkeypatch.setattr(external, "diff_line_total", lambda *a, **k: 99999)

    assert external.main(["--gate", "T-0605", "--paths", "x.py", "--dry-run"]) == 0
    assert reviewer.calls == 0                       # 미리보기는 전송하지 않는다
    out, err = capsys.readouterr()
    assert "서킷브레이커 차단" not in err
    assert "[dry-run] 프롬프트 미리보기" in out


def test_free_form_gate_name_keeps_the_guard_off(external, monkeypatch, tmp_path):
    """티켓이 아닌 자유 문자열 게이트는 estimate 가 없어 가드가 조용히 off 다."""
    reviewer = _wire(external, monkeypatch, tmp_path, series=[0], stub_diff_cap=False)
    monkeypatch.setattr(external, "diff_line_total", lambda *a, **k: 99999)

    assert external.main(["--gate", "wave4-b1", "--paths", "x.py"]) == 0
    assert reviewer.calls == 1


def test_ticket_estimate_is_read_from_frontmatter(external, tmp_path, monkeypatch):
    """estimate 는 board ticket frontmatter 에서 읽는다 (없으면 None·가드 off)."""
    tickets = tmp_path / ".project_manager" / "board" / "tickets" / "claimed"
    tickets.mkdir(parents=True)
    (tickets / "T-0900-x.md").write_text(
        "---\nid: T-0900\nestimate: medium\ntouches:\n- src/\n---\n\n# 본문\n",
        encoding="utf-8")
    (tickets / "T-0901-y.md").write_text(
        "---\nid: T-0901\ntouches: []\n---\n\n# 본문\n", encoding="utf-8")
    monkeypatch.setattr(external, "REPO", tmp_path)
    assert external.parse_ticket_estimate("T-0900", pm_home=tmp_path) == "medium"
    assert external.parse_ticket_estimate("T-0901", pm_home=tmp_path) is None
    assert external.parse_ticket_estimate("T-9999", pm_home=tmp_path) is None


# ══ ③-b estimate 해석 단일화 (T-0602 ⑥) ═════════════════════════════════════
# codex R2 지적: 리뷰쪽은 정규식으로 읽어 `estimate: small # reviewed` 를 `small # reviewed` 로
# 해석해 상한을 조용히 끄고(모르는 값 = 가드 off), 완료쪽(`ticket_finish.get_ticket_estimate`)은
# 같은 값을 YAML 로 `small` 로 읽는다 — 두 게이트가 다른 값을 본다. 해석을 board 의 frontmatter
# 로더 하나로 모아 닫는다.


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
    ("estimate: [small]", None),                    # 비-문자열 — 상한을 지어내지 않는다
    ("estimate:", None),                            # 빈 값 = 가드 off
])
def test_estimate_is_read_as_yaml_not_as_a_regex_tail(
        external, tmp_path, monkeypatch, declared, expected):
    """주석·인용·여백 형상을 YAML 해석으로 읽는다 — 꼬리를 값에 붙이면 상한이 조용히 꺼진다."""
    _write_ticket(tmp_path, "T-0910", declared)
    monkeypatch.setattr(external, "REPO", tmp_path)
    assert external.parse_ticket_estimate("T-0910", pm_home=tmp_path) == expected


def test_commented_estimate_keeps_the_circuit_breaker_on(
        external, monkeypatch, tmp_path, capsys):
    """`estimate: small # reviewed` 티켓의 초과 diff 는 그대로 차단된다 (가드 off 폐쇄·e2e)."""
    reviewer = _wire(external, monkeypatch, tmp_path, series=[0], stub_diff_cap=False)
    _write_ticket(tmp_path, "T-0911", "estimate: small # reviewed")
    monkeypatch.setattr(external, "diff_line_total", lambda *a, **k: 301)

    assert external.main(["--gate", "T-0911", "--paths", "x.py"]) == 1
    assert reviewer.calls == 0                       # 리뷰어 호출 전에 막힌다
    err = capsys.readouterr().err
    assert "서킷브레이커 차단" in err and "estimate=small" in err


def test_review_and_finish_gates_read_the_same_estimate(external, tmp_path, monkeypatch):
    """리뷰 게이트와 완료 게이트가 **같은 값**을 읽는다 (두 게이트 불일치 폐쇄).

    완료쪽은 board 로더를 통해 읽으므로, 리뷰쪽도 같은 로더를 쓰는지 실제 두 함수의 반환으로
    대조한다 — '둘 다 YAML 이겠지' 대신 같은 입력에 같은 답을 요구한다."""
    finish = _load("ticket_finish")
    path = _write_ticket(tmp_path, "T-0912", "estimate: small # reviewed")
    monkeypatch.setattr(external, "REPO", tmp_path)
    # 완료쪽 board 로더를 tmp 티켓에 묶는 shim — frontmatter 파싱은 **실 board** 그대로다.
    shim = tmp_path / "board_shim.py"
    shim.write_text(
        f"ENGINE_REV = {finish.ENGINE_REV!r}\n"
        "import importlib.util, pathlib\n"
        "_spec = importlib.util.spec_from_file_location("
        f"'shim_board', {str(TOOLS / 'board.py')!r})\n"
        "_real = importlib.util.module_from_spec(_spec)\n"
        "_spec.loader.exec_module(_real)\n"
        "def find_ticket_exact(tid, **kwargs): "
        f"return ('claimed', pathlib.Path({str(path)!r}))\n"
        "load_ticket = _real.load_ticket\n",
        encoding="utf-8")

    review_side = external.parse_ticket_estimate("T-0912", pm_home=tmp_path)
    finish_side = finish.get_ticket_estimate(shim, "T-0912")
    assert review_side == finish_side == "small"


def test_corrupt_frontmatter_turns_the_guard_off_without_crashing(
        external, tmp_path, monkeypatch):
    """손상 frontmatter 는 가드 off(None) — 상한 조회가 리뷰 실행을 크래시시키지 않는다."""
    tickets = tmp_path / ".project_manager" / "board" / "tickets" / "claimed"
    tickets.mkdir(parents=True)
    (tickets / "T-0913-x.md").write_text(
        "---\nid: T-0913\ndesign: waived: 인용 없는 콜론\n---\n\n# 본문\n", encoding="utf-8")
    monkeypatch.setattr(external, "REPO", tmp_path)
    assert external.parse_ticket_estimate("T-0913", pm_home=tmp_path) is None


# ══ ③-c 티켓 조회는 canonical ID 정확 일치 (T-0603 ④) ═══════════════════════
# codex R3 지적: estimate 조회(추가 리뷰어)와 티켓 로드(ticket_finish)가 `{id}-*.md` prefix glob
# **첫 매칭**을 믿는다 — `T-0036` 과 `T-0036-001` 이 공존하면 다른 티켓의 estimate 로 diff 상한이
# 정해지거나(허용/차단이 뒤바뀐다) 완료 게이트가 남의 touches 로 판정한다. 판정을 board 공용
# seam(`find_ticket_exact`) 하나로 모아 닫았고, 아래는 공존 픽스처에서 **세 소비처**(회귀 강등·
# estimate·완료 로드)가 전부 오인 0 임을 한 자리에서 단언한다.


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
    """강등 스코프·estimate 상한·완료 로드가 모두 canonical `T-0036` 을 본다 (오인 0·DoD)."""
    _coexisting_tickets(tmp_path)
    finish = _load("ticket_finish")
    board = _load("board")
    monkeypatch.setattr(board, "REPO", tmp_path)
    monkeypatch.setattr(external, "REPO", tmp_path)

    # ① 회귀 강등 스코프 (board)
    assert board._ticket_touches("T-0036") == ["src/pay.py"]
    assert board._gate_ticket("T-0036")[1].name.startswith("T-0036-본래")
    # ② diff 서킷브레이커 상한 (추가 리뷰어)
    assert external.parse_ticket_estimate("T-0036", pm_home=tmp_path) == "small"
    assert external.parse_ticket_touches("T-0036", pm_home=tmp_path) == ["src/pay.py"]
    # ③ 완료 게이트 티켓 로드 (ticket_finish)
    shim = _real_board_shim(tmp_path, finish)
    assert finish.get_ticket_estimate(shim, "T-0036") == "small"
    assert finish.get_ticket_touches(shim, "T-0036") == ["src/pay.py"]

    # prefixed 티켓 자신의 조회는 그대로 자기 값을 본다 (좁힘이 정상 조회를 삼키지 않는다).
    assert external.parse_ticket_estimate("T-0036-001", pm_home=tmp_path) == "large"
    assert finish.get_ticket_estimate(shim, "T-0036-001") == "large"
    assert board._ticket_touches("T-0036-001") == ["src/unrelated.py"]


def test_missing_ticket_keeps_each_consumer_on_its_own_fail_soft(
        external, tmp_path, monkeypatch):
    """정확 일치가 없으면 각 소비처는 종전 fail-soft 그대로다 (가드 off·빈 값·fail-loud)."""
    _coexisting_tickets(tmp_path)
    finish = _load("ticket_finish")
    board = _load("board")
    monkeypatch.setattr(board, "REPO", tmp_path)
    monkeypatch.setattr(external, "REPO", tmp_path)
    shim = _real_board_shim(tmp_path, finish)

    assert board._ticket_touches("T-0036-002") == []              # 스코프 없음 = FULL
    assert external.parse_ticket_estimate("T-0036-002", pm_home=tmp_path) is None
    assert finish.get_ticket_estimate(shim, "T-0036-002") is None
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
    assert external.parse_ticket_estimate("T-0040", pm_home=tmp_path) == "medium"
