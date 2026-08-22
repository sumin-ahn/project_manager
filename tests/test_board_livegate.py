"""릴리즈 라이브 게이트 단위 테스트 — `board.py livegate record/check` (T-0221·ADR-0039 D2).

`livegate record` 는 `pytest -m release` 를 회귀와 동일한 cwd 해소로 실행하고 결과를
`.local/livegate.json` 에 기록한다(실행=기록). `livegate check --rev <sha>` 는 보호훅이
push HEAD 의 green 을 소비하는 채널이다. 여기선 실 pytest 를 절대 기동하지 않는다 —
`subprocess.run` 대역으로 rc·요약행(수집 N)을 주입하고, worktree HEAD 는 `_git_head_at`
대역으로 격리한다(라이브 실행 금지·전량 mock).

**hermetic 필수**: board.py 의 경로 전역(`REPO`·`LOCAL_DIR`·`LIVEGATE_FLAG`·`LEASES_FILE`)이
import 시점에 실 repo 절대경로로 굳는다 — tmp 프로젝트로 재지정해 실 루트를 건드리지 않는다
(test_board_multipm.py 의 hermetic 패턴 동류).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import types
from pathlib import Path

import pytest
from _home_slot import seed_home_slot

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"


def _load_board():
    """board.py 를 (패키지 아님) importlib 로 경로 로드 — test_board_multipm 와 동일."""
    spec = importlib.util.spec_from_file_location("board", TOOLS / "board.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def live_board(tmp_path, monkeypatch):
    """fresh board 모듈 + livegate IO 전역을 tmp 로 재지정한 hermetic 인스턴스.

    장부에는 **홈 N=1 슬롯 행**(`slot="."`)을 깐다 → `_active_slot_path` 가 홈 자신(=tmp proj)을
    낸다. 라이브 게이트 cwd 는 이제 "행이 없으니 이 트리겠지" 폴백이 아니라 행이 가리키는
    경로다(등록 안 된 홈은 fail-loud). worktree HEAD 는 `_git_head_at` 대역으로 고정.
    """
    proj = tmp_path / "proj"
    local = proj / ".project_manager" / ".local"
    seed_home_slot(proj)
    mod = _load_board()
    monkeypatch.setattr(mod, "REPO", proj)
    monkeypatch.setattr(mod, "LOCAL_DIR", local)
    monkeypatch.setattr(mod, "LIVEGATE_FLAG", local / "livegate.json")
    monkeypatch.setattr(mod, "LEASES_FILE", local / "worktree-leases.json")
    monkeypatch.setattr(mod, "_git_head_at", lambda cwd: "cafef00dcafef00d0011223344556677")
    mod._proj = proj
    return mod


class _FakeRun:
    """board.subprocess.run 대역 — 고정 (rc, stdout) 을 돌려주고 호출을 기록한다.

    pytest 자식을 실기동하지 않고 rc·요약행만 주입한다. `_git_head_at` 은 별도 대역이라 이
    대역은 record 의 pytest 호출만 받는다.
    """

    def __init__(self, rc: int, stdout: str = ""):
        self.rc = rc
        self.stdout = stdout
        self.calls: list[dict] = []

    def __call__(self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})
        return types.SimpleNamespace(returncode=self.rc, stdout=self.stdout, stderr="")


def _rec_args(cwd=None, repo=None, slot=None):
    return argparse.Namespace(action="record", rev=None, cwd=cwd, repo=repo, slot=slot)


def _chk_args(rev=None):
    return argparse.Namespace(action="check", rev=rev, cwd=None)


def _read_flag(live_board) -> dict:
    return json.loads(live_board.LIVEGATE_FLAG.read_text(encoding="utf-8"))


# ── _livegate_ran_count 파서 (수집 N 추출) ─────────────────────────────────

@pytest.mark.parametrize("output,expected", [
    ("7 passed, 812 deselected in 45.67s", 7),          # 정상 green wave
    ("5 passed, 814 deselected in 30.00s", 5),          # 마커 소실 → 수집 미달
    ("1 failed, 6 passed, 812 deselected in 40.0s", 7), # 실행 7·1 red
    ("2 errors, 5 passed, 812 deselected in 3.0s", 7),  # error 도 실행에 포함
    ("no tests ran in 0.01s", 0),                        # 수집 0 (exit5)
    ("", 0),                                             # 빈 출력
])
def test_livegate_ran_count_parses_summary(live_board, output, expected):
    """요약행에서 수집 N = passed + failed + error(s) (deselected 제외·수집 0 은 0)."""
    assert live_board._livegate_ran_count(output) == expected


# ── record ① rc0 ∧ N==pin → pass ───────────────────────────────────────────

def test_record_rc0_pin_match_records_pass(live_board, monkeypatch, capsys):
    """rc0 이고 수집 N==pin(22) → status='pass' 기록 + rc0 (정상 릴리즈 green)."""
    fake = _FakeRun(0, "22 passed, 810 deselected in 45.67s")
    monkeypatch.setattr(live_board.subprocess, "run", fake)
    rc = live_board.cmd_livegate(_rec_args())
    assert rc == 0
    data = _read_flag(live_board)
    assert data["n"] == live_board.LIVEGATE_RELEASE_PIN == 22
    assert data["status"] == "pass"
    assert data["rc"] == 0
    assert data["head"] == "cafef00dcafef00d0011223344556677"
    assert "ts" in data
    out = capsys.readouterr().out
    assert "pass @ cafef00d" in out
    assert "release 22/22 green" in out


# ── record ② rc0 ∧ N!=pin → fail (수집 위장 차단) ───────────────────────────

def test_record_rc0_pin_mismatch_records_fail(live_board, monkeypatch, capsys):
    """rc0 이지만 수집 N(5)!=pin(22) → status='fail' + rc1 (마커 소실 false-green 차단).

    rc0 만으로는 "적게 수집됐지만 red 아님"을 green 으로 삼킬 수 있다 — 수집 pin 이 그
    수집 위장을 red 로 세운다(T-0190/T-0220 원칙의 라이브 확장).
    """
    fake = _FakeRun(0, "5 passed, 814 deselected in 30.00s")
    monkeypatch.setattr(live_board.subprocess, "run", fake)
    rc = live_board.cmd_livegate(_rec_args())
    assert rc == 1, "수집 미달인데 pass(0)를 반환하면 위장 차단이 뚫린 것"
    data = _read_flag(live_board)
    assert data["status"] == "fail"
    assert data["n"] == 5
    assert data["rc"] == 0
    err = capsys.readouterr().err
    assert "수집 5 ≠ pin 22" in err
    assert "수집 위장 차단" in err


def test_record_rc5_no_tests_records_fail(live_board, monkeypatch):
    """rc5(수집 0·"no tests ran") → fail (wrong-cwd/마커 전멸 vacuous-pass 근절)."""
    fake = _FakeRun(5, "no tests ran in 0.01s")
    monkeypatch.setattr(live_board.subprocess, "run", fake)
    rc = live_board.cmd_livegate(_rec_args())
    assert rc == 1
    data = _read_flag(live_board)
    assert data["status"] == "fail"
    assert data["n"] == 0
    assert data["rc"] == 5


# ── record ②b codex 축 편입(pin 17) → codex relay smoke 누락 시 fail-safe 차단 (ADR-0070 D7) ──

def test_record_missing_codex_axis_blocks(live_board, monkeypatch, capsys):
    """codex 라이브 축(relay smoke·@release)이 release pin 에 편입된 뒤(16→17), codex 축이 빠지거나
    skip 돼 수집이 16 으로 줄면 livegate 가 **차단**함을 못박는다 — 티켓 목표 'codex 라이브 green 없이는
    v1.4.0 main push 차단'을 수집 pin 으로 강제(ADR-0070 D7·수집 위장 차단의 codex 방향).

    pin=17 은 codex relay smoke 를 포함한다. codex 축이 (마커 소실·wrong-cwd·게이트 env 누락으로)
    안 돌면 수집이 16 으로 떨어지는데, 이때 '수집 16 ≠ pin 17' 로 red — codex green 없이 릴리즈가
    통과하는 false-green 을 막는다. (over-collection 마커 소실 5↔16 과 대칭인, codex-특정 under-collection.)"""
    without_codex = live_board.LIVEGATE_RELEASE_PIN - 1  # codex relay smoke 누락 → 17에서 16으로.
    fake = _FakeRun(0, f"{without_codex} passed, 810 deselected in 45.67s")
    monkeypatch.setattr(live_board.subprocess, "run", fake)
    rc = live_board.cmd_livegate(_rec_args())
    assert rc == 1, "codex 축 누락(수집<pin)인데 pass(0) 면 codex green 없이 릴리즈 통과(false-green)"
    data = _read_flag(live_board)
    assert data["status"] == "fail"
    assert data["n"] == without_codex   # codex 빠져 16 수집
    assert data["rc"] == 0              # red 아님 — 수집 미달이라 fail
    err = capsys.readouterr().err
    assert f"수집 {without_codex} ≠ pin {live_board.LIVEGATE_RELEASE_PIN}" in err
    assert "수집 위장 차단" in err


# ── record ③ rc!=0 → fail (실행 N==pin 이어도) ──────────────────────────────

def test_record_rc_nonzero_records_fail(live_board, monkeypatch, capsys):
    """rc!=0 → status='fail' + rc1 — 수집 N==pin(22) 이어도 red 는 fail.

    rc 게이트가 수집 N 과 독립임을 확인: 22개 실행(1 red)이라 N=22 이지만 rc1 → fail.
    """
    fake = _FakeRun(1, "1 failed, 21 passed, 810 deselected in 40.0s")
    monkeypatch.setattr(live_board.subprocess, "run", fake)
    rc = live_board.cmd_livegate(_rec_args())
    assert rc == 1
    data = _read_flag(live_board)
    assert data["status"] == "fail"
    assert data["n"] == 22         # 실행은 22 이지만
    assert data["rc"] == 1         # red 라 fail
    err = capsys.readouterr().err
    assert "release red (rc=1)" in err


# ── record ⑤ cwd 해소 = 활성 slot worktree (`_livegate_cwd`) ────────────────

def test_record_uses_regression_cwd_seam(live_board, monkeypatch):
    """record 의 pytest 는 `_livegate_cwd`(활성 slot worktree)에서 돈다.

    회귀 cwd 와는 갈린다 — 회귀는 push 되는 트리 자신에서 돌고(`_regression_cwd`), 라이브 게이트만
    PM 홈에서 실행돼 코드가 사는 슬롯 트리를 겨냥한다(T-0733)."""
    worktree = str(live_board._proj / "work" / "slot1")
    # `_active_slot_path(session=None)` 시그니처(ADR-0040 D2) — 선택 인자 수용.
    monkeypatch.setattr(live_board, "_active_slot_path", lambda session=None: worktree)
    fake = _FakeRun(0, "22 passed, 810 deselected in 45.67s")
    monkeypatch.setattr(live_board.subprocess, "run", fake)
    live_board.cmd_livegate(_rec_args())
    assert fake.calls, "pytest subprocess 가 호출되지 않았다"
    # record 는 pytest run 前에 `_resolve_livegate_flag`(→ git config)를 먼저 호출한다(T-0287
    # fail-fast). 순서 무관하게 pytest run(shell=True)을 골라 cwd 를 검증한다.
    pytest_call = next((c for c in fake.calls if c["kwargs"].get("shell")), None)
    assert pytest_call is not None, "pytest -m release subprocess 가 호출되지 않았다"
    assert pytest_call["kwargs"]["cwd"] == worktree, \
        "record 가 활성 slot worktree(=_regression_cwd)에서 돌지 않았다"


def test_record_explicit_cwd_skips_actor_slot_resolution(live_board, monkeypatch):
    """명시 `--cwd` + `--repo` 단독 + 활성 슬롯 ≥2 → 모호 fail 미발화·그 cwd 에서 실행 (v1.3.0 릴리즈 실측 결함 회귀).

    readonly 공유 슬롯(T-0358·leased) 추가로 repo 활성 슬롯이 2가 되면, eager session 해소
    (`_actor_session_override`→`resolve_actor_slot`)가 `--cwd` 핀에도 불구하고 SlotResolutionError 로
    죽던 결함 — pm-release §2 처방(`record --repo <repo> --cwd <readonly>`)을 막았다. `--cwd` 명시면
    session 해소 자체를 생략함을 못박는다."""
    override = str(live_board._proj / "readonly_slot")
    def _boom(*a, **k):
        raise AssertionError("--cwd 명시인데 actor session 해소가 호출됨 (eager 해소 재발)")
    monkeypatch.setattr(live_board, "_actor_session_override", _boom)
    fake = _FakeRun(0, "22 passed, 810 deselected in 45.67s")
    monkeypatch.setattr(live_board.subprocess, "run", fake)
    rc = live_board.cmd_livegate(_rec_args(cwd=override, repo="proj"))
    assert rc == 0
    pytest_call = next((c for c in fake.calls if c["kwargs"].get("shell")), None)
    assert pytest_call is not None and pytest_call["kwargs"]["cwd"] == override


def test_record_explicit_cwd_override(live_board, monkeypatch):
    """명시 `--cwd` 는 활성 slot 해소를 우회해 그 경로에서 돈다 (ADR-0014 override)."""
    override = str(live_board._proj / "elsewhere")
    fake = _FakeRun(0, "22 passed, 810 deselected in 45.67s")
    monkeypatch.setattr(live_board.subprocess, "run", fake)
    live_board.cmd_livegate(_rec_args(cwd=override))
    # pytest run(shell=True)을 순서 무관하게 선택 — record 는 그 前에 git config 를 부른다(T-0287 fail-fast).
    pytest_call = next((c for c in fake.calls if c["kwargs"].get("shell")), None)
    assert pytest_call is not None, "pytest -m release subprocess 가 호출되지 않았다"
    assert pytest_call["kwargs"]["cwd"] == override


# ── record ⑥ multi-lease cwd 해소: --repo/--slot 이 슬롯 핀 (T-0298·ADR-0057·--cwd 우회 불요) ──
# `livegate record` 는 이제 `--repo`/`--slot`(ADR-0057)을 받아 handoff 과 동형
# (session_name)으로 `_livegate_cwd` 에 thread 한다. multi-lease(leased≥2) 홈에서 --repo/--slot
# 명시면 그 슬롯 cwd 로 해소돼 pytest 가 돌고(rc0), 무명시면 seam 이 fail-loud(모호는 시끄럽게) —
# 광고한 remedy(`--repo <repo> --slot <N>`)가 실제로 수용돼 dead-end 가 아니다(T-0285 anti-pattern
# 회피).

def _seed_two_leases(live_board):
    """활성 슬롯 2개(A_1·B_1)를 리스 장부에 심는다 (multi-lease genuine-ambiguity 전제)."""
    live_board.LEASES_FILE.parent.mkdir(parents=True, exist_ok=True)
    live_board.LEASES_FILE.write_text(json.dumps({"leases": [
        {"slot": "work/A_1", "repo": "A", "session": "A_1", "state": "leased"},
        {"slot": "work/B_1", "repo": "B", "session": "B_1", "state": "leased"},
    ]}), encoding="utf-8")


def test_record_multilease_with_session_resolves_slot(live_board, monkeypatch):
    """(a) multi-lease + `--repo B --slot 1`(ADR-0057) → 그 슬롯 worktree cwd 해소·rc0
    (fail-loud 아님·--cwd 불요)."""
    monkeypatch.delenv("PM_SESSION_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_NAME", raising=False)
    _seed_two_leases(live_board)
    fake = _FakeRun(0, "22 passed, 810 deselected in 45.67s")
    monkeypatch.setattr(live_board.subprocess, "run", fake)
    rc = live_board.cmd_livegate(_rec_args(repo="B", slot=1))
    assert rc == 0, "명시 --repo/--slot 은 슬롯 해소 → fail-loud 아님"
    pytest_call = next((c for c in fake.calls if c["kwargs"].get("shell")), None)
    assert pytest_call is not None, "pytest -m release subprocess 가 호출되지 않았다"
    assert pytest_call["kwargs"]["cwd"] == str(live_board.REPO / "work/B_1"), \
        "record 가 --session 이 가리키는 슬롯 worktree 에서 돌지 않았다"
    data = _read_flag(live_board)
    assert data["status"] == "pass"


def test_record_multilease_no_session_fails_loud(live_board, monkeypatch):
    """(b) multi-lease + 무명시(--repo/--slot 없음) → 여전히 fail-loud(SystemExit·--repo/--cwd 안내)."""
    monkeypatch.delenv("PM_SESSION_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_NAME", raising=False)
    _seed_two_leases(live_board)
    fake = _FakeRun(0, "22 passed, 810 deselected in 45.67s")
    monkeypatch.setattr(live_board.subprocess, "run", fake)
    with pytest.raises(SystemExit) as exc:
        live_board.cmd_livegate(_rec_args())
    msg = str(exc.value)
    assert "--repo" in msg and "--cwd" in msg
    # fail-loud 는 값비싼 pytest -m release 를 돌리기 전에 거부해야 한다(cwd seam·record 앞단).
    assert not any(c["kwargs"].get("shell") for c in fake.calls), \
        "모호한데도 pytest -m release 를 돌렸다 (fail-loud 가 seam 에서 안 막음)"


def test_record_write_is_atomic_no_tmp_left(live_board, monkeypatch):
    """기록은 atomic write(temp + os.replace) — 성공 후 .tmp 잔재가 없다."""
    fake = _FakeRun(0, "22 passed, 810 deselected in 45.67s")
    monkeypatch.setattr(live_board.subprocess, "run", fake)
    live_board.cmd_livegate(_rec_args())
    tmp = live_board.LIVEGATE_FLAG.with_suffix(live_board.LIVEGATE_FLAG.suffix + ".tmp")
    assert not tmp.exists(), "atomic write 후 .tmp 잔재가 남았다"
    assert live_board.LIVEGATE_FLAG.exists()


# ── check ④ 3분기: 부재 / red / rev 불일치 + green ──────────────────────────

def test_check_absent_record_blocks(live_board, capsys):
    """기록 부재 → rc1 + '기록 없음' 사유 (record 선행 요구)."""
    rc = live_board.cmd_livegate(_chk_args(rev="cafef00dcafef00d0011223344556677"))
    assert rc == 1
    err = capsys.readouterr().err
    assert "기록 없음" in err


def test_check_red_record_blocks(live_board, capsys):
    """status!=pass 기록 → rc1 + RED 사유 (수집·rc 표면화·rev 무관)."""
    live_board.LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    live_board.LIVEGATE_FLAG.write_text(json.dumps(
        {"head": "cafef00dcafef00d0011223344556677", "status": "fail",
         "n": 5, "rc": 0, "ts": "2026-07-03T00:00:00+00:00"}), encoding="utf-8")
    rc = live_board.cmd_livegate(_chk_args(rev="cafef00dcafef00d0011223344556677"))
    assert rc == 1
    err = capsys.readouterr().err
    assert "RED" in err
    assert "수집 5" in err


def test_check_rev_mismatch_blocks(live_board, capsys):
    """pass 기록이나 head != push rev → rc1 + 'rev 불일치' (재실행 요구·부재/red 와 구분)."""
    live_board.LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    live_board.LIVEGATE_FLAG.write_text(json.dumps(
        {"head": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "status": "pass",
         "n": 7, "rc": 0, "ts": "2026-07-03T00:00:00+00:00"}), encoding="utf-8")
    rc = live_board.cmd_livegate(_chk_args(rev="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"))
    assert rc == 1
    err = capsys.readouterr().err
    assert "rev 불일치" in err
    assert "aaaaaaaa" in err   # 기록 sha
    assert "bbbbbbbb" in err   # push sha


def test_check_pass_matching_rev_green(live_board, capsys):
    """pass 기록 ∧ head==rev → rc0 green (보호훅 통과 경로)."""
    live_board.LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    sha = "cafef00dcafef00d0011223344556677"
    live_board.LIVEGATE_FLAG.write_text(json.dumps(
        {"head": sha, "status": "pass", "n": 7, "rc": 0,
         "ts": "2026-07-03T00:00:00+00:00"}), encoding="utf-8")
    rc = live_board.cmd_livegate(_chk_args(rev=sha))
    assert rc == 0
    out = capsys.readouterr().out
    assert "green @ cafef00d" in out


def test_check_requires_rev(live_board, capsys):
    """check 는 `--rev` 없으면 rc1 (push 대상 sha 필수)."""
    rc = live_board.cmd_livegate(_chk_args(rev=None))
    assert rc == 1
    err = capsys.readouterr().err
    assert "--rev" in err


# ── 라운드트립: record(pass) → check green ──────────────────────────────────

def test_record_pass_then_check_green_roundtrip(live_board, monkeypatch, capsys):
    """record 로 pass 기록 후, 그 head 로 check 하면 green (실행=기록→소비 일관)."""
    fake = _FakeRun(0, "22 passed, 810 deselected in 45.67s")
    monkeypatch.setattr(live_board.subprocess, "run", fake)
    assert live_board.cmd_livegate(_rec_args()) == 0
    recorded_head = _read_flag(live_board)["head"]
    capsys.readouterr()  # drain record 출력
    rc = live_board.cmd_livegate(_chk_args(rev=recorded_head))
    assert rc == 0
    assert "green" in capsys.readouterr().out


# ── pm-verified 처분 — 추가 리뷰어(release) 축 채널 스코프 재검증 ──────────────
# 채널 폐지 뒤에도 additional-reviewer 장부(review_rounds.json)의 반려 잔여를 외부 재송신 없이
# 종결하는 `--resolve-gate --pm-verified` 처분이 릴리즈 축(`_unresolved_must_fix_data` →
# `_gate_disposition_problem`)에서 실제로 열리고, `pm-fixed` 는 여전히 막히며, 다른 채널의
# accepted 잔여가 이 채널 처분을 막지 않는지(채널 격리)를 실 board 트리 + 실 라운드/명세 파일 +
# 실 review_rounds.json 으로 검증한다. `--resolve-gate --pm-verified` CLI 선언면(전송 0 단언 포함)
# 은 `tests/test_external_review.py` 가 진다.


def _pm_verified_pd():
    """pm_delegate.py 를 (패키지 아님) 경로 로드 — 리뷰 블록 상수·채널 판정 함수 전용 접근."""
    spec = importlib.util.spec_from_file_location(
        "board_livegate_pm_verified_pd", TOOLS / "pm_delegate.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_PVPD = _pm_verified_pd()
_PV_BLOCK_VERSION = _PVPD.PM_REVIEW_VERSION
_PV_DISPOSITION_VERSION = _PVPD.PM_REVIEW_DISPOSITION_VERSION
_PV_EXTERNAL_ROLE = _PVPD.EXTERNAL_REVIEW_ROLE
_PV_INTERNAL_ROLE = _PVPD.INTERNAL_REVIEW_ROLE
# 채널 어휘는 board 상수가 단일 진실이다(사본 금지) — 빌더·테스트가 같은 값을 쓴다.
_PV_BOARD = _load_board()
_PV_CHANNEL_INTERNAL = _PV_BOARD.GATE_CHANNEL_INTERNAL
_PV_CHANNEL_ADDITIONAL = _PV_BOARD.GATE_CHANNEL_ADDITIONAL


def _pv_seed_ticket_tree(proj: Path) -> None:
    for status in ("open", "claimed", "blocked", "done"):
        (proj / ".project_manager" / "wiki" / "tickets" / status).mkdir(parents=True, exist_ok=True)


def _pv_write_ticket(proj: Path, tid: str, status: str, *, body: str = "") -> Path:
    path = proj / ".project_manager" / "wiki" / "tickets" / status / f"{tid}-fixture.md"
    path.write_text(
        f"---\nid: {tid}\ntitle: 픽스처\ntouches:\n- x.py\n---\n\n# {tid}\n\n{body}",
        encoding="utf-8",
    )
    return path


def _pv_write_round(proj: Path, tid: str, ordinal: int, role: str, body: str) -> Path:
    directory = proj / ".project_manager" / "wiki" / "tickets" / "rounds" / tid
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{ordinal:02d}-{role}.md"
    path.write_text(f"## 라운드 ({role} · 2026-08-01)\n\n{body}", encoding="utf-8")
    return path


def _pv_finding(finding_id: str) -> dict:
    return {
        "id": finding_id, "class": "implementation-defect", "severity": "must-fix",
        "authority": "[[ADR-0001]] §경계", "evidence": f"{finding_id} probe rc=1",
        "recommendation": f"{finding_id}만 수정", "design_change": False,
    }


def _pv_review_block(findings: list) -> str:
    payload = {"version": _PV_BLOCK_VERSION, "findings": findings, "confirmations": []}
    verdict = (
        "판정: 반려\n\n## must-fix\n- 구조화 finding 참조\n\n" if findings
        else "판정: 통과\n\n## must-fix\n- 없음\n\n"
    )
    return f"{verdict}```pm-review-v1\n{json.dumps(payload, ensure_ascii=False)}\n```\n"


def _pv_confirmation_only_block(confirmations: list) -> str:
    """findings 없이 confirmations 만 실은 후속 리뷰 라운드 블록(reviewer 확인 전용·기계 확인과 다른 축)."""
    payload = {"version": _PV_BLOCK_VERSION, "findings": [], "confirmations": confirmations}
    return (
        "판정: 통과\n\n## must-fix\n- 없음\n\n"
        f"```pm-review-v1\n{json.dumps(payload, ensure_ascii=False)}\n```\n"
    )


def _pv_decision(finding_id: str, decision: str, *, reason: str | None = None) -> dict:
    return {
        "id": finding_id, "decision": decision,
        "reason": (f"PM {decision} 근거" if reason is None else reason),
        "scope": f"{finding_id} 허용 범위" if decision == "accepted" else "",
        "prerequisite": "",
    }


def _pv_disposition_block(ordinal: int, rows: list, *, reviewer_role: str) -> str:
    payload = {
        "version": _PV_DISPOSITION_VERSION, "reviewer_ordinal": ordinal,
        "reviewer_role": reviewer_role, "dispositions": rows,
    }
    return f"```pm-review-disposition-v1\n{json.dumps(payload, ensure_ascii=False)}\n```\n"


def _pv_round_outcome(sequence: int, must_fix: int) -> dict:
    return {
        "sequence": sequence, "verdict": 1, "must_fix": must_fix, "suggestions": 0,
        "started_at": f"2026-08-01T00:00:0{sequence}+00:00",
        "target_rev": f"sha256:{'a' * 63}{sequence}",
        "ts": f"2026-08-01T01:00:0{sequence}+00:00",
    }


def _pv_ledger_entry(rounds: list, *, confirm_fix: int = 0) -> dict:
    return {"count": len(rounds), "confirm_fix": confirm_fix, "rounds": rounds}


def _pv_seed_v178_shape(proj: Path, tid: str) -> dict:
    """v1.7.8 실물 형상 재현 — 잔여 1·confirm_fix 0·완료 라운드 1·X-001 rejected(사유 있음)·기계 확인 0."""
    _pv_write_ticket(proj, tid, "claimed", body=_pv_disposition_block(
        1, [_pv_decision("X-001", "rejected")], reviewer_role=_PV_EXTERNAL_ROLE,
    ))
    _pv_write_round(proj, tid, 1, _PV_EXTERNAL_ROLE, _pv_review_block([_pv_finding("X-001")]))
    return _pv_ledger_entry([_pv_round_outcome(1, 1)])


def _pv_developer_round_block(finding_ids: list) -> str:
    """dev 라운드 verify 블록 — 기계 확인이 참조할 finding 을 machine_verifiable 로 선언."""
    payload = {
        "version": _PVPD.PM_REVIEW_VERIFY_VERSION,
        "verifications": [
            {"id": finding_id, "machine_verifiable": True, "command": "echo hi",
             "expected": "hi", "before": "bye", "reason": ""}
            for finding_id in finding_ids
        ],
    }
    return (
        f"```{_PVPD.PM_REVIEW_VERIFY_BLOCK}\n"
        f"{json.dumps(payload, ensure_ascii=False)}\n```\n"
    )


def _pv_machine_confirmation_block(round_ordinal: int, finding_ids: list) -> str:
    """PM 기계 확인 블록 — 그 채널 finding 을 실제 명령 출력으로 확인했다는 기록."""
    payload = {
        "version": _PVPD.PM_REVIEW_MACHINE_CONFIRMATION_VERSION,
        "round": round_ordinal,
        "confirmations": [
            {"id": finding_id, "status": "resolved", "command": "echo hi", "observed": "hi"}
            for finding_id in finding_ids
        ],
    }
    return (
        f"```{_PVPD.PM_REVIEW_CONFIRMATION_BLOCK}\n"
        f"{json.dumps(payload, ensure_ascii=False)}\n```\n"
    )


def _pv_pm_verified_resolution(round_sequence: int, count: int, must_fix: int = 1) -> dict:
    """이미 선언돼 장부에 실린 pm-verified 처분 — 재검증(완료·릴리즈) 축의 입력."""
    return {
        "kind": "pm-verified", "ts": "2026-08-01T02:00:00+00:00", "must_fix": must_fix,
        "round_sequence": round_sequence, "rounds": count,
    }


def _pv_seed_mixed_channel_shape(
    proj: Path, tid: str, *, machine_channel: str, internal_residual: int = 1,
) -> dict:
    """두 채널이 함께 열렸던 티켓 — **한 채널만** 기계 확인이 있는 혼합 형상 공용 빌더.

    네 경로(내부 선언·내부 완료 재검증·추가 선언·릴리즈 재검증)가 이 한 빌더를 공유한다 —
    생산 배선과 테스트 기준이 갈리지 않게 형상은 여기 한 곳에서만 정의한다.

    두 채널 모두 accepted 판정을 받았고, 기계 확인이 없는 쪽은 **리뷰어 확인 라운드**로만
    해소된다. 그래서 티켓 전역(=채널 스코프 없는 판정)에서는 accepted 잔여가 0 이고 기계 확인이
    1건 이상이라 두 채널이 모두 열려 보인다 — 채널 스코프가 있어야 기계 확인이 없는 쪽이 닫힌다.

    `machine_channel` 은 `GATE_CHANNEL_INTERNAL`/`GATE_CHANNEL_ADDITIONAL` 과 같은 어휘를 쓴다.
    반환은 채널별 장부 entry(`{"internal": ..., "additional": ...}`) 이고, 호출부가 필요한
    처분(`resolution`)을 얹어 장부에 쓴다.
    """
    machine_internal = machine_channel == _PV_CHANNEL_INTERNAL
    machine_ids = ["F-001"] if machine_internal else ["X-001"]
    _pv_write_round(proj, tid, 1, _PV_INTERNAL_ROLE, _pv_review_block([_pv_finding("F-001")]))
    _pv_write_round(proj, tid, 2, _PV_EXTERNAL_ROLE, _pv_review_block([_pv_finding("X-001")]))
    _pv_write_round(proj, tid, 3, "developer", _pv_developer_round_block(machine_ids))
    reviewer_confirmed_role = _PV_EXTERNAL_ROLE if machine_internal else _PV_INTERNAL_ROLE
    reviewer_confirmed_id = "X-001" if machine_internal else "F-001"
    _pv_write_round(proj, tid, 4, reviewer_confirmed_role, _pv_confirmation_only_block([
        {"id": reviewer_confirmed_id, "status": "resolved", "evidence": "재검토 결과 해소 확인"},
    ]))
    _pv_write_ticket(proj, tid, "claimed", body=(
        _pv_disposition_block(
            1, [_pv_decision("F-001", "accepted")], reviewer_role=_PV_INTERNAL_ROLE,
        )
        + _pv_disposition_block(
            2, [_pv_decision("X-001", "accepted")], reviewer_role=_PV_EXTERNAL_ROLE,
        )
        + _pv_machine_confirmation_block(3, machine_ids)
    ))
    return {
        _PV_CHANNEL_INTERNAL: _pv_ledger_entry([_pv_round_outcome(1, internal_residual)]),
        _PV_CHANNEL_ADDITIONAL: _pv_ledger_entry([_pv_round_outcome(1, 1)]),
    }


def _pv_pm_fixed_resolution(round_sequence: int, count: int) -> dict:
    return {
        "kind": "pm-fixed",
        "pm_fixed_evidence": {
            "change": "x.py:1", "regression": "pytest tests/test_board_livegate.py -q",
            "result": "rc=0",
        },
        "round_sequence": round_sequence, "rounds": count,
    }


def _pv_round_tuple(ordinal: int, role: str, text: str):
    rounds_module = _PVPD._load_ticket_rounds()
    return rounds_module.Round(
        ordinal=ordinal, role=role,
        path=Path(rounds_module.round_filename(ordinal, role)),
        text=text, pending=False,
    )


def _pv_gate_args() -> argparse.Namespace:
    """`_complete_gate` 인자 — log·회귀 축은 통과로 두고 리뷰 게이트만 판정면에 남긴다."""
    return argparse.Namespace(
        allow_missing_log=True, tests_pass=True, allow_untested=False,
    )


@pytest.fixture
def pm_verified_release(tmp_path, monkeypatch):
    """pm-verified 처분 검증 시나리오 — 실 티켓 트리 + 실 라운드 파일 + 두 채널 실 장부.

    한 시나리오가 생산 경로를 그대로 태운다: 내부 선언(`pm_delegate rounds resolve
    --pm-verified` CLI) · 내부 완료 재검증(`board._complete_gate`) · 릴리즈 재검증
    (`board livegate record`). 추가 채널 선언 CLI 는 `tests/test_external_review.py` 가
    같은 빌더를 import 해 태운다 — 형상 정의는 한 곳(`_pv_seed_mixed_channel_shape`)뿐이다.
    """
    proj = tmp_path / "proj"
    local = proj / ".project_manager" / ".local"
    _pv_seed_ticket_tree(proj)
    local.mkdir(parents=True, exist_ok=True)
    mod = _load_board()
    monkeypatch.setattr(mod, "REPO", proj)
    monkeypatch.setattr(mod, "LOCAL_DIR", local)
    monkeypatch.setattr(mod, "LIVEGATE_FLAG", local / "livegate.json")
    monkeypatch.setattr(mod, "LEASES_FILE", local / "worktree-leases.json")
    monkeypatch.setattr(mod, "_git_config_get", lambda cwd, key: None)
    monkeypatch.setattr(mod, "_git_head_at", lambda cwd: "feedface" * 5)
    runner = _FakeRun(0, f"{mod.LIVEGATE_RELEASE_PIN} passed, 800 deselected in 40.0s")
    monkeypatch.setattr(mod.subprocess, "run", runner)

    # 내부 축 CLI(`pm_delegate rounds resolve`)는 같은 board 인스턴스·같은 tmp PM 홈을 본다 —
    # 선언과 완료 재검증이 실제로 한 형상을 공유해야 배선 결함이 테스트에 잡힌다.
    pd = _pm_verified_pd()
    monkeypatch.setattr(pd, "_CONFIG_REPO_OVERRIDE", proj)
    monkeypatch.setattr(pd, "_load_board", lambda: mod)

    ledger_path = local / "review_rounds.json"
    internal_ledger_path = pd._internal_round_ledger_path()

    def write_ledger(ledger: dict) -> None:
        local.mkdir(parents=True, exist_ok=True)
        ledger_path.write_text(json.dumps(ledger, ensure_ascii=False), encoding="utf-8")

    def write_internal_ledger(ledger: dict) -> None:
        local.mkdir(parents=True, exist_ok=True)
        internal_ledger_path.write_text(
            json.dumps(ledger, ensure_ascii=False), encoding="utf-8",
        )

    return types.SimpleNamespace(
        module=mod, pd=pd, proj=proj, runner=runner, ledger_path=ledger_path,
        internal_ledger_path=internal_ledger_path,
        write_ledger=write_ledger,
        read_ledger=lambda: json.loads(ledger_path.read_text(encoding="utf-8")),
        write_internal_ledger=write_internal_ledger,
        read_internal_ledger=lambda: json.loads(
            internal_ledger_path.read_text(encoding="utf-8")),
        resolve_internal=lambda gate, *argv: pd._cmd_rounds(
            ["resolve", "--gate", gate, *argv]),
        complete=lambda gate: mod._complete_gate(gate, _pv_gate_args()),
        record=lambda: mod.cmd_livegate(argparse.Namespace(
            action="record", rev=None, cwd=str(proj), repo=None, slot=None, task=None)),
        flag=lambda: json.loads((local / "livegate.json").read_text(encoding="utf-8")),
    )


# ── 정상 · v1.7.8 실물 형상 재현 ─────────────────────────────────────────────

def test_pm_verified_resolution_opens_the_v178_release_shape(pm_verified_release):
    """v1.7.8 실물 형상(잔여1·confirm_fix0·완료라운드1·X-001 rejected·기계확인0)이 신규 경로로 rc0.

    단언은 라이브 wave 가 실제로 도는 것(=차단이 풀렸다) — 전송 0 단언 자체는 선언면(CLI) 을 도는
    `tests/test_external_review.py` 가 진다.
    """
    tid = "T-9764"
    entry = _pv_seed_v178_shape(pm_verified_release.proj, tid)
    entry["resolution"] = {
        "kind": "pm-verified", "ts": "2026-08-01T02:00:00+00:00", "must_fix": 1,
        "round_sequence": 1, "rounds": 1,
    }
    pm_verified_release.write_ledger({tid: entry})
    assert pm_verified_release.record() == 0
    assert pm_verified_release.runner.calls, "잔여가 처분됐으면 라이브 wave 가 실제로 돌아야 한다"
    assert pm_verified_release.flag()["status"] == "pass"


def test_pm_fixed_resolution_is_still_rejected_on_the_release_ledger(pm_verified_release, capsys):
    """같은 v1.7.8 형상을 `pm-fixed` 로 선언하면 여전히 rc≠0 (명시 회귀 — 열어도 쓸 수 없는 표면)."""
    tid = "T-9765"
    entry = _pv_seed_v178_shape(pm_verified_release.proj, tid)
    entry["confirm_fix"] = 1
    entry["pm_fixed"] = 1
    entry["resolution"] = _pv_pm_fixed_resolution(1, 1)
    pm_verified_release.write_ledger({tid: entry})
    assert pm_verified_release.record() == 1
    assert pm_verified_release.runner.calls == []
    assert "이 장부에서는 pm-fixed 처분을 허용하지 않습니다" in capsys.readouterr().err


# ── 실패 경로 7종 ────────────────────────────────────────────────────────────

def test_pm_verified_declaration_blocks_when_surface_finding_count_is_short(pm_verified_release):
    """실패1 — 잔여 5인데 표면 처분 3건뿐 → 차단(표면 미달·불변식 5)."""
    proj, mod = pm_verified_release.proj, pm_verified_release.module
    tid = "T-9770"
    findings = [_pv_finding(f"X-00{i}") for i in (1, 2, 3)]
    rows = [_pv_decision(f"X-00{i}", "rejected") for i in (1, 2, 3)]
    _pv_write_ticket(proj, tid, "claimed", body=_pv_disposition_block(
        1, rows, reviewer_role=_PV_EXTERNAL_ROLE,
    ))
    _pv_write_round(proj, tid, 1, _PV_EXTERNAL_ROLE, _pv_review_block(findings))
    entry = _pv_ledger_entry([_pv_round_outcome(1, 5)])
    problem = mod._pm_verified_evidence_problem(
        tid, channel=_PV_CHANNEL_ADDITIONAL, entry=entry,
    )
    assert problem is not None
    assert "3건" in problem and "5건" in problem


def test_pm_verified_declaration_blocks_on_undisposed_finding(pm_verified_release):
    """실패2 — 표면에 미판정 finding 이 남으면 delta 파싱이 pending 으로 막힌다."""
    proj, mod = pm_verified_release.proj, pm_verified_release.module
    tid = "T-9771"
    findings = [_pv_finding("X-001"), _pv_finding("X-002")]
    rows = [_pv_decision("X-001", "rejected")]     # X-002 는 판정 없음
    _pv_write_ticket(proj, tid, "claimed", body=_pv_disposition_block(
        1, rows, reviewer_role=_PV_EXTERNAL_ROLE,
    ))
    _pv_write_round(proj, tid, 1, _PV_EXTERNAL_ROLE, _pv_review_block(findings))
    entry = _pv_ledger_entry([_pv_round_outcome(1, 2)])
    problem = mod._pm_verified_evidence_problem(
        tid, channel=_PV_CHANNEL_ADDITIONAL, entry=entry,
    )
    assert problem is not None
    assert "pending" in problem


def test_pm_verified_declaration_blocks_on_blank_rejection_reason(pm_verified_release):
    """실패3 — 사유 빈 rejected 는 처분으로 세지 않는다(파서가 malformed 로 거부·불변식 8)."""
    proj, mod = pm_verified_release.proj, pm_verified_release.module
    tid = "T-9772"
    rows = [_pv_decision("X-001", "rejected", reason="")]
    _pv_write_ticket(proj, tid, "claimed", body=_pv_disposition_block(
        1, rows, reviewer_role=_PV_EXTERNAL_ROLE,
    ))
    _pv_write_round(proj, tid, 1, _PV_EXTERNAL_ROLE, _pv_review_block([_pv_finding("X-001")]))
    entry = _pv_ledger_entry([_pv_round_outcome(1, 1)])
    problem = mod._pm_verified_evidence_problem(
        tid, channel=_PV_CHANNEL_ADDITIONAL, entry=entry,
    )
    assert problem is not None
    assert "malformed" in problem


def test_pm_verified_declaration_blocks_on_open_accepted_finding(pm_verified_release):
    """실패4 — 그 채널 accepted 잔여(미해소)가 있으면 차단."""
    proj, mod = pm_verified_release.proj, pm_verified_release.module
    tid = "T-9773"
    rows = [_pv_decision("X-001", "accepted")]
    _pv_write_ticket(proj, tid, "claimed", body=_pv_disposition_block(
        1, rows, reviewer_role=_PV_EXTERNAL_ROLE,
    ))
    _pv_write_round(proj, tid, 1, _PV_EXTERNAL_ROLE, _pv_review_block([_pv_finding("X-001")]))
    entry = _pv_ledger_entry([_pv_round_outcome(1, 1)])
    problem = mod._pm_verified_evidence_problem(
        tid, channel=_PV_CHANNEL_ADDITIONAL, entry=entry,
    )
    assert problem is not None
    assert "accepted 잔여가 있습니다" in problem


def test_pm_verified_declaration_blocks_when_accepted_lacks_machine_confirmation(pm_verified_release):
    """실패5 — accepted 가 있었는데(리뷰어 확인으로 해소) 기계 확인이 0건이면 차단(불변식 6 역방향)."""
    proj, mod = pm_verified_release.proj, pm_verified_release.module
    tid = "T-9774"
    rows = [_pv_decision("X-001", "accepted")]
    _pv_write_ticket(proj, tid, "claimed", body=_pv_disposition_block(
        1, rows, reviewer_role=_PV_EXTERNAL_ROLE,
    ))
    _pv_write_round(proj, tid, 1, _PV_EXTERNAL_ROLE, _pv_review_block([_pv_finding("X-001")]))
    _pv_write_round(proj, tid, 2, _PV_EXTERNAL_ROLE, _pv_confirmation_only_block([
        {"id": "X-001", "status": "resolved", "evidence": "재검토 결과 해소 확인"},
    ]))
    entry = _pv_ledger_entry([_pv_round_outcome(1, 1)])
    problem = mod._pm_verified_evidence_problem(
        tid, channel=_PV_CHANNEL_ADDITIONAL, entry=entry,
    )
    assert problem is not None
    assert "기계 확인" in problem


def test_pm_verified_declaration_is_re_blocked_by_a_new_round(pm_verified_release):
    """실패6(stale) — 선언 뒤 새 반려 라운드가 장부에 기록되면 다시 차단(gate_resolution_is_stale)."""
    tid = "T-9775"
    entry = _pv_seed_v178_shape(pm_verified_release.proj, tid)
    entry["resolution"] = {
        "kind": "pm-verified", "ts": "2026-08-01T02:00:00+00:00", "must_fix": 1,
        "round_sequence": 1, "rounds": 1,
    }
    entry["rounds"].append(_pv_round_outcome(2, 1))   # 처분 뒤 새 라운드 — 좌표가 갈린다
    entry["count"] = 2
    pm_verified_release.write_ledger({tid: entry})
    assert pm_verified_release.record() == 1
    assert pm_verified_release.runner.calls == []


def test_pm_verified_re_verification_uses_the_live_surface_not_the_stored_declaration(
    pm_verified_release,
):
    """실패7(선언≠재검증) — 라운드 좌표는 그대로인데 명세를 되돌리면(사유 삭제) 다시 막힌다.

    라운드 좌표만 보고 재검증을 생략하면 이 케이스가 green 으로 샌다 — 선언 시점과 재검증
    시점이 같은 술어(현재 파일)를 본다는 확인(불변식 3)."""
    tid = "T-9776"
    entry = _pv_seed_v178_shape(pm_verified_release.proj, tid)
    entry["resolution"] = {
        "kind": "pm-verified", "ts": "2026-08-01T02:00:00+00:00", "must_fix": 1,
        "round_sequence": 1, "rounds": 1,
    }
    pm_verified_release.write_ledger({tid: entry})
    assert pm_verified_release.record() == 0        # 선언 시점엔 통과.

    ticket_path = next(
        (pm_verified_release.proj / ".project_manager" / "wiki" / "tickets" / "claimed")
        .glob(f"{tid}-*.md")
    )
    ticket_path.write_text(
        ticket_path.read_text(encoding="utf-8").replace("PM rejected 근거", ""),
        encoding="utf-8",
    )
    assert pm_verified_release.record() == 1, "명세가 되돌아갔는데도 옛 선언이 계속 통과시켰다"


# ── 민감도 3종 ───────────────────────────────────────────────────────────────

def test_sensitivity_allow_pm_verified_reverted_reblocks_the_v178_shape(pm_verified_release):
    """민감도(a) — `allow_pm_verified` 를 되돌리면(False) 통과하던 v1.7.8 형상이 다시 red."""
    proj, mod = pm_verified_release.proj, pm_verified_release.module
    tid = "T-9777"
    entry = _pv_seed_v178_shape(proj, tid)
    entry["resolution"] = {
        "kind": "pm-verified", "ts": "2026-08-01T02:00:00+00:00", "must_fix": 1,
        "round_sequence": 1, "rounds": 1,
    }
    ledger = {tid: entry}
    pm_verified_release.write_ledger(ledger)
    search_dirs = mod._release_gate_search_dirs(pm_verified_release.ledger_path)
    # 배선대로(allow_pm_verified=True) 면 통과.
    assert mod._gate_disposition_problem(
        tid, entry, ledger, search_dirs,
        allow_pm_verified=True,
        pm_verified_problem=mod._gate_pm_verified_problem(
            tid, entry, _PV_CHANNEL_ADDITIONAL,
        ),
    ) is None
    # 되돌리면(기본 False) 같은 형상이 다시 차단된다.
    reverted = mod._gate_disposition_problem(tid, entry, ledger, search_dirs)
    assert reverted is not None
    assert "허용하지 않습니다" in reverted


# ── 채널 격리 양방향 — 실제 선언/완료·릴리즈 경로 (직접 술어 호출 아님) ─────────────
# 혼합 채널 형상(두 채널 모두 accepted 였고 한 채널만 기계 확인)을 네 생산 경로에 각각 태운다.
# 티켓 전역 판정으로는 두 채널이 다 열려 보이는 형상이라, 채널 스코프가 실제로 배선돼 있어야만
# 기계 확인이 없는 쪽이 닫힌다. 추가 선언 CLI 축은 tests/test_external_review.py 가 같은 빌더로 진다.


def _pv_legacy_global_problem(delegate, spec_text: str, rounds):
    """fix 전 규칙 재현 — 채널 스코프 없이 티켓 전역으로 판정한다(민감도 되돌림용).

    T-0791 에서 생산 코드의 무스코프 분기를 삭제했으므로(호출 자체가 fail-loud) 그 규칙
    (accepted 잔여 0 · **채널 무관** 기계 확인 ≥ 1)을 여기서 재구성한다. 되돌림 값이 라운드 04
    실측과 같아야 스코프 인자가 load-bearing 임이 계속 증명된다."""
    try:
        delta = delegate.parse_pm_review_delta(spec_text, rounds)
    except delegate.PMReviewError as exc:
        return f"delta 파싱 실패[{exc.code}]: {exc}"
    if delta.accepted:
        remaining = ", ".join(finding.id for finding, _disposition in delta.accepted)
        return f"PM 판정 accepted 잔여가 있습니다: {remaining}"
    confirmations = sum(
        delegate._pm_review_machine_confirmation_count(spec_text, reviewer_role=role)
        for role in delegate.REVIEW_ROLES
    )
    if confirmations < 1:
        return "기계 확인(pm-review-confirmation-v1) 기록이 없습니다"
    return None


def _pv_legacy_unscoped_problem(mod, tid: str):
    """board 완료 축 되돌림 — 그 티켓 명세·라운드를 읽어 전역 규칙으로 판정한다."""
    _status, path = mod.find_ticket_exact(tid)
    spec_text = path.read_text(encoding="utf-8")
    delegate = mod._load_pm_delegate_module()
    rounds = mod._load_ticket_rounds().load_rounds(
        mod.tickets_dir(), tid, ticket_text=spec_text,
    )
    return _pv_legacy_global_problem(delegate, spec_text, rounds)


def test_internal_declaration_is_refused_when_only_the_additional_channel_is_confirmed(
    pm_verified_release, monkeypatch, capsys,
):
    """내부 선언(실 CLI) — 추가 채널 X-* 기계 확인만 있으면 내부 게이트 처분은 거부된다.

    민감도 포함: 배선에서 채널 스코프를 되돌리면(fix 전 무스코프 호출) 같은 형상이 rc0 으로
    잘못 선언된다 — 외부 근거로 내부 잔여가 열리던 경로 그대로다."""
    tid = "T-9790"
    entries = _pv_seed_mixed_channel_shape(
        pm_verified_release.proj, tid, machine_channel=_PV_CHANNEL_ADDITIONAL,
    )
    pm_verified_release.write_internal_ledger({tid: entries[_PV_CHANNEL_INTERNAL]})

    assert pm_verified_release.resolve_internal(tid, "--pm-verified") == 1
    assert "code-reviewer 채널의 기계 확인" in capsys.readouterr().err
    assert "resolution" not in pm_verified_release.read_internal_ledger()[tid]

    # 민감도 — 스코프 인자를 되돌린 배선에서는 같은 형상이 열린다(fix 전 실값 재현).
    pd = pm_verified_release.pd
    monkeypatch.setattr(
        pd, "pm_verified_evidence_problem",
        lambda text, rounds, **_scope: _pv_legacy_global_problem(pd, text, rounds),
    )
    assert pm_verified_release.resolve_internal(tid, "--pm-verified") == 0
    assert pm_verified_release.read_internal_ledger()[tid]["resolution"]["kind"] == (
        "pm-verified"
    )


def test_internal_completion_recheck_is_closed_when_only_the_additional_channel_is_confirmed(
    pm_verified_release, monkeypatch,
):
    """내부 완료 재검증(실 완료 게이트) — 같은 형상에서 이미 실린 처분도 다시 열지 못한다.

    민감도 포함: 완료 축 배선을 무스코프로 되돌리면 완료 게이트가 통과해버린다."""
    mod = pm_verified_release.module
    tid = "T-9791"
    entries = _pv_seed_mixed_channel_shape(
        pm_verified_release.proj, tid, machine_channel=_PV_CHANNEL_ADDITIONAL,
    )
    entry = entries[_PV_CHANNEL_INTERNAL]
    entry["resolution"] = _pv_pm_verified_resolution(1, 1)
    pm_verified_release.write_internal_ledger({tid: entry})

    problems = pm_verified_release.complete(tid)

    assert len(problems) == 1
    assert "code-reviewer 채널의 기계 확인" in problems[0]

    # 민감도 — 완료 축이 채널 스코프를 잃으면(fix 전 배선) 같은 형상이 열린다.
    monkeypatch.setattr(
        mod, "_gate_pm_verified_problem",
        lambda gate, gate_entry, channel: (
            lambda: _pv_legacy_unscoped_problem(mod, gate)
        ),
    )
    assert pm_verified_release.complete(tid) == []


def test_additional_axis_opens_on_the_same_shape_the_internal_axis_refuses(
    pm_verified_release,
):
    """반대 방향(과차단 없음) — 그 채널에 기계 확인이 있으면 릴리즈 축은 같은 형상에서 열린다.

    내부 채널이 닫히는 형상이라고 추가 리뷰어 채널까지 막으면 격리가 아니라 전역 판정이다."""
    tid = "T-9792"
    entries = _pv_seed_mixed_channel_shape(
        pm_verified_release.proj, tid, machine_channel=_PV_CHANNEL_ADDITIONAL,
    )
    entry = entries[_PV_CHANNEL_ADDITIONAL]
    entry["resolution"] = _pv_pm_verified_resolution(1, 1)
    pm_verified_release.write_ledger({tid: entry})

    assert pm_verified_release.record() == 0
    assert pm_verified_release.runner.calls, "추가 채널 근거가 충분하면 라이브 wave 가 돌아야 한다"
    assert pm_verified_release.flag()["status"] == "pass"


def test_internal_declaration_and_completion_open_with_internal_machine_evidence(
    pm_verified_release, capsys,
):
    """정상 내부 해소 — 내부 채널 기계 확인이 실재하면 선언(CLI)도 완료 게이트도 열린다.

    채널을 조인 것이 정상 해소까지 막지 않는지 값으로 확인한다(선언 → 완료 실체인)."""
    tid = "T-9793"
    entries = _pv_seed_mixed_channel_shape(
        pm_verified_release.proj, tid, machine_channel=_PV_CHANNEL_INTERNAL,
    )
    pm_verified_release.write_internal_ledger({tid: entries[_PV_CHANNEL_INTERNAL]})

    assert pm_verified_release.resolve_internal(tid, "--pm-verified") == 0
    declared = pm_verified_release.read_internal_ledger()[tid]["resolution"]
    assert declared["kind"] == "pm-verified"
    assert declared["must_fix"] == 1
    assert declared["round_sequence"] == 1
    assert pm_verified_release.complete(tid) == []
    assert "완료 증거는 리뷰 통과가 아니라" in capsys.readouterr().err


def test_release_axis_stays_closed_when_only_the_internal_channel_is_confirmed(
    pm_verified_release, capsys,
):
    """역방향 — 내부 F-* 기계 확인은 추가 리뷰어 게이트의 증거가 아니다(릴리즈 실경로)."""
    tid = "T-9794"
    entries = _pv_seed_mixed_channel_shape(
        pm_verified_release.proj, tid, machine_channel=_PV_CHANNEL_INTERNAL,
    )
    entry = entries[_PV_CHANNEL_ADDITIONAL]
    entry["resolution"] = _pv_pm_verified_resolution(1, 1)
    pm_verified_release.write_ledger({tid: entry})

    assert pm_verified_release.record() == 1
    assert pm_verified_release.runner.calls == []
    assert "external-reviewer 채널의 기계 확인" in capsys.readouterr().err


def test_pm_verified_refusal_is_channel_symmetric_on_the_real_paths(
    pm_verified_release, capsys,
):
    """파리티(DoD ②) — 두 채널 실경로의 거부 사유가 role 토큰만 빼고 같은 문장이다(특례 없음)."""
    internal_tid, additional_tid = "T-9795", "T-9796"
    internal_entries = _pv_seed_mixed_channel_shape(
        pm_verified_release.proj, internal_tid, machine_channel=_PV_CHANNEL_ADDITIONAL,
    )
    pm_verified_release.write_internal_ledger(
        {internal_tid: internal_entries[_PV_CHANNEL_INTERNAL]},
    )
    assert pm_verified_release.resolve_internal(internal_tid, "--pm-verified") == 1
    internal_reason = capsys.readouterr().err

    additional_entries = _pv_seed_mixed_channel_shape(
        pm_verified_release.proj, additional_tid, machine_channel=_PV_CHANNEL_INTERNAL,
    )
    additional_entry = additional_entries[_PV_CHANNEL_ADDITIONAL]
    additional_entry["resolution"] = _pv_pm_verified_resolution(1, 1)
    pm_verified_release.write_ledger({additional_tid: additional_entry})
    assert pm_verified_release.record() == 1
    additional_reason = capsys.readouterr().err

    rule = "채널의 기계 확인(pm-review-confirmation-v1) 기록이 없습니다"
    assert f"{_PV_INTERNAL_ROLE} {rule}" in internal_reason
    assert f"{_PV_EXTERNAL_ROLE} {rule}" in additional_reason


def test_internal_surface_floor_comes_from_the_internal_ledger_residual(
    pm_verified_release, capsys,
):
    """내부 축 표면 하한 = 그 게이트 장부 잔여(불변식 5) — 잔여 3인데 표면 1건이면 거부한다."""
    tid = "T-9797"
    entries = _pv_seed_mixed_channel_shape(
        pm_verified_release.proj, tid,
        machine_channel=_PV_CHANNEL_INTERNAL, internal_residual=3,
    )
    pm_verified_release.write_internal_ledger({tid: entries[_PV_CHANNEL_INTERNAL]})

    assert pm_verified_release.resolve_internal(tid, "--pm-verified") == 1
    err = capsys.readouterr().err
    assert "1건" in err and "3건" in err
    assert "resolution" not in pm_verified_release.read_internal_ledger()[tid]


def test_sensitivity_surface_floor_check_removed_lets_a_short_surface_pass():
    """민감도(c) — 건수 대조(불변식 5)를 지우면(하한 0) 표면 미달 케이스가 잘못 green 이 된다."""
    findings = [_pv_finding(f"X-00{i}") for i in (1, 2, 3)]
    rows = [_pv_decision(f"X-00{i}", "rejected") for i in (1, 2, 3)]
    spec = _pv_disposition_block(1, rows, reviewer_role=_PV_EXTERNAL_ROLE)
    rounds = [_pv_round_tuple(1, _PV_EXTERNAL_ROLE, _pv_review_block(findings))]
    correct = _PVPD.pm_verified_evidence_problem(
        spec, rounds, reviewer_role=_PV_EXTERNAL_ROLE, surface_floor=5,   # 장부 잔여 5(실제 배선값)
    )
    assert correct is not None and "5건" in correct
    removed = _PVPD.pm_verified_evidence_problem(
        spec, rounds, reviewer_role=_PV_EXTERNAL_ROLE, surface_floor=0,   # 대조 제거를 흉내
    )
    assert removed is None, "건수 대조가 제거되면 표면 미달이 잘못 통과한다"
