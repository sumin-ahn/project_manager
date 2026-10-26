#!/usr/bin/env python3
"""추가 리뷰어 래퍼 — 추가 리뷰어(하네스) 어댑터 CLI.

사람 역할 이름은 **추가 리뷰어(additional reviewer)** 다 — 팀에 한 명 더 붙는 리뷰어다.
설정 키는 `additional_reviewer.*`(대상 tuple·상한)이고, 모듈·신규 raw
식별자도 `additional_reviewer`다. 개칭 전 `external_review` raw는 과거 감사물
판독에서만 받는다.

인자 파서와 실행 배선은 `pm_engine.cli.additional_reviewer` 가 소유한다. 이 파일은 콘솔 설정과
설치 앵커 Home 으로 그 CLI 를 부르는 진입이다.

사용:
    python3 .project_manager/tools/additional_reviewer.py [옵션]

동작:
  git diff <base> -- <paths> 추출
  (프로젝트 맥락 헤더 +) diff 결합 → 표준 프롬프트 생성
  추가 리뷰어 실행 (additional_reviewer.* 원자 tuple · 읽기 권위 code-reviewer 고정)
  출력에서 판정(통과/반려)·must-fix 파싱
  결과 요약 stdout + 원문 파일 저장 (기본 = **소유 PM 홈**의 `.project_manager/.local/review/`
    + 공유 장부 `raw_outputs.json` · --output-dir 로 격리)

채널 스위치는 없다:
  - 추가 리뷰어는 developer·architect 와 같이 **부르면 도는 역할**이다. 켜고 끄는 설정 키도, 실행
    전 동의 확인도 없다 — 저장소 내용을 모델 API 로 보내는 것은 PM 이 파일을 읽는 순간 이미
    일어나는 이 프레임워크의 정상 동작이라 특별 취급할 근거가 없다. 호출 횟수 상한(라운드·wave
    예산)만 남는다. 단 빈/공백 diff 는 dry-run 포함 무조건 exit 1 (false-green 원천 차단).

종료 코드/신호:
  - 리뷰어 실패(인증/한도/네트워크/타임아웃·구조화 wire 에서 최종 회신 미추출) → exit 1 +
    stdout 에 FALLBACK_INTERNAL (= 내부 code-reviewer 서브에이전트로 폴백하라는 신호).
    회신을 못 뽑은 실행은 rc=0 으로 끝났어도 **리뷰를 받지 못한 실행**이라 같은 축으로 내린다 —
    wire 원문은 원문 파일에 그대로 보존되고 raw 장부는 정상 마감된다.
    중단 판정: 주 = **무진행**(마지막 진행 출력 이후 침묵), 백스톱 = 벽시계. 값은 별도 상수가
    아니라 **해소된 추가 리뷰어 대상의 하네스 프로필**(`watchdog`·위임 채널과 동일 테이블)에서 오고, 배포별
    조정은 local.conf `harness.<reviewer>.idle_timeout`/`.wall_timeout`(legacy
    `additional_reviewer.idle_timeout`/`additional_reviewer.timeout` 도 계속 유효)·일회성은
    `--idle-timeout`/`--timeout`. 어느 쪽으로 중단되든 그 시점까지 받은 출력은 원문 파일에
    보존한다(전량 폐기 금지).
  - must-fix 감지 → exit 1
  - 통과 → exit 0
  - 수렴-형상 차단(--gate 별) → exit 4 (실행 전 거부). 라운드 장부의 must_fix 추이로 판정한다:
    라운드 수가 상한(local.conf additional_reviewer.rounds_max·기본 2)에 닿았거나 직전 라운드보다 must_fix 가
    늘었으면(발산·조기 차단) 라운드를 더 쓰지 않고 현재 티켓을 정지해 사용자에게 보고한다.
  - 미완 라운드 상한 도달(--gate 별) → exit 4 (실행 전 거부·전용 rc). 같은 게이트로 판정 없이
    끝난 호출 2회
    (local.conf additional_reviewer.incomplete_rounds_max)를 채우면 이후 실행을 기계 차단한다. 성격은
    **무한 루프 차단(anti-loop pause)**이다 — 연장 승인(`--ack-rounds`)은 폐지됐고, 호출하면
    아무것도 하지 않고 거부한다.
  - wave 예산 소진 → exit 4 (같은 rc·같은 실행 전 거부). 게이트별 상한과 **별개로** wave 단위
    총 라운드 예산(local.conf additional_reviewer.wave_budget·기본 24)을 두어 티켓 수 × 라운드 상한
    으로 비용이 무한 확장되는 구조를 막는다 — 재개는 같은 규율의 `--ack-wave`(예산 리셋).

설계:
  - 어댑터 seam: canonical `additional_reviewer.{harness,model,reasoning}` tuple 을 `contract` 의
    3-harness 드라이버로 해소한다. 그 tuple 이 없으면 대상이 없어 fail-loud 다 — 모델을 고정하지
    않는 실행 경로는 남기지 않는다.
  - 도메인 외부화: 프로젝트 맥락은 `.project_manager/review_context.local.md`(인스턴스 소유)
    가 있으면 주입, 없으면 generic 헤더. 엔진 도구엔 도메인 콘텐츠 0.
  - subprocess DI (run_fn 매개변수) — 테스트에서 mock 주입 가능.
  - 구조화 리뷰어 호출의 권한은 code-reviewer read 축으로 고정하며 설정으로 쓰기 권한을 올릴 수 없다.
  - 추가 리뷰어는 위임과 **같은 실행 조건**을 받는다: cwd 는 검토 대상 저장소(diff_root)이고 env 는
    위임 채널과 같은 seam(`pm_delegate.build_env`)이 조립한다. 채널이 다르다고 다른 권한·다른 홈을
    주지 않는다. 회신 본문의 품질 검사(`detect_output_contamination`)는 그대로 남는다 — 엇갈린 판정·
    옛 raw 인용이 섞이면 이번 판정이 이 리뷰의 것이라는 보장이 없어 '판정 불명확'으로 접는다.
  - 라운드 상한 기계 차단: 추가 리뷰어 호출(유료)이 무한 반복되지 않게 `--gate <T-NNNN>`
    별 라운드 장부(`.project_manager/.local/review_rounds.json`·per-clone·git-ignored)에 실 호출을
    count 하고, 수렴 축(must_fix 추이)과 미완 축이 상한에 닿으면 실행 *전에* 거부(exit 4)한다.
    호출 횟수만 세던 판정 라운드 상한은 제거됐다 — 수렴 판정이 같은 범위를 더 정확히 본다.
    "몇 라운드나 돌았나"라는
    PM 자의 집계를 기계 장부로 대체하는 것이고([[mechanize-dont-instruct-llm]]), 수렴 여부 판정도
    같은 장부의 must_fix 추이가 소유한다(사람의 "이번엔 진짜 수렴 중" 판단을 입력으로 쓰지 않는다).
    게이트를 붙이는 것도 기억에 맡기지 않는다 — `--gate` 미지정 `--ticket` 실행은 게이트를 그
    티켓으로 **자동 유도**하고(명시 `--gate` 가 항상 우선), 회계 밖 자문 실행은 명시
    opt-out `--no-gate` 로만 연다(그 실행은 무기록·비회계를 loud 로 표기한다).
  - 라운드별 산출 장부 + wave 예산: 같은 장부에 라운드마다 산출(`rounds` — 판정 rc·must-fix 수)을
    append 하고, 전 게이트 합계 호출을 wave 단위로 센다(`wave` 절). 라운드 수만으로는 "그 라운드가
    실결함을 냈는가"를 기계로 확인할 수 없어 게이트 심도 대비 비용 적정성 판단이 PM 자기보고에
    의존했다. 조회는 `--rounds-report`(호출 없는 읽기 전용 표). 기록은 무조건이고 hard 거부는
    예산 축 하나뿐이다.
"""
import os
import sys
from pathlib import Path

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
from pm_engine.cli import additional_reviewer
from pm_engine.infra import console, pm_paths

REPO = Path(__file__).resolve().parents[2]


def main(argv=None):
    console.configure_console_utf8()
    return additional_reviewer.main(pm_paths.home_at(REPO), argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
