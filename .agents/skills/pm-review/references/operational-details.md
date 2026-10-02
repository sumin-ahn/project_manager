# /pm-review 상황별 운영 상세

> 아래 절은 상시 카드에서 분리한 원문이다. 해당 상황에서만 읽는다.

**Claude PM은 이 문서의 `additional_reviewer.py` 실 실행 커맨드를 Bash 툴로 호출할 때
`timeout: 29300000`(ms)을 반드시 명시한다.** 이는 CLI `--timeout`(리뷰어 벽시계)이 아니라
호출층 Bash 툴 파라미터다. Windows 진입 규약은 상시 `SKILL.md`에 남아 있다.

## 수렴 게이트와 라운드 상한

라운드/wave 상한은 **기계적 anti-loop 정지**다. 리뷰마다 비용을 다시 묻는 절차는 없다.

engine은 `--gate <T-NNNN>`별 라운드 장부를 세고, 실행 전에 **수렴 형상**을 먼저 판정해 거부한다(rc=4). 판정 입력은 장부의 must-fix 추이뿐이라 PM 판단이 들어갈 자리가 없다.

### 게이트 회계 (자동 유도·opt-out)

게이트를 붙이는 일도 PM 기억에 맡기지 않는다. `--gate` 없는 `--ticket` 실행은 **게이트를 그 티켓으로 자동 유도**해 라운드를 예약·기록하고(stderr 유도 1줄), 명시 `--gate` 는 항상 우선한다.

- 회계 밖 자문 실행은 **`--no-gate` 명시 opt-out** 으로만 연다. 그 실행은 장부에 남지 않고 라운드·wave 예산도 쓰지 않는다. 표기는 두 자리다 — 호출 전 stderr 경고(조건형: 호출되면 회계 밖)와 실행 뒤 판정 블록의 `게이트:` 줄(확정형: `(없음 — 회계 밖·라운드 장부 미기록)`).
- `--gate` 와 `--no-gate` 동시 지정은 rc=1 로 거부된다(한 실행이 기록과 무기록을 동시에 뜻할 수 없다). 조회면(`--rounds-report`)·처분면(`--resolve-gate`)에서는 회계가 없으므로 무시 경고 목록에 든다.
- 티켓·게이트 없는 `--paths` 실 호출은 rc=1 로 거부된다. `--gate <게이트>` 또는 명시적 `--no-gate` 중 하나를 반드시 선택한다(`--dry-run` 미리보기는 예외).

기본값을 "기록"으로 둔 근거는 실측이다. `--ticket` 만으로 돈 라운드가 하루 8건 넘게 장부에 0건으로 남아, 반려 must-fix 가 릴리즈 차단 표면(`board.py livegate record`)에 도달하지 못했다.

- **라운드 상한 2회**(`local.conf`의 `additional_reviewer.rounds_max`) — 기록 라운드가 상한에 닿으면 must-fix 잔존과 무관하게 차단한다. 잔존 여부는 차단 사유 라벨만 가른다(`cap-unresolved` / `cap-reached`).
- **발산 조기 차단** — 직전 라운드보다 must-fix가 늘면 상한 도달을 기다리지 않고 그 자리에서 차단한다. 줄지 않고 평탄한 형상(상한 3 override 예: 3→2→2)은 조기 차단이 아니라 상한에서 걸린다.

미완(미마감) 라운드 상한(기본 2, `additional_reviewer.incomplete_rounds_max`)도 함께 본다 — 판정 없이 끝난 호출을 세는 축이다. 호출 전 예약이라 반복 타임아웃으로 우회할 수 없다. 호출 횟수만 세던 판정 라운드 상한은 수렴 축과 범위가 겹쳐 제거됐다.

1. rc=4 → `--rounds-report`로 장부를 먼저 읽는다. 확인 항목: *지금까지 라운드 수 · 라운드별 수락/기각 판정 요지 · must-fix 추이*.
2. 라운드를 연장하지 않고 현재 티켓을 정지해 사용자에게 보고한다. 새 티켓·분할·재설계로 잔여를 넘기지 않는다.
3. 직전 must-fix의 해소 확인 전용 표면도 core 5단계의 추가 사람 라운드로 사용하지 않는다.
4. 중대한 scope 확대나 독립적 사용자 게이트가 필요하면 board를 쓰지 않고 선택을 요청한다.
5. 종결(수락/override) 시 판정 근거를 log에 박제하고 게이트를 닫는다. 연쇄 결함이 이어졌다면 과설계 신호로 설계 재질문도 올린다(cascade-defects-signal-overengineering).

게이트별 상한과 별개로 **wave(세션) 총예산**이 있다 — 실 호출 누적이 한도(기본 24, `local.conf`의 `additional_reviewer.wave_budget`)에 닿으면 rc=4로 거부한다. 안내 문구가 `--ack-wave`를 지목하면 게이트 상한이 아니라 이 축이다. **재개 ack가 남은 축은 이것 하나**이며(보고서 확인 → 같은 scope의 정상 수렴이면 PM 자율 ack·예산 리셋·판단 근거는 log), wave 예산을 열어도 게이트의 수렴 판정은 그대로 닫혀 있다.

라운드 수렴 상황 보고에는 `--rounds-report`를 쓴다 — 게이트별 라운드 수·라운드별 판정(verdict)·must-fix 수·처분 상태·wave 소비를 표로 dump하는 read-only 조회면이다(`--gate T-NNNN`으로 단일 게이트 한정·`--ticket`/`--paths`를 주면 기록면과 같은 앵커로 해소).

## 잔여 must-fix 의 처분 선언

상한으로 종결된 게이트에 must-fix 가 남았으면 게이트를 닫기 전에 그 잔여의 처분을 장부에 선언한다. 선언되지 않은 잔여는 릴리즈를 막는다 — `board.py livegate record`/`check` 가 실행 전에 차단하고 우회 플래그는 없다(그 자리에 "사소하니 넘어간다"는 판단이 들어가 실사고가 났다).

```bash
python3 .project_manager/tools/additional_reviewer.py --resolve-gate <게이트> --pm-verified
```

- 처분은 현재 티켓 fix의 판정 표면과 기계 확인을 재검증하는 `pm-verified` 하나다.
- **최종 라운드가 "반려인데 must-fix 건수 미상"(판정 무효 라운드)이어도 잔여로 취급돼 릴리즈가 막힌다** — 확인 못 한 것을 0건으로 접지 않는다. 처분 선언은 동일하게 가능하다.
- `--resolve-gate` 는 `--dry-run` 과 조합하면 rc 1 로 거부된다(부작용 0 계약 — 장부를 쓰는 선언면이다).
- 선언은 **그때의 라운드에 결속한다** — 선언 뒤 새 반려 라운드가 오면 미처분으로 되돌아가므로 새 잔여로 다시 선언한다. suggestion 은 처분 대상이 아니다(이월 허용).
- 처분 상태는 `--rounds-report`의 처분 열(미처분/pm-verified/무대상)로 확인해 보고한다.

## 호출과 실패

- 채널을 켜고 끄는 스위치는 없다 — 부르면 돈다. `--dry-run` 은 로컬 미리보기라 하네스를 부르지 않는다.
- 리뷰어 실패(인증/한도/네트워크/타임아웃) → exit 1 + `FALLBACK_INTERNAL`(내부 code-reviewer 폴백 신호).
- **빈 diff는 무조건 exit 1**이며 우회 플래그가 없다. 안내대로 worktree cwd + `--paths` / `git add` 후 재실행한다.

## 결과 판정

- `종합 판정: 통과` → 추가 리뷰어 게이트 통과.
- `must-fix 감지` → 반려; must-fix 해소 후 재검토.
- `판정 불명확` → PM 확인 필요.
- `FALLBACK_INTERNAL` → 내부 code-reviewer로 폴백.
- must-fix 전부 해소 → 묶음 종결(`ticket_finish.py --cluster <C-이름>`) → push 하여 추가 리뷰어 게이트를 종료한다(내부 reviewer 축은 `pm_delegate.py` 가 소유).
