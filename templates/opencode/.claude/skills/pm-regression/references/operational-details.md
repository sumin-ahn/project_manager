# /pm-regression 상황별 운영 상세

> 아래 절은 상시 카드에서 분리한 원문이다. 해당 상황에서만 읽는다.

## 수집 하한 (부분수집 false-green 차단·선택)

rc0 인데 스위트 *일부만* 수집되는 파손(cwd/pythonpath)은 rc 로는 안 잡힌다. per-clone
`local.conf` 에 하한을 선언하면 FULL 게이트가 그 미만 수집을 fail 로 기록해 push 를 막는다.

```conf
# .project_manager/local.conf — 회귀가 도는 트리(worktree)의 conf 가 기준
regression.min_collected=7000   # 기본 0 = 가드 off. 자기 수집수보다 여유 있게 낮춰 잡는다.
```

- FULL 게이트 한정이다 — `--ticket`/`--touches` 스코프 실행은 대상이 아니다(매칭분만 도는 게 정상).
- 하한 미만 = `partial-collection` · 요약행을 못 읽어 검증 불가 = `unverified-collection` 으로
  기록되고 `regression check` 가 사유와 함께 push 를 막는다. 하한을 올리면 옛 green 기록도 무효화된다.

## 티켓별 빠른 루프

구현 중 자기 ticket 관련 테스트만 실행한다(빠른 피드백이며 push 게이트 아님).

```bash
python3 .project_manager/tools/board.py regression run --ticket T-pay-001   # touches → pytest -k
```

## red 처리

- 백그라운드 red 알림 → 해당 ticket인지 확인하고 고친다. 이미 done이면 done→open 복구 CLI가 없으므로 follow-up ticket으로 처리한다.
- **flaky**면 `.project_manager/quarantine.txt`에 test node id를 추가한다(회귀가 `--deselect`). 격리는 임시이므로 근본 원인 ticket도 발행한다.
