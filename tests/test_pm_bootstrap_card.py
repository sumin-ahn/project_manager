"""부트스트랩 커맨드 카드 (T-0250 · ADR-0045·0047·0057) 단위 테스트.

`pm_bootstrap._build_command_card_markdown(identity)` 는 이 세션이 쓸 전 커맨드를 정체성
(`--repo <repo> --slot <N>`·ADR-0057 canonical — 구 ADR-0043 `--session <repo>_<N>` 을
supersede) 채운 완성형으로 코드 생성 dump 한다("--help 자체를 안 가게"·사용자 지시). 검증 축:

  - 정체성 실값 보간 — 카드에 `<repo>_<N>` 류 정체성 placeholder 부재(repo/slot 실값만).
  - 사용자 입력(`T-NNNN`·`<PFX>`·`<요약>`)은 placeholder 로 남는다(ADR-0045 §Decision 1).
  - 숨은 전제 4대장(claim/prefix/livegate/migrate-identity) + reid=홈 git clean 이 해당
    커맨드 줄 바로 아래 1줄 ⚠ 경고로 인접(ADR-0045 §Decision 2 — 인접성이 학습 보장).
  - "정체성 불요" 절(ticket_finish·external_review·pm_log·pm_update·domain)·"자기 것 보기"
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

import argparse
import importlib.util
import inspect
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


@pytest.fixture(scope="module")
def ia_mod():
    """공용 `identity_args` — 카드가 내는 정체성 토큰(`--repo`/`--slot`)의 canonical grammar 원천."""
    return _load("identity_args")


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
    delegate = _load("pm_delegate")
    assert bootstrap._CODEX_HARNESS_SESSION_MARKERS == \
        delegate._load_relay().HARNESS_SESSION_MARKERS["codex"]


def test_no_codex_env_fixture_iterates_bootstrap_marker_declaration():
    """ambient env 정리는 literal 사본이 아니라 bootstrap 선언을 순회해야 한다."""
    source = inspect.getsource(_no_codex_env)
    assert "for marker in bootstrap._CODEX_HARNESS_SESSION_MARKERS:" in source
    assert 'delenv("CODEX_THREAD_ID"' not in source
    assert 'delenv("CODEX_CI"' not in source


# ── 1. 정체성 실값 보간 — placeholder 부재 (DoD ①) ────────────────────────────


def test_card_identity_is_real_value_not_placeholder(bootstrap):
    """카드에 정체성이 실값(`project_manager`·`1`)으로 보간되고 placeholder 문자가 없다(ADR-0057)."""
    card = _card(bootstrap, LEAN_IDENTITY)
    # 정체성 실값이 `--repo`/`--slot` 인자로 채워졌다.
    assert "--repo project_manager --slot 1" in card
    # 정체성 placeholder(`<repo>_<N>`·`<session>`·`<N>` 세션형)가 문자 그대로 남지 않았다.
    assert "<repo>_<N>" not in card
    assert "<session>" not in card
    assert "--repo <" not in card, "정체성 placeholder 가 남음(실값 보간 안 됨)"
    assert "--slot <" not in card, "정체성 placeholder 가 남음(실값 보간 안 됨)"


def test_card_keeps_user_input_placeholders(bootstrap):
    """사용자 입력(`T-NNNN`·`<PFX>`·`<요약>`)은 placeholder 로 남는다(ADR-0045 §1 — 허용)."""
    card = _card(bootstrap, LEAN_IDENTITY)
    for token in ("T-NNNN", "<PFX>", "<요약>", "<OLD-ID>", "<NEW-ID>"):
        assert token in card, f"사용자 입력 placeholder {token!r} 가 카드에 없음"


def test_card_slot_number_from_slot_identifier_not_session(bootstrap):
    """T-0390 codex must-fix — `--slot <N>` 은 슬롯 식별자(`work/<repo>_<N>`)에서 파생, session 아님.

    task+slot 경로에선 `identity["session"]` 이 task명(`job1`)이라 옛 `session.rsplit("_",1)[-1]`
    전제가 깨져 `--slot job1` 류 오염 명령을 낳는다. 카드 렌더가 슬롯 식별자를 원천으로 쓰면
    session 이 task명이어도 항상 실 슬롯 번호(`2`)를 채운다 — 전제 깨진 값이 흘러들어도 기계적으로
    닫힘(task 모드가 실제로 이 슬롯 카드 경로를 안 타더라도 방어)."""
    task_session_identity = {
        "repo": "A",
        "session": "job1",            # task 명의(슬롯 번호 아님) — 옛 파생이 오염되던 지점.
        "slot": "work/A_2",           # 실 슬롯 식별자 — 슬롯 번호 원천.
        "slot_path": "/home/x/work/A_2",
        "branch": "a5",
        "others": [],
        "protected_branch": None,
    }
    card = _card(bootstrap, task_session_identity)
    assert "--repo A --slot 2" in card       # 슬롯 번호는 slot 식별자에서 정확히 파생
    assert "--slot job1" not in card         # session(task명) 오염 방지(must-fix)


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


def test_card_livegate_guidance_is_executable_with_repo_slot(bootstrap):
    """lean(멀티-PM) 카드의 livegate record 안내가 실행가능 형태 — `--repo <repo> --slot <N>` 포함
    (T-0298·ADR-0057 신 표기).

    multi-lease 홈에서 정체성 인자 없는 record 는 cwd 모호 fail-loud 이므로, 안내 명령이 이 세션
    정체성을 실어야 dead-end 가 아니다.
    """
    card = _card(bootstrap, LEAN_IDENTITY)
    lines = card.splitlines()
    lg_i = _line_index(card, "livegate record")
    assert "--repo project_manager --slot 1" in lines[lg_i], \
        f"livegate 안내가 --repo/--slot 을 안 실음(multi-lease dead-end): {lines[lg_i]!r}"


def test_card_solo_livegate_guidance_omits_repo_slot(bootstrap):
    """솔로(정체성 None)는 `livegate record`(무인자·현행 형태) — leased <2 라 폴백 무변경(T-0298)."""
    card = _card(bootstrap, None)
    lines = card.splitlines()
    lg_i = _line_index(card, "livegate record")
    assert "--repo" not in lines[lg_i], f"솔로 livegate 안내에 --repo 가 붙음: {lines[lg_i]!r}"
    assert "--slot" not in lines[lg_i], f"솔로 livegate 안내에 --slot 이 붙음: {lines[lg_i]!r}"


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
    """정체성-free CLI 도구가 카드에 명시된다(--repo/--slot 불요·ADR-0045 §Decision 3).

    "정체성 불요" 절(pm_log·domain 조회) + wave 절의 정체성-free CLI 엔진(ticket_finish·
    external_review)이 노출된다. pm_update.py 는 facade(/pm-update) 뒤로 감춰져 raw CLI 로
    노출하지 않는다(ADR-0052·must-fix #4 — facade 우회 금지)."""
    card = _card(bootstrap, LEAN_IDENTITY)
    assert "정체성 불요" in card
    for tool in ("ticket_finish.py", "external_review.py", "pm_log.py", "domain.py"):
        assert tool in card, f"정체성-free 도구 {tool} 누락"
    assert "pm_update.py" not in card, "facade 우회 raw pm_update.py 가 카드에 렌더됨"


# ── 4. 자기 것 보기 가이드 — --mine 우선·전체 보드 강등 (ADR-0047·메모) ────────


def test_card_mine_guide_precedes_full_board(bootstrap):
    """`list --mine` 이 기본 조회로 앞서고, 전체 보드 뷰(`list --all`)는 "타 PM 열람·평시 불요" 로 강등."""
    card = _card(bootstrap, LEAN_IDENTITY)
    mine_i = _line_index(card, "list --mine")
    # 전체 보드 줄 = `board.py list --all`(ADR-0066·기존 무인자 전체 뷰 이관) + "타 PM 열람" 강등 주석.
    full_i = _line_index(card, "타 PM 열람")
    assert mine_i < full_i, "--mine 이 전체 보드보다 뒤에 옴(ADR-0047 자기 공간 우선 위배)"
    assert "list --all" in card, "전체 보드 뷰가 `list --all` 로 렌더 안 됨(ADR-0066)"
    # 자기 슬롯 렌즈(--repo/--slot)도 기본 조회면에 함께 앞세운다(내 것 ∩ 이 슬롯·user-first).
    assert "list --repo project_manager --slot 1" in card


def test_card_distinguishes_mine_and_slot_scope(bootstrap):
    """**user-first (ADR-0056·T-0312)**: 카드가 `--mine`(내 것 전 슬롯) vs `--repo/--slot`(내 것 ∩ 이
    슬롯) 의 스코프를 명확히 구분한다 — PM 이 "내 슬롯 작업" 조회 커맨드를 카드만 보고 안다.

    `--repo/--slot` 을 "=--mine 명시형"(동등)으로 오표기하면 slot ∩ 를 전 슬롯으로 오독한다(정정 회귀 가드).
    """
    card = _card(bootstrap, LEAN_IDENTITY)
    mine_line = _line_containing(card, "list --mine")
    slot_line = _line_containing(card, "list --repo project_manager --slot 1")
    # --mine 은 "전 슬롯" 명시 · --repo/--slot 은 "이 슬롯" 명시(스코프 구분).
    assert "전 슬롯" in mine_line, f"--mine 줄이 전-슬롯 스코프를 안 밝힘: {mine_line!r}"
    assert "이 슬롯" in slot_line, f"--repo/--slot 줄이 이-슬롯 스코프를 안 밝힘: {slot_line!r}"
    # 옛 "=--mine 명시형"(동등) 오표기가 남지 않았다(둘은 이제 스코프가 다르다).
    assert "=--mine 명시형" not in card


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


def test_card_unresolved_branch_omits_identity_flags(bootstrap):
    """정체성 미해소는 인자를 지어내지 않고 빈 형태로 분기한다(값 위조 0)."""
    card = _card(bootstrap, None)
    # 실행 커맨드 줄 어디에도 정체성 인자가 붙지 않는다(채울 실값이 없다).
    for line in _command_lines(card):
        assert not _has_identity_flag(line), f"미해소 커맨드에 정체성 인자가 붙음: {line!r}"
    # 미해소 헤더 명시 + claim/regression 이 정체성 인자 없이 렌더.
    assert "정체성: 미해소" in card
    assert "board.py claim T-NNNN" in card
    assert "board.py regression run" in card
    # 자기 슬롯 렌즈 줄(list --repo/--slot)은 정체성이 없으니 생략된다.
    assert "list --repo" not in card


def test_card_lean_branch_fills_repo_slot(bootstrap):
    """lean(session 있음)은 정체성 헤더 + actor 커맨드에 `--repo/--slot` 을 채운다(ADR-0057)."""
    card = _card(bootstrap, LEAN_IDENTITY)
    assert "세션=`project_manager_1`" in card
    assert "board.py claim T-NNNN --repo project_manager --slot 1" in card
    assert "board.py regression run --repo project_manager --slot 1" in card


def test_card_missing_session_key_renders_unresolved_defensive(bootstrap):
    """방어적 fail-soft: `session` 키가 **정말로 없는**(결손/불완전) identity 는 미해소 형태로
    graceful 렌더(카드 절이 안 깨짐). 단 이건 결손 dict 방어일 뿐 — 정상 alloc 경로는 아래
    `test_alloc_identity_includes_session` 대로 session 을 채운다(codex T-0250)."""
    broken_identity = {"repo": "A", "slot": "work/A_2", "slot_path": "/x/work/A_2",
                       "branch": "a5", "registered_repos": ["A"]}  # session 결손(비정상)
    card = _card(bootstrap, broken_identity)
    for line in _command_lines(card):
        assert not _has_identity_flag(line)
    assert "정체성: 미해소" in card


def test_alloc_identity_includes_session(bootstrap):
    """codex T-0250 must-fix: `--repo` alloc 경로 identity 가 `session`(슬롯키)을 포함해야 한다 —
    없으면 카드가 멀티-PM 을 솔로로 오판해 정체성 인자 빠진 claim 을 안내(fail-loud 유발)."""
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
    # 그 identity 로 렌더한 카드는 actor 커맨드에 --repo/--slot 을 채운다(솔로 오판 0·ADR-0057).
    card = _card(bootstrap, identity)
    assert "board.py claim T-NNNN --repo project_manager --slot 2" in card
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

_TOOL_RE = re.compile(
    r"(?:python(?:3)?|py -3(?:\.\d+)?) "
    r"\.project_manager/tools/(board\.py|pm_handoff\.py)\s+(.+)"
)


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


def test_card_commands_parse_against_real_cli(bootstrap, board_mod, handoff_mod, ia_mod):
    """dump 된 board.py/pm_handoff.py 커맨드 전건이 실 CLI argparse 로 parse 가능하다(구조
    부분) + 정체성 토큰(`--repo`/`--slot`)이 ADR-0057 canonical grammar 로 parse 가능하다
    (identity_args). 카드 카피가 각 CLI help 와 어긋나면(옵션 rename·필수 인자 추가 등) 구조
    parse 가 SystemExit 로 실패해 이 가드가 red 가 된다 — 카드↔CLI 정합을 못박는 durable
    가드(D1 canonical). 도구 실 CLI 의 --repo/--slot 채택은 별도 티켓(T-0314·T-0316) 몫이라
    이 가드는 그 병행 롤아웃에도 결정적이다(`_assert_command_parses` 분리 검증).
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
        _assert_command_parses(tool, tokens, parsers, ia_mod)


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

    def __init__(self, *, branch: str | None = "feature-x", present_slot: str = "work/X_2"):
        self.NeedsCreate = RuntimeError
        self._branch = branch
        self._bound: list[str] = []
        self._present_slot = present_slot  # 0단계 실재 검사(T-0351)용 idle 시드 슬롯.

    def bind_slot(self, slot, repo, session, *, git_runner=None):
        self._bound.append(slot)
        return _FakeLease(slot, repo, self._branch)

    def list_leases(self):
        # 0단계 실재 검사(T-0351) 통과용 idle 시드 — phase-0 는 slot/state/session/extra 만 읽는다.
        from types import SimpleNamespace
        return [SimpleNamespace(slot=self._present_slot, repo="", session="", state="idle", extra={})]

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
    # 정체성 실값(X·2)이 카드 커맨드에 채워졌다(ADR-0057).
    assert "board.py claim T-NNNN --repo X --slot 2" in out


def test_run_unresolved_emits_unresolved_card(bootstrap, tmp_path, capsys):
    """run() 미해소(무인자·자동바인딩 실패)가 정체성 인자 없는 카드를 emit 한다."""
    inst = _make_bootstrap(bootstrap, tmp_path)  # worktree_pool 없음 → 미해소 경로
    rc = inst.run()
    assert rc == 0
    out = capsys.readouterr().out
    assert "이 세션 커맨드 카드" in out
    assert "정체성: 미해소" in out
    # 미해소 카드의 실행 커맨드 줄엔 정체성 인자가 붙지 않는다(헤더 산문/주석 언급은 제외).
    card_section = out.split("이 세션 커맨드 카드", 1)[1]
    for line in _command_lines(card_section):
        assert not _has_identity_flag(line), f"미해소 카드 커맨드에 정체성 인자: {line!r}"


def test_run_json_mode_omits_card(bootstrap, tmp_path, capsys):
    """--json 모드(기계 파싱)는 카드를 싣지 않는다(카드=사람-facing markdown 전용)."""
    wp = _FakeWorktreePool(branch="feature-x")
    inst = _make_bootstrap(bootstrap, tmp_path, worktree_pool=wp)
    inst.run(repo="X", slot=2, output_json=True)
    out = capsys.readouterr().out
    assert "이 세션 커맨드 카드" not in out


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


@pytest.mark.parametrize("identity", [LEAN_IDENTITY, None])
def test_card_skill_first_pointer_after_identity_header(bootstrap, identity):
    """카드 상단(정체성 헤더 직후·첫 섹션 앞)에 pm_role 스킬 우선 규율 pointer 1줄 — lean/솔로 양분기.

    규칙·why 는 재설명하지 않고(ADR-0045 비중복) pm_role 규율 절을 가리키는 1줄 pointer 만 둔다.
    """
    card = _card(bootstrap, identity)
    assert _SKILL_POINTER in card
    pointer_i = _line_index(card, "pm_role §스킬 우선 운영 규율")
    first_section_i = _line_index(card, "# 내 작업 보기")
    assert pointer_i < first_section_i, "pointer 가 첫 섹션보다 뒤에 옴(정체성 헤더 직후 아님)"
    # pointer 는 정확히 1줄(이유 산문 미포함·ADR-0045 비중복).
    assert card.count(_SKILL_POINTER) == 1


@pytest.mark.parametrize("identity", [LEAN_IDENTITY, None])
def test_card_wave_op_skill_precedes_backbone(bootstrap, identity):
    """스킬 있는 wave op(claim·regression·finish·위임·handoff·update)은 `/pm-…` 진입이 강등된
    backbone 줄보다 먼저/primary(lean·솔로 양분기·ADR-0052 Decision 2)."""
    card = _card(bootstrap, identity)
    for skill_needle, backbone_needle in _SKILL_FIRST_OPS:
        skill_i = _line_index(card, skill_needle)
        backbone_i = _line_index(card, backbone_needle)
        assert skill_i < backbone_i, (
            f"{skill_needle!r} 스킬 줄이 backbone {backbone_needle!r} 뒤에 옴(강등 실패)"
        )


def test_card_skilled_ops_render_all_seven_skill_entries(bootstrap):
    """매핑 표의 스킬 진입 7종(qa 포함)이 모두 카드에 렌더된다(스킬 누락 0)."""
    card = _card(bootstrap, LEAN_IDENTITY)
    for skill_entry in ("/pm-wave-claim", "/pm-regression", "/pm-wave-finish",
                        "/pm-qa", "/pm-dev-delegate", "/pm-handoff", "/pm-update"):
        assert skill_entry in card, f"스킬 진입 {skill_entry!r} 누락"


def test_card_skill_lines_excluded_from_argparse_guard(bootstrap, board_mod, handoff_mod, ia_mod):
    """불변식 3: `/pm-…` 스킬 줄은 카드↔CLI argparse 정합 가드의 파싱 대상이 아니다.

    가드(`_iter_card_cli_commands`)는 backbone `python3 …/board.py|pm_handoff.py` 줄만 추출한다 —
    스킬 줄이 그 가드에 파싱돼 SystemExit 로 깨지지 않는다. 강등 backbone 줄은 여전히 추출·parse
    대상으로 남아(불변식 3·가드 non-vacuous) 카드↔CLI 정합을 계속 못박는다.
    """
    card = _card(bootstrap, LEAN_IDENTITY)
    # 카드에 스킬 진입 줄이 실제로 있다(비공허 — 가드 우회가 아니라 실제 배치).
    skill_lines = [ln for ln in card.splitlines() if ln.strip().startswith("/pm-")]
    assert len(skill_lines) >= 7, f"스킬 진입 줄이 너무 적음({len(skill_lines)})"
    # 가드 추출물은 board.py/pm_handoff.py 뿐이고 어떤 스킬 토큰(`pm-…`)도 섞이지 않는다.
    commands = list(_iter_card_cli_commands(card))
    for tool, tokens in commands:
        assert tool in ("board.py", "pm_handoff.py")
        assert not any("pm-" in tok for tok in tokens), f"스킬 줄이 가드에 유입: {tool} {tokens}"
    # 강등 backbone 은 여전히 전건 parse (스킬 줄 제외해도 가드 공허 아님).
    assert len(commands) >= 12, f"강등 후 backbone 추출이 너무 적음({len(commands)})"
    parsers = {"board.py": board_mod.build_parser(), "pm_handoff.py": handoff_mod.build_parser()}
    for tool, tokens in commands:
        _assert_command_parses(tool, tokens, parsers, ia_mod)


def test_card_demoted_backbone_keeps_identity_interpolation(bootstrap):
    """불변식 1: 강등된 backbone 줄도 정체성 `--repo <repo> --slot <N>` 실값 보간 유지(lean·
    ADR-0057), 솔로는 정체성 인자 없는 분기 유지(강등이 정체성 보간을 깨지 않음)."""
    lean = _card(bootstrap, LEAN_IDENTITY)
    assert "board.py claim T-NNNN --repo project_manager --slot 1" in lean
    assert "board.py regression run --repo project_manager --slot 1" in lean
    assert "pm_handoff.py --repo project_manager --slot 1 --session-seq <N>" in lean
    assert "--repo <" not in lean, "강등 backbone 에 정체성 placeholder 잔존"
    assert "--slot <" not in lean, "강등 backbone 에 정체성 placeholder 잔존"
    solo = _card(bootstrap, None)
    # 솔로 실행 줄(강등 backbone 포함)엔 정체성 인자가 붙지 않는다.
    for line in _command_lines(solo):
        assert not _has_identity_flag(line), f"솔로 강등 backbone 에 정체성 인자: {line!r}"
    assert "board.py claim T-NNNN" in solo and "board.py regression run" in solo


def test_card_wave_claim_warning_stays_adjacent_after_demotion(bootstrap):
    """불변식 2: 강등 후에도 claim 숨은전제 ⚠(promote 선행)이 강등 backbone claim 줄 바로 아래 인접."""
    for identity in (LEAN_IDENTITY, None):
        card = _card(bootstrap, identity)
        lines = card.splitlines()
        claim_i = _line_index(card, "board.py claim T-NNNN")
        assert "⚠" in lines[claim_i + 1] and "promote" in lines[claim_i + 1]


# ── 11. 엔진 귀속 교정 — pm_role 카탈로그 정합 (T-0280 codex must-fix 1~4) ─────────


def test_card_finish_demotes_only_ticket_finish(bootstrap):
    """must-fix #2: /pm-wave-finish 의 강등 CLI 엔진은 `ticket_finish.py` 하나뿐.

    ticket_finish 가 내부서 complete 를 수행하므로 별도 `board.py complete` 강등 줄을 finish
    아래 두면 수동 double-complete 를 유도한다 → finish 블록엔 complete 강등 줄이 없다. complete
    직접줄은 fresh-adopter/concept 경로로 wave 절 *앞*(lifecycle 직접)에 유지(usability 게이트)."""
    card = _card(bootstrap, LEAN_IDENTITY)
    lines = card.splitlines()
    # 스킬 진입 줄 자체로 앵커한다(complete 직접줄 주석의 "/pm-wave-finish" 언급과 충돌 회피).
    finish_i = _line_index(card, "/pm-wave-finish T-NNNN")
    qa_i = _line_index(card, "/pm-qa")
    finish_block = lines[finish_i:qa_i]
    assert any("ticket_finish.py" in ln for ln in finish_block)
    # 커맨드 *부분*(주석 `#` 앞)에 board.py complete 가 없어야 한다 — ticket_finish 강등 줄의
    # "내부서 board.py complete 수행" 설명 주석은 실행 줄이 아니라 무관.
    finish_cmd_parts = [ln.split("#", 1)[0] for ln in finish_block]
    assert not any("board.py complete" in part for part in finish_cmd_parts), \
        "board.py complete 가 /pm-wave-finish 강등 엔진 커맨드로 남음(double-complete 유도)"
    # complete 직접줄은 finish 스킬보다 앞(강등처럼 보이지 않게·lifecycle 직접 경로).
    assert _line_index(card, "board.py complete") < finish_i


def test_card_wave_claim_block_has_show_lint_claim(bootstrap):
    """codex must-fix: /pm-wave-claim 강등 엔진 = board.py show/lint/claim 전부(카탈로그 순서
    show→lint→claim). show/lint 는 DoD self-containment 검증 단계(read-only·⚠ 없음), claim 은
    mutating·전제 ⚠ 인접. lean/솔로 양분기."""
    for identity in (LEAN_IDENTITY, None):
        card = _card(bootstrap, identity)
        lines = card.splitlines()
        claim_skill_i = _line_index(card, "/pm-wave-claim T-NNNN")
        reg_skill_i = _line_index(card, "/pm-regression")
        block = lines[claim_skill_i:reg_skill_i]
        # 셋 다 강등 엔진 줄로 존재.
        assert any("board.py show T-NNNN" in ln for ln in block), "claim 블록에 show 누락"
        assert any("board.py lint" in ln for ln in block), "claim 블록에 lint 누락"
        assert any("board.py claim T-NNNN" in ln for ln in block), "claim 블록에 claim 누락"
        # 카탈로그 순서 show → lint → claim.
        show_i = _line_index(card, "board.py show T-NNNN")
        lint_i = next(i for i, ln in enumerate(lines) if "board.py lint" in ln)  # 첫 lint=claim 블록
        claim_i = _line_index(card, "board.py claim T-NNNN")
        assert show_i < lint_i < claim_i, "show→lint→claim 순서 위배"
        # ⚠ 전제는 claim 줄 바로 아래 인접(불변식 2) — show/lint 는 read-only 라 ⚠ 없음.
        assert "⚠" in lines[claim_i + 1] and "promote" in lines[claim_i + 1]
        assert "⚠" not in lines[show_i + 1] and "⚠" not in lines[lint_i + 1]


def test_card_qa_demotes_regression_and_lint(bootstrap):
    """must-fix #3: /pm-qa 도 skill primary + 강등 엔진 패턴 — 엔진=board.py regression/lint."""
    card = _card(bootstrap, LEAN_IDENTITY)
    lines = card.splitlines()
    qa_i = _line_index(card, "/pm-qa")
    delegate_i = _line_index(card, "/pm-dev-delegate")
    qa_block = lines[qa_i:delegate_i]
    assert any("board.py regression run" in ln for ln in qa_block), "qa 강등 엔진에 regression 누락"
    assert any("board.py lint" in ln for ln in qa_block), "qa 강등 엔진에 lint 누락"


def test_card_external_review_is_direct_sibling_gate(bootstrap):
    """must-fix #1: external_review 는 /pm-dev-delegate 의 강등 엔진이 아니라 직후 sibling
    직접-CLI 게이트(래핑 스킬 없음·직접 OK 예외). dev-delegate 엔진은 Agent 툴(python3 줄 없음)."""
    card = _card(bootstrap, LEAN_IDENTITY)
    lines = card.splitlines()
    delegate_i = _line_index(card, "/pm-dev-delegate")
    review_i = _line_index(card, "external_review.py")
    assert delegate_i < review_i, "external_review 가 dev-delegate 앞(직후 sibling 아님)"
    # '직접 금지' 강등 프레이밍이 아니라 '직접(래핑 스킬 없음)' 게이트로 표기.
    review_line = lines[review_i]
    assert "직접" in review_line and "직접 금지" not in review_line
    # dev-delegate 바로 아래는 Agent 툴 평문 note(python3 CLI 강등 줄이 아님).
    delegate_note = lines[delegate_i + 1]
    assert "Agent" in delegate_note and not delegate_note.strip().startswith("python3")


def test_card_facade_engines_are_skill_only(bootstrap):
    """must-fix #4 + 일반원칙: facade(pm-update.sh) 엔진은 raw pm_update.py python3 줄로 안 그린다 —
    skill-only + 평문 note. raw pm_update.py 는 카드에 없다(facade 우회 금지)."""
    card = _card(bootstrap, LEAN_IDENTITY)
    assert "/pm-update" in card
    assert "pm_update.py" not in card, "facade 우회 raw pm_update.py 가 카드에 렌더됨"
    assert "pm-update.sh 파사드" in card
    # /pm-update 바로 아래는 평문 facade note(python3 CLI 강등 줄이 아님).
    lines = card.splitlines()
    update_i = _line_index(card, "/pm-update")
    assert not lines[update_i + 1].strip().startswith("python3")


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


def test_card_codex_section_absent_without_env(bootstrap):
    """env 마커 부재(autouse 제거)면 카드에 codex 절이 없다 — 다른 하네스 카드 무변(회귀 0)."""
    card = _card(bootstrap, LEAN_IDENTITY)
    assert "codex 하네스" not in card
    assert ".codex/agents" not in card


def test_card_codex_section_present_with_all_three_elements(bootstrap, monkeypatch):
    """codex env 감지 시 카드 끝에 codex 절 + spike §3.5 3요소(위임 spawn·trust 2단계·방법론 소재)."""
    monkeypatch.setenv("CODEX_THREAD_ID", "019f8003-d535-7a10-....")
    card = _card(bootstrap, LEAN_IDENTITY)
    assert "# codex 하네스" in card
    # ① 위임 = 세션 내 spawn (.codex/agents 4축·`codex exec --agent` 부재).
    assert ".codex/agents/{architect,developer,code-reviewer,researcher}" in card
    assert "세션 내 spawn" in card
    assert "codex exec --agent" in card
    # ② trust 2단계 힌트(대화형 trust + `/hooks`·`-c` override 무효).
    assert "trust 2단계" in card
    assert "/hooks" in card
    assert "trust_level=trusted" in card
    # ③ 방법론 소재(공통 코어 AGENTS.md 자동 로드 + 이 카드 + `.agents/skills`·CLAUDE.md 미로드).
    assert "AGENTS.md" in card and ".agents/skills" in card
    assert "CLAUDE.md" in card and "미로드" in card


@pytest.mark.parametrize("marker", ["CODEX_THREAD_ID", "CODEX_CI"])
def test_card_codex_section_appears_for_each_marker(bootstrap, monkeypatch, marker):
    """두 실측 마커 각각(단독)으로 codex 절이 출현한다(OR predicate·spike §D3)."""
    monkeypatch.setenv(marker, "1")
    assert "# codex 하네스" in _card(bootstrap, LEAN_IDENTITY)


def test_card_codex_detection_derives_skill_prefix_and_appends_section(bootstrap, monkeypatch):
    """codex 감지는 본문 스킬을 ``$``로 파생하고 codex 설명 절을 끝에 붙인다."""
    body = _card(bootstrap, LEAN_IDENTITY)          # autouse 로 env 제거됨 → 절 부재
    assert "codex 하네스" not in body
    assert "/pm-wave-claim" in body and "$pm-wave-claim" not in body
    monkeypatch.setenv("CODEX_CI", "1")
    full = _card(bootstrap, LEAN_IDENTITY)
    assert "$pm-wave-claim" in full and "$pm-handoff" in full
    assert not re.search(
        r"(?<![A-Za-z0-9_.>/\-])/pm-[a-z][a-z0-9-]*",
        full,
    ), "codex 카드 mid-line 포함 claude slash 표기 잔존"
    assert "\n\n# codex 하네스" in full


@pytest.mark.parametrize("mode", ["slot", "solo", "task", "readonly"])
def test_card_codex_section_appended_in_all_modes(bootstrap, monkeypatch, mode):
    """codex 절은 모든 카드 모드(슬롯·솔로·task·readonly) 렌더 끝에 붙는다 — 카드=유일 전달 채널이라
    어느 모드로 부팅해도 codex PM 이 실행모델/위임 지침을 받는다(정적 doc 폴백 없음·C-v2)."""
    monkeypatch.setenv("CODEX_CI", "1")
    inst = bootstrap.PmBootstrap.__new__(bootstrap.PmBootstrap)
    if mode == "slot":
        card = inst._build_command_card_markdown(LEAN_IDENTITY)
    elif mode == "solo":
        card = inst._build_command_card_markdown(None)
    elif mode == "task":
        inst._task_name = "job1"
        card = inst._build_command_card_markdown(LEAN_IDENTITY)
    else:  # readonly
        card = inst._build_command_card_markdown({**LEAN_IDENTITY, "role": "readonly"})
    assert "# codex 하네스" in card, f"{mode} 모드 카드에 codex 절 누락"
    skill_lines = [
        line.strip() for line in card.splitlines()
        if line.strip().startswith(("/pm-", "$pm-"))
    ]
    assert skill_lines and all(line.startswith("$pm-") for line in skill_lines)


def test_task_card_execution_commands_use_task_only_identity(bootstrap):
    """task 카드의 regression/ticket_finish 실행-위치 명령에 repo/slot 병기가 재유입되지 않는다(E-c)."""
    inst = bootstrap.PmBootstrap.__new__(bootstrap.PmBootstrap)
    inst._task_name = "job1"
    card = inst._build_command_card_markdown(LEAN_IDENTITY)
    for needle in ("board.py regression run", "ticket_finish.py <T-NNNN>"):
        line = next(line for line in card.splitlines() if needle in line)
        assert "--task job1" in line
        assert "--repo" not in line
        assert "--slot" not in line


def test_safe_command_card_failsoft_covers_codex_detection(bootstrap, monkeypatch):
    """codex 감지/절 렌더가 fail-soft 경로 *안*이다 — 감지가 터져도 `_safe_command_card`=None
    (카드 절 생략·부트스트랩 무손상·ADR-0045). codex append 가 try/except 밖으로 새지 않음."""
    inst = bootstrap.PmBootstrap.__new__(bootstrap.PmBootstrap)

    def _boom():
        raise RuntimeError("codex detect boom")

    monkeypatch.setattr(bootstrap, "_is_codex_harness", _boom)
    assert inst._safe_command_card(LEAN_IDENTITY) is None


def test_safe_command_card_success_includes_codex_section_under_codex(bootstrap, monkeypatch):
    """codex 하네스에서 정상 렌더면 `_safe_command_card` 가 codex 절 담은 카드를 돌려준다(None 아님)."""
    monkeypatch.setenv("CODEX_THREAD_ID", "019f8003-d535-7a10-....")
    inst = bootstrap.PmBootstrap.__new__(bootstrap.PmBootstrap)
    card = inst._safe_command_card(LEAN_IDENTITY)
    assert card is not None and "# codex 하네스" in card
