"""status/DoD 문서군이 ADR-0023(status judgment-only) 모델과 정합함을 한 곳에서 못박는다.

ADR-0023 은 status.md 에서 derivable 테스트 숫자(헤더 scalar·합계표·소계)를 제거했다. 그런데
이 결정 후 *기존 자산*(wiki index·playbook·skill·status_done·lite DoD 예시)이 한동안 "테스트
합계표"·"스칼라 갱신" 같은 폐기된 모델 토큰을 남겨 채택자를 오도했다(T-0103/0107/0108/0109 가
순차 정리). 이 가드는 그 클래스 전체의 재발을 *출하·운영 문서군*에서 한 번에 차단한다.

토큰은 구체적(`테스트 합계표`·`스칼라 갱신`)으로 둔다 — 광범위 금지어(`합계표`·`스칼라` 단독)는
status.md 모듈 행의 *역사적 비고*(과거 ticket 이 한 일 서술)와 충돌해 오탐을 낸다.
"""

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# 루트 도그푸딩 + 양 출하 템플릿의 status/DoD 문서군.
_ROOTS = [
    REPO / ".project_manager",                              # 루트 도그푸딩
    REPO / "templates" / "claude_code" / ".project_manager",
    REPO / "templates" / "opencode" / ".project_manager",
]
STATUS_DOCS = [r / "wiki" / "status.md" for r in _ROOTS]
STATUS_DONE_DOCS = [r / "wiki" / "status_done.md" for r in _ROOTS]
README_DOCS = [r / "wiki" / "README.md" for r in _ROOTS]
PLAYBOOK_DOCS = [r / "wiki" / "pm_playbook.md" for r in _ROOTS]
LITE_DOCS = [
    REPO / "templates" / "claude_code" / "CLAUDE.lite.md",
    REPO / "templates" / "opencode" / "AGENTS.lite.md",
]

ALL_DOCS = STATUS_DOCS + STATUS_DONE_DOCS + README_DOCS + PLAYBOOK_DOCS + LITE_DOCS

# ADR-0023 으로 폐기된 모델 토큰 — 어떤 status/DoD 문서에도 없어야 한다.
FORBIDDEN_TOKENS = ("테스트 합계표", "스칼라 갱신")


@pytest.mark.parametrize("doc", ALL_DOCS, ids=lambda p: str(p.relative_to(REPO)))
def test_status_doc_has_no_retired_scalar_tokens(doc):
    """status/DoD 문서군에 ADR-0023 폐기 토큰(테스트 합계표·스칼라 갱신)이 없다.

    live(루트)·scaffold(템플릿) 무관하게 어떤 status/DoD 문서도 폐기 모델을 서술하면 안 된다.
    (3-copy byte-파리티는 단언하지 않는다 — 루트 `status.md`/`status_done.md` 는 *live 프로젝트
    데이터*라 generic 템플릿 scaffold 와 by-design 다르고, done 모듈 이동 워크플로가 그 발산을
    정상으로 만든다. README 파리티는 그것이 generic scaffold 라 별 가드에서 단언한다.)
    """
    # live status.md/status_done.md 는 *프로젝트 dev-state* — 제품/홈 분리 시 PM 홈(②)에만
    # 존재하고 제품 트리(①)엔 없을 수 있다. 부재는 검사대상 없음이라 skip(가드 의도 = "status
    # 문서가 *있으면* 폐기토큰 없어야"·존재 단언은 별 scaffold-완전성 가드의 몫).
    if not doc.is_file():
        pytest.skip(f"문서 부재(live dev-state 분리 가능): {doc.relative_to(REPO)}")
    text = doc.read_text(encoding="utf-8")
    for token in FORBIDDEN_TOKENS:
        assert token not in text, (
            f"{doc.relative_to(REPO)} 에 ADR-0023 으로 폐기된 '{token}' 모델 서술이 남음 — "
            f"status 는 judgment-only(테스트 수=pytest 실측·history=log)")
