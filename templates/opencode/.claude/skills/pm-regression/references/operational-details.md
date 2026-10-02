# /pm-regression 상황별 운영 상세

> 아래 절은 상시 카드에서 분리한 원문이다. 해당 상황에서만 읽는다.

## 수집 개수 증감 (직전 회귀 대비 표시)

rc0 인데 스위트 *일부만* 수집되는 파손(cwd/pythonpath)은 rc 로는 안 잡힌다. FULL 게이트는
직전 회귀 기록의 수집수와 이번 수집수의 증감을 회귀 줄에 함께 낸다.

```
regression: pass (rc=0) (직전 13323 · +177 · 1.3% ⚠) @ e4f2436f
```

- **막지 않는다.** 증가든 감소든 rc 를 바꾸지 않는다. 변화율 1% 이상이면 `⚠` 로 눈에 띄게
  적을 뿐이고, 적절한 변경인지는 사람이 판단한다. 설정 키는 없다.
- 비교할 직전 기록이 없거나(첫 실행) 수집수를 못 읽으면 증감 문장이 비어 있다(추정 없음).
- FULL 게이트 기록 한정이다 — `--ticket`/`--touches` 스코프 실행은 기록을 읽지도 쓰지도 않는다.
- 수집 0(rc=5)은 red다. 개발 core의 비-pytest rc0·수집 미상 동작은 유지한다. 선언 플랫폼이 있는 릴리즈는 core와 플랫폼의 양의 정수 수집수를 요구하며 미검증 결과로 livegate를 통과하지 않는다.

## 티켓별 빠른 루프

구현 중 자기 ticket 관련 테스트만 실행한다(빠른 피드백이며 push 게이트 아님).

```bash
python3 .project_manager/tools/board.py regression run --ticket T-pay-001   # touches → pytest -k
```

## red 처리

- 백그라운드 red 알림 → 해당 ticket인지 확인하고 고친다. 이미 done이면 done→open 복구 CLI가 없으므로 follow-up ticket으로 처리한다.
- **flaky**면 `.project_manager/quarantine.txt`에 test node id를 추가한다(회귀가 `--deselect`). 격리는 임시이므로 근본 원인 ticket도 발행한다.
