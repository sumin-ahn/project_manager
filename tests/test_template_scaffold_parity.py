"""출하 template scaffold parity 가드 — fresh-adopter lint-clean + v2 domain 골격 (T-0090).

v2 domain 지식 레이어가 adopter 에게 *절반만* 출하된 갭(엔진 `domain.py` 는 manifest 로
전파됐으나 wiki 골격 `domain/`·architecture retire stub·template README domain 사용법이
templates 에 안 간 것)의 **재발 방지**. 근본 원인 = template 파리티 미검증(root pm_role 만
보고 template lint 미확인) → 두 template 에 `[[ADR-0018]]`·`[[ADR-0019]]` dangling-wikilink.

검증 (출하 타깃 전부 — 아래 `TEMPLATE_NAMES` 파생):
  - `wiki/domain/{README,_template}.md` 존재.
  - `wiki/domain/_template.md` 가 현재-진실/히스토리 규칙을 싣고 canonical ↔ 존재하는 모든
    타깃에 동일 (T-0568 — 엔진의 `history` 축만 가고 규칙 원문은 안 가던 갭).
  - `wiki/architecture.md` 가 현재-진실 scaffold (부재 선언 타깃은 아래 ratchet 이 관리).
  - **각 template `board.py lint` 가 dangling-wikilink 0** (1급 acceptance·fresh-adopter
    lint-clean). 각 template 은 자기 `board.py` 를 싣고, cwd=template 으로 호출 → 그 트리의
    wiki 만 본다 (REPO 를 board.py 의 `__file__` 로 해소).
  - 루트 README(프레임워크 공통 가이드) 가 domain 사용법 키워드(`domain capture`·`covers`) 포함
    (공통분은 루트로 추출·leaf README 는 thin 어댑터 doc).

stdlib + subprocess. lint 는 warning-only(exit 0)라 종료코드가 아니라 **stdout 에
`dangling-wikilink` 부재**를 강제한다 (plain lint = advisory·`--gate` 만 nonzero).
"""
from __future__ import annotations

import importlib.util
import inspect
import re
import subprocess
import sys
from pathlib import Path

import pytest

from _harness_matrix import HARNESSES, _PM_IMPORT

REPO = Path(__file__).resolve().parents[1]
TEMPLATES = REPO / "templates"

# 파리티 대상 = 출하 타깃 **전부**. 손-열거 2벌(claude_code·opencode)이던 자리다 — 세 번째
# 하네스(codex)가 게이트에 안 잡히는 결함 클래스가 [[T-0429]] 에서 이미 실측됐고, 그 티켓이
# 만든 공용 축(`_harness_matrix`: 엔진 등록 ∩ `templates/<dir>/` 실존·등록⇒실존 loud)에서
# 그대로 파생한다. 네 번째 하네스는 자동 편입된다.
TEMPLATE_NAMES = sorted(
    dirname
    for harness in HARNESSES
    for dirname in _PM_IMPORT.HARNESS_TEMPLATE_DIRS[harness]
)


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


# domain seed README = 채택자가 읽는 판정 안내다. CHANGELOG 가 "`history` 축 판정 기준은
# `wiki/domain/README.md` 가 문서화한다"고 선언했는데 출하 seed 엔 그 축 언급이 0 이었다(선언과
# 출하물의 어긋남). 축 존재 + 판정 한 문장이 실렸는지 보고, 세 벌은 같은 문서이므로 바이트까지
# 같은지 본다(수기 전파라 한 벌만 고치는 drift 가 실제 경로다).
DOMAIN_README_REL = Path(".project_manager") / "wiki" / "domain" / "README.md"
DOMAIN_README_MARKERS = (
    "`history` 축",           # 축 존재
    '"지금 X다"',              # 판정 한 문장 — 페이지 쪽
    '"언제 X로 바뀌었다"',      # 판정 한 문장 — log 쪽
)


@pytest.mark.parametrize("name", TEMPLATE_NAMES)
def test_domain_readme_documents_history_axis(name: str):
    """domain seed README 가 `history` 축과 그 판정 한 문장을 싣는다 (CHANGELOG 선언의 근거)."""
    path = TEMPLATES / name / DOMAIN_README_REL
    text = path.read_text(encoding="utf-8")
    missing = [marker for marker in DOMAIN_README_MARKERS if marker not in text]
    assert not missing, (
        f"{name}: domain README 에 history 축 서술 누락 {missing} — 채택자가 lint 의 "
        "advisory 판정 기준을 못 받는다"
    )


def test_domain_readme_is_byte_identical_across_targets():
    """domain seed README 세 벌이 바이트 동일 (수기 전파 drift 차단)."""
    paths = [TEMPLATES / name / DOMAIN_README_REL for name in TEMPLATE_NAMES]
    assert len(paths) > 1, "출하 타깃이 하나뿐 — 파리티 비교가 공허"
    expected = paths[0].read_bytes()
    drifted = [
        str(path.relative_to(REPO)) for path in paths[1:]
        if path.read_bytes() != expected
    ]
    assert not drifted, (
        f"domain README 가 {TEMPLATE_NAMES[0]} 판과 바이트 drift — {drifted}. "
        "같은 문서이므로 한 벌을 고치면 나머지에도 그대로 복사한다."
    )


@pytest.mark.parametrize("name", TEMPLATE_NAMES)
def test_domain_skeleton_no_dogfood(name: str):
    """template domain/ 은 빈 골격(README+_template)만 — dogfood 페이지(dual-gate-review) 제외."""
    domain = _wiki(name) / "domain"
    assert not (domain / "dual-gate-review.md").exists(), (
        f"{name}: dual-gate-review.md 는 이 repo 자신의 dogfood 페이지 — template 에서 제외"
    )


# ── domain scaffold = 현재-진실/히스토리 규칙 출하 (T-0568) ───────────────────
#
# 채택자는 엔진(`domain.py lint` 의 `history` 축)만 받고 규칙 원문은 못 받는 갭이 있었다.
# 규칙은 canonical `_template.md` 가 싣고 `pm_update` 로 전 타깃에 간다. 파리티 대상은
# 하드코딩 2벌이 아니라 **존재하는 모든 타깃**을 파일 실재로 파생한다(새 하네스 템플릿 자동 편입).

DOMAIN_TEMPLATE_REL = Path(".project_manager") / "wiki" / "domain" / "_template.md"

# 규칙의 뼈대. 표현을 다듬는 건 자유지만 이 네 축이 빠지면 채택자가 규칙을 못 받는다.
DOMAIN_RULE_MARKERS = (
    '"지금 X다"',            # 판정 한 문장 — 페이지 쪽
    '"언제 X로 바뀌었다"',   # 판정 한 문장 — log 쪽
    "갱신은 덧붙이기가 아니라 교체다",
    "history 축",            # 기계 판정 포인터 (domain.py lint)
)


def _domain_template_paths() -> list[Path]:
    """canonical + `templates/` 아래 실재하는 모든 타깃의 domain `_template.md`."""
    paths = [REPO / DOMAIN_TEMPLATE_REL]
    paths += sorted(
        target / DOMAIN_TEMPLATE_REL
        for target in TEMPLATES.iterdir()
        if (target / DOMAIN_TEMPLATE_REL).exists()
    )
    return paths


def _load_domain():
    spec = importlib.util.spec_from_file_location(
        "domain", REPO / ".project_manager" / "tools" / "domain.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize(
    "path", _domain_template_paths(), ids=lambda p: str(p.relative_to(REPO)))
def test_domain_template_ships_current_truth_rule(path: Path):
    """domain scaffold 가 현재-진실/히스토리 규칙을 싣는다 (canonical + 전 타깃)."""
    text = path.read_text(encoding="utf-8")
    missing = [marker for marker in DOMAIN_RULE_MARKERS if marker not in text]
    assert not missing, (
        f"{path.relative_to(REPO)}: 현재-진실/히스토리 규칙 축 누락 {missing} — "
        "채택자가 가드(`domain.py lint` history)만 받고 규칙은 못 받는다 "
        "(canonical 수정 후 `pm_update.py --all-targets` 전파)"
    )


@pytest.mark.parametrize(
    "path", _domain_template_paths(), ids=lambda p: str(p.relative_to(REPO)))
def test_domain_template_starts_with_frontmatter(path: Path):
    """규칙 안내가 frontmatter 앞에 오면 안 된다 — 복사한 페이지가 index 에서 사라진다.

    `load_pages` 는 텍스트가 `---` 로 시작하지 않는 파일을 "페이지 아님"으로 **조용히** skip
    한다. 안내 블록을 frontmatter 위에 두면 스캐폴드를 복사해 만든 페이지가 전부 소환·lint
    대상에서 빠지고, 경고도 안 난다.
    """
    text = path.read_text(encoding="utf-8")
    assert text.lstrip().startswith("---"), (
        f"{path.relative_to(REPO)}: frontmatter 로 시작하지 않음 — 안내는 frontmatter 뒤에 둔다 "
        "(load_pages 가 조용히 skip)"
    )


def test_domain_template_is_byte_identical_across_targets():
    """canonical ↔ 전 타깃 바이트 동일.

    marker 단언만으로는 canonical 문구를 고친 뒤 전파를 빠뜨린 상태를 통과시킨다 — 채택자는
    옛 문구를 계속 받는다. 규칙은 canonical 단일 소유이므로 사본은 바이트까지 같아야 한다.
    """
    paths = _domain_template_paths()
    canonical, copies = paths[0], paths[1:]
    assert copies, "templates/ 아래 domain `_template.md` 사본이 하나도 없다(전파 채널 붕괴)"
    expected = canonical.read_bytes()
    drifted = [
        str(path.relative_to(REPO)) for path in copies
        if path.read_bytes() != expected
    ]
    assert not drifted, (
        f"domain `_template.md` 가 canonical 과 바이트 drift — {drifted}. "
        "canonical 수정 후 전파 필요(`pm_update.py --from . --all-targets`)"
    )


def test_domain_template_anchors_capture_draft_derivation():
    """`domain.py` 가 안내 블록을 뽑는 앵커가 스캐폴드에 실재한다.

    capture-draft scaffold 는 이 마커로 안내를 *파생*한다(문구 이중 서술 금지). 마커가
    사라지면 파생은 fail-soft 로 빈 문자열이 되어 draft 만 조용히 규칙을 잃는다.
    """
    dm = _load_domain()
    text = (REPO / DOMAIN_TEMPLATE_REL).read_text(encoding="utf-8")
    assert dm.CURRENT_TRUTH_GUIDE_MARKER in text, (
        f"domain `_template.md` 에 파생 앵커 '{dm.CURRENT_TRUTH_GUIDE_MARKER}' 부재 — "
        "capture-draft 산출이 규칙을 조용히 잃는다"
    )
    assert dm.current_truth_guide_block(REPO / DOMAIN_TEMPLATE_REL.parent), (
        "스캐폴드에서 안내 블록 추출 실패 — 주석 블록 형태가 깨졌다"
    )


def test_domain_template_guidance_is_history_clean():
    """스캐폴드 자신이 `history` 축을 발화시키지 않는다.

    안내 블록을 안 지운 채 페이지를 만드는 건 흔한 경로다. 그 예시가 시점 스탬프로 시작하는
    lead-in 이면 복사한 페이지가 전부 첫날부터 history finding 을 달고 태어난다.
    """
    dm = _load_domain()
    body = (REPO / DOMAIN_TEMPLATE_REL).read_text(encoding="utf-8").split("---", 2)[2]
    leadins = dm.history_leadins(body)
    assert leadins == [], (
        f"domain `_template.md` 의 안내가 history lead-in 으로 잡힌다 {leadins} — "
        "예시는 헤딩·인용·굵은 도입부가 아니라 산문/불릿으로 쓴다"
    )


# ── architecture 현재-진실 scaffold (전 타깃) ────────────────────────────────

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


# ── 출하 wiki 문서의 상대 링크 해소 (wiki seed 대칭) ─────────────────────────
#
# 채택자가 받는 wiki 문서의 링크가 안 풀리면 fresh 채택자가 깨진 안내를 받는다. 실측 회귀:
# codex 템플릿이 wiki seed 12종(architecture·status·status_done·decisions/·ideas/·specs/·
# raw/README·log/archive)을 안 실어 `README.md` 만 11개 dangling 이었고, `pm_playbook.md` 는 세
# 벌 모두 claude 전용 어댑터 경로(`.claude/agents/*.md`)를 링크해 codex·opencode 트리에서
# 깨졌다. 그래서 진입 문서 한 파일이 아니라 **출하 wiki `.md` 전부**를 본다.
_MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
# 코드 영역(fence·inline span)은 건너뛴다 — 문서가 "링크 금지" 를 설명하려고 **일부러** 담은
# 예시(❌ `[adr](decisions/0006-….md)`)까지 실제 링크로 오독하면, 규율 설명을 지워야 green 이
# 되는 거꾸로 된 압력이 생긴다. 엔진 wikilink lint 도 같은 규칙으로 코드 영역을 건너뛴다.
_CODE_FENCE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)
_CODE_SPAN = re.compile(r"(`+)(?:(?!\1).)*?\1", re.DOTALL)
# 출하 트리에 없어도 정상인 링크 = `board.py init` 이 만드는 인스턴스 산출물뿐(템플릿엔
# `pm_state.template.md` 만 실린다). 그 밖의 미해소는 seed 누락이거나 잘못된 경로다.
WIKI_RUNTIME_LINK_TARGETS = {"pm_state.md"}


def _prose_only(text: str) -> str:
    """코드 fence·inline code span 을 지운 산문 — 링크 스캔 대상."""
    return _CODE_SPAN.sub(" ", _CODE_FENCE.sub(" ", text))


def _unresolved_wiki_links(name: str) -> dict[str, set[str]]:
    """그 템플릿의 출하 wiki `.md` 별 미해소 상대 링크 (파일 상대경로 → 대상 집합)."""
    wiki = _wiki(name)
    unresolved: dict[str, set[str]] = {}
    for path in sorted(wiki.rglob("*.md")):
        targets = {
            target.split("#")[0]
            for target in _MARKDOWN_LINK.findall(
                _prose_only(path.read_text(encoding="utf-8"))
            )
            if not target.startswith(("http://", "https://", "#", "mailto:"))
        }
        missing = {
            target for target in targets
            if target and not (path.parent / target).exists()
        }
        if missing:
            unresolved[path.relative_to(wiki).as_posix()] = missing
    return unresolved


@pytest.mark.parametrize("name", TEMPLATE_NAMES)
def test_wiki_links_resolve_in_shipped_tree(name: str):
    """출하 wiki `.md` 의 상대 링크가 그 트리에서 전부 풀린다 (런타임 산출물만 예외).

    예외 선언도 같이 검증한다 — 선언한 대상이 실제로 출하되기 시작하면 선언이 낡은 것이라
    red 로 알린다(조용히 넓은 예외를 남겨 두지 않는다).
    """
    unresolved = _unresolved_wiki_links(name)
    offenders = {
        rel: sorted(targets - WIKI_RUNTIME_LINK_TARGETS)
        for rel, targets in unresolved.items()
        if targets - WIKI_RUNTIME_LINK_TARGETS
    }
    assert not offenders, (
        f"{name}: 출하 wiki 가 그 트리에 없는 경로를 링크 — {offenders}. "
        "instance seed 를 실어라(claude_code 판과 byte-identical·manifest 밖). "
        "하네스마다 다른 어댑터 경로는 링크가 아니라 plain text 로 쓴다."
    )
    shipped_runtime_targets = [
        target for target in WIKI_RUNTIME_LINK_TARGETS
        if (_wiki(name) / target).exists()
    ]
    assert not shipped_runtime_targets, (
        f"{name}: {shipped_runtime_targets} 를 이제 출하한다 — "
        "WIKI_RUNTIME_LINK_TARGETS 예외 선언을 지워라"
    )


def test_wiki_link_scan_skips_code_regions_but_still_reads_prose(tmp_path):
    """스캔이 코드 영역만 건너뛴다 — 산문 링크는 그대로 읽는다(예외가 넓어지지 않게).

    `pm_playbook.md` 의 "markdown 경로 링크 금지" 예시가 코드 span 안에 있어 실제 링크가
    아니다. 그 규칙이 fence·span 을 넘어 산문까지 삼키면 이 가드가 통째로 공허해진다.
    """
    text = (
        "산문 링크 [a](missing_prose.md)\n"
        "인라인 예시 `[b](missing_span.md)` 와 `` `[c](missing_double.md)` ``\n"
        "```\n[d](missing_fence.md)\n```\n"
    )
    targets = set(_MARKDOWN_LINK.findall(_prose_only(text)))
    assert targets == {"missing_prose.md"}


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


# ── fresh-adopter 묶음 카드 — 엔진 값에서 파생한 구조 단언 ────────────────────
#
# 출하 산문이 서술하는 절차는 **엔진이 실제로 하는 일**과 같아야 한다. 그 정합을 사람이 눈으로
# 대조하면 반드시 drift 하므로, 라운드 순번표와 종결 단계 이름을 엔진 상수에서 **파생해** 대조한다
# (문자열을 테스트에 손으로 재타이핑하지 않는다). 채택자 트리에 실제로 깔리는 표면 전수가 대상이라,
# 새 타깃이 생겨도 `TEMPLATE_NAMES` 를 통해 자동 편입된다.

_CLUSTER_STAGE_HEADING = "## 클러스터 단계 표"
_CLOSE_STEP_COUNT_TOKEN = "7단계"
# 카드 본문에 실린 라운드 순번 표기 전수 — 엔진 수열 밖 순번(확인용 라운드 등)이 하나라도 실리면
# 그 카드가 서술하는 경로가 엔진 예산과 다르다.
_ROUND_LABEL_RE = re.compile(r"\b(\d{2})-(architect|developer|code-reviewer)\b")
# 카드 종결 단계 표의 행 — 이름 부분집합 대조만 하면 지워진 단계가 표에 남아도 통과한다.
_CLOSE_STEP_TABLE_ROW_RE = re.compile(r"^\| \d+ \| ", re.M)
# 폐지된 부분 재설계 서술 — 재설계는 예산 4키를 전부 리셋해 주기를 처음부터 다시 연다.
_RETIRED_REPLAN_PHRASE = "쌍을 뒤에 붙인다"


def _load_engine_module(name: str):
    """엔진 도구를 경로 로드한다(도구는 패키지가 아님)."""
    spec = importlib.util.spec_from_file_location(
        f"scaffold_parity_{name}", REPO / ".project_manager" / "tools" / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _round_ordinal_labels() -> tuple[str, ...]:
    """엔진이 정한 묶음 라운드 순번표 — `NN-<역할>` 표기로 편다."""
    delegate = _load_engine_module("pm_delegate")
    board = _load_engine_module("board")
    sequence = delegate.cluster_round_sequence(
        board.CLUSTER_BUDGET_DEFAULT, cluster="C-round-labels",
    )
    assert sequence, "엔진 라운드 수열이 비었다 — 파생 입력이 stale"
    return tuple(f"{index:02d}-{role}" for index, role in enumerate(sequence, start=1))


def _close_step_labels() -> tuple[str, ...]:
    """엔진이 정한 종결 단계 이름 — 고정 순서 그대로."""
    finish = _load_engine_module("ticket_finish")
    steps = tuple(label for _key, label, _pre in finish.ClusterCloser.STEPS)
    assert steps, "엔진 종결 단계가 비었다 — 파생 입력이 stale"
    return steps


def _shipped_cards(name: str, stem: str) -> list[Path]:
    """그 타깃에 실제로 깔리는 카드 표면 전부(스킬 디렉터리 + 평탄 command 팔레트)."""
    root = TEMPLATES / name
    return sorted(root.glob(f"*/skills/{stem}/SKILL.md")) + sorted(
        root.glob(f"*/command/{stem}.md")
    )


# 수렴 규범의 문장 소유자는 pm_principles 하나뿐이다. 역할/플레이북/스킬은 링크와 실행 절차만
# 싣는다. 정규식은 문구 하나를 golden 으로 고정하지 않고 같은 규칙을 다시 풀어 쓴 형상을 잡는다.
_CONVERGENCE_RULE_COPY_RE = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    r"fix[^.]{0,120}마지막[^.]{0,40}(?:사람 )?라운드",
    r"(?:실패|하나라도).{0,140}(?:추가 )?(?:사람 )?라운드.{0,80}(?:열지|정지|보고)",
    r"architect.{0,180}reviewer.{0,120}(?:전체 회귀|모두).{0,80}(?:green|통과)",
    r"architect\s*1.{0,80}developer(?:_per_ticket)?\s*1.{0,80}code-reviewer\s*1.{0,80}fix\s*1",
    r"(?:developer와 fix|developer/fix).{0,220}전체 회귀.{0,100}정확히\s*1회",
    r"pm-owned:.{0,220}machine_verifiable=false.{0,80}reason=pm-owned",
))


def _convergence_rule_copies(text: str) -> list[str]:
    normalized = " ".join(text.split())
    return [match.group(0) for rule in _CONVERGENCE_RULE_COPY_RE
            if (match := rule.search(normalized))]


def _convergence_reference_guides() -> list[Path]:
    paths = [
        REPO / ".project_manager/wiki/pm_role.md",
        REPO / ".project_manager/wiki/pm_playbook.md",
        REPO / ".claude/skills/pm-dev-delegate/SKILL.md",
    ]
    for name in TEMPLATE_NAMES:
        paths.extend((
            _wiki(name) / "pm_role.md",
            _wiki(name) / "pm_playbook.md",
        ))
        paths.extend(_shipped_cards(name, "pm-dev-delegate"))
    return sorted(set(paths))


def test_convergence_normative_sentences_live_only_in_pm_principles():
    """role/playbook/skill와 출하 사본은 수렴 규범을 재서술하지 않고 canonical 절을 참조한다."""
    owners = [REPO / ".project_manager/wiki/pm_principles.md"] + [
        _wiki(name) / "pm_principles.md" for name in TEMPLATE_NAMES
    ]
    assert all(_convergence_rule_copies(path.read_text(encoding="utf-8")) for path in owners), (
        "pm_principles owner의 수렴 규범이 sensitivity 정규식에 잡히지 않음"
    )
    offenders = {}
    for path in _convergence_reference_guides():
        text = path.read_text(encoding="utf-8")
        copies = _convergence_rule_copies(text)
        if copies:
            offenders[str(path.relative_to(REPO))] = copies
        assert "pm_principles.md" in text, f"{path.relative_to(REPO)}: canonical 원칙 참조 누락"
    assert not offenders, f"pm_principles 밖 수렴 규범 복제: {offenders}"


@pytest.mark.parametrize("sample", (
    "fix가 마지막 사람 라운드다.",
    "실패하면 추가 사람 라운드를 열지 않고 사용자에게 보고한다.",
    "architect 테스트와 reviewer 테스트와 전체 회귀를 모두 green으로 만든다.",
    "architect 1 developer_per_ticket 1 code-reviewer 1 fix 1",
    "developer와 fix는 전체 회귀를 정확히 1회 실행한다.",
    "pm-owned: scope와 machine_verifiable=false 및 reason=pm-owned가 함께 있어야 한다.",
))
def test_convergence_normative_copy_guard_is_sensitive(sample: str):
    """가드가 실제 복제 문장을 잡는 음성 사례 — 스캔 대상만 비우면 통과하는 공허한 테스트 금지."""
    assert _convergence_rule_copies(sample)


@pytest.mark.parametrize("name", TEMPLATE_NAMES)
def test_shipped_delegate_card_carries_engine_round_ordinals(name: str):
    """출하 위임 카드가 묶음 단계 표와 엔진 순번 값을 싣는다(티켓당 절 부재)."""
    cards = _shipped_cards(name, "pm-dev-delegate")
    assert cards, f"{name}: pm-dev-delegate 카드 표면 0 — 출하 누락"
    labels = _round_ordinal_labels()
    for card in cards:
        text = card.read_text(encoding="utf-8")
        assert _CLUSTER_STAGE_HEADING in text, (
            f"{card.relative_to(REPO)}: 묶음 단계 표 절이 없음 — 운영 단위 서술 누락"
        )
        missing = [label for label in labels if label not in text]
        assert not missing, (
            f"{card.relative_to(REPO)}: 라운드 순번표가 엔진 값과 어긋남 (누락 {missing})"
        )
        # 순서 — 카드에 처음 등장하는 순서가 엔진 수열 순서 그대로여야 한다(존재만으로는
        # 단계가 뒤바뀐 표를 통과시킨다).
        positions = [text.index(label) for label in labels]
        assert positions == sorted(positions), (
            f"{card.relative_to(REPO)}: 라운드 순번이 엔진 수열 순서와 다름 "
            f"(카드 순서 {[label for _pos, label in sorted(zip(positions, labels))]})"
        )
        # 수열 밖 순번 0 — 확인용 라운드 같은 옛 경로 표기가 카드에 남으면 red 다.
        extra = sorted({
            f"{ordinal}-{role}" for ordinal, role in _ROUND_LABEL_RE.findall(text)
        } - set(labels))
        assert not extra, (
            f"{card.relative_to(REPO)}: 엔진 예산 밖 라운드 표기 {extra} — 경로가 둘이다"
        )
        assert _RETIRED_REPLAN_PHRASE not in text, (
            f"{card.relative_to(REPO)}: 폐지된 부분 재설계 서술이 남아 있음"
        )


@pytest.mark.parametrize("name", TEMPLATE_NAMES)
@pytest.mark.parametrize("stem", ("pm-ticket", "pm-review"))
def test_shipped_authoring_and_review_guides_reference_the_canonical_principles(
    name: str, stem: str,
):
    """ticket/reviewer 출하 표면이 수렴 원문을 복제하지 않고 참조한다.

    architect/reviewer 역할 카드의 호환성(변경 폭) 계약은 아래 역할별 테스트가
    더 구체적인 필드와 함께 검증한다. 위임 스킬의 모든 평탄 command 표면에까지
    원문 절 이름을 복제하라는 과도한 제약은 두지 않는다.
    """
    cards = _shipped_cards(name, stem)
    assert cards, f"{name}: {stem} 출하 카드 누락"
    for card in cards:
        text = card.read_text(encoding="utf-8")
        assert "pm_principles.md" in text
        assert "티켓과 위임" in text


@pytest.mark.parametrize("name", TEMPLATE_NAMES)
@pytest.mark.parametrize(
    "stem", ("pm-dev-delegate", "pm-review", "pm-wave-finish", "pm-release"),
)
def test_shipped_current_workflow_guides_do_not_move_review_residue_to_a_new_ticket(
    name: str, stem: str,
):
    cards = _shipped_cards(name, stem)
    assert cards, f"{name}: {stem} 출하 카드 누락"
    retired = (
        "후속 티켓으로", "다음 티켓으로", "별도 티켓으로",
        "cluster replan", "--resolve-gate <게이트> --into",
        "--resolve-gate <게이트> --fixed",
    )
    for card in cards:
        text = card.read_text(encoding="utf-8")
        found = [phrase for phrase in retired if phrase in text]
        assert not found, f"{card.relative_to(REPO)}: 폐지 처방 잔존 {found}"


@pytest.mark.parametrize(
    "relative",
    (
        "claude_code/.claude/agents/architect.md",
        "codex/.codex/agents/architect.toml",
        "opencode/.opencode/agents/architect.md",
    ),
)
def test_shipped_architect_contracts_name_required_test_fields(relative: str):
    text = (TEMPLATES / relative).read_text(encoding="utf-8")
    for marker in ("pm_principles.md", "대상", "명령", "기대값", "음성"):
        assert marker in text, f"{relative}: architect 계약 marker 누락 {marker}"


@pytest.mark.parametrize(
    "relative",
    (
        "claude_code/.claude/agents/code-reviewer.md",
        "codex/.codex/agents/code-reviewer.toml",
        "opencode/.opencode/agents/code-reviewer.md",
    ),
)
def test_shipped_reviewer_contracts_name_complete_fix_inputs(relative: str):
    text = (TEMPLATES / relative).read_text(encoding="utf-8")
    for marker in (
        "pm_principles.md", "코드 위치", "오류 거동", "수정 설계", "추가 회귀", "명령", "기대값",
    ):
        assert marker in text, f"{relative}: reviewer fix 계약 marker 누락 {marker}"


@pytest.mark.parametrize("name", TEMPLATE_NAMES)
def test_shipped_finish_card_carries_engine_close_steps(name: str):
    """출하 종결 카드가 엔진 종결 단계 이름 전수와 단계 수를 싣는다."""
    cards = _shipped_cards(name, "pm-wave-finish")
    assert cards, f"{name}: pm-wave-finish 카드 표면 0 — 출하 누락"
    steps = _close_step_labels()
    for card in cards:
        assert _CLOSE_STEP_COUNT_TOKEN in card.read_text(encoding="utf-8"), (
            f"{card.relative_to(REPO)}: 종결 단계 수 표기가 없음"
        )
    # 단계 이름은 상세 문서가 싣는다 — 평탄 command 팔레트는 스킬 디렉터리의 상세를 참조하므로
    # 그 타깃이 실제로 출하하는 상세 문서 전부를 한 haystack 으로 본다.
    details = sorted(
        (TEMPLATES / name).glob("*/skills/pm-wave-finish/references/operational-details.md")
    )
    assert details, f"{name}: pm-wave-finish 상세 문서 0 — 출하 누락"
    haystack = "".join(path.read_text(encoding="utf-8") for path in details)
    missing = [label for label in steps if label not in haystack]
    assert not missing, (
        f"{name}: 종결 단계 이름이 엔진 값과 어긋남 (누락 {missing})"
    )
    # 부분집합 대조만으로는 **지워진 단계가 표에 남은** 형상을 못 잡는다 — 행 수까지 센다.
    for path in details:
        rows = _CLOSE_STEP_TABLE_ROW_RE.findall(path.read_text(encoding="utf-8"))
        assert len(rows) == len(steps), (
            f"{path.relative_to(REPO)}: 종결 단계 표 행 {len(rows)} != 엔진 단계 {len(steps)}"
        )


# F-002 — 종결 5·6단계의 브랜치 계약. 엔진은 `base_branch` 미선언을 **차단**하고 묶음
# 브랜치(`branch`) 미선언만 무대상으로 접는다. 카드가 그 반대(비차단 skip)로 안내하면 같은
# 상황에서 사람이 하는 행동과 엔진이 내는 결과가 갈린다.
_CLOSE_SKIP_CLAIM_RE = re.compile(
    r"(?:통합|기준) 브랜치[^\n]{0,60}미선언[^\n]{0,80}(?:무대상|건너뛴|비차단)"
)


def _close_branch_surfaces() -> list[Path]:
    """종결 브랜치 계약을 말하는 카드 표면 전부 — canonical + 출하 사본."""
    paths = [
        REPO / ".claude" / "skills" / "pm-wave-finish" / "SKILL.md",
        REPO / ".claude" / "skills" / "pm-wave-finish" / "references" /
        "operational-details.md",
        REPO / ".claude" / "skills" / "pm-dev-delegate" / "SKILL.md",
    ]
    for name in TEMPLATE_NAMES:
        for stem in ("pm-wave-finish", "pm-dev-delegate"):
            paths.extend(_shipped_cards(name, stem))
            paths.extend(sorted(
                (TEMPLATES / name).glob(
                    f"*/skills/{stem}/references/operational-details.md"
                )
            ))
    return sorted(set(paths))


def test_completion_guidance_matches_close_branch_contract():
    """F-002: 카드의 5·6단계 안내가 엔진의 차단/건너뛰기 계약과 같은 값을 말한다."""
    finish = _load_engine_module("ticket_finish")
    board = _load_engine_module("board")
    closer = finish.ClusterCloser

    # 엔진 축 — 기준 브랜치 미선언은 두 단계 모두 차단 문구로 정지하고,
    # 무대상 건너뛰기는 묶음 브랜치(`branch`) 미선언에만 있다. 판정 자리는 그 단계가 선언한
    # 읽기 전용 선검사이므로 선언에서 파생해 본다(메서드 이름을 여기 손으로 적지 않는다).
    blocking = finish._CLOSE_INTEGRATION_UNDECLARED
    assert "base_branch" in blocking
    prechecks = {key: precheck for key, _label, precheck in closer.STEPS}
    for key in ("rebase", "merge"):
        precheck = getattr(closer, prechecks[key])
        assert "_CLOSE_INTEGRATION_UNDECLARED" in inspect.getsource(precheck), (
            f"{precheck.__name__} 가 기준 브랜치 미선언을 더는 차단하지 않는다 — 카드 문구의 근거 소멸"
        )
    assert "묶음 브랜치 미선언" in inspect.getsource(closer._step_merge)
    assert "묶음 브랜치 미선언" not in inspect.getsource(closer._step_rebase)

    # 발행 자동 장부 축 — `base_branch` 는 기록되고 묶음 브랜치만 빈다.
    assert "base_branch=base_branch" in inspect.getsource(board._cluster_auto_attach)
    auto = board._new_cluster_fm(
        board.cluster_id_for_name("T-9999"), ["T-9999"], base_branch="task/main")
    assert auto["base_branch"] == "task/main" and auto["branch"] is None

    # 카드 축 — 옛 "통합 브랜치 미선언 = 무대상" 안내는 어느 사본에도 없고,
    # 두 카드 계열이 정지 규칙과 자동 장부의 실제 형상을 싣는다.
    surfaces = _close_branch_surfaces()
    assert len(surfaces) >= 2 * (1 + len(TEMPLATE_NAMES)), (
        f"종결 브랜치 계약 카드 표면이 너무 적다: {[str(p) for p in surfaces]}"
    )
    # 릴리즈 업그레이드 노트도 같은 계약을 사용자에게 말하는 자리다 — 카드만 고치고 노트에
    # 옛 문구를 남기면 채택자가 읽는 두 기준이 다시 갈린다(옛 문구 sweep 대상에 포함).
    stale_sweep = surfaces + [REPO / "CHANGELOG.md"]
    # 계약 문장을 싣는 자리 — 종결 단계표(운영 상세)와 위임 카드 본문(SKILL·command 팔레트).
    finish_details = [p for p in surfaces if p.name == "operational-details.md"
                      and "pm-wave-finish" in p.as_posix()]
    delegate_cards = [p for p in surfaces if "pm-dev-delegate" in p.as_posix()
                      and p.name != "operational-details.md"]
    assert len(finish_details) == 1 + len(TEMPLATE_NAMES), finish_details
    assert len(delegate_cards) >= 1 + len(TEMPLATE_NAMES), delegate_cards
    for path in stale_sweep:
        text = path.read_text(encoding="utf-8")
        stale = _CLOSE_SKIP_CLAIM_RE.search(text)
        assert stale is None, (
            f"{path.relative_to(REPO)}: 기준 브랜치 미선언을 비차단 건너뛰기로 안내 — "
            f"엔진은 차단한다 ({stale.group(0) if stale else ''})"
        )
    for path in finish_details + delegate_cards:
        text = path.read_text(encoding="utf-8")
        assert "묶음 브랜치 미선언" in text, (
            f"{path.relative_to(REPO)}: 실제 무대상 축(묶음 브랜치 미선언) 누락")
        assert "base_branch" in text and "정지" in text, (
            f"{path.relative_to(REPO)}: 기준 브랜치 미선언 = 정지 계약 누락")


def test_completion_guidance_uses_cluster_finish_not_direct_complete():
    """AT-005: full/lite·wiki·카드는 direct complete 대신 정상/복구/상태 분리를 안내한다."""
    entry_guides = [
        TEMPLATES / "claude_code" / "CLAUDE.md",
        TEMPLATES / "claude_code" / "CLAUDE.lite.md",
        TEMPLATES / "codex" / "AGENTS.md",
        TEMPLATES / "opencode" / "AGENTS.md",
        TEMPLATES / "opencode" / "AGENTS.lite.md",
    ]
    methodology = [
        REPO / ".project_manager" / "wiki" / "pm_role.md",
        REPO / ".project_manager" / "wiki" / "pm_playbook.md",
        REPO / ".project_manager" / "wiki" / "tickets" / "README.md",
    ]
    for name in TEMPLATE_NAMES:
        methodology.extend((
            _wiki(name) / "pm_role.md",
            _wiki(name) / "pm_playbook.md",
        ))
    direct_command = re.compile(
        r"(?m)^\s*(?:\{\{PY\}\}\s+|python3\s+)?\.project_manager/tools/board\.py\s+complete\b"
    )
    for path in entry_guides + methodology:
        text = path.read_text(encoding="utf-8")
        assert direct_command.search(text) is None, (
            f"{path.relative_to(REPO)}: 사용자-facing direct board complete 처방 잔존"
        )
        assert "ticket_finish.py" in text or "/pm-wave-finish" in text, (
            f"{path.relative_to(REPO)}: 묶음 종결 진입 누락"
        )

    finish_surfaces = [
        REPO / ".claude" / "skills" / "pm-wave-finish" / "SKILL.md",
        REPO / ".claude" / "skills" / "pm-wave-finish" / "references" /
        "operational-details.md",
        TEMPLATES / "opencode" / ".opencode" / "command" / "pm-wave-finish.md",
    ]
    for name in TEMPLATE_NAMES:
        finish_surfaces.extend(_shipped_cards(name, "pm-wave-finish"))
        finish_surfaces.extend(sorted(
            (TEMPLATES / name).glob(
                "*/skills/pm-wave-finish/references/operational-details.md"
            )
        ))
    haystack = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(set(finish_surfaces))
    )
    for marker in (
        "ticket done", "cluster closed", "slot released",
        "--reconcile-integrated", "--integrated-rev", "--legacy-base-rev",
        "--user-ack", "--repo", "--slot", "dirty slot",
    ):
        assert marker in haystack, f"종결/복구 기준 marker 누락: {marker}"
