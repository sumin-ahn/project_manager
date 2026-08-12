"""pm_handoff 세션-차수 추론 · 대화형 skeleton · ctx 임계 config 단위 테스트.

엔진 canonical(루트 .project_manager/tools/pm_handoff.py·board.py)을 importlib 로
직접 검증한다. 무거운 외부 호출 없이 순수 로직 + 격리된 tmp 파일만 본다.

비대화 `--trigger` auto-skeleton machinery 는 폐기됐다(ADR-0038 D1·T-0186) — rich 본문은
nudge tier(ADR-0037)가 담당하므로 얇은 자동 skeleton 박제 경로가 죽은 채널이 됐다. 이
파일은 그 폐기 후 살아남는 축만 검증한다:
  - session-num 추론 (pm_state 세션 window 다음 차수·infer_next_session_num).
  - 대화형 handoff skeleton (박제 entries 자동 목록·메타 학습·pending intent·회귀 1줄).
  - ctx 임계 config 기본값(30/20·T-0207 상향) + reader + board.py init 기록.
  - 대화형 run() 경로 회귀 불변 (인계 프롬프트 stdout·log 미오염·프롬프트 템플릿 lean).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from _pytest_summary import pytest_summary

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


# ── 1. 세션-차수 자동 추론 헬퍼 (대화형 --session-num 대체 seam) ────────────────

def test_infer_next_session_num(handoff):
    assert handoff.infer_next_session_num(_PM_STATE_FIXTURE) == 5
    # entry 없음 → placeholder.
    assert handoff.infer_next_session_num(_PM_STATE_NO_ENTRY) == handoff.TRIGGER_SESSION_PLACEHOLDER
    # 앵커 자체 없음 → placeholder.
    assert handoff.infer_next_session_num("# no section") == handoff.TRIGGER_SESSION_PLACEHOLDER


# ── 2. 대화형 skeleton — 박제 entries 자동 목록 + 신 스키마 ───────────────────

def test_interactive_skeleton_lists_session_entries(handoff):
    """대화형 skeleton 이 직전 솔로 handoff 뒤의 박제 entry 헤더를 자동 목록화한다."""
    log_text = """\
## [2026-06-13] handoff | PM 8차 → 다음 PM 세션

## [2026-06-14] complete | T-0547 — 로그 entry 박제
본문은 목록에 복제하지 않는다.

## [2026-06-14] checkpoint | compaction 전
"""
    sk = handoff.build_handoff_log_skeleton(
        session_num=9, date="2026-06-14", log_text=log_text,
    )
    assert "- 이 세션 박제 entries:\n" in sk
    assert "  - ## [2026-06-14] complete | T-0547 — 로그 entry 박제" in sk
    assert "  - ## [2026-06-14] checkpoint | compaction 전" in sk
    assert "본문은 목록에 복제하지 않는다." not in sk


def test_interactive_skeleton_marks_no_session_entries(handoff):
    """수집할 박제 entry가 없으면 빈 자동 목록을 명시하고 pending intent는 손-채움으로 둔다."""
    sk = handoff.build_handoff_log_skeleton(session_num=9, date="2026-06-14")
    assert "- 이 세션 박제 entries: (이 세션 박제 entry 없음)" in sk
    assert f"- pending user intent: {handoff.PENDING_INTENT_PLACEHOLDER}" in sk


def test_interactive_skeleton_task_entries_are_scoped(handoff):
    """task handoff는 직전 자기 경계 뒤 같은 task의 complete/checkpoint 헤더만 목록화한다."""
    log_text = """\
## [2026-06-13] handoff | PM 3차 (task:alpha) → 다음 PM 세션
## [2026-06-14] complete | T-0548 — 다른 task (task:beta)
## [2026-06-14] complete | T-0549 — 자기 완료 (task:alpha)
## [2026-06-14] checkpoint | (task:alpha) — compaction
"""
    sk = handoff.build_handoff_log_skeleton(
        session_num=4, date="2026-06-14", log_text=log_text, task="alpha",
    )
    assert "T-0549 — 자기 완료 (task:alpha)" in sk
    assert "checkpoint | (task:alpha) — compaction" in sk
    assert "T-0548 — 다른 task (task:beta)" not in sk


def test_thread_tail_adapter_interface_is_absent(handoff):
    """폐기된 thread-tail 추출 경로가 API·CLI·헬퍼/상수 표면에 남지 않는다."""
    import inspect

    assert "thread_tail" not in inspect.signature(handoff.build_handoff_log_skeleton).parameters
    assert "--thread-tail" not in handoff.build_parser()._option_string_actions
    for name in ("_flatten_thread_tail", "_next_intent_lines",
                 "THREAD_TAIL_PLACEHOLDER", "THREAD_TAIL_MAX_CHARS"):
        assert not hasattr(handoff, name)


# ── 3. ctx 임계 config (board.py reader + init 기본값) ─────────────────────────

def test_ctx_threshold_defaults(board, monkeypatch):
    """local.conf 에 ctx 키가 없으면 엔진 기본(30/20 — T-0207 상향)."""
    monkeypatch.setattr(board, "local_config", lambda: {})
    th = board.ctx_thresholds()
    assert th == {"nudge_pct": 30, "stop_pct": 20}
    assert board.CTX_NUDGE_PCT_DEFAULT == 30
    assert board.CTX_STOP_PCT_DEFAULT == 20


def test_ctx_threshold_reads_local_conf(board, monkeypatch):
    monkeypatch.setattr(board, "local_config", lambda: {"ctx_nudge_pct": "30", "ctx_stop_pct": "5"})
    assert board.ctx_thresholds() == {"nudge_pct": 30, "stop_pct": 5}


def test_ctx_threshold_invalid_falls_back(board, monkeypatch):
    """비정수 값은 무시하고 기본으로 fallback (config 오타에 robust)."""
    monkeypatch.setattr(board, "local_config", lambda: {"ctx_nudge_pct": "abc"})
    th = board.ctx_thresholds()
    assert th["nudge_pct"] == 30 and th["stop_pct"] == 20


def test_board_init_writes_ctx_defaults(board, tmp_path, monkeypatch):
    """board.py init 가 local.conf 에 ctx_nudge_pct=30·ctx_stop_pct=20 을 기록한다 (T-0207)."""
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
    assert "ctx_nudge_pct=30" in conf_text
    assert "ctx_stop_pct=20" in conf_text


# ── 4. 대화형 경로 회귀 불변 (기존 동작 보존) ──────────────────────────────────

def test_interactive_skeleton_lean_schema(handoff):
    """대화형 handoff skeleton은 자동 목록 + 손-채움 3줄의 신 스키마이고 구 필드는 없다."""
    sk = handoff.build_handoff_log_skeleton(session_num=9, date="2026-06-14")
    assert sk.startswith("## [2026-06-14] handoff | PM 9차 → 다음 PM 세션")
    assert "- 이 세션 박제 entries:" in sk
    assert "- 메타 학습:" in sk
    assert "- pending user intent:" in sk
    assert "- 회귀/incident:" in sk
    assert "회귀 \"N passed / 상태\" 1줄" in sk
    assert "- 읽기 범위:" not in sk
    assert "대화 thread-tail" not in sk
    # 대화형 skeleton 은 비대화 트리거 마커를 갖지 않는다 (경로 분리·폐기 후에도 불변).
    assert "ctx-trigger" not in sk
    assert "reason=" not in sk


def test_interactive_main_requires_session_num(handoff):
    """--session-num/--wave-summary 누락 시 대화형 경로는 parser.error 로 종료한다."""
    with pytest.raises(SystemExit):
        handoff.main(["--no-pytest"])  # 둘 다 누락 → 종료.


def test_interactive_run_prompt_goes_to_stdout_not_log(handoff, tmp_path, capsys):
    """대화형 run() 불변 — 인계 프롬프트는 [5/7] stdout 로만 나가고 log entry 에 박제하지 않는다.

    run() 은 모델이 살아 있어 stdout 가 권위적이다 — log 파일은 인계 프롬프트 헤더를
    포함하지 않아야 한다(경로 분리·불변).
    """
    log_file = tmp_path / "current.md"
    state_file = tmp_path / "pm_state.md"
    playbook_file = tmp_path / "pm_playbook.md"
    log_file.write_text("# log\n", encoding="utf-8")
    state_file.write_text(_PM_STATE_FIXTURE, encoding="utf-8")
    playbook_file.write_text(_PM_PLAYBOOK_FIXTURE, encoding="utf-8")
    inst = handoff.PmHandoff(
        run_pytest_fn=lambda: (0, pytest_summary()),
        run_git_fn=lambda args: (0, ""),
        log_file=log_file,
        pm_state_file=state_file,
        pm_playbook_file=playbook_file,
    )
    rc = inst.run(session_num=5, wave_summary="ws", dry_run=False, skip_pytest=True, user_ack="solo")
    assert rc == 0
    out = capsys.readouterr().out
    # 인계 프롬프트는 stdout 으로 나간다 ([5/7]).
    assert "=== 인계 프롬프트 (PM 5차 → 다음 PM 세션) ===" in out
    # 그러나 run() 은 log 파일을 인계 프롬프트로 오염시키지 않는다 (경로 분리·불변).
    assert "=== 인계 프롬프트" not in log_file.read_text(encoding="utf-8")


def test_handoff_prompt_template_is_lean(handoff):
    """인계 프롬프트 템플릿(pm_playbook.md §부트스트랩 프롬프트)이 트리거(역할 framing +
    /pm-bootstrap)로 축소돼 있고, 옛 'board 상태 … 5~10개 불릿' 재열거 유도가 없다
    (T-0180 트리거화·ADR-0008 lean).

    lean handoff 스키마(박제 entries·메타 학습·pending intent·회귀/incident)의 거처는 *log entry
    skeleton*(`HANDOFF_LOG_SKELETON_TEMPLATE`·test_pm_bootstrap_lease 가 가드)이지 이 프롬프트가
    아니다 — 부트스트랩이 그 log entry 를 dump 하므로(T-0179) 프롬프트가 같은 라벨을 재기술하면
    중복 사족이고, 다음 PM 이 부트스트랩 실패 시에도 그 서술대로 수동 재구성·과잉 보고하게 유도한다.
    그래서 라벨 재기술의 *부재*를 가드한다(과거엔 반대로 존재를 강제해 사족을 박제했음).
    """
    text = handoff.PM_PLAYBOOK_FILE.read_text(encoding="utf-8")
    template = handoff.extract_handoff_prompt_template(text)
    assert template is not None, "프롬프트 템플릿 앵커/코드블록 추출 실패"
    # 공유 canonical은 실제 Claude/OpenCode 입력값인 slash 표기를 유지한다.
    assert "/pm-bootstrap" in template
    assert "당신은" not in template, "2인칭 역할문구 잔존 — bare(T-0193)와 모순"
    # 폐기: 인계 본문 라벨 재기술 사족 (부트스트랩 dump·log skeleton 이 단일 진실).
    for label in ("이 세션 박제 entries", "메타 학습", "pending user intent", "회귀/incident"):
        assert label not in template, f"프롬프트에 인계 라벨 '{label}' 재기술 사족 잔존 (트리거만·T-0180)"
    # 옛 재열거 유도 문구 부재 (ADR-0008 금지).
    assert "5~10개 불릿" not in template, "옛 '5~10개 불릿' 재열거 유도 잔존"
    assert "board 상태 / 진행 중 작업" not in template, "옛 'board 상태' 재열거 유도 잔존"


def test_no_stale_handoff_guidance_across_tree():
    """루트 + 템플릿(pm_update 동기화분 + 어댑터 미러)의 handoff 가이드 파일에 옛 verbose 문구가
    남지 않았는지 (ADR-0008·codex 게이트 회귀 방지). opencode의 skill 미러와
    canonical에서 생성한 command 사본을 모두 가드한다(T-0674)."""
    candidates = [
        REPO / ".project_manager/wiki/pm_playbook.md",
        REPO / ".claude/skills/pm-handoff/SKILL.md",
        REPO / "templates/claude_code/.project_manager/wiki/pm_playbook.md",
        REPO / "templates/claude_code/.claude/skills/pm-handoff/SKILL.md",
        REPO / "templates/opencode/.project_manager/wiki/pm_playbook.md",
        REPO / "templates/opencode/.claude/skills/pm-handoff/SKILL.md",
        REPO / "templates/opencode/.opencode/command/pm-handoff.md",
    ]
    stale_phrases = ("5~10 불릿", "5~10개 불릿", "board 상태 / 진행 중 작업", "board 상태·진행 중 작업")
    for path in candidates:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for phrase in stale_phrases:
            assert phrase not in text, f"{path}: 옛 handoff 문구 '{phrase}' 잔존 (ADR-0008)"
