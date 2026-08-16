"""스킬-우선 운영 규율 durable 가드 (T-0281 · ADR-0052).

[[ADR-0052]] 규율(PM wave 운영은 스킬로 invoke·backbone CLI 직접호출 금지)을 문구 rot·pointer
dangling·**카드↔pm_role 카탈로그 drift** 로부터 지키는 결정론적 가드다. 특히 #3 은 T-0280 codex
게이트가 2 라운드에 걸쳐 손으로 잡던 카드↔카탈로그 불일치 클래스(external_review 오귀속·facade
우회·sub-op 누락 등)를 LLM whack-a-mole 대신 상시 회귀로 닫는 **class-closer** 다.

검사 소스 두 개 — 둘 다 살아있어 어느 쪽이 drift 해도 fail 한다:
  - **카드**(코드 생성): `pm_bootstrap._build_command_card_markdown(identity)` 렌더.
  - **pm_role 카탈로그**(문서): `wiki/pm_role.md` 의 skill 카탈로그 표(2개·같은 헤더).

엔진 canonical(① worktree `.project_manager/tools/*.py`)을 importlib 로 로드해 카드를 순수 함수로
렌더하고, pm_role.md 는 정적 텍스트로 읽는다(무거운 런타임·실 자산 mutation 0). 4 단언:

  1. pm_role 규율 문구 존재(prose rot 가드) — 규율 섹션 + 3요소 키워드.
  2. 카드 pointer → pm_role 섹션 정합(dangling 방지).
  3. **카드 강등 엔진 ↔ pm_role 카탈로그 정합(CLASS-CLOSER)** — 카탈로그를 파싱해 각 skill→엔진을
     뽑고, 렌더 카드의 강등 backbone 과 양방향(exact-set) 대조. non-vacuous(≥5 skill 실검사).
  4. 카드 스킬-우선 대표 백스톱 — 대표 op 에서 `/pm-…` 스킬이 강등 backbone 보다 먼저(primary).

T-0280 의 카드-구조 test(`test_pm_bootstrap_card.py`)와 비중복: 거긴 카드 내부 구조/불변식,
여긴 카드↔pm_role *두 소스 정합* + 규율 자산 존재(rot).
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
PM_ROLE = REPO / ".project_manager" / "wiki" / "pm_role.md"

# 규율 섹션 헤더(ADR-0052·T-0279) + 카드가 가리키는 pointer 앵커 문구(T-0280).
_DISCIPLINE_HEADER = "## 스킬 우선 운영 규율 (backbone 직접호출 금지)"
_POINTER_ANCHOR = "pm_role §스킬 우선 운영 규율"

# 카탈로그 표 헤더(2 표 공유) — 이 줄 아래 row 들이 skill→엔진 매핑이다.
_CATALOG_HEADER = "| skill | 역할 | 감싸는 내부 엔진 (직접호출 금지) |"

# tools/*.py canonical 도구 이름 집합(자기유지 — glob). 카탈로그 셀에서 이 토큰이 있으면 CLI 엔진.
KNOWN_TOOLS = frozenset(p.name for p in TOOLS.glob("*.py"))


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def bootstrap():
    return _load("pm_bootstrap")


# lean(멀티-PM) identity — 카드에 정체성 실값을 채우는 형태(test_pm_bootstrap_card 와 동형).
LEAN_IDENTITY = {
    "repo": "project_manager",
    "session": "project_manager_1",
    "slot": "work/project_manager_1",
    "slot_path": "/home/x/work/project_manager_1",
    "branch": "release/v1.1.0",
    "others": [],
    "protected_branch": None,
}


def _render_card(bootstrap, identity) -> str:
    """PmBootstrap 인스턴스 없이 카드 헬퍼만 호출한다(순수 함수·I/O 0)."""
    inst = bootstrap.PmBootstrap.__new__(bootstrap.PmBootstrap)
    return inst._build_command_card_markdown(identity)


def _pm_role_text() -> str:
    return PM_ROLE.read_text(encoding="utf-8")


# ── 카탈로그 파싱 ─────────────────────────────────────────────────────────────


def _section_body(text: str, header_line: str) -> str | None:
    """`## <header>` 섹션의 본문(다음 `## ` 헤더 전까지)을 돌려준다(없으면 None)."""
    lines = text.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if ln.strip() == header_line:
            start = i
            break
    if start is None:
        return None
    body: list[str] = []
    for ln in lines[start + 1:]:
        if ln.startswith("## "):
            break
        body.append(ln)
    return "\n".join(body)


def _engine_tokens_from_cell(cell: str) -> set[str]:
    """카탈로그 엔진 셀 → 기대 강등 엔진 토큰 집합(`<tool> <sub-op>` 또는 `<tool>`).

    분류(T-0280 확립·ADR-0052):
      - **facade 셸**(`pm-config.sh`·`pm-update.sh` 등 `pm-*.sh`): 엔진이 facade 뒤라 카드는
        raw `python3 tools/*.py` 강등을 지어내지 않는다(skill-only) → 빈 집합. (`.sh` 우선 —
        셀에 `pm_config.py`/`pm_update.py` 가 딸려 있어도 facade 경유라 raw 강등 아님.)
      - **CLI 엔진**(tools/*.py 토큰): 그 도구 + `/`-joined sub-op 을 토큰화(예 `board.py
        show/lint/claim` → {board.py show, board.py lint, board.py claim}).
      - **비-CLI**(`Agent 툴` 등 tools/*.py 없음): 빈 집합(skill-only).
    """
    plain = cell.replace("`", "")
    if re.search(r"pm-[\w-]+\.sh", plain):  # facade → skill-only
        return set()
    tokens: set[str] = set()
    for tool in KNOWN_TOOLS:
        if tool not in plain:
            continue
        # 도구명 뒤에 선택적으로 `/`-joined sub-op 워드가 붙는다(없으면 도구 단독).
        for m in re.finditer(re.escape(tool) + r"(?:\s+([a-z][a-z/]*))?", plain):
            subs = m.group(1)
            if subs:
                for sub in subs.split("/"):
                    tokens.add(f"{tool} {sub}")
            else:
                tokens.add(tool)
    return tokens


def _parse_catalog(text: str) -> dict[str, set[str]]:
    """pm_role.md 의 skill 카탈로그 표(2개·같은 헤더)를 파싱해 {skill_token: engine_tokens}.

    escaped pipe(`\\|`·예 `developer\\|code-reviewer`)는 셀 분리자로 오인하지 않는다.
    """
    catalog: dict[str, set[str]] = {}
    in_table = False
    for raw in text.splitlines():
        s = raw.strip()
        if s == _CATALOG_HEADER:
            in_table = True
            continue
        if not in_table:
            continue
        if s.startswith("|--") or s.startswith("| --"):
            continue  # 구분선
        if not s.startswith("|"):
            in_table = False  # 표 종료
            continue
        # unescaped `|` 로만 셀 분리(escaped `\|` 는 셀 내부 리터럴 보존).
        cells = [c.strip() for c in re.split(r"(?<!\\)\|", s)]
        # 형태: ['', skill, 역할, engine, ''] → 최소 5 파트.
        if len(cells) < 5:
            continue
        skill_cell, engine_cell = cells[1], cells[3]
        m = re.search(r"(?<![\w-])(?P<entry>[/\$]pm-[\w-]+)", skill_cell)
        if not m:
            continue
        catalog[m.group("entry")] = _engine_tokens_from_cell(engine_cell)
    return catalog


# ── 카드 파싱 ─────────────────────────────────────────────────────────────────

# 강등 backbone 줄 = engine() 이 2-space 들여쓴 `python3 …/tools/<tool>` 줄. 직접 sibling
# (external_review 등 cmd() 산출)은 들여쓰기가 없어 제외된다 — 이 구분이 must-fix #1
# (external_review 오귀속) 클래스를 닫는다.
_DEMOTION_RE = re.compile(
    r"(?:python3|py -3(?:\.\d+)?) \.project_manager/tools/(\S+\.py)(.*)"
)


def _card_skill_block(card_lines: list[str], skill_token: str) -> list[str] | None:
    """카드에서 `skill_token` 스킬 줄 다음부터 그 skill 의 block 을 돌려준다(없으면 None).

    block = skill 줄 다음부터 다음 `/pm-…` 스킬 줄 또는 다음 `# ` 섹션 헤더 전까지.
    """
    start = None
    for i, ln in enumerate(card_lines):
        s = ln.strip()
        if s == skill_token or s.startswith(skill_token + " "):
            start = i
            break
    if start is None:
        return None
    block: list[str] = []
    for ln in card_lines[start + 1:]:
        s = ln.strip()
        if s.startswith("/pm-") or s.startswith("# "):
            break
        block.append(ln)
    return block


def _demotion_engine_tokens(block_lines: list[str]) -> set[str]:
    """block 의 강등 backbone 줄에서 `<tool> <sub-op>`(또는 `<tool>`) 토큰을 뽑는다."""
    tokens: set[str] = set()
    for raw in block_lines:
        if not raw.startswith(" "):
            continue  # 들여쓰기 없는 직접 sibling(external_review 등)은 강등 아님.
        s = raw.strip()
        cmd_part = s.split("#", 1)[0].strip()  # trailing '# ↳ 주석' 제거.
        m = _DEMOTION_RE.match(cmd_part)
        if not m:
            continue
        tool = m.group(1)
        args = m.group(2).split()
        first = args[0] if args else None
        if first and first.isalpha():  # sub-op 워드(show/lint/claim/regression…) 만.
            tokens.add(f"{tool} {first}")
        else:  # 첫 인자가 placeholder(`<T-NNNN>`)·flag(`--session`)면 도구 단독.
            tokens.add(tool)
    return tokens


def _crosscheck(card: str, catalog: dict[str, set[str]]):
    """카드↔카탈로그 양방향 exact-set 대조 → (checked_skills, mismatches).

    카드에 렌더된 skill block 만 검사한다 — /pm-bootstrap(카드 producer 자신)·/pm-env(facade·
    카드 미렌더)는 block 부재라 skip(강등 지어냄 0 이라 규율 위반 대상 아님).
    """
    card_lines = card.splitlines()
    checked: list[str] = []
    mismatches: list[tuple[str, list[str], list[str]]] = []
    for skill_token, cat_tokens in catalog.items():
        block = _card_skill_block(card_lines, skill_token)
        if block is None:
            continue
        checked.append(skill_token)
        card_tokens = _demotion_engine_tokens(block)
        if card_tokens != cat_tokens:
            mismatches.append((skill_token, sorted(cat_tokens), sorted(card_tokens)))
    return checked, mismatches


# ── 1. pm_role 규율 문구 존재 (prose rot 가드) ────────────────────────────────


def test_pm_role_discipline_section_exists():
    """규율 섹션 + 3요소 키워드(스킬 invoke·직접 금지·이유 판단/스킵)가 pm_role 에 살아있다."""
    text = _pm_role_text()
    body = _section_body(text, _DISCIPLINE_HEADER)
    assert body is not None, f"pm_role 에 규율 섹션 헤더 부재: {_DISCIPLINE_HEADER!r}"
    # (a) 스킬/command 로 invoke.
    assert "스킬" in body and "invoke" in body, "규율에 '스킬 invoke' 문구 부재"
    # (b) 직접 호출 금지.
    assert "직접" in body and "금지" in body, "규율에 '직접 금지' 문구 부재"
    # (c) 이유 신호 — 스킬 md 의 load-bearing 판단이 backbone 직접 실행 시 스킵된다.
    assert "판단" in body or "스킵" in body, "규율에 이유 신호(판단/스킵) 부재"


# ── 2. 카드 pointer → pm_role 섹션 정합 (dangling 방지) ────────────────────────


def test_card_pointer_targets_live_pm_role_section(bootstrap):
    """카드의 `pm_role §스킬 우선 운영 규율` pointer 가 실제 pm_role 헤더로 non-dangling."""
    card = _render_card(bootstrap, LEAN_IDENTITY)
    assert _POINTER_ANCHOR in card, f"카드에 규율 pointer {_POINTER_ANCHOR!r} 부재"
    text = _pm_role_text()
    # pointer 가 가리키는 섹션명이 실제 `## ` 헤더로 존재한다(앵커 살아있음).
    assert any(
        ln.startswith("## 스킬 우선 운영 규율") for ln in text.splitlines()
    ), "카드 pointer 가 가리키는 규율 섹션 헤더가 pm_role 에 없음(dangling)"


# ── 3. 카드 강등 엔진 ↔ pm_role 카탈로그 정합 (CLASS-CLOSER·핵심) ──────────────


def test_catalog_parse_is_non_vacuous():
    """카탈로그 파싱이 실제로 skill 을 뽑고 CLI/비-CLI 를 둘 다 분류한다(공허 가드 방지)."""
    catalog = _parse_catalog(_pm_role_text())
    assert len(catalog) >= 7, f"카탈로그에서 파싱된 skill 이 너무 적음({len(catalog)})"
    cli = {k for k, v in catalog.items() if v}          # tools/*.py 강등 엔진 있음.
    non_cli = {k for k, v in catalog.items() if not v}  # Agent 툴·facade(skill-only).
    assert cli, "CLI 엔진 skill 이 하나도 파싱되지 않음"
    assert non_cli, "비-CLI(Agent/facade) skill 이 하나도 파싱되지 않음"
    # 대표 매핑이 기대대로 파싱됐다(파서 sanity — sub-op 분해·facade 분류).
    assert catalog["/pm-wave-claim"] == {"board.py show", "board.py lint", "board.py claim"}
    assert catalog["/pm-qa"] == {"board.py regression", "board.py lint"}
    assert catalog["/pm-handoff"] == {"pm_handoff.py"}
    assert catalog["/pm-dev-delegate"] == set()   # Agent 툴 → skill-only
    assert catalog["/pm-update"] == set()          # facade → skill-only


def test_card_demotion_matches_pm_role_catalog(bootstrap):
    """카드 강등 backbone 이 pm_role 카탈로그 엔진 열과 양방향(exact-set) 정합한다.

    각 카드-렌더 skill 에서: 카탈로그 CLI 셀의 (tool, sub-op) 토큰 == 카드 그 skill block 의
    강등 backbone 토큰. 비-CLI(Agent/facade) 셀은 카드 block 에 강등 python3 줄이 없어야(빈 집합).
    카드가 엔진을 빠뜨리거나(강등 줄 삭제)·지어내거나(오귀속) 카탈로그 열이 편집되면 red.
    """
    card = _render_card(bootstrap, LEAN_IDENTITY)
    catalog = _parse_catalog(_pm_role_text())
    checked, mismatches = _crosscheck(card, catalog)
    # non-vacuous: 카드-렌더 skill 을 실제로 ≥5 검사했다(파싱 0건이면 가드 무의미).
    assert len(checked) >= 5, f"카드↔카탈로그 실검사 skill 이 너무 적음({len(checked)}): {checked}"
    assert not mismatches, (
        "카드↔pm_role 카탈로그 엔진 불일치:\n"
        + "\n".join(
            f"  {skill}: 카탈로그={cat} vs 카드강등={cardt}"
            for skill, cat, cardt in mismatches
        )
    )


def test_card_demotion_guard_is_sensitive_to_drift(bootstrap):
    """sensitivity(non-vacuous 실증) — 카드/카탈로그 어느 쪽을 깨도 #3 cross-check 가 잡는다.

    (a) 카드 강등 엔진 줄을 지우면 forward 방향으로, (b) 카탈로그 엔진 열을 바꾸면 backward
    방향으로 mismatch 가 뜬다 — 원본 소스는 건드리지 않고 파싱 산출물만 변조해 실증한다.
    """
    card = _render_card(bootstrap, LEAN_IDENTITY)
    catalog = _parse_catalog(_pm_role_text())
    # baseline 은 정합(green).
    _, base_mismatch = _crosscheck(card, catalog)
    assert not base_mismatch

    # (a) 카드에서 /pm-qa 의 `board.py lint` 강등 줄을 지운다 → /pm-qa forward 불일치.
    qa_block = _card_skill_block(card.splitlines(), "/pm-qa")
    assert qa_block is not None
    drop = next(ln for ln in qa_block if ln.startswith(" ") and "board.py lint" in ln)
    broken_card = "\n".join(ln for ln in card.splitlines() if ln != drop)
    _, mm_card = _crosscheck(broken_card, catalog)
    assert any(skill == "/pm-qa" for skill, *_ in mm_card), \
        "카드 강등 줄 삭제를 #3 이 잡지 못함(forward 가드 공허)"

    # (b) 카탈로그 /pm-handoff 엔진 열을 다른 엔진으로 바꾼다 → /pm-handoff backward 불일치.
    drifted = dict(catalog)
    drifted["/pm-handoff"] = {"board.py"}  # pm_handoff.py 를 오귀속(카드는 여전히 pm_handoff.py)
    _, mm_cat = _crosscheck(card, drifted)
    assert any(skill == "/pm-handoff" for skill, *_ in mm_cat), \
        "카탈로그 엔진 열 변경을 #3 이 잡지 못함(backward 가드 공허)"


# ── 4. 카드 스킬-우선 대표 백스톱 (T-0280 test 와 상보·durable 최소 불변식) ────


def _line_index(card: str, needle: str) -> int:
    for i, ln in enumerate(card.splitlines()):
        if needle in ln:
            return i
    raise AssertionError(f"카드에 {needle!r} 줄이 없음")


@pytest.mark.parametrize("identity", [LEAN_IDENTITY, None])
def test_card_skill_precedes_backbone_representative(bootstrap, identity):
    """대표 op(claim·handoff)에서 `/pm-…` 스킬 진입이 강등 backbone 줄보다 먼저(primary)."""
    card = _render_card(bootstrap, identity)
    for skill_needle, backbone_needle in (
        ("/pm-wave-claim", "board.py claim T-NNNN"),
        ("/pm-handoff", "pm_handoff.py"),
    ):
        assert _line_index(card, skill_needle) < _line_index(card, backbone_needle), (
            f"{skill_needle!r} 스킬이 backbone {backbone_needle!r} 뒤에 옴(강등 실패)"
        )
