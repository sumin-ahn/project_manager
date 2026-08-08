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
    """quota 는 게이트별이다 — 한 티켓이 썼다고 다른 티켓이 잠기지 않는다."""
    _wire(external, monkeypatch, tmp_path, series=[0])
    assert external.main(["--gate", "T-0599", "--paths", "x.py", "--confirm-fix"]) == 0
    assert external.main(["--gate", "T-0600", "--paths", "x.py", "--confirm-fix"]) == 0
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
