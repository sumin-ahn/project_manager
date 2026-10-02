# /pm-release 상황별 운영 상세

> 아래 절은 상시 카드에서 분리한 원문이다. 해당 상황에서만 읽는다.

## 불변·보고

- 공개 main push는 대화 승인 + 보호훅/livegate 이중 안전을 유지한다(private만 자율). `PM_ALLOW_PROTECTED_PUSH=1`·`PM_SKIP_LIVE_GATE=1`은 PM이 스스로 쓰지 않는다(사용자 명시 OK의 escape hatch이며 환경 문제는 우회 사유가 아니다).
- livegate record는 `pytest -m release`(claude·opencode·codex 3 하네스)를 실측·기록하고 보호훅은 push HEAD의 green을 `livegate check`로 재확인한다(record=기록·check=소비).
- PM은 각 단계 stdout, 특히 livegate 수집 N과 `gh release view` 결과를 읽어 보고한다.
- main push 실행·CHANGELOG 문안·GitHub Release 노트는 PM/사용자가 확정·승인한다.
- backbone: `board.py livegate record`/`livegate check` · `pm_update`([[pm-update]]) · `git`/`gh`; 라이브 하네스 테스트는 `tests/test_pm_release_live.py`; 보호훅은 보호 브랜치 push 차단 + `livegate check` green을 요구한다(pm_role §릴리즈 절차).

## 플랫폼 검증 경계

이 절은 추가 플랫폼 전체 검증의 실행 시점을 정의한다.

개발 regression run/check는 core만 소비한다. 릴리즈 livegate record는 같은 rev의 core green 뒤 qa.platforms에 선언된 wrapper 전부를 실행한다. 플랫폼 red/미실행과 수집·HEAD·선언·명령·좌표 불일치는 라이브 테스트 전에 거부한다. livegate check는 현재 선언과 같은 rev의 전체 결과를 재검증한다. Windows 전체 검증은 구현·통합 완료 후 릴리즈 직전에 수행한다.
