"""부트스트랩 커맨드 카드 (T-0250 · ADR-0045·0047) 단위 테스트.

`pm_bootstrap._build_command_card_markdown(identity)` 는 이 세션이 쓸 전 커맨드를 정체성
(`--session <repo>_<N>`·ADR-0043 canonical) 채운 완성형으로 코드 생성 dump 한다
("--help 자체를 안 가게"·사용자 지시). 검증 축:

  - 정체성 실값 보간 — 카드에 `<repo>_<N>` 류 정체성 placeholder 부재(session 실값만).
  - 사용자 입력(`T-NNNN`·`<PFX>`·`<요약>`)은 placeholder 로 남는다(ADR-0045 §Decision 1).
  - 숨은 전제 4대장(claim/prefix/livegate/migrate-identity) + reid=홈 git clean 이 해당
    커맨드 줄 바로 아래 1줄 ⚠ 경고로 인접(ADR-0045 §Decision 2 — 인접성이 학습 보장).
  - "정체성 불요" 절(ticket_finish·external_review·pm_log·pm_update·domain)·"자기 것 보기"
    (--mine 우선·전체 보드 강등·ADR-0047)·"찾아가기" 포인터 절 존재.
  - 솔로(정체성 미해소)는 `--session` 없는 형태로 분기 · fail-soft(렌더 실패=None).
  - **drift 가드(durable)**: dump 된 board.py/pm_handoff.py 커맨드 전건이 실 CLI argparse 로
    `parse_args` 가능(카드↔CLI 정합·D1 canonical 못박기). 카드 카피가 CLI 와 어긋나면 red.

엔진 canonical(루트 .project_manager/tools/*.py)을 importlib 로 직접 검증한다. 카드 렌더는
순수 함수(identity dict → str)라 대부분 I/O 없이 헬퍼를 직접 호출한다. run() 통합 2건만
worktree_pool/board/git/log 를 DI mock 으로 hermetic 하게 구동한다(실 장부·git 미접촉).
"""
from __future__ import annotations

import importlib.util
import re
import shlex
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
def bootstrap():
    return _load("pm_bootstrap")


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


# ── 1. 정체성 실값 보간 — placeholder 부재 (DoD ①) ────────────────────────────


def test_card_identity_is_real_value_not_placeholder(bootstrap):
    """카드에 정체성이 실값(`project_manager_1`)으로 보간되고 placeholder 문자가 없다."""
    card = _card(bootstrap, LEAN_IDENTITY)
    # 세션 실값이 `--session` 인자로 채워졌다.
    assert "--session project_manager_1" in card
    # 정체성 placeholder(`<repo>_<N>`·`<session>`·`<N>` 세션형)가 문자 그대로 남지 않았다.
    assert "<repo>_<N>" not in card
    assert "<session>" not in card
    assert "--session <" not in card, "정체성 placeholder 가 남음(실값 보간 안 됨)"


def test_card_keeps_user_input_placeholders(bootstrap):
    """사용자 입력(`T-NNNN`·`<PFX>`·`<요약>`)은 placeholder 로 남는다(ADR-0045 §1 — 허용)."""
    card = _card(bootstrap, LEAN_IDENTITY)
    for token in ("T-NNNN", "<PFX>", "<요약>", "<OLD-ID>", "<NEW-ID>"):
        assert token in card, f"사용자 입력 placeholder {token!r} 가 카드에 없음"


# ── 2. 숨은 전제 4대장 + reid 인접 경고 (DoD ①) ──────────────────────────────


def _line_index(card: str, needle: str) -> int:
    lines = card.splitlines()
    for i, line in enumerate(lines):
        if needle in line:
            return i
    raise AssertionError(f"카드에 {needle!r} 줄이 없음")


def test_card_claim_precondition_adjacent(bootstrap):
    """claim 경고(promote 선행)가 claim 커맨드 줄 바로 아래에 인접한다(4대장 ①)."""
    card = _card(bootstrap, LEAN_IDENTITY)
    lines = card.splitlines()
    claim_i = _line_index(card, "board.py claim T-NNNN")
    warn = lines[claim_i + 1]
    assert "⚠" in warn and "promote" in warn and "draft" in warn


def test_card_prefix_precondition_adjacent(bootstrap):
    """prefix rename·merge 경고(홈 git clean)가 각 커맨드 줄 바로 아래에 인접한다(4대장 ②)."""
    card = _card(bootstrap, LEAN_IDENTITY)
    lines = card.splitlines()
    for cmd_needle in ("prefix rename", "prefix merge"):
        ci = _line_index(card, cmd_needle)
        warn = lines[ci + 1]
        assert "⚠" in warn and "clean" in warn, f"{cmd_needle} 경고 인접 실패: {warn!r}"


def test_card_livegate_precondition_adjacent(bootstrap):
    """livegate record 경고(release-marked pin)가 커맨드 줄 바로 아래에 인접한다(4대장 ③)."""
    card = _card(bootstrap, LEAN_IDENTITY)
    lines = card.splitlines()
    lg_i = _line_index(card, "livegate record")
    warn = lines[lg_i + 1]
    assert "⚠" in warn and "release" in warn and "pin" in warn


def test_card_migrate_identity_precondition_adjacent(bootstrap):
    """migrate-identity 경고(단일세션)가 커맨드 줄 바로 아래에 인접한다(4대장 ④)."""
    card = _card(bootstrap, LEAN_IDENTITY)
    lines = card.splitlines()
    mi_i = _line_index(card, "migrate-identity")
    warn = lines[mi_i + 1]
    assert "⚠" in warn and "단일" in warn


def test_card_reid_precondition_adjacent(bootstrap):
    """reid 경고(홈 git clean·T-0259 사용자 발의)가 커맨드 줄 바로 아래에 인접한다."""
    card = _card(bootstrap, LEAN_IDENTITY)
    lines = card.splitlines()
    reid_i = _line_index(card, "board.py reid")
    warn = lines[reid_i + 1]
    assert "⚠" in warn and "clean" in warn


# ── 3. 정체성 불요 절 (DoD ①·ADR-0045 §Decision 3) ───────────────────────────


def test_card_identity_free_section(bootstrap):
    """"정체성 불요" 절에 cwd/conf/env 자동해소 커맨드 5종이 명시된다."""
    card = _card(bootstrap, LEAN_IDENTITY)
    assert "정체성 불요" in card
    for tool in ("ticket_finish.py", "external_review.py", "pm_log.py",
                 "pm_update.py", "domain.py"):
        assert tool in card, f"정체성 불요 절에 {tool} 누락"


# ── 4. 자기 것 보기 가이드 — --mine 우선·전체 보드 강등 (ADR-0047·메모) ────────


def test_card_mine_guide_precedes_full_board(bootstrap):
    """`list --mine` 이 기본 조회로 앞서고, 전체 보드 뷰는 "타 PM 열람용·평시 불요" 로 강등."""
    card = _card(bootstrap, LEAN_IDENTITY)
    mine_i = _line_index(card, "list --mine")
    # 전체 보드 줄 = `board.py list`(필터 없음) + "타 PM 열람용" 강등 주석.
    full_i = _line_index(card, "타 PM 열람용")
    assert mine_i < full_i, "--mine 이 전체 보드보다 뒤에 옴(ADR-0047 자기 공간 우선 위배)"
    # 자기 세션 렌즈도 기본 조회면에 함께 앞세운다(=--mine 명시형).
    assert "list --session project_manager_1" in card


# ── 5. 찾아가기 포인터 절 (사용자 "찾아가는 법 가이드") ────────────────────────


def test_card_navigation_pointer_section(bootstrap):
    """찾아가기 절 — show/log/대시보드/architecture/decisions/pm_role 포인터 + "필요시만"."""
    card = _card(bootstrap, LEAN_IDENTITY)
    assert "찾아가기" in card and "필요할 때만" in card
    for pointer in ("board.py show T-NNNN", "wiki/log/current.md", "대시보드",
                    "wiki/architecture.md", "wiki/decisions/README.md", "wiki/pm_role.md"):
        assert pointer in card, f"찾아가기 포인터 {pointer!r} 누락"


# ── 6. 솔로/멀티 분기 렌더 (DoD ③) ───────────────────────────────────────────


def _command_lines(card: str) -> list[str]:
    """카드에서 실행 커맨드 줄만(`python3 .project_manager/tools/…` 로 시작) 추린다.

    헤더 산문(솔로 안내의 "`--session` 명시 불요" 등)은 커맨드가 아니므로 제외한다 —
    "커맨드에 --session 이 붙나" 판정은 실행 줄만 봐야 정확하다. trailing `# 주석`(예:
    "complete 는 --session 없음")도 잘라내 순수 커맨드 부분만 돌려준다.
    """
    out: list[str] = []
    for ln in card.splitlines():
        s = ln.strip()
        if s.startswith("python3 .project_manager/tools/"):
            out.append(s.split("  #", 1)[0].rstrip())
    return out


def _has_session_flag(command_line: str) -> bool:
    """커맨드 줄이 정체성 인자 `--session` 을 (exact 토큰) 담는지 — `--session-seq` 는 별개."""
    return "--session" in command_line.split()


def test_card_solo_branch_omits_session(bootstrap):
    """솔로(identity None)는 `--session` 없는 현행 형태로 분기한다(커맨드 줄에 --session 부재)."""
    card = _card(bootstrap, None)
    # 실행 커맨드 줄 어디에도 정체성 --session 이 붙지 않는다(`--session-seq` 는 별개 토큰).
    for line in _command_lines(card):
        assert not _has_session_flag(line), f"솔로 커맨드에 --session 이 붙음: {line!r}"
    # 솔로 헤더 명시 + claim/regression 이 --session 없이 렌더.
    assert "솔로(단일 세션)" in card
    assert "board.py claim T-NNNN" in card
    assert "board.py regression run" in card
    # 자기 세션 렌즈 줄(list --session)은 세션이 없으니 생략된다.
    assert "list --session" not in card


def test_card_lean_branch_fills_session(bootstrap):
    """lean(session 있음)은 정체성 헤더 + actor 커맨드에 --session 을 채운다."""
    card = _card(bootstrap, LEAN_IDENTITY)
    assert "세션=`project_manager_1`" in card
    assert "board.py claim T-NNNN --session project_manager_1" in card
    assert "board.py regression run --session project_manager_1" in card


def test_card_missing_session_key_renders_solo_defensive(bootstrap):
    """방어적 fail-soft: `session` 키가 **정말로 없는**(결손/불완전) identity 는 솔로 형태로
    graceful 렌더(카드 절이 안 깨짐). 단 이건 결손 dict 방어일 뿐 — 정상 alloc 경로는 아래
    `test_alloc_identity_includes_session` 대로 session 을 채운다(codex T-0250)."""
    broken_identity = {"repo": "A", "slot": "work/A_2", "slot_path": "/x/work/A_2",
                       "branch": "a5", "registered_repos": ["A"]}  # session 결손(비정상)
    card = _card(bootstrap, broken_identity)
    for line in _command_lines(card):
        assert not _has_session_flag(line)
    assert "솔로(단일 세션)" in card


def test_alloc_identity_includes_session(bootstrap):
    """codex T-0250 must-fix: `--repo` alloc 경로 identity 가 `session`(슬롯키)을 포함해야 한다 —
    없으면 카드가 멀티-PM 을 솔로로 오판해 `--session` 빠진 claim 을 안내(fail-loud 유발)."""
    class _StubLease:
        slot = "work/project_manager_2"

    class _StubPool:
        class NeedsCreate(Exception):
            pass
        def alloc(self, repo, branch=None, resume=None):
            return _StubLease()
        def slot_path(self, slot):
            return f"/x/{slot}"
        def current_branch(self, slot):
            return "b5"

    inst = bootstrap.PmBootstrap()
    inst._resolve_worktree_pool = lambda: _StubPool()
    identity = inst._alloc_and_identity("project_manager", None, None)
    assert identity.get("session") == "project_manager_2", "alloc identity 는 슬롯키를 session 으로 채운다."
    # 그 identity 로 렌더한 카드는 actor 커맨드에 --session 을 채운다(솔로 오판 0).
    card = _card(bootstrap, identity)
    assert "board.py claim T-NNNN --session project_manager_2" in card
    assert "솔로" not in card


# ── 7. fail-soft (DoD ③·ADR-0045 Consequences) ───────────────────────────────


def test_safe_command_card_failsoft_returns_none(bootstrap):
    """카드 렌더가 예외를 던지면 `_safe_command_card` 는 None(카드 절 생략·부트스트랩 유지)."""
    inst = bootstrap.PmBootstrap.__new__(bootstrap.PmBootstrap)

    class _Boom(dict):
        def get(self, *_a, **_k):
            raise RuntimeError("render boom")

    # identity.get(...) 가 터지는 병리 dict — 비어있지 않게 만들어 truthy(`if identity`
    # 통과) 로 실제 .get 을 타게 한다. _build_command_card_markdown 이 예외를 던지면
    # _safe_command_card 가 흡수해 None 을 돌려준다(카드 절 생략·부트스트랩 유지).
    boom = _Boom()
    dict.__setitem__(boom, "session", "x")  # 비어있지 않게(truthy) — .get 은 여전히 raise.
    assert inst._safe_command_card(boom) is None


def test_safe_command_card_success_returns_card(bootstrap):
    """정상 identity 면 `_safe_command_card` 가 카드 문자열을 돌려준다(fail-soft 우회 아님)."""
    inst = bootstrap.PmBootstrap.__new__(bootstrap.PmBootstrap)
    card = inst._safe_command_card(LEAN_IDENTITY)
    assert card is not None and "커맨드 카드" in card


# ── 8. drift 가드 (durable·DoD ②·ADR-0045 §Decision 5) ───────────────────────

_TOOL_RE = re.compile(r"python3 \.project_manager/tools/(board\.py|pm_handoff\.py)\s+(.+)")


def _iter_card_cli_commands(card: str):
    """카드에서 board.py/pm_handoff.py 커맨드를 (tool, arg-tokens) 로 뽑는다.

    - 마크다운 리스트 마커·backtick·trailing 주석(`# …`)을 벗겨 CLI 인자만 남긴다.
    - 정체성 불요 절의 ticket_finish/external_review/... 는 board.py/pm_handoff.py 가 아니라
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


def test_card_commands_parse_against_real_cli(bootstrap, board_mod, handoff_mod):
    """dump 된 board.py/pm_handoff.py 커맨드 전건이 실 CLI argparse 로 parse 가능하다.

    카드 카피가 각 CLI help 와 어긋나면(옵션 rename·필수 인자 추가 등) parse 가 SystemExit
    로 실패해 이 가드가 red 가 된다 — 카드↔CLI 정합을 못박는 durable 가드(D1 canonical).
    """
    card = _card(bootstrap, LEAN_IDENTITY)
    parsers = {
        "board.py": board_mod.build_parser(),
        "pm_handoff.py": handoff_mod.build_parser(),
    }
    commands = list(_iter_card_cli_commands(card))
    # 카드가 커맨드를 실제로 담고 있어야 한다(vacuous-pass 방지 — 추출 0 이면 가드 무의미).
    assert len(commands) >= 12, f"카드에서 추출된 CLI 커맨드가 너무 적음({len(commands)})"
    for tool, tokens in commands:
        try:
            parsers[tool].parse_args(tokens)
        except SystemExit as exc:  # argparse 는 parse 실패 시 SystemExit.
            pytest.fail(f"카드 커맨드가 {tool} CLI 로 parse 실패: {tokens} (exit={exc.code})")


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
    """카드 통합용 최소 worktree_pool mock — bind/list/current_branch/slot_path 만."""

    def __init__(self, *, branch: str | None = "feature-x"):
        self.NeedsCreate = RuntimeError
        self._branch = branch
        self._bound: list[str] = []

    def bind_slot(self, slot, repo, session, *, git_runner=None):
        self._bound.append(slot)
        return _FakeLease(slot, repo, self._branch)

    def list_leases(self):
        return []

    def current_branch(self, slot, *, git_runner=None):
        return self._branch

    def slot_path(self, slot):
        return Path("/tmp/multipm") / slot


def _make_bootstrap(bootstrap, tmp_path, *, worktree_pool=None):
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
        worktree_pool=worktree_pool,
        pm_state_file=pm_state_file,
    )


def test_run_lean_emits_card_after_identity_surface(bootstrap, tmp_path, capsys):
    """run() lean 모드가 identity surface *뒤*에 커맨드 카드를 emit 한다."""
    wp = _FakeWorktreePool(branch="feature-x")
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=wp)
    rc = inst.run(repo="X", slot=2)
    assert rc == 0
    out = capsys.readouterr().out
    # identity surface 와 카드가 모두 나오고, 카드가 identity surface 뒤에 온다.
    assert "multi-PM identity surface" in out
    assert "이 세션 커맨드 카드" in out
    assert out.index("multi-PM identity surface") < out.index("이 세션 커맨드 카드")
    # 정체성 실값(X_2)이 카드 커맨드에 채워졌다.
    assert "board.py claim T-NNNN --session X_2" in out


def test_run_solo_emits_solo_card(bootstrap, tmp_path, capsys):
    """run() 솔로(무인자·자동바인딩 미해소)가 --session 없는 솔로 카드를 emit 한다."""
    inst = _make_bootstrap(bootstrap, tmp_path)  # worktree_pool 없음 → 솔로 경로
    rc = inst.run()
    assert rc == 0
    out = capsys.readouterr().out
    assert "이 세션 커맨드 카드" in out
    assert "솔로(단일 세션)" in out
    # 솔로 카드의 실행 커맨드 줄엔 --session 이 붙지 않는다(헤더 산문/주석 언급은 제외).
    card_section = out.split("이 세션 커맨드 카드", 1)[1]
    for line in _command_lines(card_section):
        assert not _has_session_flag(line), f"솔로 카드 커맨드에 --session: {line!r}"


def test_run_json_mode_omits_card(bootstrap, tmp_path, capsys):
    """--json 모드(기계 파싱)는 카드를 싣지 않는다(카드=사람-facing markdown 전용)."""
    wp = _FakeWorktreePool(branch="feature-x")
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=wp)
    inst.run(repo="X", slot=2, output_json=True)
    out = capsys.readouterr().out
    assert "이 세션 커맨드 카드" not in out
