"""부트스트랩 커맨드 카드 (T-0250 · ADR-0045·0047·0057) 단위 테스트.

`pm_bootstrap._build_command_card_markdown(identity)` 는 이 세션이 쓸 전 커맨드를 정체성
(`--repo <repo> --slot <N>`·ADR-0057 canonical — 구 ADR-0043 `--session <repo>_<N>` 을
supersede) 채운 완성형으로 코드 생성 dump 한다("--help 자체를 안 가게"·사용자 지시). 검증 축:

  - 정체성 실값 보간 — 카드에 `<repo>_<N>` 류 정체성 placeholder 부재(repo/slot 실값만).
  - 사용자 입력(`T-NNNN`·`<PFX>`·`<요약>`)은 placeholder 로 남는다(ADR-0045 §Decision 1).
  - 숨은 전제 4대장(claim/prefix/livegate/migrate-identity) + reid=홈 git clean 이 해당
    커맨드 줄 바로 아래 1줄 ⚠ 경고로 인접(ADR-0045 §Decision 2 — 인접성이 학습 보장).
  - "정체성 불요" 절(ticket_finish·additional_reviewer·pm_log·pm_update·domain)·"자기 것 보기"
    (--mine 우선·전체 보드 강등·ADR-0047)·"찾아가기" 포인터 절 존재.
  - 솔로(정체성 미해소)는 `--repo`/`--slot` 없는 형태로 분기 · fail-soft(렌더 실패=None).
  - **drift 가드(durable)**: dump 된 board.py/pm_handoff.py 커맨드 전건이 실 CLI argparse 로
    `parse_args` 가능(카드↔CLI 정합·D1 canonical 못박기). 카드 카피가 CLI 와 어긋나면 red.
    정체성 토큰(`--repo`/`--slot`)은 공용 `identity_args` canonical grammar 로 별도 검증한다
    (board.py/pm_handoff.py 의 실 CLI 채택은 T-0314/T-0316 몫 — 이 가드가 그 병행 롤아웃에
    coupling 되지 않게 구조(subcommand·비-정체성 옵션)와 정체성을 분리 검증한다).

엔진 canonical(루트 .project_manager/tools/*.py)을 importlib 로 직접 검증한다. 카드 렌더는
순수 함수(identity dict → str)라 대부분 I/O 없이 헬퍼를 직접 호출한다. run() 통합 2건만
worktree_pool/board/git/log 를 DI mock 으로 hermetic 하게 구동한다(실 장부·git 미접촉).
"""
from __future__ import annotations

from _pool_seams import bind_pool

import argparse
import importlib.util
import inspect
import re
import shlex
from pathlib import Path

import pytest

from _slot_seed import fake_slot_path
from pm_engine.infra.harness import watchdog

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def bootstrap():
    return _load("pm_bootstrap")


@pytest.fixture(autouse=True)
def _hermetic_engine_anchor(bootstrap, monkeypatch):
    """0단계 엔진 앵커 검사(T-0351)를 hermetic 무력화한다 (worktree ①에서 로드돼 실 REPO 가 등록
    worktree 사본으로 보이는 문제 회피·test_pm_bootstrap_lease 동형). 실 board 를 로드해
    `_pm_home_worktree_misanchor`→None 만 패치하고 board=None 경로가 그 패치본을 받게 한다."""
    real_board = bootstrap._load_board()
    if real_board is not None:
        monkeypatch.setattr(real_board, "_pm_home_worktree_misanchor",
                            lambda anchor, **_kw: None, raising=False)
    monkeypatch.setattr(bootstrap, "_load_board", lambda: real_board)


@pytest.fixture(autouse=True)
def _no_codex_env(bootstrap, monkeypatch):
    """codex 하네스 env 마커(`CODEX_THREAD_ID`/`CODEX_CI`)를 기본 제거 — 이 모듈 전 테스트를 ambient
    codex 세션과 무관하게 결정론화한다(codex 절 부재가 기본·기존 카드 회귀 무변). codex 절 출현을
    검증하는 테스트는 본문에서 명시 `monkeypatch.setenv` 로 opt-in 한다(그 setenv 가 이 fixture 의
    delenv 뒤에 실행돼 우선)."""
    for marker in bootstrap._CODEX_HARNESS_SESSION_MARKERS:
        monkeypatch.delenv(marker, raising=False)


@pytest.fixture(scope="module")
def board_mod():
    return _load("board")


@pytest.fixture(scope="module")
def handoff_mod():
    return _load("pm_handoff")


# 정체성 dict — lean(멀티-PM) 모드가 카드에 넘기는 형태(`_bind_and_identity` 산출과 동형).
LEAN_IDENTITY = {
    "repo": "project_manager",
    "session": "project_manager_1",
    "slot": "work/project_manager_1",
    "slot_path": "/home/x/work/project_manager_1",
    "branch": "release/v1.0.6",
    "others": [],
    "protected_branch": None,
}


def _card(bootstrap, identity):
    """PmBootstrap 인스턴스 없이 카드 헬퍼만 호출한다(순수 함수·I/O 0)."""
    inst = bootstrap.PmBootstrap.__new__(bootstrap.PmBootstrap)
    return inst._build_command_card_markdown(identity)


def test_codex_harness_marker_table_matches_delegate_source(bootstrap):
    """부트스트랩 복제 선언은 위임 엔진의 codex 축 단일 출처와 정확히 같아야 한다."""
    assert bootstrap._CODEX_HARNESS_SESSION_MARKERS == \
        watchdog.HARNESS_SESSION_MARKERS["codex"]


def test_no_codex_env_fixture_iterates_bootstrap_marker_declaration():
    """ambient env 정리는 literal 사본이 아니라 bootstrap 선언을 순회해야 한다."""
    source = inspect.getsource(_no_codex_env)
    assert "for marker in bootstrap._CODEX_HARNESS_SESSION_MARKERS:" in source
    assert 'delenv("CODEX_THREAD_ID"' not in source
    assert 'delenv("CODEX_CI"' not in source


# ── 1. 정체성 실값 보간 — placeholder 부재 (DoD ①) ────────────────────────────


# ── 2. 숨은 전제 4대장 + reid 인접 경고 (DoD ①) ──────────────────────────────


def _line_index(card: str, needle: str) -> int:
    lines = card.splitlines()
    for i, line in enumerate(lines):
        if needle in line:
            return i
    raise AssertionError(f"카드에 {needle!r} 줄이 없음")


def _line_containing(card: str, needle: str) -> str:
    """`needle` 을 담은 첫 줄 전체를 돌려준다(스코프 문구 단언용)."""
    for line in card.splitlines():
        if needle in line:
            return line
    raise AssertionError(f"카드에 {needle!r} 줄이 없음")


# ── 3. 정체성 불요 절 (DoD ①·ADR-0045 §Decision 3) ───────────────────────────


# ── 4. 자기 것 보기 가이드 — --mine 우선·전체 보드 강등 (ADR-0047·메모) ────────


# ── 5. 찾아가기 포인터 절 (사용자 "찾아가는 법 가이드") ────────────────────────


# ── 6. 솔로/멀티 분기 렌더 (DoD ③) ───────────────────────────────────────────


def _command_lines(card: str) -> list[str]:
    """카드에서 플랫폼 Python 실행 커맨드 줄만 추린다.

    헤더 산문(솔로 안내의 "`--repo`/`--slot` 명시 불요" 등)은 커맨드가 아니므로 제외한다 —
    "커맨드에 정체성 인자가 붙나" 판정은 실행 줄만 봐야 정확하다. trailing `# 주석`도 잘라내
    순수 커맨드 부분만 돌려준다.
    """
    out: list[str] = []
    for ln in card.splitlines():
        s = ln.strip()
        if re.match(r"^(?:python(?:3)?|py -3(?:\.\d+)?) \.project_manager/tools/", s):
            out.append(s.split("  #", 1)[0].rstrip())
    return out


def _has_identity_flag(command_line: str) -> bool:
    """커맨드 줄이 정체성 인자 `--repo`/`--slot` 을 (exact 토큰) 담는지 (ADR-0057 신 표기)."""
    tokens = command_line.split()
    return "--repo" in tokens or "--slot" in tokens


# ── 7. fail-soft (DoD ③·ADR-0045 Consequences) ───────────────────────────────


# ── 8. drift 가드 (durable·DoD ②·ADR-0045 §Decision 5) ───────────────────────

_TOOL_RE = re.compile(
    r"(?:python(?:3)?|py -3(?:\.\d+)?) "
    r"\.project_manager/tools/(board\.py|pm_handoff\.py)\s+(.+)"
)


def _iter_card_cli_commands(card: str):
    """카드에서 board.py/pm_handoff.py 커맨드를 (tool, arg-tokens) 로 뽑는다.

    - 마크다운 리스트 마커·backtick·trailing 주석(`# …`)을 벗겨 CLI 인자만 남긴다.
    - 정체성 불요 절의 ticket_finish/additional_reviewer/... 는 board.py/pm_handoff.py 가 아니라
      매칭 안 됨(예시형·`...` 포함) — 정체성-bearing canonical 커맨드만 가드 대상.
    """
    for raw in card.splitlines():
        line = raw.strip().strip("`").strip()
        m = _TOOL_RE.search(line)
        if not m:
            continue
        tool = m.group(1)
        args_str = m.group(2).rstrip("`").strip()
        # comments=True — trailing `# 주석` 을 shlex 가 토큰에서 제거(값 안의 # 은 없음).
        tokens = shlex.split(args_str, comments=True)
        yield tool, tokens


def _split_identity_tokens(tokens: list[str]) -> tuple[list[str], list[str]]:
    """토큰열에서 정체성 인자 페어(`--repo <val>`·`--slot <val>`)를 분리한다 (ADR-0057).

    카드는 이미 새 canonical 표기(`--repo/--slot`)로 정체성을 보간하지만, 각 CLI(board.py·
    pm_handoff.py) 의 실 argparse 채택은 별도 티켓(T-0314·T-0316) 몫이라 이 worktree 시점에
    아직 landing 되지 않았을 수 있다 — 이 드리프트 가드가 그 병행 롤아웃 타이밍에 결합되지
    않도록, 정체성 토큰은 공용 `identity_args` canonical grammar 로, 나머지(subcommand·기타
    옵션)는 실 CLI argparse 로 각각 독립 검증한다(`_assert_command_parses`).
    """
    identity: list[str] = []
    rest: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("--repo", "--slot") and i + 1 < len(tokens):
            identity.extend([tok, tokens[i + 1]])
            i += 2
            continue
        rest.append(tok)
        i += 1
    return identity, rest


def _assert_command_parses(tool: str, tokens: list[str], parsers: dict, ia) -> None:
    """토큰을 정체성/구조 부분으로 나눠 각각 실 파서로 parse 를 단언한다(drift 가드 공용 코어).

    구조(subcommand·비-정체성 옵션) 부분은 실 CLI(board.py/pm_handoff.py) argparse 로 —
    카드 카피가 옵션 rename·필수 인자 추가 등으로 어긋나면 여기서 red. 정체성 부분
    (`--repo`/`--slot`)은 공용 `identity_args` canonical grammar(`add_identity_args` +
    `parse_identity`)로 — 도구 실 CLI 의 --repo/--slot 채택(T-0314·T-0316)과 무관하게
    ADR-0057 해소 규칙 위반(예: slot<1)을 잡는다.
    """
    identity_tokens, rest_tokens = _split_identity_tokens(tokens)
    try:
        parsers[tool].parse_args(rest_tokens)
    except SystemExit as exc:  # argparse 는 parse 실패 시 SystemExit.
        pytest.fail(f"카드 커맨드가 {tool} CLI 로 parse 실패: {rest_tokens} (exit={exc.code})")
    if not identity_tokens:
        return
    id_parser = argparse.ArgumentParser()
    ia.add_identity_args(id_parser)
    try:
        ns = id_parser.parse_args(identity_tokens)
    except SystemExit as exc:
        pytest.fail(
            f"정체성 토큰이 identity_args grammar 로 parse 실패: {identity_tokens} (exit={exc.code})"
        )
    try:
        ia.parse_identity(ns)
    except ValueError as exc:
        pytest.fail(f"정체성 토큰이 ADR-0057 해소 규칙 위반: {identity_tokens} ({exc})")


def test_card_drift_guard_is_sensitive(bootstrap, board_mod):
    """sensitivity — 존재하지 않는 옵션을 넣은 가짜 카드 줄은 가드가 잡는다(non-vacuous)."""
    parser = board_mod.build_parser()
    # 실 CLI 에 없는 옵션 — parse_args 가 SystemExit 로 실패해야 가드가 유효.
    with pytest.raises(SystemExit):
        parser.parse_args(["claim", "T-0001", "--no-such-flag", "x"])


# ── 9. run() 통합 — 카드가 identity surface 뒤에 실제로 emit 되나 (false-green 방지) ──


class _FakeLease:
    def __init__(self, slot: str, repo: str, branch: str | None):
        self.slot = slot
        self.repo = repo
        self.branch = branch


class _FakeWorktreePool:
    """카드 통합용 최소 worktree_pool mock — list/current_branch/slot_path 만."""

    def __init__(self, *, branch: str | None = "feature-x", present_slot: str = "work/X_2"):
        self.NeedsCreate = RuntimeError
        self._branch = branch
        self._present_slot = present_slot  # 0단계 실재 검사(T-0351)용 idle 시드 슬롯.

    def lease_is_open(self, lease):
        """열림 판정은 엔진 규칙 그대로 — 열림 기록(leased + session)이면 열림."""
        return getattr(lease, "state", None) == "leased" and bool(getattr(lease, "session", ""))

    def list_leases(self):
        # 0단계 실재 검사(T-0351) 통과용 idle 시드 — phase-0 는 slot/state/session/extra 만 읽는다.
        from types import SimpleNamespace
        return [SimpleNamespace(slot=self._present_slot, repo="", session="", state="idle", extra={})]

    def current_branch(self, slot, *, git_runner=None):
        return self._branch

    def slot_path(self, slot):
        return fake_slot_path(slot)

    def slot_git_status(self, slot, *, git_runner=None):
        # `_slot_era_info` 가 슬롯 바인딩마다 부르는 대역 — 카드 통합만 보므로 parent 없음(None).
        return {"slot": slot, "branch": self._branch, "head": None, "parent": None,
                "children": [], "stashes": 0, "submodules": [], "dirty": False}


def _make_bootstrap(bootstrap, tmp_path, *, worktree_pool=None, monkeypatch):
    """격리된 PmBootstrap — board/git/log/pm_state 는 hermetic stub(실 자산 미접촉)."""
    log_file = tmp_path / "current.md"
    log_file.write_text("# log\n", encoding="utf-8")
    areas_file = tmp_path / "areas.md"
    areas_file.write_text("", encoding="utf-8")
    pm_state_file = tmp_path / "pm_state.md"
    pm_state_file.write_text("", encoding="utf-8")

    def fake_board(args):
        if args[:1] == ["lint"]:
            return 0, "✓ no lint issues\n"
        return 0, "  [open   ] T-0001  something  pm  tag\n"

    def fake_git(args):
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return 0, "main\n"
        if args[:2] == ["log", "--oneline"]:
            return 0, "abc123 commit subject\n"
        return 0, ""

    return bootstrap.PmBootstrap(
        run_board_fn=fake_board,
        run_pytest_fn=lambda: (_ for _ in ()).throw(AssertionError("pytest 호출 안 됨")),
        run_git_fn=fake_git,
        log_file=log_file,
        areas_file=areas_file,
        worktree_pool=bind_pool(monkeypatch, worktree_pool),
        pm_state_file=pm_state_file,
    )


# ── 10. 스킬-우선 재구성 — wave op 스킬 primary·backbone 강등 (T-0280 · ADR-0052) ──

# 정체성 헤더 직후에 오는 스킬-우선 운영 pointer(규칙/why 는 pm_role — 이유 재설명 없음).
_SKILL_POINTER = "> wave 운영은 스킬로 invoke·backbone 직접호출 금지 → pm_role §스킬 우선 운영 규율"

# wave op → (스킬 진입 needle, 스킬-고유 강등 CLI 엔진 needle) — 스킬이 강등 엔진 줄보다 먼저/primary.
# needle 은 해당 스킬 아래에서 *처음* 등장하는 CLI 엔진(카탈로그 sub-op 중복 없는 것)이어야 한다.
# /pm-qa(엔진 regression/lint 가 /pm-wave-claim·/pm-regression 과 중복)는 전용 block-scope 테스트로
# 검증(아래 test_card_qa_demotes_regression_and_lint). /pm-dev-delegate(Agent 툴)·/pm-update(facade
# 셸)는 CLI 강등 줄이 없어 여기서 제외.
_SKILL_FIRST_OPS = [
    ("/pm-wave-claim T-NNNN", "board.py claim T-NNNN"),
    ("/pm-regression", "board.py regression run"),
    ("/pm-wave-finish T-NNNN", "ticket_finish.py <T-NNNN>"),
    ("/pm-handoff", "pm_handoff.py"),
]


# ── 11. 엔진 귀속 교정 — pm_role 카탈로그 정합 (T-0280 codex must-fix 1~4) ─────────


# ── 12. codex 하네스 카드 절 — env 감지 (T-0405 · ADR-0069/0070 C-v2 · spike §3.5) ──
# codex 전용 정적 진입 doc 이 없는 C-v2 구조에서, 부트스트랩 카드가 codex 실행모델/위임 지침의
# 전달 채널이다. env 마커(`CODEX_THREAD_ID`/`CODEX_CI`) 감지 시에만 카드 끝에 codex 절이 붙고,
# 미설정 시 절 부재=정상(다른 하네스 카드 무변·회귀 0). env 는 monkeypatch 로 양방향 제어한다.


def test_is_codex_harness_predicate(bootstrap, monkeypatch):
    """`_is_codex_harness()` = `CODEX_THREAD_ID` 또는 `CODEX_CI` 존재의 기계 판정(양 마커·부재)."""
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.delenv("CODEX_CI", raising=False)
    assert bootstrap._is_codex_harness() is False
    monkeypatch.setenv("CODEX_THREAD_ID", "019f8003-d535-7a10-....")
    assert bootstrap._is_codex_harness() is True
    monkeypatch.delenv("CODEX_THREAD_ID")
    monkeypatch.setenv("CODEX_CI", "1")
    assert bootstrap._is_codex_harness() is True

