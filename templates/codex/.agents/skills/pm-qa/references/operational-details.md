# $pm-qa 상황별 운영 상세

> 아래 절은 상시 카드에서 분리한 원문이다. 해당 상황에서만 읽는다.

### 4. (선택) 프로젝트 evidence summary

운영 데이터(cron 로그·paper-run audit 등)가 있으면 인스턴스 overlay로 최근 cycle 요약을 덧붙인다. 없으면 noise를 피하려 skip하며 구체 경로는 인스턴스 소유다.

### 5. PM report

호출자가 다음 markdown을 합산 출력한다.

```
## PM 통합 검증 report (YYYY-MM-DD HH:MM)
- 회귀: N / N 통과 (또는 K failed — <첫 fail 1줄>)
- lint: clean (또는 N advisory / 차단 M)
- git: <clean | N files modified> · branch <name> · HEAD <SHA short>
- 최근 commit: <SHA> <subject>
- (선택) evidence: <last cycle summary>

## 결정 (PM 손)
- 회귀 통과 + lint 차단 0 + working tree clean → wave 종료/시작 OK.
- 회귀 red → baseline fix 또는 dev 재작업.
- working tree dirty → wave 종결 commit 누락·재확인.
```

## 불변

- fail-soft가 아니다. red는 즉시 보고하고 후속 단계를 중단한다.
- 1번 회귀와 3번 git은 독립이므로 multiple Bash 병렬 호출할 수 있다.
- 비즈니스 로직 없는 thin 합성이며 실제 차단 검증은 push gate(pre-push hook)가 보증한다.
- evidence는 선택·인스턴스 소유다.
- 참고: [[pm-regression]] · [[pm-wave-finish]] · [[pm-bootstrap]]; backbone CLI `python3 .project_manager/tools/board.py {lint,regression}`.
