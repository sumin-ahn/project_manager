---
name: pm-review
description: "codex 외부 교차검증 게이트 실행 규율 명령어化 — worktree cwd 앵커 + stage 선행(git add) + --paths/--ticket 경로 핀. backbone = external_review.py(opt-in). adopter#0 PM 홈 앵커면 external_review 가 빈 diff false-green 을 loud 차단·worktree 재지정 안내. 외부 전송은 과금·load-bearing 게이트에만(사소 docs 는 self/내부 리뷰). Triggers: 'codex 게이트', '외부 교차검증', 'external review 돌려', 'pm-review'."
audience: pm-internal
---

# /pm-review — codex 외부 교차검증 게이트 (실행 규율 명령어化)

> PM 이 세션마다 손으로 재조립하던 **codex 외부 교차검증 게이트 실행 규율**을 4요소로
> 비즈니스 로직 0 — backbone `external_review.py`(opt-in) 를 얇게
> 감싼다. 규율 셋(worktree cwd 앵커·stage 선행·`--paths` 경로 핀)을 한 trigger 로 고정해 손 재조립을
> 폐지한다. backbone = `.project_manager/tools/external_review.py`.

> **Windows 노트:** 아래 `python3 …` 커맨드는 Windows 에서 런처 **`py`**(예: `py -3.12 …`)를 1순위로
> 쓴다 — `python3`/`python` 은 WindowsApps 가짜 shim(Git Bash 에선 Permission denied)일 수 있다.
> **PowerShell 5.x 는 `&&` 체이닝 미지원**(ParseError·실측) — `cd X && cmd` 대신 도구의 workdir
> 파라미터나 명령 분리로 실행한다. (Linux/macOS 는 `python3` 그대로.)

## 청중 (audience)

**pm-internal** — PM 에이전트가 이중 게이트(dev→reviewer + codex)의 **외부 게이트**를 태울 때 invoke.
셋업(user-entrypoint)의 확장이 아니라 **운영중-관리** 스킬이다.

## 사용 시점 (trigger)

- dev→reviewer(내부 게이트) 통과분이 **실질 코드·설계**(엔진/알고리즘·비파괴 동작·파서·보안·ADR/설계)면
  codex 외부 교차검증을 태운다(codex-cross-review).
- **사소한 docs/prose/자명한 편집엔 돌리지 않는다** — 과금·느림. self/내부 리뷰로 끝낸다.
- codex 라운드가 길어지면(>3~4) 수렴 판단 — 근거 있으면 수락/override(외부 리뷰어도 항상 옳지 않다).

## 라운드 상한 (기계 게이트)

수렴 판단은 PM 자의에 맡기지 않는다 — engine 이 `--gate <T-NNNN>` 별 라운드 장부를 세고
**한도(기본 4·local.conf `external_review_round_limit`) 초과분을 실행 전 거부**한다(rc=4·호출 전
예약·전송-전 실패만 환불이라 반복 타임아웃도 우회 불가):

1. rc=4 가 뜨면 **그 자리에서 사용자에게 보고하고 대기**한다 — 보고 template: *지금까지 라운드 수 ·
   라운드별 수락/기각 판정 요지 · 남은 findings 의 실질성 평가 · 계속/종결/설계-재질문 권고*.
2. 사용자가 계속을 승인한 경우에만 `--ack-rounds` 를 붙여 재개한다(+한도만큼 창이 열린다). **승인
   없이 --ack-rounds 금지** — 엔진은 기록만 하고 승인 게이트는 이 규율이다.
3. 종결(수락/override)이면 판정 근거를 log 에 박제하고 게이트를 닫는다. 연쇄 결함이 이어졌다면
   과설계 신호로 설계 재질문을 함께 올린다(cascade-defects-signal-overengineering).

## 실행 규율 (3요소 · 순서대로)

> 아래 셋을 지켜야 게이트가 **참 결과**를 낸다. 어긋나면 false-green(빈 diff 통과)·stale 결과가 난다
> (adopter0-gates-use-worktree-canonical).

### (a) worktree cwd 앵커 — canonical 사본에서 실행

실 코드 변경이 있는 worktree cwd 의 canonical `external_review.py` 로 실행한다. adopter#0
형상에선 PM 홈의 import 사본이 stale 이라 REPO 앵커가 PM 홈을 가리키면 diff 가 비어 **false-green**
이 난다.

```bash
# canonical 코드 worktree 에서 실행 (PM 홈 import 사본 금지).
cd <worktree-canonical-경로>     # 예 work/project_manager_1
```

- external_review 는 PM 홈 앵커 + `--paths` 미지정을 **loud 차단**하고 worktree 재지정 경로를 안내한다
  그 안내가 뜨면 `cd <worktree>` 후 재실행한다.

### (b) stage 선행 — 신규 파일 `git add`

external_review 는 `git diff` 기반이라 **untracked(신규) 파일을 못 본다** — 검토 전에 스테이징한다
(stage-before-external-review).

```bash
git add <신규/변경 경로>     # untracked 파일이 diff 에 포함되게
```

### (c) 경로 핀 — `--paths` 또는 `--ticket`

리뷰 대상을 명시 핀한다. ticket 의 `touches` 로 자동 결정하려면 `--ticket`, 직접 지정하려면 `--paths`.

```bash
# ticket touches 로 경로 결정 (권장 — DoD/touches 와 정합)
python3 .project_manager/tools/external_review.py --ticket T-NNNN --gate T-NNNN

# 또는 경로/base 직접 지정
python3 .project_manager/tools/external_review.py --base main --paths src/ tests/ .project_manager/tools/
```

- `--gate T-NNNN` — 게이트 ticket 표식(로깅용). `--adr ADR-NNNN …` — 관련 ADR 을 프롬프트에 포함.
- 미리보기(외부 전송 없음): `--dry-run`. 비활성 상태 1회 강제: `--force`.

## 외부 전송 opt-in

- 코드 diff 가 *외부로 전송*되므로 기본 OFF. local.conf `external_review_enabled=true` 로 opt-in.
  꺼져 있으면 actual 호출은 no-op(exit 0)이고 `--dry-run` 은 항상 허용(로컬 미리보기·미전송).
- 리뷰어 실패(인증/한도/네트워크/타임아웃) → exit 1 + `FALLBACK_INTERNAL`(내부 code-reviewer 폴백 신호).
- **빈 diff 는 무조건 exit 1**(false-green 원천 차단) — 우회 플래그 없음. 안내대로 worktree
  cwd + `--paths` / `git add` 후 재실행한다.

## 결과 판정

- `종합 판정: 통과` → 외부 게이트 통과. `must-fix 감지` → 반려(must-fix 해소 후 재검토).
- `판정 불명확` → PM 확인 필요. `FALLBACK_INTERNAL` → 내부 code-reviewer 로 폴백.
- must-fix 다 해소 → 완료 부기(`ticket_finish`) → push (이중 게이트 dev→reviewer+codex 종료).

## 결정

- **외부 리뷰는 과금·load-bearing 게이트에만** — 라이브 실호출 대신 규율 경로(cwd·stage·paths)를 고정하고,
  사소 docs 는 self/내부 리뷰.
- **worktree cwd + `--paths` 규율을 스킬로 codify** — 손 재조립 폐지·false-green/stale 원천 차단
  external_review 의 PM 홈 앵커 게이트가 백스톱.
- **청중 = pm-internal**.

## 참고

- backbone: `.project_manager/tools/external_review.py`(외부 리뷰어 어댑터·opt-in).
- 도메인 단일진실: dual-gate-review(이중 게이트 dev→reviewer+codex).
- ADR: (명령어化 4요소·청중) · (external_review opt-in) · (스킬 단일 소비).
- 관성/전례: adopter0-gates-use-worktree-canonical(빈 diff false-green·stale pin) ·
  stage-before-external-review(untracked git add) · codex-cross-review(load-bearing 게이트에만).
