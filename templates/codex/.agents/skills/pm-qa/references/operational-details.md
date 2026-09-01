# $pm-qa 상황별 운영 상세

> 아래 절은 상시 카드에서 분리한 원문이다. 해당 상황에서만 읽는다.

### 4. (선택) 프로젝트 evidence summary

운영 데이터(cron 로그·paper-run audit 등)가 있으면 인스턴스 overlay로 최근 cycle 요약을 덧붙인다. 없으면 noise를 피하려 skip하며 구체 경로는 인스턴스 소유다.

### 5. PM report

호출자가 다음 markdown을 합산 출력한다.

```
## PM 통합 검증 report (YYYY-MM-DD HH:MM)
- 회귀(core): N / N 통과 (또는 K failed — <첫 fail 1줄>)
- platform[<name>]: pass · collected N · HEAD <SHA short> (선언 platform마다 1행)
- lint: clean (또는 N advisory / 차단 M)
- git: <clean | N files modified> · branch <name> · HEAD <SHA short>
- 최근 commit: <SHA> <subject>
- (선택) evidence: <last cycle summary>

## 결정 (PM 손)
- 회귀 통과 + lint 차단 0 + working tree clean → wave 종료/시작 OK.
- 회귀(core) 또는 선언 platform 어느 행이든 red/미실행 → baseline fix 또는 dev 재작업.
- working tree dirty → wave 종결 commit 누락·재확인.
```

## 불변

- fail-soft가 아니다. red는 즉시 보고하고 후속 단계를 중단한다.
- `board.py regression run` 한 번이 core와 선언 platform 전부를 직렬 실행한다. 일부 platform만
  고르는 flag/재실행은 없고, 결과 marker·same-HEAD 판정은 board가 소유한다.
- board는 각 wrapper에 `PM_QA_PLATFORM=<name>`·`PM_QA_EXPECTED_HEAD=<Git OID>`를 전달한다.
  wrapper는 stdout에 정확히 한 줄의
  `PM_QA_RESULT_V1={"platform":"<name>","head":"<Git OID>","status":"pass","collected":N}`를
  출력한다. exact 4-key JSON, 전달값과 같은 platform/HEAD, bool 아닌 양의 정수 `collected`, rc0을
  모두 만족해야 green이며 marker 부재·복수·중복 member는 red다.
- **wrapper는 자기가 만든 것을 치운다.** 전송용 번들·게스트 클론·호스트 임시는 그 wrapper가
  만든 것이고 board는 그 자리를 모른다. 치우는 시점은 **실행 시작**이다 — 중단(kill·VM 다운)에서는
  종료 코드가 돌지 않으므로 "끝나면 지운다"는 그 경우를 닫지 못한다. 고정 부모 한 곳을 정하고
  매 실행의 첫 동작으로 그 안을 비운 뒤 새로 만든다. 이 조항이 없으면 회당 번들·클론이 그대로
  쌓여 검증 머신의 디스크가 마르고, 그 red 는 코드 결함이 아니라 정리 부재다.
- 1번 회귀와 3번 git은 독립이므로 multiple Bash 병렬 호출할 수 있다.
- 비즈니스 로직 없는 thin 합성이며 실제 차단 검증은 push gate(pre-push hook)가 보증한다.
- evidence는 선택·인스턴스 소유다.
- 참고: [[pm-regression]] · [[pm-wave-finish]] · [[pm-bootstrap]]; backbone CLI `python3 .project_manager/tools/board.py {lint,regression}`.
