---
name: pm-regression
description: "비차단 백그라운드 회귀 — full 테스트를 run_in_background 로 pre-warm + 완료 알림, red 면 ticket 플래그. push 게이트(pre-push 훅)가 green 검증. dev 빠른 루프는 --ticket touches 스코프. Triggers: '회귀 돌려', '백그라운드 테스트', 'regression', 'pm-regression'."
audience: pm-internal
---

# /pm-regression — 비차단 백그라운드 회귀

> **Windows 노트:** 아래 `python3 …` 커맨드는 Windows 에서 런처 **`py`**(예: `py -3.12 …`)를 1순위로
> 쓴다 — `python3`/`python` 은 WindowsApps 가짜 shim(Git Bash 에선 Permission denied)일 수 있다.
> **PowerShell 5.x 는 `&&` 체이닝 미지원**(ParseError·실측) — `cd X && cmd` 대신 도구의 workdir
> 파라미터나 명령 분리로 실행한다. (Linux/macOS 는 `python3` 그대로.)

- 회귀는 전체 suite 이며 green 만 push한다(pre-push 훅: `board.py regression check`).
- full 회귀는 이 skill로 백그라운드 pre-warm하고, dev 루프는 `--ticket` 스코프(advisory)를 쓴다.

## 백그라운드 full 회귀

작업 한 단락 종료 또는 push 전에 하니스 background 로 실행하고 PM 은 다른 일을 계속한다.

```bash
# Bash run_in_background: true 로 호출. 완료되면 하니스가 세션을 재호출(알림).
python3 .project_manager/tools/board.py regression run
```

- green → 다음 push 즉시 통과.
- red → `regression check` 가 push 차단 → 원인을 고친다.
- 결과는 per-clone `.project_manager/.local/regression.json`에 HEAD 키로 기록된다.

## 수집 하한 (부분수집 false-green 차단·선택)

rc0 인데 스위트 *일부만* 수집되는 파손(cwd/pythonpath)은 rc 로는 안 잡힌다. per-clone
`local.conf` 에 하한을 선언하면 FULL 게이트가 그 미만 수집을 fail 로 기록해 push 를 막는다.

```conf
# .project_manager/local.conf — 회귀가 도는 트리(worktree)의 conf 가 기준
regression_min_collected=7000   # 기본 0 = 가드 off. 자기 수집수보다 여유 있게 낮춰 잡는다.
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
