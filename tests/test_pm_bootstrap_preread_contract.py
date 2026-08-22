"""출하 진입문서의 lean bootstrap 사전 읽기 계약 대조 가드."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from _harness_matrix import HARNESSES, TEMPLATES, _PM_IMPORT, entry_docs


REPO = Path(__file__).resolve().parents[1]
ROLE_DOC = REPO / ".project_manager" / "wiki" / "pm_role.md"
ENTRY_DOCS = tuple(
    path
    for harness in HARNESSES
    for relative in entry_docs(harness)
    if (
        path := (
            TEMPLATES
            / _PM_IMPORT.HARNESS_TEMPLATE_DIRS[harness][0]
            / relative
        )
    ).is_file()
)

def _bootstrap_cards() -> tuple[Path, ...]:
    """출하 `pm-bootstrap` 스킬 카드 전수 — 카드 구조 가드의 표면 열거를 재사용한다.

    카드는 **에이전트가 부트스트랩 때 실제로 읽는 문서**라 계약 대조의 1급 대상이다.
    표면 목록을 여기서 다시 나열하면 하네스 추가 시 갈리므로(이 repo 의 반복 결함 패턴)
    이미 있는 열거기를 소비한다.
    """
    from test_skill_card_structure import _skill_cards

    cards = tuple(
        card for card in _skill_cards(REPO)
        if card.parent.name == "pm-bootstrap"
    )
    assert cards, "pm-bootstrap 카드를 하나도 못 찾았다 — 표면 열거가 깨졌다(vacuous 방지)"
    return cards


CONTRACT_DOCS = (ROLE_DOC, *ENTRY_DOCS, *_bootstrap_cards())

START = "<!-- pm-bootstrap-preread:start -->"
END = "<!-- pm-bootstrap-preread:end -->"
CONTRACT_SENTENCE = (
    "세션 시작 필독 셋은 이미 로드된 진입문서, 현재 정체성의 `pm_state`, "
    "`pm-bootstrap` dump 한 번뿐이다."
)
FORBIDDEN_PRELOADS = (
    "architecture.md",
    "status.md",
    "decisions/",
    "roadmap.md",
    "board.py list",
    "pm_log.py tail",
)
REQUIRED_LEAN_SIGNALS = (
    "진입문서",
    "pm_state",
    "pm-bootstrap",
)
CIRCLED_NUMERAL = re.compile(r"[\u2460-\u2473\u3251-\u325f]")
MARKDOWN_HEADING = re.compile(r"^#{1,6}\s+\S", re.MULTILINE)
MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
ALLOWED_FORBIDDEN_PASSAGES = (
    (
        "architecture.md·status.md·decisions/·roadmap.md·전체 보드·타 슬롯 log는 "
        "시작 시 통독하지 않고 실제 필요가 생길 때 해당 절만 읽는다."
    ),
    (
        "architecture.md·status.md·decisions/·roadmap.md·전체 보드·타 슬롯 log는 "
        "시작 시 통독하지 않고, 실제 필요가 생길 때 §찾아가는 법에 따라 해당 절만 읽는다."
    ),
    (
        "현재 진실: architecture.md는 현재 아키텍처 단일 진실이며, 옛 ADR 또는 현재 "
        "의도·실측과 충돌하면 기준으로 따른다."
    ),
    (
        "architecture.md는 현재 아키텍처 단일 진실이며, 옛 ADR 또는 현재 의도·실측과 "
        "충돌하면 기준으로 따른다."
    ),
    "architecture.md가 현재 아키텍처 단일 진실이다.",
    (
        "옛 ADR과 현재 의도/실측이 충돌하면 architecture.md를 따르고, architect가 "
        "architecture 갱신과 ADR amend/supersede를 한다."
    ),
    (
        "architecture.md·status.md content-truth(구조·구현상태 판정·비고)는 architect가 "
        "유지하고 PM은 점검한다."
    ),
    (
        "- slot 모드: 내 티켓은 board.py list --mine, 진행/남은작업은 per-slot pm_state.md, "
        "연속성은 자기 슬롯 태그 handoff entry. 자기 공간만 관리한다."
    ),
    (
        "- 공유: 타 PM은 부트스트랩 대시보드 slot 1줄만 본다. log/current.md는 필요한 슬롯 "
        "태그 entry만 검색하고 평시 통독하지 않는다. 전체 보드 board.py list --all은 "
        "열람용이며 무인자 기본 뷰는 내 스트림이다. 슬롯이 하나뿐이면 대시보드에 내 슬롯 "
        "1줄만 뜬다."
    ),
    "decisions/ ADR은 *왜*의 히스토리이며 현재 구속력이 없다.",
)


def _extract_contract(text: str, *, label: str) -> str:
    assert text.count(START) == 1, f"{label}: contract start marker가 정확히 1개여야 한다"
    assert text.count(END) == 1, f"{label}: contract end marker가 정확히 1개여야 한다"
    start = text.index(START) + len(START)
    end = text.index(END)
    assert start < end, f"{label}: contract marker 순서가 뒤집혔다"
    block = text[start:end].strip()
    assert block, f"{label}: contract block이 비었다"
    return block


def _extract_contract_section(text: str, *, label: str) -> str:
    """marker가 속한 Markdown heading부터 바로 다음 heading 전까지 반환한다."""
    marker_at = text.index(START)
    headings = list(MARKDOWN_HEADING.finditer(text))
    previous = [heading for heading in headings if heading.start() < marker_at]
    assert previous, f"{label}: contract marker 앞 Markdown heading이 없다"
    section_start = previous[-1].start()
    following = [heading for heading in headings if heading.start() > marker_at]
    section_end = following[0].start() if following else len(text)
    section = text[section_start:section_end]
    assert END in section, f"{label}: contract end marker가 같은 Markdown 섹션에 없다"
    return section


def _normalize_contract_prose(text: str) -> str:
    text = MARKDOWN_LINK.sub(r"\1", text)
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)
    text = text.replace("`", "").replace("**", "")
    return " ".join(text.split())


def _assert_no_forbidden_section_preloads(text: str, *, label: str) -> None:
    section = _normalize_contract_prose(
        _extract_contract_section(text, label=label)
    )
    for passage in ALLOWED_FORBIDDEN_PASSAGES:
        section = section.replace(passage, "")
    for preload in FORBIDDEN_PRELOADS:
        assert preload not in section, (
            f"{label}: bootstrap 섹션에 시작 시 과잉 읽기 재도입: {preload}"
        )


def _assert_lean_contract(path: Path) -> None:
    assert path.is_file(), f"계약 대조 대상 누락: {path}"
    text = path.read_text(encoding="utf-8")
    block = _extract_contract(text, label=str(path))
    block = re.sub(r"(?<![A-Za-z0-9_.>/])[/\$](pm-bootstrap)", r"\1", block)
    assert CONTRACT_SENTENCE in block, f"{path}: lean 필독 셋 선언 불일치"
    for signal in REQUIRED_LEAN_SIGNALS:
        assert signal in block, f"{path}: lean 필독 신호 누락: {signal}"
    _assert_no_forbidden_section_preloads(text, label=str(path))


def test_contract_target_discovery_is_non_vacuous_and_complete():
    """하네스 목록·실제 파일에서 진입문서 셋을 파생하며 빈 목록은 loud fail한다."""
    assert len(HARNESSES) == 3, f"예상한 출하 하네스 3종과 다르다: {HARNESSES!r}"
    assert len(ENTRY_DOCS) >= len(HARNESSES)
    assert sum(path.name.endswith(".lite.md") for path in ENTRY_DOCS) == 2, (
        "현재 출하 lite 진입문서 2종이 계약 대상에서 빠졌다"
    )
    assert len(set(ENTRY_DOCS)) == len(ENTRY_DOCS)
    assert all(path.is_file() for path in CONTRACT_DOCS)


def test_forbidden_preload_allowlist_is_used_and_exact():
    """허용구문이 전부 실 섹션에 존재해야 stale/broad allowlist가 조용히 남지 않는다."""
    sections = [
        _normalize_contract_prose(
            _extract_contract_section(
                path.read_text(encoding="utf-8"),
                label=str(path),
            )
        )
        for path in CONTRACT_DOCS
    ]
    for passage in ALLOWED_FORBIDDEN_PASSAGES:
        assert any(passage in section for section in sections), (
            f"실 계약 섹션 어디에도 없는 stale allowlist 구문: {passage}"
        )


@pytest.mark.parametrize(
    "path",
    CONTRACT_DOCS,
    ids=lambda path: str(path.relative_to(REPO)),
)
def test_role_and_entry_docs_share_lean_preread_contract(path):
    _assert_lean_contract(path)


@pytest.mark.parametrize(
    "path",
    ENTRY_DOCS,
    ids=lambda path: str(path.relative_to(REPO)),
)
def test_entry_docs_keep_architecture_authority_but_defer_bulk_read(path):
    text = path.read_text(encoding="utf-8")
    prose = " ".join(text.split())
    assert "시작 시 통독하지 않고" in prose
    assert "필요" in prose and "해당 절만 읽는다" in prose
    assert "architecture.md" in prose
    assert "현재 아키텍처 단일 진실" in prose
    assert "충돌하면 기준으로 따른다" in prose


@pytest.mark.parametrize(
    "path",
    ENTRY_DOCS,
    ids=lambda path: str(path.relative_to(REPO)),
)
def test_shipped_entry_docs_have_no_circled_numerals(path):
    assert not CIRCLED_NUMERAL.search(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "source_path",
    CONTRACT_DOCS,
    ids=lambda path: str(path.relative_to(REPO)),
)
def test_contract_guard_sensitivity_detects_each_single_doc_drift(tmp_path, source_path):
    """marker 밖에 옛 목록 한 줄을 되살려도 각 문서에서 같은 가드가 red가 된다."""
    mutated = tmp_path / source_path.name
    text = source_path.read_text(encoding="utf-8")
    text = text.replace(
        END,
        END + "\n3. `architecture.md`를 먼저 읽는다(부트스트랩 1순위).",
        1,
    )
    mutated.write_text(text, encoding="utf-8")
    with pytest.raises(AssertionError, match="bootstrap 섹션에 시작 시 과잉 읽기 재도입"):
        _assert_lean_contract(mutated)
