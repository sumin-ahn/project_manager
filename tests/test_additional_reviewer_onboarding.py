"""추가 리뷰어(additional reviewer) 카드·활성문서 계약 (T-0590·T-0597·T-0598·T-0887).

사람이 부르는 역할 이름은 **추가 리뷰어**다(T-0597). `additional_reviewer*` 는 모듈 파일 이름·raw
파일 접두처럼 이미 기록된 산출물에 박힌 기계 식별자와 대상 튜플 키로만 남는다. 이 파일이 못박는
2축:

1. **스위치 없음** — 추가 리뷰어는 developer·architect 와 같이 부르면 도는 역할이다. 켜고 끄는
   키도, 이 역할만의 opt-in 질문·`--force` 강제 진입도 없다(T-0887). 채택자가 적는 것은 대상
   튜플(harness/model/reasoning) 하나이고, 카드·매뉴얼은 리뷰마다·라운드 재개마다 비용을 다시
   묻지 않는다. 라운드/wave 상한은 기계적 anti-loop 정지이고, 정상 수렴 ack 는 PM 자율이다.
2. **역할 이름 전수** — 활성 출하 표면(스킬 카드·역할 카드·방법론 wiki·README·부트스트랩 첫 턴
   카드)이 폐기 이름을 싣지 않는다. 인벤토리를 파일 열거로 만들어 새 카드·새 타깃이 목록을
   우회하지 못하게 한다.

값 드리프트를 테스트로 막는 이유: 실행 해소(하네스→실 명령)는 additional_reviewer 코어가 하고
카드·문서는 값만 싣는다. 그래서 카드의 프로필 튜플이 어긋나도 런타임이 즉시 알려주지 않는다 —
여기서 잡는다.
"""
from __future__ import annotations

import argparse
import importlib.util
import subprocess
from pathlib import Path

import pytest

from _repo_owned_inventory import OWNED, repo_owned_paths
from test_terminology import _shipped_text_surface

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
TEMPLATE_DIRS = ("claude_code", "codex", "opencode")

# 채택자가 `local.conf` 에 적어야 하는 정확한 3키 (순서 포함) — 문서·카드가 이 튜플을 그대로
# 싣는지 본다. 엔진 상수를 읽지 않고 여기 리터럴로 둔다 — 상수와 함께 조용히 바뀌면 가드가 아니다.
EXPECTED_DEFAULTS = (
    ("additional_reviewer.harness", "codex"),
    ("additional_reviewer.model", "gpt-5.6-sol"),
    ("additional_reviewer.reasoning", "max"),
)

# 폐지된 라운드 연장 승인 플래그 (T-0593) — 출하 문서에서 0 이어야 한다(축 5 가드).
RETIRED_ROUND_ACK_FLAG = "--ack-rounds"
# 수렴 게이트 카드 서술 계약 — 문장이 아니라 상한·발산·terminal stop 요소를 못박는다.
# 두 판정 경계는 엔진(`_convergence_refusal`)과 글자로 맞춘다 — 상한 도달은 must-fix 잔존과 무관한
# 차단이고(사유 라벨만 `cap-unresolved`/`cap-reached` 로 갈린다), 조기 차단은 **strict 증가**만이다
# (평탄 3→2→2 는 조기 차단이 아니라 상한에서 걸린다). 이 둘을 느슨하게 적으면 카드가 "must-fix 0
# 이면 4라운드째가 열린다"·"평탄도 조기 차단" 같은 없는 경로를 가르친다.
CONVERGENCE_GATE_CONTRACTS = (
    "additional_reviewer.rounds_max",
    "라운드 상한 2회",
    "must-fix 잔존과 무관하게 차단",
    "발산 조기 차단",
    "현재 티켓을 정지",
    "사용자에게 보고",
    "새 티켓·분할·재설계로 잔여를 넘기지 않는다",
)

# 세 하네스의 위임 카드 — claude 는 루트 canonical, codex·opencode 는 타깃 소유 override 다.
DELEGATE_CARDS = (
    ".claude/skills/pm-dev-delegate/SKILL.md",
    "templates/codex/.agents/skills/pm-dev-delegate/SKILL.md",
    "templates/opencode/.claude/skills/pm-dev-delegate/SKILL.md",
)

CANONICAL_PM_REVIEW = REPO / ".claude" / "skills" / "pm-review" / "SKILL.md"
CODEX_PM_REVIEW = (
    REPO / "templates" / "codex" / ".agents" / "skills" / "pm-review" / "SKILL.md"
)
SHARED_PM_REVIEW_CARDS = (
    CANONICAL_PM_REVIEW,
    REPO / "templates" / "claude_code" / ".claude" / "skills" / "pm-review" / "SKILL.md",
    REPO / "templates" / "opencode" / ".claude" / "skills" / "pm-review" / "SKILL.md",
)


def _card_with_operational_details(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    details = path.parent / "references" / "operational-details.md"
    if details.is_file():
        text += "\n" + details.read_text(encoding="utf-8")
    return text


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def board():
    return _load("board")


@pytest.fixture(scope="module")
def pm_update():
    return _load("pm_update")


def _additional_reviewer():
    """실행 코어 — **테스트만** 읽는다(온보딩 경로는 import 하지 않는다·키 대조용)."""
    return _load("additional_reviewer")


def _parse_conf(text: str) -> dict[str, str]:
    """local.conf 활성 키만 파싱(주석 제외·last-wins) — 엔진 reader 와 동치."""
    conf: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        conf[key.strip()] = value.strip()
    return conf


def test_codex_pm_review_card_states_the_caps_without_a_consent_axis():
    """수렴 게이트의 상한·발산·terminal stop이 codex 카드에 명시된다 (동의 축은 없다)."""
    text = _card_with_operational_details(CODEX_PM_REVIEW)
    assert "additional_reviewer.enabled" not in text
    assert "기계적 anti-loop 정지" in text
    assert "--rounds-report" in text
    for contract in CONVERGENCE_GATE_CONTRACTS:
        assert contract in text, f"수렴 게이트 서술 누락: {contract}"
    # 재개 ack 가 남은 축은 wave 예산 하나 — 라운드 축엔 연장 승인 경로가 없다.
    assert "--ack-wave" in text
    assert RETIRED_ROUND_ACK_FLAG not in text
    # 폐기된 규율: 재개 때마다 사용자 승인을 요구하던 문장.
    assert "사용자가 계속을 승인한 경우에만" not in text


def test_codex_pm_review_card_uses_codex_skill_entry_and_active_role_name():
    """codex 판은 `$pm-review` 진입 표기 + 활성 역할 이름(추가 리뷰어)을 쓴다."""
    text = CODEX_PM_REVIEW.read_text(encoding="utf-8")
    assert "# $pm-review — 추가 리뷰어 교차검증 게이트" in text
    assert "/pm-review —" not in text
    assert "additional reviewer" in text
    for key, value in EXPECTED_DEFAULTS:
        assert f"{key}={value}" in text, f"카드가 프로필 튜플을 안 싣는다: {key}"


def test_shared_pm_review_cards_use_active_role_and_the_caps():
    """공용 카드도 추가 리뷰어 역할·수렴 게이트 규율을 고정한다 (동의 축은 없다)."""
    for path in SHARED_PM_REVIEW_CARDS:
        text = path.read_text(encoding="utf-8")
        details = path.parent / "references" / "operational-details.md"
        if details.is_file():
            text += "\n" + details.read_text(encoding="utf-8")
        assert "# /pm-review — 추가 리뷰어 교차검증 게이트" in text, path
        assert "additional reviewer" in text, path
        assert "additional_reviewer.enabled" not in text, path
        for key, value in EXPECTED_DEFAULTS:
            assert f"{key}={value}" in text, (path, key)
        assert "기계적 anti-loop 정지" in text, path
        for contract in CONVERGENCE_GATE_CONTRACTS:
            assert contract in text, (path, contract)
        assert "--ack-wave" in text, path
        assert RETIRED_ROUND_ACK_FLAG not in text, path
        assert "사용자가 계속을 승인한 경우에만" not in text, path
        assert "리뷰마다·라운드 상한 재개마다 사용자에게 비용을 다시 묻지 않는다" in text, path


def test_shared_pm_review_cards_stay_byte_identical():
    """canonical ↔ claude/opencode 템플릿 미러는 byte-identical(전파 무드리프트)."""
    canonical = CANONICAL_PM_REVIEW.read_bytes()
    for path in SHARED_PM_REVIEW_CARDS[1:]:
        assert path.read_bytes() == canonical, f"pm-review 카드 드리프트: {path}"


def test_codex_pm_review_override_is_registered_in_flavor_manifest():
    """codex flavor manifest 의 file override — 없으면 공유 카드 렌더가 이 판을 덮는다.

    상위 `.agents/skills @render @source=.claude/skills` 디렉토리 항목보다 구체적인 file
    remap 이 이겨야 codex 전용 Bash timeout 절이 살아남는다(pm-dev-delegate 와 같은 기전).
    """
    manifest = (
        REPO / "templates" / "codex" / ".project_manager" / "engine.manifest"
    ).read_text(encoding="utf-8")
    assert (
        ".agents/skills/pm-review/SKILL.md    @render "
        "@source=templates/codex/.agents/skills/pm-review/SKILL.md"
    ) in manifest


# ── 축 2: 활성 문서의 역할 이름·비용 규율 ────────────────────────────────────

ACTIVE_DOCS = (
    REPO / ".project_manager" / "wiki" / "pm_role.md",
    REPO / ".project_manager" / "wiki" / "pm_playbook.md",
    REPO / "README.md",
    REPO / "docs" / "portability.md",
)


def test_active_docs_use_the_additional_reviewer_role_name():
    """활성 매뉴얼/플레이북/README/이식성 문서가 역할을 '추가 리뷰어'로 부른다."""
    for path in ACTIVE_DOCS:
        assert "추가 리뷰어" in path.read_text(encoding="utf-8"), path


def test_playbook_states_the_profile_tuple_and_no_switch():
    """사용자가 설정하는 자리에 프로필 튜플만 있고 채널 스위치가 없다 (T-0887)."""
    text = (REPO / ".project_manager" / "wiki" / "pm_playbook.md").read_text(
        encoding="utf-8"
    )
    for key, value in EXPECTED_DEFAULTS:
        assert f"{key}={value}" in text
    assert "additional_reviewer.enabled" not in text
    assert "이 역할만 켜고 끄는 스위치는 없다" in text
    assert "reviewer_cmd" in text          # 폐지된 통짜 커맨드 키를 이름으로 지목


def test_readme_documents_the_role_without_a_channel_switch():
    """README 는 과금 사실은 분명히 적되 이 역할만의 스위치를 만들지 않는다 (T-0887)."""
    text = (REPO / "README.md").read_text(encoding="utf-8")
    assert "additional_reviewer.enabled" not in text
    assert "부르면 도는\n역할" in text
    assert "채널을 켜고 끄는 스위치도, 이 역할만의 별도 승인 축도 없다" in text
    for key, value in EXPECTED_DEFAULTS:
        assert f"{key}={value}" in text
    # 폐지된 통짜 커맨드 키를 이름으로 지목한다(무엇이 더 이상 안 읽히는지).
    assert "`reviewer_cmd` 통짜 커맨드는 더 이상 읽히지 않는다" in text


def test_pm_role_makes_cap_ack_autonomous_not_a_cost_gate():
    """PM 매뉴얼: wave 예산 ack 는 자율 영역, 라운드 축은 연장 승인 자체가 없다."""
    text = (REPO / ".project_manager" / "wiki" / "pm_role.md").read_text(
        encoding="utf-8"
    )
    # 남은 ack 축(wave 예산)이 *자율* 절에 들어 있어야 한다 — 사용자 게이트 절이 아니라.
    autonomous = text.split("**자율+사후")[1].split("**사용자 게이트")[0]
    assert "--ack-wave" in autonomous
    assert "정상 수렴 ack" in autonomous
    # 폐지된 라운드 연장 승인은 자율 목록에서도 문서 전체에서도 사라져야 한다.
    assert RETIRED_ROUND_ACK_FLAG not in text
    assert "이 역할만의 별도 승인 축은 없다" in text
    assert "기계적 anti-loop 정지" in text
    assert "리뷰 라운드 축은 연장 승인이 없고" in text
    assert "현재 티켓을 정지" in text and "사용자에게 보고" in text
    assert "--confirm-fix" not in text


def test_active_docs_have_no_per_round_user_cost_approval_rule():
    """폐기 규율(라운드 재개마다 사용자 비용 승인)이 활성 문서에 남아 있지 않다."""
    retired = (
        "사용자 승인 후에만 `--ack-wave`",
        "승인 없이 `--ack-rounds` 금지",
        "사용자가 계속을 승인한 경우에만",
    )
    surfaces = (*ACTIVE_DOCS, CODEX_PM_REVIEW)
    for path in surfaces:
        text = path.read_text(encoding="utf-8")
        for phrase in retired:
            assert phrase not in text, f"{path}: 폐기된 비용 재승인 규율 잔존 — {phrase}"


# ── 축 2: 활성 출하 PM 표면 전수의 역할 이름 ─────────────────────────────────
#
# 이름 변경은 **사람이 부르는 역할**에만 적용된다. 활성 표면(출하 스킬 카드 · 방법론 wiki ·
# README/이식성 문서 · 부트스트랩 첫 턴 카드)에서 폐기 이름을 몰아내되, 다음은 건드리지 않는다:
#   - 기계 식별자 — `additional_reviewer.py` · `additional_reviewer.harness` · `reviewer_cmd`
#   - 히스토리 — ADR · CHANGELOG · done 티켓 · archive log · 과거 지적 인용("codex 게이트 must-fix")
#   - 실제로 *수신 하네스*(선택된 기본 codex)를 가리키는 codex 전용 문맥
# 인벤토리를 파일 열거로 만드는 이유: 새 카드/새 타깃이 목록을 우회해 폐기 이름을 다시 실어도
# 자동으로 red 가 되게 하기 위해서다(하드코딩 목록은 반드시 뒤처진다).

# 폐기된 역할 이름 — 사람이 부르는 이름은 **추가 리뷰어**다. 폐기 표기는 T-0597 sweep 대상이고,
# 인벤토리에 agent 카드가 없어 opencode architect 카드 하나가 R1 까지 살아남았다(카드 사각).
#
# 리터럴 분할: 폐기 낱말 가드(`tests/test_terminology.py`)가 이 원장 자체를 잔존으로 잡지 않게.
_RETIRED_ROLE_PREFIX = "외" + "부"
RETIRED_ROLE_PHRASES = (
    f"codex {_RETIRED_ROLE_PREFIX} 교차검증",
    "codex 교차검증",
    f"{_RETIRED_ROLE_PREFIX} 리뷰어",
)

# 폐기된 활동 명사 — 활동 이름은 **추가 리뷰**다(T-0599). 역할(누가)과 축이 달라 따로 둔다:
# 구키 제거 릴리즈에서 한쪽만 풀릴 수 있고, 활동 명사는 역할 이름이 없는 문장("ticket → dev →
# 폐기 활동 명사)에도 박혀 있어 역할 스캔이 통째로 놓쳤다.
RETIRED_ACTIVITY_PHRASES = (f"{_RETIRED_ROLE_PREFIX}리뷰",)

# 활성 표면 스캔이 보는 폐기 표현 전체. 두 축이 같은 인벤토리를 쓰므로 스캔은 하나다 —
# 축마다 스캔을 복사하면 새 표면이 한쪽 목록에만 들어가는 절반 커버가 생긴다.
RETIRED_REVIEW_PHRASES = (*RETIRED_ROLE_PHRASES, *RETIRED_ACTIVITY_PHRASES)

SKILL_CARD_ROOTS = (
    REPO / ".claude" / "skills",
    REPO / "templates" / "claude_code" / ".claude" / "skills",
    REPO / "templates" / "opencode" / ".claude" / "skills",
    REPO / "templates" / "codex" / ".agents" / "skills",
)

# agent 카드는 4 네임스페이스가 서로 다른 경로·포맷(codex 는 TOML)이라 스킬 카드 인벤토리로는
# 잡히지 않는다 — 역할 이름을 싣는 표면이므로 따로 열거한다(glob 패턴까지 명시).
AGENT_CARD_ROOTS = (
    (REPO / ".claude" / "agents", "*.md"),
    (REPO / "templates" / "claude_code" / ".claude" / "agents", "*.md"),
    (REPO / "templates" / "opencode" / ".opencode" / "agents", "*.md"),
    (REPO / "templates" / "codex" / ".codex" / "agents", "*.toml"),
)


def _shipping_agent_cards() -> list[Path]:
    """출하되는 역할 정의 카드 전수 (4 네임스페이스 × 역할). 열거 자체가 판정의 본질."""
    cards: list[Path] = []
    for root, pattern in AGENT_CARD_ROOTS:
        assert root.is_dir(), f"agent 네임스페이스 없음: {root}"
        found = sorted(root.glob(pattern))
        assert found, f"agent 카드 0개 — 인벤토리 앵커가 깨졌다: {root}"
        cards.extend(found)
    return cards

METHODOLOGY_SURFACES = (
    REPO / ".project_manager" / "wiki" / "pm_role.md",
    REPO / ".project_manager" / "wiki" / "pm_playbook.md",
    *(
        REPO / "templates" / flavor / ".project_manager" / "wiki" / name
        for flavor in TEMPLATE_DIRS
        for name in ("pm_role.md", "pm_playbook.md")
    ),
    REPO / "README.md",
    REPO / "docs" / "portability.md",
)

# 부트스트랩 카드는 코드가 문자열로 만든다 — 렌더 결과와 4 사본 소스를 함께 본다.
BOOTSTRAP_SOURCES = (
    TOOLS / "pm_bootstrap.py",
    *(
        REPO / "templates" / flavor / ".project_manager" / "tools" / "pm_bootstrap.py"
        for flavor in TEMPLATE_DIRS
    ),
)

CARD_IDENTITY = {
    "repo": "project_manager",
    "session": "project_manager_1",
    "slot": "work/project_manager_1",
    "slot_path": "/home/x/work/project_manager_1",
    "branch": "release/v1.0.6",
    "others": [],
    "protected_branch": None,
}


@pytest.fixture(scope="module")
def pm_bootstrap():
    return _load("pm_bootstrap")


def _shipping_skill_cards() -> list[Path]:
    """출하되는 PM 스킬 카드 전수 (4 네임스페이스 × 카드). 열거 자체가 판정의 본질."""
    cards: list[Path] = []
    for root in SKILL_CARD_ROOTS:
        assert root.is_dir(), f"스킬 네임스페이스 없음: {root}"
        found = sorted(root.glob("*/SKILL.md"))
        assert found, f"스킬 카드 0개 — 인벤토리 앵커가 깨졌다: {root}"
        cards.extend(found)
    return cards


def _active_pm_surfaces() -> list[Path]:
    return [*_shipping_skill_cards(), *_shipping_agent_cards(),
            *METHODOLOGY_SURFACES, *BOOTSTRAP_SOURCES]


def test_active_pm_surface_inventory_covers_agent_cards():
    """인벤토리가 agent 카드 4 네임스페이스를 실제로 포함한다 — 카드 사각(R1 실측)의 재발 차단."""
    scanned = {path.relative_to(REPO).as_posix() for path in _active_pm_surfaces()}
    for rel in (
        ".claude/agents/architect.md",
        "templates/claude_code/.claude/agents/architect.md",
        "templates/opencode/.opencode/agents/architect.md",
        "templates/codex/.codex/agents/architect.toml",
    ):
        assert rel in scanned, f"agent 카드가 인벤토리 밖: {rel}"


def _surface_label(path: Path) -> str:
    """진단용 자리 이름 — repo 안이면 repo-상대 경로, 밖이면 파일 이름.

    sensitivity 가 합성 표면(tmp)으로 검출기를 태울 수 있어야 하므로 repo 결합을 여기서만 푼다.
    """
    try:
        return path.relative_to(REPO).as_posix()
    except ValueError:
        return path.name


def _phrase_residue(paths, phrases, allowed_lines=None) -> list[str]:
    """주어진 표면들에서 폐기 표현이 놓인 자리 — `<자리>:<lineno> — <표현>` 목록.

    잔존 가드 3축(역할·활동 명사·구키)이 같은 검출기를 쓰게 만드는 단일 seam 이다. 축마다 스캔을
    베끼면 sensitivity 를 축마다 다시 증명해야 하고, 대개 한 축만 증명한 채 남는다.

    `allowed_lines` = `{repo상대경로: (허용 줄 텍스트, …)}` — **줄 단위 예외**다. 파일 통째
    예외는 그 파일에 새로 들어온 사용까지 영영 가려주지만, 줄 단위는 적어 둔 그 줄만 뺀다.
    """
    hits: list[str] = []
    for path in paths:
        assert path.is_file(), f"인벤토리 대상 부재: {path}"
        allowed = frozenset((allowed_lines or {}).get(_surface_label(path), ()))
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), 1
        ):
            if line.strip() in allowed:
                continue
            hits += [
                f"{_surface_label(path)}:{lineno} — {phrase}"
                for phrase in phrases
                if phrase in line
            ]
    return hits


def test_active_pm_surfaces_drop_the_retired_reviewer_names():
    """활성 출하 PM 표면 전수에 폐기된 역할 이름·활동 명사가 없다."""
    residue = _phrase_residue(_active_pm_surfaces(), RETIRED_REVIEW_PHRASES)
    assert not residue, "폐기된 명칭 잔존(활성 표면):\n  " + "\n  ".join(residue)


def test_retired_phrase_scan_detects_an_injected_residue(tmp_path):
    """주입한 폐기 표현을 검출기가 실제로 잡고, 복원하면 다시 green (비공허 sensitivity).

    잔존 0 단언은 스캔이 죽어도(빈 인벤토리·오탈자 패턴) 통과한다 — 검출기 자체를 합성
    표면으로 태워 "잡을 수 있음"을 증명한다.
    """
    surface = tmp_path / "SKILL.md"
    clean = "위임(`pm_delegate.py`)과 추가 리뷰(`additional_reviewer.py`)는 raw 를 예약한다.\n"
    surface.write_text(clean, encoding="utf-8")
    assert _phrase_residue([surface], RETIRED_REVIEW_PHRASES) == []

    activity = RETIRED_ACTIVITY_PHRASES[0]
    surface.write_text(
        clean + f"금지(반드시 ticket → dev → {activity})\n", encoding="utf-8")
    hits = _phrase_residue([surface], RETIRED_REVIEW_PHRASES)
    assert hits == [f"SKILL.md:2 — {activity}"], hits

    surface.write_text(clean, encoding="utf-8")
    assert _phrase_residue([surface], RETIRED_REVIEW_PHRASES) == []


# 활동 명사 sweep 이 닿은 엔진 파일 — 위임/추가 리뷰 실행 축 4종(T-0600). **엔진 파일 전체를
# 잔존 스캔 인벤토리(`_active_pm_surfaces`)에 넣지는 않는다**: 거긴 과거 지적 인용·릴리즈 서술
# 같은 히스토리가 사는 자리라(`test_role_rename_keeps_transport_identifiers_and_history` 가 그
# 경계를 값으로 고정한다) 전면 스캔은 과거 기록 개서를 요구한다. 여기서 보는 건 **활동 명사
# 하나**뿐이고, 역할 이름·codex 인용은 대상이 아니다.
_ACTIVITY_SWEEP_ENGINE_FILES = (
    "pm_delegate.py", "additional_reviewer.py", "pm_relay.py", "pm_handoff.py",
    "pm_import.py",
)


@pytest.mark.parametrize("name", _ACTIVITY_SWEEP_ENGINE_FILES)
def test_swept_engine_files_drop_the_retired_activity_noun(name):
    """sweep 대상 엔진 파일에 폐기 활동 명사가 0건이다 (docstring·주석·CLI help 포함)."""
    residue = _phrase_residue([TOOLS / name], RETIRED_ACTIVITY_PHRASES)
    assert not residue, "폐기된 활동 명사 잔존(엔진):\n  " + "\n  ".join(residue)


def test_engine_cli_help_names_the_activity_as_additional_review(capsys):
    """**사용자에게 렌더되는** 엔진 CLI help 가 활동을 '추가 리뷰'로 부른다.

    파일 텍스트 스캔이 아니라 argparse 가 실제로 찍는 문자열을 태운다 — 사용자 노출 표면은
    소스 어디에 적혔는지가 아니라 출력이 진실이고, 히스토리 주석과 섞이지 않는다.
    """
    delegate = _load("pm_delegate")
    with pytest.raises(SystemExit):
        delegate._cmd_raw(["--help"])
    help_text = capsys.readouterr().out

    assert "추가 리뷰" in help_text
    for phrase in RETIRED_REVIEW_PHRASES:
        assert phrase not in help_text, f"CLI help 에 폐기 명칭 잔존 — {phrase}"


def test_retired_activity_noun_scan_covers_the_swept_surfaces():
    """활동 명사 sweep 대상 8파일이 스캔 인벤토리 안에 있다 — 좁은 스캔의 false-green 방지."""
    scanned = {path.relative_to(REPO).as_posix() for path in _active_pm_surfaces()}
    expected = {
        f"{prefix}.claude/skills/pm-dev-delegate/SKILL.md"
        for prefix in ("", "templates/claude_code/", "templates/opencode/")
    } | {
        "templates/codex/.agents/skills/pm-dev-delegate/SKILL.md",
        ".project_manager/wiki/pm_role.md",
        *(f"templates/{flavor}/.project_manager/wiki/pm_role.md"
          for flavor in TEMPLATE_DIRS),
    }
    assert len(expected) == 8
    assert expected <= scanned, f"sweep 대상이 스캔 밖: {sorted(expected - scanned)}"


def test_bootstrap_first_turn_card_names_the_additional_reviewer(
    pm_bootstrap, monkeypatch
):
    """부트스트랩 첫 턴 카드의 additional_reviewer 줄이 역할을 '추가 리뷰어'로 부른다.

    수신 하네스는 채택자 `local.conf` 설정값이라 카드 문구가 고정하지 않는다 — 반면 실행
    backbone `additional_reviewer.py` 는 기계 식별자로 그대로 남는다.
    """
    for marker in ("CODEX_THREAD_ID", "CODEX_CI"):
        monkeypatch.delenv(marker, raising=False)  # 하네스 감지 절 append 제거(결정론)
    inst = pm_bootstrap.PmBootstrap.__new__(pm_bootstrap.PmBootstrap)
    card = inst._build_command_card_markdown(CARD_IDENTITY)

    review_lines = [ln for ln in card.splitlines() if "additional_reviewer.py" in ln]
    assert len(review_lines) == 1, f"additional_reviewer 줄이 1개가 아님: {review_lines}"
    assert "추가 리뷰어" in review_lines[0], review_lines[0]
    for phrase in RETIRED_REVIEW_PHRASES:
        assert phrase not in card, f"카드에 폐기 이름 잔존 — {phrase}"


def test_role_rename_keeps_transport_identifiers_and_history():
    """이름 변경은 사람 표면 한정 — 기계 식별자와 히스토리는 그대로 둔다.

    인벤토리 밖이어야 하는 것(엔진 히스토리 주석)을 값으로 못박아, 가드가 히스토리까지
    번지지 않게 경계를 고정한다.
    """
    # 실행/설정의 기계 이름은 활성 카드에서도 계속 쓰인다.
    card = CANONICAL_PM_REVIEW.read_text(encoding="utf-8")
    assert "additional_reviewer.py" in card
    assert "additional_reviewer.harness=codex" in card
    playbook = (REPO / ".project_manager" / "wiki" / "pm_playbook.md").read_text(
        encoding="utf-8"
    )
    assert "reviewer_cmd" in playbook       # 레거시 채택자 키도 이름이 바뀌지 않는다

    # 과거 codex 게이트가 낸 지적 인용은 엔진 주석의 히스토리다 — 인벤토리 대상이 아니다.
    history = TOOLS / "pm_handoff.py"
    assert "codex 교차검증 must-fix" in history.read_text(encoding="utf-8")
    assert history not in set(_active_pm_surfaces())


def test_codex_cards_keep_dollar_skill_entry_notation():
    """codex 네임스페이스 카드는 `$<스킬>` 진입 표기를 유지한다(claude/opencode 는 `/`)."""
    codex_root = REPO / "templates" / "codex" / ".agents" / "skills"
    for card in sorted(codex_root.glob("*/SKILL.md")):
        text = card.read_text(encoding="utf-8")
        heading = next(ln for ln in text.splitlines() if ln.startswith("# "))
        assert heading.startswith(f"# ${card.parent.name}"), (card, heading)
        assert not heading.startswith(f"# /{card.parent.name}"), card


# ── 축 5: 폐지된 라운드 연장 승인 플래그 잔재 0 (T-0598) ─────────────────────
#
# T-0593 이 라운드 연장 승인을 엔진에서 폐지했다(호출하면 rc=1 거부·아무것도 실행 안 함). 출하
# 문서가 그 플래그를 계속 가르치면 PM 이 존재하지 않는 출구를 시도하고, 문서-엔진 모순이 그대로
# 운영 지침이 된다. 그래서 출하 표면의 **잔존 0** 을 기계로 못박는다. 잔존이 정당한 자리는 셋뿐:
#   - `CHANGELOG.md` — 릴리즈 히스토리(그 시점의 동작 서술).
#   - 엔진 `additional_reviewer.py` — 폐지 거부 안내 문구(구 장부 필드는 제거됐다 · T-0772).
#   - **명시된 테스트 파일들** — 폐지 동작(거부)·구 장부 해석을 단언하는 테스트 자신.
# `tests/` 를 통째로 빼지 않는 이유: 그러면 테스트 docstring 에 남은 *옛 흐름 서술*(실제로 R1 이
# `test_additional_reviewer.py` 에서 잡았다)을 가드가 영영 못 본다. 파일을 이름으로 적고, 각 파일이
# 실제로 그 문자열을 갖고 있는지까지 단언해 목록이 썩지 않게 한다.
# 히스토리 디렉토리 제외는 두지 않는다 — dev-state(log·decisions·tickets 상태·sealed spike)는 PM
# 홈 repo 소유라 이 제품 repo 스캔에 애초에 없다(`.gitkeep` 뿐). 검증할 수 없는 공허한 예외는
# allowlist 를 헐겁게만 만든다.
# 잔존 스캔 3축(폐지 플래그·구 게이트 키·구 노브 키)이 공유하는 확장자 인벤토리. 산문·엔진만
# 보던 `.md`/`.py` 에 **실행/설정 표면**을 더한다(T-0600) — 진입 스크립트(`.sh`·`.cmd`)·opencode
# 설정(`.jsonc`)·codex agent 카드와 설정(`.toml`)도 폐기 키/플래그를 실을 수 있는 자리이고,
# 확장자 하나가 빠지면 그 표면의 잔존은 영영 안 보인다(현행 실잔재는 0 — 그 상태를 못박는다).
_RETIRED_ACK_SCAN_SUFFIXES = {".md", ".py", ".sh", ".cmd", ".jsonc", ".toml"}
# 엔진의 폐지 안내·구 장부 해석이 사는 파일 (canonical + 템플릿 미러 4벌 모두 같은 이름).
_RETIRED_ACK_ENGINE_FILE = "additional_reviewer.py"
# 폐지 동작을 단언하느라 플래그 리터럴이 정당하게 남는 테스트 (파일명 명시 — 디렉토리 통째 아님).
_RETIRED_ACK_TEST_FILES = (
    "tests/test_additional_reviewer_onboarding.py",   # 이 가드 자신(상수·부재 단언)
    "tests/test_additional_reviewer.py",                  # 어느 표면에서도 거부됨을 단언
    "tests/test_raw_output_ledger.py",                # 폐지 플래그 argv 의 장부 무변경
)


def _retired_ack_scan_targets() -> list[Path]:
    """폐지 플래그 잔존을 검사할 출하 표면 (allowlist 제외 후)."""
    targets: list[Path] = []
    for path in repo_owned_paths(REPO, ".", mode=OWNED):
        if not path.is_file() or path.suffix.lower() not in _RETIRED_ACK_SCAN_SUFFIXES:
            continue
        rel = path.relative_to(REPO).as_posix()
        if rel == "CHANGELOG.md" or rel in _RETIRED_ACK_TEST_FILES:
            continue
        if path.name == _RETIRED_ACK_ENGINE_FILE:
            continue
        targets.append(path)
    return targets


def test_retired_round_ack_flag_has_no_residue_in_shipping_surfaces():
    """출하 문서·코드 전수에 폐지된 라운드 연장 승인 플래그가 0건이다."""
    residue = [
        f"{path.relative_to(REPO).as_posix()}:{lineno}"
        for path in _retired_ack_scan_targets()
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), 1)
        if RETIRED_ROUND_ACK_FLAG in line
    ]
    assert not residue, (
        f"폐지된 연장 승인 플래그 잔존({RETIRED_ROUND_ACK_FLAG}) — 새 흐름"
        "(3R 상한·발산(증가) 차단·confirm-fix 1회·출구=재설계/분할)으로 고쳐라:\n  "
        + "\n  ".join(residue)
    )


def test_retired_round_ack_scan_covers_the_swept_surfaces():
    """스캔 인벤토리가 sweep 대상을 실제로 포함한다 — 빈/좁은 스캔의 false-green 방지."""
    scanned = {path.relative_to(REPO).as_posix() for path in _retired_ack_scan_targets()}
    for rel in (
        ".claude/skills/pm-review/SKILL.md",
        "templates/claude_code/.claude/skills/pm-review/SKILL.md",
        "templates/opencode/.claude/skills/pm-review/SKILL.md",
        "templates/codex/.agents/skills/pm-review/SKILL.md",
        ".project_manager/wiki/pm_role.md",
        ".project_manager/wiki/pm_playbook.md",
        "templates/codex/.project_manager/wiki/pm_playbook.md",
        ".project_manager/tools/pm_bootstrap.py",
    ):
        assert rel in scanned, f"sweep 대상이 스캔 밖: {rel}"


@pytest.mark.parametrize("rel", [
    "pm-import.sh",                                     # 루트 진입 파사드(bash)
    "templates/claude_code/pm-update.cmd",              # Windows 진입 파사드
    "templates/opencode/.opencode/opencode.jsonc",      # opencode 어댑터 설정
    "templates/codex/.codex/agents/code-reviewer.toml",  # codex agent 카드
    "templates/codex/.codex/config.toml",               # codex 어댑터 설정
])
def test_residue_scan_covers_the_execution_and_config_surfaces(rel):
    """확장자 인벤토리가 실행/설정 표면까지 본다 (T-0600 — 좁은 스캔의 false-green 방지)."""
    scanned = {path.relative_to(REPO).as_posix() for path in _retired_ack_scan_targets()}
    assert rel in scanned, f"스캔 밖 표면: {rel}"


def test_retired_round_ack_allowlist_entries_are_load_bearing():
    """allowlist 는 실제로 그 문자열을 담은 자리만 뺀다 — 빈 예외는 가드를 헐겁게 만든다."""
    engine = (TOOLS / _RETIRED_ACK_ENGINE_FILE).read_text(encoding="utf-8")
    assert RETIRED_ROUND_ACK_FLAG in engine      # 거부 안내 + 구 장부 필드 해석 주석
    assert "폐지" in engine
    assert RETIRED_ROUND_ACK_FLAG in (REPO / "CHANGELOG.md").read_text(
        encoding="utf-8")                        # 릴리즈 히스토리
    for rel in _RETIRED_ACK_TEST_FILES:          # 목록이 썩으면(잔존 0 파일이 남으면) red
        path = REPO / rel
        assert path.is_file(), f"allowlist 대상 부재: {rel}"
        assert RETIRED_ROUND_ACK_FLAG in path.read_text(encoding="utf-8"), (
            f"{rel} 에 더는 폐지 플래그가 없다 — allowlist 에서 빼라(스캔 대상 복귀)."
        )


def test_retired_round_ack_allowlisted_tests_teach_the_new_flow():
    """allowlist 테스트의 *산문*(모듈 docstring)은 폐지된 재개 흐름을 가르치지 않는다.

    파일 단위 allowlist 의 값은 "리터럴은 허용, 옛 흐름 서술은 불허"다 — 거부를 단언하는 코드는
    플래그를 쓸 수밖에 없지만, 그 파일의 docstring 이 "승인 후 재개"를 계속 설명하면 읽는 사람이
    폐지 사실을 놓친다(R1 실측 지적).
    """
    retired_prose = ("승인 후 `--ack-rounds`", "승인 후에만 `--ack-rounds`",
                     "`--ack-rounds`로만 재개", "`--ack-rounds` 로만 재개")
    # 이 파일 자신은 제외 — 폐기 문구를 *열거* 하는 자리라 정당하다(test_terminology `_SELF` 동형).
    self_rel = Path(__file__).resolve().relative_to(REPO).as_posix()
    offenders = [
        f"{rel} — {phrase}"
        for rel in _RETIRED_ACK_TEST_FILES
        if rel != self_rel
        for phrase in retired_prose
        if phrase in (REPO / rel).read_text(encoding="utf-8")
    ]
    assert not offenders, "테스트 산문에 폐지된 재개 흐름 잔존:\n  " + "\n  ".join(offenders)


# ── canonical ↔ 3 템플릿 parity (온보딩을 싣는 엔진·방법론) ──────────────────

@pytest.mark.parametrize(
    "relpath",
    [
        ".project_manager/tools/board.py",
        ".project_manager/tools/pm_import.py",
        ".project_manager/tools/pm_update.py",
        ".project_manager/wiki/pm_role.md",
        ".project_manager/wiki/pm_playbook.md",
    ],
)
def test_canonical_to_template_parity(relpath):
    """온보딩 계약을 담은 엔진/방법론 사본이 세 타깃에서 byte-identical."""
    canonical = (REPO / relpath).read_bytes()
    for flavor in TEMPLATE_DIRS:
        path = REPO / "templates" / flavor / relpath
        assert path.is_file(), f"템플릿 사본 없음: {path}"
        assert path.read_bytes() == canonical, (
            f"{flavor} 사본 드리프트 — pm_update 전파 필요: {relpath}"
        )


# ── 축 6: 노브 키 표기 (dot notation 단일 표기 · T-0767) ────────────────────
#
# `local.conf` 표기가 dot notation 하나로 통일되면서 이 축의 키도 `additional_reviewer.<속성>`
# 이 됐다. 구표기(flat `additional_reviewer_*`)의 **1릴리즈 fallback·감지 상수·안내 깔때기는 전부
# 제거**됐다 — 잔존 구표기는 이 모듈이 아니라 공용 로더가 conf 를 읽는 지점에서 fail-loud 로
# 막고 키 단위 교체를 처방한다(`tests/test_local_conf_notation.py`).
# 채널 스위치 키는 T-0887 이 삭제했다 — 추가 리뷰어는 부르면 도는 역할이라 켜고 끄는 축이 없다.
RETIRED_SWITCH_KEY = "additional_reviewer.enabled"

# 각 노브의 해소 함수와 엔진 기본값 — 어느 축이 어느 키를 읽는지까지 못박는다(세 키가 한 표에서
# 파생하므로 배선이 어긋나면 상한/예산이 서로의 값을 읽는다).
# 판정 라운드 상한은 이 표에 없다 — conf 노브 없이 엔진 고정값 하나다(대체 키 없음).
_KNOB_RESOLVERS = {
    "additional_reviewer.incomplete_rounds_max": ("_incomplete_round_limit", 2),
    "additional_reviewer.wave_budget": ("_wave_budget", 24),
}
KNOB_KEYS = tuple(_KNOB_RESOLVERS)


# 출하 표면 전량 스캔의 유일한 예외 — 제거키 **원장**. 그 자리는 "그 이름은 사라졌다" 를 선언
# 하므로 이름을 들고 있어야 한다: 엔진 매핑표 한 줄과, 그 표에서 생성돼 어댑터 파서가 품는 블록
# (`local_conf.render_adapter_block`). CHANGELOG 는 기록이라 시야 밖이다(`_historical_record`).
RETIRED_SWITCH_MAP_LINE = f'"{RETIRED_SWITCH_KEY}": None,'


def _retired_switch_offenders(files):
    """출하 표면에서 삭제된 채널 스위치 키를 산 전제로 든 줄 전수."""
    local_conf = _load("local_conf")
    offenders = []
    for path in files:
        inside_generated_ledger = False
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if local_conf.ADAPTER_BLOCK_BEGIN in line:
                inside_generated_ledger = True
            elif local_conf.ADAPTER_BLOCK_END in line:
                inside_generated_ledger = False
            if RETIRED_SWITCH_KEY not in line:
                continue
            if line.strip() == RETIRED_SWITCH_MAP_LINE:
                continue
            if (inside_generated_ledger
                    and line.strip().strip(",").strip('"') == RETIRED_SWITCH_KEY):
                continue
            offenders.append(f"{path.relative_to(REPO).as_posix()}:{lineno}")
    return offenders


def test_no_shipping_surface_names_the_retired_channel_switch():
    """카드·매뉴얼·엔진 어디에도 삭제된 스위치 키를 전제로 한 절차가 없다 (T-0887).

    문서 두 개 전용 단언으로는 새 카드·새 타깃 사본이 시야 밖에서 옛 절차를 되살린다. 세 하네스
    PM 이 같은 단계에서 서로 다른 절차를 읽는 것이 이 가드가 막는 형상이다.
    """
    offenders = _retired_switch_offenders(_shipped_text_surface())
    assert not offenders, (
        f"삭제된 채널 스위치 키 `{RETIRED_SWITCH_KEY}` 를 전제로 한 절차 잔존 — 대상 튜플"
        f"(harness/model/reasoning) 기준으로 고치라 (T-0887): {offenders}"
    )


def test_retired_switch_scan_detects_an_injected_residue(tmp_path, monkeypatch):
    """감도 — 스위치 전제 문구를 다시 넣으면 스캔이 그 줄을 검출한다."""
    card = tmp_path / "SKILL.md"
    card.write_text(
        f"`{RETIRED_SWITCH_KEY}=true` 로 채널을 켠 채택자는 교차검증을 돌린다.\n",
        encoding="utf-8")
    monkeypatch.setitem(globals(), "REPO", tmp_path)

    assert _retired_switch_offenders([card]) == ["SKILL.md:1"]


@pytest.mark.parametrize("relpath", DELEGATE_CARDS)
def test_delegate_cards_state_equal_authority(relpath):
    """세 하네스의 위임 카드가 모두 위임 권한 동등 규칙을 싣는다 (T-0887).

    코덱스·오픈코드가 PM 일 때 읽는 자리다 — 한 판만 빠지면 하네스가 바뀔 때 규칙이 사라진다.
    """
    text = (REPO / relpath).read_text(encoding="utf-8")
    assert "위임자는 피위임자에게 자신과 같은 권한을 준다" in text, relpath
    assert "위임 방향·하네스 조합과 무관하다" in text, relpath
    assert "접근 권한·경로·env·볼 수 있는" in text, relpath


def test_the_channel_has_no_switch_at_all(board, pm_update):
    """채널 스위치 축이 통째로 없다 — 판정식·안내·설정 키 어느 것도 남지 않았다 (T-0887).

    추가 리뷰어는 developer·architect 와 같이 부르면 도는 역할이다. 스위치가 남아 있으면 그 위에
    질문·경고·기본값 논쟁이 다시 자란다.
    """
    core = _additional_reviewer()
    local_conf = _load("local_conf")

    for retired in ("_is_enabled", "disabled_gate_notice",
                    "ADDITIONAL_REVIEWER_ENABLED_KEY"):
        assert not hasattr(core, retired), retired
    assert RETIRED_SWITCH_KEY not in local_conf.KNOWN_KEYS
    assert local_conf.LEGACY_KEY_MAP[RETIRED_SWITCH_KEY] is None   # 남은 행은 지운다
    assert RETIRED_SWITCH_KEY not in (TOOLS / "additional_reviewer.py").read_text(
        encoding="utf-8")


def test_no_entry_carries_an_optin_prompt_or_force_flag(board, pm_update):
    """온보딩 질문·1회 강제 플래그가 어느 진입에도 없다 — 동의 축을 통째로 지웠다."""
    core = _additional_reviewer()
    for module in (core, board, pm_update):
        for retired in ("prompt_additional_reviewer_optin",
                        "maybe_prompt_additional_reviewer",
                        "_commit_additional_reviewer_optin",
                        "ADDITIONAL_REVIEWER_OPTIN_BLOCK",
                        "ADDITIONAL_REVIEWER_DECLINE_BLOCK",
                        "enabled_decision_key",
                        "additional_reviewer_decision_key"):
            assert not hasattr(module, retired), (module.__name__, retired)
    actions = {
        action.option_strings[0]
        for action in core.build_arg_parser()._actions if action.option_strings
    }
    assert "--force" not in actions


def test_knob_key_constants_match_the_engine_table():
    """엔진의 노브 키 상수가 글자 단위로 이 표와 같다 (배선 드리프트 가드)."""
    core = _additional_reviewer()
    assert (core.ADDITIONAL_REVIEWER_INCOMPLETE_ROUND_LIMIT_KEY
            == "additional_reviewer.incomplete_rounds_max")
    assert core.ADDITIONAL_REVIEWER_WAVE_BUDGET_KEY == "additional_reviewer.wave_budget"
    assert set(KNOB_KEYS) == {
        core.ADDITIONAL_REVIEWER_INCOMPLETE_ROUND_LIMIT_KEY,
        core.ADDITIONAL_REVIEWER_WAVE_BUDGET_KEY,
    }
    # 판정 상한은 노브도 축도 아니다 — 키 상수·해소 함수·엔진 기본값이 모두 없다(축 제거).
    assert not hasattr(core, "ADDITIONAL_REVIEWER_ROUND_LIMIT_KEY")
    assert not hasattr(core, "_round_limit")
    assert not hasattr(core, "DEFAULT_ROUND_LIMIT")


@pytest.mark.parametrize("key", KNOB_KEYS, ids=list(KNOB_KEYS))
@pytest.mark.parametrize(
    ("conf_shape", "expected"),
    [
        pytest.param("set", 3, id="configured"),
        pytest.param("none", None, id="unset-default"),
    ],
)
def test_knob_resolution_reads_the_configured_key(key, conf_shape, expected):
    """노브 해소: 그 키가 값을 공급하고, 없으면 엔진 기본값이다(`expected=None`)."""
    core = _additional_reviewer()
    resolver_name, engine_default = _KNOB_RESOLVERS[key]
    conf = {"set": {key: "3"}, "none": {}}[conf_shape]

    resolved = getattr(core, resolver_name)(conf)
    assert resolved == (engine_default if expected is None else expected)


def test_empty_knob_value_is_unset():
    """공백만 있는 값은 "설정 안 함" 이라 엔진 기본값으로 간다(값 공급 판정의 의미 승계)."""
    core = _additional_reviewer()
    for key in KNOB_KEYS:
        resolver_name, engine_default = _KNOB_RESOLVERS[key]
        resolve = getattr(core, resolver_name)
        assert resolve({key: "   "}) == engine_default
        assert core.knob_value_key({key: "   "}, key) is None


@pytest.mark.parametrize("broken", ["abc", "-1", "3.5"])
def test_broken_knob_value_falls_to_the_engine_default(broken):
    """깨진 값은 엔진 기본값으로 간다 — 공급 판정은 값의 존재이지 형식이 아니다 (T-0600 엣지)."""
    core = _additional_reviewer()
    for key in KNOB_KEYS:
        resolver_name, engine_default = _KNOB_RESOLVERS[key]
        assert getattr(core, resolver_name)({key: broken}) == engine_default
        # 값을 공급한 키는 여전히 그 키다(형식 오류가 공급 사실을 뒤집지 않는다).
        assert core.knob_value_key({key: broken}, key) == key


def test_engine_guidance_names_the_knob_keys():
    """상한/예산 차단 안내가 현재 키를 가르친다 — 안내는 채택자가 값을 고치는 유일한 접점이다."""
    core = _additional_reviewer()
    round_guidance = core._ROUND_LIMIT_GUIDANCE
    assert core.ADDITIONAL_REVIEWER_INCOMPLETE_ROUND_LIMIT_KEY in round_guidance
    assert "additional_reviewer.round_limit" not in round_guidance
    assert core.ADDITIONAL_REVIEWER_WAVE_BUDGET_KEY in core._WAVE_BUDGET_GUIDANCE


def test_internal_round_guidance_names_its_own_knob_key():
    """내부 축 거부 안내도 같은 규율이다 — 설정값·조정 키를 문구가 스스로 말한다."""
    delegate = _load("pm_delegate")
    assert (delegate.INTERNAL_REVIEW_ROUNDS_MAX_KEY
            == f"delegate.{delegate.INTERNAL_REVIEW_ROLE}.rounds_max")
    guidance = delegate._INTERNAL_ROUND_REFUSAL
    assert "{knob}" in guidance and "{default}" in guidance
    assert "상한 3" not in guidance                  # 값 재타이핑 금지(설정값 주입)
    # 신키는 레지스트리에도 있다 — 채택자가 적으면 '모르는 키' 경고가 나면 안 된다.
    conf_module = _load("local_conf")
    assert delegate.INTERNAL_REVIEW_ROUNDS_MAX_KEY in conf_module.KNOWN_KEYS
    assert conf_module.unknown_keys(
        {delegate.INTERNAL_REVIEW_ROUNDS_MAX_KEY: "5"}) == ()
