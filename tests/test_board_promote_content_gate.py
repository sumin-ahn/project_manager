"""promote 내용 검토 게이트 — 본문이 *주장하는 사실* 판정 (형식 게이트와 별개 축).

형식 게이트(placeholder·thin·설계 절)는 "뼈대가 남았나"만 본다. 이 파일이 고정하는 축은 그
게이트가 구조적으로 못 보는 것들이다:

  1. **touches 실재** — 소유 repo 좌표(`repo_coordinates` 정규화 후 PM 홈 또는 슬롯 worktree)에서
     해소되는가. 부재는 **경고 1줄**이고 차단이 아니다(신설 예정 파일을 먼저 적는 것이 정상 운영).
  2. **`파일:줄` 인용** — 인용한 파일의 부재·줄 범위 초과는 **차단**. 하위 디렉터리 기준 표기
     (`wiki/decisions/x.md`)는 소유 트리에서 **유일하게** 해소될 때만 그 파일로 판정하고, 사본이
     여럿이면(그리고 basename 단독 `board.py:120` 이면) 어느 사본인지 확정할 수 없어 **판정불능
     개수**로 센다 — 어느 표기로도 실재 파일이 없으면 판정불능이 아니라 red 다(허위 인용).
  3. **architect 점검 라운드** — `design: required|done` 티켓은 점검 라운드가 회수·충전되기 전
     승격이 거부된다(초안 PM → 점검 architect → 비준 PM 3단의 기계 강제 지점). 판정은 라운드
     사이드카 소유(`load_rounds`·`round_is_pending`) 재사용이다.

**양방향**으로 고정한다 — 정상 티켓이 통과하는지(오차단 0)와 결함 티켓이 red 인지(민감도)를 같은
픽스처 집합에서 단언하고, 판정불능이 red 도 green 도 아닌 **개수**로 세어지는지도 단언한다.

경계: 겹침(다른 활성 티켓과 touches 교집합)은 이 게이트의 축이 **아니다** — 겹치는 티켓의
promote 성공을 명시 단언해 never-block 소유 경계를 코드로 못박는다.

hermetic: board.py 의 경로 전역은 import 시점에 실 repo 로 굳으므로 tmp 홈으로 재앵커한다
(`anchor_board_module`). board-git e2e 는 실 git + bare remote 를 tmp 에 세운다.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import anchor_board_module

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

requires_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="git 바이너리 부재 — 실 git 통합 케이스 skip.")

_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
}

# 새 축이 내는 kind 전수 — 형식 게이트 kind(placeholder·thin·design-pending)와 섞이지 않게
# 단언마다 이 집합으로 걸러 본다.
_NEW_KINDS = frozenset({
    "touches-missing", "citation-unresolved",
    "architect-review-pending", "content-unverifiable"})

# 하위 디렉터리 기준 표기 양방향 픽스처 — (소유 트리에 실제로 두는 경로, 본문이 적는 표기).
# 두 앵커(`wiki/`·`lib/`)를 함께 도는 이유: 실 board 의 미해소 인용이 문서 축(`wiki/…`)과 코드
# 축(`lib/…`) 양쪽에서 나왔고, 한쪽만 고정하면 다른 축의 강등을 못 본다.
_SUBDIR_CITATIONS = [
    (".project_manager/wiki/decisions/0001-x.md", "wiki/decisions/0001-x.md"),
    (".opencode/lib/guard.cjs", "lib/guard.cjs"),
]
# 같은 앵커인데 소유 트리 어디에도 없는 인용 — 이 게이트가 막으려는 허위 인용.
_SUBDIR_ABSENT = ["wiki/definitely-missing.md", "lib/definitely-missing.cjs"]
_SUBDIR_IDS = ["wiki", "lib"]

_DESIGN_FILLED = (
    "## 설계\n"
    "- **경계 실측**: 활성 티켓 전수를 게이트에 태워 오차단 수를 셌다.\n"
    "- **불변식**: 형식 게이트 판정·메시지는 무변경이다.\n"
    "- **표면 상한**: 차단은 인용 부재/범위초과와 점검 라운드 미회수 둘뿐.\n"
    "- **테스트 전략**: 양방향 픽스처로 red/green 을 고정한다.\n\n")


def _load_board():
    spec = importlib.util.spec_from_file_location(
        "board_content_gate", TOOLS / "board.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


board_mod = _load_board()


def _body(extra: str = "", *, design_section: str = "") -> str:
    """5절을 실값으로 채운 자족 본문(placeholder 0) + 케이스별 추가 문단."""
    return (
        "# T-0001 — 실 제목\n\n"
        "## 목표\n승격 순간에 본문 사실성을 판정한다.\n\n"
        "## 인터페이스\n승격 게이트가 인용과 touches 를 함께 본다.\n\n"
        "## 결정\n부재 touches 는 경고, 범위 밖 인용은 차단.\n\n"
        + design_section +
        "## 완료 조건 (Definition of Done)\n- [ ] 게이트 + 단위 테스트\n\n"
        "## 참고\n- 형식 게이트는 `_body_lint_issues` 단일 깔때기\n"
        + extra +
        "\n## 메모\n")


def _issues(mod, body, *, touches=None, design=None, tid="T-0001", content_facts=True):
    """새 축만 남긴 판정 목록 — 형식 게이트 kind 는 걸러낸다."""
    found = mod._body_lint_issues(
        tid, body, design=design, touches=touches, content_facts=content_facts)
    return [(kind, detail) for _tid, kind, detail in found if kind in _NEW_KINDS]


def _kinds(issues):
    return [kind for kind, _detail in issues]


@pytest.fixture
def anchored(tmp_path, monkeypatch):
    """board-git 없이 판정 함수만 태우는 tmp 홈 (board 루트 = `.project_manager/board`)."""
    mod = _load_board()
    anchor_board_module(mod, tmp_path, monkeypatch)
    (tmp_path / ".project_manager" / "board" / "tickets").mkdir(parents=True)
    return mod


def _write(root: Path, relative: str, text: str = "한 줄\n") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _register_slot(root: Path, slot: str = "work/demo_1", repo: str = "demo") -> Path:
    """리스 장부에 지속 slot↔repo 매핑을 심고 그 worktree 디렉터리를 만든다."""
    ledger = root / ".project_manager" / ".local" / "worktree-leases.json"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(
        json.dumps({"leases": [{"slot": slot, "repo": repo, "state": "leased"}]}),
        encoding="utf-8")
    path = root / slot
    path.mkdir(parents=True, exist_ok=True)
    return path


# ════════════════════════════════════════════════════════════════════════
# 1. touches 실재 — 경고 1줄(never-block) · 좌표는 단일 소유자가 정규화
# ════════════════════════════════════════════════════════════════════════

def test_touches_in_pm_home_is_silent(anchored, tmp_path):
    """PM 홈이 소유한 경로(문서 자산)는 무발화 — 정상 티켓 오차단 0."""
    _write(tmp_path, ".project_manager/wiki/pm_role.md")
    assert _issues(anchored, _body(),
                   touches=[".project_manager/wiki/pm_role.md"]) == []


def test_touches_only_in_slot_worktree_is_silent(anchored, tmp_path):
    """슬롯 worktree 만 소유한 경로(코드·테스트)도 해소된다 — 한 트리만 보면 정상이 부재가 된다."""
    slot = _register_slot(tmp_path)
    _write(slot, "tests/test_engine.py")
    assert _issues(anchored, _body(), touches=["tests/test_engine.py"]) == []


def test_missing_touches_is_one_warning_line_and_never_blocks(anchored, tmp_path):
    """어느 트리에도 없는 touches → 경고 **1줄**이고 그 kind 는 promote 비차단 집합이다."""
    issues = _issues(anchored, _body(),
                     touches=["tests/test_new.py", "tools/absent.py"])
    assert _kinds(issues) == ["touches-missing"], (
        f"부재 touches 는 경로 수와 무관하게 1줄이어야 한다: {issues!r}")
    detail = issues[0][1]
    assert "tests/test_new.py" in detail and "tools/absent.py" in detail
    assert "touches-missing" in board_mod._PROMOTE_ADVISORY_KINDS, \
        "touches 부재가 차단 축으로 승격됨 — 신설 예정 파일 발행이 막힌다."


def test_worktree_prefixed_touches_resolve_after_normalization(anchored, tmp_path):
    """`work/<repo>_<N>/` 접두는 좌표 단일 소유자가 검증·정규화한 뒤 그 워크스페이스에서 찾는다."""
    slot = _register_slot(tmp_path)
    _write(slot, "tools/engine.py")
    assert _issues(anchored, _body(), touches=["work/demo_1/tools/engine.py"]) == []


def test_worktree_prefixed_touches_without_ledger_are_unverifiable(anchored, tmp_path):
    """리스 장부가 없으면 접두를 검증할 수 없다 — red 도 green 도 아닌 판정불능 개수."""
    issues = _issues(anchored, _body(), touches=["work/demo_1/tools/engine.py"])
    assert _kinds(issues) == ["content-unverifiable"], issues
    assert "touches 좌표 1건" in issues[0][1]


# ════════════════════════════════════════════════════════════════════════
# 2. `파일:줄` 인용 — 해소되는 것만 판정 · 판정불능은 개수로 표면화
# ════════════════════════════════════════════════════════════════════════

def test_repo_relative_citation_in_range_is_silent(anchored, tmp_path):
    """실재 파일 + 줄 범위 안 인용은 무발화(오차단 0)."""
    _write(tmp_path, ".project_manager/tools/board.py", "a\nb\nc\n")
    body = _body("- 근거 `.project_manager/tools/board.py:3`\n")
    assert _issues(anchored, body) == []


def test_citation_beyond_line_count_is_blocking(anchored, tmp_path):
    """줄 범위를 넘는 인용은 차단 — 실측하지 않고 적은 숫자를 잡는 유일한 축."""
    _write(tmp_path, ".project_manager/tools/board.py", "a\nb\nc\n")
    body = _body("- 근거 `.project_manager/tools/board.py:41`\n")
    issues = _issues(anchored, body)
    assert _kinds(issues) == ["citation-unresolved"], issues
    assert "3줄" in issues[0][1], "실측 줄 수가 메시지에 없다."
    assert "citation-unresolved" not in board_mod._PROMOTE_ADVISORY_KINDS


def test_citation_missing_file_under_existing_anchor_is_blocking(anchored, tmp_path):
    """앵커 디렉터리는 실재하는데 파일이 없으면 차단 — 환각 경로."""
    _write(tmp_path, ".project_manager/tools/board.py", "a\n")
    body = _body("- 근거 `.project_manager/tools/absent_module.py:10`\n")
    assert _kinds(_issues(anchored, body)) == ["citation-unresolved"]


def test_basename_only_citation_is_counted_not_judged(anchored, tmp_path):
    """basename 단독 인용은 사본이 여럿이라 판정불능 — 개수로만 표면화한다."""
    _write(tmp_path, ".project_manager/tools/board.py", "a\n")
    body = _body("- 근거 `board.py:9999`\n")
    issues = _issues(anchored, body)
    assert _kinds(issues) == ["content-unverifiable"], issues
    assert "인용 1건" in issues[0][1]


@pytest.mark.parametrize("stored,cited", _SUBDIR_CITATIONS, ids=_SUBDIR_IDS)
def test_subdirectory_citation_resolving_uniquely_is_silent_in_range(
        anchored, tmp_path, stored, cited):
    """하위 디렉터리 기준 표기라도 소유 트리에서 유일하게 해소되면 통과한다(오차단 0)."""
    _write(tmp_path, stored, "a\nb\nc\n")
    assert _issues(anchored, _body(f"- 근거 `{cited}:2`\n")) == []


@pytest.mark.parametrize("stored,cited", _SUBDIR_CITATIONS, ids=_SUBDIR_IDS)
def test_subdirectory_citation_beyond_line_count_is_blocking(
        anchored, tmp_path, stored, cited):
    """유일 해소는 *판정*으로 이어진다 — 그 파일의 줄 범위를 넘으면 차단이다."""
    _write(tmp_path, stored, "a\nb\nc\n")
    issues = _issues(anchored, _body(f"- 근거 `{cited}:41`\n"))
    assert _kinds(issues) == ["citation-unresolved"], issues
    assert "3줄" in issues[0][1], "해소된 파일의 실측 줄 수가 메시지에 없다."


@pytest.mark.parametrize("cited", _SUBDIR_ABSENT, ids=_SUBDIR_IDS)
def test_subdirectory_citation_with_no_file_anywhere_is_blocking(
        anchored, tmp_path, cited):
    """어느 표기로도 실재 파일이 없으면 차단 — 판정불능 강등이 허위 인용을 삼키면 안 된다."""
    for stored, _cited in _SUBDIR_CITATIONS:      # 같은 앵커의 형제 파일은 실재한다
        _write(tmp_path, stored, "a\n")
    issues = _issues(anchored, _body(f"- 근거 `{cited}:1`\n"))
    assert _kinds(issues) == ["citation-unresolved"], issues
    assert "citation-unresolved" not in board_mod._PROMOTE_ADVISORY_KINDS


def test_subdirectory_citation_with_several_copies_is_counted_not_judged(
        anchored, tmp_path):
    """사본이 여럿이면 어느 파일의 줄 수인지 확정할 수 없다 — 부재 red 와 구분되는 판정불능."""
    slot = _register_slot(tmp_path)
    _write(tmp_path, ".opencode/lib/guard.cjs", "a\n")
    _write(slot, "templates/opencode/.opencode/lib/guard.cjs", "a\n")
    issues = _issues(anchored, _body("- 근거 `lib/guard.cjs:60`\n"))
    assert _kinds(issues) == ["content-unverifiable"], issues


def test_overlapping_roots_do_not_double_count_the_same_file(anchored, tmp_path):
    """PM 홈 ⊃ 슬롯 — 두 트리에서 같은 파일이 보여도 1건이다(중복 계수는 유일 해소를 지운다)."""
    slot = _register_slot(tmp_path)
    _write(slot, "docs/wiki/only-here.md", "a\nb\n")
    assert _issues(anchored, _body("- 근거 `wiki/only-here.md:2`\n")) == []
    issues = _issues(anchored, _body("- 근거 `wiki/only-here.md:9`\n"))
    assert _kinds(issues) == ["citation-unresolved"], issues


def test_files_inside_pruned_directories_do_not_resolve_a_citation(anchored, tmp_path):
    """순회 제외 디렉터리(객체 저장소·캐시)의 동명 파일은 해소 후보가 아니다."""
    _write(tmp_path, ".git/wiki/vendored.md", "a\n")
    assert _kinds(_issues(anchored, _body("- 근거 `wiki/vendored.md:1`\n"))) \
        == ["citation-unresolved"]


def test_citation_inside_a_code_fence_is_not_judged(anchored, tmp_path):
    """펜스 안 예시 블록의 경로는 본문 인용이 아니다 — 무판정(오탐 0)."""
    body = _body("\n```\n`.project_manager/tools/absent.py:12` 는 예시다\n```\n")
    assert _issues(anchored, body) == []


def test_same_citation_twice_is_judged_once(anchored, tmp_path):
    """같은 인용이 여러 번 나와도 같은 사실을 여러 줄로 내지 않는다."""
    _write(tmp_path, ".project_manager/tools/board.py", "a\n")
    body = _body("- 근거 `.project_manager/tools/board.py:7`\n"
                 "- 같은 근거 `.project_manager/tools/board.py:7`\n")
    assert _kinds(_issues(anchored, body)) == ["citation-unresolved"]


def test_off_tree_citation_is_counted_not_judged(anchored, tmp_path):
    """절대·홈 경로 인용은 repo 좌표가 아니다 — 판정불능 개수."""
    body = _body("- 런북 `/opt/vm/run.sh:3` · `~/vm/win11/README.md:12`\n")
    issues = _issues(anchored, body)
    assert _kinds(issues) == ["content-unverifiable"], issues
    assert "인용 2건" in issues[0][1]


# ════════════════════════════════════════════════════════════════════════
# 3. architect 점검 라운드 — design: required|done 만 대상
# ════════════════════════════════════════════════════════════════════════

def _seed_round(mod, tid: str, role: str, *, harvested: bool,
                ticket_text: str = "") -> Path:
    """라운드 파일을 시드하고(엔진 골격) 필요하면 산출로 덮어쓴다."""
    rounds = mod._load_ticket_rounds()
    directory = rounds.rounds_dir_for_ticket(tid, mod.tickets_dir())
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / rounds.round_filename(1, role)
    seed = rounds.render_round_seed(role, ticket_text or _body(), today="2026-01-01")
    path.write_text(seed, encoding="utf-8")
    if harvested:
        header = seed.splitlines()[0]
        path.write_text(f"{header}\n\n실측 대조 결과를 적었다.\n", encoding="utf-8")
    return path


def test_design_done_without_architect_round_is_blocking(anchored):
    """`design: done` 인데 점검 라운드가 없으면 차단 — 아무도 본문을 안 본 승격을 막는다."""
    issues = _issues(anchored, _body(design_section=_DESIGN_FILLED), design="done")
    assert _kinds(issues) == ["architect-review-pending"], issues
    assert "architect-review-pending" not in board_mod._PROMOTE_ADVISORY_KINDS


def test_seed_only_architect_round_is_blocking(anchored):
    """라운드 파일만 예약하고 시드 그대로면 산출이 없다 — 회수 전에는 통과하지 않는다."""
    _seed_round(anchored, "T-0001", "architect", harvested=False)
    issues = _issues(anchored, _body(design_section=_DESIGN_FILLED), design="done")
    assert _kinds(issues) == ["architect-review-pending"], issues
    assert "시드 그대로" in issues[0][1]


def test_harvested_architect_round_passes(anchored):
    """회수·충전된 점검 라운드가 있으면 무발화(정상 흐름 오차단 0)."""
    _seed_round(anchored, "T-0001", "architect", harvested=True)
    assert _issues(anchored, _body(design_section=_DESIGN_FILLED), design="done") == []


def test_developer_round_does_not_satisfy_the_review_axis(anchored):
    """다른 역할의 라운드는 점검이 아니다 — 역할로 구분한다(신원 아님)."""
    _seed_round(anchored, "T-0001", "developer", harvested=True)
    issues = _issues(anchored, _body(design_section=_DESIGN_FILLED), design="done")
    assert _kinds(issues) == ["architect-review-pending"], issues


@pytest.mark.parametrize("design", [None, "n/a", "waived: 설계 불요"])
def test_non_design_tickets_are_not_asked_for_a_review_round(anchored, design):
    """`n/a`·`waived`·필드 부재는 설계 단계 비대상 — 점검 라운드 강제가 발화하지 않는다."""
    assert _issues(anchored, _body(), design=design) == []


def test_design_required_reports_the_review_axis_too(anchored):
    """`required` 도 같은 축 대상이다(설계 절 충전 여부와 무관하게 점검은 받는다)."""
    issues = _issues(anchored, _body(design_section=_DESIGN_FILLED), design="required")
    assert _kinds(issues) == ["architect-review-pending"], issues


# ════════════════════════════════════════════════════════════════════════
# 4. 깔때기 격리 — 전역 lint 는 이 축을 켜지 않는다(소급 red 0 · push 게이트 무오염)
# ════════════════════════════════════════════════════════════════════════

def test_content_facts_off_yields_no_new_kinds(anchored, tmp_path):
    """같은 결함 입력이라도 `content_facts=False` 면 새 kind 가 0 — 기존 호출자 무변경."""
    body = _body("- 근거 `.project_manager/tools/absent.py:10`\n",
                 design_section=_DESIGN_FILLED)
    _write(tmp_path, ".project_manager/tools/board.py", "a\n")
    assert _issues(anchored, body, touches=["tools/absent.py"], design="done",
                   content_facts=False) == []


def test_global_lint_does_not_surface_the_new_kinds(anchored, tmp_path):
    """`lint_bodies`(→ `lint --gate`)는 open 티켓에 새 kind 를 내지 않는다 — 소급 red 0."""
    ticket = tmp_path / ".project_manager" / "board" / "tickets" / "open" / "T-0002-x.md"
    ticket.parent.mkdir(parents=True, exist_ok=True)
    body = _body("- 근거 `.project_manager/tools/absent.py:10`\n",
                 design_section=_DESIGN_FILLED)
    anchored.dump_ticket(ticket, {
        "id": "T-0002", "title": "x", "status": "open", "design": "done",
        "touches": ["tools/absent.py"], "depends_on": [], "blocks": [], "tags": [],
    }, body)
    kinds = {kind for _tid, kind, _detail in anchored.lint_bodies()}
    assert not (kinds & _NEW_KINDS), f"전역 lint 에 새 kind 가 샜다: {kinds!r}"


# ════════════════════════════════════════════════════════════════════════
# 5. e2e — 실 board-git 홈에서 cmd_promote rc 와 파일 이동
# ════════════════════════════════════════════════════════════════════════

def _git(argv, cwd):
    return subprocess.run(["git", *argv], cwd=str(cwd), capture_output=True,
                          text=True, encoding="utf-8", errors="replace", check=False)


@pytest.fixture
def board_git(tmp_path, monkeypatch):
    """board 가 별도 git(공유 형상)인 hermetic 홈 — draft 격리·promote 게이트가 작동한다."""
    mod = _load_board()
    anchor_board_module(mod, tmp_path, monkeypatch)
    for key, val in _GIT_IDENTITY.items():
        monkeypatch.setenv(key, val)
    board = tmp_path / ".project_manager" / "board"
    for status in ("open", "claimed", "blocked", "done"):
        (board / "tickets" / status).mkdir(parents=True, exist_ok=True)
    (board / "tickets" / "_template.md").write_text(
        (REPO / ".project_manager" / "wiki" / "tickets" / "_template.md")
        .read_text(encoding="utf-8"), encoding="utf-8")
    bare = tmp_path / "bare"
    steps = (
        (["init", "--bare", "-q", "-b", "main", str(bare)], tmp_path),
        (["init", "-q", "-b", "main"], board),
        (["remote", "add", "origin", str(bare)], board),
        (["add", "-A"], board),
        (["commit", "-qm", "board init"], board),
        (["push", "-q", "-u", "origin", "main"], board),
    )
    for argv, cwd in steps:
        r = _git(argv, cwd)
        assert r.returncode == 0, f"board-git setup 실패: git {argv} → {r.stderr}"
    mod._board_dir = board
    return mod


def _new_args(**overrides) -> argparse.Namespace:
    args = dict(title="내용 게이트", touches=None, depends=None, tag=None,
                estimate="small", prefix=None, user=None, session=None, design=None)
    args.update(overrides)
    return argparse.Namespace(**args)


def _draft(board_dir: Path) -> Path:
    return list((board_dir / "tickets" / ".drafts").glob("T-*-*.md"))[0]


def _fill_draft(mod, board_dir: Path, body: str, **frontmatter) -> str:
    """draft 를 발행하고 본문·frontmatter 를 케이스 형상으로 덮는다 → 티켓 ID."""
    assert mod.cmd_new(_new_args(**frontmatter.pop("new_args", {}))) == 0
    path = _draft(board_dir)
    fm, _ = mod.load_ticket(path)
    fm.update(frontmatter)
    mod.dump_ticket(path, fm, body)
    return fm["id"]


@requires_git
def test_promote_warns_but_ships_a_ticket_with_missing_touches(board_git, capsys):
    """부재 touches 는 경고 1줄이고 승격은 성공한다 — 신설 예정 파일이 발행을 막지 않는다."""
    board_dir = board_git._board_dir
    tid = _fill_draft(board_git, board_dir, _body(), touches=["tests/test_new.py"])

    assert board_git.cmd_promote(argparse.Namespace(id=tid)) == 0
    err = capsys.readouterr().err
    assert "touches-missing" in err and "tests/test_new.py" in err
    assert list((board_dir / "tickets" / "open").glob("T-*-*.md")), \
        "경고가 승격을 막았다 — 비차단 계약 위반."


@requires_git
def test_promote_rejects_a_citation_beyond_the_file(board_git, tmp_path, capsys):
    """줄 범위를 넘는 인용은 승격 거부(rc=1)·draft 잔류."""
    board_dir = board_git._board_dir
    _write(tmp_path, ".project_manager/tools/board.py", "a\nb\n")
    body = _body("- 근거 `.project_manager/tools/board.py:88`\n")
    tid = _fill_draft(board_git, board_dir, body)

    assert board_git.cmd_promote(argparse.Namespace(id=tid)) == 1
    assert "citation-unresolved" in capsys.readouterr().err
    assert list((board_dir / "tickets" / ".drafts").glob("T-*-*.md")), \
        "거부된 draft 는 .drafts/ 에 남아야 한다."
    assert not list((board_dir / "tickets" / "open").glob("T-*-*.md"))


@requires_git
@pytest.mark.parametrize("cited", _SUBDIR_ABSENT, ids=_SUBDIR_IDS)
def test_promote_rejects_a_subdirectory_citation_with_no_file_anywhere(
        board_git, tmp_path, capsys, cited):
    """실 CLI(`board.py promote`)에서 허위 인용은 rc=1 — 표기 편의가 승격을 통과시키지 않는다."""
    board_dir = board_git._board_dir
    for stored, _cited in _SUBDIR_CITATIONS:
        _write(tmp_path, stored, "a\n")
    tid = _fill_draft(board_git, board_dir, _body(f"- 근거 `{cited}:1`\n"))

    assert board_git.main(["promote", tid]) == 1
    assert "citation-unresolved" in capsys.readouterr().err
    assert list((board_dir / "tickets" / ".drafts").glob("T-*-*.md")), \
        "거부된 draft 는 .drafts/ 에 남아야 한다."
    assert not list((board_dir / "tickets" / "open").glob("T-*-*.md"))


@requires_git
@pytest.mark.parametrize("stored,cited", _SUBDIR_CITATIONS, ids=_SUBDIR_IDS)
def test_promote_ships_a_subdirectory_citation_that_resolves_uniquely(
        board_git, tmp_path, stored, cited):
    """같은 표기라도 유일 해소 + 범위 안이면 실 CLI 승격이 성공한다(오차단 0의 반대 방향)."""
    board_dir = board_git._board_dir
    _write(tmp_path, stored, "a\nb\nc\n")
    tid = _fill_draft(board_git, board_dir, _body(f"- 근거 `{cited}:3`\n"))

    assert board_git.main(["promote", tid]) == 0
    assert list((board_dir / "tickets" / "open").glob("T-*-*.md"))


@requires_git
def test_promote_requires_a_harvested_architect_round_then_accepts(board_git, capsys):
    """`design: done` 은 점검 라운드 회수 전 거부 → 회수 뒤 승격(같은 티켓 red→green)."""
    board_dir = board_git._board_dir
    tid = _fill_draft(board_git, board_dir, _body(design_section=_DESIGN_FILLED),
                      design="done")

    assert board_git.cmd_promote(argparse.Namespace(id=tid)) == 1
    assert "architect-review-pending" in capsys.readouterr().err

    assert board_git.cmd_section_add(
        argparse.Namespace(id=tid, role="architect", label=None)) == 0
    assert board_git.cmd_promote(argparse.Namespace(id=tid)) == 1, \
        "시드 그대로인 라운드가 회수로 인정됐다."
    round_path = board_dir / "tickets" / "rounds" / tid / "01-architect.md"
    header = round_path.read_text(encoding="utf-8").splitlines()[0]
    round_path.write_text(f"{header}\n\n인용·touches 를 실측 대조했다.\n", encoding="utf-8")

    assert board_git.cmd_promote(argparse.Namespace(id=tid)) == 0
    assert list((board_dir / "tickets" / "open").glob("T-*-*.md"))


@requires_git
def test_promote_succeeds_when_touches_overlap_another_active_ticket(board_git, tmp_path):
    """겹침은 이 게이트의 축이 아니다 — 같은 파일을 만지는 티켓의 승격은 성공한다(never-block)."""
    board_dir = board_git._board_dir
    _write(tmp_path, ".project_manager/tools/board.py", "a\n")
    existing = board_dir / "tickets" / "open" / "T-0002-x.md"
    board_git.dump_ticket(existing, {
        "id": "T-0002", "title": "x", "status": "open",
        "touches": [".project_manager/tools/board.py"],
        "depends_on": [], "blocks": [], "tags": [],
    }, _body())
    tid = _fill_draft(board_git, board_dir, _body(),
                      touches=[".project_manager/tools/board.py"])

    assert board_git.cmd_promote(argparse.Namespace(id=tid)) == 0, \
        "touches 겹침이 승격을 막았다 — 정당한 병렬 발행이 차단된다."


@requires_git
def test_advisory_classification_is_what_keeps_the_warning_from_blocking(
        board_git, monkeypatch):
    """민감도 — 경고 kind 를 비차단 집합에서 빼면 같은 draft 가 rc=1 이 된다(가드가 살아 있다)."""
    board_dir = board_git._board_dir
    tid = _fill_draft(board_git, board_dir, _body(), touches=["tests/test_new.py"])

    monkeypatch.setattr(board_git, "_PROMOTE_ADVISORY_KINDS", frozenset())
    assert board_git.cmd_promote(argparse.Namespace(id=tid)) == 1, \
        "비차단 집합을 비웠는데도 통과 — 경고가 판정되지 않고 있다(가드 무발화)."

    monkeypatch.setattr(board_git, "_PROMOTE_ADVISORY_KINDS",
                        board_mod._PROMOTE_ADVISORY_KINDS)
    assert board_git.cmd_promote(argparse.Namespace(id=tid)) == 0
