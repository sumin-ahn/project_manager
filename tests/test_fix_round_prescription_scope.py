"""fix 라운드 delta 꼬리 제약 블록 — 부착·복제 0·전달 지시 파리티 문면 가드."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
PM_DELEGATE = REPO / ".project_manager" / "tools" / "pm_delegate.py"

SKILL_SURFACES = (
    REPO / ".claude/skills/pm-dev-delegate/SKILL.md",
    REPO / "templates/claude_code/.claude/skills/pm-dev-delegate/SKILL.md",
    REPO / "templates/codex/.agents/skills/pm-dev-delegate/SKILL.md",
    REPO / "templates/opencode/.claude/skills/pm-dev-delegate/SKILL.md",
    REPO / "templates/opencode/.opencode/command/pm-dev-delegate.md",
)
DEVELOPER_CARD_SURFACES = (
    REPO / ".claude/agents/developer.md",
    REPO / ".claude/agents/developer-hard.md",
    REPO / "templates/claude_code/.claude/agents/developer.md",
    REPO / "templates/claude_code/.claude/agents/developer-hard.md",
    REPO / "templates/codex/.codex/agents/developer.toml",
    REPO / "templates/codex/.codex/agents/developer-hard.toml",
    REPO / "templates/opencode/.opencode/agents/developer.md",
    REPO / "templates/opencode/.opencode/agents/developer-hard.md",
)
PLAYBOOK_SURFACES = (
    REPO / ".project_manager/wiki/pm_playbook.md",
    REPO / "templates/claude_code/.project_manager/wiki/pm_playbook.md",
    REPO / "templates/codex/.project_manager/wiki/pm_playbook.md",
    REPO / "templates/opencode/.project_manager/wiki/pm_playbook.md",
)
PM_ROLE_SURFACES = (
    REPO / ".project_manager/wiki/pm_role.md",
    REPO / "templates/claude_code/.project_manager/wiki/pm_role.md",
    REPO / "templates/codex/.project_manager/wiki/pm_role.md",
    REPO / "templates/opencode/.project_manager/wiki/pm_role.md",
)
# 복제 0 가드 대상 18본 — 렌더러 상수가 단일 진실이고 이 표면들은 "그대로 전달" 지시만 갖는다.
DUPLICATE_CHECK_SURFACES = (
    SKILL_SURFACES + DEVELOPER_CARD_SURFACES + PLAYBOOK_SURFACES + PM_ROLE_SURFACES
)

# 값을 재는 축(민감도) — 짧은 줄은 우연 일치가 잦아 상수 진단력이 없다.
MIN_DUPLICATE_LINE_LENGTH = 10

# 제약 블록이 실어야 하는 개념 — 상수를 약한 문장으로 갈아치우면 여기서 red.
CONTENT_CONCEPTS = {
    "scope-limited": (("finding ID",), ("허용 수정 범위",)),
    "outside-forbidden": (("그 밖",), ("건드리지 않는다",)),
    "gap-report-then-stop": (("빈틈",), ("멈추고",), ("라운드 파일",), ("종료한다",)),
    "success-declared": (("정상 산출",), ("성공 종료",)),
    "report-format-4-items": (("대상:",), ("빈틈:",), ("충돌:",), ("대안:",)),
}

# 스킬이 delta 출력을 발췌 없이 그대로 전달하라는 지시를 갖는지 — 문장 복제가 아니라 개념 보유.
DELIVERY_CONCEPTS = (
    (("발췌",), ("말고", "않")),
    (("그대로",),),
    (("전달",),),
    (("제약 블록",), ("포함",)),
)


def _load_pd():
    spec = importlib.util.spec_from_file_location("pm_delegate_fix_scope", PM_DELEGATE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def pd():
    return _load_pd()


def _finding(pd_module, finding_id: str = "F-001") -> "pd_module.PMReviewFinding":
    return pd_module.PMReviewFinding(
        id=finding_id,
        classification="implementation-defect",
        authority="사양 §허용 범위",
        evidence=f"{finding_id} probe rc=1",
        recommendation=f"{finding_id}만 수정",
        design_change=False,
        reviewer_ordinal=1,
        severity="must-fix",
        reviewer_role="code-reviewer",
    )


def _disposition(pd_module, finding_id: str = "F-001") -> "pd_module.PMReviewDisposition":
    return pd_module.PMReviewDisposition(
        id=finding_id,
        decision="accepted",
        reason=f"PM {finding_id} 승인 근거",
        scope=f"{finding_id} 허용 범위",
        prerequisite="",
        reviewer_ordinal=1,
        reviewer_role="code-reviewer",
    )


def _delta(pd_module, pairs, *, finding_zero: bool = False):
    return pd_module.PMReviewDelta(accepted=tuple(pairs), finding_zero=finding_zero)


# --- 1. 부착 픽스처 단언 -----------------------------------------------------


def test_notice_attaches_once_at_tail_for_accepted_delta(pd):
    finding = _finding(pd)
    disposition = _disposition(pd)
    delta = _delta(pd, [(finding, disposition)])

    rendered = pd.render_pm_review_delta("T-TEST", delta)

    assert rendered.startswith("## PM 승인 리뷰 delta — T-TEST")
    assert pd.PM_REVIEW_FIX_SCOPE_NOTICE in rendered
    assert rendered.count(pd.PM_REVIEW_FIX_SCOPE_NOTICE) == 1
    assert rendered.rstrip().endswith(pd.PM_REVIEW_FIX_SCOPE_NOTICE.rstrip())
    finding_index = rendered.index(f"### {finding.id}")
    notice_index = rendered.index(pd.PM_REVIEW_FIX_SCOPE_NOTICE)
    assert finding_index < notice_index


# --- 2. finding 다건 — 블록은 항목마다 반복되지 않는다 -----------------------


def test_notice_attaches_once_regardless_of_finding_count(pd):
    pairs = [
        (_finding(pd, "F-001"), _disposition(pd, "F-001")),
        (_finding(pd, "F-002"), _disposition(pd, "F-002")),
    ]
    delta = _delta(pd, pairs)

    rendered = pd.render_pm_review_delta("T-TEST", delta)

    assert "F-001" in rendered and "F-002" in rendered
    assert rendered.count(pd.PM_REVIEW_FIX_SCOPE_NOTICE) == 1


# --- 3. 빈 출력 3경로 — accepted 0 · finding-zero · confirmation resolved 소진 --


@pytest.mark.parametrize("finding_zero", [False, True])
def test_empty_accepted_delta_stays_empty(pd, finding_zero):
    delta = _delta(pd, [], finding_zero=finding_zero)
    assert pd.render_pm_review_delta("T-TEST", delta) == ""


def test_confirmation_resolved_exhaustion_stays_empty(pd):
    """accepted finding 이 이후 라운드의 resolved 확인으로 소진되면 delta.accepted 에서 빠진다.

    이 경로는 렌더러 자체가 아니라 delta 계산(`parse_pm_review_delta`)에서 결정되므로,
    렌더 축과 별개로 실 파서 파이프라인을 태워야 "블록을 무조건 붙이는" 회귀를 못 본다.
    """
    rounds_module = pd._load_ticket_rounds()

    def round_(ordinal: int, text: str):
        return rounds_module.Round(
            ordinal=ordinal, role="code-reviewer",
            path=Path(rounds_module.round_filename(ordinal, "code-reviewer")),
            text=text, pending=False,
        )

    def review_round(ordinal: int, payload: dict) -> "rounds_module.Round":
        body = (
            "## 리뷰 (code-reviewer · 2026-08-21)\n\n판정: 반려\n\n## must-fix\n"
            "- 구조화 finding 참조\n\n```pm-review-v1\n"
            + json.dumps(payload, ensure_ascii=False, indent=2) + "\n```\n"
        )
        return round_(ordinal, body)

    first = review_round(1, {
        "version": pd.PM_REVIEW_VERSION,
        "findings": [{
            "id": "F-001", "class": "implementation-defect", "severity": "must-fix",
            "authority": "사양 §허용 범위", "evidence": "F-001 probe rc=1",
            "recommendation": "F-001만 수정",
            "fix_contract": {
                "location": ".project_manager/tools/pm_delegate.py:parse_pm_review_delta",
                "failure": "확인으로 해소된 finding이 accepted delta에 남는다.",
                "design": "후속 resolved 확인을 소진 처리한다.",
                "test": "tests/test_fix_round_prescription_scope.py의 delta 소진 회귀",
                "command": "python3 -m pytest tests/test_fix_round_prescription_scope.py -q",
                "expected": "passed",
            },
            "design_change": False,
        }],
        "confirmations": [],
    })
    resolved = review_round(2, {
        "version": pd.PM_REVIEW_VERSION,
        "findings": [],
        "confirmations": [
            {"id": "F-001", "status": "resolved", "evidence": "F-001 probe rc=0"},
        ],
    })
    spec = "\n```pm-review-disposition-v1\n" + json.dumps({
        "version": pd.PM_REVIEW_DISPOSITION_VERSION,
        "reviewer_ordinal": 1,
        "dispositions": [{
            "id": "F-001", "decision": "accepted", "reason": "PM F-001 승인 근거",
            "scope": "F-001 허용 범위", "prerequisite": "",
        }],
    }, ensure_ascii=False, indent=2) + "\n```\n"

    delta = pd.parse_pm_review_delta(spec, [first, resolved])

    assert delta.accepted == ()
    assert pd.render_pm_review_delta("T-TEST", delta) == ""


# --- 4. 내용 계약(비-vacuous) — 개념 토큰 -------------------------------------


def test_notice_content_holds_scope_gap_success_report_concepts(pd):
    notice = pd.PM_REVIEW_FIX_SCOPE_NOTICE
    missing = [
        name for name, groups in CONTENT_CONCEPTS.items()
        if not all(any(term in notice for term in alternatives) for alternatives in groups)
    ]
    assert not missing, f"제약 블록 개념 누락: {missing}"


# --- 5. 복제 0 가드 -----------------------------------------------------------


def _normalize_line(line: str) -> str:
    stripped = line.strip()
    if stripped.startswith("- "):
        stripped = stripped[2:].strip()
    return stripped


def _notice_lines(pd_module) -> list[str]:
    return [
        normalized
        for normalized in (
            _normalize_line(line) for line in pd_module.PM_REVIEW_FIX_SCOPE_NOTICE.splitlines()
        )
        if len(normalized) >= MIN_DUPLICATE_LINE_LENGTH
    ]


def _duplicate_lines_in(text: str, lines: list[str]) -> list[str]:
    return [line for line in lines if line in text]


@pytest.mark.parametrize(
    "path", DUPLICATE_CHECK_SURFACES, ids=lambda path: str(path.relative_to(REPO)),
)
def test_notice_text_not_duplicated_into_delivery_surfaces(pd, path):
    assert path.is_file(), f"전달 표면 없음: {path}"
    text = path.read_text(encoding="utf-8")
    duplicates = _duplicate_lines_in(text, _notice_lines(pd))
    assert not duplicates, f"{path}: 상수 줄 복제 {duplicates}"


def test_duplicate_guard_detects_injected_copy(pd, tmp_path):
    """민감도 probe — 존재가 아니라 값을 재는 축임을 확인한다."""
    lines = _notice_lines(pd)
    assert lines, "가드 대상 줄이 비었습니다"
    injected = tmp_path / "injected.md"
    injected.write_text(f"임의 문서\n{lines[0]}\n", encoding="utf-8")

    duplicates = _duplicate_lines_in(injected.read_text(encoding="utf-8"), lines)

    assert duplicates, "민감도 probe: 주입 사본이 red 를 내지 못함"


# --- 6. 전달 지시 파리티 ------------------------------------------------------


@pytest.mark.parametrize("path", SKILL_SURFACES, ids=lambda path: str(path.relative_to(REPO)))
def test_skill_surfaces_instruct_verbatim_delta_delivery(pd, path):
    del pd
    assert path.is_file(), f"스킬 표면 없음: {path}"
    text = path.read_text(encoding="utf-8")
    missing = [
        idx for idx, groups in enumerate(DELIVERY_CONCEPTS)
        if not all(any(term in text for term in alternatives) for alternatives in groups)
    ]
    assert not missing, f"{path}: 전달 지시 개념 누락 {missing}"
