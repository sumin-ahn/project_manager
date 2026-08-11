---
name: pm-review
description: "추가 리뷰어(additional reviewer) 교차검증 게이트 실행 규율 명령어化 — worktree cwd 앵커 + stage 선행(git add) + --paths/--ticket 경로 핀. backbone = external_review.py(opt-in). 앵커는 명시 selector 기반 diff_root 해소(등록 슬롯 lease 장부 자동 파생)·소유 PM 홈 해소 불가 무인자 실행은 rc=1 차단. 외부 전송은 load-bearing 게이트에만(사소 docs 는 self/내부 리뷰). Triggers: '추가 리뷰어', 'codex 게이트', '외부 교차검증', 'external review 돌려', 'pm-review'."
audience: pm-internal
---

# /pm-review — 추가 리뷰어 교차검증 게이트

backbone은 `.project_manager/tools/external_review.py`(opt-in)이며, PM이 추가 리뷰어 게이트를 실행할 때 사용한다. 역할 이름은 **추가 리뷰어(additional reviewer)** 이고 설정 키도 `additional_reviewer_enabled`·`additional_reviewer.*` 로 통일돼 있다. `external_review*` 는 모듈 파일 이름·raw 파일 접두처럼 이미 기록된 산출물에 박힌 기계 식별자와 외부 전송·격리·과금 축의 이름으로만 남는다. 개칭 전 구키를 쓰는 `local.conf` 는 실행 시 안내 1줄을 받는다(마이그레이션 절차는 README).

수신자 프로필은 `local.conf` 의 원자적 튜플 하나다.

```
additional_reviewer_enabled=true
additional_reviewer.harness=codex
additional_reviewer.model=gpt-5.6-sol
additional_reviewer.reasoning=max
```

opt-in 질문은 **첫 1회**뿐이다. `additional_reviewer_enabled=true` 는 설정된 외부 전송과 통상 과금에 대한 **지속 동의**이므로, PM은 리뷰마다·라운드 상한 재개마다 사용자에게 비용을 다시 묻지 않는다.

> **Windows 노트:** 아래 `python3 …` 커맨드는 Windows 에서 런처 **`py`**(예: `py -3.12 …`)를 1순위로
> 쓴다 — `python3`/`python` 은 WindowsApps 가짜 shim(Git Bash 에선 Permission denied)일 수 있다.
> **PowerShell 5.x 는 `&&` 체이닝 미지원**(ParseError·실측) — `cd X && cmd` 대신 도구의 workdir
> 파라미터나 명령 분리로 실행한다. (Linux/macOS 는 `python3` 그대로.)

## 사용 시점

- dev→reviewer 내부 게이트를 통과한 **실질 코드·설계**(엔진/알고리즘·비파괴 동작·파서·보안·ADR/설계)에 추가 리뷰어 교차검증을 실행한다.
- **사소한 docs/prose/자명한 편집에는 실행하지 않는다**. self/내부 리뷰로 끝낸다.
- 코드 리뷰 라운드는 **상한 2회**다. 2회로 안 닫히면 라운드를 더 쓰지 않고 재설계·티켓 분할로 전환한다.

## 수렴 게이트와 라운드 상한

라운드/wave 상한은 **기계적 anti-loop 정지**이지 비용 승인 게이트가 아니다. 비용 의사표시는 `additional_reviewer_enabled=true` 한 번으로 끝났다.

engine은 `--gate <T-NNNN>`별 라운드 장부를 세고, 실행 전에 **수렴 형상**을 먼저 판정해 거부한다(rc=4). 판정 입력은 장부의 must-fix 추이뿐이라 PM 판단이 들어갈 자리가 없다.

### 게이트 회계 (자동 유도·opt-out)

게이트를 붙이는 일도 PM 기억에 맡기지 않는다. `--gate` 없는 `--ticket` 실행은 **게이트를 그 티켓으로 자동 유도**해 라운드를 예약·기록하고(stderr 유도 1줄), 명시 `--gate` 는 항상 우선한다.

- 회계 밖 자문 실행은 **`--no-gate` 명시 opt-out** 으로만 연다. 그 실행은 장부에 남지 않고 라운드·wave 예산도 쓰지 않는다. 표기는 두 자리다 — 전송 전 stderr 경고(조건형: 전송되면 회계 밖)와 실행 뒤 판정 블록의 `게이트:` 줄(확정형: `(없음 — 회계 밖·라운드 장부 미기록)`).
- `--gate` 와 `--no-gate` 동시 지정은 rc=1 로 거부된다(한 실행이 기록과 무기록을 동시에 뜻할 수 없다). 조회면(`--rounds-report`)·처분면(`--resolve-gate`)에서는 회계가 없으므로 무시 경고 목록에 든다.
- 티켓·게이트 없는 `--paths` 실 전송은 rc=1 로 거부된다. `--gate <게이트>` 또는 명시적 `--no-gate` 중 하나를 반드시 선택한다(`--dry-run` 미리보기는 예외).

기본값을 "기록"으로 둔 근거는 실측이다. `--ticket` 만으로 돈 라운드가 하루 8건 넘게 장부에 0건으로 남아, 반려 must-fix 가 릴리즈 차단 표면(`board.py livegate record`)에 도달하지 못했다.

- **라운드 상한 2회**(`local.conf`의 `review_rounds_max`) — 기록 라운드가 상한에 닿으면 must-fix 잔존과 무관하게 차단한다. 잔존 여부는 차단 사유 라벨만 가른다(`cap-unresolved` / `cap-reached`).
- **발산 조기 차단** — 직전 라운드보다 must-fix가 늘면 상한 도달을 기다리지 않고 그 자리에서 차단한다. 줄지 않고 평탄한 형상(상한 3 override 예: 3→2→2)은 조기 차단이 아니라 상한에서 걸린다.

전송 횟수만 세는 축도 그대로 있다 — 판정 라운드 상한(기본 4, `additional_reviewer_round_limit`)과 미완(미마감) 라운드 상한(기본 2, `additional_reviewer_incomplete_round_limit`). 호출 전 예약이라 반복 타임아웃으로 우회할 수 없다.

1. rc=4 → `--rounds-report`로 장부를 먼저 읽는다. 확인 항목: *지금까지 라운드 수 · 라운드별 수락/기각 판정 요지 · must-fix 추이*.
2. **출구는 재설계·티켓 분할뿐이다.** 라운드를 연장하는 승인 플래그는 폐지됐다 — 옛 플래그를 붙여 호출하면 rc=1로 거부되고 아무것도 실행되지 않는다. 남은 지적은 다음 티켓의 목표로 옮긴다.
3. 직전 must-fix의 **해소 확인**만 필요하면 게이트당 **1회** `--confirm-fix`(확인 전용 라운드)를 쓴다. 이 라운드에서 나온 **신규 발견은 재설계 신호**로 보고하며 라운드를 잇는 근거가 아니다. 2회째는 거부된다. 이 예외는 **수렴 축에만** 열린다 — 전송 횟수 상한(판정·미완 라운드·wave 예산)은 열지 않는다.
4. 사용자에게 올리는 경우는 **비용이 아니라 판단**이다 — 중대한 scope 확대, 그 밖의 독립적 사용자 게이트 사유(미션·핵심 안전 경계·외부 게시). 이때는 위 확인 항목과 분할/재설계 권고를 함께 보고하고 대기한다.
5. 종결(수락/override) 시 판정 근거를 log에 박제하고 게이트를 닫는다. 연쇄 결함이 이어졌다면 과설계 신호로 설계 재질문도 올린다(cascade-defects-signal-overengineering).

게이트별 상한과 별개로 **wave(세션) 총예산**이 있다 — 실 전송 누적이 한도(기본 24, `local.conf`의 `additional_reviewer_wave_budget`)에 닿으면 rc=4로 거부한다. 안내 문구가 `--ack-wave`를 지목하면 게이트 상한이 아니라 이 축이다. **재개 ack가 남은 축은 이것 하나**이며(보고서 확인 → 같은 scope의 정상 수렴이면 PM 자율 ack·예산 리셋·판단 근거는 log), wave 예산을 열어도 게이트의 수렴 판정은 그대로 닫혀 있다.

라운드 수렴 상황 보고에는 `--rounds-report`를 쓴다 — 게이트별 라운드 수·라운드별 판정(verdict)·must-fix 수·처분 상태·wave 소비를 표로 dump하는 read-only 조회면이다(`--gate T-NNNN`으로 단일 게이트 한정·`--ticket`/`--paths`를 주면 기록면과 같은 앵커로 해소).

## 잔여 must-fix 의 처분 선언

상한으로 종결된 게이트에 must-fix 가 남았으면 게이트를 닫기 전에 그 잔여의 처분을 장부에 선언한다. 선언되지 않은 잔여는 릴리즈를 막는다 — `board.py livegate record`/`check` 가 실행 전에 차단하고 우회 플래그는 없다(그 자리에 "사소하니 넘어간다"는 판단이 들어가 실사고가 났다).

```bash
python3 .project_manager/tools/external_review.py --resolve-gate <게이트> --into <T-NNNN>     # 후속 티켓 재설계
python3 .project_manager/tools/external_review.py --resolve-gate <게이트> --fixed <근거 게이트>  # 코드로 해소
```

- **재설계(`--into`)는 면제가 아니다** — 대상 티켓이 done 이어야 릴리즈가 열리므로 잔여는 같은 릴리즈 안에서 소화된다. 자기 자신 지목은 거부된다.
- **해소(`--fixed`)는 근거 게이트를 지목한다** — 마지막 라운드가 통과로 끝난 장부 게이트(확인 전용 라운드 또는 후속 게이트)여야 하고, 그 게이트가 뒤이어 반려로 뒤집히면 릴리즈 시점 재검증에서 다시 막힌다. 근거의 자격은 장부 사실로 판정한다: 근거 라운드가 차단 게이트의 마지막 반려 **종료 후에 시작**됐고 대상 rev 지문이 달라야 하며, 시각은 UTC ISO 8601 엄격 파싱(비파싱·시각 미기록·구 라운드는 결속 불충분으로 거부)이다.
- **최종 라운드가 "반려인데 must-fix 건수 미상"(판정 무효 라운드)이어도 잔여로 취급돼 릴리즈가 막힌다** — 확인 못 한 것을 0건으로 접지 않는다. 처분 선언은 동일하게 가능하다.
- `--resolve-gate` 는 `--dry-run` 과 조합하면 rc 1 로 거부된다(부작용 0 계약 — 장부를 쓰는 선언면이다).
- 선언은 **그때의 라운드에 결속한다** — 선언 뒤 새 반려 라운드가 오면 미처분으로 되돌아가므로 새 잔여로 다시 선언한다. suggestion 은 처분 대상이 아니다(이월 허용).
- 처분 상태는 `--rounds-report` 의 처분 열(미처분/재설계→티켓/해소/무대상)로 확인해 보고한다.

## 실행 규율

아래 순서를 지킨다. 위반하면 false-green(빈 diff 통과) 또는 stale 결과가 난다.

### 1. worktree cwd 앵커

실 코드가 변경된 worktree cwd의 canonical `external_review.py`로 실행한다(엔진 갱신 동기 전 PM 홈 import 사본은 stale 일 수 있다). 앵커 해소는 명시 selector 기반이다 — `--ticket`/`--paths` 를 주면 엔진이 lease 장부에서 diff worktree 를 자동 파생하므로 PM 홈 cwd 에서도 올바른 슬롯으로 해소된다(provenance 첫 줄의 `diff_root` 로 확인).

```bash
# canonical 코드 worktree 에서 실행 (PM 홈 import 사본 금지).
cd <worktree-canonical-경로>     # 예 work/project_manager_1
```

소유 PM 홈을 해소할 수 없는 위치의 **무인자 실행은 rc=1 로 차단**된다(경고 후 진행 없음). 커밋만 된 변경으로 슬롯을 고르려면 `--base` 앵커를 명시한다. 복구 채널은 `--paths`(절대경로)·유효한 `--ticket` 이다.

### 2. stage 선행

external_review는 `git diff` 기반이라 untracked(신규) 파일을 보지 못한다. 검토 전에 스테이징한다.

```bash
git add <신규/변경 경로>     # untracked 파일이 diff 에 포함되게
```

### 3. 경로 핀

**Claude PM은 아래 실 실행 커맨드를 Bash 툴로 호출할 때 `timeout: 29300000`(ms)을 반드시
명시한다.** 이는 CLI `--timeout`(리뷰어 벽시계)이 아니라 호출층 Bash 툴 파라미터다.
`BASH_DEFAULT_TIMEOUT_MS=1800000`은 일반 무-파라미터 명령용이다. 그래서 **1800~3600초 구간**의
정상 리뷰는 엔진의 3600초 진단이 울리기 **전에 Bash DEFAULT 1800초가 먼저 종료**시킨다 — 무진행
판정도 부분 산출물 보존도 실행되지 않는다. 실측 사례에서 그런 벽시계 false-kill 하나가 271KB 짜리
정상 리뷰를 통째로 폐기했다.

ticket의 `touches`로 정하려면 `--ticket`, 직접 지정하려면 `--paths`로 리뷰 대상을 핀한다.
`--paths` 실 전송에는 게이트 지정(`--gate <게이트>`) 또는 명시적 `--no-gate`가 필요하다.

```bash
# ticket touches 로 경로 결정 (권장 — DoD/touches 와 정합·게이트는 이 티켓으로 자동 유도)
python3 .project_manager/tools/external_review.py --ticket T-NNNN

# 또는 경로/base 직접 지정 (실 전송 회계 선택: 여기서는 --gate)
python3 .project_manager/tools/external_review.py --base main --paths src/ tests/ .project_manager/tools/ --gate T-NNNN
```

- `--gate T-NNNN`: 게이트 표식 겸 라운드 장부 키. `--ticket` 실행에서는 자동 유도되므로 다른 이름을 쓸 때만 명시한다.
- `--no-gate`: 게이트 회계 opt-out(장부 미기록·예산 미소모·loud 표기). `--gate` 와 함께 쓰지 못한다.
- `--adr ADR-NNNN …`: 관련 ADR을 프롬프트에 포함.
- 외부 전송 없는 미리보기: `--dry-run`.
- 비활성 상태 1회 강제: `--force`.

## 외부 전송과 실패

- 코드 diff가 외부로 전송되므로 기본 OFF. `local.conf`의 `additional_reviewer_enabled=true`로 opt-in한다(첫 1회 질문·이후 지속 동의). 꺼져 있으면 actual 호출은 no-op(exit 0)이고 `--dry-run`은 항상 허용된다(로컬 미리보기·미전송).
- 리뷰어 실패(인증/한도/네트워크/타임아웃) → exit 1 + `FALLBACK_INTERNAL`(내부 code-reviewer 폴백 신호).
- **빈 diff는 무조건 exit 1**이며 우회 플래그가 없다. 안내대로 worktree cwd + `--paths` / `git add` 후 재실행한다.

## 결과 판정

- `종합 판정: 통과` → 외부 게이트 통과.
- `must-fix 감지` → 반려; must-fix 해소 후 재검토.
- `판정 불명확` → PM 확인 필요.
- `FALLBACK_INTERNAL` → 내부 code-reviewer로 폴백.
- must-fix 전부 해소 → 완료 부기(`ticket_finish`) → push하여 dev→reviewer+추가 리뷰어 이중 게이트를 종료한다.
