"""T-0696 — 추가 리뷰어 산출의 티켓 회수·finding severity 단일 진실 (라운드 사이드카).

두 축을 소유한다.
  · 회수: `external_review --ticket` 실행이 끝나면 **엔진이** 산출을 그 티켓의 새
    `external-reviewer` 라운드 파일(`tickets/rounds/<T>/NN-external-reviewer.md`)로 기록하고,
    내부 리뷰어와 같은 `review delta` 표면에 올린다. 내용 검증(블록 부재·스키마 위반·중복·JSON
    손상·finding ID 재선언·표면에 없는 confirmation 대상)에 걸리면 **라운드 파일을 만들지 않고**
    rc≠0 으로 사유를 말한다 — 산출 원문은 raw 에 남는다([[ADR-0090]] · 차등 판정·반사실 프로브
    없음).
  · severity: `pm-review-v1` finding 의 심각도가 블록의 필수 필드다(산문 재기재 없음).

hermetic: tmp REPO 에 실제 board 디렉터리를 만들고 diff·리뷰어 실행·격리 거울만 주입한다
(외부 전송 0). 라이브 codex 호출은 이 파일 어디에서도 하지 않는다.
"""
from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
EXTERNAL_REVIEW = TOOLS / "external_review.py"
DIFF = "diff --git a/x.py b/x.py\n@@ -1 +1 @@\n-old\n+new\n"
TICKET = "T-9601"
ROLE = "external-reviewer"


# 해소 가능한 추가 리뷰어 대상 — 대상은 `harness`+`model` 구조화 키로만 서므로(엔진 기본 커맨드
# 없음) 이 파일의 모든 형상이 그 세트를 깔고 시작한다.
_REVIEWER_TARGET = {
    "additional_reviewer.enabled": "true",
    "additional_reviewer.harness": "codex",
    "additional_reviewer.model": "gpt-5.6-sol",
}


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
def rounds_seam(pd):
    return pd._load_ticket_rounds()


# ── 픽스처: tmp PM 홈 board + 리뷰어 산출 ──────────────────────────────────


def _tickets_dir(pm_home: Path) -> Path:
    return pm_home / ".project_manager" / "board" / "tickets"


def _ticket_path(pm_home: Path, status: str = "claimed") -> Path:
    return _tickets_dir(pm_home) / status / f"{TICKET}-harvest.md"


def _rounds_dir(pm_home: Path) -> Path:
    return _tickets_dir(pm_home) / "rounds" / TICKET


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


def _round_paths(pm_home: Path, role: str | None = None) -> list[Path]:
    directory = _rounds_dir(pm_home)
    if not directory.is_dir():
        return []
    return sorted(
        path for path in directory.iterdir()
        if path.is_file() and (role is None or path.stem.endswith(f"-{role}"))
    )


def _round_texts(pm_home: Path, role: str | None = ROLE) -> list[str]:
    return [path.read_text(encoding="utf-8") for path in _round_paths(pm_home, role)]


def _write_round(pm_home: Path, ordinal: int, role: str, text: str) -> Path:
    path = _rounds_dir(pm_home) / f"{ordinal:02d}-{role}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")
    return path


def _rounds(pd, pm_home: Path, *, status: str = "claimed") -> list:
    spec = _ticket_path(pm_home, status).read_text(encoding="utf-8")
    return pd._load_ticket_rounds().load_rounds(
        _tickets_dir(pm_home), TICKET, ticket_text=spec,
    )


def _delta(pd, pm_home: Path, extra_spec: str = "", *, status: str = "claimed"):
    spec = _ticket_path(pm_home, status).read_text(encoding="utf-8") + extra_spec
    return pd.parse_pm_review_delta(spec, _rounds(pd, pm_home, status=status))


def _round_view(pd, ordinal: int, text: str, role: str = ROLE):
    rounds_module = pd._load_ticket_rounds()
    return rounds_module.Round(
        ordinal=ordinal, role=role,
        path=Path(rounds_module.round_filename(ordinal, role)),
        text=text, pending=False,
    )


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


def _prose(must_fix: str = "- 없음", *, verdict: str = "판정: 반려") -> str:
    """회신 산문 머리 — `**suggestion**` 절까지 적어야 must-fix 항목 회계가 블록에서 멈춘다."""
    return (
        f"{verdict}\n\n**must-fix** (반드시 수정):\n{must_fix}\n\n"
        "**suggestion** (권장):\n- 없음\n\n"
    )


def _reject_reply(*findings: dict) -> str:
    listed = "\n".join(f"- {row['id']}" for row in findings) or "- 없음"
    return _prose(listed) + _block(_payload(list(findings)))


def _confirm_reply(*confirmations: dict) -> str:
    return _prose(verdict="판정: 통과") + _block(_payload(confirmations=list(confirmations)))


def _confirming_reject_reply(finding: dict, *confirmations: dict) -> str:
    """신규 finding 과 confirmations 를 함께 담은 반려 회신(확인 라운드 형상)."""
    return (
        _prose(f"- {finding['id']}")
        + _block(_payload([finding], list(confirmations)))
    )


def _disposition(finding_id: str, *, ordinal: int = 1, role: str = ROLE,
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


def _append_spec(pm_home: Path, text: str, *, status: str = "claimed") -> None:
    path = _ticket_path(pm_home, status)
    path.write_text(
        path.read_text(encoding="utf-8") + text, encoding="utf-8", newline="",
    )


def _external_round_text(pd, payload: dict, *, today: str = "2026-08-18",
                         raw_block: str | None = None, head: str = "") -> str:
    """회수된 라운드 파일과 같은 모양의 본문(헤더 + 산문 + 블록)."""
    rounds_module = pd._load_ticket_rounds()
    return (
        rounds_module.render_round_header(ROLE, today=today) + "\n\n"
        + head
        + _prose("- 구조화 finding 참조")
        + (raw_block if raw_block is not None else _block(payload))
    )


def _wire(
    external, monkeypatch, pm_home: Path, reply, *, conf: dict | None = None,
) -> dict:
    """main() 을 tmp PM 홈으로 배선한다 — 반환 dict['n'] = 리뷰어 호출 수(외부 전송 시도).

    `reply` 가 호출 가능이면 프롬프트를 인자로 받아 회신을 만든다(프롬프트가 지시한 ID 를 따르는
    리뷰어 형상). dict['prompts'] 에는 이 배선으로 나간 프롬프트가 순서대로 쌓인다.
    """
    resolved_conf = {**_REVIEWER_TARGET, **(conf or {})}
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


# ── (a) 반려 라운드 회수 → 라운드 파일 + 같은 판정 표면 ─────────────────────


def test_rejected_round_lands_as_a_round_file_and_delta_finding(
    external, pd, monkeypatch, tmp_path, capsys,
):
    """(a) 반려 산출이 라운드 파일 하나가 되고 `review delta` 에 X- 가 뜬다."""
    ticket_path = _seed_board(tmp_path)
    spec_before = ticket_path.read_text(encoding="utf-8")
    reply = _reject_reply(_finding())
    _wire(external, monkeypatch, tmp_path, reply)

    assert _run(external, tmp_path, "--ticket", TICKET) == 1  # 반려 = 판정 rc
    paths = _round_paths(tmp_path)
    assert [path.name for path in paths] == [f"01-{ROLE}.md"]
    # 라운드 본문 = 첫 줄 헤더 + 회신 전문(무해화·거부 표식 없음).
    text = paths[0].read_text(encoding="utf-8")
    header, _blank, rest = text.partition("\n\n")
    assert re.fullmatch(rf"## .+ \({ROLE} · \d{{4}}-\d{{2}}-\d{{2}}\)", header)
    assert rest == reply
    # 명세 파일은 회수로 바뀌지 않는다(라운드는 별도 파일이다).
    assert ticket_path.read_text(encoding="utf-8") == spec_before
    assert f"{ROLE}[1]" in capsys.readouterr().err

    with pytest.raises(pd.PMReviewError) as caught:
        _delta(pd, tmp_path)
    assert caught.value.code == "pending"  # PM 판정 전에는 loud
    assert f"대상 채널=['{ROLE}[1]']" in str(caught.value)
    delta = _delta(pd, tmp_path, _disposition("X-001"))
    accepted = [finding for finding, _row in delta.accepted]
    assert [item.id for item in accepted] == ["X-001"]
    assert accepted[0].reviewer_role == ROLE
    rendered = pd.render_pm_review_delta(TICKET, delta)
    assert f"- 채널: {ROLE}" in rendered
    assert "- 심각도: must-fix" in rendered


def test_second_round_takes_the_next_ordinal(external, pd, monkeypatch, tmp_path):
    """라운드마다 파일이 늘고 순번은 티켓 전역으로 전진한다."""
    _seed_board(tmp_path)
    _wire(external, monkeypatch, tmp_path, _reject_reply(_finding("X-001")))
    assert _run(external, tmp_path, "--ticket", TICKET) == 1
    _wire(external, monkeypatch, tmp_path, _reject_reply(_finding("X-002")))
    assert _run(external, tmp_path, "--ticket", TICKET) == 1

    assert [path.name for path in _round_paths(tmp_path)] == [
        f"01-{ROLE}.md", f"02-{ROLE}.md",
    ]
    assert pd._load_ticket_rounds().verify_rounds(
        _tickets_dir(tmp_path), TICKET,
        ticket_text=_ticket_path(tmp_path).read_text(encoding="utf-8"),
    ) == []


def test_harvest_reserves_through_the_rounds_seam(
    external, pd, monkeypatch, tmp_path,
):
    """회수 예약은 준비와 **같은 seam**(`ticket_rounds.reserve_round`)을 지난다."""
    _seed_board(tmp_path)
    rounds_module = pd._load_ticket_rounds()
    seen: dict = {}
    real_reserve = rounds_module.reserve_round

    def _reserve(tickets_dir, ticket_id, role, *, content, lock):
        seen.update(ticket=ticket_id, role=role, content=content, locked=lock is not None)
        return real_reserve(tickets_dir, ticket_id, role, content=content, lock=lock)

    monkeypatch.setattr(rounds_module, "reserve_round", _reserve)
    monkeypatch.setattr(external, "_load_ticket_rounds", lambda: rounds_module)
    monkeypatch.setattr(pd, "_load_ticket_rounds", lambda: rounds_module)
    monkeypatch.setattr(external, "_load_pm_delegate", lambda: pd)
    _wire(external, monkeypatch, tmp_path, _reject_reply(_finding()))

    assert _run(external, tmp_path, "--ticket", TICKET) == 1
    assert seen["ticket"] == TICKET and seen["role"] == ROLE
    assert seen["locked"] is True          # 채번+생성은 board 락 안이다.
    assert seen["content"].endswith("\n")


# ── (b) 회수 거부: 사유 불문 파일 미생성 + raw 보존 + rc≠0 ──────────────────


_BROKEN_JSON_REPLY = (
    _prose("- X-001") + '```pm-review-v1\n{"version":2,"findings":[\n```\n'
)
# 스캐너가 fence 후보로 보지만 라벨 표기가 어긋난 형상 — 판정이 좁으면 이 fence 가 라운드에
# 착지해 그 티켓의 전역 블록 스캔이 계속 fail-loud 한다.
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
            _prose(verdict="판정: 통과") + _block(_payload()) + _block(_payload()),
            "block 이 정확히 하나가 아닙니다",
            id="duplicate",
        ),
        pytest.param(
            _prose("- X-001") + _block(_payload([_finding(severity=None)])),
            "missing=['severity']",
            id="severity-absent-in-current-generation",
        ),
        pytest.param(
            _prose("- X-001") + _block(_payload([_finding(severity="blocker")])),
            "severity 미지원",
            id="severity-unsupported",
        ),
        pytest.param(
            _prose(verdict="판정: 통과")
            + _block(_payload(confirmations=[{
                "id": "F-001", "status": "resolved", "evidence": "타 채널 ID",
            }])),
            "채널 접두 불일치",
            id="pass-round-confirmation-namespace",
        ),
        pytest.param(
            _prose("- F-001") + _block(_payload([_finding("F-001")])),
            "채널 접두 불일치",
            id="wrong-id-namespace",
        ),
        pytest.param(_BROKEN_JSON_REPLY, "JSON 파싱 실패", id="broken-json"),
        pytest.param(_BROKEN_FENCE_REPLY, "손상된 review fence", id="broken-fence"),
    ],
)
def test_refused_harvest_creates_no_round_file_and_keeps_the_raw_output(
    external, pd, monkeypatch, tmp_path, capsys, reply, reason,
):
    """(b) 회수 거부는 사유 불문 같은 처리다 — 파일 미생성·raw 보존·rc≠0."""
    ticket_path = _seed_board(tmp_path)
    before = ticket_path.read_text(encoding="utf-8")
    _wire(external, monkeypatch, tmp_path, reply)

    assert _run(external, tmp_path, "--ticket", TICKET) != 0
    err = capsys.readouterr().err
    assert "회수 문제" in err and reason in err
    assert "라운드 파일은 만들지 않았습니다" in err
    assert "raw 에 보존됩니다" in err
    assert _round_paths(tmp_path) == []
    assert ticket_path.read_text(encoding="utf-8") == before


def test_a_refused_round_does_not_lock_the_next_round(
    external, pd, monkeypatch, tmp_path, capsys,
):
    """(b) 거부된 산출 뒤에도 정상 라운드가 착지하고 delta 가 green 이다."""
    conf = {"additional_reviewer.rounds_max": "9"}
    _seed_board(tmp_path)
    _wire(external, monkeypatch, tmp_path, _BROKEN_JSON_REPLY, conf=conf)
    assert _run(external, tmp_path, "--ticket", TICKET) != 0
    assert _round_paths(tmp_path) == []
    capsys.readouterr()

    _wire(external, monkeypatch, tmp_path, _reject_reply(_finding("X-001")), conf=conf)
    assert _run(external, tmp_path, "--ticket", TICKET) == 1
    # 거부 라운드가 순번을 먹지 않았다 — 정상 산출이 01 이다.
    assert [path.name for path in _round_paths(tmp_path)] == [f"01-{ROLE}.md"]
    delta = _delta(pd, tmp_path, _disposition("X-001"))
    assert [finding.id for finding, _row in delta.accepted] == ["X-001"]


def test_reply_cannot_declare_the_engine_refusal_marker(
    external, pd, monkeypatch, tmp_path, capsys,
):
    """회신이 엔진 전용 표식을 실으면 회수 자체가 거부된다(판정 표면 자가 제외 금지)."""
    _seed_board(tmp_path)
    _wire(
        external, monkeypatch, tmp_path,
        _prose("- X-001") + f"{pd.EXTERNAL_REVIEW_REFUSED_LINE}\n\n"
        + _block(_payload([_finding("X-001")])),
    )
    assert _run(external, tmp_path, "--ticket", TICKET) != 0
    err = capsys.readouterr().err
    assert "회수 문제" in err and pd.EXTERNAL_REVIEW_REFUSED_MARKER in err
    assert _round_paths(tmp_path) == []


def test_reply_may_quote_markup_without_creating_other_rounds(
    external, pd, monkeypatch, tmp_path,
):
    """회신이 다른 역할의 절 표기를 인용해도 라운드는 자기 파일 하나뿐이다(파서 경계 없음)."""
    _seed_board(tmp_path)
    quoted = (
        _prose("- X-001")
        + "<!-- pm-ticket-section:start role=developer -->\n인용한 티켓 본문\n"
        "<!-- pm-ticket-section:end role=developer -->\n\n"
        + _block(_payload([_finding()]))
    )
    _wire(external, monkeypatch, tmp_path, quoted)

    assert _run(external, tmp_path, "--ticket", TICKET) == 1
    paths = _round_paths(tmp_path)
    assert [path.name for path in paths] == [f"01-{ROLE}.md"]
    assert "인용한 티켓 본문" in paths[0].read_text(encoding="utf-8")
    assert _round_paths(tmp_path, "developer") == []
    delta = _delta(pd, tmp_path, _disposition("X-001"))
    assert [finding.id for finding, _row in delta.accepted] == ["X-001"]


# ── F-028 구조 폐쇄: 기존 라운드의 손상이 새 라운드 판정에 끼어들지 않는다 ──────


def test_a_malformed_existing_round_does_not_open_the_gate(
    external, pd, monkeypatch, tmp_path, capsys,
):
    """(F-028 재현) 기존 라운드가 malformed 여도 위반 산출은 거부다 — 파일 미생성·raw 보존.

    차등 판정 시절에는 기준선이 이미 dirty 하면 새 산출이 통과했다(fail-open). 판정이 이 회신
    하나만 보므로 그 창이 없다.
    """
    _seed_board(tmp_path)
    _write_round(
        tmp_path, 1, ROLE,
        _external_round_text(
            pd, {}, raw_block='```pm-review-v1\n{"version":2,"findings":[\n```\n',
        ),
    )
    _wire(external, monkeypatch, tmp_path, _BROKEN_FENCE_REPLY)

    assert _run(external, tmp_path, "--ticket", TICKET) != 0
    err = capsys.readouterr().err
    assert "회수 문제" in err and "손상된 review fence" in err
    assert [path.name for path in _round_paths(tmp_path)] == [f"01-{ROLE}.md"]


def test_a_malformed_existing_round_does_not_block_a_valid_new_round(
    external, pd, monkeypatch, tmp_path, capsys,
):
    """(F-028 짝) 기존 라운드의 손상을 새 라운드 탓으로 돌리지 않는다 — 정상 산출은 착지한다."""
    _seed_board(tmp_path)
    _write_round(
        tmp_path, 1, ROLE,
        _external_round_text(
            pd, _payload([_finding("X-001", classification="style")]),
        ),
    )
    _wire(external, monkeypatch, tmp_path, _reject_reply(_finding("X-100")))

    assert _run(external, tmp_path, "--ticket", TICKET) == 1
    assert "회수 문제" not in capsys.readouterr().err
    assert [path.name for path in _round_paths(tmp_path)] == [
        f"01-{ROLE}.md", f"02-{ROLE}.md",
    ]
    assert '"id":"X-100"' in _round_texts(tmp_path)[1]


# ── (c) 확인 전용 라운드 ────────────────────────────────────────────────


def test_confirmation_round_resolves_the_finding_out_of_the_delta(
    external, pd, monkeypatch, tmp_path,
):
    """(c) confirm-fix 라운드가 X- ID 를 resolved 로 확인하면 delta 에서 사라진다."""
    _seed_board(tmp_path)
    _wire(external, monkeypatch, tmp_path, _reject_reply(_finding()))
    assert _run(external, tmp_path, "--ticket", TICKET) == 1

    resolved = _confirm_reply({
        "id": "X-001", "status": "resolved", "evidence": "회귀 통과 rc=0",
    })
    _wire(external, monkeypatch, tmp_path, resolved)
    assert _run(external, tmp_path, "--ticket", TICKET, "--confirm-fix") == 0

    delta = _delta(pd, tmp_path, _disposition("X-001"))
    assert delta.accepted == ()
    assert pd.render_pm_review_delta(TICKET, delta) == ""


# ── (d) 회수 대상이 없는 실행 ────────────────────────────────────────────


def test_paths_only_run_harvests_nothing(external, monkeypatch, tmp_path):
    """(d) 티켓 없는 실행은 회수 대상이 아니다 — 라운드가 생기지 않는다."""
    ticket_path = _seed_board(tmp_path)
    before = ticket_path.read_text(encoding="utf-8")
    _wire(external, monkeypatch, tmp_path, _reject_reply(_finding()))

    assert _run(external, tmp_path, "--paths", "x.py", "--no-gate") == 1
    assert _round_paths(tmp_path) == []
    assert ticket_path.read_text(encoding="utf-8") == before


def test_free_form_gate_without_ticket_harvests_nothing(
    external, monkeypatch, tmp_path,
):
    """자유 문자열 게이트도 회수 대상이 아니다(회수 입력은 ticket 형상 하나다)."""
    _seed_board(tmp_path)
    _wire(external, monkeypatch, tmp_path, _reject_reply(_finding()))

    assert _run(external, tmp_path, "--gate", "free-gate", "--paths", "x.py") == 1
    assert _round_paths(tmp_path) == []


def test_done_ticket_write_is_refused_loudly(external, monkeypatch, tmp_path, capsys):
    """완료 티켓에는 라운드를 만들지 않는다 — 거부 사유가 표면화되고 rc≠0 이다."""
    _seed_board(tmp_path, status="done")
    _wire(external, monkeypatch, tmp_path, _reject_reply(_finding()))

    assert _run(external, tmp_path, "--ticket", TICKET) != 0
    assert "open/claimed" in capsys.readouterr().err
    assert _round_paths(tmp_path) == []


# ── (f) 불변식 ─────────────────────────────────────────────────────────


def test_every_process_role_is_a_round_role(pd, rounds_seam):
    """(f) PM 프로세스 참여 역할 ⊆ 라운드 역할 — 채널이 늘면 여기서 걸린다."""
    assert set(pd.ROLE_CHOICES) <= set(rounds_seam.ROLES)
    assert ROLE in rounds_seam.ROLES
    assert "researcher" in rounds_seam.ROLES
    assert set(pd.REVIEW_ROLES) <= set(rounds_seam.ROLES)
    assert set(pd.PM_REVIEW_FINDING_ID_PREFIXES) == set(pd.REVIEW_ROLES)
    assert len(set(pd.PM_REVIEW_FINDING_ID_PREFIXES.values())) == len(pd.REVIEW_ROLES)
    # 추가 리뷰어 라운드는 슬롯 왕복이 아니라 엔진이 쓴다 — 준비 대상이 아니다.
    assert set(pd.TICKET_COPY_PREPARE_ROLES) == set(rounds_seam.ROLES) - {ROLE}


def test_deleted_single_file_devices_are_absent(pd, external):
    """단일 파일 컨테이너 때문에 있던 장치는 옮겨 살리지 않고 지운다([[ADR-0090]])."""
    for symbol in (
        "write_external_reviewer_section", "build_external_review_section_content",
        "ExternalReviewSectionWrite", "_render_external_review_section",
        "neutralize_ticket_growth_markup", "_neutralize_review_fence",
        "EXTERNAL_REVIEW_BLOCK_WARNING_PREFIX", "EXTERNAL_REVIEW_SECTION_LABEL",
        "_external_review_delta_regression", "_pm_review_delta_regression_reason",
        "_pm_review_probe_section_content", "_PM_REVIEW_PROBE_TEXT",
    ):
        assert not hasattr(pd, symbol), symbol
    assert not hasattr(external, "_harvest_target_ticket_body")
    assert not hasattr(external, "_ticket_body_max_bytes")


# ── (g) 기존 티켓 호환 ─────────────────────────────────────────────────


def test_disposition_without_reviewer_role_is_read_as_internal_channel(pd):
    """(g) `reviewer_role` 부재 disposition 은 code-reviewer 판정으로 해석한다."""
    round_text = (
        "## 리뷰 (code-reviewer · 2026-08-17)\n\n"
        "## must-fix\n- F-001\n\n## 판정\n판정: 반려\n\n"
        + _block(_payload([_finding("F-001")]))
    )
    legacy = json.dumps({
        "version": DISPOSITION_VERSION,
        "reviewer_ordinal": 1,
        "dispositions": [{
            "id": "F-001", "decision": "accepted", "reason": "구 티켓 판정",
            "scope": "F-001 허용 범위", "prerequisite": "",
        }],
    }, ensure_ascii=False, separators=(",", ":"))
    delta = pd.parse_pm_review_delta(
        "\n```pm-review-disposition-v1\n" + legacy + "\n```\n",
        [_round_view(pd, 1, round_text, role="code-reviewer")],
    )
    finding, _row = delta.accepted[0]
    assert finding.id == "F-001" and finding.reviewer_role == pd.INTERNAL_REVIEW_ROLE


# ── (k)(l) 산문 축소 · 0건 라운드 ───────────────────────────────────────


def test_prose_item_enumeration_is_optional_for_the_block_truth(
    external, pd, monkeypatch, tmp_path,
):
    """(k) 산문이 항목을 나열하지 않아도(판정 요약만) 블록이 판정 입력으로 선다."""
    _seed_board(tmp_path)
    reply = (
        "판정: 반려 · finding 1건(must-fix 1건)\n\n"
        "**must-fix** (반드시 수정):\n- X-001\n\n"
        + _block(_payload([_finding()]))
    )
    _wire(external, monkeypatch, tmp_path, reply)
    assert _run(external, tmp_path, "--ticket", TICKET) == 1

    body = _round_texts(tmp_path)[0]
    assert "X-001 probe rc=1" not in body.split("```pm-review-v1")[0]
    delta = _delta(pd, tmp_path, _disposition("X-001"))
    finding, _row = delta.accepted[0]
    assert finding.evidence == "X-001 probe rc=1"


def test_finding_zero_pass_round_needs_only_the_compact_pm_acceptance(
    external, pd, monkeypatch, tmp_path,
):
    """(l) 0건 통과 라운드는 산문 통과 선언 + finding-zero 판정으로 닫힌다."""
    _seed_board(tmp_path)
    _wire(external, monkeypatch, tmp_path,
          _prose(verdict="판정: 통과") + _block(_payload()))
    assert _run(external, tmp_path, "--ticket", TICKET) == 0

    zero = json.dumps({
        "version": DISPOSITION_VERSION,
        "reviewer_role": ROLE,
        "reviewer_ordinal": 1,
        "finding_zero": "accepted",
    }, ensure_ascii=False, separators=(",", ":"))
    delta = _delta(
        pd, tmp_path, "\n```pm-review-disposition-v1\n" + zero + "\n```\n",
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
    assert pd.render_pm_review_block_skeleton(ROLE, []) in prompt
    assert "X-001" in prompt and "severity" in prompt

    monkeypatch.setattr(
        pd, "PM_REVIEW_SEVERITIES", (*pd.PM_REVIEW_SEVERITIES, "blocker-probe"),
    )
    monkeypatch.setattr(external, "_load_pm_delegate", lambda: pd)
    assert "blocker-probe" in external._versioned_block_requirement()


def test_prompt_says_the_round_file_is_the_harvest_target(external):
    """프롬프트는 회수 도착지를 라운드 파일로 말한다(옛 '역할 절' 표현 없음)."""
    assert "라운드 파일로 회수" in external._VERSIONED_BLOCK_HEADER
    assert "역할 절" not in external._VERSIONED_BLOCK_HEADER


def test_disposition_template_targets_the_external_channel(
    external, pd, monkeypatch, tmp_path,
):
    """PM 은 채널마다 다른 절차를 밟지 않는다 — 같은 골격 명령이 X- finding 을 채운다."""
    _seed_board(tmp_path)
    _wire(external, monkeypatch, tmp_path, _reject_reply(_finding()))
    assert _run(external, tmp_path, "--ticket", TICKET) == 1

    rendered = pd.render_pm_review_disposition_template(
        _ticket_path(tmp_path).read_text(encoding="utf-8"), _rounds(pd, tmp_path),
    )
    payload = json.loads(
        rendered.split("```pm-review-disposition-v1\n", 1)[1].split("\n```", 1)[0]
    )
    assert payload["reviewer_role"] == ROLE
    assert [row["id"] for row in payload["dispositions"]] == ["X-001"]


# ── F-002 세대 승격: 실 보드의 v1 블록을 legacy 로 계속 읽는다 ──────────────


LEGACY_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "legacy_review_blocks_v1.json"


def _legacy_fixture_tickets() -> list[dict]:
    return json.loads(LEGACY_FIXTURE.read_text(encoding="utf-8"))["tickets"]


def _rebuild_legacy_ticket(pd, entry: dict) -> tuple[str, list]:
    """픽스처(실 보드 v1 블록 형상)를 (명세, 라운드 목록) 으로 되살린다(값은 축약본)."""
    spec = [f"---\nid: {entry['ticket']}\nstatus: claimed\n---\n# {entry['ticket']}\n"]
    rounds = []
    for ordinal, section in enumerate(entry["sections"], 1):
        role = section["role"]
        rounds.append(_round_view(
            pd, ordinal,
            f"## 리뷰 ({role} · 2026-08-17)\n\n"
            f"{section['verdict_prose']}\n\n## must-fix\n- 구조화 finding 참조\n\n"
            + _block(section["payload"]),
            role=role,
        ))
    for index, disposition in enumerate(entry["dispositions"]):
        payload = dict(disposition)
        # 라운드 순번은 티켓 전역 1..N 이다 — 판정이 가리키는 라운드를 그 축으로 옮긴다.
        if "reviewer_ordinal" in payload:
            payload["reviewer_ordinal"] = index + 1
        spec.append(
            "\n```pm-review-disposition-v1\n"
            + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            + "\n```\n"
        )
    return "".join(spec), rounds


@pytest.mark.parametrize(
    "entry", _legacy_fixture_tickets(), ids=lambda entry: entry["ticket"],
)
def test_legacy_v1_blocks_from_the_live_board_stay_readable(pd, entry):
    """구세대(v1) 블록은 severity 없이도 delta 를 낸다 — 마이그레이션된 자산을 잠그지 않는다.

    픽스처는 실 보드 7건(T-0691·T-0693·T-0701·T-0703·T-0704·T-0705·T-0735)의 블록 형상을 값만
    축약해 굳힌 것이다 — 라운드 파일로 옮겨져도 파서가 계속 읽어야 fix 루프가 돌아간다.
    """
    spec, rounds = _rebuild_legacy_ticket(pd, entry)
    assert all(
        section["payload"]["version"] == LEGACY_BLOCK_VERSION
        for section in entry["sections"]
    )
    assert not any(
        "severity" in finding
        for section in entry["sections"]
        for finding in section["payload"]["findings"]
    )

    delta = pd.parse_pm_review_delta(spec, rounds)   # malformed 면 fix 루프가 막힌다.
    rendered = pd.render_pm_review_delta(entry["ticket"], delta)
    if delta.accepted:
        assert f"- 심각도: {pd.PM_REVIEW_SEVERITY_UNSPECIFIED_LABEL}" in rendered
    summary_rows = [
        row for row in pd.render_pm_review_summary(spec, rounds).splitlines()
        if "severity=" in row
    ]
    assert summary_rows, "구 블록 티켓의 요약이 통째로 접혔다"
    assert all(
        f"severity={pd.PM_REVIEW_SEVERITY_UNSPECIFIED_LABEL}" in row
        for row in summary_rows
    )


def test_current_generation_block_still_requires_severity(pd):
    """세대 경계는 블록 payload 의 `version` 이다 — 현행 세대는 부재를 계속 거부한다."""
    round_text = _external_round_text(pd, _payload([_finding(severity=None)]))
    with pytest.raises(pd.PMReviewError, match=r"missing=\['severity'\]") as caught:
        pd.parse_pm_review_delta("", [_round_view(pd, 1, round_text)])
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


# ── F-001 혼재 티켓: 라운드 단위 관용 요약 ──────────────────────────────


def test_summary_folds_only_the_broken_round_of_a_mixed_ticket(pd):
    """(F-001) 구 세대·현행·손상 블록이 섞여도 나머지 라운드 요약은 살아 있다."""
    rounds = [
        _round_view(pd, 1, _external_round_text(
            pd, _payload([_finding("X-001", severity=None)],
                         version=LEGACY_BLOCK_VERSION),
        )),
        _round_view(pd, 2, _external_round_text(
            pd, _payload([_finding("X-002", severity="should-fix")]),
        )),
        _round_view(pd, 3, _external_round_text(
            pd, _payload([_finding("X-003", classification="style")]),
        )),
    ]
    summary = pd.render_pm_review_summary("", rounds)

    joined = "\n".join(summary.splitlines())
    assert f"{ROLE}[1] X-001 severity={pd.PM_REVIEW_SEVERITY_UNSPECIFIED_LABEL}" in joined
    assert f"{ROLE}[2] X-002 severity=should-fix" in joined
    folded = [row for row in summary.splitlines() if "요약 불가" in row]
    assert len(folded) == 1 and f"{ROLE}[3]" in folded[0]
    assert "class 미지원" in folded[0]


def test_summary_folds_a_broken_json_block_into_its_own_round_row(pd):
    """JSON 자체가 깨진 라운드도 그 한 줄로 접힌다(티켓 전체 요약을 버리지 않는다).

    손수정·마이그레이션으로 생길 수 있는 형상이다 — 회수는 이런 산출을 애초에 만들지 않는다.
    """
    rounds = [
        _round_view(pd, 1, _external_round_text(pd, _payload([_finding("X-001")]))),
        _round_view(pd, 2, _external_round_text(
            pd, {}, raw_block='```pm-review-v1\n{"version":2,"findings":[\n```\n',
        )),
    ]
    summary = pd.render_pm_review_summary(_disposition("X-001"), rounds)

    rows = summary.splitlines()
    assert any(
        f"{ROLE}[1] X-001 severity=must-fix" in row and "PM=accepted" in row
        for row in rows
    )
    folded = [row for row in rows if "요약 불가" in row]
    assert len(folded) == 1
    assert f"{ROLE}[2]" in folded[0] and "JSON 파싱 실패" in folded[0]


# ── F-003 ticket 형상 게이트 회수 ───────────────────────────────────────


def test_ticket_shaped_gate_without_ticket_flag_is_harvested(
    external, pd, monkeypatch, tmp_path,
):
    """문서화된 `--paths … --gate T-NNNN` 설계 리뷰 형상도 같은 회수 규칙을 탄다."""
    _seed_board(tmp_path)
    _wire(external, monkeypatch, tmp_path, _reject_reply(_finding()))

    assert _run(external, tmp_path, "--gate", TICKET, "--paths", "x.py") == 1
    assert [path.name for path in _round_paths(tmp_path)] == [f"01-{ROLE}.md"]
    delta = _delta(pd, tmp_path, _disposition("X-001"))
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
    _seed_board(tmp_path)
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
    assert "라운드 파일을 만들지 않습니다" in err
    assert _round_paths(tmp_path) == []


# ── F-004 finding ID 네임스페이스: 실값 프롬프트 + 재선언 거부 ──────────


def test_prompt_carries_the_next_finding_id_from_the_round_files(
    external, pd, monkeypatch, tmp_path,
):
    """엔진이 라운드 파일의 기존 최대 번호를 읽어 이번 라운드의 시작 ID 를 실값으로 싣는다."""
    _seed_board(tmp_path)
    calls = _wire(external, monkeypatch, tmp_path, _reject_reply(_finding()))
    assert _run(external, tmp_path, "--ticket", TICKET) == 1
    assert "`X-001` 부터" in calls["prompt"]

    calls = _wire(external, monkeypatch, tmp_path, _reject_reply(_finding("X-002")))
    assert _run(external, tmp_path, "--ticket", TICKET) == 1
    assert "`X-002` 부터" in calls["prompt"]        # 라운드 파일의 기존 최대 번호 + 1

    calls = _wire(external, monkeypatch, tmp_path, _confirm_reply({
        "id": "X-002", "status": "resolved", "evidence": "회귀 rc=0",
    }))
    assert _run(external, tmp_path, "--ticket", TICKET, "--confirm-fix") == 0
    assert "`X-003` 부터" in calls["prompt"]
    assert pd.next_review_finding_id("본문에 X-007 과 X-002 가 있다", ROLE) == "X-008"
    assert pd.next_review_finding_id("빈 티켓", ROLE) == "X-001"


def test_next_finding_id_sees_the_round_files_not_only_the_spec(pd, tmp_path):
    """다음 ID 의 시야는 명세 + **모든 라운드**다 — 라운드만 보면 지난 번호를 다시 지시한다."""
    rounds = [
        _round_view(pd, 1, _external_round_text(pd, _payload([_finding("X-001")]))),
        _round_view(pd, 2, _external_round_text(pd, _payload([_finding("X-004")]))),
    ]
    assert pd.next_review_finding_id("명세에는 ID 가 없다", ROLE, rounds) == "X-005"


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
    거부된다 — 출처는 회수 대상 티켓의 라운드 파일이다.
    """
    conf = {"additional_reviewer.rounds_max": "9"}
    _seed_board(tmp_path)
    seen: list[str] = []

    def _reply_following_the_prompt(prompt: str) -> str:
        seen.append(_prompt_next_id(prompt))
        return _reject_reply(_finding(seen[-1]))

    for _round_index in range(3):
        _wire(external, monkeypatch, tmp_path, _reply_following_the_prompt, conf=conf)
        assert _run(external, tmp_path, "--paths", "x.py", "--gate", TICKET) == 1

    assert seen == ["X-001", "X-002", "X-003"]
    assert len(_round_paths(tmp_path)) == 3
    delta = _delta(
        pd, tmp_path,
        _disposition("X-001") + _disposition("X-002", ordinal=2)
        + _disposition("X-003", ordinal=3),
    )
    assert sorted(finding.id for finding, _row in delta.accepted) == [
        "X-001", "X-002", "X-003",
    ]


def test_reused_finding_id_is_refused_and_the_next_round_can_land(
    external, pd, monkeypatch, tmp_path, capsys,
):
    """(F-004) 같은 ID 재선언은 회수하지 않는다 — 티켓은 계속 판정 가능하다."""
    conf = {"additional_reviewer.rounds_max": "9"}
    _seed_board(tmp_path)
    _wire(external, monkeypatch, tmp_path, _reject_reply(_finding("X-001")), conf=conf)
    assert _run(external, tmp_path, "--ticket", TICKET) == 1

    _wire(external, monkeypatch, tmp_path, _reject_reply(_finding("X-001")), conf=conf)
    assert _run(external, tmp_path, "--ticket", TICKET) != 0
    err = capsys.readouterr().err
    assert "회수 문제" in err and "재선언" in err
    assert len(_round_paths(tmp_path)) == 1        # 거부 산출은 라운드가 되지 않는다.

    # 재시도 라운드가 새 ID 로 정상 착지한다.
    _wire(external, monkeypatch, tmp_path, _reject_reply(_finding("X-002")), conf=conf)
    assert _run(external, tmp_path, "--ticket", TICKET) == 1
    delta = _delta(
        pd, tmp_path, _disposition("X-001") + _disposition("X-002", ordinal=2),
    )
    assert sorted(finding.id for finding, _row in delta.accepted) == ["X-001", "X-002"]


def test_self_confirmed_new_finding_is_refused_and_the_next_round_can_land(
    external, pd, monkeypatch, tmp_path, capsys,
):
    """자기 신규 ID 를 `findings`·`confirmations` 양쪽에 실은 회신은 회수하지 않는다.

    선행 선언이 공집합인 첫 라운드에서 나오는 형상이라 재선언 축이 아니라 블록 축에서 걸린다 —
    판정 표면은 이 형상을 "confirmation이 선행 finding ID를 참조하지 않음"으로 막는다.
    """
    conf = {"review_rounds_max": "9"}
    _seed_board(tmp_path)
    finding = _finding("X-001")
    _wire(external, monkeypatch, tmp_path, _confirming_reject_reply(
        finding, {"id": "X-001", "status": "resolved", "evidence": "자기-확인"},
    ), conf=conf)

    assert _run(external, tmp_path, "--ticket", TICKET) == 1

    err = capsys.readouterr().err
    assert "회수 문제" in err and "ID 중복: X-001" in err
    assert _round_paths(tmp_path) == []            # 거부 산출은 라운드가 되지 않는다.

    # 규약대로 나눠 낸 다음 라운드는 그대로 착지한다(역방향).
    _wire(external, monkeypatch, tmp_path, _reject_reply(finding), conf=conf)
    assert _run(external, tmp_path, "--ticket", TICKET) == 1
    assert len(_round_paths(tmp_path)) == 1
    delta = _delta(pd, tmp_path, _disposition("X-001"))
    assert [item.id for item, _row in delta.accepted] == ["X-001"]


def test_confirmation_round_may_reference_existing_ids(
    external, pd, monkeypatch, tmp_path,
):
    """확인 라운드는 기존 ID 를 참조해야 한다 — 재선언 판정이 그것까지 막지 않는다."""
    _seed_board(tmp_path)
    _wire(external, monkeypatch, tmp_path, _reject_reply(_finding("X-001")))
    assert _run(external, tmp_path, "--ticket", TICKET) == 1

    _wire(external, monkeypatch, tmp_path, _confirm_reply({
        "id": "X-001", "status": "resolved", "evidence": "회귀 rc=0",
    }))
    assert _run(external, tmp_path, "--ticket", TICKET, "--confirm-fix") == 0
    assert len(_round_paths(tmp_path)) == 2
    delta = _delta(pd, tmp_path, _disposition("X-001"))
    assert delta.accepted == ()


# ── F-016 회수 게이트 시야 == 판정 표면(confirmation 대상 대조) ──────────


def test_hallucinated_confirmation_target_is_refused(
    external, pd, monkeypatch, tmp_path, capsys,
):
    """(F-016) 티켓에 없는 ID(하네스 환각)를 확인하면 회수가 거부된다."""
    conf = {"additional_reviewer.rounds_max": "9"}
    _seed_board(tmp_path)
    _wire(external, monkeypatch, tmp_path, _reject_reply(_finding("X-001")), conf=conf)
    assert _run(external, tmp_path, "--ticket", TICKET) == 1

    _wire(external, monkeypatch, tmp_path, _confirming_reject_reply(
        _finding("X-002"),
        {"id": "X-099", "status": "resolved", "evidence": "존재하지 않는 ID"},
    ), conf=conf)
    assert _run(external, tmp_path, "--ticket", TICKET) != 0
    assert "confirmation 대상 finding 부재: X-099" in capsys.readouterr().err
    assert len(_round_paths(tmp_path)) == 1

    # 정상 라운드는 그대로 판정 대상이고 delta 는 계속 읽힌다.
    delta = _delta(pd, tmp_path, _disposition("X-001"))
    assert [finding.id for finding, _row in delta.accepted] == ["X-001"]


def test_confirmation_of_an_unharvested_id_is_refused(
    external, pd, monkeypatch, tmp_path, capsys,
):
    """(F-016) 회수되지 않은 산출의 ID 는 표면에 없다 — 그 ID 를 확인해도 거부다."""
    conf = {"additional_reviewer.rounds_max": "9"}
    _seed_board(tmp_path)
    _wire(external, monkeypatch, tmp_path, _BROKEN_JSON_REPLY, conf=conf)
    assert _run(external, tmp_path, "--ticket", TICKET) != 0   # 라운드1 회수 거부
    capsys.readouterr()

    _wire(external, monkeypatch, tmp_path, _confirming_reject_reply(
        _finding("X-002"),
        {"id": "X-001", "status": "resolved", "evidence": "회수되지 않은 산출의 ID"},
    ), conf=conf)
    assert _run(external, tmp_path, "--ticket", TICKET) != 0
    err = capsys.readouterr().err
    assert "회수 문제" in err and "confirmation 대상 finding 부재: X-001" in err
    assert _round_paths(tmp_path) == []

    # 다음 라운드가 정상 착지하고 delta 가 green 이다.
    _wire(external, monkeypatch, tmp_path, _reject_reply(_finding("X-003")), conf=conf)
    assert _run(external, tmp_path, "--ticket", TICKET) == 1
    delta = _delta(pd, tmp_path, _disposition("X-003"))
    assert [finding.id for finding, _row in delta.accepted] == ["X-003"]


def test_declarations_count_only_the_surface_rounds(pd):
    """(F-016) 표면 선언 집합은 거부 표식 라운드·산문 인용을 세지 않는다.

    거부 표식 라운드는 단일 파일 시절 산출이 마이그레이션된 형상이다(새 회수는 만들지 않는다).
    """
    normal = _round_view(pd, 1, _external_round_text(pd, _payload([_finding("X-001")])))
    refused = _round_view(pd, 2, _external_round_text(
        pd, {}, head=f"{pd.EXTERNAL_REVIEW_REFUSED_LINE}\n\n",
        raw_block=_block(_payload([_finding("X-002")])),
    ))
    spec = "\n산문에서 X-050 을 인용한다.\n"

    assert pd.collect_review_finding_declarations(spec, ROLE, [normal, refused]) == {
        "X-001",
    }
    # 재사용 방지 스캔은 거부 라운드·산문까지 넓게 본다 — 두 시야는 의도적으로 다르다.
    assert pd.next_review_finding_id(spec, ROLE, [normal, refused]) == "X-051"


def test_confirm_fix_after_an_unharvested_round_is_refused_before_any_spawn(
    external, pd, monkeypatch, tmp_path, capsys,
):
    """(F-016) 회수되지 않은 산출은 확인 전용 라운드의 근거가 되지 않는다(전송 0).

    엔진이 그 지적을 근거로 실으면 출력 규칙이 '그 X- ID 를 confirmations 에 실어라'고 지시해,
    확인 라운드가 회수 게이트에 다시 걸린다 — 엔진이 스스로 함정을 지시한다.
    """
    _seed_board(tmp_path)
    _wire(external, monkeypatch, tmp_path, _BROKEN_JSON_REPLY)
    assert _run(external, tmp_path, "--ticket", TICKET) != 0
    capsys.readouterr()

    calls = _wire(external, monkeypatch, tmp_path, _confirm_reply({
        "id": "X-001", "status": "resolved", "evidence": "회수되지 않은 산출의 ID",
    }))
    assert _run(external, tmp_path, "--ticket", TICKET, "--confirm-fix") == \
        external.EXIT_ROUND_LIMIT_EXCEEDED
    assert calls["n"] == 0                       # 외부 전송·과금 0
    err = capsys.readouterr().err
    assert "판정 표면에 없습니다" in err and "X-001" in err
    assert "일반 라운드" in err
    assert _round_paths(tmp_path) == []


def test_confirm_fix_after_a_pm_rejected_finding_is_refused_with_the_knob(
    external, pd, monkeypatch, tmp_path, capsys,
):
    """PM 이 rejected 로 판정한 지적은 확인 근거가 아니다 — 처방에 노브까지 붙는다."""
    _seed_board(tmp_path)
    _wire(external, monkeypatch, tmp_path, _reject_reply(_finding("X-001")))
    assert _run(external, tmp_path, "--ticket", TICKET) == 1
    _append_spec(tmp_path, _disposition("X-001", decision="rejected"))
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


# ── F-021 프롬프트 골격: 확인 가능한 ID 실값(직전 라운드 파일) ─────────────


def test_prompt_skeleton_carries_the_confirmable_ids_of_the_previous_round(
    external, pd, monkeypatch, tmp_path,
):
    """(F-021) 골격의 confirmations 는 **직전 라운드 파일**에서 읽은 실값이다.

    확인 전용 라운드의 임무가 '직전 라운드 must-fix 의 해소 확인'이라 리뷰 라운드 시드 프리필과
    같은 시야여야 한다([[T-0749]] F-007).
    """
    conf = {"additional_reviewer.rounds_max": "9"}
    _seed_board(tmp_path)
    calls = _wire(external, monkeypatch, tmp_path,
                  _reject_reply(_finding("X-001")), conf=conf)
    assert _run(external, tmp_path, "--ticket", TICKET) == 1
    assert pd.render_pm_review_block_skeleton(ROLE, []) in calls["prompt"]

    calls = _wire(external, monkeypatch, tmp_path,
                  _reject_reply(_finding("X-002")), conf=conf)
    assert _run(external, tmp_path, "--ticket", TICKET) == 1
    assert pd.render_pm_review_block_skeleton(ROLE, ["X-001"]) in calls["prompt"]

    # 직전 라운드(X-002)만 공급원이다 — 그 앞 라운드의 ID 는 확인 대상이 아니다.
    calls = _wire(external, monkeypatch, tmp_path,
                  _reject_reply(_finding("X-003")), conf=conf)
    assert _run(external, tmp_path, "--ticket", TICKET) == 1
    assert pd.render_pm_review_block_skeleton(ROLE, ["X-002"]) in calls["prompt"]

    # PM 이 rejected 로 판정한 ID 는 확인 대상에서 빠진다(리뷰 라운드 시드와 같은 배제).
    _append_spec(tmp_path, _disposition("X-003", ordinal=3, decision="rejected"))
    calls = _wire(external, monkeypatch, tmp_path,
                  _reject_reply(_finding("X-004")), conf=conf)
    assert _run(external, tmp_path, "--ticket", TICKET) == 1
    assert pd.render_pm_review_block_skeleton(ROLE, []) in calls["prompt"]


def test_prompt_skeleton_ignores_a_reserved_round_without_output(
    external, pd, monkeypatch, tmp_path,
):
    """예약만 해 둔 시드 라운드는 직전 산출이 아니다 — 확인 대상이 빈 목록이 되면 안 된다.

    PM 이 `section-add --role external-reviewer` 로 자리만 잡아 둔 라운드가 '직전 라운드'
    자리를 차지하면 `--confirm-fix` 가 '확인 대상이 판정 표면에 없습니다'로 막힌다.
    """
    conf = {"additional_reviewer.rounds_max": "9"}
    spec = _seed_board(tmp_path)
    _wire(external, monkeypatch, tmp_path, _reject_reply(_finding("X-001")), conf=conf)
    assert _run(external, tmp_path, "--ticket", TICKET) == 1

    rounds_module = pd._load_ticket_rounds()
    reserved = _write_round(
        tmp_path, 2, ROLE,
        rounds_module.render_round_seed(
            ROLE, spec.read_text(encoding="utf-8"), today="2026-08-18",
        ),
    )
    assert [item.pending for item in _rounds(pd, tmp_path)] == [False, True]

    calls = _wire(external, monkeypatch, tmp_path,
                  _reject_reply(_finding("X-002")), conf=conf)
    assert _run(external, tmp_path, "--ticket", TICKET) == 1
    assert pd.render_pm_review_block_skeleton(ROLE, ["X-001"]) in calls["prompt"]
    assert reserved.exists()


def test_board_commit_seam_is_called_directly_and_is_loud_when_it_is_missing(
    external, monkeypatch, tmp_path,
):
    """부분 동기 사본에서 커밋만 조용히 빠진 rc0 을 만들지 않는다 (이름 폴백 없음)."""
    source = EXTERNAL_REVIEW.read_text(encoding="utf-8")
    assert "board._rounds_mutation_sync_paths(" in source
    assert "_growth_mutation_sync_paths" not in source
    assert 'getattr(board, "_rounds_mutation_sync_paths"' not in source

    _seed_board(tmp_path)
    real_loader = external._load_board

    def _loader():
        board = real_loader()
        del board._rounds_mutation_sync_paths     # 이름이 갈린 사본 재현
        return board

    monkeypatch.setattr(external, "_load_board", _loader)
    _wire(external, monkeypatch, tmp_path, _reject_reply(_finding("X-001")))

    with pytest.raises(AttributeError):
        _run(external, tmp_path, "--ticket", TICKET)


# ── F-025 강등 안내·지연 해소 ───────────────────────────────────────────


def test_degraded_confirmable_ids_names_both_consumers(
    external, pd, monkeypatch, tmp_path, capsys,
):
    """(F-025) 해소 실패 안내는 두 소비자를 함께 말한다 — 골격 실값 · 확인 근거 표면 대조."""
    _seed_board(tmp_path)
    _write_round(
        tmp_path, 1, ROLE,
        _external_round_text(pd, _payload([_finding("X-001")])),
    )
    calls = _wire(external, monkeypatch, tmp_path, _reject_reply(_finding("X-002")))

    def _raise(*_args, **_kwargs):
        raise pd.DelegateError("확인 목록 해소 실패 probe")

    monkeypatch.setattr(pd, "collect_confirmable_finding_ids", _raise)
    monkeypatch.setattr(external, "_load_pm_delegate", lambda: pd)

    assert _run(external, tmp_path, "--ticket", TICKET) == 1
    err = capsys.readouterr().err
    assert "골격" in err and "장부 기록 그대로" in err
    # 해소가 꺼져도 라운드는 돈다 — 골격은 placeholder 로 강등된다.
    assert pd.render_pm_review_block_skeleton(ROLE) in calls["prompt"]


def test_a_disabled_run_does_not_read_the_harvest_target(
    external, monkeypatch, tmp_path,
):
    """(F-025) 비활성 no-op 은 티켓을 읽지 않는다 — 해소는 소비 지점에서만 일어난다."""
    _seed_board(tmp_path)
    _wire(external, monkeypatch, tmp_path, _reject_reply(_finding("X-001")),
          conf={**_REVIEWER_TARGET, "additional_reviewer.enabled": "false"})
    reads: list[str] = []
    monkeypatch.setattr(
        external, "_harvest_target_ticket_state",
        lambda *args, **kwargs: reads.append("read") or None,
    )

    assert _run(external, tmp_path, "--ticket", TICKET) == 0     # no-op
    assert reads == []


# ── F-005 개행: 회신 bytes 를 그대로 보존한다 ────────────────────────────


def test_crlf_reply_keeps_its_bytes_in_the_round_file(
    external, pd, monkeypatch, tmp_path,
):
    """CRLF 회신은 `\\r\\r\\n` 없이 원문 그대로 라운드 파일에 남는다(산출 bytes 보존)."""
    _seed_board(tmp_path)
    reply = _reject_reply(_finding()).replace("\n", "\r\n")
    _wire(external, monkeypatch, tmp_path, reply)

    assert _run(external, tmp_path, "--ticket", TICKET) == 1
    raw = _round_paths(tmp_path)[0].read_bytes()
    assert b"\r\r\n" not in raw
    assert raw.endswith(reply.encode("utf-8"))
    delta = _delta(pd, tmp_path, _disposition("X-001"))
    assert [finding.id for finding, _row in delta.accepted] == ["X-001"]


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
    external, monkeypatch, tmp_path,
):
    """PM 홈에 stale board 사본이 있어도 회수는 이 실행의 형제 엔진으로 쓴다."""
    _seed_board(tmp_path)
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
    assert len(_round_paths(tmp_path)) == 1


def test_engine_copy_skew_becomes_a_harvest_problem_with_a_resync_prescription(
    external, monkeypatch, tmp_path, capsys,
):
    """사본 불일치는 traceback 이 아니라 회수 실패 처방(재동기·rc≠0)으로 접힌다."""
    _seed_board(tmp_path)
    _wire(external, monkeypatch, tmp_path, _reject_reply(_finding()))

    _skew_delegate_after_prompt(external, monkeypatch, marked=True)
    assert _run(external, tmp_path, "--ticket", TICKET) == 1
    err = capsys.readouterr().err
    assert "회수 문제" in err and "재동기" in err
    assert _round_paths(tmp_path) == []


def test_rounds_seam_error_becomes_a_harvest_problem_not_a_traceback(
    external, pd, monkeypatch, tmp_path, capsys,
):
    """라운드 규약 위반(`RoundsError`)은 회수 실패 처방으로 접힌다 — 사본 skew 절이 먼저 받으면
    같은 오류가 traceback 으로 나간다(`RoundsError` 는 `RuntimeError` 하위형이다)."""
    _seed_board(tmp_path)
    rounds_module = pd._load_ticket_rounds()

    def _raise(*_args, **_kwargs):
        raise rounds_module.RoundsError("라운드 예약 충돌 probe")

    monkeypatch.setattr(rounds_module, "reserve_round", _raise)
    monkeypatch.setattr(external, "_load_ticket_rounds", lambda: rounds_module)
    monkeypatch.setattr(pd, "_load_ticket_rounds", lambda: rounds_module)
    monkeypatch.setattr(external, "_load_pm_delegate", lambda: pd)
    _wire(external, monkeypatch, tmp_path, _reject_reply(_finding()))

    assert _run(external, tmp_path, "--ticket", TICKET) == 1
    err = capsys.readouterr().err
    assert "회수 문제" in err and "라운드 예약 충돌 probe" in err
    assert _round_paths(tmp_path) == []


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
