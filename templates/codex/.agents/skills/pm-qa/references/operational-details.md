# $pm-qa 운영 상세

상시 카드에서 분리한 원문을 현재 검증 단계에 맞춰 갱신한다. 개발 보고 형식과 릴리즈 wrapper의 기존 프로토콜을 정의한다.

## 개발 보고

core 결과, 선언 플랫폼의 실행 단계, lint 및 Git 상태를 보고한다.

```text
회귀(core): pass / fail / pending, 수집 N, HEAD <SHA>
platform[<name>]: 릴리즈 검증 예정 (개발 미실행)
lint: clean / advisory / 차단
git: clean / 변경 파일 수, branch, HEAD
최근 commit: <SHA> <제목>
```

- 회귀(core) red/미실행이면 해당 후보의 후속 완료·통합을 중단한다. pending 중 다른 작업 트리의 독립 개발은 계속한다.
- 같은 HEAD·명령·트리의 완료된 core green은 재사용한다. 결과가 없으면 백그라운드 실행하고 검증 트리의 코드와 HEAD를 동결한다.
- 개발 회귀는 core만 실행한다. 선언 플랫폼의 전체 검증은 릴리즈 livegate record가 소유한다.
- 운영 데이터가 있으면 인스턴스 overlay로 필요한 요약을 덧붙인다. evidence는 선택이며 경로는 인스턴스가 소유한다.

## 릴리즈 wrapper 규약

같은 커밋의 추가 플랫폼 결과를 검증하는 기존 프로토콜을 정의한다.

- board는 릴리즈 실행의 각 wrapper에 `PM_QA_PLATFORM=<name>`과 `PM_QA_EXPECTED_HEAD=<Git OID>`를 전달한다.
- wrapper는 stdout에 정확히 한 줄을 출력한다.

  ```text
  PM_QA_RESULT_V1={"platform":"<name>","head":"<Git OID>","status":"pass","collected":N}
  ```

- JSON member는 이 네 개만 허용하고 중복을 거부한다. 전달값과 같은 platform/HEAD, bool이 아닌 양의 정수 collected 및 rc0을 모두 요구한다. marker 부재·복수는 red다.
- wrapper가 만든 전송 번들·게스트 클론·호스트 임시물은 실행 시작에 정리한다. 중단된 실행의 잔여도 다음 실행 시작에 정리한다.
- 릴리즈 플랫폼 어느 행이든 red/미실행이면 라이브 테스트 및 릴리즈 후속 단계를 중단한다. 개발 core 결과와 별도 단계에서 생성된 같은 rev의 전체 플랫폼 성공을 요구한다.
- VM/SSH 구현은 인스턴스 wrapper가 소유한다. 스킬에는 머신별 경로·접속 정보를 싣지 않는다.

## 참고

관련 실행 스킬을 연결한다.

[[pm-regression]], [[pm-wave-finish]], [[pm-release]], [[pm-bootstrap]].
