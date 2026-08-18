"""T-0696 — 추가 리뷰어 산출의 티켓 회수·finding severity 단일 진실.

두 축을 소유한다.
  · 회수: `external_review --ticket` 실행이 끝나면 **엔진이** 산출을 그 티켓의
    `external-reviewer` 역할 절로 기록하고(봉인 `by=external-review`), 내부 리뷰어와 같은
    `review delta` 표면에 올린다. 회수 거부 사유(블록 부재·스키마 위반·중복·JSON 손상·finding
    ID 재선언)는 종류를 가리지 않고 같은 처리다 — 절 머리에 엔진 표식 + 경고를 남기고 위반
    블록의 fence 를 평문으로 낮춰 산출은 보존하되, 그 절만 판정 표면에서 빼고 rc≠0 으로
    표면화한다(거부한 라운드가 티켓 delta 를 영구 malformed 로 잠그지 않는다).
  · severity: `pm-review-v1` finding 의 심각도가 블록의 필수 필드다(산문 재기재 없음).

hermetic: tmp REPO 에 실제 board 디렉터리를 만들고 diff·리뷰어 실행·격리 거울만 주입한다
(외부 전송 0). 라이브 codex 호출은 이 파일 어디에서도 하지 않는다.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import inspect
import json
import re
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


# 세대·값 enum 은 엔진 상수에서 읽는다 — 테스트가 리터럴로 적으면 승격 때 조용히 갈린다.
PD = _load("pm_delegate")
BLOCK_VERSION = PD.PM_REVIEW_VERSION
LEGACY_BLOCK_VERSION = PD.PM_REVIEW_LEGACY_VERSION
DISPOSITION_VERSION = PD.PM_REVIEW_DISPOSITION_VERSION


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
    finding_id: str = "X-001", *, severity: str | None = "must-fix",
    classification: str = "implementation-defect", design_change: bool = False,
) -> dict:
    """현행 세대 finding. `severity=None` 이면 severity 이전(v1) 스키마 형상이다."""
    finding = {
        "id": finding_id,
        "class": classification,
        "severity": severity,
        "authority": f"[[{TICKET}]] §결정",
        "evidence": f"{finding_id} probe rc=1",
        "recommendation": f"{finding_id}만 수정",
        "design_change": design_change,
    }
    if severity is None:
        del finding["severity"]
    return finding


def _payload(
    findings: list[dict] | None = None, confirmations: list[dict] | None = None,
    *, version: int = BLOCK_VERSION,
) -> dict:
    return {
        "version": version,
        "findings": list(findings or []),
        "confirmations": list(confirmations or []),
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
        + _block(_payload(list(findings)))
    )


def _confirm_reply(*confirmations: dict) -> str:
    return (
        "판정: 통과\n\n"
        "**must-fix** (반드시 수정):\n- 없음\n\n"
        "**suggestion** (권장):\n- 없음\n\n"
        + _block(_payload(confirmations=list(confirmations)))
    )


def _disposition(finding_id: str, *, ordinal: int = 0, role: str = "external-reviewer",
                 decision: str = "accepted") -> str:
    payload = {
        "version": DISPOSITION_VERSION,
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


def _wire(
    external, monkeypatch, pm_home: Path, reply, *, conf: dict | None = None,
) -> dict:
    """main() 을 tmp PM 홈으로 배선한다 — 반환 dict['n'] = 리뷰어 호출 수(외부 전송 시도).

    `reply` 가 호출 가능이면 프롬프트를 인자로 받아 회신을 만든다(프롬프트가 지시한 ID 를 따르는
    리뷰어 형상). dict['prompts'] 에는 이 배선으로 나간 프롬프트가 순서대로 쌓인다.
    """
    resolved_conf = {"additional_reviewer_enabled": "true", **(conf or {})}
    monkeypatch.setattr(external, "REPO", pm_home)
    monkeypatch.setattr(
        external, "local_config", lambda repo=None: dict(resolved_conf),
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

    calls: dict = {"n": 0, "prompt": "", "prompts": []}

    def _run_review(prompt, *args, **kwargs):
        calls["n"] += 1
        calls["prompt"] = prompt
        calls["prompts"].append(prompt)
        answer = reply(prompt) if callable(reply) else reply
        rejected = "판정: 반려" in answer
        return {
            "reviewer": "fixture", "ok": True, "output": answer, "answer": answer,
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


# ── (b) 회수 거부: 사유 불문 절 표식 + 평문 보존 + rc≠0 + 판정 표면 제외 ────


def _prose(must_fix: str = "- 없음", *, verdict: str = "판정: 반려") -> str:
    """회신 산문 머리 — `**suggestion**` 절까지 적어야 must-fix 항목 회계가 블록에서 멈춘다."""
    return (
        f"{verdict}\n\n**must-fix** (반드시 수정):\n{must_fix}\n\n"
        "**suggestion** (권장):\n- 없음\n\n"
    )


_BROKEN_JSON_REPLY = (
    _prose("- X-001") + '```pm-review-v1\n{"version":2,"findings":[\n```\n'
)
# 스캐너가 fence 후보로 보지만 라벨 표기가 어긋난 형상 — 무해화가 좁으면 이 fence 가 절에 남아
# 그 티켓의 전역 블록 스캔이 라운드와 무관하게 계속 fail-loud 한다.
_BROKEN_FENCE_REPLY = (
    _prose("- X-002") + "````pm-review-v1\n"
    + json.dumps(_payload([_finding("X-002")]), ensure_ascii=False,
                 separators=(",", ":"))
    + "\n````\n"
)


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
            + _block(_payload()) + _block(_payload()),
            "block 이 정확히 하나가 아닙니다",
            id="duplicate",
        ),
        pytest.param(
            "판정: 반려\n\n**must-fix** (반드시 수정):\n- X-001\n\n"
            + _block(_payload([_finding(severity=None)])),
            "missing=['severity']",
            id="severity-absent-in-current-generation",
        ),
        pytest.param(
            "판정: 반려\n\n**must-fix** (반드시 수정):\n- X-001\n\n"
            + _block(_payload([_finding(severity="blocker")])),
            "severity 미지원",
            id="severity-unsupported",
        ),
        pytest.param(
            "판정: 통과\n\n**must-fix** (반드시 수정):\n- 없음\n\n"
            + _block(_payload(confirmations=[{
                "id": "F-001", "status": "resolved", "evidence": "타 채널 ID",
            }])),
            "채널 접두 불일치",
            id="pass-round-confirmation-namespace",
        ),
        pytest.param(
            "판정: 반려\n\n**must-fix** (반드시 수정):\n- F-001\n\n"
            + _block(_payload([_finding("F-001")])),
            "채널 접두 불일치",
            id="wrong-id-namespace",
        ),
        pytest.param(_BROKEN_JSON_REPLY, "JSON 파싱 실패", id="broken-json"),
        pytest.param(_BROKEN_FENCE_REPLY, "손상된 review fence", id="broken-fence"),
    ],
)
def test_refused_harvest_is_loud_and_keeps_the_output_as_plain_text(
    external, pd, monkeypatch, tmp_path, capsys, reply, reason,
):
    """(b)(h)(i) 회수 거부는 사유 불문 같은 처리다 — 표식·경고·평문 보존·rc≠0."""
    ticket_path = _seed_board(tmp_path)
    _wire(external, monkeypatch, tmp_path, reply)

    assert _run(external, tmp_path, "--ticket", TICKET) != 0
    err = capsys.readouterr().err
    assert "회수 문제" in err and reason in err
    body = _sections(pd, ticket_path.read_text(encoding="utf-8"), "external-reviewer")
    assert len(body) == 1
    rows = body[0].splitlines()
    assert rows[2] == pd.EXTERNAL_REVIEW_REFUSED_LINE      # 기계 판독 표식
    assert rows[4].startswith(pd.EXTERNAL_REVIEW_BLOCK_WARNING_PREFIX)
    assert reason in body[0]
    # 산출은 절에 남되(평문) versioned fence 는 남기지 않는다.
    # 남은 fence 하나면 이 티켓의 전역 블록 스캔이 계속 fail-loud 한다(경고 산문의 언급은 무해).
    assert re.search(r"^`{3,}pm-review", body[0], re.M) is None
    if "```" in reply:
        # 평문 fence 로 낮추되 backtick 개수는 보존한다(닫는 fence 와 짝이 유지된다).
        assert re.search(r"^`{3,}text$", body[0], re.M) is not None
        assert '"version"' in body[0]
    # 절 자체는 봉인되고, 거부한 절뿐이라 판정 표면에는 올릴 블록이 없다.
    text = ticket_path.read_text(encoding="utf-8")
    assert pd.verify_ticket_seals(text) == []
    assert pd._pm_review_refused_section_keys(
        text, pd._ticket_growth_sections(text),
    ) == {("external-reviewer", 0)}
    with pytest.raises(pd.PMReviewError) as caught:
        pd.parse_pm_review_delta(text)
    assert caught.value.code == "malformed"


@pytest.mark.parametrize(
    "refused_reply,reason",
    [
        pytest.param(
            _prose("- X-002")
            + _block(_payload([_finding("X-002")]))
            + _block(_payload([_finding("X-003")])),
            "block 이 정확히 하나가 아닙니다",
            id="duplicate",
        ),
        pytest.param(
            _prose("- X-002")
            + _block(_payload([_finding("X-002", severity=None)])),
            "missing=['severity']",
            id="severity-absent",
        ),
        pytest.param(_BROKEN_JSON_REPLY, "JSON 파싱 실패", id="broken-json"),
        pytest.param(_BROKEN_FENCE_REPLY, "손상된 review fence", id="broken-fence"),
    ],
)
def test_a_refused_round_does_not_lock_the_next_round(
    external, pd, monkeypatch, tmp_path, capsys, refused_reply, reason,
):
    """(b) 거부된 라운드 뒤에도 정상 라운드가 착지하고 delta 가 green 이다.

    거부 절을 판정 표면에 세면 파서가 티켓 안 모든 리뷰 블록을 훑으므로 그 티켓의 delta 가
    영구 malformed 로 잠긴다(봉인 절은 손수정 경로가 없다 · [[ADR-0089]]).
    """
    rounds = {"review_rounds_max": "9"}
    ticket_path = _seed_board(tmp_path)
    _wire(external, monkeypatch, tmp_path,
          _reject_reply(_finding("X-001")), conf=rounds)
    assert _run(external, tmp_path, "--ticket", TICKET) == 1

    _wire(external, monkeypatch, tmp_path, refused_reply, conf=rounds)
    assert _run(external, tmp_path, "--ticket", TICKET) != 0
    assert reason in capsys.readouterr().err

    # 거부 라운드만 표면에서 빠지고 직전 정상 절은 그대로 판정 대상이다.
    text = ticket_path.read_text(encoding="utf-8")
    delta = pd.parse_pm_review_delta(text + _disposition("X-001"))
    assert [finding.id for finding, _row in delta.accepted] == ["X-001"]

    _wire(external, monkeypatch, tmp_path,
          _reject_reply(_finding("X-004")), conf=rounds)
    assert _run(external, tmp_path, "--ticket", TICKET) == 1
    text = ticket_path.read_text(encoding="utf-8")
    assert len(_sections(pd, text, "external-reviewer")) == 3
    assert pd.verify_ticket_seals(text) == []
    delta = pd.parse_pm_review_delta(
        text + _disposition("X-001") + _disposition("X-004", ordinal=2)
    )
    assert sorted(finding.id for finding, _row in delta.accepted) == ["X-001", "X-004"]


def test_a_normal_and_a_refused_section_coexist_on_the_two_surfaces(
    external, pd, monkeypatch, tmp_path,
):
    """(b) 다중 절: 정상 절만 delta 에 남고 거부 절은 요약에 '회수 거부' 로 뜬다."""
    rounds = {"review_rounds_max": "9"}
    ticket_path = _seed_board(tmp_path)
    _wire(external, monkeypatch, tmp_path,
          _reject_reply(_finding("X-001")), conf=rounds)
    assert _run(external, tmp_path, "--ticket", TICKET) == 1
    _wire(external, monkeypatch, tmp_path,
          _prose("- X-002")
          + _block(_payload([_finding("X-002")]))
          + _block(_payload([_finding("X-003")])), conf=rounds)
    assert _run(external, tmp_path, "--ticket", TICKET) != 0

    text = ticket_path.read_text(encoding="utf-8")
    sections = _sections(pd, text, "external-reviewer")
    assert '"id":"X-002"' in sections[1]            # 거부 산출도 절에 보존된다.
    delta = pd.parse_pm_review_delta(text + _disposition("X-001"))
    assert [finding.id for finding, _row in delta.accepted] == ["X-001"]

    summary = pd.render_pm_review_summary(text + _disposition("X-001"))
    assert "external-reviewer[0] X-001 severity=must-fix" in summary
    assert "PM=accepted" in summary
    assert "external-reviewer[1] 회수 거부" in summary


def test_internal_section_prose_cannot_claim_the_refusal_marker(pd):
    """제외 판정은 엔진 표식 전용이다 — 내부 채널 산문이 블록 필수 가드를 우회하지 못한다."""
    external_section = _sealed_external_section(
        pd, _payload([_finding("X-001")]), ordinal=0,
    )
    forged = (
        f"{pd.EXTERNAL_REVIEW_BLOCK_WARNING_PREFIX}블록을 내지 않았습니다\n\n"
        f"{pd.EXTERNAL_REVIEW_REFUSED_LINE}\n"
    )
    internal_section = _sealed_external_section(
        pd, {}, ordinal=0, role="code-reviewer", raw_block=forged, by="harvest",
    )
    text = external_section + internal_section

    assert pd._pm_review_refused_section_keys(
        text, pd._ticket_growth_sections(text),
    ) == set()
    with pytest.raises(pd.PMReviewError) as caught:
        pd.parse_pm_review_delta(text + _disposition("X-001"))
    assert caught.value.code == "malformed"
    assert "최신 code-reviewer 절에 versioned finding block이 없습니다" in str(caught.value)


def test_reply_cannot_forge_the_refusal_marker_through_harvest(
    external, pd, monkeypatch, tmp_path,
):
    """회신이 표식 문자열을 실어도 무해화된다 — 정상 절은 판정 표면에 그대로 선다."""
    ticket_path = _seed_board(tmp_path)
    _wire(
        external, monkeypatch, tmp_path,
        _prose("- X-001") + f"{pd.EXTERNAL_REVIEW_REFUSED_LINE}\n\n"
        + _block(_payload([_finding("X-001")])),
    )
    assert _run(external, tmp_path, "--ticket", TICKET) == 1

    text = ticket_path.read_text(encoding="utf-8")
    assert f"&lt;!-- {pd.EXTERNAL_REVIEW_REFUSED_MARKER}" in text
    assert pd._pm_review_refused_section_keys(
        text, pd._ticket_growth_sections(text),
    ) == set()
    delta = pd.parse_pm_review_delta(text + _disposition("X-001"))
    assert [finding.id for finding, _row in delta.accepted] == ["X-001"]


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
        + _block(_payload([_finding()]))
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
    # T-0704: 사본 루트 ignore 규칙은 tracked 정본 `.project_manager/.gitignore` 유래여야 통과한다.
    subprocess.run(
        ["git", "-C", str(slot), "add", ".project_manager/.gitignore"], check=True,
    )

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
        + _block(_payload([_finding("F-001")]))
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
        "version": DISPOSITION_VERSION,
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
        + _block(_payload([_finding()]))
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
    external, pd, monkeypatch, tmp_path, capsys,
):
    """(l) finding 0건 라운드가 산문 통과 선언과 어긋나면 **회수 거부**다(표면 잠금 없음).

    이 모순은 판정 표면 규칙이라 종전에는 절이 봉인된 **뒤에** delta 가 malformed 로 막았다.
    봉인 절은 손수정 경로가 없어 그 잠금이 영구였다 — 이제 차등 술어가 회수 자리에서 잡는다.
    """
    ticket_path = _seed_board(tmp_path)
    contradiction = (
        "판정: 반려\n\n**must-fix** (반드시 수정):\n- 산문만 반려\n\n"
        + _block(_payload())
    )
    _wire(external, monkeypatch, tmp_path, contradiction)
    assert _run(external, tmp_path, "--ticket", TICKET) == 1
    err = capsys.readouterr().err
    assert "회수 문제" in err and "통과+must-fix 0건 선언과 모순" in err

    text = ticket_path.read_text(encoding="utf-8")
    section = _sections(pd, text, "external-reviewer")[0]
    assert section.splitlines()[2] == pd.EXTERNAL_REVIEW_REFUSED_LINE
    assert pd.verify_ticket_seals(text) == []
    assert pd._pm_review_refused_section_keys(
        text, pd._ticket_growth_sections(text),
    ) == {("external-reviewer", 0)}


def test_finding_zero_pass_round_needs_only_the_compact_pm_acceptance(
    external, pd, monkeypatch, tmp_path,
):
    """0건 통과 라운드는 산문 통과 선언 + finding-zero 판정으로 닫힌다."""
    ticket_path = _seed_board(tmp_path)
    passing = (
        "판정: 통과\n\n**must-fix** (반드시 수정):\n- 없음\n\n"
        + _block(_payload())
    )
    _wire(external, monkeypatch, tmp_path, passing)
    assert _run(external, tmp_path, "--ticket", TICKET) == 0

    text = ticket_path.read_text(encoding="utf-8")
    zero = json.dumps({
        "version": DISPOSITION_VERSION,
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
    # 첫 라운드는 확인할 finding 이 없다 — 골격의 confirmations 도 실값(빈 목록)이다.
    assert pd.render_pm_review_block_skeleton("external-reviewer", []) in prompt
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


# ── F-002 세대 승격: 봉인된 v1 블록(실 보드 형상)을 legacy 로 계속 읽는다 ──────


LEGACY_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "legacy_review_blocks_v1.json"


def _legacy_fixture_tickets() -> list[dict]:
    return json.loads(LEGACY_FIXTURE.read_text(encoding="utf-8"))["tickets"]


def _rebuild_legacy_ticket(pd, entry: dict) -> str:
    """픽스처(실 보드 v1 블록 형상)를 봉인된 티켓 본문으로 되살린다(값은 축약본)."""
    parts = [f"---\nid: {entry['ticket']}\nstatus: claimed\n---\n# {entry['ticket']}\n"]
    for section in entry["sections"]:
        role = section["role"]
        content = (
            f"## 리뷰 ({role} · 2026-08-17)\n\n"
            f"{section['verdict_prose']}\n\n## must-fix\n- 구조화 finding 참조\n\n"
            + _block(section["payload"])
        )
        digest = pd.seal_for(content)
        parts.append(
            f"\n<!-- pm-ticket-section:start role={role} -->\n" + content
            + f"<!-- pm-ticket-section:end role={role} -->\n"
            + f"<!-- pm-ticket-seal role={role} ordinal={section['ordinal']} "
            f"sha256={digest} by=backfill -->\n"
        )
    for disposition in entry["dispositions"]:
        parts.append(
            "\n```pm-review-disposition-v1\n"
            + json.dumps(disposition, ensure_ascii=False, separators=(",", ":"))
            + "\n```\n"
        )
    return "".join(parts)


@pytest.mark.parametrize(
    "entry", _legacy_fixture_tickets(), ids=lambda entry: entry["ticket"],
)
def test_sealed_v1_blocks_from_the_live_board_stay_readable(pd, entry):
    """진행 중 티켓의 봉인된 v1 블록은 severity 없이도 delta 를 낸다(자산을 잠그지 않는다).

    픽스처는 실 보드 7건(T-0691·T-0693·T-0701·T-0703·T-0704·T-0705·T-0735)의 블록 형상을 값만
    축약해 굳힌 것이다 — 그 파일들은 봉인돼 손수정 경로가 없으므로([[ADR-0089]]) 파서가 계속
    읽어야 fix 루프가 돌아간다.
    """
    text = _rebuild_legacy_ticket(pd, entry)
    assert pd.verify_ticket_seals(text) == []
    assert all(
        section["payload"]["version"] == LEGACY_BLOCK_VERSION
        for section in entry["sections"]
    )
    assert not any(
        "severity" in finding
        for section in entry["sections"]
        for finding in section["payload"]["findings"]
    )

    delta = pd.parse_pm_review_delta(text)          # malformed 면 fix 루프가 막힌다.
    rendered = pd.render_pm_review_delta(entry["ticket"], delta)
    if delta.accepted:
        assert f"- 심각도: {pd.PM_REVIEW_SEVERITY_UNSPECIFIED_LABEL}" in rendered
    summary_rows = [
        row for row in pd.render_pm_review_summary(text).splitlines()
        if "severity=" in row
    ]
    assert summary_rows, "구 블록 티켓의 요약이 통째로 접혔다"
    assert all(
        f"severity={pd.PM_REVIEW_SEVERITY_UNSPECIFIED_LABEL}" in row
        for row in summary_rows
    )


def test_current_generation_block_still_requires_severity(pd):
    """세대 경계는 블록 payload 의 `version` 이다 — 현행 세대는 부재를 계속 거부한다."""
    ticket = _sealed_external_section(
        pd, _payload([_finding(severity=None)]),
    )
    with pytest.raises(pd.PMReviewError, match=r"missing=\['severity'\]") as caught:
        pd.parse_pm_review_delta(ticket)
    assert caught.value.code == "malformed"


def test_engine_written_skeleton_and_prompt_use_the_current_generation(pd):
    """엔진이 새로 시드·요구하는 블록은 현행 세대다(구 세대는 읽기 전용 수용)."""
    for role in pd.REVIEW_ROLES:
        skeleton = json.loads(
            pd.render_pm_review_block_skeleton(role)
            .split("```pm-review-v1\n", 1)[1].split("\n```", 1)[0]
        )
        assert skeleton["version"] == BLOCK_VERSION
        assert "severity" in skeleton["findings"][0]
    assert LEGACY_BLOCK_VERSION < BLOCK_VERSION
    assert set(pd.PM_REVIEW_SUPPORTED_VERSIONS) == {
        LEGACY_BLOCK_VERSION, BLOCK_VERSION,
    }


# ── F-001 혼재 티켓: 절 단위 관용 요약 ──────────────────────────────────


def _sealed_external_section(pd, payload: dict, *, ordinal: int = 0,
                             role: str = "external-reviewer",
                             raw_block: str | None = None,
                             by: str = "external-review") -> str:
    content = (
        f"## 추가 리뷰 ({role} · 2026-08-17)\n\n판정: 반려\n\n"
        "## must-fix\n- 구조화 finding 참조\n\n"
        + (raw_block if raw_block is not None else _block(payload))
    )
    digest = pd.seal_for(content)
    return (
        f"<!-- pm-ticket-section:start role={role} -->\n" + content
        + f"<!-- pm-ticket-section:end role={role} -->\n"
        + f"<!-- pm-ticket-seal role={role} ordinal={ordinal} sha256={digest} "
        f"by={by} -->\n"
    )


def test_summary_folds_only_the_broken_section_of_a_mixed_ticket(pd):
    """(F-001) 구 세대·현행·손상 블록이 섞여도 나머지 절 요약은 살아 있다."""
    legacy = _sealed_external_section(
        pd, _payload([_finding("X-001", severity=None)],
                     version=LEGACY_BLOCK_VERSION), ordinal=0,
    )
    current = _sealed_external_section(
        pd, _payload([_finding("X-002", severity="should-fix")]), ordinal=1,
    )
    broken = _sealed_external_section(
        pd, {}, ordinal=2,
        raw_block=_block(_payload([_finding("X-003", classification="style")])),
    )
    summary = pd.render_pm_review_summary(legacy + current + broken)

    rows = summary.splitlines()
    assert f"external-reviewer[0] X-001 severity={pd.PM_REVIEW_SEVERITY_UNSPECIFIED_LABEL}" \
        in "\n".join(rows)
    assert "external-reviewer[1] X-002 severity=should-fix" in "\n".join(rows)
    folded = [row for row in rows if "요약 불가" in row]
    assert len(folded) == 1 and "external-reviewer[2]" in folded[0]
    assert "class 미지원" in folded[0]


def test_summary_folds_a_broken_json_block_into_its_own_section_row(pd):
    """JSON 자체가 깨진 블록도 그 절 한 줄로 접힌다(티켓 전체 요약을 버리지 않는다).

    엔진이 스스로 만드는 형상이다 — 회수 거부된 산출이 평문으로 절에 보존되기 때문이다.
    """
    healthy = _sealed_external_section(
        pd, _payload([_finding("X-001")]), ordinal=0,
    )
    corrupt = _sealed_external_section(
        pd, {}, ordinal=1,
        raw_block='```pm-review-v1\n{"version":2,"findings":[\n```\n',
    )
    summary = pd.render_pm_review_summary(
        healthy + corrupt + _disposition("X-001")
    )

    rows = summary.splitlines()
    assert any(
        "external-reviewer[0] X-001 severity=must-fix" in row and "PM=accepted" in row
        for row in rows
    )
    folded = [row for row in rows if "요약 불가" in row]
    assert len(folded) == 1
    assert "external-reviewer[1]" in folded[0] and "JSON 파싱 실패" in folded[0]


# ── F-003 ticket 형상 게이트 회수 ───────────────────────────────────────


def test_ticket_shaped_gate_without_ticket_flag_is_harvested(
    external, pd, monkeypatch, tmp_path,
):
    """문서화된 `--paths … --gate T-NNNN` 설계 리뷰 형상도 같은 회수 규칙을 탄다."""
    ticket_path = _seed_board(tmp_path)
    _wire(external, monkeypatch, tmp_path, _reject_reply(_finding()))

    assert _run(external, tmp_path, "--gate", TICKET, "--paths", "x.py") == 1
    text = ticket_path.read_text(encoding="utf-8")
    assert len(_sections(pd, text, "external-reviewer")) == 1
    assert pd.verify_ticket_seals(text) == []
    delta = pd.parse_pm_review_delta(text + _disposition("X-001"))
    assert [finding.id for finding, _row in delta.accepted] == ["X-001"]


def test_ticket_shaped_gate_missing_from_the_board_says_why(
    external, monkeypatch, tmp_path, capsys,
):
    """보드에 없는 ticket 형상 게이트는 조용히 지나가지 않는다 — 사유를 말한다."""
    _seed_board(tmp_path)
    _wire(external, monkeypatch, tmp_path, _reject_reply(_finding()))

    assert _run(external, tmp_path, "--gate", "T-9999", "--paths", "x.py") == 1
    err = capsys.readouterr().err
    assert "미회수" in err and "T-9999" in err


def test_reviewer_failure_reports_that_nothing_was_harvested(
    external, monkeypatch, tmp_path, capsys,
):
    """회신이 없어 회수하지 않은 실행도 사유를 남긴다(조용한 누락 금지)."""
    ticket_path = _seed_board(tmp_path)
    before = ticket_path.read_text(encoding="utf-8")
    _wire(external, monkeypatch, tmp_path, "")
    monkeypatch.setattr(external, "run_review", lambda *a, **k: {
        "reviewer": "fixture", "ok": False, "output": "", "answer": "",
        "verdict": {"has_must_fix": False, "has_pass": False},
        "file": tmp_path / "raw" / "fixture.md", "failed": True, "started": True,
        "any_must_fix": False, "all_pass": False,
    })

    assert _run(external, tmp_path, "--ticket", TICKET) == 1
    err = capsys.readouterr().err
    assert "미회수" in err and "회신을 받지 못했습니다" in err
    assert ticket_path.read_text(encoding="utf-8") == before


# ── F-004 finding ID 네임스페이스: 실값 프롬프트 + 재선언 거부 ──────────


def test_prompt_carries_the_next_finding_id_from_the_harvest_target(
    external, pd, monkeypatch, tmp_path,
):
    """엔진이 티켓의 기존 최대 번호를 읽어 이번 라운드의 시작 ID 를 실값으로 싣는다."""
    _seed_board(tmp_path)
    calls = _wire(external, monkeypatch, tmp_path, _reject_reply(_finding()))
    assert _run(external, tmp_path, "--ticket", TICKET) == 1
    assert "`X-001` 부터" in calls["prompt"]

    calls = _wire(external, monkeypatch, tmp_path, _reject_reply(_finding("X-002")))
    assert _run(external, tmp_path, "--ticket", TICKET) == 1
    assert "`X-002` 부터" in calls["prompt"]        # 티켓의 기존 최대 번호 + 1

    calls = _wire(external, monkeypatch, tmp_path, _confirm_reply({
        "id": "X-002", "status": "resolved", "evidence": "회귀 rc=0",
    }))
    assert _run(external, tmp_path, "--ticket", TICKET, "--confirm-fix") == 0
    assert "`X-003` 부터" in calls["prompt"]
    assert pd.next_review_finding_id("본문에 X-007 과 X-002 가 있다", "external-reviewer") \
        == "X-008"
    assert pd.next_review_finding_id("빈 티켓", "external-reviewer") == "X-001"


def _prompt_next_id(prompt: str) -> str:
    """프롬프트가 지시한 이번 라운드의 시작 ID(리뷰어가 읽는 그 값)."""
    match = re.search(r"`(X-\d{3})` 부터", prompt)
    assert match is not None, prompt
    return match.group(1)


def test_gate_shaped_run_advances_the_finding_id_across_rounds(
    external, pd, monkeypatch, tmp_path,
):
    """본문이 실리지 않는 `--paths … --gate T-NNNN` 형상도 라운드마다 ID 가 전진한다.

    출처가 프롬프트용 티켓 본문이면 이 형상은 매 라운드 첫 ID 를 지시해 2라운드가 재선언으로
    거부된다 — 출처는 회수 대상 티켓 파일이다.
    """
    rounds = {"review_rounds_max": "9"}
    ticket_path = _seed_board(tmp_path)
    seen: list[str] = []

    def _reply_following_the_prompt(prompt: str) -> str:
        seen.append(_prompt_next_id(prompt))
        return _reject_reply(_finding(seen[-1]))

    for _round in range(3):
        _wire(external, monkeypatch, tmp_path,
              _reply_following_the_prompt, conf=rounds)
        assert _run(
            external, tmp_path, "--paths", "x.py", "--gate", TICKET,
        ) == 1

    assert seen == ["X-001", "X-002", "X-003"]
    text = ticket_path.read_text(encoding="utf-8")
    assert len(_sections(pd, text, "external-reviewer")) == 3
    assert pd.verify_ticket_seals(text) == []
    delta = pd.parse_pm_review_delta(
        text + _disposition("X-001") + _disposition("X-002", ordinal=1)
        + _disposition("X-003", ordinal=2)
    )
    assert sorted(finding.id for finding, _row in delta.accepted) == [
        "X-001", "X-002", "X-003",
    ]


def test_reused_finding_id_is_refused_and_the_next_round_can_land(
    external, pd, monkeypatch, tmp_path, capsys,
):
    """(F-004) 같은 ID 재선언은 판정 표면에 올리지 않는다 — 티켓은 계속 판정 가능하다."""
    # 이 케이스는 3라운드(초기·재선언·재시도)를 태운다 — 수렴 상한 축은 별도 테스트가 소유한다.
    rounds = {"review_rounds_max": "9"}
    ticket_path = _seed_board(tmp_path)
    _wire(external, monkeypatch, tmp_path, _reject_reply(_finding("X-001")), conf=rounds)
    assert _run(external, tmp_path, "--ticket", TICKET) == 1

    _wire(external, monkeypatch, tmp_path, _reject_reply(_finding("X-001")), conf=rounds)
    assert _run(external, tmp_path, "--ticket", TICKET) != 0
    err = capsys.readouterr().err
    assert "회수 문제" in err and "재선언" in err

    text = ticket_path.read_text(encoding="utf-8")
    sections = _sections(pd, text, "external-reviewer")
    assert len(sections) == 2                       # 산출은 절로 남는다(증거 보존).
    assert pd.EXTERNAL_REVIEW_BLOCK_WARNING_PREFIX in sections[1]
    assert "```pm-review-v1" not in sections[1]     # 판정 표면에는 올리지 않는다.
    assert pd.verify_ticket_seals(text) == []
    delta = pd.parse_pm_review_delta(text + _disposition("X-001"))
    assert [finding.id for finding, _row in delta.accepted] == ["X-001"]

    # 재시도 라운드가 새 ID 로 정상 착지한다.
    _wire(external, monkeypatch, tmp_path, _reject_reply(_finding("X-002")), conf=rounds)
    assert _run(external, tmp_path, "--ticket", TICKET) == 1
    text = ticket_path.read_text(encoding="utf-8")
    delta = pd.parse_pm_review_delta(
        text + _disposition("X-001") + _disposition("X-002", ordinal=2)
    )
    assert sorted(finding.id for finding, _row in delta.accepted) == ["X-001", "X-002"]


def test_confirmation_round_may_reference_existing_ids(
    external, pd, monkeypatch, tmp_path,
):
    """확인 라운드는 기존 ID 를 참조해야 한다 — 재선언 판정이 그것까지 막지 않는다."""
    ticket_path = _seed_board(tmp_path)
    _wire(external, monkeypatch, tmp_path, _reject_reply(_finding("X-001")))
    assert _run(external, tmp_path, "--ticket", TICKET) == 1

    _wire(external, monkeypatch, tmp_path, _confirm_reply({
        "id": "X-001", "status": "resolved", "evidence": "회귀 rc=0",
    }))
    assert _run(external, tmp_path, "--ticket", TICKET, "--confirm-fix") == 0
    text = ticket_path.read_text(encoding="utf-8")
    assert pd.EXTERNAL_REVIEW_BLOCK_WARNING_PREFIX not in text
    delta = pd.parse_pm_review_delta(text + _disposition("X-001"))
    assert delta.accepted == ()


# ── F-016 회수 게이트 시야 == 판정 표면(confirmation 대상 대조) ──────────


def _confirming_reject_reply(finding: dict, *confirmations: dict) -> str:
    """신규 finding 과 confirmations 를 함께 담은 반려 회신(확인 라운드 형상)."""
    return (
        _prose(f"- {finding['id']}")
        + _block(_payload([finding], list(confirmations)))
    )


def test_confirmation_of_a_refused_round_is_refused_and_the_next_round_can_land(
    external, pd, monkeypatch, tmp_path, capsys,
):
    """(F-016) 거부된 라운드의 ID 를 확인해도 회수는 거부되고 다음 라운드가 착지한다.

    거부 절의 finding 은 판정 표면에 없다. 그 ID 를 confirmations 로 실은 블록을 봉인하면
    `parse_pm_review_delta` 가 '선행 finding 미참조' 로 그 티켓을 영구 malformed 로 잠근다 —
    회수 게이트가 판정 표면과 같은 시야로 그 자리에서 거부한다.
    """
    rounds = {"review_rounds_max": "9"}
    ticket_path = _seed_board(tmp_path)
    _wire(external, monkeypatch, tmp_path, _BROKEN_JSON_REPLY, conf=rounds)
    assert _run(external, tmp_path, "--ticket", TICKET) != 0     # 라운드1 회수 거부

    _wire(external, monkeypatch, tmp_path, _confirming_reject_reply(
        _finding("X-002"),
        {"id": "X-001", "status": "resolved", "evidence": "거부 라운드의 ID"},
    ), conf=rounds)
    assert _run(external, tmp_path, "--ticket", TICKET) != 0
    err = capsys.readouterr().err
    assert "회수 문제" in err and "confirmation 대상 finding 부재: X-001" in err

    text = ticket_path.read_text(encoding="utf-8")
    sections = _sections(pd, text, "external-reviewer")
    assert len(sections) == 2
    assert '"id":"X-002"' in sections[1]              # 산출은 평문으로 보존된다.
    assert re.search(r"^`{3,}pm-review", sections[1], re.M) is None
    assert pd.verify_ticket_seals(text) == []
    assert pd._pm_review_refused_section_keys(
        text, pd._ticket_growth_sections(text),
    ) == {("external-reviewer", 0), ("external-reviewer", 1)}

    # 라운드3 이 정상 착지하고 delta 가 green 이다(거부 라운드가 티켓을 잠그지 않는다).
    _wire(external, monkeypatch, tmp_path,
          _reject_reply(_finding("X-003")), conf=rounds)
    assert _run(external, tmp_path, "--ticket", TICKET) == 1
    text = ticket_path.read_text(encoding="utf-8")
    delta = pd.parse_pm_review_delta(text + _disposition("X-003", ordinal=2))
    assert [finding.id for finding, _row in delta.accepted] == ["X-003"]


def test_hallucinated_confirmation_target_is_refused_the_same_way(
    external, pd, monkeypatch, tmp_path, capsys,
):
    """(F-016) 티켓에 없는 ID(하네스 환각)를 확인해도 같은 거부 경로다."""
    rounds = {"review_rounds_max": "9"}
    ticket_path = _seed_board(tmp_path)
    _wire(external, monkeypatch, tmp_path,
          _reject_reply(_finding("X-001")), conf=rounds)
    assert _run(external, tmp_path, "--ticket", TICKET) == 1

    _wire(external, monkeypatch, tmp_path, _confirming_reject_reply(
        _finding("X-002"),
        {"id": "X-099", "status": "resolved", "evidence": "존재하지 않는 ID"},
    ), conf=rounds)
    assert _run(external, tmp_path, "--ticket", TICKET) != 0
    assert "confirmation 대상 finding 부재: X-099" in capsys.readouterr().err

    # 정상 절(라운드1)은 그대로 판정 대상이고 delta 는 계속 읽힌다.
    text = ticket_path.read_text(encoding="utf-8")
    delta = pd.parse_pm_review_delta(text + _disposition("X-001"))
    assert [finding.id for finding, _row in delta.accepted] == ["X-001"]


def test_declarations_count_only_the_surface_sections(pd):
    """(F-016) 표면 선언 집합은 거부 절·산문 인용을 세지 않는다(재사용 방지 스캔과 시야가 다르다)."""
    normal = _sealed_external_section(pd, _payload([_finding("X-001")]), ordinal=0)
    refused = _sealed_external_section(
        pd, {}, ordinal=1,
        raw_block=(
            f"{pd.EXTERNAL_REVIEW_REFUSED_LINE}\n\n"
            f"{pd.EXTERNAL_REVIEW_BLOCK_WARNING_PREFIX}JSON 파싱 실패\n\n"
            "```text\n" + json.dumps(_payload([_finding("X-002")]),
                                     ensure_ascii=False) + "\n```\n"
        ),
    )
    text = normal + refused + "\n산문에서 X-050 을 인용한다.\n"

    assert pd.collect_review_finding_declarations(text, "external-reviewer") == {"X-001"}
    # 재사용 방지 스캔은 거부 절·산문까지 넓게 본다 — 두 시야는 의도적으로 다르다.
    assert {"X-001", "X-002", "X-050"} <= pd.collect_review_finding_ids(
        text, "external-reviewer",
    )
    assert pd.next_review_finding_id(text, "external-reviewer") == "X-051"


def test_confirm_fix_after_a_refused_round_is_refused_before_any_spawn(
    external, pd, monkeypatch, tmp_path, capsys,
):
    """(F-016) 회수 거부된 라운드는 확인 전용 라운드의 근거가 되지 않는다(전송 0).

    엔진이 그 라운드의 must-fix 를 근거로 실으면 출력 규칙이 '그 X- ID 를 confirmations 에
    실어라'고 지시해, 확인 라운드가 회수 게이트에 다시 걸린다 — 엔진이 스스로 함정을 지시한다.
    """
    ticket_path = _seed_board(tmp_path)
    _wire(external, monkeypatch, tmp_path, _BROKEN_JSON_REPLY)
    assert _run(external, tmp_path, "--ticket", TICKET) != 0
    capsys.readouterr()

    calls = _wire(external, monkeypatch, tmp_path, _confirm_reply({
        "id": "X-001", "status": "resolved", "evidence": "거부 라운드의 ID",
    }))
    assert _run(external, tmp_path, "--ticket", TICKET, "--confirm-fix") == \
        external.EXIT_ROUND_LIMIT_EXCEEDED
    assert calls["n"] == 0                       # 외부 전송·과금 0
    err = capsys.readouterr().err
    assert "판정 표면에 없습니다" in err and "X-001" in err
    assert "일반 라운드" in err
    # 절은 하나(거부 라운드)뿐이고 티켓은 그대로다.
    assert len(_sections(pd, ticket_path.read_text(encoding="utf-8"),
                         "external-reviewer")) == 1


def test_confirm_fix_evidence_keeps_items_that_are_on_the_surface(external):
    """표면에 있는 지적은 근거에 그대로 남는다(대조가 정상 확인 라운드를 막지 않는다)."""
    entry = {
        "rounds": [{"id": "r1", "verdict": 1, "must_fix": 1, "sequence": 1}],
        "records": [{"id": "r1", "verdict": 1, "must_fix_items": ["X-001 미해소"]}],
    }
    assert external._confirm_fix_evidence(
        entry, surface_finding_ids={"X-001"},
    ) is not None
    assert external._confirm_fix_evidence(
        entry, surface_finding_ids=set(),
    ) is None
    assert external._confirm_fix_offsurface_ids(entry, set()) == ["X-001"]
    # 대조 입력이 없는 실행(회수 대상 없음)은 종전 그대로다.
    assert external._confirm_fix_evidence(entry) is not None


# ── F-020 차등 술어: 표면을 malformed 로 바꾸는 절은 사유 불문 거부 ──────


@pytest.mark.parametrize(
    "second_reply,reason,pm_rejected",
    [
        pytest.param(
            _prose("- X-002") + _block(_payload()),
            "통과+must-fix 0건 선언과 모순",
            False,
            id="prose-reject-with-zero-findings",
        ),
        pytest.param(
            _confirm_reply({
                "id": "X-001", "status": "resolved", "evidence": "PM 이 반려한 지적",
            }),
            "rejected finding ID가 확인 라운드에 재등장",
            True,
            id="confirmation-of-a-pm-rejected-finding",
        ),
    ],
)
def test_a_round_that_would_lock_the_delta_surface_is_refused(
    external, pd, monkeypatch, tmp_path, capsys, second_reply, reason, pm_rejected,
):
    """(F-020) 표면을 malformed 로 바꾸는 라운드는 사유 불문 회수 자리에서 접힌다.

    두 사유 모두 종전에는 절이 봉인된 **뒤** delta 에서만 막혀(손수정 경로 없음) 티켓이 영구
    잠겼다. 라운드3 이 정상 착지하는지까지 본다.
    """
    rounds = {"review_rounds_max": "9"}
    ticket_path = _seed_board(tmp_path)
    _wire(external, monkeypatch, tmp_path,
          _reject_reply(_finding("X-001")), conf=rounds)
    assert _run(external, tmp_path, "--ticket", TICKET) == 1
    if pm_rejected:
        # PM 이 X-001 을 rejected 로 판정한 티켓 — 확인 라운드가 그 ID 를 다시 들면 표면이 막는다.
        ticket_path.write_text(
            ticket_path.read_text(encoding="utf-8")
            + _disposition("X-001", decision="rejected"),
            encoding="utf-8", newline="",
        )

    _wire(external, monkeypatch, tmp_path, second_reply, conf=rounds)
    assert _run(external, tmp_path, "--ticket", TICKET) != 0
    err = capsys.readouterr().err
    assert "회수 문제" in err and "판정 표면 malformed 유발" in err and reason in err

    text = ticket_path.read_text(encoding="utf-8")
    sections = _sections(pd, text, "external-reviewer")
    assert len(sections) == 2                       # 산출은 절에 평문으로 남는다.
    assert re.search(r"^`{3,}pm-review", sections[1], re.M) is None
    assert pd.verify_ticket_seals(text) == []
    assert pd._pm_review_refused_section_keys(
        text, pd._ticket_growth_sections(text),
    ) == {("external-reviewer", 1)}

    # 라운드3 이 정상 착지하고 delta 는 green 이다(잠금 없음).
    _wire(external, monkeypatch, tmp_path,
          _reject_reply(_finding("X-003")), conf=rounds)
    assert _run(external, tmp_path, "--ticket", TICKET) == 1
    text = ticket_path.read_text(encoding="utf-8")
    if not pm_rejected:
        text += _disposition("X-001")
    delta = pd.parse_pm_review_delta(text + _disposition("X-003", ordinal=2))
    assert [finding.id for finding, _row in delta.accepted] == (
        ["X-003"] if pm_rejected else ["X-001", "X-003"]
    )


def test_the_gate_reads_the_surface_parser_not_a_copied_rule_list(
    external, pd, monkeypatch, tmp_path,
):
    """(F-020) 게이트 시야 == 표면 — 규칙 목록을 옮겨 적지 않고 표면 파서를 그대로 태운다.

    시야가 같은 함수 하나라 표면에 malformed 규칙이 늘어도 게이트가 자동으로 따라온다. 옮겨 적으면
    규칙이 늘 때마다 두 시야가 조용히 갈린다(그 갈림이 이 티켓에서 두 번 재현됐다).
    """
    gate_source = inspect.getsource(pd._pm_review_delta_regression_reason)
    reason_source = inspect.getsource(pd._pm_review_delta_malformed_reason)
    assert "_pm_review_delta_malformed_reason(" in gate_source
    assert "parse_pm_review_delta(" in reason_source
    # 기준선도 같은 파서로 잰다 — 절 개수·사유 문자열 같은 우회 판정이 없다.
    assert gate_source.count("_pm_review_delta_malformed_reason(") == 2
    # 예정 본문 조립도 실제 쓰기와 같은 helper 다(게이트가 다른 문자열을 판정하지 않는다).
    assert "_append_ticket_growth_section(" in gate_source
    assert "_append_ticket_growth_section(" in inspect.getsource(
        pd.write_external_reviewer_section,
    )

    # 표면이 malformed 라고 말하는 산출은 사유 문자열을 몰라도 그대로 접힌다.
    base = _seed_board(tmp_path).read_text(encoding="utf-8")
    content, problem = pd.build_external_review_section_content(
        _prose("- X-002") + _block(_payload()),
        today="2026-08-18", ticket_text=base + _sealed_external_section(
            pd, _payload([_finding("X-001")]), ordinal=0,
        ),
    )
    assert problem is not None and "판정 표면 malformed 유발" in problem
    assert pd.EXTERNAL_REVIEW_REFUSED_LINE in content


def test_the_baseline_probe_section_is_known_good(pd):
    """(F-024) 기준선 프로브 자체가 정상이어야 이 판정이 fail-open 으로 꺼지지 않는다."""
    probe = pd._pm_review_probe_section_content("", "external-reviewer")
    baseline = pd._append_ticket_growth_section("", probe, "external-reviewer")
    assert pd._pm_review_delta_malformed_reason(baseline) is None
    # 프로브 ID 는 티켓의 다음 실값이라 재선언·충돌로 걸리지 않는다.
    landed = _sealed_external_section(pd, _payload([_finding("X-001")]), ordinal=0)
    assert '"id":"X-002"' in pd._pm_review_probe_section_content(
        landed, "external-reviewer",
    )
    # 채널 중립 — 내부 회수도 자기 접두로 같은 프로브를 만든다.
    assert '"id":"F-001"' in pd._pm_review_probe_section_content("", "code-reviewer")


def test_a_broken_baseline_probe_fails_loud_instead_of_opening_the_gate(
    external, pd, monkeypatch, tmp_path, capsys,
):
    """(F-024) 프로브가 깨지면 기준선이 항상 dirty 라 게이트가 조용히 꺼진다 — fail-loud 로 멈춘다."""
    ticket_path = _seed_board(tmp_path)
    before = ticket_path.read_text(encoding="utf-8")
    # 프로브가 채우는 자리표시가 비면 표면 파서가 그 절을 malformed 로 본다(엔진 결함 형상).
    monkeypatch.setattr(pd, "_PM_REVIEW_PROBE_TEXT", "")
    with pytest.raises(pd.PMReviewError, match="판정 프로브 손상"):
        pd.build_external_review_section_content(
            _reject_reply(_finding("X-001")), today="2026-08-18", ticket_text=before,
        )

    _wire(external, monkeypatch, tmp_path, _reject_reply(_finding("X-001")))
    monkeypatch.setattr(external, "_load_pm_delegate", lambda: pd)
    assert _run(external, tmp_path, "--ticket", TICKET) != 0
    err = capsys.readouterr().err
    assert "회수 문제" in err and "판정 프로브 손상" in err
    assert ticket_path.read_text(encoding="utf-8") == before   # 절을 쓰지 않는다.


@pytest.mark.parametrize("existing_defect", ["stray-fence", "disposition-of-a-refused-id"])
def test_a_pre_existing_defect_does_not_get_blamed_on_the_new_round(
    external, pd, monkeypatch, tmp_path, capsys, existing_defect,
):
    """(F-024) 리뷰 절과 무관한 기존 malformed 사유가 있어도 새 라운드는 착지한다.

    기준선을 "거부되지 않은 리뷰 절 0개 = 결함 없음" 으로 세면 그 사유가 이 라운드 탓이 되어
    이후 모든 라운드가 거부된다(봉인이라 복구 경로 없음). 반사실 프로브는 같은 파서로 기존
    결함을 먼저 재고 침묵한다.
    """
    rounds = {"review_rounds_max": "9"}
    if existing_defect == "stray-fence":
        # 역할 절 밖 스키마 예시 인용 — 표면 파서는 이걸 malformed 로 본다.
        ticket_path = _seed_board(
            tmp_path, body="\n## 참고\n" + _block(_payload([_finding("X-050")])),
        )
    else:
        ticket_path = _seed_board(tmp_path)
        _wire(external, monkeypatch, tmp_path, _BROKEN_JSON_REPLY, conf=rounds)
        assert _run(external, tmp_path, "--ticket", TICKET) != 0
        # 거부 절 평문에 남은 X-001 을 PM 이 finding 으로 읽고 판정을 써 버린 티켓.
        ticket_path.write_text(
            ticket_path.read_text(encoding="utf-8") + _disposition("X-001"),
            encoding="utf-8", newline="",
        )
    before = pd._pm_review_delta_malformed_reason(
        ticket_path.read_text(encoding="utf-8")
    )
    assert before is not None                      # 기존 결함이 있는 티켓이다.
    capsys.readouterr()

    _wire(external, monkeypatch, tmp_path,
          _reject_reply(_finding("X-100")), conf=rounds)
    assert _run(external, tmp_path, "--ticket", TICKET) == 1
    err = capsys.readouterr().err
    assert "회수 문제" not in err

    text = ticket_path.read_text(encoding="utf-8")
    landed = _sections(pd, text, "external-reviewer")[-1]
    assert pd.EXTERNAL_REVIEW_REFUSED_LINE not in landed
    assert '"id":"X-100"' in landed
    assert pd.verify_ticket_seals(text) == []
    assert ("external-reviewer", len(_sections(pd, text, "external-reviewer")) - 1) \
        not in pd._pm_review_refused_section_keys(text, pd._ticket_growth_sections(text))


def test_an_already_malformed_ticket_does_not_block_new_rounds(pd, tmp_path):
    """(F-020) 차등이라 이미 malformed 인 티켓이 다음 라운드를 영구 거부하지 않는다."""
    broken = _sealed_external_section(
        pd, {}, ordinal=0,
        raw_block=_block(_payload([_finding("X-001", classification="style")])),
    )
    assert pd._pm_review_delta_malformed_reason(broken) is not None
    _content, problem = pd.build_external_review_section_content(
        _reject_reply(_finding("X-002")), today="2026-08-18", ticket_text=broken,
    )
    assert problem is None                       # 이 라운드가 원인이 아니다.


def test_normal_rounds_pass_the_differential_predicate(pd, tmp_path):
    """(F-020) 정상 라운드는 차등 술어를 통과한다 — pending·판정 완료 둘 다."""
    empty = "# T-9601\n\n## 목표\n프로브.\n"
    _content, problem = pd.build_external_review_section_content(
        _reject_reply(_finding("X-001")), today="2026-08-18", ticket_text=empty,
    )
    assert problem is None                       # 빈 티켓 → pending

    landed = empty + _sealed_external_section(
        pd, _payload([_finding("X-001")]), ordinal=0,
    )
    _content, problem = pd.build_external_review_section_content(
        _reject_reply(_finding("X-002")), today="2026-08-18", ticket_text=landed,
    )
    assert problem is None                       # pending → pending

    judged = landed + _disposition("X-001")
    _content, problem = pd.build_external_review_section_content(
        _confirm_reply({"id": "X-001", "status": "resolved", "evidence": "회귀 rc=0"}),
        today="2026-08-18", ticket_text=judged,
    )
    assert problem is None                       # OK → OK


def test_ticket_text_is_a_required_gate_input(pd):
    """(F-022) 대조 스냅샷은 필수 인자다 — 빠뜨린 호출이 조용히 전량 거부가 되지 않는다."""
    with pytest.raises(TypeError):
        pd.build_external_review_section_content(
            _reject_reply(_finding("X-001")), today="2026-08-18",
        )


def test_confirm_fix_after_a_pm_rejected_finding_is_refused_with_the_knob(
    external, pd, monkeypatch, tmp_path, capsys,
):
    """(F-020·F-023) PM 이 rejected 로 판정한 지적은 확인 근거가 아니다 — 처방에 노브까지 붙는다."""
    ticket_path = _seed_board(tmp_path)
    _wire(external, monkeypatch, tmp_path, _reject_reply(_finding("X-001")))
    assert _run(external, tmp_path, "--ticket", TICKET) == 1
    ticket_path.write_text(
        ticket_path.read_text(encoding="utf-8")
        + _disposition("X-001", decision="rejected"),
        encoding="utf-8", newline="",
    )
    capsys.readouterr()

    calls = _wire(external, monkeypatch, tmp_path, _confirm_reply({
        "id": "X-001", "status": "resolved", "evidence": "PM 이 반려한 지적",
    }))
    assert _run(external, tmp_path, "--ticket", TICKET, "--confirm-fix") == \
        external.EXIT_ROUND_LIMIT_EXCEEDED
    assert calls["n"] == 0                          # 외부 전송·과금 0
    err = capsys.readouterr().err
    assert "판정 표면에 없습니다" in err and "X-001" in err
    assert external.REVIEW_ROUNDS_MAX_KEY in err    # 수렴 상한에서 처방이 끊기지 않는다.


# ── F-021 프롬프트 골격: 확인 가능한 ID 실값 ───────────────────────────


def test_prompt_skeleton_carries_the_confirmable_ids(
    external, pd, monkeypatch, tmp_path,
):
    """(F-021) 골격의 confirmations 는 티켓에서 읽은 실값이다(거부 절·PM rejected 제외)."""
    rounds = {"review_rounds_max": "9"}
    ticket_path = _seed_board(tmp_path)
    calls = _wire(external, monkeypatch, tmp_path,
                  _reject_reply(_finding("X-001")), conf=rounds)
    assert _run(external, tmp_path, "--ticket", TICKET) == 1
    assert pd.render_pm_review_block_skeleton("external-reviewer", []) in calls["prompt"]

    calls = _wire(external, monkeypatch, tmp_path,
                  _reject_reply(_finding("X-002")), conf=rounds)
    assert _run(external, tmp_path, "--ticket", TICKET) == 1
    assert pd.render_pm_review_block_skeleton(
        "external-reviewer", ["X-001"],
    ) in calls["prompt"]

    # PM 이 rejected 로 판정한 ID 는 확인 대상에서 빠진다(리뷰 절 시드와 같은 배제).
    ticket_path.write_text(
        ticket_path.read_text(encoding="utf-8")
        + _disposition("X-001", decision="rejected"),
        encoding="utf-8", newline="",
    )
    calls = _wire(external, monkeypatch, tmp_path,
                  _reject_reply(_finding("X-003")), conf=rounds)
    assert _run(external, tmp_path, "--ticket", TICKET) == 1
    assert pd.render_pm_review_block_skeleton(
        "external-reviewer", ["X-002"],
    ) in calls["prompt"]
    assert pd.collect_confirmable_finding_ids(
        ticket_path.read_text(encoding="utf-8"), "external-reviewer",
    ) == ["X-002", "X-003"]


# ── F-025 강등 안내·지연 해소 ───────────────────────────────────────────


def test_degraded_confirmable_ids_names_both_consumers(
    external, pd, monkeypatch, tmp_path, capsys,
):
    """(F-025) 해소 실패 안내는 두 소비자를 함께 말한다 — 골격 실값 · 확인 근거 표면 대조."""
    _seed_board(tmp_path)
    calls = _wire(external, monkeypatch, tmp_path, _reject_reply(_finding("X-001")))

    def _raise(*_args, **_kwargs):
        raise pd.DelegateError("확인 목록 해소 실패 probe")

    monkeypatch.setattr(pd, "collect_confirmable_finding_ids", _raise)
    monkeypatch.setattr(external, "_load_pm_delegate", lambda: pd)

    assert _run(external, tmp_path, "--ticket", TICKET) == 1
    err = capsys.readouterr().err
    assert "골격" in err and "장부 기록 그대로" in err
    # 해소가 꺼져도 라운드는 돈다 — 골격은 placeholder 로 강등된다.
    assert pd.render_pm_review_block_skeleton("external-reviewer") in calls["prompt"]


def test_a_disabled_run_does_not_read_the_harvest_target(
    external, pd, monkeypatch, tmp_path,
):
    """(F-025) 비활성 no-op 은 티켓을 읽지 않는다 — 해소는 소비 지점에서만 일어난다."""
    _seed_board(tmp_path)
    _wire(external, monkeypatch, tmp_path, _reject_reply(_finding("X-001")),
          conf={"additional_reviewer_enabled": "false"})
    reads: list[str] = []
    monkeypatch.setattr(
        external, "_harvest_target_ticket_body",
        lambda *args, **kwargs: reads.append("read") or None,
    )

    assert _run(external, tmp_path, "--ticket", TICKET) == 0     # no-op
    assert reads == []


# ── F-018 무해화: 라벨만 낮추고 뒤 텍스트는 보존 ────────────────────────


def test_neutralized_fence_keeps_the_text_after_the_label(pd):
    """(F-018) fence 라벨 뒤 같은 줄 설명은 버리지 않는다(산출 보존)."""
    body = (
        "  ```pm-review-v1 (다음은 내가 참조한 지난 라운드 블록이다)\r\n"
        "{}\r\n```\r\n"
    )
    neutralized = pd._neutralize_review_fence(body)
    assert neutralized == (
        "  ```text (다음은 내가 참조한 지난 라운드 블록이다)\r\n{}\r\n```\r\n"
    )
    # 라벨만 있는 줄은 종전대로 `text` 한 단어로 낮춘다.
    assert pd._neutralize_review_fence("````pm-review-v1\n{}\n````\n") == (
        "````text\n{}\n````\n"
    )


# ── F-005 개행: 지배 표기를 한 번만 입힌다 ──────────────────────────────


def test_crlf_ticket_and_crlf_reply_keep_a_single_newline_encoding(
    external, pd, monkeypatch, tmp_path,
):
    """CRLF 티켓 × CRLF 회신도 `\\r\\r\\n`·혼재 없이 기록된다(봉인은 정규화라 못 잡는다)."""
    ticket_path = _seed_board(tmp_path)
    ticket_path.write_bytes(
        ticket_path.read_text(encoding="utf-8").replace("\n", "\r\n").encode("utf-8")
    )
    reply = _reject_reply(_finding()).replace("\n", "\r\n")
    _wire(external, monkeypatch, tmp_path, reply)

    assert _run(external, tmp_path, "--ticket", TICKET) == 1
    raw = ticket_path.read_bytes()
    assert b"\r\r\n" not in raw
    assert raw.replace(b"\r\n", b"") .count(b"\n") == 0      # LF 혼재 없음
    assert raw.replace(b"\r\n", b"").count(b"\r") == 0
    text = ticket_path.read_text(encoding="utf-8")
    assert pd.verify_ticket_seals(text) == []
    delta = pd.parse_pm_review_delta(text + _disposition("X-001"))
    assert [finding.id for finding, _row in delta.accepted] == ["X-001"]


def test_lf_ticket_and_crlf_reply_normalize_to_the_ticket_encoding(
    external, pd, monkeypatch, tmp_path,
):
    """LF 티켓에 CRLF 회신을 회수해도 티켓 표기가 섞이지 않는다."""
    ticket_path = _seed_board(tmp_path)
    _wire(external, monkeypatch, tmp_path, _reject_reply(_finding()).replace("\n", "\r\n"))

    assert _run(external, tmp_path, "--ticket", TICKET) == 1
    raw = ticket_path.read_bytes()
    assert b"\r" not in raw
    assert pd.verify_ticket_seals(raw.decode("utf-8")) == []


# ── F-006 쓰기 엔진: 형제 canonical + 사본 불일치 처방 ──────────────────


def _skew_delegate_after_prompt(external, monkeypatch, *, marked: bool) -> None:
    """프롬프트 조립까지는 정상 로더로 두고 **회수 구간부터** 로더 실패를 주입한다."""
    real_loader = external._load_pm_delegate
    state = {"prompt_done": False}
    real_run_review = external.run_review

    def _run_review(*args, **kwargs):
        state["prompt_done"] = True
        return real_run_review(*args, **kwargs)

    def _loader():
        if not state["prompt_done"]:
            return real_loader()
        error = RuntimeError(
            "엔진 사본 버전 불일치 — 로더 external_review.py" if marked
            else "표시 없는 실패"
        )
        if marked:
            error._engine_rev_skew = True
        raise error

    monkeypatch.setattr(external, "run_review", _run_review)
    monkeypatch.setattr(external, "_load_pm_delegate", _loader)


def test_harvest_writes_with_the_running_engine_not_a_stale_pm_home_copy(
    external, pd, monkeypatch, tmp_path,
):
    """PM 홈에 stale board 사본이 있어도 회수는 이 실행의 형제 엔진으로 쓴다."""
    ticket_path = _seed_board(tmp_path)
    stale = tmp_path / ".project_manager" / "tools" / "board.py"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text(
        "ENGINE_REV = 'stale'\n"
        "def find_ticket_exact(*args, **kwargs):\n"
        "    raise AssertionError('stale PM 홈 사본이 회수 쓰기를 수행했다')\n",
        encoding="utf-8", newline="\n",
    )
    _wire(external, monkeypatch, tmp_path, _reject_reply(_finding()))

    assert _run(external, tmp_path, "--ticket", TICKET) == 1
    text = ticket_path.read_text(encoding="utf-8")
    assert len(_sections(pd, text, "external-reviewer")) == 1
    assert pd.verify_ticket_seals(text) == []


def test_engine_copy_skew_becomes_a_harvest_problem_with_a_resync_prescription(
    external, monkeypatch, tmp_path, capsys,
):
    """사본 불일치는 traceback 이 아니라 회수 실패 처방(재동기·rc≠0)으로 접힌다."""
    ticket_path = _seed_board(tmp_path)
    before = ticket_path.read_text(encoding="utf-8")
    _wire(external, monkeypatch, tmp_path, _reject_reply(_finding()))

    _skew_delegate_after_prompt(external, monkeypatch, marked=True)
    assert _run(external, tmp_path, "--ticket", TICKET) == 1
    err = capsys.readouterr().err
    assert "회수 문제" in err and "재동기" in err
    assert ticket_path.read_text(encoding="utf-8") == before


def test_non_skew_runtime_error_is_not_swallowed(external, monkeypatch, tmp_path):
    """표시 없는 RuntimeError 는 흡수 대상이 아니다(진단을 삼키지 않는다)."""
    _seed_board(tmp_path)
    _wire(external, monkeypatch, tmp_path, _reject_reply(_finding()))

    _skew_delegate_after_prompt(external, monkeypatch, marked=False)
    with pytest.raises(RuntimeError, match="표시 없는 실패"):
        _run(external, tmp_path, "--ticket", TICKET)


# ── F-008·F-009 출력 형식 규칙 ─────────────────────────────────────────


def test_prompt_rules_ban_requoting_and_derive_severity_from_constants(
    external, pd, monkeypatch, tmp_path,
):
    """티켓 본문 블록 재인용 금지 + severity 값은 엔진 상수에서 포맷한다."""
    _seed_board(tmp_path)
    calls = _wire(external, monkeypatch, tmp_path, _reject_reply(_finding()))
    assert _run(external, tmp_path, "--ticket", TICKET) == 1

    prompt = calls["prompt"]
    assert "재인용하지 마라" in prompt
    assert pd.PM_REVIEW_SEVERITIES[0] in prompt

    monkeypatch.setattr(
        pd, "PM_REVIEW_SEVERITIES", ("blocker-probe", *pd.PM_REVIEW_SEVERITIES),
    )
    monkeypatch.setattr(external, "_load_pm_delegate", lambda: pd)
    assert "blocker-probe" in external._versioned_block_requirement()
