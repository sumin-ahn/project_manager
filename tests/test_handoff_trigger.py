"""pm_handoff 비대화 트리거 (ctx 정지-핸드오프 — T-0013) 단위 테스트.

엔진 canonical(루트 .project_manager/tools/pm_handoff.py·board.py)을 importlib 로
직접 검증한다. 무거운 외부 호출 없이 순수 로직 + 격리된 tmp 파일만 본다.

검증 축:
  - 비대화 트리거가 사람 입력(session-num·wave-summary) 없이 동작.
  - log/current.md 에 handoff entry append + reason·ctx% 기록.
  - pm_state sliding window 정리 (정수 차수) / placeholder 시 스킵.
  - ctx 임계 config 기본값(20/10) + reader + board.py init 기록.
  - 대화형 경로 회귀 불변 (기존 run/skeleton 동작 보존).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def handoff():
    return _load("pm_handoff")


@pytest.fixture(scope="module")
def board():
    return _load("board")


# pm_state.md 세션 식별 절 최소 fixture (앵커·entry·포인터 형식 유지) ───────────
_PM_STATE_FIXTURE = """\
# PM State

## 세션 식별 (현재까지 사용된 이름)

최근 N 차 (sliding window, 기본 3 차):
  - **4차** (2026-06-14 · 직전 wave): 직전 PM 세션.
  - 이전 차 (PM 1차~3차) = `log/current.md` handoff entry 단일 진실.

## 진행 중인 의사결정
"""

# entry 가 없는 (포인터만 있는) pm_state — placeholder 추론 케이스 ───────────────
_PM_STATE_NO_ENTRY = """\
# PM State

## 세션 식별 (현재까지 사용된 이름)

최근 N 차 (sliding window, 기본 3 차):

## 진행 중인 의사결정
"""


def _make_handoff(handoff, tmp_path: Path, state_text: str):
    """격리된 tmp log·pm_state 파일을 주입한 PmHandoff 인스턴스를 만든다."""
    log_file = tmp_path / "current.md"
    state_file = tmp_path / "pm_state.md"
    log_file.write_text("# log\n", encoding="utf-8")
    state_file.write_text(state_text, encoding="utf-8")
    inst = handoff.PmHandoff(
        # subprocess 함수는 트리거 경로에서 호출되지 않아야 한다 — 호출 시 폭발.
        run_pytest_fn=lambda: (_ for _ in ()).throw(AssertionError("trigger 가 pytest 를 호출했다")),
        run_git_fn=lambda args: (_ for _ in ()).throw(AssertionError("trigger 가 git 을 호출했다")),
        log_file=log_file,
        pm_state_file=state_file,
    )
    return inst, log_file, state_file


# ── 1. 비대화 트리거: 입력 0 · entry append · reason/ctx 기록 ──────────────────

def test_trigger_appends_entry_with_reason_and_ctx(handoff, tmp_path):
    inst, log_file, state_file = _make_handoff(handoff, tmp_path, _PM_STATE_FIXTURE)

    # 사람 입력(session-num·wave-summary) 없이 호출.
    rc = inst.run_trigger(reason="ctx-stop", ctx_pct=8)

    assert rc == 0
    log_text = log_file.read_text(encoding="utf-8")
    # handoff entry 가 append 됐다.
    assert "handoff (ctx-trigger)" in log_text
    # reason·ctx% 가 권위 상태로 기록됐다.
    assert "reason=ctx-stop" in log_text
    assert "ctx=8%" in log_text
    # session-num 은 자동 추론 (4차 다음 → 5차).
    assert "PM 5차" in log_text


def test_trigger_skips_regression_and_git(handoff, tmp_path):
    """트리거 빠른 경로는 pytest·git 을 호출하지 않는다 (DI 함수가 폭발하면 실패)."""
    inst, _, _ = _make_handoff(handoff, tmp_path, _PM_STATE_FIXTURE)
    # run_pytest_fn / run_git_fn 이 호출되면 AssertionError → rc 0 도달 불가.
    assert inst.run_trigger(reason="ctx-stop", ctx_pct=10) == 0


def test_trigger_updates_session_window(handoff, tmp_path):
    inst, _, state_file = _make_handoff(handoff, tmp_path, _PM_STATE_FIXTURE)
    inst.run_trigger(reason="ctx-stop", ctx_pct=9)
    state_text = state_file.read_text(encoding="utf-8")
    # 추론된 5차가 세션 window 에 추가됐다.
    assert "**5차**" in state_text


def test_trigger_default_reason(handoff, tmp_path):
    """--reason 미지정 시 엔진 기본(ctx-stop)을 기록한다."""
    inst, log_file, _ = _make_handoff(handoff, tmp_path, _PM_STATE_FIXTURE)
    inst.run_trigger()  # 인자 전무.
    assert f"reason={handoff.TRIGGER_DEFAULT_REASON}" in log_file.read_text(encoding="utf-8")


def test_trigger_placeholder_when_no_entry(handoff, tmp_path):
    """세션 entry 가 없으면 placeholder 로 추론하고 sliding window 정리는 스킵 (entry 는 append)."""
    inst, log_file, state_file = _make_handoff(handoff, tmp_path, _PM_STATE_NO_ENTRY)
    rc = inst.run_trigger(reason="ctx-stop", ctx_pct=5)
    assert rc == 0
    # entry 는 placeholder 차수로 append 된다.
    log_text = log_file.read_text(encoding="utf-8")
    assert f"PM {handoff.TRIGGER_SESSION_PLACEHOLDER}차" in log_text
    # pm_state 는 안전하게 불변 (placeholder 차수로 sliding window 편집 안 함).
    assert state_file.read_text(encoding="utf-8") == _PM_STATE_NO_ENTRY


def test_trigger_dry_run_no_file_edit(handoff, tmp_path):
    inst, log_file, state_file = _make_handoff(handoff, tmp_path, _PM_STATE_FIXTURE)
    before_log = log_file.read_text(encoding="utf-8")
    before_state = state_file.read_text(encoding="utf-8")
    assert inst.run_trigger(reason="ctx-stop", ctx_pct=7, dry_run=True) == 0
    # dry-run 은 파일을 건드리지 않는다.
    assert log_file.read_text(encoding="utf-8") == before_log
    assert state_file.read_text(encoding="utf-8") == before_state


# ── 1b. 인계 프롬프트 박제 (D16·T-0134) ───────────────────────────────────────

# pm_playbook.md §부트스트랩 프롬프트 코드블록 최소 fixture (앵커·코드블록 형식 유지) ──
# T-0180 — 프롬프트는 트리거로 축소(역할 framing + /pm-bootstrap). 인계 본문(읽기 범위·
# 메타 학습·다음 intent·회귀/incident) 손-채움 블록은 폐기 — 부트스트랩이 log entry 에서 dump.
_PM_PLAYBOOK_FIXTURE = """\
# PM Playbook

## 다음 PM 세션 부트스트랩 프롬프트 (템플릿)

```
당신은 이 프로젝트의 PM 세션입니다.
지금 /pm-bootstrap 을 실행하세요.
```

## 다음 절
"""


def _make_handoff_with_playbook(handoff, tmp_path: Path, state_text: str, playbook_text: str | None):
    """log·pm_state 에 더해 pm_playbook 파일을 명시 주입한 PmHandoff 인스턴스.

    playbook_text=None 이면 pm_playbook 파일을 *생성하지 않아* fail-soft 경로를 친다.
    """
    log_file = tmp_path / "current.md"
    state_file = tmp_path / "pm_state.md"
    playbook_file = tmp_path / "pm_playbook.md"
    log_file.write_text("# log\n", encoding="utf-8")
    state_file.write_text(state_text, encoding="utf-8")
    if playbook_text is not None:
        playbook_file.write_text(playbook_text, encoding="utf-8")
    inst = handoff.PmHandoff(
        run_pytest_fn=lambda: (_ for _ in ()).throw(AssertionError("trigger 가 pytest 를 호출했다")),
        run_git_fn=lambda args: (_ for _ in ()).throw(AssertionError("trigger 가 git 을 호출했다")),
        log_file=log_file,
        pm_state_file=state_file,
        pm_playbook_file=playbook_file,
    )
    return inst, log_file, state_file


def test_trigger_embeds_handoff_prompt_in_log(handoff, tmp_path):
    """run_trigger 가 log handoff entry 끝에 다음 세션용 인계 프롬프트를 박제한다 (D16)."""
    inst, log_file, _ = _make_handoff_with_playbook(
        handoff, tmp_path, _PM_STATE_FIXTURE, _PM_PLAYBOOK_FIXTURE,
    )
    rc = inst.run_trigger(reason="ctx-stop", ctx_pct=8)
    assert rc == 0
    log_text = log_file.read_text(encoding="utf-8")
    # skeleton handoff entry 가 여전히 append 된다 (회귀).
    assert "handoff (ctx-trigger)" in log_text
    # 인계 프롬프트 헤더(자동 추론 5차)가 log 에 박제됐다 — stdout 휘발 아님.
    assert "=== 인계 프롬프트 (PM 5차 → 다음 PM 세션) ===" in log_text
    # pm_playbook 템플릿 코드블록 본문도 entry 안에 들어왔다.
    assert "당신은 이 프로젝트의 PM 세션입니다." in log_text
    # 박제 위치가 skeleton entry *뒤* 다 (인계 프롬프트가 handoff 헤더보다 나중).
    assert log_text.index("handoff (ctx-trigger)") < log_text.index("=== 인계 프롬프트")


def test_trigger_prompt_block_uses_shared_builder(handoff, tmp_path):
    """박제된 블록이 build_handoff_prompt_output 산출과 동일하다 (run() 과 동일 seam 재사용)."""
    inst, log_file, _ = _make_handoff_with_playbook(
        handoff, tmp_path, _PM_STATE_FIXTURE, _PM_PLAYBOOK_FIXTURE,
    )
    inst.run_trigger(reason="ctx-stop", ctx_pct=8)
    log_text = log_file.read_text(encoding="utf-8")
    # run() 의 [5/7] 가 쓰는 동일 builder 로 기대 출력을 만든다.
    expected = handoff.build_handoff_prompt_output(
        pm_playbook_text=_PM_PLAYBOOK_FIXTURE,
        session_num=5,
        wave_summary=handoff.build_trigger_wave_summary(reason="ctx-stop", ctx_pct=8),
        date_str=__import__("datetime").date.today().isoformat(),
    )
    assert expected in log_text


def test_trigger_prompt_failsoft_when_no_playbook(handoff, tmp_path):
    """pm_playbook.md 부재 시 fail-soft — 한 줄 안내만 남기고 trigger handoff 는 계속한다."""
    inst, log_file, _ = _make_handoff_with_playbook(
        handoff, tmp_path, _PM_STATE_FIXTURE, playbook_text=None,
    )
    rc = inst.run_trigger(reason="ctx-stop", ctx_pct=8)
    assert rc == 0
    log_text = log_file.read_text(encoding="utf-8")
    # skeleton 은 여전히 박제된다 (인계 프롬프트만 graceful skip).
    assert "handoff (ctx-trigger)" in log_text
    # fail-soft 안내 문구가 entry 에 남는다.
    assert "pm_playbook.md 없음" in log_text


def test_trigger_prompt_dry_run_no_file_edit(handoff, tmp_path):
    """dry-run 은 인계 프롬프트 박제 분기도 파일을 건드리지 않는다."""
    inst, log_file, state_file = _make_handoff_with_playbook(
        handoff, tmp_path, _PM_STATE_FIXTURE, _PM_PLAYBOOK_FIXTURE,
    )
    before_log = log_file.read_text(encoding="utf-8")
    before_state = state_file.read_text(encoding="utf-8")
    assert inst.run_trigger(reason="ctx-stop", ctx_pct=7, dry_run=True) == 0
    assert log_file.read_text(encoding="utf-8") == before_log
    assert state_file.read_text(encoding="utf-8") == before_state


# ── 2. 자동 채움 헬퍼 단위 ────────────────────────────────────────────────────

def test_infer_next_session_num(handoff):
    assert handoff.infer_next_session_num(_PM_STATE_FIXTURE) == 5
    # entry 없음 → placeholder.
    assert handoff.infer_next_session_num(_PM_STATE_NO_ENTRY) == handoff.TRIGGER_SESSION_PLACEHOLDER
    # 앵커 자체 없음 → placeholder.
    assert handoff.infer_next_session_num("# no section") == handoff.TRIGGER_SESSION_PLACEHOLDER


def test_build_trigger_wave_summary_shape(handoff):
    summary = handoff.build_trigger_wave_summary(reason="ctx-stop", ctx_pct=12)
    assert "reason=ctx-stop" in summary and "ctx=12%" in summary
    assert "board done" in summary  # board 현황 1줄 포함.
    # ctx_pct None 이면 ctx 표기 생략.
    assert "ctx=" not in handoff.build_trigger_wave_summary(reason="manual", ctx_pct=None)


def test_board_status_counts_keys(handoff):
    counts = handoff.board_status_counts()
    assert set(counts) == {"open", "claimed", "blocked", "done"}
    assert all(isinstance(v, int) for v in counts.values())


def test_trigger_handoff_skeleton_records_reason_ctx(handoff):
    sk = handoff.build_trigger_handoff_log_skeleton(
        session_num=7, reason="ctx-stop", ctx_pct=6, date="2026-06-14"
    )
    assert "## [2026-06-14] handoff (ctx-trigger) | PM 7차" in sk
    # reason·ctx% 권위 기록은 보존된다 (트리거 경로 고유).
    assert "reason=ctx-stop" in sk and "ctx=6%" in sk
    # trigger 경로 skeleton 도 lean 3섹션 + 옵션 (ADR-0008) 으로 동일 정비됐다.
    assert "- 읽기 범위:" in sk
    assert "- 메타 학습:" in sk
    # "다음 intent" 는 두 줄로 세분됐다 (ADR-0008 재검토 트리거·T-0047).
    assert "- 대화 thread-tail:" in sk
    assert "- pending user intent:" in sk
    assert "- 회귀/incident:" in sk
    # 미전달 시 thread-tail placeholder 유지 (하위호환).
    assert handoff.THREAD_TAIL_PLACEHOLDER in sk


def test_trigger_skeleton_precompact_marker_distinct(handoff):
    """reason=precompact 는 ctx-STOP 회전과 *구별되는* marker 로 렌더된다 (ADR-0020).

    네이티브 압축 폴백(precompact)이 ctx-임계 회전과 헤더·트리거 서술이 같으면
    다음 세션이 "수동 handoff 미완 가능"을 구분 못 한다 (codex must-fix·게이트 상보성).
    """
    sk = handoff.build_trigger_handoff_log_skeleton(
        session_num=7, reason="precompact", ctx_pct=None, date="2026-06-14"
    )
    # 헤더 라벨이 precompact-flush — ctx-trigger 아님.
    assert "## [2026-06-14] handoff (precompact-flush) | PM 7차" in sk
    assert "handoff (ctx-trigger)" not in sk
    # ⚠ "수동 handoff 미완 가능" durable 신호 + reason 권위 기록.
    assert "⚠" in sk and "수동 handoff 미완 가능" in sk
    assert "reason=precompact" in sk
    # default(ctx-STOP) 라벨/서술은 보존됨 — precompact 만 분기.
    default_sk = handoff.build_trigger_handoff_log_skeleton(
        session_num=7, reason="ctx-stop", ctx_pct=6, date="2026-06-14"
    )
    assert "handoff (ctx-trigger)" in default_sk
    assert "ctx 임계 자동 핸드오프" in default_sk


def test_trigger_skeleton_thread_tail_injected(handoff):
    """thread_tail 주입 시 대화 thread-tail 슬롯에 텍스트가 삽입된다 (placeholder 대체)."""
    sk = handoff.build_trigger_handoff_log_skeleton(
        session_num=7, reason="ctx-stop", ctx_pct=6, date="2026-06-14",
        thread_tail="relay v1 e2e 검증 중 — sid==marker 확인 요청",
    )
    assert "- 대화 thread-tail: relay v1 e2e 검증 중" in sk
    # 자동 슬롯이 채워지면 placeholder 는 사라진다.
    assert handoff.THREAD_TAIL_PLACEHOLDER not in sk
    # pending user intent 는 여전히 PM 손 placeholder.
    assert "- pending user intent:" in sk


def test_interactive_skeleton_thread_tail_injected(handoff):
    """대화형 skeleton 도 thread_tail 수용 (양 skeleton 동일 seam)."""
    sk = handoff.build_handoff_log_skeleton(
        session_num=9, date="2026-06-14", thread_tail="다음은 opencode 추출 2차로",
    )
    assert "- 대화 thread-tail: 다음은 opencode 추출 2차로" in sk
    assert handoff.THREAD_TAIL_PLACEHOLDER not in sk


def test_skeletons_thread_tail_none_keeps_placeholder(handoff):
    """thread_tail 미전달(None) 시 양 skeleton 모두 placeholder 불변 (하위호환)."""
    intr = handoff.build_handoff_log_skeleton(session_num=9, date="2026-06-14")
    trig = handoff.build_trigger_handoff_log_skeleton(
        session_num=7, reason="ctx-stop", ctx_pct=6, date="2026-06-14"
    )
    assert handoff.THREAD_TAIL_PLACEHOLDER in intr
    assert handoff.THREAD_TAIL_PLACEHOLDER in trig


def test_thread_tail_multiline_flattened_no_forged_section(handoff):
    """엔진 방어 — 다중행 thread_tail(공개 CLI 입력)이 후속 섹션을 위조하지 못한다.

    `--thread-tail` 은 공개 인터페이스라 개행 포함 입력으로 `- 회귀/incident:` 같은
    줄을 위조하거나 lean 줄단위 스키마를 깰 수 있다. 엔진이 splitlines 평탄화·trim 으로
    자기 계약을 직접 방어한다 (어댑터 평탄화와 무관·defense-in-depth·codex T-0047).
    """
    forged = "정상 발화\n- 회귀/incident: FORGED green\n- pending user intent: FORGED"
    sk = handoff.build_trigger_handoff_log_skeleton(
        session_num=7, reason="ctx-stop", ctx_pct=6, date="2026-06-14", thread_tail=forged,
    )
    # 대화 thread-tail 슬롯은 정확히 한 줄 — 위조 섹션이 별도 줄로 새지 않는다.
    tail_lines = [ln for ln in sk.splitlines() if ln.startswith("- 대화 thread-tail:")]
    assert len(tail_lines) == 1
    assert "FORGED green" in tail_lines[0]  # 평탄화돼 한 줄 안에 흡수.
    assert " / " in tail_lines[0]  # 개행이 구분자로 평탄화.
    # 위조가 진짜 후속 섹션 줄을 만들지 못했다 — 정규 섹션은 정확히 1개씩.
    assert sk.count("\n- 회귀/incident:") == 1
    assert sk.count("\n- pending user intent:") == 1


def test_thread_tail_capped_at_engine_limit(handoff):
    """엔진 cap — 거대 단일행 thread_tail 도 THREAD_TAIL_MAX_CHARS 로 잘린다 (… 마커)."""
    sk = handoff.build_trigger_handoff_log_skeleton(
        session_num=7, reason="ctx-stop", ctx_pct=6, date="2026-06-14",
        thread_tail="가" * 2000,
    )
    tail_line = next(ln for ln in sk.splitlines() if ln.startswith("- 대화 thread-tail:"))
    payload = tail_line[len("- 대화 thread-tail: "):]
    assert len(payload) <= handoff.THREAD_TAIL_MAX_CHARS
    assert payload.endswith("…")


def test_run_trigger_injects_thread_tail(handoff, tmp_path):
    """run_trigger(thread_tail="X") 가 log entry 대화 thread-tail 슬롯에 X 를 삽입한다."""
    inst, log_file, _ = _make_handoff(handoff, tmp_path, _PM_STATE_FIXTURE)
    rc = inst.run_trigger(reason="ctx-stop", ctx_pct=8, thread_tail="정지 직전 사용자 발화 샘플")
    assert rc == 0
    log_text = log_file.read_text(encoding="utf-8")
    assert "- 대화 thread-tail: 정지 직전 사용자 발화 샘플" in log_text
    assert handoff.THREAD_TAIL_PLACEHOLDER not in log_text


def test_run_trigger_no_thread_tail_keeps_placeholder(handoff, tmp_path):
    """run_trigger 가 thread_tail 미전달이면 placeholder 불변 (하위호환)."""
    inst, log_file, _ = _make_handoff(handoff, tmp_path, _PM_STATE_FIXTURE)
    assert inst.run_trigger(reason="ctx-stop", ctx_pct=8) == 0
    log_text = log_file.read_text(encoding="utf-8")
    assert handoff.THREAD_TAIL_PLACEHOLDER in log_text


def test_parser_thread_tail_flag(handoff):
    """parser 에 --thread-tail 이 존재하고 기본 None (--trigger 전용·옵션)."""
    parser = handoff.build_parser()
    ns = parser.parse_args(["--trigger", "--thread-tail", "방금 작업 중"])
    assert ns.thread_tail == "방금 작업 중"
    ns2 = parser.parse_args(["--trigger"])
    assert ns2.thread_tail is None


# ── 3. ctx 임계 config (board.py reader + init 기본값) ─────────────────────────

def test_ctx_threshold_defaults(board, monkeypatch):
    """local.conf 에 ctx 키가 없으면 엔진 기본(20/10)."""
    monkeypatch.setattr(board, "local_config", lambda: {})
    th = board.ctx_thresholds()
    assert th == {"nudge_pct": 20, "stop_pct": 10}
    assert board.CTX_NUDGE_PCT_DEFAULT == 20
    assert board.CTX_STOP_PCT_DEFAULT == 10


def test_ctx_threshold_reads_local_conf(board, monkeypatch):
    monkeypatch.setattr(board, "local_config", lambda: {"ctx_nudge_pct": "30", "ctx_stop_pct": "5"})
    assert board.ctx_thresholds() == {"nudge_pct": 30, "stop_pct": 5}


def test_ctx_threshold_invalid_falls_back(board, monkeypatch):
    """비정수 값은 무시하고 기본으로 fallback (config 오타에 robust)."""
    monkeypatch.setattr(board, "local_config", lambda: {"ctx_nudge_pct": "abc"})
    th = board.ctx_thresholds()
    assert th["nudge_pct"] == 20 and th["stop_pct"] == 10


def test_board_init_writes_ctx_defaults(board, tmp_path, monkeypatch):
    """board.py init 가 local.conf 에 ctx_nudge_pct=20·ctx_stop_pct=10 을 기록한다."""
    conf_path = tmp_path / "local.conf"
    state_path = tmp_path / "pm_state.md"
    monkeypatch.setattr(board, "LOCAL_CONF", conf_path)
    monkeypatch.setattr(board, "PM_STATE_FILE", state_path)
    # pm_state 생성·pre-push 훅·external-review opt-in 부수효과를 격리한다.
    monkeypatch.setattr(board, "PM_STATE_TEMPLATE", tmp_path / "missing-template.md")
    monkeypatch.setattr(board, "install_pre_push_hook", lambda: False)
    monkeypatch.setattr(board, "prompt_external_review_optin", lambda: None)

    import argparse
    args = argparse.Namespace(prefix=None, area=None, owner=None, session="pm")
    rc = board.cmd_init(args)

    assert rc == 0
    conf_text = conf_path.read_text(encoding="utf-8")
    assert "ctx_nudge_pct=20" in conf_text
    assert "ctx_stop_pct=10" in conf_text


# ── 4. 대화형 경로 회귀 불변 (기존 동작 보존) ──────────────────────────────────

def test_interactive_skeleton_lean_schema(handoff):
    """대화형 handoff skeleton 은 lean 3섹션 + 옵션 스키마(ADR-0008)이고 경로 마커는 없다."""
    sk = handoff.build_handoff_log_skeleton(session_num=9, date="2026-06-14")
    assert sk.startswith("## [2026-06-14] handoff | PM 9차 → 다음 PM 세션")
    # lean 3섹션 + 옵션 (읽기범위·메타학습·다음intent[2줄세분] + 회귀/incident).
    assert "- 읽기 범위:" in sk
    assert "- 메타 학습:" in sk
    # "다음 intent" 세분 (ADR-0008·T-0047): 대화 thread-tail / pending user intent.
    assert "- 대화 thread-tail:" in sk
    assert "- pending user intent:" in sk
    assert "- 회귀/incident:" in sk
    # 대화형 skeleton 은 ctx-trigger 마커를 갖지 않는다 (경로 분리).
    assert "ctx-trigger" not in sk
    assert "reason=" not in sk


def test_interactive_main_still_requires_session_num(handoff):
    """--trigger 없이 --session-num/--wave-summary 누락 시 대화형 경로는 parser.error 로 종료한다."""
    with pytest.raises(SystemExit):
        handoff.main(["--no-pytest"])  # 둘 다 누락 → 종료.


def test_parser_trigger_flags_exist(handoff):
    """parser 에 --trigger·--reason·--ctx-pct 가 존재하고 기존 플래그도 보존된다."""
    parser = handoff.build_parser()
    ns = parser.parse_args(["--trigger", "--reason", "ctx-stop", "--ctx-pct", "9"])
    assert ns.trigger is True and ns.reason == "ctx-stop" and ns.ctx_pct == 9
    # 대화형 플래그도 여전히 파싱된다 (회귀).
    ns2 = parser.parse_args(["--session-num", "5", "--wave-summary", "x", "--no-pytest"])
    assert ns2.session_num == "5" and ns2.wave_summary == "x" and ns2.no_pytest is True


def test_interactive_run_prompt_goes_to_stdout_not_log(handoff, tmp_path, capsys):
    """대화형 run() 불변 — 인계 프롬프트는 [5/7] stdout 로만 나가고 log entry 에 박제하지 않는다.

    trigger 경로는 log 에 *박제*(durable)하지만 run() 은 모델이 살아 있어 stdout 가
    권위적이다. T-0134 의 trigger 박제가 run() 의 경로(stdout-only)를 오염시키지 않음을
    잠근다 — log 파일은 인계 프롬프트 헤더를 포함하지 않아야 한다.
    """
    log_file = tmp_path / "current.md"
    state_file = tmp_path / "pm_state.md"
    playbook_file = tmp_path / "pm_playbook.md"
    log_file.write_text("# log\n", encoding="utf-8")
    state_file.write_text(_PM_STATE_FIXTURE, encoding="utf-8")
    playbook_file.write_text(_PM_PLAYBOOK_FIXTURE, encoding="utf-8")
    inst = handoff.PmHandoff(
        # run() 은 git status dump 를 부른다 — 정상 stub (trigger 와 달리 폭발 아님).
        run_pytest_fn=lambda: (0, "1 passed"),
        run_git_fn=lambda args: (0, ""),
        log_file=log_file,
        pm_state_file=state_file,
        pm_playbook_file=playbook_file,
    )
    rc = inst.run(session_num=5, wave_summary="ws", dry_run=False, skip_pytest=True)
    assert rc == 0
    out = capsys.readouterr().out
    # 인계 프롬프트는 stdout 으로 나간다 ([5/7]).
    assert "=== 인계 프롬프트 (PM 5차 → 다음 PM 세션) ===" in out
    # 그러나 run() 은 log 파일을 인계 프롬프트로 오염시키지 않는다 (경로 분리·불변).
    assert "=== 인계 프롬프트" not in log_file.read_text(encoding="utf-8")


def test_handoff_prompt_template_is_lean(handoff):
    """인계 프롬프트 템플릿(pm_playbook.md §부트스트랩 프롬프트)이 lean handoff 스키마이고
    옛 'board 상태 … 5~10개 불릿' 재열거 유도 문구가 없다 (ADR-0008·codex 교차검증 게이트).

    skeleton 만 잠그면 pm_handoff 5단계 stdout 으로 다음 세션에 실제 전달되는 *프롬프트* 템플릿의
    회귀를 놓친다 — 실 pm_playbook.md 에서 추출해 직접 단언한다.
    """
    text = handoff.PM_PLAYBOOK_FILE.read_text(encoding="utf-8")
    template = handoff.extract_handoff_prompt_template(text)
    assert template is not None, "프롬프트 템플릿 앵커/코드블록 추출 실패"
    # lean 3 레이어 + 회귀/incident 1줄 baseline 존재 (ADR-0008 — 4 라벨 항상 유지).
    for layer in ("읽기 범위", "메타 학습", "다음 intent", "회귀/incident"):
        assert layer in template, f"lean 스키마 '{layer}' 누락"
    # 메타 학습은 드롭이 아니라 "없음" 으로 유지 (3 salient 레이어 보존 원칙).
    assert "없으면 생략" not in template, "메타 학습 '없으면 생략' — 필드 유지('없음')여야 함"
    # 옛 재열거 유도 문구 부재 (ADR-0008 금지).
    assert "5~10개 불릿" not in template, "옛 '5~10개 불릿' 재열거 유도 잔존"
    assert "board 상태 / 진행 중 작업" not in template, "옛 'board 상태' 재열거 유도 잔존"


def test_no_stale_handoff_guidance_across_tree():
    """루트 + 템플릿(pm_update 동기화분 + 어댑터 사본)의 handoff 가이드 파일에 옛 verbose 문구가
    남지 않았는지 (ADR-0008·codex 게이트 회귀 방지). 어댑터 사본(opencode command·SKILL)은
    pm_update 가 안 닿거나 별도라 명시 가드한다."""
    candidates = [
        REPO / ".project_manager/wiki/pm_playbook.md",
        REPO / ".claude/skills/pm-handoff/SKILL.md",
        REPO / "templates/claude_code/.project_manager/wiki/pm_playbook.md",
        REPO / "templates/claude_code/.claude/skills/pm-handoff/SKILL.md",
        REPO / "templates/opencode/.project_manager/wiki/pm_playbook.md",
        REPO / "templates/opencode/.opencode/command/pm-handoff.md",
    ]
    stale_phrases = ("5~10 불릿", "5~10개 불릿", "board 상태 / 진행 중 작업", "board 상태·진행 중 작업")
    for path in candidates:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for phrase in stale_phrases:
            assert phrase not in text, f"{path}: 옛 handoff 문구 '{phrase}' 잔존 (ADR-0008)"
