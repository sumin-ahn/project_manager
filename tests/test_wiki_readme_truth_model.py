"""wiki/README.md 현재-진실 doc 모델 가드 (T-0107).

`wiki/README.md` 는 wiki 의 "디렉토리 의미 단일 정의처"인데 manifest 밖이라 루트·양
template 3 copy 를 수동으로 맞춰야 한다. T-0102/0103 이 진입문서·pm_role·status 기계를
ADR-0022/0023 현재-진실 모델로 옮겼으나 이 README 는 옛 모델(테스트 합계표·"architecture 는
구현상태 안 둠")로 남아 ADR 과 모순했다 — "redefine 후 기존 자산 갱신 누락" 클래스의 또 다른
인스턴스. 이 가드로 못박는다:

  (1) 3-copy byte-동일 — 한 copy 만 고치고 다른 걸 놓치는 *이 클래스* 재발 방지.
  (2) 현재-진실 모델 단언 — README 가
        (a) architecture.md 를 "현재-아키텍처 단일 진실"/"① live"/부트스트랩 맥락으로 서술,
        (b) `domain/` 디렉토리를 언급 (ADR-0018),
        (c) status 서술에 "테스트 합계표"/"테스트 수는 여기" 류 stale 문구가 없음 (ADR-0023).

권위 기준 = `pm_role.md` §부트스트랩·§"현재-진실 vs 히스토리" (이미 정합). stdlib only.
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

README_COPIES = [
    REPO / ".project_manager" / "wiki" / "README.md",
    REPO / "templates" / "claude_code" / ".project_manager" / "wiki" / "README.md",
    REPO / "templates" / "opencode" / ".project_manager" / "wiki" / "README.md",
]


def _read_canonical() -> str:
    """루트 wiki/README.md 본문 (모델 단언의 검사 대상)."""
    canonical = README_COPIES[0]
    assert canonical.exists(), f"루트 wiki/README.md 없음: {canonical}"
    return canonical.read_text(encoding="utf-8")


# ── (1) 3-copy byte-동일 파리티 ──────────────────────────────────────────────


def test_readme_three_copies_byte_identical():
    """루트·claude_code·opencode 의 wiki/README.md 가 byte-동일.

    manifest 밖(수동 정합)이라 한 copy 만 고치고 다른 걸 놓치는 drift 가 *이 클래스*.
    """
    for path in README_COPIES:
        assert path.exists(), f"wiki/README.md 없음: {path}"

    canonical_bytes = README_COPIES[0].read_bytes()
    for path in README_COPIES[1:]:
        assert path.read_bytes() == canonical_bytes, (
            f"wiki/README.md 가 byte-동일이 아니다: {path} != {README_COPIES[0]} — "
            "한 copy 만 고치고 다른 copy 를 놓쳤다 (3 copy 수동 정합 필요)."
        )


# ── (2a) architecture.md = 현재-아키텍처 단일 진실 ───────────────────────────


def test_readme_architecture_is_current_truth():
    """README 가 architecture.md 를 현재-아키텍처 단일 진실·① live·부트스트랩 맥락으로 서술."""
    text = _read_canonical()
    assert "현재-아키텍처 단일 진실" in text, (
        "wiki/README.md 가 architecture.md 를 '현재-아키텍처 단일 진실'로 서술하지 않는다 "
        "(ADR-0022)."
    )
    assert "① live" in text, (
        "wiki/README.md 가 architecture 의 '① live' (코드 실측) 축을 서술하지 않는다 (ADR-0022)."
    )
    assert "부트스트랩 #1" in text, (
        "wiki/README.md 가 architecture.md 를 부트스트랩 #1 로 서술하지 않는다 (ADR-0022)."
    )


# ── (2b) domain/ 디렉토리 언급 (ADR-0018) ────────────────────────────────────


def test_readme_mentions_domain_directory():
    """README 가 domain/ 디렉토리(살아있는 프로젝트 지식)를 언급 (ADR-0018)."""
    text = _read_canonical()
    assert "domain/" in text, (
        "wiki/README.md 가 domain/ 디렉토리를 언급하지 않는다 (ADR-0018 wiki 3축)."
    )
    assert "ADR-0018" in text, (
        "wiki/README.md 의 domain/ 설명이 근거 ADR-0018 을 명시하지 않는다."
    )


# ── (2c) status 서술 stale 문구 부재 (ADR-0023 judgment-only) ────────────────

STALE_STATUS_PHRASES = [
    "테스트 합계표",
    "테스트 합계의 단일 진실",
    "테스트 수는 여기",
    "헤더는 스칼라",
    "헤더 비대",
]


def test_readme_no_stale_status_phrases():
    """status 서술에 옛 모델(합계표·스칼라·헤더 비대) 문구가 없다 (ADR-0023 judgment-only)."""
    text = _read_canonical()
    for phrase in STALE_STATUS_PHRASES:
        assert phrase not in text, (
            f"wiki/README.md 에 stale status 문구 '{phrase}' 가 남아 있다 — "
            "status.md = judgment-only (테스트 수는 pytest 실측·박제 안 함·ADR-0023)."
        )


def test_readme_status_is_judgment_only():
    """README 가 status.md 를 judgment-only(판정·비고)로 서술하고 테스트 수=pytest 실측 명시."""
    text = _read_canonical()
    assert "judgment-only" in text, (
        "wiki/README.md 가 status.md 를 judgment-only 로 서술하지 않는다 (ADR-0023)."
    )
    assert "board.py regression" in text, (
        "wiki/README.md 가 테스트 수의 단일 진실을 board.py regression(pytest) 실측으로 "
        "서술하지 않는다 (ADR-0023)."
    )
