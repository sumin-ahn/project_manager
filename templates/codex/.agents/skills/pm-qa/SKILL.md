---
name: pm-qa
description: "개발 통합 검증 게이트. core 회귀 결과 재사용, 필요 시 백그라운드 실행, lint·git 확인. 다른 트리 개발을 계속하고 완료·통합 시 결과 확인. 추가 플랫폼 전체는 릴리즈 livegate. Triggers: 통합 검증, qa, 회귀 확인, wave 종료 검증, baseline 측정, pm-qa."
audience: pm-internal
---

# $pm-qa — 통합 검증

개발 core 회귀 결과, lint 및 Git 상태를 확인한다. 추가 플랫폼 전체 검증은 릴리즈 livegate가 소유한다.

환경별 명령 문법은 부트스트랩의 "현재 환경" 표시에 맞춰 [Windows 안내](../references/environment-windows.md) 또는 [Linux/macOS 안내](../references/environment-posix.md)를 참조한다.

상황별 보고와 wrapper 규약은 [references/operational-details.md](references/operational-details.md)를 읽는다.

## 실행 순서

이 절은 개발 후보의 검증 결과를 확인하는 순서를 정의한다.

1. 같은 HEAD·명령·작업공간의 완료된 전체 core green이 있으면 재사용한다. 기존 회귀 결과를 확인한다.

   ```bash
   python3 .project_manager/tools/board.py regression check
   ```

2. 결과가 없거나 오래됐으면 기존 하네스 백그라운드 기능으로 전체 core를 실행한다. 검증 트리의 코드와 HEAD를 고정하고, 다른 작업 트리의 독립 개발은 계속한다.

   ```bash
   python3 .project_manager/tools/board.py regression run --final
   ```

3. task가 작업공간을 여럿 보유하면 회귀 명령에 `--task <task> --repo <repo> --slot <N>`을 붙여 자기 실행 대상을 지정한다. task 귀속과 선택한 HEAD·브랜치·test_cmd를 결속하며, 모호·미소유·idle·쓰기용 readonly 대상은 부작용 전에 거부한다. bootstrap·handoff와 finish는 task 단독 입력을 유지한다.
4. `board.py lint`와 `git status -s`, `git log --oneline -5`를 확인한다. 회귀와 독립적인 검사는 병렬로 실행할 수 있다.
5. 해당 후보의 완료·통합 시점에 회귀 결과를 확인한다. 회귀(core)가 red/미실행이면 해당 후보의 후속 완료·통합은 중단한다. pending 동안 다른 트리의 개발을 중단하지 않는다.

## 플랫폼 검증

이 절은 개발 회귀와 릴리즈 검증의 경계를 정의한다.

- 개발 회귀는 현재 플랫폼의 전체 test_cmd만 실행한다. `qa.platforms`의 wrapper를 호출하지 않는다.
- 개발 보고의 `platform[<name>]`은 릴리즈 검증 예정 상태이다. 개발 중 플랫폼 미실행은 개발 완료 차단 사유가 아니다.
- 릴리즈 `livegate record`는 같은 커밋의 core green을 확인한 뒤 선언 플랫폼 전부를 실제 실행한다. 플랫폼 red/미실행은 라이브 테스트 전에 릴리즈를 차단한다. `livegate check`는 같은 rev의 전체 결과와 현재 선언·명령을 재검증한다.
- 새 플랫폼 선택 옵션이나 별도 작업 큐를 만들지 않는다. Windows 전체 검증은 모든 구현·통합 후 릴리즈 직전에 실행한다.

## 인접 스킬

각 스킬의 실행 책임을 정리한다.

- [[pm-regression]]: 백그라운드 core 회귀와 표적 개발 루프.
- [[pm-wave-finish]]: 검증 결과를 확인한 후보의 종결.
- [[pm-release]]: 선언 플랫폼 전체와 라이브 릴리즈 게이트.
- [[pm-bootstrap]]: 세션 진입과 현재 결과 확인.
