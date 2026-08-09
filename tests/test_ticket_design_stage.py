"""티켓 설계 단계 — `design:` 필드 · `## 설계` 절 · claim 게이트 (T-0594).

설계 결함이 코드 리뷰 라운드로 전가되는 클래스를 라이프사이클 앞단에서 차단한다(근거 T-0587 —
티켓 인터페이스가 명세한 입력 가정이 실환경과 어긋난 채 구현으로 전진해 리뷰 12라운드로도 미수렴).
검증 대상:

  1. **필드 파싱·기본값** — `required`/`done`/`waived: <사유>`/`n/a` 4형식 정규화, 필드 부재는
     `n/a`(구세대 티켓 하위호환), 형식 위반은 `invalid`(오타로 게이트가 조용히 꺼지지 않음).
     발행 기본값은 estimate=large → `required`, 그 외 `n/a`.
  2. **설계 절 판정** — `_template.md` 뼈대 문장 잔존/절 부재를 `design-pending` 1줄로 요약
     (`_body_lint_issues` 단일 깔때기 편입 · `n/a`·`waived` 는 뼈대가 남아도 무영향).
  3. **claim 게이트 4경로** — required 차단 / done 통과 / waived 통과 / n/a 무영향 + 구티켓
     (필드 부재) 하위호환 + invalid 차단. 차단 시 티켓은 `open/` 에 그대로 남는다.
  4. **promote 게이트** — board-git 활성 홈에서 `design: required` draft 는 승격 거부, 설계 절
     완성 + `design: done` 은 승격.
  5. **lint advisory** — kind `design-pending` 은 `_ADVISORY_LINT_KINDS` 등재라 `--gate`(pre-push)
     종료코드에 기여하지 않고, 무인자 lint 는 그대로 보고한다.
  6. **출하 파리티** — 루트 `_template.md`·pm-ticket 스킬 카드의 설계 단계 내용이 templates/ 3벌에
     전부 도달했는지(채택자 도달 가드).

**hermetic 필수**: board.py 의 경로 전역(`REPO`·`TICKETS_DIR`·`TEMPLATE_FILE` 등)은 import 시점에
실 repo 절대경로로 굳는다 — tmp 프로젝트로 monkeypatch 재지정해 실 board 를 읽거나 쓰지 않는다
(test_board_identity.py 의 hermetic 패턴 동형). board-git e2e 는 실 git + bare remote 를 tmp 에
세우는 test_board_promote_fill_gate.py 패턴을 따르고, git 부재 환경에선 skip 한다.
"""
from __future__ import annotations

import argparse
import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
TEMPLATE = REPO / ".project_manager" / "wiki" / "tickets" / "_template.md"
SKILL = REPO / ".claude" / "skills" / "pm-ticket" / "SKILL.md"

# 출하 템플릿 3벌 — 어댑터별 스킬 경로만 다르고 ticket 템플릿 경로는 공통.
_SHIPPED_TICKET_TEMPLATES = tuple(
    REPO / "templates" / target / ".project_manager" / "wiki" / "tickets" / "_template.md"
    for target in ("claude_code", "codex", "opencode"))
_SHIPPED_SKILLS = (
    REPO / "templates" / "claude_code" / ".claude" / "skills" / "pm-ticket" / "SKILL.md",
    REPO / "templates" / "codex" / ".agents" / "skills" / "pm-ticket" / "SKILL.md",
    REPO / "templates" / "opencode" / ".claude" / "skills" / "pm-ticket" / "SKILL.md",
)

requires_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="git 바이너리 부재 — 실 git 통합 케이스 skip.")

_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
}


def _load_board():
    spec = importlib.util.spec_from_file_location("board_design_stage", TOOLS / "board.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


board_mod = _load_board()


def _template_section(body: str, heading: str) -> str:
    """실 `_template.md` 본문에서 한 절을 잘라낸다 — 뼈대 문장을 테스트가 복제하지 않게.

    복제하면 템플릿이 바뀔 때 테스트만 옛 문장을 들고 green 으로 남는다(setup-rot)."""
    start = body.index(heading)
    rest = body.find("\n## ", start + len(heading))
    return body[start:] if rest == -1 else body[start:rest + 1]


_TEMPLATE_FM, _TEMPLATE_BODY = board_mod.load_ticket(TEMPLATE)
# 미충전 설계 절 = 출하 템플릿의 그 절 자체(뼈대 문장 4개 포함).
_DESIGN_SKELETON = _template_section(_TEMPLATE_BODY, board_mod._DESIGN_SECTION)

# 설계 절을 실값으로 채운 판 — 뼈대 문장 0(하위 항목 이름은 유지).
_DESIGN_FILLED = (
    "## 설계\n"
    "- **경계 실측**: `board.py new --design bogus` 실행 → rc=1 · `[중단]` 메시지 확인.\n"
    "- **불변식**: 필드 부재 티켓의 claim 은 어떤 본문에서도 rc=0 이다.\n"
    "- **표면 상한**: 입력은 frontmatter 문자열 하나 — 상태 5종으로 유한.\n"
    "- **테스트 전략**: 4상태 × (절 충전·미충전) 을 claim rc 로 고정.\n\n")


def _body(design_section: str = _DESIGN_FILLED) -> str:
    """5절을 실값으로 채운 자족 본문 + 지정한 설계 절 (placeholder 0)."""
    return (
        "# T-0001 — 실 제목\n\n"
        "## 목표\n티켓 라이프사이클에 설계 단계를 신설한다.\n\n"
        "## 인터페이스\n`design:` frontmatter 필드 + claim 게이트.\n\n"
        "## 결정\nestimate=large 만 자동 required · 그 외 n/a.\n\n"
        + design_section +
        "## 완료 조건 (Definition of Done)\n- [ ] 게이트 + 단위 테스트\n\n"
        "## 참고\n- 근거 사례 T-0587 부검\n\n"
        "## 메모\n")


# ════════════════════════════════════════════════════════════════════════
# 1. `design:` 값 파싱 + 발행 기본값
# ════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("value, expected", [
    ("required", board_mod.DESIGN_REQUIRED),
    ("done", board_mod.DESIGN_DONE),
    ("n/a", board_mod.DESIGN_NA),
    ("waived: 리뷰 상한 초과", board_mod.DESIGN_WAIVED),
    ("waived:사유", board_mod.DESIGN_WAIVED),
    ("  Required  ", board_mod.DESIGN_REQUIRED),   # 공백·대소문자 관대
    ("N/A", board_mod.DESIGN_NA),
])
def test_design_state_normalizes_four_forms(value, expected):
    """4형식(required/done/waived: <사유>/n/a)이 정규화 상태로 접힌다."""
    assert board_mod._design_state(value) == expected


@pytest.mark.parametrize("value", [None, "", "   "])
def test_design_state_missing_field_is_na(value):
    """필드 부재·빈값 = `n/a` — 구세대 티켓(설계 절도 필드도 없음) 하위호환(마이그레이션 불요)."""
    assert board_mod._design_state(value) == board_mod.DESIGN_NA


@pytest.mark.parametrize("value", ["requried", "todo", "waived", "waived:   ", "true"])
def test_design_state_rejects_unrecognized_value(value):
    """형식 위반은 `n/a` 로 삼키지 않고 invalid — 오타 하나로 게이트가 조용히 꺼지지 않는다.

    사유 없는 맨 `waived` 도 위반이다(면제의 근거가 남지 않는다)."""
    assert board_mod._design_state(value) == board_mod.DESIGN_INVALID


@pytest.mark.parametrize("estimate, expected", [
    ("large", board_mod.DESIGN_REQUIRED),
    ("medium", board_mod.DESIGN_NA),
    ("small", board_mod.DESIGN_NA),
    (None, board_mod.DESIGN_NA),
])
def test_resolve_design_defaults_by_estimate(estimate, expected):
    """발행 기본값: estimate=large → required · 그 외 n/a (전 티켓 강제 아님)."""
    assert board_mod._resolve_design(None, estimate) == expected


@pytest.mark.parametrize("explicit", ["required", "n/a", "waived: 설계 불요"])
def test_resolve_design_honors_explicit_override(explicit):
    """PM 이 발행 시 명시 지정하면 estimate 유도를 덮는다(small 에도 required 지정 가능)."""
    assert board_mod._resolve_design(explicit, "small") == explicit


def test_validate_design_rejects_bad_value_and_accepts_forms():
    """`--design` 입력 sanity — 위반은 사유 문자열, 4형식은 None(`_validate_prefix` 동형)."""
    assert board_mod._validate_design("bogus")
    assert "bogus" in board_mod._validate_design("bogus")
    for ok in ("required", "done", "n/a", "waived: 사유"):
        assert board_mod._validate_design(ok) is None


# ════════════════════════════════════════════════════════════════════════
# 2. 설계 절 판정 (`_design_issues` — claim/promote/lint 단일 깔때기)
# ════════════════════════════════════════════════════════════════════════

def test_design_placeholder_tokens_live_in_shipped_template():
    """탐지 토큰이 실제 `_template.md` 설계 절에 리터럴로 존재한다 (setup-rot·drift 가드).

    템플릿 문장만 고치고 토큰을 안 고치면 뼈대가 영영 안 잡혀 게이트가 dead 가 된다."""
    for token in board_mod._DESIGN_PLACEHOLDERS:
        assert token in _DESIGN_SKELETON, (
            f"설계 뼈대 토큰 {token!r} 이 출하 템플릿에 없음 — 탐지 dead-token.")
    assert len(board_mod._DESIGN_PLACEHOLDERS) == len(board_mod._DESIGN_PLACEHOLDER_LABELS), \
        "토큰과 항목 이름의 개수가 어긋남 — 경고 메시지가 항목을 잘못 지목한다."


@pytest.mark.parametrize("token", board_mod._DESIGN_PLACEHOLDERS)
def test_each_design_placeholder_is_detected(token):
    """뼈대 토큰 하나만 **설계 절에** 남아도 개별 탐지된다 (sensitivity — 검사 누락 방지)."""
    body = _body(_DESIGN_FILLED + f"- 잔존 뼈대: {token}\n\n")
    issues = board_mod._design_issues("T-0001", body, board_mod.DESIGN_DONE)
    assert issues, f"설계 뼈대 토큰 {token!r} 이 탐지되지 않음 — sensitivity 갭."


@pytest.mark.parametrize("token", board_mod._DESIGN_PLACEHOLDERS)
def test_placeholder_quoted_outside_the_design_section_is_not_a_gap(token):
    """설계 절 **밖**(메모·인터페이스)의 뼈대 문장 인용은 미충전이 아니다 — 오탐 0 (T-0600).

    설계 단계 자체를 다루는 후속 티켓은 본문에서 뼈대 문장을 인용한다. 본문 전체를 스캔하면
    그 인용만으로 "설계 절 미충전" 경고가 뜨고, 채운 티켓이 영영 green 이 되지 않는다.
    """
    body = _body() + f"\n메모: 뼈대 문장 '{token}' 을 인용해 설명한다.\n"
    assert board_mod._design_issues("T-0001", body, board_mod.DESIGN_DONE) == []


def test_required_with_unfilled_section_is_flagged():
    """`design: required` + 뼈대 잔존 → 미충전 항목을 지목한 1건."""
    issues = board_mod._design_issues("T-0001", _body(_DESIGN_SKELETON),
                                      board_mod.DESIGN_REQUIRED)
    assert len(issues) == 1
    _tid, kind, detail = issues[0]
    assert kind == "design-pending"
    for label in board_mod._DESIGN_PLACEHOLDER_LABELS:
        assert label in detail, f"미충전 항목 {label!r} 이 경고에 안 실림."


def test_required_with_filled_section_still_needs_promotion():
    """설계 절을 다 채워도 `required` 인 한 1건 — `done` 승격(사람 판정)이 남았다."""
    issues = board_mod._design_issues("T-0001", _body(), board_mod.DESIGN_REQUIRED)
    assert len(issues) == 1
    assert board_mod.DESIGN_DONE in issues[0][2], "승격 안내가 경고에 없음."


def test_done_with_filled_section_is_clean():
    """설계 절 완성 + `done` → 0건(게이트 통과)."""
    assert board_mod._design_issues("T-0001", _body(), board_mod.DESIGN_DONE) == []


def test_done_with_missing_section_is_flagged():
    """`done` 인데 `## 설계` 절 자체가 없으면 필드값과 절 내용이 어긋난 상태 → 1건.

    엔진이 보는 건 필드값과 절 존재뿐이므로, 절 없는 `done` 은 자기모순으로 잡는다."""
    body = _body("")
    assert board_mod._DESIGN_SECTION not in body
    issues = board_mod._design_issues("T-0001", body, board_mod.DESIGN_DONE)
    assert len(issues) == 1
    assert board_mod._DESIGN_SECTION in issues[0][2]


@pytest.mark.parametrize("design", [None, "n/a", "waived: 설계 불요"])
def test_na_and_waived_ignore_design_section(design):
    """`n/a`·`waived`·필드 부재는 설계 절이 뼈대여도 0건 — 설계 오버헤드 역류 차단."""
    assert board_mod._design_issues("T-0001", _body(_DESIGN_SKELETON), design) == []


def test_invalid_value_is_flagged_once():
    """형식 위반 값은 설계 절과 무관하게 1건(값 자체를 고치라는 안내)."""
    issues = board_mod._design_issues("T-0001", _body(), "requried")
    assert len(issues) == 1 and "requried" in issues[0][2]


def test_design_issue_is_single_line_at_most():
    """어떤 조합에서도 최대 1건 — 전역 lint 의 '경고 1줄' 보장."""
    for design in (None, "n/a", "waived: x", "required", "done", "bogus"):
        for section in (_DESIGN_SKELETON, _DESIGN_FILLED, ""):
            assert len(board_mod._design_issues("T-0001", _body(section), design)) <= 1


# ── `_body_lint_issues` 편입 (기존 seam 재사용·레거시 호출 무변경) ────────────────

def test_body_lint_issues_without_design_arg_is_legacy_clean():
    """`design` 인자를 안 넘기는 기존 호출은 설계 판정 없이 기존 검사만 받는다(무변경)."""
    assert board_mod._body_lint_issues("T-0001", _body(_DESIGN_SKELETON)) == []


def test_body_lint_issues_folds_design_verdict():
    """설계 판정이 같은 깔때기(`_body_lint_issues`)로 흘러 authoring 게이트가 재사용한다."""
    issues = board_mod._body_lint_issues("T-0001", _body(_DESIGN_SKELETON),
                                         strict_sections=True,
                                         design=board_mod.DESIGN_REQUIRED)
    assert [k for _t, k, _d in issues] == ["design-pending"]


# ════════════════════════════════════════════════════════════════════════
# hermetic board fixture — cmd_new / cmd_claim / lint 경로
# ════════════════════════════════════════════════════════════════════════

def _make_project(root: Path) -> None:
    """tickets 레이아웃 + **실 출하 템플릿** (기본값·설계 절이 실제 배포본과 같은 걸 보증)."""
    tickets = root / ".project_manager" / "wiki" / "tickets"
    for status in ("open", "claimed", "blocked", "done"):
        (tickets / status).mkdir(parents=True, exist_ok=True)
    (tickets / "_template.md").write_text(
        TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")


@pytest.fixture
def board(tmp_path, monkeypatch):
    """fresh board 모듈 + IO 전역을 tmp 프로젝트로 재지정한 hermetic 인스턴스."""
    proj = tmp_path / "proj"
    _make_project(proj)
    mod = _load_board()
    pm = proj / ".project_manager"
    wiki = pm / "wiki"
    overrides = {
        "REPO": proj,
        "TICKETS_DIR": wiki / "tickets",
        "TEMPLATE_FILE": wiki / "tickets" / "_template.md",
        "BOARD_FILE": wiki / "board.md",
        "LOG_FILE": wiki / "log" / "current.md",
        "STATUS_FILE": wiki / "status.md",
        "LOCAL_CONF": pm / "local.conf",
        "AREAS_FILE": pm / "areas.md",
        "LOCAL_DIR": pm / ".local",
        "BOARD_LOCK": pm / ".local" / "board.lock",
        "LEASES_FILE": pm / ".local" / "worktree-leases.json",
        "PM_STATE_FILE": wiki / "pm_state.md",
        "PM_STATE_TEMPLATE": wiki / "pm_state.template.md",
    }
    for name, val in overrides.items():
        monkeypatch.setattr(mod, name, val)
    (pm / ".local").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(mod, "_git_config_email", lambda: None)
    # 세션 귀속은 tmp local.conf 로 고정 — 실행 환경의 PM 세션(env)이 새지 않게 지운다
    # (claim 은 세션 미해소 시 fail-loud 라 hermetic 하려면 바인딩이 필요하다).
    for key in ("PM_SESSION_NAME", "CLAUDE_SESSION_NAME"):
        monkeypatch.delenv(key, raising=False)
    (pm / "local.conf").write_text("session=pm-1\nuser=tester\n", encoding="utf-8")
    return mod


def _new_args(estimate="small", design=None):
    return argparse.Namespace(title="t", touches=None, depends=None, tag=None,
                              estimate=estimate, design=design, prefix=None,
                              user=None, session=None)


def _claim_args(tid="T-0001"):
    return argparse.Namespace(id=tid, repo=None, slot=None, user=None)


def _seed_open(board, *, design, body=None, tid="T-0001", estimate=None):
    """open/ 에 티켓 하나를 심는다 — `design` 이 None 이면 **필드 자체를 안 쓴다**(구티켓)."""
    fm = {"id": tid, "title": "seed", "status": "open",
          "claimed_by": None, "depends_on": []}
    if design is not None:
        fm["design"] = design
    if estimate is not None:
        fm["estimate"] = estimate
    path = board.TICKETS_DIR / "open" / f"{tid}-seed.md"
    board.dump_ticket(path, fm, body if body is not None else _body())
    return path


# ── cmd_new 발행 기본값 ──────────────────────────────────────────────────

def test_cmd_new_large_ticket_defaults_to_design_required(board):
    """estimate=large 발행은 `design: required` 로 박힌다(자동 추론은 이 축 하나뿐)."""
    assert board.cmd_new(_new_args(estimate="large")) == 0
    fm, _ = board.load_ticket(list((board.TICKETS_DIR / "open").glob("T-0001-*.md"))[0])
    assert fm["design"] == board.DESIGN_REQUIRED


@pytest.mark.parametrize("estimate", ["small", "medium"])
def test_cmd_new_small_medium_default_to_na(board, estimate):
    """small/medium 은 `n/a` — 설계 오버헤드가 전 티켓 비용이 되지 않는다."""
    assert board.cmd_new(_new_args(estimate=estimate)) == 0
    fm, _ = board.load_ticket(list((board.TICKETS_DIR / "open").glob("T-0001-*.md"))[0])
    assert fm["design"] == board.DESIGN_NA


def test_cmd_new_honors_explicit_design(board):
    """`--design required` 명시가 estimate 유도를 덮는다."""
    assert board.cmd_new(_new_args(estimate="small", design="required")) == 0
    fm, _ = board.load_ticket(list((board.TICKETS_DIR / "open").glob("T-0001-*.md"))[0])
    assert fm["design"] == board.DESIGN_REQUIRED


def test_cmd_new_rejects_invalid_design_before_writing(board, capsys):
    """형식 위반 `--design` 은 발행 시점에 fail-loud — 티켓 파일이 생기지 않는다."""
    assert board.cmd_new(_new_args(design="requried")) == 1
    assert "[중단]" in capsys.readouterr().err
    assert not list((board.TICKETS_DIR / "open").glob("T-*.md"))


def test_cmd_new_body_includes_design_section(board):
    """발행 본문에 `## 설계` 절이 실린다(템플릿 절이 티켓으로 흐른다)."""
    assert board.cmd_new(_new_args()) == 0
    _fm, body = board.load_ticket(
        list((board.TICKETS_DIR / "open").glob("T-0001-*.md"))[0])
    assert board._DESIGN_SECTION in body


# ── claim 게이트 4경로 + 하위호환 ────────────────────────────────────────

def test_claim_blocked_when_design_required_and_section_unfilled(board, capsys):
    """① required + 설계 절 미충전 → rc=1 · 티켓은 open/ 에 남는다."""
    _seed_open(board, design="required", body=_body(_DESIGN_SKELETON))
    assert board.cmd_claim(_claim_args()) == 1
    err = capsys.readouterr().err
    assert "설계 단계 미완" in err and "경계 실측" in err
    assert list((board.TICKETS_DIR / "open").glob("T-0001-*.md")), \
        "차단됐는데 티켓이 open/ 을 떠남."
    assert not list((board.TICKETS_DIR / "claimed").glob("T-0001-*.md"))


def test_claim_blocked_when_required_even_with_filled_section(board):
    """required 는 설계 절을 다 채워도 차단 — `done` 승격(설계 검토 종료 선언)이 남았다."""
    _seed_open(board, design="required")
    assert board.cmd_claim(_claim_args()) == 1


def test_claim_passes_when_design_done(board):
    """② `design: done` + 설계 절 완성 → 통과."""
    _seed_open(board, design="done")
    assert board.cmd_claim(_claim_args()) == 0
    assert list((board.TICKETS_DIR / "claimed").glob("T-0001-*.md"))


def test_claim_blocked_when_done_but_section_unfilled(board):
    """`done` 인데 설계 절이 뼈대면 차단 — 필드값과 절 내용의 어긋남(엔진이 보는 두 축)."""
    _seed_open(board, design="done", body=_body(_DESIGN_SKELETON))
    assert board.cmd_claim(_claim_args()) == 1


def test_claim_passes_when_design_waived(board):
    """③ `waived: <사유>` → 통과 (설계 절 뼈대여도 무영향)."""
    _seed_open(board, design="waived: 표면 없음", body=_body(_DESIGN_SKELETON))
    assert board.cmd_claim(_claim_args()) == 0
    assert list((board.TICKETS_DIR / "claimed").glob("T-0001-*.md"))


def test_claim_unaffected_when_design_na(board):
    """④ `n/a` → 무영향 (설계 절 뼈대여도 통과)."""
    _seed_open(board, design="n/a", body=_body(_DESIGN_SKELETON))
    assert board.cmd_claim(_claim_args()) == 0


def test_claim_legacy_ticket_without_design_field_passes(board):
    """구세대 티켓(필드 부재 · 설계 절 부재)은 무영향 — 마이그레이션 불요."""
    _seed_open(board, design=None, body="# T-0001 — 구티켓\n\n## 목표\n옛 본문.\n")
    assert board.cmd_claim(_claim_args()) == 0
    assert list((board.TICKETS_DIR / "claimed").glob("T-0001-*.md"))


def test_claim_blocked_on_invalid_design_value(board, capsys):
    """형식 위반 값은 claim 에서 fail-loud — 오타로 게이트가 조용히 꺼지지 않는다."""
    _seed_open(board, design="requried")
    assert board.cmd_claim(_claim_args()) == 1
    assert "인식 불가" in capsys.readouterr().err


def test_claim_gate_runs_after_dependency_gate(board):
    """설계 게이트가 의존성 거부를 덮지 않는다 — 미완 의존은 여전히 의존성 사유로 거부."""
    _seed_open(board, design="done", tid="T-0002")
    fm = {"id": "T-0001", "title": "seed", "status": "open", "claimed_by": None,
          "depends_on": ["T-0002"], "design": "required"}
    board.dump_ticket(board.TICKETS_DIR / "open" / "T-0001-seed.md", fm,
                      _body(_DESIGN_SKELETON))
    assert board.cmd_claim(_claim_args()) == 1


# ── lint advisory ───────────────────────────────────────────────────────

def test_design_pending_is_advisory_kind():
    """`design-pending` 은 advisory 등재 — pre-push 게이트(`lint --gate`)를 막지 않는다."""
    assert "design-pending" in board_mod._ADVISORY_LINT_KINDS


def test_lint_bodies_emits_single_design_advisory(board):
    """open 티켓이 `design: required` + 뼈대면 경고 1줄(kind=design-pending)."""
    _seed_open(board, design="required", body=_body(_DESIGN_SKELETON))
    issues = board.lint_bodies()
    assert [(tid, kind) for tid, kind, _d in issues] == [("T-0001", "design-pending")]


def test_lint_bodies_silent_for_na_ticket(board):
    """`n/a` 티켓은 설계 절 뼈대가 남아도 lint 가 조용하다(오탐 0)."""
    _seed_open(board, design="n/a", body=_body(_DESIGN_SKELETON))
    assert board.lint_bodies() == []


def test_cmd_lint_gate_does_not_block_on_design_pending(board, monkeypatch):
    """`--gate` 는 design-pending 만 있으면 rc=0(never-block), 무인자 lint 는 보고(rc=1).

    남의 설계-중 티켓이 내 push 를 막지 않아야 한다 — 차단은 claim 게이트가 한다."""
    monkeypatch.setattr(board, "lint_tickets",
                        lambda: [("T-0001", "design-pending", "설계 절 미충전")])
    monkeypatch.setattr(board, "_run_lint_hooks", lambda: [])
    assert board.cmd_lint(argparse.Namespace(gate=True)) == 0
    assert board.cmd_lint(argparse.Namespace(gate=False)) == 1


# ════════════════════════════════════════════════════════════════════════
# estimate 사후 교정 재유도 advisory (T-0601 ③)
# ════════════════════════════════════════════════════════════════════════
# 자동 `required` 판정은 **발행 시점 1회**뿐이라, medium 으로 발행한 뒤 large 로 교정한 티켓에는
# 설계 게이트가 영영 안 붙는다(이 wave 실경로 2회). 올릴지 면제할지는 사람 판정이므로 엔진은
# 재유도 1줄만 낸다(never-block).


def test_design_estimate_is_advisory_kind():
    """`design-estimate` 는 advisory 등재 — pre-push 게이트를 막지 않는다."""
    assert "design-estimate" in board_mod._ADVISORY_LINT_KINDS


def test_large_estimate_with_na_design_is_readvised():
    """`estimate=large ∧ design: n/a` → 1줄 (두 값과 두 출구를 모두 안내한다)."""
    issues = board_mod._design_estimate_advisory("T-0601", "large", "n/a")
    assert len(issues) == 1
    tid, kind, detail = issues[0]
    assert (tid, kind) == ("T-0601", "design-estimate")
    assert "large" in detail and "required" in detail and "waived" in detail


def test_missing_design_field_counts_as_na(board_estimate="large"):
    """필드 부재도 `n/a` 다 — 구세대 티켓을 large 로 교정한 형상이 실경로다."""
    assert len(board_mod._design_estimate_advisory("T-0601", board_estimate, None)) == 1


@pytest.mark.parametrize("estimate, design", [
    ("small", "n/a"),                    # 자동 required 대상이 아니다
    ("medium", None),                    # 같은 축
    (None, "n/a"),                       # estimate 미선언
    ("", "n/a"),
    ("large", "required"),               # 이미 설계 대상 — design-pending 이 소유
    ("large", "done"),
    ("large", "waived: 리뷰 상한 초과"),   # 사유와 함께 면제 — 판정이 이미 남았다
    ("large", "requried"),               # invalid — 값 자체 안내는 다른 깔때기가 낸다
    ("LARGE", "required"),
])
def test_readvisory_stays_silent_outside_its_axis(estimate, design):
    """이 advisory 가 보는 건 '비대상으로 남아 있는가' 하나 — 그 밖은 전부 0건(오탐 0)."""
    assert board_mod._design_estimate_advisory("T-0601", estimate, design) == []


def test_lint_bodies_emits_the_estimate_readvisory(board):
    """사후 교정된 티켓(large + n/a)을 전역 lint 가 1줄로 보고한다."""
    _seed_open(board, design="n/a", estimate="large")
    assert [(tid, kind) for tid, kind, _d in board.lint_bodies()] == [
        ("T-0001", "design-estimate")]


def test_lint_bodies_silent_for_a_large_ticket_already_on_the_design_track(board):
    """large 인데 이미 `done` 이면 재유도가 없다 — 정상 경로 무영향."""
    _seed_open(board, design="done", estimate="large")
    assert board.lint_bodies() == []


def test_cmd_lint_gate_does_not_block_on_design_estimate(board, monkeypatch):
    """`--gate` 는 design-estimate 만 있으면 rc=0(never-block), 무인자 lint 는 보고(rc=1)."""
    monkeypatch.setattr(board, "lint_tickets",
                        lambda: [("T-0001", "design-estimate", "estimate=large 인데 n/a")])
    monkeypatch.setattr(board, "_run_lint_hooks", lambda: [])
    assert board.cmd_lint(argparse.Namespace(gate=True)) == 0
    assert board.cmd_lint(argparse.Namespace(gate=False)) == 1


# ════════════════════════════════════════════════════════════════════════
# 설계 절 **존재** 판정 앵커화 (T-0601 ⑫)
# ════════════════════════════════════════════════════════════════════════
# 충전 축(T-0594)은 이미 펜스 선-strip + 절 슬라이스로 판정한다. 존재 축도 같은 규율이어야
# `## 설계` 라는 짧은 이름이 다른 절(`## 설계 검토 이력`)이나 인용으로 성립하지 않는다.


def test_prose_heading_prefix_is_not_the_design_section():
    """`## 설계 검토 이력` 같은 **다른 절**이 설계 절로 잡히지 않는다 (접두 일치 우회 폐쇄).

    잡히면 그 절 안에 뼈대 문장이 없다는 이유로 `design: done` 이 그냥 통과한다."""
    body = _body("## 설계 검토 이력\n- 2026-08-09 PM 이 구두로 확인했다.\n\n")
    assert board_mod._design_section_gaps(body) == [
        f"{board_mod._DESIGN_SECTION} 절 부재"]
    assert len(board_mod._design_issues("T-0601", body, board_mod.DESIGN_DONE)) == 1


def test_fenced_design_heading_is_not_the_design_section():
    """펜스 안에 인용된 `## 설계` 도 절이 아니다 (선-strip 규율이 존재 축에도 적용)."""
    body = _body("## 참고 인용\n```md\n## 설계\n- **불변식**: 예시다.\n```\n\n")
    assert board_mod._design_section_gaps(body) == [
        f"{board_mod._DESIGN_SECTION} 절 부재"]


def test_exact_heading_line_is_the_design_section():
    """줄 전체가 `## 설계`(후행 공백 허용)면 그 절이다 — 정상 티켓 무영향."""
    assert board_mod._design_section_gaps(_body()) == []
    assert board_mod._design_section_gaps(
        _body(_DESIGN_FILLED.replace("## 설계\n", "## 설계   \n", 1))) == []


@pytest.mark.parametrize("heading", [
    "## 설계 (DRAFT — 리뷰 전)",      # 실재 표기 — 같은 절에 상태를 병기한다
    "## 설계 (T-0594)",
    "## 설계(초안)",
])
def test_parenthesized_subtitle_is_still_the_design_section(heading):
    """괄호 부제가 붙은 헤딩은 그 절이다 — 존재 축 강화가 실 사용례를 오차단하면 안 된다.

    괄호 없이 이어지는 말(`## 설계 검토 이력`)은 다른 절 제목이라 여전히 차단된다."""
    assert board_mod._design_section_gaps(
        _body(_DESIGN_FILLED.replace("## 설계\n", f"{heading}\n", 1))) == []


def test_subtitled_headings_are_sliced_by_the_same_rule():
    """부제가 붙는 절(`## 완료 조건 (Definition of Done)`)도 같은 규칙으로 잡힌다(괄호 부제 허용).

    존재 축 강화가 DoD 슬라이서를 함께 조여 실 템플릿의 DoD 가 통째로 안 보이면 안 된다."""
    body = _body()
    assert board_mod._dod_section_text(body) is not None
    assert board_mod._dod_open_items(body), "DoD 미체크 항목을 못 봤다 — 슬라이서 회귀"


# ════════════════════════════════════════════════════════════════════════
# 설계 절 **실질** 검사 — 4항목의 존재 + 값 (T-0602 ⑤)
# ════════════════════════════════════════════════════════════════════════
# codex R2 지적: placeholder 문자열이 없다는 이유로 **빈 `## 설계` 절**이나 임의 산문 한 줄짜리
# 절도 `design: done` 을 통과했다 — 뼈대를 지우는 것만으로 claim 게이트가 열린다. 항목 이름은
# `_DESIGN_PLACEHOLDER_LABELS` 단일 진실을 재사용하고, 값이 실제로 있는지까지 본다.

_DESIGN_LABELS = board_mod._DESIGN_PLACEHOLDER_LABELS


@pytest.mark.parametrize("section, reason", [
    ("## 설계\n\n", "빈 절"),
    ("## 설계\n설계는 리뷰에서 구두로 합의했다.\n\n", "라벨 없는 산문"),
    ("## 설계\n- 뼈대를 지웠다.\n- 항목 이름도 없다.\n\n", "라벨 없는 불릿"),
])
def test_emptied_design_section_no_longer_passes(section, reason):
    """재현: 뼈대를 **삭제한** 설계 절 — placeholder 0 이지만 4항목 전부 미충전으로 잡힌다."""
    gaps = board_mod._design_section_gaps(_body(section))

    assert gaps == [f"{label} 항목 부재" for label in _DESIGN_LABELS], (
        f"{reason} 절이 통과함(뼈대 삭제 우회): {gaps}")
    issues = board_mod._design_issues("T-0602", _body(section), board_mod.DESIGN_DONE)
    assert len(issues) == 1 and "미충전" in issues[0][2]


def test_declared_item_without_a_value_is_a_gap():
    """라벨만 남기고 값을 비운 항목도 미충전이다 (형식만 흉내낸 통과 차단)."""
    section = (
        "## 설계\n"
        "- **경계 실측**: `board.py lint` 실행 → rc=0 확인.\n"
        "- **불변식**:\n"
        "- **표면 상한**: 입력은 frontmatter 문자열 하나 — 유한.\n"
        "- **테스트 전략**:   \n\n"
    )

    assert board_mod._design_section_gaps(_body(section)) == [
        "불변식 값 없음", "테스트 전략 값 없음"]


def test_value_written_as_sub_bullets_is_not_an_empty_value():
    """값을 하위 불릿으로 적은 실 표기는 '값 없음'이 아니다 (오탐 0 — 다음 항목 줄까지가 값)."""
    section = (
        "## 설계\n"
        "- **경계 실측**: 실행 로그로 확인.\n"
        "- **불변식**:\n"
        "  - 어떤 입력에도 rc 는 0 또는 1 이다.\n"
        "- **표면 상한**: 상태 5종으로 유한.\n"
        "- **테스트 전략**:\n"
        "  - 경계값 3종 · 실패 경로 1종.\n\n"
    )

    assert board_mod._design_section_gaps(_body(section)) == []


def test_missing_one_item_names_exactly_that_item():
    """항목 하나만 지우면 그 항목만 지목한다 (경고가 무엇이 비었는지 말한다)."""
    section = _DESIGN_FILLED.replace(
        "- **표면 상한**: 입력은 frontmatter 문자열 하나 — 상태 5종으로 유한.\n", "")

    assert board_mod._design_section_gaps(_body(section)) == ["표면 상한 항목 부재"]


def test_skeleton_still_reports_one_gap_per_item():
    """뼈대 잔존 축은 종전대로다 — 항목마다 판정 1건(부재/값 없음과 중복 보고하지 않는다)."""
    gaps = board_mod._design_section_gaps(_body(_DESIGN_SKELETON))

    assert gaps == list(_DESIGN_LABELS), f"뼈대 절 판정이 항목당 1건이 아니다: {gaps}"


def test_filled_section_stays_clean_and_claimable():
    """정상 경로 무변경 — 4항목을 실값으로 채운 절은 `design: done` 으로 0건이다."""
    assert board_mod._design_section_gaps(_body()) == []
    assert board_mod._design_issues("T-0602", _body(), board_mod.DESIGN_DONE) == []


@pytest.mark.parametrize("bullet", ["-", "*", "+"])
def test_item_detection_accepts_all_bullet_markers(bullet):
    """불릿 세 종과 굵게 표기 유무는 항목 인식에 영향을 주지 않는다 (표기 편차 흡수)."""
    lines = "\n".join(
        f"{bullet} {label}: 실값 {index}." for index, label in enumerate(_DESIGN_LABELS, 1))

    assert board_mod._design_section_gaps(_body(f"## 설계\n{lines}\n\n")) == []


def test_claim_blocked_when_done_but_design_section_is_emptied(board, capsys):
    """claim 게이트 e2e — 뼈대를 지운 빈 설계 절 + `design: done` 은 여전히 차단된다 (DoD)."""
    _seed_open(board, design=board_mod.DESIGN_DONE, body=_body("## 설계\n\n"))

    assert board.cmd_claim(_claim_args()) == 1
    err = capsys.readouterr().err
    assert "설계 단계 미완" in err and "항목 부재" in err
    assert list((board.TICKETS_DIR / "open").glob("T-*.md")), "차단됐는데 티켓이 open/ 을 떠남"


# ════════════════════════════════════════════════════════════════════════
# promote 게이트 (board-git 활성 홈 e2e)
# ════════════════════════════════════════════════════════════════════════

def _git(argv, cwd):
    return subprocess.run(["git", *argv], cwd=str(cwd), capture_output=True,
                          text=True, encoding="utf-8", errors="replace", check=False)


@pytest.fixture
def board_git(tmp_path, monkeypatch):
    """board 가 별도 git(공유 형상)인 hermetic 홈 — draft 격리·promote 게이트가 작동한다."""
    mod = _load_board()
    monkeypatch.setattr(mod, "REPO", tmp_path)
    monkeypatch.setattr(mod, "BOARD_LOCK",
                        tmp_path / ".project_manager" / ".local" / "board.lock")
    for key, val in _GIT_IDENTITY.items():
        monkeypatch.setenv(key, val)
    board = tmp_path / ".project_manager" / "board"
    for status in ("open", "claimed", "blocked", "done"):
        (board / "tickets" / status).mkdir(parents=True, exist_ok=True)
    (board / "tickets" / "_template.md").write_text(
        TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
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


def _draft_path(board_dir: Path) -> Path:
    return list((board_dir / "tickets" / ".drafts").glob("T-*-*.md"))[0]


@requires_git
def test_promote_rejects_design_required_with_unfilled_section(board_git, capsys):
    """`design: required` draft 의 설계 절 미충전은 promote 에서 거부(rc=1)·`.drafts/` 잔류."""
    board_dir = board_git._board_dir
    assert board_git.cmd_new(_new_args(estimate="large")) == 0
    draft = _draft_path(board_dir)
    fm, _ = board_git.load_ticket(draft)
    assert fm["design"] == board_git.DESIGN_REQUIRED
    board_git.dump_ticket(draft, fm, _body(_DESIGN_SKELETON))   # 5절은 채우고 설계만 뼈대

    assert board_git.cmd_promote(argparse.Namespace(id=fm["id"])) == 1
    assert "design-pending" in capsys.readouterr().err
    assert list((board_dir / "tickets" / ".drafts").glob("T-*-*.md")), \
        "거부된 draft 는 .drafts/ 에 남아야 한다."
    assert not list((board_dir / "tickets" / "open").glob("T-*-*.md"))


@requires_git
def test_promote_rejects_design_required_even_when_section_is_filled(board_git, capsys):
    """설계 절을 다 채워도 `design: required` 면 promote 가 거부한다 (엄격 promote).

    claim 게이트와 같은 판정이 승격 자리에도 걸린다 — **설계 검토 완료(`done`/`waived`)가
    open 진입 조건**이라, 절만 채우고 검토 없이 공유 보드에 올리는 경로를 막는다. 미충전
    케이스만 있으면 "채우면 통과"로 오독되므로 충전 케이스를 따로 못박는다(T-0594 R1 공백).
    """
    board_dir = board_git._board_dir
    assert board_git.cmd_new(_new_args(estimate="large")) == 0
    draft = _draft_path(board_dir)
    fm, _ = board_git.load_ticket(draft)
    assert fm["design"] == board_git.DESIGN_REQUIRED
    board_git.dump_ticket(draft, fm, _body())      # 설계 절까지 전부 충전·필드만 required

    assert board_git.cmd_promote(argparse.Namespace(id=fm["id"])) == 1
    err = capsys.readouterr().err
    assert "design-pending" in err
    assert board_git.DESIGN_DONE in err, "승격 처방(design: done)이 안내에 없다."
    assert list((board_dir / "tickets" / ".drafts").glob("T-*-*.md")), \
        "거부된 draft 는 .drafts/ 에 남아야 한다."
    assert not list((board_dir / "tickets" / "open").glob("T-*-*.md"))


@requires_git
def test_promote_accepts_filled_design_section_with_done(board_git):
    """설계 절 완성 + `design: done` draft 는 승격(rc=0)·open/ 이동."""
    board_dir = board_git._board_dir
    assert board_git.cmd_new(_new_args(estimate="large")) == 0
    draft = _draft_path(board_dir)
    fm, _ = board_git.load_ticket(draft)
    fm["design"] = board_git.DESIGN_DONE
    board_git.dump_ticket(draft, fm, _body())

    assert board_git.cmd_promote(argparse.Namespace(id=fm["id"])) == 0
    assert list((board_dir / "tickets" / "open").glob("T-*-*.md")), \
        "승격된 티켓이 open/ 으로 이동 안 됨."


@requires_git
def test_promote_unaffected_for_na_ticket_with_skeleton_design(board_git):
    """`n/a` 티켓은 설계 절이 뼈대인 채로도 승격된다(설계 단계 비대상)."""
    board_dir = board_git._board_dir
    assert board_git.cmd_new(_new_args(estimate="small")) == 0
    draft = _draft_path(board_dir)
    fm, _ = board_git.load_ticket(draft)
    assert fm["design"] == board_git.DESIGN_NA
    board_git.dump_ticket(draft, fm, _body(_DESIGN_SKELETON))

    assert board_git.cmd_promote(argparse.Namespace(id=fm["id"])) == 0
    assert list((board_dir / "tickets" / "open").glob("T-*-*.md"))


# ════════════════════════════════════════════════════════════════════════
# 출하 파리티 — 채택자 3벌 도달 가드
# ════════════════════════════════════════════════════════════════════════

def test_root_template_declares_design_field_and_section():
    """루트 `_template.md` 가 `design:` 필드(기본 n/a)와 `## 설계` 절을 갖는다."""
    assert _TEMPLATE_FM.get("design") == board_mod.DESIGN_NA
    assert board_mod._DESIGN_SECTION in _TEMPLATE_BODY


@pytest.mark.parametrize("shipped", _SHIPPED_TICKET_TEMPLATES,
                         ids=lambda p: p.parents[3].name)
def test_shipped_ticket_templates_include_design_stage(shipped):
    """출하 템플릿 3벌의 ticket 템플릿이 루트와 byte-identical (설계 절·필드 도달)."""
    assert shipped.read_text(encoding="utf-8") == TEMPLATE.read_text(encoding="utf-8"), \
        f"{shipped} 가 루트 템플릿과 다름 — pm_update --all-targets 미실행(채택자 미도달)."


def test_root_skill_card_documents_design_stage():
    """pm-ticket 스킬 카드가 설계 단계 운영 규칙(작성 주체·검토 상한·승격)을 명시한다."""
    text = SKILL.read_text(encoding="utf-8")
    for needle in ("## 설계", "design: required", "design: done",
                   "PM 인라인", "architect", "2라운드", "--design"):
        assert needle in text, f"스킬 카드에 설계 단계 규율 {needle!r} 누락."


@pytest.mark.parametrize("shipped", _SHIPPED_SKILLS, ids=lambda p: p.parents[3].name)
def test_shipped_skill_cards_include_design_stage(shipped):
    """출하 스킬 3벌에도 설계 단계 절이 도달했다 (codex 는 `$` 커맨드 토큰만 다름)."""
    text = shipped.read_text(encoding="utf-8")
    for needle in ("## 설계", "design: required", "PM 인라인", "2라운드"):
        assert needle in text, f"{shipped} 에 설계 단계 규율 {needle!r} 누락."
