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
    return (ROOT / rel).read_text(encoding="utf-8")


# ── ② pm-wave-claim: lint 차단 섹션 수 = _REQUIRED_SECTIONS 길이 ────────────────

WAVE_CLAIM_DOCS = (
    ".claude/skills/pm-wave-claim/SKILL.md",
    "templates/opencode/.claude/skills/pm-wave-claim/SKILL.md",  # ADR-0065 단일 소비 미러(command 은퇴)
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
    assert "reopen" not in real, "board.py 에 reopen 이 생겼다 — 문서 정직표기(T-0256) 재검토"
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
    for rel in CTX_DOCS:
        text = _read(rel)
        assert "ctx_window_tokens_" in text, (
            f"{rel}: 하네스별 ctx 예산 키(ADR-0041) 문서화 절 누락"
        )


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
