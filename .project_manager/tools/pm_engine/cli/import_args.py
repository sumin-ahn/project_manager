"""pm_import — PM 프레임워크 import 단일 진입 커맨드 (--new = PM 홈 생성 / --into = 기존 프로젝트 임베드).

현행 채택 플로우(`docs/manual-import.md` 의 수동 longhand: `cp -r` + sed +
`board.py init` + 손)의 **기계 단계**(결정적·무LLM)를 1 커맨드로 대체하고, 그 위에
sed 로 못 채우는 **자유서술 placeholder** 채움(하니스 헤드리스 구동·opt-in)을 얹는다.

사용:
    pm_import.py (--into <기존프로젝트> | --new <프로젝트>)   # 모드 택1
                 [--harness <harness[,harness...]|all>]      # 어댑터 집합 (default: all)
                 --weight  {full,lite}                        # 무게축 (default: full)
                 [--from <프레임워크-checkout>]               # 소스 (default: 이 repo 루트)
                 [--name <표시이름>]                          # {{PROJECT_NAME}} (default: 디렉토리명)
                 [--fill {auto,manual}]                       # 자유서술 채움 (default: manual)
                 [--fill-harness {@REGISTERED_FILL_HARNESSES@}] # 구동 하니스 (default: --harness)
                 [--dry-run]                                  # 적용 없이 계획만 출력(fill 미호출)

동작:
  소스 = <--from>/templates/<harness>/ 트리(엔진 + 어댑터)를 대상으로 복사한다.
  콤마 선택 또는 `all`이면 선택된 어댑터 트리를 집합으로 병합 복사한다.
  복사 후 operational placeholder 를 sed 치환하고 `board.py init`(무인자)을 호출한다
  — 그 호출이 areas.md 에 이 clone 의 repo 행을 빈 prefix(=none 카테고리)로 등록한다.
    - sed 대상 = {{PROJECT_NAME}}·{{PROJECT_TAGLINE}}·{{PROJECT_ROOT}}·{{PY}}·{{TEST_CMD}}·{{DATE}}.
    - 엔진 문서(wiki/pm_role.md·pm_playbook.md)는 sed 제외 — local.conf 가 런타임 해소.
    - 자유서술 3종({{PROJECT_CONSTRAINTS}}·{{PROTECTED_PATHS}}·{{USER_GATE_ITEMS}})은 보존(아래 fill).
  board init·local.conf 동기화 직후 **fill 단계**가 자유서술 placeholder 를 처리한다:
    - --fill manual(기본): 하니스 미구동, placeholder 를 `<!-- TODO: ... -->` 로 표시(채택자가 손으로).
    - --fill auto: 대상 repo 분석 프롬프트로 하니스(claude -p / opencode run --format json /
      codex exec --json)를
      헤드리스 구동해 placeholder 값 + (해당 시) CLAUDE.md/pm_role.local.md 초안을 *제안*한다.
      생성물은 제안일 뿐 — 적용은 사용자 리뷰 전제(비가역 회피). --dry-run 이면 실 하니스를
      호출하지 않고 fill *계획*(채울 대상 토큰·결정된 harness·opt-in 게이트 상태)만 출력한다
      (파일 미변경·비용 0 — opt-in 게이트상 dry-run 에서 실호출 금지).
  --into: 기존 충돌 파일은 중앙 디렉토리 .pm_import_backups/<DATE>/<relpath> 에 백업 후 덮음
          (비파괴). 단 git 이 추적 중이고 미변경인 파일은 백업 생략(git 이 복원). --new:
          디렉토리 생성 + git init.

결정:
  - 독립 pm_import.py (board.py 비대화). stdlib only.
  - idempotent — 재실행 시 백업하고 안전. --dry-run 은 파일시스템 미변경(plan/apply 분리).
  - --weight lite 는 진입 파일 선택만 영향. 어댑터의 `X.lite.md`(예 CLAUDE.lite.md·
    AGENTS.lite.md)를 dst `X.md` 로 rename 배치하고 full `X.md`·원본 `*.lite.md` 는 제외한다.
    full(기본)은 모든 `*.lite.md` 를 제외하고 full 진입(X.md)만 깐다.
  - **적용 단계의 파일 단위 제외는 rc 0 이고 신호는 stderr 요약이다.** 계획을 통과한 대상이 적용
    중에 교체·삭제되거나 백업 자리가 막혀 빠지면 그 파일만 건너뛰고 `⚠️ …` 요약을 stderr 로 낸다
    (백업 못 하는 파일은 고치지 않는다). 나머지 설치·추가는 그대로 완료되므로 비0 은 "아무것도
    안 됐다"는 잘못된 신호가 된다 — 백업 자리가 막힌 공유 문서를 재렌더에서 빼고 rc 0 으로 끝내는
    기존 처리와 같은 규칙이다. 계획 단계 위반은 반대다: 아직 아무것도 복사하지 않았으므로 전체를
    rc 1 로 멈춘다(부분 설치 0). dest 루트 자체 교체만 예외로 적용 중에도 즉시 전체 중단이다.
  - fill 실구동 게이트: 하니스 실구동은 토큰·모델 비용 → 기본 OFF.
    **실호출은 환경변수 PM_IMPORT_LIVE_HARNESS=1 AND --fill auto 동시 충족 시만.** 둘 중 하나라도
    없으면 실 runner 를 호출하지 않는다(CI·기본 테스트는 stub). 회사 배포(claude code 없음)는
    opencode 구동 경로 1급 — 혼합이면 등록 순서상 첫 가용 하네스를 택한다.

opt-in 실 e2e (CI 비포함 — 토큰·모델 비용 발생):
    1) 대상 하니스 바이너리 설치 확인 (@REGISTERED_FILL_BINARIES@ 중 선택한 것이 PATH 에 있어야 함).
    2) 환경변수와 플래그를 *동시* 지정해 실구동:
           PM_IMPORT_LIVE_HARNESS=1 pm_import.py --into <repo> --fill auto [--fill-harness opencode]
       - 둘 중 하나만 주면 실호출이 차단되고 stub/manual 로 폴백한다(안전).
    3) 출력된 자유서술 placeholder 값·초안은 *제안*이다 — 사람이 검토 후 손으로 반영한다
       (pm_import 는 자유서술 채움을 자동 확정하지 않는다). --dry-run 은 실 하니스를 호출하지
       않고 *fill 계획*(채울 토큰·harness·게이트 상태)만 미리 보여준다(파일 미변경).
"""
from __future__ import annotations

from pm_engine.domain import install_rules
import argparse

def _parse_harness_arg(value: str) -> tuple[str, ...]:
    """argparse 경계: 집합 파서 오류를 표준 CLI usage 오류로 바꾼다."""
    try:
        return install_rules.parse_harness_selection(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _cli_description() -> str:
    """설치 CLI 도움말(이 모듈 문서)의 fill 목록을 현재 registry 에서 렌더한다."""
    registered = tuple(install_rules.HARNESS_TEMPLATE_DIRS)
    return (__doc__ or "").replace(
        "@REGISTERED_FILL_HARNESSES@", ",".join(registered)
    ).replace(
        "@REGISTERED_FILL_BINARIES@",
        "·".join(f"`{harness}`" for harness in registered),
    )
