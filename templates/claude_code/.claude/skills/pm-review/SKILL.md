---
name: pm-review
description: "codex 외부 교차검증 게이트 실행 규율 명령어化 — worktree cwd 앵커 + stage 선행(git add) + --paths/--ticket 경로 핀. backbone = external_review.py(opt-in). 앵커는 명시 selector 기반 diff_root 해소(등록 슬롯 lease 장부 자동 파생)·소유 PM 홈 해소 불가 무인자 실행은 rc=1 차단. 외부 전송은 과금·load-bearing 게이트에만(사소 docs 는 self/내부 리뷰). Triggers: 'codex 게이트', '외부 교차검증', 'external review 돌려', 'pm-review'."
audience: pm-internal
---

# /pm-review — codex 외부 교차검증 게이트

backbone은 `.project_manager/tools/external_review.py`(opt-in)이며, PM이 외부 게이트를 실행할 때 사용한다.

> **Windows 노트:** 아래 `python3 …` 커맨드는 Windows 에서 런처 **`py`**(예: `py -3.12 …`)를 1순위로
> 쓴다 — `python3`/`python` 은 WindowsApps 가짜 shim(Git Bash 에선 Permission denied)일 수 있다.
> **PowerShell 5.x 는 `&&` 체이닝 미지원**(ParseError·실측) — `cd X && cmd` 대신 도구의 workdir
> 파라미터나 명령 분리로 실행한다. (Linux/macOS 는 `python3` 그대로.)

## 사용 시점

- dev→reviewer 내부 게이트를 통과한 **실질 코드·설계**(엔진/알고리즘·비파괴 동작·파서·보안·ADR/설계)에 codex 외부 교차검증을 실행한다.
- **사소한 docs/prose/자명한 편집에는 실행하지 않는다**. self/내부 리뷰로 끝낸다.
- codex 라운드가 길어지면(>3~4) 근거를 갖춰 수락/override 또는 재질문할지 판단한다.

## 라운드 상한

engine은 `--gate <T-NNNN>`별 라운드 장부를 세고 한도(기본 4, `local.conf`의 `external_review_round_limit`) 초과분을 실행 전에 거부한다(rc=4). 호출 전 예약되며 전송-전 실패만 환불되어 반복 타임아웃으로 우회할 수 없다. 미완(미마감) 라운드는 별도 상한(기본 2, `DEFAULT_INCOMPLETE_ROUND_LIMIT`)이 먼저 걸린다.

1. rc=4 → 즉시 사용자에게 보고하고 대기한다. 보고 내용: *지금까지 라운드 수 · 라운드별 수락/기각 판정 요지 · 남은 findings의 실질성 평가 · 계속/종결/설계-재질문 권고*.
2. 사용자가 계속을 승인한 경우에만 `--ack-rounds`를 붙여 재개한다(+한도만큼 창이 열린다). **승인 없이 `--ack-rounds` 금지**. 엔진은 기록만 하고 승인 게이트는 이 규율이다.
3. 종결(수락/override) 시 판정 근거를 log에 박제하고 게이트를 닫는다. 연쇄 결함이 이어졌다면 과설계 신호로 설계 재질문도 올린다(cascade-defects-signal-overengineering).

게이트별 상한과 별개로 **wave(세션) 총예산**이 있다 — 실 전송 누적이 한도(기본 24, `local.conf`의 `external_review_wave_budget`)에 닿으면 rc=4로 거부한다. 안내 문구가 `--ack-wave`를 지목하면 게이트 상한이 아니라 이 축이다. 재개 규율은 `--ack-rounds`와 동일하다(사용자 승인 후에만 `--ack-wave`·예산 리셋). 두 승인은 서로를 열지 않으며 동시 소진이면 둘 다 필요하다.

라운드 수렴 상황 보고에는 `--rounds-report`를 쓴다 — 게이트별 라운드 수·라운드별 판정(verdict)·must-fix 수·wave 소비를 표로 dump하는 read-only 조회면이다(`--gate T-NNNN`으로 단일 게이트 한정·`--ticket`/`--paths`를 주면 기록면과 같은 앵커로 해소).

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

```bash
# ticket touches 로 경로 결정 (권장 — DoD/touches 와 정합)
python3 .project_manager/tools/external_review.py --ticket T-NNNN --gate T-NNNN

# 또는 경로/base 직접 지정
python3 .project_manager/tools/external_review.py --base main --paths src/ tests/ .project_manager/tools/
```

- `--gate T-NNNN`: 게이트 ticket 표식(로깅용).
- `--adr ADR-NNNN …`: 관련 ADR을 프롬프트에 포함.
- 외부 전송 없는 미리보기: `--dry-run`.
- 비활성 상태 1회 강제: `--force`.

## 외부 전송과 실패

- 코드 diff가 외부로 전송되므로 기본 OFF. `local.conf`의 `external_review_enabled=true`로 opt-in한다. 꺼져 있으면 actual 호출은 no-op(exit 0)이고 `--dry-run`은 항상 허용된다(로컬 미리보기·미전송).
- 리뷰어 실패(인증/한도/네트워크/타임아웃) → exit 1 + `FALLBACK_INTERNAL`(내부 code-reviewer 폴백 신호).
- **빈 diff는 무조건 exit 1**이며 우회 플래그가 없다. 안내대로 worktree cwd + `--paths` / `git add` 후 재실행한다.

## 결과 판정

- `종합 판정: 통과` → 외부 게이트 통과.
- `must-fix 감지` → 반려; must-fix 해소 후 재검토.
- `판정 불명확` → PM 확인 필요.
- `FALLBACK_INTERNAL` → 내부 code-reviewer로 폴백.
- must-fix 전부 해소 → 완료 부기(`ticket_finish`) → push하여 dev→reviewer+codex 이중 게이트를 종료한다.
