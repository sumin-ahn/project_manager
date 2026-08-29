"""T-0256 doc↔상수 drift 가드 — 출하 문서가 서술하는 CLI/상수 사실이 실체와 일치함을 못박는다.

감사 B(spike multipm-command-ergonomics §1.2) 가 잡은 문서 drift 는 전부 "가드 없는 사실 서술" 이었다
(lint 섹션 수·존재하지 않는 서브커맨드·--harness default·ctx 예산 키 미문서화). 수리(T-0256) 후
재드리프트를 기계로 차단한다 — redefine/수리 후 lint·test 로 못박는 확립 패턴(T-0094/0098/0099/0100).
"""

import importlib.util
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_board():
    spec = importlib.util.spec_from_file_location(
        "board_doc_guard", ROOT / ".project_manager" / "tools" / "board.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _read(rel: str) -> str:
    path = ROOT / rel
    text = path.read_text(encoding="utf-8")
    match = re.search(
        r"\[references/operational-details\.md\]\(([^)]+)\)", text
    )
    if match:
        details = path.parent / match.group(1)
        assert details.is_file(), f"{rel}: shipped operational detail 링크가 끊김: {match.group(1)}"
        text += "\n" + details.read_text(encoding="utf-8")
    return text


# ── ② pm-wave-claim: lint 차단 섹션 수 = _REQUIRED_SECTIONS 길이 ────────────────

WAVE_CLAIM_DOCS = (
    ".claude/skills/pm-wave-claim/SKILL.md",
    "templates/opencode/.claude/skills/pm-wave-claim/SKILL.md",
    "templates/opencode/.opencode/command/pm-wave-claim.md",  # T-0674 사람 slash 표면
)


def test_wave_claim_docs_match_required_sections():
    board = _load_board()
    sections = board._REQUIRED_SECTIONS
    n = len(sections)
    names = [s.lstrip("# ").strip() for s in sections]
    for rel in WAVE_CLAIM_DOCS:
        text = _read(rel)
        assert "_REQUIRED_SECTIONS" in text, f"{rel}: 상수 참조 서술 누락"
        assert f"{n}개" in text, f"{rel}: lint 차단 섹션 수 '{n}개' 서술이 상수와 어긋남"
        for name in names:
            assert name in text, f"{rel}: 차단 섹션명 '{name}' 누락"


# ── ① pm-regression: 존재하지 않는 board 서브커맨드 참조 금지 ──────────────────


def test_regression_skill_references_only_real_board_subcommands():
    board = _load_board()
    parser = board.build_parser()
    sub = next(
        a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction"
    )
    real = set(sub.choices)
    # (옛 tripwire 제거·T-0781) — "board.py 에 reopen 이 없다" 를 전제한 문서 정직표기가
    # 있어 엔진의 `reopen` **부재**를 단언했다. 그 문구는 이후 문서에서 사라졌고(현 md 전수
    # 검색 0건) 종결 되돌리기 문 `reopen` 이 실제로 생겼다. 남는 축은 아래 loop — 문서가
    # **없는** 서브커맨드를 부르지 않는지(정직표기의 실제 관측 지점)다.
    text = _read(".claude/skills/pm-regression/SKILL.md")
    for m in re.finditer(r"board\.py (\w[\w-]*)", text):
        cmd = m.group(1)
        assert cmd in real, (
            f"pm-regression SKILL 이 존재하지 않는 서브커맨드 `board.py {cmd}` 를 언급"
        )


# ── ③ ADOPT: --harness default 서술 = pm_import argparse default ────────────────


def test_adopt_harness_default_matches_pm_import():
    src = _read(".project_manager/tools/pm_import.py")
    m = re.search(
        r"--harness[\"'],[^)]*?default=[\"'](\w+)[\"']", src, flags=re.S
    )
    assert m, "pm_import.py 에서 --harness default 추출 실패 — 가드 갱신 필요"
    default = m.group(1)
    adopt = _read("ADOPT.md")
    assert f"CLI default 은 `{default}`" in adopt or f"default: {default}" in adopt, (
        f"ADOPT.md 의 --harness default 서술이 실 default '{default}' 와 어긋남"
    )


# ── ④ ctx 예산 키: 출하 full 진입문서에 하네스별 키 문서화 존재 ─────────────────

CTX_DOCS = (
    "templates/claude_code/CLAUDE.md",
    "templates/opencode/AGENTS.md",
)


def test_ctx_budget_keys_documented_in_full_entry_docs():
    """하네스별 ctx 예산 키가 출하 진입문서에 있다 — 표기는 `harness.<name>.<속성>` 하나다."""
    for rel in CTX_DOCS:
        text = _read(rel)
        assert "harness.<name>.ctx_window_tokens" in text, (
            f"{rel}: 하네스별 ctx 예산 키(ADR-0041) 문서화 절 누락"
        )
        # suffix 표기(`ctx_window_tokens_<harness>`)는 폐지됐다 — 두 문법이 공존하면 채택자가
        # 읽히지 않는 키를 설정하고 조용히 기본값으로 돈다.
        assert "ctx_window_tokens_" not in text, f"{rel}: 폐지된 suffix 표기 잔존"


# ── ⑤ opencode README --opencode-model 예시 = pm_import canonical(옛 예시 잔존 금지) ──
# T-0265: 같은 플래그를 설명하는 두 표면(pm_import.py `--opencode-model` argparse help /
# opencode README §모델 선택 예시)이 서로 다른 모델을 들면 채택자가 폐기된 모델을 복붙한다.
# canonical = pm_import.py help 의 예시(단일 진실 — 별도 상수 없음). 가드는 "canonical 이
# 등장한다" 가 아니라 **README 의 --opencode-model 구체 예시가 전부 canonical 과 일치한다**
# (옛 예시 잔존 0)를 단언해야 실효가 있다.

OPENCODE_README = "templates/opencode/README.md"

# 구체 model 인자만 캡처(placeholder metavar 'PROVIDER/MODEL'·prose 배제):
# `--opencode-model` 바로 뒤에 공백 후 오는 provider/model — 소문자 시작(대문자 metavar 배제)·
# 슬래시 포함. → 코드 예시의 실제 모델 문자열만 걸린다.
_README_MODEL_EXAMPLE = re.compile(r"--opencode-model\s+([a-z][\w./:+-]+)")


def _pm_import_opencode_model_example() -> str:
    src = _read(".project_manager/tools/pm_import.py")
    m = re.search(
        r"""add_argument\(["']--opencode-model["'].*?예 ['"]([^'"]+)['"]""",
        src,
        flags=re.S,
    )
    assert m, "pm_import.py --opencode-model help 에서 예시 모델 추출 실패 — 가드 갱신 필요"
    return m.group(1)


def test_opencode_readme_model_example_matches_pm_import():
    canonical = _pm_import_opencode_model_example()
    readme = _read(OPENCODE_README)
    examples = _README_MODEL_EXAMPLE.findall(readme)
    assert examples, (
        f"{OPENCODE_README}: --opencode-model 구체 예시가 없다 — 가드가 무력해짐"
    )
    stale = sorted({e for e in examples if e != canonical})
    assert not stale, (
        f"{OPENCODE_README}: --opencode-model 예시가 canonical '{canonical}' 와 어긋남 "
        f"(옛 예시 잔존: {stale}). pm_import.py help 와 동기화하라."
    )


# ── ⑤ tickets README: 상태 집합·종결 명령·소유·DoD 계약이 엔진과 같은 말을 한다 ──
#
# 이 README 는 `pm_update` 전파 대상이 아니라 **인스턴스 소유** 스캐폴드라 4벌이 손으로 갈린다
# (루트 + 출하 3타깃). 엔진이 상태·명령·게이트를 바꿔도 이 문서는 조용히 옛 사실을 계속
# 출하하므로, 서술을 상수·파서·게이트 실행값과 직접 대조한다.

TICKETS_README_COPIES = (
    ".project_manager/wiki/tickets/README.md",
    "templates/claude_code/.project_manager/wiki/tickets/README.md",
    "templates/codex/.project_manager/wiki/tickets/README.md",
    "templates/opencode/.project_manager/wiki/tickets/README.md",
)


def _board_subcommands(board) -> set[str]:
    parser = board.build_parser()
    sub = next(
        a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction"
    )
    return set(sub.choices)


def test_tickets_readme_lists_every_status_dir():
    """상태 집합 서술(핵심 약속 bullet + 디렉토리 트리)이 board.STATUS_DIRS 전량을 덮는다."""
    board = _load_board()
    for rel in TICKETS_README_COPIES:
        text = _read(rel)
        for status in board.STATUS_DIRS:
            assert f"`{status}/`" in text, (
                f"{rel}: 상태 `{status}/` 가 핵심 약속 서술에서 빠짐")
            assert re.search(rf"── {re.escape(status)}/\s", text), (
                f"{rel}: 상태 {status}/ 가 디렉토리 트리에서 빠짐")


def test_tickets_readme_documents_disposition_and_reopen_commands():
    """처분·복구 명령과 처분 종류가 실제 파서·상수와 일치한다(없는 명령을 부르지 않는다)."""
    board = _load_board()
    real = _board_subcommands(board)
    assert {"discard", "reopen"} <= real, "board.py 에 discard/reopen 이 없다 — 가드 갱신 필요"
    for rel in TICKETS_README_COPIES:
        text = _read(rel)
        assert "board.py discard" in text, f"{rel}: 처분 종결 명령 안내 누락"
        assert "board.py reopen" in text, f"{rel}: 종결 복구 명령 안내 누락"
        for kind in board.DISPOSITION_KINDS:
            assert kind in text, f"{rel}: 처분 종류 `{kind}` 서술 누락"
        # 서브커맨드는 ASCII 소문자 토큰이다 — `\w` 로 잡으면 한국어 조사(`board.py 가`)가
        # 서브커맨드로 오인된다.
        for m in re.finditer(r"board\.py ([a-z][a-z-]*)", text):
            assert m.group(1) in real, (
                f"{rel}: 존재하지 않는 서브커맨드 `board.py {m.group(1)}` 를 언급")


def test_tickets_readme_states_owner_only_mutations():
    """소유 정체성 계약 서술이 실제 소유-대조 커맨드 집합·유일한 이전 경로와 일치한다."""
    board = _load_board()
    source = _read(".project_manager/tools/board.py")
    owner_gated = sorted(set(re.findall(
        r'_ownership_rejection\("(\w+)"', source)))
    assert owner_gated, "board.py 에서 소유 대조 커맨드를 못 찾았다 — 가드 시야가 어긋났다"
    for rel in TICKETS_README_COPIES:
        text = _read(rel)
        for cmd in owner_gated:
            assert f"`{cmd}`" in text, f"{rel}: 소유 대조 커맨드 `{cmd}` 서술 누락"
        assert "--takeover" in text, f"{rel}: 소유자 부재 이전 경로(--takeover) 안내 누락"
        assert "claimed_by" in text, f"{rel}: 소유 판정 축(claimed_by) 서술 누락"


# complete 게이트의 DoD 축 4형상 — 문서 문장과 게이트 실행값을 같은 테스트에서 대조한다.
_DOD_BLOCKED_SHAPES = {
    "절 부재": "# 티켓\n\n## 목표\n옛 형식.\n",
    "체크박스 0개": "## 완료 조건\n\n산문으로만 적은 완료 조건.\n",
    "전량 이월": "## 완료 조건\n\n- [>] 항목 (이월: 다른 티켓으로 병합)\n",
}
_DOD_PASSING_SHAPE = "## 완료 조건\n\n- [x] 코드\n- [>] 문서 (이월: 후속 귀속)\n"


def test_tickets_readme_dod_contract_matches_complete_gate():
    """DoD 축 서술이 게이트 실측과 일치한다 — 산문 DoD 면제 문구가 남아 있으면 실패."""
    board = _load_board()
    for label, body in _DOD_BLOCKED_SHAPES.items():
        assert board._dod_open_items(body), f"게이트가 {label} 형상을 막지 않는다"
    assert board._dod_open_items(_DOD_PASSING_SHAPE) == [], "부분 이월이 막힌다"

    for rel in TICKETS_README_COPIES:
        text = _read(rel)
        assert "검사 대상이 아니다" not in text, (
            f"{rel}: 산문 DoD 면제(옛 계약) 문구 잔존 — 게이트는 그 형상을 차단한다")
        for label in _DOD_BLOCKED_SHAPES:
            assert label in text, f"{rel}: 차단 형상 '{label}' 서술 누락"
        assert "부분 이월" in text, f"{rel}: 통과 형상(부분 이월) 서술 누락"


# ── ⑥ pm-wave-finish: DoD preflight의 확정/불확정 경로와 log 멱등 계약 ────────

WAVE_FINISH_DOCS = (
    ".claude/skills/pm-wave-finish/SKILL.md",
    "templates/claude_code/.claude/skills/pm-wave-finish/SKILL.md",
    "templates/codex/.agents/skills/pm-wave-finish/SKILL.md",
    "templates/opencode/.claude/skills/pm-wave-finish/SKILL.md",
    "templates/opencode/.opencode/command/pm-wave-finish.md",
)


def test_wave_finish_docs_distinguish_dod_preflight_outcomes_and_single_log_entry():
    """판정 가능 차단과 fail-soft 불확정을 섞지 않고 ticket당 log 하나를 약속한다."""
    for rel in WAVE_FINISH_DOCS:
        text = _read(rel)
        for phrase in (
            "판정 가능",
            "판정 불가",
            "fail-soft",
            "중복 append",
            "ticket당 완료 entry 하나",
        ):
            assert phrase in text, f"{rel}: pm-wave-finish 계약 문구 '{phrase}' 누락"
