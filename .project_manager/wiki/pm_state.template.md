---
title: PM State (dynamic handoff state)
created: {{DATE}}
updated: {{DATE}}
type: handoff-state
---

# PM State — 동적 핸드오프 상태

> ⚠️ **이 파일은 per-clone 로컬 (git-ignored) — `pm_state.template.md` 에서 `pm-init` 이 생성.**
> 담는 건 **이 clone/PM 의 연속성**(세션 window · 내 진행 · 다음 액션)뿐. **프로젝트-전역 진실
> (board·status·ADR·log)은 공유 채널에** — 여기 두면 다중 PM 에서 갈라진다.
>
> [`pm_role.md`](pm_role.md)(정적 운영 매뉴얼)에서 분리한 **휘발성 상태**. 매 핸드오프마다 바뀐다.
> PM 세션은 정적 매뉴얼(pm_role.md)을 매번 정독하지 않아도, 이 파일만 보면 "지금 어디까지 왔나"를 안다.
> 일부 절은 `/pm-handoff` (backbone `pm_handoff.py`)가 자동 갱신한다 — 아래 앵커·표 형식을 바꾸면 정규식도 같이.

## 세션 식별 (현재까지 사용된 이름)

> ⚠️ `/pm-handoff` skill (backbone `pm_handoff.py`) 가 이 표를 sliding window 로
> 자동 정리한다. 표 형식·앵커 (`## 세션 식별 (현재까지 사용된 이름)`) 를
> 바꾸면 backbone CLI 의 정규식도 같이 바꿔야 한다.
> 역할 네이밍 규칙(`pm` / 구현 세션 / orchestrator)은 [`pm_role.md`](pm_role.md) §"세션 식별 규칙" 참조.

최근 N 차 (sliding window, 기본 3 차):
<!-- /pm-handoff 가 자동 갱신 — 형식: "  - **N차** (YYYY-MM-DD · <wave_summary>): ..." -->
  - **1차** ({{DATE}} · 부트스트랩): 초기 PM 세션.
  - 이전 차 (PM 1차~1차) = `log/current.md` handoff entry 단일 진실.

## 진행 중인 의사결정

<!-- TODO: 현재 진행 중인 큰 결정·작업을 표로 추적. 핸드오프마다 갱신. -->

| 항목 | 상태 |
|---|---|
| (예시) board | done 0 / open 0 / claimed 0 / blocked 0 |

## 남은 작업 전체 그림

<!-- TODO: 게이트별·phase별 우선순위와 외부 대기 항목을 정리. 핸드오프마다 갱신.
예시 구조:
### 🟢 board open N — 즉시 진행 가능
### 🔵 외부 대기 (키 발급·배포 등)
### 🟡 pre-ADR (ideas/)
### 🔒 사용자 게이트 대기
-->
