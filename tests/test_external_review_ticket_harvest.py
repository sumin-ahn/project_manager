"""T-0696 — 추가 리뷰어 산출의 티켓 회수·finding severity 단일 진실.

두 축을 소유한다.
  · 회수: `external_review --ticket` 실행이 끝나면 **엔진이** 산출을 그 티켓의
    `external-reviewer` 역할 절로 기록하고(봉인 `by=external-review`), 내부 리뷰어와 같은
    `review delta` 표면에 올린다. 블록 부재·스키마 위반은 절 경고 + rc≠0 으로 표면화한다.
  · severity: `pm-review-v1` finding 의 심각도가 블록의 필수 필드다(산문 재기재 없음).

hermetic: tmp REPO 에 실제 board 디렉터리를 만들고 diff·리뷰어 실행·격리 거울만 주입한다
(외부 전송 0). 라이브 codex 호출은 이 파일 어디에서도 하지 않는다.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
DIFF = "diff --git a/x.py b/x.py\n@@ -1 +1 @@\n-old\n+new\n"
TICKET = "T-9601"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"{name}_ticket_harvest", TOOLS / f"{name}.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def external():
    return _load("external_review")


@pytest.fixture
def pd():
    return _load("pm_delegate")


@pytest.fixture
def board():
    return _load("board")


# ── 픽스처: tmp PM 홈 board + 리뷰어 산출 ──────────────────────────────────


def _ticket_path(pm_home: Path, status: str = "claimed") -> Path:
    return (
        pm_home / ".project_manager" / "board" / "tickets" / status
        / f"{TICKET}-harvest.md"
    )


def _seed_board(pm_home: Path, *, status: str = "claimed", body: str = "") -> Path:
    path = _ticket_path(pm_home, status)
    path.parent.mkdir(parents=True, exist_ok=True)
    (pm_home / ".project_manager" / "wiki").mkdir(parents=True, exist_ok=True)
    (pm_home / ".project_manager" / ".local").mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        f"id: {TICKET}\n"
        "title: 회수 픽스처\n"
        f"status: {status}\n"
        "touches:\n- x.py\n"
        "---\n"
        f"# {TICKET}\n\n## 목표\n추가 리뷰어 산출 회수.\n" + body,
        encoding="utf-8", newline="\n",
    )
    return path


def _finding(
    finding_id: str = "X-001", *, severity: str = "must-fix",
    classification: str = "implementation-defect", design_change: bool = False,
) -> dict:
    return {
        "id": finding_id,
        "class": classification,
        "severity": severity,
        "authority": f"[[{TICKET}]] §결정",
        "evidence": f"{finding_id} probe rc=1",
        "recommendation": f"{finding_id}만 수정",
        "design_change": design_change,
    }


def _block(payload: dict) -> str:
    return (
        "```pm-review-v1\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + "\n```\n"
    )


def _reject_reply(*findings: dict, prose_items: bool = True) -> str:
    listed = (
        "\n".join(f"- {row['id']}" for row in findings) if prose_items else "- 없음"
    )
    return (
        "판정: 반려\n\n"
        f"**must-fix** (반드시 수정):\n{listed}\n\n"
        "**suggestion** (권장):\n- 없음\n\n"
        + _block({
            "version": 1, "findings": list(findings), "confirmations": [],
        })
    )


def _confirm_reply(*confirmations: dict) -> str:
    return (
        "판정: 통과\n\n"
        "**must-fix** (반드시 수정):\n- 없음\n\n"
        "**suggestion** (권장):\n- 없음\n\n"
        + _block({
            "version": 1, "findings": [], "confirmations": list(confirmations),
        })
    )


def _disposition(finding_id: str, *, ordinal: int = 0, role: str = "external-reviewer",
                 decision: str = "accepted") -> str:
    payload = {
        "version": 1,
        "reviewer_role": role,
        "reviewer_ordinal": ordinal,
        "dispositions": [{
            "id": finding_id,
            "decision": decision,
            "reason": "PM 수락 근거",
            "scope": f"{finding_id} 허용 범위" if decision == "accepted" else "",
            "prerequisite": "",
        }],
    }
    return (
        "\n```pm-review-disposition-v1\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + "\n```\n"
    )


def _wire(external, monkeypatch, pm_home: Path, reply: str) -> dict:
    """main() 을 tmp PM 홈으로 배선한다 — 반환 dict['n'] = 리뷰어 호출 수(외부 전송 시도)."""
    monkeypatch.setattr(external, "REPO", pm_home)
    monkeypatch.setattr(
        external, "local_config",
        lambda repo=None: {"additional_reviewer_enabled": "true"},
    )
    monkeypatch.setattr(external, "extract_diff", lambda *a, **k: (DIFF, []))

    def _workspace(*args, **kwargs):
        root = pm_home / "reviewer"
        tree, home = root / "tree", root / "home"
        tree.mkdir(parents=True, exist_ok=True)
        home.mkdir(parents=True, exist_ok=True)
        return external.ReviewerWorkspace(
            root=root, tree=tree, home=home,
            files=1, skipped_unsafe=0, git_repo=True,
        )

    calls = {"n": 0, "prompt": ""}

    def _run_review(prompt, *args, **kwargs):
        calls["n"] += 1
        calls["prompt"] = prompt
        rejected = "판정: 반려" in reply
        return {
            "reviewer": "fixture", "ok": True, "output": reply, "answer": reply,
            "verdict": {"has_must_fix": rejected, "has_pass": not rejected},
            "file": pm_home / "raw" / "fixture.md",
            "failed": False, "started": True,
            "any_must_fix": rejected, "all_pass": not rejected,
        }

    monkeypatch.setattr(external, "create_reviewer_workspace", _workspace)
    monkeypatch.setattr(external, "run_review", _run_review)
    return calls


def _run(external, pm_home: Path, *argv: str) -> int:
    return external.main([*argv, "--output-dir", str(pm_home / "raw")])


def _sections(pd, text: str, role: str) -> list[str]:
    return [
        text[section.content_start:section.content_end]
        for section in pd._ticket_growth_sections(text)
        if section.role == role
    ]


# ── (a) 반려 라운드 회수 → 같은 판정 표면 ──────────────────────────────────


def test_rejected_round_lands_as_sealed_external_section_and_delta_finding(
    external, pd, monkeypatch, tmp_path, capsys,
):
    """(a) 반려 산출이 봉인된 external-reviewer 절이 되고 `review delta` 에 X- 가 뜬다."""
    ticket_path = _seed_board(tmp_path)
    _wire(external, monkeypatch, tmp_path, _reject_reply(_finding()))

    assert _run(external, tmp_path, "--ticket", TICKET) == 1  # 반려 = 판정 rc
    text = ticket_path.read_text(encoding="utf-8")
    assert pd.verify_ticket_seals(text) == []
    assert pd.parse_ticket_seals(text)[("external-reviewer", 0)].by == "external-review"
    body = _sections(pd, text, "external-reviewer")
    assert len(body) == 1 and '"id":"X-001"' in body[0]
    assert pd.EXTERNAL_REVIEW_BLOCK_WARNING_PREFIX not in body[0]
    assert "external-reviewer[0]" in capsys.readouterr().err

    with pytest.raises(pd.PMReviewError) as caught:
        pd.parse_pm_review_delta(text)
    assert caught.value.code == "pending"  # PM 판정 전에는 loud
    # 판정 표면이 하나라 PM 은 어느 채널을 판정해야 하는지 진단에서 바로 안다.
    assert "대상 채널=['external-reviewer[0]']" in str(caught.value)
    delta = pd.parse_pm_review_delta(text + _disposition("X-001"))
    accepted = [finding for finding, _row in delta.accepted]
    assert [item.id for item in accepted] == ["X-001"]
    assert accepted[0].reviewer_role == "external-reviewer"
    rendered = pd.render_pm_review_delta(TICKET, delta)
    assert "- 채널: external-reviewer" in rendered
    assert "- 심각도: must-fix" in rendered


def test_second_round_appends_a_new_sealed_section(
    external, pd, monkeypatch, tmp_path,
):
    """라운드마다 절이 누적되고 ordinal 이 채널 안에서 독립적으로 증가한다."""
    ticket_path = _seed_board(tmp_path)
    _wire(external, monkeypatch, tmp_path, _reject_reply(_finding("X-001")))
    assert _run(external, tmp_path, "--ticket", TICKET) == 1
    _wire(external, monkeypatch, tmp_path, _reject_reply(_finding("X-002")))
    assert _run(external, tmp_path, "--ticket", TICKET) == 1

    text = ticket_path.read_text(encoding="utf-8")
    assert len(_sections(pd, text, "external-reviewer")) == 2
    assert pd.verify_ticket_seals(text) == []
    seals = pd.parse_ticket_seals(text)
    assert {key for key in seals if key[0] == "external-reviewer"} == {
        ("external-reviewer", 0), ("external-reviewer", 1),
    }


# ── (b) 블록 부재·위반 → rc≠0 · 절 경고 · delta malformed ────────────────


@pytest.mark.parametrize(
    "reply,reason",
    [
        pytest.param(
            "판정: 통과\n\n**must-fix** (반드시 수정):\n- 없음\n",
            "block 이 정확히 하나가 아닙니다",
            id="absent",
        ),
        pytest.param(
            "판정: 통과\n\n**must-fix** (반드시 수정):\n- 없음\n\n"
            + _block({"version": 1, "findings": [], "confirmations": []})
            + _block({"version": 1, "findings": [], "confirmations": []}),
            "block 이 정확히 하나가 아닙니다",
            id="duplicate",
        ),
        pytest.param(
            "판정: 반려\n\n**must-fix** (반드시 수정):\n- X-001\n\n"
            + _block({
                "version": 1,
                "findings": [{
                    key: value for key, value in _finding().items()
                    if key != "severity"
                }],
                "confirmations": [],
            }),
            "missing=['severity']",
            id="severity-absent",
        ),
        pytest.param(
            "판정: 반려\n\n**must-fix** (반드시 수정):\n- X-001\n\n"
            + _block({
                "version": 1,
                "findings": [_finding(severity="blocker")],
                "confirmations": [],
            }),
            "severity 미지원",
            id="severity-unsupported",
        ),
        pytest.param(
            "판정: 통과\n\n**must-fix** (반드시 수정):\n- 없음\n\n"
            + _block({
                "version": 1,
                "findings": [],
                "confirmations": [{
                    "id": "F-001", "status": "resolved", "evidence": "타 채널 ID",
                }],
            }),
            "채널 접두 불일치",
            id="pass-round-confirmation-namespace",
        ),
        pytest.param(
            "판정: 반려\n\n**must-fix** (반드시 수정):\n- F-001\n\n"
            + _block({
                "version": 1,
                "findings": [_finding("F-001")],
                "confirmations": [],
            }),
            "채널 접두 불일치",
            id="wrong-id-namespace",
        ),
    ],
)
def test_missing_or_malformed_block_is_loud_in_section_and_exit_code(
    external, pd, monkeypatch, tmp_path, capsys, reply, reason,
):
    """(b)(h)(i) 스키마 위반은 조용히 장부에만 남지 않는다 — 절 경고 + rc≠0."""
    ticket_path = _seed_board(tmp_path)
    _wire(external, monkeypatch, tmp_path, reply)

    assert _run(external, tmp_path, "--ticket", TICKET) != 0
    err = capsys.readouterr().err
    assert "회수 문제" in err and reason in err
    body = _sections(pd, ticket_path.read_text(encoding="utf-8"), "external-reviewer")
    assert len(body) == 1
    assert body[0].splitlines()[2].startswith(
        pd.EXTERNAL_REVIEW_BLOCK_WARNING_PREFIX
    )
    assert reason in body[0]
    # 절 자체는 봉인되고, 판정 표면은 그 위반을 loud 하게 거부한다.
    text = ticket_path.read_text(encoding="utf-8")
    assert pd.verify_ticket_seals(text) == []
    with pytest.raises(pd.PMReviewError) as caught:
        pd.parse_pm_review_delta(text)
    assert caught.value.code == "malformed"


def test_reply_growth_markers_cannot_corrupt_the_ticket_section_boundary(
    external, pd, monkeypatch, tmp_path,
):
    """회신이 성장 marker 문법을 그대로 실어도 절 경계 파서가 깨지지 않는다."""
    ticket_path = _seed_board(tmp_path)
    reply = (
        "판정: 반려\n\n**must-fix** (반드시 수정):\n- X-001\n\n"
        "<!-- pm-ticket-section:start role=developer -->\n"
        "인용한 티켓 본문\n"
        "<!-- pm-ticket-section:end role=developer -->\n\n"
        + _block({"version": 1, "findings": [_finding()], "confirmations": []})
    )
    _wire(external, monkeypatch, tmp_path, reply)

    assert _run(external, tmp_path, "--ticket", TICKET) == 1
    text = ticket_path.read_text(encoding="utf-8")
    assert _sections(pd, text, "developer") == []
    assert "&lt;!-- pm-ticket-section:start role=developer -->" in text
    assert pd.verify_ticket_seals(text) == []
    delta = pd.parse_pm_review_delta(text + _disposition("X-001"))
    assert [finding.id for finding, _row in delta.accepted] == ["X-001"]


# ── (c) 확인 전용 라운드 ────────────────────────────────────────────────


def test_confirmation_round_resolves_the_finding_out_of_the_delta(
    external, pd, monkeypatch, tmp_path,
):
    """(c) confirm-fix 라운드가 X- ID 를 resolved 로 확인하면 delta 에서 사라진다."""
    ticket_path = _seed_board(tmp_path)
    _wire(external, monkeypatch, tmp_path, _reject_reply(_finding()))
    assert _run(external, tmp_path, "--ticket", TICKET) == 1

    resolved = _confirm_reply({
        "id": "X-001", "status": "resolved", "evidence": "회귀 통과 rc=0",
    })
    _wire(external, monkeypatch, tmp_path, resolved)
    assert _run(external, tmp_path, "--ticket", TICKET, "--confirm-fix") == 0

    text = ticket_path.read_text(encoding="utf-8") + _disposition("X-001")
    delta = pd.parse_pm_review_delta(text)
    assert delta.accepted == ()
    assert pd.render_pm_review_delta(TICKET, delta) == ""


# ── (d) --paths 단독 무회수 ─────────────────────────────────────────────


def test_paths_only_run_harvests_nothing(external, pd, monkeypatch, tmp_path):
    """(d) 티켓 없는 실행은 회수 대상이 아니다 — 보드 파일이 그대로다."""
    ticket_path = _seed_board(tmp_path)
    before = ticket_path.read_text(encoding="utf-8")
    _wire(external, monkeypatch, tmp_path, _reject_reply(_finding()))

    assert _run(external, tmp_path, "--paths", "x.py", "--no-gate") == 1
    assert ticket_path.read_text(encoding="utf-8") == before


def test_free_form_gate_without_ticket_harvests_nothing(
    external, pd, monkeypatch, tmp_path,
):
    """자유 문자열 게이트도 회수 대상이 아니다(회수 입력은 `--ticket` 하나다)."""
    ticket_path = _seed_board(tmp_path)
    before = ticket_path.read_text(encoding="utf-8")
    _wire(external, monkeypatch, tmp_path, _reject_reply(_finding()))

    assert _run(external, tmp_path, "--gate", "free-gate", "--paths", "x.py") == 1
    assert ticket_path.read_text(encoding="utf-8") == before


def test_done_ticket_write_is_refused_loudly(external, pd, monkeypatch, tmp_path):
    """완료 티켓에는 절을 쓰지 않는다 — 거부 사유가 표면화되고 rc≠0 이다."""
    ticket_path = _seed_board(tmp_path, status="done")
    before = ticket_path.read_text(encoding="utf-8")
    _wire(external, monkeypatch, tmp_path, _reject_reply(_finding()))

    assert _run(external, tmp_path, "--ticket", TICKET) != 0
    assert ticket_path.read_text(encoding="utf-8") == before


# ── (e) researcher 편입 (prepare → harvest 왕복) ─────────────────────────


def _researcher_env(pd, tmp_path, monkeypatch):
    pm_home = tmp_path / "pm-home"
    slot = tmp_path / "slot"
    pm_tools = pm_home / ".project_manager" / "tools"
    pm_tools.mkdir(parents=True)
    for source in TOOLS.glob("*.py"):
        shutil.copy2(source, pm_tools / source.name)
    tickets = pm_home / ".project_manager" / "board" / "tickets" / "claimed"
    tickets.mkdir(parents=True)
    (pm_home / ".project_manager" / ".local").mkdir(parents=True)
    # 사본 루트가 git 무시 대상인지 실제 확인하는 경계라 slot 은 실 저장소여야 한다.
    slot.mkdir()
    slot_ignore = slot / ".project_manager" / ".gitignore"
    slot_ignore.parent.mkdir()
    slot_ignore.write_text(".local/\n", encoding="utf-8", newline="\n")
    subprocess.run(["git", "-C", str(slot), "init", "-q"], check=True)

    board = pd._load_module_from_path(
        pm_tools / "board.py", "board.py", verifier=pd._verify_engine_rev,
    )
    board.REPO = pm_home
    board.LOCAL_DIR = pm_home / ".project_manager" / ".local"
    board.BOARD_LOCK = board.LOCAL_DIR / "board.lock"
    board._growth_mutation_sync = lambda _message, _path: True
    monkeypatch.setattr(pd, "_load_board_for_repo", lambda _repo: board)
    return pm_home, slot, tickets, board


def test_researcher_ticket_copy_roundtrip_lands_its_own_section(
    pd, tmp_path, monkeypatch,
):
    """(e) researcher 도 사본 prepare → 자기 절 기록 → harvest 로 산출을 남긴다."""
    pm_home, slot, tickets, board = _researcher_env(pd, tmp_path, monkeypatch)
    source = tickets / f"{TICKET}-researcher.md"
    source.write_text(
        f"---\nid: {TICKET}\nstatus: claimed\n---\n# {TICKET}\n",
        encoding="utf-8", newline="\n",
    )
    assert board.cmd_section_add(
        argparse.Namespace(role="researcher", label=None, id=TICKET)
    ) == 0

    plan = pd.prepare_ticket_copy(
        ticket=TICKET, role="researcher", cwd=slot, pm_home=pm_home,
    )
    copy_text = plan.path.read_text(encoding="utf-8")
    section = pd._ticket_role_section(copy_text, "researcher", ordinal=0)
    plan.path.write_text(
        copy_text[:section.content_start]
        + "## 조사 질문\n- 회수 경로 실측\n"
        + copy_text[section.content_end:],
        encoding="utf-8", newline="\n",
    )
    result = pd.harvest_ticket_copy(
        copy_path=plan.path, cwd=slot, pm_home=pm_home, capability=plan.capability,
    )

    assert result.changed is True
    text = source.read_text(encoding="utf-8")
    assert "회수 경로 실측" in text
    assert pd.verify_ticket_seals(text) == []
    assert pd.parse_ticket_seals(text)[("researcher", 0)].by == "harvest"


def test_external_reviewer_copy_prepare_is_refused(pd, tmp_path, monkeypatch):
    """external-reviewer 절은 사본 왕복이 아니라 엔진이 쓴다 — prepare 는 거부다."""
    pm_home, slot, tickets, _board = _researcher_env(pd, tmp_path, monkeypatch)
    (tickets / f"{TICKET}-x.md").write_text(
        f"---\nid: {TICKET}\nstatus: claimed\n---\n# {TICKET}\n",
        encoding="utf-8", newline="\n",
    )
    with pytest.raises(pd.DelegateError, match="미지원 역할"):
        pd.prepare_ticket_copy(
            ticket=TICKET, role="external-reviewer", cwd=slot, pm_home=pm_home,
        )


# ── (f) 불변식 ─────────────────────────────────────────────────────────


def test_every_process_role_is_a_ticket_growth_role(pd, board):
    """(f) PM 프로세스 참여 역할 ⊆ 티켓 성장 역할 — 채널이 늘면 여기서 걸린다."""
    assert set(pd.ROLE_CHOICES) <= set(pd.TICKET_COPY_ROLES)
    assert "external-reviewer" in pd.TICKET_COPY_ROLES
    assert "researcher" in pd.TICKET_COPY_ROLES
    assert set(pd.REVIEW_ROLES) <= set(pd.TICKET_COPY_ROLES)
    assert set(pd.PM_REVIEW_FINDING_ID_PREFIXES) == set(pd.REVIEW_ROLES)
    assert len(set(pd.PM_REVIEW_FINDING_ID_PREFIXES.values())) == len(pd.REVIEW_ROLES)
    # section-add 선택지와 사본 준비 대상이 같은 집합에서 나온다.
    assert set(board.TICKET_GROWTH_ROLE_LABELS) == set(pd.TICKET_COPY_ROLES)
    assert set(pd.TICKET_COPY_PREPARE_ROLES) == set(pd.TICKET_COPY_ROLES) - {
        "external-reviewer",
    }
    assert "external-review" in pd.TICKET_SEAL_WRITERS


# ── (g) 기존 티켓 호환 ─────────────────────────────────────────────────


def test_disposition_without_reviewer_role_is_read_as_internal_channel(pd):
    """(g) `reviewer_role` 부재 disposition 은 code-reviewer 판정으로 해석한다."""
    content = (
        "## 리뷰 (code-reviewer · 2026-08-17)\n\n"
        "## must-fix\n- F-001\n\n## 판정\n판정: 반려\n\n"
        + _block({
            "version": 1,
            "findings": [_finding("F-001")],
            "confirmations": [],
        })
    )
    digest = pd.seal_for(content)
    ticket = (
        "<!-- pm-ticket-section:start role=code-reviewer -->\n"
        + content
        + "<!-- pm-ticket-section:end role=code-reviewer -->\n"
        + f"<!-- pm-ticket-seal role=code-reviewer ordinal=0 sha256={digest} "
        "by=backfill -->\n"
    )
    legacy = json.dumps({
        "version": 1,
        "reviewer_ordinal": 0,
        "dispositions": [{
            "id": "F-001", "decision": "accepted", "reason": "구 티켓 판정",
            "scope": "F-001 허용 범위", "prerequisite": "",
        }],
    }, ensure_ascii=False, separators=(",", ":"))
    delta = pd.parse_pm_review_delta(
        ticket + "\n```pm-review-disposition-v1\n" + legacy + "\n```\n"
    )
    finding, _row = delta.accepted[0]
    assert finding.id == "F-001" and finding.reviewer_role == pd.INTERNAL_REVIEW_ROLE


# ── (j)(k)(l) severity 렌더·산문 축소·0건 교차 확인 ──────────────────────


def test_board_show_renders_severity_channel_and_disposition_state(
    external, pd, board, monkeypatch, tmp_path, capsys,
):
    """(j) `board.py show` 가 블록에서 severity·채널·판정 상태를 요약 렌더한다."""
    ticket_path = _seed_board(tmp_path)
    _wire(external, monkeypatch, tmp_path, _reject_reply(_finding(severity="should-fix")))
    assert _run(external, tmp_path, "--ticket", TICKET) == 1
    capsys.readouterr()

    monkeypatch.setattr(
        board, "find_ticket", lambda _tid: ("claimed", ticket_path),
    )
    assert board.cmd_show(argparse.Namespace(id=TICKET)) == 0
    out = capsys.readouterr().out
    assert "-- 리뷰 finding 요약 --" in out
    assert "external-reviewer[0] X-001 severity=should-fix" in out
    assert "PM=미판정" in out

    ticket_path.write_text(
        ticket_path.read_text(encoding="utf-8") + _disposition("X-001"),
        encoding="utf-8", newline="\n",
    )
    assert board.cmd_show(argparse.Namespace(id=TICKET)) == 0
    assert "PM=accepted" in capsys.readouterr().out


def test_show_summary_keeps_working_on_a_legacy_section_without_a_block(
    pd, board, monkeypatch, tmp_path, capsys,
):
    """versioned block 도입 전 리뷰 절이 섞여 있어도 조회는 열려 있다(표시면 규칙)."""
    content = "## 리뷰 (code-reviewer · 2026-08-13)\n\n판정: 통과\n\n## must-fix\n- 없음\n"
    digest = pd.seal_for(content)
    legacy = (
        "\n<!-- pm-ticket-section:start role=code-reviewer -->\n"
        + content
        + "<!-- pm-ticket-section:end role=code-reviewer -->\n"
        + f"<!-- pm-ticket-seal role=code-reviewer ordinal=0 sha256={digest} "
        "by=backfill -->\n"
    )
    ticket_path = _seed_board(tmp_path, body=legacy)
    monkeypatch.setattr(board, "find_ticket", lambda _tid: ("claimed", ticket_path))

    assert board.cmd_show(argparse.Namespace(id=TICKET)) == 0
    out = capsys.readouterr().out
    assert "code-reviewer[0] versioned block 없음" in out


def test_prose_item_enumeration_is_optional_for_the_block_truth(
    external, pd, monkeypatch, tmp_path,
):
    """(k) 산문이 항목을 나열하지 않아도(판정 요약만) 블록이 판정 입력으로 선다."""
    ticket_path = _seed_board(tmp_path)
    reply = (
        "판정: 반려 · finding 1건(must-fix 1건)\n\n"
        "**must-fix** (반드시 수정):\n- X-001\n\n"
        + _block({"version": 1, "findings": [_finding()], "confirmations": []})
    )
    _wire(external, monkeypatch, tmp_path, reply)
    assert _run(external, tmp_path, "--ticket", TICKET) == 1

    text = ticket_path.read_text(encoding="utf-8")
    body = _sections(pd, text, "external-reviewer")[0]
    assert "X-001 probe rc=1" not in body.split("```pm-review-v1")[0]
    delta = pd.parse_pm_review_delta(text + _disposition("X-001"))
    finding, _row = delta.accepted[0]
    assert finding.evidence == "X-001 probe rc=1"


def test_finding_zero_round_must_agree_with_the_prose_pass_declaration(
    external, pd, monkeypatch, tmp_path,
):
    """(l) finding 0건 라운드가 산문 통과 선언과 어긋나면 종전대로 malformed 다."""
    ticket_path = _seed_board(tmp_path)
    contradiction = (
        "판정: 반려\n\n**must-fix** (반드시 수정):\n- 산문만 반려\n\n"
        + _block({"version": 1, "findings": [], "confirmations": []})
    )
    _wire(external, monkeypatch, tmp_path, contradiction)
    assert _run(external, tmp_path, "--ticket", TICKET) == 1
    with pytest.raises(pd.PMReviewError, match="통과\\+must-fix 0건 선언과 모순") as caught:
        pd.parse_pm_review_delta(ticket_path.read_text(encoding="utf-8"))
    assert caught.value.code == "malformed"


def test_finding_zero_pass_round_needs_only_the_compact_pm_acceptance(
    external, pd, monkeypatch, tmp_path,
):
    """0건 통과 라운드는 산문 통과 선언 + finding-zero 판정으로 닫힌다."""
    ticket_path = _seed_board(tmp_path)
    passing = (
        "판정: 통과\n\n**must-fix** (반드시 수정):\n- 없음\n\n"
        + _block({"version": 1, "findings": [], "confirmations": []})
    )
    _wire(external, monkeypatch, tmp_path, passing)
    assert _run(external, tmp_path, "--ticket", TICKET) == 0

    text = ticket_path.read_text(encoding="utf-8")
    zero = json.dumps({
        "version": 1,
        "reviewer_role": "external-reviewer",
        "reviewer_ordinal": 0,
        "finding_zero": "accepted",
    }, ensure_ascii=False, separators=(",", ":"))
    delta = pd.parse_pm_review_delta(
        text + "\n```pm-review-disposition-v1\n" + zero + "\n```\n"
    )
    assert delta.finding_zero is True and delta.accepted == ()


# ── 프롬프트: 구조화 블록 요구는 엔진 상수에서만 나온다 ────────────────────


def test_prompt_requires_the_versioned_block_rendered_from_parser_constants(
    external, pd, monkeypatch, tmp_path,
):
    """출력 형식의 스키마는 파서 상수에서 렌더한다 — 프롬프트가 스키마를 다시 적지 않는다."""
    _seed_board(tmp_path)
    calls = _wire(external, monkeypatch, tmp_path, _reject_reply(_finding()))
    assert _run(external, tmp_path, "--ticket", TICKET) == 1

    prompt = calls["prompt"]
    assert pd.render_pm_review_block_skeleton("external-reviewer") in prompt
    assert "X-001" in prompt and "severity" in prompt

    monkeypatch.setattr(
        pd, "PM_REVIEW_SEVERITIES", (*pd.PM_REVIEW_SEVERITIES, "blocker-probe"),
    )
    monkeypatch.setattr(external, "_load_pm_delegate", lambda: pd)
    assert "blocker-probe" in external._versioned_block_requirement()


def test_disposition_template_targets_the_external_channel(
    external, pd, monkeypatch, tmp_path,
):
    """PM 은 채널마다 다른 절차를 밟지 않는다 — 같은 골격 명령이 X- finding 을 채운다."""
    ticket_path = _seed_board(tmp_path)
    _wire(external, monkeypatch, tmp_path, _reject_reply(_finding()))
    assert _run(external, tmp_path, "--ticket", TICKET) == 1

    text = ticket_path.read_text(encoding="utf-8")
    rendered = pd.render_pm_review_disposition_template(text)
    payload = json.loads(
        rendered.split("```pm-review-disposition-v1\n", 1)[1].split("\n```", 1)[0]
    )
    assert payload["reviewer_role"] == "external-reviewer"
    assert [row["id"] for row in payload["dispositions"]] == ["X-001"]
