"""출하 template scaffold parity 가드 — fresh-adopter lint-clean + v2 domain 골격 (T-0090).

v2 domain 지식 레이어가 adopter 에게 *절반만* 출하된 갭(엔진 `domain.py` 는 manifest 로
전파됐으나 wiki 골격 `domain/`·architecture retire stub·template README domain 사용법이
templates 에 안 간 것)의 **재발 방지**. 근본 원인 = template 파리티 미검증(root pm_role 만
보고 template lint 미확인) → 두 template 에 `[[ADR-0018]]`·`[[ADR-0019]]` dangling-wikilink.

검증 (두 template = claude_code·opencode):
  - `wiki/domain/{README,_template}.md` 존재.
  - `wiki/architecture.md` 가 retire stub("retired").
  - **각 template `board.py lint` 가 dangling-wikilink 0** (1급 acceptance·fresh-adopter
    lint-clean). 각 template 은 자기 `board.py` 를 싣고, cwd=template 으로 호출 → 그 트리의
    wiki 만 본다 (REPO 를 board.py 의 `__file__` 로 해소).
  - 루트 README(프레임워크 공통 가이드) 가 domain 사용법 키워드(`domain capture`·`covers`) 포함
    (공통분은 루트로 추출·leaf README 는 thin 어댑터 doc).

stdlib + subprocess. lint 는 warning-only(exit 0)라 종료코드가 아니라 **stdout 에
`dangling-wikilink` 부재**를 강제한다 (plain lint = advisory·`--gate` 만 nonzero).
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TEMPLATES = REPO / "templates"
TEMPLATE_NAMES = ["claude_code", "opencode"]


def _wiki(name: str) -> Path:
    return TEMPLATES / name / ".project_manager" / "wiki"


# ── domain 골격 존재 (두 template) ────────────────────────────────────────────

@pytest.mark.parametrize("name", TEMPLATE_NAMES)
def test_domain_skeleton_present(name: str):
    """두 template 에 wiki/domain/{README,_template}.md 존재 (수기 전파·manifest 밖)."""
    domain = _wiki(name) / "domain"
    for fname in ("README.md", "_template.md"):
        path = domain / fname
        assert path.exists(), f"{name}: domain 골격 누락 {path} (T-0090 수기 전파 필요)"


@pytest.mark.parametrize("name", TEMPLATE_NAMES)
def test_domain_skeleton_no_dogfood(name: str):
    """template domain/ 은 빈 골격(README+_template)만 — dogfood 페이지(dual-gate-review) 제외."""
    domain = _wiki(name) / "domain"
    assert not (domain / "dual-gate-review.md").exists(), (
        f"{name}: dual-gate-review.md 는 이 repo 자신의 dogfood 페이지 — template 에서 제외"
    )


# ── architecture retire stub (두 template) ───────────────────────────────────

@pytest.mark.parametrize("name", TEMPLATE_NAMES)
def test_architecture_is_living_truth(name: str):
    """architecture.md 가 현재-진실 scaffold (① live / ② target) — ADR-0022 부활(retire stub 아님).

    ADR-0017 의 architecture.md retire 를 ADR-0022 가 amend → 현재-아키텍처 단일 진실로 부활.
    템플릿 scaffold 도 retire stub 이 아니라 ①live/②target 골격이어야 한다.
    """
    arch = _wiki(name) / "architecture.md"
    assert arch.exists(), f"{name}: architecture.md 없음 {arch}"
    text = arch.read_text(encoding="utf-8")
    assert "현재-아키텍처 단일 진실" in text, f"{name}: architecture.md 가 현재-진실 scaffold 아님 (ADR-0022 부활)"
    assert "target" in text, f"{name}: architecture.md 에 ①live/②target 분리 없음"
    assert "domain/" in text, f"{name}: architecture 가 domain/ 세부로 안내 안 함"


# ── fresh-adopter lint-clean: dangling-wikilink 0 (1급 acceptance) ───────────

@pytest.mark.parametrize("name", TEMPLATE_NAMES)
def test_template_lint_no_dangling_wikilink(name: str):
    """각 template `board.py lint` 가 dangling-wikilink 0 (fresh-adopter lint-clean).

    각 template 은 자기 board.py 를 싣는다 — cwd=template root 로 호출하면 board.py 가
    `__file__` 로 REPO 를 자기 트리로 해소해 그 wiki 만 lint 한다. plain lint 는
    advisory(exit 0)라 stdout 에 dangling-wikilink 부재를 강제한다.
    """
    root = TEMPLATES / name
    board_py = root / ".project_manager" / "tools" / "board.py"
    assert board_py.exists(), f"{name}: board.py 없음 {board_py}"
    result = subprocess.run(
        [sys.executable, str(board_py), "lint"],
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    combined = result.stdout + result.stderr
    assert "dangling-wikilink" not in combined, (
        f"{name}: board.py lint dangling-wikilink 검출 (fresh-adopter lint 깨짐) — "
        f"출하 methodology/scaffold 는 framework-내부 ADR 을 wikilink 하지 않는다:\n{combined}"
    )


# ── template README domain 사용법 키워드 ─────────────────────────────────────

def test_framework_guide_has_domain_usage():
    """루트 README(프레임워크 공통 가이드)가 domain 사용법 키워드 포함 (domain capture·covers).

    domain 사용법 narrative 는 하니스 무관 공통분 → 루트 README §5 가 단일 진실(leaf README 는
    thin 어댑터 doc 으로 축소). 옛 test_claude_template_readme_has_domain_usage 가 claude_code
    leaf README 를 검사했으나 공통 가이드가 루트로 추출됨. (어댑터 진입 doc 의 domain 언급은
    test_claude_adapter_v2_docs.test_claude_md_mentions_domain_layer 가 별도로 가드.)
    """
    readme = REPO / "README.md"
    assert readme.exists(), f"루트 README 없음 {readme}"
    text = readme.read_text(encoding="utf-8")
    for kw in ("domain capture", "covers"):
        assert kw in text, f"루트 README(프레임워크 가이드)에 domain 사용법 키워드 '{kw}' 누락 (§5)"


# ── 출하 template = 개인 절대경로 0 (채택 누출 가드) ──────────────────────────

# 개인 머신 절대 home 경로. 채택자는 fresh clone → pm_import 로 templates/<harness>/ tracked
# 파일만 받으므로 거기 `/home/<user>` 류가 새면 죽은 경로·개인정보가 그대로 박힌다. 일반화된
# 예시(`{{PROJECT_ROOT}}`·`/path/to/...`)만 허용. (이름-비의존 — 개인 프로젝트명을 여기 하드코딩하면
# 그 자체가 누출이라 *절대경로 벡터*만 검사.)
_PERSONAL_PATH = re.compile(r"/home/[^/\s\"']+|/Users/[^/\s\"']+")


def test_templates_no_personal_path_leak():
    """출하 template 의 tracked 파일에 개인 머신 절대경로(`/home/…`·`/Users/…`)가 없다.

    실측 incident: settings.local.json 의 additionalDirectories 에 `/home/<user>/<project>` 누출.
    (settings.local.json 은 gitignored 라 fresh clone 엔 부재 → tracked 파일만 검사.)
    """
    tracked = subprocess.run(
        ["git", "-c", "core.quotepath=false", "ls-files", "templates/"],
        cwd=str(REPO), capture_output=True, text=True, encoding="utf-8",
    ).stdout.splitlines()
    offenders = []
    for rel in tracked:
        if "/node_modules/" in rel:
            continue
        p = REPO / rel
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if _PERSONAL_PATH.search(line):
                offenders.append(f"{rel}:{i}: {line.strip()[:120]}")
    assert not offenders, (
        "출하 template tracked 파일에 개인 절대경로 누출 — `{{PROJECT_ROOT}}`/`/path/to/…` 로 일반화:\n"
        + "\n".join(offenders)
    )
