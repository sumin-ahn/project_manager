"""릴리즈 must-fix 잔여 기계 차단 (T-0613).

라운드 상한(T-0593)으로 종결된 게이트의 잔여 must-fix 가 후속 티켓으로 소화되지 않은 채 릴리즈가
진행되던 것이 근절 대상이다(실사고: PM 자체 판정으로 must-fix 4건을 이월한 채 릴리즈). 이 파일은
두 표면을 단언한다:

  ① 처분 선언 (`external_review.py --resolve-gate <게이트> --into|--fixed`) — 잔여 must-fix 를
     **어떻게 소화했는지**를 라운드 장부에 기록한다. 이관(`--into`)은 후속 티켓 ID 를, 해소
     (`--fixed`)는 통과로 끝난 **근거 게이트**를 장부 사실로 요구한다.
  ② 릴리즈 차단 (`board.py livegate record`) — 실행 **전에** 장부를 스캔해 미처분/미소화 잔여를
     rc 비영으로 막는다. 이관은 면제가 아니라 처분이라 대상 티켓이 done 이어야 열린다. 우회
     플래그는 없고, 판정 입력은 장부의 기록 사실뿐이다(PM 자의 판정이 들어갈 자리 없음).

hermetic: 두 도구의 경로 전역(REPO·LOCAL_DIR·LIVEGATE_FLAG)을 tmp 프로젝트로 재지정하고, 라이브
pytest·git 은 대역으로 격리한다(외부 전송 0·실행 0). `test_board_livegate.py`(livegate 축)와
`test_review_convergence_gate.py`(장부 축)의 hermetic 패턴 동류다.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

# 반려로 끝난 게이트의 최소 장부 항목 — 최종 라운드 must_fix 3건. 신규 writer는 완료 `ts`뿐 아니라
# 예약 `started_at`과 실제 검토 diff의 `target_rev`를 함께 싣는다. 근거는 반려 **종료 뒤 시작**하고
# 다른 revision을 검토한 통과여야 한다(완료 순서만으로는 동시 리뷰를 배제하지 못함).
_REJECTED_REV_1 = "sha256:" + "a" * 64
_REJECTED_REV_2 = "sha256:" + "b" * 64
_PASSED_REV = "sha256:" + "c" * 64
_EARLIER_REV = "sha256:" + "d" * 64
_REJECTED_ROUNDS = [
    {"sequence": 1, "verdict": 1, "must_fix": 4, "suggestions": 2,
     "started_at": "2026-07-31T23:00:00+00:00", "target_rev": _REJECTED_REV_1,
     "ts": "2026-08-01T00:00:00+00:00"},
    {"sequence": 2, "verdict": 1, "must_fix": 3, "suggestions": 1,
     "started_at": "2026-08-01T23:00:00+00:00", "target_rev": _REJECTED_REV_2,
     "ts": "2026-08-02T00:00:00+00:00"},
]
# 통과로 끝난 게이트 — 마지막 반려 종료 뒤 시작·변경된 diff를 검토한 `--fixed` 근거.
_PASSED_ROUNDS = [{"sequence": 1, "verdict": 0, "must_fix": 0, "suggestions": 0,
                   "started_at": "2026-08-02T01:00:00+00:00", "target_rev": _PASSED_REV,
                   "ts": "2026-08-03T00:00:00+00:00"}]
# 반려보다 **앞서 시작한** 통과 — 연관성 없는 근거(우연한 완료 순서)를 태우는 절.
_EARLIER_PASSED_ROUNDS = [{"sequence": 1, "verdict": 0, "must_fix": 0,
                           "started_at": "2026-07-19T00:00:00+00:00",
                           "target_rev": _EARLIER_REV,
                           "ts": "2026-07-20T00:00:00+00:00"}]
# 판정이 무효했던 라운드 — 전송은 됐으나 must_fix 를 셀 근거가 없다('미상').
_UNKNOWN_ROUNDS = [{"sequence": 1, "verdict": 1, "must_fix": None,
                    "started_at": "2026-08-01T23:00:00+00:00",
                    "target_rev": _REJECTED_REV_2,
                    "ts": "2026-08-02T00:00:00+00:00"}]


def _load(name: str, alias: str):
    spec = importlib.util.spec_from_file_location(alias, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_project(tmp_path: Path) -> Path:
    """tmp PM 홈 뼈대 — `.local`(장부) + board 티켓 디렉토리."""
    proj = tmp_path / "proj"
    (proj / ".project_manager" / ".local").mkdir(parents=True)
    for status in ("open", "claimed", "blocked", "done"):
        (proj / ".project_manager" / "wiki" / "tickets" / status).mkdir(parents=True)
    return proj


def _add_ticket(proj: Path, tid: str, status: str) -> None:
    (proj / ".project_manager" / "wiki" / "tickets" / status / f"{tid}-fixture.md").write_text(
        f"---\nid: {tid}\ntitle: 픽스처\ntouches:\n- x.py\n---\n\n# {tid}\n", encoding="utf-8",
    )


def _ledger_path(proj: Path) -> Path:
    return proj / ".project_manager" / ".local" / "review_rounds.json"


def _write_ledger(proj: Path, ledger: dict | str) -> None:
    _ledger_path(proj).write_text(
        ledger if isinstance(ledger, str) else json.dumps(ledger, ensure_ascii=False),
        encoding="utf-8",
    )


def _read_ledger(proj: Path) -> dict:
    return json.loads(_ledger_path(proj).read_text(encoding="utf-8"))


# ── ① 처분 선언 표면 (external_review --resolve-gate) ───────────────────────


@pytest.fixture
def declare(tmp_path, monkeypatch):
    """처분 선언 실행 대역 — tmp PM 홈에 앵커된 external_review 인스턴스.

    반환 객체의 `run(*argv)` 가 `main()` 을 태우고, `ledger()` 가 결과 장부를 돌려준다.
    외부 전송 경로는 타지 않는다(선언면은 전송 전에 끝난다).
    """
    proj = _make_project(tmp_path)
    module = _load("external_review", "must_fix_external_review")
    monkeypatch.setattr(module, "REPO", proj)
    _write_ledger(proj, {
        "T-0610": {"count": 3, "rounds": list(_REJECTED_ROUNDS)},       # 반려로 종결
        "T-0612": {"count": 1, "rounds": list(_PASSED_ROUNDS)},         # 반려 이후 통과 = 근거
        "T-0613": {"count": 1, "rounds": list(_REJECTED_ROUNDS)},       # 근거가 못 되는 반려 게이트
        "T-0614": {"count": 1, "rounds": list(_EARLIER_PASSED_ROUNDS)},  # 반려보다 앞선 통과
        "T-0615": {"count": 1, "rounds": list(_UNKNOWN_ROUNDS)},        # 판정 무효(미상)
    })
    _add_ticket(proj, "T-0611", "open")
    _add_ticket(proj, "T-0620", "done")
    return types.SimpleNamespace(
        module=module, proj=proj,
        run=lambda *argv: module.main(list(argv)),
        ledger=lambda: _read_ledger(proj),
    )


def test_into_declaration_records_follow_up_ticket(declare, capsys):
    """`--resolve-gate --into` — 이관 대상 티켓 ID 를 장부에 남긴다 (rc 0)."""
    assert declare.run("--resolve-gate", "T-0610", "--into", "T-0611") == 0
    resolution = declare.ledger()["T-0610"]["resolution"]
    assert resolution["kind"] == "into"
    assert resolution["ticket"] == "T-0611"
    assert resolution["must_fix"] == 3      # 선언 시점 잔여 (감사 사실)
    assert resolution["ts"]
    # 선언이 결속한 라운드 좌표 — 이 뒤에 새 라운드가 오면 릴리즈 게이트가 미처분으로 본다.
    assert resolution["round_sequence"] == _REJECTED_ROUNDS[-1]["sequence"]
    assert resolution["rounds"] == len(_REJECTED_ROUNDS)
    out = capsys.readouterr().out
    assert "이관→T-0611" in out
    assert "done 일 때만" in out            # 면제가 아님을 선언 응답이 말한다


def test_fixed_declaration_requires_passing_evidence_gate(declare):
    """`--fixed <근거 게이트>` — 근거 게이트가 통과로 끝났으면 해소 선언이 기록된다."""
    assert declare.run("--resolve-gate", "T-0610", "--fixed", "T-0612") == 0
    resolution = declare.ledger()["T-0610"]["resolution"]
    assert resolution["kind"] == "fixed"
    assert resolution["evidence_gate"] == "T-0612"


def test_fixed_rejected_when_evidence_gate_still_rejected(declare, capsys):
    """근거 게이트의 마지막 라운드가 반려면 해소 선언을 거부한다 (장부 무변경).

    '해소했다'는 주장이 아니라 **장부의 통과 기록**이 근거다 — 주장만으로 선언되면 이 게이트는
    자의 판정을 장부에 옮겨 적는 도구가 된다."""
    assert declare.run("--resolve-gate", "T-0610", "--fixed", "T-0613") == 1
    assert declare.ledger()["T-0610"].get("resolution") is None
    err = capsys.readouterr().err
    assert "근거 게이트 T-0613" in err
    assert "마지막 라운드가 통과가 아닙니다" in err


def test_fixed_self_reference_is_refused(declare, capsys):
    """자기 자신은 근거가 될 수 없다 — 반려로 끝난 게이트 안에서 자기 통과를 찾는 셈이다."""
    assert declare.run("--resolve-gate", "T-0610", "--fixed", "T-0610") == 1
    assert declare.ledger()["T-0610"].get("resolution") is None
    assert "자기 자신은 근거가 될 수 없습니다" in capsys.readouterr().err


def test_fixed_evidence_must_postdate_the_rejection(declare, capsys):
    """근거 통과가 반려보다 **앞서 시작**하면 거부 — 우연한 완료 순서는 근거가 아니다."""
    assert declare.run("--resolve-gate", "T-0610", "--fixed", "T-0614") == 1
    assert declare.ledger()["T-0610"].get("resolution") is None
    err = capsys.readouterr().err
    assert "반려 이후에 시작되지 않았습니다" in err
    assert "그 반려 **이후 시작한** 통과여야" in err


def test_fixed_rejects_concurrent_review_started_before_rejection(declare, capsys):
    """반려 전 시작·반려 후 종료한 동시 리뷰는 `--fixed` 근거가 아니다 (T-0618 핵심 재현)."""
    ledger = declare.ledger()
    ledger["T-0616"] = {"count": 1, "rounds": [{
        "sequence": 1, "verdict": 0, "must_fix": 0,
        "started_at": "2026-08-01T23:30:00+00:00",  # 반려(8/2 00:00) 전 시작
        "target_rev": _REJECTED_REV_2,                # 같은 미수정 diff
        "ts": "2026-08-03T00:00:00+00:00",          # 반려 뒤 늦게 종료
    }]}
    _write_ledger(declare.proj, ledger)

    assert declare.run("--resolve-gate", "T-0610", "--fixed", "T-0616") == 1
    assert "반려 이후에 시작되지 않았습니다" in capsys.readouterr().err


@pytest.mark.parametrize("field,value", [
    ("ts", "zzz"),
    ("started_at", "2026-08-02 01:00:00"),
    ("ts", "2026-08-03T09:00:00+09:00"),
])
def test_fixed_rejects_malformed_or_non_utc_timestamps(declare, capsys, field, value):
    """완료/시작 ts는 엄격한 ISO 8601 UTC만 허용 — 손상·비UTC는 fail-closed."""
    ledger = declare.ledger()
    row = dict(_PASSED_ROUNDS[0])
    row[field] = value
    ledger["T-0616"] = {"count": 1, "rounds": [row]}
    _write_ledger(declare.proj, ledger)

    assert declare.run("--resolve-gate", "T-0610", "--fixed", "T-0616") == 1
    err = capsys.readouterr().err
    assert "엄격한 ISO 8601 UTC" in err and "결속 불충분" in err


@pytest.mark.parametrize("started_at", [
    pytest.param(None, id="missing"),
    pytest.param("2026-08-02 08:00:00", id="non-iso"),
    pytest.param("2026-08-02T08:00:00+09:00", id="non-utc"),
])
def test_fixed_rejects_unbound_rejection_started_at(declare, capsys, started_at):
    """반려 started_at 도 누락·비파싱·비UTC면 선후 결속을 확인할 수 없어 거부한다."""
    ledger = declare.ledger()
    row = dict(ledger["T-0610"]["rounds"][-1])
    if started_at is None:
        row.pop("started_at")
    else:
        row["started_at"] = started_at
    ledger["T-0610"]["rounds"][-1] = row
    _write_ledger(declare.proj, ledger)

    assert declare.run("--resolve-gate", "T-0610", "--fixed", "T-0612") == 1
    err = capsys.readouterr().err
    assert "엄격한 ISO 8601 UTC" in err and "결속 불충분" in err


def test_fixed_rejects_rejection_completed_before_it_started(declare, capsys):
    """반려 시작 10:00·완료 09:00·근거 시작 09:30 조합은 다른 rev여도 거부한다."""
    ledger = declare.ledger()
    blocked = dict(ledger["T-0610"]["rounds"][-1])
    blocked.update({
        "started_at": "2026-08-02T10:00:00+00:00",
        "ts": "2026-08-02T09:00:00+00:00",
    })
    ledger["T-0610"]["rounds"][-1] = blocked
    evidence = dict(ledger["T-0612"]["rounds"][-1])
    evidence.update({
        "started_at": "2026-08-02T09:30:00+00:00",
        "ts": "2026-08-02T11:00:00+00:00",
    })
    ledger["T-0612"]["rounds"][-1] = evidence
    _write_ledger(declare.proj, ledger)

    assert declare.run("--resolve-gate", "T-0610", "--fixed", "T-0612") == 1
    err = capsys.readouterr().err
    assert "반려 라운드 완료 ts가 started_at 보다 앞서" in err
    assert "결속 불충분" in err


def test_fixed_rejects_old_round_without_binding_fields(declare, capsys):
    """구 라운드는 마이그레이션으로 꾸미지 않고 결속 불충분 사유로 거부한다."""
    ledger = declare.ledger()
    ledger["T-0616"] = {"count": 1, "rounds": [{
        "sequence": 1, "verdict": 0, "must_fix": 0,
        "ts": "2026-08-03T00:00:00+00:00",
    }]}
    _write_ledger(declare.proj, ledger)

    assert declare.run("--resolve-gate", "T-0610", "--fixed", "T-0616") == 1
    assert "결속 불충분" in capsys.readouterr().err


def test_fixed_rejects_unchanged_target_revision(declare, capsys):
    """반려 뒤 새로 시작했어도 같은 diff fingerprint면 코드 해소 근거가 아니다."""
    ledger = declare.ledger()
    row = {**_PASSED_ROUNDS[0], "target_rev": _REJECTED_REV_2}
    ledger["T-0616"] = {"count": 1, "rounds": [row]}
    _write_ledger(declare.proj, ledger)

    assert declare.run("--resolve-gate", "T-0610", "--fixed", "T-0616") == 1
    assert "같은 대상 rev fingerprint" in capsys.readouterr().err


def test_unknown_residual_can_be_disposed(declare, capsys):
    """판정 무효('미상')로 끝난 게이트도 처분을 선언할 수 있다 — 릴리즈가 막는 축이므로.

    차단(릴리즈)과 선언(여기)이 서로 다른 술어를 쓰면 "막히는데 처분은 못 하는" 데드락이 난다."""
    assert declare.run("--resolve-gate", "T-0615", "--into", "T-0611") == 0
    resolution = declare.ledger()["T-0615"]["resolution"]
    assert resolution["kind"] == "into"
    assert resolution["must_fix"] is None                 # 미상은 건수로 위장하지 않는다
    assert "미상(판정 무효 라운드)" in capsys.readouterr().out


def test_dry_run_refuses_declaration(declare, capsys):
    """`--dry-run` 은 거부한다 — 기록이 목적인 표면에서 '미리보기'는 성립하지 않는다."""
    assert declare.run("--resolve-gate", "T-0610", "--into", "T-0611", "--dry-run") == 1
    assert declare.ledger()["T-0610"].get("resolution") is None
    assert "--dry-run` 과 함께 쓸 수 없습니다" in capsys.readouterr().err


def test_fixed_rejected_when_evidence_gate_absent_from_ledger(declare, capsys):
    """장부에 없는 게이트는 근거가 될 수 없다 (기록 사실만 입력)."""
    assert declare.run("--resolve-gate", "T-0610", "--fixed", "T-9999") == 1
    assert declare.ledger()["T-0610"].get("resolution") is None
    assert "라운드 장부에 그 게이트의 기록이 없습니다" in capsys.readouterr().err


def test_fixed_without_evidence_is_refused(declare, capsys):
    """근거 게이트 지목은 **필수** — 값 없는 `--fixed` 는 선언되지 않는다."""
    assert declare.run("--resolve-gate", "T-0610", "--fixed") == 1
    assert declare.ledger()["T-0610"].get("resolution") is None
    assert "--fixed <근거 게이트>" in capsys.readouterr().err


def test_cli_help_documents_fixed_binding_and_dry_run_refusal(declare):
    """CLI 도움말도 새 `--fixed` 선후/rev 결속과 resolve dry-run 거부를 직접 말한다."""
    help_text = declare.module.build_arg_parser().format_help()
    assert "반려 종료 뒤 시작" in help_text
    assert "target_rev" in help_text
    assert "결속 필드 없는 구 라운드는 거부" in help_text
    assert "--resolve-gate 는 기록 명령이므로 함께 쓰면 exit 1" in help_text


@pytest.mark.parametrize("argv", [
    ("--resolve-gate", "T-0610"),                                 # 처분 미지정
    ("--resolve-gate", "T-0610", "--into", "T-0611", "--fixed", "T-0612"),  # 두 처분 동시
])
def test_disposition_must_be_exactly_one(declare, argv, capsys):
    """처분은 정확히 하나다 — 미지정·중복 둘 다 거부(장부 무변경)."""
    assert declare.run(*argv) == 1
    assert declare.ledger()["T-0610"].get("resolution") is None
    assert "처분을 **하나** 지정" in capsys.readouterr().err


def test_self_referential_into_is_refused(declare, capsys):
    """자기 자신으로의 이관은 우회다 — 그 게이트가 done 이라는 사실만으로 잔여가 지워진다."""
    assert declare.run("--resolve-gate", "T-0610", "--into", "T-0610") == 1
    assert declare.ledger()["T-0610"].get("resolution") is None
    assert "자기 자신" in capsys.readouterr().err


def test_into_ticket_must_exist_on_board(declare, capsys):
    """이관 대상이 보드에 없으면 선언 시점에 거부한다 (ID 오기·미생성)."""
    assert declare.run("--resolve-gate", "T-0610", "--into", "T-7777") == 1
    assert declare.ledger()["T-0610"].get("resolution") is None
    assert "보드에서 찾지 못했습니다" in capsys.readouterr().err


def test_gate_without_ledger_record_is_refused(declare, capsys):
    """장부에 없는 게이트는 처분할 잔여가 없다."""
    assert declare.run("--resolve-gate", "T-0555", "--into", "T-0611") == 1
    assert "T-0555" not in declare.ledger()
    assert "라운드 장부에 기록이 없습니다" in capsys.readouterr().err


def test_gate_without_residual_must_fix_is_refused(declare, capsys):
    """통과로 끝난 게이트는 처분 대상이 아니다 (릴리즈 게이트도 안 본다)."""
    assert declare.run("--resolve-gate", "T-0612", "--into", "T-0611") == 1
    assert declare.ledger()["T-0612"].get("resolution") is None
    assert "처분할 잔여 must-fix 가 없습니다" in capsys.readouterr().err


def test_disposition_flags_require_resolve_gate(declare, capsys):
    """`--into`/`--fixed` 만으로는 뜻이 없다 — 부작용 0 지점에서 거부."""
    assert declare.run("--into", "T-0611") == 1
    assert "--resolve-gate <게이트> 와 함께" in capsys.readouterr().err.replace("`", "")


@pytest.mark.parametrize("argv", [
    ("--resolve-gate", "wave", "--into", "T-0611"),
    ("--resolve-gate", "T-0610", "--fixed", "wave"),
])
def test_reserved_ledger_key_is_refused(declare, argv, capsys):
    """장부 예약 키(`wave`)는 게이트 이름으로 받지 않는다 — 두 표면 모두."""
    assert declare.run(*argv) == 1
    assert "예약 키" in capsys.readouterr().err


def test_redeclaration_replaces_previous_disposition(declare, capsys):
    """재선언은 이전 처분을 교체하고 그 사실을 알린다 (조용한 덮어쓰기 금지)."""
    assert declare.run("--resolve-gate", "T-0610", "--fixed", "T-0612") == 0
    assert declare.run("--resolve-gate", "T-0610", "--into", "T-0611") == 0
    assert declare.ledger()["T-0610"]["resolution"]["kind"] == "into"
    assert "이전 처분 선언을 교체" in capsys.readouterr().err


def test_rounds_report_shows_disposition_column(declare, capsys):
    """`--rounds-report` 처분 열 — 미처분/이관/해소/무대상이 릴리즈 판정과 같은 사실을 보인다."""
    assert declare.run("--rounds-report") == 0
    report = capsys.readouterr().out
    assert "게이트 T-0610" in report and "처분=미처분" in report
    assert "처분=무대상" in report                       # T-0612 (잔여 없음)
    assert declare.run("--resolve-gate", "T-0610", "--into", "T-0611") == 0
    assert declare.run("--rounds-report") == 0
    assert "처분=이관→T-0611" in capsys.readouterr().out
    assert declare.run("--resolve-gate", "T-0610", "--fixed", "T-0612") == 0
    assert declare.run("--rounds-report") == 0
    assert "처분=해소(근거 T-0612)" in capsys.readouterr().out


# ── ② 릴리즈 차단 (board livegate record) ──────────────────────────────────


class _FakeRun:
    """board.subprocess.run 대역 — pytest 를 실기동하지 않고 rc·요약행만 주입한다."""

    def __init__(self, rc: int = 0, stdout: str = ""):
        self.rc = rc
        self.stdout = stdout
        self.calls: list[tuple] = []

    def __call__(self, *args, **kwargs):
        self.calls.append(args)
        return types.SimpleNamespace(returncode=self.rc, stdout=self.stdout, stderr="")


@pytest.fixture
def release(tmp_path, monkeypatch):
    """tmp PM 홈에 앵커된 board 인스턴스 + livegate IO 격리.

    `_git_config_get` 대역으로 hooksPath 미설정(솔로 형상)을 고정해 flag/장부 해소가 tmp
    `.local` 에 떨어지게 하고, `subprocess.run` 대역이 받는 호출은 **release wave 뿐**이 되게 한다
    (사전 검사가 라이브 wave 를 돌리기 전에 막는지를 호출 수로 단언할 수 있다)."""
    proj = _make_project(tmp_path)
    module = _load("board", "must_fix_board")
    local = proj / ".project_manager" / ".local"
    monkeypatch.setattr(module, "REPO", proj)
    monkeypatch.setattr(module, "LOCAL_DIR", local)
    monkeypatch.setattr(module, "LIVEGATE_FLAG", local / "livegate.json")
    monkeypatch.setattr(module, "LEASES_FILE", local / "worktree-leases.json")
    monkeypatch.setattr(module, "_git_config_get", lambda cwd, key: None)
    monkeypatch.setattr(module, "_git_head_at", lambda cwd: "feedface" * 5)
    runner = _FakeRun(0, f"{module.LIVEGATE_RELEASE_PIN} passed, 800 deselected in 40.0s")
    monkeypatch.setattr(module.subprocess, "run", runner)
    _add_ticket(proj, "T-0611", "claimed")
    _add_ticket(proj, "T-0620", "done")
    return types.SimpleNamespace(
        module=module, proj=proj, runner=runner,
        record=lambda: module.cmd_livegate(argparse.Namespace(
            action="record", rev=None, cwd=str(proj), repo=None, slot=None, task=None)),
        flag=lambda: json.loads((local / "livegate.json").read_text(encoding="utf-8")),
    )


def _rejected_gate(**resolution) -> dict:
    """반려로 끝난 게이트 항목 — 처분을 주면 **선언 당시 라운드 좌표**까지 결속해 넣는다.

    좌표(`round_sequence`·`rounds`)는 처분 유효성의 일부다 — 선언 뒤 새 라운드가 오면 그 처분은
    지나간 잔여의 것이라 미처분으로 판정된다. 좌표를 생략한 선언을 태우는 절은 그 축 전용
    테스트가 따로 본다."""
    entry = {"count": 3, "rounds": list(_REJECTED_ROUNDS)}
    if resolution:
        binding = {"round_sequence": _REJECTED_ROUNDS[-1]["sequence"],
                   "rounds": len(_REJECTED_ROUNDS)}
        entry["resolution"] = {**binding, **resolution}
    return entry


def test_absent_ledger_is_not_a_target(release):
    """라운드 장부가 아예 없는 형상(추가 리뷰어 비활성 채택자)은 검사 무대상 — 그대로 진행."""
    assert not _ledger_path(release.proj).exists()
    assert release.record() == 0
    assert release.flag()["status"] == "pass"
    assert (release.proj / ".project_manager" / ".local" / "release-must-fix").read_text(
        encoding="ascii") == "clear\n"
    assert len(release.runner.calls) == 1        # release wave 가 실제로 돌았다


def test_unresolved_must_fix_blocks_before_live_wave(release, capsys):
    """미처분 잔여 must-fix → rc 비영 차단 · 라이브 wave 미실행 · 목록/처방 표면화."""
    _write_ledger(release.proj, {"T-0610": _rejected_gate()})
    assert release.record() == 1
    assert release.runner.calls == [], "차단은 값비싼 라이브 wave 를 돌리기 전이어야 한다"
    err = capsys.readouterr().err
    assert "미해소 must-fix 잔여 1건" in err
    assert "T-0610: 최종 라운드 must_fix 3건 · 처분 선언 없음" in err
    assert "--resolve-gate <게이트> --into <T-NNNN>" in err     # 처방
    assert "우회 플래그 없음" in err
    assert (release.proj / ".project_manager" / ".local" / "release-must-fix").read_text(
        encoding="ascii") == "blocked\n"


def test_block_records_fail_so_push_hook_stays_closed(release):
    """차단은 fail 을 **기록**한다 — 같은 HEAD 의 옛 green 이 살아남아 훅을 통과하지 않게."""
    (release.proj / ".project_manager" / ".local" / "livegate.json").write_text(
        json.dumps({"head": "feedface" * 5, "status": "pass", "n": 18, "rc": 0}),
        encoding="utf-8")
    _write_ledger(release.proj, {"T-0610": _rejected_gate()})
    assert release.record() == 1
    flag = release.flag()
    assert flag["status"] == "fail"
    assert flag["reason"] == "unresolved-must-fix"
    check = release.module.cmd_livegate(argparse.Namespace(
        action="check", rev="feedface" * 5, cwd=str(release.proj)))
    assert check == 1, "훅이 소비하는 check 도 red 여야 하류 전체가 막힌다"


def test_into_open_ticket_still_blocks(release, capsys):
    """이관 선언돼도 대상 티켓이 done 이 아니면 차단 유지 — 이관은 면제가 아니다."""
    _write_ledger(release.proj, {
        "T-0610": _rejected_gate(kind="into", ticket="T-0611")})
    assert release.record() == 1
    assert release.runner.calls == []
    err = capsys.readouterr().err
    assert "이관 선언 T-0611" in err
    assert "아직 done 이 아닙니다(현재 claimed)" in err


def test_into_done_ticket_passes(release):
    """이관 대상이 done 이면 통과 — 같은 릴리즈 안에서 소화된 경우."""
    _write_ledger(release.proj, {
        "T-0610": _rejected_gate(kind="into", ticket="T-0620")})
    assert release.record() == 0
    assert release.flag()["status"] == "pass"


def test_into_unknown_ticket_blocks(release, capsys):
    """보드에 없는 티켓으로의 이관 선언은 처분으로 인정하지 않는다 (fail-closed)."""
    _write_ledger(release.proj, {
        "T-0610": _rejected_gate(kind="into", ticket="T-7777")})
    assert release.record() == 1
    assert "찾지 못했습니다" in capsys.readouterr().err


def test_fixed_declaration_passes(release):
    """해소 선언(`fixed`)은 통과 — 근거 게이트 확인은 선언 시점에 끝났다."""
    _write_ledger(release.proj, {
        "T-0610": _rejected_gate(kind="fixed", evidence_gate="T-0612"),
        "T-0612": {"count": 1, "rounds": list(_PASSED_ROUNDS)}})
    assert release.record() == 0
    assert release.flag()["status"] == "pass"


def test_fixed_rejection_interval_is_reverified_at_release(release, capsys):
    """반려 자체의 시작/완료 선후가 손상된 fixed 선언은 릴리즈 재검증에서도 차단한다."""
    blocked = _rejected_gate(kind="fixed", evidence_gate="T-0612")
    blocked["rounds"][-1] = {
        **blocked["rounds"][-1],
        "started_at": "2026-08-02T10:00:00+00:00",
        "ts": "2026-08-02T09:00:00+00:00",
    }
    evidence = {
        **_PASSED_ROUNDS[-1],
        "started_at": "2026-08-02T09:30:00+00:00",
        "ts": "2026-08-02T11:00:00+00:00",
    }
    _write_ledger(release.proj, {
        "T-0610": blocked,
        "T-0612": {"count": 1, "rounds": [evidence]},
    })

    assert release.record() == 1
    assert release.runner.calls == []
    err = capsys.readouterr().err
    assert "반려 라운드 완료 ts가 started_at 보다 앞서" in err
    assert "결속 불충분" in err


def test_suggestion_only_residue_passes(release):
    """suggestion 만 남은 게이트는 비대상 — 이월 허용 계약(검사 축은 must_fix 뿐)."""
    _write_ledger(release.proj, {"T-0610": {
        "count": 2,
        "rounds": [{"sequence": 1, "verdict": 1, "must_fix": 2, "suggestions": 1},
                   {"sequence": 2, "verdict": 1, "must_fix": 0, "suggestions": 5}]}})
    assert release.record() == 0
    assert release.flag()["status"] == "pass"


def test_unknown_residual_blocks(release, capsys):
    """판정 무효 라운드('미상')로 끝난 게이트는 차단 — 확인 못 한 것은 잔여 없음이 아니다.

    전송은 됐는데 판정이 무효했던 라운드(타임아웃·오염·섹션 부재)를 0 으로 접으면 차단이 조용히
    풀린다. 이 축은 처분 선언으로만 열린다."""
    _write_ledger(release.proj, {"T-0610": {
        "count": 1, "rounds": list(_UNKNOWN_ROUNDS)}})
    assert release.record() == 1
    assert release.runner.calls == []
    assert "must_fix 미상(판정 무효 라운드)" in capsys.readouterr().err


def test_unknown_count_on_passing_round_is_not_a_residual(release):
    """통과(rc 0)로 끝난 라운드의 미상은 비대상 — 판정은 났고 must-fix 섹션이 없었을 뿐이다."""
    _write_ledger(release.proj, {"T-0610": {
        "count": 1, "rounds": [{"sequence": 1, "verdict": 0, "must_fix": None,
                                "ts": "2026-08-03T00:00:00+00:00"}]}})
    assert release.record() == 0


def test_unknown_residual_clears_with_disposition(release):
    """미상 게이트도 처분(이관 done)으로 열린다 — 차단 축과 처분 축이 같은 술어를 쓴다."""
    _write_ledger(release.proj, {"T-0610": {
        "count": 1, "rounds": list(_UNKNOWN_ROUNDS),
        "resolution": {"kind": "into", "ticket": "T-0620",
                       "round_sequence": 1, "rounds": 1}}})
    assert release.record() == 0


def test_corrupt_ledger_blocks(release, capsys):
    """장부가 있는데 읽지 못하면 차단 — '확인하지 못했다'와 '잔여가 없다'는 다르다."""
    _write_ledger(release.proj, "{이건 JSON 이 아니다")
    assert release.record() == 1
    assert release.runner.calls == []
    assert "잔여 must-fix 를 확인할 수 없어 차단" in capsys.readouterr().err


def test_wave_budget_section_is_not_a_gate(release):
    """장부 예약 키(`wave` 절)는 게이트가 아니라 검사 순회에서 건너뛴다."""
    _write_ledger(release.proj, {
        "wave": {"id": "x", "started": None, "spent": 3},
        "T-0610": _rejected_gate(kind="fixed", evidence_gate="T-0612"),
        "T-0612": {"count": 1, "rounds": list(_PASSED_ROUNDS)}})
    assert release.record() == 0


def test_corrupt_resolution_is_not_a_disposition(release, capsys):
    """해석할 수 없는 처분 값은 처분이 아니다 — 손상 한 줄이 차단을 열지 않는다."""
    _write_ledger(release.proj, {"T-0610": _rejected_gate(kind="이관했음", ticket="T-0620")})
    assert release.record() == 1
    assert "처분 선언 없음" in capsys.readouterr().err


def test_disposition_is_bound_to_the_round_it_disposed(release, capsys):
    """처분 뒤에 새 반려 라운드가 오면 옛 선언은 그 잔여를 지우지 못한다 (라운드 결속).

    처분이 게이트 단위로만 남으면 한 번의 선언이 이후 모든 잔여를 무기한 사면한다 — 선언은
    "그때 그 잔여"의 소화 기록이라 좌표(마지막 라운드 순번·산출 수)에 결속한다."""
    entry = _rejected_gate(kind="into", ticket="T-0620")
    entry["rounds"] = [*_REJECTED_ROUNDS,
                       {"sequence": 3, "verdict": 1, "must_fix": 2}]   # 선언 뒤 새 반려
    _write_ledger(release.proj, {"T-0610": entry})
    assert release.record() == 1
    err = capsys.readouterr().err
    assert "이후 새 라운드가 기록됐습니다" in err
    assert "선언 시점 #2/2건 ≠ 현재 #3/3건" in err


def test_unbound_disposition_is_not_a_disposition(release, capsys):
    """라운드 좌표 없는 선언은 처분으로 인정하지 않는다 — 어느 잔여를 처분했는지 확인 불가."""
    _write_ledger(release.proj, {"T-0610": {
        "count": 3, "rounds": list(_REJECTED_ROUNDS),
        "resolution": {"kind": "into", "ticket": "T-0620"}}})       # 좌표 필드 없음
    assert release.record() == 1
    assert "처분 선언 없음" in capsys.readouterr().err


def test_fixed_evidence_is_reverified_at_release(release, capsys):
    """`fixed` 근거 게이트가 뒤이어 반려로 뒤집히면 릴리즈 시점에 차단한다 (근거 재검증).

    근거는 선언 시점에 한 번 확인하고 끝낼 사실이 아니다 — 통과가 뒤집히면 '코드로 해소됐다'는
    선언의 근거 자체가 사라진다."""
    _write_ledger(release.proj, {
        "T-0610": _rejected_gate(kind="fixed", evidence_gate="T-0612"),
        "T-0612": {"count": 2, "rounds": [*_PASSED_ROUNDS,
                                          {"sequence": 2, "verdict": 1, "must_fix": 2}]}})
    assert release.record() == 1
    err = capsys.readouterr().err
    assert "근거 게이트 T-0612 — 마지막 라운드가 통과가 아닙니다" in err
    assert "잔여 must_fix 2건" in err


def test_fixed_evidence_gate_removed_from_ledger_blocks(release, capsys):
    """근거 게이트 기록이 장부에서 사라지면 차단 — 근거 부재는 통과가 아니다."""
    _write_ledger(release.proj, {
        "T-0610": _rejected_gate(kind="fixed", evidence_gate="T-0612")})
    assert release.record() == 1
    assert "기록이 장부에서 사라졌습니다" in capsys.readouterr().err


@pytest.mark.parametrize("entry,expected", [
    (7, "게이트 항목 형식 오류(int)"),                       # 비-dict 항목 (옛 skip 통과)
    ({"rounds": "3 rounds"}, "`rounds` 형식 오류(str)"),      # 문자열 (옛 '기록 없음' 통과)
    ({"rounds": 3}, "`rounds` 형식 오류(int)"),               # 정수 (옛 예외 중단)
    ({"rounds": [{"sequence": 1, "verdict": 1, "must_fix": 3}, "x"]},
     "매핑이 아닌 원소"),                                     # 라운드 원소 손상
    ({"rounds": list(_REJECTED_ROUNDS), "resolution": "이관함"},
     "`resolution` 형식 오류(str)"),                          # 처분 절 손상
])
def test_corrupt_gate_entries_all_block(release, capsys, entry, expected):
    """존재하는데 해석 불가한 항목은 전부 차단으로 수렴 — 통과/중단 조각을 남기지 않는다."""
    _write_ledger(release.proj, {"T-0610": entry})
    assert release.record() == 1
    assert release.runner.calls == []
    err = capsys.readouterr().err
    assert expected in err
    assert release.flag()["status"] == "fail"     # 옛 green 이 남지 않는다


def test_check_reverifies_ledger_after_record(release, capsys):
    """record green 뒤 같은 HEAD 에 새 반려가 기록되면 **check(push 훅)** 가 막는다.

    기록은 HEAD 로 키되지만 라운드 장부는 그 뒤로도 자란다 — check 가 스냅샷만 믿으면 그 창으로
    미해소 잔여가 push 된다(record/check 비대칭)."""
    head = "feedface" * 5
    assert release.record() == 0                              # 장부 무대상 → green 기록
    check = argparse.Namespace(action="check", rev=head, cwd=str(release.proj))
    assert release.module.cmd_livegate(check) == 0
    _write_ledger(release.proj, {"T-0610": _rejected_gate()})  # 같은 HEAD 에서 새 반려
    assert release.module.cmd_livegate(check) == 1
    assert "미해소 must-fix 잔여" in capsys.readouterr().err


def test_livegate_has_no_bypass_flag(release):
    """우회 플래그 없음 — livegate subparser 어디에도 skip/force 축이 없다.

    "must-fix 잔여가 있으면 릴리즈하지 않는다"는 판정에 예외 입력이 생기면 그 예외가 곧 자의
    판정의 자리다(이번 사고의 원인). 실 등록면(subparser)의 플래그 인벤토리로 못박는다."""
    subparsers = next(
        action for action in release.module.build_parser()._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    options = {opt for action in subparsers.choices["livegate"]._actions
               for opt in action.option_strings}
    assert options, "livegate subparser 옵션을 읽지 못했다 (인벤토리 가드가 무의미해진다)"
    assert not [opt for opt in options
                if any(word in opt for word in ("skip", "force", "bypass", "allow"))], options


# ── ③ push 보호훅 — 라이브 축 우회가 이 차단까지 열지 않는다 ───────────────


def _install_hook_fixture(
    tmp_path: Path, *, status: str, reason: str | None,
    marker_content: str | None = "clear\n",
) -> Path:
    """보호 pre-push 훅 + sidecar(보호목록·release 계약·engine-root) 를 tmp 에 깐다.

    livegate.json 은 `status`/`reason` 그대로 만든다 — 우회 거부 판정의 입력이 **사유 표식**이지
    fail 여부가 아님을 테스트가 직접 태울 수 있게 둘을 분리한다.
    훅 본문은 출하 상수(`worktree_pool._PROTECTED_PRE_PUSH_HOOK`)를 그대로 쓴다.
    """
    pool = _load("worktree_pool", f"must_fix_pool_{status}_{reason or 'none'}")
    hook_dir = tmp_path / "hooks"
    hook_dir.mkdir()
    (hook_dir / "pre-push").write_text(pool._PROTECTED_PRE_PUSH_HOOK, encoding="utf-8")
    (hook_dir / "protected").write_text("main\n", encoding="utf-8")
    (hook_dir / "gate-contract").write_text("release\n\n", encoding="utf-8")
    engine_root = tmp_path / "pmhome"
    local = engine_root / ".project_manager" / ".local"
    local.mkdir(parents=True)
    record = {"head": "abc123", "status": status, "n": 0, "rc": None}
    if reason:
        record["reason"] = reason
    (local / "livegate.json").write_text(json.dumps(record), encoding="utf-8")
    if marker_content is not None:
        (local / "release-must-fix").write_text(marker_content, encoding="ascii")
    (hook_dir / "engine-root").write_text(f"{engine_root}\n", encoding="utf-8")
    return hook_dir / "pre-push"


def _run_hook(hook: Path):
    import subprocess
    env = {**os.environ, "PM_ALLOW_PROTECTED_PUSH": "1", "PM_SKIP_LIVE_GATE": "1"}
    env.pop("PM_SKIP_SELF_TEST", None)
    return subprocess.run(["sh", str(hook)], input="refs/heads/local 0000 refs/heads/main 1111\n",
                          capture_output=True, text=True, env=env)


@pytest.mark.skipif(shutil.which("sh") is None, reason="POSIX sh 부재(훅 직접 실행 불가)")
def test_skip_live_gate_env_does_not_bypass_must_fix_block(tmp_path):
    """`PM_SKIP_LIVE_GATE=1` 은 라이브 축 우회다 — must-fix 잔여로 찍힌 fail 은 우회하지 못한다.

    우회 사유(오프라인·라이브 무관 변경·긴급 hotfix)와 이 차단 사유(리뷰 잔여)는 다른 축이다.
    훅은 fail 기록의 사유 표식을 읽어 그 한 사유만 거부한다(다른 사유의 우회는 현행 유지)."""
    result = _run_hook(_install_hook_fixture(
        tmp_path, status="fail", reason="unresolved-must-fix", marker_content="blocked\n"))
    assert result.returncode != 0
    assert "미해소 must-fix 잔여" in result.stderr
    assert "우회 대상이 아니다" in result.stderr


@pytest.mark.skipif(shutil.which("sh") is None, reason="POSIX sh 부재(훅 직접 실행 불가)")
@pytest.mark.parametrize("status,reason", [
    ("pass", None),              # green 기록 — 종전 우회 경로 무변경
    ("fail", "release-red"),     # **fail 이지만 다른 사유** — 라이브 축 red 는 우회 대상 그대로
])
def test_skip_live_gate_env_still_bypasses_other_reasons(tmp_path, status, reason):
    """다른 사유의 우회는 현행 유지 — 이 변경은 **한 사유만** 좁게 닫는다.

    판정 입력이 "fail 인가"가 아니라 "사유가 미해소 must-fix 인가"임을 못박는다 — 전자로 넓히면
    라이브 red·인프라 실패까지 우회가 막혀 긴급 hotfix 경로가 사라진다."""
    assert _run_hook(_install_hook_fixture(
        tmp_path, status=status, reason=reason)).returncode == 0


@pytest.mark.skipif(shutil.which("sh") is None, reason="POSIX sh 부재(훅 직접 실행 불가)")
@pytest.mark.parametrize("marker_content", [None, "", "clear", "clear\nextra\n", "unknown\n"])
def test_skip_live_gate_marker_absent_or_malformed_is_fail_closed(tmp_path, marker_content):
    """표식 부재·부분행·추가행·미지값은 잔여 없음이 아니라 판정 불가라 우회를 거부한다."""
    result = _run_hook(_install_hook_fixture(
        tmp_path, status="pass", reason=None, marker_content=marker_content))
    assert result.returncode != 0
    assert "잔여 판정 표식" in result.stderr


@pytest.mark.skipif(shutil.which("sh") is None, reason="POSIX sh 부재(훅 직접 실행 불가)")
def test_green_then_new_rejection_writer_closes_skip_marker(tmp_path, monkeypatch):
    """옛 green 뒤 신규 반려가 오면 장부 writer가 blocked를 먼저 써 우회도 즉시 닫는다."""
    hook = _install_hook_fixture(tmp_path, status="pass", reason=None)
    assert _run_hook(hook).returncode == 0
    engine_root = Path((hook.parent / "engine-root").read_text(encoding="utf-8").strip())
    review = _load("external_review", "must_fix_marker_writer")
    monkeypatch.setattr(review, "REPO", engine_root)
    with review._round_ledger_lock():
        review._save_round_ledger({"T-0610": _rejected_gate()})

    assert (engine_root / ".project_manager" / ".local" / "release-must-fix").read_text(
        encoding="ascii") == "blocked\n"
    result = _run_hook(hook)
    assert result.returncode != 0
    assert "미해소 must-fix 잔여" in result.stderr


# ── ④ 두 도구가 같은 장부를 본다 (선언 → 릴리즈) ───────────────────────────


def test_declaration_then_release_uses_one_ledger(tmp_path, monkeypatch, capsys):
    """처분 선언(external_review)이 쓴 장부를 릴리즈 게이트(board)가 그대로 읽는다.

    두 도구가 서로 다른 규칙으로 같은 값을 읽으면 "선언했는데 여전히 막힌다"(또는 그 반대)가
    난다 — 해석은 board 의 공용 seam 한 곳이 소유한다."""
    proj = _make_project(tmp_path)
    _add_ticket(proj, "T-0620", "done")
    _write_ledger(proj, {"T-0610": {"count": 3, "rounds": list(_REJECTED_ROUNDS)}})

    review = _load("external_review", "must_fix_e2e_review")
    monkeypatch.setattr(review, "REPO", proj)
    board = _load("board", "must_fix_e2e_board")
    local = proj / ".project_manager" / ".local"
    monkeypatch.setattr(board, "REPO", proj)
    monkeypatch.setattr(board, "LOCAL_DIR", local)
    monkeypatch.setattr(board, "LIVEGATE_FLAG", local / "livegate.json")
    monkeypatch.setattr(board, "LEASES_FILE", local / "worktree-leases.json")
    monkeypatch.setattr(board, "_git_config_get", lambda cwd, key: None)
    monkeypatch.setattr(board, "_git_head_at", lambda cwd: "0badc0de" * 5)
    monkeypatch.setattr(board.subprocess, "run", _FakeRun(
        0, f"{board.LIVEGATE_RELEASE_PIN} passed, 800 deselected in 40.0s"))
    record = argparse.Namespace(action="record", rev=None, cwd=str(proj),
                                repo=None, slot=None, task=None)

    assert board.cmd_livegate(record) == 1                      # 미처분 → 차단
    assert review.main(["--resolve-gate", "T-0610", "--into", "T-0620"]) == 0
    assert (local / "release-must-fix").read_text(encoding="ascii") == "clear\n"
    assert board.cmd_livegate(record) == 0                      # done 이관 → 통과
    assert board._unresolved_must_fix_gates(_ledger_path(proj)) == []
    capsys.readouterr()
