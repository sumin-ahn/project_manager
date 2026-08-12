"""promote 본문 fill 검증 게이트 — 절별 placeholder 잔존 탐지 (T-0366·ADR-0049 authoring flow).

`board.py new`(draft) → 본문 fill → `board.py promote` flow 의 backbone 게이트는 promote 전
`_body_lint_issues`(단일 깔때기·`cmd_new` 발행 게이트와 공유·T-0196)로 placeholder 잔존을
검사해 빈/미충전 draft 의 승격을 차단한다. 이 파일은 **강화된 placeholder 집합**(T-0366 —
인터페이스·결정·참고 절의 뼈대 문장 + `T-XXXX`)이 절별로 개별 탐지되는지(sensitivity)와,
목표/DoD 만 채우고 인터페이스·결정을 뼈대로 남긴 "절반-채운" draft 가 이제 promote-차단되는지
(이전 게이트 갭의 회귀)를 고정한다.

- **sensitivity**: `_PLACEHOLDERS` 의 각 리터럴이 본문에 남으면 개별적으로 placeholder issue 를
  낸다(하나라도 탐지 누락되면 red).
- **갭 회귀**: 강화 전에는 목표/DoD 만 채우면 통과했다(인터페이스·결정·`T-XXXX` 미탐지). 강화 후
  절반-채운 본문이 non-empty issue 를 내야 한다.
- **자족성 통과**: 5절을 실값으로 채운 self-contained 본문은 issue 0(promotable).
- **end-to-end**: board-git 활성 hermetic 홈에서 절반-채운 draft 의 `cmd_promote` 가 거부(rc=1),
  자족 본문은 승격(rc=0)한다 — 게이트가 실제 CLI 경로에서 작동함을 side-effect 로 단언.
"""
from __future__ import annotations

import argparse
import importlib.util
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


def _load_board():
    spec = importlib.util.spec_from_file_location("board_fill_gate", TOOLS / "board.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


board_mod = _load_board()


# 현행 `_template.md` 5절(목표/인터페이스/결정/완료조건/참고)의 뼈대 문장을 그대로 담은 template —
# 절반-채운/미충전 케이스를 hermetic 하게 모사한다. `<제목>`·`T-NNNN` 은 cmd_new 가 치환하므로
# 본문 게이트 판정 대상은 절별 뼈대 문장이다.
_FULL_TEMPLATE_BODY = (
    "# T-0001 — 실제 제목\n\n"
    "## 목표\n무엇을 만들 / 바꿀 / 검증할지 1~3 문장.\n\n"
    "## 인터페이스\n이 ticket 이 만들거나 바꾸는 함수·클래스·CLI·데이터 형식의 시그니처/규격.\n\n"
    "## 결정\n구현 방향에 대한 확정 사항 (어떤 방식으로 / 왜). 미정 사항은 \"열린 질문\" 으로.\n\n"
    "## 완료 조건 (Definition of Done)\n- [ ] 핵심 산출물 (파일, 동작)\n\n"
    "## 참고\n- [[architecture]] 관련 절\n- 관련 ADR / spec: [[xxxxx]]\n"
    "- 패턴 reference (이미 done 된 비슷한 ticket): T-XXXX\n\n"
    "## 메모\n"
)

# 목표·DoD 만 실값으로 채우고 인터페이스·결정·참고를 *뼈대로 남긴* 본문 — 강화 전 게이트는
# 이걸 통과시켰다(갭). 강화 후엔 인터페이스·결정·참고 placeholder 로 promote-차단돼야 한다.
_HALF_FILLED_BODY = (
    "# T-0001 — 실제 제목\n\n"
    "## 목표\n placeholder 게이트를 절별로 강화한다(T-0366).\n\n"
    "## 인터페이스\n이 ticket 이 만들거나 바꾸는 함수·클래스·CLI·데이터 형식의 시그니처/규격.\n\n"
    "## 결정\n구현 방향에 대한 확정 사항 (어떤 방식으로 / 왜). 미정 사항은 \"열린 질문\" 으로.\n\n"
    "## 완료 조건 (Definition of Done)\n- [ ] board.py placeholder 집합 강화 + 테스트\n\n"
    "## 참고\n- [[architecture]] 관련 절\n- 관련 ADR / spec: [[xxxxx]]\n"
    "- 패턴 reference (이미 done 된 비슷한 ticket): T-XXXX\n\n"
    "## 메모\n"
)

# 5절 전부 실값으로 채운 self-contained 본문 — issue 0(promotable).
_FILLED_BODY = (
    "# T-0001 — 실제 제목\n\n"
    "## 목표\n promote 게이트를 절별 placeholder 로 강화한다.\n\n"
    "## 인터페이스\n`board._PLACEHOLDERS` 에 인터페이스/결정/참고 뼈대 문장을 추가한다.\n\n"
    "## 결정\n placeholder 집합 재사용(단일 깔때기)로 promote 게이트 강화·required-section 미추가.\n\n"
    "## 완료 조건 (Definition of Done)\n- [ ] board.py placeholder 집합 강화 + 단위 테스트\n\n"
    "## 참고\n- ADR-0049 4요소 authoring flow\n- T-0196 발행 게이트 재사용\n\n"
    "## 메모\n"
)


# ════════════════════════════════════════════════════════════════════════
# sensitivity — 각 placeholder 리터럴이 개별 탐지된다
# ════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("token", board_mod._PLACEHOLDERS)
def test_each_placeholder_token_is_detected(token):
    """`_PLACEHOLDERS` 의 각 리터럴이 본문에 남으면 개별적으로 placeholder issue 를 낸다.

    자족 본문 뒤에 문제 토큰 하나만 얹어, 그 토큰이 실제로 게이트에 걸리는지 격리 확인한다
    (탐지 집합에 등재만 돼 있고 실효 검사가 빠지는 dead-token 방지)."""
    body = _FILLED_BODY + f"\n간섭 문장: {token}\n"
    issues = board_mod._body_lint_issues("T-0001", body)
    kinds = {(kind, detail) for _tid, kind, detail in issues}
    assert any(kind == "placeholder" and token in detail for kind, detail in kinds), (
        f"placeholder 토큰 {token!r} 이 게이트에 탐지되지 않음 — sensitivity 갭.")


def test_strengthened_tokens_present():
    """T-0366 로 추가된 절별 토큰이 집합에 존재(회귀 방어) — 인터페이스/결정/참고 미충전 커버."""
    for token in ("이 ticket 이 만들거나 바꾸는", "구현 방향에 대한 확정 사항",
                  "[[architecture]] 관련 절", "T-XXXX"):
        assert token in board_mod._PLACEHOLDERS, (
            f"강화 토큰 {token!r} 이 _PLACEHOLDERS 에서 빠짐 — 절별 자족성 게이트 후퇴.")


# ════════════════════════════════════════════════════════════════════════
# 갭 회귀 — 절반-채운 draft 는 차단, 자족 본문은 통과
# ════════════════════════════════════════════════════════════════════════

def test_full_template_body_blocked():
    """제목만 바뀐 순수 템플릿(5절 뼈대) → 다수 placeholder issue(빈 draft)."""
    issues = board_mod._body_lint_issues("T-0001", _FULL_TEMPLATE_BODY)
    kinds = [k for _t, k, _d in issues]
    assert kinds.count("placeholder") >= 5, (
        f"순수 템플릿이 5절 이상 placeholder 를 내야 하는데 {kinds!r}.")


def test_half_filled_body_now_blocked():
    """목표·DoD 만 채우고 인터페이스·결정·참고를 뼈대로 남긴 draft → placeholder 차단(강화 전 갭).

    강화 전 `_PLACEHOLDERS` 는 인터페이스/결정/T-XXXX 를 몰라 이 본문을 통과시켰다. 이제 인터페이스·
    결정·참고 절 미충전이 promote-차단돼야 "자족성 = placeholder 0"(ADR-0049)이 성립한다."""
    issues = board_mod._body_lint_issues("T-0001", _HALF_FILLED_BODY)
    details = " ".join(d for _t, _k, d in issues)
    assert issues, "절반-채운 draft 가 통과함 — 게이트 갭 회귀."
    # 인터페이스·결정·참고 절이 각각 잡혀야 한다(목표·DoD 는 실값이라 안 잡힘).
    assert "이 ticket 이 만들거나 바꾸는" in details, "인터페이스 절 미충전 미탐지."
    assert "구현 방향에 대한 확정 사항" in details, "결정 절 미충전 미탐지."
    assert "T-XXXX" in details, "참고 pattern-reference 미충전 미탐지."
    assert "무엇을 만들 / 바꿀 / 검증할지" not in details, "채운 목표 절이 오탐됨."
    assert "핵심 산출물 (파일, 동작)" not in details, "채운 DoD 절이 오탐됨."


def test_self_contained_body_passes():
    """5절을 실값으로 채운 self-contained 본문 → issue 0(promotable)."""
    assert board_mod._body_lint_issues("T-0001", _FILLED_BODY) == [], (
        "자족 본문이 promote 게이트에 걸림 — 오탐(실값 절을 placeholder 로 오판).")


# ════════════════════════════════════════════════════════════════════════
# end-to-end — board-git 활성 홈에서 cmd_promote 게이트 실제 작동
# ════════════════════════════════════════════════════════════════════════

def _git(argv, cwd):
    return subprocess.run(["git", *argv], cwd=str(cwd), capture_output=True,
                          text=True, encoding="utf-8", errors="replace", check=False)


def _template_file_text() -> str:
    """실 `_template.md`(frontmatter + 5절 본문)를 hermetic board 에 심을 텍스트로 구성."""
    return (
        "---\n"
        "id: T-NNNN\n"
        "title: <제목>\n"
        "status: open\n"
        "created_by:\n"
        "claimed_by:\n"
        "claimed_at:\n"
        "completed_at:\n"
        "depends_on: []\n"
        "blocks: []\n"
        "touches: []\n"
        "estimate: small\n"
        "tags: []\n"
        "---\n\n"
    ) + _FULL_TEMPLATE_BODY


@pytest.fixture
def board_git(tmp_path, monkeypatch):
    mod = _load_board()
    anchor_board_module(mod, tmp_path, monkeypatch)
    for key, val in _GIT_IDENTITY.items():
        monkeypatch.setenv(key, val)
    board = tmp_path / ".project_manager" / "board"
    for status in ("open", "claimed", "blocked", "done"):
        (board / "tickets" / status).mkdir(parents=True, exist_ok=True)
    (board / "tickets" / "_template.md").write_text(_template_file_text(), encoding="utf-8")
    bare = tmp_path / "bare"
    # bare 는 *`str(bare)` 경로*에 만들어야 한다 — 인자 없이 돌리면 cwd(tmp_path)를 bare 화해
    # remote 가 실재하지 않고 push 가 조용히 실패(best-effort 가 삼킴)해 e2e 가 가짜가 된다.
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
        assert r.returncode == 0, f"board-git setup 실패: git {argv} → {r.returncode}\n{r.stderr}"
    assert (bare / "HEAD").exists(), "bare remote 가 str(bare) 에 생성되지 않음(하네스 버그 회귀)."
    mod._board_dir = board
    return mod


def _new_args(title: str) -> argparse.Namespace:
    return argparse.Namespace(title=title, touches=None, depends=None, tag=None,
                              estimate="small", prefix=None, user=None, session=None)


def _draft_id(board_dir: Path) -> str:
    path = list((board_dir / "tickets" / ".drafts").glob("T-*-*.md"))[0]
    return "-".join(path.stem.split("-")[:2])


@requires_git
def test_promote_rejects_half_filled_draft(board_git):
    """목표·DoD 만 채우고 인터페이스·결정을 뼈대로 남긴 draft 의 promote 는 거부(rc=1)·open/ 미이동."""
    board_dir = board_git._board_dir
    board_git.cmd_new(_new_args("절반"))
    tid = _draft_id(board_dir)
    draft_path = list((board_dir / "tickets" / ".drafts").glob("T-*-*.md"))[0]

    fm, _ = board_git.load_ticket(draft_path)
    board_git.dump_ticket(draft_path, fm, _HALF_FILLED_BODY)

    rc = board_git.cmd_promote(argparse.Namespace(id=tid))
    assert rc == 1, "인터페이스/결정 뼈대가 남은 draft 가 promote 통과함(게이트 갭)."
    assert list((board_dir / "tickets" / ".drafts").glob("T-*-*.md")), \
        "거부된 draft 는 .drafts/ 에 남아야 한다."
    assert not list((board_dir / "tickets" / "open").glob("T-*-*.md")), \
        "거부됐는데 draft 가 open/ 으로 이동됨."


@requires_git
def test_promote_accepts_self_contained_draft(board_git):
    """5절을 실값으로 채운 draft 는 promote 성공(rc=0)·open/ 이동."""
    board_dir = board_git._board_dir
    board_git.cmd_new(_new_args("자족"))
    tid = _draft_id(board_dir)
    draft_path = list((board_dir / "tickets" / ".drafts").glob("T-*-*.md"))[0]

    fm, _ = board_git.load_ticket(draft_path)
    board_git.dump_ticket(draft_path, fm, _FILLED_BODY)

    rc = board_git.cmd_promote(argparse.Namespace(id=tid))
    assert rc == 0, "자족 본문의 draft promote 가 거부됨(오탐)."
    assert not list((board_dir / "tickets" / ".drafts").glob("T-*-*.md")), \
        "승격된 draft 가 .drafts/ 에 남음(open/ 이동 실패)."
    assert list((board_dir / "tickets" / "open").glob("T-*-*.md")), \
        "승격된 티켓이 open/ 으로 이동 안 됨."


# ════════════════════════════════════════════════════════════════════════
# 절-삭제 회피 — authoring 게이트만 strict 5절 존재 요구, 전역 lint 는 3절 불변
# ════════════════════════════════════════════════════════════════════════

# 인터페이스·결정 절을 통째로 삭제한 본문(잔존 placeholder 토큰 없음) — placeholder 검사만으론
# 못 잡는 우회(codex must-fix). authoring 게이트(strict)는 절 부재를 thin 으로 차단하고, 전역
# lint(3절 불변)는 인터페이스/결정을 요구하지 않아 비차단(레거시 blast-radius 0).
_SECTION_DELETED_BODY = (
    "# T-0001 — 실 제목\n\n"
    "## 목표\n실제 목표를 채웠다.\n\n"
    "## 완료 조건 (Definition of Done)\n- [ ] 실제 산출물\n\n"
    "## 참고\n- [[ADR-0049]]\n\n"
    "## 메모\n"
)


def test_strict_gate_catches_deleted_sections_global_lint_does_not():
    """인터페이스·결정 절을 삭제한 본문 — strict(authoring)는 thin 차단·default(전역 lint)는 비차단.

    placeholder 뼈대 문장이 없어 placeholder 검사는 통과하므로, 절 *부재* 를 thin 으로 세우는
    strict 경로만 이 우회를 잡는다. 전역 lint(3절 불변)는 인터페이스/결정을 요구하지 않아 clean —
    레거시 티켓 blast-radius 0 을 동시 단언한다(단일 함수·소비측 파라미터)."""
    # 전역 lint 경로(strict_sections 기본 False) — 3절(목표/완료조건/참고)만 요구 → clean.
    assert board_mod._body_lint_issues("T-0001", _SECTION_DELETED_BODY) == [], (
        "전역 lint 가 인터페이스/결정 절 부재를 차단함 — 레거시 blast-radius(3절 불변 위반).")
    # authoring 게이트(strict) — 5절 존재 강제 → 인터페이스·결정 절 부재를 thin 으로 차단.
    strict = board_mod._body_lint_issues("T-0001", _SECTION_DELETED_BODY, strict_sections=True)
    details = " ".join(d for _t, _k, d in strict)
    assert any(k == "thin" for _t, k, _d in strict), "strict 게이트가 절 삭제를 못 잡음(우회)."
    assert "## 인터페이스" in details, "인터페이스 절 삭제 미탐지(strict)."
    assert "## 결정" in details, "결정 절 삭제 미탐지(strict)."


def test_strict_sections_constant_is_five():
    """strict 절 집합 = 5절(목표/인터페이스/결정/DoD/참고)·전역은 3절 유지(회귀 방어)."""
    assert board_mod._STRICT_REQUIRED_SECTIONS == (
        "## 목표", "## 인터페이스", "## 결정", "## 완료 조건", "## 참고")
    assert board_mod._REQUIRED_SECTIONS == ("## 목표", "## 완료 조건", "## 참고")


@requires_git
def test_promote_rejects_section_deleted_draft(board_git):
    """인터페이스·결정 절을 삭제한 draft 의 promote 는 거부(rc=1)·open/ 미이동(strict authoring 게이트)."""
    board_dir = board_git._board_dir
    board_git.cmd_new(_new_args("절삭제"))
    tid = _draft_id(board_dir)
    draft_path = list((board_dir / "tickets" / ".drafts").glob("T-*-*.md"))[0]

    fm, _ = board_git.load_ticket(draft_path)
    board_git.dump_ticket(draft_path, fm, _SECTION_DELETED_BODY)

    rc = board_git.cmd_promote(argparse.Namespace(id=tid))
    assert rc == 1, "인터페이스/결정 절을 삭제한 draft 가 promote 통과함(절-삭제 우회)."
    assert list((board_dir / "tickets" / ".drafts").glob("T-*-*.md")), \
        "거부된 draft 는 .drafts/ 에 남아야 한다."
    assert not list((board_dir / "tickets" / "open").glob("T-*-*.md")), \
        "거부됐는데 draft 가 open/ 으로 이동됨."
