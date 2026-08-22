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
